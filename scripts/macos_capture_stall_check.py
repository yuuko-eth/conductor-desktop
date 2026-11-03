#!/usr/bin/env python3
"""Regression check: two conductor-desktop servers from one Python path on macOS.

On recent macOS an in-process capture from the second live process of an
executable path stalled 30 s (replayd keys capture clients by path), so the
second MCP client's startup and every capture took ~30 s. This starts two real
servers over stdio from the same interpreter, has the first capture, then
times the second server's startup and captures, and finally both at once.

Run it from Terminal in the GUI session, not over SSH: SSH-launched processes
don't go through replayd, so they never stall and the check proves nothing.

    .venv/bin/python scripts/macos_capture_stall_check.py [--limit 1.0]

Exits 1 if any capture (or the second server's startup) exceeds --limit s.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _start(stack: AsyncExitStack) -> tuple[ClientSession, float]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from conductor_desktop.server import run; run()"],
        env=dict(os.environ),
    )
    t = time.perf_counter()
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session, time.perf_counter() - t


async def _capture(session: ClientSession) -> float:
    t = time.perf_counter()
    res = await session.call_tool("capture_screen", {})
    took = time.perf_counter() - t
    if res.isError:
        raise SystemExit(f"capture_screen failed: {res.content}")
    return took


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=float, default=1.0, help="seconds allowed per capture")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    if sys.platform != "darwin":
        print("macOS only")
        return 0
    if os.environ.get("SSH_CONNECTION"):
        print("warning: running over SSH, where captures never stall; run from Terminal")

    results: list[tuple[str, float]] = []
    async with AsyncExitStack() as stack:
        a, a_start = await _start(stack)
        results.append(("server A startup", a_start))
        results.append(("server A capture (now holds a capture client)", await _capture(a)))
        b, b_start = await _start(stack)
        results.append(("server B startup (runs a capture self-check)", b_start))
        for i in range(args.rounds):
            results.append((f"server B capture #{i + 1}", await _capture(b)))
            results.append((f"server A capture #{i + 1}", await _capture(a)))
        for i in range(args.rounds):
            ta, tb = await asyncio.gather(_capture(a), _capture(b))
            results.append((f"A+B at once #{i + 1} (slower)", max(ta, tb)))

    worst = 0.0
    for label, secs in results:
        print(f"{secs:7.2f} s  {label}")
        if "startup" not in label or label.startswith("server B"):
            # A's startup has no competitor; B's startup includes the self-check
            # capture, so it counts, with a few seconds of normal start-up allowed.
            budget = args.limit + (5.0 if "startup" in label else 0.0)
            if secs > budget:
                worst = max(worst, secs)
    if worst:
        print(f"FAIL: slowest over budget took {worst:.2f} s (limit {args.limit} s per capture)")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
