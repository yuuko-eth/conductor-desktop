"""System tray icon for the holder ``conductor-desktop`` process.

Only the desktop-lock holder shows a tray icon — spectators run silent.

Threading: pystray's macOS backend (and arguably others) wants the main
thread for its event loop. The MCP asyncio loop therefore runs on a
worker thread; the tray thread signals shutdown via
``asyncio.Event`` + ``loop.call_soon_threadsafe``.

Falls back to a headless asyncio loop if pystray/Pillow aren't installed
or no system tray is dockable (Wayfire / Sway / SSH session).

State indicator
---------------
The icon reflects four states (idle/observing/acting/errored). The
``TrayStateMachine`` lives in ``tray_state.py``; ``run_holder``
constructs it, hands it to ``_serve_async`` (which installs it for the
tool wrapper in ``tooling.py``), and registers an observer that swaps the
icon image when the state changes.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import webbrowser
from importlib.resources import files
from typing import Any
from urllib.parse import quote

from . import state as _state
from .config import MCPConfig
from .core.errors import best_effort
from .desktop_lock import DesktopLock
from .tray_state import (
    ALL_STATES,
    STATE_IDLE,
    TrayStateMachine,
)

log = logging.getLogger("conductor.mcp.tray")


# Pre-rasterized native sizes shipped in assets/. Cairo (rsvg-convert)
# snaps stroke widths to the pixel grid at the target raster size, so
# rasterizing the SVG separately at each integer size beats any
# downstream PIL/Cocoa/GTK resample. See scripts/regen_tray_icons.sh.
#
#   22  — macOS menubar @1x, X11 systray, GNOME panel default
#   44  — macOS menubar @2x (retina)
#   64  — AppIndicator / KDE panels
#   88  — HiDPI panels (2x)
#  256  — debug/preview alias (state_<state>.png points here)
_NATIVE_PX = (22, 44, 64, 88, 256)
_DEFAULT_LOAD_PX = 22  # what we hand to non-darwin pystray backends


# Linux and FreeBSD: the tray runs in a process of its own (tray_child.py
# says why), so this process never loads GTK.
_TRAY_IN_CHILD = sys.platform.startswith(("linux", "freebsd"))


class _TrayLink:
    """JSON lines to the tray process; a gone process is just no tray."""

    def __init__(self, child) -> None:
        self._child = child
        self._lock = threading.Lock()

    def send(self, msg: dict) -> None:
        with self._lock:
            try:
                self._child.stdin.write(json.dumps(msg) + "\n")
                self._child.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock, best_effort("closing the tray process's stdin", log):
            self._child.stdin.close()


def _run_holder_with_tray_child(config: MCPConfig, lock: DesktopLock) -> None:
    """The server on this thread, the tray in ``conductor_desktop.tray_child``."""
    import subprocess

    from .server import _serve_async

    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "conductor_desktop.tray_child"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "NO_AT_BRIDGE": "1"})
    except OSError as e:
        log.warning("tray unavailable (%s) — running holder without tray icon", e)
        asyncio.run(_serve_async(config, lock))
        return
    link = _TrayLink(child)
    state_machine = TrayStateMachine()

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        link.send({"title": "Conductor Desktop (holder)", "status_text": _status_text(config),
                   "dashboard": _dashboard_url(config) if config.dashboard else None})
        state_machine.set_observer(lambda state: link.send({"state": state}))

        def toggle_pause() -> None:
            st = _state.get()
            if st is None:
                return
            paused = not st.input_paused.is_set()
            if paused:
                st.input_paused.set()
            else:
                st.input_paused.clear()
            state_machine.set_paused(paused)

        async def perm_test() -> None:
            from .check_permissions import run_self_test_async
            call_id = state_machine.start("permission_self_test", klass="observing")
            try:
                ok, detail = await run_self_test_async()
            except Exception as e:
                log.exception("perm test crashed")
                ok, detail = False, f"{type(e).__name__}: {e}"
            log.info("tray perm test: ok=%s detail=%s", ok, detail)
            # A failure latches the icon red so the operator notices.
            state_machine.end(call_id, success=ok, error=None if ok else detail)

        tasks: set[asyncio.Task] = set()

        def act(action: str) -> None:
            if action == "quit":
                log.info("tray: quit requested — releasing desktop lock and shutting down")
                shutdown.set()
            elif action == "pause":
                toggle_pause()
            elif action == "perm_test":
                task = loop.create_task(perm_test())
                tasks.add(task)
                task.add_done_callback(tasks.discard)

        def read() -> None:
            for line in child.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "unavailable" in msg:
                    log.info("No system tray available (%s) — running without tray icon",
                             msg["unavailable"])
                if "action" in msg:
                    loop.call_soon_threadsafe(act, msg["action"])

        threading.Thread(target=read, name="tray-link-reader", daemon=True).start()

        async def tick() -> None:
            # The latch's expiry, and the submenus when something changed.
            shown = None
            while True:
                await asyncio.sleep(1.0)
                with best_effort("checking the tray latch", log):
                    state_machine.reclassify_check()
                st = _state.get()
                paused = bool(st and st.input_paused.is_set())
                current = (state_machine.revision, state_machine.current_state, paused)
                if current != shown:
                    link.send({"menu": {"recent": _recent_labels(state_machine),
                                        "status": _status_lines(config, state_machine),
                                        "paused": paused}})
                    shown = current

        ticker = loop.create_task(tick())
        try:
            await _serve_async(config, lock, shutdown_event=shutdown, tray_state=state_machine)
        finally:
            ticker.cancel()

    try:
        asyncio.run(serve())
    finally:
        # stdin closing tells the tray to go.
        link.close()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()


def run_holder(config: MCPConfig, lock: DesktopLock) -> None:
    """Run the MCP server with a tray icon. Blocks until quit or transport EOF."""
    import os

    # GTK's accessibility bridge would load libatspi into this process for the
    # tray icon and drive it from the tray's thread, while the ui_* tools use
    # libatspi as a client on their own thread; libatspi isn't thread-safe.
    # The icon needs no accessibility of its own.
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    if _TRAY_IN_CHILD:
        return _run_holder_with_tray_child(config, lock)
    try:
        import pystray
        from PIL import Image  # noqa: F401
    except Exception as e:
        # ImportError: pystray / Pillow not installed.
        # ValueError: pystray imports gi.require_version("AppIndicator3")
        # at module load time on Linux/FreeBSD; absent on systems without
        # libappindicator (FreeBSD, headless Linux).
        # Xlib.error.DisplayConnectionError (and other Xlib errors): on
        # X11 pystray opens Display() at import time; a stale or missing
        # DISPLAY env from the MCP launcher used to take the whole server
        # down here. Tray is cosmetic — never let it sink the MCP loop.
        log.warning("tray unavailable (%s: %s) — running holder without tray icon",
                    type(e).__name__, e, exc_info=True)
        from .server import _serve_async
        asyncio.run(_serve_async(config, lock))
        return

    # Detect a degraded backend selection on Linux: when ``gi`` is broken
    # in the venv (commonly: ``--system-site-packages`` with a Python ABI
    # that doesn't match apt's python3-gi .so), pystray's _gtk
    # (AppIndicator) candidate silently fails and we fall through to
    # ``_xorg``. That backend draws a raw XEmbed tray window that GNOME
    # and KDE Plasma barely support — symptom is a dead menu (clicks do
    # nothing) and smeared icon updates on state change. We're better
    # off skipping the tray entirely than shipping that UX.
    if sys.platform.startswith("linux"):
        backend_mod = getattr(pystray.Icon, "__module__", "")
        if backend_mod.endswith("._xorg"):
            log.warning(
                "tray skipped: pystray fell through to the X11 backend "
                "(%s). Menu is non-functional and icon updates smear on "
                "modern desktops. Repair gi in the venv (e.g. rebuild on "
                "the Python ABI apt's python3-gi targets, or pip-install "
                "PyGObject) to get the AppIndicator backend.",
                backend_mod,
            )
            from .server import _serve_async
            asyncio.run(_serve_async(config, lock))
            return

    state_machine = TrayStateMachine()

    # Wire asyncio loop on a worker thread.
    loop_holder: dict[str, Any] = {"loop": None, "shutdown": None, "exc": None}
    ready = threading.Event()

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        shutdown = asyncio.Event()
        loop_holder["shutdown"] = shutdown
        ready.set()
        from .server import _serve_async
        try:
            loop.run_until_complete(_serve_async(
                config, lock,
                shutdown_event=shutdown,
                tray_state=state_machine,
            ))
        except Exception as e:
            loop_holder["exc"] = e
            log.exception("MCP server thread crashed: %s", e)
        finally:
            # Before reading the icon: see _setup below for why.
            loop_holder["done"] = True
            with best_effort("closing the MCP event loop", log):
                loop.close()
            # Worker exiting means the MCP transport completed — typically
            # stdio EOF when the client (Claude Code) closes its end. Tell
            # the tray on the main thread to wind down too, otherwise
            # pystray's NSApplication runloop keeps the process alive
            # forever as an orphan parented to launchd. icon.stop() is
            # documented thread-safe.
            ico = loop_holder.get("icon")
            if ico is not None:
                with best_effort("stopping the tray icon", log):
                    ico.stop()

    worker = threading.Thread(target=_worker, name="mcp-asyncio", daemon=True)
    worker.start()
    # Wait until the worker has set up its loop + shutdown event before we
    # build a tray that might call into them.
    ready.wait()

    icon, latch_ticker_stop = _build_icon(
        config, lock, loop_holder, worker, state_machine,
    )
    # Share the icon back to the worker so it can stop the tray when the
    # MCP transport completes (stdio EOF / client disconnect).
    loop_holder["icon"] = icon
    # Suppress pystray's own ERROR log if the platform has no tray.
    pystray_logger = logging.getLogger("pystray._base")
    prev_level = pystray_logger.level
    pystray_logger.setLevel(logging.CRITICAL)

    def _setup(icon) -> None:
        # The worker can be gone already: when host permissions are missing
        # the server refuses to start within a fraction of a second, before
        # the icon existed, so its icon.stop() found nothing, and icon.run()
        # held the process open forever with the client waiting on it.
        # setup runs once the tray loop is live,
        # where stop() works. The worker marks itself done before it looks
        # for the icon, so whichever thread comes second stops the tray.
        icon.visible = True  # pystray's default setup, which this replaces
        if loop_holder.get("done"):
            icon.stop()

    try:
        icon.run(setup=_setup)
    except KeyboardInterrupt:
        _signal_shutdown(loop_holder)
    except Exception as e:
        log.info("No system tray available (%s) — running without tray icon", e,
                 exc_info=True)
        with best_effort("stopping the tray icon", log):
            icon.stop()
        # Block on the worker so the process stays alive serving the MCP transport.
        worker.join()
    finally:
        latch_ticker_stop.set()
        pystray_logger.setLevel(prev_level)
        # If the tray exited but the worker is still running (e.g. user picked
        # Quit from the menu), wait for it to drain so cleanup completes.
        if worker.is_alive():
            worker.join(timeout=5.0)
        # Surface a worker exception if one happened.
        if loop_holder["exc"] is not None:
            raise loop_holder["exc"]


def _load_state_icons(load_px: int = _DEFAULT_LOAD_PX) -> dict[str, Any]:
    """Load PIL images at the requested native pixel size.

    Returns ``{state: PIL.Image}`` ready to assign to ``icon.icon``. The
    file is shipped at exactly ``load_px``, so pystray's internal
    LANCZOS resize early-outs (``self._icon.size == size``).
    """
    from PIL import Image
    out: dict[str, Any] = {}
    for state in ALL_STATES:
        path = files(__package__).joinpath("assets", f"state_{state}_{load_px}.png")
        with path.open("rb") as fh:
            img = Image.open(fh).convert("RGBA")
            img.load()
        out[state] = img
    return out


def _load_state_png_bytes() -> dict[str, dict[int, bytes]]:
    """Load raw PNG bytes for every (state, native-size) pair.

    Returns ``{state: {px: bytes}}``. Used by the macOS multi-rep
    NSImage builder so we hand Cocoa pre-rasterized 22 + 44 reps and
    skip pystray's LANCZOS path entirely.
    """
    out: dict[str, dict[int, bytes]] = {}
    for state in ALL_STATES:
        out[state] = {}
        for px in _NATIVE_PX:
            path = files(__package__).joinpath("assets", f"state_{state}_{px}.png")
            with path.open("rb") as fh:
                out[state][px] = fh.read()
    return out


def _patch_macos_assert_image(icon, state_machine: TrayStateMachine,
                              png_bytes: dict[str, dict[int, bytes]]) -> None:
    """Replace pystray-darwin's _assert_image with a multi-rep NSImage builder.

    Why: ``pystray._darwin._assert_image`` LANCZOS-resizes the source
    PIL image to the menubar thickness and hands Cocoa a single fixed-
    pixel bitmap with no @2x representation — soft on retina menubars.

    Our replacement builds an NSImage with both 22 px and 44 px
    NSBitmapImageRep entries (logical size = thickness pt) so AppKit
    picks the right rep per backing scale. Reads state from
    ``state_machine.current_state`` rather than ``self._icon`` so we
    don't depend on pystray's PIL pipeline at all.

    Not a template image: macOS would auto-tint to monochrome and we
    lose the color = state semantics. Accepted tradeoff.

    Threading: pystray invokes ``_assert_image`` from whatever thread
    sets ``icon.icon`` — for us, the latch ticker and asyncio worker.
    AppKit (NSImage / NSStatusBarButton) is main-thread-only; calling
    these off-main has been observed to leave the status item's button
    in a state where left-clicks no longer pop the menu. We therefore
    (a) skip work when the (state, thickness) pair hasn't changed and
    (b) marshal the actual NSImage build + setImage_ onto the main
    thread via NSOperationQueue.
    """
    import AppKit
    import Foundation

    cache: dict[str, Any] = {"key": None}

    def _build_nsimage(thickness: float, state: str):
        bytes_by_size = png_bytes.get(state) or png_bytes[STATE_IDLE]
        nsimage = AppKit.NSImage.alloc().initWithSize_((thickness, thickness))
        for px in (22, 44):
            data_bytes = bytes_by_size.get(px)
            if data_bytes is None:
                continue
            nsdata = Foundation.NSData.dataWithBytes_length_(
                data_bytes, len(data_bytes),
            )
            rep = AppKit.NSBitmapImageRep.alloc().initWithData_(nsdata)
            if rep is None:
                continue
            rep.setSize_((thickness, thickness))
            nsimage.addRepresentation_(rep)
        return nsimage

    def _assert_image(self):
        try:
            thickness = float(self._status_bar.thickness())
        except Exception:
            log.debug("status bar thickness unavailable", exc_info=True)
            thickness = 22.0
        state = state_machine.current_state
        key = (state, thickness)
        if cache["key"] == key:
            return
        cache["key"] = key

        def _apply():
            nsimage = _build_nsimage(thickness, state)
            self._icon_image = nsimage
            button = self._status_item.button()
            if button is not None:
                button.setImage_(nsimage)

        # Marshal AppKit work to the main thread. waitUntilDone=NO so the
        # caller (often an asyncio worker or the latch ticker) doesn't
        # block on the main loop. If we're already on the main thread,
        # NSOperationQueue still runs the block via the queue — fine.
        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    icon._assert_image = types.MethodType(_assert_image, icon)


def _make_menu_refresher(icon) -> Any:
    """Return a thread-safe `icon.update_menu()` wrapper.

    On macOS, pystray's `_update_menu` rebuilds the NSMenu and calls
    `setMenu_` on the NSStatusItem — both AppKit operations that must
    run on the main thread. Calling them from the latch-ticker thread
    has the same failure mode that bit `_assert_image` (status-item
    button stops popping the menu). Marshal via NSOperationQueue with
    waitUntilDone=NO so the caller doesn't block on the main loop.

    Off macOS, the X11 / AppIndicator / GTK backends are thread-tolerant
    enough that a direct call is fine, and adding a marshal layer would
    just be overhead.
    """
    if sys.platform != "darwin":
        return icon.update_menu
    try:
        import Foundation  # noqa: F401
    except ImportError:
        return icon.update_menu
    import Foundation as _Foundation

    def _refresh() -> None:
        _Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
            icon.update_menu
        )

    return _refresh


def _recent_labels(state_machine: TrayStateMachine) -> list[str]:
    """The "Recent tool calls" submenu's lines, newest first."""
    labels = []
    # Clock times, not "Ns ago": the menu is rebuilt only when a call
    # starts or ends, so a relative age would go stale in between.
    for call in reversed(state_machine.recent(5)):
        at = time.strftime("%H:%M:%S", time.localtime(call.started_at))
        if call.in_flight:
            labels.append(f"{call.tool} · in flight since {at}")
        else:
            tag = "ok" if call.success else "ERR"
            dur = f"{call.duration_ms}ms" if call.duration_ms is not None else "?"
            labels.append(f"{call.tool} · {tag} {dur} at {at}")
    return labels or ["(no calls yet)"]


def _status_lines(config: MCPConfig, state_machine: TrayStateMachine) -> list[str]:
    """The "Status" submenu's lines."""
    listening = (f"{config.host or '127.0.0.1'}:{config.port}"
                 if config.transport != "stdio" else "stdio")
    start_ts = _state.holder_start_ts
    started = (time.strftime("%Y-%m-%d %H:%M", time.localtime(start_ts))
               if start_ts else "?")
    return [
        f"Backend: {config.backend}",
        f"Grounder: {getattr(config, 'grounder_url', '?') or 'not configured'}",
        f"Transport: {config.transport} ({listening})",
        f"PID: {os.getpid()}",
        f"Started: {started}",
        f"State: {state_machine.current_state}",
    ]


def _status_text(config: MCPConfig) -> str:
    """The menu's first, greyed-out line."""
    transport_label = config.transport
    if config.transport != "stdio":
        transport_label = f"{config.transport} {config.host}:{config.port}"
    return f"conductor-desktop · pid={os.getpid()} · {transport_label}"


def _dashboard_url(config: MCPConfig) -> str:
    # A wildcard bind is listening everywhere, but it isn't an address
    # a browser can open; loopback reaches the same server.
    host = config.host if config.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    # With auth on, the token logs this browser in once (a cookie) and
    # the dashboard redirects to its bare URL.
    query = f"?token={quote(config.auth_token)}" if config.auth_token else ""
    return f"http://{host}:{config.port}/dashboard{query}"


def _build_icon(config: MCPConfig, lock: DesktopLock,
                loop_holder: dict[str, Any], worker: threading.Thread,
                state_machine: TrayStateMachine):
    import pystray

    icons = _load_state_icons(_DEFAULT_LOAD_PX)

    def on_quit(icon, _item):
        log.info("tray: quit requested — releasing desktop lock and shutting down")
        _signal_shutdown(loop_holder)
        icon.stop()

    def on_open_dashboard(icon, _item):
        webbrowser.open(_dashboard_url(config))

    def on_pause_toggle(icon, _item):
        # Toggle the input-pause flag on the running asyncio loop.
        # The flag is read by the tool wrapper for
        # ACTING-classified tools. Also notify the state machine so
        # the icon swaps to (or out of) the paused glyph immediately.
        st = _state.get()
        if st is None:
            return
        if st.input_paused.is_set():
            st.input_paused.clear()
            state_machine.set_paused(False)
        else:
            st.input_paused.set()
            state_machine.set_paused(True)

    def is_paused(_item) -> bool:
        st = _state.get()
        return bool(st and st.input_paused.is_set())

    def on_perm_test(icon, _item):
        # Runs on the server's loop, not here: this is the tray's UI thread
        # (the main thread on macOS), and the probe takes seconds.
        loop = loop_holder.get("loop")
        if loop is None:
            log.warning("tray perm test: the server isn't running")
            return
        from .check_permissions import run_self_test_async
        call_id = state_machine.start("permission_self_test", klass="observing")
        future = asyncio.run_coroutine_threadsafe(run_self_test_async(), loop)

        def _done(f) -> None:
            try:
                ok, detail = f.result()
            except Exception as e:
                log.exception("perm test crashed")
                ok, detail = False, f"{type(e).__name__}: {e}"
            log.info("tray perm test: ok=%s detail=%s", ok, detail)
            # A failure latches the icon red so the operator notices.
            state_machine.end(call_id, success=ok, error=None if ok else detail)

        future.add_done_callback(_done)

    # ── Recent calls submenu (callable text, fresh on open) ──────────
    def _recent_items():
        return [pystray.MenuItem(label, lambda *_a: None, enabled=False)
                for label in _recent_labels(state_machine)]

    def recent_menu():
        # pystray invokes this factory with no args each time the menu opens.
        return _recent_items()

    # ── Status submenu (callable text) ───────────────────────────────
    def _status_items():
        return [pystray.MenuItem(s, lambda *_a: None, enabled=False)
                for s in _status_lines(config, state_machine)]

    def status_menu():
        return _status_items()

    # ── Top-level menu ───────────────────────────────────────────────
    status_text = _status_text(config)

    menu = pystray.Menu(
        pystray.MenuItem(status_text, lambda *_a: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Pause input", on_pause_toggle, checked=is_paused,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open dashboard", on_open_dashboard,
                         visible=config.dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Recent tool calls",
            pystray.Menu(recent_menu),  # callable submenu
        ),
        pystray.MenuItem(
            "Status",
            pystray.Menu(status_menu),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Re-run permission test", on_perm_test),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        "conductor-desktop",
        icons[STATE_IDLE],
        "Conductor Desktop (holder)",
        menu,
    )

    # macOS: replace pystray's LANCZOS-resize NSImage path with a multi-rep
    # builder that uses our pre-rasterized 22 + 44 px PNGs. Must happen
    # before icon.run() so the first paint uses the patched method.
    # Click regression resolved by main-thread NSOperationQueue marshal
    # inside _patch_macos_assert_image.
    if sys.platform == "darwin":
        try:
            png_bytes = _load_state_png_bytes()
            _patch_macos_assert_image(icon, state_machine, png_bytes)
        except Exception:
            log.exception("macOS NSImage patch failed; falling back to pystray default")

    # State-change observer: swap icon image when the state machine fires.
    # `icon.icon = ...` is safe to assign cross-thread on all three pystray
    # backends in practice (the backend marshals the change on next tick).
    # On macOS our patched _assert_image reads state directly from the
    # state machine — assigning still triggers _update_icon which calls
    # _assert_image, which is the refresh we want.
    def _show_state(new_state: str) -> None:
        img = icons.get(new_state)
        if img is not None:
            # Fails while the backend isn't in run() yet; the first state is
            # picked up naturally on launch.
            with best_effort("swapping the tray icon", log):
                icon.icon = img

    if type(icon).__module__ in ("pystray._appindicator", "pystray._gtk"):
        _use_stable_icon_files(icon, icons)
    # A tool call moves the state several times; the ticker below flushes
    # what the gate held back.
    gate = _IconGate(_show_state, shown=STATE_IDLE)
    state_machine.set_observer(gate.want)

    # Latch ticker: calls reclassify_check() every second so the error
    # latch's expiration triggers a transition out of red even when no
    # tool events fire during the dwell.
    #
    # Also refreshes the menu, when something changed, so the
    # dynamic-callable submenus ("Recent tool calls", "Status") pick up new
    # state. Only then: a rebuild closes a menu the operator has open. pystray's docstring on
    # update_menu() is explicit: "not all supported platforms allow the
    # menu to be generated when shown" — on macOS (NSMenu) and the X11 /
    # AppIndicator / GTK backends, the descriptor tree is walked only at
    # icon-construction / setMenu_ time, so dynamic callables only fire
    # then. Without this tick the menu freezes after boot.
    #
    # On macOS update_menu() ends up calling setMenu_ on the status item
    # (AppKit), which must run on the main thread — same constraint that
    # bit us with the icon-image patch. Marshal via NSOperationQueue.
    refresh_menu = _make_menu_refresher(icon)
    stop_event = threading.Event()

    def _latch_ticker() -> None:
        shown: tuple[int, str] | None = None
        while not stop_event.wait(1.0):
            try:
                state_machine.reclassify_check()
            except Exception:
                log.exception("latch ticker crashed; continuing")
            gate.flush()
            current = (state_machine.revision, state_machine.current_state)
            if current == shown:
                continue
            try:
                refresh_menu()
                shown = current
            except Exception:
                log.exception("tray menu refresh crashed; continuing")

    t = threading.Thread(target=_latch_ticker, name="tray-latch-ticker",
                         daemon=True)
    t.start()

    return icon, stop_event


class _IconGate:
    """At most one icon change a second, the latest state winning.

    Each change costs the desktop work on its own main thread: gnome-shell's
    AppIndicator extension reloads the image there, and while it did that
    several times per tool call it stopped answering AT-SPI for seconds.
    ``want`` is the state machine's observer; ``flush`` runs from the
    once-a-second ticker and shows what ``want`` held back.
    """

    def __init__(self, show, shown: str | None = None, min_interval: float = 1.0,
                 clock=time.monotonic) -> None:
        self._show = show
        self._shown = shown
        self._min = min_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._at = float("-inf")

    def want(self, state: str) -> None:
        with self._lock:
            self._pending = state
            state = self._take()
        if state is not None:
            self._show(state)

    def flush(self) -> None:
        with self._lock:
            state = self._take()
        if state is not None:
            self._show(state)

    def _take(self) -> str | None:
        if self._clock() - self._at < self._min:
            return None
        state, self._pending = self._pending, None
        if state is None or state == self._shown:
            return None
        self._shown, self._at = state, self._clock()
        return state


def _use_stable_icon_files(icon, icons: dict[str, Any]) -> None:
    """Give pystray's GTK backends one PNG per state, written once.

    They write every new image to a fresh ``tempfile.mktemp()`` path, delete
    the previous one, and hand the path to the desktop, which reads it later
    from its own main loop; by then a quick run of changes had replaced the
    file ("Failed to recognize image format" from gnome-shell's AppIndicator
    extension, every change). Files that stay put can't be read half-gone.
    """
    folder = tempfile.mkdtemp(prefix="conductor-desktop-tray-")
    atexit.register(shutil.rmtree, folder, True)
    paths = {}
    for state, img in icons.items():
        path = os.path.join(folder, f"{state}.png")
        img.save(path, "PNG")
        paths[id(img)] = path
    own_update, own_remove = icon._update_fs_icon, icon._remove_fs_icon

    def _update_fs_icon(self) -> None:
        path = paths.get(id(self.icon))
        if path is None:
            return own_update()
        self._icon_path = path
        self._icon_valid = True

    def _remove_fs_icon(self) -> None:
        # The first image, set before this patch, is pystray's own temp file.
        if self._icon_path and os.path.dirname(self._icon_path) != folder:
            return own_remove()
        self._icon_valid = False

    icon._update_fs_icon = types.MethodType(_update_fs_icon, icon)
    icon._remove_fs_icon = types.MethodType(_remove_fs_icon, icon)


def _signal_shutdown(loop_holder: dict[str, Any]) -> None:
    """Cross-thread: set the asyncio shutdown event from the tray thread."""
    loop = loop_holder.get("loop")
    shutdown = loop_holder.get("shutdown")
    if loop is None or shutdown is None:
        return
    try:
        loop.call_soon_threadsafe(shutdown.set)
    except RuntimeError:
        # Loop may already be closed — fine, we're shutting down anyway.
        pass
