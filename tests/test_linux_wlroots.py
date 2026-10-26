"""linux_wlroots window listing and focus, per source (sway, Hyprland, wlrctl)."""

from __future__ import annotations

import json

import pytest

from conductor_desktop.core.backends import linux_wlroots as wl
from conductor_desktop.core.errors import BackendError

TITLE = "/tmp/x/conductor-smoke-1.txt - Mousepad"

SWAY_TREE = {"type": "root", "nodes": [{"type": "output", "name": "Virtual-1", "nodes": [
    {"type": "workspace", "name": "1", "nodes": [
        {"type": "con", "id": 5, "name": TITLE, "pid": 3288, "app_id": "org.xfce.mousepad"},
        {"type": "con", "id": 7, "name": "alpha term", "pid": 3301, "app_id": "foot"},
    ]}]}]}

HYPR_CLIENTS = [
    {"address": "0x55aa", "title": TITLE, "class": "mousepad", "pid": 3288, "monitor": 0,
     "focusHistoryID": 1},
    {"address": "0x77bb", "title": "a (b) [c]", "class": "foot", "pid": 3301, "monitor": 0,
     "focusHistoryID": 0},
]

# "<app_id>: <title>"; a title may itself contain ": ".
WLRCTL_LIST = f"org.xfce.mousepad: {TITLE}\nfoot: alpha: term\n"


@pytest.fixture
def compositor(monkeypatch):
    """Pretend one of sway / hyprland / wlrctl-only is running; returns the
    commands run, and each tool's reply comes from ``replies``."""
    calls: list[list[str]] = []

    def use(source: str, replies: dict[str, str] | None = None) -> list[list[str]]:
        monkeypatch.setattr(wl, "_have_sway", lambda: source == "sway")
        monkeypatch.setattr(wl, "_have_hyprctl", lambda: source == "hyprland")

        async def no_screens():
            return []

        monkeypatch.setattr(wl, "_screens_or_none", no_screens)
        listing = {"sway": json.dumps(SWAY_TREE), "hyprland": json.dumps(HYPR_CLIENTS),
                   "wlrctl": WLRCTL_LIST}[source]

        async def fake_run(cmd, timeout=5):
            calls.append(cmd)
            if cmd[:3] in (["swaymsg", "-t", "get_tree"], ["hyprctl", "-j", "clients"],
                           ["wlrctl", "toplevel", "list"]):
                return listing
            return (replies or {}).get(cmd[0], "ok" if cmd[0] == "hyprctl" else "")

        monkeypatch.setattr(wl, "_run", fake_run)
        return calls

    return use


async def test_hyprland_focused_is_the_newest_in_focus_history(compositor):
    compositor("hyprland")
    assert [w["focused"] for w in await wl.list_windows()] == [False, True]


async def test_wlrctl_lines_split_into_app_id_and_title(compositor):
    compositor("wlrctl")
    windows = await wl.list_windows()
    assert [(w["process_name"], w["title"]) for w in windows] == [
        ("org.xfce.mousepad", TITLE), ("foot", "alpha: term")]


@pytest.mark.parametrize(("source", "command"), [
    ("sway", ["swaymsg", "[con_id=5]", "focus"]),
    ("hyprland", ["hyprctl", "dispatch", "focuswindow", "address:0x55aa"]),
    # wlrctl matches titles exactly, and a bare argument would be an app id.
    ("wlrctl", ["wlrctl", "toplevel", "focus", f"title:{TITLE}"]),
])
async def test_focus_by_exact_title(compositor, source, command):
    calls = compositor(source)
    assert await wl.focus_window(title=TITLE) is True
    assert calls[-1] == command


@pytest.mark.parametrize(("source", "target"), [
    ("sway", "[con_id=5]"), ("hyprland", "address:0x55aa"), ("wlrctl", f"title:{TITLE}"),
])
async def test_title_is_a_case_insensitive_substring(compositor, source, target):
    calls = compositor(source)
    assert await wl.focus_window(title="CONDUCTOR-SMOKE") is True
    assert target in calls[-1]


async def test_hyprland_title_with_regex_characters_focuses_by_address(compositor):
    calls = compositor("hyprland")
    assert await wl.focus_window(title="a (b) [c]") is True
    assert calls[-1][-1] == "address:0x77bb"


@pytest.mark.parametrize(("source", "target"), [
    ("sway", "[con_id=7]"), ("hyprland", "address:0x77bb"),
])
async def test_focus_by_pid(compositor, source, target):
    calls = compositor(source)
    assert await wl.focus_window(pid=3301) is True
    assert target in calls[-1]


async def test_no_match_is_false_and_focuses_nothing(compositor):
    calls = compositor("sway")
    assert await wl.focus_window(title="nope") is False
    assert calls == [["swaymsg", "-t", "get_tree"]]


async def test_pid_on_wlrctl_says_why(compositor):
    compositor("wlrctl")
    with pytest.raises(BackendError, match="lists no pids"):
        await wl.focus_window(pid=3288)


async def test_hyprctl_refusal_raises(compositor):
    compositor("hyprland", {"hyprctl": "No such window found"})
    with pytest.raises(BackendError, match="No such window found"):
        await wl.focus_window(title=TITLE)
