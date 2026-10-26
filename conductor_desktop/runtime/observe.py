"""What a tool reply carries after an action: deltas, then a screenshot or a
description, per config."""

from __future__ import annotations

import asyncio
from enum import Enum

from mcp.types import ImageContent, TextContent

from ..core.perception import AsyncObserver, format_delta_block
from ..state import current
from .capture import FrameTarget, capture_for_grounder, capture_png, emit_png, slugify


class DrainScope(Enum):
    """Which observers a tool call's envelope drains.

    The host's AX / AT-SPI / wlroots observers keep accumulating whatever
    the scope: a call that skips them isn't throwing their events away, it
    is leaving them for the next call that wants desktop context.
    """

    #: Host desktop observers plus the caller's own per-call observer.
    ALL = "all"
    #: Only the caller's per-call observer. A Chrome interaction shouldn't
    #: drag a Terminal AXTextArea storm into the envelope.
    CALLER_ONLY = "caller_only"
    #: Nothing. A phone tap shouldn't drag laptop-side noise in either, and
    #: the mobile backends have no observer of their own to drain.
    NONE = "none"

    @staticmethod
    def for_web(include_desktop: bool) -> DrainScope:
        """What a `web_*` tool drains. Defaults to the browser alone."""
        return DrainScope.ALL if include_desktop else DrainScope.CALLER_ONLY

    @staticmethod
    def for_target(device_id: str | None) -> DrainScope:
        """What an action drains, given what it acted on."""
        return DrainScope.ALL if device_id is None else DrainScope.NONE


def hint_key(device_id: str | None) -> str:
    """Map device_id|None to the rolling-hint dict key. None → '' (host)."""
    return device_id or ""


def rolling_hint(device_id: str | None) -> str | None:
    """Read the rolling describe hint scoped to host (None) or a device."""
    return current().rolling_describe_hints.get(hint_key(device_id)) or None


def drain_deltas(extra: AsyncObserver | None = None,
                 scope: DrainScope = DrainScope.ALL) -> list[TextContent]:
    """Drain the in-scope observers and return one TextContent block.

    `extra` is a per-call observer the caller owns, drained under every
    scope but NONE. The explicit-extra pattern keeps per-tool observers
    scoped — two concurrent web_* calls won't drain each other's buffers.

    Returns [] (no envelope contribution) when no observer sources are
    active. Returns one TextContent with `[deltas (no changes)]` when
    sources exist but drained nothing — useful so the brain learns
    "the channel is silent" rather than wondering whether deltas are
    enabled at all.
    """
    if not current().config.deltas_enabled or scope is DrainScope.NONE:
        return []
    sources: list[AsyncObserver] = (
        list(current().desktop_observers) if scope is DrainScope.ALL else []
    )
    if extra is not None:
        sources.append(extra)
    if not sources:
        return []
    all_deltas = []
    total_raw = 0
    total_dropped = 0
    for obs in sources:
        deltas, raw, dropped = obs.drain_and_filter()
        all_deltas.extend(deltas)
        total_raw += raw
        total_dropped += dropped
    return [TextContent(
        type="text",
        text=format_delta_block(all_deltas, raw_count=total_raw, dropped=total_dropped),
    )]


# Pause before the post-action capture. A Calculator digit finished drawing
# 36 ms after the key (measured on Windows 11), and a
# capture with no pause still showed the old value. Longer animations (window
# close ~200 ms, app launch >1 s) aren't waited out: the agent looks again.
POST_ACTION_PAUSE_S = 0.05


async def post_action_observation(device_id: str | None = None
                                   ) -> list[TextContent | ImageContent]:
    """Return the post-action observation, shape depends on config.

    Order: deltas (from any active desktop_observers) → screenshot/describe.
    Brain reads structural-cheap first, falls back to visual-expensive.

    - text_only=True:              deltas + VLM description as TextContent
    - auto_screenshot=True:        deltas + screenshot per screenshot_mode
    - otherwise (the default):     deltas only (driver calls capture_screen() itself)

    `device_id`: when set, the captured frame comes from the mobile device
    rather than the host, and the drain scope narrows to nothing — a phone
    tap shouldn't drag laptop-side noise (focus changes, AXTextArea storms
    from a chat window) into the envelope.
    """
    cfg = current().config
    # Text-only mode is how a model without vision sees results at all, so it
    # observes after every action even with auto_screenshot off.
    observe = cfg.auto_screenshot or cfg.text_only
    if observe:
        await asyncio.sleep(POST_ACTION_PAUSE_S)
    deltas = drain_deltas(scope=DrainScope.for_target(device_id))
    if not observe:
        return deltas
    label = "screen" if device_id is None else f"screen-{slugify(device_id)}"
    if cfg.text_only and not cfg.describe_configured:
        # The action itself succeeded; don't turn it into an error.
        return [*deltas, TextContent(type="text", text=(
            f"[{label}] (no describe model configured, so no description; read the result "
            "with ui_tree / ui_find or capture_screen)"))]
    if cfg.text_only:
        b64, _, _ = await capture_for_grounder(device_id=device_id)
        desc = await current().describe_grounder.describe(b64, hint=rolling_hint(device_id))
        return [
            *deltas,
            TextContent(type="text",
                text=f"[{label}] {desc}" if desc else f"[{label}] (describer returned empty)"),
        ]
    png, w, h = await capture_png(device_id=device_id)
    return [*deltas, *await emit_png(png, label, w, h, frame=FrameTarget(device_id))]
