"""macOS backend — pyobjc (CGWindow, AXUIElement, NSPasteboard).

Target: macOS (Intel and Apple silicon).
Requires: Accessibility permission in System Settings > Privacy.
"""

from __future__ import annotations

import asyncio
import logging

from ..errors import BackendError, best_effort, reason
from . import _common

log = logging.getLogger("conductor_desktop.macos")


# ── Screen enumeration ──────────────────────────────────────────────


async def list_screens() -> list[dict]:
    """Enumerate NSScreens. See https://docs.conductor-mcp.io/concepts/scene-graph/ (Screen node).

    Coordinates are returned in the Quartz top-left origin space (same
    convention as `kCGWindowBounds`), so hit-testing window bounds against
    these screen rects works without further conversion. `NSScreen.frame()`
    uses bottom-left origin; we flip to match.

    Fields: index, name, bounds, scale, primary. Per-screen
    `current_space` / `focused_window` / `spaces` are added by the scene
    graph composer.
    """
    try:
        from AppKit import NSScreen
    except ImportError as e:
        raise BackendError("pyobjc-framework-Cocoa is not installed (run uv sync)") from e

    screens = NSScreen.screens()
    if not screens:
        raise BackendError("macOS reports no screens (a headless session?)")

    # Convert NSScreen (bottom-left origin) → Quartz (top-left origin).
    # The total virtual height is the max-y of any screen's top edge.
    total_h = max(int(s.frame().origin.y + s.frame().size.height) for s in screens)
    main = NSScreen.mainScreen()
    primary_id = id(main) if main is not None else None

    out = []
    for i, s in enumerate(screens):
        f = s.frame()
        x = int(f.origin.x)
        w = int(f.size.width)
        h = int(f.size.height)
        # Flip y from NSScreen (bottom-left) to Quartz (top-left).
        y = int(total_h - (f.origin.y + h))
        try:
            name = str(s.localizedName())
        except Exception:
            log.debug("reading screen %d name failed", i, exc_info=True)
            name = f"Display {i}"
        try:
            scale = float(s.backingScaleFactor())
        except Exception:
            log.debug("reading screen %d scale failed", i, exc_info=True)
            scale = 1.0
        # NSScreen.visibleFrame is frame minus menubar and the dock's
        # currently-reserved strip; we stash both rects so the dock
        # detector can derive thickness without re-querying AppKit.
        try:
            vf = s.visibleFrame()
            vis = (int(vf.origin.x), int(vf.origin.y),
                   int(vf.size.width), int(vf.size.height))
        except Exception:
            log.debug("reading screen %d visible frame failed", i, exc_info=True)
            vis = None
        screen_entry = {
            "index": i,
            "name": name or f"Display {i}",
            "bounds": [x, y, w, h],
            "scale": scale,
            "primary": id(s) == primary_id,
            # Carry-fields stripped before return — dock detector only.
            "_ns_frame": (int(f.origin.x), int(f.origin.y),
                          int(f.size.width), int(f.size.height)),
            "_ns_visible": vis,
        }
        out.append(screen_entry)

    # Attach per-screen dock info from the NSScreen.visibleFrame delta —
    # CGWindow's Dock-owner entries don't carry the visible dock strip's
    # geometry on modern macOS (the bounds returned are the fullscreen
    # compositor canvas, not the dock rect itself).
    try:
        _attach_dock_info(out)
    except Exception as e:
        log.debug("dock detection failed: %s", e, exc_info=True)
    for s in out:
        s.pop("_ns_frame", None)
        s.pop("_ns_visible", None)
    return out


_DOCK_ORIENTATIONS = {"bottom", "left", "right"}


def _read_dock_defaults() -> tuple[str, bool]:
    """Return ``(orientation, autohide)`` from ``com.apple.dock`` prefs.

    Two synchronous reads of CFPreferences via pyobjc — much cheaper
    than forking ``defaults read`` twice. macOS doesn't support a top
    orientation; if some future override sets one we still pass it
    through as the literal string.
    """
    orientation = "bottom"
    autohide = False
    with best_effort("reading dock prefs", log):
        from Foundation import CFPreferencesCopyAppValue
        o = CFPreferencesCopyAppValue("orientation", "com.apple.dock")
        if o:
            orientation = str(o)
        a = CFPreferencesCopyAppValue("autohide", "com.apple.dock")
        if a is not None:
            autohide = bool(a)
    return orientation, autohide


def _attach_dock_info(screens: list[dict]) -> None:
    """Mutate ``screens`` in place, adding a ``dock`` dict per screen.

    Modern macOS (Ventura+) doesn't surface the dock strip as its own
    CGWindow — the "Dock"-owned layer-20 entry covers the full screen
    canvas, with the visible strip composited inside. We instead read
    ``NSScreen.visibleFrame`` and subtract it from ``NSScreen.frame``:
    the delta on the orientation edge is the dock's reserved thickness
    (in pt, NSScreen bottom-left coords), and the screen carrying a
    non-zero delta is the one the dock is currently on. ``hidden``
    follows the same work-area semantics as the Windows backend:
    ``autohide AND dock_thickness == 0`` — the dock isn't claiming
    real estate on this display right now.
    """
    orientation, autohide = _read_dock_defaults()
    if orientation not in _DOCK_ORIENTATIONS:
        orientation = "bottom"

    # Virtual-space top-edge in NSScreen coords — used to flip the
    # dock rect back into Quartz top-left for ``bounds``. Matches the
    # same total_h pivot used in ``list_screens``.
    try:
        from AppKit import NSScreen
        ns_screens = NSScreen.screens()
        total_h = max(int(s.frame().origin.y + s.frame().size.height)
                      for s in ns_screens) if ns_screens else 0
    except Exception:
        log.debug("reading screen frames for the dock failed", exc_info=True)
        total_h = 0

    # macOS menu-bar height in points; default for Big Sur+. We only
    # need a coarse guess to avoid mis-attributing the menubar's
    # 24-25 pt strip as a dock when orientation is "bottom".
    _MENUBAR_PT = 30

    for s in screens:
        frame = s.get("_ns_frame")
        visible = s.get("_ns_visible")
        if frame is None or visible is None:
            continue
        fx, fy, fw, fh = frame
        vx, vy, vw, vh = visible

        # Compute reservation thickness on each edge in NS coords.
        # NS uses bottom-left origin, so:
        #   bottom strip  = vy - fy
        #   top strip     = (fy + fh) - (vy + vh)
        #   left strip    = vx - fx
        #   right strip   = (fx + fw) - (vx + vw)
        if orientation == "bottom":
            thickness = max(0, vy - fy)
            ns_strip = (fx, fy, fw, thickness) if thickness else None
        elif orientation == "left":
            thickness = max(0, vx - fx)
            ns_strip = (fx, fy, thickness, fh) if thickness else None
        elif orientation == "right":
            thickness = max(0, (fx + fw) - (vx + vw))
            x0 = fx + fw - thickness
            ns_strip = (x0, fy, thickness, fh) if thickness else None
        else:
            thickness = 0
            ns_strip = None

        if ns_strip is not None and thickness > 0:
            sx_, sy_, sw_, sh_ = ns_strip
            # Flip NSScreen bottom-left → Quartz top-left for the wire
            # bounds (matches the screen.bounds origin convention).
            qy = total_h - (sy_ + sh_) if total_h else sy_
            bounds: list[int] | None = [sx_, qy, sw_, sh_]
        else:
            bounds = None

        # ``hidden``: when autohide is on and the dock isn't currently
        # reserving any work area, the bar is parked off-screen. When
        # autohide is off, thickness is always >0 on the dock's screen
        # so this evaluates False — matching Windows' rcWork semantics.
        # On the screens that *don't* host the dock, thickness is also
        # 0, but we shouldn't claim "hidden" there — only attach the
        # dock to a screen that either currently shows it (thickness>0)
        # or, in autohide-collapsed mode, the primary screen.
        if thickness > 0:
            s["dock"] = {
                "edge": orientation,
                "bounds": bounds,
                "autohide": autohide,
                "hidden": False,
            }
        elif autohide and s.get("primary"):
            # Dock is hidden somewhere — best guess is the primary
            # display, since macOS docks the bar there by default and
            # the user has to drag a window to another screen to move it.
            s["dock"] = {
                "edge": orientation,
                "bounds": None,
                "autohide": True,
                "hidden": True,
            }

    # Suppress mistaken menubar-only deltas: if every screen got a
    # thickness < menubar_pt and orientation is "bottom", we likely
    # measured the menu bar — drop the dock entry on those screens.
    if orientation == "bottom":
        for s in screens:
            dk = s.get("dock")
            if not dk or not dk.get("bounds"):
                continue
            _, _, _, h = dk["bounds"]
            if h <= _MENUBAR_PT // 2:
                s["dock"] = None


# ── Window Management ───────────────────────────────────────────────


def _map_layer(cg_layer: int) -> str:
    """Map kCGWindowLayer integer to the Layer enum in design_scene_graph.md.

    Apple doesn't expose stable enum names for every band; we use
    practical groupings from NSWindowLevel constants
    (NSNormalWindowLevel=0, NSFloatingWindowLevel=3,
    NSModalPanelWindowLevel=8, NSMainMenuWindowLevel=24, etc.).
    """
    if cg_layer < 0:
        return "background"
    if cg_layer == 0:
        return "normal"
    if cg_layer < 8:
        return "floating"
    if cg_layer < 20:
        return "modal"
    if cg_layer < 24:
        return "overlay"
    if cg_layer < 100:
        return "menu"
    return "always_on_top"


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows via CGWindowListCopyWindowInfo.

    CGWindowListCopyWindowInfo with kCGWindowListOptionOnScreenOnly returns
    windows in front-to-back z-order. We preserve that order and emit
    a `z` index (0 = topmost), plus a `layer` band derived from
    `kCGWindowLayer` and a `monitor` index from hit-testing the window
    center against `list_screens()`.

    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "macOS (backends/macos.py)".
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError as e:
        raise BackendError("pyobjc-framework-Quartz is not installed (run uv sync)") from e

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
    if window_list is None:
        raise BackendError("CGWindowListCopyWindowInfo returned nothing "
                           "(Screen Recording permission missing?)")

    try:
        screens = await list_screens()
    except BackendError as e:  # windows still list; they just get no monitor
        log.debug("list_windows without screens: %s", e)
        screens = []

    windows = []
    z_index = 0
    for w in window_list or []:
        owner = w.get("kCGWindowOwnerName", "")
        name = w.get("kCGWindowName", "")
        pid = w.get("kCGWindowOwnerPID", 0)
        bounds = w.get("kCGWindowBounds", {})
        cg_layer = int(w.get("kCGWindowLayer", 0) or 0)

        # Skip windows without names (menu bar items, etc.)
        if not name and not owner:
            continue

        wbounds = [
            int(bounds.get("X", 0)),
            int(bounds.get("Y", 0)),
            int(bounds.get("Width", 0)),
            int(bounds.get("Height", 0)),
        ]

        # NSModalPanelWindowLevel == 8: app-modal panels (NSAlert
        # runModal, modal sheets when not sheet-attached). Sheet-style
        # window modality lives below 8 and is indistinguishable from
        # normal floating without an AX query — left as None for v1.
        # parent_window_id requires AX kAXParentAttribute per window,
        # which is too expensive to do unconditionally; the scene graph
        # carries None on macOS until we wire a focused-only AX lookup.
        modality = "app" if cg_layer == 8 else None

        windows.append({
            "id": str(w.get("kCGWindowNumber", 0)),
            "title": name or owner,
            "process_name": owner,
            "pid": pid,
            "bounds": wbounds,
            "state": "normal",
            "desktop": 0,
            "focused": False,  # will be set below
            "monitor": _common.attribute_monitor(wbounds, screens),
            "z": z_index,
            "layer": _map_layer(cg_layer),
            "sticky": False,  # not detected: needs the private CGS Spaces API
            "workspace": {"global_index": 0, "per_output": None},
            "parent_window_id": None,
            "modality": modality,
        })
        z_index += 1

    # Mark focused window
    with best_effort("finding the focused window", log):
        from AppKit import NSWorkspace
        active_app = NSWorkspace.sharedWorkspace().activeApplication()
        active_pid = active_app.get("NSApplicationProcessIdentifier", 0)
        for w in windows:
            if w["pid"] == active_pid:
                w["focused"] = True
                break

    return windows


async def _activate_app(app) -> bool:
    """Try multiple activation strategies in order.

    The classic `activateWithOptions_(NSApplicationActivateIgnoringOtherApps)`
    is effectively deprecated on macOS 14 (Sonoma) and later — Apple
    tightened activation so that apps must cooperate ("yield activation")
    for another process to bring them forward. That flag now silently
    returns False from many caller contexts. The AppleScript fallback
    still works because osascript has broader TCC-granted privileges.
    """
    from AppKit import NSApplicationActivateIgnoringOtherApps
    # 1. New API: plain activate() with no flags (macOS 14+).
    with best_effort("NSRunningApplication.activate", log):
        if hasattr(app, "activate") and app.activate():
            return True
    # 2. Legacy flag — sometimes still effective on older macOS.
    with best_effort("activateWithOptions_", log):
        if app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            return True
    # 3. AppleScript fallback — reliable when Accessibility is granted.
    name = app.localizedName() or ""
    if name:
        try:
            script = "on run argv\ntell application (item 1 of argv) to activate\nend run"
            if await _osascript(script, name, timeout=3):
                return True
        except (OSError, TimeoutError):
            pass
    return False


async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus a window or app by title substring or by PID.

    Strategy:
    1. If `pid` given: activate that running application directly.
    2. If `title` given: collect candidate apps whose localizedName contains
       the title, PLUS apps owning a window whose title contains it.
       Try activating each until one succeeds. Window-title matching is
       what makes "Cloudflare Dashboard - Google Chrome" findable by tab
       title, not just by app name ("Google Chrome").

    Returns False when nothing matches or no match would activate. Raises
    BackendError if pyobjc isn't installed.
    """
    try:
        from AppKit import NSRunningApplication, NSWorkspace
    except ImportError as e:
        raise BackendError("pyobjc-framework-Cocoa is not installed (run uv sync)") from e

    # Strategy 1: direct PID activation.
    if pid:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return False
        return await _activate_app(app)

    if not title:
        return False

    needle = title.lower()
    candidates = []
    seen_pids: set[int] = set()

    # Strategy 2a: match app localizedName.
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        try:
            app_pid = int(app.processIdentifier())
        except Exception:
            log.debug("reading an app's pid failed", exc_info=True)
            continue
        if app_pid in seen_pids:
            continue
        name = app.localizedName() or ""
        if needle in name.lower():
            candidates.append(app)
            seen_pids.add(app_pid)

    # Strategy 2b: match window title (kCGWindowName). Covers the case
    # where the user searches for a tab title or document name.
    try:
        windows = await list_windows()
    except BackendError as e:  # app names still match
        log.debug("focus_window without window titles: %s", e)
        windows = []
    for w in windows:
        win_title = (w.get("title") or "").lower()
        owner = (w.get("process_name") or "").lower()
        if needle in win_title or needle in owner:
            wpid = int(w.get("pid", 0) or 0)
            if wpid and wpid not in seen_pids:
                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(wpid)
                if app is not None:
                    candidates.append(app)
                    seen_pids.add(wpid)

    # Try each candidate until one activates successfully.
    for app in candidates:
        if await _activate_app(app):
            return True

    return False


async def _osascript(script: str, *args: str, timeout: float = 5) -> bool:
    """Run an AppleScript, passing ``args`` as ``argv`` rather than splicing them in.

    Values reach the script through ``on run argv`` so a window title or app
    name can never close a string literal and run as AppleScript.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()  # don't leave a hung osascript behind
        await proc.wait()
        raise
    return proc.returncode == 0


# ── Clipboard ───────────────────────────────────────────────────────


async def get_clipboard() -> str:
    """Read clipboard via pbpaste ("" when it holds no text)."""
    return (await _pb("pbpaste")).decode("utf-8", errors="replace")


async def set_clipboard(text: str) -> None:
    """Write to clipboard via pbcopy."""
    await _pb("pbcopy", text.encode("utf-8"))


async def _pb(tool: str, stdin: bytes | None = None) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            tool,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=3)
    except (OSError, TimeoutError) as e:
        raise BackendError(f"{tool} failed: {reason(e)}") from e
    if proc.returncode != 0:
        raise BackendError(f"{tool} exited {proc.returncode}: "
                           f"{stderr.decode(errors='replace').strip()}")
    return stdout


# ── Accessibility ───────────────────────────────────────────────────


def _find_ax_window(title: str, create_app, copy_attr, workspace):
    """The AXWindow across all running apps whose AXTitle equals `title`
    (case-insensitive), else the first whose title contains it.

    Exact first: a Finder window named "alice" otherwise lost to a Terminal
    window titled "alice@devbox: ~" that happened to come earlier.
    Only reached when no app *name* matched, so it can't shadow the app-level
    path that extract_table depends on.
    """
    needle = title.lower()
    partial = None
    for app in workspace.sharedWorkspace().runningApplications():
        try:
            app_ref = create_app(app.processIdentifier())
            err, windows = copy_attr(app_ref, "AXWindows", None)
            if not windows:
                continue
            for win in windows:
                err, wt = copy_attr(win, "AXTitle", None)
                wt = str(wt).lower() if wt else ""
                if wt == needle:
                    return win
                if partial is None and needle in wt:
                    partial = win
        except Exception:
            log.debug("reading an app's AX windows failed", exc_info=True)
            continue
    return partial


async def extract_table(title: str | None = None, max_rows: int = 100):
    """macOS StructuredProvider: walk AX, extract the salient table, classify
    blindness. Returns a protocols.TableExtract. matrix §4, handoff §2/§4.

    The same walk the ``ui_*`` tools use, over ``_AXTableSource`` — which
    reads a table's AXRows rather than its AXChildren, so off-screen rows come
    for free on a non-virtualized NSTableView (verified: 747 rows, row 746 @
    y=18034). Each cell still costs synchronous cross-process AX calls, so a
    full 747-row read took ~53s: max_rows windows it to a viewport+buffer, and
    a live-updating table should be frozen before a full snapshot (its AXRows
    refs go stale mid-walk → off-screen rows read as unknown). See handoff §6.
    """
    from ..protocols import FAILURE_A11Y_COLD, FAILURE_CANVAS_BLACKOUT
    from ..structured import extract_table as _extract

    # max_children caps rows at the outline AND cells-per-row; keep headroom for
    # wide grids. Deep tree is fine on macOS (no Linux-style hang hazard).
    read = await ui_walk(title, depth=12, max_children=max(max_rows, 64),
                         timeout=_TABLE_TIMEOUT, source=_AXTableSource)
    root = read.window if read is not None else None
    result = _extract(root, "ax")
    if result.rows > 0 or root is None:
        return result

    # 0 cells found — distinguish "blind" from "genuinely no table" (matrix §7).
    total, scroll_areas, containers = _ax_topology(root)
    if scroll_areas and containers == 0:
        # Custom-drawn grid inside an AXScrollArea, no AXTable/AXCell (Numbers).
        result.failure = FAILURE_CANVAS_BLACKOUT
    elif total < 80:
        # Tree barely materialized — Chromium a11y not activated until an AT
        # attaches (Slack/Electron lazy-activation hazard).
        result.failure = FAILURE_A11Y_COLD
    return result


def _ax_topology(root) -> tuple[int, int, int]:
    """(total_nodes, scroll_areas, table_containers) for failure heuristics."""
    from ..ui.model import iter_nodes
    total = scroll = cont = 0
    for node in iter_nodes(root):
        total += 1
        scroll += node.role == "scroll_area"
        cont += node.role in ("table", "tree")
    return total, scroll, cont


def _ax_range(copy_attr, element, attr) -> tuple[int, int] | None:
    """Read an AX*IndexRange → (location, length), or None.

    Cells wrap their index range in an opaque AXValue (kAXValueCFRangeType);
    AXValueGetValue unwraps it to a (location, length) tuple. matrix §4
    (Activity Monitor: AXRowIndexRange/AXColumnIndexRange).
    """
    try:
        err, v = copy_attr(element, attr, None)
        if v is None:
            return None
        from ApplicationServices import AXValueGetValue, kAXValueCFRangeType
        ok, rng = AXValueGetValue(v, kAXValueCFRangeType, None)
        if ok and rng is not None:
            # pyobjc returns CFRange as a (location, length) tuple
            return (int(rng[0]), int(rng[1]))
    except Exception:
        log.debug("reading %s failed", attr, exc_info=True)
    return None


def _ax_actions(element) -> list[str]:
    """Action names supported by this element (e.g. AXPress) → act channel."""
    with best_effort("reading AX action names", log):
        from ApplicationServices import AXUIElementCopyActionNames
        err, names = AXUIElementCopyActionNames(element, None)
        if names:
            return [str(n) for n in names]
    return []


_AX_VALUE_MAX = 500


def _ax_string_prefix(element, limit: int) -> str:
    """First `limit` chars of a text element via AXStringForRange, or ""."""
    try:
        from ApplicationServices import (
            AXUIElementCopyParameterizedAttributeValue,
            AXValueCreate,
            kAXValueCFRangeType,
        )
        rng = AXValueCreate(kAXValueCFRangeType, (0, limit))
        err, s = AXUIElementCopyParameterizedAttributeValue(
            element, "AXStringForRange", rng, None,
        )
        return str(s) if err == 0 and s else ""
    except Exception:
        log.debug("reading AXStringForRange failed", exc_info=True)
        return ""


# ── ui_* modality (https://docs.conductor-mcp.io/concepts/accessibility/) ──────────────────────

# Read in one AXUIElementCopyMultipleAttributeValues call per node. AXValue is
# deliberately absent: it can be unbounded text and PyObjC leaks every copy
# (see _AXSource._value), so values are read separately and length-checked.
_UI_ATTRS = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXPlaceholderValue",
    "AXIdentifier", "AXEnabled", "AXFocused", "AXSelected", "AXExpanded",
    "AXDisclosing", "AXPosition", "AXSize", "AXNumberOfCharacters",
]
_UI_VALUE_ROLES = frozenset({
    "AXTextField", "AXSearchField", "AXTextArea", "AXStaticText", "AXComboBox",
    "AXPopUpButton", "AXCheckBox", "AXRadioButton", "AXSlider", "AXIncrementor",
    "AXProgressIndicator", "AXLevelIndicator",
})
_UI_SETTABLE_ROLES = frozenset({
    "AXTextField", "AXSearchField", "AXTextArea", "AXComboBox", "AXSlider",
})
_UI_NAME_MAX = 500


def _ax_plain(v):
    """Unwrap one CopyMultipleAttributeValues entry: None for a missing
    attribute (an AXValue of kAXValueAXErrorType), a tuple for a point or
    size, else the value itself."""
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    try:
        from ApplicationServices import (
            AXValueGetType,
            AXValueGetValue,
            kAXValueAXErrorType,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        t = AXValueGetType(v)
    except Exception:
        log.debug("AXValueGetType failed", exc_info=True)
        return v
    if t == kAXValueAXErrorType:
        return None
    if t == kAXValueCGPointType:
        ok, pt = AXValueGetValue(v, t, None)
        return (float(pt.x), float(pt.y)) if ok else None
    if t == kAXValueCGSizeType:
        ok, sz = AXValueGetValue(v, t, None)
        return (float(sz.width), float(sz.height)) if ok else None
    return v


def _ax_flag(v) -> bool | None:
    return bool(v) if isinstance(v, (bool, int)) else None


class _AXSource:
    """``ElementSource`` over AXUIElements for ``core.ui.walk``."""

    def __init__(self) -> None:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCopyMultipleAttributeValues,
            AXUIElementGetAttributeValueCount,
            AXUIElementIsAttributeSettable,
        )

        from ..ui import ax
        self._copy = AXUIElementCopyAttributeValue
        self._copy_multi = AXUIElementCopyMultipleAttributeValues
        self._count = AXUIElementGetAttributeValueCount
        self._settable = AXUIElementIsAttributeSettable
        self.normalize = ax.normalize

    def describe(self, element):
        from ..ui.ax import carries_state_value
        from ..ui.model import RawNode
        try:
            err, vals = self._copy_multi(element, _UI_ATTRS, 0, None)
        except Exception:
            log.debug("reading AX attributes failed", exc_info=True)
            vals = None
        a = dict(zip(_UI_ATTRS, (_ax_plain(v) for v in (vals or []))))
        role = str(a.get("AXRole") or "unknown")
        subrole = str(a.get("AXSubrole") or "")
        protected = subrole == "AXSecureTextField"
        name = a.get("AXTitle") or a.get("AXDescription") or a.get("AXPlaceholderValue") or ""

        value = ""
        checked = None
        if role in _UI_VALUE_ROLES and not protected:
            value = self._value(element, a.get("AXNumberOfCharacters"))
            if carries_state_value(role, subrole):
                checked = int(value) if value.isdigit() else None
                value = ""

        pos, size = a.get("AXPosition"), a.get("AXSize")
        bounds = None
        if isinstance(pos, tuple) and isinstance(size, tuple):
            bounds = (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))

        settable = role in _UI_SETTABLE_ROLES and self._can_set(element, "AXValue")

        row_range = col_range = None
        if "Cell" in role:
            # Only cells carry them, and only a cell pays the two extra round
            # trips. Rows don't: verified on an AXOutline whose 100 AXRows
            # report neither range while all 600 of their AXCells do.
            row_range = _ax_range(self._copy, element, "AXRowIndexRange")
            col_range = _ax_range(self._copy, element, "AXColumnIndexRange")

        expanded = _ax_flag(a.get("AXExpanded"))
        expandable = None
        if expanded is None:
            expanded = _ax_flag(a.get("AXDisclosing"))
            if expanded is not None and role == "AXRow":
                # Leaf rows report AXDisclosing too, but only real parents can set it.
                expandable = self._can_set(element, "AXDisclosing")
        return RawNode(
            native_role=role, subrole=subrole, name=str(name)[:_UI_NAME_MAX],
            value=value, id=str(a.get("AXIdentifier") or ""),
            actions=_ax_actions(element), bounds=bounds,
            enabled=_ax_flag(a.get("AXEnabled")), focused=_ax_flag(a.get("AXFocused")),
            selected=_ax_flag(a.get("AXSelected")), expanded=expanded,
            value_settable=settable, protected=protected, checked=checked,
            expandable=expandable, row_range=row_range, col_range=col_range,
        )

    def _can_set(self, element, attr: str) -> bool:
        try:
            err, s = self._settable(element, attr, None)
            return err == 0 and bool(s)
        except Exception:
            log.debug("checking %s settable failed", attr, exc_info=True)
            return False

    def _value(self, element, n_chars) -> str:
        if isinstance(n_chars, int) and n_chars > _AX_VALUE_MAX:
            return _ax_string_prefix(element, _AX_VALUE_MAX)
        try:
            err, v = self._copy(element, "AXValue", None)
        except Exception:
            log.debug("reading AXValue failed", exc_info=True)
            return ""
        v = _ax_plain(v)
        if v is None or isinstance(v, tuple):
            return ""
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v)[:_AX_VALUE_MAX]

    def children(self, element) -> list:
        try:
            err, kids = self._copy(element, "AXChildren", None)
            return list(kids) if kids else []
        except Exception:
            log.debug("reading AXChildren failed", exc_info=True)
            return []

    def child_count(self, element) -> int:
        try:
            err, n = self._count(element, "AXChildren", None)
            return int(n) if err == 0 else 0
        except Exception:
            log.debug("counting AXChildren failed", exc_info=True)
            return 0


class _AXTableSource(_AXSource):
    """``_AXSource`` that enumerates a table's rows, not its realized children.

    AXChildren on an AXTable/AXOutline exposes only near-viewport rows; the
    dedicated AXRows attribute enumerates the full set, including off-screen
    rows, for free on a non-virtualized NSTableView (matrix §4: 711 rows, row
    700 @ y=16930 without scrolling). Only ``read_grid`` walks with it: it
    renumbers children, and a ``ui_*`` ref is a path of child indices, so the
    two would disagree about where an element lives.
    """

    def children(self, element) -> list:
        node_role = self._role(element)
        if "table" in node_role.lower() or "outline" in node_role.lower():
            with best_effort("reading AXRows", log):
                err, rows = self._copy(element, "AXRows", None)
                if rows:
                    return list(rows)
        return super().children(element)

    def _role(self, element) -> str:
        try:
            err, role = self._copy(element, "AXRole", None)
            return str(role) if role else ""
        except Exception:
            log.debug("reading AXRole failed", exc_info=True)
            return ""


_AX_PRESS_ACTIONS = ("AXPress", "AXOpen", "AXConfirm", "AXPick")
# Errors after which the action may still have happened: -25204 (cannot
# complete: the reply timed out, typical when the action opens modal UI) and
# -25205, which Finder's sidebar cell returns from AXOpen while navigating.
_AX_UNCERTAIN_ERRORS = frozenset({-25204, -25205})


class _AXActor(_AXSource):
    """``ActionAdapter``: performs verbs; core.ui.act checks and confirms them."""

    def __init__(self) -> None:
        super().__init__()
        from ApplicationServices import AXUIElementPerformAction, AXUIElementSetAttributeValue
        self._perform = AXUIElementPerformAction
        self._set = AXUIElementSetAttributeValue

    def _do(self, element, action: str) -> str | None:
        err = self._perform(element, action)
        return None if err == 0 else f"{action} failed (AX error {err})"

    def _write(self, element, attr: str, value) -> str | None:
        err = self._set(element, attr, value)
        return None if err == 0 else f"setting {attr} failed (AX error {err})"

    def _press(self, element, **effect):
        from ..ui.act import Effect

        actions = _ax_actions(element)
        for action in _AX_PRESS_ACTIONS:
            if action in actions:
                err = self._perform(element, action)
                if err in _AX_UNCERTAIN_ERRORS:
                    return Effect(warning=f"{action} answered AX error {err}", **effect)
                return Effect(None if err == 0 else f"{action} failed (AX error {err})", **effect)
        return Effect("no press action (AXPress, AXOpen, AXConfirm or AXPick)")

    def perform(self, element, verb, node, params):
        from ..ui.act import Effect

        native = node.native_role
        if verb in ("press", "toggle"):
            return self._press(element)
        if verb == "select":
            if self._can_set(element, "AXSelected"):
                return Effect(self._write(element, "AXSelected", True))
            if native == "AXCell":
                # Outline cells (Finder sidebar) select through their row.
                err, row = self._copy(element, "AXParent", None)
                if row is not None and self._can_set(row, "AXSelected"):
                    return Effect(self._write(row, "AXSelected", True), verify_element=row)
            return self._press(element)
        if verb == "expand":
            want = bool(params.get("expand", True))
            if native == "AXRow":
                return Effect(self._write(element, "AXDisclosing", want))
            actions = _ax_actions(element)
            if want:
                action = "AXPress" if "AXPress" in actions else "AXShowMenu"
                return Effect(self._do(element, action), verifiable=False)
            if "AXCancel" in actions:
                return Effect(self._do(element, "AXCancel"), verifiable=False)
            return Effect("this element can't close its menu; send Escape with key()")
        if verb == "set_value":
            value = params.get("value", "")
            if node.role == "slider":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return Effect(f"a slider value must be a number, got {value!r}")
            elif self._can_set(element, "AXFocused"):
                self._set(element, "AXFocused", True)  # web fields ignore values set unfocused
            return Effect(self._write(element, "AXValue", value))
        if verb == "focus":
            if node.role in ("window", "dialog"):
                self._activate_owner(element)
                if self._can_set(element, "AXMain"):
                    self._set(element, "AXMain", True)
                return Effect(self._do(element, "AXRaise"), verifiable=False)
            return Effect(self._write(element, "AXFocused", True))
        if verb == "scroll_to":
            return Effect(self._do(element, "AXScrollToVisible"), verifiable=False)
        return Effect(f"unknown verb {verb!r}")

    @staticmethod
    def _activate_owner(element) -> None:
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
            from ApplicationServices import AXUIElementGetPid
            err, pid = AXUIElementGetPid(element, None)
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if err == 0 else None
            if app is not None and not (hasattr(app, "activate") and app.activate()):
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception as e:
            log.debug("activate for ui_focus failed: %s", e, exc_info=True)


def _ax_locator_root(app_ref, loc, copy_attr, window_id_of=None):
    """The live root a locator was read from: the menu bar, or the window with
    the stored CGWindowID (by title only when no id was available). Sheets are
    searched too, since a Save sheet is read as its own window."""
    window_id_of = window_id_of or _ax_window_id
    if loc.root == "menubar":
        err, bar = copy_attr(app_ref, "AXMenuBar", None)
        return bar
    windows: list = []
    err, listed = copy_attr(app_ref, "AXWindows", None)
    windows.extend(listed or [])
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        err, win = copy_attr(app_ref, attr, None)
        if win is not None:
            windows.append(win)
    for win in list(windows):
        err, kids = copy_attr(win, "AXChildren", None)
        windows.extend(k for k in (kids or []) if copy_attr(k, "AXRole", None)[1] == "AXSheet")
    for win in windows:
        if loc.window_id is not None:
            if window_id_of(win) == loc.window_id:
                return win
        else:
            err, t = copy_attr(win, "AXTitle", None)
            if (str(t) if t else "") == loc.window_title:
                return win
    return None


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` still exists. Not NSRunningApplication: its pid lookup
    returned None for a running TextEdit when called from the worker thread."""
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def ui_act(locator, verb: str, params: dict, timeout: float = 5.0):
    """Resolve ``locator`` and run ``verb`` on it; returns a ``core.ui.act.ActOutcome``."""
    return await asyncio.to_thread(_ui_act_sync, locator, verb, params, timeout)


def _ui_act_sync(loc, verb, params, timeout):
    import time

    from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementCreateApplication

    from ..ui.act import ActOutcome, run_action
    from ..ui.resolve import resolve

    if not _pid_alive(loc.pid):
        return ActOutcome("stale_ref", detail="its app has quit", window_gone=True)
    app_ref = AXUIElementCreateApplication(loc.pid)
    root = _ax_locator_root(app_ref, loc, AXUIElementCopyAttributeValue)
    if root is None:
        if loc.root == "menubar":
            return ActOutcome("stale_ref", detail="the app's menu bar is unavailable")
        return ActOutcome("stale_ref", detail="its window has closed", window_gone=True)

    actor = _AXActor()
    res = resolve(actor, root, loc, deadline=time.monotonic() + timeout)
    if res.status == "stale":
        return ActOutcome("stale_ref", detail="that element is no longer in the window",
                          candidates=res.candidates)
    if res.status == "ambiguous":
        return ActOutcome("ambiguous_ref", detail="the element moved and several elements now match it",
                          candidates=res.candidates)
    return run_action(actor, res.element, res.node, verb, params)


_ax_get_window_fn = None


def _ax_window_id(window) -> int | None:
    """CGWindowID of an AXWindow via the private ``_AXUIElementGetWindow``.

    Private but stable for many macOS releases; None when it's unavailable,
    in which case refs fall back to keying the window by title.
    """
    global _ax_get_window_fn
    try:
        import ctypes

        import objc
        if _ax_get_window_fn is None:
            lib = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            fn = lib._AXUIElementGetWindow
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            fn.restype = ctypes.c_int32
            _ax_get_window_fn = fn
        wid = ctypes.c_uint32(0)
        if _ax_get_window_fn(objc.pyobjc_id(window), ctypes.byref(wid)) == 0 and wid.value:
            return int(wid.value)
    except Exception as e:
        log.debug("_AXUIElementGetWindow unavailable: %s", e, exc_info=True)
    return None


def _app_window(app_ref, copy_attr):
    """The app's focused window, else its main window, else its first window."""
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        with best_effort(f"reading {attr}", log):
            err, win = copy_attr(app_ref, attr, None)
            if win is not None:
                return win
    with best_effort("reading AXWindows", log):
        err, windows = copy_attr(app_ref, "AXWindows", None)
        if windows:
            return windows[0]
    return None


def _app_name(pid: int, running_app, workspace) -> str:
    """localizedName for ``pid``. The pid lookup has returned no name live, so
    fall back to scanning the running applications."""
    with best_effort("reading the app name", log):
        app = running_app.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.localizedName():
            return str(app.localizedName())
        for app in workspace.sharedWorkspace().runningApplications():
            if int(app.processIdentifier()) == pid and app.localizedName():
                return str(app.localizedName())
    return ""


def _ui_target(title, create_app, copy_attr, get_pid, workspace):
    """``(pid, app_ref, window_or_None)`` for ``title``, or None when nothing matches.

    ``title`` matches an app name first, then a window title; no title means
    the frontmost app.
    """
    if title:
        needle = title.lower()
        for app in workspace.sharedWorkspace().runningApplications():
            if needle in (app.localizedName() or "").lower():
                pid = int(app.processIdentifier())
                app_ref = create_app(pid)
                return pid, app_ref, _app_window(app_ref, copy_attr)
        win = _find_ax_window(title, create_app, copy_attr, workspace)
        if win is None:
            return None
        err, pid = get_pid(win, None)
        if err != 0:
            return None
        return int(pid), create_app(int(pid)), win
    active = workspace.sharedWorkspace().activeApplication()
    pid = active.get("NSApplicationProcessIdentifier") if active else None
    if not pid:
        return None
    app_ref = create_app(int(pid))
    return int(pid), app_ref, _app_window(app_ref, copy_attr)


_AX_SEARCH_KEYS = {
    "link": "AXLinkSearchKey",
    "button": "AXButtonSearchKey",
    "checkbox": "AXCheckBoxSearchKey",
    "text_field": "AXTextFieldSearchKey",
    "static_text": "AXStaticTextSearchKey",
    "image": "AXGraphicSearchKey",
    "table": "AXTableSearchKey",
    "list": "AXListSearchKey",
}
_AX_WEB_LIMIT = 500


def _ax_search_key(role: str | None) -> str:
    """Search key for a single shared role; anything else searches all types."""
    return _AX_SEARCH_KEYS.get((role or "").strip().lower(), "AXAnyTypeSearchKey")


def _ax_search_predicate(web, key: str, text: str | None, limit: int) -> list:
    try:
        from ApplicationServices import AXUIElementCopyParameterizedAttributeValue
        pred = {
            "AXSearchKey": key, "AXResultsLimit": limit, "AXDirection": "AXDirectionNext",
            "AXImmediateDescendantsOnly": False, "AXVisibleOnly": False,
        }
        if text:
            pred["AXSearchText"] = text
        err, res = AXUIElementCopyParameterizedAttributeValue(
            web, "AXUIElementsForSearchPredicate", pred, None,
        )
        return list(res) if err == 0 and res else []
    except Exception as e:
        log.debug("AXUIElementsForSearchPredicate failed: %s", e, exc_info=True)
        return []


def _ax_path_to(element, ancestor, copy_attr, max_up: int = 64) -> tuple[int, ...] | None:
    """Native child-index path from ``ancestor`` down to ``element``, rebuilt
    by climbing AXParent; None if ``ancestor`` isn't reached."""
    path: list[int] = []
    cur = element
    for _ in range(max_up):
        err, parent = copy_attr(cur, "AXParent", None)
        if parent is None:
            return None
        err, kids = copy_attr(parent, "AXChildren", None)
        idx = next((i for i, k in enumerate(kids or []) if k == cur), None)
        if idx is None:
            return None
        path.append(idx)
        if parent == ancestor:
            return tuple(reversed(path))
        cur = parent
    return None


def _ax_web_search(source, web, web_path, web_search: dict, deadline: float, stats) -> list:
    """Matching elements of one AXWebArea as ``UINode``s with native paths.

    A text search first; the predicate's text matching is narrower than our
    ranking, so when it finds nothing, fetch by type and let ui_find rank.
    """
    import time

    from ApplicationServices import AXUIElementCopyAttributeValue

    from ..ui.walk import make_node

    key = _ax_search_key(web_search.get("role"))
    text = web_search.get("text")
    hits = _ax_search_predicate(web, key, text, _AX_WEB_LIMIT)
    if not hits and text:
        hits = _ax_search_predicate(web, key, None, _AX_WEB_LIMIT)
    nodes = []
    for el in hits:
        if time.monotonic() >= deadline:
            stats.budget_hit = True
            break
        rel = _ax_path_to(el, web, AXUIElementCopyAttributeValue)
        if rel is None:
            continue
        nodes.append(make_node(source, el, web_path + rel))
        stats.nodes += 1
    return nodes


# A full 747-row AXTable read was measured at ~53 s (handoff §6), so the
# table walk gets a budget that a legitimate one fits inside rather than the
# ui_tool default; past it the read comes back partial, not hung.
_TABLE_TIMEOUT = 90.0


async def ui_walk(
    title: str | None = None,
    depth: int = 8,
    max_children: int = 50,
    include_menubar: bool = False,
    timeout: float = 10.0,
    web_search: dict | None = None,
    source=None,
):
    """Walk the target window (and optionally the menu bar) into a ``UIRead``.

    Runs in a worker thread so a long walk doesn't stall the event loop; the
    walk budget is ``timeout`` seconds shared by both roots.

    ``web_search`` (``{"role", "text"}``, from ui_find) switches web areas to
    the native search: instead of walking a page's DOM-sized tree, each
    AXWebArea gets its matching elements as direct children, with native
    paths rebuilt from AXParent.

    ``source`` overrides the ``ElementSource`` the walk reads through, for
    ``extract_table``'s row-enumerating one. The ``ui_*`` tools leave it alone.
    """
    return await asyncio.to_thread(
        _ui_walk_sync, title, depth, max_children, include_menubar, timeout, web_search,
        source,
    )


def _ui_walk_sync(title, depth, max_children, include_menubar, timeout, web_search=None,
                  source=None):
    import time

    try:
        from AppKit import NSRunningApplication, NSWorkspace
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementGetPid,
        )
    except ImportError as e:
        # Not None: None means "nothing matched", and reporting a missing
        # dependency as "no app or window matches" sends the operator looking
        # for the wrong thing.
        raise BackendError("pyobjc is not installed (run uv sync)") from e
    from ..ui.model import UIRead
    from ..ui.walk import WalkStats, walk

    start = time.monotonic()
    deadline = start + timeout
    target = _ui_target(title, AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                        AXUIElementGetPid, NSWorkspace)
    if target is None:
        return None
    pid, app_ref, win = target
    app_name = _app_name(pid, NSRunningApplication, NSWorkspace)

    source = (source or _AXSource)()
    stats = WalkStats()
    web_areas: list = []

    def stop_at_web_areas(element, node) -> bool:
        if node.native_role == "AXWebArea":
            web_areas.append((element, node))
            return False
        return True

    descend = stop_at_web_areas if web_search is not None else None
    window = menubar = None
    window_id = None
    window_title = ""
    if win is not None:
        window_id = _ax_window_id(win)
        err, t = AXUIElementCopyAttributeValue(win, "AXTitle", None)
        window_title = str(t) if t else ""
        window, _ = walk(source, win, depth=depth, max_children=max_children,
                         deadline=deadline, stats=stats, descend=descend)
    if include_menubar:
        err, bar = AXUIElementCopyAttributeValue(app_ref, "AXMenuBar", None)
        if bar is not None:
            menubar, _ = walk(source, bar, depth=depth, max_children=max_children,
                              deadline=deadline, stats=stats, descend=descend)
    for element, node in web_areas:
        if time.monotonic() >= deadline:
            stats.budget_hit = True
            break
        node.children = _ax_web_search(source, element, node.path, web_search, deadline, stats)
    return UIRead(
        app=app_name, pid=pid, window_id=window_id, window_title=window_title,
        window=window, menubar=menubar, nodes=stats.nodes,
        elapsed=time.monotonic() - start, timeout=timeout, budget_hit=stats.budget_hit,
    )


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402


class MacDesktop(DesktopBackend):
    """macOS: CGWindow, pbcopy/pbpaste, the AX tree."""

    name = "macos"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True, table=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
    extract_table = staticmethod(extract_table)
