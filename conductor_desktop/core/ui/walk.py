"""Platform-neutral a11y walker with depth, sibling and time budgets.

A backend supplies an ``ElementSource`` over its native elements; the walker
owns paths and truncation so every platform reports them the same way.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .model import RawNode, UINode


class ElementSource(Protocol):
    def describe(self, element: Any) -> RawNode: ...
    def children(self, element: Any) -> list[Any]: ...
    def child_count(self, element: Any) -> int: ...
    def normalize(self, raw: RawNode) -> tuple[str, list[str], list[str]]: ...


# Native levels a walk never goes past, whatever ``free`` lets through.
MAX_NATIVE_DEPTH = 64


@dataclass
class WalkStats:
    nodes: int = 0
    budget_hit: bool = False


def make_node(source: ElementSource, element: Any, path: tuple[int, ...]) -> UINode:
    """Describe and normalize one element into a childless ``UINode``."""
    raw = source.describe(element)
    role, verbs, states = source.normalize(raw)
    return UINode(
        role=role, native_role=raw.native_role, name=raw.name, value=raw.value,
        id=raw.id, verbs=verbs, states=states, bounds=raw.bounds, path=path,
        row_range=raw.row_range, col_range=raw.col_range,
    )


def walk(
    source: ElementSource,
    element: Any,
    *,
    depth: int,
    max_children: int,
    deadline: float,
    stats: WalkStats | None = None,
    clock: Callable[[], float] = time.monotonic,
    descend: Callable[[Any, UINode], bool] | None = None,
    free: Callable[[UINode], bool] | None = None,
) -> tuple[UINode, WalkStats]:
    """Walk ``element``. ``deadline`` is a ``clock()`` value; once passed, the
    walk stops reading and records how many children each open node skipped.

    ``descend(element, node)`` returning False leaves that node's children
    unread, for a backend that fills them another way (a native search).

    ``free(node)`` returning True means the node doesn't count toward
    ``depth``: GTK 4 nests unnamed panels ten or twenty deep, so ``depth``
    counts meaningful levels there. ``MAX_NATIVE_DEPTH`` still bounds the walk.
    """
    stats = stats if stats is not None else WalkStats()
    root = _walk(source, element, (), 0, depth, max_children, deadline, clock, stats, descend, free)
    return root, stats


def _walk(source, element, path, level, depth, max_children, deadline, clock, stats, descend,
          free) -> UINode:
    """``level`` is the parent's depth; this node adds one unless it's the root or free."""
    node = make_node(source, element, path)
    stats.nodes += 1
    if path and not (free is not None and free(node)):
        level += 1
    if descend is not None and not descend(element, node):
        return node
    if level >= depth or len(path) >= MAX_NATIVE_DEPTH:
        node.unwalked_children = source.child_count(element)
        return node

    kids = source.children(element)
    if len(kids) > max_children:
        node.omitted_children = len(kids) - max_children
        kids = kids[:max_children]
    for i, kid in enumerate(kids):
        if clock() >= deadline:
            stats.budget_hit = True
            node.budget_skipped = len(kids) - i
            break
        node.children.append(_walk(source, kid, (*path, i), level, depth, max_children,
                                   deadline, clock, stats, descend, free))
    return node
