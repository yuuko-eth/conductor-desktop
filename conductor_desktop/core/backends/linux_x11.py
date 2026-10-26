"""Linux X11 backend — wmctrl, xdotool, xclip, AT-SPI2.

Target: any X11 session (GNOME, KDE Plasma, XFCE, ...).
System deps: wmctrl, xdotool, xclip, at-spi2-core; xwininfo (window positions)
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil

from ..errors import BackendError
from . import _common
from ._cli import run as _run
from ._cli import run_ok as _run_ok
from ._cli import xclip_get, xclip_set

log = logging.getLogger("conductor_desktop.x11")


def _require_bin(name: str) -> None:
    """Raise a clear error if a required CLI helper is missing.

    Without this guard, a missing `wmctrl` on a SteamOS atomic-update host
    surfaces as `focus_window` returning False with an empty diagnostic —
    the planner can't tell "wrong title" from "no window list at all".
    """
    if shutil.which(name) is None:
        raise BackendError(
            f"{name!r} not on PATH — required by the linux_x11 backend. "
            f"Install with apt/dnf/pacman (see https://docs.conductor-mcp.io/platforms/linux/x11/; "
            f"on Steam Deck see https://docs.conductor-mcp.io/platforms/linux/steamos/ — the atomic update "
            f"wipes pacman-installed binaries)."
        )


# ── Screen enumeration ──────────────────────────────────────────────


# xrandr --current line for an active output (one without geometry is off):
#   "VGA-1 connected primary 1920x1200+0+0 (normal left ...) 518mm x 324mm"
# Groups: name, primary, w, h, x, y.
_OUTPUT_RE = re.compile(
    r"^(\S+) (?:dis)?connected( primary)? (\d+)x(\d+)\+(-?\d+)\+(-?\d+)"
)


def _parse_outputs(output: str) -> list[dict]:
    screens = []
    for line in output.splitlines():
        m = _OUTPUT_RE.match(line)
        if not m:
            continue
        name, primary, w, h, x, y = m.groups()
        screens.append({
            "index": len(screens),
            "name": name,
            "bounds": [int(x), int(y), int(w), int(h)],
            "scale": 1.0,
            "primary": bool(primary),
        })
    # No output flagged primary: nobody ran `xrandr --primary`, or the
    # driver has no RandR 1.2 (FreeBSD's scfb/vesa list one "default"
    # output). X then treats the first as the default, as KDE and wlroots do.
    if screens and not any(s["primary"] for s in screens):
        screens[0]["primary"] = True
    return screens


async def list_screens() -> list[dict]:
    """Enumerate monitors via `xrandr --current`.

    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Linux X11". Returns one entry per
    active output, with bounds in root-window coords (matches wmctrl
    window bounds). HiDPI scale is not surfaced by xrandr; defaults to
    1.0. Each screen also gets a ``dock`` field when an EWMH dock window
    can be attributed to it.

    ``--current``: without it xrandr has the server re-probe every output,
    which reads the monitor's EDID over DDC while X waits (64 ms a call on
    one GNOME X11 box, and list_windows asks on every call); --listactivemonitors
    probes even with --current.
    """
    screens = _parse_outputs(await _run(["xrandr", "--current"]))

    try:
        await _attach_dock_info(screens)
    except Exception as e:
        log.debug("dock detection failed: %s", e, exc_info=True)
    return screens


# ── Dock detection ──────────────────────────────────────────────────


async def _root_client_list() -> list[str]:
    """Return every managed top-level window id (hex string) per EWMH.

    ``_NET_CLIENT_LIST`` enumerates regular-app windows AND dock-type
    windows (panels, taskbars, Plank, tint2, etc.) — anything the WM
    has chosen to manage. Override-redirect surfaces (true popups,
    some Chrome OS-style "shelf" overlays) are not in this list and
    can't be detected without an XComposite hook; we accept that
    limitation as documented in design_scene_graph.md.
    """
    try:
        out = await _run(["xprop", "-root", "_NET_CLIENT_LIST"], timeout=2)
    except BackendError:
        return []
    # Output: "_NET_CLIENT_LIST(WINDOW): window id # 0xa00001, 0xa00002, ..."
    _, _, rhs = out.partition("#")
    ids: list[str] = []
    for tok in rhs.split(","):
        tok = tok.strip()
        if tok.startswith("0x"):
            ids.append(tok)
    return ids


async def _xprop_for_dock(wid_hex: str) -> dict:
    """Fetch the per-window xprop bundle needed for dock classification.

    One fork per window — callers must use ``asyncio.gather`` so the
    sweep doesn't serialise. Returns ``{}`` on failure (window died,
    xprop refused, etc.).
    """
    try:
        out = await _run(
            ["xprop", "-id", wid_hex,
             "_NET_WM_WINDOW_TYPE", "_NET_WM_STRUT_PARTIAL",
             "_NET_WM_STRUT", "_NET_WM_STATE"],
            timeout=2,
        )
    except BackendError:
        return {}
    props: dict[str, str] = {}
    for line in out.splitlines():
        if " = " in line:
            k, _, v = line.partition(" = ")
            props[k.strip()] = v.strip()
    return props


def _parse_strut(props: dict) -> tuple[int, int, int, int] | None:
    """Parse ``_NET_WM_STRUT_PARTIAL`` (or ``_NET_WM_STRUT``) into
    (left, right, top, bottom) reservation widths.

    PARTIAL carries 12 values (4 reservations + 8 start/end spans);
    the older STRUT has just the 4 reservations. Either is fine for
    detecting which edge a dock is pinned to and how thick it is.
    """
    raw = props.get("_NET_WM_STRUT_PARTIAL(CARDINAL)") \
        or props.get("_NET_WM_STRUT(CARDINAL)")
    if not raw:
        return None
    try:
        vals = [int(v.strip()) for v in raw.split(",")[:4]]
    except ValueError:
        return None
    if len(vals) < 4:
        return None
    return (vals[0], vals[1], vals[2], vals[3])


async def _attach_dock_info(screens: list[dict]) -> None:
    """Mutate ``screens`` in place, attributing each ``_NET_WM_WINDOW_TYPE_DOCK``
    window to a monitor and recording its geometry.

    Per-monitor attribution uses geometric intersection — the dock's
    centre point against ``screens[i].bounds`` — so multi-monitor
    setups with separate panels per output behave correctly. ``hidden``
    is heuristic: ``_NET_WM_STATE_HIDDEN`` if present, else off-screen
    via geometry. EWMH doesn't expose an "autohide" flag, so
    ``autohide`` is ``None`` (the setting lives in panel-specific
    configs like ``xfconf`` / ``plasmashellrc`` and we don't probe
    those).
    """
    ids = await _root_client_list()
    if not ids:
        return

    propsets = await asyncio.gather(*(_xprop_for_dock(wid) for wid in ids))

    # We need the dock window's geometry too. We could re-xprop with
    # ``-geometry`` flag but the cleanest path is a single ``wmctrl
    # -l -G`` lookup, which is already used by list_windows.
    try:
        wm_out = await _run(["wmctrl", "-l", "-G"], timeout=2)
    except BackendError:
        wm_out = ""
    geom: dict[str, tuple[int, int, int, int]] = {}
    for line in wm_out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        wid_hex = parts[0]
        # Normalise wmctrl's 0x00a00001 form to xprop's 0xa00001 form.
        try:
            wid_norm = hex(int(wid_hex, 16))
        except ValueError:
            continue
        try:
            x, y, w, h = (int(parts[2]), int(parts[3]),
                          int(parts[4]), int(parts[5]))
        except ValueError:
            continue
        geom[wid_norm] = (x, y, w, h)

    dock_entries: list[tuple[tuple[int, int, int, int], dict, str | None]] = []
    for wid_hex, props in zip(ids, propsets):
        wm_type = props.get("_NET_WM_WINDOW_TYPE(ATOM)", "")
        if "_NET_WM_WINDOW_TYPE_DOCK" not in wm_type:
            continue
        try:
            wid_norm = hex(int(wid_hex, 16))
        except ValueError:
            continue
        rect = geom.get(wid_norm)
        if rect is None:
            # No geometry from wmctrl (some panels are skip-pager and
            # don't show up there). Fall back to strut for edge only.
            rect = (0, 0, 0, 0)
        strut = _parse_strut(props)
        edge: str | None = None
        if strut is not None:
            left, right, top, bottom = strut
            # The non-zero entry wins; ties prefer top/bottom (most
            # docks are horizontal).
            edge_vals = {"left": left, "right": right,
                         "top": top, "bottom": bottom}
            nonzero = {k: v for k, v in edge_vals.items() if v > 0}
            if nonzero:
                edge = max(nonzero, key=nonzero.get)
        dock_entries.append((rect, props, edge))

    if not dock_entries:
        return

    for s in screens:
        sx, sy, sw, sh = s["bounds"]
        best_idx = -1
        best_overlap = 0
        for i, (rect, _props, _edge) in enumerate(dock_entries):
            rx, ry, rw, rh = rect
            if rw <= 0 or rh <= 0:
                continue
            ix = max(0, min(rx + rw, sx + sw) - max(rx, sx))
            iy = max(0, min(ry + rh, sy + sh) - max(ry, sy))
            area = ix * iy
            if area > best_overlap:
                best_overlap = area
                best_idx = i
        if best_idx < 0:
            continue
        rect, props, edge = dock_entries[best_idx]
        rx, ry, rw, rh = rect
        wm_state = props.get("_NET_WM_STATE(ATOM)", "")
        hidden: bool | None
        if "_NET_WM_STATE_HIDDEN" in wm_state:
            hidden = True
        else:
            # Heuristic: dock window is mostly outside its screen.
            total = rw * rh
            hidden = total > 0 and (best_overlap / total) < 0.5
        # Fall back to inferring edge from geometry when strut is absent.
        if edge is None and rw > 0 and rh > 0:
            edge = _edge_from_geometry(rect, (sx, sy, sw, sh))
        s["dock"] = {
            "edge": edge,
            "bounds": [rx, ry, rw, rh] if rw > 0 and rh > 0 else None,
            # EWMH has no autohide atom — the planner can still read
            # ``hidden`` and infer "probably autohidden" if it flips.
            "autohide": None,
            "hidden": hidden,
        }


def _edge_from_geometry(
    rect: tuple[int, int, int, int],
    screen: tuple[int, int, int, int],
) -> str:
    """Infer dock edge when no strut atom is set.

    Used for cases like wlroots-with-Xwayland docks that don't bother
    setting _NET_WM_STRUT. A full-width / full-height panel will tie on
    three edges at distance 0 — we disambiguate using the panel's
    aspect ratio: horizontal panels (wider than tall) belong on top or
    bottom; vertical ones (taller than wide) on left or right.
    """
    rx, ry, rw, rh = rect
    sx, sy, sw, sh = screen
    horizontal_candidates = {
        "top": abs(ry - sy),
        "bottom": abs((sy + sh) - (ry + rh)),
    }
    vertical_candidates = {
        "left": abs(rx - sx),
        "right": abs((sx + sw) - (rx + rw)),
    }
    if rw >= rh:
        return min(horizontal_candidates, key=horizontal_candidates.get)
    return min(vertical_candidates, key=vertical_candidates.get)


# ── Window Management ───────────────────────────────────────────────


# _NET_WM_WINDOW_TYPE atom name → Layer enum (see design_scene_graph.md).
_WM_TYPE_LAYER = {
    "_NET_WM_WINDOW_TYPE_DESKTOP": "background",
    "_NET_WM_WINDOW_TYPE_DOCK": "overlay",
    "_NET_WM_WINDOW_TYPE_TOOLBAR": "floating",
    "_NET_WM_WINDOW_TYPE_MENU": "menu",
    "_NET_WM_WINDOW_TYPE_UTILITY": "floating",
    "_NET_WM_WINDOW_TYPE_SPLASH": "modal",
    "_NET_WM_WINDOW_TYPE_DIALOG": "modal",
    "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU": "menu",
    "_NET_WM_WINDOW_TYPE_POPUP_MENU": "menu",
    "_NET_WM_WINDOW_TYPE_TOOLTIP": "tooltip",
    "_NET_WM_WINDOW_TYPE_NOTIFICATION": "overlay",
    "_NET_WM_WINDOW_TYPE_NORMAL": "normal",
}


async def _xprop_window(wid_hex: str) -> dict:
    """Fetch the per-window xprop bundle needed for scene-graph fields.

    Includes ``WM_TRANSIENT_FOR`` (owner pointer for dialogs/menus —
    closes the modal-child→base-window edge in the scene graph) so we
    don't pay a second xprop fork. Returns {} on failure. Costs one
    fork per window; callers parallelize via asyncio.gather.
    """
    try:
        out = await _run(
            ["xprop", "-id", wid_hex,
             "_NET_WM_WINDOW_TYPE", "_NET_WM_STATE", "_NET_WM_DESKTOP",
             "WM_TRANSIENT_FOR"],
            timeout=2,
        )
    except BackendError:
        return {}

    props: dict[str, str] = {}
    for line in out.splitlines():
        if " = " in line:
            k, _, v = line.partition(" = ")
            props[k.strip()] = v.strip()
        elif ":  not found." in line:
            pass
    return props


def _xprop_to_parent_modality(props: dict) -> tuple[str | None, str | None]:
    """Resolve (parent_window_id, modality) from raw xprop output.

    ``WM_TRANSIENT_FOR(WINDOW)`` carries the owner window id as
    ``window id # 0x...``. ``_NET_WM_STATE_MODAL`` declares this
    window blocks input: with a transient parent it blocks just that
    parent ("window"); without one it's app-modal (EWMH semantics).
    System modality has no EWMH equivalent so we never emit "system"
    from X11.
    """
    transient = props.get("WM_TRANSIENT_FOR(WINDOW)", "")
    parent_id: str | None = None
    if "#" in transient:
        _, _, rhs = transient.partition("#")
        tok = rhs.strip().rstrip(",").strip()
        if tok.startswith("0x"):
            parent_id = tok

    wm_state = props.get("_NET_WM_STATE(ATOM)", "")
    modality: str | None = None
    if "_NET_WM_STATE_MODAL" in wm_state:
        modality = "window" if parent_id else "app"
    return parent_id, modality


def _xprop_to_layer_state_sticky(props: dict) -> tuple[str, str, bool]:
    """Resolve (layer, state, sticky) from raw xprop output."""
    wm_type = props.get("_NET_WM_WINDOW_TYPE(ATOM)", "")
    wm_state = props.get("_NET_WM_STATE(ATOM)", "")
    wm_desk = props.get("_NET_WM_DESKTOP(CARDINAL)", "")

    # _NET_WM_WINDOW_TYPE may list multiple atoms in priority order; the
    # first recognised one wins.
    layer = "normal"
    for tok in wm_type.split(","):
        tok = tok.strip()
        if tok in _WM_TYPE_LAYER:
            layer = _WM_TYPE_LAYER[tok]
            break

    # _NET_WM_STATE overrides for fullscreen / above / below.
    if "_NET_WM_STATE_FULLSCREEN" in wm_state:
        layer = "fullscreen"
    elif "_NET_WM_STATE_ABOVE" in wm_state and layer == "normal":
        layer = "always_on_top"
    elif "_NET_WM_STATE_BELOW" in wm_state and layer == "normal":
        layer = "background"

    state = "normal"
    if "_NET_WM_STATE_HIDDEN" in wm_state:
        state = "minimized"
    elif "_NET_WM_STATE_FULLSCREEN" in wm_state:
        state = "fullscreen"
    elif ("_NET_WM_STATE_MAXIMIZED_VERT" in wm_state
          and "_NET_WM_STATE_MAXIMIZED_HORZ" in wm_state):
        state = "maximized"

    # EWMH _NET_WM_DESKTOP = 0xFFFFFFFF (4294967295) means "all desktops".
    sticky = "4294967295" in wm_desk

    return layer, state, sticky


async def _stacking_order() -> dict[int, int]:
    """Return {window_id_int: z} where z=0 is topmost.

    _NET_CLIENT_LIST_STACKING is bottom-to-top per EWMH; we reverse.
    Empty dict on failure (e.g. no EWMH-compliant WM).
    """
    try:
        out = await _run(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"], timeout=2)
    except BackendError:
        return {}
    # Output: "_NET_CLIENT_LIST_STACKING(WINDOW): window id # 0xa00001, 0xa00002, ..."
    _, _, rhs = out.partition("#")
    ids = []
    for tok in rhs.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.append(int(tok, 16))
        except ValueError:
            continue
    ids.reverse()  # top-first
    return {wid: i for i, wid in enumerate(ids)}


# xwininfo -root -tree line: id, name, size+offset in the parent, then the
# absolute origin. Negative values print as "+-100".
#   0x4200325 "notes.txt": ("xed" "Xed")  650x500+10+40  +50+82
_TREE_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s.*\s\d+x\d+\+-?\d+\+-?\d+\s+\+(-?\d+)\+(-?\d+)\s*$")


def _parse_tree_origins(output: str) -> dict[int, tuple[int, int]]:
    origins = {}
    for line in output.splitlines():
        if m := _TREE_RE.match(line):
            origins[int(m.group(1), 16)] = (int(m.group(2)), int(m.group(3)))
    return origins


async def _absolute_origins() -> dict[int, tuple[int, int]]:
    """Every window's top-left in root coordinates, by id, from one xwininfo.

    wmctrl -G adds the client's offset inside its frame to a position that
    already includes it on some window managers (Cinnamon's Muffin: +10,+40,
    so a window's bounds were that far right and down). Empty when
    xwininfo is missing or fails; wmctrl's position stands then.
    """
    if not shutil.which("xwininfo"):
        return {}
    try:
        return _parse_tree_origins(await _run(["xwininfo", "-root", "-tree"], timeout=2))
    except BackendError as e:
        log.debug("xwininfo -tree failed: %s", e)
        return {}


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows via wmctrl -l -G -p, enriched with scene-graph fields.

    Adds: monitor (hit-test against list_screens), z (from
    _NET_CLIENT_LIST_STACKING), layer/state/sticky (from per-window xprop).
    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Linux X11".
    """
    _require_bin("wmctrl")
    origins_task = asyncio.create_task(_absolute_origins())
    output = await _run(["wmctrl", "-l", "-G", "-p"])
    origins = await origins_task
    # Get the active window ID
    try:
        active_out = await _run(["xdotool", "getactivewindow"])
        active_id = int(active_out.strip())
    except (BackendError, ValueError):
        active_id = None

    # Parse wmctrl output first (cheap), then enrich in parallel.
    raw: list[dict] = []
    for line in output.strip().splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        wid_hex = parts[0]
        desk = int(parts[1])
        if desktop is not None and desk != desktop:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            pid = 0
        x, y, w, h = int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6])
        title = parts[8] if len(parts) > 8 else ""

        proc_name = ""
        if pid > 0:
            try:
                import psutil
                proc_name = psutil.Process(pid).name()
            except psutil.Error:
                pass

        try:
            wid_int = int(wid_hex, 16)
        except ValueError:
            wid_int = 0
        focused = (active_id is not None and wid_int == active_id)
        x, y = origins.get(wid_int, (x, y))

        raw.append({
            "id": wid_hex,
            "wid_int": wid_int,
            "title": title,
            "process_name": proc_name,
            "pid": pid,
            "bounds": [x, y, w, h],
            "desktop": desk,
            "focused": focused,
        })

    # Fetch screens + stacking + per-window xprop in parallel.
    screens_task = asyncio.create_task(_screens_or_none())
    stacking_task = asyncio.create_task(_stacking_order())
    xprop_tasks = [asyncio.create_task(_xprop_window(r["id"])) for r in raw]
    screens = await screens_task
    stacking = await stacking_task
    xprops = await asyncio.gather(*xprop_tasks) if xprop_tasks else []

    windows = []
    for r, props in zip(raw, xprops):
        layer, state, sticky = _xprop_to_layer_state_sticky(props)
        parent_window_id, modality = _xprop_to_parent_modality(props)
        z = stacking.get(r["wid_int"], -1)
        windows.append({
            "id": r["id"],
            "title": r["title"],
            "process_name": r["process_name"],
            "pid": r["pid"],
            "bounds": r["bounds"],
            "state": state,
            "desktop": r["desktop"],
            "focused": r["focused"],
            "monitor": _common.attribute_monitor(r["bounds"], screens),
            "z": z,
            "layer": layer,
            "sticky": sticky,
            "workspace": {"global_index": r["desktop"], "per_output": None},
            "parent_window_id": parent_window_id,
            "modality": modality,
        })
    return windows


async def _screens_or_none() -> list[dict]:
    """Screens for monitor attribution; windows still list without them."""
    try:
        return await list_screens()
    except BackendError as e:
        log.debug("list_windows without screens: %s", e)
        return []


async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus a window by title substring or PID; False when none matches."""
    _require_bin("wmctrl")
    if title:
        return await _run_ok(["wmctrl", "-a", title])
    if pid:
        # Find window by PID and focus it
        windows = await list_windows()
        for w in windows:
            if w["pid"] == pid:
                return await _run_ok(["wmctrl", "-i", "-a", w["id"]])
    return False


# ── Clipboard ───────────────────────────────────────────────────────


async def get_clipboard() -> str:
    """Read clipboard via xclip ("" when it holds no text)."""
    return await xclip_get()


async def set_clipboard(text: str) -> None:
    """Write to clipboard via xclip."""
    await xclip_set(text)


# ── Accessibility (AT-SPI2) ─────────────────────────────────────────

from .linux_ui import ui_act, ui_walk  # noqa: E402  (ui_* tools)


def _atspi_available() -> bool:
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        return True
    except (ImportError, ValueError):
        return False


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402


class X11Desktop(DesktopBackend):
    """X11 on Linux and FreeBSD: wmctrl, xclip, AT-SPI."""

    name = "linux_x11"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
