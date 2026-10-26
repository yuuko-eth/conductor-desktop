"""Win32Input._pixel_to_absolute mapping correctness.

The previous form `int((x - vx) * 65535 / vw)` made the rightmost /
bottommost pixel column unreachable: on a 1920-wide monitor pixel 1919
mapped to ~65501, which Windows round-trips back to pixel 1918. The
new form uses (size - 1) as the denominator and round-to-nearest, with
a [0, 65535] clamp so out-of-bounds inputs from `mouse_move_at` don't
hit SendInput's undocumented behavior outside that range.
"""

from __future__ import annotations

from conductor_desktop.core.executor import Win32Input


def _make(vx: int, vy: int, vw: int, vh: int) -> Win32Input:
    """Build a Win32Input bypassing __init__ (which would touch
    ctypes.windll.user32 — only available on Windows)."""
    inst = Win32Input.__new__(Win32Input)

    class FakeUser32:
        @staticmethod
        def GetSystemMetrics(idx: int) -> int:
            return {76: vx, 77: vy, 78: vw, 79: vh}[idx]

    inst._user32 = FakeUser32()
    inst._SM_XVIRTUALSCREEN = 76
    inst._SM_YVIRTUALSCREEN = 77
    inst._SM_CXVIRTUALSCREEN = 78
    inst._SM_CYVIRTUALSCREEN = 79
    return inst


def test_corners_reach_full_range_on_primary_only() -> None:
    """Single 1920x1080 monitor at the origin: top-left → 0, bottom-right → 65535."""
    w = _make(0, 0, 1920, 1080)
    assert w._pixel_to_absolute(0, 0) == (0, 0)
    assert w._pixel_to_absolute(1919, 1079) == (65535, 65535)


def test_negative_origin_translated_before_normalizing() -> None:
    """Secondary monitor positioned to the left of primary: virtual origin
    is negative. The translation by (vx, vy) means a click at that
    monitor's top-left lands at normalized (0, 0)."""
    # Two side-by-side 1920x1080 monitors with secondary on the LEFT:
    # virtual rect is x=[-1920, 1920), y=[0, 1080) → vx=-1920, vw=3840.
    w = _make(-1920, 0, 3840, 1080)
    assert w._pixel_to_absolute(-1920, 0) == (0, 0)
    assert w._pixel_to_absolute(1919, 1079) == (65535, 65535)


def test_out_of_bounds_inputs_clamped() -> None:
    """`mouse_move_at` accepts any int. Negative or oversized coords
    produce a normalized value outside [0, 65535] which SendInput
    handles inconsistently — we clamp."""
    w = _make(0, 0, 1920, 1080)
    assert w._pixel_to_absolute(-9999, -9999) == (0, 0)
    assert w._pixel_to_absolute(99999, 99999) == (65535, 65535)


def test_old_off_by_one_is_fixed() -> None:
    """Regression: with the old `/ vw` + `int()` form, pixel 1919 on a
    1920-wide monitor mapped to 65501 → round-tripped to pixel 1918.
    The new form lands exactly at 65535."""
    w = _make(0, 0, 1920, 1080)
    ax, _ = w._pixel_to_absolute(1919, 0)
    assert ax == 65535


def test_zero_size_doesnt_divide_by_zero() -> None:
    """1px-wide degenerate virtual screen (or GetSystemMetrics returning
    0/1 in some sandbox) shouldn't divide by zero."""
    w = _make(0, 0, 1, 1)
    # vw = max(1 - 1, 1) = 1; so any x maps via /1. No exception.
    assert w._pixel_to_absolute(0, 0) == (0, 0)
