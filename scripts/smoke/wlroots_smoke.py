"""Phase 1 smoke for backends/linux_wlroots.py — self-contained.

labwc (the Raspberry Pi OS Bookworm Wayland default, branded as
"Wayfire" / "wf-panel-pi" on the desktop) is a wlroots compositor but
exposes neither swaymsg nor hyprctl IPC. Our backend's compositor-
detection chain therefore lands on the `wlrctl` titles-only fallback.

This script reproduces that fallback inline so we can confirm the
schema and behavior without deploying the full conductor_desktop package.

Run:
    WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) \\
        python3 wlroots_smoke.py
"""
import asyncio
import shutil
import time


async def _run(cmd, timeout=5):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{cmd[0]} failed (rc={proc.returncode}): {err}")
    return stdout.decode("utf-8", errors="replace")


def _have_sway():
    return shutil.which("swaymsg") is not None


def _have_hyprctl():
    return shutil.which("hyprctl") is not None


async def list_screens():
    """Phase 1: with neither sway nor hyprctl, no portable enumeration
    exists for labwc/other wlroots compositors. Returns []. Production
    code is the same."""
    if _have_sway() or _have_hyprctl():
        return ["(unexpected — would use IPC path)"]
    return []


async def list_windows():
    """wlrctl titles-only fallback (production path for labwc/Wayfire)."""
    if _have_sway() or _have_hyprctl():
        return [{"_": "would use IPC path"}]
    try:
        output = await _run(["wlrctl", "toplevel", "list"])
    except (RuntimeError, FileNotFoundError) as e:
        print(f"!! wlrctl failed: {e}")
        return []

    windows = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        windows.append({
            "id": str(len(windows)),
            "title": line,
            "process_name": "",
            "pid": 0,
            "bounds": [0, 0, 0, 0],
            "state": "normal",
            "desktop": 0,
            "focused": False,
            "monitor": -1,
            "z": len(windows),
            "layer": "normal",
            "sticky": False,
            "workspace": {"global_index": None, "per_output": None},
        })
    return windows


async def main():
    print(f"sway IPC available:    {_have_sway()}")
    print(f"hyprctl IPC available: {_have_hyprctl()}")
    print(f"wlrctl available:      {shutil.which('wlrctl') is not None}")
    print()
    t0 = time.perf_counter()
    screens = await list_screens()
    print(f"SCREENS ({(time.perf_counter()-t0)*1000:.0f}ms): {screens}")
    print("  (expected [] on labwc — no portable monitor IPC)")
    print()
    t0 = time.perf_counter()
    ws = await list_windows()
    t = (time.perf_counter() - t0) * 1000
    print(f"WINDOWS ({t:.0f}ms, {len(ws)} total) — fallback path:")
    for w in ws:
        # Confirm every Phase 1 field is present even in the minimal path.
        keys = sorted(w.keys())
        print(f"  z={w['z']:>2} title={w['title']!r}")
        print(f"     keys: {keys}")


asyncio.run(main())
