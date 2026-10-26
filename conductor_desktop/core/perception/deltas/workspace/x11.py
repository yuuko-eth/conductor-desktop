"""X11: watch `_NET_CURRENT_DESKTOP` on the root window.

Event-driven, and it covers every EWMH-compliant window manager — i3, xfwm4,
mutter-x11, openbox, GNOME on Xorg — because they all maintain the same root
property. The display's socket is hooked into the observer's GLib mainloop,
so events are drained on the same thread as everything else the observer
does.

No-op on Wayland: XWayland has a `$DISPLAY` but doesn't meaningfully maintain
`_NET_CURRENT_DESKTOP`, so trusting it there would report a workspace that
never changes.
"""

from __future__ import annotations

import logging
import os

from .base import OnWorkspace, WorkspaceSource

log = logging.getLogger("conductor.perception.atspi")

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib
    _GLIB_AVAILABLE = True
except Exception:
    log.debug("GLib unavailable", exc_info=True)
    _GLIB_AVAILABLE = False

# python-xlib. Optional; the source stays silent when it is missing.
# Apt: `python3-xlib`; pip: `python-xlib`.
try:
    from Xlib import X as _Xconst
    from Xlib import display as _xdisplay_mod
    from Xlib.error import XError as _XError
    _XLIB_AVAILABLE = True
except Exception:
    log.debug("python-xlib unavailable", exc_info=True)
    _XLIB_AVAILABLE = False


def is_x11_session() -> bool:
    """True iff the current process is running on an X11 session.

    XDG_SESSION_TYPE is the authoritative signal on systemd-logind systems
    (all modern distros). Falls back to env-presence heuristics on the rare
    host where the variable is unset. Wayland sessions with XWayland have
    $DISPLAY too, so DISPLAY alone is not sufficient.
    """
    sess = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if sess == "wayland":
        return False
    if sess == "x11":
        return True
    return bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")


class X11WorkspaceWatcher(WorkspaceSource):
    name = "x11-propertynotify"

    def __init__(self, on_workspace: OnWorkspace) -> None:
        super().__init__(on_workspace)
        self._display = None  # Xlib.display.Display or None
        self._root = None  # root window
        self._atom = 0  # _NET_CURRENT_DESKTOP
        self._watch_id = 0  # GLib IO watch source id, 0 = none

    def start(self) -> bool:
        if not _XLIB_AVAILABLE or not _GLIB_AVAILABLE or not is_x11_session():
            return False
        if self._watch_id:
            return True
        try:
            self._display = _xdisplay_mod.Display()
            self._root = self._display.screen().root
            self._atom = self._display.intern_atom("_NET_CURRENT_DESKTOP")
            # PropertyChangeMask on the root window is the EWMH-spec'd way
            # to be notified of workspace changes. Other clients (panels,
            # pagers) also subscribe — this doesn't conflict.
            self._root.change_attributes(event_mask=_Xconst.PropertyChangeMask)
            self._display.flush()
            # GLib.IO_IN fires when the X socket has bytes to read; the
            # callback then drains every parsed event.
            fd = self._display.fileno()
            self._watch_id = GLib.io_add_watch(fd, GLib.IO_IN, self._on_readable)
        except Exception as e:
            log.debug("X11 workspace watcher setup failed (%s); skipping", e,
                      exc_info=True)
            self.stop()
            return False
        log.info("X11 _NET_CURRENT_DESKTOP watcher attached (fd=%d)", fd)
        return True

    def stop(self) -> None:
        if self._watch_id:
            try:
                GLib.source_remove(self._watch_id)
            except Exception:
                log.debug("removing the X11 IO watch failed", exc_info=True)
            self._watch_id = 0
        if self._display is not None:
            # Closing flushes pending requests and closes the socket
            # cleanly, so the X server doesn't log a "client killed".
            try:
                self._display.close()
            except Exception:
                log.debug("closing the X display failed", exc_info=True)
        self._display = None
        self._root = None

    def _on_readable(self, _fd: int, _condition: int) -> bool:
        """GLib IO watch callback. Returns True to keep watching.

        NEVER let exceptions propagate — like the AT-SPI handler, a raise
        here would tear down the GLib mainloop. Log and swallow.
        """
        if self._display is None:
            return False  # remove watch — display gone
        try:
            # pending_events() flushes the socket buffer and returns the
            # count of parsed events ready in the queue. Drain them all so
            # we don't get re-armed mid-burst.
            while self._display.pending_events() > 0:
                ev = self._display.next_event()
                if ev.type != _Xconst.PropertyNotify:
                    continue
                if getattr(ev, "atom", 0) != self._atom:
                    continue
                index = self.current()
                if index is not None:
                    self._on_workspace(index, self.name)
        except _XError:
            log.debug("X11 event drain errored (continuing)", exc_info=True)
        except Exception:
            log.debug("X11 IO watch callback errored (continuing)", exc_info=True)
        return True

    def current(self) -> int | None:
        """Read _NET_CURRENT_DESKTOP from the root window. None on any
        failure (atom absent, property unset, parse error, round trip).

        Per EWMH the property is CARDINAL (one 32-bit unsigned int). Passing
        property_type=0 (AnyPropertyType) lets the server return whatever it
        has; we cast to int.
        """
        if self._display is None or self._root is None:
            return None
        try:
            prop = self._root.get_full_property(self._atom, 0)  # AnyPropertyType
            if prop is None or not getattr(prop, "value", None):
                return None
            return int(prop.value[0])
        except Exception:
            log.debug("reading _NET_CURRENT_DESKTOP failed", exc_info=True)
            return None
