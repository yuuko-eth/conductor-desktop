"""Input via Quartz CGEvent (macOS)."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from ...errors import BackendError
from ..base import _exc_str
from . import mac_layout
from .base import LocalInputBackend, _utf16_units

log = logging.getLogger(__name__)


class QuartzInput(LocalInputBackend):
    """Mouse/keyboard via Quartz CGEvent (macOS).

    Coordinates are in point space (matches what mss returns on macOS),
    so no Retina scaling is applied here.
    """

    async def check_access(self) -> tuple[bool, str]:
        """The TCC Accessibility grant, which CGEventPost needs."""
        # Probe the TCC Accessibility grant directly. Input injection here
        # goes through CGEventPost, which is gated by
        # kTCCServiceAccessibility — exactly what AXIsProcessTrusted() reports.
        #
        # Do NOT shell out to `osascript`/System Events: that tests the
        # *Automation* (Apple Events) service, a different TCC bucket, and
        # gives a false FAIL when a GUI host disclaims responsibility for the
        # spawned server (e.g. Claude Desktop) so TCC attributes the request
        # to this binary's own identity instead of the launcher's.
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
            # prompt=True both reports trust AND, when not yet trusted, shows the
            # grant dialog and registers conductor-desktop in the Accessibility list
            # so the user can just toggle it on. The System Settings "+" picker
            # is unreliable for our LSUIElement/path-launched bundle (silently
            # no-ops), so letting TCC populate the entry via its own prompt — the
            # same way the Screen Recording check does — is the robust path.
            if AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
                return True, "Accessibility permission granted"
            return False, (
                "Accessibility permission not granted. Approve the prompt that "
                "just appeared, or: System Settings → Privacy & Security → "
                "Accessibility and enable conductor-desktop (now listed there)."
            )
        except Exception as e:  # pyobjc missing or the TCC call itself failing
            log.debug("accessibility self-test failed", exc_info=True)
            return False, f"Accessibility check failed: {_exc_str(e)}"

    #: How long a key stays down, and the pause after it comes up. Apps that
    #: poll the keyboard's state rather than take its events (emulators such
    #: as ContrAlto's Alto, which looks once a frame, games, VMs) miss a key
    #: that's up again within a frame; 10 ms dropped most of them.
    _KEY_HOLD = 0.05
    _KEY_GAP = 0.03
    #: A mouse button stays down longer, about as long as a person's click:
    #: Smalltalk on the Alto missed 50 ms clicks and took 150 ms ones. Two
    #: still fit in macOS's double-click interval.
    _CLICK_HOLD = 0.15

    # macOS virtual keycodes for named keys.
    _KEYCODES: ClassVar[dict[str, int]] = {
        "return": 0x24, "enter": 0x24,
        "tab": 0x30,
        "space": 0x31, " ": 0x31,
        "delete": 0x33, "backspace": 0x33,
        "escape": 0x35, "esc": 0x35,
        "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
        "home": 0x73, "end": 0x77,
        "pageup": 0x74, "pagedown": 0x79,
        "forwarddelete": 0x75,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
        "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
        "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    }

    _MODIFIERS: ClassVar[dict[str, int]] = {
        "cmd": 1 << 20, "command": 1 << 20, "meta": 1 << 20,
        "shift": 1 << 17,
        "alt": 1 << 19, "option": 1 << 19, "opt": 1 << 19,
        "ctrl": 1 << 18, "control": 1 << 18,
        "fn": 1 << 23,
    }

    # macOS virtual keycodes for the actual modifier keys. We press/release
    # these for real (not just CGEventSetFlags on a non-modifier event) so
    # the HID ambient flag state latches and clears cleanly — otherwise
    # Command can appear "sticky" on the event following a hotkey call.
    _MODIFIER_KEYCODES: ClassVar[dict[str, int]] = {
        "cmd": 0x37, "command": 0x37, "meta": 0x37,
        "shift": 0x38,
        "alt": 0x3A, "option": 0x3A, "opt": 0x3A,
        "ctrl": 0x3B, "control": 0x3B,
        "fn": 0x3F,
    }

    # Base keycodes for the character keys (standard US QWERTY): letters,
    # digits and unshifted punctuation. Hotkeys like cmd+c need the key itself,
    # and typed characters carry it too (see _stroke).
    _LETTER_KEYCODES: ClassVar[dict[str, int]] = {
        "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03,
        "g": 0x05, "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
        "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F,
        "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
        "y": 0x10, "z": 0x06,
        "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
        "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
        "-": 0x1B, "=": 0x18, "[": 0x21, "]": 0x1E, "\\": 0x2A, ";": 0x29,
        "'": 0x27, ",": 0x2B, ".": 0x2F, "/": 0x2C, "`": 0x32,
    }

    #: The US-layout character Shift turns each of these keys into.
    _SHIFTED: ClassVar[dict[str, str]] = dict(zip(
        '~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./", strict=True))

    def _stroke(self, ch: str, keys: mac_layout.Keys | None = None
                ) -> tuple[int, tuple[str, ...]] | None:
        """(keycode, modifiers to hold) for a character, else None.

        On the current layout when ``keys`` has it (mac_layout.current), else
        on a US keyboard. Typed characters carry their real key as well as
        their text: apps that read the key rather than the text (remote
        desktops, VMs, emulators, games) saw keycode 0, the A key, for every
        character.
        """
        if keys is not None and ch in keys:
            code, state = keys[ch]
            return code, (*(("shift",) if state & mac_layout.SHIFT else ()),
                          *(("option",) if state & mac_layout.OPTION else ()))
        if ch in self._LETTER_KEYCODES:
            return self._LETTER_KEYCODES[ch], ()
        if ch.isascii() and ch.isupper():
            return self._LETTER_KEYCODES[ch.lower()], ("shift",)
        if ch in self._SHIFTED:
            return self._LETTER_KEYCODES[self._SHIFTED[ch]], ("shift",)
        if ch == " ":
            return self._KEYCODES["space"], ()
        return None

    @staticmethod
    def _layout_keys() -> mac_layout.Keys | None:
        """The current layout's keys; None keeps the US key codes.

        US key codes are positions: on German, US z's key types y, and cmd+z
        by code was cmd+y there, so nothing was undone.
        """
        found = mac_layout.current()
        return found[1] if found is not None else None

    def _button_events(self, button: str):
        from Quartz import (
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGEventOtherMouseDown,
            kCGEventOtherMouseUp,
            kCGEventRightMouseDown,
            kCGEventRightMouseUp,
            kCGMouseButtonCenter,
            kCGMouseButtonLeft,
            kCGMouseButtonRight,
        )
        if button == "right":
            return kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        if button == "middle":
            return kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter
        return kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft

    def _lookup_keycode(self, name: str, keys: mac_layout.Keys | None = None) -> int | None:
        """A named key's code, or the key that types a one-character name
        on the current layout (US when ``keys`` lacks it: on Russian, cmd+z
        is at the US z, where apps look for it then)."""
        k = name.lower()
        if k in self._KEYCODES:
            return self._KEYCODES[k]
        # Only a character on a key's plain level: "?" needs Shift, which
        # key_press gets from _type_char.
        if keys is not None and len(k) == 1 and k in keys and not keys[k][1]:
            return keys[k][0]
        if k in self._LETTER_KEYCODES:
            return self._LETTER_KEYCODES[k]
        return None

    def _event_source(self):
        # A private CGEventSource has isolated flag state — it does NOT
        # combine with the HID system state. Without this, events created
        # with source=None inherit the HID flag state, so a `cmd` flag on a
        # recent hotkey event can leak into the next plain key_press (Enter
        # becomes cmd+Enter, click becomes cmd+click, etc.). Creating every
        # event from our private source prevents that.
        src = getattr(self, "_source", None)
        if src is None:
            from Quartz import CGEventSourceCreate, kCGEventSourceStatePrivate
            src = CGEventSourceCreate(kCGEventSourceStatePrivate)
            self._source = src
        return src

    async def click(self, x, y, button="left", clicks=1):
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            CGEventSetIntegerValueField,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseEventClickState,
        )
        down_t, up_t, btn = self._button_events(button)
        pt = (float(x), float(y))
        src = self._event_source()
        # Move first so apps that care see the cursor arrive
        move = CGEventCreateMouseEvent(src, kCGEventMouseMoved, pt, btn)
        CGEventPost(kCGHIDEventTap, move)
        await asyncio.sleep(self._KEY_GAP)
        for i in range(max(1, clicks)):
            down = CGEventCreateMouseEvent(src, down_t, pt, btn)
            up = CGEventCreateMouseEvent(src, up_t, pt, btn)
            # Click count field drives macOS double/triple-click semantics
            CGEventSetIntegerValueField(down, kCGMouseEventClickState, i + 1)
            CGEventSetIntegerValueField(up, kCGMouseEventClickState, i + 1)
            CGEventPost(kCGHIDEventTap, down)
            await asyncio.sleep(self._CLICK_HOLD)
            CGEventPost(kCGHIDEventTap, up)
            await asyncio.sleep(0.04)

    async def move_to(self, x, y):
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )
        evt = CGEventCreateMouseEvent(self._event_source(), kCGEventMouseMoved,
                                       (float(x), float(y)), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, evt)

    async def scroll(self, x, y, direction, amount=3):
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventCreateScrollWheelEvent,
            CGEventPost,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
            kCGScrollEventUnitLine,
        )
        src = self._event_source()
        # Move first so the scroll lands under the intended region
        move = CGEventCreateMouseEvent(src, kCGEventMouseMoved,
                                        (float(x), float(y)), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, move)
        direction = direction.lower()
        # Vertical: positive = up (natural direction); Horizontal: positive = right
        if direction in ("up", "down"):
            dy = amount if direction == "up" else -amount
            evt = CGEventCreateScrollWheelEvent(src, kCGScrollEventUnitLine, 1, dy)
        else:
            dx = amount if direction == "right" else -amount
            evt = CGEventCreateScrollWheelEvent(src, kCGScrollEventUnitLine, 2, 0, dx)
        CGEventPost(kCGHIDEventTap, evt)

    def _post_left(self, event_type, x, y) -> None:
        """Post one left-button mouse event of ``event_type`` at (x, y)."""
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGMouseButtonLeft
        evt = CGEventCreateMouseEvent(self._event_source(), event_type,
                                      (float(x), float(y)), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, evt)

    async def _drag_press(self, x, y):
        from Quartz import kCGEventLeftMouseDown, kCGEventMouseMoved
        self._post_left(kCGEventMouseMoved, x, y)
        self._post_left(kCGEventLeftMouseDown, x, y)

    async def _drag_move(self, x, y):
        from Quartz import kCGEventLeftMouseDragged
        self._post_left(kCGEventLeftMouseDragged, x, y)

    async def _drag_release(self, x, y):
        from Quartz import kCGEventLeftMouseUp
        self._post_left(kCGEventLeftMouseUp, x, y)

    def _key_event(self, code: int, down: bool, flags: int = 0, char: str | None = None):
        """A keyboard event whose modifier flags are exactly ``flags``.

        Never leave the flags to the event source: an event created without
        them can carry flags from earlier events, and on macOS 26 a posted
        event's flags stay in the system's HID state. A plain arrow key left
        Fn + numpad there and cmd+up left Fn, after which every typed
        character arrived as Fn+character and was dropped.
        """
        from Quartz import CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString, CGEventSetFlags
        evt = CGEventCreateKeyboardEvent(self._event_source(), code, down)
        CGEventSetFlags(evt, flags)
        if char is not None:
            # Its length counts UTF-16 units: a character outside the BMP
            # (an emoji) is two, and passing 1 typed half of it.
            CGEventKeyboardSetUnicodeString(evt, len(_utf16_units(char)), char)
        return evt

    async def type_text(self, text):
        keys = self._layout_keys()
        for ch in text:
            if ch in "\r\n":
                await self._tap(self._KEYCODES["return"])
            elif ch == "\t":
                await self._tap(self._KEYCODES["tab"])
            else:
                await self._type_char(ch, keys)

    async def _tap(self, code: int, flags: int = 0, char: str | None = None) -> None:
        """One key down and up, held long enough for an app that polls."""
        from Quartz import CGEventPost, kCGHIDEventTap
        CGEventPost(kCGHIDEventTap, self._key_event(code, True, flags, char))
        await asyncio.sleep(self._KEY_HOLD)
        CGEventPost(kCGHIDEventTap, self._key_event(code, False, flags, char))
        await asyncio.sleep(self._KEY_GAP)

    async def _type_char(self, ch: str, keys: mac_layout.Keys | None = None) -> None:
        """A character as its key on the current layout (or a US keyboard),
        its modifiers pressed for real when it needs them; the text rides
        along, so apps that read text get ``ch`` on any layout. A character
        with no key goes as text on keycode 0."""
        stroke = self._stroke(ch, keys)
        if stroke is None:
            await self._tap(0, char=ch)
            return
        code, mods = stroke
        await self._with_modifiers(mods, code, char=ch)

    async def _with_modifiers(self, mods: tuple[str, ...], code: int,
                              char: str | None = None) -> None:
        """Tap ``code`` with ``mods`` held, each pressed and released for real."""
        from Quartz import CGEventPost, kCGHIDEventTap
        if not mods:
            await self._tap(code, char=char)
            return
        held = 0
        pressed = []
        try:
            for m in mods:
                held |= self._MODIFIERS[m]
                CGEventPost(kCGHIDEventTap, self._key_event(self._MODIFIER_KEYCODES[m], True, held))
                pressed.append(m)
                await asyncio.sleep(self._KEY_GAP)
            await self._tap(code, held, char=char)
        finally:
            for m in reversed(pressed):
                held &= ~self._MODIFIERS[m]
                CGEventPost(kCGHIDEventTap,
                            self._key_event(self._MODIFIER_KEYCODES[m], False, held))
                await asyncio.sleep(self._KEY_GAP)

    async def key_press(self, key):
        keys = self._layout_keys()
        code = self._lookup_keycode(key, keys)
        if code is not None:
            await self._tap(code)
        elif len(key) == 1:  # "?", "A": its key, with Shift when it takes it
            await self._type_char(key, keys)
        else:  # an unknown name goes as text
            await self._tap(0, char=key)

    async def hotkey(self, keys):
        from Quartz import CGEventPost, kCGHIDEventTap
        # Collect (keycode, flag_mask) for real modifier presses, and the
        # non-modifier targets. Pressing the real modifier keycodes (not
        # just setting flags on a non-modifier event) is what apps that track
        # modifier state see; every event still states its flags exactly, so
        # nothing is left held afterwards (see _key_event).
        mod_presses: list[tuple[int, int]] = []
        non_mod: list[str] = []
        combined_flags = 0
        for k in keys:
            lk = k.lower()
            if lk in self._MODIFIERS:
                kc = self._MODIFIER_KEYCODES.get(lk)
                if kc is None:
                    raise BackendError(f"no keycode mapped for modifier: {k!r}")
                mod_presses.append((kc, self._MODIFIERS[lk]))
                combined_flags |= self._MODIFIERS[lk]
            else:
                non_mod.append(k)
        if not non_mod:
            return
        layout = self._layout_keys()
        pressed: list[tuple[int, int]] = []
        held = 0
        try:
            # Press modifiers in order (left-to-right as given), each carrying
            # the modifiers held so far, as a keyboard would.
            for kc, mask in mod_presses:
                held |= mask
                CGEventPost(kCGHIDEventTap, self._key_event(kc, True, held))
                pressed.append((kc, mask))
                await asyncio.sleep(0.005)
            # Tap each non-modifier key with the combined modifier mask.
            for k in non_mod:
                code = self._lookup_keycode(k, layout)
                if code is None:
                    raise BackendError(f"unknown key in hotkey: {k!r}")
                await self._tap(code, combined_flags)
        finally:
            # Always release modifiers in reverse order — a raised exception
            # mid-sequence must not leave a modifier stuck held. Each release
            # carries only the modifiers still down after it.
            for kc, mask in reversed(pressed):
                held &= ~mask
                CGEventPost(kCGHIDEventTap, self._key_event(kc, False, held))
                await asyncio.sleep(0.005)
