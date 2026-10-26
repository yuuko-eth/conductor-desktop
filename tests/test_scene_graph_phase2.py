"""Phase 2 of the scene-graph rollout — see https://docs.conductor-mcp.io/concepts/scene-graph/.

Covers DAG construction (build_scene_graph) against synthetic Phase 1
backend output, plus a darwin-only end-to-end smoke that exercises
the real executor through ResidentExecutor.
"""

from __future__ import annotations

import json

import pytest

from conductor_desktop.core.models.scene_graph import (
    build_scene_graph,
    scene_graph_to_dict,
)

# ── Synthetic fixtures ─────────────────────────────────────────────


def _make_window(id, title, monitor, z, *, focused=False, sticky=False,
                 workspace=None, layer="normal", desktop=0,
                 parent_window_id=None, modality=None):
    """Phase 1 window-dict shape."""
    return {
        "id": id, "title": title, "process_name": "test", "pid": 100 + int(id),
        "bounds": [0, 0, 800, 600], "state": "normal", "desktop": desktop,
        "focused": focused, "monitor": monitor, "z": z, "layer": layer,
        "sticky": sticky,
        "workspace": workspace or {"global_index": desktop, "per_output": None},
        "parent_window_id": parent_window_id,
        "modality": modality,
    }


SCREENS_SINGLE = [
    {"index": 0, "name": "Built-in", "bounds": [0, 0, 1920, 1080],
     "scale": 2.0, "primary": True},
]

SCREENS_DUAL = [
    {"index": 0, "name": "Built-in", "bounds": [0, 0, 1280, 800],
     "scale": 2.0, "primary": True},
    {"index": 1, "name": "DP-1", "bounds": [1280, 0, 1920, 1080],
     "scale": 1.0, "primary": False},
]


# ── Composer invariants ────────────────────────────────────────────


def test_empty_inputs_make_empty_graph():
    sg = build_scene_graph(target="host", windows=[], screens=[])
    assert sg.target == "host"
    assert sg.windows == {}
    assert sg.screens == {}
    assert sg.spaces == {}
    assert sg.active_screen == -1


def test_single_screen_single_desktop_one_space():
    """Three windows on the only screen on desktop 0 → one ws:global:0
    space whose windows are z-ordered."""
    windows = [
        _make_window("3", "Bottom", monitor=0, z=2),
        _make_window("1", "Top",    monitor=0, z=0, focused=True),
        _make_window("2", "Middle", monitor=0, z=1),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)

    assert set(sg.spaces) == {"ws:global:0"}
    sp = sg.spaces["ws:global:0"]
    # Z-order: topmost first.
    assert sp.windows == ["1", "2", "3"]
    # Heuristic label takes the focused window's title.
    assert sp.label == "Top"
    assert sp.label_source == "heuristic"

    # Screen wiring. Screens map uses int keys (same type as
    # active_screen / window.monitor / space.screen_ids).
    s0 = sg.screens[0]
    assert s0.spaces == ["ws:global:0"]
    assert s0.focused_window == "1"
    assert s0.current_space == "ws:global:0"
    assert sg.active_screen == 0


def test_global_workspace_spans_all_screens():
    """X11/Win32 global workspaces span every screen, even ones without
    windows in this view. screen.spaces should still include them."""
    windows = [
        _make_window("1", "A", monitor=0, z=0, focused=True, desktop=2),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_DUAL)
    assert sg.spaces["ws:global:2"].screen_ids == [0, 1]
    assert "ws:global:2" in sg.screens[0].spaces
    assert "ws:global:2" in sg.screens[1].spaces


def test_per_output_workspace_bound_to_one_screen():
    """Sway/Hyprland-shape per_output workspaces stay on their own screen."""
    windows = [
        _make_window("1", "sway-A", monitor=0, z=0,
                     workspace={"global_index": None,
                                "per_output": {"output": "eDP-1", "name": "code", "index": 1}}),
        _make_window("2", "sway-B", monitor=1, z=0,
                     workspace={"global_index": None,
                                "per_output": {"output": "DP-1", "name": "web", "index": 2}},
                     focused=True),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_DUAL)
    assert set(sg.spaces) == {"ws:eDP-1:code", "ws:DP-1:web"}
    assert sg.spaces["ws:eDP-1:code"].screen_ids == [0]
    assert sg.spaces["ws:DP-1:web"].screen_ids == [1]
    # Platform-named labels win over the heuristic.
    assert sg.spaces["ws:eDP-1:code"].label == "code"
    assert sg.spaces["ws:eDP-1:code"].label_source == "platform"
    # Active screen = where the focused window lives.
    assert sg.active_screen == 1
    assert sg.screens[1].current_space == "ws:DP-1:web"


def test_sticky_buckets_by_screen():
    """Sticky windows bucket per monitor so D7's per-(screen,space,layer)
    z-order invariant holds. Two sticky windows on screen 0 share
    ``ws:sticky:0``; a sticky window with monitor=-1 falls into the
    bare ``ws:sticky`` catch-all."""
    windows = [
        _make_window("1", "Sticky-A", monitor=0, z=0, sticky=True, desktop=0),
        _make_window("2", "Sticky-B", monitor=0, z=1, sticky=True, desktop=3),
        _make_window("3", "Normal",   monitor=0, z=2, desktop=0, focused=True),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    assert "ws:sticky:0" in sg.spaces
    assert sorted(sg.spaces["ws:sticky:0"].windows) == ["1", "2"]
    assert sg.spaces["ws:sticky:0"].screen_ids == [0]
    # The non-sticky window has its own normal space.
    assert "ws:global:0" in sg.spaces


def test_sticky_on_dual_screen_splits_buckets():
    """One sticky window per screen → distinct per-screen sticky buckets;
    no cross-screen z-order ambiguity."""
    windows = [
        _make_window("1", "Sticky-L", monitor=0, z=0, sticky=True, desktop=0),
        _make_window("2", "Sticky-R", monitor=1, z=0, sticky=True, desktop=0),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_DUAL)
    assert "ws:sticky:0" in sg.spaces and "ws:sticky:1" in sg.spaces
    assert sg.spaces["ws:sticky:0"].windows == ["1"]
    assert sg.spaces["ws:sticky:1"].windows == ["2"]
    assert sg.spaces["ws:sticky:0"].screen_ids == [0]
    assert sg.spaces["ws:sticky:1"].screen_ids == [1]


def test_sticky_with_unknown_monitor_uses_catchall():
    """Sticky window whose monitor couldn't be attributed (monitor=-1)
    falls into the bare ``ws:sticky`` bucket spanning every screen."""
    windows = [
        _make_window("1", "Sticky-X", monitor=-1, z=0, sticky=True, desktop=0),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_DUAL)
    assert "ws:sticky" in sg.spaces
    assert sg.spaces["ws:sticky"].screen_ids == [0, 1]


def test_active_screen_falls_back_to_primary_when_no_focus():
    """No window is focused → active_screen = primary screen index."""
    windows = [_make_window("1", "Idle", monitor=1, z=0)]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_DUAL)
    # Primary in SCREENS_DUAL is index 0.
    assert sg.active_screen == 0
    assert sg.screens[0].focused_window is None


def test_window_ids_preserved_and_window_map_complete():
    """No window should drop out of the graph during composition."""
    windows = [_make_window(str(i), f"w{i}", monitor=0, z=i) for i in range(5)]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    assert set(sg.windows.keys()) == {"0", "1", "2", "3", "4"}
    # Every window appears in exactly one space — D2 invariant on
    # non-Haiku platforms.
    all_in_spaces = [wid for sp in sg.spaces.values() for wid in sp.windows]
    assert sorted(all_in_spaces) == ["0", "1", "2", "3", "4"]


def test_parent_window_id_and_modality_propagate():
    """parent_window_id + modality survive the flat-dict → DAG round-trip
    and consumers can resolve "what is this dialog blocking?" from the
    parent edge alone (no global "blocked_by" denormalisation)."""
    windows = [
        # Base app window.
        _make_window("1", "Editor", monitor=0, z=1, focused=False),
        # Window-modal sheet attached to the editor — blocks just it.
        _make_window("2", "Save changes?", monitor=0, z=0,
                     focused=True, layer="modal",
                     parent_window_id="1", modality="window"),
        # App-modal alert from a different app — blocks every window of pid 103.
        _make_window("3", "Crash report", monitor=0, z=2, layer="modal",
                     parent_window_id=None, modality="app"),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    assert sg.windows["2"].parent_window_id == "1"
    assert sg.windows["2"].modality == "window"
    assert sg.windows["3"].parent_window_id is None
    assert sg.windows["3"].modality == "app"
    # Default-None propagation for non-modal windows.
    assert sg.windows["1"].parent_window_id is None
    assert sg.windows["1"].modality is None
    # The "what blocks window X?" scan: modals whose parent_window_id is X
    # OR whose scope is wider.
    blockers_of_1 = [w for w in sg.windows.values()
                     if w.modality == "window" and w.parent_window_id == "1"]
    assert [w.id for w in blockers_of_1] == ["2"]


def test_haiku_bitmask_window_fans_across_spaces():
    """Haiku's BWindow::Workspaces() can pin a window to workspaces 1+3
    but not 2. Composer reads ``workspace.global_indices`` and adds the
    window to each matching SpaceNode; this is the documented D2
    exception."""
    windows = [
        # Window pinned to workspaces 1 and 3.
        {
            "id": "10", "title": "Pinned", "process_name": "tracker",
            "pid": 1, "bounds": [0, 0, 400, 300], "state": "normal",
            "desktop": 1, "focused": True, "monitor": 0, "z": 0,
            "layer": "normal", "sticky": False,
            "workspace": {
                "global_index": 1, "global_indices": [1, 3], "per_output": None,
            },
        },
        # Normal single-workspace window for contrast.
        _make_window("11", "Solo", monitor=0, z=1, desktop=1),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    assert "ws:global:1" in sg.spaces
    assert "ws:global:3" in sg.spaces
    assert "10" in sg.spaces["ws:global:1"].windows
    assert "10" in sg.spaces["ws:global:3"].windows
    # Solo window stays in just its own space.
    assert sg.spaces["ws:global:1"].windows.count("11") == 1
    assert "11" not in sg.spaces["ws:global:3"].windows
    # Window map dedupes by id (lives in graph.windows once even though
    # it appears in two spaces).
    assert set(sg.windows.keys()) == {"10", "11"}


def test_heuristic_label_strips_browser_suffix():
    """Browser titles like 'Foo — Mozilla Firefox' should label as 'Foo'."""
    windows = [
        _make_window("1", "Inbox — Mozilla Firefox", monitor=0, z=0, focused=True),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    assert sg.spaces["ws:global:0"].label == "Inbox"


def test_scene_graph_to_dict_is_json_serializable():
    """asdict + json.dumps must round-trip without crashing — this is the
    contract for the MCP tool output."""
    windows = [
        _make_window("1", "A", monitor=0, z=0, focused=True),
        _make_window("2", "B", monitor=0, z=1),
    ]
    sg = build_scene_graph(target="host", windows=windows, screens=SCREENS_SINGLE)
    d = scene_graph_to_dict(sg)
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["target"] == "host"
    assert "1" in decoded["windows"]
    assert decoded["screens"]["0"]["focused_window"] == "1"


# ── macOS smoke ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_macos_get_scene_graph_smoke():
    """End-to-end on darwin: list_windows + list_screens → DAG."""
    import sys
    if sys.platform != "darwin":
        pytest.skip("macOS-only smoke test")

    from conductor_desktop.core.backends import macos
    from conductor_desktop.core.models.scene_graph import build_scene_graph

    windows = await macos.list_windows()
    screens = await macos.list_screens()
    if not windows or not screens:
        pytest.skip("no windows/screens — likely headless")

    sg = build_scene_graph(target="host", windows=windows, screens=screens)

    # Every window in the flat list should have made it into the graph.
    assert set(sg.windows.keys()) == {w["id"] for w in windows}
    # Active screen should be a valid index from the screens list.
    valid_indices = {s["index"] for s in screens}
    assert sg.active_screen in valid_indices
    # At least one space exists, and every window belongs to exactly one.
    assert len(sg.spaces) >= 1
    all_in_spaces = [wid for sp in sg.spaces.values() for wid in sp.windows]
    assert sorted(all_in_spaces) == sorted(sg.windows.keys())
    # JSON-serialize without error.
    json.dumps(scene_graph_to_dict(sg))
