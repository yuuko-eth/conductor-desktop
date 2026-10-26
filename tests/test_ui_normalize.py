"""The normalize helpers the three platform modules share."""

from __future__ import annotations

import pytest

from conductor_desktop.core.ui import atspi, ax, uia
from conductor_desktop.core.ui.model import ROLES, RawNode
from conductor_desktop.core.ui.normalize import Verbs, role_table, states_of

# ── role_table ──────────────────────────────────────────────────────

def test_role_table_maps_and_defaults_to_other():
    role_of = role_table({"Button": "button"}, {})
    assert role_of("Button") == "button"
    assert role_of("SomethingNew") == "other"


def test_a_subrole_is_the_more_specific_answer():
    role_of = role_table({"AXRow": "row"}, {("AXRow", "AXOutlineRow"): "tree_item"})
    assert role_of("AXRow") == "row"
    assert role_of("AXRow", "AXOutlineRow") == "tree_item"


@pytest.mark.parametrize("module", [ax, atspi, uia])
def test_every_platform_table_stays_inside_the_shared_vocabulary(module):
    mapped = set(module._ROLE.values()) | set(module._SUBROLE.values())
    assert mapped <= ROLES


# ── Verbs ───────────────────────────────────────────────────────────

def test_verbs_keep_order_and_are_said_once():
    verbs = Verbs()
    for verb in ("press", "focus", "press", "scroll_to"):
        verbs.add(verb)
    assert verbs.list() == ["press", "focus", "scroll_to"]


def test_verbs_membership_and_emptiness():
    verbs = Verbs()
    assert not verbs
    assert "press" not in verbs
    verbs.add("press")
    assert verbs and "press" in verbs


def test_the_returned_list_is_a_copy():
    verbs = Verbs()
    verbs.add("press")
    out = verbs.list()
    out.append("toggle")
    assert verbs.list() == ["press"]


# ── states_of ───────────────────────────────────────────────────────

def test_only_notable_states_are_said():
    assert states_of(RawNode("Button", enabled=True), "button") == []
    assert states_of(RawNode("Button", enabled=False), "button") == ["disabled"]


def test_a_checked_tab_is_selected_not_checked():
    # The platforms report tab selection through the checked attribute, and
    # "checked" would read as a checkbox.
    assert states_of(RawNode("TabItem", checked=1), "tab") == ["selected"]
    assert states_of(RawNode("CheckBox", checked=1), "checkbox") == ["checked"]


def test_a_mixed_checkbox():
    assert states_of(RawNode("CheckBox", checked=2), "checkbox") == ["mixed"]


def test_selection_is_said_once_however_it_was_learnt():
    states = states_of(RawNode("TabItem", checked=1, selected=True), "tab")
    assert states == ["selected"]


def test_the_full_set():
    raw = RawNode("Edit", enabled=False, focused=True, selected=True,
                  expanded=True, protected=True)
    assert states_of(raw, "text_field") == [
        "disabled", "focused", "selected", "expanded", "protected",
    ]


@pytest.mark.parametrize("module", [ax, atspi, uia])
def test_the_three_platforms_agree_on_states(module):
    # The states block was byte-identical between atspi and uia and differed
    # from ax by two lines; one of the three has to be right for all of them.
    raw = RawNode(next(iter(module._ROLE)), enabled=False, focused=True)
    assert module.normalize(raw)[2] == ["disabled", "focused"]
