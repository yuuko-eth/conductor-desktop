"""Grounding: a target description → screen-pixel elements, plus the caveats
and ambiguity replies built around them."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..core.platform_detect import detect_icon_env
from ..grounder import CoordSpace, Grounder, GrounderError, bbox_to_px, family_is_point_only
from ..scene_fusion import point_context, screen_origin, z_known
from ..state import current
from .capture import capture_for_grounder, mobile_executor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenElement:
    """A single grounder match in screen-pixel coordinates.

    `position` is a synthesized screen-region anchor ("top-right", "center",
    …) derived from the center against the coordinate frame at projection
    time. It's the disambiguation fallback for grounders whose `description`
    is a weak handle: LocateAnything's `<ref>` just echoes the queried phrase,
    so N matches come back with identical labels but distinct positions.
    """
    bbox: tuple[int, int, int, int]
    description: str
    position: str = ""
    # True when `bbox` was synthesized around a click point (see
    # GroundedElement.point) — the only point-only signal that survives a
    # managed gateway.
    point: bool = False

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


def screen_region(cx: int, cy: int, frame_w: int, frame_h: int) -> str:
    """Map a center point to a 3×3 screen-region label ("top-left", "center",
    "center-right", …). Used to synthesize spatial anchors when the grounder's
    own labels don't distinguish candidates. Frame is the coordinate space the
    point lives in (screen px for locate/click, image px for crop)."""
    if frame_w <= 0 or frame_h <= 0:
        return ""
    col = "left" if cx < frame_w / 3 else ("right" if cx > 2 * frame_w / 3 else "center")
    row = "top" if cy < frame_h / 3 else ("bottom" if cy > 2 * frame_h / 3 else "middle")
    if row == "middle" and col == "center":
        return "center"
    if row == "middle":
        return f"center-{col}"          # center-left | center-right
    if col == "center":
        return f"{row}-center"          # top-center | bottom-center
    return f"{row}-{col}"               # top-left | top-right | bottom-left | bottom-right


def project_bbox(bbox: tuple[int, int, int, int], *, to: tuple[int, int],
                 frm: tuple[int, int],
                 space: CoordSpace) -> tuple[tuple[int, int, int, int], str]:
    """One backend box in the frame the caller wants, with the 3x3 region its
    centre lands in.

    ``frm`` is the image the backend saw and ``to`` the destination frame:
    the screen for locate and click, the capture itself for crop.
    """
    x1, y1, x2, y2 = bbox_to_px(bbox, to[0], to[1], frm[0], frm[1], space)
    return (x1, y1, x2, y2), screen_region((x1 + x2) // 2, (y1 + y2) // 2, *to)


def disambiguating_anchors(elements: list[ScreenElement]) -> list[str]:
    """One distinct positional anchor per element. Region labels alone can
    collide (two dock icons both "bottom-center"); ties within a region are
    broken by a left-to-right, top-to-bottom ordinal so every handle the brain
    sees is unique even when the geometry clusters."""
    from collections import defaultdict
    anchors = [e.position for e in elements]
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, region in enumerate(anchors):
        groups[region].append(idx)
    for region, idxs in groups.items():
        if len(idxs) > 1 and region:
            ordered = sorted(idxs, key=lambda k: elements[k].center)
            for rank, k in enumerate(ordered, 1):
                anchors[k] = f"{region} (#{rank} of {len(idxs)}, left-to-right)"
    return anchors


async def ground_to_screen_elements(target: str, device_id: str | None = None,
                                     screen: int | str | None = None,
                                     grounder: Grounder | None = None,
                                     ) -> list[ScreenElement]:
    """Ground a target and project every match into screen-pixel coords.

    Returns [] when nothing matches. Returns multiple entries when the target
    description was ambiguous; the caller decides whether to auto-resolve
    (via `resolve_one`) or surface the candidates to the brain (e.g.
    `locate(multiple=True)`).

    Bbox coords are screen-local when ``screen=`` is set — same coordinate
    space as the capture they were grounded against — so the brain can
    forward them straight to ``click_at(..., screen=N)``.

    `grounder` selects the backend: defaults to the point/click `grounder`
    (correct for click, locate(single), wait_for). Region ops that need a real
    bbox extent — locate(multiple) — pass `region_grounder` so they still work
    when the click grounder is point-only (Holo). See server.py's
    `region_grounder`.
    """
    st = current()
    g = grounder if grounder is not None else st.grounder
    shot_b64, img_w, img_h = await capture_for_grounder(
        device_id=device_id, screen=screen,
    )
    elements = await g.ground(shot_b64, target)
    if not elements:
        return []
    if device_id:
        screen_w, screen_h = await mobile_executor(device_id).get_dimensions()
    else:
        screen_w, screen_h = await st.executor.get_screen_dimensions(screen=screen)
    out: list[ScreenElement] = []
    for el in elements:
        bbox, position = project_bbox(el.bbox, to=(screen_w, screen_h),
                                      frm=(img_w, img_h), space=CoordSpace.NORM1000)
        out.append(ScreenElement(bbox=bbox, description=el.description,
                                 position=position, point=el.point))
    return out


@dataclass(frozen=True)
class EnumeratedScreenElement:
    """One OmniParser element projected into screen-pixel coordinates.

    Mirrors `ScreenElement` (screen-px bbox + synthesized region anchor) but
    carries the enumeration-specific `kind`/`interactive`/`content` fields the
    grounder path doesn't have.
    """
    bbox: tuple[int, int, int, int]
    kind: str
    interactive: bool
    content: str
    position: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


async def scene_snapshot(device_id: str | None = None) -> dict | None:
    """Best-effort scene-graph dict for Route-A fusion — host only.

    Returns ``None`` on mobile (``device_id`` set) or any failure, so callers
    treat fusion as optional enrichment that never breaks the base tool. Kept
    tiny and called *right after* the screenshot in the enumerate path so the
    window topology can't drift from the frame it's annotating.
    """
    if device_id:
        return None
    try:
        return await current().executor.scene_graph()
    except Exception as e:  # enrichment only: any failure means "no scene"
        log.debug("scene snapshot unavailable: %s", e, exc_info=True)
        return None


async def capture_and_enumerate(
    device_id: str | None = None,
    screen: int | str | None = None,
    *,
    annotate: bool = False,
) -> tuple[list[EnumeratedScreenElement], str | None, dict | None]:
    """Enumerate every element on screen and project each into screen px.

    Captures via the same path as the grounder (so SCREENSHOT_MAX_WIDTH and
    the device/screen selectors apply identically), calls the OmniParser
    backend, then maps each image-pixel bbox into screen pixels through the
    shared `bbox_to_px` scaler. Also snapshots the scene graph immediately
    after the capture (host only) so `scene_fusion` can clip/annotate the
    inventory without the window topology drifting from the frame. Returns
    `(elements, annotated_b64_or_None, scene_dict_or_None)`.

    Raises GrounderError if the enumerator isn't configured (the tool is only
    registered when it is, so this is a defensive guard) or the backend is
    unreachable.
    """
    st = current()
    if st.enumerator is None:
        raise GrounderError(
            "enumeration backend not configured — set "
            "CONDUCTOR_DESKTOP_OMNIPARSER_URL to a running OmniParser server."
        )
    shot_b64, img_w, img_h = await capture_for_grounder(
        device_id=device_id, screen=screen,
    )
    # Capture-once: grab window topology now, before the ~1s OmniParser round
    # trip, so a window that moves mid-parse can't mis-own elements.
    scene = await scene_snapshot(device_id)
    # Icon labeling is keyed on the desktop/icon-theme of the screen we shot.
    # For the host that's detect_icon_env(); for a mobile device_id the host
    # env doesn't apply (and the ref library has no mobile refs), so leave it
    # off and let labeling no-op.
    if device_id:
        os_id, theme = None, None
    else:
        os_id, theme = detect_icon_env()
    elements, annotated = await st.enumerator.parse(
        shot_b64, annotate=annotate, os_id=os_id, theme=theme,
    )
    if not elements:
        return [], annotated, scene
    if device_id:
        screen_w, screen_h = await mobile_executor(device_id).get_dimensions()
    else:
        screen_w, screen_h = await st.executor.get_screen_dimensions(screen=screen)
    out: list[EnumeratedScreenElement] = []
    for el in elements:
        # OmniParser returns image-pixel xyxy.
        bbox, position = project_bbox(el.bbox, to=(screen_w, screen_h),
                                      frm=(img_w, img_h), space=CoordSpace.IMAGE_PIXELS)
        out.append(EnumeratedScreenElement(
            bbox=bbox, kind=el.kind, interactive=el.interactive,
            content=el.content, position=position,
        ))
    return out, annotated, scene


def format_ambiguity(target: str, elements: list[ScreenElement]) -> str:
    """The brain-facing error when N>1 matches come back. Each candidate's
    spatial anchor is the only handle a text-only agent has to rewrite the
    target on retry — list them clearly.

    When the grounder's own labels distinguish the candidates (Qwen/Gemma
    emit 5–12 word spatial descriptions), use them. When they don't — a
    grounding-only model like LocateAnything echoes the queried phrase, so
    every candidate's label is identical — fall back to synthesized
    screen-region anchors ("top-right", "center", …) so the brain still gets
    distinct handles to disambiguate on retry."""
    labels = [e.description.strip() for e in elements]
    nonempty = [lbl for lbl in labels if lbl]
    labels_distinct = len(set(nonempty)) == len(elements)
    region_anchors = disambiguating_anchors(elements)
    lines = []
    for i, e in enumerate(elements, 1):
        cx, cy = e.center
        if labels_distinct:
            anchor = labels[i - 1]
        else:
            anchor = region_anchors[i - 1] or labels[i - 1] or "(no anchor)"
        lines.append(f"  {i}. \"{anchor}\" at ({cx},{cy})")
    return (f"multiple matches for '{target}' ({len(elements)} found); "
            "rewrite the target with one of these spatial anchors "
            f"(e.g. \"{target} in the {region_anchors[0] or 'top-left'}\"):\n"
            + "\n".join(lines))


def is_point_hit(e) -> bool:
    """Was this hit's bbox synthesized around a click point?

    Reads the element's own flag, set at `_point_to_element` — the single
    site where a synthetic bbox is minted, and shared by every point-emitting
    family. This is what survives a managed gateway, where `grounder_family`
    is "managed" and `family_is_point_only` tells us nothing about which model
    actually served the request. Callers that know the configured family can
    still OR it in; the flag alone is authoritative at runtime.
    """
    return bool(getattr(e, "point", False))


def _owner_z_known(ctx: dict, scene: dict | None) -> bool:
    owner = ((scene or {}).get("windows") or {}).get(ctx.get("w"))
    # Unit callers pass a ctx without its window in the scene; trust it then.
    return owner is None or z_known(owner)


def grounding_caveats(point_hit: bool, ctx: dict,
                       scene: dict | None) -> list[str]:
    """Agent-facing caveats for a single grounded hit.

    The grounder cannot tell us how much to trust a hit, but the surrounding
    state often can, and it is free — we already fuse each hit with its owning
    window. Each caveat names the recovery rather than just the symptom, the
    same contract `GrounderError` keeps for transport failures.

    The point-only caveat is the load-bearing one. A single-best grounder
    answers *every* query with its one best guess: it never emits N candidates
    (so the ambiguity refusal cannot fire) and in practice rarely emits
    no-match (so an absent target still returns a confident point). Seen on
    the managed ensemble: `locate("the Blender 3D modelling
    viewport")`, with Blender not running, returned three *different* dock
    coordinates across three runs. Nothing in the payload distinguished that
    from a real match, so we say so rather than imply a confidence we do not
    have.
    """
    out: list[str] = []
    if point_hit:
        out.append(
            "single-best grounder: it returns one guess per query and reports "
            "neither ambiguity nor no-match, so a result is not evidence the "
            "target exists. For a broad or possibly-absent target, confirm with "
            "read_screen or enumerate_elements before acting on these coords."
        )
    # No owning window: the point landed on the desktop, the dock, or an
    # overlay the scene graph does not own. Legitimate for dock/menubar
    # targets, and the signature of a hallucinated hit — the caller has the
    # intent to tell those apart, so report rather than refuse.
    if scene and not ctx:
        out.append(
            "grounded point is inside no known window (desktop, dock, or an "
            "unowned overlay) — expected for dock/menubar targets, otherwise "
            "verify with read_screen before clicking."
        )
    # Only when a listed window holds focus: with the Windows Start menu, the
    # taskbar or another unlisted overlay focused, every window under the
    # point is "background" and the warning would send the agent the wrong way.
    # And only when the owner's stacking is known: without it the owner is a
    # guess (xfdesktop, under xfwm4 with xprop failing), not a finding.
    elif ctx and not ctx.get("focused") and _owner_z_known(ctx, scene) and any(
            w.get("focused") for w in ((scene or {}).get("windows") or {}).values()):
        out.append(
            f"grounded into a background window ({ctx.get('app')}), not the "
            "focused one — focus_window first, or confirm that is the window "
            "you meant."
        )
    if ctx.get("blk"):
        out.append(
            "the owning window is blocked by a modal — a click will not reach "
            "it; dismiss the modal first."
        )
    return out


async def hit_caveats(e: ScreenElement, screen: int | str | None = None,
                       device_id: str | None = None,
                       scene: dict | None = None) -> list[str]:
    """`grounding_caveats` for a hit whose scene we haven't snapshotted yet.

    Costs one `get_scene_graph` on the grounded-action path. That is small
    beside the screenshot + grounder round-trip it follows, and it has to
    happen *before* the action fires: afterwards the window topology is the
    post-click one, which cannot tell us what we were about to click into.
    """
    if scene is None:
        scene = await scene_snapshot(device_id)
    ox, oy = screen_origin(scene, screen)
    x, y = e.center
    point_hit = is_point_hit(e) or family_is_point_only(
        current().config.grounder_family)
    return grounding_caveats(point_hit, point_context(scene, x + ox, y + oy),
                              scene)


async def resolve_one(target: str, device_id: str | None = None,
                       screen: int | str | None = None
                       ) -> ScreenElement | str:
    """Apply the 0/1/N policy. Returns a single ScreenElement on a unique
    match, or an error string (without `error: ` prefix) on 0 or N matches.
    Callers wrap the string in `_err(...)` and append observation as needed.
    """
    elements = await ground_to_screen_elements(
        target, device_id=device_id, screen=screen,
    )
    if not elements:
        return f"could not locate '{target}'"
    if len(elements) > 1:
        return format_ambiguity(target, elements)
    return elements[0]
