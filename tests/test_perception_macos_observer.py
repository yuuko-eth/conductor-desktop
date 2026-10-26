"""Tests for perception.deltas.macos_observer.

Covers the parts that can be tested without a real CFRunLoop or a live
macOS desktop: kind mapping, source-id derivation, Layer 1 retention,
callback exception isolation, and the StateDelta normalisation path
when fed synthetic AX events.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Skip the whole file on non-darwin since it import-depends on
# ApplicationServices via the module under test.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS-only: requires pyobjc-framework-ApplicationServices",
)


from conductor_desktop.core.perception.deltas import macos_observer
from conductor_desktop.core.perception.deltas.macos_observer import (
    _AX_KIND_MAP,
    _AX_SKIP_ROLES_FOR_VALUE,
    _OBSERVERS_BY_ID,
    MacOSObserver,
    _ax_callback,
    _stable_source_id,
)

# ── Kind mapping ────────────────────────────────────────────────────

def test_focus_change_maps_to_focus_changed():
    assert _AX_KIND_MAP["AXFocusedUIElementChanged"] == "focus_changed"


def test_value_change_maps_to_value_changed():
    assert _AX_KIND_MAP["AXValueChanged"] == "value_changed"


def test_window_focus_maps_to_window_state_changed():
    # App-level / window-level events all collapse onto window_state_changed.
    # Element-level focus is a separate kind.
    assert _AX_KIND_MAP["AXFocusedWindowChanged"] == "window_state_changed"
    assert _AX_KIND_MAP["AXMainWindowChanged"] == "window_state_changed"
    assert _AX_KIND_MAP["AXApplicationActivated"] == "window_state_changed"


def test_destroyed_element_maps_to_node_removed():
    assert _AX_KIND_MAP["AXUIElementDestroyed"] == "node_removed"


def test_unknown_notification_returns_none():
    # Important: callback handles unknown gracefully (kind=None → drop).
    assert _AX_KIND_MAP.get("SomeFutureNotification") is None


# ── Source ID stability ────────────────────────────────────────────

def test_source_id_uses_pid_and_role_name_when_no_ax_identifier(monkeypatch):
    # When AXIdentifier is absent, fall back to (pid, role, name) — usually
    # stable enough for coalesce within a tool-call frame.
    monkeypatch.setattr(macos_observer, "_safe_attr",
                        lambda el, name: None)  # AXIdentifier absent
    sid = _stable_source_id(1234, MagicMock(), "AXButton", "Save")
    assert sid == "1234:AXButton:Save"


def test_source_id_prefers_ax_identifier_when_present(monkeypatch):
    def fake_attr(el, name):
        return "submit-btn-1" if name == "AXIdentifier" else None
    monkeypatch.setattr(macos_observer, "_safe_attr", fake_attr)
    sid = _stable_source_id(1234, MagicMock(), "AXButton", "Save")
    assert sid == "1234:submit-btn-1"


def test_source_id_handles_missing_pid(monkeypatch):
    monkeypatch.setattr(macos_observer, "_safe_attr", lambda el, n: None)
    sid = _stable_source_id(None, MagicMock(), "AXButton", None)
    # Falls back gracefully, doesn't crash.
    assert "?" in sid


# ── Layer 1 retention ──────────────────────────────────────────────

def test_skip_roles_includes_scroll_and_menu_chrome():
    # Roles that fire value-changed constantly during scroll / menu activity.
    assert "AXScrollArea" in _AX_SKIP_ROLES_FOR_VALUE
    assert "AXScrollBar" in _AX_SKIP_ROLES_FOR_VALUE
    assert "AXMenuBar" in _AX_SKIP_ROLES_FOR_VALUE


def test_value_change_on_scroll_area_dropped(monkeypatch):
    # Layer 1: value_changed on AXScrollArea is filtered before _emit.
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        # Fake _safe_attr: role=AXScrollArea, others empty
        monkeypatch.setattr(macos_observer, "_safe_attr",
                            lambda el, n: "AXScrollArea" if n == "AXRole" else None)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXValueChanged")
        assert emitted == []
    finally:
        loop.close()


def test_focus_change_on_scroll_area_kept(monkeypatch):
    # Same role, different kind: focus_changed should still fire even on
    # a scrollable container. The skip is value-specific.
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        monkeypatch.setattr(macos_observer, "_safe_attr",
                            lambda el, n: "AXScrollArea" if n == "AXRole" else None)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXFocusedUIElementChanged")
        assert len(emitted) == 1
        assert emitted[0].kind == "focus_changed"
        assert emitted[0].role == "AXScrollArea"
    finally:
        loop.close()


def test_unknown_notification_dropped(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        monkeypatch.setattr(macos_observer, "_safe_attr", lambda el, n: None)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXFutureNotification")
        assert emitted == []
    finally:
        loop.close()


# ── StateDelta shape ────────────────────────────────────────────────

def test_value_change_produces_normalized_delta(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        obs._current_pid = 4321
        attrs = {
            "AXRole": "AXTextField",
            "AXTitle": "Username",
            "AXValue": "alice",
            "AXIdentifier": "user-input",
        }
        monkeypatch.setattr(macos_observer, "_safe_attr",
                            lambda el, n: attrs.get(n))
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXValueChanged")
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "value_changed"
        assert d.source == "ax"
        assert d.role == "AXTextField"
        assert d.name == "Username"
        assert d.value == "alice"
        # source_id keys on AXIdentifier when present.
        assert d.source_id == "4321:user-input"
    finally:
        loop.close()


def test_long_text_buffer_skips_axvalue_copy(monkeypatch):
    # Regression: copying AXValue leaks natively under PyObjC, so a
    # Terminal-sized buffer must be length-checked and never copied.
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        attrs = {"AXRole": "AXTextArea", "AXNumberOfCharacters": 1_000_000,
                 "AXValue": "x" * 1_000_000}
        requested = []

        def fake_attr(el, n):
            requested.append(n)
            return attrs.get(n)

        monkeypatch.setattr(macos_observer, "_safe_attr", fake_attr)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXValueChanged")
        assert "AXValue" not in requested
        assert len(emitted) == 1 and emitted[0].value is None
    finally:
        loop.close()


def test_handle_event_falls_back_to_description_when_no_title(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        obs = MacOSObserver(loop)
        attrs = {
            "AXRole": "AXButton",
            "AXTitle": None,
            "AXDescription": "Submit form",
            "AXValue": None,
        }
        monkeypatch.setattr(macos_observer, "_safe_attr",
                            lambda el, n: attrs.get(n))
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_ax_event(MagicMock(), "AXFocusedUIElementChanged")
        assert len(emitted) == 1
        assert emitted[0].name == "Submit form"
    finally:
        loop.close()


# ── Callback exception isolation ───────────────────────────────────

def test_ax_callback_swallows_exceptions():
    # If the callback raises, propagating across the C boundary would
    # crash the process. Must be caught and logged. Refcon must be an
    # int (id of a registered observer) — pass a fake one through the
    # registry so the lookup succeeds, then make _handle_ax_event raise.
    raised_observer = MagicMock()
    raised_observer._handle_ax_event.side_effect = RuntimeError("boom")
    fake_id = 999_999_001
    _OBSERVERS_BY_ID[fake_id] = raised_observer
    try:
        # Should NOT raise.
        _ax_callback(MagicMock(), MagicMock(), "AXValueChanged", fake_id)
        assert raised_observer._handle_ax_event.call_count == 1
    finally:
        _OBSERVERS_BY_ID.pop(fake_id, None)


def test_ax_callback_handles_unregistered_refcon():
    # An unknown refcon id (race during start/stop, stale C-side reference)
    # should be silently ignored rather than raising.
    _ax_callback(MagicMock(), MagicMock(), "AXValueChanged", 999_999_999)


def test_ax_callback_handles_none_refcon():
    # Defensive: if for some reason refcon is None (shouldn't happen in
    # production), the callback returns silently rather than raising.
    _ax_callback(MagicMock(), MagicMock(), "AXValueChanged", None)


def test_ax_callback_handles_empty_notification_name():
    _ax_callback(MagicMock(), MagicMock(), "", 999_999_999)
    _ax_callback(MagicMock(), MagicMock(), None, 999_999_999)


# ── Lifecycle (without spinning a real CFRunLoop) ──────────────────

async def test_double_start_is_idempotent(monkeypatch):
    # Calling start() twice should not spawn two threads or raise.
    loop = asyncio.get_running_loop()
    obs = MacOSObserver(loop)
    # Mock the runloop setup so we don't actually spin it up.
    monkeypatch.setattr(obs, "_run", lambda: obs._ready_event.set())
    await obs.start()
    first_thread = obs._thread
    await obs.start()  # no-op
    assert obs._thread is first_thread
    await obs.stop()


async def test_stop_without_start_is_safe():
    obs = MacOSObserver(asyncio.get_running_loop())
    await obs.stop()  # must not raise


async def test_start_propagates_setup_error(monkeypatch):
    obs = MacOSObserver(asyncio.get_running_loop())

    def boom():
        obs._start_error = RuntimeError("simulated framework missing")
        obs._ready_event.set()
    monkeypatch.setattr(obs, "_run", boom)

    with pytest.raises(RuntimeError, match="simulated framework missing"):
        await obs.start()


# ── Failed-subscription memo ───────────────────────────────────────

async def test_failed_pid_memo_is_dropped_when_the_app_is_switched_away():
    # The memo stops the 500ms poll retrying forever; it is not meant to
    # blacklist an app for the process lifetime. An app that failed because
    # it was still launching, or because TCC hadn't been granted yet,
    # deserves a retry the next time it is activated — and a recycled pid
    # must not inherit someone else's verdict.
    obs = MacOSObserver(asyncio.get_running_loop())
    obs._failed_pids.add(4242)
    obs._teardown_app_observer(4242)
    assert 4242 not in obs._failed_pids


async def test_teardown_clears_per_run_app_state():
    obs = MacOSObserver(asyncio.get_running_loop())
    obs._failed_pids.add(1)
    obs._current_pid = 1
    obs._thread_teardown()
    assert obs._failed_pids == set()
    assert obs._current_pid is None


def test_the_ax_messaging_timeout_is_bounded():
    # Handling one event makes up to six AX round trips on the same thread
    # as the app-switch and Space polling, so AX's own 6s default lets one
    # hung app stall everything this observer watches.
    assert 0 < macos_observer._AX_MESSAGING_TIMEOUT_SECONDS <= 3.0


# ── Space de-dupe is thread-safe ───────────────────────────────────

async def test_space_dedupe_emits_once_when_poll_and_notification_race():
    # _poll_active_space runs on the observer thread; the NSWorkspace
    # notification arrives on whichever thread Apple picks. One transition
    # must still be one delta.
    import threading

    obs = MacOSObserver(asyncio.get_running_loop())
    emitted = []
    obs._emit = lambda d: emitted.append(d)
    obs._spaces.prime(1)

    start = threading.Barrier(8)

    def race():
        start.wait()
        obs._emit_space_changed(2, origin="poll")

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(emitted) == 1
    assert emitted[0].extra["to"] == "2"
    assert emitted[0].extra["from"] == "1"
