"""Every Quartz keyboard event states its modifier flags.

On macOS 26 a posted event's flags stay in the system's HID state. Events
left to inherit flags from the event source made a plain arrow key leave
Fn + numpad held and cmd+up leave Fn held, after which every typed
character arrived as Fn+character and was dropped.
"""

from __future__ import annotations

import sys
import types

import pytest

from conductor_desktop.core.executor import QuartzInput

CMD, SHIFT = QuartzInput._MODIFIERS["cmd"], QuartzInput._MODIFIERS["shift"]


@pytest.fixture
def posted(monkeypatch):
    events: list[dict] = []
    fake = types.ModuleType("Quartz")
    fake.CGEventCreateKeyboardEvent = lambda src, code, down: {"code": code, "down": down}
    fake.CGEventKeyboardSetUnicodeString = lambda ev, n, s: ev.update(char=s)
    fake.CGEventSetFlags = lambda ev, flags: ev.update(flags=flags)
    fake.CGEventPost = lambda tap, ev: events.append(ev)
    fake.kCGHIDEventTap = 0
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    return events


@pytest.fixture
def quartz(monkeypatch):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("conductor_desktop.core.executor.input.quartz.asyncio.sleep", no_sleep)
    q = QuartzInput.__new__(QuartzInput)
    q._source = object()
    return q


async def test_typed_characters_carry_no_modifiers(quartz, posted):
    await quartz.type_text("k2")
    assert [e["flags"] for e in posted] == [0, 0, 0, 0]


async def test_a_plain_arrow_key_carries_no_modifiers(quartz, posted):
    await quartz.key_press("right")
    assert [e["flags"] for e in posted] == [0, 0]


async def test_a_hotkey_releases_every_modifier(quartz, posted):
    await quartz.hotkey(["cmd", "shift", "up"])
    flags = [(e["down"], e["flags"]) for e in posted]
    assert flags == [
        (True, CMD), (True, CMD | SHIFT),          # modifiers go down one by one
        (True, CMD | SHIFT), (False, CMD | SHIFT),  # the key, with both held
        (False, CMD), (False, 0),                   # released in reverse; none left
    ]


# Typed characters carry their real key too: apps that read keys, not text
# (remote desktops, VMs, the ContrAlto Alto emulator), saw keycode 0, the A
# key, for every character.

SHIFT_KEY = QuartzInput._MODIFIER_KEYCODES["shift"]


def keys(posted):
    return [(e["code"], e["down"], e["flags"], e.get("char")) for e in posted]


async def test_typed_characters_carry_their_key(quartz, posted):
    await quartz.type_text("k/")
    assert keys(posted) == [(0x28, True, 0, "k"), (0x28, False, 0, "k"),
                            (0x2C, True, 0, "/"), (0x2C, False, 0, "/")]


@pytest.mark.parametrize(("ch", "code"), [("A", 0x00), ("?", 0x2C), ("|", 0x2A), ("~", 0x32)])
async def test_a_shifted_character_presses_shift_for_real(quartz, posted, ch, code):
    await quartz.type_text(ch)
    assert keys(posted) == [(SHIFT_KEY, True, SHIFT, None),
                            (code, True, SHIFT, ch), (code, False, SHIFT, ch),
                            (SHIFT_KEY, False, 0, None)]


async def test_a_character_with_no_us_key_goes_as_text(quartz, posted):
    await quartz.type_text("é")
    assert keys(posted) == [(0, True, 0, "é"), (0, False, 0, "é")]


async def test_a_newline_is_the_return_key(quartz, posted):
    await quartz.type_text("\n")
    assert keys(posted) == [(0x24, True, 0, None), (0x24, False, 0, None)]


async def test_key_takes_a_punctuation_character(quartz, posted):
    await quartz.key_press("?")
    assert [e["code"] for e in posted] == [SHIFT_KEY, 0x2C, 0x2C, SHIFT_KEY]


async def test_a_hotkey_takes_punctuation(quartz, posted):
    await quartz.hotkey(["cmd", "/"])
    assert [(e["code"], e["flags"]) for e in posted if e["code"] == 0x2C] == [(0x2C, CMD)] * 2


async def test_keys_are_held_long_enough_for_an_app_that_polls(quartz, posted, monkeypatch):
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr("conductor_desktop.core.executor.input.quartz.asyncio.sleep", sleep)
    await quartz.key_press("enter")
    assert slept[0] >= 1 / 30  # down for more than a frame or two


async def test_a_click_is_held_like_a_persons(quartz, monkeypatch):
    fake = types.ModuleType("Quartz")
    events: list[str] = []
    fake.CGEventCreateMouseEvent = lambda src, kind, pt, btn: kind
    fake.CGEventSetIntegerValueField = lambda ev, field, value: None
    fake.CGEventPost = lambda tap, ev: events.append(ev)
    for name in ("kCGEventMouseMoved", "kCGHIDEventTap", "kCGMouseEventClickState",
                 "kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGEventOtherMouseDown",
                 "kCGEventOtherMouseUp", "kCGEventRightMouseDown", "kCGEventRightMouseUp",
                 "kCGMouseButtonCenter", "kCGMouseButtonLeft", "kCGMouseButtonRight"):
        setattr(fake, name, name)
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    slept: list[tuple[str, float]] = []

    async def sleep(s):
        slept.append((events[-1], s))
    monkeypatch.setattr("conductor_desktop.core.executor.input.quartz.asyncio.sleep", sleep)
    monkeypatch.setattr(quartz, "_event_source", lambda: None)
    await quartz.click(10, 20)
    assert events == ["kCGEventMouseMoved", "kCGEventLeftMouseDown", "kCGEventLeftMouseUp"]
    held = dict(slept)
    assert held["kCGEventMouseMoved"] > 0  # the app sees the pointer arrive first
    assert held["kCGEventLeftMouseDown"] >= 0.1  # Smalltalk on the Alto missed 50 ms


# ── The current layout (mac_layout) ─────────────────────────────────
# Measured on macOS with German: key z typed y, and cmd+z pressed the
# key that is y there, so nothing was undone.

# German, as UCKeyTranslate reports it: z and y swap, @ is Option+L.
DE = {"z": (0x10, 0), "y": (0x06, 0), "Z": (0x10, 0x02), "@": (0x25, 0x08), "a": (0x00, 0)}
RU = {"ф": (0x00, 0), "я": (0x06, 0)}
OPTION = QuartzInput._MODIFIERS["option"]


@pytest.fixture
def layout(monkeypatch):
    from conductor_desktop.core.executor.input import mac_layout

    def use(keys):
        monkeypatch.setattr(mac_layout, "current", lambda: ("test", keys))
    return use


def _downs(posted):
    return [(e["code"], e["flags"], e.get("char")) for e in posted if e["down"]]


async def test_key_presses_the_layouts_key(quartz, posted, layout):
    layout(DE)
    await quartz.key_press("z")
    assert _downs(posted) == [(0x10, 0, None)]


async def test_cmd_z_presses_the_key_that_types_z(quartz, posted, layout):
    layout(DE)
    await quartz.hotkey(["cmd", "z"])
    assert (0x10, CMD, None) in _downs(posted)


async def test_a_shortcut_letter_the_layout_lacks_keeps_its_us_key(quartz, posted, layout):
    layout(RU)
    await quartz.hotkey(["cmd", "z"])
    assert (0x06, CMD, None) in _downs(posted)


async def test_typed_text_goes_by_the_layouts_keys(quartz, posted, layout):
    layout(DE)
    await quartz.type_text("zy")
    assert _downs(posted) == [(0x10, 0, "z"), (0x06, 0, "y")]


async def test_a_character_holds_option_when_its_layout_needs_it(quartz, posted, layout):
    layout(DE)
    await quartz.type_text("@")
    option_key = QuartzInput._MODIFIER_KEYCODES["option"]
    assert _downs(posted) == [(option_key, OPTION, None), (0x25, OPTION, "@")]
    assert posted[-1]["flags"] == 0  # released


async def test_without_a_layout_keys_are_us(quartz, posted):
    await quartz.key_press("z")
    assert _downs(posted) == [(0x06, 0, None)]
