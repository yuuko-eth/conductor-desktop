"""Caller-supplied strings must reach scripts and device shells as data.

Window titles, typed text, clipboard text and key names all come from the
agent. Every backend that hands one to an interpreter (AppleScript, GNOME
Shell JS, PowerShell, the Android device ``sh``) must pass it so that no
value can close its literal and run as code. Each test feeds a hostile value
through the real code path and checks what the interpreter would receive.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import sys

import pytest

from conductor_desktop.core.backends import linux_gnome, mobile, windows

HOSTILE = [
    "plain",
    "it's",
    '" & (do shell script "touch /tmp/x") & "',
    "'); global.log('pwned'); ('",
    "trailing backslash \\",
    "\\'); evil(); //",
    "$(reboot)",
    "`reboot`; reboot",
    "a && b || c > /tmp/x",
    "new\nline",
    "unicode — 測試 🎉",
]


class _Proc:
    returncode = 0

    async def wait(self):
        return 0

    async def communicate(self, _input=None):
        return b"", b""


def _capture_exec(monkeypatch, module):
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **_kw):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    return calls


# ── macOS: AppleScript gets the title through argv ──────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend imports AppKit")
@pytest.mark.parametrize("title", HOSTILE)
async def test_macos_osascript_passes_values_as_argv(monkeypatch, title):
    from conductor_desktop.core.backends import macos

    calls = _capture_exec(monkeypatch, macos)
    script = "on run argv\ntell application (item 1 of argv) to activate\nend run"
    await macos._osascript(script, title)

    (argv,) = calls
    assert argv[:2] == ("osascript", "-e")
    assert list(argv[2:]) == [script, title]


# ── GNOME Shell: the title is a JSON string literal ─────────────────


@pytest.mark.parametrize("title", HOSTILE)
async def test_gnome_eval_embeds_title_as_json_literal(monkeypatch, title):
    seen: list[str] = []

    async def fake_eval(js: str) -> str:
        seen.append(js)
        return "ok"

    monkeypatch.setattr(linux_gnome, "_gnome_eval", fake_eval)
    await linux_gnome.focus_window(title=title)

    (js,) = seen
    literal = re.search(r"\.includes\((.*)\)\);", js).group(1)
    # The whole argument must be exactly one JSON string that decodes back.
    assert json.loads(literal) == title.lower()


# ── Windows: PowerShell fallback carries the text as base64 ─────────


@pytest.mark.parametrize("text", HOSTILE)
async def test_windows_clipboard_fallback_never_splices_text(monkeypatch, text):
    monkeypatch.setitem(sys.modules, "win32clipboard", None)  # force the fallback
    calls = _capture_exec(monkeypatch, windows)
    await windows.set_clipboard(text)  # raises BackendError on failure

    (argv,) = calls
    script = argv[-1]
    b64 = re.fullmatch(
        r"Set-Clipboard -Value \(\[Text\.Encoding\]::UTF8\.GetString\("
        r"\[Convert\]::FromBase64String\('([A-Za-z0-9+/=]*)'\)\)\)",
        script,
    ).group(1)
    assert base64.b64decode(b64).decode("utf-8") == text


# ── Android: `adb shell` argv is re-parsed by the device's sh ───────


def _device_sh_argv(adb_args: tuple[str, ...]) -> list[str]:
    """What the device's sh sees: adb joins the args after ``shell`` with spaces."""
    assert adb_args[0] == "shell"
    return shlex.split(" ".join(adb_args[1:]))


@pytest.fixture
def android(monkeypatch):
    ex = mobile.AndroidExecutor("emulator-5554")
    sent: list[tuple[str, ...]] = []

    async def fake_adb_text(*args, timeout=15):
        sent.append(args)
        return ""

    async def fake_adb(*args, timeout=15):
        sent.append(args)
        return b"", b"", 0

    monkeypatch.setattr(ex, "_adb_text", fake_adb_text)
    monkeypatch.setattr(ex, "_adb", fake_adb)
    return ex, sent


@pytest.mark.parametrize("text", HOSTILE)
async def test_android_type_text_reaches_input_intact(android, text):
    ex, sent = android
    assert (await ex.type_text(text)).success
    assert _device_sh_argv(sent[0]) == ["input", "text", text.replace(" ", "%s")]


@pytest.mark.parametrize("key", ["home", "KEYCODE_HOME;reboot", "x $(reboot)"])
async def test_android_key_press_is_one_argument(android, key):
    ex, sent = android
    await ex.key_press(key)
    argv = _device_sh_argv(sent[0])
    assert argv[:2] == ["input", "keyevent"] and len(argv) == 3


@pytest.mark.parametrize("target", ["com.android.settings", "com.foo/.Main", "x; reboot", "a/$(reboot)"])
async def test_android_app_launch_is_one_argument(android, target):
    ex, sent = android
    await ex.app_launch(target)
    assert target in _device_sh_argv(sent[0])
