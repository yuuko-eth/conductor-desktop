"""Accessibility modality — ``ui_*`` tools. See https://docs.conductor-mcp.io/concepts/accessibility/.

``ui_tree`` / ``ui_find`` read; the seven action tools act on their refs.
"""

from __future__ import annotations

import inspect
from typing import Annotated

from mcp.types import TextContent
from pydantic import Field

from ..core.ui.find import MATCH_MODES
from ..runtime.observe import post_action_observation
from ..runtime.replies import output_of
from ..state import current
from ..tooling import Content, ToolError, tool

_MAX_TIMEOUT = 30.0
_MAX_LIMIT = 100


def _clamp_timeout(timeout: float) -> float:
    return min(max(float(timeout), 0.1), _MAX_TIMEOUT)


Title = Annotated[str | None, Field(description=(
    "Substring of an app name, else of a window title. Omit for the "
    "frontmost app's focused window."))]
IncludeMenubar = Annotated[bool, Field(description=(
    "Also include the app's menu bar, which is left out by default."))]
Timeout = Annotated[float, Field(description=(
    "Seconds to spend, max 30. When it runs out the result is partial and "
    "says so."))]


@tool(kind="observing")
async def ui_tree(
    title: Title = None,
    depth: Annotated[int, Field(description="How many levels to walk below the window.")] = 8,
    max_children: Annotated[int, Field(description="Children listed per element before truncating.")] = 50,
    include_menubar: IncludeMenubar = False,
    format: Annotated[str, Field(description=(
        '"outline" (compact lines) or "json" (the unfolded tree with native '
        "roles, ids and child-index paths)."))] = "outline",
    timeout: Timeout = 10.0,
) -> str:
    """Read a window's native accessibility tree as text, one line per
    control, each with a ref the ui_* actions take; no grounder or
    screenshot. ui_find is cheaper when you know what you're looking for.
    Truncation is always marked ("…N more children"): re-read with a larger
    depth / max_children or a narrower title before concluding an element is
    absent. Canvases, games and remote desktops have no tree: use the visual
    tools there. For web page content, prefer web_*.

    Lines look like:

        button "Save" [e21] press @884,612,68,24
        checkbox "Wrap lines" [e30] toggle checked @40,80,16,16
        text_field "Search" [e14] set_value focus focused @10,40,200,22 "draft"

    - ``[e21]`` is the ref; re-reading the same element gives the same ref.
    - Verbs: press, set_value, toggle, select, expand, focus.
    - States: disabled, focused, selected, checked, mixed, expanded, protected.
    - ``@x,y,w,h`` are screen bounds, for click_at when no verb fits.
    - Trailing quoted text is the element's value (first 80 chars).

    Unnamed wrapper groups are folded away. macOS, Windows and Linux (AT-SPI);
    other platforms return ``unsupported``.
    """
    if format not in ("outline", "json"):
        raise ToolError('format must be "outline" or "json"')
    if depth < 0 or max_children < 1:
        raise ToolError("depth must be >= 0 and max_children >= 1")
    result = await current().executor.ui_tree(
        title, depth=depth, max_children=max_children,
        include_menubar=include_menubar, format=format, timeout=_clamp_timeout(timeout),
    )
    return output_of(result)


@tool(kind="observing")
async def ui_find(
    query: Annotated[str | None, Field(description=(
        "Text matched against element names and values; several words also "
        'match ancestor names ("sidebar documents").'))] = None,
    role: Annotated[str | None, Field(description=(
        "Exact role filter: a shared role (button, link, text_field, checkbox, "
        "tab, menu_item, …) or a native one (AXButton). Comma-separate several."))] = None,
    name: Annotated[str | None, Field(description="Text matched against the name only.")] = None,
    value: Annotated[str | None, Field(description="Text matched against the value only.")] = None,
    id: Annotated[str | None, Field(description="Exact native identifier.")] = None,
    actionable: Annotated[bool | None, Field(description=(
        "true: only elements with a verb; false: only those without."))] = None,
    states: Annotated[list[str] | None, Field(description=(
        'States that must all hold; "!" negates: ["checked"], ["!disabled"].'))] = None,
    title: Title = None,
    include_menubar: IncludeMenubar = False,
    limit: Annotated[int, Field(description=(
        "Matches listed, 1-100. The header still counts all of them."))] = 10,
    match: Annotated[str, Field(description=(
        '"auto" or "fuzzy" (whole words outrank substrings and typos), '
        '"exact" (case-insensitive equality, trailing "…" ignored). '
        '"semantic" is not installed and fails.'))] = "auto",
    timeout: Timeout = 10.0,
) -> str:
    """Find native UI elements by text and filters without reading the whole
    tree: a short ranked list with refs for the ui_* actions. query matches
    names and values, fuzzily by default; role, states, id and actionable
    filter exactly. If the reply warns that the top scores are close, don't
    act on #1 blindly: add role or title, or read ui_tree. In a browser, page
    links and buttons are found at any depth.

    Each line reads like a ui_tree line plus a score and the named ancestors.

    Example:

        4 matches in TextEdit (pid 42) — window "Untitled"
        1. button "Save" [e21] press @884,612,68,24  score 1.00  path: Save As
        2. combo_box "Save as PDF…" [e30] expand  score 0.79  path: Save As
        ⚠ top-2 scores within 0.10 — ambiguous; narrow with role/title or read ui_tree

    A search with only filters lists matches in tree order at score 1.00.
    macOS, Windows and Linux (AT-SPI); other platforms return ``unsupported``.
    """
    if match not in MATCH_MODES:
        raise ToolError(f"match must be one of {', '.join(MATCH_MODES)}")
    if not 1 <= limit <= _MAX_LIMIT:
        raise ToolError(f"limit must be between 1 and {_MAX_LIMIT}")
    result = await current().executor.ui_find(
        query, role=role, name=name, value=value, id=id, actionable=actionable,
        states=states, title=title, include_menubar=include_menubar, limit=limit,
        match=match, timeout=_clamp_timeout(timeout),
    )
    return output_of(result)


# ── Actions ──────────────────────────────────────────────────────────
#
# Shared contract (https://docs.conductor-mcp.io/concepts/accessibility/ §Action tools): the ref is
# re-resolved first (stale_ref / ambiguous_ref with candidates), the verb must
# be one the element lists (else unsupported_action with a click_at hint), the
# action goes through the accessibility API, and state is re-read to report
# verified / mismatch / unverified. Deltas follow, like the input tools.

_ACT_NOTE = """
    Before acting, the ref is looked up again. If the element is gone or now
    ambiguous the call fails with ``stale_ref`` / ``ambiguous_ref`` and lists
    similar elements, and nothing is acted on. An element that doesn't offer
    this verb fails with ``unsupported_action`` and its bounds, for click_at.
    The result ends in (verified), mismatch: …, or (unverified) when nothing
    can be read back. macOS, Windows and Linux (AT-SPI).
"""

Ref = Annotated[str, Field(description='Element ref, e.g. "e21", from ui_find or ui_tree.')]


async def _act(ref: str, verb: str, **params) -> list[Content]:
    result = await current().executor.ui_act(ref, verb, params)
    text = output_of(result)
    return [TextContent(type="text", text=text), *await post_action_observation()]


def _with_note(fn):
    fn.__doc__ = inspect.cleandoc(fn.__doc__ or "") + "\n\n" + inspect.cleandoc(_ACT_NOTE)
    return fn


@tool(kind="acting")
@_with_note
async def ui_press(ref: Ref) -> list[Content]:
    """Press a button, link or menu item by its ref from ui_find or ui_tree,
    through the accessibility API with no pointer: it works on an unfocused
    window. A stale ref fails and nothing is pressed, so prefer this to
    clicking for anything destructive."""
    return await _act(ref, "press")


@tool(kind="acting")
@_with_note
async def ui_set_value(
    ref: Ref,
    value: Annotated[str, Field(description=(
        'The new value, replacing the whole old one. Sliders take a number as text ("0.5").'))],
) -> list[Content]:
    """Replace the value of a text field, text area, combo box or slider by its
    ref from ui_find or ui_tree, then read it back. No clicking or typing
    involved, and a stale ref fails without acting. Password fields are set
    but never read back or echoed."""
    return await _act(ref, "set_value", value=value)


@tool(kind="acting")
@_with_note
async def ui_toggle(
    ref: Ref,
    on: Annotated[bool | None, Field(description=(
        "true/false sets that state and does nothing if it already holds; omit to flip."))] = None,
) -> list[Content]:
    """Toggle a checkbox or switch by its ref from ui_find or ui_tree; pass on=
    for a definite state rather than a flip. A stale ref fails without
    acting."""
    return await _act(ref, "toggle", on=on)


@tool(kind="acting")
@_with_note
async def ui_select(ref: Ref) -> list[Content]:
    """Select a tab, radio button, or a list, table or outline row or cell by
    its ref from ui_find or ui_tree. A stale ref fails without acting.

    Selection is state, not navigation: Finder's sidebar highlights a row on
    select but stays in the same folder; ui_press the row's cell to open it."""
    return await _act(ref, "select")


@tool(kind="acting")
@_with_note
async def ui_expand(
    ref: Ref,
    expand: Annotated[bool, Field(description="true expands or opens; false collapses.")] = True,
) -> list[Content]:
    """Expand or collapse an outline row, or open a menu bar item, menu button
    or pop-up, by its ref from ui_find or ui_tree. A stale ref fails without
    acting. An opened menu isn't verified: read it afterwards with
    ui_find(include_menubar=true) or ui_tree."""
    return await _act(ref, "expand", expand=expand)


@tool(kind="acting")
@_with_note
async def ui_focus(ref: Ref) -> list[Content]:
    """Give keyboard focus to a field, or raise a window or dialog and bring
    its app forward, by its ref from ui_find or ui_tree. Use it before
    type_text or key. A stale ref fails without acting."""
    return await _act(ref, "focus")


@tool(kind="acting")
@_with_note
async def ui_scroll_to(ref: Ref) -> list[Content]:
    """Scroll an element into view by its ref from ui_find or ui_tree, e.g.
    before a click_at on its bounds. Bounds change when it moves: re-read
    with ui_find or ui_tree for the new ones. A stale ref fails without
    acting."""
    return await _act(ref, "scroll_to")
