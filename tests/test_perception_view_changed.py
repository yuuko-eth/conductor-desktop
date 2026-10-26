"""Phase 3 of the scene-graph rollout — perception channel wiring.

Covers the pure-function filter edits in classifier.py and
observer_base.py: view_changed kind, KIND_SCORE entry, PROTECTED set,
scope-aware coalesce key, cap_by_importance protected-first ordering.

Platform event sources land in Phase 4; this file tests only the
filter plumbing.
"""

from __future__ import annotations

from conductor_desktop.core.perception.classifier import (
    KIND_SCORE,
    StateDelta,
)
from conductor_desktop.core.perception.observer_base import (
    LAST_WINS_KINDS,
    PROTECTED,
    cap_by_importance,
    coalesce,
)

# ── Schema / registry ──────────────────────────────────────────────


def test_view_changed_in_kind_score_and_last_wins_and_protected():
    """view_changed must be wired into all three filter registries."""
    assert "view_changed" in KIND_SCORE
    assert KIND_SCORE["view_changed"] == 110  # outranks navigation (100)
    assert KIND_SCORE["view_changed"] > KIND_SCORE["navigation"]
    assert "view_changed" in LAST_WINS_KINDS
    assert "view_changed" in PROTECTED


def test_origin_scope_defaults_to_none():
    """StateDelta gains an optional origin_scope field (Phase 4 populates it)."""
    d = StateDelta(kind="value_changed", source="atspi", monotonic_ts=0.0)
    assert d.origin_scope is None


# ── Scope-aware coalesce ───────────────────────────────────────────


def _delta(kind, monotonic_ts, *, source_id=None, scope=None, name=None):
    return StateDelta(
        kind=kind, source="atspi", monotonic_ts=monotonic_ts,
        source_id=source_id, name=name, origin_scope=scope,
    )


def test_same_widget_different_spaces_does_not_coalesce():
    """A value_changed on widget X in Space A and again in Space B must
    yield two separate deltas — the user's attention moved spaces, both
    matter."""
    a = _delta("value_changed", 1.0, source_id="widget-1",
               scope={"space_id": "ws:global:0"})
    b = _delta("value_changed", 2.0, source_id="widget-1",
               scope={"space_id": "ws:global:1"})
    out = coalesce([a, b])
    assert len(out) == 2
    assert out[0].origin_scope["space_id"] == "ws:global:0"
    assert out[1].origin_scope["space_id"] == "ws:global:1"


def test_same_widget_same_space_coalesces():
    """Without the space dimension changing, same-widget repeats still
    collapse — coalesce_key's pre-Phase-3 contract is preserved."""
    a = _delta("value_changed", 1.0, source_id="widget-1",
               scope={"space_id": "ws:global:0"})
    b = _delta("value_changed", 2.0, source_id="widget-1",
               scope={"space_id": "ws:global:0"})
    out = coalesce([a, b])
    assert len(out) == 1
    assert out[0].extra["repeat_count"] == 2
    assert out[0].monotonic_ts == 2.0  # last wins


def test_pre_phase4_deltas_without_origin_scope_still_coalesce():
    """Back-compat: observers that haven't been updated to fill
    origin_scope (Phase 4 work) should keep coalescing as before."""
    a = _delta("value_changed", 1.0, source_id="widget-1")
    b = _delta("value_changed", 2.0, source_id="widget-1")
    out = coalesce([a, b])
    assert len(out) == 1
    assert out[0].extra["repeat_count"] == 2


def test_view_changed_swipes_coalesce_by_destination():
    """5 rapid view_changed events sharing a destination space coalesce
    into one with repeat_count=5 (operator swiping through Spaces fast)."""
    swipes = [
        _delta("view_changed", float(i), source_id="ws:global:3")
        for i in range(5)
    ]
    out = coalesce(swipes)
    assert len(out) == 1
    assert out[0].extra["repeat_count"] == 5
    # Latest swipe wins (timestamp 4.0).
    assert out[0].monotonic_ts == 4.0


def test_view_changed_distinct_destinations_stay_distinct():
    """Swiping desktop 0 → 1 → 2 yields three deltas, not one."""
    out = coalesce([
        _delta("view_changed", 1.0, source_id="ws:global:0"),
        _delta("view_changed", 2.0, source_id="ws:global:1"),
        _delta("view_changed", 3.0, source_id="ws:global:2"),
    ])
    assert [d.source_id for d in out] == ["ws:global:0", "ws:global:1", "ws:global:2"]


# ── cap_by_importance: protected-first ─────────────────────────────


def test_protected_bypasses_cap_and_floats_to_front():
    """A view_changed in the middle of 25 regulars (cap=20) must:
    (a) survive — protected bypass cap, (b) appear at index 0 of kept."""
    deltas = []
    # 25 regular events.
    for i in range(25):
        deltas.append(StateDelta(  # noqa: PERF401 — multi-line ctor reads better as a loop
            kind="value_changed", source="atspi", monotonic_ts=float(i),
            source_id=f"widget-{i}",
        ))
    # Insert a view_changed in the middle.
    vc = StateDelta(
        kind="view_changed", source="atspi", monotonic_ts=12.5,
        source_id="ws:global:1",
    )
    deltas.insert(13, vc)
    kept, dropped = cap_by_importance(deltas, cap=20)
    assert len(kept) == 20
    # 1 protected + 25 regular - 19 surviving regulars = 6 dropped.
    assert dropped == 25 - 19
    assert kept[0] is vc  # view_changed at the front
    # The regulars that survive should be chronological after it.
    regular_kept = kept[1:]
    timestamps = [d.monotonic_ts for d in regular_kept]
    assert timestamps == sorted(timestamps)


def test_protected_alone_under_cap_returns_zero_dropped():
    """3 view_changed + 5 regulars with cap=20 → all 8 kept, 0 dropped,
    protected first."""
    deltas = [
        StateDelta(kind="view_changed", source="atspi", monotonic_ts=1.0, source_id="ws:0"),
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=2.0, source_id="w1"),
        StateDelta(kind="view_changed", source="atspi", monotonic_ts=3.0, source_id="ws:1"),
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=4.0, source_id="w2"),
        StateDelta(kind="view_changed", source="atspi", monotonic_ts=5.0, source_id="ws:2"),
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=6.0, source_id="w3"),
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=7.0, source_id="w4"),
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=8.0, source_id="w5"),
    ]
    kept, dropped = cap_by_importance(deltas, cap=20)
    assert len(kept) == 8
    assert dropped == 0
    # Protected first, in the order they appeared in the input.
    assert [d.kind for d in kept[:3]] == ["view_changed"] * 3
    # Regulars after, in chronological order.
    assert [d.kind for d in kept[3:]] == ["value_changed"] * 5


def test_protected_overrun_cap_keeps_all_protected_drops_regulars():
    """When protected alone exceeds cap, all protected still survive and
    no regulars are kept — bypass semantics are absolute."""
    deltas = [
        StateDelta(kind="view_changed", source="atspi", monotonic_ts=float(i),
                   source_id=f"ws:{i}")
        for i in range(25)
    ]
    deltas.append(StateDelta(kind="value_changed", source="atspi",
                             monotonic_ts=99.0, source_id="w-rare"))
    kept, dropped = cap_by_importance(deltas, cap=20)
    assert len(kept) == 25  # all view_changed survive
    assert all(d.kind == "view_changed" for d in kept)
    assert dropped == 1  # the lone regular got dropped


def test_under_cap_zero_drop_path():
    """Sanity: when nothing exceeds cap, the function is identity-shaped
    (modulo protected-first reordering)."""
    deltas = [
        StateDelta(kind="value_changed", source="atspi", monotonic_ts=float(i),
                   source_id=f"w{i}")
        for i in range(5)
    ]
    kept, dropped = cap_by_importance(deltas, cap=20)
    assert kept == deltas
    assert dropped == 0
