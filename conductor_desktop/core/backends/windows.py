"""Windows backend — pywinauto, win32clipboard, ctypes.

Target: Windows 10 Pro laptop.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from ..errors import BackendError, best_effort, reason
from .windows_ui import _run as _uia_thread
from .windows_ui import ui_act, ui_walk

log = logging.getLogger("conductor_desktop.windows")


# ── Screen enumeration ──────────────────────────────────────────────


# Win32 constants
_MONITORINFOF_PRIMARY = 0x00000001
_MONITOR_DEFAULTTONULL = 0
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_DLGMODALFRAME = 0x00000001
_DS_MODALFRAME = 0x00000080
_GW_OWNER = 4

# Well-known Win32 class names for menu/dialog/tooltip discrimination.
# These come from gdi32/user32 built-ins and are stable across Windows
# versions. Anything not in this set falls back to the GWL_EXSTYLE-based
# layer heuristic.
_CLASS_LAYER = {
    "#32768": "menu",          # popup menu (system menu class)
    "ComboLBox": "menu",       # combo box dropdown list
    "tooltips_class32": "tooltip",
}


# SHAppBarMessage / APPBARDATA — taskbar position + autohide state.
# uEdge is one of ABE_LEFT (0), ABE_TOP (1), ABE_RIGHT (2), ABE_BOTTOM (3).
_ABM_GETSTATE = 0x4
_ABM_GETTASKBARPOS = 0x5
_ABS_AUTOHIDE = 0x1
_ABE_EDGE = {0: "left", 1: "top", 2: "right", 3: "bottom"}


def _enumerate_monitors() -> list[tuple[int, dict]]:
    """Enumerate display monitors via EnumDisplayMonitors.

    Returns list of (HMONITOR int, screen-dict). The HMONITOR handles
    are then matched against MonitorFromWindow() results during
    list_windows() to attribute each window to a monitor index.

    The screen dict carries a private ``_rc_work`` tuple (kept on the
    dict so the dock detector can recover it without re-querying
    GetMonitorInfoW). Callers that emit screens externally should
    strip it; ``list_screens`` does that before returning.
    """
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    monitors: list[tuple[int, dict]] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HMONITOR, ctypes.wintypes.HDC,
        ctypes.POINTER(RECT), ctypes.wintypes.LPARAM,
    )

    def cb(hmon, hdc, lprc, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        rc = info.rcMonitor
        wk = info.rcWork
        idx = len(monitors)
        monitors.append((int(hmon), {
            "index": idx,
            "name": info.szDevice or f"Display {idx}",
            "bounds": [int(rc.left), int(rc.top),
                       int(rc.right - rc.left), int(rc.bottom - rc.top)],
            "scale": _monitor_scale(hmon),
            "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            "_rc_work": (int(wk.left), int(wk.top),
                         int(wk.right), int(wk.bottom)),
            "_rc_monitor": (int(rc.left), int(rc.top),
                            int(rc.right), int(rc.bottom)),
        }))
        return True

    user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(cb), 0)
    return monitors


def _monitor_scale(hmon) -> float:
    """The monitor's display scaling (1.5 at 150%), from its effective DPI.

    The input executor makes the process per-monitor DPI aware, so each
    monitor reports its own DPI; an unaware process would get 96 everywhere.
    """
    import ctypes
    import ctypes.wintypes

    try:
        get_dpi = ctypes.windll.shcore.GetDpiForMonitor  # Windows 8.1+
        get_dpi.argtypes = [ctypes.wintypes.HMONITOR, ctypes.c_int,
                            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        # 0 = MDT_EFFECTIVE_DPI, the DPI the Settings scale slider sets.
        if get_dpi(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0 and dpi_x.value:
            return round(dpi_x.value / 96, 2)
    except (AttributeError, OSError):
        log.debug("GetDpiForMonitor failed", exc_info=True)
    return 1.0


async def list_screens() -> list[dict]:
    """Enumerate display monitors via EnumDisplayMonitors + GetMonitorInfoW.

    See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Windows". Primary monitor is marked
    via the ``MONITORINFOF_PRIMARY`` flag. Each screen also carries a
    ``dock`` field describing the taskbar on that monitor (primary tray
    via ``SHAppBarMessage(ABM_GETTASKBARPOS)``, secondary trays via
    ``Shell_SecondaryTrayWnd`` window scan).
    """
    try:
        screens = [s for _, s in _enumerate_monitors()]
    except Exception as e:  # ctypes/Win32 failures come in many types
        raise BackendError(f"EnumDisplayMonitors failed: {reason(e)}") from e

    try:
        _attach_dock_info(screens)
    except Exception as e:
        log.debug("dock detection failed: %s", e, exc_info=True)

    # Strip the private GetMonitorInfo carry-fields before returning —
    # they're an implementation detail of the dock detector.
    for s in screens:
        s.pop("_rc_work", None)
        s.pop("_rc_monitor", None)
    return screens


def _query_taskbar_state() -> tuple[bool, str | None, tuple[int, int, int, int] | None]:
    """Return ``(autohide, primary_edge, primary_rect)``.

    ``SHAppBarMessage`` is the documented API for taskbar metadata.
    ``ABM_GETSTATE`` is the global autohide flag (applies to all
    taskbars). ``ABM_GETTASKBARPOS`` returns the *primary* taskbar's
    edge + rect; secondary taskbars carry the same edge implicitly
    (Explorer pins them to the same screen edge as the primary).
    """
    import ctypes
    import ctypes.wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class APPBARDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("hWnd", ctypes.wintypes.HWND),
            ("uCallbackMessage", ctypes.wintypes.UINT),
            ("uEdge", ctypes.wintypes.UINT),
            ("rc", RECT),
            ("lParam", ctypes.wintypes.LPARAM),
        ]

    shell32 = ctypes.windll.shell32
    shell32.SHAppBarMessage.restype = ctypes.wintypes.LPARAM

    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    state = int(shell32.SHAppBarMessage(_ABM_GETSTATE, ctypes.byref(data)))
    autohide = bool(state & _ABS_AUTOHIDE)

    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    ok = int(shell32.SHAppBarMessage(_ABM_GETTASKBARPOS, ctypes.byref(data)))
    if not ok:
        return autohide, None, None
    rect = (data.rc.left, data.rc.top, data.rc.right, data.rc.bottom)
    edge = _ABE_EDGE.get(int(data.uEdge))
    return autohide, edge, rect


def _enum_secondary_trays() -> list[tuple[int, int, int, int]]:
    """Find every ``Shell_SecondaryTrayWnd`` and return its rect.

    Multi-monitor Windows pins one secondary tray to each extra
    display when the "Show taskbar on all displays" setting is on.
    We enumerate by repeated FindWindowExW so a third / fourth
    monitor's tray isn't missed.
    """
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rects: list[tuple[int, int, int, int]] = []
    prev = 0
    while True:
        hwnd = user32.FindWindowExW(0, prev, "Shell_SecondaryTrayWnd", None)
        if not hwnd:
            break
        rc = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            rects.append((rc.left, rc.top, rc.right, rc.bottom))
        prev = hwnd
    return rects


def _edge_from_rect_on_screen(
    rect: tuple[int, int, int, int],
    screen_rc: tuple[int, int, int, int],
) -> str | None:
    """Infer which screen edge a rect is pinned against.

    For docked taskbars the rect is flush with exactly one edge.
    A full-width tray ties three edges at distance 0 (left, right and
    bottom for a bottom-pinned tray), so we tie-break on the tray's
    aspect ratio — horizontal trays (wider than tall) belong on top
    or bottom, vertical trays on left or right. Returns ``None`` when
    no edge is within 8 px on the relevant axis (caller falls back).
    """
    rl, rt, rr, rb = rect
    sl, st, sr, sb = screen_rc
    width = rr - rl
    height = rb - rt
    horizontal = {"top": abs(rt - st), "bottom": abs(sb - rb)}
    vertical = {"left": abs(rl - sl), "right": abs(sr - rr)}
    candidates = horizontal if width >= height else vertical
    edge = min(candidates, key=candidates.get)
    if candidates[edge] > 8:
        return None
    return edge


def _attach_dock_info(screens: list[dict]) -> None:
    """Mutate ``screens`` in place, adding a ``dock`` dict per screen.

    Primary screen takes its edge/rect from ``ABM_GETTASKBARPOS``;
    secondary screens lift their rect from ``Shell_SecondaryTrayWnd``
    and infer the edge geometrically. ``hidden`` is ``rcWork == rcMonitor``
    (autohide active and not currently revealed → the tray isn't
    reserving any work-area on that monitor).
    """
    autohide, primary_edge, primary_rect = _query_taskbar_state()
    secondary_rects = _enum_secondary_trays()

    tray_rects: list[tuple[int, int, int, int]] = []
    if primary_rect is not None:
        tray_rects.append(primary_rect)
    tray_rects.extend(secondary_rects)

    for s in screens:
        screen_rc = s.get("_rc_monitor")
        work_rc = s.get("_rc_work")
        if not screen_rc or not work_rc:
            continue
        # Find the tray whose center lies inside this monitor.
        best: tuple[int, int, int, int] | None = None
        for rect in tray_rects:
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            if (screen_rc[0] <= cx < screen_rc[2]
                    and screen_rc[1] <= cy < screen_rc[3]):
                best = rect
                break

        # rcWork == rcMonitor means nothing reserved on this monitor.
        # That can mean (a) autohide on, currently hidden, or (b) no
        # tray on this monitor at all. We disambiguate using ``best``.
        not_reserving = work_rc == screen_rc
        if best is None:
            # No tray claimed this screen — emit None so callers don't
            # mistake autohide-collapsed for "no taskbar here".
            s["dock"] = None
            continue

        # Primary edge from ABM if this is the primary tray; else infer.
        if primary_rect is not None and best == primary_rect and primary_edge:
            edge: str | None = primary_edge
        else:
            edge = _edge_from_rect_on_screen(best, screen_rc)
            if edge is None and primary_edge is not None:
                edge = primary_edge  # last-resort fallback
            elif edge is None:
                edge = "bottom"

        bx, by, br, bb = best
        s["dock"] = {
            "edge": edge,
            "bounds": [bx, by, br - bx, bb - by],
            "autohide": autohide,
            "hidden": autohide and not_reserving,
        }


# ── Window Management ───────────────────────────────────────────────


def _ex_style_to_layer(ex_style: int) -> str:
    """Map WS_EX_* extended styles to the Layer enum.

    Caller should consult :data:`_CLASS_LAYER` first — class-name
    detection is the cleaner signal for popup menus / tooltips /
    dropdowns, which is what most callers actually care about. This
    function only catches the broader "is this floating-ish" hint.
    """
    if ex_style & _WS_EX_TOPMOST:
        return "always_on_top"
    if ex_style & _WS_EX_TOOLWINDOW:
        return "floating"
    return "normal"


def _read_class_name(hwnd, user32) -> str:
    """Read the Win32 class name for ``hwnd``. Empty on failure."""
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    if user32.GetClassNameW(hwnd, buf, 256) > 0:
        return buf.value
    return ""


def _modality_for(style: int, ex_style: int, owner_hwnd: int) -> str | None:
    """Approximate modality from Win32 style bits + owner presence.

    Windows has no first-class "modality scope" flag — modality is
    enforced by the message pump (DialogBox / EnableWindow). We
    approximate from style bits that conventionally accompany modal
    dialogs:
      - DS_MODALFRAME or WS_EX_DLGMODALFRAME with an owner → "window"
        (sheet-like; blocks the owner)
      - same bits but no owner → "app" (rare; typically a top-level
        app-modal dialog with no parent set)
    System modality (UAC) does not appear in EnumWindows results
    because it runs on the secure desktop — never reachable from here.
    """
    is_modal_frame = bool(
        (style & _DS_MODALFRAME) or (ex_style & _WS_EX_DLGMODALFRAME)
    )
    if not is_modal_frame:
        return None
    return "window" if owner_hwnd else "app"


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows via EnumWindows, enriched with scene-graph fields.

    ``EnumWindows`` fires its callback top-to-bottom in z-order natively;
    we use the callback index as ``z``. Each window's monitor is resolved
    via ``MonitorFromWindow(MONITOR_DEFAULTTONULL)`` matched against
    ``_enumerate_monitors()`` HMONITORs. See https://docs.conductor-mcp.io/concepts/scene-graph/ →
    "Windows".
    """
    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        windows = []

        monitors = _enumerate_monitors()
        hmon_to_index = {hmon: scr["index"] for hmon, scr in monitors}

        def enum_callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            # Get PID
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            # Get bounds
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            # Get process name
            proc_name = ""
            with best_effort("reading the process name", log):
                import psutil
                proc_name = psutil.Process(pid.value).name()

            # Check focused
            focused = (hwnd == user32.GetForegroundWindow())

            # Check state
            state = "normal"
            if user32.IsZoomed(hwnd):
                state = "maximized"
            elif user32.IsIconic(hwnd):
                state = "minimized"

            # Style bits drive layer band + modality detection.
            style = int(user32.GetWindowLongW(hwnd, _GWL_STYLE) or 0)
            ex_style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) or 0)
            # Class name discriminates menu / tooltip / dropdown from
            # normal/toplevel windows — this is the only place those
            # appear in EnumWindows on Windows.
            class_name = _read_class_name(hwnd, user32)
            layer = _CLASS_LAYER.get(class_name, _ex_style_to_layer(ex_style))

            # Owner pointer for dialogs. GetWindow(GW_OWNER) is the
            # documented dialog-to-base-window edge. 0 = no owner.
            owner_hwnd = int(user32.GetWindow(hwnd, _GW_OWNER) or 0)
            parent_window_id = str(owner_hwnd) if owner_hwnd else None
            modality = _modality_for(style, ex_style, owner_hwnd)

            # Monitor attribution.
            hmon = int(user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONULL) or 0)
            monitor = hmon_to_index.get(hmon, -1)

            windows.append({
                "id": str(hwnd),
                "title": title,
                "process_name": proc_name,
                "pid": pid.value,
                "bounds": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
                "state": state,
                "desktop": 0,
                "focused": focused,
                "monitor": monitor,
                "z": len(windows),
                "layer": layer,
                "sticky": False,  # not detected: virtual-desktop pinning needs pyvda.
                "workspace": {"global_index": 0, "per_output": None},
                "parent_window_id": parent_window_id,
                "modality": modality,
            })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return windows

    except Exception as e:  # ctypes/Win32 failures come in many types
        raise BackendError(f"EnumWindows failed: {reason(e)}") from e


async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus the first window matching ``title`` (substring) or ``pid``.
    False when none matches."""
    import ctypes
    user32 = ctypes.windll.user32

    for w in await list_windows():
        if (title and title.lower() in w["title"].lower()) or (pid and w["pid"] == pid):
            hwnd = int(w["id"])
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if not user32.SetForegroundWindow(hwnd):
                # Windows refuses foreground changes to a process that
                # didn't receive the last input event.
                raise BackendError(f"SetForegroundWindow refused {w['title']!r}")
            return True
    return False


# ── Clipboard ───────────────────────────────────────────────────────


async def get_clipboard() -> str:
    """Read clipboard text via win32clipboard ("" when it holds no text)."""
    try:
        import win32clipboard
    except ImportError:
        out = await _powershell("Get-Clipboard -Raw")
        return out.decode("utf-8", errors="replace").removesuffix("\r\n")
    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return ""
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except win32clipboard.error as e:  # another process holds the clipboard open
        raise BackendError(f"clipboard unavailable: {reason(e)}") from e


async def set_clipboard(text: str) -> None:
    """Write to clipboard via win32clipboard."""
    try:
        import win32clipboard
    except ImportError:
        # Fallback to PowerShell. The text travels as base64, whose alphabet
        # can't close the quoted literal, and is decoded inside the script,
        # which also sidesteps the console code page mangling non-ASCII.
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        await _powershell("Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
                          f"[Convert]::FromBase64String('{b64}')))")
        return
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except win32clipboard.error as e:  # another process holds the clipboard open
        raise BackendError(f"clipboard unavailable: {reason(e)}") from e


async def _powershell(script: str) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError) as e:
        raise BackendError(f"powershell failed: {reason(e)}") from e
    if proc.returncode != 0:
        raise BackendError(f"powershell exited {proc.returncode}: "
                           f"{stderr.decode(errors='replace').strip()[:300]}")
    return stdout


# ── Accessibility (UIA via pywinauto) ───────────────────────────────


def _find_uia_window(title: str | None):
    """Resolve the target top-level window (by title substring, else foreground)."""
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    if title:
        for w in desktop.windows():
            try:
                if title.lower() in w.window_text().lower():
                    return w
            except Exception:
                log.debug("reading a window title failed", exc_info=True)
                continue
        return None
    # Foreground window
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    for w in desktop.windows():
        try:
            if w.handle == hwnd:
                return w
        except Exception:
            log.debug("reading a window handle failed", exc_info=True)
            continue
    return None


def _uia_iface(w, name):
    """Return a UIA pattern interface or None (pywinauto raises if unsupported)."""
    try:
        from pywinauto.uia_defines import NoPatternInterfaceError
    except ImportError:
        NoPatternInterfaceError = Exception
    try:
        return getattr(w, name)
    except NoPatternInterfaceError:
        return None
    except Exception:
        log.debug("reading UIA %s failed", name, exc_info=True)
        return None


def _uia_patterns(w) -> tuple[list[str], bool]:
    """``(patterns, value_settable)`` for one element, in ``core.ui.uia``'s
    vocabulary — so a cell this module builds by hand normalizes exactly like
    one the ``ui_*`` walk read (matrix §5, the act channel)."""
    patterns: list[str] = []
    settable = False
    if _uia_iface(w, "iface_value") is not None:
        patterns.append("Value")
        with best_effort("reading ValuePattern.IsReadOnly", log):
            settable = not bool(w.iface_value.CurrentIsReadOnly)
    if _uia_iface(w, "iface_invoke") is not None:
        patterns.append("Invoke")
    if _uia_iface(w, "iface_selection_item") is not None:
        patterns.append("SelectionItem")
    if _uia_iface(w, "iface_toggle") is not None:
        patterns.append("Toggle")
    return patterns, settable


def _ui_node(native_role: str, *, name: str = "", value: str = "",
             bounds=None, states=None, patterns=None, settable: bool = False,
             row_range=None, col_range=None, children=None):
    """One ``UINode`` from what pywinauto read, normalized like a walked one."""
    from ..ui.model import RawNode, UINode
    from ..ui.uia import normalize

    raw = RawNode(native_role=native_role, name=name, value=value,
                  actions=list(patterns or []), bounds=bounds,
                  value_settable=settable, row_range=row_range, col_range=col_range)
    role, verbs, node_states = normalize(raw)
    return UINode(role=role, native_role=native_role, name=name, value=value,
                  verbs=verbs, states=node_states if states is None else list(states), bounds=bounds,
                  row_range=row_range, col_range=col_range, children=list(children or []))


def _uia_grid_item(w) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """(row_range, col_range) from GridItemPattern, or (None, None).

    Returns (start, span) per axis. Absent on tree-grids whose GetItem resolves
    to the row (column-collapse, matrix §5) — leave None and let the extractor
    assign columns from the row's Edit children.
    """
    gi = _uia_iface(w, "iface_grid_item")
    if gi is None:
        return None, None
    try:
        rspan = int(getattr(gi, "CurrentRowSpan", 1) or 1)
        cspan = int(getattr(gi, "CurrentColumnSpan", 1) or 1)
        return (int(gi.CurrentRow), rspan), (int(gi.CurrentColumn), cspan)
    except Exception:
        log.debug("reading GridItemPattern failed", exc_info=True)
        return None, None


# ── Structured grid provider (UIA GridPattern) ──────────────────────


def _is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        log.debug("IsUserAnAdmin failed", exc_info=True)
        return False


def _find_grid_container(top):
    """Walk the window's UIA tree, return the best (wrapper, grid, rows, cols).

    Gate on a real GridPattern with RowCount>0 (control_type alone lies — a
    List/Custom may have no grid). Pick by focus then size (matrix routing
    rule 3: 'first table ≠ target table').
    """
    from pywinauto.controls.uiawrapper import UIAWrapper

    best = None  # (score_tuple, wrapper, grid, rows, cols)

    def visit(ei, depth):
        nonlocal best
        if depth > 40:
            return
        try:
            w = UIAWrapper(ei)
        except Exception:
            log.debug("wrapping a UIA element failed", exc_info=True)
            w = None
        if w is not None:
            grid = _uia_iface(w, "iface_grid")
            if grid is not None:
                try:
                    rows = int(grid.CurrentRowCount)
                    cols = int(grid.CurrentColumnCount)
                except Exception:
                    log.debug("reading the grid size failed", exc_info=True)
                    rows = cols = 0
                if rows > 0 and cols > 0:
                    focused = 0
                    with best_effort("reading the grid focus", log):
                        focused = 1 if w.has_keyboard_focus() else 0
                    score = (focused, rows * cols)
                    if best is None or score > best[0]:
                        best = (score, w, grid, rows, cols)
        try:
            kids = ei.children()
        except Exception:
            log.debug("reading UIA children failed", exc_info=True)
            kids = []
        for k in kids:
            visit(k, depth + 1)

    visit(top.element_info, 0)
    return best


def _leaf_children(cw):
    """Edit/Text descendants of a cell/row → fallthrough text for tree-grids."""
    out = []
    with best_effort("reading cell children", log):
        for k in cw.children():
            ct = ""
            with best_effort("reading a cell child's control type", log):
                ct = k.element_info.control_type or ""
            if ct in ("Edit", "Text", "Document"):
                val = ""
                iv = _uia_iface(k, "iface_value")
                if iv is not None:
                    with best_effort("reading a cell child's value", log):
                        val = str(iv.CurrentValue or "")
                if not val:
                    with best_effort("reading a cell child's text", log):
                        val = k.window_text() or ""
                lbounds = None
                with best_effort("reading a cell child's bounds", log):
                    rect = k.rectangle()
                    lbounds = (rect.left, rect.top, rect.width(), rect.height())
                out.append(_ui_node(ct, value=val[:500], bounds=lbounds))
    return out


def _read_cell(cw):
    """Build a cell ``UINode`` from a UIA cell wrapper (value, bbox, grididx, acts)."""
    try:
        role = cw.element_info.control_type or "DataItem"
    except Exception:
        log.debug("reading the cell control type failed", exc_info=True)
        role = "DataItem"
    val = ""
    iv = _uia_iface(cw, "iface_value")
    if iv is not None:
        with best_effort("reading the cell value", log):
            val = str(iv.CurrentValue or "")
    if not val:
        with best_effort("reading the cell text", log):
            val = cw.window_text() or ""
    bounds = None
    with best_effort("reading the cell bounds", log):
        rect = cw.rectangle()
        bounds = (rect.left, rect.top, rect.width(), rect.height())
    rr, cc = _uia_grid_item(cw)
    patterns, settable = _uia_patterns(cw)
    return _ui_node(role, value=val[:500], bounds=bounds, patterns=patterns,
                    settable=settable, row_range=rr, col_range=cc,
                    children=_leaf_children(cw))


async def extract_table(title: str | None = None, max_rows: int = 100):
    """See ``_extract_table_sync``; runs on the ui_* UIA thread."""
    return await _uia_thread(_extract_table_sync, title, max_rows)


def _extract_table_sync(title: str | None, max_rows: int):
    """Windows StructuredProvider: UIA GridPattern → TableExtract. matrix §5.

    Reads cells via GridPattern.GetItem (random access) rather than tree-walking
    — that's what reaches virtualized/off-screen rows (Excel's bounded window,
    Task Mgr's full row set). Cells are funneled through the shared extractor so
    column-collapse + fallthrough + capability flags stay uniform with macOS.
    """
    from ..protocols import (
        FAILURE_ELEVATION_REQUIRED,
        FAILURE_NOT_IMPLEMENTED,
        StructuredCapabilities,
        TableExtract,
    )
    from ..structured import extract_table as _extract
    from ..ui.model import UINode

    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo
    except ImportError:
        log.error("pywinauto not installed")
        return TableExtract(0, 0, [], "none", StructuredCapabilities(),
                            failure=FAILURE_NOT_IMPLEMENTED)

    target = _find_uia_window(title)
    if target is None:
        # UIPI silently hides higher-integrity-level windows from a medium-IL
        # client (matrix §5). If we're not elevated, that's the likely cause.
        failure = None if _is_elevated() else FAILURE_ELEVATION_REQUIRED
        return TableExtract(0, 0, [], "none", StructuredCapabilities(), failure=failure)

    found = _find_grid_container(target)
    if found is None:
        return TableExtract(0, 0, [], "none", StructuredCapabilities())

    _score, container_w, grid, rows, cols = found
    n_read = min(rows, max_rows)

    row_nodes: list[UINode] = []
    for r in range(n_read):
        cells: list[UINode] = []
        cell_cols: list[int] = []  # each cell's grid column; a skipped cell leaves a gap
        rects_seen: list[tuple] = []
        for c in range(cols):
            try:
                cell_elem = grid.GetItem(r, c)
                cw = UIAWrapper(UIAElementInfo(cell_elem))
            except Exception:
                log.debug("GridPattern.GetItem(%d, %d) failed", r, c, exc_info=True)
                continue
            cells.append(_read_cell(cw))
            cell_cols.append(c)
            if cells[-1].bounds:
                rects_seen.append(cells[-1].bounds)
        if not cells:
            continue
        # Column-collapse (tree-grid, Task Mgr): GetItem(r,c) resolved to the row
        # for every c → identical rects. Replace with the row's Edit-child
        # columns so per-column text is correct (matrix §5). Detect on rects, not
        # grid indices (which we're about to stamp anyway).
        collapsed = len(cells) > 1 and len(set(rects_seen)) <= 1
        if collapsed and (cells[0].children):
            leaves = cells[0].children
            row_value = cells[0].value  # row's own value, e.g. the name column
            new_cells = []
            for ci, lf in enumerate(leaves):
                text = lf.value
                # Tree-grids draw the name/tree column outside an Edit child, so
                # the first leaf can be empty while the row element carries the
                # name as its value (Task Mgr: "Process: Discord"). Backfill it.
                if not text and ci == 0 and row_value:
                    text = row_value
                new_cells.append(_ui_node(
                    "GridCell", value=text, bounds=lf.bounds or cells[0].bounds,
                    row_range=(r, 1), col_range=(ci, 1),
                ))
                # The split cells act through the row they came from.
                new_cells[-1].verbs = list(cells[0].verbs)
            cells = new_cells
        else:
            # Stamp the known (r,c) from our GetItem walk when GridItemPattern
            # didn't supply indices, so every cell is unambiguously addressed.
            for c, cell in zip(cell_cols, cells, strict=True):
                if cell.row_range is None:
                    cell.row_range = (r, 1)
                if cell.col_range is None:
                    cell.col_range = (c, 1)
        row_nodes.append(_ui_node("DataItem", children=cells))

    cname = ""
    with best_effort("reading the grid name", log):
        cname = container_w.window_text() or ""
    cstates = ["focused"] if _score[0] else []
    container = _ui_node("DataGrid", name=cname, states=cstates, children=row_nodes)
    return _extract(container, "uia")


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402


class WindowsDesktop(DesktopBackend):
    """Windows: Win32 windows, win32clipboard, UIA through pywinauto."""

    name = "windows"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True, table=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
    extract_table = staticmethod(extract_table)
