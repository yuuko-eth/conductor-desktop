"""StructuredProvider extraction: which nodes are a grid, and what comes out.

The extractor reads walked `UINode`s, so its vocabulary is the shared one —
`role == "cell"`, not "does this platform's role name contain 'cell'". What
each platform's native role normalizes *to* is pinned in test_ui_ax.py /
test_ui_uia.py / test_ui_atspi.py; these tests pin what extraction then does
with it.
"""

from __future__ import annotations

import pytest

from conductor_desktop.core.structured import extract_table
from conductor_desktop.core.ui.model import UINode


def _node(role: str, **kw) -> UINode:
    kw.setdefault("native_role", role)
    return UINode(role=role, **kw)


def _cell(text: str, r: int, c: int, **kw) -> UINode:
    return _node("cell", value=text, row_range=(r, 1), col_range=(c, 1), **kw)


# ── What is a container, and what is a cell ────────────────────────

def test_table_of_cells_extracts():
    table = _node("table", children=[
        _node("row", children=[_cell("Alice", 0, 0), _cell("30", 0, 1)]),
        _node("row", children=[_cell("Bob", 1, 0), _cell("41", 1, 1)]),
    ])
    out = extract_table(table, "ax")
    assert (out.rows, out.cols) == (2, 2)
    assert {(c.r, c.c, c.text) for c in out.cells} == {
        (0, 0, "Alice"), (0, 1, "30"), (1, 0, "Bob"), (1, 1, "41"),
    }
    assert out.source == "ax"
    assert out.capabilities.has_structure and out.capabilities.has_values


def test_a_tree_with_no_cells_is_not_a_table():
    # VS Code's file tree is an AXOutline of rows and nothing else — routing
    # rule 2: gate on cell presence, not on the container's role.
    tree = _node("tree", children=[
        _node("row", children=[_node("static_text", value="src/")]),
        _node("row", children=[_node("static_text", value="tests/")]),
    ])
    out = extract_table(tree, "ax")
    assert out.rows == 0
    assert out.source == "none"


def test_a_cell_outside_a_container_is_not_a_table():
    # A lone cell is not a grid; something has to hold it.
    assert extract_table(_node("group", children=[_cell("x", 0, 0)]), "ax").rows == 0


def test_a_node_with_grid_coordinates_is_a_cell_whatever_its_role():
    # backends/windows.py's tree-grid path synthesizes cells UIA never named,
    # and they arrive with whatever role their ControlType normalized to.
    grid = _node("table", children=[
        _node("row", children=[_node("other", value="Discord", row_range=(0, 1),
                                     col_range=(0, 1)),
                               _node("other", value="12%", row_range=(0, 1),
                                     col_range=(1, 1))]),
    ])
    out = extract_table(grid, "uia")
    assert [c.text for c in out.cells] == ["Discord", "12%"]


def test_an_unknown_source_is_an_error_not_an_empty_table():
    # Silently finding nothing would look exactly like "this app has no table".
    with pytest.raises(ValueError, match="unknown a11y source"):
        extract_table(_node("table"), "atsip")


def test_none_tree_is_empty():
    assert extract_table(None, "ax").rows == 0


# ── Cell text fallthrough ───────────────────────────────────────────

def test_cell_text_falls_through_to_a_static_text_child():
    table = _node("table", children=[
        _node("row", children=[
            _node("cell", row_range=(0, 1), col_range=(0, 1), children=[
                _node("static_text", value="from the child"),
            ]),
        ]),
    ])
    assert extract_table(table, "ax").cells[0].text == "from the child"


def test_cell_text_ignores_a_non_text_child():
    table = _node("table", children=[
        _node("row", children=[
            _node("cell", row_range=(0, 1), col_range=(0, 1), children=[
                _node("button", value="Delete"),
            ]),
        ]),
    ])
    assert extract_table(table, "ax").cells[0].text == ""


# ── Capabilities and geometry ───────────────────────────────────────

def test_zero_area_bounds_are_dropped():
    # Off-screen cells report (x, y, 0, 0) on Linux (matrix §6).
    table = _node("table", children=[
        _node("row", children=[
            _cell("on", 0, 0, bounds=(10, 20, 100, 18)),
            _cell("off", 1, 0, bounds=(10, 9000, 0, 0)),
        ]),
    ])
    out = extract_table(table, "ax")
    boxes = {c.text: c.bbox for c in out.cells}
    assert boxes["on"] == (10, 20, 100, 18)
    assert boxes["off"] is None
    assert out.capabilities.has_geometry


@pytest.mark.parametrize("verbs, can_set", [
    (["set_value"], True),            # a writable spreadsheet cell
    (["press"], True),                # AXOpen / Invoke: act without clicking
    (["toggle"], True),               # a checkbox cell
    (["select", "scroll_to"], False),  # neither changes the cell
    ([], False),
])
def test_can_act_follows_the_cell_verbs(verbs, can_set):
    # The act channel is the normalized verb list, so an action the old
    # substring markers didn't know (AXOpen, AT-SPI "click") counts now.
    table = _node("table", children=[
        _node("row", children=[_cell("x", 0, 0, verbs=list(verbs))]),
    ])
    assert extract_table(table, "ax").capabilities.can_act is can_set


# ── Picking among several grids ─────────────────────────────────────

def test_the_focused_grid_wins_over_the_bigger_one():
    # Routing rule 3: the first table is not necessarily the target table.
    root = _node("group", children=[
        _node("table", children=[
            _node("row", children=[_cell(f"big{i}", i, 0)]) for i in range(5)
        ]),
        _node("table", states=["focused"], children=[
            _node("row", children=[_cell("wanted", 0, 0)]),
        ]),
    ])
    out = extract_table(root, "ax")
    assert [c.text for c in out.cells] == ["wanted"]


# ── Columns the platform didn't number ──────────────────────────────

def test_cells_with_no_index_range_become_one_column_of_rows():
    # An AXOutline's cells carry no index range at all — a Finder sidebar
    # reads as 24 rows of one column, which is what it looks like.
    table = _node("table", children=[
        _node("row", children=[_node("cell", value="a")]),
        _node("row", children=[_node("cell", value="b")]),
    ])
    out = extract_table(table, "ax")
    assert (out.rows, out.cols) == (2, 1)
    assert [(c.r, c.c, c.text) for c in out.cells] == [(0, 0, "a"), (1, 0, "b")]


def test_cells_with_a_row_but_no_column_are_numbered_across_the_row():
    # Tree-grids report the row and leave the column None (matrix §5 Task Mgr).
    table = _node("table", children=[
        _node("row", children=[_node("cell", value="a", row_range=(3, 1)),
                               _node("cell", value="b", row_range=(3, 1))]),
    ])
    out = extract_table(table, "ax")
    assert [(c.r, c.c, c.text) for c in out.cells] == [(3, 0, "a"), (3, 1, "b")]
