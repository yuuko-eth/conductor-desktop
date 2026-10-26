"""Smoke test the tray state-indicator without launching the full MCP server.

Builds a TrayStateMachine, attaches it to a real pystray Icon (so we
exercise the icon-swap path on the actual platform tray), and cycles
through all four states on a timer:

    idle → observing → acting → idle → errored → idle (loop)

Watch the menubar; you should see the four colored glyphs in turn.
The error state should latch ~2s before snapping back to idle even
if the next "start" arrives sooner.

Quit via the menu's Quit item.

Run from conductor-desktop/:
    ./.venv/bin/python scripts/smoketest_tray.py
"""

from __future__ import annotations

import sys
import threading
import time

import pystray

from conductor_desktop.tray import (
    _DEFAULT_LOAD_PX,
    _load_state_icons,
    _load_state_png_bytes,
    _patch_macos_assert_image,
)
from conductor_desktop.tray_state import (
    STATE_IDLE,
    TrayStateMachine,
)


def _load_icons():
    return _load_state_icons(_DEFAULT_LOAD_PX)


def main() -> None:
    sm = TrayStateMachine()
    icons = _load_icons()
    stop = threading.Event()

    def on_quit(icon, _item):
        stop.set()
        icon.stop()

    icon = pystray.Icon(
        "conductor-tray-smoke",
        icons[STATE_IDLE],
        "Conductor Desktop — smoke test",
        pystray.Menu(
            pystray.MenuItem(
                "current state",
                lambda *_: None,
                enabled=False,
            ),
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    if sys.platform == "darwin":
        _patch_macos_assert_image(icon, sm, _load_state_png_bytes())

    def on_state_change(new_state: str) -> None:
        print(f"[state] {new_state}", flush=True)
        try:
            icon.icon = icons[new_state]
        except Exception as e:
            print(f"  icon-swap failed: {e}", flush=True)

    sm.set_observer(on_state_change)

    def driver() -> None:
        # Wait for the icon to be ready before driving.
        time.sleep(2.0)
        # Cycle 1: observing
        cid = sm.start("screenshot")
        time.sleep(1.5)
        sm.end(cid, success=True)
        time.sleep(1.0)
        # Cycle 2: acting
        cid = sm.start("click", args=("the button",))
        time.sleep(1.5)
        sm.end(cid, success=True)
        time.sleep(1.0)
        # Cycle 3: errored (latch hold ~2s)
        cid = sm.start("type_text", args=("hello",))
        time.sleep(0.5)
        sm.end(cid, success=False, error="ECHO")
        time.sleep(0.5)
        # Latch should still be red here
        cid2 = sm.start("screenshot")
        time.sleep(0.4)
        sm.end(cid2, success=True)
        # Tick the latch ticker manually for the smoke test (in a real
        # tray the ticker thread does this every second).
        for _ in range(30):
            if stop.is_set():
                return
            sm.reclassify_check()
            time.sleep(0.3)
        # Steady idle
        print("[driver] cycle done — leaving in idle state. Use Quit menu.",
              flush=True)

    t = threading.Thread(target=driver, name="smoke-driver", daemon=True)
    t.start()

    try:
        icon.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
