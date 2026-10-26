"""Unit tests for Route-A scene fusion (pure, no network/model)."""

from __future__ import annotations

from conductor_desktop.scene_fusion import (
    blocked_window_ids,
    clip_rect_to_image,
    fuse,
    point_context,
    resolve_scope,
    screen_origin,
)


def _scene():
    """A focused browser (w1), an app-modal dialog on top (w2), and a dock.

    Layout (screen px):
      w1  bounds [0,100,1280,640]  → y in [100,740]   focused, z=1, pid 100
      w2  bounds [400,300,480,200] → x[400,880] y[300,500]  modal=app, z=0, pid 100
      dock bounds [0,740,1280,60]  → y in [740,800]   layer=dock, z=2, pid 200
    """
    return {
        "screens": {"0": {"index": 0, "bounds": [0, 0, 1280, 800], "primary": True}},
        "windows": {
            "w1": {
                "id": "w1", "title": "Uniqlo — Chrome", "process_name": "Chrome",
                "pid": 100, "bounds": [0, 100, 1280, 640], "state": "normal",
                "focused": True, "monitor": 0, "z": 1, "layer": "normal",
                "modality": None, "parent_window_id": None,
            },
            "w2": {
                "id": "w2", "title": "Confirm", "process_name": "Chrome",
                "pid": 100, "bounds": [400, 300, 480, 200], "state": "normal",
                "focused": False, "monitor": 0, "z": 0, "layer": "modal",
                "modality": "app", "parent_window_id": "w1",
            },
            "dock": {
                "id": "dock", "title": "", "process_name": "Dock",
                "pid": 200, "bounds": [0, 740, 1280, 60], "state": "normal",
                "focused": False, "monitor": 0, "z": 2, "layer": "dock",
                "modality": None, "parent_window_id": None,
            },
        },
    }


def _elements():
    return [
        {"id": 0, "x": 640, "y": 450},  # inside dialog (and w1 underneath)
        {"id": 1, "x": 100, "y": 200},  # inside w1 only
        {"id": 2, "x": 640, "y": 770},  # inside dock
    ]


def test_blocked_ids_app_modal():
    # App modal (w2, pid 100) blocks the process's other window (w1), not itself
    # and not the different-process dock.
    assert blocked_window_ids(_scene()) == {"w1"}


def test_active_scope_clips_to_modal():
    kept, header, summary = fuse(_elements(), _scene(), scope="active")
    ids = [e["id"] for e in kept]
    assert ids == [0]                       # only the element inside the dialog
    assert summary["clipped_to"] == "w2"
    assert summary["modal"] == "app"
    assert summary["fused"] is True
    e0 = kept[0]
    assert e0["w"] == "w2"                   # front-most owner, not w1 underneath
    assert e0["blk"] == 0                    # the modal itself is reachable


def test_focused_scope_ignores_modal_and_dock():
    kept, _, summary = fuse(_elements(), _scene(), scope="focused")
    ids = sorted(e["id"] for e in kept)
    assert ids == [0, 1]                     # both inside w1's bounds; dock excluded
    assert summary["clipped_to"] == "w1"


def test_all_scope_owner_and_blocked_tags():
    kept, header, summary = fuse(_elements(), _scene(), scope="all")
    by_id = {e["id"]: e for e in kept}
    assert len(kept) == 3
    assert by_id[0]["w"] == "w2"             # top-most window wins ownership
    assert by_id[1]["w"] == "w1" and by_id[1]["blk"] == 1   # blocked by the modal
    assert by_id[2]["w"] == "dock" and by_id[2]["blk"] == 0
    # Header is one compact row per referenced window, sorted front-first.
    assert [h["id"] for h in header] == ["w2", "w1", "dock"]
    assert next(h for h in header if h["id"] == "w1")["blocked"] == 1


def test_app_scope_selects_by_process_name():
    clip, meta = resolve_scope(_scene(), "app:dock")
    assert meta["clip_to"] == "dock"
    assert clip == [0, 740, 1280, 60]


def test_window_scope_by_id():
    clip, meta = resolve_scope(_scene(), "window:w1")
    assert meta["clip_to"] == "w1" and clip == [0, 100, 1280, 640]


def test_window_scope_keeps_a_mixed_case_id():
    scene = _scene()
    scene["windows"]["Ab-W1"] = {**scene["windows"].pop("w1"), "id": "Ab-W1"}
    clip, meta = resolve_scope(scene, "Window:Ab-W1")
    assert meta["clip_to"] == "Ab-W1" and clip == [0, 100, 1280, 640]


def test_point_context_reports_owner_focus_block():
    ctx = point_context(_scene(), 100, 200)
    assert ctx == {"w": "w1", "app": "Chrome", "blk": 1, "focused": True}


def test_no_scene_degrades_gracefully():
    els = _elements()
    kept, header, summary = fuse(els, None, scope="active")
    assert kept == els and header == [] and summary["fused"] is False
    assert point_context(None, 1, 2) == {}


def test_fullscreen_overlay_does_not_steal_ownership():
    # Regression (found live on macOS Ventura): the Dock owns a full-screen
    # window in the "overlay" band that sits in front of normal windows
    # (lower z). It must NOT win ownership of elements in the app behind it.
    scene = {
        "screens": {"0": {"index": 0, "bounds": [0, 0, 1280, 800]}},
        "windows": {
            "dock_canvas": {
                "id": "161", "title": "Dock", "process_name": "Dock", "pid": 200,
                "bounds": [0, 0, 1280, 800], "state": "normal", "focused": False,
                "monitor": 0, "z": 10, "layer": "overlay",
                "modality": None, "parent_window_id": None,
            },
            "term": {
                "id": "30325", "title": "conductor-grounder", "process_name": "Terminal",
                "pid": 300, "bounds": [0, 25, 1280, 775], "state": "normal",
                "focused": True, "monitor": 0, "z": 12, "layer": "normal",
                "modality": None, "parent_window_id": None,
            },
        },
    }
    kept, header, _ = fuse([{"id": 0, "x": 640, "y": 400}], scene, scope="active")
    assert kept[0]["w"] == "30325"          # Terminal, not the Dock overlay
    assert "161" not in [h["id"] for h in header]


def test_screen_origin_offsets_by_monitor():
    scene = _scene()
    scene["screens"]["1"] = {"index": 1, "bounds": [1280, 0, 1920, 1080]}
    assert screen_origin(scene, 1) == (1280, 0)
    assert screen_origin(scene, None) == (0, 0)   # whole-desktop capture
    assert screen_origin(None, 1) == (0, 0)


def test_clip_rect_to_image_no_scale():
    # Capture matches screen size 1:1, no monitor offset.
    box = clip_rect_to_image([400, 300, 480, 200], (0, 0), 1280, 800, 1280, 800)
    assert box == (400, 300, 880, 500)


def test_clip_rect_to_image_downscaled_capture():
    # SCREENSHOT_MAX_WIDTH halved the capture (2560→1280): rect scales too.
    box = clip_rect_to_image([400, 300, 480, 200], (0, 0), 1280, 800, 2560, 1600)
    assert box == (200, 150, 440, 250)


def test_clip_rect_to_image_second_monitor_origin():
    # Window at absolute x=1300 on a screen whose origin is (1280, 0) → local x=20.
    box = clip_rect_to_image([1300, 100, 200, 150], (1280, 0), 1920, 1080, 1920, 1080)
    assert box == (20, 100, 220, 250)


def test_clip_rect_to_image_degenerate_returns_none():
    assert clip_rect_to_image([0, 0, 1, 1], (0, 0), 1280, 800, 1280, 800) is None
    assert clip_rect_to_image("nope", (0, 0), 1280, 800, 1280, 800) is None
    assert clip_rect_to_image([0, 0, 100, 100], (0, 0), 1280, 800, 0, 800) is None
