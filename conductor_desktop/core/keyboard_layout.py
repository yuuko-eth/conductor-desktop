"""The active keyboard layout on a Linux Wayland desktop, as an xkb name.

ydotool sends evdev key codes, which mean US positions unless the layout
says otherwise: on German, the code for US z types y, and ctrl+z by position
is ctrl+y. Knowing the layout lets the ydotool backend find the right key.
X11 needs none of this: xdotool sends key symbols through the X keymap.

Asks, in order: IBus (GNOME runs its input sources through it), GNOME's
input sources, KDE's layout service, Hyprland, then XKB_DEFAULT_LAYOUT. None
means it couldn't tell, and callers keep the US positions.
"""

from __future__ import annotations

import ast
import asyncio
import configparser
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("conductor_desktop.keyboard_layout")


@dataclass(frozen=True)
class Layout:
    layout: str          # "de", "ru", "us"
    variant: str = ""    # "nodeadkeys", "dvorak", ""

    @property
    def is_us(self) -> bool:
        return self.layout == "us" and not self.variant


_CACHE_SECONDS = 2.0
_cache: tuple[float, Layout | None] | None = None


async def active() -> Layout | None:
    """The layout keys type with now. Cached for two seconds."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    found = None
    for probe in (_ibus, _gnome, _kde, _hyprland, _env):
        try:
            found = await probe()
        except Exception:  # advice only: a failed probe means "ask the next"
            log.debug("keyboard layout probe %s failed", probe.__name__, exc_info=True)
            found = None
        if found is not None:
            break
    _cache = (now, found)
    return found


def forget() -> None:
    global _cache
    _cache = None


def parse_xkb(spec: str) -> Layout | None:
    """'de', 'de+nodeadkeys' (GNOME), 'de(nodeadkeys)' (KDE, setxkbmap)."""
    spec = spec.strip()
    if not spec:
        return None
    if "+" in spec:
        layout, variant = spec.split("+", 1)
    elif "(" in spec and spec.endswith(")"):
        layout, variant = spec[:-1].split("(", 1)
    else:
        layout, variant = spec, ""
    return Layout(layout, variant)


async def _ibus() -> Layout | None:
    if not shutil.which("ibus"):
        return None
    engine = await _output("ibus", "engine")
    # xkb:de::deu, xkb:de:nodeadkeys:deu. An input method engine (chewing)
    # types through the layout under it, which this can't see: fall through.
    if not engine.startswith("xkb:"):
        return None
    parts = engine.split(":")
    return Layout(parts[1], parts[2] if len(parts) > 2 else "")


async def _gnome() -> Layout | None:
    if "gnome" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return None
    # The most recently used source is the active one.
    for key in ("mru-sources", "sources"):
        text = await _output("gsettings", "get", "org.gnome.desktop.input-sources", key)
        sources = _gvariant_pairs(text)
        for kind, spec in sources:
            if kind == "xkb":
                return parse_xkb(spec)
            break  # an IBus engine is first: its layout isn't named here
        if sources:
            return None
    return None


def _gvariant_pairs(text: str) -> list[tuple[str, str]]:
    """gsettings' "[('xkb', 'de'), ('ibus', 'chewing')]" (or "@a(ss) [])."""
    text = text.removeprefix("@a(ss)").strip()
    try:
        value = ast.literal_eval(text) if text else []
    except (ValueError, SyntaxError):
        return []
    return [(str(a), str(b)) for a, b in value]


async def _kde() -> Layout | None:
    if "kde" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return None
    rc = Path.home() / ".config" / "kxkbrc"
    if not rc.exists():
        return None
    config = configparser.ConfigParser(interpolation=None)
    config.read(rc)
    layouts = [x for x in config.get("Layout", "LayoutList", fallback="").split(",") if x]
    variants = config.get("Layout", "VariantList", fallback="").split(",")
    if not layouts:
        return None
    index = 0
    reply = await _output("gdbus", "call", "--session", "-d", "org.kde.keyboard",
                          "-o", "/Layouts", "-m", "org.kde.KeyboardLayouts.getLayout")
    # "(uint32 1,)"
    digits = "".join(c for c in reply if c.isdigit())
    if digits and int(digits) < len(layouts):
        index = int(digits)
    variant = variants[index] if index < len(variants) else ""
    return Layout(layouts[index], variant)


async def _hyprland() -> Layout | None:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return None
    devices = json.loads(await _output("hyprctl", "devices", "-j") or "{}")
    keyboards = devices.get("keyboards") or []
    main = next((k for k in keyboards if k.get("main")), keyboards[0] if keyboards else None)
    if not main:
        return None
    layouts = [x for x in (main.get("layout") or "").split(",") if x]
    variants = (main.get("variant") or "").split(",")
    index = int(main.get("active_layout_index") or 0)
    if not layouts or index >= len(layouts):
        return None
    return Layout(layouts[index], variants[index] if index < len(variants) else "")


async def _env() -> Layout | None:
    # Sway and others read these when they build the keymap.
    layout = os.environ.get("XKB_DEFAULT_LAYOUT", "").split(",")[0]
    variant = os.environ.get("XKB_DEFAULT_VARIANT", "").split(",")[0]
    return Layout(layout, variant) if layout else None


async def _output(*cmd: str) -> str:
    """A command's stdout, stripped; "" when it fails or takes over a second."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
    except TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace").strip() if proc.returncode == 0 else ""
