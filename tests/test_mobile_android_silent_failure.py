"""Regression: AndroidExecutor used to swallow both adb returncode and
stderr — `tap` reported `success=True, "Tapped (123,456)"` when the
underlying `adb shell input tap` had failed (device offline, screen
locked, INJECT_EVENTS denied, OOM). These tests pin the new behavior:

  - rc != 0 → raises with stderr surfaced
  - rc == 0 but device-side stderr matches a failure pattern → raises
  - timeout → raises with descriptive message
  - clean run → returns stdout
  - get_dimensions stays resilient (probe-time failures fall back to
    the default screen size rather than crashing the grounded action
    path).
"""

from __future__ import annotations

import asyncio

import pytest

from conductor_desktop.core.backends.mobile import AndroidExecutor


def _fake_create(stdout: bytes, stderr: bytes, rc: int, *, hang: bool = False):
    """Build a fake `asyncio.create_subprocess_exec` returning a proc whose
    `communicate()` yields (stdout, stderr) and whose returncode is `rc`.
    If `hang=True`, communicate() blocks forever — for timeout tests."""

    class FakeProc:
        returncode = rc

        async def communicate(self) -> tuple[bytes, bytes]:
            if hang:
                await asyncio.sleep(3600)
            return stdout, stderr

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return self.returncode

    async def factory(*_args, **_kwargs):
        return FakeProc()

    return factory


# ── _adb / _adb_text contract ──────────────────────────────────────


async def test_adb_text_raises_on_nonzero_rc(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"error: device offline", rc=1),
    )
    ex = AndroidExecutor("emulator-5554")
    with pytest.raises(RuntimeError, match=r"rc=1.*device offline"):
        await ex._adb_text("shell", "input", "tap", "100", "200")


async def test_adb_text_raises_on_device_side_exception(monkeypatch) -> None:
    """`adb shell` exited 0 but the device-side `input` binary threw —
    the legacy adb shell protocol doesn't propagate exit codes from the
    inner command, so the rc=0 alone is misleading."""
    stderr = (
        b"java.lang.SecurityException: Injecting to another application "
        b"requires INJECT_EVENTS permission"
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", stderr, rc=0),
    )
    ex = AndroidExecutor("emulator-5554")
    with pytest.raises(RuntimeError, match=r"device-side error.*SecurityException"):
        await ex._adb_text("shell", "input", "tap", "100", "200")


async def test_adb_text_raises_on_monkey_error_type(monkeypatch) -> None:
    """`monkey` reports missing components with `Error type N` to stderr
    while exiting 0. Used by app_launch's pure-package-name path."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"** Error type 3\n** Component not found", rc=0),
    )
    ex = AndroidExecutor("emulator-5554")
    with pytest.raises(RuntimeError, match="Error type"):
        await ex._adb_text("shell", "monkey", "-p", "com.bogus", "1")


async def test_adb_text_raises_on_oom_kill(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"Killed", rc=0),
    )
    ex = AndroidExecutor("emulator-5554")
    with pytest.raises(RuntimeError, match="Killed"):
        await ex._adb_text("shell", "input", "swipe", "0", "0", "100", "100")


async def test_adb_text_clean_run_returns_stdout(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"Physical size: 1080x2400\n", b"", rc=0),
    )
    ex = AndroidExecutor("emulator-5554")
    out = await ex._adb_text("shell", "wm", "size")
    assert "1080x2400" in out


async def test_adb_raises_with_descriptive_timeout_message(monkeypatch) -> None:
    """Hung adb (e.g. wedged adbd, USB pull mid-call) must surface as a
    timeout RuntimeError, not propagate as a bare asyncio.TimeoutError
    that the action-layer try/except renders unhelpfully."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"", rc=0, hang=True),
    )
    ex = AndroidExecutor("emulator-5554")
    with pytest.raises(RuntimeError, match=r"timed out after 0\.05s"):
        await ex._adb("shell", "input", "tap", "1", "1", timeout=0.05)


# ── tap / swipe propagate the failure to ActionResult ──────────────


async def test_tap_returns_failure_action_result_on_rc_failure(monkeypatch) -> None:
    """Round-trip the rc-check fix through `tap` to confirm callers
    see ActionResult(success=False) instead of phantom success."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"error: device unauthorized", rc=1),
    )
    ex = AndroidExecutor("emulator-5554")
    result = await ex.tap(100, 200)
    assert result.success is False
    assert "unauthorized" in (result.error or "")


async def test_swipe_returns_failure_on_device_side_exception(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"java.lang.SecurityException: ...", rc=0),
    )
    ex = AndroidExecutor("emulator-5554")
    result = await ex.swipe(0, 0, 100, 100)
    assert result.success is False
    assert "SecurityException" in (result.error or "")


# ── get_dimensions stays resilient ─────────────────────────────────


async def test_get_dimensions_falls_back_when_adb_fails(monkeypatch) -> None:
    """Probe-time adb hiccups (transient unauthorized state, ANRs in
    `wm size`) must not crash the grounded mobile-action path. Default
    1080×1920 is wrong for many devices but the brain re-grounds from
    the actual screenshot, so a wrong-by-aspect-ratio dimension is
    recoverable while an exception isn't."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_create(b"", b"error: device offline", rc=1),
    )
    ex = AndroidExecutor("emulator-5554")
    w, h = await ex.get_dimensions()
    assert (w, h) == (1080, 1920)


async def test_get_dimensions_uses_cached_value(monkeypatch) -> None:
    """Once cached, no adb call is needed — verifies the cache path is
    untouched by the resilience change."""
    ex = AndroidExecutor("emulator-5554")
    ex._screen_w, ex._screen_h = 1440, 3088

    async def boom(*_args, **_kwargs):
        raise AssertionError("should not call adb when cached")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert await ex.get_dimensions() == (1440, 3088)
