"""Shared fixtures for conductor-desktop tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from conductor_desktop import state as state_mod
from conductor_desktop.audit import AuditLog


class _HeldLock:
    def is_held(self) -> bool:
        return True


@pytest.fixture
def install_state(monkeypatch):
    """Install a fake server state for tool calls.

    Tests build fakes with just the fields their tool reads. This fills in
    the ones the tool wrapper reads on every call (tray, audit, pause flag,
    lock, timing) with inert defaults, and returns the installed object.
    """
    def install(st=None, **fields):
        st = st if st is not None else SimpleNamespace()
        for key, value in fields.items():
            setattr(st, key, value)
        defaults = {
            "config": SimpleNamespace(),
            "tray_state": None,
            "audit": AuditLog(enabled=False),
            "input_paused": threading.Event(),
            "lock": _HeldLock(),
        }
        for key, value in defaults.items():
            if not hasattr(st, key):
                setattr(st, key, value)
        if not hasattr(st.config, "tool_timing"):
            st.config.tool_timing = False
        monkeypatch.setattr(state_mod, "_current", st)
        return st
    return install


@pytest.fixture
def surface():
    """``surface(**fields)``: the tools a server built from
    ``MCPConfig(**fields)`` lists, by name."""
    import asyncio

    from conductor_desktop.config import MCPConfig
    from conductor_desktop.server import build_mcp

    def listed(**config_fields) -> dict:
        config = MCPConfig(**{"grounder_url": "", **config_fields})
        return {t.name: t for t in asyncio.run(build_mcp(config).list_tools())}
    return listed


@pytest.fixture
def kind():
    """``kind(name)``: a declared tool's kind, "acting" or "observing"."""
    import conductor_desktop.tools  # noqa: F401  (declares the tools)
    from conductor_desktop import tooling

    return lambda name: tooling.specs()[name].kind


@pytest.fixture(autouse=True)
def no_input_method(monkeypatch):
    """No input method unless a test sets one: detection would otherwise ask
    the machine running the tests (its IBus, its macOS input source)."""
    from conductor_desktop.core import input_method

    async def none():
        return None

    input_method.forget()
    monkeypatch.setattr(input_method, "_detect", none)
    yield
    input_method.forget()


@pytest.fixture(autouse=True)
def no_keyboard_layout(monkeypatch):
    """US positions unless a test sets a layout: detection would otherwise
    ask the machine running the tests."""
    from conductor_desktop.core import keyboard_layout

    async def none():
        return None

    keyboard_layout.forget()
    monkeypatch.setattr(keyboard_layout, "active", none)
    yield
    keyboard_layout.forget()


@pytest.fixture(autouse=True)
def no_mac_layout(monkeypatch):
    """US key codes unless a test sets a macOS layout."""
    from conductor_desktop.core.executor.input import mac_layout

    monkeypatch.setattr(mac_layout, "current", lambda: None)
