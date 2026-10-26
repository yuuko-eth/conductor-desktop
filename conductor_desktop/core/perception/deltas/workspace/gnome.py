"""GNOME: poll `org.gnome.Shell.Eval` for the active workspace index.

GNOME publishes no D-Bus signal for "the active workspace changed"; the
gnome-shell-internal signal is only observable through the Eval pipe, and a
sustained subscription over Eval costs more than asking every 500 ms for the
same integer. So this one polls, at the same cadence as the macOS observer's
Space poll.

Eval is gated behind `unsafe_mode` in GNOME 41+ (https://docs.conductor-mcp.io/platforms/linux/gnome-wayland/),
in which case every call fails and the source goes quiet rather than noisy —
the operator may flip unsafe_mode on later and polling picks it up.
"""

from __future__ import annotations

import logging

from .base import OnWorkspace, SessionBus, WorkspaceSource

log = logging.getLogger("conductor.perception.atspi")

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib
    _GLIB_AVAILABLE = True
except Exception:
    log.debug("GLib unavailable", exc_info=True)
    _GLIB_AVAILABLE = False

try:
    if _GLIB_AVAILABLE:
        from gi.repository import Gio
        _GIO_AVAILABLE = True
    else:
        _GIO_AVAILABLE = False
except Exception:
    log.debug("Gio unavailable", exc_info=True)
    _GIO_AVAILABLE = False


# Cadence for the poll. Matches the macOS observer's 500 ms tick: about the
# human-perception threshold for "snappy", and cheap (one in-process gdbus
# call to the local gnome-shell, typically <10 ms).
POLL_INTERVAL_MS = 500

# The JS Eval runs. A workspace index is a small integer, so the reply is a
# couple of bytes.
_ACTIVE_WORKSPACE_JS = "global.workspace_manager.get_active_workspace_index()"


class GnomeWorkspacePoll(WorkspaceSource):
    name = "poll"

    def __init__(self, on_workspace: OnWorkspace, bus: SessionBus) -> None:
        super().__init__(on_workspace)
        self._bus = bus
        self._source_id = 0  # GLib timeout source id, 0 = none
        # Memo: once Eval is seen gated, stop logging the same warning every
        # tick. unsafe_mode might be flipped on later; we keep polling.
        self._gated_warned = False

    def start(self) -> bool:
        if not _GIO_AVAILABLE or self._source_id:
            return bool(self._source_id)
        if self._bus.get() is None:
            return False
        # GLib.timeout_add returns the source id we need for clean teardown.
        # The callback returns True to keep the timer alive.
        self._source_id = GLib.timeout_add(POLL_INTERVAL_MS, self._tick)
        log.info("GNOME workspace poll attached (interval=%dms)", POLL_INTERVAL_MS)
        return True

    def stop(self) -> None:
        if not self._source_id:
            return
        # Removing the timeout drops GLib's strong ref to our callback
        # closure, so the closure (and the `self` it captured) can be GC'd.
        try:
            GLib.source_remove(self._source_id)
        except Exception:
            log.debug("removing the workspace poll source failed", exc_info=True)
        self._source_id = 0

    def _tick(self) -> bool:
        """GLib timeout callback. Returns True to keep the timer alive.

        Keep it O(1): one in-process gdbus call, an int comparison, and at
        most one StateDelta.
        """
        index = self.current()
        if index is not None:
            self._on_workspace(index, self.name)
        return True  # a transient failure is worth another tick

    def current(self) -> int | None:
        """The active workspace index, or None on any failure: Eval gated by
        unsafe_mode, gnome-shell absent (XFCE/MATE/sway), connection timeout,
        parse failure."""
        bus = self._bus.get()
        if bus is None:
            return None
        try:
            reply = bus.call_sync(
                "org.gnome.Shell",
                "/org/gnome/Shell",
                "org.gnome.Shell",
                "Eval",
                GLib.Variant("(s)", (_ACTIVE_WORKSPACE_JS,)),
                GLib.VariantType.new("(bs)"),
                Gio.DBusCallFlags.NONE,
                500,  # ms timeout — gnome-shell Eval is normally <10ms
                None,
            )
        except GLib.Error:
            # name-not-found (no gnome-shell) or timeout — treat as "not
            # available". Don't log per-tick; we're polling.
            return None
        except Exception:
            log.debug("GNOME Shell Eval call failed", exc_info=True)
            return None

        try:
            success, payload = reply.unpack()
        except Exception:
            log.debug("unpacking the Eval reply failed", exc_info=True)
            return None
        if not success:
            if not self._gated_warned:
                log.info(
                    "GNOME Shell Eval is gated (unsafe_mode=false); workspace "
                    "polling silent until enabled — see the unsafe_mode "
                    "section of https://docs.conductor-mcp.io/platforms/linux/gnome-wayland/"
                )
                self._gated_warned = True
            return None
        try:
            # Eval wraps the JS return in JSON.stringify; for a plain integer
            # like 2, payload is just "2" (no quotes).
            return int(payload)
        except (TypeError, ValueError):
            return None
