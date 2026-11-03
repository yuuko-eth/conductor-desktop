"""Cross-process advisory lock for desktop input.

Only one ``conductor-desktop`` process per machine should be allowed to inject
input (clicks, keystrokes, drags, …) at a time. Read-only operations
(screenshots, describes, CDP read calls) remain unrestricted — they don't
collide on the desktop.

Mechanism: a regular file under ``~/.conductor-desktop/`` (beside the audit
logs) held with an OS-level advisory lock (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows).
Advisory locks are released by the kernel when the holding process exits,
so a crashed holder can't strand the desktop.

Holder metadata (PID, start time, transport, last activity, idle policy) is
written to a sibling ``.json`` file via atomic rename, so spectators can
produce a useful error message without touching the locked file.

The lock is held for as long as the holder wants it: the OS frees it only
when the holder lets go or exits, so nobody can take it from a live holder.
An idle holder lets go by itself (server.py's idle release, configured by
``CONDUCTOR_DESKTOP_LOCK_IDLE_RELEASE``), and whoever acts next takes it.

The pause switch (tray "Pause input") is a file beside the lock file, not
process state, so it stops every server sharing the lock, including one
that takes the lock after the pause was set.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import state_dir

log = logging.getLogger("conductor.mcp.lock")


DEFAULT_LOCK_NAME = "desktop.lock"
PAUSE_FILE_NAME = "input-paused"

# How often the holder rewrites its last-activity time; spectators only need
# it to the nearest few seconds.
_TOUCH_INTERVAL = 10.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class HolderInfo:
    pid: int
    started_at: str
    transport: str
    # Last tool call the holder served (ISO time), "" if never recorded.
    last_active: str = ""
    # Seconds of inactivity after which the holder lets go; 0 = never.
    idle_release: float = 0.0

    def to_dict(self) -> dict:
        return {"pid": self.pid, "started_at": self.started_at, "transport": self.transport,
                "last_active": self.last_active, "idle_release": self.idle_release}

    @classmethod
    def from_dict(cls, d: dict) -> HolderInfo:
        return cls(
            pid=int(d.get("pid", 0)),
            started_at=str(d.get("started_at", "")),
            transport=str(d.get("transport", "")),
            last_active=str(d.get("last_active", "") or ""),
            idle_release=float(d.get("idle_release", 0) or 0),
        )

    def idle_seconds(self) -> float | None:
        """Seconds since the holder's last recorded activity, if known."""
        try:
            then = datetime.fromisoformat(self.last_active or self.started_at)
        except ValueError:
            return None
        return max(0.0, (datetime.now(UTC) - then).total_seconds())


class PauseFlag:
    """The tray's "Pause input" switch, shared by every server on one lock.

    Same interface as the ``threading.Event`` it replaces (``is_set``, ``set``,
    ``clear``). A file, so a server that takes the lock later is paused too,
    and the pause survives the holder quitting: input stays stopped until
    someone resumes it from a tray, or deletes the file.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def is_set(self) -> bool:
        return self.path.exists()

    def set(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_now_iso() + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class DesktopLock:
    """Process-level wrapper around a single advisory file lock.

    Not async-safe by itself — callers that try to acquire from multiple
    coroutines should serialize with an :class:`asyncio.Lock`.
    """

    def __init__(self, path: Path | str | None = None, *, transport: str = "stdio",
                 idle_release: float = 0.0) -> None:
        if path is None:
            path = state_dir() / DEFAULT_LOCK_NAME
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(self.path.suffix + ".json")
        self.pause_path = self.path.with_name(PAUSE_FILE_NAME)
        self.idle_release = idle_release
        self._transport = transport
        self._fd: int | None = None
        self._started_at = ""
        self._last_touch = 0.0
        # Re-entrant guard: try_acquire while already held is a no-op success.
        self._mu = threading.Lock()
        self._listeners: list[Callable[[bool], None]] = []

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        """Call ``fn(held)`` whenever this process takes or lets go of the lock.

        Called on whichever thread acquired or released, outside the lock's
        own mutex; a listener that touches UI marshals to its UI thread.
        """
        self._listeners.append(fn)

    def _notify(self, held: bool) -> None:
        for fn in list(self._listeners):
            try:
                fn(held)
            except Exception:
                log.warning("desktop lock listener failed", exc_info=True)

    def touch(self) -> None:
        """Record activity in the metadata, at most every few seconds."""
        now = time.monotonic()
        with self._mu:
            if self._fd is None or now - self._last_touch < _TOUCH_INTERVAL:
                return
            self._last_touch = now
            try:
                self._write_meta(last_active=_now_iso())
            except OSError as e:
                log.debug("desktop lock: meta touch failed: %s", e)

    # ── public API ────────────────────────────────────────────────────

    def is_held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Attempt to take the lock without blocking.

        Returns True if this process now holds it (either freshly acquired
        or already held). Returns False if another live process holds it.
        """
        with self._mu:
            if self._fd is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _flock_nb(fd)
            except BlockingIOError:
                os.close(fd)
                return False
            except OSError as e:
                os.close(fd)
                log.warning("desktop lock: unexpected OSError on flock: %s", e)
                return False
            self._fd = fd
            self._started_at = _now_iso()
            self._last_touch = time.monotonic()
            self._write_meta(last_active=self._started_at)
        self._notify(True)
        return True

    def release(self) -> None:
        """Remove the holder metadata file, then drop the lock.

        In that order: once the lock is free, the next holder writes its own
        metadata, and an unlink after that would delete it. Safe to call
        multiple times.
        """
        with self._mu:
            if self._fd is None:
                return
            try:
                self.meta_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                _funlock(self._fd)
            except OSError as e:
                log.warning("desktop lock: unlock failed: %s", e)
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._notify(False)

    def read_holder_info(self) -> HolderInfo | None:
        """Read the holder metadata file, if it exists and parses.

        Returns None when no metadata is on disk (no holder ever wrote it,
        or holder released cleanly).
        """
        try:
            with open(self.meta_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            log.debug("desktop lock: meta unreadable (%s)", e)
            return None
        try:
            return HolderInfo.from_dict(data)
        except (TypeError, ValueError):
            return None

    # ── internals ─────────────────────────────────────────────────────

    def _write_meta(self, last_active: str = "") -> None:
        info = HolderInfo(
            pid=os.getpid(),
            started_at=self._started_at or _now_iso(),
            transport=self._transport,
            last_active=last_active,
            idle_release=self.idle_release,
        )
        # Atomic write via tempfile + os.replace, so spectators never read a
        # half-written file.
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".desktop.lock.", suffix=".json.tmp",
            dir=str(self.meta_path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(info.to_dict(), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.meta_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ── platform-specific flock helpers ───────────────────────────────────


if sys.platform == "win32":
    import msvcrt

    def _flock_nb(fd: int) -> None:
        # Lock one byte at offset 0; LK_NBLCK is non-blocking exclusive.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            # Windows raises generic OSError; map "would block" to BlockingIOError
            # so callers can distinguish contention from real failures.
            import errno
            if e.errno in (errno.EACCES, errno.EDEADLK):
                raise BlockingIOError(str(e)) from e
            raise

    def _funlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _flock_nb(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _funlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
