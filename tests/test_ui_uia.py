"""Windows UIA → shared ui_* role / verb / state normalization."""

from __future__ import annotations

import pytest

from conductor_desktop.core.ui import uia
from conductor_desktop.core.ui.model import ROLES, VERBS, RawNode
from conductor_desktop.core.ui.uia import expand_state, normalize, role_of


def test_tables_only_emit_shared_roles():
    assert set(uia._ROLE.values()) | set(uia._SUBROLE.values()) <= ROLES


@pytest.mark.parametrize("native, sub, role", [
    ("Button", "", "button"),
    ("Edit", "", "text_field"),
    ("Document", "", "text_area"),
    ("TabItem", "", "tab"),
    ("Window", "dialog", "dialog"),
    ("Window", "", "window"),
    ("Pane", "", "group"),
    ("SemanticZoom", "", "other"),
])
def test_role_mapping(native, sub, role):
    assert role_of(native, sub) == role


def test_button_invokes_and_disabled():
    assert normalize(RawNode("Button", actions=["Invoke"], enabled=False)) == (
        "button", ["press"], ["disabled"])


def test_toggle_button_toggles_instead_of_pressing():
    # Win11 Settings switches are Buttons with Toggle (and sometimes Invoke).
    assert normalize(RawNode("Button", actions=["Toggle", "Invoke"], checked=1)) == (
        "button", ["toggle"], ["checked"])
    assert normalize(RawNode("CheckBox", actions=["Toggle"], checked=2))[2] == ["mixed"]


def test_tab_and_list_item_select():
    assert normalize(RawNode("TabItem", actions=["SelectionItem"], selected=True)) == (
        "tab", ["select"], ["selected"])
    assert normalize(RawNode("ListItem", actions=["SelectionItem", "ScrollItem"]))[1] == [
        "select", "scroll_to"]


def test_expand_skips_leaf_nodes():
    expanded, expandable = expand_state(3)
    leaf = RawNode("TreeItem", actions=["ExpandCollapse", "SelectionItem"],
                   expanded=expanded, expandable=expandable)
    assert normalize(leaf)[1] == ["select"]
    expanded, expandable = expand_state(1)
    parent = RawNode("TreeItem", actions=["ExpandCollapse", "SelectionItem"],
                     expanded=expanded, expandable=expandable)
    assert normalize(parent) == ("tree_item", ["select", "expand"], ["expanded"])


def test_menu_item_opens_submenu_or_presses():
    assert normalize(RawNode("MenuItem", actions=["ExpandCollapse"], expandable=True))[1] == ["expand"]
    assert normalize(RawNode("MenuItem", actions=["Invoke"]))[1] == ["press"]


def test_edit_sets_value_and_focuses_password_protected():
    assert normalize(RawNode("Edit", actions=["Value", "Focusable"], value_settable=True,
                             protected=True, focused=True)) == (
        "text_field", ["set_value", "focus"], ["focused", "protected"])
    # A read-only value can't be set.
    assert normalize(RawNode("Edit", actions=["Value", "Focusable"]))[1] == ["focus"]


def test_windows_focus():
    assert normalize(RawNode("Window", subrole="dialog"))[:2] == ("dialog", ["focus"])


def test_static_text_has_no_verbs():
    assert normalize(RawNode("Text", name="Display is 15")) == ("static_text", [], [])


def test_verbs_are_shared_vocabulary():
    raw = RawNode("ComboBox", actions=["ExpandCollapse", "Value", "Focusable", "ScrollItem",
                                       "Invoke", "Toggle", "SelectionItem"],
                  value_settable=True, expandable=True)
    assert set(normalize(raw)[1]) <= set(VERBS)


def test_grid_coordinates_make_a_writable_element_settable():
    # Excel reports a cell as DataItem, which the role table reads as a row.
    # The GridItemPattern coordinates are what says "this is a cell".
    cell = RawNode("DataItem", actions=["Value"], value_settable=True,
                   row_range=(4, 1), col_range=(2, 1))
    assert "set_value" in normalize(cell)[1]
    assert "set_value" not in normalize(RawNode("DataItem", actions=["Value"],
                                                value_settable=True))[1]
