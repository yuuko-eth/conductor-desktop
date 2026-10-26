"""Scene graph — multi-screen, multi-space, z-ordered window topology.

See https://docs.conductor-mcp.io/concepts/scene-graph/ for the schema rationale and architectural
decisions (D1–D8). This module defines the dataclass shapes plus a
generic ``build_scene_graph`` composer that turns the flat ``list_windows``
+ ``list_screens`` output into the DAG.

The composer is platform-agnostic. Backends that have richer native
data (e.g. macOS CGS Spaces, sway's per-output workspace tree) can
override ``HostExecutor.get_scene_graph`` to provide it; everyone else
gets the heuristic composition from the flat lists.

Output is plain dataclasses; convert to JSON via :func:`scene_graph_to_dict`
for MCP tool returns.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

# ── Layer enum (mirrored from design_scene_graph.md, low→high in z) ──

LAYER_ORDER = (
    "background", "desktop", "normal", "floating", "modal",
    "fullscreen", "always_on_top", "overlay", "tooltip", "menu",
)


@dataclass
class WindowNode:
    """A single window. Mirrors a ``list_windows`` entry.

    All fields are preserved verbatim from the backend's flat output;
    we don't reinterpret the bounds or recompute the layer here. What the
    graph adds is where the window lives: inside a SpaceNode + ScreenNode
    hierarchy.

    ``parent_window_id`` and ``modality`` close the dialog/menu→owner
    edge. Every desktop platform has a native owner pointer
    (X11 ``WM_TRANSIENT_FOR``, GNOME ``meta_window.transient_for``,
    macOS ``kAXParentAttribute`` / ``NSWindow.parentWindow``, Win32
    ``GetWindow(hwnd, GW_OWNER)``, Haiku ``BWindow::Subset``); backends
    that can read it cheaply fill ``parent_window_id``; others emit
    ``None``.

    ``modality`` describes *block scope*, not a yes/no flag — it's a
    property of the modal window itself, scanned by consumers to answer
    "is window X currently blocked?" rather than denormalised onto
    every blocked window. Values:
      - ``None`` — not modal.
      - ``"window"`` — blocks only ``parent_window_id`` (sheet, dialog
        with TRANSIENT_FOR + MODAL state).
      - ``"app"`` — blocks every window of the parent's process
        (NSAlert in app-modal mode, ``B_MODAL_APP_WINDOW_FEEL``,
        EWMH MODAL without TRANSIENT_FOR).
      - ``"system"`` — blocks everything (Win32 UAC,
        ``B_MODAL_ALL_WINDOW_FEEL``).
    """
    id: str
    title: str
    process_name: str
    pid: int
    bounds: list[int]
    state: str
    desktop: int
    focused: bool
    monitor: int
    z: int
    layer: str
    sticky: bool
    workspace: dict[str, Any]
    parent_window_id: str | None = None
    modality: str | None = None


@dataclass
class ScreenNode:
    """A physical screen / monitor.

    index, name, bounds, scale and primary come from ``list_screens``.
    The per-screen state markers (``current_space``, ``focused_window``)
    and the ``spaces`` edge are derived from the windows on this screen
    during composition.

    ``dock`` carries the per-monitor task-strip when the backend can
    detect one (Win Shell_TrayWnd, macOS Dock, X11 ``_NET_WM_WINDOW_TYPE_DOCK``,
    GNOME panel/Dash-to-Dock). Backends that can't recover it (wlroots
    fallback, headless) leave it ``None``. Shape:
      - ``edge`` — ``"top" | "bottom" | "left" | "right"``
      - ``bounds`` — ``[x, y, w, h]`` in the same root-coord space as
        ``ScreenNode.bounds``. When ``hidden`` the bounds are the
        on-screen sliver, NOT the would-be-revealed rect.
      - ``autohide`` — settings flag (enabled in compositor / OS prefs).
      - ``hidden`` — runtime flag (currently not reserving work area).
        ``None`` if the backend can't separate runtime from setting.
    """
    index: int
    name: str
    bounds: list[int]
    scale: float
    primary: bool
    current_space: str | None = None
    focused_window: str | None = None
    spaces: list[str] = field(default_factory=list)
    dock: dict[str, Any] | None = None


@dataclass
class SpaceNode:
    """A workspace / Space / virtual desktop.

    On platforms with global workspaces (X11, GNOME-default, Windows,
    Haiku), each unique ``workspace.global_index`` becomes one SpaceNode
    spanning every screen — ``screen_ids`` lists all screens it appears
    on. On per-output platforms (sway, Hyprland, macOS with separate
    Spaces), each ``(output, name)`` tuple becomes its own SpaceNode
    bound to one screen.

    ``windows`` is the ordered z-stack (z=0 first). ``label_source``
    tracks where ``label`` came from so future label policies (model-
    authored, platform-renamed, etc.) can override correctly.
    """
    id: str
    label: str | None
    screen_ids: list[int]
    windows: list[str]
    label_source: str  # "platform" | "model" | "heuristic"


@dataclass
class SceneGraph:
    """The DAG root. See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Scene-graph root".

    ``screens`` is keyed by the same ``int`` screen index that
    ``active_screen``, ``window.monitor`` and ``space.screen_ids`` use, so
    consumers never have to juggle a stringified key just for this one
    dict. ``json.dumps`` coerces int keys to strings automatically at the
    wire boundary, so the MCP tool output is unchanged.
    """
    target: str
    screens: dict[int, ScreenNode]
    spaces: dict[str, SpaceNode]
    windows: dict[str, WindowNode]
    active_screen: int
    captured_monotonic: float


# ── Label heuristic ────────────────────────────────────────────────


def _heuristic_label(focused_window: WindowNode | None, fallback_index: int) -> str:
    """Cheap auto-label from focused-window content.

    Strategy:
    - Focused window's title (first 40 chars, stripped of trailing
      app-name suffix) → "title"-ish.
    - Else focused process_name → "process"-ish.
    - Else fall back to "Workspace N".

    The full design (`https://docs.conductor-mcp.io/concepts/scene-graph/` → "Label authoring")
    layers platform-native names + model-authored labels on top of this;
    only the heuristic is built.
    """
    if focused_window:
        title = (focused_window.title or "").strip()
        # Trim trailing " — App" / " - App" suffix common in browsers.
        for sep in (" — ", " - ", " | "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        if title:
            return title[:40]
        proc = (focused_window.process_name or "").strip()
        if proc:
            return proc[:40]
    return f"Workspace {fallback_index}"


def _space_ids_for_window(w: dict) -> list[tuple[str, str | None, str | None]]:
    """Compute every ``(space_id, output_name, native_label)`` this window
    belongs to.

    The common case is a single-element list — D2's "window appears once"
    invariant. Haiku is the documented exception: ``BWindow::Workspaces()``
    is a bitmask, so a window can be on workspaces 1+3 but not 2 — D2's
    sticky-or-nothing flag can't represent it. Backends emit
    ``workspace.global_indices: list[int]`` and the composer fans out
    one SpaceNode membership per index.

    See ``_space_id_for_window`` for the legacy single-space shape, kept
    so existing observer code that queries one canonical space_id per
    window keeps working (and per-output / sticky / single-global paths
    still go through it).
    """
    ws = w.get("workspace") or {}
    indices = ws.get("global_indices")
    if (
        indices
        and not ws.get("per_output")
        and not w.get("sticky")
    ):
        return [(f"ws:global:{int(i)}", None, None) for i in indices]
    return [_space_id_for_window(w)]


def _space_id_for_window(w: dict) -> tuple[str, str | None, str | None]:
    """Compute (space_id, output_name, native_label) for a window.

    Returns the canonical SpaceNode id this window belongs to plus, when
    applicable, the per-output identity and any platform-native label.
    Three shapes:
    - Per-output (sway/Hyprland/wlroots-with-IPC): ``ws:<output>:<name|index>``
    - Global (X11/GNOME/Windows/Haiku): ``ws:global:<index>``
    - Sticky on global platforms: ``ws:sticky:<monitor>`` when the window
      has a known monitor, else ``ws:sticky`` as a last-resort catch-all.
      Per-screen bucketing preserves D7's z-scope invariant (z is local
      to each (screen, space, layer)) — without it, sticky windows from
      different monitors would share a single SpaceNode whose z-order
      can't be meaningful.
    """
    ws = w.get("workspace") or {}
    per_out = ws.get("per_output")
    if per_out:
        output = per_out.get("output") or "?"
        native = per_out.get("name")
        idx = per_out.get("index", 0)
        ident = native if native is not None else str(idx)
        return f"ws:{output}:{ident}", output, native
    # Global workspace.
    if w.get("sticky"):
        mon = int(w.get("monitor", -1))
        if mon >= 0:
            return f"ws:sticky:{mon}", None, None
        return "ws:sticky", None, None
    gi = ws.get("global_index")
    if gi is None:
        gi = w.get("desktop", 0)
    return f"ws:global:{gi}", None, None


# ── Composer ────────────────────────────────────────────────────────


def build_scene_graph(
    target: str,
    windows: list[dict],
    screens: list[dict],
) -> SceneGraph:
    """Compose a SceneGraph from the backend's flat window and screen lists.

    Args:
        target: ``"host"`` or a mobile device_id.
        windows: list of window dicts from ``HostExecutor.list_windows``.
        screens: list of screen dicts from ``HostExecutor.list_screens``.

    Returns:
        A SceneGraph DAG. JSON-encode via :func:`scene_graph_to_dict`.

    Composition rules (see design D1–D8):
      - Each unique workspace identity (per-output tuple or global index)
        becomes one SpaceNode.
      - Sticky windows on global platforms go to ``ws:sticky:<monitor>``
        (``ws:sticky`` when the monitor is unknown); per-output sticky is
        unmodelled.
      - SpaceNode.windows is ordered by z (topmost first).
      - ScreenNode.spaces is the union of spaces that have at least one
        window on that screen, OR (for global workspaces) every screen.
      - ScreenNode.focused_window / current_space follow the focused
        flag — at most one per screen.
      - SceneGraph.active_screen is the screen that holds the global
        focused window. Falls back to the primary screen if none focused.
    """
    # Window map (preserves the list_windows dict shape).
    window_nodes: dict[str, WindowNode] = {}
    for w in windows:
        window_nodes[w["id"]] = WindowNode(
            id=w["id"],
            title=w.get("title", ""),
            process_name=w.get("process_name", ""),
            pid=int(w.get("pid", 0) or 0),
            bounds=list(w.get("bounds", [0, 0, 0, 0])),
            state=w.get("state", "normal"),
            desktop=int(w.get("desktop", 0) or 0),
            focused=bool(w.get("focused", False)),
            monitor=int(w.get("monitor", -1)),
            z=int(w.get("z", 0)),
            layer=w.get("layer", "normal"),
            sticky=bool(w.get("sticky", False)),
            workspace=dict(w.get("workspace") or {}),
            parent_window_id=w.get("parent_window_id"),
            modality=w.get("modality"),
        )

    # Screen map (int-keyed; matches every cross-reference's type).
    screen_nodes: dict[int, ScreenNode] = {}
    for s in screens:
        idx = int(s.get("index", -1))
        dock = s.get("dock")
        screen_nodes[idx] = ScreenNode(
            index=idx,
            name=s.get("name", f"Display {idx}"),
            bounds=list(s.get("bounds", [0, 0, 0, 0])),
            scale=float(s.get("scale", 1.0)),
            primary=bool(s.get("primary", False)),
            dock=dict(dock) if isinstance(dock, dict) else None,
        )

    # Discover spaces from windows.
    space_nodes: dict[str, SpaceNode] = {}
    # Track which screens each space appears on (only for per-output spaces).
    per_output_screens: dict[str, set[int]] = {}

    for w in windows:
        # Most windows belong to a single space; Haiku's bitmask
        # workspaces are the documented multi-membership case (D2 caveat).
        for space_id, output, native_label in _space_ids_for_window(w):
            if space_id not in space_nodes:
                # Initial creation. Real screen_ids fill in below.
                space_nodes[space_id] = SpaceNode(
                    id=space_id,
                    label=native_label,
                    screen_ids=[],
                    windows=[],
                    label_source="platform" if native_label else "heuristic",
                )
                per_output_screens[space_id] = set()
            space_nodes[space_id].windows.append(w["id"])
            # For per-output spaces, attribute to the window's monitor.
            if output is not None:
                mon = int(w.get("monitor", -1))
                if mon >= 0:
                    per_output_screens[space_id].add(mon)

    # Resolve screen_ids and finalize labels per space.
    all_screen_ids = sorted(screen_nodes.keys())
    for space_id, sp in space_nodes.items():
        if space_id.startswith("ws:sticky:"):
            # Per-screen sticky bucket — bound to its one monitor.
            try:
                sp.screen_ids = [int(space_id.split(":")[-1])]
            except ValueError:
                sp.screen_ids = list(all_screen_ids)
        elif space_id.startswith("ws:global:") or space_id == "ws:sticky":
            # Global workspaces span every screen (per EWMH/Win32 semantics).
            # The bare ``ws:sticky`` catch-all (sticky window with unknown
            # monitor) also lives on every screen.
            sp.screen_ids = list(all_screen_ids)
        else:
            sp.screen_ids = sorted(per_output_screens.get(space_id, set()))

        # Sort windows by z (topmost first); ties keep insertion order.
        sp.windows.sort(key=lambda wid: window_nodes[wid].z)

        # If no platform label, apply heuristic from the focused window
        # in this space (if any), else from the topmost window.
        if sp.label is None:
            focused_in_space = next(
                (window_nodes[wid] for wid in sp.windows if window_nodes[wid].focused),
                None,
            )
            top_window = window_nodes[sp.windows[0]] if sp.windows else None
            anchor = focused_in_space or top_window
            # Use the desktop/index for fallback numbering.
            fallback_idx = 0
            if space_id.startswith("ws:global:"):
                try:
                    fallback_idx = int(space_id.split(":")[-1])
                except ValueError:
                    pass
            sp.label = _heuristic_label(anchor, fallback_idx)

    # Wire screen.spaces.
    for screen in screen_nodes.values():
        screen.spaces = sorted(
            (space_id for space_id, sp in space_nodes.items()
             if screen.index in sp.screen_ids),
        )

    # Per-screen focused_window + current_space.
    for screen in screen_nodes.values():
        screen_wins = [w for w in windows if int(w.get("monitor", -1)) == screen.index]
        focused = next((w for w in screen_wins if w.get("focused")), None)
        if focused:
            screen.focused_window = focused["id"]
            space_id, _, _ = _space_id_for_window(focused)
            screen.current_space = space_id

    # Global active_screen: screen with the focused window. Fallback to
    # primary screen, else first screen, else -1.
    active_screen = -1
    for screen in screen_nodes.values():
        if screen.focused_window:
            active_screen = screen.index
            break
    if active_screen == -1:
        primary = next((s for s in screen_nodes.values() if s.primary), None)
        if primary:
            active_screen = primary.index
        elif screen_nodes:
            active_screen = next(iter(screen_nodes.values())).index

    return SceneGraph(
        target=target,
        screens=screen_nodes,
        spaces=space_nodes,
        windows=window_nodes,
        active_screen=active_screen,
        captured_monotonic=time.monotonic(),
    )


def scene_graph_to_dict(sg: SceneGraph) -> dict:
    """JSON-friendly dict form (dataclasses.asdict, recursive)."""
    return asdict(sg)
