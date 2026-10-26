"""Append-only audit log: one JSON line per tool call, one file per day.

Who drove this desktop, when, with which tool, and did it work. The MCP
server is a remote-control surface for a real machine, so the operator needs
a record independent of whatever the agent says it did — for incident
review, for "what touched that file at 02:00", and because an agent's own
account of its actions is not evidence.

**Design constraints, in order:**

1. *Never break a tool call.* Every entry point swallows its own errors. A
   full disk, a read-only home directory or a bad ``CONDUCTOR_DESKTOP_AUDIT_DIR``
   degrades to "no audit log", never to a failed click. After
   ``_MAX_FAILURES`` write errors the writer disables itself and says so
   once.
2. *Don't write secrets by default.* Tool arguments carry whatever the agent
   typed, which includes passwords, tokens and clipboard contents. The
   default ``redacted`` policy records an argument's name and the *shape* of
   its value, and replaces the values most likely to be secret with
   ``<redacted:N chars>``. ``full`` is opt-in and documented as unsafe.
3. *Survive several servers.* A holder and its spectators are separate
   processes sharing one log. Lines are written with a single ``write()`` in
   ``O_APPEND`` mode and kept short, so interleaving is safe in practice on
   POSIX; each line carries its ``pid`` and ``session`` so they can be told
   apart afterwards.

Files are ``audit-YYYY-MM-DD.jsonl`` (UTC) under
``~/.conductor-desktop/logs`` by default. Old files are deleted on startup
according to ``CONDUCTOR_DESKTOP_AUDIT_RETENTION_DAYS``; see https://docs.conductor-mcp.io/security/audit-log/
for the retention and cleanup story.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import state_dir

log = logging.getLogger("conductor.mcp.audit")

#: Argument names whose *values* are replaced by a shape under the default
#: ``redacted`` policy. These are the ones that carry typed text, page
#: script, shell input or clipboard contents — i.e. where a password or a
#: token actually shows up. Matched case-insensitively on the exact name.
SENSITIVE_ARGS: frozenset[str] = frozenset({
    "text", "value", "content", "js", "element_js", "expression", "command", "cmd",
    "script", "password", "secret", "token", "key_text", "data",
})

#: Under ``redacted``, non-sensitive string values are kept but truncated to
#: this many characters — enough to identify a target ("Save", "#chart",
#: "Calculator") without turning the log into a transcript.
_REDACTED_STR_CAP = 120

#: Under ``full``, the cap is looser but still bounded: an audit line is a
#: record, not a payload store.
_FULL_STR_CAP = 2000

#: Give up after this many consecutive write failures.
_MAX_FAILURES = 5

VALID_POLICIES = ("none", "redacted", "full")


def default_dir() -> Path:
    """Where logs go when ``CONDUCTOR_DESKTOP_AUDIT_DIR`` is unset.

    One hidden directory in the user's home on every platform. Deliberately
    not the per-OS convention (``~/Library/Logs``, ``%LOCALAPPDATA%``): an
    operator who has to find these under time pressure, possibly over SSH on
    a host they don't normally use, benefits more from one path that is the
    same everywhere than from native tidiness.
    """
    return state_dir() / "logs"


def _shape(value: Any, *, sensitive: bool, policy: str) -> Any:
    """One argument value, reduced according to the policy."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if policy == "full":
            return value if len(value) <= _FULL_STR_CAP else value[:_FULL_STR_CAP] + "…"
        if sensitive:
            return f"<redacted:{len(value)} chars>"
        return value if len(value) <= _REDACTED_STR_CAP else value[:_REDACTED_STR_CAP] + "…"
    if isinstance(value, (list, tuple)):
        if sensitive and policy != "full":
            return f"<redacted:{len(value)} items>"
        return [_shape(v, sensitive=False, policy=policy) for v in value[:20]]
    if isinstance(value, dict):
        return {
            str(k): _shape(v, sensitive=str(k).lower() in SENSITIVE_ARGS, policy=policy)
            for k, v in list(value.items())[:20]
        }
    return f"<{type(value).__name__}>"


def redact(kwargs: dict[str, Any], policy: str) -> dict[str, Any]:
    """Apply the argument policy to one call's keyword arguments.

    ``none`` drops them, ``redacted`` (default) keeps names and reduced
    values, ``full`` keeps values up to a cap. Positional arguments are not
    recorded: every MCP tool is called by keyword, so in practice there are
    none, and a bare list of values is far less useful in a log than names.
    """
    if policy == "none" or not kwargs:
        return {}
    return {
        str(k): _shape(v, sensitive=str(k).lower() in SENSITIVE_ARGS, policy=policy)
        for k, v in kwargs.items()
    }


def _sensitive_strings(value: Any, *, sensitive: bool) -> list[str]:
    """Every string a sensitive argument carries, nested ones included."""
    if isinstance(value, str):
        return [value] if sensitive else []
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _sensitive_strings(v, sensitive=sensitive)]
    if isinstance(value, dict):
        return [s for k, v in value.items()
                for s in _sensitive_strings(
                    v, sensitive=sensitive or str(k).lower() in SENSITIVE_ARGS)]
    return []


def scrub_error(error: str, kwargs: dict[str, Any], policy: str) -> str:
    """Hide sensitive argument values that an error message repeats.

    Errors quote their input ("JS exception in 'fetch(token)'", "could not
    type 'hunter2'"), so the argument policy has to reach them too. Under
    ``full`` the arguments are logged anyway and the error is kept as is.
    Values shorter than 3 characters are left: masking every "a" in a
    message hides nothing and garbles the rest.
    """
    if policy == "full" or not kwargs:
        return error
    secrets = {s for k, v in kwargs.items()
               for s in _sensitive_strings(v, sensitive=str(k).lower() in SENSITIVE_ARGS)
               if len(s) >= 3}
    for s in sorted(secrets, key=len, reverse=True):
        error = error.replace(s, f"<redacted:{len(s)} chars>")
    return error


class AuditLog:
    """A day-rotating JSONL writer. Construct once, call :meth:`record`.

    Disabled instances are cheap no-ops, so the caller never has to branch.
    """

    def __init__(self, *, enabled: bool, directory: Path | None = None,
                 policy: str = "redacted", retention_days: int = 14) -> None:
        self.enabled = enabled
        self.directory = Path(directory) if directory else default_dir()
        self.policy = policy if policy in VALID_POLICIES else "redacted"
        self.retention_days = max(0, int(retention_days))
        self.session = uuid.uuid4().hex[:12]
        self._fh = None
        self._day = ""
        self._failures = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, *, version: str = "", platform: str = "") -> None:
        """Open today's file, prune old ones, and write a ``session`` line.

        Called once at server start. Failure here disables the log rather
        than stopping the server: a machine that cannot write an audit file
        is still a machine the operator may need to drive.
        """
        if not self.enabled:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.prune()
            self.record_event("session", {
                "version": version,
                "platform": platform,
                "pid": os.getpid(),
                "policy": self.policy,
                "retention_days": self.retention_days,
            })
            log.info("audit log: %s (policy=%s, retention=%s)", self.directory,
                     self.policy,
                     f"{self.retention_days}d" if self.retention_days else "forever")
        except Exception as e:
            self.enabled = False
            log.warning("audit log disabled — cannot use %s: %s", self.directory, e,
                        exc_info=True)

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def prune(self) -> int:
        """Delete ``audit-*.jsonl`` older than the retention window.

        Returns the number removed. ``retention_days=0`` keeps everything,
        for an operator who ships the files somewhere else and wants them
        kept until that happens.
        """
        if not self.retention_days:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).date()
        removed = 0
        try:
            for path in self.directory.glob("audit-*.jsonl"):
                stamp = path.stem.removeprefix("audit-")
                try:
                    # Date-only comparison against a date cutoff; there is no
                    # instant here for a zone to disambiguate.
                    day = datetime.strptime(stamp, "%Y-%m-%d").date()  # noqa: DTZ007
                except ValueError:
                    continue  # not ours — leave it alone
                if day < cutoff:
                    path.unlink()
                    removed += 1
        except OSError as e:
            log.debug("audit prune failed: %s", e)
        return removed

    # ── writing ──────────────────────────────────────────────────────

    def _stream(self):
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._fh is not None and day == self._day:
            return self._fh
        self.close()
        # Owner-only: under the ``full`` policy a line can hold typed text.
        # The modes only apply when this call creates the path.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory / f"audit-{day}.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Line-buffered append: one write per line, so several servers can
        # share the file without a lock.
        self._fh = open(fd, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — closed by self.close()
        self._day = day
        return self._fh

    def record_event(self, kind: str, fields: dict[str, Any]) -> None:
        """Write one line of an arbitrary kind. Never raises."""
        if not self.enabled:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            "session": self.session,
            **fields,
        }
        try:
            self._stream().write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._failures = 0
        except Exception as e:
            self._failures += 1
            if self._failures >= _MAX_FAILURES:
                self.enabled = False
                log.warning("audit log disabled after %d write failures: %s",
                            self._failures, e, exc_info=True)
            else:
                log.debug("audit write failed: %s", e, exc_info=True)

    def record(self, *, tool: str, klass: str, ok: bool, elapsed: float,
               kwargs: dict[str, Any] | None = None, error: str | None = None) -> None:
        """Record one completed tool call."""
        if not self.enabled:
            return
        fields: dict[str, Any] = {
            "tool": tool,
            "class": klass,
            "ok": ok,
            "ms": round(elapsed * 1000, 1),
            "pid": os.getpid(),
        }
        args = redact(kwargs or {}, self.policy)
        if args:
            fields["args"] = args
        if error:
            fields["error"] = scrub_error(error, kwargs or {}, self.policy)[:500]
        self.record_event("call", fields)


def from_config(config: Any) -> AuditLog:
    """Build the log from an :class:`~conductor_desktop.config.MCPConfig`."""
    return AuditLog(
        enabled=getattr(config, "audit", False),
        directory=Path(config.audit_dir) if getattr(config, "audit_dir", "") else None,
        policy=getattr(config, "audit_args", "redacted"),
        retention_days=getattr(config, "audit_retention_days", 14),
    )


def summarize(directory: Path | None = None) -> str:
    """One-line human summary of what's on disk — used by ``diagnose``."""
    directory = directory or default_dir()
    try:
        files = sorted(directory.glob("audit-*.jsonl"))
    except OSError:
        files = []
    if not files:
        return f"no audit files in {directory}"
    total = sum(f.stat().st_size for f in files)
    return (f"{len(files)} file(s), {total / 1024:.0f} KiB, "
            f"{files[0].stem.removeprefix('audit-')} … "
            f"{files[-1].stem.removeprefix('audit-')} in {directory}")


__all__ = [
    "SENSITIVE_ARGS",
    "VALID_POLICIES",
    "AuditLog",
    "default_dir",
    "from_config",
    "redact",
    "summarize",
]
