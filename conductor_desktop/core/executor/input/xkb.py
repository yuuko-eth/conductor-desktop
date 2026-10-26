"""Which evdev key, with which modifiers, types a character on an xkb layout.

Built with libxkbcommon (through ctypes), which every Wayland desktop has:
it compiles the layout the way the compositor does, and each key's levels
say what it types plain, with Shift, with AltGr, and with both.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import logging

log = logging.getLogger(__name__)

# evdev codes of the modifiers a level can need. AltGr is right Alt, which
# xkb's default lv3:ralt_switch makes the level-three shift.
SHIFT, ALTGR = 42, 100

# The keypad types digits and operators too, but only with Num Lock on.
_KEYPAD = {55, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 96, 98, 117, 121}

# xkb keycodes are evdev codes plus 8.
_EVDEV_OFFSET = 8

# What a key types → (evdev code, modifier evdev codes to hold).
Keys = dict[str, tuple[int, tuple[int, ...]]]


class _RuleNames(ctypes.Structure):
    _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                ("layout", ctypes.c_char_p), ("variant", ctypes.c_char_p),
                ("options", ctypes.c_char_p)]


@functools.cache
def _lib():
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    lib = ctypes.CDLL(name)
    vp, u32 = ctypes.c_void_p, ctypes.c_uint32
    for fn, res, args in (
            ("xkb_context_new", vp, [ctypes.c_int]),
            ("xkb_context_unref", None, [vp]),
            ("xkb_keymap_new_from_names", vp, [vp, ctypes.POINTER(_RuleNames), ctypes.c_int]),
            ("xkb_keymap_unref", None, [vp]),
            ("xkb_keymap_min_keycode", u32, [vp]),
            ("xkb_keymap_max_keycode", u32, [vp]),
            ("xkb_keymap_num_levels_for_key", u32, [vp, u32, u32]),
            ("xkb_keymap_key_get_syms_by_level", ctypes.c_int,
             [vp, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]),
            ("xkb_keymap_key_get_mods_for_level", ctypes.c_size_t,
             [vp, u32, u32, u32, ctypes.POINTER(u32), ctypes.c_size_t]),
            ("xkb_keymap_mod_get_index", u32, [vp, ctypes.c_char_p]),
            ("xkb_keysym_to_utf32", u32, [u32])):
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    return lib


@functools.lru_cache(maxsize=8)
def keys_for(layout: str, variant: str = "") -> Keys | None:
    """The layout's characters and the keys that type them, or None when
    libxkbcommon is missing or doesn't know the layout."""
    try:
        lib = _lib()
    except OSError:
        log.debug("libxkbcommon not found", exc_info=True)
        return None
    ctx = lib.xkb_context_new(0)
    if not ctx:
        return None
    names = _RuleNames(b"evdev", b"pc105", layout.encode(), variant.encode(), None)
    keymap = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
    try:
        if not keymap:
            log.debug("xkb doesn't know layout %r variant %r", layout, variant)
            return None
        return _read(lib, keymap)
    finally:
        if keymap:
            lib.xkb_keymap_unref(keymap)
        lib.xkb_context_unref(ctx)


def _read(lib, keymap) -> Keys:
    shift_bit = 1 << lib.xkb_keymap_mod_get_index(keymap, b"Shift")
    level3 = lib.xkb_keymap_mod_get_index(keymap, b"Mod5")
    altgr_bit = 1 << level3 if level3 < 32 else 0
    found: dict[str, tuple[int, int, tuple[int, ...]]] = {}  # ch → (cost, code, mods)
    masks = (ctypes.c_uint32 * 8)()
    syms = ctypes.POINTER(ctypes.c_uint32)()
    for kc in range(lib.xkb_keymap_min_keycode(keymap), lib.xkb_keymap_max_keycode(keymap) + 1):
        code = kc - _EVDEV_OFFSET
        if code in _KEYPAD:
            continue
        for level in range(lib.xkb_keymap_num_levels_for_key(keymap, kc, 0)):
            mods = _mods_for(lib, keymap, kc, level, masks, shift_bit, altgr_bit)
            if mods is None:
                continue
            for i in range(lib.xkb_keymap_key_get_syms_by_level(keymap, kc, 0, level,
                                                                ctypes.byref(syms))):
                cp = lib.xkb_keysym_to_utf32(syms[i])
                if not cp:  # a dead key, or a key that isn't a character
                    continue
                ch = chr(cp)
                if not ch.isprintable():
                    continue
                cost = (len(mods), code)
                if ch not in found or cost < found[ch][:2]:
                    found[ch] = (*cost, mods)  # type: ignore[assignment]
    return {ch: (code, mods) for ch, (_n, code, mods) in found.items()}


def _mods_for(lib, keymap, kc, level, masks, shift_bit, altgr_bit) -> tuple[int, ...] | None:
    """The modifiers to hold for ``level`` of key ``kc``: the simplest mask
    made only of Shift and AltGr, or None when every mask needs another
    (Caps Lock, Num Lock)."""
    n = lib.xkb_keymap_key_get_mods_for_level(keymap, kc, 0, level, masks, len(masks))
    if level == 0 and n == 0:
        return ()
    best = None
    for mask in masks[:n]:
        if mask & ~(shift_bit | altgr_bit):
            continue
        mods = (*((SHIFT,) if mask & shift_bit else ()), *((ALTGR,) if mask & altgr_bit else ()))
        if best is None or len(mods) < len(best):
            best = mods
    return best
