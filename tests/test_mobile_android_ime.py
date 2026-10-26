"""Android type_text warns when the text likely didn't land: adb's `input
text` reports success while a composing keyboard (Gboard Zhuyin, Pinyin,
kana) holds the key events as a composition, and it drops non-ASCII."""

from __future__ import annotations

import pytest

from conductor_desktop.core.backends.mobile import AndroidExecutor, _composing_ime_note

GBOARD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"


@pytest.mark.parametrize("out", [
    f"{GBOARD}\nen-US\n",
    f"{GBOARD}\nzh-TW\n  mCurrentSubtype=InputMethodSubtype{{locale=en_US mode=keyboard}}\n",
    "null\nja-JP\n",
    "",
])
def test_no_note_for_plain_keyboards(out):
    assert _composing_ime_note(out) == ""


def test_composing_only_keyboard_package():
    out = "com.google.android.apps.inputmethod.zhuyin/.ZhuyinIME\nen-US\n"
    assert "composes key events" in _composing_ime_note(out)


def test_current_subtype_locale_settles_it():
    out = f"{GBOARD}\nen-US\n  mCurrentSubtype=InputMethodSubtype{{languageTag=zh-TW mode=keyboard}}\n"
    assert "zh layout" in _composing_ime_note(out)


def test_device_locale_stands_in_without_a_subtype():
    note = _composing_ime_note(f"{GBOARD}\nzh-TW\n")
    assert "zh-TW" in note and "candidate bar" in note


async def test_type_text_appends_warnings(monkeypatch):
    calls = []

    async def fake_adb_text(self, *args, timeout=15):
        calls.append(args)
        return f"{GBOARD}\nja-JP\n" if "settings" in args[1] else ""

    monkeypatch.setattr(AndroidExecutor, "_adb_text", fake_adb_text)
    result = await AndroidExecutor("emulator-5554").type_text("héllo")
    assert result.success
    assert "warning:" in result.output
    assert "non-ASCII" in result.output and "ja-JP" in result.output


async def test_type_text_survives_a_failed_ime_read(monkeypatch):
    async def fake_adb_text(self, *args, timeout=15):
        if "settings" in args[1]:
            raise RuntimeError("adb gone")
        return ""

    monkeypatch.setattr(AndroidExecutor, "_adb_text", fake_adb_text)
    result = await AndroidExecutor("emulator-5554").type_text("hello")
    assert result.success and "warning" not in result.output
