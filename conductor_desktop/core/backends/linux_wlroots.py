"""Linux wlroots Wayland backend — wlrctl, wl-clipboard, AT-SPI2.

Target: wlroots-based compositors: labwc (Raspberry Pi OS), Wayfire,
Sway, Hyprland.
System deps: wlrctl, wl-clipboard, at-spi2-core
"""

from __future__ import annotations

import json
import logging
import shutil

from ..errors import BackendError, reason
from ._cli import run as _run
from ._cli import wl_get, wl_set

log = logging.getLogger("conductor_desktop.wlroots")


# ── Compositor detection ────────────────────────────────────────────


def _have_sway() -> bool:
    return shutil.which("swaymsg") is not None

def _have_hyprctl() -> bool:
    return shutil.which("hyprctl") is not None


# ── Screen enumeration ──────────────────────────────────────────────


async def list_screens() -> list[dict]:
    """Enumerate outputs via the compositor IPC.

    Sway: ``swaymsg -t get_outputs`` (i3-style JSON).
    Hyprland: ``hyprctl -j monitors``.
    Other wlroots compositors (Wayfire, river) have no portable enumeration,
    so this raises there. See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Linux wlroots".
    """
    tried = []
    if _have_sway():
        try:
            out = await _run(["swaymsg", "-t", "get_outputs"])
            data = json.loads(out)
            screens = []
            for i, o in enumerate(data):
                if not o.get("active", True):
                    continue
                rect = o.get("rect", {})
                screens.append({
                    "index": i,
                    "name": o.get("name", f"output-{i}"),
                    "bounds": [
                        int(rect.get("x", 0)),
                        int(rect.get("y", 0)),
                        int(rect.get("width", 0)),
                        int(rect.get("height", 0)),
                    ],
                    "scale": float(o.get("scale", 1.0)),
                    "primary": bool(o.get("primary", False)) or i == 0,
                })
            return screens
        except (BackendError, ValueError, KeyError, TypeError) as e:
            tried.append(f"swaymsg get_outputs: {reason(e)}")

    if _have_hyprctl():
        try:
            out = await _run(["hyprctl", "-j", "monitors"])
            data = json.loads(out)
            screens = []
            for i, o in enumerate(data):
                screens.append({
                    "index": int(o.get("id", i)),
                    "name": o.get("name", f"monitor-{i}"),
                    "bounds": [
                        int(o.get("x", 0)),
                        int(o.get("y", 0)),
                        int(o.get("width", 0)),
                        int(o.get("height", 0)),
                    ],
                    "scale": float(o.get("scale", 1.0)),
                    "primary": bool(o.get("focused", False)) or i == 0,
                })
            return screens
        except (BackendError, ValueError, KeyError, TypeError) as e:
            tried.append(f"hyprctl monitors: {reason(e)}")

    raise BackendError("; ".join(tried) or
                       "no screen list on this compositor (needs sway or Hyprland IPC)")


async def _screens_or_none() -> list[dict]:
    """Screens for monitor attribution; windows still list without them."""
    try:
        return await list_screens()
    except BackendError as e:
        log.debug("list_windows without screens: %s", e)
        return []


# ── Window Management ───────────────────────────────────────────────


def _flatten_sway_tree(node: dict, monitor_by_output: dict[str, int],
                       z_counter: list[int], output: str = "",
                       workspace: str = "") -> list[dict]:
    """Depth-first flatten of swaymsg get_tree into window dicts.

    sway's tree: root → outputs → workspaces → containers → windows.
    We propagate output + workspace name as we descend. `z_counter` is a
    one-element list used as a mutable counter (z=0 is the first window
    visited, which is the topmost when sway emits focused-first).
    """
    out = []
    node_type = node.get("type", "")
    if node_type == "output":
        output = node.get("name", output)
    elif node_type == "workspace":
        workspace = node.get("name", workspace)

    is_window = node_type in ("con", "floating_con") and node.get("pid") is not None
    if is_window:
        rect = node.get("rect", {})
        is_fullscreen = bool(node.get("fullscreen_mode", 0))
        if is_fullscreen:
            layer = "fullscreen"
            state = "fullscreen"
        elif node_type == "floating_con":
            layer = "floating"
            state = "normal"
        else:
            layer = "normal"
            state = "normal"
        out.append({
            "id": str(node.get("id", "")),
            "title": node.get("name", "") or "",
            "process_name": node.get("app_id") or node.get("window_properties", {}).get("class", "") or "",
            "pid": int(node.get("pid", 0) or 0),
            "bounds": [
                int(rect.get("x", 0)),
                int(rect.get("y", 0)),
                int(rect.get("width", 0)),
                int(rect.get("height", 0)),
            ],
            "state": state,
            "desktop": 0,  # legacy; per-output workspaces don't have a global index
            "focused": bool(node.get("focused", False)),
            "monitor": monitor_by_output.get(output, -1),
            "z": z_counter[0],
            "layer": layer,
            "sticky": False,
            "workspace": {
                "global_index": None,
                "per_output": {
                    "output": output,
                    "name": workspace or None,
                    "index": 0,
                },
            },
        })
        z_counter[0] += 1

    # Floating children first (rendered on top), then tiled.
    for child in node.get("floating_nodes", []) or []:
        out.extend(_flatten_sway_tree(child, monitor_by_output, z_counter, output, workspace))
    for child in node.get("nodes", []) or []:
        out.extend(_flatten_sway_tree(child, monitor_by_output, z_counter, output, workspace))
    return out


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows via swaymsg / hyprctl, falling back to wlrctl titles.

    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Linux wlroots". Pure-wlrctl path is
    a minimal title-only fallback for Wayfire / river / other wlroots
    compositors without an i3-style IPC.
    """
    return (await _windows())[1]


async def _windows() -> tuple[str, list[dict]]:
    """``list_windows`` plus which tool listed them ("sway", "hyprland" or
    "wlrctl"): each names windows its own way, and focusing needs that name."""
    tried = []
    if _have_sway():
        try:
            screens = await _screens_or_none()
            monitor_by_output = {s["name"]: s["index"] for s in screens}
            out = await _run(["swaymsg", "-t", "get_tree"])
            tree = json.loads(out)
            return "sway", _flatten_sway_tree(tree, monitor_by_output, [0])
        except (BackendError, ValueError, KeyError, TypeError) as e:
            tried.append(f"swaymsg get_tree: {reason(e)}")

    if _have_hyprctl():
        try:
            screens = await _screens_or_none()
            monitor_by_id = {s["index"]: s for s in screens}
            out = await _run(["hyprctl", "-j", "clients"])
            data = json.loads(out)
            windows = []
            # hyprctl returns clients in stacking order (top-first).
            for i, c in enumerate(data):
                at = c.get("at", [0, 0])
                sz = c.get("size", [0, 0])
                mon_id = int(c.get("monitor", -1))
                is_fs = bool(c.get("fullscreen", False))
                is_float = bool(c.get("floating", False))
                ws = c.get("workspace") or {}
                if is_fs:
                    layer, state = "fullscreen", "fullscreen"
                elif is_float:
                    layer, state = "floating", "normal"
                else:
                    layer, state = "normal", "normal"
                windows.append({
                    "id": c.get("address", str(i)),
                    "title": c.get("title", "") or "",
                    "process_name": c.get("class", "") or "",
                    "pid": int(c.get("pid", 0) or 0),
                    "bounds": [int(at[0]), int(at[1]), int(sz[0]), int(sz[1])],
                    "state": state,
                    "desktop": int(ws.get("id", 0) or 0),
                    # hyprctl has no "focused": the focused client is the
                    # most recent in the focus history.
                    "focused": c.get("focusHistoryID") == 0,
                    "monitor": mon_id if mon_id in monitor_by_id else -1,
                    "z": i,
                    "layer": layer,
                    "sticky": False,
                    "workspace": {
                        "global_index": None,
                        "per_output": {
                            "output": monitor_by_id.get(mon_id, {}).get("name") if mon_id in monitor_by_id else None,
                            "name": ws.get("name"),
                            "index": int(ws.get("id", 0) or 0),
                        },
                    },
                })
            return "hyprland", windows
        except (BackendError, ValueError, KeyError, TypeError) as e:
            tried.append(f"hyprctl clients: {reason(e)}")

    # Generic wlroots fallback: titles only via wlrctl.
    try:
        output = await _run(["wlrctl", "toplevel", "list"])
    except BackendError as e:
        raise BackendError("; ".join([*tried, f"wlrctl: {e}"])) from None

    windows = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # "<app_id>: <title>"; an app id never contains ": ", a title may.
        app_id, sep, title = line.partition(": ")
        if not sep:
            app_id, title = "", line
        windows.append({
            "id": str(len(windows)),
            "title": title,
            "process_name": app_id,
            "pid": 0,
            "bounds": [0, 0, 0, 0],
            "state": "normal",
            "desktop": 0,
            "focused": False,
            "monitor": -1,
            "z": len(windows),
            "layer": "normal",
            "sticky": False,
            "workspace": {"global_index": None, "per_output": None},
        })
    return "wlrctl", windows


async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus the first window whose title contains ``title`` (ignoring case),
    or whose pid is ``pid``, like the other backends. False: nothing matched.

    The match runs here, on the window list, and the focus goes by the
    compositor's own name for the window: wlrctl only matches a title
    exactly (and a bare argument is an app id), and Hyprland's ``title:`` is
    a regex.
    """
    if not title and not pid:
        return False
    source, windows = await _windows()
    if pid and source == "wlrctl":
        raise BackendError("focusing by pid needs sway or Hyprland; wlrctl lists no pids")
    needle = title.casefold() if title else None
    match = next((w for w in windows
                  if (pid and w.get("pid") == pid)
                  or (needle and needle in (w.get("title") or "").casefold())), None)
    if match is None:
        return False
    if source == "sway":
        cmd = ["swaymsg", f"[con_id={int(match['id'])}]", "focus"]
    elif source == "hyprland":
        cmd = ["hyprctl", "dispatch", "focuswindow", f"address:{match['id']}"]
    else:
        cmd = ["wlrctl", "toplevel", "focus", f"title:{match['title']}"]
    out = await _run(cmd)
    if source == "hyprland" and out.strip() != "ok":
        raise BackendError(f"hyprctl focuswindow: {out.strip()}")
    return True


# ── Clipboard ───────────────────────────────────────────────────────


async def get_clipboard() -> str:
    """Read clipboard via wl-paste ("" when it holds no text)."""
    return await wl_get()


async def set_clipboard(text: str) -> None:
    """Write to clipboard via wl-copy."""
    await wl_set(text)


# ── Accessibility (AT-SPI2 — same as X11) ───────────────────────────
# AT-SPI2 works on Wayland too — GTK/Qt apps register regardless of
# display server. Reuse the X11 backend's a11y implementation.


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402
from .linux_ui import ui_act, ui_walk  # noqa: E402


class WlrootsDesktop(DesktopBackend):
    """wlroots compositors (Wayfire, Sway, Hyprland): wlrctl or the compositor IPC, wl-clipboard, AT-SPI."""

    name = "linux_wlroots"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
