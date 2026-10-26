"""Screenshots smaller than the screen: the reply says so, and image_coords maps back.

The bug this pins: inline screenshots were shrunk (short edge 540) while the
coordinate tools took screen pixels, and nothing said so. A driver with no
grounder clicked at 0.675× of where it meant to on a 1280×800 Mac.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from conductor_desktop.config import MCPConfig
from conductor_desktop.core.executor import ActionResult
from conductor_desktop.runtime.capture import FrameTarget, emit_png, shrink_png_for_payload
from conductor_desktop.tools import coords, mobile


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(("size", "cap", "want"), [
    ((1280, 800), 1568, (1280, 800)),      # a laptop goes out unscaled
    ((3840, 2160), 1568, (1568, 882)),     # 4K comes down by the long edge
    ((1080, 2340), 1568, (724, 1568)),     # a portrait phone keeps its shape
    ((3840, 2160), 0, (3840, 2160)),       # 0 disables the cap
])
def test_the_cap_is_on_the_long_edge(size, cap, want):
    _, w, h = shrink_png_for_payload(_png(*size), *size, cap)
    assert (w, h) == want


@pytest.fixture
def st(install_state, tmp_path):
    return install_state(
        config=SimpleNamespace(screenshot_mode="inline", payload_max_edge=1568,
                               screenshot_dir=str(tmp_path)),
        frames={},
    )


async def test_a_shrunk_screenshot_says_how_it_maps(st):
    out = await emit_png(_png(3136, 1960), "screen", 3136, 1960, frame=FrameTarget())
    note = out[1].text
    assert "1568x980" in note and "3136x1960" in note and "× 2.000" in note
    assert "image_coords=true" in note
    assert st.frames[None].scale == (2.0, 2.0)


async def test_an_unshrunk_screenshot_needs_no_note_but_is_remembered(st):
    out = await emit_png(_png(1280, 800), "screen", 1280, 800, frame=FrameTarget())
    assert [c.type for c in out] == ["image"]
    assert st.frames[None].scale == (1.0, 1.0)


async def test_a_crop_is_not_a_frame(st):
    out = await emit_png(_png(3136, 1960), "crop-x", 3136, 1960)
    assert [c.type for c in out] == ["image"]
    assert st.frames == {}


async def test_file_mode_frames_are_native(st):
    st.config.screenshot_mode = "file"
    await emit_png(_png(3136, 1960), "screen", 3136, 1960, frame=FrameTarget())
    assert st.frames[None].scale == (1.0, 1.0)


class _Executor:
    def __init__(self) -> None:
        self.clicks: list[tuple] = []

    async def get_screen_dimensions(self, screen=None) -> tuple[int, int]:
        return (3136, 1960)

    async def click(self, x, y, button="left", clicks=1, screen=None):
        self.clicks.append((x, y, screen))
        return ActionResult(success=True)

    async def tap(self, x, y):
        self.clicks.append((x, y, "tap"))
        return ActionResult(success=True)


@pytest.fixture
def executor(monkeypatch, st):
    async def no_observation(*_a, **_kw):
        return []

    monkeypatch.setattr(coords, "post_action_observation", no_observation)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", no_observation)
    st.executor = _Executor()
    return st.executor


async def test_image_coords_scale_back_to_the_screen(executor):
    await emit_png(_png(3136, 1960), "screen", 3136, 1960, frame=FrameTarget())
    out = await coords.click_at(400, 300, image_coords=True)
    assert not out.isError
    assert executor.clicks == [(800, 600, None)]


async def test_image_coords_carry_the_screen_the_image_was_of(executor):
    await emit_png(_png(3136, 1960), "screen-1", 3136, 1960, frame=FrameTarget(screen=1))
    await coords.click_at(10, 10, image_coords=True)
    assert executor.clicks == [(20, 20, 1)]


async def test_image_coords_refuse_a_different_screen(executor):
    await emit_png(_png(1280, 800), "screen-1", 1280, 800, frame=FrameTarget(screen=1))
    out = await coords.click_at(10, 10, screen=0, image_coords=True)
    assert out.isError and "screen=1" in out.content[0].text
    assert executor.clicks == []


async def test_image_coords_need_a_screenshot_first(executor):
    out = await coords.click_at(10, 10, image_coords=True)
    assert out.isError and "capture_screen" in out.content[0].text
    assert executor.clicks == []


async def test_without_the_flag_coordinates_pass_through(executor):
    await emit_png(_png(3136, 1960), "screen", 3136, 1960, frame=FrameTarget())
    await coords.click_at(400, 300)
    assert executor.clicks == [(400, 300, None)]


async def test_phone_taps_map_from_that_phones_screenshot(executor, monkeypatch):
    await emit_png(_png(1080, 2340), "screen-abc", 1080, 2340, frame=FrameTarget("abc"))
    # The host's frame must not leak into the phone's mapping.
    await emit_png(_png(1280, 800), "screen", 1280, 800, frame=FrameTarget())
    monkeypatch.setattr(mobile, "mobile_executor", lambda _d: executor)
    await mobile.mobile_tap("abc", 362, 784, image_coords=True)
    assert executor.clicks == [(540, 1170, "tap")]


def test_the_old_short_edge_setting_fails_loudly(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_PAYLOAD_MAX_WIDTH", "540")
    with pytest.raises(RuntimeError, match="PAYLOAD_MAX_EDGE"):
        MCPConfig.from_env()

