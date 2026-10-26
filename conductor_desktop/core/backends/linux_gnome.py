"""Linux GNOME Wayland backend — gdbus, wl-clipboard, AT-SPI2.

Target: Dev workstation (Ubuntu GNOME Wayland).
System deps: wl-clipboard, at-spi2-core
Note: Window management is limited on GNOME Wayland without extensions.
"""

from __future__ import annotations

import ast
import json
import logging

from ..errors import BackendError
from ._cli import run as _run
from ._cli import wl_or_x_get, wl_or_x_set

log = logging.getLogger("conductor_desktop.gnome")


# ── Window Management ───────────────────────────────────────────────

# GNOME Wayland window management is limited. We use gdbus to call
# GNOME Shell's eval interface for listing and focusing windows.


async def _gnome_eval(js: str) -> str:
    """Evaluate JavaScript in GNOME Shell via gdbus; the raw GVariant reply.

    Raises BackendError when Shell refuses: GNOME 41+ disables Eval unless
    unsafe mode is on, and answers every call with ``(false, '')``.
    """
    out = await _run([
        "gdbus", "call", "--session",
        "--dest", "org.gnome.Shell",
        "--object-path", "/org/gnome/Shell",
        "--method", "org.gnome.Shell.Eval",
        js,
    ], timeout=5)
    if out.strip().startswith("(false,"):
        detail = out.strip()[len("(false,"):].rstrip(")").strip(" '")
        raise BackendError(
            "GNOME Shell refused org.gnome.Shell.Eval"
            + (f" ({detail})" if detail else "")
            + ". GNOME 41+ allows it only in unsafe mode: see the unsafe_mode "
              "section of https://docs.conductor-mcp.io/platforms/linux/gnome-wayland/.")
    return out


async def _gnome_eval_json(js: str) -> list | dict:
    """``_gnome_eval`` for JS that returns an array or object."""
    parsed = _extract_eval_json(await _gnome_eval(js))
    if parsed is None:
        raise BackendError("GNOME Shell Eval returned no JSON (the script threw?)")
    return parsed


def _extract_eval_json(gdbus_output: str) -> list | dict | None:
    """Parse the JSON payload from an org.gnome.Shell.Eval response.

    gdbus prints the call result in GVariant text format:
    ``(true, '<escaped-string>')`` on success or ``(false, '<error>')``
    when Eval is gated or the JS threw. The inner string is GLib-escaped
    (``\\\\``, ``\\'``); gjs ``Eval`` itself wraps the JS return value in
    ``JSON.stringify`` before handing it off, so the inner string is the
    JSON form of whatever the JS returned. We use ``ast.literal_eval``
    to unescape the GVariant layer (Python's single-quoted string
    grammar accepts the same ``\\\\`` / ``\\'`` escapes) and then
    ``json.loads`` for the JSON. The JS therefore MUST return an array
    or object directly — never call ``JSON.stringify`` from JS, or you
    get a JSON-of-JSON wrapper that parses to a string instead of a
    list/dict.

    Returns None when Eval was refused, the JS threw, or the payload
    isn't parseable JSON.
    """
    s = gdbus_output.strip()
    if not s.startswith("(true,"):
        return None
    body = s[len("(true,"):].lstrip()
    if body.endswith(")"):
        body = body[:-1].rstrip()
    try:
        inner = ast.literal_eval(body)
    except (ValueError, SyntaxError, RecursionError):
        return None
    if not isinstance(inner, str):
        # Eval returned a primitive (number/bool) — not a JSON payload.
        return None
    try:
        return json.loads(inner)
    except (ValueError, RecursionError):
        return None


# Meta.WindowType (mutter) → Layer enum. See design_scene_graph.md.
_META_TYPE_LAYER = {
    0: "normal",         # NORMAL
    1: "background",     # DESKTOP
    2: "overlay",        # DOCK
    3: "modal",          # DIALOG
    4: "modal",          # MODAL_DIALOG
    5: "floating",       # TOOLBAR
    6: "menu",           # MENU
    7: "floating",       # UTILITY
    8: "modal",          # SPLASHSCREEN
    9: "menu",           # DROPDOWN_MENU
    10: "menu",          # POPUP_MENU
    11: "tooltip",       # TOOLTIP
    12: "overlay",       # NOTIFICATION
    13: "menu",          # COMBO
    14: "floating",      # DND
    15: "normal",        # OVERRIDE_OTHER
}


async def list_screens() -> list[dict]:
    """Enumerate monitors via gnome-shell eval.

    Uses ``global.display.get_n_monitors()`` + ``get_monitor_geometry(i)``
    and ``get_primary_monitor()``. See https://docs.conductor-mcp.io/concepts/scene-graph/ →
    "Linux GNOME".
    """
    # Monitor connector name moved between Mutter versions
    # (get_monitor_connector on the Display object, then to MonitorManager,
    # then private). We try each known accessor and fall back to a
    # synthetic name on failure — geometry/index/primary still work.
    # The eval also gathers DOCK-typed Meta windows so we can attribute
    # each panel to a monitor in one round-trip — a second org.gnome.Shell.Eval
    # call costs another gdbus fork and we want list_screens cheap. The
    # top-panel itself is a Clutter actor (Main.panel), not a Meta window,
    # so it never appears in this list — that's fine, its reservation
    # already shows up via getWorkAreaForMonitor.
    js = """
    (function() {
      const d = global.display;
      const n = d.get_n_monitors();
      const primary = d.get_primary_monitor();
      let Main = null;
      try { Main = imports.ui.main; } catch (e) {}
      const docks = global.get_window_actors()
        .map(a => a.meta_window)
        .filter(w => {
          try { return w.get_window_type() === 2; } catch (e) { return false; }
        })
        .map(w => {
          let r;
          try { r = w.get_frame_rect(); }
          catch (e) { r = {x:0,y:0,width:0,height:0}; }
          let mon = -1;
          try { mon = w.get_monitor(); } catch (e) {}
          return { x: r.x, y: r.y, w: r.width, h: r.height, mon: mon };
        });
      const out = [];
      for (let i = 0; i < n; i++) {
        const g = d.get_monitor_geometry(i);
        let name = 'Monitor ' + i;
        try {
          if (typeof d.get_monitor_connector === 'function') {
            const c = d.get_monitor_connector(i);
            if (c) name = c;
          } else if (global.backend && global.backend.get_monitor_manager) {
            const mm = global.backend.get_monitor_manager();
            if (mm && typeof mm.get_monitor_connectors === 'function') {
              const cs = mm.get_monitor_connectors();
              if (cs && cs[i]) name = cs[i];
            }
          }
        } catch (e) { /* swallow API-shape drift */ }
        let scale = 1.0;
        try {
          if (typeof d.get_monitor_scale === 'function') {
            scale = d.get_monitor_scale(i);
          }
        } catch (e) {}
        let work = null;
        try {
          if (Main && Main.layoutManager
              && typeof Main.layoutManager.getWorkAreaForMonitor === 'function') {
            const wa = Main.layoutManager.getWorkAreaForMonitor(i);
            work = { x: wa.x, y: wa.y, w: wa.width, h: wa.height };
          }
        } catch (e) {}
        let best = null;
        let bestOverlap = -1;
        for (const dk of docks) {
          if (dk.mon === i) { best = dk; break; }
          const ix = Math.max(0, Math.min(dk.x+dk.w, g.x+g.width) - Math.max(dk.x, g.x));
          const iy = Math.max(0, Math.min(dk.y+dk.h, g.y+g.height) - Math.max(dk.y, g.y));
          const area = ix * iy;
          if (area > bestOverlap) { best = dk; bestOverlap = area; }
        }
        let dock = null;
        if (best && best.w > 0 && best.h > 0) {
          const diffs = {
            left: Math.abs(best.x - g.x),
            top: Math.abs(best.y - g.y),
            right: Math.abs((g.x + g.width) - (best.x + best.w)),
            bottom: Math.abs((g.y + g.height) - (best.y + best.h)),
          };
          let edge = 'bottom';
          let minDiff = Infinity;
          for (const k of ['top','bottom','left','right']) {
            if (diffs[k] < minDiff) { minDiff = diffs[k]; edge = k; }
          }
          let reserved = null;
          if (work) {
            reserved = (edge === 'top') ? (work.y - g.y)
              : (edge === 'bottom') ? ((g.y + g.height) - (work.y + work.h))
              : (edge === 'left') ? (work.x - g.x)
              : ((g.x + g.width) - (work.x + work.w));
          }
          const ox = Math.max(0, Math.min(best.x+best.w, g.x+g.width) - Math.max(best.x, g.x));
          const oy = Math.max(0, Math.min(best.y+best.h, g.y+g.height) - Math.max(best.y, g.y));
          const onArea = ox * oy;
          const totArea = best.w * best.h;
          const offscreen = totArea > 0 && (onArea / totArea) < 0.5;
          let autohide = null;
          let hidden = null;
          if (reserved !== null) {
            // Dock exists but reserves no work area → autohide active.
            autohide = (reserved <= 1);
            hidden = autohide && offscreen;
          } else {
            // No work-area accessor available; fall back to geometry only.
            hidden = offscreen;
          }
          dock = {
            edge: edge,
            bounds: [best.x, best.y, best.w, best.h],
            autohide: autohide,
            hidden: hidden,
          };
        }
        out.push({
          index: i,
          name: name,
          bounds: [g.x, g.y, g.width, g.height],
          scale: scale,
          primary: i === primary,
          dock: dock,
        });
      }
      return out;
    })()
    """
    screens = await _gnome_eval_json(js)
    if not isinstance(screens, list):
        raise BackendError(f"GNOME Shell returned {type(screens).__name__}, not a screen list")
    return screens


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows via GNOME Shell eval, enriched with scene-graph fields.

    ``global.get_window_actors()`` returns bottom-to-top — we reverse so
    z=0 is topmost. Adds: monitor, z, layer (from Meta.WindowType), sticky
    (is_on_all_workspaces), workspace ({global_index, per_output}).
    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Linux GNOME".
    """
    # Do NOT wrap in JSON.stringify here — gjs Eval already JSON.stringify's
    # the return value, so wrapping again produces a JSON-of-JSON string
    # that the parser would have to unwrap twice. See _extract_eval_json.
    # transient_for is read with a guarded try/catch because the
    # accessor has shifted between Mutter versions (3.x had it on
    # MetaWindow directly; some intermediate releases moved it under
    # a private struct). When unavailable we just emit null and the
    # scene graph carries no parent edge for that platform.
    js = """
    global.get_window_actors()
      .map(a => a.meta_window)
      .filter(w => !w.is_skip_taskbar())
      .map(w => {
        const r = w.get_frame_rect();
        const ws = w.get_workspace();
        let parent_seq = null;
        try {
          const tf = (typeof w.get_transient_for === 'function')
            ? w.get_transient_for()
            : w.transient_for;
          if (tf) parent_seq = String(tf.get_stable_sequence());
        } catch (e) { /* Mutter API shape drift */ }
        return {
          id: String(w.get_stable_sequence()),
          title: w.get_title() || '',
          process_name: w.get_wm_class() || '',
          pid: w.get_pid(),
          bounds: [r.x, r.y, r.width, r.height],
          state: w.maximized_horizontally && w.maximized_vertically ? 'maximized'
            : w.minimized ? 'minimized'
            : w.is_fullscreen() ? 'fullscreen' : 'normal',
          desktop: ws ? ws.index() : 0,
          focused: w.has_focus(),
          monitor: w.get_monitor(),
          window_type: w.get_window_type(),
          sticky: w.is_on_all_workspaces(),
          parent_window_id: parent_seq,
        };
      })
    """
    parsed = await _gnome_eval_json(js)
    if not isinstance(parsed, list):
        raise BackendError(f"GNOME Shell returned {type(parsed).__name__}, not a window list")
    # get_window_actors is bottom-to-top per mutter; flip so z=0 is topmost.
    parsed.reverse()
    windows = []
    for i, w in enumerate(parsed):
        wtype = int(w.get("window_type", 0))
        layer = _META_TYPE_LAYER.get(wtype, "normal")
        if w.get("state") == "fullscreen":
            layer = "fullscreen"
        parent_window_id = w.get("parent_window_id")
        # Mutter window types 3 (DIALOG), 4 (MODAL_DIALOG),
        # 8 (SPLASHSCREEN) imply modal-ish behaviour. Mutter
        # doesn't expose an explicit ``is_modal``; we approximate
        # from window_type + transient_for presence:
        #   - MODAL_DIALOG → "window" if attached, else "app"
        #   - DIALOG → "window" if attached (sheet semantics), else None
        #             (an unparented "dialog" is typically just a
        #              tear-off / utility, not actually blocking)
        modality: str | None = None
        if wtype == 4:
            modality = "window" if parent_window_id else "app"
        elif wtype == 3 and parent_window_id:
            modality = "window"
        windows.append({
            "id": w["id"],
            "title": w["title"],
            "process_name": w["process_name"],
            "pid": w["pid"],
            "bounds": w["bounds"],
            "state": w["state"],
            "desktop": w["desktop"],
            "focused": w["focused"],
            "monitor": int(w.get("monitor", -1)),
            "z": i,
            "layer": layer,
            "sticky": bool(w.get("sticky", False)),
            "workspace": {"global_index": w["desktop"], "per_output": None},
            "parent_window_id": parent_window_id,
            "modality": modality,
        })
    if desktop is not None:
        windows = [w for w in windows if w.get("desktop") == desktop]
    return windows



async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus a window via GNOME Shell eval."""
    if title:
        needle = json.dumps(title.lower())
        js = f"""
        (function() {{
          const wins = global.get_window_actors()
            .map(a => a.meta_window)
            .filter(w => w.get_title() && w.get_title().toLowerCase().includes({needle}));
          if (wins.length > 0) {{
            wins[0].activate(global.get_current_time());
            return 'ok';
          }}
          return 'not found';
        }})()
        """
        return "ok" in await _gnome_eval(js)

    if pid:
        js = f"""
        (function() {{
          const wins = global.get_window_actors()
            .map(a => a.meta_window)
            .filter(w => w.get_pid() === {int(pid)});
          if (wins.length > 0) {{
            wins[0].activate(global.get_current_time());
            return 'ok';
          }}
          return 'not found';
        }})()
        """
        return "ok" in await _gnome_eval(js)

    return False


# ── Clipboard ───────────────────────────────────────────────────────
# Same as wlroots — wl-copy / wl-paste


async def get_clipboard() -> str:
    """Read the clipboard ("" when it holds no text)."""
    return await wl_or_x_get()


async def set_clipboard(text: str) -> None:
    await wl_or_x_set(text)


# ── Accessibility (AT-SPI2 — shared with X11) ───────────────────────


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402
from .linux_ui import ui_act, ui_walk  # noqa: E402


class GnomeDesktop(DesktopBackend):
    """GNOME on Wayland: Shell Eval over gdbus, wl-clipboard, AT-SPI."""

    name = "linux_gnome"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
