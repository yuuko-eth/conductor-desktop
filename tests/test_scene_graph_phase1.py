"""Phase 1 of the scene-graph rollout — see https://docs.conductor-mcp.io/concepts/scene-graph/.

Covers the additive Phase 1 surface that exists across every backend:

* The cross-platform ``attribute_monitor`` hit-test helper.
* Layer/feel mapping helpers per backend (pure functions, no syscalls).
* Schema invariants on the ``list_windows`` dict shape — new optional
  fields are present alongside the legacy ones.

Backend ``list_windows`` / ``list_screens`` integration tests live in the
per-platform test modules (``test_executor_xdotool.py`` etc.) because
they need real WM access; the unit tests here are platform-agnostic.
"""

from __future__ import annotations

import pytest

from conductor_desktop.core.backends import _common

# ── attribute_monitor ──────────────────────────────────────────────


SCREENS_LAPTOP_ONLY = [
    {"index": 0, "name": "Built-in", "bounds": [0, 0, 1280, 800],
     "scale": 2.0, "primary": True},
]

SCREENS_DUAL = [
    {"index": 0, "name": "Built-in", "bounds": [0, 0, 1280, 800],
     "scale": 2.0, "primary": True},
    {"index": 1, "name": "External", "bounds": [1280, 0, 1920, 1080],
     "scale": 1.0, "primary": False},
]


def test_attribute_monitor_single_screen_hits():
    # Window centered well inside the laptop screen.
    assert _common.attribute_monitor([100, 100, 400, 300], SCREENS_LAPTOP_ONLY) == 0


def test_attribute_monitor_off_screen_returns_minus_one():
    # Window's center is well outside the only screen — e.g. minimized
    # to a parking position the WM uses for offscreen state.
    assert _common.attribute_monitor([-2000, -2000, 100, 100], SCREENS_LAPTOP_ONLY) == -1


def test_attribute_monitor_picks_correct_screen_in_dual():
    # Center at (1280+960, 540) — squarely inside the external 1920×1080.
    assert _common.attribute_monitor([1280, 0, 1920, 1080], SCREENS_DUAL) == 1
    # Window straddling both: center decides. Bounds [1180,0,200,200] →
    # center (1280, 100) — on the boundary. Our `<` upper bound puts it
    # on the right screen (1).
    assert _common.attribute_monitor([1180, 0, 200, 200], SCREENS_DUAL) == 1


def test_attribute_monitor_empty_inputs():
    assert _common.attribute_monitor([], SCREENS_DUAL) == -1
    assert _common.attribute_monitor([0, 0, 100, 100], []) == -1


# ── Per-backend layer mapping (pure helpers, no syscalls) ──────────


def test_macos_layer_mapping():
    from conductor_desktop.core.backends.macos import _map_layer
    assert _map_layer(-100) == "background"
    assert _map_layer(0) == "normal"
    assert _map_layer(3) == "floating"
    assert _map_layer(8) == "modal"
    assert _map_layer(20) == "overlay"
    assert _map_layer(24) == "menu"
    assert _map_layer(1000) == "always_on_top"


def test_x11_xprop_to_layer_state_sticky():
    from conductor_desktop.core.backends.linux_x11 import _xprop_to_layer_state_sticky
    # Fullscreen wins over the window-type band.
    layer, state, sticky = _xprop_to_layer_state_sticky({
        "_NET_WM_WINDOW_TYPE(ATOM)": "_NET_WM_WINDOW_TYPE_NORMAL",
        "_NET_WM_STATE(ATOM)": "_NET_WM_STATE_FULLSCREEN",
        "_NET_WM_DESKTOP(CARDINAL)": "0",
    })
    assert layer == "fullscreen"
    assert state == "fullscreen"
    assert sticky is False

    # All-desktops sticky: _NET_WM_DESKTOP = 0xFFFFFFFF.
    layer, state, sticky = _xprop_to_layer_state_sticky({
        "_NET_WM_WINDOW_TYPE(ATOM)": "_NET_WM_WINDOW_TYPE_DOCK",
        "_NET_WM_STATE(ATOM)": "",
        "_NET_WM_DESKTOP(CARDINAL)": "4294967295",
    })
    assert layer == "overlay"
    assert sticky is True

    # Above hint on a normal window → always_on_top.
    layer, _, _ = _xprop_to_layer_state_sticky({
        "_NET_WM_WINDOW_TYPE(ATOM)": "_NET_WM_WINDOW_TYPE_NORMAL",
        "_NET_WM_STATE(ATOM)": "_NET_WM_STATE_ABOVE",
        "_NET_WM_DESKTOP(CARDINAL)": "0",
    })
    assert layer == "always_on_top"


def test_windows_ex_style_to_layer():
    from conductor_desktop.core.backends.windows import _ex_style_to_layer
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    assert _ex_style_to_layer(0) == "normal"
    assert _ex_style_to_layer(WS_EX_TOPMOST) == "always_on_top"
    assert _ex_style_to_layer(WS_EX_TOOLWINDOW) == "floating"
    # TOPMOST wins over TOOLWINDOW when both are set — the user notices
    # the on-top-ness first; tooltips/palettes can sit underneath.
    assert _ex_style_to_layer(WS_EX_TOPMOST | WS_EX_TOOLWINDOW) == "always_on_top"


# ── macOS list_screens / list_windows smoke (skipped off-darwin) ───


@pytest.mark.asyncio
async def test_macos_list_screens_smoke():
    """On macOS, list_screens() must return at least one screen with
    bounds and primary set, and list_windows() must populate every new
    Phase 1 field on at least one window.
    """
    import sys
    if sys.platform != "darwin":
        pytest.skip("macOS-only smoke test")

    from conductor_desktop.core.backends import macos
    screens = await macos.list_screens()
    assert len(screens) >= 1
    s0 = screens[0]
    for f in ("index", "name", "bounds", "scale", "primary"):
        assert f in s0
    assert len(s0["bounds"]) == 4
    # Built-in display is normally primary; tolerate non-primary in CI
    # but require at least one screen marked primary.
    assert any(s.get("primary") for s in screens)

    windows = await macos.list_windows()
    if not windows:
        pytest.skip("no on-screen windows — likely headless or locked")
    w = windows[0]
    for f in ("id", "title", "process_name", "pid", "bounds",
              "state", "desktop", "focused",
              "monitor", "z", "layer", "sticky", "workspace"):
        assert f in w, f"missing field {f!r} on window {w!r}"
    # workspace dict shape.
    assert "global_index" in w["workspace"]
    assert "per_output" in w["workspace"]
    # z is monotonic & non-negative.
    zs = [w["z"] for w in windows]
    assert zs == list(range(len(zs)))


@pytest.mark.asyncio
async def test_x11_stacking_falls_back_to_xlib_without_xprop(monkeypatch):
    from conductor_desktop.core.backends import linux_x11
    from conductor_desktop.core.errors import BackendError

    async def no_xprop(cmd, timeout=5):
        raise BackendError("xprop is not installed (not on PATH)")

    monkeypatch.setattr(linux_x11, "_run", no_xprop)
    monkeypatch.setattr(linux_x11, "_xlib_stacking_ids", lambda: [0xa00017, 0x1600017])
    assert await linux_x11._stacking_order() == {0x1600017: 0, 0xa00017: 1}


@pytest.mark.asyncio
async def test_x11_list_windows_keeps_whole_and_empty_titles(monkeypatch):
    """wmctrl's title is the 9th field: it was cut at its first space, and an
    untitled window (8 fields) was dropped."""
    from conductor_desktop.core.backends import linux_x11

    wmctrl = ("0x00c00003 -1 2253   0    0    1920 27   host \n"
              "0x01600017  0 0      10   37   945  1033 host notes - Mousepad\n")

    async def run(cmd, timeout=5):
        return wmctrl if cmd[0] == "wmctrl" else "22020119"

    async def nothing(*_a):
        return {}

    monkeypatch.setattr(linux_x11, "_require_bin", lambda _b: None)
    monkeypatch.setattr(linux_x11, "_run", run)
    monkeypatch.setattr(linux_x11, "_absolute_origins", nothing)
    monkeypatch.setattr(linux_x11, "_stacking_order", nothing)
    monkeypatch.setattr(linux_x11, "_xprop_window", nothing)
    monkeypatch.setattr(linux_x11, "_screens_or_none", lambda: nothing())
    titles = [w["title"] for w in await linux_x11.list_windows()]
    assert titles == ["", "notes - Mousepad"]


@pytest.mark.asyncio
async def test_x11_origins_fall_back_to_xlib_without_xwininfo(monkeypatch):
    from conductor_desktop.core.backends import linux_x11

    monkeypatch.setattr(linux_x11.shutil, "which", lambda _b: None)
    monkeypatch.setattr(linux_x11, "_xlib_client_origins", lambda: {0x1600017: (10, 37)})
    assert await linux_x11._absolute_origins() == {0x1600017: (10, 37)}
