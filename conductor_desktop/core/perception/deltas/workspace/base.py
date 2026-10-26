"""What every Linux workspace source is.

A Linux desktop tells you the active workspace in one of several ways and
there is no telling in advance which of them this session answers, so the
observer attaches all of them and lets each say whether it found its desktop.
Every source is the same three things: attach to the mainloop, report the
current workspace, detach. The observer de-dupes across them, so the GNOME-X11
case, where two sources both see the same transition, emits one delta.

They all run on the observer's GLib mainloop thread, which is what keeps the
shared de-dupe state free of locking.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("conductor.perception.atspi")

# Called with (workspace_index, origin) when a source sees a transition.
# `origin` is diagnostic — which source saw it.
OnWorkspace = Callable[[int, str], None]


class SessionBus:
    """The session D-Bus connection, acquired once and shared.

    Sources take this rather than a connection so that each can ask for the
    bus itself: KDE used to be reachable only when the GNOME poll's setup had
    run first and left a connection behind, which made KDE support depend on
    GNOME code on a machine that has no GNOME.
    """

    def __init__(self, gio: Any) -> None:
        self._gio = gio
        self._bus: Any = None
        self._tried = False

    def get(self) -> Any:
        """The connection, or None when there isn't one. Tried once."""
        if self._tried:
            return self._bus
        self._tried = True
        if self._gio is None:
            return None
        try:
            self._bus = self._gio.bus_get_sync(self._gio.BusType.SESSION, None)
        except Exception as e:
            log.debug("session D-Bus unavailable (%s)", e, exc_info=True)
            self._bus = None
        return self._bus

    def release(self) -> None:
        """Drop our reference. The connection itself is process-wide and
        GDBus owns it; this only lets PyGObject release our handle."""
        self._bus = None
        self._tried = False


class WorkspaceSource:
    """One way to learn the active workspace."""

    #: Short name, used in the delta's `origin` field and in logs.
    name = "workspace"

    def __init__(self, on_workspace: OnWorkspace) -> None:
        self._on_workspace = on_workspace

    def start(self) -> bool:
        """Attach to the mainloop. False when this desktop doesn't have it.

        Must not raise: a source that can't attach is the normal case on
        every desktop that isn't its own.
        """
        raise NotImplementedError

    def current(self) -> int | None:
        """The active workspace index now, or None when it can't be read.
        Used to prime the de-dupe so the first read isn't a transition."""
        return None

    def stop(self) -> None:
        """Detach from the mainloop. Runs on the mainloop thread, and is
        safe to call when `start` returned False."""
