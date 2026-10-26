"""Turn an executor ``ActionResult`` into a tool reply, or a ``ToolError``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from mcp.types import TextContent

from ..tooling import Content, ToolError
from .observe import post_action_observation

if TYPE_CHECKING:
    from ..core.executor import ActionResult


async def action_reply(label: str, result: ActionResult, *,
                       observe: bool = True, device_id: str | None = None,
                       notes: Sequence[Content] = ()) -> list[Content]:
    """The reply to an input action: ``"<label> — <outcome>"``, then ``notes``
    (e.g. grounding caveats), then the post-action observation.

    A failed action raises ToolError carrying the same notes and observation,
    so the agent still sees the screen it failed on.
    """
    after = await post_action_observation(device_id=device_id) if observe else []
    if not result.success:
        raise ToolError(f"{label} — failed: {result.error or 'unknown'}",
                        content=[*notes, *after])
    return [TextContent(type="text", text=f"{label} — {result.output or 'ok'}"),
            *notes, *after]


def output_of(result: ActionResult, *, empty: str = "",
              error: str = "not available") -> str:
    """A read's output text, or ToolError with the backend's error."""
    if not result.success:
        raise ToolError(result.error or error)
    return result.output or empty
