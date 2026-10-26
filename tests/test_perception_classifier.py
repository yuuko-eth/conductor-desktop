"""Tests for perception.classifier — StateDelta, scoring, formatting."""

from __future__ import annotations

from conductor_desktop.core.perception.classifier import (
    VALUE_TRUNC,
    StateDelta,
    format_delta_block,
)


def _d(**overrides) -> StateDelta:
    """Minimal StateDelta with sane defaults; override any field via kwarg."""
    base = dict(
        kind="focus_changed",
        source="cdp",
        monotonic_ts=0.0,
    )
    base.update(overrides)
    return StateDelta(**base)


# ── value truncation ─────────────────────────────────────────────────

def test_value_truncated_when_too_long():
    d = _d(kind="value_changed", value="x" * (VALUE_TRUNC + 100))
    assert d.value is not None
    assert len(d.value) == VALUE_TRUNC + 1  # ellipsis added
    assert d.value.endswith("…")


def test_value_untruncated_when_short():
    d = _d(kind="value_changed", value="hello")
    assert d.value == "hello"


def test_value_none_stays_none():
    d = _d(kind="focus_changed", value=None)
    assert d.value is None


# ── importance scoring ──────────────────────────────────────────────

def test_navigation_outranks_focus():
    nav = _d(kind="navigation")
    foc = _d(kind="focus_changed")
    assert nav.importance() > foc.importance()


def test_console_error_boosted_over_warning():
    err = _d(kind="console", extra={"level": "error"})
    warn = _d(kind="console", extra={"level": "warning"})
    assert err.importance() > warn.importance()


def test_dialog_node_added_boosted():
    dlg = _d(kind="node_added", role="Dialog")
    div = _d(kind="node_added", role="Generic")
    assert dlg.importance() > div.importance()


def test_repeat_count_modest_boost():
    one = _d(kind="value_changed", extra={"repeat_count": 1})
    many = _d(kind="value_changed", extra={"repeat_count": 50})
    # Boost is bounded; many should win but not by a landslide.
    assert many.importance() > one.importance()
    assert many.importance() - one.importance() <= 5


# ── coalesce key ────────────────────────────────────────────────────

def test_coalesce_key_uses_source_id_when_present():
    a = _d(kind="value_changed", source_id="node-7", name="Edit")
    b = _d(kind="value_changed", source_id="node-7", name="Edit")
    assert a.coalesce_key() == b.coalesce_key()


def test_coalesce_key_falls_back_to_name_without_source_id():
    a = _d(kind="value_changed", source_id=None, name="B5")
    b = _d(kind="value_changed", source_id=None, name="B5")
    assert a.coalesce_key() == b.coalesce_key()


def test_coalesce_key_distinguishes_unidentified_deltas():
    # Two deltas with no source_id and no name must not collapse onto
    # each other — the fallback keys on object identity.
    a = _d(kind="value_changed")
    b = _d(kind="value_changed")
    assert a.coalesce_key() != b.coalesce_key()


def test_coalesce_key_distinguishes_kinds():
    a = _d(kind="focus_changed", source_id="node-7")
    b = _d(kind="value_changed", source_id="node-7")
    assert a.coalesce_key() != b.coalesce_key()


# ── to_text formatting ──────────────────────────────────────────────

def test_to_text_includes_role_name_value():
    d = _d(kind="value_changed", role="Cell", name="B5", value="hello")
    text = d.to_text()
    assert "value_changed" in text
    assert "role=Cell" in text
    assert 'name="B5"' in text
    assert 'value="hello"' in text


def test_to_text_names_changed_attribute_before_value():
    # A dialog opening flips `open` to "" — without attr= the line was a
    # bare `state_changed value=""` that said nothing about what changed.
    d = _d(kind="state_changed", source="bidi", value="", extra={"attr": "open"})
    assert d.to_text() == 'state_changed attr=open value=""'


def test_to_text_url_extra_surfaces_inline():
    d = _d(kind="navigation", extra={"url": "https://example.com"})
    text = d.to_text()
    assert 'url="https://example.com"' in text


def test_to_text_repeat_count_appended():
    d = _d(kind="value_changed", name="B5", value="hi", extra={"repeat_count": 11})
    text = d.to_text()
    assert "(×11)" in text


def test_to_text_omits_repeat_count_when_one():
    d = _d(kind="value_changed", name="B5", value="hi", extra={"repeat_count": 1})
    assert "(×1)" not in d.to_text()


def test_to_text_handles_missing_optional_fields():
    d = _d(kind="navigation")
    # Should not crash; just emits the kind.
    text = d.to_text()
    assert text == "navigation"


def test_to_text_truncates_long_urls():
    long_url = "https://example.com/path?" + "k=v&" * 200  # ~800 chars
    d = _d(kind="network", extra={"url": long_url, "status": 403})
    text = d.to_text()
    # Truncated form ends with ellipsis; must be much shorter than input.
    assert "…" in text
    assert len(text) < 250  # generous; was >800 without truncation


def test_to_text_keeps_short_urls_intact():
    d = _d(kind="navigation", extra={"url": "https://example.com/x"})
    text = d.to_text()
    assert 'url="https://example.com/x"' in text
    assert "…" not in text


# ── format_delta_block ──────────────────────────────────────────────

def test_format_block_header_with_full_metadata():
    deltas = [_d(kind="navigation"), _d(kind="focus_changed", name="B5")]
    block = format_delta_block(deltas, turn=42, raw_count=187, dropped=0)
    lines = block.split("\n")
    assert lines[0] == "[deltas turn=42 raw=187 kept=2]"


def test_format_block_header_minimal():
    block = format_delta_block([_d(kind="navigation")])
    assert block.startswith("[deltas kept=1]")


def test_format_block_empty_truly_quiet():
    # No deltas, no raw activity → terse "(no changes)" line. The brain
    # learns "the channel exists and is silent" without a full header.
    assert format_delta_block([]) == "[deltas (no changes)]"
    assert format_delta_block([], raw_count=0) == "[deltas (no changes)]"


def test_format_block_empty_after_filtering():
    # Activity happened but Layer 1+2+3 ate all of it — surface raw count
    # so the brain knows there was tail noise it didn't see.
    block = format_delta_block([], raw_count=187)
    assert block == "[deltas raw=187 kept=0]"


def test_format_block_dropped_footer():
    deltas = [_d(kind="navigation")]
    block = format_delta_block(deltas, dropped=37)
    assert block.endswith("[+37 dropped]")


def test_format_block_no_dropped_footer_when_zero():
    deltas = [_d(kind="navigation")]
    block = format_delta_block(deltas, dropped=0)
    assert "dropped" not in block
