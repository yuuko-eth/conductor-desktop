"""The tray icon in a process of its own (Linux, FreeBSD).

pystray's GTK backends run a GLib main loop on the default main context, and
libatspi, which the ui_* tools drive from their own thread, attaches its
D-Bus connections to that same context. With both in one process the tray's
thread dispatched AT-SPI traffic while the ui_* thread was using it: the
server segfaulted in libglib or hung (Ubuntu 22.04 GNOME X11: 4 crashes or hangs
in 8 probe runs with the tray, none in 6 without). Giving libatspi a private
context crashed sooner. So GTK lives here and libatspi in the server.

The server (tray.py, ``_run_holder_with_tray_child``) writes JSON lines to
stdin: one config line, then ``{"state": ...}`` for each icon state and
``{"menu": {...}}`` for the submenus. This writes ``{"action": ...}`` lines
to stdout for the menu items the server acts on. stdin closing means the
server is gone, and the tray goes with it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import webbrowser

log = logging.getLogger("conductor.mcp.tray_child")


class Menu:
    """The latest menu snapshot from the server."""

    def __init__(self) -> None:
        self.recent: list[str] = ["(no calls yet)"]
        self.status: list[str] = []
        self.paused = False

    def update(self, snap: dict) -> None:
        self.recent = list(snap.get("recent") or self.recent)
        self.status = list(snap.get("status") or self.status)
        self.paused = bool(snap.get("paused", self.paused))


def handle(line: str, gate, menu: Menu, refresh) -> bool:
    """Apply one line from the server. False once the server asked to stop."""
    try:
        msg = json.loads(line)
    except ValueError:
        log.warning("tray child: not JSON from the server: %.80r", line)
        return True
    if "state" in msg:
        gate.want(msg["state"])
    if "menu" in msg:
        menu.update(msg["menu"])
        refresh()
    return not msg.get("stop")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    out_lock = threading.Lock()

    def send(msg: dict) -> None:
        with out_lock:
            try:
                sys.stdout.write(json.dumps(msg) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                pass

    config = json.loads(sys.stdin.readline() or "{}")
    # The icon needs no accessibility of its own.
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    try:
        import pystray
    except Exception as e:  # see tray.run_holder: missing, no DISPLAY, no gi
        log.warning("tray unavailable: %s", e, exc_info=True)
        send({"unavailable": f"{type(e).__name__}: {e}"})
        return
    if getattr(pystray.Icon, "__module__", "").endswith("._xorg"):
        # tray.run_holder explains: a dead menu and smeared icons on GNOME/KDE.
        send({"unavailable": "pystray fell through to its X11 backend; repair gi in the venv"})
        return

    from . import tray

    icons = tray._load_state_icons()
    menu = Menu()
    noop = lambda *_a: None  # noqa: E731

    def on_quit(icon, _item) -> None:
        send({"action": "quit"})
        icon.stop()

    def on_dashboard(_icon, _item) -> None:
        webbrowser.open(config["dashboard"])

    icon = pystray.Icon(
        "conductor-desktop", icons[tray.STATE_IDLE], config.get("title", "Conductor Desktop"),
        pystray.Menu(
            pystray.MenuItem(config.get("status_text", "conductor-desktop"), noop, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause input", lambda *_a: send({"action": "pause"}),
                             checked=lambda _item: menu.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", on_dashboard, visible=bool(config.get("dashboard"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Recent tool calls", pystray.Menu(
                lambda: [pystray.MenuItem(s, noop, enabled=False) for s in menu.recent])),
            pystray.MenuItem("Status", pystray.Menu(
                lambda: [pystray.MenuItem(s, noop, enabled=False) for s in menu.status])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Re-run permission test", lambda *_a: send({"action": "perm_test"})),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    if type(icon).__module__ in ("pystray._appindicator", "pystray._gtk"):
        tray._use_stable_icon_files(icon, icons)

    def show(state: str) -> None:
        img = icons.get(state)
        if img is not None:
            try:
                icon.icon = img
            except Exception:
                log.debug("swapping the tray icon failed", exc_info=True)

    gate = tray._IconGate(show, shown=tray.STATE_IDLE)
    stop = threading.Event()

    def ticker() -> None:
        while not stop.wait(1.0):
            gate.flush()

    def reader() -> None:
        try:
            for line in sys.stdin:
                if not handle(line, gate, menu, icon.update_menu):
                    break
        finally:
            stop.set()
            icon.stop()

    def setup(icon) -> None:
        icon.visible = True
        threading.Thread(target=reader, name="tray-child-reader", daemon=True).start()
        threading.Thread(target=ticker, name="tray-child-ticker", daemon=True).start()
        send({"ready": True})

    try:
        icon.run(setup=setup)
    except Exception as e:
        log.info("no system tray: %s", e, exc_info=True)
        send({"unavailable": f"{type(e).__name__}: {e}"})
    finally:
        stop.set()


if __name__ == "__main__":
    main()
