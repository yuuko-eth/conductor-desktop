"""Labels for unnamed controls, borrowed from their one text child.

Finder's sidebar rows have no name; the words live on a static text two levels
down (row → cell → "Documents"). The outermost unnamed actionable element
whose subtree holds exactly one static text gets that text as ``label``, and
the text node is marked ``is_label`` so outlines and searches show the control
once instead of three times.

``name`` is never changed: locators match on the native name.
"""

from __future__ import annotations

from .model import UINode

LABEL_DEPTH = 3


def _texts(node: UINode, depth: int, out: list[UINode]) -> bool:
    """Collect static texts below ``node``; False when the subtree can't
    yield a single trustworthy label (more than one text, or truncated)."""
    if node.truncated:
        return False
    for child in node.children:
        if child.role == "static_text" and (child.value or child.name):
            out.append(child)
            if len(out) > 1:
                return False
        if depth > 1 and not _texts(child, depth - 1, out):
            return False
    return True


def _hide_echoes(node: UINode, text: str, depth: int) -> None:
    """Mark static texts below a named control that only repeat its name
    (GTK draws a button's name as a label inside it)."""
    for child in node.children:
        if child.role == "static_text" and (child.value or child.name).strip().lower() == text:
            child.is_label = True
        if depth > 1:
            _hide_echoes(child, text, depth - 1)


def derive_labels(node: UINode) -> None:
    if not node.name and node.actionable and node.children:
        texts: list[UINode] = []
        if _texts(node, LABEL_DEPTH, texts) and len(texts) == 1:
            node.label = texts[0].value or texts[0].name
            texts[0].is_label = True
            return
    if node.name and node.actionable and node.children:
        _hide_echoes(node, node.name.strip().lower(), LABEL_DEPTH)
    for child in node.children:
        derive_labels(child)
