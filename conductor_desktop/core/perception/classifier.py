"""Platform-neutral StateDelta dataclass + per-kind formatting.

Backends produce StateDelta instances; the AsyncObserver buffers and
drains them; `_post_action_observation` in the MCP server appends the
formatted block to the next tool envelope.

Eleven kinds, four sources, eight fields — small enough to hold in head,
large enough to round-trip CDP / AT-SPI / AX / WinEvent without loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DeltaKind = Literal[
    "focus_changed",          # focus moved to a new element
    "value_changed",          # text/value mutated
    "state_changed",          # state bit flipped (checked/expanded/enabled)
    "name_changed",           # element name / window title changed
    "selection_changed",      # selection range changed
    "node_added",             # new element / window / dialog appeared
    "node_removed",           # element / window disappeared
    "window_state_changed",   # app activate / minimize / page load
    "navigation",             # CDP-only: page/frame navigated
    "console",                # CDP-only: console error/warning
    "network",                # CDP-only: response received (filtered)
    # See https://docs.conductor-mcp.io/concepts/scene-graph/ → "Perception delta channel
    # integration". Fired when the active
    # Space / virtual desktop / workspace changes. Re-contextualizes
    # every event after it: pulled to the front of the envelope.
    "view_changed",
]

DeltaSource = Literal["cdp", "bidi", "atspi", "ax", "uia"]


# Importance score for Layer 3 cap. Higher = more likely to survive when
# the envelope cap kicks in. Tuned to favor structural / one-shot events
# (navigation, console error, dialog appeared) over the typing-storm class
# (selection_changed, plain value_changed) — the latter compresses well
# under coalesce so a few survivors carry the signal.
KIND_SCORE: dict[str, int] = {
    # Outranks every other kind: a Space/desktop switch re-contextualizes
    # everything that follows, so we never want the cap to drop it on
    # importance grounds even if PROTECTED accounting somehow misses.
    "view_changed": 110,
    "navigation": 100,
    "console": 90,
    "node_added": 80,
    "window_state_changed": 75,
    "node_removed": 60,
    "value_changed": 50,
    "state_changed": 50,
    "focus_changed": 40,
    "name_changed": 30,
    "selection_changed": 20,
    "network": 10,
}


# Truncation budget for the value field. Long values (Sheets cell with
# pasted essay) bloat the envelope without adding signal — first 200 chars
# is enough to confirm "yes, the paste landed."
VALUE_TRUNC = 200

# Truncation budget for URLs in extras. Signed video / API URLs can be
# >1.5 KB each; without this, a single 15s YouTube watch dumps ~10 KB of
# query-string noise into the envelope. 160 chars keeps host + path +
# enough query params to identify the resource.
URL_TRUNC = 160


def _truncate_url(url: str) -> str:
    """Truncate URL with ellipsis, preserving the start (host+path)."""
    if len(url) <= URL_TRUNC:
        return url
    return url[:URL_TRUNC] + "…"


@dataclass
class StateDelta:
    kind: DeltaKind
    source: DeltaSource
    monotonic_ts: float
    # Stable per-source identity captured at emit time. Used by coalesce()
    # as the dedup key. None when the source can't supply one (e.g. some
    # CDP events fire without a backendNodeId); coalesce falls back to
    # (kind, name).
    source_id: str | None = None
    role: str | None = None
    name: str | None = None
    value: str | None = None
    bbox: tuple[int, int, int, int] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # design_scene_graph.md → "Origin scope on every delta". Set so two
    # events on the same widget in different Spaces don't coalesce, and so
    # off-current-space chatter could be scored down. Only view_changed
    # deltas fill it today; every other kind leaves it None.
    # Shape: {"target": str, "screen": int | None, "space_id": str | None}
    origin_scope: dict | None = None

    def __post_init__(self) -> None:
        if self.value is not None and len(self.value) > VALUE_TRUNC:
            self.value = self.value[:VALUE_TRUNC] + "…"

    def importance(self) -> int:
        """Layer 3 cap score. Higher survives.

        Boosts: console errors above warnings, navigation across origins,
        node_added with role=Dialog (modal — high signal), repeated events
        (high count = more activity, slight boost).
        """
        base = KIND_SCORE.get(self.kind, 0)
        if self.kind == "console" and self.extra.get("level") == "error":
            base += 5
        if self.kind == "node_added" and (self.role or "").lower() in {
            "dialog", "alertdialog", "alert"
        }:
            base += 10
        rep = self.extra.get("repeat_count", 1)
        if rep > 1:
            base += min(5, rep // 5)
        return base

    def coalesce_key(self) -> tuple:
        """Dedup key for Layer 2 coalesce.

        Prefer source-native id; fall back to name; finally fall back to
        the delta's identity so unidentifiable events don't collapse onto
        each other.

        Scope-aware: when the observer attached ``origin_scope.space_id``,
        two events on the same widget in different Spaces
        get distinct keys and don't collapse. Same widget in the same
        Space at two timestamps still coalesces as before.
        """
        ident = self.source_id or self.name or f"_{id(self)}"
        space = self.origin_scope.get("space_id") if self.origin_scope else None
        return (self.kind, ident, space)

    def to_text(self) -> str:
        """One-line human-readable form.

        Format is space-separated key=value pairs, quoted only where it
        matters (name/value/url). Brain reads this directly — keep it
        terse and scannable.
        """
        parts: list[str] = [self.kind]
        if self.role:
            parts.append(f"role={self.role}")
        if self.name:
            parts.append(f'name="{self.name}"')
        # Which DOM attribute a state/value/name change is about (CDP, BiDi).
        # Before the value, so `state_changed attr=open value=""` says what
        # opened instead of printing a bare empty value.
        attr = self.extra.get("attr")
        if attr:
            parts.append(f"attr={attr}")
        if self.value is not None:
            parts.append(f'value="{self.value}"')
        # Surface a few well-known extras inline; the rest stay in the dict
        # for verbose-mode debugging. Skip empty strings so we don't emit
        # noise like `method=""` when the field wasn't populated. URLs get
        # truncated — signed video URLs can be >1.5 KB and dwarf everything
        # else in the envelope.
        for k in ("url", "level", "status", "phase"):
            if k in self.extra:
                v = self.extra[k]
                if v == "" or v is None:
                    continue
                if isinstance(v, str):
                    if k == "url":
                        v = _truncate_url(v)
                    parts.append(f'{k}="{v}"')
                else:
                    parts.append(f"{k}={v}")
        rep = self.extra.get("repeat_count", 1)
        if rep > 1:
            parts.append(f"(×{rep})")
        return " ".join(parts)


def format_delta_block(
    deltas: list[StateDelta],
    *,
    turn: int | None = None,
    raw_count: int | None = None,
    dropped: int = 0,
) -> str:
    """Render a list of (already-coalesced, already-capped) deltas as
    a single text block suitable for the MCP tool envelope.

    Header form:
        [deltas turn=42 raw=187 kept=12]
    or, when turn/raw_count not supplied:
        [deltas kept=12]

    Empty cases (no surviving deltas):
        [deltas (no changes)]            when truly quiet (raw_count 0/None)
        [deltas raw=187 kept=0]           when Layer 1+2+3 dropped everything
                                          (still informative — brain knows
                                          there was activity it didn't see)

    Footer (only when dropped > 0):
        [+M dropped]
    """
    if not deltas:
        if raw_count is None or raw_count == 0:
            return "[deltas (no changes)]"
        # Activity happened but nothing survived the filters — surface the
        # raw count so the brain can ask "what did I miss?" if needed.
        return f"[deltas raw={raw_count} kept=0]"
    header_bits = ["deltas"]
    if turn is not None:
        header_bits.append(f"turn={turn}")
    if raw_count is not None:
        header_bits.append(f"raw={raw_count}")
    header_bits.append(f"kept={len(deltas)}")
    lines = ["[" + " ".join(header_bits) + "]"]
    lines.extend(d.to_text() for d in deltas)
    if dropped > 0:
        lines.append(f"[+{dropped} dropped]")
    return "\n".join(lines)
