"""read_grid against a real table: File Explorer's Details view (Windows).

The smoke matrix's a11y scenario runs read_grid on its scratch editor, which
has no table, so it only proves the call doesn't fail. This one makes a
scratch folder with 60 known files, opens it in Explorer, switches to Details
view and checks what read_grid returns against what is on disk:

- every file comes back, in Explorer's name order, one row per file;
- the grid has more than the name column (date, type, size);
- the cells on screen carry a click bbox inside the window, and rows past the
  viewport still come back (GridPattern.GetItem reaches virtualized rows);
- max_rows windows the read.

Run it in the logged-in desktop session (RDP is fine), not over SSH: session 0
sees no windows. Don't touch the keyboard or mouse while it runs. It closes
the Explorer window and deletes the folder afterwards. Only file names it made
itself are printed, so the report is safe to read over SSH.

    .venv\\Scripts\\python.exe -W ignore scripts\\smoke\\read_grid_explorer.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

N_FILES = 60
STEP_TIMEOUT = 60.0


def first_json(text: str):
    """The JSON value at the start of a reply (tools may append a deltas block)."""
    return json.JSONDecoder().raw_decode(text.lstrip())[0]


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def check(self, name: str, ok: bool, note: str = "") -> bool:
        self.results.append({"check": name, "ok": bool(ok), "note": note})
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {note}" if note else ""), flush=True)
        return ok


async def call(session: ClientSession, tool: str, **args) -> tuple[bool, str]:
    print(f"   {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})", flush=True)
    try:
        res = await asyncio.wait_for(session.call_tool(tool, args), STEP_TIMEOUT)
    except TimeoutError:
        return False, f"timed out after {STEP_TIMEOUT:.0f}s"
    return not res.isError, "\n".join(c.text for c in res.content if c.type == "text")


async def find_window(session: ClientSession, needle: str, timeout: float = 15.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, text = await call(session, "list_windows", match=needle, page_size=0)
        if ok:
            windows = first_json(text).get("windows", [])
            if windows:
                return windows[0]
        await asyncio.sleep(1.0)
    return None


def row_texts(cells: list[dict]) -> dict[int, list[str]]:
    rows: dict[int, list[str]] = {}
    for cell in cells:
        rows.setdefault(cell["r"], []).append(cell.get("text") or "")
    return rows


async def run(session: ClientSession, folder: Path, checks: Checks) -> dict:
    facts: dict = {}
    names = [f"grid-{i:03d}" for i in range(N_FILES)]

    subprocess.Popen(["explorer.exe", str(folder)])
    win = await find_window(session, folder.name)
    if not checks.check("explorer window opened", win is not None, folder.name):
        return facts
    title = win["title"]
    facts["window"] = {"title": title, "bounds": win.get("bounds")}

    await call(session, "focus_window", title=folder.name)
    await asyncio.sleep(0.5)
    await call(session, "hotkey", keys=["ctrl", "shift", "6"])  # Details view
    await asyncio.sleep(1.5)

    t = time.monotonic()
    ok, text = await call(session, "read_grid", title=folder.name, max_rows=100)
    facts["read_seconds"] = round(time.monotonic() - t, 2)
    if not checks.check("read_grid call ok", ok, text[:200] if not ok else ""):
        return facts
    grid = first_json(text)
    cells = grid.get("cells", [])
    facts.update(rows=grid.get("rows"), cols=grid.get("cols"), source=grid.get("source"),
                 capabilities=grid.get("capabilities"), failure=grid.get("failure"),
                 cells=len(cells))
    checks.check("no failure", grid.get("failure") is None, str(grid.get("failure")))
    checks.check("found a grid", grid.get("source") not in (None, "none"),
                 f"source={grid.get('source')}")
    checks.check("one row per file", grid.get("rows") == N_FILES,
                 f"rows={grid.get('rows')}, want {N_FILES}")
    checks.check("more than the name column", (grid.get("cols") or 0) >= 3,
                 f"cols={grid.get('cols')}")

    rows = row_texts(cells)
    facts["rows_read"] = len(rows)
    missing = [n for n in names if not any(n in t for texts in rows.values() for t in texts)]
    checks.check("every file name read", not missing,
                 f"missing {len(missing)}: {missing[:5]}" if missing else f"{N_FILES}/{N_FILES}")
    misplaced = [r for r in range(N_FILES) if not any(names[r] in t for t in rows.get(r, []))]
    checks.check("rows in name order", not misplaced,
                 f"first misplaced rows: {misplaced[:5]}" if misplaced else "")
    facts["row0"] = rows.get(0, [])

    bounds = win.get("bounds") or [0, 0, 0, 0]
    wx, wy, ww, wh = bounds

    def inside(b) -> bool:
        x, y, w, h = b
        cx, cy = x + w / 2, y + h / 2
        return wx <= cx <= wx + ww and wy <= cy <= wy + wh

    with_bbox = [c for c in cells if c.get("bbox")]
    # A realized row just past the viewport can still report a rect, so "on
    # screen" means the bbox centre is inside the window.
    visible_rows = {c["r"] for c in with_bbox if inside(c["bbox"])}
    facts.update(cells_with_bbox=len(with_bbox), rows_on_screen=len(visible_rows),
                 rows_with_bbox_outside_window=len({c["r"] for c in with_bbox} - visible_rows))
    checks.check("first row has an on-screen bbox", 0 in visible_rows,
                 f"{len(visible_rows)} rows on screen")
    checks.check("rows past the viewport still read", len(rows) > len(visible_rows),
                 f"{len(rows)} rows read, {len(visible_rows)} on screen")
    facts["can_set_cells"] = sum(1 for c in cells if c.get("can_set"))

    ok, text = await call(session, "read_grid", title=folder.name, max_rows=10)
    small = first_json(text) if ok else {}
    small_rows = row_texts(small.get("cells", []))
    checks.check("max_rows=10 windows the read", ok and len(small_rows) == 10,
                 f"rows read={len(small_rows)}, rows reported={small.get('rows')}")

    ok, text = await call(session, "ui_find", title=folder.name, name=names[5])
    checks.check("ui_find agrees on a cell", ok and names[5] in text)

    await call(session, "focus_window", title=folder.name)
    await asyncio.sleep(0.3)
    await call(session, "hotkey", keys=["ctrl", "w"])
    await asyncio.sleep(1.0)
    gone = await find_window(session, folder.name, timeout=3.0) is None
    checks.check("explorer window closed", gone)
    return facts


async def main() -> None:
    if sys.platform != "win32":
        sys.exit("Windows only: File Explorer is the table under test.")
    folder = Path(tempfile.mkdtemp(prefix="conductor-grid-"))
    for i in range(N_FILES):
        # Distinct sizes so the Size column isn't one repeated value.
        (folder / f"grid-{i:03d}.txt").write_bytes(b"x" * (i * 97 + 1))

    lock = Path(tempfile.gettempdir()) / "conductor-smoke.lock"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CONDUCTOR_DESKTOP_", "CONDUCTOR_MCP_"))}
    env.update(CONDUCTOR_DESKTOP_AUTO_SCREENSHOT="false", CONDUCTOR_DESKTOP_DELTAS="false",
               CONDUCTOR_DESKTOP_AUDIT="false", CONDUCTOR_DESKTOP_LOCK_PATH=str(lock),
               PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from conductor_desktop.server import run; run()"], env=env)
    checks = Checks()
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            facts = await run(session, folder, checks)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    passed = sum(r["ok"] for r in checks.results)
    verdict = "pass" if passed == len(checks.results) else "fail"
    out = Path.home() / "read_grid_explorer.json"
    out.write_text(json.dumps({"verdict": verdict, "checks": checks.results, "facts": facts},
                              indent=2), encoding="utf-8")
    print(f"\n{verdict}: {passed}/{len(checks.results)} checks; facts:")
    print(json.dumps(facts, indent=2))
    print(f"report: {out}")


if __name__ == "__main__":
    asyncio.run(main())
