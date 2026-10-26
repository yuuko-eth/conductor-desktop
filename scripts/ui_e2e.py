"""E2E for the ``ui_*`` tools through the real MCP server over stdio.

Runs the ui_* end-to-end scenarios against a real desktop and writes a
Markdown report with every tool call, its result and its time.

Run it inside the logged-in desktop session (on Windows, NOT over SSH:
session 0 sees no windows):

    python scripts/ui_e2e.py                       # all scenarios
    python scripts/ui_e2e.py survey calc notepad   # some
    python scripts/ui_e2e.py --report out.md

No grounder is configured: the server starts with no grounder or describe URL,
so any tool that needs one fails, and every ``ui_*`` step that passes proves it
doesn't (scenario 8).

Windows, Linux and FreeBSD; the macOS run was done by hand (see the design
doc). On Linux and FreeBSD the apps follow the desktop: GNOME (Calculator, Text Editor, Settings,
Files), KDE (KCalc, KWrite, kcmshell6, Dolphin) or XFCE (Mousepad, Appearance
settings, Thunar); set ``UI_E2E_DESKTOP=gnome|kde|xfce`` to choose (autodetect
reads XDG_CURRENT_DESKTOP, which an SSH session doesn't have). Only scratch
windows are edited: the editor runs as a separate instance on a new document,
Files opens a scratch folder, and a toggled setting is put back.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REF = re.compile(r"\[(e\d+)\]")


@dataclass
class Step:
    scenario: str
    tool: str
    args: dict
    ok: bool
    text: str
    seconds: float


@dataclass
class Run:
    session: ClientSession
    steps: list[Step] = field(default_factory=list)
    verdicts: dict[str, tuple[str, str]] = field(default_factory=dict)
    scenario: str = ""

    async def call(self, tool: str, **args) -> tuple[bool, str]:
        t = time.monotonic()
        try:
            res = await self.session.call_tool(tool, args)
            text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            ok = not (res.isError or text.startswith("error:"))
        except Exception as e:
            ok, text = False, f"{type(e).__name__}: {e}"
        self.steps.append(Step(self.scenario, tool, args, ok, text, time.monotonic() - t))
        return ok, text

    async def ref(self, tool: str = "ui_find", pick: str | None = None, **args) -> str | None:
        """The first ref in a ui_find / ui_tree result (on a line containing ``pick``)."""
        ok, text = await self.call(tool, **args)
        if not ok:
            return None
        for line in text.splitlines():
            if pick is None or pick.lower() in line.lower():
                m = REF.search(line)
                if m:
                    return m.group(1)
        return None

    def verdict(self, status: str, note: str) -> None:
        self.verdicts[self.scenario] = (status, note)


def launch(*cmd: str) -> None:
    subprocess.Popen(list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start(target: str) -> None:
    launch("cmd", "/c", "start", "", target)


async def wait_window(run: Run, title: str, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        res = await run.session.call_tool("ui_tree", {"title": title, "depth": 0})
        text = "".join(getattr(c, "text", "") for c in res.content)
        if not (res.isError or text.startswith("error:")):
            return True
        await asyncio.sleep(0.5)
    return False


# ── Scenarios ───────────────────────────────────────────────────────


async def survey(run: Run) -> None:
    """Outline size and walk time for the apps the scenarios use."""
    start("calculator:")
    launch("notepad.exe")
    start("ms-settings:colors")
    launch("explorer.exe", str(Path.home()))
    await asyncio.sleep(4)
    for title in ("Calculator", "Notepad", "Settings", Path.home().name):
        ok, text = await run.call("ui_tree", title=title)
        run.steps[-1].text = f"({len(text)} chars, {len(REF.findall(text))} refs)\n" + text.splitlines()[0]
    run.verdict("info", "outline sizes recorded")


async def calc(run: Run) -> None:
    start("calculator:")
    if not await wait_window(run, "Calculator"):
        return run.verdict("fail", "Calculator window did not appear")
    clear = await run.ref(query="Clear", role="button", title="Calculator")
    if clear:
        await run.call("ui_press", ref=clear)
    for key in ("One", "Two", "Plus", "Three", "Equals"):
        ref = await run.ref(name=key, role="button", title="Calculator")
        if ref is None:
            return run.verdict("fail", f"button {key!r} not found")
        ok, _ = await run.call("ui_press", ref=ref)
        if not ok:
            return run.verdict("fail", f"pressing {key!r} failed")
    ok, text = await run.call("ui_find", id="CalculatorResults", title="Calculator")
    run.verdict("pass" if "15" in text else "fail", "display reads 15" if "15" in text else "no 15 in display")


async def notepad(run: Run) -> None:
    launch("notepad.exe")
    if not await wait_window(run, "Notepad"):
        return run.verdict("fail", "Notepad window did not appear")
    ref = await run.ref(role="text_area,text_field", actionable=True, title="Notepad")
    if ref is None:
        return run.verdict("fail", "no editable text element")
    ok, text = await run.call("ui_set_value", ref=ref, value="hello from ui_set_value")
    run.verdict("pass" if ok and "(verified)" in text else "fail", text.splitlines()[0] if text else "")


async def menu(run: Run) -> None:
    launch("notepad.exe")
    if not await wait_window(run, "Notepad"):
        return run.verdict("fail", "Notepad window did not appear")
    file_ref = await run.ref(name="File", title="Notepad", include_menubar=True)
    if file_ref is None:
        return run.verdict("fail", "File menu not found")
    ok, _ = await run.call("ui_expand", ref=file_ref)
    await asyncio.sleep(0.5)
    save_as = await run.ref(query="Save as", role="menu_item", title="Notepad", include_menubar=True)
    if save_as is None:
        return run.verdict("fail", "Save as item not found after expanding File")
    await run.call("ui_press", ref=save_as)
    if not await wait_window(run, "Save as"):
        return run.verdict("fail", "Save as dialog did not appear")
    save = await run.ref(name="Save", role="button", title="Save as")
    cancel = await run.ref(name="Cancel", role="button", title="Save as")
    if save is None or cancel is None:
        return run.verdict("fail", "Save / Cancel not found in the dialog")
    run.verdict("pass", "found Save in the dialog")

    # Scenario 6 reuses the dialog: close it, then act on the old Save ref.
    run.scenario = "stale"
    await run.call("ui_press", ref=cancel)
    await asyncio.sleep(1)
    ok, text = await run.call("ui_press", ref=save)
    run.verdict("pass" if "stale_ref" in text else "fail", text.splitlines()[0] if text else "")


async def settings(run: Run) -> None:
    start("ms-settings:colors")
    if not await wait_window(run, "Settings"):
        return run.verdict("fail", "Settings window did not appear")
    await asyncio.sleep(2)
    ref = await run.ref(query="Transparency effects", title="Settings", actionable=True)
    if ref is None:
        return run.verdict("fail", "Transparency effects toggle not found")
    ok1, t1 = await run.call("ui_toggle", ref=ref)
    ok2, t2 = await run.call("ui_toggle", ref=ref)
    # Leave transparency on (the host's setting before the first run).
    await run.call("ui_toggle", ref=ref, on=True)
    good = ok1 and ok2 and "(verified)" in t1 and "(verified)" in t2
    bad = t1 if not (ok1 and "(verified)" in t1) else t2
    run.verdict("pass" if good else "fail", "toggled and back, verified" if good else bad.splitlines()[0])


async def explorer(run: Run) -> None:
    home = Path.home()
    launch("explorer.exe", str(home))
    if not await wait_window(run, home.name):
        return run.verdict("fail", "Explorer window did not appear")
    await asyncio.sleep(1)
    ref = await run.ref(query="Documents", role="tree_item", title=home.name)
    if ref is None:
        return run.verdict("fail", "Documents not found in the navigation pane")
    ok, text = await run.call("ui_select", ref=ref)
    run.verdict("pass" if ok and "(verified)" in text else "fail", text.splitlines()[0] if text else "")


DOOM = Path.home() / "doom"


async def blind(run: Run) -> None:
    """A game draws everything itself: the window should offer nothing to act
    on, and pressing it should say so with a click_at hint.

    Uses GZDoom + Freedoom from ~/doom (both free), else a running window
    named by UI_E2E_BLIND_TITLE (Half-Life 2, CS2).
    """
    title = os.environ.get("UI_E2E_BLIND_TITLE", "")
    game = None
    exe = next(DOOM.glob("gzdoom/gzdoom.exe"), None)
    iwad = next(DOOM.glob("freedoom*/freedoom1.wad"), None)
    if not title and exe and iwad:
        game = subprocess.Popen([str(exe), "-iwad", str(iwad), "-width", "800", "-height", "600",
                                 "+vid_fullscreen", "0", "+queryiwad", "0"], cwd=exe.parent)
        title = "gzdoom"
        if not await wait_window(run, title, seconds=20):
            game.kill()
            return run.verdict("fail", "GZDoom window did not appear")
        await asyncio.sleep(3)  # let the title screen draw
    if not title:
        return run.verdict("skip", "no ~/doom (GZDoom + Freedoom) and no UI_E2E_BLIND_TITLE")
    try:
        ok, tree_text = await run.call("ui_tree", title=title)
        ok2, found = await run.call("ui_find", actionable=True, title=title)
        # The window itself and its title bar (drawn by Windows, not the game) don't count.
        chrome = re.compile(r'\d+\. (window|dialog) |\d+\. button "(Minimize|Maximize|Restore|Close)"')
        inner = [line for line in found.splitlines() if REF.search(line) and not chrome.match(line)]
        window = await run.ref(pick="window", tool="ui_tree", title=title, depth=0)
        ok3, pressed = await run.call("ui_press", ref=window) if window else (False, "no window ref")
        blind_ok = ok and not inner and "unsupported_action" in pressed and "click_at" in pressed
        note = (f"{len(inner)} actionable elements inside; press: "
                f"{pressed.splitlines()[0] if pressed else ''}")
        run.verdict("pass" if blind_ok else "fail", note)
    finally:
        if game is not None:
            game.kill()


WINDOWS_SCENARIOS = {
    "survey": survey, "calc": calc, "notepad": notepad, "menu": menu,
    "settings": settings, "explorer": explorer, "blind": blind,
}


# ── Linux (AT-SPI) ──────────────────────────────────────────────────

def _desktop() -> str:
    current = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    for name in ("kde", "xfce"):
        if name in current:
            return name
    return "gnome"


DESKTOP = os.environ.get("UI_E2E_DESKTOP") or _desktop()
SCRATCH = Path("/tmp/ui_e2e_files")
_spawned: list[subprocess.Popen] = []


def spawn(*cmd: str, env: dict | None = None, keep: bool = False) -> subprocess.Popen:
    """Start an app in its own session. ``keep``: kill it when the run ends."""
    proc = subprocess.Popen(list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, env=env)
    if keep:
        _spawned.append(proc)
    return proc


def atspi_windows(pid: int | None = None) -> set[str]:
    """Titles of every AT-SPI window (of one process), to spot the one a launch opens."""
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    titles = set()
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None or (pid is not None and app.get_process_id() != pid):
            continue
        for j in range(app.get_child_count()):
            win = app.get_child_at_index(j)
            if win is not None and win.get_name():
                titles.add(win.get_name().strip())
    return titles


async def new_window(before: set[str], seconds: float = 15.0) -> str | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        fresh = sorted(atspi_windows() - before)
        if fresh:
            return fresh[0]
        await asyncio.sleep(0.5)
    return None


async def find_line(run: Run, **args) -> tuple[str | None, str]:
    """The first ranked ui_find result: ``(ref, line)``."""
    ok, text = await run.call("ui_find", **args)
    for line in text.splitlines() if ok else ():
        if re.match(r"\s*1\. ", line) and (m := REF.search(line)):
            return m.group(1), line
    return None, text.splitlines()[0] if text else ""


def cmd_output(*args: str) -> str:
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def close_window(title: str) -> None:
    """Close a window by title (X11 only); used to clean up after a failure."""
    if shutil.which("wmctrl"):
        subprocess.run(["wmctrl", "-c", title], capture_output=True)


def verb_tool(line: str) -> str:
    """The tool that activates an element, from the verbs on its line."""
    for verb in ("expand", "press", "toggle", "select"):
        if f" {verb}" in line:
            return f"ui_{verb}"
    return "ui_press"


async def linux_survey(run: Run) -> None:
    for title in APPS[DESKTOP]["survey"]:
        ok, text = await run.call("ui_tree", title=title)
        head = text.splitlines()[0] if text else ""
        run.steps[-1].text = f"({len(text)} chars, {len(REF.findall(text))} refs)\n{head}"
    run.verdict("info", "outline sizes recorded")


async def linux_calc(run: Run) -> None:
    app = APPS[DESKTOP]
    if app["calc"] is None:
        return run.verdict("skip", f"no calculator on {DESKTOP}")
    spawn(app["calc"], keep=True)
    title = app.get("calc_title", "Calculator")
    if not await wait_window(run, title):
        return run.verdict("fail", f"{title} window did not appear")
    # KCalc names its keys in words ("Seven", "Add"), GNOME Calculator by glyph.
    keys = app.get("calc_keys") or ("C", "1", "2", "+", "3", "=")
    for key in keys:
        ref = await run.ref(name=key, role="button", match="exact", title=title)
        if ref is None:
            return run.verdict("fail", f"button {key!r} not found")
        ok, _ = await run.call("ui_press", ref=ref)
        if not ok:
            return run.verdict("fail", f"pressing {key!r} failed")
    # GNOME Calculator updates its display asynchronously after "=".
    deadline = time.monotonic() + 3
    while True:
        ok, text = await run.call("ui_find", role="text_field,text_area", title=title)
        if '"15"' in text or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.3)
    run.verdict("pass" if '"15"' in text else "fail", "display reads 15" if '"15"' in text else "no 15 in display")


async def editor_window(run: Run) -> str | None:
    """An editor window on a scratch file, opened once per run, found by the
    process it was launched as.

    GNOME Text Editor restores the user's open documents as tabs even with
    --standalone --ignore-session, and the window title follows the active
    tab, so neither "the new window" nor the scratch file's name finds it.
    Acting scenarios pick the scratch file's text area by its tab.
    """
    # Re-read the title every time: an editor that marks unsaved changes in its
    # title (KWrite appends " *") invalidates a cached one after the first edit.
    if getattr(run, "editor_pid", None):
        titles = sorted(atspi_windows(run.editor_pid))
        if titles:
            run.editor = titles[0]
        return getattr(run, "editor", None)
    SCRATCH.mkdir(exist_ok=True)
    scratch = SCRATCH / "scratch.txt"
    scratch.write_text("")
    # Fresh scratch XDG homes: no restored session (the scratch file is the
    # only tab) and no crash-recovery question left by an earlier killed run.
    env = dict(os.environ)
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        home = SCRATCH / var.lower()
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir()
        env[var] = str(home)
    proc = spawn(*APPS[DESKTOP]["editor"], str(scratch), env=env, keep=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        titles = sorted(atspi_windows(proc.pid))
        if titles:
            run.editor_pid = proc.pid
            run.editor = titles[0]
            return run.editor
        await asyncio.sleep(0.5)
    return None


async def linux_editor(run: Run) -> None:
    title = await editor_window(run)
    if title is None:
        return run.verdict("fail", "no new editor window")
    # Tabbed editors keep every open document's text view in the tree; act
    # only on the scratch file's (its path names the tab).
    ok, text = await run.call("ui_find", role="text_area", actionable=True, title=title)
    lines = [line for line in text.splitlines() if REF.search(line)]
    ref = next((REF.search(line).group(1) for line in lines if "scratch.txt" in line), None)
    if ref is None and len(lines) == 1:
        ref = REF.search(lines[0]).group(1)
    if ref is None:
        return run.verdict("fail", f"no text area known to be scratch.txt's in {title!r}")
    ok, text = await run.call("ui_set_value", ref=ref, value="hello from ui_set_value")
    run.verdict("pass" if ok and "(verified)" in text else "fail", text.splitlines()[0] if text else "")


async def linux_menu(run: Run) -> None:
    title = await editor_window(run)
    if title is None:
        return run.verdict("fail", "no new editor window")
    # GTK 3 apps have a menu bar; GTK 4 apps a primary-menu button.
    # GTK exposes a menu bar entry as a menu; Qt as a menu_item holding a menu.
    ref, line = await find_line(run, name="File", role="menu,menu_item", title=title,
                                include_menubar=True)
    if ref is None:
        ref, line = await find_line(run, query="Main menu", actionable=True, title=title)
    if ref is None:
        return run.verdict("fail", "no File menu or main menu button")
    opener = verb_tool(line)
    await run.call(opener, ref=ref)
    await asyncio.sleep(0.8)
    item = None
    for where in ({"title": title, "include_menubar": True}, {}):
        item, found = await find_line(run, query="Save As", role="menu_item,button", **where)
        # Only a clear "Save As" item: a weak match ("Save…") is another action.
        score = re.search(r"score (\d\.\d+)", found)
        if item and (not score or float(score.group(1)) < 0.9 or "ambiguous" in found):
            item = None
        if item:
            break
    by_keyboard = item is None
    if item is not None:
        await run.call("ui_press", ref=item)
    else:
        # GTK 4.14 popover menu items expose no name or text, so the item
        # can't be found. Close the popover and open the dialog by keyboard to
        # still test the dialog read and the stale ref.
        await run.call(opener, ref=ref, **({"on": False} if opener == "ui_toggle" else {}))
        window = await run.ref(tool="ui_tree", pick="window", title=title, depth=0)
        focused, focus_text = (await run.call("ui_focus", ref=window)) if window else (False, "no window ref")
        if not focused:
            # Keys would go to whatever window has focus (the terminal running this).
            return run.verdict("blocked", "menu items are unnamed (GTK 4 popover) and the window can't be "
                                          f"raised for a keyboard fallback: {focus_text.splitlines()[0]}")
        await run.call("hotkey", keys=["ctrl", "shift", "s"])
    # GTK titles the dialog after the action; KDE's file dialog says "Save File".
    # Without a portal GTK 4 puts up its own chooser, titled "Save a File" —
    # set UI_E2E_SAVE_DIALOG for a host whose title differs from its desktop's.
    dialog = os.environ.get("UI_E2E_SAVE_DIALOG") or APPS[DESKTOP].get("save_dialog", "Save As")
    if not await wait_window(run, dialog):
        # A GTK 3 app answers nothing over AT-SPI while its modal dialog is up,
        # so the dialog can be on screen and unreadable. X11 knows better.
        on_screen = dialog.lower() in cmd_output("wmctrl", "-l").lower()
        close_window(dialog)
        if on_screen:
            return run.verdict("blocked", "the dialog is on screen but its app stops answering "
                                          "AT-SPI while a modal dialog is open (GTK 3)")
        return run.verdict("fail", f"Save As dialog did not appear; windows: {sorted(atspi_windows())}")
    save = await run.ref(name="Save", role="button", match="exact", title=dialog)
    cancel = await run.ref(name="Cancel", role="button", match="exact", title=dialog)
    if save is None or cancel is None:
        return run.verdict("fail", "Save / Cancel not found in the dialog")
    if by_keyboard:
        run.verdict("partial", "menu items are unnamed (GTK 4 popover); dialog opened with "
                               "Ctrl+Shift+S, Save found in it")
    else:
        run.verdict("pass", "found Save in the dialog")

    run.scenario = "stale"
    await run.call("ui_press", ref=cancel)
    await asyncio.sleep(1)
    ok, text = await run.call("ui_press", ref=save)
    run.verdict("pass" if "stale_ref" in text else "fail", text.splitlines()[0] if text else "")


async def linux_settings(run: Run) -> None:
    app = APPS[DESKTOP]
    env = dict(os.environ)
    env.setdefault("XDG_CURRENT_DESKTOP", "GNOME")
    spawn(*app["settings"], env=env)
    title = app["settings_title"]
    # A settings module's first launch after boot is slow (KDE's QML module
    # took >15 s cold and 1.4 s warm); don't call that a failure.
    if not await wait_window(run, title, seconds=40):
        return run.verdict("fail", f"{title} window did not appear")
    await asyncio.sleep(2)
    # Never a mixed checkbox: toggling it can't be put back to mixed.
    ref, line = await find_line(run, role="checkbox", actionable=True, states=["!mixed"],
                                title=title, **({"query": app["setting"]} if app["setting"] else {}))
    if ref is None:
        return run.verdict("fail", "no toggleable setting found")
    was_on = " checked" in line
    ok1, t1 = await run.call("ui_toggle", ref=ref)
    await asyncio.sleep(0.5)
    ok2, t2 = await run.call("ui_toggle", ref=ref)
    await run.call("ui_toggle", ref=ref, on=was_on)
    good = ok1 and ok2 and "(verified)" in t1 and "(verified)" in t2
    bad = t1 if not (ok1 and "(verified)" in t1) else t2
    run.verdict("pass" if good else "fail", "toggled and back, verified" if good else bad.splitlines()[0])


async def linux_files(run: Run) -> None:
    app = APPS[DESKTOP]
    SCRATCH.mkdir(exist_ok=True)
    spawn(*app["files"], str(SCRATCH))
    title = SCRATCH.name
    if not await wait_window(run, title, seconds=15):
        return run.verdict("fail", "file manager window did not appear")
    await asyncio.sleep(1)
    place = app["sidebar"]
    ref, line = await find_line(run, query=place, role="list_item,tree_item,row,cell", title=title)
    if ref is None:
        return run.verdict("fail", f"{place} not found in the sidebar")
    ok, text = await run.call("ui_select", ref=ref)
    run.verdict("pass" if ok and "(verified)" in text else "fail", text.splitlines()[0] if text else "")
    # Close the scratch window; selecting a place renamed it.
    close_window(place)
    close_window(SCRATCH.name)


async def linux_blind(run: Run) -> None:
    """A window that draws itself: a game or GL demo either has an empty tree
    or isn't registered with AT-SPI at all (GLX/SDL apps aren't)."""
    title = os.environ.get("UI_E2E_BLIND_TITLE", "")
    proc = None
    if not title:
        for cmd, name in (("glxgears", "glxgears"), ("xeyes", "xeyes")):
            if shutil.which(cmd):
                proc, title = spawn(cmd), name
                await asyncio.sleep(2)
                break
    if not title:
        return run.verdict("skip", "no UI_E2E_BLIND_TITLE and no glxgears / xeyes")
    try:
        ok, text = await run.call("ui_find", actionable=True, title=title)
        if not ok and "no app or window matches" in text:
            return run.verdict("pass", f"{title} is not in the accessibility tree; visual tools only")
        chrome = re.compile(r'\d+\. (window|dialog) |\d+\. button "(Minimize|Maximize|Close)"')
        inner = [line for line in text.splitlines() if REF.search(line) and not chrome.match(line)]
        run.verdict("pass" if not inner else "fail", f"{len(inner)} actionable elements inside")
    finally:
        if proc is not None:
            proc.kill()


APPS = {
    "gnome": {
        "survey": ("Calculator", "Settings", SCRATCH.name),
        "calc": "gnome-calculator",
        "editor": ("gnome-text-editor", "--standalone", "--ignore-session", "--new-window"),
        "settings": ("gnome-control-center", "multitasking"),
        "settings_title": "Settings",
        "setting": "Hot Corner",
        "files": ("nautilus", "--new-window"),
        "sidebar": "Documents",
    },
    "kde": {
        "survey": ("KCalc", "General Behavior", SCRATCH.name),
        "calc": "kcalc",
        "calc_title": "KCalc",
        "calc_keys": ("All clear", "One", "Two", "Add", "Three", "Equals"),
        "editor": ("kwrite",),
        # systemsettings itself crashes under llvmpipe in a VM; the standalone
        # module host is the same QML module in its own window.
        "settings": ("kcmshell6", "kcm_workspace"),
        "save_dialog": "Save File",
        "settings_title": "General Behavior",
        "setting": "tooltips",
        "files": ("dolphin",),
        "sidebar": "Documents",
    },
    "xfce": {
        # Not SCRATCH.name: it's a substring of the editor's window title.
        "survey": ("Mousepad", "Appearance", "Thunar"),
        "calc": None,
        "editor": ("mousepad", "--disable-server"),
        "settings": ("xfce4-appearance-settings",),
        "settings_title": "Appearance",
        "setting": None,
        "files": ("thunar",),
        "sidebar": "Desktop",
    },
}

LINUX_SCENARIOS = {
    "survey": linux_survey, "calc": linux_calc, "editor": linux_editor, "menu": linux_menu,
    "settings": linux_settings, "files": linux_files, "blind": linux_blind,
}

# FreeBSD runs the same X11 desktops through the same backend as Linux.
SCENARIOS = (LINUX_SCENARIOS if sys.platform.startswith(("linux", "freebsd"))
             else WINDOWS_SCENARIOS)


# ── Report ──────────────────────────────────────────────────────────


def report(run: Run, path: Path) -> None:
    lines = ["# ui_* E2E", "", f"{time.strftime('%Y-%m-%d %H:%M')} · {sys.platform}", "",
             "| Scenario | Verdict | Note |", "|---|---|---|"]
    for name, (status, note) in run.verdicts.items():
        lines.append(f"| {name} | {status} | {note.replace('|', '/')} |")
    lines.append("")
    for s in run.steps:
        args = ", ".join(f"{k}={v!r}" for k, v in s.args.items())
        lines += [f"## [{s.scenario}] {s.tool}({args}) — {'ok' if s.ok else 'ERROR'} {s.seconds:.2f}s",
                  "", "```", s.text[:4000], "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


class DirectSession:
    """The ``ui_*`` tools on an in-process executor, for a host whose server
    won't start (a headless Xvfb whose captures are black fails the startup
    screenshot check). Same executor code, without the MCP layer or deltas."""

    _VERBS = {"ui_press": "press", "ui_set_value": "set_value", "ui_toggle": "toggle",
              "ui_select": "select", "ui_expand": "expand", "ui_focus": "focus",
              "ui_scroll_to": "scroll_to"}

    def __init__(self) -> None:
        from conductor_desktop.core.executor import create_executor
        from conductor_desktop.core.models.host_node import HostNode

        self.executor = create_executor(HostNode(label="ui-e2e", host_type="resident"))

    async def initialize(self) -> None:
        await self.executor.connect()

    async def call_tool(self, tool: str, args: dict):
        from types import SimpleNamespace

        ex = self.executor
        args = dict(args)
        if tool in ("ui_tree", "ui_find"):
            result = await getattr(ex, tool)(**args)
        elif tool in self._VERBS:
            result = await ex.ui_act(args.pop("ref"), self._VERBS[tool], args)
        elif tool == "hotkey":
            result = await ex.hotkey(args["keys"])
        else:
            raise ValueError(f"--direct doesn't provide {tool}")
        text = result.output if result.success else f"error: {result.error}"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text or "")],
                               isError=not result.success)


async def run_scenarios(session, names: list[str], report_path: Path) -> None:
    run = Run(session)
    for name in names:
        run.scenario = name
        print(f"-- {name}", flush=True)
        try:
            await SCENARIOS[name](run)
        except Exception as e:
            run.verdict("error", f"{type(e).__name__}: {e}")
        print(f"   {run.verdicts.get(name, ('?', ''))}", flush=True)
    report(run, report_path)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    ap.add_argument("--report", default=str(Path.home() / "ui_e2e_report.md"))
    ap.add_argument("--direct", action="store_true",
                    help="drive the executor in-process instead of the MCP server")
    opts = ap.parse_args()

    try:
        if opts.direct:
            session = DirectSession()
            await session.initialize()
            await run_scenarios(session, opts.scenarios, Path(opts.report))
        else:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("CONDUCTOR_DESKTOP_GROUNDER_URL", "CONDUCTOR_DESKTOP_DESCRIBE_URL")}
            env.update(CONDUCTOR_DESKTOP_AUTO_SCREENSHOT="false", PYTHONIOENCODING="utf-8")
            params = StdioServerParameters(
                command=sys.executable, args=["-c", "from conductor_desktop.server import run; run()"],
                env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                await run_scenarios(session, opts.scenarios, Path(opts.report))
    finally:
        for proc in _spawned:
            proc.kill()
    print(f"report: {opts.report}")


if __name__ == "__main__":
    asyncio.run(main())
