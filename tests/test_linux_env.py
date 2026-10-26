"""DISPLAY / XAUTHORITY autodetection (conductor_desktop.linux_env)."""

from __future__ import annotations

import os
import socket
import sys

import pytest

from conductor_desktop import linux_env


@pytest.fixture
def connects(monkeypatch):
    """Record every connect attempt; answer from a set of live addresses."""
    live: set = set()
    tried: list = []

    def fake(family, address):
        tried.append((family, address))
        return address in live

    monkeypatch.setattr(linux_env, "_connects", fake)
    return live, tried


def test_ssh_forwarded_display_is_checked_over_tcp(connects):
    live, tried = connects
    live.add(("localhost", 6010))
    assert linux_env._display_alive("localhost:10.0")
    assert tried == [(socket.AF_INET, ("localhost", 6010))]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract sockets are Linux-only")
def test_abstract_socket_counts_without_the_socket_file(connects):
    live, _ = connects
    live.add("\0/tmp/.X11-unix/X7")
    assert linux_env._display_alive(":7")
    assert linux_env._display_alive("unix:7.0")


def test_garbage_display_is_not_alive(connects):
    assert not linux_env._display_alive("nonsense")
    assert not linux_env._display_alive(":x")


@pytest.mark.parametrize("a, b, same", [
    (":0", ":0.0", True),
    ("unix:0", ":0", True),
    (":0", "localhost:10.0", False),
    (None, ":0", False),
])
def test_same_display(a, b, same):
    assert linux_env._same_display(a, b) is same


def test_session_xauthority_is_not_applied_to_a_forwarded_display(monkeypatch, tmp_path):
    cookie = tmp_path / "xauth"
    cookie.write_text("")
    monkeypatch.setattr(linux_env.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(linux_env, "_display_alive", lambda d: True)
    monkeypatch.setattr(linux_env, "_probe_proc_environ", lambda: (":0", str(cookie)))
    linux_env.autodetect_x11_env()
    assert "XAUTHORITY" not in os.environ
