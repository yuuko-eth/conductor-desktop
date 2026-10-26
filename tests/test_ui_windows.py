"""Windows ui_* backend helpers that don't need UIA."""

from __future__ import annotations

import pytest

from conductor_desktop.core.backends.windows_ui import pick_window

WINDOWS = [
    ("user@host: ~/Notepad notes", "WindowsTerminal", True),
    ("Untitled - Notepad", "Notepad", True),
    ("Notepad", "ApplicationFrameHost", True),
    ("Calculator", "ApplicationFrameHost", True),
]


@pytest.mark.parametrize("title, index", [
    ("notepad", 2),        # exact title beats the app name and substrings
    ("Notepad.exe", None),
    ("untitled", 1),       # substring
    ("calc", 3),
    ("windowsterminal", 0),  # app name
    (None, None),
])
def test_pick_window(title, index):
    assert pick_window(WINDOWS, title) == index


def test_app_name_beats_title_substring():
    windows = [("Notes about Slack", "Code", True), ("general", "slack", True)]
    assert pick_window(windows, "slack") == 1


def test_app_name_prefers_the_main_window_over_a_frontmost_popup():
    windows = [("PopupHost", "Notepad", False), ("Untitled - Notepad", "Notepad", True)]
    assert pick_window(windows, "notepad") == 1


def test_exact_title_still_reaches_an_owned_dialog():
    windows = [("Save as", "Notepad", False), ("Untitled - Notepad", "Notepad", True)]
    assert pick_window(windows, "save as") == 0


def test_popup_is_used_when_it_is_the_only_match():
    assert pick_window([("PopupHost", "Notepad", False)], "notepad") == 0


class _COMError(Exception):
    def __init__(self, hresult):
        super().__init__(hresult, None, (None, None, None, 0, None))
        self.hresult = hresult


def test_retry_waits_out_a_briefly_disabled_element():
    from conductor_desktop.core.backends import windows_ui as w

    calls = []

    def toggle():
        calls.append(1)
        if len(calls) < 3:
            raise _COMError(w._UIA_E_ELEMENTNOTENABLED)

    w._retry_not_enabled(toggle, sleep=lambda s: None)
    assert len(calls) == 3


def test_retry_gives_up_and_raises_other_errors_at_once():
    from conductor_desktop.core.backends import windows_ui as w

    now = [0.0]

    def clock():
        now[0] += 0.5
        return now[0]

    def disabled():
        raise _COMError(w._UIA_E_ELEMENTNOTENABLED)

    with pytest.raises(_COMError):
        w._retry_not_enabled(disabled, sleep=lambda s: None, clock=clock)

    calls = []

    def gone():
        calls.append(1)
        raise _COMError(w._UIA_E_ELEMENTNOTAVAILABLE)

    with pytest.raises(_COMError):
        w._retry_not_enabled(gone, sleep=lambda s: None)
    assert len(calls) == 1


def test_skipped_menu_bar_points_at_include_menubar():
    from conductor_desktop.core.ui.format import format_outline
    from conductor_desktop.core.ui.model import UINode, UIRead

    bar = UINode("menu", "MenuBar", menubar_skipped=3, path=(1,))
    win = UINode("window", "Window", name="Untitled - Notepad", children=[bar])
    text = format_outline(UIRead("Notepad", 1, 2, "Untitled - Notepad", win))
    assert "…3 menu bar items not read (include_menubar=true)" in text
    assert "depth limit" not in text


# ── The grid path builds nodes by hand, so they must normalize the same ──
# `GridPattern.GetItem` never tree-walks, so `_read_cell` and friends build
# UINodes directly. They go through core/ui/uia.normalize for exactly this
# reason: a hand-built cell and a walked one have to carry the same verbs.

def _node(native_role, **kw):
    from conductor_desktop.core.backends import windows as w
    return w._ui_node(native_role, **kw)


def test_a_synthesized_grid_cell_is_a_cell():
    node = _node("GridCell", value="12%", row_range=(3, 1), col_range=(1, 1))
    assert node.role == "cell"
    assert (node.row_range, node.col_range) == ((3, 1), (1, 1))


def test_a_writable_cell_offers_set_value():
    # A spreadsheet cell with a non-read-only ValuePattern is settable without
    # ground→click; that is what read_grid reports as can_act. Excel reports
    # its cells as DataItem — a *row* — so the grid coordinates have to be what
    # says "cell", not the ControlType.
    cell = _node("DataItem", patterns=["Value"], settable=True, row_range=(0, 1),
                 col_range=(0, 1))
    assert "set_value" in cell.verbs
    assert "set_value" not in _node("DataItem", patterns=["Value"],
                                    row_range=(0, 1)).verbs
    # A row that is not in a grid and says nothing about a column stays a row.
    assert "set_value" not in _node("DataItem", patterns=["Value"], settable=True).verbs


def test_an_invokable_cell_presses():
    assert _node("DataItem", patterns=["Invoke"]).verbs == ["press"]


def test_a_leaf_child_keeps_its_text_role():
    # Cell text falls through to Edit/Text children, which the extractor finds
    # by normalized role.
    assert _node("Edit", value="Discord").role == "text_field"
    assert _node("Text", value="12%").role == "static_text"


def test_explicit_states_win_over_derived_ones():
    # The container is marked focused from the grid probe, not from a RawNode.
    assert _node("DataGrid", states=["focused"]).states == ["focused"]
    assert _node("DataGrid").states == []
