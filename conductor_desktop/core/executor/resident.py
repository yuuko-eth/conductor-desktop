"""``ResidentExecutor``: observe and act on the machine the server runs on."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from .. import input_method
from ..backends.desktop import Capabilities, DesktopBackend
from ..errors import BackendError, best_effort
from ..platform_detect import Platform, detect
from .base import ActionResult, HostExecutor, _attempt, _call, _exc_str
from .input import LocalInputBackend
from .permissions import _test_screenshot_permissions
from .platforms import PLATFORMS

if TYPE_CHECKING:
    from ..models.host_node import HostNode

log = logging.getLogger(__name__)

# Where type_text sends short text as key presses (xdotool, ydotool), which a
# composing input method takes. Windows and macOS send each key with its
# character, which input methods let through.
_KEY_PRESS_TYPERS = {Platform.LINUX_X11, Platform.FREEBSD_X11, Platform.LINUX_GNOME,
                     Platform.LINUX_KDE, Platform.LINUX_WLROOTS}


def _derive_labels(read) -> None:
    from ..ui.label import derive_labels

    for _, root in read.roots():
        derive_labels(root)


def _ui_read_error(read, title: str | None) -> ActionResult | None:
    """The error for a ui_walk result that has nothing to read, else None."""
    if read is None:
        return ActionResult(success=False, error=(
            f"no app or window matches {title!r}" if title else "no focused app"))
    if read.window is None and read.menubar is None:
        if read.unresponsive_apps:
            # An app answers nothing while it runs a modal dialog (GTK 3), and a
            # process that quit without deregistering never answers at all.
            return ActionResult(success=False, error=(
                f"no window matches {title!r}; {len(read.unresponsive_apps)} app(s) didn't answer "
                f"the accessibility bus: {', '.join(read.unresponsive_apps)}. An app with a modal "
                f"dialog open stops answering until it's dismissed."))
        return ActionResult(success=False, error=(
            f"{read.app} has no open window (include_menubar=true reads its menu bar)"))
    return None


#: Terminal emulators, as whole tokens of a process name / app id
#: ("gnome-terminal-server" → gnome, terminal, server).
_TERMINAL_APPS = frozenset({
    "terminal", "konsole", "iterm", "iterm2", "alacritty", "kitty", "tilix",
    "terminator", "wezterm", "urxvt", "rxvt", "xterm", "foot", "footclient",
    "st", "ptyxis", "ghostty", "kgx", "console", "guake", "tilda", "yakuake",
    "sakura", "terminology", "qterminal", "lxterminal",
})
#: Title words that name a terminal on their own. A title is mostly the
#: shell's "user@host: ~", so this is only a fallback, and the short or
#: common-word names ("st", "foot", "console") are left out: matched as
#: substrings they caught "Stack Overflow" and "list".
_TERMINAL_TITLE_WORDS = frozenset({
    "terminal", "konsole", "iterm", "iterm2", "alacritty", "kitty", "tilix",
    "terminator", "wezterm", "urxvt", "xterm", "ptyxis", "ghostty",
})


def _is_terminal(process_name: str, title: str) -> bool:
    """Whether the focused window looks like a terminal emulator."""
    if set(re.split(r"[^a-z0-9]+", process_name.lower())) & _TERMINAL_APPS:
        return True
    return bool(set(re.split(r"[^a-z0-9]+", title.lower())) & _TERMINAL_TITLE_WORDS)


class ResidentExecutor(HostExecutor):
    """Execute actions on the local machine using host agent backends."""

    def __init__(self, host: HostNode) -> None:
        from ..ui.refs import RefTable

        super().__init__(host)
        self._desktop = DesktopBackend()
        self._input_backend: LocalInputBackend | None = None
        self._platform = Platform.UNKNOWN
        self._ui_refs = RefTable()

    async def connect(self) -> bool:
        found = detect(self.host.platform)
        self._platform = found.platform
        self.host.platform, self.host.platform_reason = found.platform.value, found.reason
        log.info("Platform %s (%s)", found.platform, found.reason)
        backends = PLATFORMS[self._platform]
        loaded = True
        try:
            self._desktop = backends.load_desktop()
        except Exception:  # an import error, a missing native library, ...
            log.warning("no platform backend for %s; window, clipboard and "
                        "accessibility tools are off", self._platform, exc_info=True)
            loaded = False
        try:
            self._input_backend = backends.load_input()
        except ImportError:
            log.warning("no input backend for %s; pointer and keyboard tools are off",
                        self._platform, exc_info=True)
            self._input_backend = None

        # ── Permission self-test ──
        log.info("Running permission self-test on %s...", self._platform)

        ss_ok, ss_detail = await _test_screenshot_permissions(self._platform)
        self.permissions.screenshot_works = ss_ok
        self.permissions.screenshot_detail = ss_detail

        if self._input_backend is None:
            input_ok, input_detail = False, f"no input backend for {self._platform}"
        else:
            input_ok, input_detail = await self._input_backend.check_access()
        self.permissions.input_works = input_ok
        self.permissions.input_detail = input_detail

        self.permissions.semantic_works = loaded and self._desktop.caps != Capabilities()
        self.permissions.semantic_detail = (
            f"Backend loaded: {self._desktop.name}" if self.permissions.semantic_works
            else "No platform backend"
        )

        if not self.permissions.ok:
            log.warning("Permission issues detected:\n%s", self.permissions.summary())
        else:
            log.info("Permissions OK: %s", self.permissions.summary())

        return True  # always "connected" locally, but may have limited capabilities

    async def disconnect(self) -> None:
        pass

    async def _resolve_screen(
        self, screen: int | str | None
    ) -> tuple[int | None, dict | None, list[dict]]:
        """Resolve a screen selector to ``(index, screen_dict, all_screens)``.

        ``screen=None`` → ``(None, None, screens)`` meaning "no offset,
        full virtual desktop". int/str selectors resolve against the
        backend's ``list_screens()``. On a single-monitor host, asking
        for ``0`` or ``"primary"`` is a no-op (resolves to that one
        screen) — keeps multi-monitor-aware callers working on laptops
        without code changes. Out-of-range ints and unknown names raise
        :class:`BackendError`.
        """
        screens: list[dict] = []
        unavailable = ""
        try:
            screens = await self.screens()
        except BackendError as e:
            # Screens are optional without screen=: the full desktop still works.
            log.debug("list_screens failed during screen resolve: %s", e)
            unavailable = f": {e}"

        if screen is None:
            return None, None, screens

        if not screens:
            raise BackendError(f"screen= needs the screen list, which isn't available{unavailable}")

        if isinstance(screen, int):
            if 0 <= screen < len(screens):
                return screen, screens[screen], screens
            raise BackendError(
                f"screen index {screen} out of range (0..{len(screens) - 1})"
            )

        if isinstance(screen, str):
            # "primary" → the screen flagged primary, falling back to index 0
            # on backends that don't mark one (some headless / wlroots setups).
            if screen.lower() == "primary":
                for i, s in enumerate(screens):
                    if s.get("primary"):
                        return i, s, screens
                return 0, screens[0], screens
            # Otherwise: exact match against the screen ``name`` (xrandr
            # output, NSScreen localizedName, Win32 device, wlroots output).
            for i, s in enumerate(screens):
                if s.get("name") == screen:
                    return i, s, screens
            names = ", ".join(repr(s.get("name", "")) for s in screens)
            raise BackendError(
                f"no screen named {screen!r} (available: {names})"
            )

        raise BackendError(f"screen must be int|str|None, got {type(screen).__name__}")

    async def capture_screenshot(self, screen: int | str | None = None) -> str:
        from ..backends import _common

        idx, _, screens = await self._resolve_screen(screen)
        return (await _common.take_screenshot(screen=idx, screens=screens)).png_b64

    async def get_screen_dimensions(
        self, screen: int | str | None = None
    ) -> tuple[int, int]:
        # Prefer the cheap geometric path: ``list_screens()`` already
        # carries per-monitor bounds. Falls back to a real capture only
        # when the screen list is unavailable.
        _, target, screens = await self._resolve_screen(screen)
        if target is not None:
            _, _, w, h = target["bounds"]
            return int(w), int(h)
        if screen is None and screens:
            # Full virtual desktop: union of all monitor bounds.
            xs = [s["bounds"][0] for s in screens]
            ys = [s["bounds"][1] for s in screens]
            rights = [s["bounds"][0] + s["bounds"][2] for s in screens]
            bottoms = [s["bounds"][1] + s["bounds"][3] for s in screens]
            return max(rights) - min(xs), max(bottoms) - min(ys)
        from ..backends import _common
        shot = await _common.take_screenshot()
        return shot.width, shot.height

    # ---- Pointer ----

    async def _input(self, work: Awaitable[Any], output: str = "") -> ActionResult:
        """Run one input-backend call. Input backends raise BackendError when
        the tool refuses (xdotool, an unknown key); anything else is a bug."""
        _, failed = await _attempt(work)
        return failed or ActionResult(success=True, output=output)

    async def _offset_for(
        self, screen: int | str | None
    ) -> tuple[int, int] | ActionResult:
        """Translate screen-local coords to absolute via the screen's
        origin. Returns ``(dx, dy)`` on success or an error ``ActionResult``
        that callers can return as-is.
        """
        try:
            _, target, _ = await self._resolve_screen(screen)
        except BackendError as e:
            return ActionResult(success=False, error=_exc_str(e))
        if target is None:
            return (0, 0)
        x0, y0, _, _ = target["bounds"]
        return (int(x0), int(y0))

    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1,
                    screen: int | str | None = None) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error=f"No input backend for {self._platform}")
        off = await self._offset_for(screen)
        if isinstance(off, ActionResult):
            return off
        ax, ay = x + off[0], y + off[1]
        return await self._input(self._input_backend.click(ax, ay, button, clicks),
                                 f"Clicked ({ax},{ay}) {button}")

    async def move_to(self, x: int, y: int,
                      screen: int | str | None = None) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        off = await self._offset_for(screen)
        if isinstance(off, ActionResult):
            return off
        ax, ay = x + off[0], y + off[1]
        return await self._input(self._input_backend.move_to(ax, ay), f"Moved to ({ax},{ay})")

    async def scroll(self, x: int, y: int, direction: str, amount: int = 3,
                     screen: int | str | None = None) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        off = await self._offset_for(screen)
        if isinstance(off, ActionResult):
            return off
        return await self._input(
            self._input_backend.scroll(x + off[0], y + off[1], direction, amount))

    async def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 20, hold_ms: int = 300,
                   screen: int | str | None = None) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        off = await self._offset_for(screen)
        if isinstance(off, ActionResult):
            return off
        return await self._input(self._input_backend.drag(
            x1 + off[0], y1 + off[1], x2 + off[0], y2 + off[1], steps=steps, hold_ms=hold_ms))

    # ---- Keyboard ----

    async def type_text(self, text: str, literal: bool = False) -> ActionResult:
        """Type `text` into the focused input.

        By default, long strings (>= 8 chars) use a clipboard-paste fast path
        for reliability. Set `literal=True` to force character-by-character
        injection — required when the target strips pasted content (e.g.
        Chrome's URL bar drops a leading "javascript:" scheme as an anti-
        phishing measure, but accepts it when typed).
        """
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        result = await self._input(self._type_text(text, literal))
        if result.success and (note := await self._input_method_note(text, literal)):
            result.output = f"ok; {note}"
        return result

    async def _composing_linux_ime(self) -> input_method.InputMethod | None:
        """The input method, when it's a composing one on Linux, where
        xdotool and ydotool send short text as key presses that it takes."""
        if self._platform not in _KEY_PRESS_TYPERS:
            return None
        im = await input_method.current()
        return im if im is not None and im.composing else None

    async def _input_method_note(self, text: str, literal: bool) -> str:
        if not literal or not (im := await self._composing_linux_ime()):
            return ""
        return (f"{im.describe()} is on and composes key presses, so letters and digits "
                f"may have become its own text; without keystrokes=true the text is pasted")

    async def _type_text(self, text: str, literal: bool) -> None:
        # Long text (>= 8 chars) is pasted: more reliable than keystroke
        # injection. The text stays on the clipboard. The app reads the
        # clipboard whenever it gets to the paste key, which can be any time
        # later, so restoring the old contents could paste those instead; and
        # only text can be saved, so an image or files were lost anyway.
        # Text the input backend can't type (ydotool: non-ASCII) is pasted too.
        paste = len(text) >= 8 or not await self._input_backend.can_type(text)
        if not paste and not literal:
            # A composing input method would take the key presses: Chewing
            # turned "a b" into a pending ㄇ composition.
            paste = await self._composing_linux_ime() is not None
        if not literal and paste and self._desktop.caps.clipboard:
            await self._desktop.set_clipboard(text)
            await asyncio.sleep(0.05)
            await self._input_backend.hotkey(await self._get_paste_keys())
            await asyncio.sleep(0.1)
        else:
            await self._input_backend.type_text(text)

    async def _get_paste_keys(self) -> list[str]:
        """Determine the correct paste hotkey for the current platform and window."""
        if self._platform == Platform.MACOS:
            # macOS uses Cmd+V in every app, including Terminal.app.
            # Modifier names must match what the Quartz backend recognizes —
            # X11 keysyms like "Super_L" crash with "unknown key in hotkey".
            return ["cmd", "v"]
        # Terminals paste with Ctrl+Shift+V (Ctrl+V is a literal ^V there).
        if self._desktop.caps.windows:
            focused = None
            with best_effort("finding the focused window for the paste keys", log):
                focused = next((w for w in await self._desktop.list_windows()
                                if w.get("focused")), None)
            if focused and _is_terminal(focused.get("process_name") or "",
                                        focused.get("title") or ""):
                return ["Control_L", "Shift_L", "v"]
        return ["Control_L", "v"]

    async def key_press(self, key: str) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        return await self._noted_key([key], self._input(self._input_backend.key_press(key)))

    async def hotkey(self, keys: list[str]) -> ActionResult:
        if self._input_backend is None:
            return ActionResult(success=False, error="No input backend")
        return await self._noted_key(keys, self._input(self._input_backend.hotkey(keys)))

    async def _noted_key(self, keys: list[str], press) -> ActionResult:
        """``press``'s result, noting when a composing input method gets the key.

        The key still goes to it, as a real key press should: driving an
        input method on purpose is a key press's job, and text is type_text's.
        """
        result = await press
        if result.success and input_method.is_bare_key(keys):
            im = await input_method.current()
            if im is not None and im.composing:
                result.output = (f"ok; {im.describe()} may have taken this key as input "
                                 f"(Zhuyin: a is ㄇ); to enter text, use type_text")
        return result

    # ---- Semantic (direct backend calls) ----

    async def exec_shell(self, command: str, timeout: float = 30) -> ActionResult:
        from ..backends import _common
        try:
            result = await _common.shell_exec(command, timeout)
        except BackendError as e:
            return ActionResult(success=False, error=_exc_str(e))
        if result.exit_code == 0:
            return ActionResult(success=True, output=result.stdout)
        return ActionResult(success=False, output=result.stdout,
                            error=result.stderr or f"exit code {result.exit_code}")

    async def get_clipboard(self) -> ActionResult:
        if not self._desktop.caps.clipboard:
            return await super().get_clipboard()
        return await _call(self._desktop.get_clipboard())

    async def set_clipboard(self, text: str) -> ActionResult:
        if not self._desktop.caps.clipboard:
            return await super().set_clipboard(text)
        return await _call(self._desktop.set_clipboard(text), lambda _: "")

    async def focus_window(self, title: str | None = None, pid: int | None = None) -> ActionResult:
        if not self._desktop.caps.windows:
            return await super().focus_window(title, pid)
        found, failed = await _attempt(self._desktop.focus_window(title=title, pid=pid))
        if failed:
            return failed
        if found:
            return ActionResult(success=True)
        # Backend silently returned False — build a diagnostic so the driver
        # can self-correct instead of seeing a bare "unknown" error.
        if title is None and pid is None:
            return ActionResult(success=False, error="no title or pid given")
        if pid is not None and title is None:
            return ActionResult(success=False, error=f"no process with pid={pid}")
        sample = ""
        if self._desktop.caps.windows:
            windows: list[dict] = []
            with best_effort("listing windows for the focus diagnostic", log):
                windows = await self._desktop.list_windows()
            # Deduplicate while preserving order; cap preview.
            uniq = list(dict.fromkeys(w["title"] for w in windows if w.get("title")))
            preview = ", ".join(f"{t!r}" for t in uniq[:8])
            more = f" (+{len(uniq) - 8} more)" if len(uniq) > 8 else ""
            sample = f"; available titles: [{preview}{more}]" if uniq else ""
        return ActionResult(
            success=False,
            error=f"no window matching {title!r}{sample}",
        )

    async def windows(self) -> list[dict]:
        if not self._desktop.caps.windows:
            return await super().windows()
        return await self._desktop.list_windows()

    async def screens(self) -> list[dict]:
        if not self._desktop.caps.screens:
            return await super().screens()
        return await self._desktop.list_screens()


    async def ui_tree(self, title: str | None = None, depth: int = 8, max_children: int = 50,
                      include_menubar: bool = False, format: str = "outline",
                      timeout: float = 10.0) -> ActionResult:
        """Walk the target window into a ``UIRead``, give its nodes refs, render it."""
        if not self._desktop.caps.ui:
            return await super().ui_tree(title, depth, max_children, include_menubar, format, timeout)
        from ..ui.format import format_json, format_outline

        read = await self._desktop.ui_walk(
            title, depth=depth, max_children=max_children,
            include_menubar=include_menubar, timeout=timeout,
        )
        if (error := _ui_read_error(read, title)) is not None:
            return error
        _derive_labels(read)
        self._ui_refs.assign(read.roots(), pid=read.pid, window_id=read.window_id,
                             window_title=read.window_title)
        return ActionResult(success=True,
                            output=format_json(read) if format == "json" else format_outline(read))

    # ui_find walks deeper than ui_tree's default; the time budget bounds it.
    _UI_FIND_DEPTH = 16
    _UI_FIND_MAX_CHILDREN = 100

    async def ui_find(self, query: str | None = None, role: str | None = None,
                      name: str | None = None, value: str | None = None, id: str | None = None,
                      actionable: bool | None = None, states: list[str] | None = None,
                      title: str | None = None, include_menubar: bool = False, limit: int = 10,
                      match: str = "auto", timeout: float = 10.0) -> ActionResult:
        """Walk the target, filter and rank its elements, give the results refs."""
        if not self._desktop.caps.ui:
            return await super().ui_find(query, role, name, value, id, actionable, states, title,
                                         include_menubar, limit, match, timeout)
        from ..ui.find import Criteria, find, format_find

        if match == "semantic":
            return ActionResult(success=False, error=(
                "semantic matching is not installed yet; use match=auto, fuzzy or exact"))
        criteria = Criteria(query=query, role=role, name=name, value=value, id=id,
                            actionable=actionable, states=list(states or []))
        if criteria.empty:
            return ActionResult(success=False, error=(
                "give a query, name, value, role, id, actionable or states"))
        text = name or query
        web_search = {"role": role, "text": text} if (text or role) else None
        read = await self._desktop.ui_walk(
            title, depth=self._UI_FIND_DEPTH, max_children=self._UI_FIND_MAX_CHILDREN,
            include_menubar=include_menubar, timeout=timeout, web_search=web_search,
        )
        if (error := _ui_read_error(read, title)) is not None:
            return error
        _derive_labels(read)
        result = find(read.roots(), criteria, limit=limit, match=match)
        self._ui_refs.assign(read.roots(), pid=read.pid, window_id=read.window_id,
                             window_title=read.window_title,
                             also=[cand.node for cand in result.candidates])
        return ActionResult(success=True, output=format_find(read, result))

    async def ui_act(self, ref: str, verb: str, params: dict | None = None,
                     timeout: float = 5.0) -> ActionResult:
        """Resolve ``ref``, run ``verb`` on it and keep the ref table current."""
        if not self._desktop.caps.ui:
            return await super().ui_act(ref, verb, params, timeout)
        from ..ui.act import format_outcome

        loc = self._ui_refs.get(ref)
        if loc is None:
            return ActionResult(success=False, error=(
                f"unknown_ref: {ref} is not a current ref. Refs come from ui_tree and ui_find "
                "and last for the 5 most recent reads of a window; read again."))
        outcome = await self._desktop.ui_act(loc, verb, params or {}, timeout)
        if outcome.window_gone:
            self._ui_refs.forget_window(loc.pid, loc.window_id, loc.window_title)
        moved = outcome.node is not None and outcome.node.path != loc.path
        if moved:
            self._ui_refs.relocate(ref, outcome.node.path)
        ok, text = format_outcome(ref, verb, outcome, moved=moved)
        return ActionResult(success=True, output=text) if ok else ActionResult(success=False, error=text)

    async def extract_table(self, title: str | None = None, max_rows: int = 100) -> ActionResult:
        if not self._desktop.caps.table:
            # Reported as an unimplemented provider, not an error.
            return await super().extract_table(title, max_rows)
        return await _call(self._desktop.extract_table(title, max_rows=max_rows),
                           lambda table: json.dumps(table.to_dict()))


def create_executor(host: HostNode) -> HostExecutor:
    match host.host_type:
        case "resident":
            return ResidentExecutor(host)
        case "adb" | "wda":
            # Phones are driven through the mobile_* tools next to the
            # desktop, not as the server's primary executor.
            env = "ANDROID" if host.host_type == "adb" else "IOS"
            raise ValueError(
                f"backend={host.host_type} is not a desktop backend; keep "
                f"backend=resident and set CONDUCTOR_DESKTOP_MOBILE_{env}=1 to use "
                f"the mobile_* tools"
            )
        case "vnc" | "rdp" | "vbox":
            raise NotImplementedError(f"{host.host_type} executor not yet implemented")
        case _:
            raise ValueError(f"Unknown host type: {host.host_type}")
