#!/usr/bin/env python3
"""Type the smoke lines into several kinds of text field and read them back.

The smoke matrix types into one editor per platform, so an editor's quirk
looks like a typing bug and the other way round (Windows 11 Notepad with
Formatting on turns a leading "-" or "1 " into list formatting). This runs
the same lines through ``type_text`` / ``key`` into each target here, a few
rounds each, and reads them back with select-all, copy and clipboard_get:

  Windows  notepad (WinUI, Formatting as found), edit (Win32 EDIT via a
           WinForms TextBox), richedit (WinForms RichTextBox), edge (a
           <textarea> in Edge on a throwaway profile), gvim (Easy mode, when
           installed)
  macOS    textedit (plain text), chrome (a <textarea>, throwaway profile)

Starts its own server over stdio on a scratch input lock, so a server
already on the desktop is left alone. Each target's window is its own and
closed afterwards. Run it inside the desktop session.

    python scripts/typing_targets_check.py [--rounds 3] [--targets edit,edge]

Exits 1 if any round of any target reads back different text.
"""

from __future__ import annotations

import argparse
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

LINES = ["-conductor smoke 1", "ünïcødé 測試 🎉", "-a b", "1 é🎉"]
MOD = "cmd" if sys.platform == "darwin" else "ctrl"
TAG = f"conductor-typing-{os.getpid()}"


def text(res) -> str:
    return " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def windows_of(res) -> list[dict]:
    try:
        return json.loads(text(res)).get("windows", [])
    except ValueError:
        return []


# ── targets ─────────────────────────────────────────────────────────


class Target:
    """A window with a text field: start it, find it by title, close it."""

    name = ""
    save_on_close = False

    def __init__(self, work: Path) -> None:
        self.work = work
        self.proc: subprocess.Popen | None = None

    @property
    def title(self) -> str:  # substring of the window title
        return f"{TAG}-{self.name}"

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        if self.proc is None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, check=False)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


_WINFORMS = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = '{title}'; $f.Width = 700; $f.Height = 400; $f.StartPosition = 'CenterScreen'
$t = New-Object System.Windows.Forms.{control}
$t.Multiline = $true; $t.Dock = 'Fill'; $t.Font = New-Object System.Drawing.Font('Segoe UI', 12)
# A multi-line Win32 EDIT has no Ctrl+A; apps built on it add their own.
$t.Add_KeyDown({{ if ($_.Control -and $_.KeyCode -eq 'A') {{ $t.SelectAll(); $_.SuppressKeyPress = $true }} }})
$f.Controls.Add($t); $f.Add_Shown({{ $f.Activate(); $t.Focus() }})
[void]$f.ShowDialog()
"""


class WinForms(Target):
    control = ""

    def start(self) -> None:
        script = self.work / f"{self.name}.ps1"
        script.write_text(_WINFORMS.format(title=self.title, control=self.control),
                          encoding="utf-8-sig")
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class Edit(WinForms):
    name, control = "edit", "TextBox"


class RichEdit(WinForms):
    name, control = "richedit", "RichTextBox"


class Notepad(Target):
    name = "notepad"
    save_on_close = True

    def start(self) -> None:
        f = self.work / f"{self.title}.txt"
        f.write_text("", encoding="utf-8")
        # Notepad runs every window in one process: close by the window, not this pid.
        subprocess.Popen(["notepad.exe", str(f)])

    def stop(self) -> None:
        pass  # closed with ctrl+s, ctrl+w from the session


_PAGE = ("<!doctype html><meta charset=utf-8><title>{title}</title>"
         "<textarea autofocus style='width:95vw;height:90vh;font-size:20px'></textarea>")


class Browser(Target):
    exe: list[str] = []

    def start(self) -> None:
        page = self.work / f"{self.name}.html"
        page.write_text(_PAGE.format(title=self.title), encoding="utf-8")
        profile = self.work / f"{self.name}-profile"
        self.proc = subprocess.Popen(
            [*self.exe, f"--user-data-dir={profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-sync", "--new-window",
             page.as_uri()])


class Edge(Browser):
    name = "edge"
    exe = [shutil.which("msedge") or
           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]


class Chrome(Browser):
    name = "chrome"
    exe = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]


class GVim(Target):
    """gVim in Easy mode (-y): modeless, with Windows-style Ctrl+A/C/V."""
    name = "gvim"

    def start(self) -> None:
        exe = shutil.which("gvim.exe") or r"C:\tools\vim\vim92\gvim.exe"
        if exe.lower().endswith(".bat"):  # the installer's launcher; start the real one
            exe = r"C:\tools\vim\vim92\gvim.exe"
        f = self.work / f"{self.title}.txt"
        f.write_text("", encoding="utf-8")
        self.proc = subprocess.Popen([exe, "-y", "-n", str(f)])  # -n: no swap file


class TextEdit(Target):
    name = "textedit"
    save_on_close = True

    def start(self) -> None:
        f = self.work / f"{self.title}.txt"
        f.write_text("", encoding="utf-8")
        subprocess.run(["open", "-a", "TextEdit", str(f)], check=False)

    def stop(self) -> None:
        pass


TARGETS = {
    "win32": [Notepad, Edit, RichEdit, Edge, GVim],
    "darwin": [TextEdit, Chrome],
}


# ── the check ───────────────────────────────────────────────────────


async def find_window(s: ClientSession, title: str, timeout: float = 20.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wins = windows_of(await s.call_tool("list_windows", {"match": title, "page_size": 0}))
        if wins:
            return wins[0]
        await asyncio.sleep(0.5)
    return None


async def focused_title(s: ClientSession) -> str:
    wins = windows_of(await s.call_tool("list_windows", {"page_size": 0}))
    return next((w.get("title") or "" for w in wins if w.get("focused")), "")


SETTLE = 0.0  # --settle: seconds to wait before and after each Enter

# How the lines go in. "lines": type_text per line with key enter between
# (lines under 8 characters go as key presses, the rest is pasted); "block":
# one type_text with the whole text (pasted); "-keys": keystrokes=true, so
# nothing is pasted and a block's newlines go as characters.
MODES = ("lines", "block", "lines-keys", "block-keys")


async def one_round(s: ClientSession, target: Target, mode: str = "lines") -> tuple[bool, str]:
    await s.call_tool("focus_window", {"title": target.title})
    await asyncio.sleep(0.5)
    if target.title not in await focused_title(s):
        return False, "the target didn't take focus; nothing typed"
    await s.call_tool("hotkey", {"keys": [MOD, "a"]})
    await s.call_tool("key", {"key": "delete"})
    keys = {"keystrokes": True} if mode.endswith("-keys") else {}
    if mode.startswith("block"):
        await s.call_tool("type_text", {"text": "\n".join(LINES), **keys})
    for i, line in enumerate(LINES if mode.startswith("lines") else ()):
        if i:
            await asyncio.sleep(SETTLE)
            await s.call_tool("key", {"key": "enter"})
            await asyncio.sleep(SETTLE)
        await s.call_tool("type_text", {"text": line, **keys})
    await asyncio.sleep(0.5)
    await s.call_tool("hotkey", {"keys": [MOD, "a"]})
    await s.call_tool("hotkey", {"keys": [MOD, "c"]})
    await asyncio.sleep(0.3)
    got = text(await s.call_tool("clipboard_get", {}))
    got = got.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    want = "\n".join(LINES)
    if got == want:
        return True, "exact"
    diff = [f"{w!r} -> {g!r}" for w, g in zip(want.split("\n"), got.split("\n")) if w != g]
    return False, "; ".join(diff) or f"got {got!r}"


async def close(s: ClientSession, target: Target) -> None:
    if target.save_on_close:
        await s.call_tool("focus_window", {"title": target.title})
        await asyncio.sleep(0.3)
        if target.title in await focused_title(s):
            await s.call_tool("hotkey", {"keys": [MOD, "s"]})
            await asyncio.sleep(1.0)  # a close right after the save gets dropped
            for _ in range(3):
                await s.call_tool("hotkey", {"keys": [MOD, "w"]})
                if await find_window(s, target.title, timeout=3.0) is None:
                    break
                await s.call_tool("focus_window", {"title": target.title})
            else:
                print(f"warning: {target.name} window still open after closing", flush=True)
    target.stop()


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the lines aren't ASCII
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--targets", default="", help="comma-separated names (default: all here)")
    ap.add_argument("--modes", default="lines",
                    help=f"comma-separated, of {', '.join(MODES)} (default: lines)")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="seconds to wait around each Enter (tells an app's race from ours)")
    args = ap.parse_args()
    global SETTLE
    SETTLE = args.settle
    kinds = TARGETS.get(sys.platform, [])
    if args.targets:
        wanted = set(args.targets.split(","))
        kinds = [k for k in kinds if k.name in wanted]
    if not kinds:
        print(f"no typing targets for {sys.platform}")
        return 0
    work = Path(tempfile.mkdtemp(prefix="typing-targets-"))
    env = {**os.environ, "CONDUCTOR_DESKTOP_LOCK_PATH": str(work / "desktop.lock"),
           "CONDUCTOR_DESKTOP_AUDIT": "false", "CONDUCTOR_DESKTOP_DELTAS": "false",
           "CONDUCTOR_DESKTOP_TOOL_TIMING": "false"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from conductor_desktop.server import run; run()"], env=env)
    modes = [m for m in args.modes.split(",") if m in MODES] or ["lines"]
    rows: list[tuple[str, int, int, list[str]]] = []
    with open(work / "server.log", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            for kind in kinds:
                target = kind(work)
                try:
                    target.start()
                except OSError as e:
                    rows.append((target.name, 0, 0, [f"couldn't start: {e}"]))
                    continue
                try:
                    if await find_window(s, target.title) is None:
                        rows.append((target.name, 0, 0, ["no window appeared"]))
                        continue
                    await asyncio.sleep(1.0)  # let it finish drawing and take focus
                    for mode in modes:
                        passed, notes = 0, []
                        for n in range(args.rounds):
                            ok, note = await one_round(s, target, mode)
                            passed += ok
                            if not ok:
                                notes.append(f"round {n + 1}: {note}")
                        label = target.name if modes == ["lines"] else f"{target.name}/{mode}"
                        rows.append((label, passed, args.rounds, notes))
                finally:
                    await close(s, target)
    print(f"\nplatform={sys.platform} rounds={args.rounds} settle={SETTLE}s lines={LINES!r}")
    for name, passed, total, notes in rows:
        verdict = "PASS" if total and passed == total else "FAIL"
        print(f"{verdict}  {name:22} {passed}/{total}")
        for note in notes:
            print(f"      {note}")
    return 0 if all(t and p == t for _, p, t, _ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
