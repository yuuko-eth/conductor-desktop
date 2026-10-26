"""Tests for perception.observer_base — coalesce, cap, AsyncObserver."""

from __future__ import annotations

import asyncio

import pytest

from conductor_desktop.core.perception.classifier import StateDelta
from conductor_desktop.core.perception.observer_base import (
    AsyncObserver,
    cap_by_importance,
    coalesce,
)


def _d(kind: str = "focus_changed", **extra) -> StateDelta:
    base = dict(
        kind=kind,
        source="cdp",
        monotonic_ts=0.0,
    )
    base.update(extra)
    return StateDelta(**base)


# ── coalesce ────────────────────────────────────────────────────────

def test_coalesce_keeps_event_like_kinds_intact():
    raw = [
        _d("navigation", extra={"url": "https://a"}),
        _d("navigation", extra={"url": "https://b"}),
        _d("console", extra={"level": "error", "text": "boom"}),
    ]
    out = coalesce(raw)
    assert len(out) == 3


def test_coalesce_collapses_value_changed_repeats_last_wins():
    raw = [
        _d("value_changed", source_id="node-7", value="h"),
        _d("value_changed", source_id="node-7", value="he"),
        _d("value_changed", source_id="node-7", value="hel"),
        _d("value_changed", source_id="node-7", value="hell"),
        _d("value_changed", source_id="node-7", value="hello"),
    ]
    out = coalesce(raw)
    assert len(out) == 1
    assert out[0].value == "hello"
    assert out[0].extra["repeat_count"] == 5


def test_coalesce_distinct_elements_dont_collapse():
    raw = [
        _d("value_changed", source_id="node-7", value="a"),
        _d("value_changed", source_id="node-8", value="b"),
        _d("value_changed", source_id="node-7", value="aa"),
    ]
    out = coalesce(raw)
    # node-7 collapses, node-8 stays separate → 2 entries.
    assert len(out) == 2


def test_coalesce_preserves_first_position_in_narrative():
    raw = [
        _d("focus_changed", source_id="A", name="A"),  # pos 0
        _d("navigation", extra={"url": "x"}),          # pos 1
        _d("focus_changed", source_id="A", name="A"),  # collapses to pos 0
    ]
    out = coalesce(raw)
    assert len(out) == 2
    assert out[0].kind == "focus_changed"
    assert out[1].kind == "navigation"


def test_coalesce_node_added_not_collapsed():
    # node_added is event-like: same role appearing twice means two real
    # events (e.g. a modal opened and closed and opened again).
    raw = [
        _d("node_added", role="Dialog", name="Save?"),
        _d("node_added", role="Dialog", name="Save?"),
    ]
    out = coalesce(raw)
    assert len(out) == 2


def test_coalesce_empty():
    assert coalesce([]) == []


def test_coalesce_does_not_mutate_input_list():
    a = _d("value_changed", source_id="x", value="1")
    b = _d("value_changed", source_id="x", value="2")
    raw = [a, b]
    coalesce(raw)
    assert raw == [a, b]
    assert len(raw) == 2


# ── cap_by_importance ──────────────────────────────────────────────

def test_cap_under_threshold_returns_unchanged():
    deltas = [_d("focus_changed") for _ in range(5)]
    out, dropped = cap_by_importance(deltas, cap=10)
    assert out == deltas
    assert dropped == 0


def test_cap_drops_low_score_kinds():
    # 5 navigations (high score) + 5 selection_changed (low score), cap=5.
    # Navigations should survive.
    nav = [_d("navigation", extra={"url": f"u{i}"}) for i in range(5)]
    sel = [_d("selection_changed") for _ in range(5)]
    deltas = nav + sel
    out, dropped = cap_by_importance(deltas, cap=5)
    assert dropped == 5
    assert len(out) == 5
    assert all(d.kind == "navigation" for d in out)


def test_cap_preserves_temporal_order_of_survivors():
    # Mix high/low: nav, sel, nav, sel, nav. Cap=3 should keep all navs
    # in their original positions.
    deltas = [
        _d("navigation", extra={"url": "a"}),    # 0 — keep
        _d("selection_changed"),                  # 1 — drop
        _d("navigation", extra={"url": "b"}),    # 2 — keep
        _d("selection_changed"),                  # 3 — drop
        _d("navigation", extra={"url": "c"}),    # 4 — keep
    ]
    out, dropped = cap_by_importance(deltas, cap=3)
    assert dropped == 2
    assert [d.extra["url"] for d in out] == ["a", "b", "c"]


def test_cap_zero_drops_all():
    deltas = [_d("navigation") for _ in range(3)]
    out, dropped = cap_by_importance(deltas, cap=0)
    assert out == []
    assert dropped == 3


# ── AsyncObserver ──────────────────────────────────────────────────

class _FakeObserver(AsyncObserver):
    """Minimal concrete observer for testing the base class."""

    def __init__(self, loop, **kw):
        super().__init__(loop, **kw)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def test_emit_buffers_delta():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop)
    obs._emit(_d("navigation"))
    # _emit schedules via call_soon_threadsafe; let the loop process it.
    await asyncio.sleep(0)
    assert len(obs._buffer) == 1


async def test_drain_clears_buffer():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop)
    obs._emit(_d("navigation"))
    await asyncio.sleep(0)
    out = obs.drain()
    assert len(out) == 1
    assert obs.drain() == []


async def test_ring_buffer_caps_at_max():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop, buffer_max=3)
    for i in range(10):
        obs._emit(_d("navigation", extra={"url": f"u{i}"}))
    await asyncio.sleep(0)
    out = obs.drain()
    # Most recent 3 retained.
    assert len(out) == 3
    assert [d.extra["url"] for d in out] == ["u7", "u8", "u9"]


async def test_drain_and_filter_returns_raw_count_and_dropped():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop, envelope_cap=2)
    # 3 navigations (event-like, no coalesce) → 3 raw, 2 kept, 1 dropped.
    for i in range(3):
        obs._emit(_d("navigation", extra={"url": f"u{i}"}))
    await asyncio.sleep(0)
    deltas, raw, dropped = obs.drain_and_filter()
    assert raw == 3
    assert len(deltas) == 2
    assert dropped == 1


async def test_drain_and_filter_applies_coalesce():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop)
    # 5 value_changed on same node → coalesces to 1 with repeat_count=5.
    for v in ["a", "ab", "abc", "abcd", "abcde"]:
        obs._emit(_d("value_changed", source_id="node-1", value=v))
    await asyncio.sleep(0)
    deltas, raw, dropped = obs.drain_and_filter()
    assert raw == 5
    assert len(deltas) == 1
    assert dropped == 0
    assert deltas[0].value == "abcde"
    assert deltas[0].extra["repeat_count"] == 5


async def test_emit_after_loop_close_does_not_raise():
    # Race: a backend thread fires _emit() after the asyncio loop has
    # been closed. Must not crash; the event is allowed to drop.
    loop = asyncio.new_event_loop()
    obs = _FakeObserver(loop)
    loop.close()
    # No assertion needed — the test passes if this doesn't raise.
    obs._emit(_d("navigation"))


@pytest.mark.parametrize("method", ["start", "stop"])
async def test_subclass_must_implement_lifecycle(method):
    # AsyncObserver itself is abstract in spirit; calling start/stop on
    # the base raises NotImplementedError.
    loop = asyncio.get_running_loop()
    obs = AsyncObserver(loop)
    with pytest.raises(NotImplementedError):
        await getattr(obs, method)()


async def test_raw_count_reports_events_the_ring_dropped():
    # `len(drained)` caps at buffer_max, which under-reports exactly the
    # storm `raw=187 kept=12` exists to report.
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop, buffer_max=3)
    for i in range(10):
        obs._emit(_d("navigation", extra={"url": f"u{i}"}))
    await asyncio.sleep(0)
    deltas, raw, _ = obs.drain_and_filter()
    assert raw == 10
    assert len(deltas) == 3


async def test_raw_count_counts_from_the_last_drain():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop)
    for i in range(4):
        obs._emit(_d("navigation", extra={"url": f"u{i}"}))
    await asyncio.sleep(0)
    obs.drain_and_filter()
    obs._emit(_d("navigation", extra={"url": "later"}))
    await asyncio.sleep(0)
    _, raw, _ = obs.drain_and_filter()
    assert raw == 1


async def test_raw_seen_is_cumulative_for_the_diagnostic():
    loop = asyncio.get_running_loop()
    obs = _FakeObserver(loop, buffer_max=2)
    for i in range(5):
        obs._emit(_d("navigation", extra={"url": f"u{i}"}))
    await asyncio.sleep(0)
    obs.drain()
    obs._emit(_d("navigation", extra={"url": "u5"}))
    await asyncio.sleep(0)
    assert obs.raw_seen == 6
