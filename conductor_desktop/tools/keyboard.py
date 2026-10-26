"""Keyboard tools — no grounding, acts on current focus."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..runtime.replies import action_reply
from ..state import current
from ..tooling import Content, tool


@tool(kind="acting")
async def type_text(text: Annotated[str, Field(description="The text to type.")],
                    keystrokes: Annotated[bool, Field(description=(
                        "true: press a key for every character, never paste: for "
                        "emulators, VMs, remote desktops and games, which don't "
                        "take a paste."))] = False,
                    ) -> list[Content]:
    """Type text into whatever has keyboard focus right now, so focus the field
    first (ui_focus, click, or focus_window for the app). Text of 8 or more
    characters, characters the input method can't type, or any text while a
    Linux input method such as Chewing or Pinyin is composing, is pasted
    through the clipboard and stays there, replacing what the user had
    copied, unless keystrokes=true. To replace a native field's whole value,
    ui_set_value is more reliable."""
    result = await current().executor.type_text(text, literal=keystrokes)
    return await action_reply(f"type({len(text)} chars)", result, observe=False)


@tool(kind="acting")
async def key(key: Annotated[str, Field(description=(
                  'One key name, case-insensitive: "enter", "escape", "tab", '
                  '"backspace", "delete", "up", "pagedown", "f5", ...'))]
              ) -> list[Content]:
    """Press one key in the focused window, e.g. enter to submit or escape to
    close a dialog. Key names are case-insensitive: enter, escape, tab,
    backspace, delete, space, up/down/left/right, home, end, pageup,
    pagedown, f1-f12. For a combination such as ctrl+c, use hotkey. A letter
    or digit goes to the input method like a real key (on Zhuyin, a is ㄇ), so
    enter text with type_text."""
    result = await current().executor.key_press(key)
    return await action_reply(f"key({key})", result, observe=False)


@tool(kind="acting")
async def hotkey(keys: Annotated[list[str], Field(description=(
                     'Modifiers then the key, e.g. ["ctrl", "c"] or '
                     '["cmd", "shift", "4"].'))]
                 ) -> list[Content]:
    """Press a key combination in the focused window: the modifiers are held
    while the last key is pressed. Modifiers are ctrl, shift, alt (option on
    macOS), cmd (macOS Command; Super on Linux) and win/super. Use the
    platform's own shortcut: cmd+c on macOS, ctrl+c on Windows and Linux."""
    result = await current().executor.hotkey(keys)
    return await action_reply(f"hotkey({'+'.join(keys)})", result, observe=False)
