"""Tests for the X11 _NET_CURRENT_DESKTOP workspace source (Phase 4a-ii).

The watcher is event-driven via a GLib IO watch on the X display socket;
unit tests here mock python-xlib + the X display object so we exercise
the normalization + dedupe path without needing a real X server. End-to-
end against a live X session lives on the smoke-test hosts (ubuntu-gnome on
"Ubuntu on Xorg" sign-in).

The de-dupe across sources lives on LinuxObserver, so the delta-shape tests
drive it through the observer's `_on_workspace` — the same callback every
source is handed.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Skip the whole file on non-linux since linux_observer imports
# PyGObject typelibs at module load time. The X11-specific paths are
# under additional gates that work even when xlib is missing.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux-only: linux_observer imports PyGObject + AT-SPI typelibs",
)


from conductor_desktop.core.perception.deltas.linux_observer import LinuxObserver
from conductor_desktop.core.perception.deltas.workspace import x11 as x11_mod
from conductor_desktop.core.perception.deltas.workspace.x11 import (
    X11WorkspaceWatcher,
    is_x11_session,
)


def _watcher(on_workspace=None) -> X11WorkspaceWatcher:
    return X11WorkspaceWatcher(on_workspace or (lambda index, origin: None))


# ── is_x11_session detection ────────────────────────────────────────

def test_is_x11_session_xdg_session_type_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert is_x11_session() is True


def test_is_x11_session_xdg_session_type_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")  # XWayland — must not fool us
    assert is_x11_session() is False


def test_is_x11_session_fallback_display_only(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert is_x11_session() is True


def test_is_x11_session_fallback_wayland_present(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert is_x11_session() is False


def test_is_x11_session_no_display(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert is_x11_session() is False


# ── start() declines when xlib is missing / on Wayland ──────────────

def test_start_declines_when_xlib_unavailable(monkeypatch):
    monkeypatch.setattr(x11_mod, "_XLIB_AVAILABLE", False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")

    watcher = _watcher()
    assert watcher.start() is False
    assert watcher._display is None
    assert watcher._watch_id == 0


def test_start_declines_on_wayland(monkeypatch):
    monkeypatch.setattr(x11_mod, "_XLIB_AVAILABLE", True)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    watcher = _watcher()
    assert watcher.start() is False
    assert watcher._display is None
    assert watcher._watch_id == 0


# ── current() ───────────────────────────────────────────────────────

def test_current_returns_none_when_display_unset():
    # Default _display/_root are None — must not raise.
    assert _watcher().current() is None


def test_current_parses_cardinal():
    watcher = _watcher()
    prop = MagicMock()
    prop.value = [3]
    root = MagicMock()
    root.get_full_property.return_value = prop
    watcher._display = MagicMock()
    watcher._root = root
    watcher._atom = 42
    assert watcher.current() == 3
    root.get_full_property.assert_called_once_with(42, 0)


def test_current_handles_missing_property():
    watcher = _watcher()
    root = MagicMock()
    root.get_full_property.return_value = None
    watcher._display = MagicMock()
    watcher._root = root
    assert watcher.current() is None


def test_current_swallows_xerror():
    watcher = _watcher()
    root = MagicMock()
    root.get_full_property.side_effect = RuntimeError("X server gone")
    watcher._display = MagicMock()
    watcher._root = root
    # Errors during reads must not propagate — the observer thread would
    # otherwise tear down the mainloop.
    assert watcher.current() is None


# ── The observer's shared emit, reached from the X11 origin ─────────

def test_emit_workspace_changed_from_x11_origin_shape():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # Nothing primed, so the first transition fires.
        obs._on_workspace(2, "x11-propertynotify")
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "view_changed"
        assert d.source == "atspi"
        assert d.source_id == "workspace-2"
        assert d.name == "active-workspace-changed"
        # origin_scope must be the same shape 4a-iii / 4a-i emit so
        # the brain sees one delta format across platforms.
        assert d.origin_scope == {
            "target": "host",
            "screen": None,
            "space_id": "ws:global:2",
        }
        assert d.extra["scope"] == "space"
        assert d.extra["from"] is None
        assert d.extra["to"] == "ws:global:2"
        assert d.extra["target"] == "host"
        assert d.extra["origin"] == "x11-propertynotify"
        assert obs._workspaces.last == 2
    finally:
        loop.close()


def test_emit_workspace_changed_dedupes_across_origins():
    # On GNOME-X11 the poll source and the PropertyNotify source both run.
    # Whichever sees a transition first emits; the other finds the tracker
    # already updated and stays silent.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._workspaces.prime(1)
        obs._on_workspace(2, "x11-propertynotify")
        assert len(emitted) == 1
        # GNOME poll observes the same workspace — must dedupe.
        obs._on_workspace(2, "poll")
        assert len(emitted) == 1
    finally:
        loop.close()


def test_emit_workspace_changed_carries_from_field_on_subsequent_emit():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._workspaces.prime(1)
        obs._on_workspace(2, "x11-propertynotify")
        d = emitted[-1]
        assert d.extra["from"] == "ws:global:1"
        assert d.extra["to"] == "ws:global:2"
    finally:
        loop.close()


# ── _on_readable drain semantics ────────────────────────────────────


class _StubXEvent:
    """Minimal Xlib event stand-in; only the attributes our handler
    inspects are populated."""

    def __init__(self, event_type: int, atom: int = 0):
        self.type = event_type
        self.atom = atom


def _stub_xlib(monkeypatch) -> None:
    """Force _Xconst so the type/atom comparisons hit our stubbed values
    regardless of whether python-xlib actually loaded."""
    fake_x = MagicMock()
    fake_x.PropertyNotify = 28  # canonical X11 event type, value doesn't matter
    monkeypatch.setattr(x11_mod, "_Xconst", fake_x)
    monkeypatch.setattr(x11_mod, "_XError", RuntimeError)


def test_on_readable_reports_on_current_desktop_propertynotify(monkeypatch):
    _stub_xlib(monkeypatch)
    seen: list[tuple[int, str]] = []
    watcher = _watcher(lambda index, origin: seen.append((index, origin)))
    watcher._atom = 42

    # Fake display: two events drained over two pending_events()
    # iterations (first call returns 1, processing then drops to 0).
    events = [_StubXEvent(28, atom=42)]
    pending_returns = [len(events), 0]

    display = MagicMock()
    display.pending_events.side_effect = lambda: pending_returns.pop(0)
    display.next_event.side_effect = events
    watcher._display = display

    # current() returns 5 (the destination workspace).
    monkeypatch.setattr(watcher, "current", lambda: 5)

    assert watcher._on_readable(99, 0) is True
    assert seen == [(5, "x11-propertynotify")]


def test_on_readable_ignores_unrelated_propertynotify(monkeypatch):
    # Many other PropertyNotify events fire on the root (e.g. EWMH
    # _NET_ACTIVE_WINDOW); we must ignore everything except our atom.
    _stub_xlib(monkeypatch)
    seen: list[tuple[int, str]] = []
    watcher = _watcher(lambda index, origin: seen.append((index, origin)))
    watcher._atom = 42

    events = [
        _StubXEvent(28, atom=999),  # wrong atom — ignore
        _StubXEvent(99, atom=42),   # wrong event type — ignore
    ]
    pending_returns = [2, 1, 0]

    display = MagicMock()
    display.pending_events.side_effect = lambda: pending_returns.pop(0)
    display.next_event.side_effect = events
    watcher._display = display

    # If our filter were broken this would have been called.
    watcher.current = MagicMock(return_value=5)

    watcher._on_readable(99, 0)
    assert seen == []
    watcher.current.assert_not_called()


def test_on_readable_with_none_display_removes_watch():
    # Display gone (post-teardown). Returning False removes the GLib IO
    # watch so we don't get re-armed forever.
    assert _watcher()._on_readable(99, 0) is False


def test_on_readable_swallows_exceptions(monkeypatch):
    _stub_xlib(monkeypatch)
    watcher = _watcher()
    display = MagicMock()
    display.pending_events.side_effect = RuntimeError("boom")
    watcher._display = display
    # MUST not propagate — the GLib mainloop has no exception handler at
    # the callback boundary.
    assert watcher._on_readable(99, 0) is True
