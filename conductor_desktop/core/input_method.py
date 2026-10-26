"""Which input method turns key presses into text, and whether it composes.

An input method in native mode (Zhuyin, Chewing, Pinyin, kana) takes a bare
letter or digit key as the start of a composition: on Zhuyin, "a" is ㄇ, not
"a". type_text's characters get past it on Windows and macOS, where each
event carries its character; on Linux, xdotool and ydotool send short ASCII
text as key presses, which IBus and fcitx5 compose like any other.

What was measured on real desktops is in https://docs.conductor-mcp.io/guides/keyboard-layouts/.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("conductor_desktop.input_method")


@dataclass(frozen=True)
class InputMethod:
    name: str            # "chewing", "com.apple.inputmethod.TCIM.Zhuyin", "zh 04040404"
    framework: str       # "ibus", "fcitx5", "macos", "windows"
    composing: bool      # bare key presses may start a composition instead of typing

    def describe(self) -> str:
        return f"{self.name} ({self.framework})"


# Windows keyboard layouts whose language comes with a composing IME: the
# layout is all Windows reports, since a TSF IME's native/English mode isn't
# readable from another process. Vietnamese Telex composes across two keys
# (aa is â); the classic Vietnamese layout doesn't, but shares the language.
_COMPOSING_LANGUAGES = {0x04: "zh", 0x11: "ja", 0x12: "ko", 0x2A: "vi"}

_CACHE_SECONDS = 2.0
_cache: tuple[float, InputMethod | None] | None = None


async def current() -> InputMethod | None:
    """The input method for the focused window, or None when it can't be
    told. Cached for two seconds: type_text and key ask on every call."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    try:
        found = await _detect()
    except Exception:  # detection is advice; never fail an action over it
        log.debug("input method detection failed", exc_info=True)
        found = None
    _cache = (now, found)
    return found


def forget() -> None:
    """Drop the cached answer (tests, or after switching input methods)."""
    global _cache
    _cache = None


async def _detect() -> InputMethod | None:
    if sys.platform == "win32":
        return _windows()
    if sys.platform == "darwin":
        return _macos()
    return await _linux()


def _windows() -> InputMethod | None:
    import ctypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    thread = user32.GetWindowThreadProcessId(ctypes.c_void_p(user32.GetForegroundWindow()), None)
    hkl = (user32.GetKeyboardLayout(thread) or 0) & 0xFFFFFFFF
    if not hkl:
        return None
    lang = _COMPOSING_LANGUAGES.get(hkl & 0x3FF)
    return InputMethod(name=f"{lang or 'layout'} {hkl:08x}", framework="windows",
                       composing=lang is not None)


def _macos() -> InputMethod | None:
    import ctypes
    import ctypes.util
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                      ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    source = carbon.TISCopyCurrentKeyboardInputSource()
    if not source:
        return None
    try:
        sid = carbon.TISGetInputSourceProperty(
            source, ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID"))
        buf = ctypes.create_string_buffer(256)
        if not sid or not cf.CFStringGetCString(sid, buf, len(buf), 0x08000100):  # UTF-8
            return None
    finally:
        cf.CFRelease(source)
    name = buf.value.decode()
    # A keyboard layout is com.apple.keylayout.*; input methods (Zhuyin,
    # Pinyin, Kotoeri) are com.apple.inputmethod.* or a third party's id.
    return InputMethod(name=name, framework="macos",
                       composing=not name.startswith("com.apple.keylayout."))


async def _linux() -> InputMethod | None:
    hints = " ".join(os.environ.get(k, "") for k in ("XMODIFIERS", "GTK_IM_MODULE",
                                                    "QT_IM_MODULE")).lower()
    order = (_fcitx5, _ibus) if "fcitx" in hints else (_ibus, _fcitx5)
    for probe in order:
        if (found := await probe()) is not None:
            return found
    return None


async def _ibus() -> InputMethod | None:
    if not shutil.which("ibus"):
        return None
    engine = await _output("ibus", "engine")
    if not engine:
        return None
    # xkb:us::eng and the like are plain keyboard layouts.
    return InputMethod(name=engine, framework="ibus", composing=not engine.startswith("xkb:"))


async def _fcitx5() -> InputMethod | None:
    # fcitx5-remote talks D-Bus, and D-Bus starts fcitx5 when it isn't
    # running; fcitx5 then took over from a running IBus. Ask only a running one.
    if not shutil.which("fcitx5-remote") or not await _running("fcitx5"):
        return None
    # 0: not running, 1: inactive (plain keyboard), 2: active.
    state = await _output("fcitx5-remote")
    if state not in ("1", "2"):
        return None
    name = await _output("fcitx5-remote", "-n") or "fcitx5"
    return InputMethod(name=name, framework="fcitx5",
                       composing=state == "2" and not name.startswith("keyboard-"))


async def _running(process: str) -> bool:
    """Whether this user runs ``process`` (by exact name)."""
    return bool(await _output("pgrep", "-x", "-u", str(os.getuid()), process))


async def _output(*cmd: str) -> str:
    """A command's stdout, stripped; "" when it fails or takes over a second."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
    except TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace").strip() if proc.returncode == 0 else ""


def is_bare_key(keys: list[str]) -> bool:
    """Whether pressing ``keys`` together types a character an input method
    would compose: one printable character, with Shift at most."""
    *mods, last = keys or [""]
    return (len(last) == 1 and last.isprintable() and not last.isspace()
            and all(m.lower() in ("shift", "shift_l", "shift_r") for m in mods))
