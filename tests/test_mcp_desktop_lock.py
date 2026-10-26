"""Tests for the conductor-desktop desktop input lock and the spectator gate."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from conductor_desktop.desktop_lock import DesktopLock

# ── Lock primitive ───────────────────────────────────────────────────


def test_first_acquire_wins(tmp_path: Path) -> None:
    p = tmp_path / "desktop.lock"
    a = DesktopLock(p, transport="stdio")
    b = DesktopLock(p, transport="stdio")
    assert a.try_acquire() is True
    assert a.is_held() is True
    # Second instance pointing at same file is blocked by the OS-level flock.
    assert b.try_acquire() is False
    assert b.is_held() is False
    a.release()


def test_release_lets_other_acquire(tmp_path: Path) -> None:
    p = tmp_path / "desktop.lock"
    a = DesktopLock(p)
    b = DesktopLock(p)
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    a.release()
    assert a.is_held() is False
    assert b.try_acquire() is True
    b.release()


def test_holder_metadata_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "desktop.lock"
    holder = DesktopLock(p, transport="sse")
    assert holder.try_acquire()
    info = holder.read_holder_info()
    assert info is not None
    assert info.pid == os.getpid()
    assert info.transport == "sse"
    assert info.started_at  # non-empty ISO timestamp
    holder.release()
    # After release the meta file should be gone.
    assert holder.read_holder_info() is None


def test_release_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "desktop.lock"
    a = DesktopLock(p)
    a.try_acquire()
    a.release()
    a.release()  # second call is a no-op, must not raise


def test_acquire_reentrant_returns_true(tmp_path: Path) -> None:
    p = tmp_path / "desktop.lock"
    a = DesktopLock(p)
    assert a.try_acquire() is True
    # Already held; re-acquire from the same instance is a no-op success.
    assert a.try_acquire() is True
    a.release()


def test_stale_meta_does_not_block_acquire(tmp_path: Path) -> None:
    """If a previous holder crashed without releasing, the OS released the
    flock for us. A leftover .json from that holder must not prevent a new
    process from acquiring — try_acquire still works.
    """
    p = tmp_path / "desktop.lock"
    meta = p.with_suffix(p.suffix + ".json")
    # Simulate a stale meta file from a previous holder that crashed.
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text('{"pid": 99999999, "started_at": "old", "transport": "stdio"}')
    a = DesktopLock(p)
    assert a.try_acquire() is True
    info = a.read_holder_info()
    assert info is not None
    # New holder overwrote the metadata with its own PID.
    assert info.pid == os.getpid()
    a.release()


# ── the input lock in the tool wrapper ────────────────────────────────


def _acting(fn):
    """``fn`` wrapped like an acting tool, without adding it to the surface."""
    from conductor_desktop.tooling import _wrap
    return _wrap(fn, fn.__name__, "acting", True)


@pytest.fixture
def fake_state(tmp_path: Path, monkeypatch, install_state):
    """Install a minimal server state whose only meaningful field is the lock.

    Each test starts with a fresh, *un-acquired* lock; tests opt in by
    calling `state.lock.try_acquire()` themselves.
    """
    p = tmp_path / "desktop.lock"

    class _Stub:
        def __init__(self):
            self.lock = DesktopLock(p)
            self.lock_upgrade_mu = asyncio.Lock()

    stub = _Stub()
    install_state(stub)
    yield stub
    stub.lock.release()


@pytest.mark.asyncio
async def test_decorator_blocks_when_no_lock(fake_state) -> None:
    """Spectator: lock not held by us, *and* held by another process →
    the call is an error result and the tool body never runs.
    """
    # Take the lock from a *different* DesktopLock instance to simulate a
    # holder living in another process.
    foreign_holder = DesktopLock(fake_state.lock.path, transport="stdio")
    assert foreign_holder.try_acquire()
    try:
        called = []

        async def fn():
            called.append(1)
            return "should-not-run"

        result = await _acting(fn)()
        assert called == [], "wrapped fn must not run when lock is contended"
        assert result.isError and len(result.content) == 1
        text = result.content[0].text
        assert "desktop input lock held by" in text
        assert "pid=" in text  # surfaces holder identity
    finally:
        foreign_holder.release()


@pytest.mark.asyncio
async def test_decorator_passes_through_when_we_hold_lock(fake_state) -> None:
    assert fake_state.lock.try_acquire()
    called = []

    async def fn(x):
        called.append(x)
        return f"ran({x})"

    result = await _acting(fn)(7)
    assert called == [7]
    assert result.content[0].text == "ran(7)"


@pytest.mark.asyncio
async def test_decorator_opportunistic_upgrade(fake_state) -> None:
    """Spectator promoted to holder when no other process holds the lock.

    The wrapper finds we don't hold it, tries try_acquire, succeeds, and
    runs the tool.
    """
    assert not fake_state.lock.is_held()
    called = []

    async def fn():
        called.append("ran")
        return "ok"

    result = await _acting(fn)()
    assert called == ["ran"]
    assert result.content[0].text == "ok"
    assert fake_state.lock.is_held(), "the call should have promoted us to holder"


def test_release_removes_the_metadata_before_freeing_the_lock(tmp_path: Path,
                                                              monkeypatch) -> None:
    # Once the lock is free the next holder writes its metadata; deleting
    # ours after that would delete theirs.
    from conductor_desktop import desktop_lock

    lock = DesktopLock(tmp_path / "desktop.lock")
    assert lock.try_acquire()
    seen: list[bool] = []
    real_unlock = desktop_lock._funlock

    def spy(fd):
        seen.append(lock.meta_path.exists())
        real_unlock(fd)

    monkeypatch.setattr(desktop_lock, "_funlock", spy)
    lock.release()
    assert seen == [False]
