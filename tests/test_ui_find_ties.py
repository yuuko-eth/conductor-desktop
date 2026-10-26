"""ui_find: controls beat their own labels, and digits match number words."""

from __future__ import annotations

from conductor_desktop.core.ui.find import Criteria, find
from conductor_desktop.core.ui.model import UINode


def _window(*kids: UINode) -> list[tuple[str, UINode]]:
    return [("window", UINode("window", "Window", name="Settings", children=list(kids)))]


def test_a_label_next_to_its_control_is_not_ambiguous():
    label = UINode("static_text", "Text", name="Transparency effects", path=(0,))
    switch = UINode("button", "Button", name="Transparency effects", verbs=["toggle"], path=(1,))
    result = find(_window(label, switch), Criteria(query="Transparency effects"))
    assert result.candidates[0].node is switch
    assert not result.ambiguous


def test_two_controls_with_one_name_stay_ambiguous():
    a = UINode("button", "Button", name="Close", verbs=["press"], path=(0,))
    b = UINode("button", "Button", name="Close", verbs=["press"], path=(1,))
    assert find(_window(a, b), Criteria(query="Close")).ambiguous


def test_digit_matches_number_word():
    four = UINode("button", "Button", name="Four", verbs=["press"], path=(0,))
    fourteen = UINode("button", "Button", name="Forty", verbs=["press"], path=(1,))
    result = find(_window(four, fourteen), Criteria(query="4", role="button"))
    assert result.candidates[0].node is four
    assert result.candidates[0].score == 1.0
    assert find(_window(four), Criteria(name="4", role="button"), match="exact").matched == 1


def test_digit_still_matches_digit_names():
    key = UINode("button", "Button", name="4", verbs=["press"], path=(0,))
    assert find(_window(key), Criteria(query="4")).candidates[0].score == 1.0
