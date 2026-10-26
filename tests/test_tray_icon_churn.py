"""The tray changes its icon at most once a second, from files that stay put.

gnome-shell's AppIndicator extension reloads the icon on its main thread;
several changes per tool call, each from a fresh temp file pystray deleted
again, left it not answering AT-SPI for seconds (Ubuntu 22.04 GNOME X11).
"""
from __future__ import annotations

import os
import tempfile

from PIL import Image

from conductor_desktop import tray


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def gate(clock):
    shown: list[str] = []
    return tray._IconGate(shown.append, shown="idle", clock=clock), shown


def test_a_call_shows_one_change_then_the_ticker_shows_where_it_ended():
    clock = Clock()
    g, shown = gate(clock)
    for state in ("acting", "idle", "observing", "idle"):  # one tool call's worth
        g.want(state)
    assert shown == ["acting"]
    clock.now += 1.0
    g.flush()
    assert shown == ["acting", "idle"]


def test_the_ticker_shows_nothing_when_nothing_changed():
    clock = Clock()
    g, shown = gate(clock)
    g.want("idle")
    clock.now += 5
    g.flush()
    assert shown == []


def test_a_change_after_a_quiet_second_shows_at_once():
    clock = Clock()
    g, shown = gate(clock)
    g.want("acting")
    clock.now += 1.5
    g.want("errored")
    assert shown == ["acting", "errored"]


class FakeGtkIcon:
    """pystray._util.gtk's temp-file handling, as 0.19.5 has it."""

    def __init__(self, image) -> None:
        self.icon = image
        self._icon_path = None
        self._icon_valid = False
        self._update_fs_icon()

    def _remove_fs_icon(self):
        if self._icon_path:
            os.unlink(self._icon_path)
            self._icon_path = None
        self._icon_valid = False

    def _update_fs_icon(self):
        self._icon_path = tempfile.mktemp()
        with open(self._icon_path, "wb") as f:
            self.icon.save(f, "PNG")
        self._icon_valid = True

    def update(self, image):  # what the icon setter's _update_icon does
        self.icon = image
        self._remove_fs_icon()
        self._update_fs_icon()


def test_each_state_has_one_file_that_outlives_the_change():
    icons = {s: Image.new("RGBA", (22, 22), c) for s, c in
             (("idle", "grey"), ("acting", "blue"), ("errored", "red"))}
    icon = FakeGtkIcon(icons["idle"])
    first = icon._icon_path
    tray._use_stable_icon_files(icon, icons)

    icon.update(icons["acting"])
    acting = icon._icon_path
    assert not os.path.exists(first)  # pystray's own first file is still cleaned up
    assert acting.endswith("acting.png")
    icon.update(icons["errored"])
    icon.update(icons["acting"])
    assert icon._icon_path == acting and os.path.exists(acting)
    assert Image.open(acting).getpixel((0, 0)) == Image.new("RGBA", (1, 1), "blue").getpixel((0, 0))
