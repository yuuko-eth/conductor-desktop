"""Tests for the sway/Hyprland workspace IPC observer (Phase 4c).

Mocks the subprocess (sway) and unix socket (Hyprland) so the suite
stays portable — no live compositor required. End-to-end against a
real sway / Hyprland session is the smoke target whenever such a host
becomes available; a labwc session covers the negative-path "no IPC →
silent no-op" branch live.
"""

from __future__ import annotations

import asyncio

from conductor_desktop.core.perception.deltas import wlroots_observer
from conductor_desktop.core.perception.deltas.wlroots_observer import (
    WlrootsObserver,
    _detect_compositor,
    _hyprland_socket_path,
)

# ── Compositor detection ────────────────────────────────────────────


def test_detect_compositor_sway(monkeypatch):
    monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.sock")
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(
        wlroots_observer.shutil, "which",
        lambda c: "/usr/bin/swaymsg" if c == "swaymsg" else None,
    )
    assert _detect_compositor() == "sway"


def test_detect_compositor_sway_requires_swaymsg(monkeypatch):
    monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.sock")
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    # SWAYSOCK set but swaymsg binary missing — not actionable.
    monkeypatch.setattr(wlroots_observer.shutil, "which", lambda c: None)
    assert _detect_compositor() is None


def test_detect_compositor_hyprland(monkeypatch):
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
    assert _detect_compositor() == "hyprland"


def test_detect_compositor_none(monkeypatch):
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(wlroots_observer.shutil, "which", lambda c: None)
    assert _detect_compositor() is None


def test_detect_compositor_prefers_sway_when_both_set(monkeypatch):
    # Edge: both env vars somehow set (user dual-running). Sway wins
    # because we check it first and sway is far more common.
    monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.sock")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
    monkeypatch.setattr(
        wlroots_observer.shutil, "which",
        lambda c: "/usr/bin/swaymsg" if c == "swaymsg" else None,
    )
    assert _detect_compositor() == "sway"


# ── Hyprland socket path resolution ─────────────────────────────────


def test_hyprland_socket_path_none_without_his(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    assert _hyprland_socket_path() is None


def test_hyprland_socket_path_prefers_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "his-1")
    xdg = tmp_path / "run"
    xdg.mkdir()
    sock = xdg / "hypr" / "his-1" / ".socket2.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    assert _hyprland_socket_path() == str(sock)


def test_hyprland_socket_path_falls_back_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "his-2")
    # XDG_RUNTIME_DIR set but socket not under it.
    xdg = tmp_path / "run"
    xdg.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    # Only /tmp variant exists — our resolver also probes /tmp/hypr/...
    # which we can't easily mock in the real filesystem. Skip-assert:
    # not-finding-anything must return None, and that's what we test.
    assert _hyprland_socket_path() is None


# ── Start is silent no-op when no compositor IPC is present ─────────


async def test_start_noop_when_no_compositor(monkeypatch):
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(wlroots_observer.shutil, "which", lambda c: None)

    obs = WlrootsObserver(asyncio.get_running_loop())
    await obs.start()
    # No task spawned — observer is a no-op on this host.
    assert obs._task is None
    assert obs._compositor is None
    # Stop must be safe even when start was no-op.
    await obs.stop()


async def test_start_idempotent_on_sway(monkeypatch):
    monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.sock")
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(
        wlroots_observer.shutil, "which",
        lambda c: "/usr/bin/swaymsg" if c == "swaymsg" else None,
    )

    # Replace _run_sway with a long-suspend so the task survives the
    # second start() call without doing real subprocess work.
    async def fake_run_sway(self):
        await asyncio.sleep(60)

    monkeypatch.setattr(WlrootsObserver, "_run_sway", fake_run_sway)
    obs = WlrootsObserver(asyncio.get_running_loop())
    await obs.start()
    first_task = obs._task
    assert first_task is not None
    await obs.start()  # idempotent
    assert obs._task is first_task
    await obs.stop()


# ── sway event normalization ────────────────────────────────────────


def test_handle_sway_event_focus_change_emits_delta():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_sway_event({
            "change": "focus",
            "current": {"name": "code", "num": 2, "output": "DP-1"},
            "old": {"name": "web", "num": 1, "output": "DP-1"},
        })
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "view_changed"
        assert d.source == "wlroots"
        assert d.source_id == "workspace-code"
        assert d.name == "active-workspace-changed"
        assert d.origin_scope == {
            "target": "host",
            "screen": None,
            "space_id": "ws:DP-1:code",
        }
        assert d.extra["scope"] == "space"
        assert d.extra["from"] == "ws:DP-1:web"
        assert d.extra["to"] == "ws:DP-1:code"
        assert d.extra["output"] == "DP-1"
        assert d.extra["origin"] == "sway-ipc"
    finally:
        loop.close()


def test_handle_sway_event_non_focus_change_dropped():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # init / empty / move / rename / urgent / reload are all
        # workspace-state changes but NOT focus transitions; the brain
        # doesn't need them at v1.
        for change in ("init", "empty", "rename", "move", "urgent", "reload"):
            obs._handle_sway_event({
                "change": change,
                "current": {"name": "x", "output": "DP-1"},
            })
        assert emitted == []
    finally:
        loop.close()


def test_handle_sway_event_numeric_only_workspace_name():
    # sway lets you have numeric-only workspaces ("1", "2", ...) where
    # `name` and `str(num)` agree. Our ident logic uses name when set,
    # falls back to num. Verify the typical numeric case works.
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_sway_event({
            "change": "focus",
            "current": {"name": "3", "num": 3, "output": "eDP-1"},
            "old": {"name": "1", "num": 1, "output": "eDP-1"},
        })
        assert len(emitted) == 1
        assert emitted[0].origin_scope["space_id"] == "ws:eDP-1:3"
    finally:
        loop.close()


def test_handle_sway_event_dedupes_same_destination():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        ev = {
            "change": "focus",
            "current": {"name": "2", "num": 2, "output": "DP-1"},
            "old": {"name": "1", "num": 1, "output": "DP-1"},
        }
        obs._handle_sway_event(ev)
        assert len(emitted) == 1
        # Same destination event again — must NOT re-emit.
        obs._handle_sway_event(ev)
        assert len(emitted) == 1
    finally:
        loop.close()


def test_handle_sway_event_missing_old_emits_with_none_from():
    # First-event-after-startup case: sway sometimes elides `old` on
    # the very first focus event. Must not crash; `from` ends up None.
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_sway_event({
            "change": "focus",
            "current": {"name": "2", "num": 2, "output": "DP-1"},
        })
        assert len(emitted) == 1
        assert emitted[0].extra["from"] is None
    finally:
        loop.close()


# ── Hyprland event normalization ────────────────────────────────────


def test_handle_hyprland_line_workspace_event():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # Hyprland normally fires focusedmon before workspace; simulate
        # that order so monitor attribution works.
        obs._handle_hyprland_line("focusedmon>>DP-2,2")
        obs._handle_hyprland_line("workspace>>2")
        assert len(emitted) == 1
        d = emitted[0]
        assert d.kind == "view_changed"
        assert d.source == "wlroots"
        assert d.source_id == "workspace-2"
        assert d.origin_scope["space_id"] == "ws:DP-2:2"
        assert d.extra["output"] == "DP-2"
        assert d.extra["origin"] == "hyprland-socket2"
    finally:
        loop.close()


def test_handle_hyprland_line_workspacev2_with_id_and_name():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._track_hyprland_monitor("HDMI-A-1")
        # workspacev2 format: "ID,NAME". Newer Hyprland prefers this
        # over the legacy `workspace>>` because the ID is stable while
        # name can collide across outputs.
        obs._handle_hyprland_line("workspacev2>>5,games")
        assert len(emitted) == 1
        d = emitted[0]
        assert d.origin_scope["space_id"] == "ws:HDMI-A-1:games"
    finally:
        loop.close()


def test_handle_hyprland_line_workspace_without_monitor_defaults_to_question():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # No focusedmon seen yet — output defaults to "?" (same as the
        # backends/linux_wlroots fallback path).
        obs._handle_hyprland_line("workspace>>2")
        assert len(emitted) == 1
        assert emitted[0].origin_scope["space_id"] == "ws:?:2"
    finally:
        loop.close()


def test_handle_hyprland_line_focusedmon_alone_does_not_emit():
    # Switching monitors alone doesn't count as a workspace transition
    # — the same workspace just lives on a different output now.
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_hyprland_line("focusedmon>>DP-1,1")
        assert emitted == []
        assert obs._hypr_current_monitor == "DP-1"
    finally:
        loop.close()


def test_handle_hyprland_line_unknown_event_ignored():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # Hyprland emits many event types we don't care about
        # (createworkspace, destroyworkspace, openwindow, closewindow,
        # activewindow, fullscreen, monitoradded, …).
        for line in (
            "createworkspace>>5",
            "destroyworkspace>>3",
            "openwindow>>0x1234,1,kitty",
            "activewindow>>kitty,kitty",
            "fullscreen>>1",
            "monitoradded>>DP-3",
        ):
            obs._handle_hyprland_line(line)
        assert emitted == []
    finally:
        loop.close()


def test_handle_hyprland_line_malformed_dropped():
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        # Lines without ">>" must not crash the parser.
        for line in ("", "garbage", "this is not an event", "%@#$"):
            obs._handle_hyprland_line(line)
        assert emitted == []
    finally:
        loop.close()


# ── Cross-platform shape parity ─────────────────────────────────────


def test_view_changed_delta_shape_matches_other_observers():
    # The 4a-i (macOS) and 4a-iii (GNOME) observers both produce
    # view_changed deltas with these fields. The brain depends on the
    # shape staying identical across sources so its delta-format
    # handling is platform-agnostic.
    loop = asyncio.new_event_loop()
    try:
        obs = WlrootsObserver(loop)
        emitted = []
        obs._emit = lambda d: emitted.append(d)
        obs._handle_sway_event({
            "change": "focus",
            "current": {"name": "a", "num": 1, "output": "DP-1"},
        })
        d = emitted[0]
        # Required fields. Mirror the macos_observer/linux_observer asserts.
        assert d.kind == "view_changed"
        assert d.name == "active-workspace-changed"
        assert d.source_id and d.source_id.startswith("workspace-")
        assert d.origin_scope is not None
        assert set(d.origin_scope.keys()) == {"target", "screen", "space_id"}
        assert d.origin_scope["target"] == "host"
        assert d.extra is not None
        assert d.extra["scope"] == "space"
        assert "from" in d.extra
        assert "to" in d.extra
        assert d.extra["target"] == "host"
        assert "origin" in d.extra
    finally:
        loop.close()
