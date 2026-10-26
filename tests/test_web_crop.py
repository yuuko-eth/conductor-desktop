"""web_crop: one capture at a time per tab, and fixed headers hidden around it.

Fake registry and session, no browser required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conductor_desktop.browser import TabInfo
from conductor_desktop.tools import web

BBOX = {"kind": "ok", "x": 10, "y": 500, "w": 200, "h": 100}


class FakeSession:
    """Records evaluate/capture order; captures take a moment, like Chrome's."""

    def __init__(self, log: list[str], active: list[int]) -> None:
        self.log = log
        self.active = active

    async def evaluate(self, expression, *, await_promise=True):
        if "data-conductor-hidden" in expression and "setAttribute" in expression:
            self.log.append("hide")
            return 1
        if "removeAttribute('data-conductor-hidden')" in expression and "setAttribute" not in expression:
            self.log.append("restore")
            return None
        self.log.append("resolve")
        return dict(BBOX)

    async def capture_screenshot(self, *, clip=None, beyond_viewport=True, fmt="png"):
        self.active[0] += 1
        self.active[1] = max(self.active[1], self.active[0])
        self.log.append("capture")
        await asyncio.sleep(0.01)
        self.active[0] -= 1
        return b"png"

    async def close(self):
        pass


class FakeRegistry:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.active = [0, 0]  # in flight now, most at once

    async def select(self, **kw):
        endpoint = SimpleNamespace(open=self._open, observer=None)
        return endpoint, TabInfo("chrome", 2, "target-2", "Docs", "http://localhost/")

    async def _open(self, tab):
        return FakeSession(self.log, self.active)


@pytest.fixture
def reg(monkeypatch, install_state):
    install_state(config=SimpleNamespace(cdp_default_tab_filter=None, deltas_enabled=False))
    registry = FakeRegistry()
    monkeypatch.setattr(web, "_registry", lambda: registry)
    monkeypatch.setattr(web, "png_dims", lambda png: (200, 100))
    monkeypatch.setattr(web, "drain_deltas", lambda **kw: [])

    async def fake_emit(png, label, w, h, *a, **kw):
        return []

    monkeypatch.setattr(web, "emit_png", fake_emit)
    web._crop_locks.clear()
    return registry


async def test_concurrent_crops_of_one_tab_take_turns(reg):
    await asyncio.gather(web.web_crop(selector="#a"), web.web_crop(selector="#b"))
    assert reg.active[1] == 1
    # Each crop's resolve, hide, capture and restore finish before the next starts.
    assert reg.log == ["resolve", "hide", "capture", "restore"] * 2


async def test_overlays_are_restored_when_the_capture_fails(reg, monkeypatch):
    async def boom(self, **kw):
        reg.log.append("capture")
        raise web.CDPError("capture failed")

    monkeypatch.setattr(FakeSession, "capture_screenshot", boom)
    out = await web.web_crop(selector="#a")
    assert out.isError
    assert reg.log == ["resolve", "hide", "capture", "restore"]


async def test_hide_fixed_off_hides_nothing_but_still_clears_the_mark(reg):
    await web.web_crop(selector="#a", hide_fixed=False)
    assert reg.log == ["resolve", "capture", "restore"]


def test_overlay_scripts_find_the_marked_target_and_clear_the_mark():
    assert "data-conductor-crop" in web._HIDE_OVERLAYS_JS
    assert "__ALL__" not in web._HIDE_OVERLAYS_JS
    assert "removeAttribute('data-conductor-crop')" in web._RESTORE_OVERLAYS_JS
