"""`wait` and `wait_for` are bounded, and wait_for's deadline holds."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conductor_desktop.tools import observation


@pytest.fixture
def tools(monkeypatch, install_state):
    async def no_observation(*_a, **_kw):
        return []

    monkeypatch.setattr(observation, "post_action_observation", no_observation)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", no_observation)
    install_state(config=SimpleNamespace(wait_for_poll_interval=0.01))
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **kw):
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr(observation.asyncio, "sleep", fast_sleep)
    return slept


async def test_wait_is_capped_and_says_so(tools):
    out = await observation.wait(seconds=3600)
    assert tools == [observation._MAX_WAIT_S]
    assert "capped at 60s" in out.content[0].text


async def test_a_short_wait_is_not_capped(tools):
    out = await observation.wait(seconds=0.5)
    assert tools == [0.5] and "capped" not in out.content[0].text


async def test_wait_for_passes_screen_and_device(monkeypatch, tools):
    seen: list[dict] = []

    async def ground(target, **kw):
        seen.append(kw)
        return [SimpleNamespace(center=(5, 6))]

    monkeypatch.setattr(observation, "ground_to_screen_elements", ground)
    out = await observation.wait_for(target="OK button", screen=1)
    assert not out.isError and "at (5,6)" in out.content[0].text
    assert seen == [{"device_id": None, "screen": 1}]


async def test_wait_for_cuts_off_a_grounder_call_at_the_deadline(monkeypatch, tools):
    async def hang(target, **kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(observation, "ground_to_screen_elements", hang)
    out = await asyncio.wait_for(observation.wait_for(target="x", timeout=0.05), 2)
    assert out.isError and "timeout" in out.content[0].text
