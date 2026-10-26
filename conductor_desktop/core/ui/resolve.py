"""Ref resolution: turn a locator back into a live element, or say why not.

1. Follow the native child-index path and check the element is the same one
   (id when it has one, else role + name).
2. Otherwise search the window: exactly one match means the element moved;
   several mean the ref is ambiguous; none means it's stale, reported with
   similarly named elements of the same role.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .find import similarity
from .model import UINode
from .refs import Locator
from .walk import ElementSource, make_node

MAX_HITS = 5
MAX_CANDIDATES = 3
_SIMILAR = 0.6


@dataclass
class Resolution:
    status: str                  # "ok" | "moved" | "ambiguous" | "stale"
    element: Any = None
    node: UINode | None = None
    candidates: list[UINode] = field(default_factory=list)


def matches_locator(node: UINode, loc: Locator) -> bool:
    if node.native_role != loc.native_role:
        return False
    if loc.id:
        return node.id == loc.id
    return node.name == loc.name


def _follow(source: ElementSource, root: Any, path: tuple[int, ...]) -> Any:
    element = root
    for i in path:
        kids = source.children(element)
        if i >= len(kids):
            return None
        element = kids[i]
    return element


def search(
    source: ElementSource,
    root: Any,
    visit: Callable[[Any, UINode], bool],
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    max_depth: int = 40,
    max_children: int = 500,
) -> None:
    """Depth-first, in tree order; stops when ``visit`` returns True or time runs out."""
    stack: list[tuple[Any, tuple[int, ...]]] = [(root, ())]
    while stack:
        if clock() >= deadline:
            return
        element, path = stack.pop()
        if visit(element, make_node(source, element, path)):
            return
        if len(path) < max_depth:
            kids = source.children(element)[:max_children]
            stack.extend((kids[i], (*path, i)) for i in reversed(range(len(kids))))


def resolve(
    source: ElementSource,
    root: Any,
    loc: Locator,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> Resolution:
    element = _follow(source, root, loc.path)
    if element is not None:
        node = make_node(source, element, loc.path)
        if matches_locator(node, loc):
            return Resolution("ok", element, node)
    if not (loc.id or loc.name):
        # An unnamed element without an id can only be found by its path.
        return Resolution("stale")

    exact: list[tuple[Any, UINode]] = []
    similar: list[UINode] = []

    def visit(el: Any, node: UINode) -> bool:
        if matches_locator(node, loc):
            exact.append((el, node))
            return len(exact) >= MAX_HITS
        if (loc.name and len(similar) < MAX_CANDIDATES and node.native_role == loc.native_role
                and similarity(loc.name, node.name) >= _SIMILAR):
            similar.append(node)
        return False

    search(source, root, visit, deadline=deadline, clock=clock)
    if len(exact) == 1:
        return Resolution("moved", *exact[0])
    if exact:
        return Resolution("ambiguous", candidates=[node for _, node in exact])
    return Resolution("stale", candidates=similar)
