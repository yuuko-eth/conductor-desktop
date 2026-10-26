"""The input backend interface: mouse and keyboard on the resident host."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


def _utf16_units(ch: str) -> list[int]:
    """``ch`` as UTF-16 code units: one for the BMP, a surrogate pair above it."""
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


class LocalInputBackend(ABC):
    async def check_access(self) -> tuple[bool, str]:
        """Whether input can be injected here, and if not, how to fix it.
        Runs once on connect."""
        return True, "not checked on this platform"

    @abstractmethod
    async def click(self, x: int, y: int, button: str, clicks: int) -> None: ...
    @abstractmethod
    async def move_to(self, x: int, y: int) -> None: ...
    @abstractmethod
    async def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...

    async def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 20, hold_ms: int = 300) -> None:
        """Press at (x1, y1), hold ``hold_ms`` so the window manager sees a
        drag start, move to (x2, y2) in ``steps`` steps, pause over the drop
        target and release. The button is released even when a step fails."""
        await self._drag_press(x1, y1)
        x, y = x1, y1
        try:
            await asyncio.sleep(hold_ms / 1000)
            for i in range(1, steps + 1):
                x, y = x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps
                await self._drag_move(x, y)
                await asyncio.sleep(0.015)
            await asyncio.sleep(0.2)
        finally:
            await self._drag_release(x, y)

    @abstractmethod
    async def _drag_press(self, x: int, y: int) -> None:
        """Move to (x, y) and press the left button."""
    @abstractmethod
    async def _drag_move(self, x: int, y: int) -> None:
        """Move to (x, y) with the left button held."""
    @abstractmethod
    async def _drag_release(self, x: int, y: int) -> None:
        """Release the left button at (x, y)."""

    async def can_type(self, text: str) -> bool:
        """Whether ``type_text`` can produce every character of ``text``.
        The executor pastes what it can't."""
        return True

    @abstractmethod
    async def type_text(self, text: str) -> None: ...
    @abstractmethod
    async def key_press(self, key: str) -> None: ...
    @abstractmethod
    async def hotkey(self, keys: list[str]) -> None: ...
