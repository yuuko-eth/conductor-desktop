"""Detect platform and display server, select the right backend."""

from __future__ import annotations

import configparser
import functools
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class Platform(str, Enum):  # noqa: UP042  (not StrEnum: a source install runs on 3.10)
    """The desktop the server drives: which backends it gets is
    ``core.executor.platforms.PLATFORMS``. A ``str`` so logs and comparisons
    read as the plain name ("linux-x11")."""

    LINUX_X11 = "linux-x11"
    FREEBSD_X11 = "freebsd-x11"
    LINUX_GNOME = "linux-gnome"
    LINUX_KDE = "linux-kde"
    LINUX_WLROOTS = "linux-wlroots"
    MACOS = "macos"
    WINDOWS = "windows"
    HAIKU = "haiku"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Detection:
    """The platform route and why it was picked, e.g. "XDG_CURRENT_DESKTOP=KDE"
    — ``diagnose`` shows the reason so a misrouted host explains itself."""

    platform: Platform
    reason: str


def detect(override: str = "") -> Detection:
    """The route for this host. ``override`` (CONDUCTOR_DESKTOP_PLATFORM) names a
    ``Platform`` value and skips detection; a value that names none is
    ignored with a warning, never fatal."""
    if override.strip():
        try:
            return Detection(Platform(override.strip().lower()),
                             f"CONDUCTOR_DESKTOP_PLATFORM={override.strip()}")
        except ValueError:
            valid = ", ".join(p.value for p in Platform)
            log.warning("CONDUCTOR_DESKTOP_PLATFORM=%r names no platform (one of: %s); "
                        "detecting instead", override, valid)
            found = _detect()
            return Detection(found.platform,
                             f"{found.reason}; ignored CONDUCTOR_DESKTOP_PLATFORM={override!r}")
    return _detect()


def _detect() -> Detection:
    system = platform.system().lower()

    if system == "darwin":
        return Detection(Platform.MACOS, "Darwin")
    if system == "windows":
        return Detection(Platform.WINDOWS, "Windows")
    if system == "linux":
        return _detect_linux_display()
    if system == "haiku":
        return Detection(Platform.HAIKU, "Haiku")
    if system == "freebsd":
        # FreeBSD desktops are overwhelmingly X11; xdotool/wmctrl/xclip
        # behave identically to Linux. Route to the linux_x11 backend.
        # Add a freebsd-wayland branch later if anyone actually runs that.
        return Detection(Platform.FREEBSD_X11, "FreeBSD (always X11)")

    return Detection(Platform.UNKNOWN, f"unsupported OS {platform.system()!r}")


def _detect_linux_display() -> Detection:
    """Detect Linux display server and compositor."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")

    if session_type == "x11":
        return Detection(Platform.LINUX_X11, "XDG_SESSION_TYPE=x11")
    if not wayland_display and os.environ.get("DISPLAY"):
        return Detection(Platform.LINUX_X11, "DISPLAY set, no WAYLAND_DISPLAY")

    if session_type == "wayland":
        return _detect_wayland_compositor("XDG_SESSION_TYPE=wayland")
    if wayland_display:
        return _detect_wayland_compositor(f"WAYLAND_DISPLAY={wayland_display}")

    return Detection(Platform.LINUX_X11,
                     "no XDG_SESSION_TYPE, WAYLAND_DISPLAY or DISPLAY; X11 by default")


#: XDG_CURRENT_DESKTOP substrings, checked in this order. Unity and Ubuntu
#: come last: they draw GNOME's icons, but "ubuntu" could prefix a remix's
#: own desktop name.
_DESKTOP_NAMES = (
    ("gnome", ("gnome",)),
    ("kde", ("kde", "plasma")),
    ("wlroots", ("sway", "hyprland", "wayfire")),
    ("gnome", ("unity", "ubuntu")),
)


def _desktop_family() -> str | None:
    """'gnome', 'kde' or 'wlroots' from XDG_CURRENT_DESKTOP, else None. The
    one reading of the desktop name, shared by the backend route and the icon
    theme so the two can't disagree about which desktop this is."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    for family, names in _DESKTOP_NAMES:
        if any(name in desktop for name in names):
            return family
    return None


def _detect_wayland_compositor(session: str) -> Detection:
    """Detect which Wayland compositor is running.

    The desktop the session names wins over an installed tool: ``wlrctl`` on
    a KDE or GNOME machine doesn't make KWin or Mutter a wlroots compositor.
    """
    family = _desktop_family()
    named = f"{session}, XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '')}"

    if family == "gnome":
        return Detection(Platform.LINUX_GNOME, named)
    if family == "kde":
        return Detection(Platform.LINUX_KDE, named)

    # wlroots-based compositors (Sway, Hyprland, Wayfire, labwc, ...)
    if family == "wlroots":
        return Detection(Platform.LINUX_WLROOTS, named)
    for var in ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE"):
        if os.environ.get(var):
            return Detection(Platform.LINUX_WLROOTS, f"{session}, {var} set")
    if _has_command("wlrctl"):
        return Detection(Platform.LINUX_WLROOTS, f"{named}, wlrctl installed")

    # Unknown compositor: the GNOME backend's clipboard and AT-SPI work on any
    # Wayland desktop; its window tools fail with a reason.
    return Detection(Platform.LINUX_GNOME,
                     f"{named}: no known compositor and no wlrctl, so the generic "
                     "Wayland fallback (install wlrctl for wlroots window tools)")


def _has_command(name: str) -> bool:
    # shutil.which works on all platforms (replaces unix-only `which`)
    return shutil.which(name) is not None


def _slug(s: str) -> str:
    """Lowercase kebab slug — MUST match OmniParser's util _slug / theme tags.

    The icon library tags refs ``theme:<slug>`` where slug = this exact rule
    (e.g. gsettings ``'Yaru-blue'`` → ``yaru-blue``). A mismatched slug excludes
    every themed ref for the platform, so keep these two in lockstep.
    """
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


@functools.lru_cache(maxsize=1)
def detect_icon_env() -> tuple[str | None, str | None]:
    """(os, theme) for OmniParser icon labeling — keyed on DESKTOP+ICON THEME.

    This is a DIFFERENT axis from `detect()`, which keys on display
    server / input backend (linux-x11 vs linux-wlroots vs linux-gnome). Icon
    matching cares about which icon theme is *rendered*, which is a desktop-
    environment question independent of the display server: GNOME on X11 still
    draws Yaru icons, KDE draws Breeze not Yaru. So we detect the DE from
    XDG_CURRENT_DESKTOP (not the session type) and the theme from the DE's own
    config, returning strings that line up with the library's
    ``platform:<id>`` / ``theme:<slug>`` tags.

      macOS   → ('macos', 'light'|'dark')  # the appearance; app/dock refs are
                                           #   theme-agnostic so they match either,
                                           #   menu-bar SF Symbols are light/dark
      Windows → ('windows', 'win10'|'win11')  # OS version is the icon-style axis
                                              #   (build 22000+ = win11); full-colour
                                              #   shell icons don't vary light/dark
      GNOME   → ('linux-gnome', <gsettings icon-theme slug>)  e.g. 'yaru-blue'
      KDE     → ('linux-kde',  <kdeglobals Icons/Theme slug>) e.g. 'breeze-dark'
      other   → (None, None)           # nothing to gate on → caller omits os=

    Cached: the host's DE/theme is stable for a process; the gsettings probe
    runs once. (`detect_icon_env.cache_clear()` if a live theme switch matters.)
    """
    system = platform.system().lower()
    if system == "darwin":
        return ("macos", _macos_appearance())
    if system == "windows":
        return ("windows", _windows_version())
    if system != "linux":
        return (None, None)

    family = _desktop_family()
    if family == "gnome":
        return ("linux-gnome", _gnome_icon_theme())
    if family == "kde":
        return ("linux-kde", _kde_icon_theme())
    return (None, None)


def _windows_version() -> str:
    """'win11' on build >= 22000, else 'win10' — the Windows icon-style axis.

    Microsoft kept the kernel version at 10.0.x for Windows 11, so the build
    number is the discriminator (22000 = the first Win11 build). The icon set
    (imageres.dll / shell32.dll) was restyled in 11, which is what the theme tag
    gates on.
    """
    try:
        build = int(platform.version().split(".")[-1])
        return "win11" if build >= 22000 else "win10"
    except ValueError:
        return "win10"


def _macos_appearance() -> str:
    """'dark' if the system is in Dark Mode, else 'light'.

    AppleInterfaceStyle is the string 'Dark' in Dark Mode and ABSENT (read
    fails) in Light Mode — the canonical probe. Light is the safe default.
    """
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"], check=False,
            capture_output=True, text=True, timeout=5,
        )
        return "dark" if out.returncode == 0 and "dark" in out.stdout.lower() else "light"
    except (OSError, subprocess.SubprocessError):
        return "light"


def _gnome_icon_theme() -> str | None:
    """Active GTK icon theme via gsettings, slugified; None if unavailable."""
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"], check=False,
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        name = out.stdout.strip().strip("'\"")
        return _slug(name) or None
    except (OSError, subprocess.SubprocessError):
        return None


def _kde_icon_theme() -> str | None:
    """Active KDE icon theme from ~/.config/kdeglobals [Icons] Theme, slugified."""
    path = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "kdeglobals",
    )
    try:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read(path, encoding="utf-8")
        name = cp.get("Icons", "Theme", fallback="").strip()
        return _slug(name) or None
    except (configparser.Error, OSError, ValueError):  # ValueError: not UTF-8
        return None
