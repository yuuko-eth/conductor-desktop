"""Screen-aware routing in ResidentExecutor.

Exercises the ``screen=`` param threading: monitor selector resolution,
single-monitor no-op semantics, and absolute-coords translation for
pointer actions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conductor_desktop.core.backends.desktop import Capabilities, DesktopBackend
from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor import ResidentExecutor
from conductor_desktop.core.models.host_node import HostNode
from conductor_desktop.runtime import capture
from conductor_desktop.tooling import ToolError


class _Screens(DesktopBackend):
    caps = Capabilities(screens=True)

    def __init__(self, screens: list[dict]):
        self.list_screens = AsyncMock(return_value=screens)


def _make_executor(
    screens: list[dict] | None,
    platform: str = "linux-x11",
) -> tuple[ResidentExecutor, AsyncMock]:
    """Build a ResidentExecutor wired to a fake backend + input backend.

    Returns the executor and the AsyncMock that records input-backend calls
    so tests can assert on absolute coords.
    """
    host = HostNode(
        uuid="h", label="t", host_type="resident", status="online",
    )
    ex = ResidentExecutor(host)
    ex._platform = platform

    if screens is not None:
        ex._desktop = _Screens(screens)

    fake_input = SimpleNamespace(
        click=AsyncMock(return_value=None),
        move_to=AsyncMock(return_value=None),
        scroll=AsyncMock(return_value=None),
        drag=AsyncMock(return_value=None),
    )
    ex._input_backend = fake_input
    return ex, fake_input


# ── _resolve_screen ────────────────────────────────────────────────


async def test_resolve_none_is_full_desktop() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    idx, target, screens = await ex._resolve_screen(None)
    assert idx is None
    assert target is None
    assert len(screens) == 1


async def test_resolve_int_index() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    idx, target, _ = await ex._resolve_screen(1)
    assert idx == 1
    assert target["name"] == "HDMI-2"


async def test_resolve_primary_string() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    idx, target, _ = await ex._resolve_screen("primary")
    assert idx == 1
    assert target["name"] == "HDMI-2"


async def test_resolve_name_exact_match() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "DP-2", "bounds": [1920, 0, 2560, 1440],
         "scale": 1.0, "primary": False},
    ])
    idx, target, _ = await ex._resolve_screen("DP-2")
    assert idx == 1
    assert target["bounds"][2] == 2560


async def test_resolve_unknown_name_raises() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    with pytest.raises(BackendError, match="no screen named"):
        await ex._resolve_screen("DP-99")


async def test_resolve_out_of_range_raises() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    with pytest.raises(BackendError, match="out of range"):
        await ex._resolve_screen(1)


async def test_resolve_negative_index_raises() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    with pytest.raises(BackendError, match="out of range"):
        await ex._resolve_screen(-1)


async def test_resolve_no_backend_screens_raises() -> None:
    """Out-of-range / unknown screen must error if list_screens is empty —
    don't silently fall through to monitor 0."""
    ex, _ = _make_executor(screens=[])
    with pytest.raises(BackendError, match=r"screen list, which isn.t available"):
        await ex._resolve_screen(0)


# ── Single-monitor no-op semantics ────────────────────────────────


async def test_single_monitor_screen_0_is_noop() -> None:
    """A script written for multi-monitor (passing screen=0) should run on
    a single-monitor laptop without code changes."""
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "eDP-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    result = await ex.click(100, 200, screen=0)
    assert result.success
    # Single monitor at origin (0,0) → coords pass through unchanged.
    fake_input.click.assert_called_once_with(100, 200, "left", 1)


async def test_single_monitor_screen_primary_is_noop() -> None:
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "eDP-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    result = await ex.click(50, 60, screen="primary")
    assert result.success
    fake_input.click.assert_called_once_with(50, 60, "left", 1)


async def test_single_monitor_out_of_range_still_errors() -> None:
    """The no-op only applies to screen=0/primary; screen=1 on a single-
    monitor host is still a real out-of-range error."""
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "eDP-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
    ])
    result = await ex.click(100, 200, screen=1)
    assert not result.success
    assert "out of range" in result.error
    fake_input.click.assert_not_called()


# ── Coordinate translation on multi-monitor ───────────────────────


async def test_click_screen1_offsets_by_monitor_origin() -> None:
    """A click at screen-local (100,200) on the second monitor (which sits
    at virtual desktop origin 1920,0) must end up at absolute (2020,200)."""
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    result = await ex.click(100, 200, screen=1)
    assert result.success
    fake_input.click.assert_called_once_with(2020, 200, "left", 1)


async def test_click_no_screen_uses_absolute_coords() -> None:
    """Without screen=, coords are pure virtual-desktop absolutes — no
    offsetting happens even on multi-monitor setups."""
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    result = await ex.click(2500, 400)
    assert result.success
    fake_input.click.assert_called_once_with(2500, 400, "left", 1)


async def test_drag_offsets_both_endpoints() -> None:
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    result = await ex.drag(10, 20, 30, 40, screen=1)
    assert result.success
    fake_input.drag.assert_called_once_with(
        1930, 20, 1950, 40, steps=20, hold_ms=300,
    )


async def test_scroll_offsets_anchor_point() -> None:
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    result = await ex.scroll(100, 200, "down", amount=5, screen=1)
    assert result.success
    fake_input.scroll.assert_called_once_with(2020, 200, "down", 5)


async def test_move_to_screen_by_name() -> None:
    ex, fake_input = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "DP-2", "bounds": [-1920, 0, 1920, 1080],
         "scale": 1.0, "primary": False},
    ])
    # Monitor-left-of-primary at origin (-1920, 0) — coords should subtract.
    result = await ex.move_to(50, 60, screen="DP-2")
    assert result.success
    fake_input.move_to.assert_called_once_with(-1870, 60)


# ── get_screen_dimensions ─────────────────────────────────────────


async def test_get_screen_dimensions_per_monitor() -> None:
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 0, 2560, 1440],
         "scale": 1.0, "primary": False},
    ])
    assert await ex.get_screen_dimensions(screen=1) == (2560, 1440)


async def test_get_screen_dimensions_virtual_desktop_union() -> None:
    """screen=None on multi-monitor should return the full virtual extent,
    not just the primary."""
    ex, _ = _make_executor(screens=[
        {"index": 0, "name": "HDMI-1", "bounds": [0, 0, 1920, 1080],
         "scale": 1.0, "primary": True},
        {"index": 1, "name": "HDMI-2", "bounds": [1920, 100, 2560, 1440],
         "scale": 1.0, "primary": False},
    ])
    # Union: x=[0..4480], y=[0..1540] → 4480x1540.
    assert await ex.get_screen_dimensions(screen=None) == (4480, 1540)


# ── Server-level mutual exclusion ─────────────────────────────────


def test_screen_and_device_id_are_mutually_exclusive() -> None:
    """``screen=`` is for host monitors; mobile devices have no
    list_screens. The combination must fail at the server boundary, not
    silently feed `screen=` into the mobile path."""

    with pytest.raises(ToolError, match="screen= is for host monitors"):
        capture.check_screen_device(device_id="emulator-5554", screen=0)


def test_screen_alone_or_device_alone_is_fine() -> None:

    # Each on its own is allowed; combinations of None with either too.
    capture.check_screen_device(device_id=None, screen=None)
    capture.check_screen_device(device_id="emulator-5554", screen=None)
    capture.check_screen_device(device_id=None, screen=0)
    capture.check_screen_device(device_id=None, screen="primary")
