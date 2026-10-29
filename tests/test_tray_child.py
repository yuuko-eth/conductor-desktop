"""Linux and FreeBSD run the tray in a process of its own.

GTK's main loop and libatspi in one process shared GLib's default main
context, and the server segfaulted in libglib or hung (Ubuntu 22.04 GNOME X11).
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from conductor_desktop import server, tray, tray_child


class Gate:
    def __init__(self) -> None:
        self.wanted: list[str] = []

    def want(self, state: str) -> None:
        self.wanted.append(state)


def test_the_child_applies_states_and_menus():
    gate, menu, refreshed = Gate(), tray_child.Menu(), []
    assert tray_child.handle('{"state": "acting"}', gate, menu, lambda: refreshed.append(1))
    assert tray_child.handle(json.dumps({"menu": {"recent": ["ui_find · ok 40ms at 12:00:00"],
                                                  "status": ["PID: 1"], "paused": True}}),
                             gate, menu, lambda: refreshed.append(1))
    assert gate.wanted == ["acting"]
    assert menu.recent == ["ui_find · ok 40ms at 12:00:00"] and menu.paused and refreshed == [1]
    assert not tray_child.handle('{"stop": true}', gate, menu, lambda: None)
    assert tray_child.handle("not json", gate, menu, lambda: None)  # logged, not fatal


# A stand-in tray process: records what the server sends, and picks Quit
# from the menu once it has seen a state.
FAKE_TRAY = textwrap.dedent("""
    import json, sys
    seen = open(sys.argv[1], "w")
    for line in sys.stdin:
        seen.write(line); seen.flush()
        if "state" in json.loads(line):
            print(json.dumps({"action": "quit"}), flush=True)
""")


class Config:
    transport, host, port, backend, dashboard, auth_token = "stdio", "", 0, "resident", False, ""


@pytest.fixture
def fake_tray(monkeypatch, tmp_path):
    seen = tmp_path / "seen.jsonl"
    script = tmp_path / "fake_tray.py"
    script.write_text(FAKE_TRAY)
    real_popen = subprocess.Popen
    children = []

    def popen(cmd, **kw):
        children.append(real_popen([sys.executable, str(script), str(seen)], **kw))
        return children[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    return seen, children


def test_quit_in_the_tray_shuts_the_server_down(fake_tray, monkeypatch):
    seen, children = fake_tray

    async def serve(config, lock, shutdown_event=None, tray_state=None):
        call = tray_state.start("ui_find", klass="observing")  # a state for the tray
        tray_state.end(call, success=True)
        await shutdown_event.wait()  # set when the tray's Quit arrives

    monkeypatch.setattr(server, "_serve_async", serve)
    tray._run_holder_with_tray_child(Config(), lock=None)

    lines = [json.loads(line) for line in seen.read_text().splitlines()]
    assert lines[0]["status_text"].startswith("conductor-desktop · pid=")
    assert any("state" in line for line in lines)
    assert children[0].poll() is not None, "the tray process outlived the server"


def test_a_server_that_dies_at_startup_takes_the_tray_with_it(fake_tray, monkeypatch):
    _seen, children = fake_tray

    async def refuse(*_a, **_kw):
        raise RuntimeError("conductor-desktop refuses to start")

    monkeypatch.setattr(server, "_serve_async", refuse)
    with pytest.raises(RuntimeError, match="refuses to start"):
        tray._run_holder_with_tray_child(Config(), lock=None)
    assert children[0].poll() is not None


def test_the_server_env_keeps_accessibility_for_apps_it_launches(fake_tray, monkeypatch):
    """NO_AT_BRIDGE is the tray child's alone: exec_shell hands our env to the
    apps the agent starts, and with it set ui_* can't read them."""
    monkeypatch.delenv("NO_AT_BRIDGE", raising=False)
    monkeypatch.setattr(tray, "_TRAY_IN_CHILD", True)
    monkeypatch.setattr(tray, "_run_holder_with_tray_child", lambda config, lock: None)
    tray.run_holder(Config(), lock=None)
    import os
    assert "NO_AT_BRIDGE" not in os.environ
