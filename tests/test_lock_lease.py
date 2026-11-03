"""The desktop input lock as a lease: idle release, shared pause, handover.

Two servers on one machine share the lock file. The holder lets go after
``idle_release`` seconds without a tool call, whoever acts next takes it,
the pause switch is a file every server sees, and the tray follows the lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from conftest import FakeTrayLock

from conductor_desktop import server, tooling, tray
from conductor_desktop.desktop_lock import DesktopLock, HolderInfo, PauseFlag
from conductor_desktop.tools import system


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "desktop.lock"


def _other_holder(lock_path, idle_release=300.0) -> DesktopLock:
    """A second DesktopLock on the same file stands in for another server:
    flock excludes per open file description, so it contends like one."""
    other = DesktopLock(lock_path, transport="stdio", idle_release=idle_release)
    assert other.try_acquire()
    return other


# ── lock metadata, listeners, pause file ─────────────────────────────


def test_meta_records_activity_and_idle_policy(lock_path):
    holder = DesktopLock(lock_path, idle_release=120)
    assert holder.try_acquire()
    try:
        info = DesktopLock(lock_path).read_holder_info()
        assert info.pid == os.getpid() and info.idle_release == 120
        assert info.last_active and info.idle_seconds() < 5
    finally:
        holder.release()


def test_old_meta_without_new_fields_still_reads():
    info = HolderInfo.from_dict({"pid": 7, "started_at": "2026-11-03T00:00:00+00:00",
                                 "transport": "stdio"})
    assert info.idle_release == 0 and info.last_active == ""
    assert info.idle_seconds() is not None  # falls back to started_at


def test_listeners_hear_acquire_and_release(lock_path):
    lock = DesktopLock(lock_path)
    heard = []
    lock.add_listener(heard.append)
    lock.try_acquire()
    lock.try_acquire()  # already held: no second event
    lock.release()
    lock.release()  # not held: no event
    assert heard == [True, False]


def test_pause_is_shared_through_the_file(lock_path):
    a = PauseFlag(DesktopLock(lock_path).pause_path)
    b = PauseFlag(DesktopLock(lock_path).pause_path)
    assert not a.is_set()
    a.set()
    assert b.is_set()  # another server sees it
    b.clear()
    assert not a.is_set()


# ── idle release ─────────────────────────────────────────────────────


def _st(lock, **kw):
    return SimpleNamespace(lock=lock, calls_in_flight=0, last_activity=0.0,
                           idle_released_at=None, input_paused=PauseFlag(lock.pause_path),
                           **kw)


def test_holder_lets_go_after_the_idle_limit(lock_path):
    lock = DesktopLock(lock_path, idle_release=300)
    lock.try_acquire()
    st = _st(lock)
    assert not server.release_if_idle(st, now=299.0)
    assert server.release_if_idle(st, now=301.0)
    assert not lock.is_held() and st.idle_released_at == 301.0


@pytest.mark.parametrize("why", ["in_flight", "paused", "disabled"])
def test_holder_keeps_the_lock(lock_path, why):
    lock = DesktopLock(lock_path, idle_release=0 if why == "disabled" else 300)
    lock.try_acquire()
    st = _st(lock)
    if why == "in_flight":
        st.calls_in_flight = 1  # a long wait_for is not idle
    if why == "paused":
        st.input_paused.set()  # the tray to resume from goes with the lock
    try:
        assert not server.release_if_idle(st, now=10_000.0)
        assert lock.is_held()
    finally:
        lock.release()
        st.input_paused.clear()


# ── taking the lock, and refusals ────────────────────────────────────


def _tool_state(install_state, lock):
    return install_state(
        config=SimpleNamespace(tool_timing=False),
        lock=lock, lock_upgrade_mu=asyncio.Lock(),
        input_paused=PauseFlag(lock.pause_path),
        executor=SimpleNamespace(), tray_state=None,
    )


async def _act():
    ran = []

    async def fn():
        ran.append(1)
        return "acted"

    return await tooling._wrap(fn, "click_at", "acting", needs_lock=True)(), ran


async def test_refusal_says_when_the_holder_lets_go(install_state, lock_path):
    _tool_state(install_state, DesktopLock(lock_path, idle_release=300))
    other = _other_holder(lock_path, idle_release=300)
    try:
        result, ran = await _act()
    finally:
        other.release()
    text = result.content[0].text
    assert result.isError and ran == []
    assert f"pid {os.getpid()}" in text
    assert "lets go after 5 min 00 s idle" in text and "retrying in about" in text
    assert "release_input" in text and "Release input lock" in text


async def test_a_server_that_lost_the_lock_is_told(install_state, lock_path):
    st = _tool_state(install_state, DesktopLock(lock_path, idle_release=300))
    st.idle_released_at = 1.0
    other = _other_holder(lock_path)
    try:
        result, _ = await _act()
    finally:
        other.release()
    assert "This server let go of the desktop after 5 min 00 s idle" in result.content[0].text


async def test_acting_takes_a_free_lock_back(install_state, lock_path):
    lock = DesktopLock(lock_path, idle_release=300)
    st = _tool_state(install_state, lock)
    st.idle_released_at = 1.0
    result, ran = await _act()
    try:
        assert not result.isError and ran == [1]
        assert lock.is_held() and st.idle_released_at is None
    finally:
        lock.release()


async def test_paused_with_no_holder_takes_the_lock_then_refuses(install_state, lock_path):
    """With nobody holding the lock there's no tray icon to resume from;
    taking it brings the icon back, and the pause still stops the action."""
    lock = DesktopLock(lock_path, idle_release=300)
    st = _tool_state(install_state, lock)
    st.input_paused.set()
    try:
        result, ran = await _act()
        assert result.isError and "paused" in result.content[0].text and ran == []
        assert lock.is_held()
    finally:
        st.input_paused.clear()
        lock.release()


async def test_calls_count_as_activity(install_state, lock_path):
    lock = DesktopLock(lock_path, idle_release=300)
    st = _tool_state(install_state, lock)
    st.last_activity = 0.0
    seen_in_flight = []

    async def fn():
        seen_in_flight.append(st.calls_in_flight)
        return "ok"

    await tooling._wrap(fn, "list_windows", "observing", needs_lock=False)()
    assert seen_in_flight == [1] and st.calls_in_flight == 0
    assert st.last_activity > 0.0


# ── release_input ────────────────────────────────────────────────────


async def test_release_input_hands_the_desktop_over(install_state, lock_path):
    lock = DesktopLock(lock_path, idle_release=300)
    _tool_state(install_state, lock)
    lock.try_acquire()
    out = await system.release_input()
    assert "released" in out.content[0].text and not lock.is_held()
    other = _other_holder(lock_path)  # another server can take it now
    other.release()
    again = await system.release_input()
    assert "doesn't hold" in again.content[0].text


# ── the Linux tray follows the lock ──────────────────────────────────


FAKE_TRAY = textwrap.dedent("""
    import sys
    for line in sys.stdin:
        pass
""")


class _Config:
    transport, host, port, backend, dashboard, auth_token = "stdio", "", 0, "resident", False, ""


def test_tray_child_runs_only_while_holding(monkeypatch, tmp_path):
    script = tmp_path / "fake_tray.py"
    script.write_text(FAKE_TRAY)
    real_popen = subprocess.Popen
    children = []

    def popen(cmd, **kw):
        children.append(real_popen([sys.executable, str(script)], **kw))
        return children[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    lock = FakeTrayLock(held=False)
    alive_at = {}

    async def serve(config, lock_, shutdown_event=None, tray_state=None):
        await asyncio.sleep(0.05)
        alive_at["spectator"] = len(children)
        lock.try_acquire()
        await asyncio.sleep(0.05)
        alive_at["held"] = [c.poll() is None for c in children]
        lock.release()
        await asyncio.sleep(0.5)
        alive_at["released"] = [c.poll() is None for c in children]

    monkeypatch.setattr(server, "_serve_async", serve)
    tray._run_holder_with_tray_child(_Config(), lock)

    assert alive_at["spectator"] == 0, "a spectator shows no tray"
    assert alive_at["held"] == [True]
    assert alive_at["released"] == [False], "the tray goes with the lock"


def test_tray_child_menu_offers_release():
    from pathlib import Path

    from conductor_desktop import tray_child
    src = Path(tray_child.__file__).read_text()
    assert '"Release input lock"' in src and '{"action": "release"}' in src


def test_meta_is_json(lock_path):
    holder = DesktopLock(lock_path, idle_release=60)
    holder.try_acquire()
    try:
        data = json.loads(holder.meta_path.read_text())
        assert set(data) >= {"pid", "started_at", "transport", "last_active", "idle_release"}
    finally:
        holder.release()
