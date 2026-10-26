"""Linux ui_* backend helpers that don't need AT-SPI."""

from __future__ import annotations

from conductor_desktop.core.backends.linux_ui import (
    SlowApps,
    order_windows,
    parse_frame_extents,
    parse_hypr_clients,
    parse_stacking,
    parse_xwininfo,
    screen_bounds,
)


def test_screen_bounds_rejects_offscreen_and_empty():
    assert screen_bounds((640, 329, 41, 25), relative=False, origin=None) == (640, 329, 41, 25)
    # GTK 3 reports elements that aren't on screen at INT_MIN, 1x1.
    assert screen_bounds((-2147483648, -2147483648, 1, 1), relative=False, origin=None) is None
    assert screen_bounds((10, 10, 0, 20), relative=False, origin=None) is None
    assert screen_bounds(None, relative=False, origin=None) is None


def test_gtk4_bounds_are_window_relative():
    # Calculator's "4" key at WINDOW 16,341 in a window whose content starts at 99,131.
    assert screen_bounds((16, 341, 64, 44), relative=True, origin=(99, 131)) == (115, 472, 64, 44)
    # No origin (Wayland, Shell Eval gated): no bounds rather than wrong ones.
    assert screen_bounds((16, 341, 64, 44), relative=True, origin=None) is None


# Trimmed `hyprctl -j clients`: Hyprland is the one Wayland compositor that
# hands out window origins, which is what makes GTK 4's window-relative extents
# usable there. `at` is the content origin — no frame to subtract.
HYPR_CLIENTS = """[
 {"address": "0x55746c5a07f0", "at": [646, 406], "size": [613, 373],
  "pid": 1359, "class": "org.gnome.Calculator", "title": "Calculator"},
 {"address": "0x55746c5a1234", "at": [21, 21], "size": [613, 758],
  "pid": 1067, "class": "foot", "title": "user@arch:~"}
]"""


def test_parse_hypr_clients_gives_window_origins():
    origins = parse_hypr_clients(HYPR_CLIENTS)
    assert origins[(1359, "Calculator")] == (646, 406)
    assert origins[(1067, "user@arch:~")] == (21, 21)
    # Composed with GTK 4's WINDOW extents, that is a real screen rectangle:
    # Calculator's "7" key, verified against a grim capture on Hyprland.
    assert screen_bounds((18, 205, 116, 44), relative=True,
                         origin=origins[(1359, "Calculator")]) == (664, 611, 116, 44)


def test_parse_hypr_clients_survives_bad_input():
    assert parse_hypr_clients("") == {}
    assert parse_hypr_clients("not json") == {}
    # A client mid-map can lack the keys; the rest still parse.
    assert parse_hypr_clients('[{"pid": 1}, {"at": [1, 2], "pid": 7, "title": "t"}]') == {(7, "t"): (1, 2)}


def test_parse_frame_extents_and_xwininfo():
    assert parse_frame_extents("_GTK_FRAME_EXTENTS(CARDINAL) = 61, 61, 55, 67\n") == (61, 61, 55, 67)
    assert parse_frame_extents("_GTK_FRAME_EXTENTS:  not found.\n") is None
    out = ("xwininfo: Window id: 0x4200004 \"Calculator\"\n\n"
           "  Absolute upper-left X:  38\n  Absolute upper-left Y:  76\n  Width: 482\n")
    assert parse_xwininfo(out) == (38, 76)
    assert parse_xwininfo("") is None


WMCTRL = """\
0x028003d0  0 57479  host alice@host: ~
0x04200004  0 3654460 host Calculator
0x02c00004  0 3654461 host notes.csv (~/Downloads) - Text Editor
0x02c00099  0 3654461 host
"""
XPROP = "_NET_CLIENT_LIST_STACKING(WINDOW): window id # 0x28003d0, 0x2c00004, 0x4200004\n"


def test_parse_stacking_topmost_first():
    stacking = parse_stacking(WMCTRL, XPROP)
    assert stacking[(3654460, "Calculator")] == (0, 0x4200004)
    assert stacking[(3654461, "notes.csv (~/Downloads) - Text Editor")] == (1, 0x2c00004)
    assert stacking[(57479, "alice@host: ~")][0] == 2
    assert (3654461, "") not in stacking  # not in the stacking list


def test_slow_app_is_skipped_after_two_strikes():
    now = [100.0]
    slow = SlowApps(clock=lambda: now[0])
    slow.record(":1.42", 0.3, "mousepad")
    assert not slow.skip(":1.42")  # fast enough
    # One slow read is also what a GTK 3 app looks like while a modal dialog
    # is up; hiding it then would make its own dialog unreadable.
    slow.record(":1.42", 16.3, "gnome-shell")
    assert not slow.skip(":1.42")
    slow.record(":1.42", 16.3, "gnome-shell")
    assert slow.skip(":1.42") and not slow.skip(":1.43")
    assert slow.name(":1.42") == "gnome-shell"
    now[0] += 31
    assert not slow.skip(":1.42")  # listed again, re-timed next lookup
    slow.record("", 20.0)
    assert not slow.skip("")


def test_an_app_that_never_answers_is_skipped_at_once():
    # A process that went away without deregistering: calls time out, so one
    # strike is enough (unlike an app that answers slowly).
    slow = SlowApps(clock=lambda: 0.0)
    slow.record(":1.77", 1.5, "", answered=False)
    assert slow.skip(":1.77")


def test_one_fast_read_clears_the_strike():
    slow = SlowApps(clock=lambda: 0.0)
    for seconds in (2.0, 0.1, 2.0):
        slow.record(":1.9", seconds, "mousepad")
    assert not slow.skip(":1.9")


def test_outline_says_which_apps_were_skipped():
    from conductor_desktop.core.ui.format import format_outline
    from conductor_desktop.core.ui.model import UINode, UIRead

    win = UINode("window", "frame", name="Mousepad")
    read = UIRead("mousepad", 7, ":1.9/org/a11y/1", "Mousepad", win, nodes=1,
                  unresponsive_apps=["thunar"])
    assert "1 unresponsive app(s) skipped: thunar" in format_outline(read)


def test_order_windows_active_then_stacking_then_atspi():
    stacking = parse_stacking(WMCTRL, XPROP)
    wins = [
        (57479, "alice@host: ~", False),
        (99, "Unlisted", False),
        (3654460, "Calculator", False),
        (3654461, "notes.csv (~/Downloads) - Text Editor", True),
    ]
    assert order_windows(wins, stacking) == [3, 2, 0, 1]
