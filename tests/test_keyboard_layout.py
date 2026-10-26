"""Keys by the active layout, not US positions (ydotool).

Measured on GNOME Wayland with German: type_text("zy") gave
"yz", key("z") gave "y", and ctrl+z pressed ctrl+y, so nothing was undone.
"""
from __future__ import annotations

import pytest

from conductor_desktop.core import keyboard_layout
from conductor_desktop.core.executor import XdotoolInput, YdotoolInput
from conductor_desktop.core.executor.input import xkb
from conductor_desktop.core.keyboard_layout import Layout, parse_xkb

# Enough of German and Russian for the tests, as libxkbcommon builds them.
DE = {"z": (21, ()), "y": (44, ()), "Z": (21, (42,)), "@": (16, (100,)), "-": (53, ()),
      "ß": (12, ()), "a": (30, ()), " ": (57, ())}
RU = {"ф": (30, ()), "я": (44, ()), "н": (21, ()), "-": (12, ()), " ": (57, ())}


@pytest.fixture
def recorded(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""
    monkeypatch.setattr(YdotoolInput, "_run", fake_run)
    return seen


@pytest.fixture
def layout(monkeypatch):
    def use(name, keys):
        async def active():
            return Layout(name)
        monkeypatch.setattr(keyboard_layout, "active", active)
        monkeypatch.setattr(xkb, "keys_for", lambda layout, variant="": keys)
    return use


async def test_text_goes_by_the_layouts_keys(recorded, layout):
    layout("de", DE)
    await YdotoolInput().type_text("zy")
    assert recorded == [["ydotool", "key", "--key-delay", "20",
                         "21:1", "21:0", "44:1", "44:0"]]


async def test_a_character_holds_the_modifiers_its_level_needs(recorded, layout):
    layout("de", DE)
    await YdotoolInput().type_text("Z@")
    assert recorded[0][4:] == ["42:1", "21:1", "21:0", "42:0", "100:1", "16:1", "16:0", "100:0"]


async def test_a_newline_is_enter(recorded, layout):
    layout("de", DE)
    await YdotoolInput().type_text("a\n")
    assert recorded[0][4:] == ["30:1", "30:0", "28:1", "28:0"]


async def test_ctrl_z_presses_the_key_that_types_z(recorded, layout):
    layout("de", DE)
    await YdotoolInput().hotkey(["ctrl", "z"])
    assert recorded == [["ydotool", "key", "29:1", "21:1", "21:0", "29:0"]]


async def test_a_shortcut_letter_the_layout_lacks_keeps_its_us_key(recorded, layout):
    # Russian has no z: apps look for ctrl+z at the US position then.
    layout("ru", RU)
    await YdotoolInput().hotkey(["ctrl", "z"])
    assert recorded == [["ydotool", "key", "29:1", "44:1", "44:0", "29:0"]]


async def test_key_presses_the_layouts_key(recorded, layout):
    layout("de", DE)
    await YdotoolInput().key_press("z")
    await YdotoolInput().key_press("enter")
    assert recorded == [["ydotool", "key", "21:1", "21:0"], ["ydotool", "key", "28:1", "28:0"]]


@pytest.mark.parametrize(("keys", "text", "typable"), [
    (DE, "zy ß", True), (DE, "ян", False), (RU, "ян", True), (RU, "zy", False)])
async def test_what_the_layout_can_type_and_what_is_pasted(layout, keys, text, typable):
    layout("xx", keys)
    assert await YdotoolInput().can_type(text) is typable


# xdotool types off-layout characters through a remapped keycode, which a
# slow app can read after the map is restored, so those are pasted.
@pytest.mark.parametrize(("name", "keys", "text", "typable"), [
    ("us", None, "-a b", True), ("us", None, "1 é🎉", False),
    ("de", DE, "zy ß", True), ("de", DE, "ß🎉", False), ("ru", RU, "ян", True)])
async def test_xdotool_types_what_the_layout_has(layout, name, keys, text, typable):
    layout(name, keys)
    assert await XdotoolInput().can_type(text) is typable


async def test_text_the_layout_cant_type_is_refused_not_dropped(recorded, layout):
    layout("ru", RU)
    with pytest.raises(Exception, match="isn't on the active keyboard layout"):
        await YdotoolInput().type_text("zy")
    assert recorded == []


async def test_us_keeps_ydotools_own_typing(recorded, layout):
    layout("us", None)
    await YdotoolInput().type_text("-a b")
    assert recorded == [["ydotool", "type", "--key-delay", "20", "--", "-a b"]]


async def test_a_layout_xkb_cant_build_keeps_us_positions(recorded, layout):
    layout("de", None)  # no libxkbcommon, or an unknown layout
    await YdotoolInput().hotkey(["ctrl", "z"])
    assert recorded == [["ydotool", "key", "29:1", "44:1", "44:0", "29:0"]]


# ── Finding the layout ──────────────────────────────────────────────


@pytest.mark.parametrize(("spec", "expected"), [
    ("de", Layout("de")), ("de+nodeadkeys", Layout("de", "nodeadkeys")),
    ("us(dvorak)", Layout("us", "dvorak")), ("", None)])
def test_xkb_names_as_gnome_and_kde_write_them(spec, expected):
    assert parse_xkb(spec) == expected


@pytest.mark.parametrize(("engine", "expected"), [
    ("xkb:de::deu", Layout("de")), ("xkb:de:nodeadkeys:deu", Layout("de", "nodeadkeys")),
    ("chewing", None)])
async def test_ibus_names_the_layout_of_an_xkb_engine(monkeypatch, engine, expected):
    async def output(*cmd):
        return engine
    monkeypatch.setattr(keyboard_layout.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(keyboard_layout, "_output", output)
    assert await keyboard_layout._ibus() == expected


async def test_gnome_takes_the_most_recently_used_source(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    async def output(*cmd):
        return "[('xkb', 'de'), ('xkb', 'us')]" if cmd[-1] == "mru-sources" else ""
    monkeypatch.setattr(keyboard_layout, "_output", output)
    assert await keyboard_layout._gnome() == Layout("de")


async def test_gnome_with_an_input_method_first_names_no_layout(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")

    async def output(*cmd):
        return "[('ibus', 'chewing'), ('xkb', 'us')]"
    monkeypatch.setattr(keyboard_layout, "_output", output)
    assert await keyboard_layout._gnome() is None


def _have_xkbcommon() -> bool:
    try:
        xkb._lib()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _have_xkbcommon(), reason="needs libxkbcommon")
def test_libxkbcommon_builds_german_as_measured():
    keys = xkb.keys_for.__wrapped__("de")
    assert keys["z"] == (21, ()) and keys["y"] == (44, ()) and keys["@"] == (16, (100,))
