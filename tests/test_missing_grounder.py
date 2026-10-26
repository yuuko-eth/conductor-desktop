"""A role with no backend configured: calls fail with a message for the agent."""

from __future__ import annotations

import pytest

from conductor_desktop.grounder import CURRENT_TOOL, GrounderError, GrounderNotConfigured, MissingGrounder


async def _raised(coro) -> str:
    with pytest.raises(GrounderNotConfigured) as e:
        await coro
    assert isinstance(e.value, GrounderError)  # the server wrapper's soft-error path
    return str(e.value)


async def test_ground_names_the_tool_the_env_var_and_the_route():
    token = CURRENT_TOOL.set("locate")
    try:
        msg = await _raised(MissingGrounder("ground").ground("b64", "Save button"))
    finally:
        CURRENT_TOOL.reset(token)
    assert msg.startswith("locate needs a grounder")
    assert "CONDUCTOR_DESKTOP_GROUNDER_URL" in msg
    assert "ui_find" in msg and "click_at" in msg


async def test_describe_and_region_messages():
    token = CURRENT_TOOL.set("read_screen")
    try:
        msg = await _raised(MissingGrounder("describe").describe("b64", hint="x"))
        assert msg.startswith("read_screen needs a describe model")
        assert "CONDUCTOR_DESKTOP_DESCRIBE_URL" in msg and "capture_screen" in msg
        img = await _raised(MissingGrounder("describe").describe_image("b64", "image/png"))
        assert img == msg
    finally:
        CURRENT_TOOL.reset(token)
    region = await _raised(MissingGrounder("region").ground("b64", "table"))
    assert region.startswith("this tool needs a grounder that returns bounding boxes")


async def test_close_is_a_no_op_and_roles_are_checked():
    await MissingGrounder("ground").close()
    with pytest.raises(ValueError):
        MissingGrounder("click")
