"""ui_find scoring, filters, ranking, ambiguity, paths and output."""

from __future__ import annotations

from conductor_desktop.core.ui import find as findmod
from conductor_desktop.core.ui.find import MIN_SCORE, Criteria, find, format_find, similarity
from conductor_desktop.core.ui.model import UINode, UIRead


def btn(name, path, **kw):
    return UINode("button", "AXButton", name=name, verbs=["press"], path=path, **kw)


def _window(extra=()):
    return UINode("window", "AXWindow", name="Untitled", children=[
        UINode("dialog", "AXSheet", name="Save As", path=(0,), children=[
            UINode("group", "AXGroup", path=(0, 0), children=[
                btn("Save", (0, 0, 0), bounds=(884, 612, 68, 24)),
                btn("Cancel", (0, 0, 1)),
                UINode("combo_box", "AXPopUpButton", name="Save as PDF…", verbs=["expand"],
                       path=(0, 0, 2)),
            ]),
            UINode("text_field", "AXTextField", name="Save As:", value="Untitled", id="_NS:9",
                   verbs=["set_value", "focus"], states=["focused"], path=(0, 1)),
            UINode("checkbox", "AXCheckBox", name="Hide extension", verbs=["toggle"],
                   states=["checked"], path=(0, 2)),
            UINode("checkbox", "AXCheckBox", name="Keep open", verbs=["toggle"],
                   states=["disabled"], path=(0, 3)),
            *extra,
        ]),
        UINode("static_text", "AXStaticText", value="Where: Documents", path=(1,)),
    ])


def _find(c, **kw):
    return find([("window", _window())], c, **kw)


def _names(result):
    return [cand.node.name or cand.node.value for cand in result.candidates]


# --- similarity -------------------------------------------------------------

def test_similarity_levels():
    assert similarity("Save", "Save") == 1.0
    assert similarity("save as", "Save As…") == 1.0
    word = similarity("Save", "Save as PDF…")
    sub = similarity("ave", "Save as PDF…")
    typo = similarity("Svae", "Save")
    assert 1.0 > word > sub and word > typo >= MIN_SCORE
    assert similarity("Save", "Close") < MIN_SCORE
    assert similarity("Save", "Save as PDF", exact=True) == 0.0


def test_difflib_fallback_keeps_the_ordering(monkeypatch):
    monkeypatch.setattr(findmod, "_fuzz", None)
    assert MIN_SCORE <= similarity("Svae", "Save") < similarity("Save", "Save as PDF…")


# --- ranking ----------------------------------------------------------------

def test_query_ranks_equality_first_and_is_not_ambiguous():
    r = _find(Criteria(query="Save"))
    assert _names(r) == ["Save", "Save As", "Save As:", "Save as PDF…"]
    assert r.candidates[0].score == 1.0 and not r.ambiguous and r.matched == 4


def test_one_word_query_does_not_match_children_through_their_ancestor():
    assert "Cancel" not in _names(_find(Criteria(query="Save")))


def test_multi_word_query_uses_ancestor_names():
    assert _find(Criteria(query="save as cancel")).candidates[0].node.name == "Cancel"


def test_duplicate_names_are_ambiguous():
    r = find([("window", _window(extra=[btn("Save", (0, 4))]))], Criteria(query="Save"))
    assert r.ambiguous


def test_actionable_wins_ties_otherwise_tree_order():
    root = UINode("window", "AXWindow", children=[
        UINode("static_text", "AXStaticText", name="Save", path=(0,)),
        btn("Save", (1,)),
    ])
    r = find([("window", root)], Criteria(query="save"))
    # The static text is the button's label, not a second target to choose from.
    assert [c.node.role for c in r.candidates] == ["button", "static_text"] and not r.ambiguous


def test_exact_mode():
    assert _names(_find(Criteria(query="save as pdf"), match="exact")) == ["Save as PDF…"]


def test_name_and_value_match_only_their_field():
    assert _names(_find(Criteria(value="untitled"))) == ["Save As:"]
    assert [c.node.role for c in _find(Criteria(name="untitled", role="window")).candidates] == ["window"]


def test_text_search_skips_the_root_unless_a_role_asks_for_it():
    assert "window" not in [c.node.role for c in _find(Criteria(query="Untitled")).candidates]
    assert _find(Criteria(name="untitled")).matched == 0
    assert _find(Criteria(role="window")).candidates[0].node.name == "Untitled"


# --- filters ----------------------------------------------------------------

def test_filter_only_is_tree_order_at_one():
    r = _find(Criteria(role="checkbox"))
    assert _names(r) == ["Hide extension", "Keep open"]
    assert {c.score for c in r.candidates} == {1.0} and not r.ranked and not r.ambiguous


def test_role_accepts_native_and_lists():
    assert len(_find(Criteria(role="AXCheckBox")).candidates) == 2
    assert len(_find(Criteria(role="checkbox, button")).candidates) == 4


def test_states_required_and_negated():
    assert _names(_find(Criteria(role="checkbox", states=["!disabled"]))) == ["Hide extension"]
    assert _names(_find(Criteria(states=["checked"]))) == ["Hide extension"]


def test_id_and_actionable_filters():
    assert _names(_find(Criteria(id="_NS:9"))) == ["Save As:"]
    r = _find(Criteria(actionable=True), limit=2)
    assert len(r.candidates) == 2 and r.matched == 6
    assert "Where: Documents" in _names(_find(Criteria(actionable=False), limit=10))


def test_filters_combine_with_text():
    assert _names(_find(Criteria(query="save", role="button"))) == ["Save"]


def test_empty_criteria():
    assert Criteria().empty and not Criteria(actionable=False).empty


# --- paths ------------------------------------------------------------------

def test_path_skips_generic_wrappers_and_the_root():
    save = _find(Criteria(query="Save")).candidates[0]
    assert save.path == "Save As"


def test_long_paths_keep_the_last_segments():
    leaf = btn("Go", (0, 0, 0, 0, 0))
    node = leaf
    for name in "edcba":
        node = UINode("group", "AXGroup", name=name, children=[node])
    root = UINode("window", "AXWindow", children=[node])
    assert find([("window", root)], Criteria(query="go")).candidates[0].path == "… › c › d › e"


def test_repeated_ancestor_names_collapse():
    root = UINode("window", "AXWindow", children=[
        UINode("group", "AXWebArea", name="Page", children=[
            UINode("group", "AXGroup", name="Page", children=[btn("Go", (0, 0, 0))]),
        ]),
    ])
    assert find([("window", root)], Criteria(query="go")).candidates[0].path == "Page"


def test_menubar_results_say_so():
    bar = UINode("menu", "AXMenuBar", children=[
        UINode("menu_item", "AXMenuBarItem", name="File", verbs=["expand"], path=(0,), children=[
            UINode("menu", "AXMenu", path=(0, 0), children=[
                UINode("menu_item", "AXMenuItem", name="Save…", verbs=["press"], path=(0, 0, 0)),
            ]),
        ]),
    ])
    roots = [("window", _window()), ("menubar", bar)]
    assert find(roots, Criteria(query="file")).candidates[0].path == "menu bar"
    save = find(roots, Criteria(query="save…", role="menu_item")).candidates[0]
    assert save.path == "menu bar › File"


# --- output -----------------------------------------------------------------

def _read(root):
    return UIRead(app="TextEdit", pid=42, window_id=9, window_title="Untitled", window=root)


def test_format_find():
    root = _window()
    out = format_find(_read(root), find([("window", root)], Criteria(query="Save"), limit=2))
    assert out.splitlines() == [
        '4 matches in TextEdit (pid 42) — window "Untitled" — showing top 2',
        '1. button "Save" press @884,612,68,24  score 1.00  path: Save As',
        '2. dialog "Save As"  score 0.84',
    ]


def test_format_ambiguous_and_empty():
    root = _window(extra=[btn("Save", (0, 4))])
    out = format_find(_read(root), find([("window", root)], Criteria(query="Save", role="button")))
    assert out.splitlines()[-1].startswith("⚠ top-2 scores within 0.10")
    empty = format_find(_read(root), find([("window", root)], Criteria(query="zebra")))
    assert empty.splitlines()[0].startswith("0 matches in TextEdit")
    assert empty.splitlines()[1].startswith("no element matched")


def test_filter_only_output_has_no_scores():
    root = _window()
    out = format_find(_read(root), find([("window", root)], Criteria(role="checkbox")))
    assert "score" not in out
