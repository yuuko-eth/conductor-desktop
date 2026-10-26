"""Tests for per-monitor dock detection (Phase 2 follow-up).

Covers:
 - ScreenNode carries the optional ``dock`` field through the composer.
 - Pure parsing helpers in the X11 / Windows backends (strut → edge,
   tray-rect → edge), which are runnable on every platform because
   they're pure functions over already-decoded data.

The full per-backend smoke (CGWindowListCopyWindowInfo,
SHAppBarMessage, org.gnome.Shell.Eval) requires a live desktop and
runs as part of the platform-specific phase-2 smokes.
"""

from __future__ import annotations

import json

from conductor_desktop.core.models.scene_graph import (
    build_scene_graph,
    scene_graph_to_dict,
)

# ── Composer ───────────────────────────────────────────────────────


def _screen(index: int, dock: dict | None = None) -> dict:
    s = {"index": index, "name": f"M{index}",
         "bounds": [0, 0, 1920, 1080], "scale": 1.0,
         "primary": index == 0}
    if dock is not None:
        s["dock"] = dock
    return s


def test_dock_field_propagates_through_composer():
    dock = {
        "edge": "bottom",
        "bounds": [0, 1040, 1920, 40],
        "autohide": False,
        "hidden": False,
    }
    sg = build_scene_graph(
        target="host", windows=[],
        screens=[_screen(0, dock=dock)],
    )
    assert sg.screens[0].dock == dock
    # JSON-serialises cleanly — this is the MCP wire contract.
    encoded = json.loads(json.dumps(scene_graph_to_dict(sg)))
    assert encoded["screens"]["0"]["dock"] == dock


def test_dock_field_defaults_to_none():
    sg = build_scene_graph(
        target="host", windows=[],
        screens=[_screen(0)],
    )
    assert sg.screens[0].dock is None


def test_dock_field_per_monitor_independent():
    """Multi-monitor setups can have a dock on one display and none on
    the other; the composer must keep them independent (no leaking)."""
    dock_a = {
        "edge": "left", "bounds": [0, 0, 60, 1080],
        "autohide": True, "hidden": True,
    }
    sg = build_scene_graph(
        target="host", windows=[],
        screens=[
            _screen(0, dock=dock_a),
            _screen(1, dock=None),
        ],
    )
    assert sg.screens[0].dock == dock_a
    assert sg.screens[1].dock is None


def test_dock_dict_is_copied_not_aliased():
    """Mutating the returned scene-graph dock dict should not leak back
    into the caller's screens input."""
    dock = {"edge": "bottom", "bounds": [0, 1040, 1920, 40],
            "autohide": False, "hidden": False}
    screens = [_screen(0, dock=dock)]
    sg = build_scene_graph(target="host", windows=[], screens=screens)
    sg.screens[0].dock["edge"] = "top"
    assert screens[0]["dock"]["edge"] == "bottom"


# ── X11 parsing helpers ─────────────────────────────────────────────


def test_x11_parse_strut_partial_picks_nonzero_edge():
    from conductor_desktop.core.backends.linux_x11 import _parse_strut
    # Plasma X11 bottom panel ~40 px tall on a 1920x1080 monitor.
    props = {
        "_NET_WM_STRUT_PARTIAL(CARDINAL)":
        "0, 0, 0, 40, 0, 0, 0, 0, 0, 0, 0, 1920"
    }
    assert _parse_strut(props) == (0, 0, 0, 40)


def test_x11_parse_strut_falls_back_to_legacy_atom():
    from conductor_desktop.core.backends.linux_x11 import _parse_strut
    # XFCE 4.10-era panel only set _NET_WM_STRUT.
    props = {"_NET_WM_STRUT(CARDINAL)": "0, 0, 30, 0"}
    assert _parse_strut(props) == (0, 0, 30, 0)


def test_x11_parse_strut_returns_none_when_missing():
    from conductor_desktop.core.backends.linux_x11 import _parse_strut
    assert _parse_strut({}) is None


def test_x11_edge_from_geometry_bottom_panel():
    from conductor_desktop.core.backends.linux_x11 import _edge_from_geometry
    # 1920x40 panel flush against bottom of a 1920x1080 monitor.
    rect = (0, 1040, 1920, 40)
    screen = (0, 0, 1920, 1080)
    assert _edge_from_geometry(rect, screen) == "bottom"


def test_x11_edge_from_geometry_left_dock():
    from conductor_desktop.core.backends.linux_x11 import _edge_from_geometry
    rect = (0, 100, 60, 800)
    screen = (0, 0, 1920, 1080)
    assert _edge_from_geometry(rect, screen) == "left"


# ── Windows parsing helpers ─────────────────────────────────────────


def test_windows_edge_from_rect_on_screen_bottom():
    from conductor_desktop.core.backends.windows import _edge_from_rect_on_screen
    # Shell_TrayWnd at bottom: rect (l, t, r, b) flush with the bottom.
    tray = (0, 1040, 1920, 1080)
    screen = (0, 0, 1920, 1080)
    assert _edge_from_rect_on_screen(tray, screen) == "bottom"


def test_windows_edge_from_rect_on_screen_top():
    from conductor_desktop.core.backends.windows import _edge_from_rect_on_screen
    tray = (0, 0, 1920, 40)
    screen = (0, 0, 1920, 1080)
    assert _edge_from_rect_on_screen(tray, screen) == "top"


def test_windows_edge_from_rect_returns_none_when_floating():
    from conductor_desktop.core.backends.windows import _edge_from_rect_on_screen
    # Detached tray (>8 px from every edge) — caller should fall back.
    tray = (200, 200, 600, 240)
    screen = (0, 0, 1920, 1080)
    assert _edge_from_rect_on_screen(tray, screen) is None


async def _x11_screens(monkeypatch, xrandr_out: str) -> list[dict]:
    from conductor_desktop.core.backends import linux_x11

    async def fake_run(cmd, **kw):
        return xrandr_out

    async def no_dock(screens):
        return None

    monkeypatch.setattr(linux_x11, "_run", fake_run)
    monkeypatch.setattr(linux_x11, "_attach_dock_info", no_dock)
    return await linux_x11.list_screens()


async def test_x11_list_screens_first_is_primary_when_none_flagged(monkeypatch):
    # FreeBSD's scfb driver: one RandR-less "default" output, no "primary".
    screens = await _x11_screens(
        monkeypatch,
        "Screen 0: minimum 640 x 480, current 1024 x 768, maximum 1024 x 768\n"
        "default connected 1024x768+0+0 0mm x 0mm\n")
    assert [(s["name"], s["bounds"], s["primary"]) for s in screens] == [
        ("default", [0, 0, 1024, 768], True)]


async def test_x11_list_screens_keeps_the_flagged_primary(monkeypatch):
    # Mint's VM with two screens; disconnected outputs have no geometry.
    screens = await _x11_screens(
        monkeypatch,
        "Screen 0: minimum 1 x 1, current 2560 x 800, maximum 16384 x 16384\n"
        "Virtual1 connected primary 1280x800+0+0 (normal left inverted right x axis y axis) 0mm x 0mm\n"
        "   1280x800      59.81*+\n"
        "Virtual2 connected 1280x800+1280+0 (normal left inverted right x axis y axis) 0mm x 0mm\n"
        "Virtual3 disconnected (normal left inverted right x axis y axis)\n")
    assert [(s["index"], s["name"], s["bounds"], s["primary"]) for s in screens] == [
        (0, "Virtual1", [0, 0, 1280, 800], True), (1, "Virtual2", [1280, 0, 1280, 800], False)]


async def test_x11_list_screens_never_reprobes_the_outputs(monkeypatch):
    # A probe reads every monitor's EDID while X waits; --current doesn't.
    from conductor_desktop.core.backends import linux_x11
    seen = []

    async def fake_run(cmd, **kw):
        seen.append(cmd)
        return "VGA-1 connected primary 1920x1200+0+0 (normal) 518mm x 324mm\n"

    async def no_dock(screens):
        return None

    monkeypatch.setattr(linux_x11, "_run", fake_run)
    monkeypatch.setattr(linux_x11, "_attach_dock_info", no_dock)
    await linux_x11.list_screens()
    assert seen == [["xrandr", "--current"]]


def test_x11_tree_origins_are_absolute():
    # Mint's Muffin: the frame at +40+42, xed's client 10,40 inside it.
    # wmctrl -G said 60,122 for that client; it's at 50,82.
    from conductor_desktop.core.backends.linux_x11 import _parse_tree_origins
    out = """
xwininfo: Window id: 0x323 (the root window) (has no name)
     53 children:
     0x4200345 "xed": ()  10x10+-100+-100  +-100+-100
     0x260015d (has no name): ()  670x550+40+42  +40+42
        1 child:
        0x4200325 "geo-probe.txt (/tmp)": ("xed" "Xed")  650x500+10+40  +50+82
"""
    origins = _parse_tree_origins(out)
    assert origins[0x4200325] == (50, 82)
    assert origins[0x4200345] == (-100, -100)
    assert 0x323 not in origins
