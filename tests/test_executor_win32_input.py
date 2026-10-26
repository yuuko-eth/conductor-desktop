"""Win32Input's wheel directions and SendInput failure reporting, without Windows."""

from __future__ import annotations

import types

import pytest

from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor import Win32Input


def _typer():
    typer = Win32Input.__new__(Win32Input)
    typer._MOUSEEVENTF_MOVE = 0x1
    typer._ABS_DESK = 0xC000
    typer._pixel_to_absolute = lambda x, y: (x, y)
    typer._mouse_input = lambda dx, dy, flags, data=0: (flags, data)
    sent = []
    typer._send_inputs = lambda *inputs: sent.extend(inputs)
    return typer, sent


@pytest.mark.parametrize(("direction", "flag", "data"), [
    ("up", 0x0800, 360), ("down", 0x0800, -360),
    ("left", 0x1000, -360), ("right", 0x1000, 360),
])
async def test_scroll_uses_the_horizontal_wheel_for_left_and_right(direction, flag, data):
    typer, sent = _typer()
    await typer.scroll(10, 20, direction, 3)
    assert sent[-1] == (flag, data)


async def test_scroll_rejects_an_unknown_direction():
    typer, sent = _typer()
    with pytest.raises(BackendError, match="unknown scroll direction"):
        await typer.scroll(10, 20, "sideways", 3)
    assert sent == []


def test_a_blocked_sendinput_raises(monkeypatch):
    class _Input:  # stands in for the ctypes INPUT type: INPUT * n builds an array
        def __mul__(self, n):
            return lambda *items: items

    typer = Win32Input.__new__(Win32Input)
    typer._INPUT = _Input()
    typer._user32 = types.SimpleNamespace(SendInput=lambda n, arr, size: 0)
    monkeypatch.setattr("ctypes.sizeof", lambda _t: 40)
    with pytest.raises(BackendError, match="inserted 0 of 2"):
        typer._send_inputs("down", "up")
