"""Windows ``ui_*`` backend: UIA elements → ``core.ui`` walker, resolver and actions.

Talks to IUIAutomation through comtypes directly (pywinauto's wrappers read
one property per COM call). Every read uses a CacheRequest, so describing an
element costs one round trip, and children come back from
``FindAllBuildCache`` already described.

All UIA work runs on one dedicated COM-initialized thread: UIA objects are
tied to the apartment that created them, and the event loop must not block
on a slow provider.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import logging
import os
import threading

from ..errors import best_effort

log = logging.getLogger("conductor_desktop.windows_ui")

_VALUE_MAX = 500
_NAME_MAX = 200
# UIA_IsDialogPropertyId (Windows 10 1809+); older builds report "not supported".
_IS_DIALOG_PROPERTY_ID = 30174

# COM errors after which the action may still have happened: the element went
# away (an Invoke that closes its own dialog) or the provider timed out
# (typical when the action opens modal UI).
_UIA_E_ELEMENTNOTAVAILABLE = -2147220991  # 0x80040201
_UIA_E_TIMEOUT = -2146233083              # 0x80131505
_UNCERTAIN_HRESULTS = frozenset({_UIA_E_ELEMENTNOTAVAILABLE, _UIA_E_TIMEOUT})
# Settings disables a switch for a moment while it applies the previous
# change, so a second toggle right after the first answers "not enabled".
_UIA_E_ELEMENTNOTENABLED = -2147220992    # 0x80040200
_NOT_ENABLED_RETRY_SECONDS = 1.0


def _retry_not_enabled(fn, *, sleep=None, clock=None, seconds: float = _NOT_ENABLED_RETRY_SECONDS):
    """Call ``fn``, retrying while it fails with UIA_E_ELEMENTNOTENABLED."""
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + seconds
    while True:
        try:
            return fn()
        except Exception as e:
            if _hresult(e) != _UIA_E_ELEMENTNOTENABLED or clock() >= deadline:
                raise
            sleep(0.1)


_PATTERNS = ("Invoke", "Toggle", "SelectionItem", "ExpandCollapse", "Value", "RangeValue",
             "ScrollItem", "Text")

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_local = threading.local()


def _com_thread_init() -> None:
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # importing comtypes on this thread already initialized COM (RPC_E_CHANGED_MODE)


def _run(fn, *args):
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="uia", initializer=_com_thread_init)
    return asyncio.get_running_loop().run_in_executor(_executor, fn, *args)


class _UIA:
    """IUIAutomation, its generated module and one CacheRequest, per UIA thread."""

    def __init__(self) -> None:
        import comtypes
        import comtypes.client

        self.mod = mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self.iuia = comtypes.CoCreateInstance(
            mod.CUIAutomation._reg_clsid_, interface=mod.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self.control_types = {
            getattr(mod, attr): attr[len("UIA_"):-len("ControlTypeId")]
            for attr in dir(mod) if attr.startswith("UIA_") and attr.endswith("ControlTypeId")
        }
        self.pattern_ids = {p: getattr(mod, f"UIA_{p}PatternId") for p in _PATTERNS}
        self.available = {p: getattr(mod, f"UIA_Is{p}PatternAvailablePropertyId") for p in _PATTERNS}
        self.state_props = {
            "toggle": mod.UIA_ToggleToggleStatePropertyId,
            "selected": mod.UIA_SelectionItemIsSelectedPropertyId,
            "expand": mod.UIA_ExpandCollapseExpandCollapseStatePropertyId,
            "value_ro": mod.UIA_ValueIsReadOnlyPropertyId,
            "range_ro": mod.UIA_RangeValueIsReadOnlyPropertyId,
            "range_value": mod.UIA_RangeValueValuePropertyId,
        }
        self.condition = self.iuia.ControlViewCondition
        self.children_scope = mod.TreeScope_Children

        cr = self.iuia.CreateCacheRequest()
        for prop in (
            mod.UIA_ControlTypePropertyId, mod.UIA_NamePropertyId, mod.UIA_AutomationIdPropertyId,
            mod.UIA_BoundingRectanglePropertyId, mod.UIA_IsEnabledPropertyId,
            mod.UIA_HasKeyboardFocusPropertyId, mod.UIA_IsKeyboardFocusablePropertyId,
            mod.UIA_IsPasswordPropertyId, mod.UIA_NativeWindowHandlePropertyId,
            mod.UIA_ProcessIdPropertyId, _IS_DIALOG_PROPERTY_ID,
            *self.available.values(), *self.state_props.values(),
        ):
            try:
                cr.AddProperty(prop)
            except Exception:
                log.debug("UIA property %s not cacheable here", prop, exc_info=True)
        self.cache = cr


def _uia() -> _UIA:
    uia = getattr(_local, "uia", None)
    if uia is None:
        uia = _local.uia = _UIA()
    return uia


def _plain(v):
    """A cached property value, or None for UIA's ReservedNotSupportedValue (a COM object)."""
    return v if isinstance(v, (bool, int, float, str)) else None


def _cached(element, prop):
    try:
        return _plain(element.GetCachedPropertyValue(prop))
    except Exception:
        log.debug("reading cached UIA property %s failed", prop, exc_info=True)
        return None


def _hresult(e: Exception) -> int | None:
    hr = getattr(e, "hresult", None)
    if hr is None and getattr(e, "args", None):
        hr = e.args[0] if isinstance(e.args[0], int) else None
    return hr


class _UIASource:
    """``ElementSource`` over IUIAutomationElements for ``core.ui.walk``.

    ``refresh`` rebuilds the cache before describing, for re-reads after an
    action; a walk reads the cache its FindAllBuildCache filled.
    """

    def __init__(self, refresh: bool = False) -> None:
        from ..ui import uia as table
        self.u = _uia()
        self.refresh = refresh
        self.normalize = table.normalize

    def _fresh(self, element):
        if not self.refresh:
            return element
        try:
            return element.BuildUpdatedCache(self.u.cache)
        except Exception:
            # Describe from the old cache; the element may have just gone away.
            log.debug("BuildUpdatedCache failed", exc_info=True)
            return element

    def describe(self, element):
        from ..ui.model import RawNode
        from ..ui.uia import TOGGLE_STATES, expand_state

        u = self.u
        element = self._fresh(element)
        native = u.control_types.get(_safe(lambda: element.CachedControlType), "Unknown")
        patterns = [p for p, prop in u.available.items() if _cached(element, prop)]
        protected = bool(_safe(lambda: element.CachedIsPassword))
        focusable = bool(_safe(lambda: element.CachedIsKeyboardFocusable))
        actions = [p for p in patterns if p != "Text"]
        if focusable:
            actions.append("Focusable")

        bounds = None
        rect = _safe(lambda: element.CachedBoundingRectangle)
        if rect is not None and rect.right > rect.left and rect.bottom > rect.top:
            bounds = (int(rect.left), int(rect.top), int(rect.right - rect.left),
                      int(rect.bottom - rect.top))

        props = u.state_props
        checked = TOGGLE_STATES.get(_cached(element, props["toggle"])) if "Toggle" in patterns else None
        selected = _cached(element, props["selected"]) if "SelectionItem" in patterns else None
        expanded = expandable = None
        if "ExpandCollapse" in patterns:
            expanded, expandable = expand_state(_cached(element, props["expand"]))

        value = ""
        settable = False
        if "RangeValue" in patterns:
            settable = _cached(element, props["range_ro"]) is False
            number = _cached(element, props["range_value"])
            if isinstance(number, float) and number.is_integer():
                number = int(number)
            value = "" if number is None else str(number)
        elif "Value" in patterns or "Text" in patterns:
            if "Value" in patterns:
                settable = _cached(element, props["value_ro"]) is False
            if not protected:
                value = self._text(element, patterns)

        subrole = "dialog" if native == "Window" and _cached(element, _IS_DIALOG_PROPERTY_ID) else ""
        return RawNode(
            native_role=native, subrole=subrole,
            name=str(_safe(lambda: element.CachedName) or "")[:_NAME_MAX],
            value=value, id=str(_safe(lambda: element.CachedAutomationId) or ""),
            actions=actions, bounds=bounds,
            enabled=_flag(_safe(lambda: element.CachedIsEnabled)),
            focused=_flag(_safe(lambda: element.CachedHasKeyboardFocus)),
            selected=_flag(selected), expanded=expanded, expandable=expandable,
            value_settable=settable, protected=protected, checked=checked,
        )

    def _text(self, element, patterns) -> str:
        """A bounded value: TextPattern ranges take a length; ValuePattern
        can't, so a Value-only element is read whole and cut."""
        u = self.u
        if "Text" in patterns:
            with best_effort("reading TextPattern", log):
                pat = element.GetCurrentPattern(u.pattern_ids["Text"]).QueryInterface(
                    u.mod.IUIAutomationTextPattern)
                return str(pat.DocumentRange.GetText(_VALUE_MAX) or "")
        if "Value" in patterns:
            with best_effort("reading ValuePattern", log):
                pat = element.GetCurrentPattern(u.pattern_ids["Value"]).QueryInterface(
                    u.mod.IUIAutomationValuePattern)
                return str(pat.CurrentValue or "")[:_VALUE_MAX]
        return ""

    def children(self, element) -> list:
        u = self.u
        try:
            arr = element.FindAllBuildCache(u.children_scope, u.condition, u.cache)
        except Exception:
            log.debug("FindAllBuildCache failed", exc_info=True)
            return []
        if not arr:
            return []
        return [arr.GetElement(i) for i in range(arr.Length)]

    def child_count(self, element) -> int:
        u = self.u
        try:
            arr = element.FindAll(u.children_scope, u.condition)
            return int(arr.Length) if arr else 0
        except Exception:
            log.debug("FindAll failed", exc_info=True)
            return 0


def _safe(fn):
    try:
        return fn()
    except Exception:
        log.debug("UIA call failed", exc_info=True)
        return None


def _flag(v) -> bool | None:
    return bool(v) if isinstance(v, (bool, int)) else None


class _UIAActor(_UIASource):
    """``ActionAdapter``: performs verbs through control patterns; core.ui.act confirms them."""

    def __init__(self) -> None:
        super().__init__(refresh=True)

    def _pattern(self, element, name: str):
        iface = getattr(self.u.mod, f"IUIAutomation{name}Pattern")
        pat = element.GetCurrentPattern(self.u.pattern_ids[name])
        if not pat:
            raise LookupError(f"the element no longer supports the {name} pattern")
        return pat.QueryInterface(iface)

    def _call(self, what: str, fn, **effect):
        from ..ui.act import Effect
        try:
            _retry_not_enabled(fn)
        except Exception as e:
            log.debug("%s failed", what, exc_info=True)
            hr = _hresult(e)
            if hr in _UNCERTAIN_HRESULTS:
                return Effect(warning=f"{what} answered HRESULT 0x{hr & 0xFFFFFFFF:08X}", **effect)
            return Effect(f"{what} failed ({e})")
        return Effect(**effect)

    def perform(self, element, verb, node, params):
        from ..ui.act import Effect

        if verb == "press":
            return self._call("Invoke", lambda: self._pattern(element, "Invoke").Invoke())
        if verb == "toggle":
            return self._call("Toggle", lambda: self._pattern(element, "Toggle").Toggle())
        if verb == "select":
            return self._call("SelectionItem.Select",
                              lambda: self._pattern(element, "SelectionItem").Select())
        if verb == "expand":
            want = bool(params.get("expand", True))
            pat = lambda: self._pattern(element, "ExpandCollapse")  # noqa: E731
            # Menus and combo drop-downs open popups whose state some providers
            # don't update; tree items keep a readable state.
            verifiable = node.role == "tree_item"
            if want:
                return self._call("ExpandCollapse.Expand", lambda: pat().Expand(), verifiable=verifiable)
            return self._call("ExpandCollapse.Collapse", lambda: pat().Collapse(), verifiable=verifiable)
        if verb == "set_value":
            value = params.get("value", "")
            if "RangeValue" in self.describe_patterns(element):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return Effect(f"a slider value must be a number, got {value!r}")
                return self._call("RangeValue.SetValue",
                                  lambda: self._pattern(element, "RangeValue").SetValue(number))
            _safe(element.SetFocus)
            return self._call("Value.SetValue",
                              lambda: self._pattern(element, "Value").SetValue(str(value)))
        if verb == "focus":
            if node.role in ("window", "dialog"):
                hwnd = _safe(lambda: element.CachedNativeWindowHandle)
                if hwnd:
                    _raise_window(int(hwnd))
                return self._call("SetFocus", element.SetFocus, verifiable=False)
            return self._call("SetFocus", element.SetFocus)
        if verb == "scroll_to":
            return self._call("ScrollItem.ScrollIntoView",
                              lambda: self._pattern(element, "ScrollItem").ScrollIntoView(),
                              verifiable=False)
        return Effect(f"unknown verb {verb!r}")

    def describe_patterns(self, element) -> list[str]:
        return [p for p, prop in self.u.available.items() if _cached(element, prop)]


# ── Windows and processes ───────────────────────────────────────────

_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _kernel32():
    """kernel32 with a preserved last error and pointer-sized handles."""
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    return k


def _raise_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def _pid_alive(pid: int) -> bool:
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied still means it exists (an elevated process).
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _process_name(pid: int) -> str:
    """Executable stem (``Notepad``), or "" when the process can't be opened."""
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.splitext(os.path.basename(buf.value))[0]
    finally:
        kernel32.CloseHandle(handle)


from ..ui.target import pick_window  # noqa: E402  (main = unowned, not a tool window)

_DWMWA_CLOAKED = 14
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080


def _enum_windows() -> list[tuple[int, str, int, bool]]:
    """``(hwnd, title, pid, main)`` of visible, titled, uncloaked top-level
    windows, front to back. ``main``: no owner and not a tool window.

    Win32 rather than the UIA root's children: UIA files an owned dialog
    (Notepad's Save as) under its owner, so it isn't a child of the desktop.
    """
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    found: list[tuple[int, str, int, bool]] = []

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        cloaked = wintypes.DWORD()
        if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked),
                                        ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return True  # suspended UWP frames and windows on other virtual desktops
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        tool = bool(user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW)
        main = not user32.GetWindow(hwnd, _GW_OWNER) and not tool
        found.append((int(hwnd), buf.value, int(pid.value), main))
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(proc, 0)
    return found


def _top_windows(u: _UIA) -> list:
    return [el for hwnd, *_ in _enum_windows() if (el := _window_element(u, hwnd)) is not None]


def _window_element(u: _UIA, hwnd: int):
    try:
        return u.iuia.ElementFromHandleBuildCache(hwnd, u.cache)
    except Exception:
        log.debug("ElementFromHandleBuildCache(%s) failed", hwnd, exc_info=True)
        return None


def _ui_target(u: _UIA, title: str | None):
    """The top-level window element for ``title``; the foreground window without one."""
    if not title:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return _window_element(u, hwnd) if hwnd else None
    windows = _enum_windows()
    i = pick_window([(t, _process_name(pid), main) for _, t, pid, main in windows], title)
    return None if i is None else _window_element(u, windows[i][0])


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        log.debug("IsUserAnAdmin failed", exc_info=True)
        return False


# ── ui_walk / ui_act ────────────────────────────────────────────────


async def ui_walk(
    title: str | None = None,
    depth: int = 8,
    max_children: int = 50,
    include_menubar: bool = False,
    timeout: float = 10.0,
    web_search: dict | None = None,
):
    """Walk the target window into a ``UIRead`` on the UIA thread.

    Menu bars are part of a Windows window's own tree; they are read as
    collapsed nodes unless ``include_menubar``. ``web_search`` is accepted
    for interface parity and ignored (no native search fast path yet).
    """
    return await _run(_ui_walk_sync, title, depth, max_children, include_menubar, timeout)


def _ui_walk_sync(title, depth, max_children, include_menubar, timeout):
    import time

    from ..ui.model import UIRead
    from ..ui.walk import WalkStats, walk

    start = time.monotonic()
    u = _uia()
    win = _ui_target(u, title)
    if win is None:
        if title and not _is_elevated():
            log.info("ui_walk: no window for %r (elevated windows are hidden from an "
                     "unelevated server by UIPI)", title)
        return None

    source = _UIASource()
    pid = int(_safe(lambda: win.CachedProcessId) or 0)
    hwnd = _safe(lambda: win.CachedNativeWindowHandle)

    def skip_menubars(element, node) -> bool:
        if node.native_role == "MenuBar" and node.path:
            node.menubar_skipped = source.child_count(element)
            return False
        return True

    stats = WalkStats()
    window, _ = walk(source, win, depth=depth, max_children=max_children,
                     deadline=start + timeout, stats=stats,
                     descend=None if include_menubar else skip_menubars)
    return UIRead(
        app=_process_name(pid), pid=pid, window_id=int(hwnd) if hwnd else None,
        window_title=str(_safe(lambda: win.CachedName) or ""), window=window,
        nodes=stats.nodes, elapsed=time.monotonic() - start, timeout=timeout,
        budget_hit=stats.budget_hit,
    )


async def ui_act(locator, verb: str, params: dict, timeout: float = 5.0):
    """Resolve ``locator`` and run ``verb`` on it; returns a ``core.ui.act.ActOutcome``."""
    return await _run(_ui_act_sync, locator, verb, params, timeout)


def _locator_root(u: _UIA, loc):
    """The live window a locator was read from, by HWND (titles change)."""
    user32 = ctypes.windll.user32
    if loc.window_id is not None:
        hwnd = int(loc.window_id)
        if not user32.IsWindow(hwnd):
            return None
        win = _window_element(u, hwnd)
        if win is None:
            return None
        # A reused HWND belongs to another process. UWP apps are read and
        # acted on through the same ApplicationFrameHost frame, so its pid matches.
        pid = _safe(lambda: win.CachedProcessId)
        if pid and loc.pid and int(pid) != loc.pid:
            return None
        return win
    for w in _top_windows(u):
        if str(_safe(lambda w=w: w.CachedName) or "") == loc.window_title:
            return w
    return None


def _ui_act_sync(loc, verb, params, timeout):
    import time

    from ..ui.act import ActOutcome, run_action
    from ..ui.resolve import resolve

    if not _pid_alive(loc.pid):
        return ActOutcome("stale_ref", detail="its app has quit", window_gone=True)
    u = _uia()
    root = _locator_root(u, loc)
    if root is None:
        return ActOutcome("stale_ref", detail="its window has closed", window_gone=True)

    actor = _UIAActor()
    res = resolve(actor, root, loc, deadline=time.monotonic() + timeout)
    if res.status == "stale":
        return ActOutcome("stale_ref", detail="that element is no longer in the window",
                          candidates=res.candidates)
    if res.status == "ambiguous":
        return ActOutcome("ambiguous_ref", detail="the element moved and several elements now match it",
                          candidates=res.candidates)
    return run_action(actor, res.element, res.node, verb, params)
