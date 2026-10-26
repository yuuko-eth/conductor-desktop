"""What an acting tool's reply carries after the action.

Off by default: deltas only, no capture. AUTO_SCREENSHOT=1 adds the
screenshot; TEXT_ONLY=1 adds a description whether or not AUTO_SCREENSHOT
is on, because that's how a model without vision sees results at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from conductor_desktop.config import MCPConfig
from conductor_desktop.runtime import observe


def test_auto_screenshot_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_DESKTOP_AUTO_SCREENSHOT", raising=False)
    assert MCPConfig.from_env().auto_screenshot is False
    monkeypatch.setenv("CONDUCTOR_DESKTOP_AUTO_SCREENSHOT", "1")
    assert MCPConfig.from_env().auto_screenshot is True


@pytest.fixture
def calls(monkeypatch, install_state):
    seen: list[str] = []
    delta = TextContent(type="text", text="[deltas (no changes)]")
    monkeypatch.setattr(observe, "drain_deltas", lambda **kw: [delta])

    async def capture_png(device_id=None):
        seen.append("capture")
        return b"png", 10, 10

    async def emit_png(png, label, w, h, **kw):
        seen.append("emit")
        return [TextContent(type="text", text="<image>")]

    async def capture_for_grounder(device_id=None):
        seen.append("capture")
        return "b64", 10, 10

    async def describe(b64, hint=None):
        seen.append("describe")
        return "a dialog is open"

    monkeypatch.setattr(observe, "capture_png", capture_png)
    monkeypatch.setattr(observe, "emit_png", emit_png)
    monkeypatch.setattr(observe, "capture_for_grounder", capture_for_grounder)
    monkeypatch.setattr(observe, "rolling_hint", lambda device_id: None)

    def install(**cfg):
        config = SimpleNamespace(**{"auto_screenshot": False, "text_only": False,
                                    "describe_configured": True, **cfg})
        install_state(config=config, describe_grounder=SimpleNamespace(describe=describe))
        return seen

    return install


async def test_default_reply_is_deltas_only(calls):
    seen = calls()
    out = await observe.post_action_observation()
    assert [c.text for c in out] == ["[deltas (no changes)]"]
    assert seen == []


async def test_auto_screenshot_adds_the_screenshot(calls):
    seen = calls(auto_screenshot=True)
    await observe.post_action_observation()
    assert seen == ["capture", "emit"]


async def test_text_only_describes_even_with_auto_screenshot_off(calls):
    seen = calls(text_only=True)
    out = await observe.post_action_observation()
    assert seen == ["capture", "describe"]
    assert out[-1].text == "[screen] a dialog is open"
