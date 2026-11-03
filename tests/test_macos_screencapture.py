"""macOS captures go through /usr/sbin/screencapture, not an in-process grab.

In-process grabs (mss → CGWindowListCreateImage) stall 30 s on recent macOS
when a second live process from the same executable path captures (replayd
keys clients by path). These tests pin the replacement's behaviour without a
real display: argv, Retina scaling to points, stitching, and the mss fallback.
"""

from __future__ import annotations

import asyncio

import pytest
from PIL import Image

from conductor_desktop.core.backends import _common


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def communicate(self):
        return b"", b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_screencapture(monkeypatch):
    """Stand-in for screencapture: writes a BMP at ``scale`` x the requested
    rect, filled with a colour picked from the rect's x origin."""
    calls: list[list[str]] = []
    opts = {"scale": 2, "returncode": 0}

    async def fake_exec(*argv, **_kw):
        calls.append(list(argv))
        if opts["returncode"] == 0:
            x, y, w, h = (int(v) for v in argv[argv.index("-R") + 1].split(","))
            colour = (255, 0, 0) if x == 0 else (0, 0, 255)
            size = (int(w * opts["scale"]), int(h * opts["scale"]))
            Image.new("RGB", size, colour).save(argv[-1], "BMP")
        return _FakeProc(opts["returncode"])

    monkeypatch.setattr(_common.sys, "platform", "darwin")
    monkeypatch.setattr(_common.os.path, "exists",
                        lambda p, _real=_common.os.path.exists: p == _common._SCREENCAPTURE or _real(p))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls, opts


def _decode(shot) -> Image.Image:
    import base64
    import io
    return Image.open(io.BytesIO(base64.b64decode(shot.png_b64)))


async def test_full_desktop_single_display_scaled_to_points(fake_screencapture, monkeypatch):
    calls, _ = fake_screencapture
    monkeypatch.setattr(_common, "_macos_display_rects", lambda: [(0, 0, 1280, 800)])
    monkeypatch.setattr(_common, "_mss_grab", lambda *a: pytest.fail("mss must not run"))

    shot = await _common.take_screenshot()

    assert (shot.width, shot.height) == (1280, 800)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "/usr/sbin/screencapture"
    assert "-x" in argv  # no shutter sound
    assert argv[argv.index("-R") + 1] == "0,0,1280,800"


async def test_single_screen_uses_its_bounds(fake_screencapture, monkeypatch):
    calls, _ = fake_screencapture
    monkeypatch.setattr(_common, "_macos_display_rects", lambda: pytest.fail("not needed"))
    screens = [{"bounds": [0, 0, 1280, 800]}, {"bounds": [-1920, -200, 1920, 1080]}]

    shot = await _common.take_screenshot(screen=1, screens=screens)

    assert (shot.width, shot.height) == (1920, 1080)
    assert calls[0][calls[0].index("-R") + 1] == "-1920,-200,1920,1080"


async def test_non_integer_scale_is_resized(fake_screencapture, monkeypatch):
    _, opts = fake_screencapture
    opts["scale"] = 1.5
    monkeypatch.setattr(_common, "_macos_display_rects", lambda: [(0, 0, 1000, 600)])

    shot = await _common.take_screenshot()

    assert (shot.width, shot.height) == (1000, 600)


async def test_two_displays_stitched_like_mss_monitor_zero(fake_screencapture, monkeypatch):
    calls, _ = fake_screencapture
    # Secondary display to the left of and above the primary.
    monkeypatch.setattr(_common, "_macos_display_rects",
                        lambda: [(0, 0, 1280, 800), (-1920, -200, 1920, 1080)])

    img = _decode(await _common.take_screenshot())

    assert img.size == (1920 + 1280, 1080)
    assert len(calls) == 2
    img = img.convert("RGB")
    assert img.getpixel((1920 + 10, 200 + 10)) == (255, 0, 0)  # primary
    assert img.getpixel((10, 10)) == (0, 0, 255)  # secondary
    assert img.getpixel((1920 + 10, 1079)) == (0, 0, 0)  # below primary: uncovered


async def test_falls_back_to_mss_when_screencapture_fails(fake_screencapture, monkeypatch):
    _, opts = fake_screencapture
    opts["returncode"] = 1
    monkeypatch.setattr(_common, "_macos_display_rects", lambda: [(0, 0, 1280, 800)])
    grabbed = []

    def fake_mss(screen, display=None):
        grabbed.append(screen)
        return Image.new("RGB", (1280, 800), (10, 200, 30))

    monkeypatch.setattr(_common, "_mss_grab", fake_mss)

    shot = await _common.take_screenshot()

    assert grabbed == [None]
    assert (shot.width, shot.height) == (1280, 800)


async def test_other_platforms_skip_screencapture(monkeypatch):
    monkeypatch.setattr(_common.sys, "platform", "linux")
    monkeypatch.setattr(_common, "_macos_screencapture",
                        lambda *a: pytest.fail("screencapture is macOS-only"))
    monkeypatch.setattr(_common, "_mss_grab",
                        lambda *a: Image.new("RGB", (640, 480), (10, 200, 30)))

    shot = await _common.take_screenshot()

    assert (shot.width, shot.height) == (640, 480)
