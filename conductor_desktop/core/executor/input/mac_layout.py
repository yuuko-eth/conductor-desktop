"""Which macOS key code, with which modifiers, types a character on the
current keyboard layout.

The Quartz backend posted US key codes: on German, key z typed y, and cmd+z
pressed the key that is y there, so nothing was undone. UCKeyTranslate over
the current layout says what each key types; this turns that round. An
input method (Zhuyin, Kotoeri) types through the layout under it, which is
the one asked for here.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import logging

log = logging.getLogger(__name__)

# Carbon modifier state for UCKeyTranslate: (EventRecord modifiers >> 8).
SHIFT, OPTION = 0x02, 0x08
_STATES = (0, SHIFT, OPTION, SHIFT | OPTION)

# Keypad keys type digits and operators too; the main keys come first.
_KEYPAD = {0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51, 0x52, 0x53, 0x54, 0x55,
           0x56, 0x57, 0x58, 0x59, 0x5B, 0x5C}

# What a key types → (key code, Carbon modifier state).
Keys = dict[str, tuple[int, int]]


@functools.cache
def _frameworks():
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    vp = ctypes.c_void_p
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = vp
    carbon.TISGetInputSourceProperty.restype = vp
    carbon.TISGetInputSourceProperty.argtypes = [vp, vp]
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    carbon.UCKeyTranslate.restype = ctypes.c_int32
    carbon.UCKeyTranslate.argtypes = [
        vp, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16)]
    cf.CFDataGetBytePtr.restype = vp
    cf.CFDataGetBytePtr.argtypes = [vp]
    cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [vp]
    return cf, carbon


def current() -> tuple[str, Keys] | None:
    """The current layout's id and its keys, or None when macOS won't say
    (no Unicode layout data: then callers keep US key codes)."""
    try:
        cf, carbon = _frameworks()
    except OSError:
        return None
    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        return None
    try:
        sid = carbon.TISGetInputSourceProperty(
            source, ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID"))
        buf = ctypes.create_string_buffer(256)
        if not sid or not cf.CFStringGetCString(sid, buf, len(buf), 0x08000100):
            return None
        layout_id = buf.value.decode()
        data = carbon.TISGetInputSourceProperty(
            source, ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData"))
        if not data:
            return None
        return layout_id, _keys(layout_id, cf.CFDataGetBytePtr(data), carbon.LMGetKbdType())
    finally:
        cf.CFRelease(source)


@functools.lru_cache(maxsize=8)
def _keys(layout_id: str, layout_ptr: int, kbd_type: int) -> Keys:
    """Every character the layout types, and the simplest key for it.

    Cached by layout: the layout data stays put while the layout is current
    (the id is in the key so a switch doesn't reuse another layout's map).
    """
    _cf, carbon = _frameworks()
    found: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
    out = (ctypes.c_uint16 * 4)()
    length, dead = ctypes.c_ulong(), ctypes.c_uint32()
    for code in range(128):
        if code in _KEYPAD:
            continue
        for state in _STATES:
            dead.value = 0
            if carbon.UCKeyTranslate(layout_ptr, code, 0, state, kbd_type, 0,
                                     ctypes.byref(dead), len(out), ctypes.byref(length), out):
                continue
            if dead.value or length.value != 1:
                continue  # a dead key waits for the next one; skip it
            ch = chr(out[0])
            if not ch.isprintable():
                continue
            cost = (state.bit_count(), code)
            if ch not in found or cost < found[ch][0]:
                found[ch] = (cost, (code, state))
    return {ch: key for ch, (_cost, key) in found.items()}
