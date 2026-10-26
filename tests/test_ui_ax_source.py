"""The macOS ``ElementSource``: the reads it makes, and the ones it refuses to.

`_AXSource` talks to pyobjc, so these build one without `__init__` and hand it
fake AX calls — the guards under test are about *which* attributes get read,
which is exactly what the fakes can see.
"""

from __future__ import annotations

import pytest

pytest.importorskip("conductor_desktop.core.backends.macos")

from conductor_desktop.core.backends import macos


def _source(copy_attr, cls=None):
    src = object.__new__(cls or macos._AXSource)
    src._copy = copy_attr
    src._settable = lambda el, attr, _: (0, False)
    return src


# ── Never copy a long AXValue ──────────────────────────────────────
# PyObjC never releases the CFString from AXUIElementCopyAttributeValue, so
# copying a Terminal text area (~1M chars of scrollback) to slice it leaked
# ~10 MB per walk.

def test_long_text_value_is_not_copied(monkeypatch):
    requested = []

    def copy_attr(el, attr, _):
        requested.append(attr)
        return (0, "x" * 1_000_000)

    monkeypatch.setattr(macos, "_ax_string_prefix", lambda el, limit: "prefix")
    assert _source(copy_attr)._value(object(), 1_000_000) == "prefix"
    assert "AXValue" not in requested


def test_short_value_is_read_directly():
    src = _source(lambda el, attr, _: (0, "hello"))
    assert src._value(object(), 5) == "hello"


def test_an_unmeasured_value_is_still_capped():
    # AXNumberOfCharacters is absent on plenty of elements; the slice is the
    # backstop for the ones that answer with something long anyway.
    src = _source(lambda el, attr, _: (0, "y" * 10_000))
    assert len(src._value(object(), None)) == macos._AX_VALUE_MAX


# ── Grid coordinates are a cell's cost, not every node's ───────────

def _describe(role, ranges=None):
    """describe() one element whose AXRole is `role`; returns (raw, attrs read)."""
    read: list[str] = []
    ranges = ranges or {}

    def copy_attr(el, attr, _):
        read.append(attr)
        return (0, ranges.get(attr))

    src = _source(copy_attr)
    src._copy_multi = lambda el, attrs, n, _: (0, [role if a == "AXRole" else None
                                                  for a in attrs])
    src.normalize = lambda raw: ("other", [], [])
    return src.describe(object()), read


def test_a_cell_reads_its_index_ranges():
    raw, read = _describe("AXCell", {"AXRowIndexRange": None})
    assert "AXRowIndexRange" in read and "AXColumnIndexRange" in read


@pytest.mark.parametrize("role", ["AXRow", "AXButton", "AXGroup", "AXStaticText"])
def test_everything_else_pays_nothing_for_them(role):
    # Verified live: an AXOutline's 100 AXRows report neither range while all
    # 600 of their AXCells do, so two round trips per non-cell buy nothing.
    _raw, read = _describe(role)
    assert "AXRowIndexRange" not in read and "AXColumnIndexRange" not in read
