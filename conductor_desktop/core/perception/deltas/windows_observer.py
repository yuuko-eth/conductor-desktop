"""Windows WinEvent delta observer.

One `SetWinEventHook` covers the whole desktop, like AT-SPI's registry on
Linux and unlike macOS AX (per PID). The hook is out-of-context, so Windows
delivers each event to the thread that installed it, inside that thread's
message loop: the observer owns a thread that installs the hooks and pumps
`GetMessageW` until `WM_QUIT`.

What each event says comes from two places:

- Top-level window events (foreground, shown, destroyed, minimized) read the
  window title and class with plain user32 calls.
- Element events (focus, value, name, state, selection) ask oleacc for the
  element's `IAccessible` (`AccessibleObjectFromEvent`), which every UIA
  provider also answers through the MSAA proxy. That is a cross-process
  call, so the event type and the cheap filters go first.

There is no `view_changed`: Windows has no WinEvent for a virtual-desktop
switch (`EVENT_SYSTEM_DESKTOPSWITCH` is the secure desktop, e.g. a UAC
prompt), and the only way to read the current one is undocumented COM whose
GUIDs move between builds.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import time
from typing import Any

from ..classifier import StateDelta
from ..threaded import ThreadedObserver

log = logging.getLogger("conductor.perception.winevent")

# ── WinEvent constants (winuser.h) ───────────────────────────────────

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_VALUECHANGE = 0x800E

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

OBJID_WINDOW = 0
OBJID_CARET = -8
OBJID_CURSOR = -9
CHILDID_SELF = 0
GA_ROOT = 2
WM_QUIT = 0x0012

# Event → delta kind, for the events whose subject is an element.
_ELEMENT_KINDS: dict[int, str] = {
    EVENT_OBJECT_FOCUS: "focus_changed",
    EVENT_OBJECT_VALUECHANGE: "value_changed",
    EVENT_OBJECT_STATECHANGE: "state_changed",
    EVENT_OBJECT_NAMECHANGE: "name_changed",
    EVENT_OBJECT_SELECTION: "selection_changed",
}

# The hooked ranges. One hook per contiguous range keeps the count small
# without subscribing to the location/reorder storms in between.
_HOOK_RANGES: tuple[tuple[int, int], ...] = (
    (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
    (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPSTART),
    (EVENT_SYSTEM_DIALOGSTART, EVENT_SYSTEM_DIALOGSTART),
    (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
    (EVENT_OBJECT_DESTROY, EVENT_OBJECT_SHOW),
    (EVENT_OBJECT_FOCUS, EVENT_OBJECT_SELECTION),
    (EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_STATECHANGE),
    (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE),
    (EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_VALUECHANGE),
)

# Layer 1: value and name churn from these MSAA roles is chrome (a scroll
# thumb, a progress bar, the clock), the same cut as Linux's skip roles.
_SKIP_ROLES_FOR_CHURN: frozenset[str] = frozenset({
    "scroll bar",
    "progress bar",
    "indicator",
    "clock",
    "animation",
    "tool tip",
    "status bar",
})

# Window classes whose events are the shell's own churn, not the user's app.
_SHELL_CLASSES: frozenset[str] = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "TaskListThumbnailWnd",
    "MSTaskListWClass",
    "tooltips_class32",
    "ForegroundStaging",
})

# Max chars of a value we keep; the twin of the AX and AT-SPI caps.
_VALUE_KEEP_MAX = 256

# Top-level windows remembered so a destroy can still say which one went.
_SEEN_WINDOWS_MAX = 512


class WindowsObserver(ThreadedObserver):
    """Hooks WinEvents desktop-wide and normalizes them into StateDelta."""

    thread_name = "conductor-winevent-observer"

    def __init__(self, loop: asyncio.AbstractEventLoop, **kw) -> None:
        super().__init__(loop, **kw)
        self._thread_id = 0
        self._hooks: list[int] = []
        self._proc: Any = None  # the WINEVENTPROC, held so it isn't collected
        self._windows: dict[int, str] = {}  # hwnd → title, for node_removed
        self._acc: Any = None  # _Oleacc, built on the observer thread

    # ── Lifecycle (ThreadedObserver hooks) ──────────────────────────

    def _reset_for_start(self) -> None:
        self._thread_id = 0
        self._windows.clear()

    def _signal_stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _thread_setup(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # The hook thread pumps messages, so it is an STA: oleacc's proxies
        # expect one.
        ctypes.oledll.ole32.CoInitialize(None)
        self._acc = _Oleacc()
        proc_type = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )
        self._proc = proc_type(self._on_event)
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, proc_type,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ]
        for lo, hi in _HOOK_RANGES:
            hook = user32.SetWinEventHook(
                lo, hi, None, self._proc, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            if hook:
                self._hooks.append(hook)
            else:
                log.debug("SetWinEventHook(%#x..%#x) failed", lo, hi)
        if not self._hooks:
            raise RuntimeError("SetWinEventHook failed for every event range")
        self._thread_id = kernel32.GetCurrentThreadId()
        log.info("WinEvent observer attached, %d/%d hook ranges",
                 len(self._hooks), len(_HOOK_RANGES))

    def _thread_serve(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        # GetMessageW returns 0 on WM_QUIT and -1 on error; both end the loop.
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _thread_teardown(self) -> None:
        user32 = ctypes.windll.user32
        for hook in self._hooks:
            user32.UnhookWinEvent(hook)
        self._hooks = []
        self._proc = None
        self._acc = None
        self._thread_id = 0
        ctypes.oledll.ole32.CoUninitialize()

    # ── Event handling ──────────────────────────────────────────────

    def _on_event(self, _hook, event, hwnd, id_object, id_child, _thread, _ts) -> None:
        """The WINEVENTPROC, on the observer thread. Never raises: an
        exception escaping a ctypes callback is printed and swallowed, and
        the event is lost either way."""
        try:
            delta = self._normalize(event, hwnd, id_object, id_child)
            if delta is not None:
                self._emit(delta)
        except Exception:
            log.debug("WinEvent callback error (event=%#x)", event, exc_info=True)

    def _normalize(self, event: int, hwnd: int | None, id_object: int,
                   id_child: int) -> StateDelta | None:
        hwnd = hwnd or 0
        if event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_DESTROY):
            # Only top-level windows: every control in every app fires these.
            if id_object != OBJID_WINDOW or id_child != CHILDID_SELF:
                return None
            if event == EVENT_OBJECT_DESTROY:
                title = self._windows.pop(hwnd, None)
                if title is None:
                    return None
                return self._window_delta("node_removed", hwnd, title, "window")
            if not _is_top_level(hwnd):
                return None
            title, cls = _window_text(hwnd), _window_class(hwnd)
            if not title or cls in _SHELL_CLASSES:
                return None
            self._remember(hwnd, title)
            return self._window_delta("node_added", hwnd, title, "window", cls=cls)

        if event == EVENT_SYSTEM_FOREGROUND:
            title, cls = _window_text(hwnd), _window_class(hwnd)
            if cls in _SHELL_CLASSES:
                return None
            if title:
                self._remember(hwnd, title)
            return self._window_delta("window_state_changed", hwnd, title, "window",
                                      cls=cls, state="activated")
        if event in (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND):
            state = "minimized" if event == EVENT_SYSTEM_MINIMIZESTART else "restored"
            return self._window_delta("window_state_changed", hwnd, _window_text(hwnd),
                                      "window", state=state)
        if event == EVENT_SYSTEM_DIALOGSTART:
            return self._window_delta("node_added", hwnd, _window_text(hwnd), "dialog")
        if event == EVENT_SYSTEM_MENUPOPUPSTART:
            return self._window_delta("node_added", hwnd, None, "menu")

        kind = _ELEMENT_KINDS.get(event)
        # The pointer and the caret are objects too: the pointer renames
        # itself with every shape change ("Edit", "Normal").
        if kind is None or id_object in (OBJID_CURSOR, OBJID_CARET):
            return None
        root = _root_of(hwnd)
        if root and _window_class(root) in _SHELL_CLASSES:
            return None
        info = self._acc.describe(hwnd, id_object, id_child) if self._acc else None
        if info is None:
            return None
        role, name, value, state_bits, bbox = info
        if kind in ("value_changed", "name_changed") and role in _SKIP_ROLES_FOR_CHURN:
            return None
        if kind == "value_changed":
            value = value if value is not None and len(value) <= _VALUE_KEEP_MAX else None
        elif kind == "state_changed":
            value = None if state_bits is None else f"{state_bits:#x}"
        else:
            value = None
        app = _window_text(root) if root else ""
        return StateDelta(
            kind=kind,  # type: ignore[arg-type]
            source="uia",
            monotonic_ts=time.monotonic(),
            source_id=f"{app or '?'}:{role or '?'}:{name or ''}",
            role=role,
            name=name,
            value=value,
            bbox=bbox,
        )

    def _remember(self, hwnd: int, title: str) -> None:
        if hwnd not in self._windows and len(self._windows) >= _SEEN_WINDOWS_MAX:
            self._windows.pop(next(iter(self._windows)))
        self._windows[hwnd] = title

    @staticmethod
    def _window_delta(kind: str, hwnd: int, title: str | None, role: str,
                      **extra: Any) -> StateDelta:
        return StateDelta(
            kind=kind,  # type: ignore[arg-type]
            source="uia",
            monotonic_ts=time.monotonic(),
            source_id=f"hwnd:{hwnd:#x}",
            role=role,
            name=title or None,
            bbox=_window_rect(hwnd),
            extra={k: v for k, v in extra.items() if v},
        )


# ── user32 helpers ────────────────────────────────────────────────────


def _window_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _root_of(hwnd: int) -> int:
    if not hwnd:
        return 0
    return ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT) or 0


def _is_top_level(hwnd: int) -> bool:
    return bool(hwnd) and _root_of(hwnd) == hwnd


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    from ctypes import wintypes

    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


# ── oleacc ────────────────────────────────────────────────────────────


class _Oleacc:
    """`AccessibleObjectFromEvent` and the handful of IAccessible reads a
    delta needs. Built on the observer thread, after COM is initialized."""

    def __init__(self) -> None:
        import comtypes.client
        from comtypes.automation import VARIANT

        self._VARIANT = VARIANT
        self._mod = comtypes.client.GetModule("oleacc.dll")
        self._from_event = ctypes.oledll.oleacc.AccessibleObjectFromEvent
        self._role_text = ctypes.windll.oleacc.GetRoleTextW

    def describe(self, hwnd: int, id_object: int, id_child: int):
        """(role, name, value, state bits, bbox) for the event's element, or
        None when the element is already gone."""
        from comtypes import POINTER

        acc = POINTER(self._mod.IAccessible)()
        child = self._VARIANT()
        try:
            self._from_event(hwnd, id_object, id_child, ctypes.byref(acc),
                             ctypes.byref(child))
        except OSError:
            return None
        if not acc:
            return None
        role = self._role(acc, child)
        name = _read(lambda: acc.accName(child))
        value = _read(lambda: acc.accValue(child))
        state = _read(lambda: acc.accState(child))
        bbox = _read(lambda: tuple(int(v) for v in acc.accLocation(child)))
        return (role, name or None, value, state if isinstance(state, int) else None,
                bbox if bbox and len(bbox) == 4 else None)

    def _role(self, acc: Any, child: Any) -> str | None:
        role = _read(lambda: acc.accRole(child))
        if not isinstance(role, int):
            return role if isinstance(role, str) else None
        buf = ctypes.create_unicode_buffer(64)
        if not self._role_text(role, buf, len(buf)):
            return None
        return buf.value


def _read(fn) -> Any:
    """One IAccessible read with its errors swallowed: the element can go
    away between the event and the read, and a provider may not implement
    the property at all."""
    try:
        return fn()
    except Exception:
        # Hot path (several reads per event): a plain handler rather than
        # best_effort's generator, as on Linux.
        log.debug("IAccessible read failed", exc_info=True)
        return None
