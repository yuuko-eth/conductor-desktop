"""ui_tree outline (folding, markers, escaping) and JSON renderings."""

from __future__ import annotations

import json

from conductor_desktop.core.ui.format import format_json, format_outline
from conductor_desktop.core.ui.model import UINode, UIRead


def _read(window, menubar=None, **kw):
    return UIRead(app="TextEdit", pid=42, window_id=9, window_title="Untitled",
                  window=window, menubar=menubar, nodes=6, elapsed=0.12, **kw)


def _window():
    save = UINode("button", "AXButton", name="Save", verbs=["press"],
                  bounds=(884, 612, 68, 24), ref="e2", path=(0, 0, 0))
    wrapper = UINode("group", "AXGroup", path=(0, 0), children=[save])
    outer = UINode("group", "AXGroup", path=(0,), children=[wrapper])
    empty = UINode("group", "AXGroup", path=(1,))
    text = UINode("text_area", "AXTextArea", verbs=["set_value", "focus", "scroll_to"],
                  states=["focused"], value='line1\nline2 "q"', bounds=(0, 0, 10, 10),
                  ref="e3", path=(2,))
    label = UINode("static_text", "AXStaticText", value="x" * 600, bounds=(1, 1, 1, 1), path=(3,))
    toolbar = UINode("toolbar", "AXToolbar", path=(4,), omitted_children=12, children=[
        UINode("button", "AXButton", name="A", verbs=["press"], ref="e4", path=(4, 0)),
        UINode("button", "AXButton", name="B", path=(4, 1)),
    ])
    sidebar = UINode("group", "AXGroup", name="Sidebar", ref="e5", path=(5,), unwalked_children=3)
    return UINode("window", "AXWindow", name="Untitled", ref="e1", verbs=["focus"],
                  bounds=(0, 25, 800, 600),
                  children=[outer, empty, text, label, toolbar, sidebar])


def test_golden_outline():
    assert format_outline(_read(_window())) == "\n".join([
        'TextEdit (pid 42) — window "Untitled" — 6 nodes in 0.12s',
        'window "Untitled" [e1] focus @0,25,800,600',
        '  button "Save" [e2] press @884,612,68,24',
        '  text_area [e3] set_value focus focused @0,0,10,10 "line1\\nline2 \\"q\\""',
        '  static_text "' + "x" * 80 + '…"',
        '  toolbar',
        '    button "A" [e4] press',
        '    button "B"',
        '    …12 more children (max_children)',
        '  group "Sidebar" [e5]',
        '    …3 children below the depth limit (depth)',
    ])


def test_budget_marker_and_header():
    win = UINode("window", "AXWindow", name="W", budget_skipped=4, children=[
        UINode("button", "AXButton", name="x", verbs=["press"]),
    ])
    lines = format_outline(_read(win, budget_hit=True, timeout=10.0)).splitlines()
    assert lines[0].endswith("— walk budget hit after 10s, partial tree")
    assert lines[-1] == "  …4 more children not read (walk budget)"


def test_untitled_window_header_uses_the_root():
    sheet = UINode("dialog", "AXWindow", name="save")
    read = UIRead(app="TextEdit", pid=42, window_id=None, window_title="", window=sheet)
    assert format_outline(read).splitlines()[0].startswith('TextEdit (pid 42) — dialog "save" —')
    read.window = UINode("window", "AXWindow")
    assert "— untitled window —" in format_outline(read).splitlines()[0]


def test_root_is_never_folded():
    win = UINode("window", "AXWindow", children=[UINode("button", "AXButton", name="x")])
    assert format_outline(_read(win)).splitlines()[1] == "window"


def test_menubar_section_and_no_window_header():
    bar = UINode("menu", "AXMenuBar", children=[
        UINode("menu_item", "AXMenuBarItem", name="File", verbs=["expand"], ref="e9"),
    ])
    lines = format_outline(_read(None, menubar=bar)).splitlines()
    assert "— no window —" in lines[0]
    assert lines[1:] == ["menu bar:", "menu", '  menu_item "File" [e9] expand']


def test_json_is_unfolded_with_native_paths_and_no_empty_fields():
    d = json.loads(format_json(_read(_window())))
    assert (d["app"], d["pid"], d["window_id"]) == ("TextEdit", 42, 9)
    outer = d["window"]["children"][0]
    assert outer == {
        "role": "group", "native_role": "AXGroup", "path": [0],
        "children": [{"role": "group", "native_role": "AXGroup", "path": [0, 0], "children": [
            {"role": "button", "native_role": "AXButton", "name": "Save", "ref": "e2",
             "verbs": ["press"], "bounds": [884, 612, 68, 24], "path": [0, 0, 0]},
        ]}],
    }
    assert len(d["window"]["children"][3]["value"]) == 500
    assert d["window"]["children"][4]["omitted_children"] == 12
    assert "menubar" not in d
