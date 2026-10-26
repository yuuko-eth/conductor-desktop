"""Backends raise BackendError; the executor and the tool wrapper report it.

A return value keeps one meaning (focus_window False = no match, get_clipboard
"" = empty clipboard); everything that went wrong carries its reason.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conductor_desktop.core.backends import _cli, _common
from conductor_desktop.core.backends.desktop import CAPABILITY_METHODS, Capabilities, DesktopBackend
from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor import ResidentExecutor
from conductor_desktop.core.models.host_node import HostNode

# ── _cli ────────────────────────────────────────────────────────────

PY = sys.executable


async def test_run_names_a_missing_tool():
    with pytest.raises(BackendError, match="not installed"):
        await _cli.run(["conductor-no-such-tool"])


async def test_run_reports_stderr_on_failure():
    with pytest.raises(BackendError, match=r"failed \(rc=3\): boom"):
        await _cli.run([PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])


async def test_run_ok_is_false_on_non_zero_but_raises_for_a_missing_tool():
    assert await _cli.run_ok([PY, "-c", "raise SystemExit(1)"]) is False
    assert await _cli.run_ok([PY, "-c", "pass"]) is True
    with pytest.raises(BackendError):
        await _cli.run_ok(["conductor-no-such-tool"])


async def test_run_times_out_with_a_reason():
    with pytest.raises(BackendError, match="timed out"):
        await _cli.run([PY, "-c", "import time; time.sleep(5)"], timeout=0.2)


@pytest.mark.parametrize("stderr", ["Error: target STRING not available", "Nothing is copied"])
async def test_an_empty_clipboard_reads_as_empty(monkeypatch, stderr):
    async def fake_run(cmd, timeout=5):
        raise BackendError(f"{cmd[0]} failed (rc=1): {stderr}")

    monkeypatch.setattr(_cli, "run", fake_run)
    assert await _cli.xclip_get() == ""


async def test_a_broken_clipboard_is_an_error(monkeypatch):
    async def fake_run(cmd, timeout=5):
        raise BackendError(f"{cmd[0]} failed (rc=1): Can't open display")

    monkeypatch.setattr(_cli, "run", fake_run)
    with pytest.raises(BackendError, match="Can't open display"):
        await _cli.xclip_get()


async def test_wayland_clipboard_falls_back_to_xclip_and_reports_both(monkeypatch):
    async def wl_fails():
        raise BackendError("wl-paste is not installed (not on PATH)")

    async def x_fails():
        raise BackendError("xclip is not installed (not on PATH)")

    monkeypatch.setattr(_cli, "wl_get", wl_fails)
    monkeypatch.setattr(_cli, "xclip_get", x_fails)
    with pytest.raises(BackendError, match=r"wl-paste .*; xclip "):
        await _cli.wl_or_x_get()


async def test_shell_timeout_is_an_error_but_a_non_zero_exit_is_a_result():
    result = await _common.shell_exec(f'"{PY}" -c "raise SystemExit(4)"')
    assert result.exit_code == 4
    with pytest.raises(BackendError, match="timed out"):
        await _common.shell_exec(f'"{PY}" -c "import time; time.sleep(5)"', timeout=0.2)


# ── GNOME: a refused Eval says so ───────────────────────────────────


async def test_gnome_eval_refusal_names_unsafe_mode(monkeypatch):
    from conductor_desktop.core.backends import linux_gnome

    async def refused(cmd, timeout=5):
        return "(false, '')\n"

    monkeypatch.setattr(linux_gnome, "_run", refused)
    with pytest.raises(BackendError, match="unsafe mode"):
        await linux_gnome.list_windows()


# ── The executor boundary ───────────────────────────────────────────


def _executor(**methods) -> ResidentExecutor:
    """An executor whose desktop backend has ``methods``, and the
    capabilities they belong to."""
    caps = Capabilities(**{cap: any(m in methods for m in names)
                           for cap, names in CAPABILITY_METHODS.items()})
    desktop = type("FakeDesktop", (DesktopBackend,), {
        "caps": caps, **{name: staticmethod(fn) for name, fn in methods.items()}})
    ex = ResidentExecutor(HostNode(uuid="h", label="t", host_type="resident"))
    ex._desktop = desktop()
    return ex


def _raise(message):
    async def fail(*_a, **_kw):
        raise BackendError(message)
    return fail


async def test_failed_clipboard_write_is_reported():
    ex = _executor(set_clipboard=_raise("xclip is not installed (not on PATH)"))
    result = await ex.set_clipboard("x")
    assert not result.success and "xclip is not installed" in result.error


async def test_focus_distinguishes_no_match_from_failure():
    no_match = _executor(focus_window=AsyncMock(return_value=False),
                         list_windows=AsyncMock(return_value=[{"title": "Editor"}]))
    result = await no_match.focus_window(title="Nope")
    assert not result.success and "no window matching 'Nope'" in result.error
    assert "'Editor'" in result.error

    broken = _executor(focus_window=_raise("GNOME Shell refused org.gnome.Shell.Eval"))
    result = await broken.focus_window(title="Editor")
    assert result.error == "GNOME Shell refused org.gnome.Shell.Eval"


async def test_list_windows_reports_the_backend_reason():
    ex = _executor(list_windows=_raise("wmctrl is not installed (not on PATH)"))
    result = await ex.list_windows()
    assert not result.success and "wmctrl" in result.error


async def test_an_unexpected_backend_exception_is_reported_by_type():
    async def bug():
        raise KeyError("bounds")

    result = await _executor(list_windows=bug).list_windows()
    assert not result.success and result.error.startswith("KeyError")


async def test_scene_graph_survives_missing_screens():
    ex = _executor(list_windows=AsyncMock(return_value=[{
        "id": "1", "title": "t", "process_name": "p", "pid": 1,
        "bounds": [0, 0, 10, 10], "focused": True}]),
        list_screens=_raise("xrandr failed"))
    graph = await ex.scene_graph()
    assert "1" in graph["windows"]


async def test_an_unknown_screen_raises_instead_of_inventing_a_size():
    ex = _executor(list_screens=AsyncMock(return_value=[
        {"index": 0, "name": "eDP-1", "bounds": [0, 0, 1280, 800], "primary": True}]))
    with pytest.raises(BackendError, match="no screen named 'HDMI-9'"):
        await ex.get_screen_dimensions("HDMI-9")
    with pytest.raises(BackendError, match="no screen named 'HDMI-9'"):
        await ex.capture_screenshot("HDMI-9")


async def test_paste_typing_leaves_the_text_on_the_clipboard():
    # No restore: the app may read the clipboard long after the paste key,
    # and would then paste the restored contents instead.
    set_calls = []

    async def set_clipboard(text):
        set_calls.append(text)

    get_clipboard = AsyncMock(return_value="old")
    ex = _executor(get_clipboard=get_clipboard, set_clipboard=set_clipboard)
    ex._platform = "macos"
    ex._input_backend = SimpleNamespace(hotkey=AsyncMock())
    result = await ex.type_text("long enough to paste")
    assert result.success
    assert set_calls == ["long enough to paste"]
    get_clipboard.assert_not_called()
    ex._input_backend.hotkey.assert_awaited_once_with(["cmd", "v"])


async def test_paste_typing_fails_when_the_clipboard_cant_be_set():
    ex = _executor(get_clipboard=AsyncMock(return_value="old"),
                   set_clipboard=_raise("wl-copy failed (rc=1)"))
    ex._platform = "macos"
    ex._input_backend = SimpleNamespace(hotkey=AsyncMock())
    result = await ex.type_text("long enough to paste")
    assert not result.success and "wl-copy" in result.error
    ex._input_backend.hotkey.assert_not_called()  # it would paste the old contents


@pytest.mark.parametrize(("text", "pasted"), [("1 é🎉", True), ("-a b", False)])
async def test_short_text_the_backend_cant_type_is_pasted(text, pasted):
    # ydotool types ASCII only; it dropped "é🎉" without a word.
    from conductor_desktop.core.executor import YdotoolInput

    set_clipboard = AsyncMock()
    ex = _executor(set_clipboard=set_clipboard)
    ex._platform = "linux-wlroots"
    ex._input_backend = SimpleNamespace(
        can_type=YdotoolInput().can_type, hotkey=AsyncMock(), type_text=AsyncMock())
    assert (await ex.type_text(text)).success
    assert set_clipboard.await_count == int(pasted)
    assert ex._input_backend.type_text.await_count == int(not pasted)


# ── The tool wrapper ────────────────────────────────────────────────


async def test_a_backend_error_reaches_the_agent_as_an_error_result(install_state):
    from conductor_desktop.tools import observation

    async def capture_fails(screen=None):
        raise BackendError("no screen named 'HDMI-9' (available: 'eDP-1')")

    install_state(executor=SimpleNamespace(capture_screenshot=capture_fails),
                  config=SimpleNamespace(text_only=False))
    out = await observation.capture_screen(screen="HDMI-9")
    assert out.isError
    assert out.content[0].text == "error: no screen named 'HDMI-9' (available: 'eDP-1')"
