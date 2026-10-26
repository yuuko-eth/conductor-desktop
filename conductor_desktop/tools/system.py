"""System / windows tools — shell exec, window focus, scene graph, clipboard."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from ..runtime.capture import check_screen_device, mobile_executor
from ..runtime.replies import action_reply, output_of
from ..state import current
from ..tooling import Content, ToolError, tool

# Sort aliases → window-dict field names. `z` is scene z-order (front=0), the
# natural default; `process` maps to the `process_name` key.
_WINDOW_SORT_FIELDS = {
    "z": "z", "title": "title", "process": "process_name",
    "pid": "pid", "focused": "focused", "monitor": "monitor",
}


def _win_sort_key(v):
    """Stable key that sorts ``None`` last and never cross-type-compares
    (within one field the values are homogeneous; the None flag short-circuits
    before the value is reached)."""
    return (v is None, v if v is not None else 0)


def _paginate_windows(
    raw: str, *, match: str | None, sort: str, desc: bool, page: int, page_size: int,
    monitor: int | None = None,
) -> str | None:
    """Filter (monitor index, then substring SQL ``%match%`` style over
    title+process), sort, and page a `list_windows` JSON array into a compact
    paged envelope.

    ``monitor`` keeps windows whose ``monitor`` field equals it. When no
    window carries that field (the platform doesn't report it), the filter is
    skipped and the envelope gains a ``note`` — an empty list would read as
    "no windows on that screen", which isn't known.

    Returns the envelope JSON string, or ``None`` if ``raw`` isn't a JSON list
    (caller passes the original output through unchanged — e.g. mobile).
    """
    try:
        wins = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(wins, list):
        return None

    total = len(wins)
    note = None
    if monitor is not None:
        if any(isinstance(w, dict) and "monitor" in w for w in wins):
            wins = [w for w in wins if isinstance(w, dict) and w.get("monitor") == monitor]
        else:
            note = ("screen= ignored: this platform doesn't report which "
                    "monitor a window is on")
    if match:
        needle = match.lower()
        wins = [
            w for w in wins
            if isinstance(w, dict)
            and needle in f"{w.get('title', '')} {w.get('process_name', '')}".lower()
        ]
    matched = len(wins)

    field = _WINDOW_SORT_FIELDS.get((sort or "z").lower())
    if field and any(isinstance(w, dict) and w.get(field) is not None for w in wins):
        wins = sorted(
            wins,
            key=lambda w: _win_sort_key(w.get(field) if isinstance(w, dict) else None),
            reverse=desc,
        )

    if page_size and page_size > 0:
        pages = max(1, (matched + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        shown = wins[start:start + page_size]
        eff_page_size = page_size
    else:  # page_size <= 0 → return everything (escape hatch)
        pages, page, shown, eff_page_size = 1, 1, wins, matched

    return json.dumps({
        "total": total,
        "matched": matched,
        "page": page,
        "pages": pages,
        "page_size": eff_page_size,
        "returned": len(shown),
        **({"note": note} if note else {}),
        "windows": shown,
    }, default=str)


@tool(kind="acting", gate="shell_enabled")
async def exec_shell(
    command: Annotated[str, Field(description="The command line, run by the platform shell.")],
    timeout: Annotated[float, Field(description="Seconds before the command is abandoned.")] = 30.0,
) -> str:
    """Run a shell command on the host and return its output: cmd.exe on
    Windows, /bin/sh elsewhere. Often quicker than clicking through a UI
    (launching apps, reading files, checking state). A non-zero exit is
    reported as an error with the command's output."""
    result = await current().executor.exec_shell(command, timeout=timeout)
    return output_of(result, empty="ok", error="exec failed")


@tool(kind="acting")
async def focus_window(
    title: Annotated[str, Field(description=(
        "Substring of the window title, as list_windows shows it. On macOS "
        "an app name also matches."))],
) -> list[Content]:
    """Bring a window to the front and give it keyboard focus, by a
    substring of its title. Use it before type_text, key or hotkey, which go
    to whatever has focus. For a browser tab inside a window use
    activate_tab; for a dialog you have a ref for, ui_focus."""
    result = await current().executor.focus_window(title=title)
    return await action_reply(f"focus_window({title!r})", result)


@tool(kind="observing")
async def get_scene_graph() -> str:
    """Return the host's screens, workspaces and windows as one JSON snapshot:
    which windows sit on which screen and workspace, in z-order, and which
    is focused. It's topology, not contents: menu items and dialog buttons
    aren't in it; read those with ui_tree. list_windows is the filtered,
    paged view of the same windows.

    - ``screens``: keyed by index, each with its current space, focused
      window and visible spaces.
    - ``spaces``: keyed by ``ws:<output>:<name>`` (per-output compositors) or
      ``ws:global:<n>``; each lists its windows topmost first.
    - ``windows``: list_windows entries keyed by window id; ``monitor`` points
      into ``screens``. A context menu or modal dialog shows up as a window
      with ``layer`` menu / modal, and ``modality`` says which windows it
      blocks.
    - ``active_screen``: the screen with keyboard focus.
    """
    return output_of(await current().executor.get_scene_graph(), empty="{}")


@tool(kind="observing")
async def read_grid(
    title: Annotated[str | None, Field(description=(
        "Substring of an app name, else a window title. Omit for the active "
        "app's focused window, the same one ui_tree would read."))] = None,
    max_rows: Annotated[int, Field(description=(
        "Rows to read. Off-screen rows can be read too, but each costs an "
        "accessibility call."))] = 100,
) -> str:
    """Read a table, list or grid from a window's native accessibility tree as
    JSON cells, with no screenshot or grounder: process lists, file lists,
    mail lists, data grids. Each cell has its text and an on-screen bbox to
    click. Implemented on macOS and Windows; elsewhere, and on surfaces with
    no table control (canvas-drawn sheets), it returns a failure reason:
    fall back to ui_tree or read_screen.

    Returns ``{rows, cols, cells:[{r,c,bbox,text,can_set}], source,
    capabilities, failure}``. ``bbox`` is null for an off-screen cell;
    ``can_set`` marks a cell writable without clicking. ``capabilities`` is
    ``{has_structure, has_values, has_geometry, can_act}``. ``failure`` is
    null on success, else ``canvas_blackout``, ``a11y_cold``,
    ``elevation_required`` or ``not_implemented``.
    """
    return output_of(await current().executor.extract_table(title=title, max_rows=max_rows),
                     empty="{}")


@tool(kind="observing")
async def list_screens() -> str:
    """List the host's monitors as JSON: index, name (e.g. "DP-1"), bounds [x,
    y, w, h] in virtual-desktop coordinates, HiDPI scale and which is
    primary. Use an index or name as screen= on the capture, grounding and
    coordinate tools; list_windows entries point back here by monitor index."""
    return output_of(await current().executor.list_screens(), empty="[]")


@tool(kind="observing")
async def list_windows(
    device_id: Annotated[str | None, Field(description=(
        "A phone from mobile_list_devices: returns its resumed activity and "
        "activity stack (Android) or foreground app (iOS) instead of host "
        "windows, and the filters below don't apply."))] = None,
    screen: Annotated[int | str | None, Field(description=(
        'Keep only windows on this monitor: index, name from list_screens, or "primary".'))] = None,
    match: Annotated[str | None, Field(description=(
        'Case-insensitive substring of the title or process name, e.g. "chrome".'))] = None,
    sort: Annotated[str, Field(description=(
        '"z" (front first), "title", "process", "pid", "focused" or "monitor".'))] = "z",
    desc: Annotated[bool, Field(description="Reverse the sort.")] = False,
    page: Annotated[int, Field(description="1-based page number.")] = 1,
    page_size: Annotated[int, Field(description="Windows per page; 0 returns all of them.")] = 20,
) -> str:
    """List open windows with title, process, bounds, z-order, focus and
    monitor, filtered, sorted and paged. Use match= to skip the wall of
    menu-bar and widget windows. The reply is {total, matched, page, pages,
    returned, windows}: matched > returned means more pages. It shows where
    windows are, not what's inside them: menus and dialog contents are in
    ui_tree.

    How much detail each window carries depends on the platform: X11, macOS,
    Windows and Hyprland report bounds, z-order and state; a compositor that
    only reports titles leaves them at defaults. On a platform that doesn't
    say which monitor a window is on, screen= is ignored and the reply's
    ``note`` says so.
    """
    check_screen_device(device_id, screen)
    if device_id:
        ex = mobile_executor(device_id)
        if not hasattr(ex, "list_activities"):
            raise ToolError(f"list_activities not supported on device {device_id}'s transport")
        return output_of(await ex.list_activities(), empty="[]")

    monitor = None
    if screen is not None:
        # Same resolution as capture's screen= (index, name, "primary").
        resolve = getattr(current().executor, "_resolve_screen", None)
        if resolve is None:
            raise ToolError("screen= is not supported by this executor")
        monitor, _, _ = await resolve(screen)

    raw = output_of(await current().executor.list_windows(), empty="[]")
    paged = _paginate_windows(
        raw,
        match=match, sort=sort, desc=desc, page=page, page_size=page_size,
        monitor=monitor,
    )
    # Non-JSON output (shouldn't happen for host) → pass through unchanged.
    return paged if paged is not None else raw


@tool(kind="observing")
async def clipboard_get() -> str:
    """Return the clipboard's text. Paired with selecting and copying (hotkey),
    it reads back text an app shows, e.g. to check what got typed."""
    return output_of(await current().executor.get_clipboard())


@tool(kind="acting")
async def clipboard_set(
    text: Annotated[str, Field(description="The text to put on the clipboard.")],
) -> list[Content]:
    """Replace the clipboard's contents with text. Pasting it with hotkey
    enters long or non-ASCII text exactly, in one step."""
    result = await current().executor.set_clipboard(text)
    return await action_reply(f"set_clipboard({len(text)} chars)", result, observe=False)
