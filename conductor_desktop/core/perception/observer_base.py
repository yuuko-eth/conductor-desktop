"""AsyncObserver base + the three filter layers.

Backends subclass AsyncObserver: implement `start()` / `stop()`, call
`_emit()` from whatever thread the platform delivers events on. The base
handles thread-safe buffering, ring-cap, drain, coalesce, and importance
cap.

Three layers of filtering live in this file (Layers 2 and 3) and per
backend (Layer 1 — the per-kind retention filter that drops events
before normalization, e.g. CDP attribute changes that aren't aria-*).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable

from .classifier import StateDelta

log = logging.getLogger("conductor.perception")


# Defaults; MCPConfig will override at construction time.
DEFAULT_BUFFER_MAX = 512
DEFAULT_ENVELOPE_CAP = 20

# Kinds that semantically express "the state of this thing is now X" or
# fire repeatedly for the same logical resource. Layer 2 collapses repeats
# for these, keeping only the latest with a repeat_count annotation. Other
# kinds (navigation, console, node_added/removed) preserve all occurrences
# — each is structurally meaningful and rare.
#
# `network` is included because video streams / API retries / chunked
# transfers fire dozens of near-identical responses per second; the
# normalizer assigns source_id = host+path so retries collapse without
# losing the "this resource keeps 4xx-ing" signal (visible via repeat_count).
#
# `window_state_changed` is included because page loads with iframes /
# workers fire DOMContentLoaded + load multiple times per actual page
# transition; coalesce by phase keeps "yes the page loaded" without
# triplicating it.
LAST_WINS_KINDS: frozenset[str] = frozenset({
    "value_changed",
    "state_changed",
    "focus_changed",
    "name_changed",
    "selection_changed",
    "network",
    "window_state_changed",
    # navigation: Page.frameNavigated and Page.navigatedWithinDocument can both fire
    # for the same URL transition. With source_id=url, distinct destinations
    # stay distinct (youtube → /account → google) but exact duplicates
    # collapse. Different URLs in the same frame still yield separate
    # deltas — the brain still sees the journey.
    "navigation",
    # See design_scene_graph.md. An operator swiping through 5 Spaces in 200ms coalesces to the final
    # destination with repeat_count=5 — `source_id` is the destination
    # space_id, so swipes through the same space collapse but distinct
    # destinations stay distinct.
    "view_changed",
})


# Kinds that survive Layer 3 cap unconditionally. Pulled out of the
# scoring pool, kept entirely, and floated to the front of the envelope
# regardless of monotonic_ts — they re-contextualize every event after
# them, so chronological order matters less than visibility-first.
#
# Currently view_changed only (see design D6/D8). Restack changes were
# explicitly rejected from getting a dedicated kind because they fire
# too often; focus_changed + window_state_changed already cover the
# "top of normal layer moved" signal.
PROTECTED: frozenset[str] = frozenset({
    "view_changed",
})


def coalesce(raw: Iterable[StateDelta]) -> list[StateDelta]:
    """Layer 2 — turn-frame coalesce.

    Collapses same-(kind, identity) repeats for state-like kinds into the
    latest occurrence with `extra['repeat_count']`. Preserves order of
    first/latest occurrence so the resulting sequence still reads as a
    narrative (click → navigation → focus settled → value changed).

    Pure function. No side effects on the input list.
    """
    seen: dict[tuple, int] = {}
    out: list[StateDelta] = []
    for d in raw:
        if d.kind in LAST_WINS_KINDS:
            key = d.coalesce_key()
            if key in seen:
                idx = seen[key]
                prev = out[idx]
                prev_count = prev.extra.get("repeat_count", 1)
                d.extra["repeat_count"] = prev_count + 1
                out[idx] = d
                continue
            seen[key] = len(out)
        out.append(d)
    return out


def cap_by_importance(
    deltas: list[StateDelta], cap: int = DEFAULT_ENVELOPE_CAP,
) -> tuple[list[StateDelta], int]:
    """Layer 3 — envelope cap.

    Returns (kept, dropped_count). PROTECTED-kind deltas (currently
    ``view_changed``) bypass the cap entirely AND float to the front of
    the returned list regardless of monotonic_ts — they re-contextualize
    every event that follows. Non-protected deltas keep chronological
    order; over-cap, importance() picks survivors.

    When len(non_protected) <= remaining_cap, returns everything with
    protected-first ordering and dropped=0.

    See design_scene_graph.md → "Filter system" and observer_base.PROTECTED.
    """
    protected = [d for d in deltas if d.kind in PROTECTED]
    regular = [d for d in deltas if d.kind not in PROTECTED]

    # Protected entries always survive — cap only applies to regulars.
    # If protected alone exceeds cap, we still keep them all (they're
    # "PROTECTED" — bypassing cap is the contract). Regulars get 0 slots.
    remaining = max(0, cap - len(protected))

    if len(regular) <= remaining:
        kept = protected + regular
        return kept, 0

    # Score-then-keep-original-order for regulars: pick survivors by
    # score, then restore relative ordering so the temporal narrative
    # reads chronologically after the protected prefix.
    indexed = list(enumerate(regular))
    indexed.sort(key=lambda iv: iv[1].importance(), reverse=True)
    survivors = sorted(indexed[:remaining], key=lambda iv: iv[0])
    kept_regular = [d for _, d in survivors]
    dropped = len(regular) - remaining
    return protected + kept_regular, dropped


class AsyncObserver:
    """Base class for per-source observers.

    Lifecycle:
        observer = SomeObserver(loop, ...)
        await observer.start()   # spawn thread / register hooks
        ...                      # events emit into ring buffer
        deltas, raw, dropped = observer.drain_and_filter()
        await observer.stop()    # tear down hooks / join thread

    Subclasses implement `start()` and `stop()`. They call `_emit()` from
    whichever thread the platform delivers events on; `_emit()` schedules
    the append on the asyncio loop, so the buffer stays single-threaded
    from the consumer's perspective.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        buffer_max: int = DEFAULT_BUFFER_MAX,
        envelope_cap: int = DEFAULT_ENVELOPE_CAP,
    ) -> None:
        self._loop = loop
        # Ring: the deque drops the oldest itself once it is full, so a loud
        # page costs one append per event instead of re-slicing the list.
        self._buffer: deque[StateDelta] = deque(maxlen=buffer_max)
        self._buffer_max = buffer_max
        self._envelope_cap = envelope_cap
        self._raw_seen = 0  # cumulative raw event count, for diag and raw_count
        self._raw_drained = 0  # _raw_seen as of the last drain()

    async def start(self) -> None:
        """Spawn observer thread / register platform hooks. Override."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Tear down hooks / join thread. Must be idempotent. Override."""
        raise NotImplementedError

    @property
    def raw_seen(self) -> int:
        """Events emitted since construction, the ones the ring dropped
        included. The delta diagnostic samples it; `drain_and_filter` reports
        the difference since the last drain."""
        return self._raw_seen

    # ── Producer side ───────────────────────────────────────────────

    def _emit(self, delta: StateDelta) -> None:
        """Thread-safe entry point for backends.

        Backends call this from their native thread (CFRunLoop callback,
        Win32 WindowProc, GLib mainloop, CDP reader). It schedules the
        append on the asyncio loop so the buffer stays consistent without
        a lock.
        """
        try:
            self._loop.call_soon_threadsafe(self._append, delta)
        except RuntimeError:
            # Loop is closed (shutdown race). Drop silently — the event
            # would have been discarded anyway.
            pass

    def _append(self, delta: StateDelta) -> None:
        self._raw_seen += 1
        # maxlen is the ring: the oldest goes once the buffer is full, so a
        # loud page can't OOM us.
        self._buffer.append(delta)

    # ── Consumer side ───────────────────────────────────────────────

    def drain(self) -> list[StateDelta]:
        """Return and clear the raw buffer.

        Use this when you want unfiltered access (verifier-loop journal).
        For the MCP tool envelope, prefer drain_and_filter().
        """
        out = list(self._buffer)
        self._buffer.clear()
        self._raw_drained = self._raw_seen
        return out

    def drain_and_filter(self) -> tuple[list[StateDelta], int, int]:
        """Drain + coalesce + cap. Returns (deltas, raw_count, dropped).

        raw_count is every event emitted since the last drain, the ones the
        ring already dropped included: `len(raw)` caps at buffer_max, which
        under-reports exactly the storm `raw=187 kept=12` exists to report.
        dropped is the Layer 3 overflow.
        """
        seen_before = self._raw_drained
        raw = self.drain()
        raw_count = self._raw_seen - seen_before
        coalesced = coalesce(raw)
        kept, dropped = cap_by_importance(coalesced, cap=self._envelope_cap)
        return kept, raw_count, dropped
