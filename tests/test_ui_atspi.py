"""Linux AT-SPI → shared ui_* role / verb / state normalization.

Fixtures mirror what GTK 4 (GNOME 46) and GTK 3 (XFCE) apps expose.
"""

from __future__ import annotations

import pytest

from conductor_desktop.core.ui import atspi
from conductor_desktop.core.ui.atspi import normalize, role_of
from conductor_desktop.core.ui.model import ROLES, VERBS, RawNode


def test_tables_only_emit_shared_roles():
    assert set(atspi._ROLE.values()) | set(atspi._SUBROLE.values()) <= ROLES


@pytest.mark.parametrize("native, sub, role", [
    ("push button", "", "button"),
    ("frame", "", "window"),
    ("file chooser", "", "dialog"),
    ("text", "", "text_field"),
    ("text", "multi-line", "text_area"),
    ("page tab", "", "tab"),
    ("panel", "", "group"),
    ("table cell", "", "cell"),
    ("separator", "", "other"),
])
def test_role_mapping(native, sub, role):
    assert role_of(native, sub) == role


def test_push_button_clicks():
    # Calculator key: GTK 4 lists only "click" once GActions are dropped.
    assert normalize(RawNode("push button", name="4", actions=["click", "Focusable"])) == (
        "button", ["press"], [])
    assert normalize(RawNode("push button", actions=["click"], enabled=False))[2] == ["disabled"]


def test_check_box_toggles():
    # Settings "Hot Corner" switch.
    assert normalize(RawNode("check box", actions=["toggle", "Focusable"], checked=0)) == (
        "checkbox", ["toggle"], [])
    # GTK 3 check box and toggle button press to toggle.
    assert normalize(RawNode("check box", actions=["click"], checked=1)) == (
        "checkbox", ["toggle"], ["checked"])
    assert normalize(RawNode("toggle button", actions=["click"], checked=2))[1:] == (
        ["toggle"], ["mixed"])


def test_check_box_without_actions_has_no_verbs():
    # GTK 4 radio-like check boxes in a list row: the row selects.
    assert normalize(RawNode("check box", actions=["Focusable"], checked=1)) == (
        "checkbox", [], ["checked"])


def test_selectable_rows_and_tabs_select():
    assert normalize(RawNode("list item", name="Documents", actions=["Selectable", "Focusable"],
                             selected=True)) == ("list_item", ["select", "scroll_to"], ["selected"])
    assert normalize(RawNode("page tab", actions=["Selectable"]))[1] == ["select"]
    # Not selectable: no select verb.
    assert normalize(RawNode("list item", actions=[]))[1] == ["scroll_to"]


def test_radio_button_selects():
    assert normalize(RawNode("radio button", actions=["click"], selected=True)) == (
        "radio", ["select"], ["selected"])


def test_gtk3_menu_opens_and_items_press():
    assert normalize(RawNode("menu", name="File", actions=["click", "Selectable"])) == (
        "menu", ["expand"], [])
    assert normalize(RawNode("menu item", name="Save As...", actions=["click", "Selectable"]))[1] == ["press"]


def test_popup_button_presses_and_expands():
    assert normalize(RawNode("push button", actions=["click", "HasPopup"]))[1] == ["expand", "press"]


def test_leaf_table_cell_does_not_expand():
    # GTK 3 lists "expand or contract" on every cell.
    leaf = RawNode("table cell", actions=["expand or contract", "edit", "activate"], expandable=False)
    assert normalize(leaf)[1] == ["press", "scroll_to"]
    parent = RawNode("table cell", actions=["expand or contract", "activate"], expandable=True,
                     expanded=True)
    assert normalize(parent) == ("cell", ["expand", "press", "scroll_to"], ["expanded"])


def test_editable_text_sets_value_and_focuses():
    raw = RawNode("text", subrole="multi-line", actions=["EditableText", "Focusable"],
                  value_settable=True, focused=True)
    assert normalize(raw) == ("text_area", ["set_value", "focus"], ["focused"])
    # Calculator's read-only result line.
    assert normalize(RawNode("text", actions=["Focusable"]))[1] == ["focus"]


def test_password_text_is_protected():
    raw = RawNode("password text", actions=["EditableText"], value_settable=True, protected=True)
    assert normalize(raw) == ("text_field", ["set_value"], ["protected"])


def test_spin_button_sets_numbers():
    assert normalize(RawNode("spin button", value_settable=True))[:2] == ("slider", ["set_value"])


def test_windows_focus_and_labels_have_no_verbs():
    assert normalize(RawNode("frame", name="Calculator"))[:2] == ("window", ["focus"])
    assert normalize(RawNode("label", name="Appearance")) == ("static_text", [], [])


def test_verbs_are_shared_vocabulary():
    raw = RawNode("combo box", actions=["press", "toggle", "expand or contract", "Selectable",
                                       "HasPopup", "Focusable", "EditableText"],
                  value_settable=True, expandable=True)
    assert set(normalize(raw)[1]) <= set(VERBS)
