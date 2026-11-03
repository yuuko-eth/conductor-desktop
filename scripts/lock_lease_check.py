#!/usr/bin/env python3
"""Acceptance check: the desktop input lock as a lease, with two real servers.

Starts two conductor-desktop servers over stdio on a scratch lock file, so a
server already running on the desktop is left alone, and walks the handover:

  A holds at startup and shows the tray icon; B starts as a spectator
  B's input is refused, naming A and when A lets go
  A goes idle past the limit and lets go; its icon goes
  B acts, takes the lock, and the icon moves to B
  A is told it lost the desktop
  a pause set outside B stops B, and a paused B doesn't let go when idle
  release_input hands the desktop over; A takes it back

Each server's stderr goes to its own log; the "tray icon shown/hidden" lines
there are checked on every platform. Where it can, the check also looks at
the real icon: on macOS 14 a status-bar window owned by the server; on macOS
26, where Control Center owns every menu-bar item, the number of items
against a count taken before the servers started; on Linux the server's
tray_child process (unless the desktop has no tray to dock into). Input is
pointer moves only.

Run it inside the desktop session (Terminal on macOS, the logged-on session
on Windows, the session's environment on Linux):

    python scripts/lock_lease_check.py [--idle 20] [--out DIR]

Exits 1 if any step fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

results: list[bool] = []
# macOS: whether apps own their status-bar windows here (decided at startup).
_OWNS_STATUS_WINDOWS = False
# Linux: servers whose tray child found no tray to dock into.
_NO_TRAY: dict[int, bool] = {}
_BASE_ITEMS: int | None = None
_SERVERS: list = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  - {detail}" if detail else ""), flush=True)


def text(res) -> str:
    return " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def _status_windows() -> list[dict]:
    import Quartz
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    return [w for w in wins if w.get("kCGWindowLayer") == 25]


def status_item_count() -> int | None:
    """On-screen menu-bar items (macOS), for the macOS 26 count check."""
    return len(_status_windows()) if sys.platform == "darwin" else None


def live_tray(pid: int) -> bool | None:
    """Whether server ``pid`` shows a tray icon, where that can be told from
    outside by process; None where it can't (Windows, macOS 26)."""
    if sys.platform == "darwin":
        owners = {int(w["kCGWindowOwnerPID"]) for w in _status_windows()}
        if pid in owners:
            return True
        # macOS 14: apps own their status windows. macOS 26: Control Center
        # owns them all, so a missing pid says nothing.
        python_owned = any(str(w.get("kCGWindowOwnerName", "")).lower().startswith("python")
                           for w in _status_windows())
        return False if python_owned or _OWNS_STATUS_WINDOWS else None
    if sys.platform.startswith(("linux", "freebsd")):
        if _NO_TRAY.get(pid):
            return None
        import psutil
        try:
            kids = psutil.Process(pid).children()
        except psutil.NoSuchProcess:
            return False
        return any("conductor_desktop.tray_child" in " ".join(k.cmdline()) for k in kids
                   if k.is_running() and k.status() != psutil.STATUS_ZOMBIE)
    return None


class Server:
    def __init__(self, name: str, out: Path) -> None:
        self.name, self.log = name, out / f"server-{name}.log"
        self.pid: int | None = None
        self.session: ClientSession | None = None

    def events(self) -> list[str]:
        """'shown' / 'hidden' in the order this server logged them."""
        out = []
        if not self.log.exists():  # not started yet
            return out
        for line in self.log.read_text(errors="replace").splitlines():
            if "tray icon shown" in line:
                out.append("shown")
            elif "tray icon hidden" in line:
                out.append("hidden")
        return out

    def shows(self) -> bool:
        ev = self.events()
        shown = bool(ev) and ev[-1] == "shown"
        if (self.pid and self.log.exists()
                and "No system tray available" in self.log.read_text(errors="replace")):
            _NO_TRAY[self.pid] = True
        return shown

    async def start(self, stack: AsyncExitStack, env: dict) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from conductor_desktop.server import run; run()"], env=env)
        errlog = stack.enter_context(open(self.log, "w"))
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        self.session = await stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        for line in self.log.read_text(errors="replace").splitlines():
            if "desktop input lock:" in line and "(pid " in line:
                self.pid = int(line.rsplit("(pid ", 1)[1].split(")")[0])

    async def move(self, x: int, y: int):
        return await self.session.call_tool("mouse_move_at", {"x": x, "y": y})


async def settle() -> None:
    await asyncio.sleep(2.0)  # tray changes go through the UI thread / a child


def _count_ok() -> bool | None:
    """macOS 26: menu-bar items = the count before the servers started plus
    one per server whose log says its icon is shown."""
    if _BASE_ITEMS is None or _OWNS_STATUS_WINDOWS:
        return None
    return status_item_count() == _BASE_ITEMS + sum(s.shows() for s in _SERVERS)


def tray_state(srv: Server) -> str:
    log_state = "shown" if srv.shows() else "hidden"
    live = live_tray(srv.pid) if srv.pid else None
    extra = ""
    if _BASE_ITEMS is not None and not _OWNS_STATUS_WINDOWS:
        extra = f" menu-bar items={status_item_count()} (before={_BASE_ITEMS})"
    return f"log={log_state} live={live}{extra}"


def tray_is(srv: Server, shown: bool) -> bool:
    logged = srv.shows()  # first: it also notes a server whose desktop has no tray
    live = live_tray(srv.pid) if srv.pid else None
    counted = _count_ok()
    return logged == shown and (live is None or live == shown) and counted is not False


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--idle", type=float, default=20.0, help="idle release, seconds")
    ap.add_argument("--out", type=Path, default=None, help="where the server logs go")
    args = ap.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="lease-check-"))
    out.mkdir(parents=True, exist_ok=True)
    lockdir = Path(tempfile.mkdtemp(prefix="lease-lock-"))
    lock_file, meta, pause = lockdir / "desktop.lock", lockdir / "desktop.lock.json", lockdir / "input-paused"
    env = {**os.environ,
           "CONDUCTOR_DESKTOP_LOCK_PATH": str(lock_file),
           "CONDUCTOR_DESKTOP_LOCK_IDLE_RELEASE": str(args.idle),
           "CONDUCTOR_DESKTOP_AUDIT": "false", "CONDUCTOR_DESKTOP_DELTAS": "false",
           "CONDUCTOR_DESKTOP_TOOL_TIMING": "false", "CONDUCTOR_DESKTOP_DASHBOARD": "false"}
    wait = args.idle + 18  # the idle check runs every idle/4 s, at most 15 s
    print(f"platform={sys.platform} idle={args.idle}s logs={out} lock={lock_file}", flush=True)

    global _BASE_ITEMS, _OWNS_STATUS_WINDOWS
    _BASE_ITEMS = status_item_count()
    a, b = Server("A", out), Server("B", out)
    _SERVERS.extend([a, b])
    async with AsyncExitStack() as stack:
        await a.start(stack, env)
        await settle()
        if sys.platform == "darwin":
            _OWNS_STATUS_WINDOWS = a.pid in {int(w["kCGWindowOwnerPID"]) for w in _status_windows()}
            print(f"macOS status items owned by the app: {_OWNS_STATUS_WINDOWS} "
                  f"(else counted: {_BASE_ITEMS} before)", flush=True)
        holder = json.loads(meta.read_text())["pid"] if meta.exists() else None
        check("A holds the lock at startup", holder == a.pid, f"A={a.pid} meta={holder}")
        check("A shows the tray icon", tray_is(a, True), tray_state(a))

        await b.start(stack, env)
        await settle()
        check("B starts as a spectator without an icon", tray_is(b, False), tray_state(b))

        res = await b.move(200, 200)
        t = text(res)
        check("B's input is refused, naming A and when it lets go",
              res.isError and f"pid {a.pid}" in t and "idle" in t and "release_input" in t, t[:200])

        res = await a.move(210, 210)
        check("A acts", not res.isError, text(res)[:80])

        print(f"... {wait:.0f} s for A to go idle", flush=True)
        await asyncio.sleep(wait)
        check("A let go after the idle limit", not meta.exists())
        check("A's icon went with the lock", tray_is(a, False), tray_state(a))

        res = await b.move(220, 220)
        await settle()
        holder = json.loads(meta.read_text())["pid"] if meta.exists() else None
        check("B acts and takes the lock", not res.isError and holder == b.pid, text(res)[:80])
        check("the icon moved to B", tray_is(b, True) and tray_is(a, False),
              f"A: {tray_state(a)}; B: {tray_state(b)}")

        res = await a.move(230, 230)
        t = text(res)
        check("A is told it lost the desktop", res.isError and "let go of the desktop" in t, t[:160])

        pause.write_text("set by lock_lease_check\n")
        res = await b.move(240, 240)
        check("a pause set outside B stops B", res.isError and "paused" in text(res), text(res)[:100])
        print(f"... {wait:.0f} s: a paused holder must keep the lock", flush=True)
        await asyncio.sleep(wait)
        check("paused B kept the lock and its icon", meta.exists() and tray_is(b, True), tray_state(b))
        pause.unlink()

        res = await b.session.call_tool("release_input", {})
        await settle()
        check("release_input hands over; B's icon goes",
              "released" in text(res) and not meta.exists() and tray_is(b, False), tray_state(b))
        res = await a.move(250, 250)
        await settle()
        check("A takes it back; A's icon returns", not res.isError and tray_is(a, True), tray_state(a))
        pids = (a.pid, b.pid)

    await asyncio.sleep(3)
    left = [p for p in pids if p and live_tray(p)]
    items = status_item_count()
    back_to_base = _BASE_ITEMS is None or items == _BASE_ITEMS
    check("both servers gone, no icon left behind", not left and back_to_base,
          f"still showing: {left}" + (f"; menu-bar items {items}, before {_BASE_ITEMS}"
                                      if _BASE_ITEMS is not None else ""))
    print(f"\n{sum(results)}/{len(results)} passed  (server logs in {out})", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
