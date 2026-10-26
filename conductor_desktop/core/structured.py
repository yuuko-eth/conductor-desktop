"""StructuredProvider — turn a walked ``UINode`` tree into a TableExtract.

Platform-agnostic because the walk already normalized: every backend's table
walk hands over `core/ui` nodes whose `role` is the shared vocabulary and
whose `verbs` are the shared act channel, so "is this a cell?" and "can this
be written?" are statements about the node rather than guesses about one
platform's role names. Grid coordinates (`row_range` / `col_range`) are the
one thing only a table walk fills in; this module gates on cell presence,
handles column-collapse + child-text fallthrough, and emits the common schema.

Per-platform findings: https://docs.conductor-mcp.io/concepts/structured-reads/. This is the
read/extraction path; the act channel (set_value / invoke) is a separate
contract.
"""

from __future__ import annotations

from .protocols import (
    StructuredCapabilities,
    TableCell,
    TableExtract,
)
from .ui.model import UINode

# Which platform read the tree. It no longer decides anything here — the walk
# normalized the roles — but it lands in the TableExtract so the caller knows
# what answered, so an unknown one is still an error rather than a label.
_SOURCES = frozenset({"ax", "atspi", "uia"})

# Normalized roles that may *contain* a grid. Gate on cell presence, NOT this
# set — VS Code's file-tree is an AXOutline with zero cells (matrix §4,
# routing rule 2).
_CONTAINER_ROLES = frozenset({"table", "tree"})
# Normalized roles whose nodes are cells.
_CELL_ROLES = frozenset({"cell"})
# Leaf roles whose `value` carries text when a cell's own value is empty
# (child-StaticText / Edit fallthrough — matrix §4 macOS, §5 Win tree-grid).
_TEXT_LEAF_ROLES = frozenset({"static_text", "text_field", "text_area"})
# Verbs that mean "writable / invokable without ground→click". Selecting or
# scrolling to a cell doesn't change it, so neither counts.
_SETTABLE_VERBS = frozenset({"set_value", "press", "toggle"})


def _is_container(node: UINode) -> bool:
    return node.role in _CONTAINER_ROLES


def _is_cell(node: UINode) -> bool:
    # A node the walk gave grid coordinates to is a cell whatever it calls
    # itself: the Windows tree-grid path synthesizes cells UIA never named.
    if node.row_range is not None or node.col_range is not None:
        return True
    return node.role in _CELL_ROLES


def _descendant_cells(node: UINode, out: list[UINode], depth: int = 0) -> None:
    if depth > 40:
        return
    for child in node.children:
        if _is_cell(child):
            out.append(child)
        else:
            _descendant_cells(child, out, depth + 1)


def _cell_text(node: UINode) -> str:
    """Cell's own value, else first descendant text-leaf value (fallthrough)."""
    if node.value:
        return node.value
    stack = list(node.children)
    seen = 0
    while stack and seen < 200:
        seen += 1
        n = stack.pop(0)
        if n.value and n.role in _TEXT_LEAF_ROLES:
            return n.value
        stack.extend(n.children)
    return ""


def _can_set(node: UINode) -> bool:
    return any(v in _SETTABLE_VERBS for v in node.verbs)


def _valid_bbox(node: UINode) -> tuple[int, int, int, int] | None:
    """Drop 0-area boxes — off-screen cells report (x,y,0,0) (matrix §6 Linux)."""
    b = node.bounds
    if b and b[2] > 0 and b[3] > 0:
        return b
    return None


def _find_tables(root: UINode) -> list[tuple[UINode, list[UINode]]]:
    """All container nodes that actually have descendant cells, depth-first."""
    found: list[tuple[UINode, list[UINode]]] = []

    def walk(node: UINode, depth: int = 0) -> None:
        if depth > 40:
            return
        if _is_container(node):
            cells: list[UINode] = []
            _descendant_cells(node, cells)
            if cells:  # gate on cell presence
                found.append((node, cells))
        for child in node.children:
            walk(child, depth + 1)

    walk(root)
    return found


def _score(container: UINode, cells: list[UINode]) -> tuple[int, int]:
    """Pick by focus then size — 'first table ≠ target table' (routing rule 3)."""
    focused = 1 if "focused" in container.states else 0
    return (focused, len(cells))


def extract_table(root: UINode | None, source: str) -> TableExtract:
    """Extract the most salient table from a walked tree into the common schema.

    `source` names which platform read the tree, and is echoed into the
    TableExtract.

    Returns an empty TableExtract (rows=0, source='none') when no cell-bearing
    container is present — the caller classifies that as a `failure` if it knows
    the surface *should* have had a table (canvas_blackout / a11y_cold / etc.).
    """
    if source not in _SOURCES:
        raise ValueError(
            f"unknown a11y source {source!r}; expected one of "
            f"{', '.join(sorted(_SOURCES))}"
        )
    if root is None:
        return TableExtract(0, 0, [], "none", StructuredCapabilities())

    tables = _find_tables(root)
    if not tables:
        return TableExtract(0, 0, [], "none", StructuredCapabilities())

    container, cell_nodes = max(tables, key=lambda t: _score(t[0], t[1]))

    cells: list[TableCell] = []
    column_collapse = _detect_column_collapse(cell_nodes)
    # Fallback column assignment for tree-grids that report grididx=None: order
    # cells within a row by x, assign sequential columns (matrix §5 Task Mgr).
    row_seq: dict[int, int] = {}

    for n in cell_nodes:
        r = n.row_range[0] if n.row_range else len(row_seq)
        if column_collapse or n.col_range is None:
            c = row_seq.get(r, 0)
            row_seq[r] = c + 1
        else:
            c = n.col_range[0]
        cells.append(
            TableCell(
                r=r,
                c=c,
                bbox=_valid_bbox(n),
                text=_cell_text(n),
                can_set=_can_set(n),
            )
        )

    rows = (max((cell.r for cell in cells), default=-1)) + 1
    cols = (max((cell.c for cell in cells), default=-1)) + 1

    caps = StructuredCapabilities(
        has_structure=True,
        has_values=any(cell.text for cell in cells),
        has_geometry=any(cell.bbox for cell in cells),
        can_act=any(cell.can_set for cell in cells),
    )
    return TableExtract(rows, cols, cells, source, caps)


def _detect_column_collapse(cell_nodes: list[UINode]) -> bool:
    """True when columns collapse onto the row: cells in a row share value+rect.

    XAML tree-grids (Win Task Mgr) and some AXOutlines return the whole-row
    value/rect for every column → GetItem(r,c) resolves to the row. Detect by
    duplicate (bounds) across cells that claim different columns.
    """
    by_bounds: dict[tuple, int] = {}
    multi_col = False
    for n in cell_nodes:
        if n.col_range and n.col_range[0] > 0:
            multi_col = True
        if n.bounds:
            by_bounds[n.bounds] = by_bounds.get(n.bounds, 0) + 1
    # If genuine per-column index ranges exist, trust them.
    if multi_col:
        return False
    # Otherwise if many cells share identical rects, columns collapsed.
    collapsed = sum(1 for v in by_bounds.values() if v > 1)
    return collapsed > 0
