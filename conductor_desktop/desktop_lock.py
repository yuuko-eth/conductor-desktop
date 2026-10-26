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

Holder metadata (PID, start time, transport) is written to a sibling
``.json`` file via atomic rename, so spectators can produce a useful
error message without touching the locked file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import state_dir

log = logging.getLogger("conductor.mcp.lock")


DEFAULT_LOCK_NAME = "desktop.lock"


@dataclass
class HolderInfo:
    pid: int
    started_at: str
    transport: str

    def to_dict(self) -> dict:
        return {"pid": self.pid, "started_at": self.started_at, "transport": self.transport}

    @classmethod
    def from_dict(cls, d: dict) -> HolderInfo:
        return cls(
            pid=int(d.get("pid", 0)),
            started_at=str(d.get("started_at", "")),
            transport=str(d.get("transport", "")),
        )


class DesktopLock:
    """Process-level wrapper around a single advisory file lock.

    Not async-safe by itself — callers that try to acquire from multiple
    coroutines should serialize with an :class:`asyncio.Lock`.
    """

    def __init__(self, path: Path | str | None = None, *, transport: str = "stdio") -> None:
        if path is None:
            path = state_dir() / DEFAULT_LOCK_NAME
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(self.path.suffix + ".json")
        self._transport = transport
        self._fd: int | None = None
        # Re-entrant guard: try_acquire while already held is a no-op success.
        self._mu = threading.Lock()

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
            self._write_meta()
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

    def _write_meta(self) -> None:
        info = HolderInfo(
            pid=os.getpid(),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            transport=self._transport,
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
