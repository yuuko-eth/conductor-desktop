"""Auto-detect DISPLAY / XAUTHORITY for the MCP server on Linux & FreeBSD.

Background: MCP launchers (Claude Code's ``~/.claude.json``, systemd units,
shell wrappers) tend to pin ``DISPLAY=:1`` and ``XAUTHORITY=...`` once and
forget about it. GDM/sddm/lightdm hand out a different display number on
the next session (commonly ``:0`` after a reboot vs ``:1`` first-boot, or
the reverse), and the stale env makes the server crash inside pystray's
import-time ``Xlib.display.Display()`` call.

This module fills holes (and replaces values that don't connect) by
probing the running graphical session before anything touches X.

Order of probes:
  1. If the current env's DISPLAY accepts a connection (local socket, or
     TCP for an SSH-forwarded ``localhost:10.0``), keep it. Only patch
     XAUTHORITY if missing or stale, and only from the same display.
  2. Scan ``/proc/*/environ`` for a process owned by our uid that exports both
     DISPLAY and XAUTHORITY (gnome-shell, kwin_x11, etc.). This is the
     most reliable source — those values came from the actual session.
  3. Fall back to scanning ``/tmp/.X11-unix/`` for sockets owned by us
     and pick the lowest-numbered one.
"""

from __future__ import annotations

import logging
import os
import socket
import sys

log = logging.getLogger("conductor.mcp.linux_env")

_SOCK_DIR = "/tmp/.X11-unix"


def autodetect_x11_env() -> None:
    """Idempotent: only fill holes / replace values that don't connect."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return

    cur_display = os.environ.get("DISPLAY", "").strip()
    cur_xauth = os.environ.get("XAUTHORITY", "").strip()

    display_ok = bool(cur_display) and _display_alive(cur_display)
    xauth_ok = bool(cur_xauth) and os.path.exists(cur_xauth)
    if display_ok and xauth_ok:
        return

    detected = _probe_proc_environ() or (None, None)
    new_display, new_xauth = detected

    if not display_ok:
        candidate = new_display or _scan_sockets()
        if candidate and _display_alive(candidate):
            log.warning(
                "DISPLAY autodetect: %r -> %r (prior value did not connect)",
                cur_display, candidate,
            )
            os.environ["DISPLAY"] = candidate
        elif cur_display:
            log.warning(
                "DISPLAY=%r does not connect and no live X server found; "
                "leaving env unchanged so the failure mode is obvious",
                cur_display,
            )

    # The session's XAUTHORITY only fits the session's display: an SSH
    # forward kept above (localhost:10.0) has its own cookie.
    if (not xauth_ok and new_xauth and os.path.exists(new_xauth)
            and _same_display(new_display, os.environ.get("DISPLAY", ""))):
        log.warning(
            "XAUTHORITY autodetect: %r -> %r", cur_xauth, new_xauth,
        )
        os.environ["XAUTHORITY"] = new_xauth


def _display_alive(display: str) -> bool:
    """Whether an X server answers at ``display``. Connects, does NOT auth.

    ``:N`` and ``unix:N`` are local: Linux's abstract socket first (it is
    there even when a sandbox hides ``/tmp/.X11-unix``), then the socket
    file. ``host:N`` is TCP port 6000+N, which is how SSH X forwarding
    (``localhost:10.0``) shows up.
    """
    host, sep, rest = display.rpartition(":")
    if not sep:
        return False
    try:
        n = int(rest.split(".", 1)[0])
    except ValueError:
        return False
    if host and host != "unix":
        return _connects(socket.AF_INET, (host, 6000 + n))
    path = f"{_SOCK_DIR}/X{n}"
    if sys.platform.startswith("linux") and _connects(socket.AF_UNIX, "\0" + path):
        return True
    return os.path.exists(path) and _connects(socket.AF_UNIX, path)


def _same_display(a: str | None, b: str) -> bool:
    """``:0``, ``:0.0`` and ``unix:0`` are one display; ``localhost:0`` isn't."""
    def key(d: str) -> tuple[str, str]:
        host, _, rest = d.rpartition(":")
        return ("" if host == "unix" else host, rest.split(".", 1)[0])
    return bool(a) and key(a) == key(b)


def _connects(family: int, address) -> bool:
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    s.settimeout(0.5)
    try:
        s.connect(address)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _scan_sockets() -> str | None:
    """Return ``:N`` for the lowest live X socket owned by current uid."""
    try:
        entries = os.listdir(_SOCK_DIR)
    except OSError:
        return None
    uid = os.getuid()
    nums: list[int] = []
    for name in entries:
        if not name.startswith("X"):
            continue
        try:
            n = int(name[1:])
        except ValueError:
            continue
        path = f"{_SOCK_DIR}/{name}"
        try:
            st = os.stat(path)
        except OSError:
            continue
        # Some display managers (gdm) leave the socket world-writable but
        # owned by the session user; uid match is the strongest signal.
        if st.st_uid == uid:
            nums.append(n)
    if not nums:
        return None
    return f":{min(nums)}"


def _probe_proc_environ() -> tuple[str, str] | None:
    """Walk /proc/*/environ for our own processes and pull DISPLAY+XAUTHORITY.

    ``/proc/<pid>/environ`` is owner-readable, but root can read everyone's,
    so the owner is checked too: a server run as root must not borrow
    another user's session.
    """
    proc = "/proc"
    if not os.path.isdir(proc):
        return None  # FreeBSD w/o procfs mounted, etc.
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    self_pid = str(os.getpid())
    uid = os.getuid()
    for pid in pids:
        if pid == self_pid:
            continue
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        env_path = f"{proc}/{pid}/environ"
        try:
            with open(env_path, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if not raw:
            continue
        display = ""
        xauth = ""
        for chunk in raw.split(b"\0"):
            if chunk.startswith(b"DISPLAY="):
                display = chunk[len(b"DISPLAY="):].decode("utf-8", "replace").strip()
            elif chunk.startswith(b"XAUTHORITY="):
                xauth = chunk[len(b"XAUTHORITY="):].decode("utf-8", "replace").strip()
        if display and xauth and _display_alive(display):
            return display, xauth
    return None
