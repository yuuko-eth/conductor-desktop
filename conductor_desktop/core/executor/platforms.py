"""Which desktop and input backend each platform gets: the one routing table."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from ..backends.desktop import DesktopBackend
from ..platform_detect import Platform
from .input import LocalInputBackend


def _load(package: str, spec: str):
    module, cls = spec.split(":")
    return getattr(importlib.import_module(f"{package}.{module}", __package__), cls)


@dataclass(frozen=True)
class PlatformBackends:
    """Both backends are ``"module:Class"`` specs, imported on connect: the
    desktop modules import their platform's native libraries at the top, and
    a module may be missing from a build altogether (the public release
    leaves out Haiku's, since the server can't be installed there)."""

    #: under ``core.backends``
    desktop: str | None
    #: under ``core.executor.input``
    input: str | None

    def load_desktop(self) -> DesktopBackend:
        """The desktop backend; raises ImportError & co. when it can't load."""
        if self.desktop is None:
            return DesktopBackend()
        return _load("..backends", self.desktop)()

    def load_input(self) -> LocalInputBackend | None:
        """The input backend, or None when the platform has none; raises
        ImportError when its module isn't there."""
        return _load(".input", self.input)() if self.input else None


PLATFORMS: dict[Platform, PlatformBackends] = {
    Platform.LINUX_X11: PlatformBackends("linux_x11:X11Desktop", "xdotool:XdotoolInput"),
    # FreeBSD's X11 tools (xdotool, wmctrl, xclip) behave like Linux's.
    Platform.FREEBSD_X11: PlatformBackends("linux_x11:X11Desktop", "xdotool:XdotoolInput"),
    Platform.LINUX_GNOME: PlatformBackends("linux_gnome:GnomeDesktop", "ydotool:YdotoolInput"),
    # KWin: windows through a KWin script, clipboard through Klipper (see linux_kde).
    Platform.LINUX_KDE: PlatformBackends("linux_kde:KdeDesktop", "ydotool:KwinYdotoolInput"),
    Platform.LINUX_WLROOTS: PlatformBackends("linux_wlroots:WlrootsDesktop",
                                             "ydotool:WlrootsYdotoolInput"),
    Platform.MACOS: PlatformBackends("macos:MacDesktop", "quartz:QuartzInput"),
    Platform.WINDOWS: PlatformBackends("windows:WindowsDesktop", "win32:Win32Input"),
    Platform.HAIKU: PlatformBackends("haiku:HaikuDesktop", "haiku:HaikuInput"),
    Platform.UNKNOWN: PlatformBackends(None, None),
}
