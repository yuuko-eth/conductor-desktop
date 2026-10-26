"""A server that dies at startup must not leave the tray holding the process.

When host permissions are missing, the server refuses to start within a
fraction of a second, which can be before the tray icon exists. The worker's
icon.stop() then found nothing to stop, icon.run() blocked forever, and the
MCP client hung waiting on a process that would never answer (fedora-sway,
2026-10-16).
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from conductor_desktop import server, tray


class _FakeIcon:
    """Blocks in run() until stop(), like pystray; calls setup from a thread."""

    def __init__(self) -> None:
        self.visible = False
        self._stopped = threading.Event()
        self.timed_out = False

    def run(self, setup=None) -> None:
        if setup is not None:
            threading.Thread(target=setup, args=(self,), daemon=True).start()
        if not self._stopped.wait(timeout=5):
            self.timed_out = True

    def stop(self) -> None:
        self._stopped.set()


def test_startup_crash_before_the_icon_exists_ends_the_process(monkeypatch):
    # The in-process tray (macOS, Windows); Linux's runs in its own process.
    monkeypatch.setattr(tray, "_TRAY_IN_CHILD", False)
    fake_pystray = types.ModuleType("pystray")
    fake_pystray.Icon = type("Icon", (), {"__module__": "pystray._fake"})
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)

    async def refuse(*_a, **_kw):
        raise RuntimeError("conductor-desktop refuses to start")

    monkeypatch.setattr(server, "_serve_async", refuse)

    icon = _FakeIcon()

    def build_icon_after_the_worker_died(config, lock, loop_holder, worker, state):
        worker.join(timeout=5)  # the race, made certain
        return icon, threading.Event()

    monkeypatch.setattr(tray, "_build_icon", build_icon_after_the_worker_died)

    with pytest.raises(RuntimeError, match="refuses to start"):
        tray.run_holder(config=None, lock=None)
    assert not icon.timed_out, "the tray kept the process alive after the server died"
