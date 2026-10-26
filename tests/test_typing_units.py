"""The keystroke typers send characters outside the BMP whole.

Both APIs count UTF-16 units: Quartz takes a unit count with the string,
Win32's KEYEVENTF_UNICODE takes one unit per event. Counting code points
instead dropped emoji (seen on the macOS smoke run).
"""

from __future__ import annotations

import sys
import types

from conductor_desktop.core.executor import QuartzInput, Win32Input
from conductor_desktop.core.executor.input.base import _utf16_units


def test_utf16_units():
    assert _utf16_units("a") == [0x61]
    assert _utf16_units("é") == [0xE9]
    assert _utf16_units("🎉") == [0xD83C, 0xDF89]


async def test_quartz_passes_the_utf16_length(monkeypatch):
    set_calls = []
    fake = types.ModuleType("Quartz")
    fake.CGEventCreateKeyboardEvent = lambda src, code, down: {"down": down}
    fake.CGEventKeyboardSetUnicodeString = lambda ev, n, s: set_calls.append((n, s))
    fake.CGEventPost = lambda tap, ev: None
    fake.CGEventSetFlags = lambda ev, flags: None
    fake.kCGHIDEventTap = 0
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    typer = QuartzInput.__new__(QuartzInput)
    typer._source = object()

    await typer.type_text("é🎉")
    assert set_calls == [(1, "é"), (1, "é"), (2, "🎉"), (2, "🎉")]


async def test_win32_sends_surrogates_as_separate_events():
    typer = Win32Input.__new__(Win32Input)
    typer._KEYEVENTF_KEYUP, typer._KEYEVENTF_UNICODE = 0x2, 0x4
    sent = []
    typer._key_input = lambda vk, scan, flags: (scan, flags)
    typer._send_inputs = lambda *inputs: sent.append(inputs)

    await typer.type_text("a🎉")
    # One event per SendInput call: down and up never share a batch.
    assert sent == [((0x61, 0x4),), ((0x61, 0x6),),
                    ((0xD83C, 0x4),), ((0xD83C, 0x6),), ((0xDF89, 0x4),), ((0xDF89, 0x6),)]
