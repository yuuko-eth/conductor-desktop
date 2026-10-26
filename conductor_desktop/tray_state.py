"""Tray state machine + recent-calls ring buffer.

Owns the live state the tray icon reflects: which tool calls are in
flight, whether an error happened recently (latch), and the rolling
history of the last N calls (drives the menu's "Recent tool calls"
submenu and — eventually — the dashboard's live stream).

Threading
---------
Tool lifecycle events are published from the asyncio loop running MCP
tools. The tray runs on a separate thread (pystray needs the main
thread on macOS, so the asyncio loop is on the worker thread; on Linux
and Windows it's the inverse — see ``tray.run_holder``). State changes
fire an observer callback synchronously on the publishing thread.
``pystray.Icon.icon = new_image`` is safe to assign cross-thread on all
three backends in practice.

A ``threading.Lock`` guards the buffer + latch fields. Reads from the
tray thread (menu open) take the lock briefly; writes from the asyncio
thread (tool start/end) hold it only long enough to mutate.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .audit import redact

log = logging.getLogger("conductor.mcp.tray_state")


# State machine states. Strings rather than an Enum so the values match
# the asset filename suffix directly (`state_<name>.png`) and round-trip
# clean over JSON for the eventual dashboard SSE channel.
STATE_IDLE = "idle"
STATE_OBSERVING = "observing"
STATE_ACTING = "acting"
STATE_ERRORED = "errored"
STATE_PAUSED = "paused"

ALL_STATES = (
    STATE_IDLE, STATE_OBSERVING, STATE_ACTING, STATE_ERRORED, STATE_PAUSED,
)


# Precedence for current_state when multiple inputs apply at once:
#   errored > paused > acting > observing > idle
#
# Errored wins because it's transient and signals something the
# operator should look at. Paused beats the rest because the operator
# explicitly asked for it; acting under paused is impossible (the
# wrapper refuses ACTING tools when paused), so the only co-occurrence
# is paused + observing, where paused is the right user-facing label
# (they want to know the agent can't *act*; that no observation is
# also blocked is implementation detail).


# Latch tuning (spec §1 "Error debouncing")
ERROR_LATCH_HOLD_S = 2.0       # minimum red-icon dwell after an error
ERROR_REARM_WINDOW_S = 5.0     # within this window, same-tool re-error
                               # does not re-extend the latch


@dataclass
class ToolCall:
    """One row in the recent-calls ring buffer."""
    call_id: str
    tool: str                       # bare name, no MCP prefix
    input_summary: str
    started_at: float               # time.time()
    klass: str                      # "acting" | "observing"
    duration_ms: int | None = None
    success: bool | None = None
    error: str | None = None

    @property
    def in_flight(self) -> bool:
        return self.duration_ms is None


def _summarize_input(args: tuple, kwargs: dict, max_len: int = 80) -> str:
    """One-line summary of a tool's args for the tray menu and dashboard.

    Goes through the audit log's ``redacted`` policy, so typed text, page JS
    and shell input show up as ``<redacted:N chars>`` here too. Positional
    values have no name to classify them by, so only their count is shown.
    """
    try:
        parts = [f"<{len(args)} positional>"] if args else []
        parts += [f"{k}={_truncate_value(v)}"
                  for k, v in redact(kwargs, "redacted").items()]
        s = ", ".join(parts)
    except Exception:
        log.debug("summarizing tool args failed", exc_info=True)
        s = "<unrepr>"
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _truncate_value(v, n: int = 40) -> str:
    s = v if isinstance(v, str) and v.startswith("<redacted:") else repr(v)
    return s if len(s) <= n else s[: n - 1] + "…"


class TrayStateMachine:
    """Owns in-flight set, error latch, and recent-calls buffer.

    The tray icon's displayed state is the value of ``current_state``,
    which is recomputed on every event. The observer fires only when
    the value *changes* — so a string of observing-tool calls collapses
    to a single idle→observing transition.
    """

    def __init__(self, *, ring_size: int = 16) -> None:
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._recent: deque[ToolCall] = deque(maxlen=ring_size)
        self._in_flight: dict[str, ToolCall] = {}
        # Error latch: until this monotonic timestamp, force STATE_ERRORED.
        self._latch_until: float = 0.0
        # Per-tool last-error monotonic timestamp, for re-arm suppression.
        self._last_error_at: dict[str, float] = {}
        # Operator pause toggle (set/cleared from the tray menu thread).
        self._paused: bool = False
        self._observer: Callable[[str], None] | None = None
        self._last_emitted: str = STATE_IDLE
        # Bumped on every start / end / pause, so the tray can rebuild its
        # menu only when there is something new to show.
        self._revision: int = 0

    # --- public API -------------------------------------------------------

    def set_observer(self, fn: Callable[[str], None] | None) -> None:
        """Register a callback that fires when the displayed state changes.

        Called synchronously on whichever thread published the event.
        Pass ``None`` to clear.
        """
        self._observer = fn

    def start(self, tool: str, args: tuple = (), kwargs: dict | None = None,
              *, klass: str = "acting") -> str:
        """Record a tool call starting. Returns a call_id for ``end``.

        ``klass`` is the tool's kind, "acting" or "observing"; an acting call
        in flight turns the icon orange."""
        kwargs = kwargs or {}
        call = ToolCall(
            call_id=uuid.uuid4().hex[:12],
            tool=tool,
            input_summary=_summarize_input(args, kwargs),
            started_at=time.time(),
            klass=klass,
        )
        with self._lock:
            self._in_flight[call.call_id] = call
            self._recent.append(call)
            self._revision += 1
        self._maybe_emit()
        return call.call_id

    def end(self, call_id: str, *, success: bool, error: str | None = None
            ) -> None:
        """Record a tool call finishing (or erroring)."""
        now_mono = time.monotonic()
        with self._lock:
            call = self._in_flight.pop(call_id, None)
            if call is not None:
                call.duration_ms = int((time.time() - call.started_at) * 1000)
                call.success = success
                call.error = error
            self._revision += 1
            if not success:
                tool = call.tool if call is not None else "<unknown>"
                last = self._last_error_at.get(tool, 0.0)
                # Re-arm suppression: if same tool errored within the
                # window, we keep the *original* latch end (don't extend).
                if now_mono - last > ERROR_REARM_WINDOW_S:
                    self._latch_until = max(
                        self._latch_until, now_mono + ERROR_LATCH_HOLD_S
                    )
                self._last_error_at[tool] = now_mono
        self._maybe_emit()

    @property
    def current_state(self) -> str:
        """Compute the state to display right now.

        Precedence: errored > paused > acting > observing > idle.
        See ``ALL_STATES`` for the rationale.
        """
        now_mono = time.monotonic()
        with self._lock:
            if now_mono < self._latch_until:
                return STATE_ERRORED
            if self._paused:
                return STATE_PAUSED
            if not self._in_flight:
                return STATE_IDLE
            for call in self._in_flight.values():
                if call.klass == "acting":
                    return STATE_ACTING
            return STATE_OBSERVING

    def set_paused(self, paused: bool) -> None:
        """Flip the operator-pause flag.

        Called from the tray menu's pause toggle. Triggers an immediate
        observer fire if the displayed state changes.
        """
        with self._lock:
            self._paused = bool(paused)
            self._revision += 1
        self._maybe_emit()

    def recent(self, n: int | None = None) -> list[ToolCall]:
        """Snapshot the most-recent ``n`` calls (newest last).

        The calls are copies: a call still in flight keeps changing in
        here, and a caller rendering the list must not see it change
        under it.
        """
        with self._lock:
            calls = [dataclasses.replace(c) for c in self._recent]
        if n is not None and n < len(calls):
            calls = calls[-n:]
        return calls

    @property
    def revision(self) -> int:
        """A counter that changes whenever the recent calls or pause do."""
        with self._lock:
            return self._revision

    def reclassify_check(self) -> None:
        """Called from a timer to re-emit when the latch expires.

        The state machine is event-driven; without this, an idle period
        right after an error wouldn't fire idle→idle (correct) but the
        red-to-idle transition only happens on the *next* tool event.
        Tray.py runs a small ticker that calls this every ~1s.
        """
        self._maybe_emit()

    # --- internals --------------------------------------------------------

    def _maybe_emit(self) -> None:
        # Read, compare and swap under one lock, so two threads finishing
        # calls at once can't both fire the same transition or leave a stale
        # state as the last one emitted. The observer runs outside it.
        with self._emit_lock:
            new = self.current_state
            if new == self._last_emitted:
                return
            self._last_emitted = new
        obs = self._observer
        if obs is not None:
            try:
                obs(new)
            except Exception:
                log.exception("tray observer raised; continuing")
