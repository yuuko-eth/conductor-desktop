"""The shared drag sequence every input backend gets from LocalInputBackend."""

from __future__ import annotations

import asyncio

import pytest

from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor.input import LocalInputBackend


class _Recorder(LocalInputBackend):
    def __init__(self, fail_at_move: int | None = None):
        self.calls: list[tuple] = []
        self._fail_at_move = fail_at_move

    async def _drag_press(self, x, y):
        self.calls.append(("press", x, y))

    async def _drag_move(self, x, y):
        self.calls.append(("move", x, y))
        if len([c for c in self.calls if c[0] == "move"]) == self._fail_at_move:
            raise BackendError("xdotool failed")

    async def _drag_release(self, x, y):
        self.calls.append(("release", x, y))

    async def click(self, *a): ...
    async def move_to(self, *a): ...
    async def scroll(self, *a): ...
    async def type_text(self, *a): ...
    async def key_press(self, *a): ...
    async def hotkey(self, *a): ...


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


async def test_drag_presses_steps_and_releases_at_the_target():
    rec = _Recorder()
    await rec.drag(0, 100, 40, 20, steps=4)
    assert rec.calls == [("press", 0, 100), ("move", 10, 80), ("move", 20, 60),
                         ("move", 30, 40), ("move", 40, 20), ("release", 40, 20)]


async def test_a_failed_step_still_releases_the_button():
    rec = _Recorder(fail_at_move=2)
    with pytest.raises(BackendError):
        await rec.drag(0, 0, 40, 0, steps=4)
    assert rec.calls[-1] == ("release", 20, 0)


async def test_a_failed_press_releases_nothing():
    class _PressFails(_Recorder):
        async def _drag_press(self, x, y):
            raise BackendError("no pointer")

    rec = _PressFails()
    with pytest.raises(BackendError):
        await rec.drag(0, 0, 10, 10)
    assert rec.calls == []
