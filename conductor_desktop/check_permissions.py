"""Standalone host-permission check for conductor-desktop.

Run this anytime to see whether the current process has the grants it needs
to capture the screen and inject mouse/keyboard input:

    python -m conductor_desktop.check_permissions

Remember: macOS TCC permissions attach to the *launching* process (the terminal
or IDE), so this script must be run from the same terminal that will later
launch `conductor-desktop`. Granting permissions while the terminal is running
does NOT take effect until you fully quit and relaunch it.
"""

from __future__ import annotations

import asyncio
import os
import sys

from .core.executor import create_executor
from .core.models.host_node import HostNode


async def _probe_permissions():
    """Run the permission probe and return the resulting Permissions object.

    Used by both the CLI ``_run`` path and the programmatic
    ``run_self_test_async`` helper. The probe's executor is disconnected
    before returning, so a dashboard or tray re-run doesn't leak one.
    """
    host = HostNode(label="permission-check", host_type="resident")
    executor = create_executor(host)
    try:
        await executor.connect()
        return executor.permissions
    finally:
        await executor.disconnect()


def _summarize_perms(perms) -> tuple[bool, str]:
    parts = [
        f"screenshot={'OK' if perms.screenshot_works else 'FAIL'}",
        f"input={'OK' if perms.input_works else 'FAIL'}",
    ]
    if perms.semantic_detail:
        parts.append(f"backend={'OK' if perms.semantic_works else 'FAIL'}")
    detail = "; ".join(parts)
    if not perms.ok:
        first = (perms.screenshot_detail if not perms.screenshot_works
                 else perms.input_detail if not perms.input_works
                 else perms.semantic_detail or "")
        if first:
            detail = f"{first} ({detail})"
    return bool(perms.ok), detail


async def run_self_test_async() -> tuple[bool, str]:
    """Permission probe for the dashboard's /api/perm-test and the tray's
    "Re-run permission test" (submitted to the server's loop).

    Returns ``(ok, detail)``; ``detail`` is a one-line summary suitable
    for both the tray and the dashboard.
    """
    perms = await _probe_permissions()
    return _summarize_perms(perms)


def macos_grant_hint() -> list[str]:
    """Where to grant Screen Recording and Accessibility on macOS.

    macOS checks the app responsible for a process, and harnesses differ:
    a terminal (and Claude Code inside it) passes its grants on to what it
    launches, while some apps (Claude Desktop) disclaim the process, so
    macOS checks the binary itself. Name both, with this binary's real path.
    """
    binary = os.path.realpath(sys.executable)
    return [
        "On macOS, grant Screen Recording and Accessibility (System Settings →",
        "Privacy & Security) to whichever of these macOS shows or asks about:",
        "  - the app that launches conductor-desktop: your terminal, Claude Desktop,",
        "    an IDE; or",
        "  - if that app doesn't pass its permissions on, this binary:",
        f"      {binary}",
        "    (add it with +, then cmd+shift+G and paste the path).",
        "Then fully quit the launching app (cmd+Q) and start it again.",
    ]


async def _run() -> int:
    perms = await _probe_permissions()

    def line(label: str, ok: bool, detail: str) -> str:
        mark = "OK" if ok else "FAIL"
        return f"  [{mark:>4}] {label}: {detail}"

    print()
    print("=" * 70)
    print("conductor-desktop host permission check")
    print("=" * 70)
    print(line("Screenshot", perms.screenshot_works, perms.screenshot_detail))
    print(line("Input     ", perms.input_works, perms.input_detail))
    if perms.semantic_detail:
        print(line("Backend   ", perms.semantic_works, perms.semantic_detail))
    print("-" * 70)

    if perms.ok:
        print("All good — conductor-desktop will start cleanly from this terminal.")
        if sys.platform == "darwin":
            print("Started from another app (Claude Desktop, an IDE), it may need its")
            print("own grants: if macOS asks, grant that app or this binary:")
            print(f"  {os.path.realpath(sys.executable)}")
        print("=" * 70)
        print()
        return 0

    print("Missing permissions.")
    if sys.platform == "darwin":
        for text in macos_grant_hint():
            print(text)
        print("This check runs as the terminal; run it again after relaunching.")
    elif sys.platform == "win32":
        print("On Windows:")
        print("  1. Run from an INTERACTIVE desktop session, not SSH. OpenSSH")
        print("     lands in session 0, which cannot capture or drive the user")
        print("     desktop — Screenshot reports FAIL there even when grants are")
        print("     fine. Use RDP or run at the physical console instead.")
        print("  2. To inject input into ELEVATED apps (Task Manager, regedit,")
        print("     elevated PowerShell), run conductor-desktop itself as")
        print("     Administrator — UIPI blocks input to higher-integrity targets.")
    else:
        print("On Linux Wayland: ensure your user is in the `input` group and")
        print("/dev/uinput is accessible for ydotool (see setup-deps-*.sh scripts).")
    print()
    print("Until this reports all OK, `conductor-desktop` will refuse to start.")
    print("=" * 70)
    print()
    return 1


def main() -> None:
    # Windows consoles default to a legacy code page (cp1252) that can't encode
    # the non-ASCII glyphs in the report (e.g. the macOS "→"); force UTF-8 so the
    # check never crashes mid-print with a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
