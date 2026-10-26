"""RefTable: which nodes get refs, reuse across reads, and eviction."""

from __future__ import annotations

from conductor_desktop.core.ui.model import UINode, iter_nodes
from conductor_desktop.core.ui.refs import RefTable


def _tree(save_path=(0,)):
    return UINode("window", "AXWindow", name="Doc", children=[
        UINode("button", "AXButton", name="Save", verbs=["press"], path=save_path),
        UINode("group", "AXGroup", path=(1,), children=[
            UINode("button", "AXButton", verbs=["press"], path=(1, 0)),
        ]),
        UINode("static_text", "AXStaticText", value="hi", path=(2,)),
        UINode("link", "AXLink", verbs=["scroll_to"], path=(3,)),
    ])


def _assign(table, root, window_id=7, title="Doc", kind="window"):
    table.assign([(kind, root)], pid=1, window_id=window_id, window_title=title)
    return root


def _save(root):
    return root.children[0].ref


def test_refs_go_to_named_or_actionable_nodes():
    table = RefTable()
    root = _tree()
    assert table.assign([("window", root)], pid=1, window_id=7, window_title="Doc") == 3
    with_refs = [(nd.role, nd.name) for nd in iter_nodes(root) if nd.ref]
    assert with_refs == [("window", "Doc"), ("button", "Save"), ("button", "")]


def test_also_gives_refs_to_extra_nodes():
    table = RefTable()
    root = _tree()
    group = root.children[1]
    table.assign([("window", root)], pid=1, window_id=7, window_title="Doc", also=[group])
    assert group.ref and table.get(group.ref).path == (1,)


def test_relocate_points_the_ref_at_the_new_path():
    table = RefTable()
    ref = _save(_assign(table, _tree()))
    table.relocate(ref, (5,))
    assert table.get(ref).path == (5,)
    assert _save(_assign(table, _tree(save_path=(5,)))) == ref  # re-reading finds the same ref


def test_forget_window_drops_its_refs():
    table = RefTable()
    ref = _save(_assign(table, _tree(), window_id=7))
    other = _save(_assign(table, _tree(), window_id=8))
    table.forget_window(1, 7, "Doc")
    assert table.get(ref) is None and table.get(other) is not None


def test_locator_keeps_native_path_and_window():
    table = RefTable()
    root = _assign(table, _tree())
    loc = table.get(root.children[1].children[0].ref)
    assert (loc.pid, loc.window_id, loc.root, loc.path) == (1, 7, "window", (1, 0))


def test_rereading_keeps_refs():
    table = RefTable()
    assert _save(_assign(table, _tree())) == _save(_assign(table, _tree()))


def test_title_change_keeps_refs_when_window_id_known():
    table = RefTable()
    assert _save(_assign(table, _tree())) == _save(_assign(table, _tree(), title="Doc — Edited"))


def test_moved_element_gets_a_new_ref():
    table = RefTable()
    assert _save(_assign(table, _tree())) != _save(_assign(table, _tree(save_path=(5,))))


def test_menubar_root_is_distinct_from_window():
    table = RefTable()
    assert _save(_assign(table, _tree())) != _save(_assign(table, _tree(), kind="menubar"))


def test_refs_expire_after_reads_per_window():
    table = RefTable(reads_per_window=2)
    old = _save(_assign(table, _tree()))
    _assign(table, _tree(save_path=(9,)))
    assert table.get(old) is not None
    window_ref = _assign(table, _tree(save_path=(9,))).ref
    assert table.get(old) is None
    assert table.get(window_ref) is not None  # still in the two live reads


def test_window_cap_evicts_the_oldest_window():
    table = RefTable(max_windows=2)
    first = _save(_assign(table, _tree(), window_id=1))
    _assign(table, _tree(), window_id=2)
    _assign(table, _tree(), window_id=3)
    assert table.get(first) is None


def test_title_keys_windows_without_an_id():
    table = RefTable()
    a = _save(_assign(table, _tree(), window_id=None, title="A"))
    b = _save(_assign(table, _tree(), window_id=None, title="B"))
    assert a == b  # same locator (title isn't part of it) …
    assert len(table._reads) == 2  # … but tracked as two windows
