"""Input method detection, and what type_text and key do when one composes.

Measured on real desktops (https://docs.conductor-mcp.io/guides/keyboard-layouts/): IBus Chewing took the key
presses xdotool and ydotool send for short ASCII text, so "-a b" came out as
a pending ㄇ composition, or as nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conductor_desktop.core import input_method
from conductor_desktop.core.input_method import InputMethod, is_bare_key

CHEWING = InputMethod(name="chewing", framework="ibus", composing=True)
US = InputMethod(name="xkb:us::eng", framework="ibus", composing=False)


def _use(monkeypatch, im):
    async def detect():
        return im
    input_method.forget()
    monkeypatch.setattr(input_method, "_detect", detect)


@pytest.mark.parametrize(("keys", "bare"), [
    (["a"], True), (["1"], True), (["shift", "b"], True), (["Shift_L", "?"], True),
    (["ctrl", "a"], False), (["cmd", "v"], False), (["enter"], False), (["space"], False),
    ([" "], False), (["shift", "tab"], False), ([], False)])
def test_which_presses_an_input_method_would_compose(keys, bare):
    assert is_bare_key(keys) is bare


@pytest.mark.parametrize(("engine", "composing"), [
    ("chewing", True), ("libpinyin", True), ("xkb:us::eng", False)])
async def test_ibus_composes_unless_the_engine_is_a_layout(monkeypatch, engine, composing):
    monkeypatch.setattr(input_method.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(input_method, "_output", AsyncMock(return_value=engine))
    found = await input_method._ibus()
    assert found == InputMethod(name=engine, framework="ibus", composing=composing)


@pytest.mark.parametrize(("state", "name", "composing"), [
    ("2", "chewing", True), ("1", "chewing", False), ("2", "keyboard-us", False)])
async def test_fcitx5_composes_when_active_on_an_input_method(monkeypatch, state, name, composing):
    monkeypatch.setattr(input_method.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    async def output(*cmd):
        return "1234" if cmd[0] == "pgrep" else name if "-n" in cmd else state
    monkeypatch.setattr(input_method, "_output", output)
    assert (await input_method._fcitx5()).composing is composing


async def test_fcitx5_not_running_is_no_answer(monkeypatch):
    monkeypatch.setattr(input_method.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(input_method, "_output", AsyncMock(return_value="0"))
    assert await input_method._fcitx5() is None


async def test_fcitx5_is_not_asked_unless_it_runs(monkeypatch):
    # D-Bus would start it for fcitx5-remote, and it would take over from IBus.
    monkeypatch.setattr(input_method.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    asked = []

    async def output(*cmd):
        asked.append(cmd[0])
        return ""  # pgrep: no such process
    monkeypatch.setattr(input_method, "_output", output)
    assert await input_method._fcitx5() is None
    assert asked == ["pgrep"]


@pytest.mark.parametrize(("xmodifiers", "first"), [("@im=fcitx", "fcitx5"), ("@im=ibus", "ibus"),
                                                   ("", "ibus")])
async def test_the_session_says_which_framework_to_ask_first(monkeypatch, xmodifiers, first):
    for k in ("GTK_IM_MODULE", "QT_IM_MODULE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XMODIFIERS", xmodifiers)
    asked = []

    def probe(name):
        async def run():
            asked.append(name)
        return run
    monkeypatch.setattr(input_method, "_ibus", probe("ibus"))
    monkeypatch.setattr(input_method, "_fcitx5", probe("fcitx5"))
    await input_method._linux()
    assert asked[0] == first


async def test_a_failing_probe_is_no_answer_not_an_error(monkeypatch):
    async def boom():
        raise OSError("no session bus")
    input_method.forget()
    monkeypatch.setattr(input_method, "_detect", boom)
    assert await input_method.current() is None


# ── The executor ────────────────────────────────────────────────────


def _executor(platform="linux-x11"):
    from conductor_desktop.core.backends.desktop import Capabilities, DesktopBackend
    from conductor_desktop.core.executor import ResidentExecutor
    from conductor_desktop.core.models.host_node import HostNode

    set_clipboard = AsyncMock()
    desktop = type("FakeDesktop", (DesktopBackend,), {
        "caps": Capabilities(clipboard=True), "set_clipboard": staticmethod(set_clipboard)})
    ex = ResidentExecutor(HostNode(uuid="h", label="t", host_type="resident"))
    ex._desktop = desktop()
    ex._desktop.set_clipboard = set_clipboard
    ex._platform = platform
    ex._input_backend = SimpleNamespace(can_type=AsyncMock(return_value=True), hotkey=AsyncMock(),
                                        type_text=AsyncMock(), key_press=AsyncMock())
    return ex


@pytest.mark.parametrize(("platform", "im", "pasted"), [
    ("linux-x11", CHEWING, True), ("linux-gnome", CHEWING, True), ("linux-x11", US, False),
    ("linux-x11", None, False),
    # Their key events carry the character, which input methods let through.
    ("windows", CHEWING, False), ("macos", CHEWING, False)])
async def test_short_text_is_pasted_past_a_composing_input_method(monkeypatch, platform, im, pasted):
    _use(monkeypatch, im)
    ex = _executor(platform)
    assert (await ex.type_text("-a b")).success
    assert ex._desktop.set_clipboard.await_count == int(pasted)
    assert ex._input_backend.type_text.await_count == int(not pasted)


async def test_keystrokes_still_type_but_say_the_input_method_may_take_them(monkeypatch):
    _use(monkeypatch, CHEWING)
    ex = _executor()
    result = await ex.type_text("-a b", literal=True)
    ex._input_backend.type_text.assert_awaited_once_with("-a b")
    assert "chewing (ibus)" in result.output and "keystrokes=true" in result.output


@pytest.mark.parametrize(("keys", "noted"), [(["a"], True), (["shift", "b"], True),
                                             (["ctrl", "a"], False), (["enter"], False)])
async def test_a_bare_key_under_a_composing_input_method_is_noted(monkeypatch, keys, noted):
    _use(monkeypatch, CHEWING)
    ex = _executor("windows")
    result = await (ex.key_press(keys[0]) if len(keys) == 1 else ex.hotkey(keys))
    assert result.success
    assert ("use type_text" in result.output) is noted


async def test_no_note_without_a_composing_input_method(monkeypatch):
    _use(monkeypatch, US)
    result = await _executor().key_press("a")
    assert result.success and result.output == ""
