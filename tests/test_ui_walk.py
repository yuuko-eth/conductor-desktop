"""Platform-neutral ui walker: native paths and the three truncation budgets."""

from __future__ import annotations

from conductor_desktop.core.ui.ax import normalize
from conductor_desktop.core.ui.model import RawNode
from conductor_desktop.core.ui.walk import walk

FAR = float("inf")


class FakeSource:
    normalize = staticmethod(normalize)

    def describe(self, el):
        return RawNode(el.get("role", "AXGroup"), name=el["name"], actions=el.get("actions", []))

    def children(self, el):
        return el["kids"]

    def child_count(self, el):
        return len(el["kids"])


def n(name="", kids=(), **kw):
    return {"name": name, "kids": list(kids), **kw}


def test_paths_are_native_child_indices():
    tree = n("root", [n("a"), n("b", [n("c"), n("d")])])
    root, stats = walk(FakeSource(), tree, depth=8, max_children=50, deadline=FAR)
    d = root.children[1].children[1]
    assert d.name == "d" and d.path == (1, 1)
    assert stats.nodes == 5 and not stats.budget_hit


def test_depth_limit_counts_unwalked_children():
    tree = n("root", [n("a"), n("b", [n("c"), n("d")])])
    root, _ = walk(FakeSource(), tree, depth=1, max_children=50, deadline=FAR)
    assert root.children[1].children == [] and root.children[1].unwalked_children == 2


def test_max_children_counts_omitted_siblings():
    root, _ = walk(FakeSource(), n("root", [n(str(i)) for i in range(5)]),
                   depth=8, max_children=2, deadline=FAR)
    assert [c.name for c in root.children] == ["0", "1"] and root.omitted_children == 3


def test_descend_hook_leaves_children_unread():
    tree = n("root", [n("web", [n("x"), n("y")], role="AXWebArea"), n("z")])
    root, stats = walk(FakeSource(), tree, depth=8, max_children=50, deadline=FAR,
                       descend=lambda el, node: node.native_role != "AXWebArea")
    web = root.children[0]
    assert web.children == [] and not web.truncated
    assert root.children[1].name == "z" and stats.nodes == 3


def test_free_nodes_do_not_count_toward_depth():
    # GTK 4: a button under three unnamed panels is one meaningful level down.
    tree = n("root", [n("", [n("", [n("", [n("4", role="AXButton", actions=["AXPress"])])])])])
    wrapper = lambda node: not node.name  # noqa: E731
    root, _ = walk(FakeSource(), tree, depth=1, max_children=50, deadline=FAR, free=wrapper)
    button = root.children[0].children[0].children[0].children[0]
    assert button.name == "4" and button.path == (0, 0, 0, 0)
    # Without the hook the depth limit stops at the first panel.
    root, _ = walk(FakeSource(), tree, depth=1, max_children=50, deadline=FAR)
    assert root.children[0].children == [] and root.children[0].unwalked_children == 1


def test_free_nodes_still_stop_at_the_native_cap():
    from conductor_desktop.core.ui.walk import MAX_NATIVE_DEPTH

    tree = n("leaf")
    for _ in range(MAX_NATIVE_DEPTH + 5):
        tree = n("", [tree])
    root, stats = walk(FakeSource(), tree, depth=1, max_children=50, deadline=FAR,
                       free=lambda node: True)
    assert stats.nodes == MAX_NATIVE_DEPTH + 1


def _ticking_clock():
    t = iter(range(1000))
    return lambda: next(t)


def test_budget_stops_reading_and_counts_skipped():
    root, stats = walk(FakeSource(), n("root", [n(str(i)) for i in range(4)]),
                       depth=8, max_children=50, deadline=2, clock=_ticking_clock())
    assert len(root.children) == 2 and root.budget_skipped == 2 and stats.budget_hit


def test_budget_and_max_children_are_counted_separately():
    root, _ = walk(FakeSource(), n("root", [n(str(i)) for i in range(5)]),
                   depth=8, max_children=3, deadline=1, clock=_ticking_clock())
    assert len(root.children) == 1
    assert root.budget_skipped == 2 and root.omitted_children == 2
