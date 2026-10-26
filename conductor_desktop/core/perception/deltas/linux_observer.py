"""Linux AT-SPI delta observer.

Single-thread design:

1. **GLib mainloop on a daemon thread** — Atspi.EventListener delivers
   callbacks on whatever thread runs the GLib mainloop, mirroring
   macOS's CFRunLoop shape. `_emit()` bridges to asyncio via
   call_soon_threadsafe.

2. **Desktop-wide subscription** — unlike macOS AX (per-PID) or Windows
   UIA (per-HWND), AT-SPI's registry is global. One EventListener
   covers every accessible app on the desktop; no app-activation
   poller, no re-subscribe layer. This is the structural simplification
   the handoff doc calls out.

3. **Workspace sources beside it** — the active workspace comes from
   whichever of GNOME / X11 / kwin this session answers, each a
   `WorkspaceSource` plugin attached to the same mainloop (see
   `workspace/base.py`). They share one de-dupe, so GNOME-on-Xorg, where
   two of them see the same transition, still emits one delta.

Retention: PyGObject manages refcounts itself, but we hold the
EventListener on `self._listener` so it isn't GC'd between register()
and the callbacks firing.

Permissions: AT-SPI requires the GTK accessibility bridge to be active
on the session (gsettings org.gnome.desktop.interface
toolkit-accessibility = true, or GTK_MODULES=gail:atk-bridge). The
setup script enables this; failures here log a warning and the
observer comes up but sees no events from non-bridged apps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ...errors import best_effort
from ..classifier import StateDelta
from ..threaded import ThreadedObserver
from ..view import SpaceTracker, build_view_changed
from .workspace import (
    GnomeWorkspacePoll,
    KdeWorkspaceWatcher,
    SessionBus,
    WorkspaceSource,
    X11WorkspaceWatcher,
)

log = logging.getLogger("conductor.perception.atspi")


# gi imports — wrapped because the perception module is imported on every
# MCP startup, including darwin/Windows hosts where PyGObject + Atspi
# typelibs are absent. Mirrors the macos_observer.py pattern.
try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
    _GI_AVAILABLE = True
except Exception as _gi_import_error:  # ImportError, ValueError (require_version), or RuntimeError
    log.debug("gi/Atspi unavailable", exc_info=True)
    _GI_AVAILABLE = False
    _gi_import_failure = _gi_import_error

try:
    if _GI_AVAILABLE:
        from gi.repository import Gio
    else:
        Gio = None
except Exception:
    log.debug("Gio unavailable", exc_info=True)
    Gio = None


# AT-SPI event-name → StateDelta kind. Names are the canonical
# colon-separated event strings registered with
# Atspi.EventListener.register(). Subevent splits (children-changed:add
# vs :remove, text-changed:insert vs :delete) are registered separately
# so we don't need to inspect `detail1` to disambiguate.
_ATSPI_KIND_MAP: dict[str, str] = {
    "object:state-changed:focused": "focus_changed",
    "object:text-changed:insert": "value_changed",
    "object:text-changed:delete": "value_changed",
    "object:value-changed": "value_changed",
    "object:state-changed:checked": "state_changed",
    "object:state-changed:expanded": "state_changed",
    "object:state-changed:selected": "state_changed",
    "object:property-change:accessible-name": "name_changed",
    "object:children-changed:add": "node_added",
    "object:children-changed:remove": "node_removed",
    "object:selection-changed": "selection_changed",
    "window:create": "node_added",
    "window:destroy": "node_removed",
    "window:activate": "window_state_changed",
}


_ATSPI_EVENT_TYPES: tuple[str, ...] = tuple(_ATSPI_KIND_MAP.keys())


# Layer 1 — drop value-changes from chrome-ish roles. AT-SPI role names
# are lowercase + space-separated ("scroll bar", not "AXScrollBar").
#
# "terminal" is here because a terminal emitting build output fires
# text-changed per line at output rates — the loudest single source on a
# Linux desktop, and one whose content the agent reads from the screen or
# from the command's own output anyway. Its focus / window events still
# come through; only the per-line value churn goes.
_ATSPI_SKIP_ROLES_FOR_VALUE: frozenset[str] = frozenset({
    "scroll bar",
    "scroll pane",
    "panel",
    "icon",
    "menu",
    "menu bar",
    "tool bar",
    "terminal",
})


# Max chars to keep from an AT-SPI value. The twin of macOS's
# _AX_VALUE_KEEP_MAX, and for the same reason: a URL bar (~80 chars), a
# search box (~50) or a short form field is the signal ("the user typed
# X"), and a pasted buffer is tens of KB of nothing, held for as long as
# the delta sits in the ring. AT-SPI hands us the inserted text rather
# than the whole buffer, so this bites less often than on macOS — but a
# paste is one event and unbounded.
_ATSPI_VALUE_KEEP_MAX = 256


class LinuxObserver(ThreadedObserver):
    """Subscribes a single Atspi.EventListener desktop-wide and
    normalizes events into StateDelta.

    Lifecycle:
        obs = LinuxObserver(asyncio.get_running_loop())
        await obs.start()       # spawns daemon thread + GLib mainloop
        ...
        await obs.stop()        # GLib.idle_add(quit), join thread

    Errors during start (PyGObject/Atspi typelib missing, AT-SPI bridge
    not active) raise RuntimeError on start() rather than silently
    producing no events.
    """

    thread_name = "conductor-atspi-observer"

    def __init__(self, loop: asyncio.AbstractEventLoop, **kw) -> None:
        super().__init__(loop, **kw)
        self._gloop: Any = None  # GLib.MainLoop
        self._listener: Any = None  # Atspi.EventListener
        # Workspace sources: each says at start() whether this desktop is
        # one it can read. They share `_workspaces` for de-dupe, so the
        # GNOME-on-Xorg double coverage emits one delta per transition.
        self._bus = SessionBus(Gio)
        self._sources: list[WorkspaceSource] = []
        self._workspaces = SpaceTracker()

    # ── Lifecycle (ThreadedObserver hooks) ──────────────────────────

    def _signal_stop(self) -> None:
        # GLib.idle_add hops the quit onto the mainloop's thread — calling
        # _gloop.quit() directly from another thread is undefined behavior.
        if self._gloop is None:
            return
        try:
            GLib.idle_add(self._gloop.quit)
        except Exception:
            log.debug("GLib.idle_add(quit) failed (continuing)", exc_info=True)

    def _thread_setup(self) -> None:
        if not _GI_AVAILABLE:
            raise RuntimeError(
                f"PyGObject/Atspi unavailable: {_gi_import_failure}. "
                "Install via system packages: "
                "apt install python3-gi gir1.2-atspi-2.0"
            )
        # Atspi.init() is idempotent on recent versions but required at
        # least once per process. Must run on the thread that will host
        # the mainloop.
        Atspi.init()
        self._gloop = GLib.MainLoop()
        self._listener = Atspi.EventListener.new(self._handle_event)
        registered = 0
        for ev_type in _ATSPI_EVENT_TYPES:
            try:
                self._listener.register(ev_type)
                registered += 1
            except Exception:
                # Some AT-SPI builds reject specific event-type strings
                # (older versions ignore subevent suffixes). Log and
                # continue — partial subscription is better than none.
                log.debug("Atspi register(%s) failed", ev_type, exc_info=True)
        log.info("AT-SPI observer attached, %d/%d event types registered",
                 registered, len(_ATSPI_EVENT_TYPES))
        self._attach_workspace_sources()

    def _thread_serve(self) -> None:
        self._gloop.run()

    def _thread_teardown(self) -> None:
        for source in self._sources:
            with best_effort(f"detaching the {source.name} workspace source", log):
                source.stop()
        self._sources = []
        # Best-effort deregister so we don't leave dangling D-Bus
        # subscriptions if the mainloop quits cleanly.
        if self._listener is not None:
            for ev_type in _ATSPI_EVENT_TYPES:
                with best_effort(f"deregistering {ev_type}", log):
                    self._listener.deregister(ev_type)
        self._listener = None
        self._gloop = None
        # Drop our D-Bus reference. PyGObject releases the underlying
        # GDBusConnection if no other Python ref holds it; the connection
        # itself is process-wide and managed by GDBus internals.
        self._bus.release()

    # ── Workspace sources ───────────────────────────────────────────

    def _attach_workspace_sources(self) -> None:
        """Attach every workspace source this desktop answers.

        None of them is required: a session with no gnome-shell, no X11 and
        no kwin (a bare sway, say) simply reports no workspace transitions,
        and the AT-SPI half keeps working. Priming takes the first source
        that can name the current workspace, so the first transition we
        report is a real one rather than "None → wherever we started".
        """
        candidates: list[WorkspaceSource] = [
            GnomeWorkspacePoll(self._on_workspace, self._bus),
            X11WorkspaceWatcher(self._on_workspace),
            KdeWorkspaceWatcher(self._on_workspace, self._bus),
        ]
        for source in candidates:
            attached = False
            with best_effort(f"attaching the {source.name} workspace source", log):
                attached = source.start()
            if not attached:
                continue
            self._sources.append(source)
            if self._workspaces.last is None:
                self._workspaces.prime(source.current())

    def _on_workspace(self, index: int, origin: str) -> None:
        """A source saw the active workspace. Emit unless we already knew.

        Runs on the GLib mainloop thread, as does every source, so the
        de-dupe never contends.
        """
        changed, prev = self._workspaces.take(index)
        if not changed:
            return
        self._emit(build_view_changed(
            source="atspi",  # loosely = "linux observer stack"
            source_id=f"workspace-{index}",
            space_id=f"ws:global:{index}",
            prev_space_id=f"ws:global:{prev}" if prev is not None else None,
            origin=origin,
        ))

    # ── Event handling ──────────────────────────────────────────────

    def _handle_event(self, event: Any) -> None:
        """Called from the GLib mainloop thread. Normalize → emit.

        NEVER let an exception propagate — the GLib callback bridge has
        no Python-side handler and a raise here would tear down the
        mainloop. Log and swallow.
        """
        try:
            ev_type = getattr(event, "type", None) or ""
            kind = _ATSPI_KIND_MAP.get(ev_type)
            if kind is None:
                return

            # AT-SPI fires state-changed:focused twice per focus shift
            # (lost on previous, gained on next). detail1==1 means
            # "the state bit is now true" — drop the loss event so we
            # see one focus_changed per actual focus.
            if (ev_type == "object:state-changed:focused"
                    and getattr(event, "detail1", 0) != 1):
                return

            source = getattr(event, "source", None)
            if source is None:
                # Defensive — every AT-SPI event has a source, but a
                # destroyed-mid-dispatch element could in principle yield
                # None.
                return

            # get_role_name is itself a D-Bus round trip, and it gates the
            # other half-dozen (name, value, extents, application), so the
            # cheap checks above go first and this is the floor: one round
            # trip for an event we end up dropping.
            role = _safe_call(source.get_role_name) or ""
            # Layer 1: drop value-changes from chrome-ish roles. Other
            # kinds on the same role are kept — focus_changed on a menu
            # is still useful ("menu opened").
            if kind == "value_changed" and role in _ATSPI_SKIP_ROLES_FOR_VALUE:
                return

            name = _safe_call(source.get_name) or None
            value = _extract_value(event, kind)
            bbox = _safe_bbox(source)

            delta = StateDelta(
                kind=kind,
                source="atspi",
                monotonic_ts=time.monotonic(),
                source_id=_stable_source_id(source, role, name),
                role=role or None,
                name=name,
                value=value,
                bbox=bbox,
            )
            self._emit(delta)
        except Exception:
            log.exception("AT-SPI callback error (event=%r)",
                          getattr(event, "type", "?"))


# ── Module-level helpers ─────────────────────────────────────────────


def _safe_call(fn) -> Any:
    """Invoke a no-arg AT-SPI accessor with errors swallowed.

    AT-SPI accessors can fail mid-flight: the source element was
    destroyed between the event firing and the accessor call (D-Bus
    round-trip races with widget teardown), or the app went
    unresponsive. Returning None on any error is the right default —
    StateDelta fields are all Optional.
    """
    try:
        return fn()
    except Exception:
        # Hot path (several accessors per AT-SPI event): a plain handler
        # rather than best_effort's generator.
        log.debug("AT-SPI accessor failed", exc_info=True)
        return None


def _extract_value(event: Any, kind: str) -> str | None:
    """Best-effort string form of the new value for a value/state event.

    AT-SPI hands the inserted text in `event.any_data` for text-changed,
    and the new state value (0/1) as `detail1` for state-changed.
    Numeric value-changed deltas surface via the Value interface — we
    keep this defensive because `any_data` is a GValue and conversions
    can fail across binding versions.

    Text-changed also carries the inserted length in `detail2`, which is
    the cheap pre-check (the twin of macOS's AXNumberOfCharacters): a
    pasted buffer is skipped before the GValue is ever stringified.
    """
    if kind == "value_changed":
        length = getattr(event, "detail2", None)
        if isinstance(length, int) and length > _ATSPI_VALUE_KEEP_MAX:
            return None
        any_data = getattr(event, "any_data", None)
        if any_data is not None and any_data != "":
            with best_effort("converting any_data", log):
                text = str(any_data)
                return text if len(text) <= _ATSPI_VALUE_KEEP_MAX else None
    if kind == "state_changed":
        detail1 = getattr(event, "detail1", None)
        if detail1 is not None:
            return "1" if detail1 == 1 else "0"
    return None


def _safe_bbox(source: Any) -> tuple[int, int, int, int] | None:
    """Pull screen-space (x, y, w, h) from an Atspi.Component if present.

    Many roles (label, application, document) don't implement Component
    and return None — that's fine, the StateDelta bbox is Optional.
    Returning None on any error is the right default.
    """
    if not _GI_AVAILABLE:
        return None
    try:
        # Atspi.Accessible exposes the Component interface either as
        # .get_component() or via interface introspection; the former
        # is the high-level shortcut on recent typelibs.
        component = None
        if hasattr(source, "get_component"):
            component = source.get_component()
        if component is None:
            return None
        rect = component.get_extents(Atspi.CoordType.SCREEN)
        if rect is None:
            return None
        return (int(rect.x), int(rect.y), int(rect.width), int(rect.height))
    except Exception:
        log.debug("reading the element extents failed", exc_info=True)
        return None


def _stable_source_id(source: Any, role: str, name: str | None) -> str:
    """Build a stable-ish coalesce key for an AT-SPI element.

    Prefer the AT-SPI 'id' attribute when set (apps that explicitly
    expose it — uncommon but unambiguous). Fall back to
    (app-name, role, name) which is usually stable across the same
    element's events within a tool-call frame. Worst case: the same
    element appears under multiple keys and coalesce just doesn't
    dedupe — verbose but correct.
    """
    attrs = _safe_call(source.get_attributes) or {}
    explicit_id = attrs.get("id") if isinstance(attrs, dict) else None
    app_name: str | None = None
    with best_effort("reading the element's application", log):
        app = source.get_application()
        if app is not None:
            app_name = _safe_call(app.get_name)
    prefix = app_name or "?"
    if explicit_id:
        return f"{prefix}:{explicit_id}"
    return f"{prefix}:{role or '?'}:{name or ''}"
