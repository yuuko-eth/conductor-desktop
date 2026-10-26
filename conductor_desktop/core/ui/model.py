"""Shared data model for the ``ui_*`` tools (https://docs.conductor-mcp.io/concepts/accessibility/ §Data model)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

ROLES = frozenset({
    "button", "checkbox", "radio", "tab", "link", "text_field", "text_area",
    "static_text", "menu", "menu_item", "list", "list_item", "row", "cell",
    "table", "tree", "tree_item", "combo_box", "slider", "window", "dialog",
    "group", "image", "scroll_area", "toolbar", "other",
})

VERBS = ("press", "set_value", "toggle", "select", "expand", "focus", "scroll_to")

# Verbs that don't on their own make an element worth acting on: nearly every
# web element can scroll into view.
_PASSIVE_VERBS = frozenset({"scroll_to"})


@dataclass
class RawNode:
    """What a platform source reads for one element, before normalization."""

    native_role: str
    subrole: str = ""
    name: str = ""
    value: str = ""
    id: str = ""
    actions: list[str] = field(default_factory=list)
    bounds: tuple[int, int, int, int] | None = None
    # Tri-state flags: None means the element doesn't expose the attribute.
    enabled: bool | None = None
    focused: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    value_settable: bool = False
    protected: bool = False
    # 0 / 1 / 2 (off / on / mixed) for checkbox, radio and tab, whose native
    # value is state rather than content; the source leaves ``value`` empty.
    checked: int | None = None
    # Whether an expandable-looking element (outline row) can actually be
    # expanded; None when the source didn't check.
    expandable: bool | None = None
    # Where a cell sits in its grid, as (start, span) per axis. Only cells have
    # them, and only a source that reads them — the ``ui_*`` walk doesn't, the
    # table walk does. See https://docs.conductor-mcp.io/concepts/structured-reads/ §4-6.
    row_range: tuple[int, int] | None = None
    col_range: tuple[int, int] | None = None


@dataclass
class UINode:
    role: str
    native_role: str
    name: str = ""
    value: str = ""
    id: str = ""
    verbs: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    bounds: tuple[int, int, int, int] | None = None
    # Child indices from the walk root in the NATIVE tree. Display folding
    # never changes it; ref resolution walks it step by step.
    path: tuple[int, ...] = ()
    children: list[UINode] = field(default_factory=list)
    ref: str | None = None
    # Text borrowed from the one static text inside an unnamed control
    # (core/ui/label.py); ``is_label`` marks that text node.
    label: str = ""
    is_label: bool = False
    # Grid coordinates, on cells read by a table walk (core/structured.py).
    row_range: tuple[int, int] | None = None
    col_range: tuple[int, int] | None = None
    # Truncation, always surfaced by the formatters.
    omitted_children: int = 0   # siblings past max_children
    unwalked_children: int = 0  # children below the depth limit
    budget_skipped: int = 0     # children not read because the walk budget ran out
    menubar_skipped: int = 0    # children of a menu bar inside the window (include_menubar=false)

    @property
    def title(self) -> str:
        """The native name, else the borrowed label."""
        return self.name or self.label

    @property
    def actionable(self) -> bool:
        return any(v not in _PASSIVE_VERBS for v in self.verbs)

    @property
    def truncated(self) -> bool:
        return bool(self.omitted_children or self.unwalked_children or self.budget_skipped
                    or self.menubar_skipped)


def iter_nodes(node: UINode) -> Iterator[UINode]:
    yield node
    for child in node.children:
        yield from iter_nodes(child)


@dataclass
class UIRead:
    """One ``ui_tree`` read: the target, its roots and the walk's cost."""

    app: str
    pid: int
    window_id: int | str | None
    window_title: str
    window: UINode | None
    menubar: UINode | None = None
    nodes: int = 0
    elapsed: float = 0.0
    timeout: float = 10.0
    budget_hit: bool = False
    # Apps skipped because they stopped answering (Linux AT-SPI); surfaced so a
    # read that can't see an app doesn't look like the app isn't there.
    unresponsive_apps: list[str] = field(default_factory=list)

    def roots(self) -> list[tuple[str, UINode]]:
        return [(kind, node) for kind, node in (("window", self.window), ("menubar", self.menubar))
                if node is not None]
