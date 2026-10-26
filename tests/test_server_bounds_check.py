"""`check_pixel_coords` rejects garbage `*_at` inputs at the server
boundary so we don't get phantom-success from backends that silently
clamp or no-op out-of-range coords."""

from __future__ import annotations

import pytest

from conductor_desktop.runtime import capture


class _FakeExecutor:
    def __init__(self, w: int, h: int) -> None:
        self._w = w
        self._h = h

    async def get_screen_dimensions(self, screen=None) -> tuple[int, int]:
        # Screen 1 is a 2560x1440 monitor beside the desktop's primary.
        return (2560, 1440) if screen == 1 else (self._w, self._h)


class _FakeState:
    def __init__(self, executor) -> None:
        self.executor = executor


@pytest.fixture
def fake_state(monkeypatch, install_state):
    state = _FakeState(_FakeExecutor(1920, 1080))
    install_state(state)
    return state


async def test_in_bounds_passes(fake_state) -> None:
    assert await capture.check_pixel_coords((100, 100)) is None
    assert await capture.check_pixel_coords((1919, 1079)) is None
    assert await capture.check_pixel_coords((0, 0)) is None


async def test_multi_monitor_slack_allowed(fake_state) -> None:
    """2× slack on every side accommodates plausible multi-monitor extents:
    secondary to the right (3839, 1079) and to the left (-1920, 0)."""
    assert await capture.check_pixel_coords((3839, 1079)) is None
    assert await capture.check_pixel_coords((-1919, 0)) is None


async def test_garbage_rejected(fake_state) -> None:
    """A model passing percentage (10000) or hallucinating wildly should
    be caught instead of silently no-oping."""
    err = await capture.check_pixel_coords((99999, 50))
    assert err is not None and "outside reasonable bounds" in err

    err = await capture.check_pixel_coords((50, -99999))
    assert err is not None


async def test_drag_rejects_either_endpoint(fake_state) -> None:
    """Both endpoints of a drag must validate — a good source paired with
    a garbage target should still error."""
    err = await capture.check_pixel_coords((100, 100), (99999, 99999))
    assert err is not None
    err = await capture.check_pixel_coords((99999, 99999), (100, 100))
    assert err is not None


async def test_screen_local_points_must_lie_on_that_screen(fake_state) -> None:
    assert await capture.check_pixel_coords((2559, 1439), screen=1) is None
    err = await capture.check_pixel_coords((2560, 10), screen=1)
    assert err is not None and "off screen 1" in err
    assert await capture.check_pixel_coords((-1, 10), screen=1) is not None
