"""macOS AX → shared ui_* role / verb / state normalization."""

from __future__ import annotations

import pytest

from conductor_desktop.core.ui import ax
from conductor_desktop.core.ui.ax import normalize, role_of
from conductor_desktop.core.ui.model import ROLES, VERBS, RawNode, UINode


def test_tables_only_emit_shared_roles():
    assert set(ax._ROLE.values()) | set(ax._SUBROLE.values()) <= ROLES


@pytest.mark.parametrize("native, sub, role", [
    ("AXButton", "", "button"),
    ("AXRadioButton", "AXTabButton", "tab"),
    ("AXRadioButton", "", "radio"),
    ("AXWindow", "AXDialog", "dialog"),
    ("AXWindow", "AXStandardWindow", "window"),
    ("AXRow", "AXOutlineRow", "tree_item"),
    ("AXPopUpButton", "", "combo_box"),
    ("AXSomethingNew", "", "other"),
])
def test_role_mapping(native, sub, role):
    assert role_of(native, sub) == role


def test_button_press_and_disabled():
    assert normalize(RawNode("AXButton", actions=["AXPress", "AXShowMenu"], enabled=False)) == (
        "button", ["press"], ["disabled"])


def test_checkbox_toggles_and_checked_becomes_state():
    assert normalize(RawNode("AXCheckBox", checked=1, actions=["AXPress"])) == (
        "checkbox", ["toggle"], ["checked"])
    assert normalize(RawNode("AXCheckBox", checked=2, actions=["AXPress"]))[2] == ["mixed"]
    assert normalize(RawNode("AXCheckBox", checked=0, actions=["AXPress"]))[2] == []


def test_tab_selects():
    assert normalize(RawNode("AXRadioButton", subrole="AXTabButton", checked=1,
                             actions=["AXPress"])) == ("tab", ["select"], ["selected"])


@pytest.mark.parametrize("native, sub, carries", [
    ("AXCheckBox", "", True), ("AXRadioButton", "AXTabButton", True),
    ("AXTextField", "", False), ("AXSlider", "", False),
])
def test_state_value_roles(native, sub, carries):
    assert ax.carries_state_value(native, sub) is carries


def test_menu_bar_item_and_popup_expand():
    assert normalize(RawNode("AXMenuBarItem", actions=["AXPress", "AXCancel"]))[1] == ["expand"]
    assert normalize(RawNode("AXPopUpButton", actions=["AXPress", "AXShowMenu"]))[1] == ["expand"]


def test_text_field_sets_value_and_focuses():
    assert normalize(RawNode("AXTextField", value_settable=True, focused=True)) == (
        "text_field", ["set_value", "focus"], ["focused"])


def test_secure_field_is_protected():
    _, verbs, states = normalize(RawNode("AXTextField", subrole="AXSecureTextField",
                                         value_settable=True, focused=False, protected=True))
    assert "set_value" in verbs and "protected" in states


def test_outline_row_selects_and_expands():
    assert normalize(RawNode("AXRow", subrole="AXOutlineRow", selected=True, expanded=True)) == (
        "tree_item", ["select", "expand"], ["selected", "expanded"])


def test_leaf_outline_row_does_not_expand():
    _, verbs, _ = normalize(RawNode("AXRow", subrole="AXOutlineRow", selected=False,
                                    expanded=False, expandable=False))
    assert verbs == ["select"]


def test_scroll_to_alone_is_not_actionable():
    _, verbs, _ = normalize(RawNode("AXLink", actions=["AXScrollToVisible"]))
    assert verbs == ["scroll_to"]
    assert not UINode("link", "AXLink", verbs=verbs).actionable


@pytest.mark.parametrize("raw", [
    RawNode("AXButton", actions=["AXPress", "AXRaise", "AXScrollToVisible"]),
    RawNode("AXSlider", actions=["AXIncrement"], value_settable=True),
    RawNode("AXComboBox", value_settable=True, focused=False, actions=["AXShowMenu"]),
    RawNode("AXDisclosureTriangle", actions=["AXPress"]),
])
def test_verbs_stay_in_the_shared_set(raw):
    assert set(normalize(raw)[1]) <= set(VERBS)
