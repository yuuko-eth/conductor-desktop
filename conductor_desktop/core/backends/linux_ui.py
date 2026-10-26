"""Linux ``ui_*`` backend: AT-SPI accessibles → ``core.ui`` walker, resolver and actions.

Shared by the X11, GNOME and wlroots backends. AT-SPI is a D-Bus protocol,
so reading and acting work the same under any display server; only screen
positions differ. GTK 4 reports extents relative to its window (screen
extents are always 0,0), so a GTK 4 window's origin comes from the display
server: X11 geometry plus ``_GTK_FRAME_EXTENTS``, else GNOME Shell's frame
rect. Without either (Wayland with Shell Eval gated) its elements have no
bounds.

All libatspi calls run on one worker thread: libatspi is not thread-safe, and
the event loop must not block on a slow app.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import shutil
import subprocess
import threading
import warnings
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("conductor_desktop.linux_ui")

# Atspi.Action.get_action_name is deprecated in libatspi 2.52 but its
# replacement, get_name, is shadowed by Accessible.get_name in PyGObject.
warnings.filterwarnings("ignore", message=r".*get_action_name is deprecated", category=DeprecationWarning)

_VALUE_MAX = 500
_NAME_MAX = 200
_MAX_ACTIONS = 20
# A GTK 3 table that manages its descendants can report thousands of rows.
_MAX_CHILDREN = 500
# GTK 3 reports elements that aren't on screen at INT_MIN.
_OFFSCREEN = -(1 << 30)

# The shell's own accessible, and mutter's decorations for X11 clients, whose
# titles repeat their client's.
_SKIP_APPS = frozenset({"gnome-shell", "mutter-x11-frames"})
_WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser", "color chooser",
                           "font chooser"})
_DIALOG_ROLES = _WINDOW_ROLES - {"frame", "window"}
# Roles whose Action names aren't read: GTK 4 lists every GAction of a window
# or label there (win.close, clipboard.copy), one D-Bus call each.
_NO_ACTION_ROLES = frozenset({
    "panel", "filler", "frame", "window", "dialog", "label", "static", "section", "scroll pane",
    "viewport", "separator", "tool bar", "status bar", "page tab list", "list", "list box",
    "table", "tree", "tree table", "menu bar", "heading", "paragraph", "image", "icon", "text",
    "entry", "password text", "spin button", "document text", "document web", "document frame",
    "layered pane", "split pane", "grouping", "form", "landmark", "scroll bar",
})
# Text read as the value. A label's text is its name, so it is read only when
# the name is empty.
_TEXT_VALUE_ROLES = frozenset({"entry", "text", "spin button", "combo box", "terminal",
                               "document text", "editbar"})
_LABEL_ROLES = frozenset({"label", "static", "caption", "heading", "paragraph"})
_NUMERIC_ROLES = frozenset({"slider", "spin button"})
_CHECK_ROLES = frozenset({"check box", "switch", "toggle button", "check menu item"})
_RADIO_ROLES = frozenset({"radio button", "radio menu item"})
_PRESS_ORDER = ("click", "press", "activate", "jump", "open")

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _run(fn, *args):
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="atspi")
    return asyncio.get_running_loop().run_in_executor(_executor, fn, *args)


# libatspi's per-call D-Bus timeout. One hung app (a stopped process still
# registered on the bus) otherwise stalls every window lookup for its default.
# Not too short: a GTK 3 app running a modal dialog answers in seconds, and
# cutting it off there hides the dialog we were asked to read. The strike
# counter below is what handles an app that never answers.
_CALL_TIMEOUT_MS = 5000
# Listing every app's windows uses a shorter one: a dead app costs this per
# call, and _target retries at the full timeout when a title didn't match.
_ENUM_TIMEOUT_MS = 1500
# An app this slow to list its windows, twice in a row, is skipped for a while.
_SLOW_APP_SECONDS = 1.0
_SLOW_APP_STRIKES = 2
_SLOW_APP_SKIP_SECONDS = 30.0


def _atspi():
    """Atspi, configured on first use (from the worker thread).

    libatspi isn't thread-safe: every call in this process goes through the
    worker thread. Its D-Bus connections sit on GLib's default main context,
    so nothing else here may run that context: the tray, whose GTK loop did,
    runs in a process of its own on Linux (tray_child.py; the server crashed
    in libglib or hung). Don't give libatspi a private main context instead:
    calls hung (2026-09) or crashed on the first call (2026-10).
    """
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    if not getattr(_atspi, "configured", False):
        _set_timeout(Atspi, _CALL_TIMEOUT_MS)
        _atspi.configured = True
    return Atspi


def _set_timeout(A, ms: int) -> None:
    _safe(lambda: A.set_timeout(ms, -1))


class SlowApps:
    """Apps that answer too slowly twice in a row, skipped until their time is up.

    One strike isn't enough: a GTK 3 app stops answering AT-SPI while it runs a
    modal dialog, and hiding it then makes its own dialog unreadable. A stopped
    process still registered on the bus stays slow, so it's skipped on the
    second read.
    """

    def __init__(self, clock=None) -> None:
        import time
        self.clock = clock or time.monotonic
        self._until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._names: dict[str, str] = {}

    def skip(self, bus: str) -> bool:
        until = self._until.get(bus)
        if until is None:
            return False
        if self.clock() >= until:
            del self._until[bus]
            return False
        return True

    def name(self, bus: str) -> str:
        return self._names.get(bus) or bus

    def record(self, bus: str, seconds: float, name: str = "", answered: bool = True) -> None:
        if not bus:
            return
        if name:
            self._names[bus] = name
        if not answered:
            # A process that has gone without deregistering: it never answers,
            # so one strike is enough.
            log.warning("AT-SPI app %s (%s) did not answer in %.1fs; skipping it for %.0fs",
                        name or "?", bus, seconds, _SLOW_APP_SKIP_SECONDS)
            self._until[bus] = self.clock() + _SLOW_APP_SKIP_SECONDS
            self._strikes.pop(bus, None)
            return
        if seconds < _SLOW_APP_SECONDS:
            self._strikes.pop(bus, None)
            return
        strikes = self._strikes.get(bus, 0) + 1
        self._strikes[bus] = strikes
        if strikes < _SLOW_APP_STRIKES:
            log.info("AT-SPI app %s (%s) took %.1fs to list its windows (strike %d of %d)",
                     name or "?", bus, seconds, strikes, _SLOW_APP_STRIKES)
            return
        log.warning("AT-SPI app %s (%s) took %.1fs to list its windows twice; skipping it "
                    "for %.0fs", name or "?", bus, seconds, _SLOW_APP_SKIP_SECONDS)
        self._until[bus] = self.clock() + _SLOW_APP_SKIP_SECONDS
        self._strikes.pop(bus, None)


_slow_apps = SlowApps()


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        log.debug("AT-SPI call %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


# ── Pure helpers ────────────────────────────────────────────────────


def screen_bounds(rect: tuple[int, int, int, int] | None, *, relative: bool,
                  origin: tuple[int, int] | None) -> tuple[int, int, int, int] | None:
    """Screen bounds from extents as read; window-relative ones need the window's origin."""
    if rect is None:
        return None
    x, y, w, h = rect
    if w <= 0 or h <= 0 or x <= _OFFSCREEN or y <= _OFFSCREEN:
        return None
    if relative:
        if origin is None:
            return None
        return (origin[0] + x, origin[1] + y, w, h)
    return (x, y, w, h)


def parse_frame_extents(xprop_out: str) -> tuple[int, int, int, int] | None:
    """``(left, right, top, bottom)`` from ``xprop -id W _GTK_FRAME_EXTENTS``."""
    m = re.search(r"=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", xprop_out)
    return tuple(int(v) for v in m.groups()) if m else None  # type: ignore[return-value]


def parse_xwininfo(out: str) -> tuple[int, int] | None:
    x = re.search(r"Absolute upper-left X:\s*(-?\d+)", out)
    y = re.search(r"Absolute upper-left Y:\s*(-?\d+)", out)
    return (int(x.group(1)), int(y.group(1))) if x and y else None


def parse_stacking(wmctrl_out: str, xprop_out: str) -> dict[tuple[int, str], tuple[int, int]]:
    """``(pid, title) → (z, xid)`` from ``wmctrl -lp`` and the root's
    ``_NET_CLIENT_LIST_STACKING``; z=0 is the topmost window."""
    ids = [int(tok, 16) for tok in re.findall(r"0x[0-9a-fA-F]+", xprop_out.partition("#")[2])]
    z_of = {xid: z for z, xid in enumerate(reversed(ids))}
    found: dict[tuple[int, str], tuple[int, int]] = {}
    for line in wmctrl_out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            xid, pid = int(parts[0], 16), int(parts[2])
        except ValueError:
            continue
        title = parts[4] if len(parts) > 4 else ""
        z = z_of.get(xid)
        if z is not None and ((pid, title) not in found or z < found[(pid, title)][0]):
            found[(pid, title)] = (z, xid)
    return found


def order_windows(windows: list[tuple[int, str, bool]],
                  stacking: dict[tuple[int, str], tuple[int, int]]) -> list[int]:
    """Indices of ``(pid, title, active)`` front to back: the active window,
    then X11 stacking order, then AT-SPI order."""
    def key(i: int):
        pid, title, active = windows[i]
        z = stacking.get((pid, title))
        return (0 if active else 1, z[0] if z else 1 << 20, i)
    return sorted(range(len(windows)), key=key)


def _number(v) -> str:
    if not isinstance(v, (int, float)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _uncertain(e: Exception) -> bool:
    """A D-Bus timeout: the app may still act (a click that opens a modal dialog)."""
    text = str(e).lower()
    return "timeout" in text or "timed out" in text or "noreply" in text or "no reply" in text


# ── Display server: stacking, origins, raising ──────────────────────


def _cmd(args: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and shutil.which("wmctrl") is not None


def _x11_stacking() -> dict[tuple[int, str], tuple[int, int]]:
    if not _x11():
        return {}
    return parse_stacking(_cmd(["wmctrl", "-lp"]),
                          _cmd(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"]))


def _hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


def parse_hypr_clients(out: str) -> dict[tuple[int, str], tuple[int, int]]:
    """``(pid, title) → (x, y)`` from ``hyprctl -j clients``.

    Hyprland is the one Wayland compositor that hands out window origins, so
    GTK 4's window-relative extents can be made into screen coordinates here
    the way ``xwininfo`` does it on X11. ``at`` is already the content origin
    — there is no server-side frame to subtract.
    """
    import json

    try:
        clients = json.loads(out or "[]")
    except (ValueError, TypeError):
        return {}
    origins: dict[tuple[int, str], tuple[int, int]] = {}
    for c in clients:
        try:
            at = c["at"]
            origins[(int(c["pid"]), c.get("title") or "")] = (int(at[0]), int(at[1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return origins


def _hypr_origin(pid: int, title: str) -> tuple[int, int] | None:
    if not _hyprland():
        return None
    return parse_hypr_clients(_cmd(["hyprctl", "-j", "clients"])).get((pid, title))


def _shell_eval(js: str) -> str | None:
    """GNOME Shell Eval's result string, or None when Eval is gated or absent."""
    out = _cmd(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.Eval", js])
    m = re.match(r"\(true, '(.*)'\)\s*$", out.strip(), re.DOTALL)
    return m.group(1) if m else None


def _js_window(pid: int, title: str) -> str:
    import json
    return (f"global.get_window_actors().map(a => a.meta_window)"
            f".find(w => w.get_pid() === {int(pid)} && w.get_title() === {json.dumps(title)})")


def _window_origin(pid: int, title: str, stacking) -> tuple[int, int] | None:
    """Screen position of a GTK 4 window's content (its shadow excluded)."""
    hit = stacking.get((pid, title))
    if hit is not None:
        xid = hex(hit[1])
        pos = parse_xwininfo(_cmd(["xwininfo", "-id", xid]))
        if pos is not None:
            left, _, top, _ = parse_frame_extents(_cmd(["xprop", "-id", xid, "_GTK_FRAME_EXTENTS"])) or (0, 0, 0, 0)
            return (pos[0] + left, pos[1] + top)
    hypr = _hypr_origin(pid, title)
    if hypr is not None:
        return hypr
    out = _shell_eval(f"(() => {{ const w = {_js_window(pid, title)}; "
                      f"if (!w) return null; const r = w.get_frame_rect(); return [r.x, r.y]; }})()")
    m = re.match(r"\[(-?\d+),\s*(-?\d+)\]", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _raise_window(pid: int, title: str) -> bool:
    """Raise a window through X11 or GNOME Shell; False when neither can.

    AT-SPI itself can't raise windows, and on Wayland with Shell Eval gated
    nothing else can: callers must not claim the window was raised.
    Hyprland is the exception among Wayland compositors — ``hyprctl dispatch
    focuswindow`` raises, so ``ui_focus`` means what it says there.
    """
    hit = _x11_stacking().get((pid, title))
    if hit is not None:
        _cmd(["wmctrl", "-i", "-a", hex(hit[1])])
        return True
    if _hyprland() and (pid, title) in parse_hypr_clients(_cmd(["hyprctl", "-j", "clients"])):
        return _cmd(["hyprctl", "dispatch", "focuswindow", f"pid:{int(pid)}"]).strip() == "ok"
    out = _shell_eval(f"(() => {{ const w = {_js_window(pid, title)}; "
                      f"if (w) w.activate(global.get_current_time()); return !!w; }})()")
    return out == "true"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── Windows ─────────────────────────────────────────────────────────


@dataclass
class _Win:
    acc: Any
    title: str
    app: str
    pid: int
    main: bool
    active: bool
    key: str
    relative: bool  # GTK 4: extents are relative to the window


def _is_gtk4(app) -> bool:
    name = (_safe(app.get_toolkit_name) or "").lower()
    version = _safe(app.get_toolkit_version) or ""
    return name == "gtk" and str(version).startswith("4")


def _window_key(acc) -> str:
    """Bus name + object path: stable for the window's life, unlike its title."""
    bus = _safe(lambda: acc.app.bus_name) or ""
    return f"{bus}{_safe(lambda: acc.path) or ''}"


def _windows(A, skipped: list[str] | None = None, unanswered: list[str] | None = None,
             record_dead: bool = True) -> list[_Win]:
    import time

    desktop = A.get_desktop(0)
    own = os.getpid()
    found: list[_Win] = []
    for i in range(_safe(desktop.get_child_count, 0)):
        app = _safe(lambda i=i: desktop.get_child_at_index(i))
        if app is None:
            continue
        # Key by the app's D-Bus name, read from its Application object: it is
        # a local field, so a process that has gone still has one, and it is
        # unique per app (``app.bus_name`` is None here, and ``app.path`` is
        # the same root path for every app — keying on that made one hung app
        # share a key with every healthy one).
        bus = (_safe(lambda app=app: app.app.bus_name) or _safe(lambda app=app: app.bus_name)
               or f"index:{i}")
        if _slow_apps.skip(bus):
            if skipped is not None:
                skipped.append(_slow_apps.name(bus))
            continue
        start = time.monotonic()
        # The child count comes first, and it decides whether the app answered
        # at all: libatspi reports a timed-out call as -1 (and a name as "")
        # rather than raising, so a process that quit without deregistering
        # looks like an app with no windows. One call is enough to tell, and
        # the rest of this pass — including the toolkit query — skips it.
        count = _safe(app.get_child_count)
        if count is None or count < 0:
            _slow_apps.record(bus, time.monotonic() - start, "", answered=not record_dead)
            if unanswered is not None:
                unanswered.append(_slow_apps.name(bus))
            continue
        name = _safe(app.get_name) or ""
        pid = int(_safe(app.get_process_id) or 0)
        if name in _SKIP_APPS or pid == own:
            continue
        relative = _is_gtk4(app)
        for j in range(count):
            win = _safe(lambda app=app, j=j: app.get_child_at_index(j))
            role = _safe(win.get_role_name) if win is not None else None
            if role not in _WINDOW_ROLES:
                continue
            states = _safe(win.get_state_set)
            active = bool(states is not None and states.contains(A.StateType.ACTIVE))
            found.append(_Win(win, (_safe(win.get_name) or "").strip(), name, pid,
                              role not in _DIALOG_ROLES, active, _window_key(win), relative))
        _slow_apps.record(bus, time.monotonic() - start, name)
    return found


def _ordered_windows(A, skipped: list[str] | None = None, unanswered: list[str] | None = None,
                     record_dead: bool = True):
    wins = _windows(A, skipped, unanswered, record_dead)
    stacking = _x11_stacking()
    order = order_windows([(w.pid, w.title, w.active) for w in wins], stacking)
    return [wins[i] for i in order], stacking


def _pick(wins: list[_Win], title: str | None, stacking) -> _Win | None:
    from ..ui.target import pick_window

    if not wins:
        return None
    if not title:
        top = wins[0]
        return top if (top.active or (top.pid, top.title) in stacking) else None
    i = pick_window([(w.title, w.app, w.main) for w in wins], title)
    return None if i is None else wins[i]


def _target(A, title: str | None, skipped: list[str] | None = None,
            unanswered: list[str] | None = None):
    """``(window, stacking)`` for ``title``; the active (else topmost) window without one.

    Enumeration runs at the short timeout so a dead app is cheap. A named
    window that didn't match gets one retry at the full timeout: an app
    running a modal dialog answers in seconds, and its dialog is exactly what
    the caller is asking for."""
    unanswered = unanswered if unanswered is not None else []
    _set_timeout(A, _ENUM_TIMEOUT_MS)
    wins, stacking = _ordered_windows(A, skipped, unanswered, record_dead=False)
    hit = _pick(wins, title, stacking)
    if hit is None and title and unanswered:
        log.info("no window matched %r; retrying at the full timeout for %s",
                 title, ", ".join(sorted(set(unanswered))))
        _set_timeout(A, _CALL_TIMEOUT_MS)
        wins, stacking = _ordered_windows(A, skipped)
        hit = _pick(wins, title, stacking)
    _set_timeout(A, _CALL_TIMEOUT_MS)
    return hit, stacking


# ── Source and actor ────────────────────────────────────────────────


class _ATSPISource:
    """``ElementSource`` over Atspi.Accessible for ``core.ui.walk``.

    ``refresh`` drops libatspi's cached name and states before describing, for
    re-reads after an action.
    """

    def __init__(self, relative: bool = False, origin: tuple[int, int] | None = None,
                 refresh: bool = False) -> None:
        from ..ui import atspi as table
        self.A = _atspi()
        self.relative = relative
        self.origin = origin
        self.refresh = refresh
        self.normalize = table.normalize

    def describe(self, acc):
        from ..ui.model import RawNode

        A = self.A
        S = A.StateType
        if self.refresh:
            _safe(acc.clear_cache)
        role = _safe(acc.get_role_name) or "unknown"
        # GTK 3 pads menu item names with spaces before the accelerator.
        name = (_safe(acc.get_name) or "").strip()[:_NAME_MAX]
        ifaces = set(_safe(acc.get_interfaces) or ())
        st = _safe(acc.get_state_set)

        def has(state) -> bool:
            return bool(st is not None and _safe(lambda: st.contains(state)))

        actions: list[str] = []
        if "Action" in ifaces and role not in _NO_ACTION_ROLES:
            for i in range(min(_safe(acc.get_n_actions, 0), _MAX_ACTIONS)):
                action = (_safe(lambda i=i: acc.get_action_name(i)) or "").strip().lower()
                if action and "." not in action:
                    actions.append(action)
        for marker, state in (("Selectable", S.SELECTABLE), ("HasPopup", S.HAS_POPUP),
                              ("Focusable", S.FOCUSABLE)):
            if has(state):
                actions.append(marker)
        editable = "EditableText" in ifaces and has(S.EDITABLE) and not has(S.READ_ONLY)
        if editable:
            actions.append("EditableText")
        numeric = "Value" in ifaces and role in _NUMERIC_ROLES
        protected = role == "password text"

        value = ""
        if not protected:
            if "Text" in ifaces and (role in _TEXT_VALUE_ROLES or (not name and role in _LABEL_ROLES)):
                value = self._text(acc)
            if not value and numeric:
                value = _number(_safe(acc.get_current_value))

        bounds = None
        if has(S.SHOWING) or role in _WINDOW_ROLES:
            coords = A.CoordType.WINDOW if self.relative else A.CoordType.SCREEN
            r = _safe(lambda: acc.get_extents(coords))
            bounds = screen_bounds((r.x, r.y, r.width, r.height) if r is not None else None,
                                   relative=self.relative, origin=self.origin)

        checked = None
        selected = has(S.SELECTED)
        if role in _RADIO_ROLES:
            selected = has(S.CHECKED)
        elif role in _CHECK_ROLES or has(S.CHECKABLE):
            checked = 2 if has(S.INDETERMINATE) else 1 if (has(S.CHECKED) or has(S.PRESSED)) else 0

        return RawNode(
            native_role=role, subrole="multi-line" if has(S.MULTI_LINE) else "",
            name=name, value=value, id=_safe(acc.get_accessible_id) or "",
            actions=actions, bounds=bounds,
            enabled=None if st is None else (has(S.SENSITIVE) or has(S.ENABLED)),
            focused=has(S.FOCUSED), selected=selected, expanded=has(S.EXPANDED),
            value_settable=editable or (numeric and not has(S.READ_ONLY)),
            protected=protected, checked=checked, expandable=has(S.EXPANDABLE),
        )

    def _text(self, acc) -> str:
        """A bounded text read: the length first, never the whole buffer.

        Through ``Atspi.Text`` explicitly: on an Accessible, ``get_text`` is
        the getter for the Text interface itself (libatspi 2.52) and takes no
        range.
        """
        Text = self.A.Text
        count = _safe(lambda: Text.get_character_count(acc), 0) or 0
        if count <= 0:
            return ""
        return str(_safe(lambda: Text.get_text(acc, 0, min(count, _VALUE_MAX))) or "")

    def children(self, acc) -> list:
        count = min(_safe(acc.get_child_count, 0) or 0, _MAX_CHILDREN)
        kids = (_safe(lambda i=i: acc.get_child_at_index(i)) for i in range(count))
        return [k for k in kids if k is not None]

    def child_count(self, acc) -> int:
        return int(_safe(acc.get_child_count, 0) or 0)


class _ATSPIActor(_ATSPISource):
    """``ActionAdapter``: performs verbs through AT-SPI interfaces; core.ui.act confirms them."""

    def __init__(self, win: _Win, origin: tuple[int, int] | None) -> None:
        super().__init__(win.relative, origin, refresh=True)
        self.win = win

    def _call(self, what: str, fn, **effect):
        from ..ui.act import Effect
        try:
            ok = fn()
        except Exception as e:
            log.debug("%s failed", what, exc_info=True)
            if _uncertain(e):
                return Effect(warning=f"{what} got no reply in time", **effect)
            return Effect(f"{what} failed ({e})")
        if ok is False:
            return Effect(f"{what} was refused by the app")
        return Effect(**effect)

    def _action(self, acc, names) -> tuple[int, str] | None:
        indices: dict[str, int] = {}
        for i in range(_safe(acc.get_n_actions, 0) or 0):
            indices.setdefault((_safe(lambda i=i: acc.get_action_name(i)) or "").strip().lower(), i)
        for name in names:
            if name in indices:
                return indices[name], name
        return None

    def _do(self, acc, names, **effect):
        from ..ui.act import Effect
        hit = self._action(acc, names)
        if hit is None:
            return Effect(f"the element no longer offers the {names[0]!r} action")
        i, name = hit
        return self._call(f'action "{name}"', lambda: acc.do_action(i), **effect)

    def perform(self, acc, verb, node, params):
        from ..ui.act import Effect

        if verb == "press":
            return self._do(acc, _PRESS_ORDER)
        if verb == "toggle":
            return self._do(acc, ("toggle", *_PRESS_ORDER))
        if verb == "select":
            # The container with Selection may be a few levels up: a GTK 3
            # tree view's text cell sits inside its row's cell.
            child = acc
            for _ in range(3):
                parent = _safe(child.get_parent)
                if parent is None:
                    break
                if "Selection" in (_safe(parent.get_interfaces) or ()):
                    index = _safe(child.get_index_in_parent)
                    if index is not None and index >= 0:
                        effect = self._call("Selection.select_child",
                                            lambda parent=parent, index=index:
                                            parent.select_child(index))
                        # GTK 3 answers false when the row is already selected.
                        if effect.error and "selected" in self.normalize(self.describe(acc))[2]:
                            return Effect()
                        return effect
                    break
                child = parent
            return self._do(acc, _PRESS_ORDER)
        if verb == "expand":
            want = bool(params.get("expand", True))
            # Tree rows and cells flip with "expand or contract", and keep a state.
            if self._action(acc, ("expand or contract",)) is not None:
                if ("expanded" in node.states) == want:
                    return Effect()
                return self._do(acc, ("expand or contract",))
            # Menus and pop-up buttons open by pressing; the popup's state
            # isn't on the element.
            return self._do(acc, _PRESS_ORDER, verifiable=False)
        if verb == "set_value":
            value = params.get("value", "")
            if node.role == "slider":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return Effect(f"a slider value must be a number, got {value!r}")
                return self._call("Value.set_current_value", lambda: acc.set_current_value(number))
            _safe(acc.grab_focus)
            return self._call("EditableText.set_text_contents",
                              lambda: acc.set_text_contents(str(value)))
        if verb == "focus":
            if node.role in ("window", "dialog"):
                if not _raise_window(self.win.pid, self.win.title):
                    return Effect(
                        "can't raise windows here: no X11 window list, and GNOME Shell Eval is "
                        "gated (on Wayland, enable Shell unsafe mode or raise it yourself)")
                _safe(acc.grab_focus)
                return Effect(verifiable=False)
            return self._call("Component.grab_focus", acc.grab_focus)
        if verb == "scroll_to":
            return self._call("Component.scroll_to",
                              lambda: acc.scroll_to(self.A.ScrollType.ANYWHERE), verifiable=False)
        return Effect(f"unknown verb {verb!r}")


# ── ui_walk / ui_act ────────────────────────────────────────────────


def _is_wrapper(node) -> bool:
    """An unnamed, verb-less container: free for the depth count (GTK 4 panels)."""
    return node.role in ("group", "scroll_area", "other") and not node.name and not node.verbs


async def ui_walk(
    title: str | None = None,
    depth: int = 8,
    max_children: int = 50,
    include_menubar: bool = False,
    timeout: float = 10.0,
    web_search: dict | None = None,
):
    """Walk the target window into a ``UIRead`` on the AT-SPI thread.

    GTK 3 and Qt menu bars are part of the window's tree; they are read as
    collapsed nodes unless ``include_menubar``. ``web_search`` is accepted for
    interface parity and ignored (no Collection fast path yet).
    """
    return await _run(_ui_walk_sync, title, depth, max_children, include_menubar, timeout)


def _ui_walk_sync(title, depth, max_children, include_menubar, timeout):
    import time

    from ..ui.model import UIRead
    from ..ui.walk import WalkStats, walk

    start = time.monotonic()
    try:
        A = _atspi()
    except (ImportError, ValueError) as e:
        log.warning("ui_walk: AT-SPI is unavailable (python3-gi and gir1.2-atspi-2.0): %s", e)
        return None
    skipped: list[str] = []
    unanswered: list[str] = []
    win, stacking = _target(A, title, skipped, unanswered)
    if win is None:
        silent = sorted(set(skipped) | set(unanswered))
        if silent:
            log.warning("no window matched %r; %d app(s) didn't answer: %s",
                        title, len(silent), ", ".join(silent))
            # Reported, not swallowed: the window may be there behind a modal.
            return UIRead(app="", pid=0, window_id=None, window_title=title or "", window=None,
                          elapsed=time.monotonic() - start, timeout=timeout,
                          unresponsive_apps=silent)
        return None

    origin = _window_origin(win.pid, win.title, stacking) if win.relative else None
    source = _ATSPISource(win.relative, origin)

    def skip_menubars(acc, node) -> bool:
        if node.native_role == "menu bar" and node.path:
            node.menubar_skipped = source.child_count(acc)
            return False
        return True

    stats = WalkStats()
    window, _ = walk(source, win.acc, depth=depth, max_children=max_children,
                     deadline=start + timeout, stats=stats,
                     descend=None if include_menubar else skip_menubars, free=_is_wrapper)
    return UIRead(
        app=win.app, pid=win.pid, window_id=win.key, window_title=win.title, window=window,
        nodes=stats.nodes, elapsed=time.monotonic() - start, timeout=timeout,
        budget_hit=stats.budget_hit, unresponsive_apps=sorted(set(skipped)),
    )


async def ui_act(locator, verb: str, params: dict, timeout: float = 5.0):
    """Resolve ``locator`` and run ``verb`` on it; returns a ``core.ui.act.ActOutcome``."""
    return await _run(_ui_act_sync, locator, verb, params, timeout)


def _ui_act_sync(loc, verb, params, timeout):
    import time

    from ..ui.act import ActOutcome, run_action
    from ..ui.resolve import resolve

    if not _pid_alive(loc.pid):
        return ActOutcome("stale_ref", detail="its app has quit", window_gone=True)
    A = _atspi()
    win = next((w for w in _windows(A) if w.key == loc.window_id and w.pid == loc.pid), None)
    if win is None:
        return ActOutcome("stale_ref", detail="its window has closed", window_gone=True)

    origin = _window_origin(win.pid, win.title, _x11_stacking()) if win.relative else None
    actor = _ATSPIActor(win, origin)
    res = resolve(actor, win.acc, loc, deadline=time.monotonic() + timeout)
    if res.status == "stale":
        return ActOutcome("stale_ref", detail="that element is no longer in the window",
                          candidates=res.candidates)
    if res.status == "ambiguous":
        return ActOutcome("ambiguous_ref", detail="the element moved and several elements now match it",
                          candidates=res.candidates)
    return run_action(actor, res.element, res.node, verb, params)
