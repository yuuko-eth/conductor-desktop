"""Which platform a Linux session is routed to, and the KDE clipboard path."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conductor_desktop.core import platform_detect as pd
from conductor_desktop.core.platform_detect import Platform

_SESSION_VARS = ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP",
                 "SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE")


@pytest.fixture
def session(monkeypatch):
    """Set a Linux session's environment; ``wlrctl=`` says whether it's installed."""
    def set_session(wlrctl: bool = False, **env: str) -> None:
        for var in _SESSION_VARS:
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        monkeypatch.setattr(pd, "_has_command", lambda name: wlrctl and name == "wlrctl")
    return set_session


@pytest.mark.parametrize(("env", "wlrctl", "expected"), [
    ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"},
     False, Platform.LINUX_X11),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
     True, Platform.LINUX_GNOME),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"},
     False, Platform.LINUX_KDE),
    # wlrctl installed on a Plasma machine used to route KWin to wlroots.
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"},
     True, Platform.LINUX_KDE),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "plasma"},
     False, Platform.LINUX_KDE),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "sway"},
     False, Platform.LINUX_WLROOTS),
    ({"XDG_SESSION_TYPE": "wayland", "HYPRLAND_INSTANCE_SIGNATURE": "abc"},
     False, Platform.LINUX_WLROOTS),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "labwc:wlroots"},
     True, Platform.LINUX_WLROOTS),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "labwc:wlroots"},
     False, Platform.LINUX_GNOME),
    # Unity/Ubuntu read as GNOME for the route too, as they do for icons...
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "Unity"},
     True, Platform.LINUX_GNOME),
    # ...but a remix's own compositor name wins over the "ubuntu" prefix.
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:sway"},
     False, Platform.LINUX_WLROOTS),
    ({}, False, Platform.LINUX_X11),
])
def test_linux_session_routing(session, env, wlrctl, expected):
    session(wlrctl=wlrctl, **env)
    assert pd._detect_linux_display().platform == expected


def test_the_wayland_fallback_says_to_install_wlrctl(session):
    session(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="labwc:wlroots")
    found = pd._detect_linux_display()
    assert found.platform == Platform.LINUX_GNOME
    assert "XDG_CURRENT_DESKTOP=labwc:wlroots" in found.reason
    assert "install wlrctl" in found.reason


def test_the_reason_names_the_deciding_variable(session):
    session(XDG_SESSION_TYPE="wayland", HYPRLAND_INSTANCE_SIGNATURE="abc")
    assert pd._detect_linux_display().reason == (
        "XDG_SESSION_TYPE=wayland, HYPRLAND_INSTANCE_SIGNATURE set")


@pytest.mark.parametrize("override", ["linux-wlroots", " LINUX-WLROOTS "])
def test_an_override_skips_detection(session, override):
    session(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="KDE")
    found = pd.detect(override)
    assert found.platform == Platform.LINUX_WLROOTS
    assert found.reason == "CONDUCTOR_DESKTOP_PLATFORM=" + override.strip()


def test_an_unknown_override_is_ignored_with_a_warning(session, monkeypatch, caplog):
    session(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="KDE")
    monkeypatch.setattr(pd.platform, "system", lambda: "Linux")
    found = pd.detect("wayland")
    assert found.platform == Platform.LINUX_KDE
    assert "ignored CONDUCTOR_DESKTOP_PLATFORM='wayland'" in found.reason
    assert "names no platform" in caplog.text


def test_no_override_detects(session, monkeypatch):
    session(XDG_SESSION_TYPE="x11")
    monkeypatch.setattr(pd.platform, "system", lambda: "Linux")
    assert pd.detect("") == pd.Detection(Platform.LINUX_X11, "XDG_SESSION_TYPE=x11")


def test_the_icon_theme_reads_the_desktop_like_the_route(session, monkeypatch):
    session(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="ubuntu:sway")
    monkeypatch.setattr(pd.platform, "system", lambda: "Linux")
    pd.detect_icon_env.cache_clear()
    try:
        assert pd.detect_icon_env() == (None, None)  # sway, not GNOME's Yaru
    finally:
        pd.detect_icon_env.cache_clear()


@pytest.fixture
def kde(monkeypatch):
    """The linux_kde module, with busctl recorded in ``kde.calls``;
    ``kde.replies`` maps a Klipper method to its output or an exception."""
    from conductor_desktop.core.backends import linux_kde

    fake = SimpleNamespace(mod=linux_kde, calls=[], replies={})

    async def fake_run(cmd, timeout=5):
        fake.calls.append(cmd)
        method = "getClipboardContents" if "getClipboardContents" in cmd else "setClipboardContents"
        reply = fake.replies.get(method, "")
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(linux_kde, "_run", fake_run)
    return fake


HOSTILE = "true\n'x' \"y\" -a b $(id) 測試 🎉"
# Text that busctl would read as its own options. The string above has a "-a"
# in the middle, which busctl accepts; only a *leading* dash breaks it, which
# is how this went unnoticed until a KDE smoke run typed "-conductor smoke 1"
# and the paste silently produced the previous clipboard instead.
OPTION_LOOKALIKES = ["-conductor smoke 1", "--help", "-n", "--address=unix:path=/tmp/x"]


async def test_klipper_set_passes_text_as_one_string_argument(kde):
    await kde.mod.set_clipboard(HOSTILE)
    assert kde.calls == [["busctl", "--user", "--", "call", "org.kde.klipper", "/klipper",
                          "org.kde.klipper.klipper", "setClipboardContents", "s", HOSTILE]]


@pytest.mark.parametrize("text", OPTION_LOOKALIKES)
async def test_clipboard_text_that_looks_like_an_option_stays_an_argument(kde, text):
    await kde.mod.set_clipboard(text)
    cmd = kde.calls[0]
    assert cmd[-1] == text
    # Everything busctl must not parse comes after the separator.
    assert "--" in cmd and cmd.index("--") < cmd.index(text)


async def test_klipper_reads_also_guard_the_separator(kde):
    kde.replies["getClipboardContents"] = json.dumps({"type": "s", "data": ["x"]})
    await kde.mod.get_clipboard()
    cmd = kde.calls[0]
    assert cmd.index("--") < cmd.index("call")


async def test_klipper_get_reads_the_json_reply(kde):
    kde.replies["getClipboardContents"] = json.dumps({"type": "s", "data": [HOSTILE]})
    assert await kde.mod.get_clipboard() == HOSTILE
    assert "--json=short" in kde.calls[0]


async def test_klipper_failure_falls_back_to_wl_clipboard(kde, monkeypatch):
    from conductor_desktop.core.errors import BackendError

    kde.replies["getClipboardContents"] = BackendError("busctl is not installed")

    async def wl_get():
        return "from wl-paste"

    monkeypatch.setattr(kde.mod, "wl_or_x_get", wl_get)
    assert await kde.mod.get_clipboard() == "from wl-paste"


async def test_both_clipboard_paths_failing_names_both(kde, monkeypatch):
    from conductor_desktop.core.errors import BackendError

    kde.replies["setClipboardContents"] = BackendError("Klipper is not running")

    async def wl_set(text):
        raise BackendError("wl-copy timed out")

    monkeypatch.setattr(kde.mod, "wl_or_x_set", wl_set)
    with pytest.raises(BackendError, match="Klipper: Klipper is not running; wl-copy timed out"):
        await kde.mod.set_clipboard("x")
