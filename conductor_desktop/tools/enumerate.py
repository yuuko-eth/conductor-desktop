"""Optional full-screen enumeration tool — backed by OmniParser.

One tool, ``enumerate_elements``, on only when it has a backend: a
self-hosted OmniParser (``CONDUCTOR_DESKTOP_OMNIPARSER_URL``) or managed mode,
where the gateway runs the parser (``MCPConfig.enumerate_enabled``).

This is the fourth perception capability alongside the grounder's
single/region grounding and prose ``read_screen`` — where those answer "where is
*this*?" and "what's on screen?", enumeration answers "give me a clickable
inventory of *everything*".
"""

from __future__ import annotations

import base64
import json
from typing import Annotated

from mcp.types import TextContent
from pydantic import Field

from ..runtime.capture import emit_png, png_dims
from ..runtime.grounding import capture_and_enumerate
from ..scene_fusion import fuse, screen_origin
from ..tooling import Content, tool


@tool(kind="observing", gate="enumerate_enabled")
async def enumerate_elements(
    interactive_only: Annotated[bool, Field(description=(
        "true: only elements detected as clickable; false adds static text."))] = True,
    kind: Annotated[str | None, Field(description=(
        '"icon" or "text" to keep one type; omit for both.'))] = None,
    scope: Annotated[str, Field(description=(
        'What to inventory: "screen" (everything), "active" (the front-most '
        'dialog, else the focused window), "focused", "window:<id>" or '
        '"app:<name>". Ignored on a phone.'))] = "screen",
    annotate: Annotated[bool, Field(description=(
        "true: also return the screenshot with the boxes drawn on it; adds "
        "about 0.7 s."))] = False,
    device_id: Annotated[str | None, Field(description=(
        "A phone's serial from mobile_list_devices; omit for this computer's "
        "screen."))] = None,
    screen: Annotated[int | str | None, Field(description=(
        'Monitor index or name from list_screens, or "primary"; omit for the '
        "whole desktop. Coordinates are then local to that monitor."))] = None,
) -> list[Content]:
    """List every UI element detected on the screen (icons and text, via
    OmniParser) with click coordinates in screen pixels, each tagged with its
    owning window. Use it to survey what's actionable when you can't name a
    target; to find one named element use locate, for prose use read_screen.
    Elements with blk=1 sit behind a modal dialog, so a click won't reach
    them. scope="active" drops the dock, menu bar and background apps.

    Each element: `{"id", "x", "y", "bbox", "type", "interactive",
    "content", "position", "w", "blk"}`. `x`/`y` is the center, ready for
    click_at; `type` is "icon" or "text"; `content` is OCR text or an icon
    caption; `position` is a screen-region anchor; `w` is the owning
    window's id, listed in the `windows` header.

    The reply also carries the `windows` header (one `{id, app, title, z,
    focused, modal, blocked}` row per window referenced) and a summary
    (`scope`, `focused`, `clipped_to`). Detection labels what it sees, so
    confirm a click target with locate when precision matters. On a phone
    there's no window information: scope is ignored and `w`/`blk` are absent.
    """
    elements, annotated_b64, scene = await capture_and_enumerate(
        device_id=device_id, screen=screen, annotate=annotate,
    )

    if kind is not None:
        kind = kind.strip().lower()
        elements = [e for e in elements if e.kind == kind]
    if interactive_only:
        elements = [e for e in elements if e.interactive]

    base = [
        {
            "id": i,
            "x": e.center[0], "y": e.center[1],
            "bbox": list(e.bbox),
            "type": e.kind,
            "interactive": e.interactive,
            "content": e.content,
            "position": e.position,
        }
        for i, e in enumerate(elements)
    ]
    origin = screen_origin(scene, screen)
    kept, windows_header, summary = fuse(base, scene, scope=scope,
                                         origin=origin)
    payload = {
        **summary,
        "windows": windows_header,
        "count": len(kept),
        "elements": kept,
    }
    out: list[Content] = [
        TextContent(type="text", text=json.dumps(payload)),
    ]
    if annotate and annotated_b64:
        png = base64.b64decode(annotated_b64)
        img_w, img_h = png_dims(png)
        out.extend(await emit_png(png, "enumerated", img_w, img_h))
    return out
