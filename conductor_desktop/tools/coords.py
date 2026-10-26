"""Coordinate-escape-hatch tools — for drivers that localize from screenshots directly.

Bypasses the grounder. The brain owns ``(x, y)`` resolution before the call.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..runtime.capture import check_pixel_coords, frame_to_native
from ..runtime.observe import post_action_observation
from ..runtime.replies import action_reply
from ..state import current
from ..tooling import Content, ToolError, tool

Screen = Annotated[int | str | None, Field(description=(
    "Monitor index or name from list_screens: x,y are then local to that "
    "monitor. Omit for whole-desktop coordinates."))]
ImageCoords = Annotated[bool, Field(description=(
    "true: x,y are pixels in the last screenshot you were shown (capture_screen "
    "or an action's screenshot), and the server scales them to the screen."))]
X = Annotated[int, Field(description="Pixels from the left edge.")]
Y = Annotated[int, Field(description="Pixels from the top edge.")]


async def _resolve(points: list[tuple[int, int]], screen: int | str | None,
                   image_coords: bool
                   ) -> tuple[list[tuple[int, int]], int | str | None]:
    if image_coords:
        points, screen = frame_to_native(None, screen, points)
    if (err := await check_pixel_coords(*points, screen=screen)) is not None:
        raise ToolError(err, content=await post_action_observation())
    return points, screen


@tool(kind="acting")
async def click_at(x: X, y: Y,
                   button: Annotated[str, Field(description='"left", "right" or "middle".')] = "left",
                   clicks: Annotated[int, Field(description="2 for a double click.")] = 1,
                   screen: Screen = None, image_coords: ImageCoords = False,
                   ) -> list[Content]:
    """Click at exact pixel coordinates, with no grounder involved.
    Coordinates are screen pixels, the space ui_tree, list_windows and locate
    report. A screenshot may be smaller than the screen (its reply says so):
    either multiply, or pass image_coords=true to give pixels from the image.
    """
    [(x, y)], screen = await _resolve([(x, y)], screen, image_coords)
    result = await current().executor.click(x, y, button=button, clicks=clicks, screen=screen)
    return await action_reply(f"click_at({x},{y})", result)


@tool(kind="acting")
async def drag_at(x1: Annotated[int, Field(description="Start, pixels from the left.")],
                  y1: Annotated[int, Field(description="Start, pixels from the top.")],
                  x2: Annotated[int, Field(description="End, pixels from the left.")],
                  y2: Annotated[int, Field(description="End, pixels from the top.")],
                  screen: Screen = None, image_coords: ImageCoords = False,
                  ) -> list[Content]:
    """Drag from (x1,y1) to (x2,y2) in exact pixel coordinates, e.g. a
    slider, a selection, or a file onto a folder. Same coordinate rules as
    click_at: screen pixels, or image_coords=true for pixels from the last
    screenshot.
    """
    [(x1, y1), (x2, y2)], screen = await _resolve(
        [(x1, y1), (x2, y2)], screen, image_coords)
    result = await current().executor.drag(x1, y1, x2, y2, screen=screen)
    return await action_reply(f"drag_at ({x1},{y1}) → ({x2},{y2})", result)


@tool(kind="acting")
async def mouse_move_at(x: X, y: Y,
                        screen: Screen = None, image_coords: ImageCoords = False,
                        ) -> list[Content]:
    """Move the pointer to exact pixel coordinates, e.g. to hover a tooltip.
    Same coordinate rules as click_at: screen pixels, or image_coords=true
    for pixels from the last screenshot.
    """
    [(x, y)], screen = await _resolve([(x, y)], screen, image_coords)
    result = await current().executor.move_to(x, y, screen=screen)
    return await action_reply(f"mouse_move_at({x},{y})", result)


@tool(kind="acting")
async def scroll_at(x: X, y: Y,
                    direction: Annotated[str, Field(description='"up", "down", "left" or "right".')],
                    amount: Annotated[int, Field(description="Scroll ticks.")] = 3,
                    screen: Screen = None, image_coords: ImageCoords = False,
                    ) -> list[Content]:
    """Scroll at exact pixel coordinates, with no grounder involved. The
    scroll goes to whatever is under the pointer, so aim at the scrollable
    area itself. Same coordinate rules as click_at: screen pixels, or
    image_coords=true for pixels from the last screenshot.
    """
    [(x, y)], screen = await _resolve([(x, y)], screen, image_coords)
    result = await current().executor.scroll(x, y, direction, amount=amount, screen=screen)
    return await action_reply(f"scroll_at({x},{y}) {direction} x{amount}", result)
