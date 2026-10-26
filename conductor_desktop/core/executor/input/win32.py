"""Input via SendInput (Windows)."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from ...errors import BackendError
from .base import LocalInputBackend, _utf16_units

log = logging.getLogger(__name__)


class Win32Input(LocalInputBackend):
    """Mouse/keyboard via ctypes SendInput (Windows).

    Uses the Win32 SendInput API for input injection.
    Requires same-or-higher integrity level as target (UIPI).
    """

    async def check_access(self) -> tuple[bool, str]:
        # UIPI can't be tested ahead of time; a refused SendInput reports then.
        return True, "Assumed OK (UIPI failures caught at runtime)"

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes

        self._user32 = ctypes.windll.user32

        # Make this process DPI-aware so GetSystemMetrics returns physical
        # pixels (not DPI-virtualized ones). Without this, on a 1920x1080
        # display at 150% scaling we'd get sw=1280 from SM_CXSCREEN, and
        # `x * 65535 / 1280` then gets mapped onto the physical 1920x1080
        # surface by SendInput → cursor lands at 1.5× the intended pixel.
        # Try PER_MONITOR_AWARE_V2 first (Win10 1703+); fall back to the
        # process-wide DPI-aware call (Win7+).
        try:
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                self._user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                log.debug("could not make the process DPI aware", exc_info=True)

        # ── SendInput structure definitions (Win32 API) ──

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.wintypes.LONG),
                ("dy", ctypes.wintypes.LONG),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.wintypes.WORD),
                ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUT_UNION)]

        self._INPUT = INPUT
        self._MOUSEINPUT = MOUSEINPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._INPUT_UNION = _INPUT_UNION

        # Input type constants
        self._INPUT_MOUSE = 0
        self._INPUT_KEYBOARD = 1

        # Mouse event flags
        self._MOUSEEVENTF_MOVE = 0x0001
        self._MOUSEEVENTF_LEFTDOWN = 0x0002
        self._MOUSEEVENTF_LEFTUP = 0x0004
        self._MOUSEEVENTF_RIGHTDOWN = 0x0008
        self._MOUSEEVENTF_RIGHTUP = 0x0010
        self._MOUSEEVENTF_MIDDLEDOWN = 0x0020
        self._MOUSEEVENTF_MIDDLEUP = 0x0040
        self._MOUSEEVENTF_WHEEL = 0x0800
        # ABSOLUTE: interpret dx/dy as normalized 0-65535 instead of relative.
        # VIRTUALDESK: combined with ABSOLUTE, makes those normalized coords
        # span the *virtual* desktop (all monitors). Without VIRTUALDESK,
        # coords map to the primary monitor only and any pointer move on a
        # secondary monitor lands at the wrong pixel — visible most clearly
        # with `mouse_move` since click side-effects can hit windows by
        # absolute coord even when the visible cursor is misplaced.
        # Always OR both at the callsite so intent is explicit.
        self._MOUSEEVENTF_ABSOLUTE = 0x8000
        self._MOUSEEVENTF_VIRTUALDESK = 0x4000
        # Absolute coordinates across the whole virtual desktop — every
        # move/click below is expressed this way.
        self._ABS_DESK = self._MOUSEEVENTF_ABSOLUTE | self._MOUSEEVENTF_VIRTUALDESK

        # Keyboard event flags
        self._KEYEVENTF_KEYUP = 0x0002
        self._KEYEVENTF_UNICODE = 0x0004

        # Screen metrics for absolute coordinate conversion. Use the
        # virtual screen (covers all monitors) since we always pair
        # SendInput with VIRTUALDESK above.
        self._SM_CXSCREEN = 0
        self._SM_CYSCREEN = 1
        self._SM_XVIRTUALSCREEN = 76
        self._SM_YVIRTUALSCREEN = 77
        self._SM_CXVIRTUALSCREEN = 78
        self._SM_CYVIRTUALSCREEN = 79

        # Common virtual key codes
        self._VK_MAP: dict[str, int] = {
            "return": 0x0D, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
            "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
            "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
            "f11": 0x7A, "f12": 0x7B,
            "shift": 0x10, "shift_l": 0x10, "shift_r": 0xA1,
            "control": 0x11, "control_l": 0x11, "control_r": 0xA3,
            "ctrl": 0x11, "ctrl_l": 0x11, "ctrl_r": 0xA3,
            "alt": 0x12, "alt_l": 0x12, "alt_r": 0xA5,
            "win": 0x5B, "super": 0x5B, "super_l": 0x5B, "super_r": 0x5C,
            "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
            "printscreen": 0x2C, "pause": 0x13,
        }

    def _send_inputs(self, *inputs) -> None:
        """Call SendInput with one or more INPUT structures. SendInput returns
        how many it inserted; fewer means input is blocked (UIPI against a
        higher-integrity window, or the secure desktop)."""
        import ctypes
        n = len(inputs)
        arr = (self._INPUT * n)(*inputs)
        sent = self._user32.SendInput(n, arr, ctypes.sizeof(self._INPUT))
        if sent != n:
            raise BackendError(
                f"SendInput inserted {sent} of {n} events: input is blocked "
                "(an elevated window has focus, or the desktop is locked)")

    def _pixel_to_absolute(self, x: int, y: int) -> tuple[int, int]:
        """Convert pixel coords (in the virtual desktop coord system) to
        normalized 0-65535 SendInput coords across the virtual desktop.

        Pairs with `_MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK` at
        the SendInput callsite. The virtual screen origin can be negative
        (e.g. a monitor positioned to the left of primary), so we
        translate by (vx, vy) before normalizing.

        Mapping uses (size - 1) as the denominator and round-to-nearest.
        With the more common (size) form + truncation, the rightmost /
        bottommost pixel column is unreachable: on a 1920-wide monitor
        pixel 1919 maps to ~65501 which round-trips back to pixel 1918.
        The clamp guards against out-of-bounds inputs from
        `mouse_move_at` reaching SendInput, which has undocumented
        behavior outside [0, 65535].
        """
        get = self._user32.GetSystemMetrics
        vx = get(self._SM_XVIRTUALSCREEN)
        vy = get(self._SM_YVIRTUALSCREEN)
        vw = max(get(self._SM_CXVIRTUALSCREEN) - 1, 1)
        vh = max(get(self._SM_CYVIRTUALSCREEN) - 1, 1)
        ax = max(0, min(65535, round((x - vx) * 65535 / vw)))
        ay = max(0, min(65535, round((y - vy) * 65535 / vh)))
        return ax, ay

    def _mouse_input(self, dx: int, dy: int, flags: int, data: int = 0):
        """Build a mouse INPUT structure."""
        import ctypes
        mi = self._MOUSEINPUT(dx, dy, data, flags, 0, ctypes.pointer(ctypes.c_ulong(0)))
        inp = self._INPUT()
        inp.type = self._INPUT_MOUSE
        inp.union.mi = mi
        return inp

    def _key_input(self, vk: int = 0, scan: int = 0, flags: int = 0):
        """Build a keyboard INPUT structure."""
        import ctypes
        ki = self._KEYBDINPUT(vk, scan, flags, 0, ctypes.pointer(ctypes.c_ulong(0)))
        inp = self._INPUT()
        inp.type = self._INPUT_KEYBOARD
        inp.union.ki = ki
        return inp

    def _resolve_vk(self, key: str) -> int:
        """Map key name to Win32 virtual key code."""
        k = key.lower().replace(" ", "")
        if k in self._VK_MAP:
            return self._VK_MAP[k]
        # Single character — use VkKeyScan to get its VK code
        if len(key) == 1:
            vk = self._user32.VkKeyScanW(ord(key)) & 0xFF
            if vk != 0xFF:
                return vk
        return 0

    async def click(self, x, y, button="left", clicks=1):
        ax, ay = self._pixel_to_absolute(x, y)
        # Button flag pairs: (down, up)
        btn = {
            "left": (self._MOUSEEVENTF_LEFTDOWN, self._MOUSEEVENTF_LEFTUP),
            "right": (self._MOUSEEVENTF_RIGHTDOWN, self._MOUSEEVENTF_RIGHTUP),
            "middle": (self._MOUSEEVENTF_MIDDLEDOWN, self._MOUSEEVENTF_MIDDLEUP),
        }.get(button, (self._MOUSEEVENTF_LEFTDOWN, self._MOUSEEVENTF_LEFTUP))
        # Move to position
        move = self._mouse_input(ax, ay, self._MOUSEEVENTF_MOVE | self._ABS_DESK)
        self._send_inputs(move)
        await asyncio.sleep(0.01)
        for i in range(clicks):
            if i > 0:
                # Inter-click gap in human-cadence range. <50ms can drop below
                # Chromium/Electron's noise floor for double-click recognition.
                await asyncio.sleep(0.06)
            down = self._mouse_input(ax, ay, btn[0] | self._MOUSEEVENTF_ABSOLUTE | self._MOUSEEVENTF_VIRTUALDESK)
            up = self._mouse_input(ax, ay, btn[1] | self._MOUSEEVENTF_ABSOLUTE | self._MOUSEEVENTF_VIRTUALDESK)
            # Split down/up so dwell is non-zero. SendInput-batched down+up
            # arrives with 0µs dwell, which some apps reject as malformed.
            self._send_inputs(down)
            await asyncio.sleep(0.02)
            self._send_inputs(up)

    async def move_to(self, x, y):
        ax, ay = self._pixel_to_absolute(x, y)
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_MOVE | self._ABS_DESK))

    # (MOUSEEVENTF_WHEEL or MOUSEEVENTF_HWHEEL, sign of mouseData)
    _WHEEL: ClassVar[dict[str, tuple[int, int]]] = {
        "up": (0x0800, 1), "down": (0x0800, -1), "left": (0x1000, -1), "right": (0x1000, 1),
    }

    async def scroll(self, x, y, direction, amount=3):
        if direction not in self._WHEEL:
            raise BackendError(f"SendInput: unknown scroll direction {direction!r}")
        ax, ay = self._pixel_to_absolute(x, y)
        # Move mouse to scroll position first
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_MOVE | self._ABS_DESK))
        await asyncio.sleep(0.01)
        # WHEEL_DELTA is 120 per notch. WHEEL: positive scrolls up;
        # HWHEEL: positive scrolls right.
        flag, sign = self._WHEEL[direction]
        self._send_inputs(self._mouse_input(0, 0, flag, 120 * amount * sign))

    async def _drag_press(self, x, y):
        ax, ay = self._pixel_to_absolute(x, y)
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_MOVE | self._ABS_DESK))
        await asyncio.sleep(0.02)
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_LEFTDOWN | self._ABS_DESK))

    async def _drag_move(self, x, y):
        ax, ay = self._pixel_to_absolute(x, y)
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_MOVE | self._ABS_DESK))

    async def _drag_release(self, x, y):
        ax, ay = self._pixel_to_absolute(x, y)
        self._send_inputs(self._mouse_input(ax, ay, self._MOUSEEVENTF_LEFTUP | self._ABS_DESK))

    async def type_text(self, text):
        # KEYEVENTF_UNICODE sends arbitrary characters without VK mapping. Its
        # scan code is one UTF-16 unit, so a character outside the BMP goes
        # as its two surrogates, each a down/up pair, in order. Down and up go
        # out separately with a short dwell, as for clicks: sent in one batch,
        # the last character was sometimes lost (about one run in two on
        # Windows 11 Notepad).
        for ch in text:
            for unit in _utf16_units(ch):
                self._send_inputs(self._key_input(0, unit, self._KEYEVENTF_UNICODE))
                await asyncio.sleep(0.01)
                self._send_inputs(
                    self._key_input(0, unit, self._KEYEVENTF_UNICODE | self._KEYEVENTF_KEYUP))
            await asyncio.sleep(0.02)

    async def key_press(self, key):
        vk = self._resolve_vk(key)
        if vk == 0:
            raise BackendError(f"Unknown key: {key}")
        self._send_inputs(self._key_input(vk))
        await asyncio.sleep(0.01)
        self._send_inputs(self._key_input(vk, flags=self._KEYEVENTF_KEYUP))

    async def hotkey(self, keys):
        # Press all modifiers down, then release in reverse order
        vks = []
        for key in keys:
            vk = self._resolve_vk(key)
            if vk == 0:
                raise BackendError(f"Unknown key in hotkey: {key}")
            vks.append(vk)
        for vk in vks:
            self._send_inputs(self._key_input(vk))
            await asyncio.sleep(0.01)
        for vk in reversed(vks):
            self._send_inputs(self._key_input(vk, flags=self._KEYEVENTF_KEYUP))
            await asyncio.sleep(0.01)
