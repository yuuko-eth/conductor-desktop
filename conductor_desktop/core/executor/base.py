"""The executor interface: ``HostExecutor``, ``ActionResult`` and the boundary
that turns backend exceptions into failed results."""

from __future__ import annotations

import base64
import io
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import BackendError

if TYPE_CHECKING:
    from ..models.host_node import HostNode

log = logging.getLogger(__name__)


def resize_screenshot_b64(b64: str, max_width: int) -> str:
    """Resize a base64 PNG screenshot if wider than max_width.

    Returns the resized image as base64 PNG. Preserves aspect ratio.
    If max_width is 0 or the image is already smaller, returns the original.
    """
    if not b64 or max_width <= 0:
        return b64

    from PIL import Image

    img_bytes = base64.b64decode(b64)
    img = Image.open(io.BytesIO(img_bytes))

    if img.width <= max_width:
        return b64

    ratio = max_width / img.width
    new_height = int(img.height * ratio)
    img = img.resize((max_width, new_height), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class ActionResult:
    """Result of executing a tool call on a host."""
    success: bool = True
    output: str = ""
    error: str = ""
    screenshot_b64: str = ""  # post-action screenshot if captured


def _exc_str(e: BaseException) -> str:
    """Never return an empty string — a bare NotImplementedError has str(e)=''."""
    return str(e) or repr(e) or type(e).__name__


async def _attempt(work: Awaitable[Any]) -> tuple[Any, ActionResult | None]:
    """The executor boundary: ``(value, None)``, or ``(None, failed ActionResult)``.

    A BackendError's message is written for the agent. Anything else is a
    backend bug: it's logged with its traceback and reported by type.
    """
    try:
        return await work, None
    except BackendError as e:
        return None, ActionResult(success=False, error=_exc_str(e))
    except Exception as e:
        log.exception("backend call failed")
        return None, ActionResult(success=False, error=f"{type(e).__name__}: {_exc_str(e)}")


async def _call(work: Awaitable[Any], render: Callable[[Any], str] = str) -> ActionResult:
    """A backend call as an ActionResult, its value rendered as the output."""
    value, failed = await _attempt(work)
    return failed or ActionResult(success=True, output=render(value))


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


@dataclass
class PermissionReport:
    """Result of permission self-test on connect."""
    screenshot_works: bool = False
    screenshot_detail: str = ""
    input_works: bool = False
    input_detail: str = ""
    semantic_works: bool = False
    semantic_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.screenshot_works and self.input_works

    def summary(self) -> str:
        parts = []
        if not self.screenshot_works:
            parts.append(f"Screenshot: FAIL ({self.screenshot_detail})")
        if not self.input_works:
            parts.append(f"Input: FAIL ({self.input_detail})")
        if parts:
            return "; ".join(parts)
        return "All permissions OK"


class HostExecutor(ABC):
    """Abstract interface for host observation and action execution."""

    def __init__(self, host: HostNode) -> None:
        self.host = host
        self.permissions = PermissionReport()

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to host. Returns True on success."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection."""
        ...

    @abstractmethod
    async def capture_screenshot(self, screen: int | str | None = None) -> str:
        """Capture screenshot, return base64-encoded PNG.

        ``screen`` selects a specific monitor by ``list_screens()`` index
        or screen ``name``; ``None`` (default) captures the full virtual
        desktop spanning all monitors. On single-monitor hosts ``None``,
        ``0``, and ``"primary"`` are all equivalent no-ops. Raises
        BackendError for an unknown screen or a failed capture.
        """
        ...

    @abstractmethod
    async def get_screen_dimensions(self, screen: int | str | None = None) -> tuple[int, int]:
        """Return (width, height) in pixels for the selected monitor.

        ``None`` returns the full virtual-desktop dimensions. Raises
        BackendError for an unknown screen.
        """
        ...

    # ---- Pointer input ----
    # ``screen`` (if set) means (x,y) are screen-local; the executor adds
    # the screen's origin before injecting the absolute coords the input
    # backend expects. ``None`` means (x,y) are already absolute virtual-
    # desktop coords (legacy behavior).

    @abstractmethod
    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1,
                    screen: int | str | None = None) -> ActionResult:
        ...

    @abstractmethod
    async def move_to(self, x: int, y: int,
                      screen: int | str | None = None) -> ActionResult:
        ...

    @abstractmethod
    async def scroll(self, x: int, y: int, direction: str, amount: int = 3,
                     screen: int | str | None = None) -> ActionResult:
        ...

    @abstractmethod
    async def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 20, hold_ms: int = 300,
                   screen: int | str | None = None) -> ActionResult:
        """Drag from (x1,y1) to (x2,y2) with intermediate steps.

        hold_ms: how long to hold at source before moving (ms).
            Gives the window manager time to register a drag gesture.
        steps: number of intermediate mouse-move steps.
            Smooths the drag so the WM can track it and update drop targets.
        """
        ...

    # ---- Keyboard input ----

    @abstractmethod
    async def type_text(self, text: str, literal: bool = False) -> ActionResult:
        """``literal``: key by key, never through the clipboard."""
        ...

    @abstractmethod
    async def key_press(self, key: str) -> ActionResult:
        ...

    @abstractmethod
    async def hotkey(self, keys: list[str]) -> ActionResult:
        ...

    # ---- Touch/gesture input (mobile + touchscreen) ----

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> ActionResult:
        """Swipe from (x1,y1) to (x2,y2) with controlled velocity."""
        # Default: delegate to drag (desktop has no velocity concept)
        return await self.drag(x1, y1, x2, y2)

    async def long_press(self, x: int, y: int, duration_ms: int = 800) -> ActionResult:
        """Press and hold at (x,y) for duration_ms."""
        return ActionResult(success=False, error="Long press not available on this host")

    async def pinch(self, cx: int, cy: int, scale: float, velocity: float = 1.0) -> ActionResult:
        """Pinch gesture centered at (cx,cy). scale<1 = pinch in, scale>1 = pinch out."""
        return ActionResult(success=False, error="Pinch not available on this host")

    # ---- Semantic operations (when host agent available) ----

    async def exec_shell(self, command: str, timeout: float = 30) -> ActionResult:
        return ActionResult(success=False, error="Shell not available on this host")

    async def get_clipboard(self) -> ActionResult:
        return ActionResult(success=False, error="Clipboard not available")

    async def set_clipboard(self, text: str) -> ActionResult:
        return ActionResult(success=False, error="Clipboard not available")

    async def focus_window(self, title: str | None = None, pid: int | None = None) -> ActionResult:
        return ActionResult(success=False, error="Window management not available")

    async def windows(self) -> list[dict]:
        """The host's windows (see ``list_windows``); raises BackendError."""
        raise BackendError("window listing is not available on this host")

    async def screens(self) -> list[dict]:
        """The host's monitors (see ``list_screens``); raises BackendError."""
        raise BackendError("screen listing is not available on this host")

    async def scene_graph(self) -> dict:
        """The scene-graph DAG (https://docs.conductor-mcp.io/concepts/scene-graph/ → "Scene-graph root"),
        composed from :meth:`windows` and :meth:`screens`. A host whose screens
        can't be listed still gets a graph, with no screen nodes."""
        from ..models.scene_graph import build_scene_graph, scene_graph_to_dict

        windows = await self.windows()
        try:
            screens = await self.screens()
        except BackendError as e:
            log.debug("scene graph without screens: %s", e)
            screens = []
        return scene_graph_to_dict(build_scene_graph(target="host", windows=windows,
                                                     screens=screens))

    async def list_windows(self) -> ActionResult:
        # Each dict has: id, title, process_name, pid, bounds:[x,y,w,h], state,
        # desktop, focused. `focused` is a soft hint — backends compute it
        # differently (macOS: any window of active PID; X11: id == active id;
        # Windows: GetForegroundWindow; GNOME: has_focus). Treat at most one
        # entry as authoritative, and don't assume "exactly one" focused.
        # Optional fields per backend: monitor (int, -1 if off-screen), z
        # (front=0), layer, sticky, workspace. See https://docs.conductor-mcp.io/concepts/scene-graph/.
        return await _call(self.windows(), _json)

    async def list_screens(self) -> ActionResult:
        """Physical screens: index, name, bounds [x,y,w,h], scale, primary."""
        return await _call(self.screens(), _json)

    async def get_scene_graph(self) -> ActionResult:
        return await _call(self.scene_graph(), _json)

    async def ui_tree(self, title: str | None = None, depth: int = 8, max_children: int = 50,
                      include_menubar: bool = False, format: str = "outline",
                      timeout: float = 10.0) -> ActionResult:
        return ActionResult(success=False,
                            error="unsupported: ui_tree is not implemented for this host's platform yet")

    async def ui_find(self, query: str | None = None, role: str | None = None,
                      name: str | None = None, value: str | None = None, id: str | None = None,
                      actionable: bool | None = None, states: list[str] | None = None,
                      title: str | None = None, include_menubar: bool = False, limit: int = 10,
                      match: str = "auto", timeout: float = 10.0) -> ActionResult:
        return ActionResult(success=False,
                            error="unsupported: ui_find is not implemented for this host's platform yet")

    async def ui_act(self, ref: str, verb: str, params: dict | None = None,
                     timeout: float = 5.0) -> ActionResult:
        return ActionResult(success=False,
                            error=f"unsupported: ui_{verb} is not implemented for this host's platform yet")

    async def extract_table(self, title: str | None = None, max_rows: int = 100) -> ActionResult:
        """Read a table/grid from the native a11y tree into the common schema.

        Default: report the provider as unimplemented for this executor rather
        than erroring, so callers can route on the failure state uniformly. See
        protocols.TableExtract.
        """
        from ..protocols import (
            FAILURE_NOT_IMPLEMENTED,
            StructuredCapabilities,
            TableExtract,
        )
        te = TableExtract(0, 0, [], "none", StructuredCapabilities(), failure=FAILURE_NOT_IMPLEMENTED)
        return ActionResult(success=True, output=json.dumps(te.to_dict()))
