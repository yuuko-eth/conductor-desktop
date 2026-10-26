"""KDE Plasma backend: KWin scripting for windows, Klipper for the clipboard, AT-SPI2.

KWin exposes no window list on D-Bus — the only way in is to load a KWin
script (``org.kde.KWin /Scripting``), and a script can't return a value. It
can ``print()``, and kwin's stdout is its journal, so a script writes
nonce-tagged JSON lines and this module reads them back with ``journalctl
--user``. That is the whole trick, and it costs about 30 ms per call, which
is the same order as ``wmctrl`` on X11. Measured on KWin 6.6.4: ``run()``
returns only after the script has finished, and the lines are already in the
journal by then, so no polling is needed.

Covers Wayland and X11 alike — same kwin, same D-Bus object either way,
though Plasma on X11 routes to ``linux_x11``, which has EWMH.

System deps: systemd's ``busctl`` and ``journalctl``, at-spi2-core. A KDE
without systemd has no journal to read, and window listing says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from ..errors import BackendError
from ._cli import run as _run
from ._cli import wl_or_x_get, wl_or_x_set

log = logging.getLogger("conductor_desktop.kde")

# ── Clipboard ───────────────────────────────────────────────────────

# wl-paste hangs on Plasma 6 until its timeout, so Klipper (Plasma's clipboard
# service) goes first. busctl, not gdbus: gdbus parses each argument as
# GVariant text, so clipboard text such as ``true`` or ``'x'`` would change
# type or fail; busctl takes the string as is after its ``s`` signature.
_KLIPPER_CALL = ["call", "org.kde.klipper", "/klipper", "org.kde.klipper.klipper"]


def _klipper_cmd(*args: str, json_out: bool = False) -> list[str]:
    """A busctl Klipper call whose arguments can't be read as options.

    The ``--`` matters: busctl parses anything starting with ``-`` as its own
    option wherever it appears, so copying ``-conductor smoke 1`` died with
    ``busctl: invalid option -- 'c'`` and copying ``--help`` was swallowed
    silently, leaving the old clipboard in place for the next paste. Text is
    data, never options.
    """
    options = ["busctl", "--user"]
    if json_out:
        options.append("--json=short")
    return [*options, "--", *_KLIPPER_CALL, *args]


async def _klipper_get() -> str:
    out = await _run(_klipper_cmd("getClipboardContents", json_out=True))
    try:
        return json.loads(out)["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise BackendError(f"Klipper returned an unexpected reply: {out[:200]!r}") from None


async def _klipper_set(text: str) -> None:
    await _run(_klipper_cmd("setClipboardContents", "s", text))


async def get_clipboard() -> str:
    """Read the clipboard ("" when it holds no text)."""
    try:
        return await _klipper_get()
    except BackendError as k_err:
        log.debug("Klipper read failed, trying wl-clipboard: %s", k_err)
        try:
            return await wl_or_x_get()
        except BackendError as wl_err:
            raise BackendError(f"Klipper: {k_err}; {wl_err}") from None


async def set_clipboard(text: str) -> None:
    try:
        await _klipper_set(text)
    except BackendError as k_err:
        log.debug("Klipper write failed, trying wl-clipboard: %s", k_err)
        try:
            await wl_or_x_set(text)
        except BackendError as wl_err:
            raise BackendError(f"Klipper: {k_err}; {wl_err}") from None


# ── KWin scripting bridge ───────────────────────────────────────────

# Same `--` rule as the Klipper calls: a plugin name or path is data.
_SCRIPTING = ["busctl", "--user", "--", "call", "org.kde.KWin", "/Scripting",
              "org.kde.kwin.Scripting"]
# How far back to scan the journal. The nonce is what identifies the reply;
# this only bounds how much of it is read, and the read happens milliseconds
# after the script ran.
_JOURNAL_SINCE = "-15s"
_SCRIPT_TIMEOUT = 10.0
# How long to wait for journald to catch up with what kwin already printed,
# and how often to look while waiting.
_REPLY_TIMEOUT = 2.0
_REPLY_POLL = 0.05

# Shared by every script: kwin gives a window's id as a QUuid that prints
# with braces, and the braces are noise in a tool's output.
_JS_PREAMBLE = """
function cid(w) { return String(w.internalId).replace("{", "").replace("}", ""); }
function emit(kind, obj) { print(NONCE + " " + kind + " " + JSON.stringify(obj)); }
"""


def _script_id(out: str) -> int:
    """The id ``loadScript`` replied with. busctl prints it as ``i 3``."""
    token = out.strip().split()
    try:
        return int(token[-1])
    except (IndexError, ValueError):
        raise BackendError(f"KWin's loadScript returned an unexpected reply: {out[:200]!r}") from None


async def _run_kwin_script(body: str, timeout: float = _SCRIPT_TIMEOUT) -> list[dict]:
    """Load ``body`` into KWin, run it, and return the records it printed.

    ``body`` gets ``NONCE`` and the helpers in ``_JS_PREAMBLE``, and must
    ``emit(kind, obj)`` its records and finish with
    ``print(NONCE + " END " + n)``. A reply without that end marker is an
    error rather than a short list: journald drops messages when a service
    floods it, and a silently truncated window list is worse than none.

    Every record comes back as ``{"kind": ..., **fields}``.
    """
    nonce = "CONDUCTOR" + secrets.token_hex(8)
    plugin = f"conductor-{nonce}"
    tmp = Path(tempfile.mkdtemp(prefix="conductor-kwin-"))
    try:
        path = tmp / "script.js"
        # The nonce and any parameters reach the script as JSON literals.
        # A script is code kwin executes, so nothing is ever interpolated raw.
        path.write_text(f"var NONCE = {json.dumps(nonce)};\n{_JS_PREAMBLE}\n{body}\n",
                        encoding="utf-8")
        script_id = _script_id(await _run([*_SCRIPTING, "loadScript", "ss", str(path), plugin]))
        if script_id < 0:
            raise BackendError(
                "KWin refused to load the script (loadScript returned -1). "
                "That usually means kwin can't read the file.")
        try:
            await _run(["busctl", "--user", "--", "call", "org.kde.KWin",
                        f"/Scripting/Script{script_id}", "org.kde.kwin.Script", "run"],
                       timeout=timeout)
            return await _await_reply(nonce)
        finally:
            await _unload(plugin)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _unload(plugin: str) -> None:
    """Best effort: a script left loaded would block the next run of its name."""
    try:
        await _run([*_SCRIPTING, "unloadScript", "s", plugin])
    except BackendError as e:
        log.debug("unloading the KWin script %s failed: %s", plugin, e)


async def _journal_text() -> str:
    try:
        return await _run(["journalctl", "--user", "--since", _JOURNAL_SINCE, "-o", "cat"])
    except BackendError as e:
        raise BackendError(
            f"reading kwin's output needs journalctl, which failed ({e}). "
            "KWin scripts can only answer through the journal, so window "
            "listing needs systemd here.") from None


def _parse_reply(text: str, nonce: str) -> tuple[list[dict], int | None]:
    """The nonce-tagged records in ``text``, and the count its end marker
    announced (None when the end marker hasn't been written yet).

    Raises only for output that can't become valid by waiting.
    """
    records: list[dict] = []
    total = None
    for line in text.splitlines():
        if not line.startswith(nonce + " "):
            continue
        kind, _, payload = line[len(nonce) + 1:].partition(" ")
        if kind == "END":
            try:
                total = int(payload.strip())
            except ValueError:
                raise BackendError(
                    f"KWin's reply had an unreadable end marker: {payload[:80]!r}") from None
            continue
        try:
            record = json.loads(payload)
        except ValueError:
            raise BackendError(
                f"KWin printed a record this can't read: {payload[:200]!r}") from None
        record["kind"] = kind
        records.append(record)
    return records, total


async def _await_reply(nonce: str) -> list[dict]:
    """Poll the journal until the script's whole reply is there.

    kwin has finished by the time ``run()`` returns, but journald ingests its
    output asynchronously, so a single read is a race — one that shows up as
    a missing end marker perhaps once in a hundred calls. In practice the
    first read already has everything (measured: zero retries over a 30-call
    run); this is the guard for when it doesn't.
    """
    deadline = time.monotonic() + _REPLY_TIMEOUT
    records: list[dict] = []
    total = None
    while True:
        records, total = _parse_reply(await _journal_text(), nonce)
        if total is not None and total == len(records):
            return records
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(_REPLY_POLL)
    if total is None:
        raise BackendError(
            f"KWin ran the script but its output never reached the journal "
            f"within {_REPLY_TIMEOUT:g}s. journald drops messages when a "
            f"service floods it.")
    raise BackendError(
        f"KWin's reply stayed truncated: it announced {total} records and "
        f"{len(records)} arrived (journald dropped the rest).")


# ── Screens ─────────────────────────────────────────────────────────

_SCREENS_JS = """
var order = workspace.screenOrder;
var primary = order.length ? order[0].name : "";
var screens = workspace.screens;
for (var i = 0; i < screens.length; i++) {
    var s = screens[i];
    var g = s.geometry;
    emit("S", {
        index: i, name: s.name, bounds: [g.x, g.y, g.width, g.height],
        scale: s.devicePixelRatio, primary: s.name === primary,
    });
}
print(NONCE + " END " + screens.length);
"""


def _screen_dicts(records: list[dict]) -> list[dict]:
    return [{
        "index": int(r.get("index", i)),
        "name": str(r.get("name") or f"output-{i}"),
        "bounds": [int(n) for n in r.get("bounds", [0, 0, 0, 0])],
        "scale": float(r.get("scale", 1.0) or 1.0),
        "primary": bool(r.get("primary", False)) or i == 0,
    } for i, r in enumerate(r for r in records if r["kind"] == "S")]


async def list_screens() -> list[dict]:
    """Enumerate outputs through KWin's own screen list."""
    return _screen_dicts(await _run_kwin_script(_SCREENS_JS))


# ── Windows ─────────────────────────────────────────────────────────

# Emits the screens too: attributing a window to a monitor needs both, and
# one script run is one round trip.
_WINDOWS_JS = _SCREENS_JS.replace('print(NONCE + " END " + screens.length);', "") + """
// The named type flags come from a window type, which only X11 windows
// have: an xdg-shell toplevel declares none, so on Wayland every one of
// them is false and the last two checks are what classify a window
// (measured on KWin 6.6.4 — a kate menu reports windowType -1 with
// popupWindow true, and a kdialog box reports type Normal with modal true).
function layerOf(w) {
    if (w.fullScreen) return "fullscreen";
    if (w.desktopWindow) return "background";
    if (w.dock || w.notification || w.criticalNotification || w.onScreenDisplay) return "overlay";
    if (w.menu || w.dropdownMenu || w.popupMenu) return "menu";
    if (w.tooltip) return "tooltip";
    if (w.splash || w.dialog) return "modal";
    if (w.toolbar || w.utility) return "floating";
    if (w.popupWindow) return "menu";
    if (w.modal) return "modal";
    if (w.keepAbove) return "always_on_top";
    if (w.keepBelow) return "background";
    return "normal";
}
function stateOf(w) {
    if (w.minimized) return "minimized";
    if (w.fullScreen) return "fullscreen";
    if (w.maximizeMode === 3) return "maximized";
    return "normal";
}
// stackingOrder is bottom-to-top; the tools report z=0 as the topmost.
var order = workspace.stackingOrder;
var zOf = {};
for (var i = 0; i < order.length; i++) { zOf[cid(order[i])] = order.length - 1 - i; }
var list = workspace.windowList();
for (var j = 0; j < list.length; j++) {
    var w = list[j];
    var g = w.frameGeometry;
    var id = cid(w);
    emit("W", {
        id: id,
        title: w.caption || "",
        process_name: w.resourceClass || "",
        pid: w.pid || 0,
        bounds: [g.x, g.y, g.width, g.height],
        state: stateOf(w),
        // kwin numbers virtual desktops from 1; the tools number from 0, and
        // a window on all of them belongs to no single one.
        desktop: w.desktops.length ? w.desktops[0].x11DesktopNumber - 1 : -1,
        focused: !!w.active,
        output: w.output ? w.output.name : "",
        z: zOf.hasOwnProperty(id) ? zOf[id] : -1,
        layer: layerOf(w),
        sticky: !!w.onAllDesktops,
        modal: !!w.modal,
        parent_window_id: w.transientFor ? cid(w.transientFor) : null,
    });
}
print(NONCE + " END " + (screens.length + list.length));
"""


def _window_dicts(records: list[dict], desktop: int | None) -> list[dict]:
    monitor_by_output = {s["name"]: s["index"] for s in _screen_dicts(records)}
    windows = []
    for r in records:
        if r["kind"] != "W":
            continue
        if desktop is not None and int(r.get("desktop", -1)) != desktop:
            continue
        parent = r.get("parent_window_id")
        # EWMH semantics, the same ones linux_x11 reports: a modal with a
        # transient parent blocks that window, one without blocks its app.
        modality = None
        if r.get("modal"):
            modality = "window" if parent else "app"
        desk = int(r.get("desktop", -1))
        windows.append({
            "id": str(r.get("id", "")),
            "title": str(r.get("title") or ""),
            "process_name": str(r.get("process_name") or ""),
            "pid": int(r.get("pid", 0) or 0),
            "bounds": [int(n) for n in r.get("bounds", [0, 0, 0, 0])],
            "state": str(r.get("state") or "normal"),
            "desktop": desk,
            "focused": bool(r.get("focused", False)),
            "monitor": monitor_by_output.get(str(r.get("output") or ""), -1),
            "z": int(r.get("z", -1)),
            "layer": str(r.get("layer") or "normal"),
            "sticky": bool(r.get("sticky", False)),
            "workspace": {"global_index": desk if desk >= 0 else None, "per_output": None},
            "parent_window_id": parent,
            "modality": modality,
        })
    return windows


async def list_windows(desktop: int | None = None) -> list[dict]:
    """List windows through a KWin script, with their screens in the same run."""
    return _window_dicts(await _run_kwin_script(_WINDOWS_JS), desktop)


_FOCUS_JS = """
var want = WANT_ID;
var list = workspace.windowList();
var hit = 0;
for (var i = 0; i < list.length; i++) {
    if (cid(list[i]) === want) {
        workspace.activeWindow = list[i];
        hit = 1;
    }
}
emit("F", {activated: hit === 1});
print(NONCE + " END 1");
"""


async def focus_window(title: str | None = None, pid: int | None = None) -> bool:
    """Focus the first window whose title contains ``title`` (ignoring case),
    or whose pid is ``pid``, like the other backends. False: nothing matched.

    The match runs here, on the window list, and the activation goes by
    kwin's own id for the window — a title is not unique and kwin has no
    lookup by one.
    """
    if not title and not pid:
        return False
    needle = title.casefold() if title else None
    match = next((w for w in await list_windows()
                  if (pid and w.get("pid") == pid)
                  or (needle and needle in (w.get("title") or "").casefold())), None)
    if match is None:
        return False
    records = await _run_kwin_script(_FOCUS_JS.replace("WANT_ID", json.dumps(match["id"])))
    return any(r["kind"] == "F" and r.get("activated") for r in records)


# ── Backend ─────────────────────────────────────────────────────────

from .desktop import Capabilities, DesktopBackend  # noqa: E402
from .linux_ui import ui_act, ui_walk  # noqa: E402


class KdeDesktop(DesktopBackend):
    """KDE Plasma on Wayland: KWin scripting, Klipper, AT-SPI."""

    name = "linux_kde"
    caps = Capabilities(windows=True, screens=True, clipboard=True, ui=True)

    list_windows = staticmethod(list_windows)
    focus_window = staticmethod(focus_window)
    list_screens = staticmethod(list_screens)
    get_clipboard = staticmethod(get_clipboard)
    set_clipboard = staticmethod(set_clipboard)
    ui_walk = staticmethod(ui_walk)
    ui_act = staticmethod(ui_act)
