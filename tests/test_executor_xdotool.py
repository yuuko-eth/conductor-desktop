"""XdotoolInput key-name normalization.

Regression for the silent-Enter bug: `key('Enter')` was reaching xdotool
verbatim. xdotool prints "(symbol) No such key name 'Enter'. Ignoring it."
to stderr and exits 0, so the failure was invisible to the executor and
the model saw `key(Enter) — ok` while Chrome stayed on the New Tab page.
"""

from __future__ import annotations

import pytest

from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor import XdotoolInput


@pytest.mark.parametrize("name,expected", [
    ("Enter", "Return"),
    ("enter", "Return"),
    ("Return", "Return"),
    ("Esc", "Escape"),
    ("escape", "Escape"),
    ("backspace", "BackSpace"),
    ("Delete", "Delete"),
    ("PageUp", "Prior"),
    ("PageDown", "Next"),
    ("cmd", "Super_L"),
    ("win", "Super_L"),
    ("space", "space"),
    ("a", "a"),
    ("F5", "F5"),
    ("f2", "F2"),
    ("f12", "F12"),
])
def test_normalize_key(name: str, expected: str) -> None:
    assert XdotoolInput._normalize_key(name) == expected


async def test_key_press_translates_enter(monkeypatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(XdotoolInput, "_run", fake_run)
    await XdotoolInput().key_press("Enter")
    assert seen == [["xdotool", "key", "Return"]]


async def test_hotkey_translates_each_segment(monkeypatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(XdotoolInput, "_run", fake_run)
    await XdotoolInput().hotkey(["ctrl", "Enter"])
    assert seen == [["xdotool", "key", "ctrl+Return"]]


async def test_hotkey_takes_lowercase_function_keys(monkeypatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(XdotoolInput, "_run", fake_run)
    await XdotoolInput().hotkey(["alt", "f2"])
    assert seen == [["xdotool", "key", "alt+F2"]]


async def test_run_surfaces_unknown_keysym(monkeypatch) -> None:
    """xdotool exits 0 on bad keysyms; _run must raise so the executor
    reports failure instead of phantom success."""
    import asyncio

    class FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"(symbol) No such key name 'Enter'. Ignoring it."

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(BackendError, match="No such key name"):
        await XdotoolInput()._run(["xdotool", "key", "Enter"])


async def test_run_raises_on_nonzero_exit(monkeypatch) -> None:
    """A failed xdotool invocation (e.g. bad $DISPLAY, missing window)
    exits non-zero with stderr we don't pattern-match — the returncode
    check is the only thing standing between that failure and a phantom
    `mouse_move … ok` in the action summary."""
    import asyncio

    class FakeProc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"Can't open display: :99"

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(BackendError, match=r"xdotool exit 1.*display"):
        await XdotoolInput()._run(["xdotool", "mousemove", "100", "200"])


async def test_text_starting_with_a_dash_is_not_an_option(monkeypatch) -> None:
    # Without "--", xdotool fails with "unrecognized option '-a b'".
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(XdotoolInput, "_run", fake_run)
    await XdotoolInput().type_text("-a b")
    assert seen[0][-2:] == ["--", "-a b"]
