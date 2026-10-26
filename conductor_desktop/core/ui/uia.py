"""Windows UIA → shared ``ui_*`` vocabulary. Pure tables, no comtypes import.

A UIA source fills ``RawNode`` with the ControlType name (pywinauto's
spelling, ``"CheckBox"``) as ``native_role`` and the supported control
patterns as ``actions``: ``Invoke``, ``Toggle``, ``SelectionItem``,
``ExpandCollapse``, ``Value``, ``RangeValue``, ``ScrollItem``, plus
``Focusable`` when ``IsKeyboardFocusable`` is set. ``subrole`` is ``"dialog"``
for a window whose ``IsDialog`` is true.
"""

from __future__ import annotations

from .model import RawNode
from .normalize import Verbs, role_table, states_of

_ROLE = {
    "Button": "button",
    "SplitButton": "button",
    "CheckBox": "checkbox",
    "RadioButton": "radio",
    "TabItem": "tab",
    "Hyperlink": "link",
    "Edit": "text_field",
    "Document": "text_area",
    "Text": "static_text",
    "Menu": "menu",
    "MenuBar": "menu",
    "MenuItem": "menu_item",
    "ComboBox": "combo_box",
    "List": "list",
    "ListItem": "list_item",
    "DataItem": "row",
    "Table": "table",
    "DataGrid": "table",
    "Tree": "tree",
    "TreeItem": "tree_item",
    "HeaderItem": "cell",
    # Synthesized by the tree-grid path in backends/windows.py, which
    # splits a row into per-column cells UIA never named.
    "GridCell": "cell",
    "Slider": "slider",
    "Spinner": "slider",
    "Window": "window",
    "Group": "group",
    "Pane": "group",
    "Tab": "group",
    "Header": "group",
    "StatusBar": "group",
    "TitleBar": "group",
    "AppBar": "toolbar",
    "Image": "image",
    "ScrollBar": "scroll_area",
    "ToolBar": "toolbar",
}

_SUBROLE = {
    ("Window", "dialog"): "dialog",
}

_TEXT_ROLES = frozenset({"text_field", "text_area", "combo_box"})
_SELECTABLE_ROLES = frozenset({"row", "list_item", "tree_item", "cell", "tab", "radio"})
# A grid cell with a writable ValuePattern is writable — that is how a
# spreadsheet cell is set without ground→click (matrix §5, the act channel).
# Grid coordinates say "cell" even when the ControlType doesn't: Excel reports
# its cells as DataItem, which is a row.
_VALUE_ROLES = frozenset({"text_field", "text_area", "combo_box", "slider", "cell"})


role_of = role_table(_ROLE, _SUBROLE)


def normalize(raw: RawNode) -> tuple[str, list[str], list[str]]:
    """``(role, verbs, states)`` for one UIA element.

    Patterns decide verbs, not ControlType: a Win11 Settings switch is a
    Button with Toggle, and a menu item opens a submenu through ExpandCollapse.
    """
    role = role_of(raw.native_role, raw.subrole)
    patterns = set(raw.actions)
    verbs = Verbs()
    add = verbs.add

    # A leaf (ExpandCollapseState_LeafNode) reports the pattern but can't expand.
    expandable = "ExpandCollapse" in patterns and raw.expandable is not False
    if "Toggle" in patterns:
        add("toggle")
    if "SelectionItem" in patterns and role in _SELECTABLE_ROLES:
        add("select")
    if expandable and role in ("menu_item", "combo_box", "tree_item", "button", "list_item"):
        add("expand")
    if "Invoke" in patterns and "toggle" not in verbs and not (role in ("tab", "radio") and "select" in verbs):
        add("press")
    in_grid = raw.row_range is not None or raw.col_range is not None
    if raw.value_settable and (role in _VALUE_ROLES or in_grid):
        add("set_value")
    if role in ("window", "dialog") or ("Focusable" in patterns and role in _TEXT_ROLES):
        add("focus")
    if "ScrollItem" in patterns:
        add("scroll_to")

    return role, verbs.list(), states_of(raw, role)


# ToggleState_Off / On / Indeterminate → RawNode.checked.
TOGGLE_STATES = {0: 0, 1: 1, 2: 2}
# ExpandCollapseState: Collapsed 0, Expanded 1, PartiallyExpanded 2, LeafNode 3.
EXPAND_LEAF = 3


def expand_state(state: int | None) -> tuple[bool | None, bool | None]:
    """``(expanded, expandable)`` from an ExpandCollapseState."""
    if state is None:
        return None, None
    if state == EXPAND_LEAF:
        return None, False
    return state in (1, 2), True
