"""Positional-anchor synthesis for the N-match disambiguation path.

LocateAnything's `<ref>` echoes the queried phrase, so N matches come back
with identical labels — useless as disambiguation handles for a text-only
brain. These tests lock in the fallback: synthesize distinct screen-region
anchors from each candidate's geometry, and lead with them in the ambiguity
message only when the grounder's own labels don't already distinguish.
"""

from __future__ import annotations

from conductor_desktop.runtime.grounding import (
    ScreenElement,
    disambiguating_anchors,
    format_ambiguity,
    screen_region,
)


def _el(bbox, description="", position=""):
    return ScreenElement(bbox=bbox, description=description, position=position)


# --- screen_region (3×3 grid mapping) ------------------------------------

def test_screen_region_corners_and_center():
    W, H = 1920, 1080
    assert screen_region(100, 100, W, H) == "top-left"
    assert screen_region(1820, 100, W, H) == "top-right"
    assert screen_region(100, 1000, W, H) == "bottom-left"
    assert screen_region(1820, 1000, W, H) == "bottom-right"
    assert screen_region(960, 540, W, H) == "center"


def test_screen_region_edges():
    W, H = 1920, 1080
    assert screen_region(960, 100, W, H) == "top-center"
    assert screen_region(960, 1000, W, H) == "bottom-center"
    assert screen_region(100, 540, W, H) == "center-left"
    assert screen_region(1820, 540, W, H) == "center-right"


def test_screen_region_degenerate_frame_is_empty():
    assert screen_region(10, 10, 0, 0) == ""


# --- disambiguating_anchors (tie-breaking within a region) ---------------

def test_anchors_distinct_regions_pass_through():
    els = [_el((0, 0, 10, 10), position="top-left"),
           _el((0, 0, 10, 10), position="bottom-right")]
    assert disambiguating_anchors(els) == ["top-left", "bottom-right"]


def test_anchors_same_region_get_left_to_right_ordinals():
    # Two icons both land in "bottom-center"; ties broken by x then y.
    els = [_el((300, 1000, 320, 1020), position="bottom-center"),
           _el((100, 1000, 120, 1020), position="bottom-center")]
    out = disambiguating_anchors(els)
    # element[1] is further left → rank #1
    assert out[1] == "bottom-center (#1 of 2, left-to-right)"
    assert out[0] == "bottom-center (#2 of 2, left-to-right)"
    assert len(set(out)) == 2  # guaranteed distinct


# --- format_ambiguity dispatch -------------------------------------------

def test_format_uses_model_labels_when_distinct():
    """Qwen/Gemma supply rich distinct descriptions — keep them."""
    els = [_el((0, 0, 10, 10), description="close button on left pane", position="top-left"),
           _el((100, 0, 110, 10), description="close button on right pane", position="top-right")]
    msg = format_ambiguity("close button", els)
    assert "close button on left pane" in msg
    assert "close button on right pane" in msg
    # region anchors NOT used as the handle when labels already distinguish
    assert '"top-left" at' not in msg


def test_format_falls_back_to_regions_when_labels_identical():
    """LocateAnything echoes the query → identical labels → use positions."""
    els = [_el((50, 50, 70, 70), description="wifi icon", position="top-left"),
           _el((1850, 50, 1870, 70), description="wifi icon", position="top-right")]
    msg = format_ambiguity("wifi icon", els)
    assert '"top-left" at' in msg
    assert '"top-right" at' in msg
    # the example rewrite in the header uses the first candidate's region
    assert "wifi icon in the top-left" in msg


def test_format_falls_back_to_regions_when_labels_empty():
    els = [_el((50, 50, 70, 70), description="", position="top-left"),
           _el((1850, 50, 1870, 70), description="", position="top-right")]
    msg = format_ambiguity("x", els)
    assert '"top-left" at' in msg
    assert "(no anchor)" not in msg
