"""Multi-monitor coordinates when a monitor sits left of or above the primary.

macOS and Windows put the primary's top-left at (0, 0), so such a monitor has
negative absolute coords, and a whole-desktop capture's pixel (0, 0) is the
union's top-left, not the origin. Layout used throughout, as measured on a
MacBook with an external display above and to the left:

    external  Quartz (-281, -1080, 1920, 1080)   NSScreen frame (-281, 800, ...)
    built-in  Quartz (0, 0, 1280, 800)           NSScreen frame (0, 0, ...)
"""

from __future__ import annotations

import sys
import types

from conductor_desktop.core.backends import macos
from conductor_desktop.core.executor.resident import ResidentExecutor
from conductor_desktop.grounder import GroundedElement
from conductor_desktop.runtime import grounding


class _Rect:
    def __init__(self, x, y, w, h):
        self.origin = types.SimpleNamespace(x=x, y=y)
        self.size = types.SimpleNamespace(width=w, height=h)


class _NSScreen:
    def __init__(self, name, frame):
        self._name, self._frame = name, _Rect(*frame)

    def frame(self):
        return self._frame

    def visibleFrame(self):
        return self._frame

    def localizedName(self):
        return self._name

    def backingScaleFactor(self):
        return 2.0


def _fake_appkit(monkeypatch, screens, main):
    appkit = types.ModuleType("AppKit")
    appkit.NSScreen = types.SimpleNamespace(screens=lambda: screens, mainScreen=lambda: main)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(macos, "_attach_dock_info", lambda _out: None)


async def test_macos_screens_flip_around_the_menu_bar_display(monkeypatch):
    builtin = _NSScreen("Built-in", (0, 0, 1280, 800))
    external = _NSScreen("External", (-281, 800, 1920, 1080))
    # The key window is on the external display, so mainScreen() is that one.
    _fake_appkit(monkeypatch, [builtin, external], main=external)

    out = await macos.list_screens()

    assert out[0]["bounds"] == [0, 0, 1280, 800]
    assert out[1]["bounds"] == [-281, -1080, 1920, 1080]
    # Primary is the menu-bar display, screens()[0], wherever focus is.
    assert [s["primary"] for s in out] == [True, False]


async def test_macos_screen_below_the_primary(monkeypatch):
    builtin = _NSScreen("Built-in", (0, 0, 1280, 800))
    below = _NSScreen("Below", (0, -1080, 1920, 1080))
    _fake_appkit(monkeypatch, [builtin, below], main=builtin)

    out = await macos.list_screens()

    assert out[0]["bounds"] == [0, 0, 1280, 800]
    assert out[1]["bounds"] == [0, 800, 1920, 1080]


def _executor_with(screens):
    ex = object.__new__(ResidentExecutor)

    async def fake_screens():
        return screens

    ex.screens = fake_screens
    return ex


async def test_desktop_origin_is_the_union_top_left():
    ex = _executor_with([{"bounds": [0, 0, 1280, 800]},
                         {"bounds": [-281, -1080, 1920, 1080]}])
    assert await ex.get_desktop_origin() == (-281, -1080)
    assert await ex.get_screen_dimensions() == (1920, 1880)


async def test_desktop_origin_without_screens_is_zero():
    assert await _executor_with([]).get_desktop_origin() == (0, 0)


class _Grounder:
    async def ground(self, _b64, _target):
        # The whole norm-1000 frame, so the centre is exactly the image's.
        return [GroundedElement(bbox=(0, 0, 1000, 1000), description="x")]


async def test_whole_desktop_grounding_returns_absolute_coords(install_state, monkeypatch):
    ex = _executor_with([{"bounds": [0, 0, 1280, 800]},
                         {"bounds": [-281, -1080, 1920, 1080]}])
    install_state(grounder=_Grounder(), executor=ex)

    async def capture(device_id=None, screen=None):
        return "", 1920, 1880

    monkeypatch.setattr(grounding, "capture_for_grounder", capture)

    [whole] = await grounding.ground_to_screen_elements("x")
    [local] = await grounding.ground_to_screen_elements("x", screen=1)

    # Image centre (960, 940) → absolute (679, -140), on the external display.
    assert whole.center == (960 - 281, 940 - 1080)
    # screen=N results stay local to that screen; click(screen=N) adds its origin.
    assert local.center == (960, 540)
