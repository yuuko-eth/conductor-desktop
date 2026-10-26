"""Input via xdotool (X11 on Linux and FreeBSD)."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from ... import keyboard_layout
from ...errors import BackendError
from ..base import _exc_str
from . import xkb
from .base import LocalInputBackend

log = logging.getLogger(__name__)


class XdotoolInput(LocalInputBackend):
    """Mouse/keyboard input via xdotool (Linux X11)."""

    async def check_access(self) -> tuple[bool, str]:
        """Whether xdotool is installed (X11 needs no grant)."""
        # xdotool — no permissions needed, just check it exists
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "getmouselocation",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            return True, "xdotool OK"
        except FileNotFoundError:
            return False, "xdotool not installed. Install with: sudo apt install xdotool"
        except (OSError, TimeoutError) as e:
            return False, f"xdotool probe failed: {_exc_str(e)}"

    # Cross-platform key names → X11 keysyms. The brain-facing convention
    # (and the names QuartzInput / Win32Input both accept) uses "enter",
    # "esc", "backspace", "pageup". xdotool wants "Return", "Escape",
    # "BackSpace", "Prior" — and silently no-ops with exit 0 on unknown
    # names, so the failure was invisible. Map at the boundary; pass
    # through anything we don't recognize so callers using native X11
    # keysyms (Return, Page_Up) still work.
    _KEYSYMS: ClassVar[dict[str, str]] = {
        "enter": "Return", "return": "Return",
        "esc": "Escape", "escape": "Escape",
        "backspace": "BackSpace", "back_space": "BackSpace",
        "del": "Delete", "delete": "Delete",
        "ins": "Insert", "insert": "Insert",
        "pageup": "Prior", "page_up": "Prior", "pgup": "Prior",
        "pagedown": "Next", "page_down": "Next", "pgdn": "Next",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End",
        "tab": "Tab", "space": "space",
        "win": "Super_L", "cmd": "Super_L", "command": "Super_L",
        "meta": "Super_L", "super": "Super_L",
        # xdotool knows only "F2", and the other backends take "f2".
        **{f"f{n}": f"F{n}" for n in range(1, 25)},
    }

    @classmethod
    def _normalize_key(cls, name: str) -> str:
        return cls._KEYSYMS.get(name.lower(), name)

    # Hard cap so a hung xdotool (seized X server, frozen WM) can't wedge
    # the tool call. Primitives normally complete in <50ms.
    _TIMEOUT = 10.0

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
                f"xdotool: timed out after {self._TIMEOUT}s ({' '.join(args)})"
            )
        err = stderr.decode().strip()
        # xdotool prints "(symbol) No such key name 'X'. Ignoring it." to
        # stderr and exits 0 on unknown keysyms — the silent-failure mode
        # that masked the Enter bug. Surface it so the executor reports
        # ActionResult(success=False) instead of phantom success.
        if err and "No such key name" in err:
            raise BackendError(f"xdotool: {err}")
        # Non-zero exit → real failure (bad $DISPLAY, missing window, OOM).
        # Without this the caller sees ActionResult(success=True) for a
        # mouse_move that never actually happened.
        if proc.returncode != 0:
            raise BackendError(
                f"xdotool exit {proc.returncode}: {err or '(no stderr)'}"
            )
        return stdout.decode().strip()

    async def click(self, x, y, button="left", clicks=1):
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        await self._run(["xdotool", "mousemove", str(x), str(y)])
        for _ in range(clicks):
            await self._run(["xdotool", "click", btn])

    async def move_to(self, x, y):
        await self._run(["xdotool", "mousemove", str(x), str(y)])

    async def scroll(self, x, y, direction, amount=3):
        await self._run(["xdotool", "mousemove", str(x), str(y)])
        btn = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(direction, "5")
        for _ in range(amount):
            await self._run(["xdotool", "click", btn])

    async def _drag_press(self, x, y):
        await self._run(["xdotool", "mousemove", str(x), str(y)])
        await self._run(["xdotool", "mousedown", "1"])

    async def _drag_move(self, x, y):
        await self._run(["xdotool", "mousemove", str(x), str(y)])

    async def _drag_release(self, x, y):
        await self._run(["xdotool", "mouseup", "1"])

    async def can_type(self, text):
        # A character the keymap lacks, xdotool types by mapping it onto a
        # spare keycode and restoring the map as soon as it's pressed; an app
        # that reads the key after the restore gets nothing (gedit on GNOME X11
        # lost a trailing 🎉 two runs in three). The executor pastes those.
        if text.isascii():
            return True
        layout = await keyboard_layout.active()
        keys = xkb.keys_for(layout.layout, layout.variant) if layout else None
        return keys is not None and all(ch.isascii() or ch in keys for ch in text)

    async def type_text(self, text):
        # "--": text starting with "-" is text, not an option.
        await self._run(["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text])

    async def key_press(self, key):
        await self._run(["xdotool", "key", self._normalize_key(key)])

    async def hotkey(self, keys):
        combo = "+".join(self._normalize_key(k) for k in keys)
        await self._run(["xdotool", "key", combo])
