"""`scroll_at`: the coordinate twin of `scroll`, for a driver with no grounder."""

from __future__ import annotations

import pytest

from conductor_desktop.core.executor import ActionResult
from conductor_desktop.tools import coords


class _Executor:
    def __init__(self) -> None:
        self.scrolls: list[tuple] = []

    async def get_screen_dimensions(self) -> tuple[int, int]:
        return (1920, 1080)

    async def scroll(self, x, y, direction, amount=3, screen=None):
        self.scrolls.append((x, y, direction, amount, screen))
        return ActionResult(success=True)


@pytest.fixture
def executor(monkeypatch, install_state):
    async def no_observation(*_a, **_kw):
        return []

    monkeypatch.setattr(coords, "post_action_observation", no_observation)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", no_observation)
    ex = _Executor()
    install_state(executor=ex)
    return ex


async def test_scrolls_at_the_given_point(executor):
    out = await coords.scroll_at(300, 400, "down", amount=5)
    assert not out.isError
    assert executor.scrolls == [(300, 400, "down", 5, None)]
    assert "scroll_at(300,400) down x5" in out.content[0].text


async def test_rejects_a_point_off_every_screen(executor):
    out = await coords.scroll_at(99_999, 400, "down")
    assert out.isError
    assert executor.scrolls == []


def test_it_is_an_acting_tool_that_takes_the_lock(kind):
    assert kind("scroll_at") == "acting"
