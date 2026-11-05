"""macOS focus_window: which apps it tries, and that its AppKit reads drain.

``runningApplications()`` hands back a few hundred autoreleased
NSRunningApplications per call, and the event-loop thread never drains a
pool on its own: ~34 KB leaked per focus_window before the reads got one.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools

import pytest

pytest.importorskip("conductor_desktop.core.backends.macos")

from conductor_desktop.core.backends import macos


class _App:
    def __init__(self, pid: int, name: str) -> None:
        self._pid, self._name = pid, name

    def processIdentifier(self):
        return self._pid

    def localizedName(self):
        return self._name


def _fakes(apps):
    by_pid = {a.processIdentifier(): a for a in apps}

    class Workspace:
        @staticmethod
        def sharedWorkspace():
            return Workspace

        @staticmethod
        def runningApplications():
            return list(apps)

    class Running:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid):
            return by_pid.get(pid)

    return Workspace, Running


def test_app_names_come_before_window_titles_and_each_app_once():
    chrome, term, notes = _App(1, "Google Chrome"), _App(2, "Terminal"), _App(3, "Notes")
    ws, running = _fakes([chrome, term, notes])
    windows = [
        {"title": "Chrome tips — Notes", "process_name": "Notes", "pid": 3},
        {"title": "chrome docs", "process_name": "Google Chrome", "pid": 1},
    ]
    got = macos._focus_candidates("chrome", windows, ws, running)
    assert got == [chrome, notes]


def test_candidates_are_read_inside_an_autorelease_pool(monkeypatch):
    depth = []
    inside = []

    @contextlib.contextmanager
    def pool():
        depth.append(1)
        try:
            yield
        finally:
            depth.pop()

    async def no_windows():
        return []

    def candidates(*a):
        inside.append(bool(depth))
        return []

    monkeypatch.setattr(macos, "_autorelease_pool", pool)
    monkeypatch.setattr(macos, "list_windows", no_windows)
    monkeypatch.setattr(macos, "_focus_candidates", candidates)
    assert asyncio.run(macos.focus_window(title="anything")) is False
    assert inside == [True]


def test_loop_drain_reopens_its_pool_each_period_and_closes_it_on_cancel(monkeypatch):
    objc = pytest.importorskip("objc")
    events = []

    class Pool:
        def __enter__(self):
            events.append("open")

        def __exit__(self, *exc):
            events.append("drain")

    monkeypatch.setattr(objc, "autorelease_pool", Pool)

    async def run():
        task = asyncio.create_task(macos.drain_loop_autoreleased(period=0.01))
        await asyncio.sleep(0.035)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert events[0] == "open" and events[-1] == "drain"
    assert events.count("open") == events.count("drain") >= 3
    assert all(a != b for a, b in itertools.pairwise(events))  # never nested
