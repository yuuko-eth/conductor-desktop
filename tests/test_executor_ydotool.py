"""YdotoolInput argument building and the failures ydotool hides behind exit 0."""

from __future__ import annotations

import asyncio
import json

import pytest

from conductor_desktop.core.errors import BackendError
from conductor_desktop.core.executor import YdotoolInput
from conductor_desktop.core.executor.input import KwinYdotoolInput, WlrootsYdotoolInput, ydotool


async def test_text_starting_with_a_dash_is_not_an_option(monkeypatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(YdotoolInput, "_run", fake_run)
    await YdotoolInput().type_text("-a b")
    assert seen[0][-2:] == ["--", "-a b"]


class _Proc:
    def __init__(self, stderr: bytes, returncode: int = 0):
        self._stderr, self.returncode = stderr, returncode

    async def communicate(self):
        return b"", self._stderr


@pytest.mark.parametrize("stderr", [
    b"type: invalid option -- 'a'\ntype: invalid option -- ' '",  # ydotool 1.x, exit 0
    b"failed to connect socket `/tmp/.ydotool_socket': No such file or directory",
])
async def test_stderr_failures_with_exit_0_raise(monkeypatch, stderr) -> None:
    async def fake_exec(*args, **kwargs):
        return _Proc(stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(BackendError, match="ydotool: "):
        await YdotoolInput()._run(["ydotool", "type", "x"])


@pytest.fixture
def recorded(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []

    async def fake_run(self, args: list[str]) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(YdotoolInput, "_run", fake_run)
    return seen


@pytest.mark.parametrize(("direction", "wheel"), [
    # evdev: positive REL_WHEEL scrolls up, positive REL_HWHEEL right.
    ("up", ["0", "3"]), ("down", ["0", "-3"]),
    ("left", ["-3", "0"]), ("right", ["3", "0"]),
])
async def test_scroll_uses_evdev_wheel_signs(recorded, direction, wheel) -> None:
    await YdotoolInput().scroll(10, 20, direction, 3)
    assert recorded[-1] == ["ydotool", "mousemove", "--wheel", "--", *wheel]


async def test_unknown_scroll_direction_raises(recorded) -> None:
    with pytest.raises(BackendError, match="unknown scroll direction"):
        await YdotoolInput().scroll(10, 20, "sideways", 3)
    assert recorded == []


async def test_kwin_moves_to_the_corner_then_by_x_y_in_two_calls(recorded) -> None:
    await KwinYdotoolInput().click(640, 360)
    assert recorded == [
        ["ydotool", "mousemove", "--", "-100000", "-100000"],
        ["ydotool", "mousemove", "--", "640", "360"],
        ["ydotool", "click", "0xC0"],
    ]


@pytest.fixture
def kwin_bus(monkeypatch):
    """busctl against a fake KWin with two input devices; records set-property."""
    names = {"event0": "AT Translated Set 2 keyboard", "event21": "ydotoold virtual device"}
    sets: list[list[str]] = []

    async def fake_cli(cmd, timeout=5):
        if cmd[2] == "set-property":
            sets.append(cmd[4:])
            return ""
        path, prop = cmd[5], cmd[7]
        if prop == "devicesSysNames":
            return json.dumps({"type": "as", "data": list(names)})
        return json.dumps({"type": "s", "data": names[path.rsplit("/", 1)[1]]})

    monkeypatch.setattr(ydotool, "_run_cli", fake_cli)
    return sets


async def test_kwin_flattens_only_the_ydotool_device(kwin_bus) -> None:
    path = await ydotool._kwin_flatten_acceleration("ydotoold virtual device")
    assert path == "/org/kde/KWin/InputDevice/event21"
    assert kwin_bus == [
        [path, "org.kde.KWin.InputDevice", "pointerAccelerationProfileFlat", "b", "true"],
        [path, "org.kde.KWin.InputDevice", "pointerAcceleration", "d", "0"],
    ]


async def test_kwin_without_the_device_says_so(kwin_bus) -> None:
    with pytest.raises(BackendError, match="no input device named 'nope'"):
        await ydotool._kwin_flatten_acceleration("nope")
    assert kwin_bus == []


async def test_non_ascii_is_refused_not_dropped(recorded) -> None:
    with pytest.raises(BackendError, match="only type ASCII"):
        await YdotoolInput().type_text("é")
    assert recorded == []


@pytest.mark.parametrize(("env", "move"), [
    ({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}, ["hyprctl", "dispatch", "movecursor", "640", "360"]),
    ({"SWAYSOCK": "/run/user/1000/sway-ipc.sock"},
     ["swaymsg", "seat", "-", "cursor", "set", "640", "360"]),
])
async def test_wlroots_places_the_pointer_through_the_compositor(monkeypatch, recorded, env, move):
    for var in ("HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    ipc: list[list[str]] = []

    async def fake_cli(cmd, timeout=5):
        ipc.append(cmd)
        return "ok"

    monkeypatch.setattr(ydotool, "_run_cli", fake_cli)
    await WlrootsYdotoolInput().click(640, 360)
    assert ipc == [move]
    assert recorded == [["ydotool", "click", "0xC0"]]


# The warp leaves no window with pointer focus, and a wheel event after it
# went nowhere; a device motion after the warp (never before it: a warp
# drops the focus again) makes Hyprland pick the window. Clicks don't need it.
async def test_hyprland_scroll_moves_the_device_after_the_warp(monkeypatch, recorded):
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    calls: list[list[str]] = []

    async def fake_cli(cmd, timeout=5):
        calls.append(cmd)
        return "ok"

    async def fake_run(self, cmd):
        calls.append(cmd)

    monkeypatch.setattr(ydotool, "_run_cli", fake_cli)
    monkeypatch.setattr(WlrootsYdotoolInput, "_run", fake_run)
    await WlrootsYdotoolInput().scroll(640, 360, "down", 3)
    assert calls == [
        ["hyprctl", "dispatch", "movecursor", "640", "360"],
        ["ydotool", "mousemove", "-x", "1", "-y", "0"],
        ["ydotool", "mousemove", "-x", "-1", "-y", "0"],
        ["ydotool", "mousemove", "--wheel", "--", "0", "-3"],
    ]


async def test_wlroots_without_an_ipc_falls_back_to_ydotool(monkeypatch, recorded):
    for var in ("HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(var, raising=False)
    await WlrootsYdotoolInput().move_to(5, 6)
    assert recorded == [["ydotool", "mousemove", "--absolute", "-x", "5", "-y", "6"]]
