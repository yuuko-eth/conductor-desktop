"""Phase 1 smoke for backends/windows.py — self-contained.

Reproduces the exact ctypes calls from
conductor_desktop/core/backends/windows.py::list_screens() and
::list_windows() so we can verify the Win32 surface behaves as designed
without deploying the full split-out conductor_desktop package to this host.

Run from the existing host-agent venv (has psutil):
    C:\\Users\\jack\\conductor\\host-agent\\.venv\\Scripts\\python.exe \\
        win_smoke.py
"""
import ctypes
import ctypes.wintypes
import json
import time
from collections import Counter

# ── Constants (must match backends/windows.py) ─────────────────────
MONITORINFOF_PRIMARY = 0x00000001
MONITOR_DEFAULTTONULL = 0
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

# Desktop access rights — DESKTOP_ENUMERATE | DESKTOP_READOBJECTS is the
# minimum for EnumWindows; ALL for full attach.
DESKTOP_ALL_ACCESS = 0x000F01FF


def try_attach_to_input_desktop():
    """SSH on Windows lands in session 0 by default; EnumWindows there sees
    only invisible service-session windows. Production conductor_desktop runs
    inside the user's interactive session (Claude Desktop is user-session),
    so this isn't an issue in normal use — but for SSH-based smoke tests
    we need to migrate the calling thread to the input desktop.
    Returns (ok, detail).
    """
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
    ]
    user32.SetThreadDesktop.restype = ctypes.wintypes.BOOL
    user32.SetThreadDesktop.argtypes = [ctypes.wintypes.HANDLE]

    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_ALL_ACCESS)
    if not hdesk:
        err = ctypes.windll.kernel32.GetLastError()
        return False, f"OpenInputDesktop GetLastError={err}"
    ok = user32.SetThreadDesktop(hdesk)
    if not ok:
        err = ctypes.windll.kernel32.GetLastError()
        return False, f"SetThreadDesktop GetLastError={err}"
    return True, f"attached to input desktop (handle=0x{hdesk:x})"


# ── attribute_monitor — copied verbatim from _common.py ────────────
def attribute_monitor(bounds, screens):
    if not bounds or not screens:
        return -1
    x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
    cx, cy = x + w // 2, y + h // 2
    for s in screens:
        sb = s.get("bounds") or [0, 0, 0, 0]
        sx, sy, sw, sh = sb[0], sb[1], sb[2], sb[3]
        if sx <= cx < sx + sw and sy <= cy < sy + sh:
            return int(s.get("index", -1))
    return -1


def ex_style_to_layer(ex_style):
    if ex_style & WS_EX_TOPMOST:
        return "always_on_top"
    if ex_style & WS_EX_TOOLWINDOW:
        return "floating"
    return "normal"


# ── EnumDisplayMonitors ────────────────────────────────────────────
def enumerate_monitors():
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

    monitors = []
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
        idx = len(monitors)
        monitors.append((int(hmon), {
            "index": idx,
            "name": info.szDevice or f"Display {idx}",
            "bounds": [int(rc.left), int(rc.top),
                       int(rc.right - rc.left), int(rc.bottom - rc.top)],
            "scale": 1.0,
            "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
        }))
        return True

    user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(cb), 0)
    return monitors


def list_screens():
    return [s for _, s in enumerate_monitors()]


# ── EnumWindows ────────────────────────────────────────────────────
def list_windows():
    user32 = ctypes.windll.user32
    windows = []

    monitors = enumerate_monitors()
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

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        proc_name = ""
        try:
            import psutil
            proc_name = psutil.Process(pid.value).name()
        except Exception:
            pass

        focused = (hwnd == user32.GetForegroundWindow())

        state = "normal"
        if user32.IsZoomed(hwnd):
            state = "maximized"
        elif user32.IsIconic(hwnd):
            state = "minimized"

        ex_style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) or 0)
        layer = ex_style_to_layer(ex_style)

        hmon = int(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL) or 0)
        monitor = hmon_to_index.get(hmon, -1)

        windows.append({
            "id": str(hwnd),
            "title": title,
            "process_name": proc_name,
            "pid": pid.value,
            "bounds": [rect.left, rect.top,
                       rect.right - rect.left, rect.bottom - rect.top],
            "state": state,
            "desktop": 0,
            "focused": focused,
            "monitor": monitor,
            "z": len(windows),
            "layer": layer,
            "sticky": False,
            "workspace": {"global_index": 0, "per_output": None},
        })
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return windows


# ── Driver ─────────────────────────────────────────────────────────
def main():
    ok, detail = try_attach_to_input_desktop()
    print(f"INPUT-DESKTOP: {'OK' if ok else 'FAIL'} — {detail}")
    print()

    t0 = time.perf_counter()
    screens = list_screens()
    print(f"SCREENS ({(time.perf_counter()-t0)*1000:.0f}ms):")
    print(json.dumps(screens, indent=2, ensure_ascii=False))
    print()

    t0 = time.perf_counter()
    ws = list_windows()
    t = (time.perf_counter() - t0) * 1000
    print(f"WINDOWS ({t:.0f}ms / {t/max(len(ws),1):.1f}ms each, {len(ws)} total)")
    print()
    print("Per-monitor:", dict(Counter(w["monitor"] for w in ws)))
    print("Layers:    ", dict(Counter(w["layer"] for w in ws)))
    print("States:    ", dict(Counter(w["state"] for w in ws)))
    focused = [(w["title"][:60], w["process_name"]) for w in ws if w["focused"]]
    print("Focused:   ", focused)
    print()
    print("Top 15 z-order:")
    for w in sorted(ws, key=lambda x: x["z"])[:15]:
        z, m = w["z"], w["monitor"]
        layer = w["layer"]
        state = w["state"]
        title = w["title"][:50]
        proc = w["process_name"][:25]
        print(f"  z={z:>2} m={m:>2} {layer:<14} {state:<10} {proc:<26} {title}")


if __name__ == "__main__":
    main()
