"""KDE: subscribe to `org.kde.KWin.VirtualDesktopManager` signals.

Covers Plasma on Wayland and on X11 alike — it is the same kwin and the same
D-Bus object either way. Plasma 6 keys desktops by UUID rather than by index,
so this keeps a UUID → index map, read from the `desktops` property at setup
and refreshed on `desktopCreated` / `desktopRemoved`, to emit the same
`ws:global:<n>` shape as GNOME and X11.

Silent no-op when `org.kde.KWin` isn't owned on the bus — a non-KDE session,
or KDE that wasn't up yet when the observer started.
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

_SERVICE = "org.kde.KWin"
_PATH = "/VirtualDesktopManager"
_IFACE = "org.kde.KWin.VirtualDesktopManager"


class KdeWorkspaceWatcher(WorkspaceSource):
    name = "kwin-dbus"

    def __init__(self, on_workspace: OnWorkspace, bus: SessionBus) -> None:
        super().__init__(on_workspace)
        self._bus = bus
        self._sub_ids: list[int] = []  # signal_subscribe handles
        self._uuid_to_index: dict[str, int] = {}

    def start(self) -> bool:
        if not _GIO_AVAILABLE or self._bus.get() is None:
            return False
        if self._sub_ids:
            return True
        # Refresh once at startup. If org.kde.KWin isn't owned, the property
        # read fails and we skip subscribing, keeping the bus traffic-free
        # on non-KDE sessions.
        if not self.refresh_desktop_map():
            log.debug("KDE kwin VirtualDesktopManager not found on bus; skipping.")
            return False
        try:
            self._sub_ids = [
                self._subscribe("currentChanged", self._on_current_changed),
                self._subscribe("desktopCreated", self._on_desktops_changed),
                self._subscribe("desktopRemoved", self._on_desktops_changed),
            ]
        except Exception:
            log.exception("KDE kwin signal_subscribe failed; watcher will be silent")
            return False
        log.info("KDE kwin VirtualDesktopManager watcher attached (desktops=%d)",
                 len(self._uuid_to_index))
        return True

    def stop(self) -> None:
        bus = self._bus.get()
        if bus is not None:
            # signal_unsubscribe drops GDBus's strong ref to our callback
            # closure, so the captured `self` releases cleanly.
            for sub_id in self._sub_ids:
                try:
                    bus.signal_unsubscribe(sub_id)
                except Exception:
                    log.debug("unsubscribing a KDE D-Bus signal failed", exc_info=True)
        self._sub_ids = []
        self._uuid_to_index = {}

    def _subscribe(self, signal: str, handler) -> int:
        return self._bus.get().signal_subscribe(
            _SERVICE, _IFACE, signal, _PATH,
            None,  # arg0 filter
            Gio.DBusSignalFlags.NONE,
            handler,
        )

    # ── Reads ───────────────────────────────────────────────────────

    def _get_property(self, name: str):
        """One `org.freedesktop.DBus.Properties.Get` on the manager, or None."""
        bus = self._bus.get()
        if bus is None:
            return None
        try:
            reply = bus.call_sync(
                _SERVICE, _PATH,
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (_IFACE, name)),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE,
                500,  # ms — local D-Bus call, typically <10ms
                None,
            )
            (value,) = reply.unpack()
        except GLib.Error:
            return None
        except Exception:
            log.debug("reading kwin %s failed", name, exc_info=True)
            return None
        return value

    def refresh_desktop_map(self) -> bool:
        """Rebuild the UUID → index map. True when the read succeeded, which
        also answers "is KDE here?"."""
        value = self._get_property("desktops")
        if value is None:
            return False
        # value is a list of (int, str, str) tuples: (index, uuid, name).
        new_map: dict[str, int] = {}
        try:
            for index, uuid, _name in value:
                new_map[str(uuid)] = int(index)
        except (TypeError, ValueError):
            return False
        self._uuid_to_index = new_map
        return True

    def current(self) -> int | None:
        """kwin's `current` property, resolved to an integer index. None on
        any failure (KDE absent, UUID unknown in the map, D-Bus error)."""
        uuid = self._get_property("current")
        if uuid is None:
            return None
        return self._uuid_to_index.get(str(uuid))

    # ── Signals ─────────────────────────────────────────────────────

    def _on_current_changed(
        self, _connection, _sender, _path, _iface, _signal, parameters,
        *_user_data,
    ) -> None:
        """GDBus callback for `currentChanged(s id)`.

        NEVER let exceptions propagate — a raise across the GDBus boundary
        tears down the mainloop, the same hazard as the AT-SPI handler.
        """
        try:
            (uuid,) = parameters.unpack()
        except Exception:
            log.debug("KDE currentChanged: payload unpack failed", exc_info=True)
            return
        index = self._uuid_to_index.get(str(uuid))
        if index is None:
            # Stale map — refresh once and retry. Common path: kwin emitted
            # currentChanged before desktopCreated was processed.
            self.refresh_desktop_map()
            index = self._uuid_to_index.get(str(uuid))
        if index is None:
            log.debug("KDE currentChanged: unknown desktop UUID %r", uuid)
            return
        self._on_workspace(index, self.name)

    def _on_desktops_changed(
        self, _connection, _sender, _path, _iface, _signal, _parameters,
        *_user_data,
    ) -> None:
        """Refresh the map. Both desktopCreated and desktopRemoved carry an
        (id, data) payload we don't need: a full re-read of `desktops` is
        O(N) and N is always tiny (kwin allows 20; people have 2–4)."""
        self.refresh_desktop_map()
