"""Input via ydotool 1.x (Wayland)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import ClassVar

from ... import keyboard_layout
from ...backends._cli import run as _run_cli
from ...errors import BackendError
from ..base import _exc_str
from . import xkb
from .base import LocalInputBackend

log = logging.getLogger(__name__)


class YdotoolInput(LocalInputBackend):
    """Mouse/keyboard input via ydotool 1.x (Linux Wayland).

    Requires upstream ydotool ≥1.0 + ydotoold daemon + /dev/uinput access.
    Distro packages (Ubuntu/Debian ship 0.1.8) use a different syntax and
    will silently no-op every action — build from source.

    Click bitmask: low byte = button (0x00 L, 0x01 R, 0x02 M); 0x40 = down,
    0x80 = up; 0xC0 = full click. Plain 0x00 means "do nothing", a sleep.
    Key syntax in 1.x is raw Linux keycodes only (`<code>:<state>`), so
    we map name strings via _KEYCODES.
    """

    async def check_access(self) -> tuple[bool, str]:
        """Whether ydotool can reach /dev/uinput."""
        # Check ydotool / uinput access with a zero-length *relative* move.
        # An absolute move to (0, 0) parked the pointer in GNOME's hot corner
        # and opened Activities on every start.
        try:
            proc = await asyncio.create_subprocess_exec(
                "ydotool", "mousemove", "-x", "0", "-y", "0",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                stderr_text = stderr.decode().strip()
                if "uinput" in stderr_text.lower() or "permission" in stderr_text.lower():
                    return False, (
                        "ydotool cannot access /dev/uinput. Fix with:\n"
                        "  sudo usermod -aG input $USER  # then re-login\n"
                        "  # OR: sudo chmod 0660 /dev/uinput"
                    )
                return False, f"ydotool failed: {stderr_text}"
            return True, "ydotool OK"
        except FileNotFoundError:
            return False, "ydotool not installed. Install with: sudo apt install ydotool"
        except (OSError, TimeoutError) as e:
            return False, f"ydotool probe failed: {_exc_str(e)}"

    # Linux input-event-codes (subset). Names mirror what the brain LLM
    # produces and what QuartzInput / Win32Input / XdotoolInput already
    # accept ("enter", "esc", "ctrl"…). See
    # /usr/include/linux/input-event-codes.h for the full list.
    _KEYCODES: ClassVar[dict[str, int]] = {
        # Modifiers — accept brain-facing names AND X11 keysym aliases
        # ("Control_L"/"Shift_L"/...) since LLMs trained on Linux GUI
        # scripting often produce those even on Wayland targets.
        "ctrl": 29, "control": 29, "leftctrl": 29,
        "control_l": 29, "ctrl_l": 29,
        "rightctrl": 97, "control_r": 97, "ctrl_r": 97,
        "shift": 42, "leftshift": 42, "shift_l": 42,
        "rightshift": 54, "shift_r": 54,
        "alt": 56, "leftalt": 56, "option": 56, "opt": 56,
        "alt_l": 56,
        "rightalt": 100, "altgr": 100, "alt_r": 100, "iso_level3_shift": 100,
        "super": 125, "meta": 125, "win": 125, "cmd": 125, "command": 125,
        "super_l": 125, "meta_l": 125, "hyper_l": 125,
        "rightsuper": 126, "rightmeta": 126,
        "super_r": 126, "meta_r": 126, "hyper_r": 126,
        # Editing / navigation
        "esc": 1, "escape": 1,
        "backspace": 14, "back_space": 14,
        "tab": 15,
        "enter": 28, "return": 28,
        "space": 57, " ": 57,
        "capslock": 58,
        "delete": 111, "del": 111,
        "insert": 110, "ins": 110,
        "home": 102, "end": 107,
        "pageup": 104, "page_up": 104, "pgup": 104,
        "pagedown": 109, "page_down": 109, "pgdn": 109,
        "up": 103, "down": 108, "left": 105, "right": 106,
        # Function keys
        "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
        "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
        # Number row (top of keyboard, NOT keypad)
        "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
        "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
        "minus": 12, "-": 12, "equal": 13, "=": 13,
        # Letters
        "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
        "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
        "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
        "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
        # Punctuation (US layout)
        "leftbrace": 26, "[": 26, "rightbrace": 27, "]": 27,
        "semicolon": 39, ";": 39, "apostrophe": 40, "'": 40,
        "grave": 41, "`": 41, "backslash": 43, "\\": 43,
        "comma": 51, ",": 51, "dot": 52, ".": 52, "slash": 53, "/": 53,
    }

    @classmethod
    def _to_keycode(cls, name: str) -> int:
        code = cls._KEYCODES.get(name.lower())
        if code is None:
            raise BackendError(
                f"ydotool: unknown key name {name!r} (no Linux keycode mapping)."
            )
        return code

    _TIMEOUT = 10.0

    _SILENT_FAILURES = ("failed to connect socket", "Unknown command",
                        "invalid option", "unrecognized option")

    async def _run(self, args: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise BackendError(  # noqa: B904 — message is complete; context is noise
                f"ydotool: timed out after {self._TIMEOUT}s ({' '.join(args)})"
            )
        err = stderr.decode().strip()
        # ydotoold connection failures and unknown commands or options print
        # to stderr and exit 0 — surface them so phantom success doesn't
        # reach callers.
        if err and any(m in err for m in self._SILENT_FAILURES):
            raise BackendError(f"ydotool: {err}")
        if proc.returncode != 0:
            raise BackendError(
                f"ydotool exit {proc.returncode}: {err or '(no stderr)'}"
            )
        return stdout.decode().strip()

    async def _move_to(self, x, y):
        await self._run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])

    async def click(self, x, y, button="left", clicks=1):
        btn = {"left": 0x00, "right": 0x01, "middle": 0x02}.get(button, 0x00)
        full_click = f"0x{0xC0 | btn:02X}"
        await self._move_to(x, y)
        for _ in range(clicks):
            await self._run(["ydotool", "click", full_click])

    async def move_to(self, x, y):
        await self._point_at(x, y)

    async def _point_at(self, x, y):
        """Where the pointer rests (a hover, a scroll), as opposed to where
        it presses; the same move unless a compositor needs more."""
        await self._move_to(x, y)

    # ydotool passes wheel values through as raw evdev units: a positive
    # REL_WHEEL scrolls up and a positive REL_HWHEEL scrolls right (checked in
    # kate on KWin: +3 scrolled up).
    _WHEEL: ClassVar[dict[str, tuple[int, int]]] = {
        "up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
    }

    async def scroll(self, x, y, direction, amount=3):
        if direction not in self._WHEEL:
            raise BackendError(f"ydotool: unknown scroll direction {direction!r}")
        dx, dy = self._WHEEL[direction]
        await self._point_at(x, y)
        await self._run(["ydotool", "mousemove", "--wheel", "--",
                         str(dx * amount), str(dy * amount)])

    async def _drag_press(self, x, y):
        await self._move_to(x, y)
        await self._run(["ydotool", "click", "0x40"])  # left button down (no up)

    async def _drag_move(self, x, y):
        await self._move_to(x, y)

    async def _drag_release(self, x, y):
        await self._run(["ydotool", "click", "0x80"])  # left button up

    # Keys a character list can't hold: what type_text presses for them.
    _TEXT_KEYS: ClassVar[dict[str, tuple[int, tuple[int, ...]]]] = {
        "\n": (28, ()), "\t": (15, ())}

    async def _layout_keys(self) -> xkb.Keys | None:
        """The active layout's keys, or None to keep US positions (the
        layout is US, or it couldn't be told or compiled).

        ydotool sends evdev codes, which are positions: on German, US z's
        key types y, and ctrl+z by position is ctrl+y, redo.
        """
        layout = await keyboard_layout.active()
        if layout is None or layout.is_us:
            return None
        return xkb.keys_for(layout.layout, layout.variant)

    async def can_type(self, text):
        keys = await self._layout_keys()
        if keys is None:
            # ydotool types through a US keymap and silently drops the rest.
            return text.isascii()
        return all(ch in keys or ch in self._TEXT_KEYS for ch in text)

    async def type_text(self, text):
        keys = await self._layout_keys()
        if keys is None:
            if not text.isascii():
                raise BackendError("ydotool can only type ASCII; "
                                   "leave literal off to paste this text instead")
            # "--": without it, text starting with "-" is an unknown option,
            # which ydotool reports on stderr and still exits 0.
            await self._run(["ydotool", "type", "--key-delay", "20", "--", text])
            return
        events: list[str] = []
        for ch in text:
            stroke = keys.get(ch) or self._TEXT_KEYS.get(ch)
            if stroke is None:
                raise BackendError(f"ydotool: {ch!r} isn't on the active keyboard layout; "
                                   "leave literal off to paste this text instead")
            events += self._press(*stroke)
        await self._run(["ydotool", "key", "--key-delay", "20", *events])

    @staticmethod
    def _press(code: int, mods: tuple[int, ...] = ()) -> list[str]:
        return ([f"{m}:1" for m in mods] + [f"{code}:1", f"{code}:0"]
                + [f"{m}:0" for m in reversed(mods)])

    async def _key(self, name: str, keys: xkb.Keys | None) -> tuple[int, tuple[int, ...]]:
        """A key name's code, and the modifiers its character needs on the
        active layout. A character the layout lacks (z on Russian) keeps
        its US position, where apps look for shortcuts on such layouts."""
        if keys is not None and len(name) == 1 and name in keys:
            return keys[name]
        return self._to_keycode(name), ()

    async def key_press(self, key):
        code, mods = await self._key(key, await self._layout_keys())
        await self._run(["ydotool", "key", *self._press(code, mods)])

    async def hotkey(self, keys):
        # Press modifiers and the trigger key in order, then release in
        # reverse — the canonical chord shape for raw evdev events. A
        # character's own modifiers are left out: in ctrl+z, z is the key.
        layout = await self._layout_keys()
        codes = [(await self._key(k, layout))[0] for k in keys]
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        await self._run(["ydotool", "key", *seq])


class KwinYdotoolInput(YdotoolInput):
    """ydotool under KWin (KDE Plasma Wayland).

    ydotool's ``--absolute`` moves to the corner and then by (x, y) in one
    call, and KWin drops the second move: the pointer stays at (0, 0). The
    relative move is also accelerated. So a move here is two calls, and
    ``check_access`` sets the ydotoold device's acceleration to flat through
    KWin's D-Bus device settings, which touches only that virtual device.
    """

    _DEVICE_NAME = "ydotoold virtual device"

    async def _move_to(self, x, y):
        await self._run(["ydotool", "mousemove", "--", "-100000", "-100000"])
        await self._run(["ydotool", "mousemove", "--", str(x), str(y)])

    async def check_access(self) -> tuple[bool, str]:
        ok, detail = await super().check_access()
        if not ok:
            return ok, detail
        try:
            device = await _kwin_flatten_acceleration(self._DEVICE_NAME)
        except BackendError as e:
            return True, f"{detail}; pointer moves may miss their target: {e}"
        return True, f"{detail}; flat pointer acceleration on {device}"


class WlrootsYdotoolInput(YdotoolInput):
    """ydotool on wlroots compositors, with the pointer placed by the
    compositor's IPC where it has one.

    ydotool's absolute moves are relative moves through libinput's pointer
    acceleration, and miss: on Sway a move to x=620 landed past x=640, on
    Hyprland a move to x=1500 landed left of one to x=700. ``swaymsg seat -
    cursor set`` and ``hyprctl dispatch movecursor`` land on the pixel. Other
    wlroots compositors (labwc, river, ...) have no such call and keep
    ydotool's move.

    Hyprland's warp moves the pointer without giving the window under it
    pointer focus, so a wheel event after it went nowhere (the scrollbar
    flashed, the text stayed) and a hover wouldn't register. A device
    motion makes Hyprland re-pick the focus, and it has to come last: a warp
    after it drops the focus again. One pixel out and back can end a pixel
    off, so only scroll and hover take it; clicks land on the warp alone.
    """

    async def _move_to(self, x, y):
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            await self._hypr_warp(x, y)
        elif os.environ.get("SWAYSOCK"):
            await _run_cli(["swaymsg", "seat", "-", "cursor", "set", str(x), str(y)])
        else:
            await super()._move_to(x, y)

    async def _point_at(self, x, y):
        """Move, and on Hyprland give the window under the pointer focus."""
        await self._move_to(x, y)
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            await self._run(["ydotool", "mousemove", "-x", "1", "-y", "0"])
            await self._run(["ydotool", "mousemove", "-x", "-1", "-y", "0"])

    @staticmethod
    async def _hypr_warp(x, y):
        out = await _run_cli(["hyprctl", "dispatch", "movecursor", str(x), str(y)])
        if out.strip() != "ok":
            raise BackendError(f"hyprctl movecursor: {out.strip()}")


_KWIN_DEVICE = "org.kde.KWin.InputDevice"


async def _kwin_get(path: str, interface: str, prop: str):
    out = await _run_cli(["busctl", "--user", "--json=short", "get-property",
                          "org.kde.KWin", path, interface, prop])
    try:
        return json.loads(out)["data"]
    except (ValueError, KeyError, TypeError):
        raise BackendError(f"KWin returned an unexpected reply for {prop}: {out[:200]!r}") from None


async def _kwin_flatten_acceleration(name: str) -> str:
    """Flat pointer acceleration at speed 0 on the KWin input device called
    ``name``; its D-Bus path. Raises BackendError when KWin has no such device."""
    sys_names = await _kwin_get("/org/kde/KWin/InputDevice", "org.kde.KWin.InputDeviceManager",
                                "devicesSysNames")
    for sys_name in sys_names:
        path = f"/org/kde/KWin/InputDevice/{sys_name}"
        if await _kwin_get(path, _KWIN_DEVICE, "name") != name:
            continue
        for prop, sig, value in (("pointerAccelerationProfileFlat", "b", "true"),
                                 ("pointerAcceleration", "d", "0")):
            await _run_cli(["busctl", "--user", "set-property", "org.kde.KWin", path,
                            _KWIN_DEVICE, prop, sig, value])
        return path
    raise BackendError(f"KWin lists no input device named {name!r} (is ydotoold running?)")
