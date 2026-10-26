"""The running server's state.

One ``State`` per process, installed by ``server._serve_async`` once the
executor, grounders and observers are up. Tools, the tray and the dashboard
read it through :func:`current` / :func:`get` instead of reaching into the
server module.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .audit import AuditLog

if TYPE_CHECKING:
    from .config import MCPConfig
    from .core.executor import HostExecutor
    from .core.perception import AsyncObserver
    from .desktop_lock import DesktopLock
    from .enumerate import Enumerator
    from .grounder import Grounder
    from .managed_enumerator import ManagedEnumerator
    from .tray_state import TrayStateMachine


@dataclass
class State:
    config: MCPConfig
    executor: HostExecutor
    grounder: Grounder
    # Describe backend. Split from `grounder` so a grounding-only ground model
    # (LocateAnything) can coexist with a Qwen3-VL describe model. When
    # describe isn't separately configured this is the SAME object as
    # `grounder` (one server does both); _build_state dedupes the close.
    describe_grounder: Grounder
    # Region/extent backend — serves crop, locate.bbox, and locate(multiple),
    # which all need a real bounding box. When the click `grounder` is
    # point-only (Holo emits a click point, no extent) this aliases
    # `describe_grounder` (a bbox-capable Qwen/Gemma, mandatory in that
    # ensemble); otherwise it aliases `grounder` (qwen/gemma/LocateAnything do
    # boxes). Built in _build_state via `family_is_point_only`; the close path
    # guards with `is` checks since it may alias either of the other two.
    region_grounder: Grounder
    # OmniParser enumeration backend powering the `enumerate_elements` tool
    # (full-screen element list). Self-hosted: an Enumerator over multipart
    # /parse, present only when CONDUCTOR_DESKTOP_OMNIPARSER_URL is set. Managed: a
    # ManagedEnumerator over the gateway's /v1/enumerate, always present (the
    # gateway owns whether a parser is deployed). None when neither applies.
    enumerator: Enumerator | ManagedEnumerator | None
    lock: DesktopLock
    # Per-target rolling describe hints. Key "" = host; key device_id =
    # the corresponding mobile device. Was a single str pre-mobile; the
    # dict scope prevents context smearing when the brain bounces between
    # host and phone (e.g. an Atlantic-City-Sheets hint applied to a
    # Settings.app screenshot on Android).
    rolling_describe_hints: dict[str, str] = field(default_factory=dict)
    # Serializes the spectator → holder upgrade attempt across coroutines.
    # Created lazily in _build_state so it binds to the running loop.
    lock_upgrade_mu: asyncio.Lock | None = None
    # Tray state machine. Set when run via `tray.run_holder`; None for
    # headless / spectator runs. The tool wrapper (tooling.py) publishes
    # start/end events to it so the icon and recent-calls menu stay live.
    tray_state: TrayStateMachine | None = None
    # Operator pause toggle (tray menu). When set, ACTING-classified
    # tools refuse with a clear message; OBSERVING tools pass through.
    # threading.Event so the tray thread can flip it without holding any
    # asyncio lock.
    input_paused: threading.Event = field(default_factory=threading.Event)
    # Per-session perception observers (AT-SPI / AX / WinEvent — added in
    # subsequent slices). drain_deltas() iterates this list to gather
    # platform-wide events. Per-call observers (e.g. CDP, scoped to one
    # web_* tool's connection) are NOT registered here — they're passed
    # explicitly to drain_deltas() so concurrent tool calls don't drain
    # each other's buffers.
    desktop_observers: list[AsyncObserver] = field(default_factory=list)
    # Diagnostic loop for delta-channel back-pressure / leak investigation.
    # Spawned only when CONDUCTOR_DESKTOP_DELTA_DIAG=true; cancelled on shutdown.
    delta_diag_task: asyncio.Task | None = None
    # Mobile (Android via adb, iOS via WDA). None when neither
    # CONDUCTOR_DESKTOP_MOBILE_ANDROID nor CONDUCTOR_DESKTOP_MOBILE_IOS is set.
    # When non-None, the registry holds per-device executors and serves
    # the mobile_* action tools + device_id-routed perception tools.
    mobile: Any | None = None
    # Append-only audit log (one JSON line per tool call). Always present —
    # a disabled AuditLog is a no-op — so the tool wrapper never branches.
    audit: AuditLog = field(default_factory=lambda: AuditLog(enabled=False))
    # The last screenshot sent inline per target (None = the host, else a
    # device id), so `*_at(image_coords=true)` can map image pixels back to
    # input coordinates.
    frames: dict[str | None, Frame] = field(default_factory=dict)


@dataclass(frozen=True)
class Frame:
    """An image the caller was shown, and how it maps to input coordinates.

    ``screen`` is the host monitor it was captured from (None for the whole
    desktop or a phone): coordinates in it are local to that monitor, the
    same convention the ``*_at`` tools use for ``screen=``. ``scale`` is
    native pixels per image pixel, 1.0 when the image wasn't shrunk.
    """
    width: int
    height: int
    native_width: int
    native_height: int
    screen: int | str | None = None

    @property
    def scale(self) -> tuple[float, float]:
        return (self.native_width / self.width, self.native_height / self.height)

    def to_native(self, x: int, y: int) -> tuple[int, int]:
        sx, sy = self.scale
        return (round(x * sx), round(y * sy))


_current: State | None = None

#: Wall-clock start of this server (``time.time()``), for the tray's and the
#: dashboard's uptime line. Set alongside the state.
holder_start_ts: float | None = None


def current() -> State:
    """The installed state. Raises before the server has finished starting."""
    if _current is None:
        raise RuntimeError("MCP server state not initialized")
    return _current


def get() -> State | None:
    """The installed state, or None before startup / after shutdown."""
    return _current


def install(state: State | None, *, started_at: float | None = None) -> None:
    global _current, holder_start_ts
    _current = state
    if started_at is not None:
        holder_start_ts = started_at
