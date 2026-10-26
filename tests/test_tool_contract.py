"""Properties every tool shares: what counts as a failure, and who needs the lock."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from conductor_desktop import tooling
from conductor_desktop.config import MCPConfig
from conductor_desktop.core.executor import ActionResult
from conductor_desktop.server import build_mcp
from conductor_desktop.tools import grounded, keyboard, system
from conductor_desktop.tray_state import TrayStateMachine


class _Audit:
    def __init__(self):
        self.calls: list[dict] = []

    def record(self, **kw):
        self.calls.append(kw)


class _Executor:
    def __init__(self, result: ActionResult):
        self.result = result
        self.clicks = 0

    async def key_press(self, key):
        return self.result

    async def get_clipboard(self):
        return self.result

    async def click(self, x, y, **_kw):
        self.clicks += 1
        return self.result


@pytest.fixture
def state(monkeypatch, install_state):
    def make(result: ActionResult):
        st = SimpleNamespace(
            config=SimpleNamespace(tool_timing=False),
            executor=_Executor(result),
            audit=_Audit(),
            tray_state=TrayStateMachine(),
            input_paused=threading.Event(),
            lock=SimpleNamespace(is_held=lambda: True),
        )
        install_state(st)
        return st
    return make


async def test_failed_action_is_an_error_result(state):
    st = state(ActionResult(success=False, error="xdotool: no display"))
    out = await keyboard.key("Enter")
    assert out.isError
    assert out.content[0].text == "error: key(Enter) — failed: xdotool: no display"
    assert st.audit.calls[-1]["ok"] is False
    assert st.tray_state.recent()[-1].success is False


async def test_successful_action_is_audited_as_success(state):
    st = state(ActionResult(success=True, output="pressed"))
    out = await keyboard.key("Enter")
    assert not out.isError
    assert out.content[0].text == "key(Enter) — pressed"
    assert st.audit.calls[-1]["ok"] is True


async def test_read_whose_text_starts_with_failed_is_not_a_failure(state):
    st = state(ActionResult(success=True, output="Failed login: check your password"))
    out = await system.clipboard_get()
    assert not out.isError
    assert out.content[0].text.startswith("Failed login")
    assert st.audit.calls[-1]["ok"] is True


async def test_tool_error_keeps_its_content_after_the_error_line(state, monkeypatch):
    state(ActionResult(success=True))

    async def unresolved(*_a, **_kw):
        return "could not locate 'the file icon'"

    async def observation(*_a, **_kw):
        return [TextContent(type="text", text="[screen]")]

    monkeypatch.setattr(grounded, "resolve_one", unresolved)
    monkeypatch.setattr(grounded, "post_action_observation", observation)
    out = await grounded.click("the file icon")
    assert out.isError
    assert [c.text for c in out.content] == [
        "error: could not locate 'the file icon'", "[screen]"]


async def test_paused_input_refuses_acting_tools_only(state):
    st = state(ActionResult(success=True, output="hello"))
    st.input_paused.set()
    refused = await keyboard.key("Enter")
    assert refused.isError and "paused" in refused.content[0].text
    assert st.audit.calls[-1]["error"] == "input_paused"
    assert not (await system.clipboard_get()).isError


async def test_double_click_is_one_call_not_two(state, monkeypatch):
    st = state(ActionResult(success=True))
    element = SimpleNamespace(center=(10, 20))

    async def resolve(*_a, **_kw):
        return element

    async def no_caveats(*_a, **_kw):
        return []

    async def no_observation(*_a, **_kw):
        return []

    monkeypatch.setattr(grounded, "resolve_one", resolve)
    monkeypatch.setattr(grounded, "hit_caveats", no_caveats)
    monkeypatch.setattr(grounded, "post_action_observation", no_observation)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", no_observation)

    await grounded.double_click("the file icon")
    assert [c["tool"] for c in st.audit.calls] == ["double_click"]
    assert st.executor.clicks == 1


def test_every_host_acting_tool_takes_the_input_lock():
    # Mobile tools act on a connected device, not the host desktop the lock
    # guards, so a spectator may still drive a phone.
    unlocked = sorted(
        spec.name for spec in tooling.specs().values()
        if spec.kind == "acting" and not spec.name.startswith("mobile_") and not spec.lock
    )
    assert not unlocked, f"acting tools without the input lock: {unlocked}"


def test_registered_tools_carry_read_only_hint_and_no_output_schema():
    import asyncio

    config = MCPConfig(grounder_url="", mobile_android_enabled=True,
                       describe_image_enabled=True, omniparser_url="http://parser")
    listed = {t.name: t for t in asyncio.run(build_mcp(config).list_tools())}
    assert set(listed) == set(tooling.specs())
    for name, spec in tooling.specs().items():
        assert listed[name].annotations.readOnlyHint is (spec.kind == "observing"), name
        assert listed[name].outputSchema is None, name


def test_gates_leave_tools_off_the_surface():
    import asyncio

    config = MCPConfig(grounder_url="", web_enabled=False, shell_enabled=False)
    names = {t.name for t in asyncio.run(build_mcp(config).list_tools())}
    assert not any(n.startswith(("web_", "mobile_")) for n in names)
    assert "exec_shell" not in names and "describe_image" not in names
    assert "enumerate_elements" not in names and "click" in names


@pytest.mark.parametrize(("process_name", "title", "expected"), [
    ("gnome-terminal-server", "user@host: ~", True),
    ("org.gnome.Ptyxis", "user@host: ~", True),
    ("st-256color", "user@host: ~", True),
    ("footclient", "vim notes.md", True),
    ("Alacritty", "", True),
    ("", "Terminal — zsh", True),
    ("firefox", "Stack Overflow - Where Developers Learn", False),
    ("code", "list.py — project", False),
    ("", "Foot massage booking", False),
    ("libreoffice", "Untitled 1 - LibreOffice Calc", False),
])
def test_terminal_detection(process_name, title, expected):
    from conductor_desktop.core.executor.resident import _is_terminal

    assert _is_terminal(process_name, title) is expected
