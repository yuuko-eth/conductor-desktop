"""Labels for unnamed controls: derivation, outline, search, action text."""

from __future__ import annotations

from conductor_desktop.core.ui.find import Criteria, find
from conductor_desktop.core.ui.format import format_json, format_outline
from conductor_desktop.core.ui.label import derive_labels
from conductor_desktop.core.ui.model import UINode, UIRead


def text(value, path=()):
    return UINode("static_text", "AXStaticText", value=value, path=path)


def finder_row(label, i):
    """row → cell → static text + image, as Finder's sidebar reads."""
    cell = UINode("cell", "AXCell", verbs=["press", "select"], path=(0, i, 0), children=[
        text(label, (0, i, 0, 0)),
        UINode("image", "AXImage", name="icon", path=(0, i, 0, 1)),
    ])
    return UINode("tree_item", "AXRow", verbs=["select"], path=(0, i), children=[cell])


def sidebar():
    return UINode("window", "AXWindow", name="user", verbs=["focus"], children=[
        UINode("tree", "AXOutline", name="sidebar", path=(0,),
               children=[finder_row("Desktop", 0), finder_row("Documents", 1)]),
    ])


def test_outermost_unnamed_control_borrows_its_one_text():
    root = sidebar()
    derive_labels(root)
    row = root.children[0].children[1]
    assert row.label == "Documents" and row.name == ""
    assert row.children[0].label == ""  # the cell inside stays unnamed
    assert row.children[0].children[0].is_label


def test_no_label_from_two_texts_named_controls_or_truncated_subtrees():
    two = UINode("button", "AXButton", verbs=["press"], children=[text("a"), text("b")])
    named = UINode("button", "AXButton", name="Go", verbs=["press"], children=[text("x")])
    cut = UINode("button", "AXButton", verbs=["press"], children=[
        UINode("group", "AXGroup", unwalked_children=2), text("x"),
    ])
    passive = UINode("group", "AXGroup", children=[text("x")])
    for node in (two, named, cut, passive):
        derive_labels(node)
        assert node.label == "" and not any(c.is_label for c in node.children)


def test_text_repeating_a_named_control_is_hidden():
    # GTK draws a button's name as a label inside it: Calculator's "4".
    button = UINode("button", "push button", name="4", verbs=["press"], children=[
        UINode("static_text", "label", name="4"), UINode("static_text", "label", name="four")])
    derive_labels(button)
    assert [c.is_label for c in button.children] == [True, False]
    lines = format_outline(_read(UINode("window", "frame", name="Calculator", children=[button])))
    assert lines.count('"4"') == 1


def test_labels_stay_within_the_depth_limit():
    deep = text("far")
    for _ in range(3):
        deep = UINode("group", "AXGroup", children=[deep])
    button = UINode("button", "AXButton", verbs=["press"], children=[deep])
    derive_labels(button)
    assert button.label == ""


def _read(root):
    return UIRead(app="Finder", pid=603, window_id=1, window_title="user", window=root, nodes=9)


def test_outline_shows_the_label_once():
    root = sidebar()
    derive_labels(root)
    lines = format_outline(_read(root)).splitlines()
    assert '    tree_item "Documents" select' in lines
    assert not any(line.strip().startswith('static_text "Documents"') for line in lines)


def test_json_keeps_name_and_label_apart():
    root = sidebar()
    derive_labels(root)
    import json
    row = json.loads(format_json(_read(root)))["window"]["children"][0]["children"][1]
    assert row["label"] == "Documents" and "name" not in row


def test_search_returns_the_control_not_its_text():
    root = sidebar()
    derive_labels(root)
    r = find([("window", root)], Criteria(query="Documents"))
    assert [c.node.role for c in r.candidates] == ["tree_item"] and not r.ambiguous
    assert r.candidates[0].path == "sidebar"


def test_action_text_uses_the_label():
    from test_ui_act import El, Fake, _node

    from conductor_desktop.core.ui.act import format_outcome, run_action

    row = El("AXRow", subrole="AXOutlineRow", selected=False)
    row.kids = [El("AXCell", actions=["AXOpen"], kids=[El("AXStaticText", value="Documents")])]
    node = _node(row)
    out = run_action(Fake(), row, node, "select", {})
    assert format_outcome("e5", "select", out)[1] == 'ui_select e5 — tree_item "Documents" — now selected (verified)'
