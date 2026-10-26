"""Route A fusion — enrich OmniParser enumeration with window-topology context.

The grounder/OmniParser vision pass answers *"what actionable boxes are on
screen?"* but is blind to *which window each box belongs to* and *whether that
window is even reachable right now*. The scene graph (``get_scene_graph`` →
``WindowNode`` topology) answers exactly that, and — crucially — lets us **clip
the inventory to the surface the agent can actually act on**, which is where the
token savings come from (the raw enumerate sweeps the dock, menu bar, and every
background app).

Everything here is a pure function over two plain dicts — the enumerate payload
elements (already projected to **screen pixels** upstream, see
``runtime.grounding.capture_and_enumerate``) and ``scene_graph_to_dict(...)`` — so it is
unit-testable with synthetic fixtures and never touches the network or a model.

Coordinate contract: element ``x``/``y`` and ``WindowNode.bounds`` ([x,y,w,h])
must share an origin. For the default capture (whole virtual desktop) they do.
When enumeration targets a single ``screen=N`` its coords are screen-local, so
callers pass ``origin=(x,y)`` (that screen's top-left) to shift them back
into the scene's absolute space before the join — see ``screen_origin``.

Occlusion is deliberately *not* modelled: OmniParser only detects visible
pixels, so a covered element is never in the list to begin with. What matters is
**ownership** (``w``) and **reachability** (``blk`` — blocked by a modal), not
pixel overlap.
"""

from __future__ import annotations

from typing import Any

# Windows in these states / layers can't own a visible, clickable pixel — a
# minimized window renders nothing, and the desktop/wallpaper is never a click
# target. Excluding them keeps owner resolution honest.
#
# `overlay`/`always_on_top` matter more than they look: macOS (Ventura+) emits a
# *full-screen* "Dock"-owned window in the overlay band (kCGWindowLayer 20) that
# sits in front of normal windows — left in, it wins ownership of every element
# behind it (observed live: the whole Terminal inventory tagged to "Dock"). We
# keep `normal`/`floating`/`modal` so real windows and dialogs still own their
# elements, and drop the system-chrome bands that only composite over them.
_DEAD_STATES = {"minimized", "hidden"}
_NON_OWNER_LAYERS = {"background", "desktop", "overlay", "always_on_top"}


def _bounds_ok(b: Any) -> bool:
    return isinstance(b, (list, tuple)) and len(b) == 4


def _contains(bounds: Any, x: int, y: int) -> bool:
    """Point-in-rect for a ``[x, y, w, h]`` bounds list (inclusive edges)."""
    if not _bounds_ok(bounds):
        return False
    bx, by, bw, bh = bounds
    return bx <= x <= bx + bw and by <= y <= by + bh


def _visible_windows(scene: dict) -> list[dict]:
    """Owner candidates, sorted front-first (``z=0`` is frontmost).

    Filters out minimized/hidden windows and the wallpaper layer so the first
    window containing a point is the one actually rendered there.
    """
    wins = (scene.get("windows") or {}).values()
    out = [
        w for w in wins
        if (w.get("state") or "").lower() not in _DEAD_STATES
        and (w.get("layer") or "").lower() not in _NON_OWNER_LAYERS
        and _bounds_ok(w.get("bounds"))
    ]
    out.sort(key=lambda w: w.get("z") if w.get("z") is not None else 1_000_000)
    return out


def _owner_of(x: int, y: int, visible: list[dict]) -> dict | None:
    """Front-most visible window whose bounds contain the point, or None."""
    for w in visible:
        if _contains(w.get("bounds"), x, y):
            return w
    return None


def blocked_window_ids(scene: dict) -> set[str]:
    """Ids of windows a currently-active modal is blocking from input.

    Driven by ``WindowNode.modality`` (the modal declares its *block scope*,
    not the blocked windows):
      - ``system`` — blocks everything except the modal and its own children.
      - ``app``    — blocks the modal's process's other windows.
      - ``window`` — blocks the modal's ``parent_window_id`` only.
    """
    wins: dict[str, dict] = scene.get("windows") or {}
    blocked: set[str] = set()
    for m in wins.values():
        mod = (m.get("modality") or "").lower()
        if not mod:
            continue
        mid = m.get("id")
        if mod == "system":
            for wid, w in wins.items():
                if wid != mid and w.get("parent_window_id") != mid:
                    blocked.add(wid)
        elif mod == "app":
            for wid, w in wins.items():
                if wid != mid and w.get("pid") == m.get("pid"):
                    blocked.add(wid)
        elif mod == "window":
            parent = m.get("parent_window_id")
            if parent:
                blocked.add(parent)
    return blocked


def _focused_id(scene: dict) -> str | None:
    for wid, w in (scene.get("windows") or {}).items():
        if w.get("focused"):
            return wid
    return None


def _top_modal(scene: dict) -> dict | None:
    """Front-most visible modal window, or None."""
    modals = [w for w in _visible_windows(scene) if w.get("modality")]
    return modals[0] if modals else None


def resolve_scope(scene: dict, scope: str) -> tuple[Any, dict]:
    """Turn a ``scope`` selector into a clip rect + metadata.

    Returns ``(clip_bounds_or_None, meta)``. A ``None`` clip means "don't
    clip" (whole capture). ``meta`` carries ``scope``/``focus``/``clip_to``
    (window id scoped to) and, for the modal case, ``modal``.

    Selectors:
      - ``active`` (default) — the front-most modal if one is up, else the
        focused window. "Everything I can act on this instant."
      - ``focused`` — the focused window, ignoring modals.
      - ``screen`` / ``all`` — no clip (the pre-fusion behaviour).
      - ``window:<id>`` — that window.
      - ``app:<name>`` — the front-most visible window whose process name
        contains ``<name>`` (case-insensitive).

    The selector's kind is case-insensitive; a window id is matched exactly,
    since ids can be mixed case.
    """
    scope = (scope or "active").strip()
    kind, sep, arg = scope.partition(":")
    kind = kind.lower()
    if not sep:
        scope = kind
    wins: dict[str, dict] = scene.get("windows") or {}
    focus = _focused_id(scene)

    if scope in ("screen", "all"):
        return None, {"scope": scope, "focus": focus, "clip_to": None}

    if sep and kind == "window":
        wid = arg.strip()
        w = wins.get(wid)
        return (w.get("bounds") if w else None), {
            "scope": scope, "focus": focus, "clip_to": (wid if w else None),
        }

    if sep and kind == "app":
        name = arg.strip().lower()
        cand = next(
            (w for w in _visible_windows(scene)
             if name and name in (w.get("process_name") or "").lower()),
            None,
        )
        return (cand.get("bounds") if cand else None), {
            "scope": scope, "focus": focus,
            "clip_to": (cand.get("id") if cand else None),
        }

    if scope == "active":
        modal = _top_modal(scene)
        if modal:
            return modal.get("bounds"), {
                "scope": "active", "focus": focus,
                "clip_to": modal.get("id"), "modal": modal.get("modality"),
            }

    # "focused", or "active" with no modal up.
    w = wins.get(focus) if focus else None
    return (w.get("bounds") if w else None), {
        "scope": scope, "focus": focus, "clip_to": focus,
    }


def screen_origin(scene: dict | None, screen: int | str | None) -> tuple[int, int]:
    """Top-left of ``screen=N`` in the scene's absolute space, else (0, 0).

    Enumeration against a single monitor returns screen-local coords, but
    ``WindowNode.bounds`` are virtual-desktop absolute — shift by this before
    the join. Whole-desktop capture (``screen`` is None) needs no shift.
    """
    if not scene or screen is None:
        return (0, 0)
    try:
        idx = int(screen)
    except (TypeError, ValueError):
        return (0, 0)
    screens = scene.get("screens") or {}
    s = screens.get(str(idx)) or screens.get(idx)
    b = s.get("bounds") if s else None
    if _bounds_ok(b):
        return (int(b[0]), int(b[1]))
    return (0, 0)


def clip_rect_to_image(
    clip: Any,
    origin: tuple[int, int],
    img_w: int,
    img_h: int,
    screen_w: int,
    screen_h: int,
) -> tuple[int, int, int, int] | None:
    """Project a window-bounds rect (screen space) into an image-pixel crop box.

    Used by ``read_screen(scope=…)`` to clip a captured screenshot to one window
    before handing it to the VLM. ``clip`` is ``[x, y, w, h]`` in the scene's
    screen space; the capture is ``img_w × img_h`` pixels of a ``screen_w ×
    screen_h`` screen (they differ when SCREENSHOT_MAX_WIDTH downscales), so we
    scale by the image/screen ratio. ``origin`` shifts a ``screen=N`` capture
    (screen-local) back under the absolute window bounds.

    Returns ``(left, top, right, bottom)`` clamped to the image, or ``None``
    when inputs are unusable or the box collapses — callers fall back to
    describing the whole frame.
    """
    if not _bounds_ok(clip) or not screen_w or not screen_h:
        return None
    bx, by, bw, bh = clip
    ox, oy = origin
    sx = img_w / screen_w
    sy = img_h / screen_h
    left = max(0, int(round((bx - ox) * sx)))
    top = max(0, int(round((by - oy) * sy)))
    right = min(img_w, int(round((bx - ox + bw) * sx)))
    bottom = min(img_h, int(round((by - oy + bh) * sy)))
    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)


def _windows_header(scene: dict, ids: set[str], blocked: set[str]) -> list[dict]:
    """Compact, normalized window descriptors for the ids referenced.

    One row per window (not per element) so element rows stay tiny — they
    reference a window by ``w`` id instead of inlining its metadata.
    """
    wins: dict[str, dict] = scene.get("windows") or {}
    header = []
    for wid in ids:
        w = wins.get(wid)
        if not w:
            continue
        header.append({
            "id": wid,
            "app": w.get("process_name"),
            "title": (w.get("title") or "")[:60],
            "z": w.get("z"),
            "focused": bool(w.get("focused")),
            "modal": w.get("modality"),
            "blocked": 1 if wid in blocked else 0,
        })
    header.sort(key=lambda d: d["z"] if d["z"] is not None else 1_000_000)
    return header


def point_context(scene: dict | None, x: int, y: int) -> dict:
    """Ownership + reachability for a single point (used by ``locate``).

    Returns ``{"w", "app", "blk", "focused"}`` or ``{}`` when there's no
    scene (mobile / snapshot failed) so callers can ``dict.update`` blindly.
    """
    if not scene:
        return {}
    owner = _owner_of(x, y, _visible_windows(scene))
    if owner is None:
        return {}
    wid = owner.get("id")
    return {
        "w": wid,
        "app": owner.get("process_name"),
        "blk": 1 if wid in blocked_window_ids(scene) else 0,
        "focused": bool(owner.get("focused")),
    }


def fuse(
    elements: list[dict],
    scene: dict | None,
    scope: str = "active",
    origin: tuple[int, int] = (0, 0),
) -> tuple[list[dict], list[dict], dict]:
    """Clip an enumerate inventory to ``scope`` and tag each element's owner.

    ``elements`` are enumerate payload dicts carrying at least ``x``/``y``
    (screen-px centers). Returns ``(kept, windows_header, summary)``:
      - ``kept`` — elements inside the scope's clip rect, each gaining
        ``w`` (owner window id) and ``blk`` (1 if that window is modally
        blocked). Original ids/fields are preserved.
      - ``windows_header`` — compact rows for every window referenced.
      - ``summary`` — ``scope``/``focused``/``clipped_to``/``modal``/``fused``.

    Degrades gracefully: with no ``scene`` (mobile, or snapshot failure) it
    returns the elements untouched and ``fused=False`` — same shape as the
    pre-fusion tool, so nothing breaks.
    """
    if not scene:
        return elements, [], {"scope": scope, "fused": False}

    ox, oy = origin
    visible = _visible_windows(scene)
    blocked = blocked_window_ids(scene)
    clip, meta = resolve_scope(scene, scope)

    kept: list[dict] = []
    used: set[str] = set()
    for e in elements:
        ax, ay = e["x"] + ox, e["y"] + oy
        if clip is not None and not _contains(clip, ax, ay):
            continue
        owner = _owner_of(ax, ay, visible)
        wid = owner.get("id") if owner else None
        row = dict(e)
        row["w"] = wid
        row["blk"] = 1 if (wid is not None and wid in blocked) else 0
        kept.append(row)
        if wid:
            used.add(wid)

    if meta.get("clip_to"):
        used.add(meta["clip_to"])
    if meta.get("focus"):
        used.add(meta["focus"])

    summary = {
        "scope": meta.get("scope", scope),
        "focused": meta.get("focus"),
        "clipped_to": meta.get("clip_to"),
        "modal": meta.get("modal"),
        "fused": True,
    }
    return kept, _windows_header(scene, used, blocked), summary
