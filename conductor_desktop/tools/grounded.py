"""Grounded interaction tools — natural-language target → screen action.

Each tool calls ``resolve_one`` to ground the target description against
the current screenshot, then dispatches to the executor. Ambiguous
descriptions return a spatial-anchor list rather than guessing.
"""

from __future__ import annotations

from typing import Annotated

from mcp.types import TextContent
from pydantic import Field

from ..runtime.grounding import hit_caveats, resolve_one, scene_snapshot
from ..runtime.observe import post_action_observation
from ..runtime.replies import action_reply
from ..state import current
from ..tooling import Content, ToolError, tool

Target = Annotated[str, Field(description=(
    'What to act on, as "{thing} in {place}", e.g. "Submit button in the '
    'signup form".'))]
Screen = Annotated[int | str | None, Field(description=(
    "Monitor index or name from list_screens, to ground on that monitor only. "
    "Omit for the whole desktop."))]


def _warn_lines(warn: list[str], prefix: str = "") -> list[TextContent]:
    """Render grounding caveats as their own result lines.

    These act *after* grounding, so a caveat can't gate the action the way
    `locate`'s can — by the time the agent reads this the click has landed.
    It is still worth emitting: a single-best grounder returns a confident
    point for a target that isn't on screen, so "clicked at (916,764)" alone
    reads as success whether or not anything was there. The caveat is what
    turns a silent wrong click into a visible one, and it must be gathered
    before the action (see `hit_caveats`).
    """
    return [TextContent(type="text", text=f"warn: {prefix}{w}") for w in warn]


@tool(kind="acting")
async def click(target: Target,
                button: Annotated[str, Field(description='"left", "right" or "middle".')] = "left",
                clicks: Annotated[int, Field(description="2 for a double click.")] = 1,
                screen: Screen = None,
                ) -> list[Content]:
    """Click an element described in words, found on screen by the grounder
    model. Describe it as "{thing} in {place}". If several things match,
    nothing is clicked and the error lists them: describe the one you mean
    more precisely. Needs a grounder; without one the error says what to use
    instead. For a native app control, ui_press is safer: a ref can't land
    on the wrong thing.

    The reply may carry `warn:` lines: the point was in a background window,
    on no window, or came from a grounder that can't report ambiguity. The
    click still happened, but "clicked" alone doesn't prove it hit the right
    thing; look at the screen before building on it.
    """
    return await _click(target, button, clicks, screen)


@tool(kind="acting")
async def double_click(target: Target, screen: Screen = None) -> list[Content]:
    """Double-click an element described in words, e.g. to open a file. Same
    grounding, refusal and warn: rules as click."""
    return await _click(target, "left", 2, screen)


@tool(kind="acting")
async def right_click(target: Target, screen: Screen = None) -> list[Content]:
    """Right-click an element described in words, usually to open a context
    menu; read its items with ui_find or read_screen, then pick one. Same
    grounding, refusal and warn: rules as click."""
    return await _click(target, "right", 1, screen)


async def _click(target: str, button: str, clicks: int,
                 screen: int | str | None) -> list[Content]:
    """Shared body of the click tools.

    The tools call this rather than each other: a declared tool is the
    wrapped callable, so calling ``click`` from ``double_click`` would audit,
    time and report the call twice.
    """
    resolved = await resolve_one(target, screen=screen)
    if isinstance(resolved, str):
        raise ToolError(resolved, content=await post_action_observation())
    x, y = resolved.center
    warn = await hit_caveats(resolved, screen=screen)
    result = await current().executor.click(x, y, button=button, clicks=clicks, screen=screen)
    return await action_reply(f"click({button},{clicks}) at ({x},{y})", result,
                              notes=_warn_lines(warn))


@tool(kind="acting")
async def mouse_move(target: Target, screen: Screen = None) -> list[Content]:
    """Move the pointer over an element described in words without clicking, to
    show hover UI such as tooltips or menus that open on hover. Same
    grounding and refusal rules as click; mouse_move_at takes coordinates
    instead."""
    resolved = await resolve_one(target, screen=screen)
    if isinstance(resolved, str):
        raise ToolError(resolved, content=await post_action_observation())
    x, y = resolved.center
    warn = await hit_caveats(resolved, screen=screen)
    result = await current().executor.move_to(x, y, screen=screen)
    return await action_reply(f"mouse_move to ({x},{y})", result, notes=_warn_lines(warn))


@tool(kind="acting")
async def drag(from_target: Annotated[str, Field(description=(
                   'Where the drag starts, as "{thing} in {place}".'))],
               to_target: Annotated[str, Field(description=(
                   'Where it ends, as "{thing} in {place}".'))],
               screen: Screen = None,
               ) -> list[Content]:
    """Drag from one element described in words to another, e.g. a file onto a
    folder. Both ends are found by the grounder first; if either is missing
    or ambiguous, nothing moves and the error says which end. Both ends must
    be on the same monitor. drag_at takes coordinates instead.
    """
    src = await resolve_one(from_target, screen=screen)
    if isinstance(src, str):
        raise ToolError(f"source: {src}")
    dst = await resolve_one(to_target, screen=screen)
    if isinstance(dst, str):
        raise ToolError(f"target: {dst}")
    sx, sy = src.center
    dx, dy = dst.center
    # One scene snapshot, shared by both endpoints — they were grounded
    # against the same frame, so re-reading the topology per endpoint would
    # only add latency.
    scene = await scene_snapshot()
    warn = [*_warn_lines(await hit_caveats(src, screen=screen, scene=scene),
                         prefix="source: "),
            *_warn_lines(await hit_caveats(dst, screen=screen, scene=scene),
                         prefix="target: ")]
    result = await current().executor.drag(sx, sy, dx, dy, screen=screen)
    return await action_reply(f"drag ({sx},{sy}) → ({dx},{dy})", result, notes=warn)


@tool(kind="acting")
async def scroll(target: Annotated[str, Field(description=(
                     '"screen" for the middle of the screen, or the scrollable '
                     'area as "{thing} in {place}".'))],
                 direction: Annotated[str, Field(description='"up", "down", "left" or "right".')],
                 amount: Annotated[int, Field(description="Scroll ticks.")] = 3,
                 screen: Screen = None,
                 ) -> list[Content]:
    """Scroll an area described in words, or the middle of the screen with
    target="screen" (no grounder needed for that). The scroll goes to
    whatever is under that point, so name the scrollable area itself, e.g.
    "file list in the Finder window". To bring a native element into view,
    ui_scroll_to is more direct; scroll_at takes coordinates."""
    warn: list[str] = []
    if target.strip().lower() == "screen":
        w, h = await current().executor.get_screen_dimensions(screen=screen)
        x, y = w // 2, h // 2
    else:
        resolved = await resolve_one(target, screen=screen)
        if isinstance(resolved, str):
            raise ToolError(resolved)
        x, y = resolved.center
        warn = await hit_caveats(resolved, screen=screen)
    result = await current().executor.scroll(x, y, direction, amount=amount, screen=screen)
    return await action_reply(f"scroll {direction} x{amount} at ({x},{y})", result,
                              notes=_warn_lines(warn))
