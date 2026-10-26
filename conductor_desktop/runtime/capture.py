"""Screen capture and image payloads: host or mobile device, inline or on disk."""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass

from mcp.types import ImageContent, TextContent

from ..core.executor import resize_screenshot_b64
from ..imaging import png_dims
from ..state import Frame, current
from ..tooling import ToolError


def check_screen_device(device_id: str | None,
                         screen: int | str | None) -> None:
    """Mobile devices have no monitors — reject the combination cleanly."""
    if device_id is not None and screen is not None:
        raise ToolError(
            "screen= is for host monitors only; mobile devices have no "
            "list_screens. Drop one of the two."
        )


async def capture_b64(device_id: str | None,
                       screen: int | str | None = None) -> str:
    """Capture a screenshot from the host (device_id=None) or a mobile device.

    ``screen``: host-only monitor selector (index or screen name). Mutually
    exclusive with ``device_id``.
    """
    check_screen_device(device_id, screen)
    if device_id:
        ex = mobile_executor(device_id)
        try:
            b64 = await ex.screenshot()
        except RuntimeError as e:  # adb timed out
            raise ToolError(f"mobile screenshot failed for device {device_id}: {e}") from None
        if not b64:
            raise ToolError(f"mobile screenshot failed for device {device_id}")
        return b64
    return await current().executor.capture_screenshot(screen=screen)


async def capture_png(device_id: str | None = None,
                       screen: int | str | None = None
                       ) -> tuple[bytes, int, int]:
    """Capture screen as raw PNG bytes plus (width, height)."""
    b64 = await capture_b64(device_id, screen=screen)
    raw = base64.b64decode(b64)
    w, h = png_dims(raw)
    return raw, w, h


async def capture_for_grounder(device_id: str | None = None,
                                screen: int | str | None = None
                                ) -> tuple[str, int, int]:
    """Capture + optional resize for the grounder. Returns (b64, img_w, img_h)."""
    b64 = await capture_b64(device_id, screen=screen)
    max_w = current().config.screenshot_max_width
    if max_w > 0:
        b64 = await asyncio.to_thread(resize_screenshot_b64, b64, max_w)
    w, h = png_dims(base64.b64decode(b64))
    return b64, w, h


def mobile_executor(device_id: str):
    """Resolve a per-device executor or raise a clear ToolError.

    Centralizes the registry-missing / gate-disabled / device-unknown error
    messaging so every mobile_* tool surfaces the same actionable text.

    Strips paired surrounding ASCII quotes (`"…"`, `'…'`) and whitespace —
    small planners often re-quote the serial from a JSON dump, and an
    extra pair of quotes shouldn't render the whole device unreachable.
    """
    reg = current().mobile
    if reg is None:
        raise ToolError(
            "mobile surface disabled — set CONDUCTOR_DESKTOP_MOBILE_ANDROID=true "
            "(or _IOS) and restart the MCP server."
        )
    did = (device_id or "").strip()
    if len(did) >= 2 and did[0] == did[-1] and did[0] in ('"', "'"):
        did = did[1:-1].strip()
    try:
        return reg.get_executor(did)
    except RuntimeError as e:  # transport off, or ambiguous without a device list
        raise ToolError(str(e)) from None


def png_to_image_content(png: bytes) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(png).decode("ascii"),
        mimeType="image/png",
    )


def shrink_png_for_payload(png: bytes, width: int, height: int, max_edge: int
                            ) -> tuple[bytes, int, int]:
    """Resize `png` so its *long* edge fits in `max_edge` px, aspect-
    preserving (LANCZOS). No-op when max_edge <= 0 or the image already fits.

    The long edge, because that's what the model's own image limit is about:
    a 1280×800 laptop screen goes out unscaled, a portrait phone keeps its
    proportions, and a 4K desktop comes down to 1568×882.
    """
    long = max(width, height)
    if max_edge <= 0 or long <= max_edge:
        return png, width, height
    scale = max_edge / long
    new_w = max(1, round(width * scale))
    new_h = max(1, round(height * scale))
    from PIL import Image as PILImage
    # `with` closes the source Image (and its BytesIO) deterministically;
    # the resized copy is independent and gets GC'd at function return.
    # Without this, repeated multi-MB decodes fragment pymalloc until RSS
    # climbs unbounded.
    with PILImage.open(io.BytesIO(png)) as img:
        resized = img.resize((new_w, new_h), PILImage.LANCZOS)
    buf = io.BytesIO()
    try:
        resized.save(buf, format="PNG", optimize=True)
    finally:
        resized.close()
    return buf.getvalue(), new_w, new_h


_SHOT_COUNTER = 0


def slugify(s: str, max_len: int = 32) -> str:
    """Safe filename fragment — alphanumerics + dashes, capped length."""
    import re
    slug = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return slug[:max_len] or "item"


def save_png(png: bytes, label: str) -> str:
    """Persist a PNG to the configured screenshot dir, return the absolute path."""
    import os
    import time
    global _SHOT_COUNTER
    _SHOT_COUNTER += 1
    os.makedirs(current().config.screenshot_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}-{_SHOT_COUNTER:04d}-{label}.png"
    path = os.path.join(current().config.screenshot_dir, filename)
    with open(path, "wb") as f:
        f.write(png)
    return path


async def emit_png(png: bytes, label: str, width: int, height: int,
                   *, full_res: bool = False, frame: FrameTarget | None = None
                   ) -> list[TextContent | ImageContent]:
    """Turn a PNG into MCP content per the configured screenshot_mode.

    - inline: image content only (long edge capped at payload_max_edge)
    - file:   write native-res PNG to disk, return a text pointer
    - both:   image content + text pointer; disk copy always native res

    Pass full_res=True to skip the inline resize for a specific call.

    ``frame`` marks a capture of a whole screen the coordinate tools act on
    (not a crop): the image is remembered for ``image_coords=true``, and a
    shrunk inline copy carries a line saying how it maps to input pixels.
    """
    mode = current().config.screenshot_mode
    if mode == "file":
        # No ImageContent sent — skip the resize entirely.
        path = save_png(png, label)
        _remember(frame, width, height, width, height)
        return [TextContent(
            type="text",
            text=f"{label} → {path} ({width}x{height}); Read this path to view.",
        )]
    payload_max = 0 if full_res else current().config.payload_max_edge
    # Off the event loop: the resize and the optimized re-encode of a
    # full-desktop capture take a noticeable fraction of a second.
    inline_png, inline_w, inline_h = await asyncio.to_thread(
        shrink_png_for_payload, png, width, height, payload_max)
    _remember(frame, inline_w, inline_h, width, height)
    out: list[TextContent | ImageContent] = [png_to_image_content(inline_png)]
    if frame is not None and (inline_w, inline_h) != (width, height):
        out.append(TextContent(type="text", text=_scale_note(
            label, frame, inline_w, inline_h, width, height)))
    if mode == "inline":
        return out
    # both
    path = save_png(png, label)
    if (inline_w, inline_h) != (width, height):
        ptr_text = (
            f"{label} → {path} (full {width}x{height}, "
            f"inline {inline_w}x{inline_h}); Read this path for full res."
        )
    else:
        ptr_text = f"{label} → {path} ({width}x{height}); Read this path to view."
    return [*out, TextContent(type="text", text=ptr_text)]


@dataclass(frozen=True)
class FrameTarget:
    """What a whole-screen capture was of: the host (``device_id`` None),
    optionally one monitor of it, or a phone."""
    device_id: str | None = None
    screen: int | str | None = None


def _remember(target: FrameTarget | None, w: int, h: int,
              native_w: int, native_h: int) -> None:
    if target is None:
        return
    current().frames[target.device_id] = Frame(
        width=w, height=h, native_width=native_w, native_height=native_h,
        screen=target.screen)


def _scale_note(label: str, target: FrameTarget, w: int, h: int,
                native_w: int, native_h: int) -> str:
    tools = ("mobile_tap / mobile_swipe" if target.device_id is not None
             else "click_at / drag_at / scroll_at / mouse_move_at")
    return (
        f"[{label}] image is {w}x{h}, shrunk from {native_w}x{native_h} "
        f"(native = image × {native_w / w:.3f}). {tools} take native pixels: "
        f"multiply, or pass image_coords=true to give pixels from this image."
    )


def frame_to_native(device_id: str | None, screen: int | str | None,
                    points: list[tuple[int, int]]
                    ) -> tuple[list[tuple[int, int]], int | str | None]:
    """Map points in the last image of a target to input coordinates.

    Returns the points and the ``screen`` they're local to. A ``screen`` the
    caller passed must match the image's, or the mapping would be wrong.
    """
    f = current().frames.get(device_id)
    if f is None:
        what = "this device" if device_id is not None else "the screen"
        raise ToolError(
            f"image_coords=true needs a screenshot of {what} to map from, and "
            "there isn't one yet: call capture_screen first.")
    if screen is not None and str(screen) != str(f.screen):
        raise ToolError(
            f"image_coords=true maps from the last screenshot, which was of "
            f"screen={f.screen!r}, not screen={screen!r}. Omit screen, or "
            "capture that screen first.")
    return [f.to_native(x, y) for x, y in points], f.screen


async def check_pixel_coords(*points: tuple[int, int],
                             screen: int | str | None = None) -> str | None:
    """Reject obviously-bogus coords for `*_at` tools before they reach
    the backend. Backends differ on out-of-bounds: Win32 clamps, xdotool
    clamps, Quartz clamps, ydotool may silently no-op. Validating at the
    server gives consistent failure semantics.

    With ``screen``, the points are local to that screen and must lie on
    it: one past its edge lands on a neighbouring monitor or nowhere.
    Without, the bound is 2× the desktop on every side. Generous enough
    for any plausible multi-monitor layout; tight enough to catch the
    common bug class (model passes the wrong unit — percentage, inches —
    or hallucinates a coord).
    """
    ex = current().executor
    if screen is not None:
        w, h = await ex.get_screen_dimensions(screen)
        for (x, y) in points:
            if not (0 <= x < w and 0 <= y < h):
                return (f"({x},{y}) is off screen {screen!r}, which is {w}x{h}; "
                        "coords with screen= are local to that screen")
        return None
    w, h = await ex.get_screen_dimensions()
    for (x, y) in points:
        if x < -w or x > 2 * w or y < -h or y > 2 * h:
            return (
                f"({x},{y}) outside reasonable bounds for the {w}x{h} "
                f"desktop — pass coords from a screenshot or use the "
                f"target-based tool (mouse_move/click/drag)"
            )
    return None
