"""Tests for the KDE kwin VirtualDesktopManager workspace source (Phase 4d).

The watcher is signal-driven over the session bus; these tests stub the bus
so the UUID→index resolution, the stale-map refresh and the callback's
exception isolation are exercised without a live kwin. End-to-end lives on
the KDE smoke hosts.

The source reports a workspace index; turning that into a delta is
LinuxObserver's shared `_on_workspace`, which the shape tests drive.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from conductor_desktop.core.perception.deltas.workspace import kde

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not kde._GIO_AVAILABLE,
    reason="Linux with PyGObject: the watcher builds real GLib variants",
)


from conductor_desktop.core.perception.deltas.linux_observer import LinuxObserver
from conductor_desktop.core.perception.deltas.workspace.base import SessionBus
from conductor_desktop.core.perception.deltas.workspace.kde import KdeWorkspaceWatcher


class _StubBus(SessionBus):
    """A SessionBus that hands out a connection we control (or none)."""

    def __init__(self, connection=None):
        super().__init__(None)
        self._bus = connection
        self._tried = True


def _watcher(connection=None, on_workspace=None) -> KdeWorkspaceWatcher:
    return KdeWorkspaceWatcher(
        on_workspace or (lambda index, origin: None), _StubBus(connection),
    )


# ── UUID → index map refresh ────────────────────────────────────────


def _stub_desktops_reply(desktops: list[tuple[int, str, str]]):
    """Build a fake reply object matching what kwin returns from
    Properties.Get("desktops"). The shape is (variant<a(iss)>,)."""
    reply = MagicMock()
    reply.unpack.return_value = (desktops,)
    return reply


def test_refresh_desktop_map_builds_uuid_index():
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([
        (0, "aaa-uuid", "Desktop 1"),
        (1, "bbb-uuid", "Workshop"),
        (2, "ccc-uuid", "Email"),
    ])
    watcher = _watcher(bus)
    assert watcher.refresh_desktop_map() is True
    assert watcher._uuid_to_index == {
        "aaa-uuid": 0,
        "bbb-uuid": 1,
        "ccc-uuid": 2,
    }


def test_refresh_desktop_map_no_bus():
    watcher = _watcher(None)
    assert watcher.refresh_desktop_map() is False
    assert watcher._uuid_to_index == {}


def test_refresh_desktop_map_swallows_dbus_error():
    bus = MagicMock()
    bus.call_sync.side_effect = RuntimeError("kwin not on bus")
    # KDE not present → False, no crash.
    assert _watcher(bus).refresh_desktop_map() is False


# ── current desktop read ────────────────────────────────────────────


def test_current_resolves_uuid():
    reply = MagicMock()
    reply.unpack.return_value = ("bbb-uuid",)
    bus = MagicMock()
    bus.call_sync.return_value = reply
    watcher = _watcher(bus)
    watcher._uuid_to_index = {"aaa-uuid": 0, "bbb-uuid": 1}
    assert watcher.current() == 1


def test_current_unknown_uuid_returns_none():
    reply = MagicMock()
    reply.unpack.return_value = ("zzz-stale-uuid",)
    bus = MagicMock()
    bus.call_sync.return_value = reply
    watcher = _watcher(bus)
    watcher._uuid_to_index = {"aaa-uuid": 0}
    assert watcher.current() is None


# ── currentChanged signal handling ──────────────────────────────────


def test_on_current_changed_reports_index():
    seen: list[tuple[int, str]] = []
    watcher = _watcher(on_workspace=lambda index, origin: seen.append((index, origin)))
    watcher._uuid_to_index = {"aaa-uuid": 0, "bbb-uuid": 1}

    params = MagicMock()
    params.unpack.return_value = ("bbb-uuid",)
    watcher._on_current_changed(None, None, None, None, None, params)
    assert seen == [(1, "kwin-dbus")]


def test_on_current_changed_unknown_uuid_refreshes_then_reports():
    # Stale map case: kwin emits currentChanged before our cached map has
    # the new desktop. We must refresh once and retry.
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([
        (0, "aaa-uuid", "Desktop 1"),
        (1, "bbb-uuid", "Workshop"),
    ])
    seen: list[tuple[int, str]] = []
    watcher = _watcher(bus, lambda index, origin: seen.append((index, origin)))
    watcher._uuid_to_index = {"aaa-uuid": 0}  # stale, missing bbb

    params = MagicMock()
    params.unpack.return_value = ("bbb-uuid",)
    watcher._on_current_changed(None, None, None, None, None, params)
    assert seen == [(1, "kwin-dbus")]


def test_on_current_changed_uuid_still_unknown_after_refresh_drops():
    # Pathological: refresh succeeded but the new UUID still isn't in the
    # map (timing race or kwin bug). Must not report, must not crash.
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([(0, "aaa-uuid", "Desktop 1")])
    seen: list[tuple[int, str]] = []
    watcher = _watcher(bus, lambda index, origin: seen.append((index, origin)))
    watcher._uuid_to_index = {"aaa-uuid": 0}

    params = MagicMock()
    params.unpack.return_value = ("zzz-uuid",)
    watcher._on_current_changed(None, None, None, None, None, params)
    assert seen == []


def test_on_current_changed_payload_unpack_failure_swallowed():
    seen: list[tuple[int, str]] = []
    watcher = _watcher(on_workspace=lambda index, origin: seen.append((index, origin)))
    watcher._uuid_to_index = {"aaa": 0}

    params = MagicMock()
    params.unpack.side_effect = RuntimeError("malformed variant")
    # MUST not raise — the GDBus callback boundary has no Python handler.
    watcher._on_current_changed(None, None, None, None, None, params)
    assert seen == []


# ── desktopCreated / desktopRemoved handlers ────────────────────────


def test_on_desktops_changed_refreshes_map():
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([
        (0, "aaa", "Desktop 1"),
        (1, "bbb", "New Desktop"),
    ])
    watcher = _watcher(bus)
    watcher._uuid_to_index = {"aaa": 0}
    watcher._on_desktops_changed(None, None, None, None, None, None)
    assert watcher._uuid_to_index == {"aaa": 0, "bbb": 1}


# ── start() declines when KDE / the bus is absent ───────────────────


def test_start_declines_without_a_bus():
    watcher = _watcher(None)
    assert watcher.start() is False
    assert watcher._sub_ids == []
    assert watcher._uuid_to_index == {}


def test_start_declines_when_kwin_absent():
    # KDE not on the bus → property read fails → we don't subscribe.
    bus = MagicMock()
    bus.call_sync.side_effect = RuntimeError("name not found")
    watcher = _watcher(bus)
    assert watcher.start() is False
    assert watcher._sub_ids == []
    bus.signal_subscribe.assert_not_called()


def test_start_attaches_three_subscriptions_when_kde_present():
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([
        (0, "aaa", "Desktop 1"),
        (1, "bbb", "Workshop"),
    ])
    bus.signal_subscribe.side_effect = [101, 102, 103]
    watcher = _watcher(bus)
    assert watcher.start() is True
    # Three subscriptions: currentChanged, desktopCreated, desktopRemoved
    assert watcher._sub_ids == [101, 102, 103]
    assert bus.signal_subscribe.call_count == 3


def test_start_does_not_depend_on_another_source_acquiring_the_bus():
    # KDE support used to require the GNOME poll's setup to have run first
    # and left a connection behind — on a machine with no GNOME.
    bus = MagicMock()
    bus.call_sync.return_value = _stub_desktops_reply([(0, "aaa", "Desktop 1")])
    bus.signal_subscribe.side_effect = [1, 2, 3]
    watcher = KdeWorkspaceWatcher(lambda index, origin: None, _StubBus(bus))
    assert watcher.start() is True


# ── Cross-platform shape parity ─────────────────────────────────────


def test_kde_view_changed_delta_matches_shape_of_other_observers():
    loop = asyncio.new_event_loop()
    try:
        obs = LinuxObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._workspaces.prime(0)
        obs._on_workspace(1, "kwin-dbus")
        d = emitted[0]
        # Mirrors macos / 4a-ii / 4a-iii / 4c invariants.
        assert d.kind == "view_changed"
        assert d.name == "active-workspace-changed"
        assert d.source_id == "workspace-1"
        assert d.origin_scope is not None
        assert set(d.origin_scope.keys()) == {"target", "screen", "space_id"}
        assert d.origin_scope["space_id"] == "ws:global:1"
        assert d.extra is not None
        assert d.extra["scope"] == "space"
        assert d.extra["target"] == "host"
        assert d.extra["origin"] == "kwin-dbus"
        assert d.extra["from"] == "ws:global:0"
        assert d.extra["to"] == "ws:global:1"
    finally:
        loop.close()
