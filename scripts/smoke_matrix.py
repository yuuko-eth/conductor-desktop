"""Cross-platform smoke test: every executor primitive through the real MCP server.

Pins down what each platform does today, so a backend refactor can be
checked against it: run it before the refactor, run it after, and compare
the two JSON results.

    python scripts/smoke_matrix.py                    # every scenario
    python scripts/smoke_matrix.py screens capture    # some
    python scripts/smoke_matrix.py --observe-only     # no input at all
    python scripts/smoke_matrix.py --compare before.json

Run it in the logged-in desktop session (on Windows, not over SSH: session 0
sees no windows). The acting scenarios open a scratch text editor, type into
it, and select + copy its text; `landing` and `scroll` click and scroll in it
and read back where the caret went. The clipboard is put back afterwards.
Don't use the keyboard or mouse while they run.

The server starts with no grounder, with deltas on, and with its own lock
file, so it can act even while another conductor-desktop holds the desktop.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Typed into the editor. type_text pastes text of 8+ characters through the
# clipboard and types shorter text key by key, so each pair covers both
# paths: a leading "-" (a typer that reads it as a command-line option) and
# non-ASCII including a character outside the Basic Multilingual Plane.
# The short non-ASCII line starts with a digit so an editor's automatic
# capitalization can't change it.
LINES = ["-conductor smoke 1", "ünïcødé 測試 🎉", "-a b", "1 é🎉"]
CLIP_TEXT = "conductor clipboard ✓ 測試 🎉"
A11Y_TEXT = "conductor a11y probe"
MOD = "cmd" if sys.platform == "darwin" else "ctrl"
STEP_TIMEOUT = 60.0


@dataclass
class Step:
    scenario: str
    tool: str
    args: dict
    ok: bool
    text: str
    images: int
    seconds: float


@dataclass
class Run:
    session: ClientSession
    steps: list[Step] = field(default_factory=list)
    verdicts: dict[str, dict] = field(default_factory=dict)
    scenario: str = ""
    editor: dict | None = None      # {"title", "pid", "bounds"} once opened
    facts: dict = field(default_factory=dict)

    async def call(self, tool: str, **args) -> tuple[bool, str, list]:
        t = time.monotonic()
        print(f"   {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})", flush=True)
        try:
            # A hung step fails with its name instead of freezing the run.
            res = await asyncio.wait_for(self.session.call_tool(tool, args), STEP_TIMEOUT)
            text = "\n".join(c.text for c in res.content if c.type == "text")
            images = [c for c in res.content if c.type == "image"]
            ok = not res.isError
        except TimeoutError:
            ok, text, images = False, f"timed out after {STEP_TIMEOUT:.0f}s", []
        except Exception as e:
            ok, text, images = False, f"{type(e).__name__}: {e}", []
        self.steps.append(Step(self.scenario, tool, args, ok, text, len(images),
                               round(time.monotonic() - t, 3)))
        return ok, text, images

    def verdict(self, status: str, note: str = "", **facts) -> None:
        self.verdicts[self.scenario] = {"status": status, "note": note, **facts}


def first_json(text: str):
    """The JSON value at the start of a reply (tools may append a deltas block)."""
    return json.JSONDecoder().raw_decode(text.lstrip())[0]


def deltas_of(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("[deltas"):
            return line
    return None


# ── Observing scenarios ─────────────────────────────────────────────


async def surface(run: Run) -> None:
    tools = (await run.session.list_tools()).tools
    names = sorted(t.name for t in tools)
    read_only = sorted(t.name for t in tools if t.annotations and t.annotations.readOnlyHint)
    run.verdict("pass" if names else "fail", f"{len(names)} tools, {len(read_only)} read-only",
                tools=names)


async def diagnose(run: Run) -> None:
    ok, text, _ = await run.call("diagnose")
    lines = {k.strip(): v.strip() for k, v in
             (line.split(":", 1) for line in text.splitlines() if ":" in line)}
    perms = {k: lines.get(k, "?").split(" ")[0] for k in ("screenshot", "input", "semantic backend")}
    good = ok and perms["screenshot"] == "OK" and perms["input"] == "OK"
    run.verdict("pass" if good else "fail", ", ".join(f"{k}={v}" for k, v in perms.items()),
                backend=lines.get("backend"), permissions=perms)


def _unsupported(text: str) -> bool:
    """Whether a failed reply says the backend doesn't offer the tool at all
    (KDE Wayland has no window listing), as opposed to it breaking."""
    return "not available on this host" in text


async def screens(run: Run) -> None:
    ok, text, _ = await run.call("list_screens")
    if not ok:
        return run.verdict("skip" if _unsupported(text) else "fail", text[:200])
    data = first_json(text)
    primary = [s for s in data if s.get("primary")]
    shape = all(len(s.get("bounds", [])) == 4 for s in data)
    run.facts["screen"] = (primary or data)[0]["bounds"] if data else None
    run.facts["screens"] = data
    run.verdict("pass" if data and primary and shape else "fail",
                f"{len(data)} screen(s), primary={bool(primary)}",
                screens=[{k: s.get(k) for k in ("name", "bounds", "scale", "primary")} for s in data])


def _field_coverage(windows: list[dict]) -> dict:
    """Which window fields this platform fills in with something real."""
    cover = {}
    for key in ("title", "pid", "process_name", "bounds", "z", "focused", "monitor",
                "layer", "state"):
        vals = [w.get(key) for w in windows]
        cover[key] = sum(v not in (None, "", [], [0, 0, 0, 0], 0) for v in vals)
    return cover


async def windows(run: Run) -> None:
    ok, text, _ = await run.call("list_windows", page_size=0)
    if not ok:
        return run.verdict("skip" if _unsupported(text) else "fail", text[:200])
    env = first_json(text)
    wins = env.get("windows", [])
    if not wins:
        return run.verdict("skip", "no windows open (the editor opens later)")
    focused = [w.get("title") for w in wins if w.get("focused")]
    run.verdict("pass", f"{len(wins)} windows, focused={focused[:1]}",
                count=len(wins), fields=_field_coverage(wins))


async def scene(run: Run) -> None:
    ok, text, _ = await run.call("get_scene_graph")
    if not ok:
        return run.verdict("skip" if _unsupported(text) else "fail", text[:200])
    sg = first_json(text)
    keys = sorted(sg)
    run.verdict("pass" if sg.get("windows") is not None and sg.get("screens") else "fail",
                f"{len(sg.get('screens') or {})} screens, {len(sg.get('spaces') or {})} spaces, "
                f"{len(sg.get('windows') or {})} windows", keys=keys)


def _png_stats(img) -> dict:
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(base64.b64decode(img.data))) as im:
        stat = ImageStat.Stat(im.convert("L"))
        return {"size": list(im.size), "stddev": round(stat.stddev[0], 1),
                "mean": round(stat.mean[0], 1)}


async def capture(run: Run) -> None:
    results = {}
    for label, args in (("desktop", {}), ("screen0", {"screen": 0}),
                        ("primary", {"screen": "primary"})):
        ok, text, images = await run.call("capture_screen", full_res=False, **args)
        results[label] = _png_stats(images[0]) if ok and images else {"error": text[:120]}
    # A failed capture is flat black (a display that's off, a blanked NVIDIA
    # session); a flat dark-grey empty desktop (Hyprland without a
    # wallpaper) is a real frame.
    blank = [k for k, v in results.items()
             if "error" not in v and v["stddev"] < 1 and v["mean"] < 5]
    # Per-screen capture needs the screen list, which some backends don't have.
    unsupported = [k for k, v in results.items() if _unsupported(v.get("error", ""))]
    errors = [k for k, v in results.items() if "error" in v and k not in unsupported]
    status = "pass" if not blank and not errors and len(unsupported) < len(results) else "fail"
    note = ("; ".join(f"{k} {v.get('size')} sd={v.get('stddev')}" for k, v in results.items()
                      if "error" not in v) + (f"; errors: {errors}" if errors else "")
            + (f"; no screen listing: {unsupported}" if unsupported else "")
            + (f"; BLANK: {blank}" if blank else ""))
    run.verdict(status, note, captures=results)


async def clipboard(run: Run) -> None:
    ok0, before, _ = await run.call("clipboard_get")
    run.facts["clipboard"] = before if ok0 else None
    ok1, _, _ = await run.call("clipboard_set", text=CLIP_TEXT)
    ok2, after, _ = await run.call("clipboard_get")
    got = after.split("\n[deltas")[0]
    run.verdict("pass" if ok1 and ok2 and got == CLIP_TEXT else "fail",
                "round-trip exact" if got == CLIP_TEXT else f"got {got!r}")


async def shell(run: Run) -> None:
    ok, text, _ = await run.call("exec_shell", command="echo smoke-ok")
    run.verdict("pass" if ok and "smoke-ok" in text else "fail", text.splitlines()[0][:120] if text else "")


async def web(run: Run) -> None:
    ok, text, _ = await run.call("web_list_tabs")
    run.verdict("info", ("reachable: " if ok else "unreachable: ") + text.splitlines()[0][:150])


# ── The scratch editor ──────────────────────────────────────────────


def _editor_command(scratch: Path) -> list[str]:
    """How to open ``scratch`` in a text editor whose window we can close again.

    macOS opens it in the TextEdit that may already be running (cleanup
    closes just this document); a second TextEdit instance would reopen the
    user's autosaved documents. Elsewhere each editor runs as its own
    process, so cleanup can kill exactly the process started here.
    """
    if sys.platform == "darwin":
        return ["open", "-a", "TextEdit", str(scratch)]
    if sys.platform == "win32":
        return ["notepad.exe", str(scratch)]
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    candidates = [
        ("kde" in desktop, ["kwrite"]),
        # -n: a new instance, not a tab in a kate that's already open; -b:
        # stay in the foreground, since kate forks and leaves the process
        # started here (the one cleanup kills) at once.
        ("kde" in desktop, ["kate", "-n", "-b"]),
        ("xfce" in desktop, ["mousepad", "--disable-server"]),
        # Mint's editor, a gedit fork.
        ("cinnamon" in desktop, ["xed", "--standalone"]),
        (True, ["gnome-text-editor", "--standalone", "--ignore-session"]),
        # Ubuntu 22.04 (GNOME 42): gedit, and no --ignore-session.
        (True, ["gedit", "--standalone"]),
        (True, ["xed", "--standalone"]),
        (True, ["mousepad", "--disable-server"]),
        (True, ["kwrite"]),
        (True, ["kate", "-n", "-b"]),
    ]
    for wanted, cmd in candidates:
        path = shutil.which(cmd[0]) if wanted else None
        # Ubuntu 22.04 points gnome-text-editor at gedit through
        # update-alternatives, and gedit rejects the flags above.
        if path and Path(path).resolve().name == cmd[0]:
            return [*cmd, str(scratch)]
    raise RuntimeError("no known text editor installed")


async def _window_list(run: Run) -> list[dict]:
    ok, text, _ = await run.call("list_windows", page_size=0)
    return first_json(text).get("windows", []) if ok else []


async def open_editor(run: Run) -> None:
    # The editor is found, focused and checked through list_windows.
    ok, text, _ = await run.call("list_windows", page_size=0)
    if not ok and _unsupported(text):
        return run.verdict("skip", f"no window listing here: {text[:200]}")
    # A unique file name, so the editor window is the one whose title has it.
    scratch = Path(tempfile.mkdtemp()) / f"conductor-smoke-{os.getpid()}.txt"
    scratch.write_text("", encoding="utf-8")
    run.facts["editor_file"] = scratch
    cmd = _editor_command(scratch)
    run.facts["editor_proc"] = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and run.editor is None:
        await asyncio.sleep(1)
        mine = [w for w in await _window_list(run) if scratch.stem in (w.get("title") or "")]
        run.editor = mine[0] if mine else None
    if run.editor is None:
        return run.verdict("fail", f"no window titled {scratch.stem!r} after {' '.join(cmd)}")
    await asyncio.sleep(1.5)  # let it finish drawing and take focus
    run.verdict("pass", f"{cmd[0]} → {run.editor.get('title')!r}",
                window={k: run.editor.get(k) for k in ("title", "process_name", "bounds")})


async def focus(run: Run) -> None:
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    ok, text, _ = await run.call("focus_window", title=run.editor["title"])
    await asyncio.sleep(0.5)
    wins = await _window_list(run)
    focused = [w.get("title") for w in wins if w.get("focused")]
    good = ok and run.editor["title"] in focused
    run.verdict("pass" if good else "fail", f"focused={focused[:1]}", reply=text.splitlines()[0])


async def _editor_title(run: Run) -> str:
    """The scratch editor's title *now*, not the one it opened with.

    An editor rewrites its title when the document becomes modified — Kate
    inserts a "*", Notepad a leading "*" — so the title captured when the
    window opened stops matching once a scenario has typed into it. The file
    stem is in the title either way, so re-resolve through it. (Targeting a
    window by id instead of by title is a known gap.)
    """
    stem = run.facts["editor_file"].stem
    live = [w for w in await _window_list(run) if stem in (w.get("title") or "")]
    return live[0]["title"] if live else run.editor["title"]


async def _focus_editor(run: Run) -> bool:
    """Focus the scratch editor and confirm it holds focus.

    Every scenario that sends input calls this first: select-all + delete in
    the wrong window would wipe someone's document.
    """
    await run.call("focus_window", title=await _editor_title(run))
    await asyncio.sleep(0.5)
    focused = [w for w in await _window_list(run) if w.get("focused")]
    return bool(focused) and run.facts["editor_file"].stem in (focused[0].get("title") or "")


def _center(bounds) -> tuple[int, int] | None:
    if not bounds or len(bounds) != 4 or not bounds[2]:
        return None
    x, y, w, h = bounds
    return x + w // 2, y + h // 2


async def pointer(run: Run) -> None:
    """Pointer primitives inside the editor window, where they can't hit anything else."""
    center = _center(run.editor.get("bounds") if run.editor else None)
    if center is None:
        return run.verdict("skip", "no editor bounds on this platform")
    if not await _focus_editor(run):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    x, y = center
    results = {}
    for tool, args in (("mouse_move_at", {"x": x, "y": y}),
                       ("click_at", {"x": x, "y": y}),
                       ("drag_at", {"x1": x - 40, "y1": y, "x2": x + 40, "y2": y})):
        ok, text, _ = await run.call(tool, **args)
        results[tool] = ok
    run.verdict("pass" if all(results.values()) else "fail",
                ", ".join(f"{k}={'ok' if v else 'ERR'}" for k, v in results.items()))


async def typing(run: Run) -> None:
    """type_text + key + hotkey, read back through select-all, copy, clipboard_get."""
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    if not await _focus_editor(run):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    await run.call("hotkey", keys=[MOD, "a"])
    await run.call("key", key="delete")
    for i, line in enumerate(LINES):
        if i:
            await run.call("key", key="enter")
        await run.call("type_text", text=line)
    await asyncio.sleep(0.5)
    got = (await _read_editor(run)).rstrip("\n")
    want = "\n".join(LINES)
    if got == want:
        return run.verdict("pass", "typed text read back exactly")
    diff = [f"{w!r} → {g!r}" for w, g in zip(want.split("\n"), got.split("\n")) if w != g]
    run.verdict("fail", "; ".join(diff) or f"got {got!r}", got=got)


def _run_quiet(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _macos_input_source() -> str:
    """The selected input source's id (com.apple.inputmethod.TCIM.Zhuyin).

    Not HIToolbox's AppleCurrentKeyboardLayoutInputSourceID: that is the
    layout underneath, and stays ABC with Zhuyin selected.
    """
    import ctypes
    import ctypes.util
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                      ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    source = carbon.TISCopyCurrentKeyboardInputSource()
    if not source:
        return ""
    try:
        sid = carbon.TISGetInputSourceProperty(
            source, ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID"))
        buf = ctypes.create_string_buffer(256)
        ok = sid and cf.CFStringGetCString(sid, buf, len(buf), 0x08000100)  # UTF-8
        return buf.value.decode() if ok else ""
    finally:
        cf.CFRelease(source)


def _input_method() -> dict:
    """The keyboard layout or input method the focused window types with,
    as far as this platform tells: a data point, never a verdict."""
    if sys.platform == "win32":
        # The layout only: the IMM open/conversion state reads the same with
        # Zhuyin on as with no IME at all (TSF input methods don't report it).
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetKeyboardLayout.restype = ctypes.c_void_p
        thread = user32.GetWindowThreadProcessId(ctypes.c_void_p(user32.GetForegroundWindow()),
                                                 None)
        return {"hkl": f"{(user32.GetKeyboardLayout(thread) or 0) & 0xFFFFFFFF:08x}"}
    if sys.platform == "darwin":
        return {"source": _macos_input_source()}
    info = {"xkb": _run_quiet(["gsettings", "get", "org.gnome.desktop.input-sources",
                               "sources"]),
            "ibus": _run_quiet(["ibus", "engine"]) if shutil.which("ibus") else "",
            "fcitx5": _run_quiet(["fcitx5-remote", "-n"]) if shutil.which("fcitx5-remote") else "",
            "env": {k: os.environ[k] for k in ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")
                    if k in os.environ}}
    return {k: v for k, v in info.items() if v}


# Key presses, and what they type with no input method in the way. The last
# two end a composition "a" may have opened: space picks a candidate (Zhuyin:
# a character for ㄇ), enter commits it as it stands.
KEY_PRESSES = [("a", [("key", {"key": "a"})], "a"),
               ("1", [("key", {"key": "1"})], "1"),
               ("shift+b", [("hotkey", {"keys": ["shift", "b"]})], "B"),
               ("a space", [("key", {"key": "a"}), ("key", {"key": "space"})], "a "),
               ("a enter", [("key", {"key": "a"}), ("key", {"key": "enter"})], "a\n"),
               # Vietnamese Telex composes across two keys: aa is â, dd is đ.
               ("a a", [("key", {"key": "a"}), ("key", {"key": "a"})], "aa"),
               ("type dd", [("type_text", {"text": "dd"})], "dd"),
               # Layouts: these keys sit elsewhere on QWERTZ, and a backend
               # that sends US key positions types y for z.
               ("type zy", [("type_text", {"text": "zy"})], "zy"),
               # Short text that isn't on a US keyboard at all.
               ("type ян", [("type_text", {"text": "ян"})], "ян"),
               ("z", [("key", {"key": "z"})], "z"),
               # Undo after two pastes leaves the first: by position, QWERTZ's
               # ctrl+z would be ctrl+y, redo, and leave both.
               ("undo", [("clipboard_set", {"text": "one"}), ("hotkey", {"keys": [MOD, "v"]}),
                         ("clipboard_set", {"text": " two"}), ("hotkey", {"keys": [MOD, "v"]}),
                         ("hotkey", {"keys": [MOD, "z"]})], "one")]


async def keys(run: Run) -> None:
    """Bare letter and digit key presses, one at a time, read back.

    type_text sends characters, which input methods let through; a key press
    is a key, which an input method in native mode (Zhuyin, Pinyin, kana)
    takes as the start of a composition. Information for now: what each
    platform and input method does is what this collects.
    """
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    if not await _focus_editor(run):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    method = _input_method()
    got: dict[str, dict[str, str]] = {}
    lost_prefix: list[str] = []
    for label, calls, _want in KEY_PRESSES:
        await run.call("hotkey", keys=[MOD, "a"])
        await run.call("key", key="delete")
        # After a "-", so an editor's automatic capitalization (macOS) can't
        # change a lone letter; pasted, since the "-" key is Zhuyin's ㄦ.
        await run.call("clipboard_set", text=KEY_PREFIX)
        await run.call("hotkey", keys=[MOD, "v"])
        await asyncio.sleep(0.3)
        for tool, args in calls:
            await run.call(tool, **args)
            await asyncio.sleep(0.3)
        # Read before and after escape: a composition still open shows up
        # (or not) before, and escape drops it, so the next press starts clean.
        before = await _read_editor_or_nothing(run)
        await run.call("key", key="escape")
        await asyncio.sleep(0.3)
        after = await _read_editor_or_nothing(run)
        got[label] = {"before_escape": _unprefixed(before), "after_escape": _unprefixed(after)}
        if not after.startswith(KEY_PREFIX):
            lost_prefix.append(label)
    wrong = [label for label, _calls, want in KEY_PRESSES
             if got[label]["after_escape"] != want]
    note = ", ".join(f"{k}→{v['before_escape']!r}/{v['after_escape']!r}" for k, v in got.items())
    lost = f" (the pasted prefix didn't land for {len(lost_prefix)})" if lost_prefix else ""
    run.verdict("info", ("NOT as typed (before/after escape): " if wrong else "as typed: ") + note
                + lost, got=got, input_method=method, lost_prefix=lost_prefix)


async def _read_editor_or_nothing(run: Run) -> str:
    """The editor's text as it stands (a trailing newline counts), or ""
    when there was nothing to copy: copying an empty selection leaves the
    clipboard as it was, so mark it first."""
    await run.call("clipboard_set", text=NOTHING_COPIED)
    text = await _read_editor(run)
    return "" if text == NOTHING_COPIED else text


NOTHING_COPIED = "(conductor smoke: nothing copied)"
KEY_PREFIX = "-"


def _unprefixed(text: str) -> str:
    """What the key presses added after KEY_PREFIX, or the whole text when
    the prefix didn't land (the verdict counts those): a paste right after
    clipboard_set came out empty on Hyprland and Sway."""
    return text.removeprefix(KEY_PREFIX)


async def a11y(run: Run) -> None:
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    # Its own text: what earlier scenarios leave varies (keys ends on "one").
    if not await _load_fixture(run, A11Y_TEXT):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    title = await _editor_title(run)
    results = {}
    ok, text, _ = await run.call("ui_tree", title=title)
    results["ui_tree"] = ok
    results["text_in_tree"] = A11Y_TEXT in text
    ok, text, _ = await run.call("ui_find", role="text_area,text_field,document", title=title)
    results["ui_find"] = ok and "[e" in text
    ok, text, _ = await run.call("ui_tree", title=title, format="json", depth=4)
    results["ui_tree_json"] = ok and first_json(text).get("window") is not None
    ok, text, _ = await run.call("read_grid", title=title)
    results["read_grid"] = first_json(text).get("failure") if ok else "error"
    good = results["ui_tree"] and results["ui_find"] and results["ui_tree_json"]
    run.verdict("pass" if good else "fail",
                ", ".join(f"{k}={v}" for k, v in results.items()), results=results)


# ── Where input lands ───────────────────────────────────────────────
# A tool that returns ok proves nothing about where the click went: Sway and
# Hyprland once passed `pointer` while landing hundreds of pixels off. These
# scenarios check the landing with no grounder and no a11y, so they run
# everywhere typing does: load numbered lines into the editor, click, type a
# short marker at the caret, and read the text back. The line and column the
# marker landed on is where the click went.

FIXTURE_LINES = 200
# 54 characters: short enough not to wrap in a small editor window, long
# enough that a click a fifth of the way across lands inside the text.
FIXTURE = "\n".join(f"{i:03d} " + "abcdefghij" * 5 for i in range(FIXTURE_LINES))
_MARK = re.compile(r"@\d")


async def _editor_window(run: Run) -> dict | None:
    stem = run.facts["editor_file"].stem
    live = [w for w in await _window_list(run) if stem in (w.get("title") or "")]
    return live[0] if live else None


async def _editor_has_focus(run: Run) -> bool:
    focused = [w for w in await _window_list(run) if w.get("focused")]
    return bool(focused) and run.facts["editor_file"].stem in (focused[0].get("title") or "")


async def _read_editor(run: Run) -> str:
    """The editor's whole text, through select-all, copy and clipboard_get."""
    await run.call("hotkey", keys=[MOD, "a"])
    await run.call("hotkey", keys=[MOD, "c"])
    await asyncio.sleep(0.5)
    _ok, text, _ = await run.call("clipboard_get")
    return text.split("\n[deltas")[0].replace("\r\n", "\n")


async def _load_fixture(run: Run, text: str = "") -> bool:
    """Replace the editor's text with ``text`` (FIXTURE by default) and put
    the caret at the top."""
    if not await _focus_editor(run):
        return False
    await run.call("hotkey", keys=[MOD, "a"])
    await run.call("key", key="delete")
    await run.call("clipboard_set", text=text or FIXTURE)
    await run.call("hotkey", keys=[MOD, "v"])
    await asyncio.sleep(1.0)
    await _to_top(run)
    return True


async def _to_top(run: Run) -> None:
    await run.call("hotkey", keys=["cmd", "up"] if sys.platform == "darwin" else ["ctrl", "home"])
    await asyncio.sleep(0.5)


async def _mark_at(run: Run, x: int, y: int, mark: str) -> str | None:
    """Click at (x, y) and type ``mark`` at the caret. None, or why not.

    Focus is checked between the click and the typing: a click that missed
    the editor must not type into whatever it hit instead.
    """
    await run.call("click_at", x=x, y=y)
    await asyncio.sleep(0.4)
    if not await _editor_has_focus(run):
        return f"click at ({x},{y}) took focus away from the editor"
    await run.call("type_text", text=mark)
    await asyncio.sleep(0.2)
    return None


def _find_marks(text: str) -> dict[str, tuple[int, int]]:
    """Each marker's (fixture line, column), columns counted without markers."""
    found: dict[str, tuple[int, int]] = {}
    for line in text.split("\n"):
        plain = _MARK.sub("", line)
        if not plain[:3].isdigit():
            continue
        for m in _MARK.finditer(line):
            found[m.group()] = (int(plain[:3]), len(_MARK.sub("", line[:m.start()])))
    return found


async def _land_clicks(run: Run, win: dict) -> tuple[list[str] | str, str, dict]:
    """Click three spots in the editor and see where the caret went.

    Returns (what failed, a note, details); what failed is a reason string
    when the probing couldn't finish, else the list of failed checks.
    """
    if not await _load_fixture(run):
        return "the scratch editor didn't take focus; no input sent", "", {}
    x, y, w, h = win["bounds"]
    # Clear of the title bar and toolbar at the top and the status bar at the
    # bottom, and inside the 54-character lines horizontally.
    probes = {"@1": (x + w // 5, y + h * 35 // 100),
              "@2": (x + w // 5, y + h * 65 // 100),
              "@3": (x + w * 45 // 100, y + h * 65 // 100)}
    for mark, (px, py) in probes.items():
        if (why := await _mark_at(run, px, py, mark)) is not None:
            return why, "", {"probes": probes}
    marks = _find_marks(await _read_editor(run))
    missing = [m for m in probes if m not in marks]
    if missing:
        return f"markers not in the text: {missing}", "", {"marks": marks}
    (r1, _), (r2, c2), (r3, c3) = marks["@1"], marks["@2"], marks["@3"]
    # 30% of the window's height is at least two lines, and at most one line
    # per 8 px: a smaller line height means the clicks went further than aimed.
    most = h * 30 // 100 // 8
    checks = {
        "lower click lands on a later line": 2 <= r2 - r1 <= most,
        "same height lands on the same line": r3 == r2,
        "further right lands on a later column": c3 > c2,
    }
    note = f"rows {r1}→{r2} (+{r2 - r1}, ≤{most}), cols {c2}→{c3} on row {r3}"
    return [k for k, v in checks.items() if not v], note, {"marks": marks, "probes": probes}


async def landing(run: Run) -> None:
    """Clicks land where they were aimed: two rows apart, then two columns apart."""
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    win = await _editor_window(run)
    if not win or not _center(win.get("bounds")):
        return run.verdict("skip", "no editor bounds on this platform")
    bad, note, details = await _land_clicks(run, win)
    if isinstance(bad, str):
        return run.verdict("fail", bad, **details)
    run.verdict("fail" if bad else "pass", ("; ".join(bad) + " — " if bad else "") + note,
                **details)


def _screen_at(screens: list[dict], point: tuple[int, int]) -> dict | None:
    px, py = point
    for s in screens:
        x, y, w, h = s["bounds"]
        if x <= px < x + w and y <= py < y + h:
            return s
    return None


async def _drag_editor(run: Run, grab: tuple[int, int], drop: tuple[int, int],
                       onto: dict) -> tuple[dict | None, str]:
    """Drag the editor by its title bar from ``grab`` to ``drop``, and wait
    for its centre to be on ``onto``. Returns (the window now, what went wrong)."""
    await run.call("drag_at", x1=grab[0], y1=grab[1], x2=drop[0], y2=drop[1])
    deadline = time.monotonic() + 3
    while True:
        await asyncio.sleep(0.5)
        win = await _editor_window(run)
        center = _center(win.get("bounds")) if win else None
        here = _screen_at(run.facts["screens"], center) if center else None
        if here is not None and here["bounds"] == onto["bounds"]:
            return win, ""
        if time.monotonic() > deadline:
            where = here.get("name") if here else "off every screen"
            return win, f"dragged {grab} → {drop}, but the editor is on {where}, not {onto.get('name')}"


def _wm_move_commands(stem: str, onto: dict, win: dict) -> list[list[str]] | None:
    """The window manager's own commands that put the window titled ``stem``
    on screen ``onto``, or None where there's no scripted move.

    Linux, where a title-bar drag can't be the test: X11 and Sway draw the
    title bar outside the bounds a window reports, and Hyprland has none.
    """
    name = onto.get("name") or ""
    if os.environ.get("SWAYSOCK"):
        return [["swaymsg", f'[title="{stem}"]', "move", "container", "to", "output", name]]
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return [["hyprctl", "dispatch", "focuswindow", f"title:{stem}"],
                ["hyprctl", "dispatch", "movewindow", f"mon:{name}"]]
    if os.environ.get("WAYLAND_DISPLAY") or not os.environ.get("DISPLAY"):
        return None
    # X11: centred on that screen, unmaximised first (a maximised window
    # ignores a move).
    ox, oy, ow, oh = onto["bounds"]
    _x, _y, w, h = win["bounds"]
    x, y = ox + max(0, (ow - w) // 2), oy + max(0, (oh - h) // 2)
    return [["wmctrl", "-r", stem, "-b", "remove,maximized_vert,maximized_horz"],
            ["wmctrl", "-r", stem, "-e", f"0,{x},{y},-1,-1"]]


async def _wm_move_editor(run: Run, onto: dict, win: dict) -> tuple[dict | None, str]:
    """Move the editor onto ``onto`` with the window manager's commands, and
    wait for its centre to be there. Returns (the window now, what went wrong)."""
    cmds = _wm_move_commands(run.facts["editor_file"].stem, onto, win)
    for cmd in cmds or []:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        print(f"   $ {' '.join(cmd)} → {p.returncode}", flush=True)
        if p.returncode != 0:
            return win, f"{cmd[0]} failed: {(p.stderr or p.stdout).strip()[:120]}"
    deadline = time.monotonic() + 3
    while True:
        await asyncio.sleep(0.5)
        now = await _editor_window(run)
        center = _center(now.get("bounds")) if now else None
        here = _screen_at(run.facts["screens"], center) if center else None
        if here is not None and here["bounds"] == onto["bounds"]:
            return now, ""
        if time.monotonic() > deadline:
            where = here.get("name") if here else "off every screen"
            return now, f"moved it to {onto.get('name')}, but the editor is on {where}"


async def monitors(run: Run) -> None:
    """Put the editor on another monitor, check clicks land there too (it
    may scale differently), then put it back.

    Windows and macOS drag it by its title bar; Linux uses the window
    manager's own move (_wm_move_commands says why). Back, because some
    editors (Windows 11 Notepad) reopen where they were last closed.
    """
    screens = run.facts.get("screens") or []
    if len(screens) < 2:
        return run.verdict("skip", f"{len(screens)} screen(s); this needs two")
    # Mirrored outputs (e.g. DP-1 and VGA-1 cloned) share one area: nowhere to drag to.
    if len({tuple(s["bounds"]) for s in screens}) < 2:
        return run.verdict("skip", f"{len(screens)} screens, all mirrored; this needs two areas")
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    win = await _editor_window(run)
    center = _center(win.get("bounds")) if win else None
    home = _screen_at(screens, center) if center else None
    if home is None:
        return run.verdict("skip", "no editor bounds on this platform")
    others = [s for s in screens if s["bounds"] != home["bounds"]]
    # A monitor at a different scale is the harder case, so prefer one.
    other = next((s for s in others if s.get("scale") != home.get("scale")), others[0])
    route = f"{home.get('name')} (x{home.get('scale')}) → {other.get('name')} (x{other.get('scale')})"
    if sys.platform not in ("darwin", "win32"):
        if _wm_move_commands("", other, win) is None:
            return run.verdict("skip", "no scripted window move on this desktop")
        moved, why = await _wm_move_editor(run, other, win)
        if why:
            return run.verdict("fail", why, route=route)
        bad, note, details = await _land_clicks(run, moved)
        _back, back_why = await _wm_move_editor(run, home, moved)
        return _monitors_verdict(run, other, route, moved, bad, note, details, back_why)
    if not await _focus_editor(run):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    x, y, w, _h = win["bounds"]
    # Mid-width on the title bar: past Notepad's tabs, on GNOME's title.
    grab = (x + w // 2, y + round(16 * (home.get("scale") or 1)))
    ox, oy, ow, oh = other["bounds"]
    moved, why = await _drag_editor(run, grab, (ox + ow // 2, oy + oh // 3), other)
    if why:
        return run.verdict("fail", why, route=route)
    bad, note, details = await _land_clicks(run, moved)
    mx, my, mw, _mh = moved["bounds"]
    # Back to where it was grabbed from.
    _back, back_why = await _drag_editor(
        run, (mx + mw // 2, my + round(16 * (other.get("scale") or 1))), grab, home)
    _monitors_verdict(run, other, route, moved, bad, note, details, back_why)


def _monitors_verdict(run: Run, other: dict, route: str, moved: dict,
                      bad: list[str] | str, note: str, details: dict, back_why: str) -> None:
    if isinstance(bad, str):
        return run.verdict("fail", f"on {other.get('name')}: {bad}", route=route, **details)
    if back_why:
        bad = [*bad, f"back: {back_why}"]
    run.verdict("fail" if bad else "pass",
                ("; ".join(bad) + " — " if bad else "") + f"{route}, there {note}, and back",
                route=route, bounds_there=moved["bounds"], **details)


async def scroll(run: Run) -> None:
    """scroll_at moves the view the named way: mark the middle row, scroll, mark again."""
    if run.editor is None:
        return run.verdict("skip", "no editor window")
    win = await _editor_window(run)
    center = _center(win.get("bounds") if win else None)
    if center is None:
        return run.verdict("skip", "no editor bounds on this platform")
    if not await _load_fixture(run):
        return run.verdict("fail", "the scratch editor didn't take focus; no input sent")
    x, y, w, _h = win["bounds"]
    cx, cy = x + w // 4, center[1]  # left of centre: inside the text, clear of a minimap
    steps = [("@4", None), ("@5", ("down", 10)), ("@6", ("up", 5))]
    for mark, move in steps:
        if move is not None:
            ok, text, _ = await run.call("scroll_at", x=cx, y=cy, direction=move[0], amount=move[1])
            if not ok:
                return run.verdict("fail", f"scroll_at {move[0]}: {text.splitlines()[0][:150]}")
            await asyncio.sleep(1.0)
        if (why := await _mark_at(run, cx, cy, mark)) is not None:
            return run.verdict("fail", why)
    marks = _find_marks(await _read_editor(run))
    missing = [m for m, _ in steps if m not in marks]
    if missing:
        return run.verdict("fail", f"markers not in the text: {missing}", marks=marks)
    r0, r1, r2 = marks["@4"][0], marks["@5"][0], marks["@6"][0]
    checks = {"down scrolls towards the end": r1 > r0, "up scrolls back": r2 < r1}
    bad = [k for k, v in checks.items() if not v]
    note = f"middle row {r0} → down 10 → {r1} → up 5 → {r2}"
    run.verdict("fail" if bad else "pass", ("; ".join(bad) + " — " if bad else "") + note,
                marks=marks, lines_per_tick_down=(r1 - r0) / 10, lines_per_tick_up=(r1 - r2) / 5)


async def deltas(run: Run) -> None:
    """Actions that return an observation (pointer, focus) carry a deltas
    block when an observer runs; typing and hotkeys return none."""
    seen = [d for step in run.steps if (d := deltas_of(step.text))]
    events = [d for d in seen if "no changes" not in d]
    if not seen:
        return run.verdict("fail", "no deltas block on any action reply")
    run.verdict("pass" if events else "info",
                f"{len(events)}/{len(seen)} action replies carried events", sample=seen[:2])


async def cleanup(run: Run) -> None:
    """Close the scratch editor, check its window is gone, and put the
    clipboard back.

    Never kills by a pid read off a window: only the process this script
    started, or (macOS) the one scratch document, by its unique name.
    """
    scratch = run.facts.get("editor_file")
    proc = run.facts.get("editor_proc")
    if sys.platform == "darwin" and scratch is not None:
        # By name: the file name is unique, and TextEdit reports /private/var
        # for a /var temp path, so a path match misses.
        script = ('on run argv\n'
                  'tell application "TextEdit" to close (every document whose name is '
                  '(item 1 of argv)) saving no\nend run')
        subprocess.run(["osascript", "-e", script, scratch.name], capture_output=True, check=False)
    else:
        if run.editor is not None and await _focus_editor(run):
            # Close it as a person would, saved so no dialog asks. Windows 11
            # Notepad runs every window and tab in one process, not the
            # notepad.exe started here, so killing that misses the window,
            # and it keeps unsaved tabs for next time. Mousepad saves a
            # restore session while it runs and offers it on the next start
            # unless it was closed, even on SIGTERM.
            await run.call("hotkey", keys=["ctrl", "s"])
            await asyncio.sleep(1.0)
            await run.call("hotkey", keys=["ctrl", "w"])
            await asyncio.sleep(1.0)
        if proc is not None and proc.poll() is None:
            proc.kill()
    left, unchecked = [], ""
    if run.editor is not None:
        deadline = time.monotonic() + 5
        while True:
            ok, text, _ = await run.call("list_windows", page_size=0)
            if ok:
                unchecked = ""
                left = [w.get("title") for w in first_json(text).get("windows", [])
                        if scratch.stem in (w.get("title") or "")]
                if not left:
                    break
            else:
                # An empty list here would read as "closed" (the server
                # crashing is when this matters most). Ask again: wmctrl
                # fails with BadWindow when a window goes mid-listing.
                unchecked = text.splitlines()[0][:120] if text else "no reply"
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.5)
    if scratch is not None:
        shutil.rmtree(scratch.parent, ignore_errors=True)
    if run.facts.get("clipboard") is not None:
        await run.call("clipboard_set", text=run.facts["clipboard"])
    if unchecked:
        return run.verdict("fail", f"couldn't check the editor window closed ({unchecked}); "
                                   "look for it by hand")
    if left:
        return run.verdict("fail", f"editor window still open, close it by hand: {left}")
    run.verdict("info", "editor closed, clipboard restored")


OBSERVING = {"surface": surface, "diagnose": diagnose, "screens": screens,
             "windows": windows, "scene": scene, "capture": capture, "web": web}
ACTING = {"clipboard": clipboard, "shell": shell, "editor": open_editor, "focus": focus,
          "pointer": pointer, "typing": typing, "keys": keys, "a11y": a11y, "landing": landing,
          "monitors": monitors,
          "scroll": scroll, "deltas": deltas, "cleanup": cleanup}
SCENARIOS = {**OBSERVING, **ACTING}


# ── Report ──────────────────────────────────────────────────────────


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, cwd=Path(__file__).parent).stdout.strip() or "?"
    except OSError:
        return "?"


def write_reports(run: Run, stem: Path) -> dict:
    result = {
        "host": platform.node(), "platform": sys.platform, "os": platform.platform(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "commit": _git_head(), "time": time.strftime("%Y-%m-%d %H:%M"),
        "verdicts": run.verdicts, "steps": [asdict(s) for s in run.steps],
    }
    stem.with_suffix(".json").write_text(json.dumps(result, indent=1, ensure_ascii=False,
                                                    default=str), encoding="utf-8")
    header = (f"{result['time']} · {result['os']} · {result['desktop']} {result['session']} · "
              f"commit {result['commit']}")
    lines = [f"# Smoke matrix — {result['host']}", "", header, "",
             "| Scenario | Verdict | Note |", "|---|---|---|"]
    for name, v in run.verdicts.items():
        lines.append(f"| {name} | {v['status']} | {v['note'].replace('|', '/')} |")
    lines.append("")
    for s in run.steps:
        args = ", ".join(f"{k}={v!r}" for k, v in s.args.items())
        img = f" +{s.images} image" if s.images else ""
        title = f"## [{s.scenario}] {s.tool}({args}) — {'ok' if s.ok else 'ERROR'} {s.seconds:.2f}s{img}"
        lines += [title, "", "```", s.text[:3000], "```", ""]
    stem.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return result


def compare(old_path: Path, new: dict, notes: bool = False) -> None:
    """Print verdicts whose status changed; with ``notes``, also those whose
    note did (window counts and image variance change run to run)."""
    old = json.loads(old_path.read_text(encoding="utf-8"))
    print(f"\ncompare with {old_path.name} (commit {old.get('commit')}):")
    changed = False

    def key(v: dict) -> tuple:
        # The scratch file name carries a pid; it differs on every run.
        note = re.sub(r"conductor-smoke-\d+", "conductor-smoke-N", v.get("note", ""))
        return (v.get("status"), note) if notes else (v.get("status"),)

    for name in sorted(set(old["verdicts"]) | set(new["verdicts"])):
        a = old["verdicts"].get(name, {})
        b = new["verdicts"].get(name, {})
        if name in old["verdicts"] and name in new["verdicts"] and key(a) != key(b):
            changed = True
            print(f"  {name}: {a.get('status')} {a.get('note', '')!r}\n"
                  f"  {' ' * len(name)}→ {b.get('status')} {b.get('note', '')!r}")
    if not changed:
        print("  no verdict changed")


async def run_all(session, names: list[str], stem: Path, compare_path: Path | None,
                  compare_notes: bool = False) -> None:
    run = Run(session)
    try:
        for name in names:
            run.scenario = name
            print(f"-- {name}", flush=True)
            try:
                await SCENARIOS[name](run)
            except Exception as e:
                run.verdict("error", f"{type(e).__name__}: {e}")
            v = run.verdicts.get(name, {"status": "?", "note": ""})
            print(f"   {v['status']}: {v['note']}", flush=True)
            write_reports(run, stem)  # an interrupted run still leaves a record
    finally:
        # Close the scratch editor and restore the clipboard even when the run
        # is cut short (a hang, Ctrl+C).
        if "cleanup" not in run.verdicts and "editor_file" in run.facts:
            run.scenario = "cleanup"
            await cleanup(run)
    result = write_reports(run, stem)
    print(f"report: {stem.with_suffix('.md')}")
    if compare_path is not None:
        compare(compare_path, result, notes=compare_notes)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--observe-only", action="store_true", help="skip every scenario that acts")
    ap.add_argument("--out", default=str(Path.home() / f"smoke_{platform.node()}"),
                    help="report path without extension (.md and .json are written)")
    ap.add_argument("--compare", type=Path, help="an earlier .json result to diff against")
    ap.add_argument("--compare-notes", action="store_true",
                    help="with --compare, also list verdicts whose note changed")
    opts = ap.parse_args()
    names = opts.scenarios or list(OBSERVING if opts.observe_only else SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenarios: {unknown} (known: {', '.join(SCENARIOS)})")

    lock = Path(tempfile.gettempdir()) / "conductor-smoke.lock"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CONDUCTOR_DESKTOP_", "CONDUCTOR_MCP_"))}
    env.update(CONDUCTOR_DESKTOP_AUTO_SCREENSHOT="false", CONDUCTOR_DESKTOP_DELTAS="true",
               CONDUCTOR_DESKTOP_AUDIT="false", CONDUCTOR_DESKTOP_LOCK_PATH=str(lock),
               PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from conductor_desktop.server import run; run()"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await run_all(session, names, Path(opts.out), opts.compare, opts.compare_notes)


if __name__ == "__main__":
    asyncio.run(main())
