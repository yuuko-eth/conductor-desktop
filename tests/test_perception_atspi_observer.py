"""Tests for perception.deltas.linux_observer.

Covers the parts that can be tested without a real GLib mainloop or a
live AT-SPI bridge: kind mapping, source-id derivation, Layer 1
retention, focused-state filter, callback exception isolation, and
StateDelta normalisation when fed synthetic AT-SPI events.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Skip the whole file on non-linux since it import-depends on PyGObject
# / Atspi typelibs via the module under test.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux-only: requires PyGObject + AT-SPI typelibs",
)


from conductor_desktop.core.perception.deltas.linux_observer import (
    _ATSPI_KIND_MAP,
    _ATSPI_SKIP_ROLES_FOR_VALUE,
    _ATSPI_VALUE_KEEP_MAX,
    LinuxObserver,
    _stable_source_id,
)


def _make_event(ev_type: str, *, role: str = "push button",
                name: str | None = "Save", any_data: object = None,
                detail1: int = 1, detail2: int = 0,
                attributes: dict | None = None,
                app_name: str = "gedit") -> MagicMock:
    """Synthesize an Atspi.Event-shaped object good enough for
    _handle_event to exercise the normalization path."""
    source = MagicMock()
    source.get_role_name.return_value = role
    source.get_name.return_value = name
    source.get_attributes.return_value = attributes or {}
    app = MagicMock()
    app.get_name.return_value = app_name
    source.get_application.return_value = app
    # Component / extents — return None for most tests; bbox-specific
    # tests can override.
    source.get_component.return_value = None

    event = MagicMock()
    event.type = ev_type
    event.source = source
    event.detail1 = detail1
    event.detail2 = detail2
    event.any_data = any_data
    return event


# ── Kind mapping ────────────────────────────────────────────────────

def test_focus_change_maps_to_focus_changed():
    assert _ATSPI_KIND_MAP["object:state-changed:focused"] == "focus_changed"


def test_text_changed_maps_to_value_changed():
    assert _ATSPI_KIND_MAP["object:text-changed:insert"] == "value_changed"
    assert _ATSPI_KIND_MAP["object:text-changed:delete"] == "value_changed"


def test_value_changed_maps_to_value_changed():
    assert _ATSPI_KIND_MAP["object:value-changed"] == "value_changed"


def test_state_changes_map_to_state_changed():
    assert _ATSPI_KIND_MAP["object:state-changed:checked"] == "state_changed"
    assert _ATSPI_KIND_MAP["object:state-changed:expanded"] == "state_changed"
    assert _ATSPI_KIND_MAP["object:state-changed:selected"] == "state_changed"


def test_children_changed_splits_into_add_and_remove():
    assert _ATSPI_KIND_MAP["object:children-changed:add"] == "node_added"
    assert _ATSPI_KIND_MAP["object:children-changed:remove"] == "node_removed"


def test_window_activate_maps_to_window_state_changed():
    assert _ATSPI_KIND_MAP["window:activate"] == "window_state_changed"


def test_window_create_destroy_map_to_node_add_remove():
    assert _ATSPI_KIND_MAP["window:create"] == "node_added"
    assert _ATSPI_KIND_MAP["window:destroy"] == "node_removed"


def test_unknown_event_returns_none():
    assert _ATSPI_KIND_MAP.get("object:future-event") is None


# ── Source ID stability ────────────────────────────────────────────

def test_source_id_uses_app_role_name_when_no_explicit_id():
    source = MagicMock()
    source.get_attributes.return_value = {}
    app = MagicMock()
    app.get_name.return_value = "gedit"
    source.get_application.return_value = app
    sid = _stable_source_id(source, "push button", "Save")
    assert sid == "gedit:push button:Save"


def test_source_id_prefers_explicit_id_when_present():
    source = MagicMock()
    source.get_attributes.return_value = {"id": "save-btn"}
    app = MagicMock()
    app.get_name.return_value = "gedit"
    source.get_application.return_value = app
    sid = _stable_source_id(source, "push button", "Save")
    assert sid == "gedit:save-btn"


def test_source_id_handles_missing_application():
    source = MagicMock()
    source.get_attributes.return_value = {}
    source.get_application.return_value = None
    sid = _stable_source_id(source, "push button", None)
    assert "?" in sid


# ── Layer 1 retention ──────────────────────────────────────────────

def test_skip_roles_includes_scroll_and_menu_chrome():
    assert "scroll bar" in _ATSPI_SKIP_ROLES_FOR_VALUE
    assert "scroll pane" in _ATSPI_SKIP_ROLES_FOR_VALUE
    assert "menu bar" in _ATSPI_SKIP_ROLES_FOR_VALUE
    assert "tool bar" in _ATSPI_SKIP_ROLES_FOR_VALUE


def test_skip_roles_includes_the_terminal():
    # A terminal emitting build output is the loudest single source on a
    # Linux desktop, and its content is on screen anyway.
    assert "terminal" in _ATSPI_SKIP_ROLES_FOR_VALUE


def test_value_change_in_a_terminal_dropped():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_event(_make_event("object:text-changed:insert",
                                      role="terminal", any_data="make[1]: ..."))
        assert emitted == []
    finally:
        loop.close()


def test_window_event_from_a_terminal_kept():
    # The skip is value-specific: "the terminal window came up" still counts.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_event(_make_event("window:activate", role="terminal"))
        assert len(emitted) == 1
        assert emitted[0].kind == "window_state_changed"
    finally:
        loop.close()


# ── Value length cap (the twin of macOS's _AX_VALUE_KEEP_MAX) ───────

def test_short_value_is_kept():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_event(_make_event("object:text-changed:insert",
                                      role="entry", any_data="hello",
                                      detail2=5))
        assert emitted[0].value == "hello"
    finally:
        loop.close()


def test_pasted_buffer_is_dropped_by_the_length_cap():
    # A paste is one event and unbounded; without the cap the whole buffer
    # rides in the ring for as long as the delta lives.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        big = "x" * (_ATSPI_VALUE_KEEP_MAX + 1)
        obs._handle_event(_make_event("object:text-changed:insert",
                                      role="entry", any_data=big,
                                      detail2=len(big)))
        # The event still fires — the agent learns the field changed.
        assert len(emitted) == 1
        assert emitted[0].value is None
    finally:
        loop.close()


def test_length_cap_applies_when_detail2_is_unset():
    # detail2 is the cheap pre-check, not the authority: bindings that
    # leave it at 0 must still hit the cap on the stringified value.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_event(_make_event("object:text-changed:insert",
                                      role="entry",
                                      any_data="x" * (_ATSPI_VALUE_KEEP_MAX + 1),
                                      detail2=0))
        assert emitted[0].value is None
    finally:
        loop.close()


def test_value_change_on_scroll_bar_dropped():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event("object:value-changed", role="scroll bar")
        obs._handle_event(ev)
        assert emitted == []
    finally:
        loop.close()


def test_focus_change_on_scroll_bar_kept():
    # Same role, different kind: focus_changed should still fire on a
    # scrollable container. The skip is value-specific.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event("object:state-changed:focused", role="scroll bar",
                         detail1=1)
        obs._handle_event(ev)
        assert len(emitted) == 1
        assert emitted[0].kind == "focus_changed"
        assert emitted[0].role == "scroll bar"
    finally:
        loop.close()


def test_unknown_event_dropped():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_event(_make_event("object:future-event"))
        assert emitted == []
    finally:
        loop.close()


# ── Focused-state filter ────────────────────────────────────────────

def test_focused_state_loss_dropped():
    # AT-SPI fires focused twice per shift (lost on prev, gained on
    # next). detail1==0 means "no longer focused" — drop, otherwise we
    # see one event per actual focus change becoming two.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event("object:state-changed:focused", detail1=0)
        obs._handle_event(ev)
        assert emitted == []
    finally:
        loop.close()


def test_focused_state_gain_emits():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event("object:state-changed:focused", detail1=1)
        obs._handle_event(ev)
        assert len(emitted) == 1
        assert emitted[0].kind == "focus_changed"
    finally:
        loop.close()


# ── StateDelta shape ────────────────────────────────────────────────

def test_text_insert_produces_normalized_delta():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event(
            "object:text-changed:insert",
            role="text", name="Document",
            any_data="hello",
            attributes={"id": "main-text"},
            app_name="gedit",
        )
        obs._handle_event(ev)
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "value_changed"
        assert d.source == "atspi"
        assert d.role == "text"
        assert d.name == "Document"
        assert d.value == "hello"
        # source_id keys on explicit AT-SPI 'id' attribute when present.
        assert d.source_id == "gedit:main-text"
    finally:
        loop.close()


def test_state_changed_delta_carries_state_value():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # checked state going truthy
        ev = _make_event(
            "object:state-changed:checked",
            role="check box", name="Enable feature",
            detail1=1,
        )
        obs._handle_event(ev)
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "state_changed"
        assert d.value == "1"
    finally:
        loop.close()


def test_window_activate_emits_window_state_changed():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = _make_event("window:activate", role="frame", name="gedit")
        obs._handle_event(ev)
        assert len(emitted) == 1
        assert emitted[0].kind == "window_state_changed"
    finally:
        loop.close()


# ── Callback exception isolation ───────────────────────────────────

def test_handle_event_swallows_exceptions(monkeypatch):
    # If anything inside _handle_event raises, propagating across the
    # GLib boundary would tear down the mainloop. Must be caught and
    # logged.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        # Force the role-name lookup to blow up.
        ev = _make_event("object:value-changed")
        ev.source.get_role_name.side_effect = RuntimeError("boom")
        # Should NOT raise.
        obs._handle_event(ev)
    finally:
        loop.close()


def test_handle_event_with_none_source_dropped():
    # Defensive: every AT-SPI event has a source, but a destroyed-
    # mid-dispatch element could yield None. Don't crash.
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = MagicMock()
        ev.type = "object:value-changed"
        ev.source = None
        ev.detail1 = 0
        ev.any_data = None
        obs._handle_event(ev)
        assert emitted == []
    finally:
        loop.close()


# ── Lifecycle (without spinning a real GLib mainloop) ──────────────

async def test_double_start_is_idempotent(monkeypatch):
    loop = asyncio.get_running_loop()
    obs = LinuxObserver(loop)
    monkeypatch.setattr(obs, "_run", lambda: obs._ready_event.set())
    await obs.start()
    first_thread = obs._thread
    await obs.start()  # no-op
    assert obs._thread is first_thread
    await obs.stop()


async def test_stop_without_start_is_safe():
    obs = LinuxObserver(asyncio.get_running_loop())
    await obs.stop()  # must not raise


async def test_start_propagates_setup_error(monkeypatch):
    obs = LinuxObserver(asyncio.get_running_loop())

    def boom():
        obs._start_error = RuntimeError("simulated typelib missing")
        obs._ready_event.set()
    monkeypatch.setattr(obs, "_run", boom)

    with pytest.raises(RuntimeError, match="simulated typelib missing"):
        await obs.start()
