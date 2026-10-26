"""Observation / inspection tools — read-only screen + grounder access.

Six always-on tools: ``capture_screen``, ``locate``, ``crop``,
``read_screen``, ``wait``, ``wait_for`` — plus an optional
``describe_image`` (arbitrary image on disk → prose), gated on
``CONDUCTOR_DESKTOP_DESCRIBE_IMAGE`` so text-only-brain-plus-webcam hosts can
opt in without cluttering the default surface for everyone else.

None of these inject input, so none takes the desktop input lock. All the
screen-facing tools accept the optional ``device_id`` and ``screen``
selectors per the schema documented in design_scene_graph.md.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
from typing import Annotated

from mcp.types import TextContent
from pydantic import Field

from ..core.errors import best_effort
from ..core.executor import resize_screenshot_b64
from ..grounder import CoordSpace, GroundedElement, family_is_point_only
from ..runtime.capture import (
    FrameTarget,
    capture_b64,
    capture_for_grounder,
    emit_png,
    png_dims,
    slugify,
)
from ..runtime.grounding import (
    ScreenElement,
    format_ambiguity,
    ground_to_screen_elements,
    grounding_caveats,
    is_point_hit,
    project_bbox,
    scene_snapshot,
)
from ..runtime.observe import hint_key, post_action_observation, rolling_hint
from ..scene_fusion import (
    clip_rect_to_image,
    point_context,
    resolve_scope,
    screen_origin,
)
from ..state import current
from ..tooling import Content, ToolError, tool

log = logging.getLogger(__name__)

DeviceId = Annotated[str | None, Field(description=(
    "A phone's serial from mobile_list_devices; omit for this computer's screen."))]
Screen = Annotated[int | str | None, Field(description=(
    'Monitor index or name from list_screens, or "primary"; omit for the whole '
    "desktop. Coordinates are then local to that monitor. Not with device_id."))]
Scope = Annotated[str, Field(description=(
    'What is in frame: "screen" (everything), "active" (the front-most dialog, '
    'else the focused window), "focused", "window:<id>" or "app:<name>". Host '
    "only; falls back to the whole screen when window bounds are unavailable."))]
Target = Annotated[str, Field(description=(
    'The element, as "{thing} in {place}", e.g. "Save button in the Export dialog".'))]


@tool(kind="observing")
async def capture_screen(
    full_res: Annotated[bool, Field(description=(
        "true: send the inline image at native resolution, skipping the size "
        "cap. Disk copies are always native."))] = False,
    device_id: DeviceId = None,
    screen: Screen = None,
) -> list[Content]:
    """Take a screenshot and return the image itself: the way to look at the
    screen when you need pixels, or when no describe model is configured for
    read_screen. An image smaller than the screen says so in a text line with
    the scale; to act on a point read off it, pass image_coords=true to
    click_at and the other *_at tools. For a text description use read_screen;
    for one region, crop or web_crop.

    Depending on the server's screenshot mode the reply is the image inline
    (the default; long edge capped at CONDUCTOR_DESKTOP_PAYLOAD_MAX_EDGE, 1568
    px), a path to a native-resolution PNG on disk to open with your
    file-reading tool, or both. In text-only mode a description comes first.
    """
    b64 = await capture_b64(device_id, screen=screen)
    png = base64.b64decode(b64)
    img_w, img_h = png_dims(png)
    label = "screen" if device_id is None else f"screen-{slugify(device_id)}"
    if screen is not None:
        label = f"{label}-{screen}"
    out: list[Content] = []
    if current().config.text_only:
        desc_b64 = b64
        max_w = current().config.screenshot_max_width
        if max_w > 0:
            desc_b64 = await asyncio.to_thread(resize_screenshot_b64, b64, max_w)
        desc = await current().describe_grounder.describe(desc_b64, hint=rolling_hint(device_id))
        if desc:
            out.append(TextContent(type="text", text=f"[{label}] {desc}"))
    out.extend(await emit_png(png, label, img_w, img_h, full_res=full_res,
                              frame=FrameTarget(device_id, screen)))
    return out


@tool(kind="observing")
async def locate(
    target: Target,
    multiple: Annotated[bool, Field(description=(
        'true: return every match as {"matches": [...]} with bounding boxes '
        "instead of refusing on several; for repeated UI such as rows, cards "
        "or tabs."))] = False,
    device_id: DeviceId = None,
    screen: Screen = None,
) -> str:
    """Find one element by description and return its screen coordinates
    without clicking: {"x", "y", "bbox"} plus window tags. Needs a grounder
    model and errors without one. Refuses when the description matches several
    elements, listing them so you can be more specific. Read the `warn` list
    before acting on the point: a single-best grounder always returns a guess,
    so a point alone doesn't prove the target is on screen.

    Coordinates are in the space the coordinate tools take: pass them to
    click_at / drag_at (with the same `screen=`), or to mobile_tap /
    mobile_swipe for a phone.

    Host results carry `w` (owning window id), `app`, `focused` (is it the
    focused window) and `blk` (1 when a modal blocks that window, so a click
    won't reach it). Single-match replies add `candidates`, the number the
    grounder offered: 1 means it claimed uniqueness, not that uniqueness was
    checked. Under a point-only grounder the bbox is a marker around the point,
    flagged by `extent: "point"`; use crop or multiple=True for a real extent.

    With multiple=True each match also has `description` (the grounder's
    label) and `position`, a screen-region handle such as "top-right" that
    stays distinct even when the grounder echoes the query.
    """
    st = current()
    # Enumeration (multiple=True) is a region op — it needs real bbox extents
    # and N matches, which a point-only click grounder (Holo) can't give. Route
    # it to the region backend. Single-target stays on the precise point grounder.
    region = st.region_grounder if multiple else None
    elements = await ground_to_screen_elements(
        target, device_id=device_id, screen=screen, grounder=region,
    )
    if not elements:
        raise ToolError(f"could not locate '{target}'")
    # Route-A fusion: tag each hit with its owning window + reachability so the
    # agent knows whether the target is in the focused window or behind a modal
    # / in a background app. Best-effort (host only); never breaks locate.
    scene = await scene_snapshot(device_id)
    ox, oy = screen_origin(scene, screen)
    if multiple:
        matches = [
            {
                "x": e.center[0], "y": e.center[1],
                "bbox": list(e.bbox),
                "description": e.description,
                # Synthesized screen-region handle — the only distinct anchor
                # when the grounder echoes the query (LocateAnything).
                "position": e.position,
                **point_context(scene, e.center[0] + ox, e.center[1] + oy),
            }
            for e in elements
        ]
        return json.dumps({"matches": matches})
    if len(elements) > 1:
        raise ToolError(format_ambiguity(target, elements))
    e = elements[0]
    x, y = e.center
    out = {"x": x, "y": y, "bbox": list(e.bbox)}
    ctx = point_context(scene, x + ox, y + oy)
    out.update(ctx)
    # How many candidates the grounder actually offered. Always emitted, so a
    # confident unique match is distinguishable from an arbitrary 1-of-many
    # pick — which it otherwise is not, since the N-refusal below only fires
    # at len>1 and a single-best grounder can never produce that input.
    out["candidates"] = len(elements)
    # Under a point-only grounder (Holo) x,y are precise but the bbox is a tiny
    # synthetic box around the point, not the element's real extent. Flag it so
    # the agent doesn't mistake the ~17px box for a real one — and point it at
    # the tools that do give a real extent.
    point_hit = is_point_hit(e) or family_is_point_only(
        st.config.grounder_family)
    if point_hit:
        out["extent"] = "point"
        out["note"] = ("bbox is a point marker, not the element's real extent "
                       "(point-only grounder); use crop or locate(multiple=True) "
                       "for a real region")
    warn = grounding_caveats(point_hit, ctx, scene)
    if warn:
        out["warn"] = warn
    return json.dumps(out)


@tool(kind="observing")
async def crop(
    target: Target,
    padding: Annotated[int, Field(description=(
        "Pixels added on every side of the element's box."))] = 16,
    scope: Scope = "screen",
    device_id: DeviceId = None,
    screen: Screen = None,
) -> list[Content]:
    """Return the region of the screen around a described element as an image,
    plus its bbox in screen pixels. Use it to look closely at one panel, chart
    or control without a full screenshot. Needs a grounder model that returns
    bounding boxes, and errors without one. Refuses when the description
    matches several elements, listing them.

    A narrowed `scope` means a match in a background window can't win, and
    the grounder sees fewer pixels; the bbox is in whole-screen pixels either
    way. The image follows the same screenshot mode as capture_screen.

    On dashboards with obfuscated class names, pair it with web_mark, which
    outlines an element for crop to find:

        web_mark(contains_text="Top countries", climb=3)
        crop(target="the card outlined in magenta")
    """
    b64 = await capture_b64(device_id, screen=screen)
    # Scope: clip to one window before grounding. The offset keeps the
    # reported bbox in whole-capture pixels.
    box, clip_note, _ = await _scope_box(scope, device_id, screen,
                                         *png_dims(base64.b64decode(b64)))
    if box:
        b64 = await asyncio.to_thread(_crop_b64, b64, box)
    off_x, off_y = (box[0], box[1]) if box else (0, 0)
    raw = base64.b64decode(b64)
    img_w, img_h = png_dims(raw)
    shot_for_ground = b64
    max_w = current().config.screenshot_max_width
    if max_w > 0:
        shot_for_ground = await asyncio.to_thread(resize_screenshot_b64, b64, max_w)
    ground_w, ground_h = png_dims(base64.b64decode(shot_for_ground))

    # crop needs the element's real bbox extent — route to the region
    # backend so it works under a point-only click grounder (Holo),
    # where current().grounder would only return a ~17px point marker.
    raw_elements = await current().region_grounder.ground(shot_for_ground, target)
    if not raw_elements:
        if clip_note:
            app = clip_note.split("=", 1)[1]
            raise ToolError(f"could not locate '{target}' in the {app} window "
                            f'— pass scope="screen" to search the whole screen')
        raise ToolError(f"could not locate '{target}'")

    def _project(el: GroundedElement) -> ScreenElement:
        # Into the capture's own frame: crop returns a region of that image.
        bbox, position = project_bbox(el.bbox, to=(img_w, img_h),
                                      frm=(ground_w, ground_h),
                                      space=CoordSpace.NORM1000)
        return ScreenElement(bbox=bbox, description=el.description, position=position)

    projected = [_project(el) for el in raw_elements]
    if len(projected) > 1:
        raise ToolError(format_ambiguity(target, projected))

    px = projected[0]
    left = max(0, px.bbox[0] - padding)
    top = max(0, px.bbox[1] - padding)
    right = min(img_w, px.bbox[2] + padding)
    bottom = min(img_h, px.bbox[3] + padding)
    label_px = (f"bbox=[{left + off_x},{top + off_y},"
                f"{right + off_x},{bottom + off_y}]")
    png, crop_w, crop_h = await asyncio.to_thread(_crop_png, raw, (left, top, right, bottom))

    out: list[Content] = [
        TextContent(type="text", text=f"crop: {label_px}"
                    + (f" [{clip_note}]" if clip_note else "")),
    ]
    if current().config.text_only:
        crop_b64 = base64.b64encode(png).decode("ascii")
        desc = await current().describe_grounder.describe(
            crop_b64,
            hint=f"Describe this cropped region (originally grounded as: {target}).")
        if desc:
            out.append(TextContent(type="text", text=f"[crop] {desc}"))
    out.extend(await emit_png(png, f"crop-{slugify(target)}", crop_w, crop_h))
    return out


# Magic-byte sniffing for the `describe_image` tool. PNG and JPEG
# are the only formats every VLM we ship today decodes natively from a data
# URL; anything else gets a clear error rather than a confusing 4xx from
# the grounder. Kept tight on purpose — the file mode is for webcam frames
# and similar, not a general image-format zoo.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff",      "image/jpeg"),
)


def _sniff_image_mime(data: bytes) -> str:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    raise ValueError("unsupported image format (only PNG and JPEG are supported)")


def _load_image_file(path: str) -> tuple[str, str]:
    """Read an image from disk, return (base64-encoded bytes, mime type).

    Raises FileNotFoundError if the path is missing or not a regular file,
    and ValueError for any byte stream that doesn't start with a PNG or
    JPEG magic prefix.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    data = p.read_bytes()
    mime = _sniff_image_mime(data)
    return base64.b64encode(data).decode("ascii"), mime


def _scope_wants_clip(scope: str | None) -> bool:
    """True when `scope` asks for a window clip (anything but the whole screen)."""
    return bool(scope) and scope.strip().lower() not in ("screen", "all")


def _crop_png(png: bytes, box: tuple[int, int, int, int]) -> tuple[bytes, int, int]:
    """Crop a PNG to ``box`` (left, top, right, bottom): ``(png, width, height)``.
    Blocking: call through ``asyncio.to_thread``. The ``with`` blocks release
    the decoded pixels at once; repeated multi-MB decodes otherwise fragment
    pymalloc and RSS climbs."""
    from PIL import Image as PILImage
    with PILImage.open(io.BytesIO(png)) as im, im.crop(box) as cropped:
        buf = io.BytesIO()
        cropped.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), cropped.width, cropped.height


def _crop_b64(b64: str, box: tuple[int, int, int, int]) -> str:
    """Crop a base64 PNG to ``box`` → base64 PNG. Blocking, like ``_crop_png``."""
    png, _, _ = _crop_png(base64.b64decode(b64), box)
    return base64.b64encode(png).decode("ascii")


async def _scope_box(scope: str | None, device_id: str | None,
                     screen: int | str | None, img_w: int, img_h: int
                     ) -> tuple[tuple[int, int, int, int] | None, str | None, str | None]:
    """Resolve `scope` to a crop box for a host capture of ``img_w × img_h``.

    Returns ``(box, note, context)``: ``box`` is (left, top, right, bottom) in
    image pixels, ``note`` the ``scope=<app>`` reply tag, ``context`` a phrase
    for the describer. All three are ``None`` for a whole-frame read — the
    "screen"/"all" scope, mobile, or no usable scene graph / window bounds —
    so callers fall back to the full capture instead of failing.
    """
    if device_id is not None or not _scope_wants_clip(scope):
        return None, None, None
    scene = await scene_snapshot(device_id)
    clip, meta = resolve_scope(scene, scope) if scene else (None, {})
    if not clip:
        return None, None, None
    try:
        screen_w, screen_h = await current().executor.get_screen_dimensions(screen=screen)
        box = clip_rect_to_image(clip, screen_origin(scene, screen),
                                 img_w, img_h, screen_w, screen_h)
    except Exception:
        log.debug("scope clip failed; using the full frame", exc_info=True)
        return None, None, None
    if not box:
        return None, None, None
    win = (scene.get("windows") or {}).get(meta.get("clip_to"), {})
    app = win.get("process_name") or "window"
    title = (win.get("title") or "").strip()
    context = f"(cropped to the {app} window" + (f': "{title}"' if title else "") + ")"
    return box, f"scope={app}", context


@tool(kind="observing")
async def read_screen(
    hint: Annotated[str | None, Field(description=(
        'Steer the description, e.g. "the Export dialog". It persists for later '
        'reads of the same screen or phone until changed: "" clears it, omit to '
        "keep the current one."))] = None,
    scope: Scope = "screen",
    device_id: DeviceId = None,
    screen: Screen = None,
) -> str:
    """Describe what is on the screen right now as text, from a vision model:
    the cheap way to orient before acting and to check the result after. Needs
    a describe model and errors without one; then use capture_screen to see
    the image yourself, or ui_tree for native apps. It reports what it can
    detect, not what is fully visible, so don't use it to judge scroll
    position. On a cluttered screen, narrow it with scope or hint.

    scope clips the capture to one window before the model sees it, so the
    text can't wander into background apps or the dock; the reply then starts
    with `[scope=<app>]`. hint steers what the model looks at; scope controls
    what's in frame. Hints are kept per target (the host, or each phone), not
    per monitor, and also steer the descriptions that follow actions in
    text-only mode.

    For an image file rather than the live screen, use describe_image when
    it's enabled.
    """
    st = current()
    key = hint_key(device_id)
    if hint is not None:
        if hint == "":
            st.rolling_describe_hints.pop(key, None)
        else:
            st.rolling_describe_hints[key] = hint
    effective = rolling_hint(device_id)
    b64, img_w, img_h = await capture_for_grounder(device_id=device_id, screen=screen)
    # Scope: clip the capture to one window before describing so the VLM can't
    # confabulate about background apps it can't see. Host screen-mode only;
    # any failure silently falls back to the full frame.
    clip_note = None
    scoped_hint = effective
    box, note, context = await _scope_box(scope, device_id, screen, img_w, img_h)
    if box:
        with best_effort("cropping to the scoped window", log):
            b64 = await asyncio.to_thread(_crop_b64, b64, box)
            clip_note = note
            scoped_hint = f"{effective} {context}".strip() if effective else context
    desc = await st.describe_grounder.describe(b64, hint=scoped_hint)
    if not desc:
        raise ToolError("describer returned empty")
    return f"[{clip_note}] {desc}" if clip_note else desc


# Optional: arbitrary-image → prose. Split out of `read_screen` so the live-
# screen tool stays single-purpose, and gated off by default because most
# hosts drive a vision-capable brain that reads pixels directly — this is for
# the text-only-brain-plus-webcam flow. Enable with CONDUCTOR_DESKTOP_DESCRIBE_IMAGE=true.
@tool(kind="observing", gate="describe_image_enabled")
async def describe_image(
    image_path: Annotated[str, Field(description=(
        "Path to a PNG or JPEG file on this computer."))],
    hint: Annotated[str | None, Field(description=(
        'Steer the description, e.g. "who is in frame". This call only; it '
        "doesn't change read_screen's hint."))] = None,
) -> str:
    """Describe a PNG or JPEG file on disk as text, with a prompt that doesn't
    assume a desktop screenshot. For images from outside the screen, such as a
    webcam frame or a downloaded photo; for the live screen use read_screen.
    Needs a describe model and errors without one; other file formats are
    refused.

    A typical flow grabs a frame first:

        exec_shell("ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 /tmp/cam.jpg")
        describe_image(image_path="/tmp/cam.jpg", hint="who is in frame")
    """
    try:
        b64, mime = _load_image_file(image_path)
    except (FileNotFoundError, ValueError) as e:
        raise ToolError(str(e)) from None
    desc = await current().describe_grounder.describe_image(b64, mime, hint=hint or None)
    if not desc:
        raise ToolError("describer returned empty")
    return desc


# A tool call that sleeps longer than this holds the agent's turn and most
# clients' request timeouts; past it, wait in steps and look in between.
_MAX_WAIT_S = 60.0


@tool(kind="observing")
async def wait(
    seconds: Annotated[float, Field(description=(
        f"How long to sleep, at most {_MAX_WAIT_S:.0f}."))],
) -> list[Content]:
    """Sleep for a number of seconds, then return a fresh screenshot when
    auto-screenshot is on. When you can describe what you're waiting for,
    wait_for is better; a fixed wait is for animations and loads you can't
    describe."""
    slept = min(max(0.0, seconds), _MAX_WAIT_S)
    await asyncio.sleep(slept)
    note = f" (capped at {_MAX_WAIT_S:.0f}s)" if slept < seconds else ""
    return [
        TextContent(type="text", text=f"waited {slept:.2f}s{note}"),
        *await post_action_observation(),
    ]


@tool(kind="observing")
async def wait_for(
    target: Annotated[str, Field(description=(
        'What to wait for, e.g. "Download complete banner in Chrome".'))],
    timeout: Annotated[float, Field(description=(
        f"Seconds before giving up, at most {_MAX_WAIT_S:.0f}. A grounder "
        "call still running at the deadline is cut off."))] = 10.0,
    device_id: DeviceId = None,
    screen: Screen = None,
) -> list[Content]:
    """Wait until a described element appears on the screen, polling the
    grounder, then return a screenshot. Needs a grounder model and errors
    without one. Timing out is an error that still carries the final
    screenshot. If several elements match, the first is reported, in the
    coordinates locate would give."""
    interval = current().config.wait_for_poll_interval
    timeout = min(max(0.0, timeout), _MAX_WAIT_S)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            async with asyncio.timeout_at(deadline):
                elements = await ground_to_screen_elements(
                    target, device_id=device_id, screen=screen)
        except TimeoutError:
            elements = []
        if elements:
            x, y = elements[0].center
            extra = "" if len(elements) == 1 else f" ({len(elements)} matches; first reported)"
            return [
                TextContent(type="text",
                            text=f"found '{target}' at ({x},{y}) after {attempts} attempt(s){extra}"),
                *await post_action_observation(device_id=device_id),
            ]
        if loop.time() + interval >= deadline:
            raise ToolError(
                f"timeout: '{target}' not found after {attempts} attempt(s) in {timeout:.1f}s",
                content=await post_action_observation(device_id=device_id),
            )
        await asyncio.sleep(interval)
