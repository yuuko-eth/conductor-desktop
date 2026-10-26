"""Action verbs on a resolved element: check, perform, confirm by re-reading.

The platform adapter only performs; what counts as success and how it is
reported is decided here, so every platform answers the same way.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .format import _quote, node_line
from .model import UINode
from .walk import ElementSource, make_node

SETTLE_SECONDS = 1.0
POLL_SECONDS = 0.1


@dataclass
class Effect:
    """What an adapter did. ``verify_element`` redirects the re-read (a cell
    selected through its row); ``verifiable=False`` when nothing can be read
    back (a menu opened)."""

    error: str | None = None
    verify_element: Any = None
    verifiable: bool = True
    # The app may have acted but answered with an error (Finder's sidebar
    # cell navigates on AXOpen yet returns -25205). Not a failure.
    warning: str | None = None


class ActionAdapter(ElementSource, Protocol):
    def perform(self, element: Any, verb: str, node: UINode, params: dict) -> Effect: ...


@dataclass
class ActOutcome:
    status: str                  # done | stale_ref | ambiguous_ref | unsupported_action | action_failed
    verification: str = ""       # verified | mismatch | unverified (status "done" only)
    detail: str = ""
    node: UINode | None = None   # the element as resolved, before the action
    candidates: list[UINode] = field(default_factory=list)
    window_gone: bool = False


LABEL_SECONDS = 0.5


def _label(node: UINode) -> str:
    return f"{node.role} {_quote(node.title, 60)}" if node.title else node.role


def _borrow_label(adapter: ActionAdapter, element: Any, node: UINode, clock) -> None:
    """Give an unnamed control the label a read would show (core/ui/label.py)."""
    from .label import LABEL_DEPTH, derive_labels
    from .walk import walk

    sub, _ = walk(adapter, element, depth=LABEL_DEPTH, max_children=20,
                  deadline=clock() + LABEL_SECONDS, clock=clock)
    derive_labels(sub)
    node.label = sub.label


def _expectation(verb: str, node: UINode, params: dict):
    """``(predicate, verified_detail, mismatch_detail)`` or None when there is
    nothing to re-read."""
    wait = f"after {SETTLE_SECONDS:g}s"
    if verb == "toggle":
        on = params.get("on")

        def word(n: UINode) -> str:
            return "mixed" if "mixed" in n.states else "checked" if "checked" in n.states else "unchecked"

        if on is None and "mixed" in node.states:
            # From mixed, toolkits differ on the next state (GTK 3 goes to
            # unchecked); any definite state means the toggle took.
            return (lambda n: "mixed" not in n.states, "no longer mixed",
                    lambda n: f"still {word(n)} {wait}")
        target = ("checked" not in node.states) if on is None else bool(on)
        want = "checked" if target else "unchecked"
        return (lambda n: ("checked" in n.states) == target and "mixed" not in n.states,
                f"now {want}", lambda n: f"still {word(n)} {wait}")
    if verb == "select":
        # A radio reports its selection as checked (AXValue 1, UIA toggle
        # state, AT-SPI "checked"), so selected alone never arrives for it.
        def picked(n: UINode) -> bool:
            return "selected" in n.states or (n.role == "radio" and "checked" in n.states)

        return (picked, "now selected", lambda n: f"not selected {wait}")
    if verb == "expand":
        target = bool(params.get("expand", True))
        word = "expanded" if target else "collapsed"
        return (lambda n: ("expanded" in n.states) == target, f"now {word}",
                lambda n: f"not {word} {wait}")
    if verb == "set_value":
        if "protected" in node.states:
            return None
        want = str(params.get("value", ""))

        def same(n: UINode) -> bool:
            if n.value == want[:500]:
                return True
            try:
                return abs(float(n.value) - float(want)) < 1e-6
            except ValueError:
                return False

        return (same, f"value is now {_quote(want, 80)}",
                lambda n: f"value reads back as {_quote(n.value, 80)}")
    if verb == "focus":
        return (lambda n: "focused" in n.states, "now focused", lambda n: f"not focused {wait}")
    return None


_UNVERIFIED = {
    "press": "pressed (unverified: a press leaves no state to re-read; check the deltas or re-read)",
    "set_value": "value set (unverified: protected field, not read back)",
    "expand": "opened (unverified)",
    "focus": "raised (unverified)",
    "scroll_to": "scrolled into view (unverified)",
    "toggle": "toggled (unverified)",
    "select": "selected (unverified)",
}


def run_action(
    adapter: ActionAdapter,
    element: Any,
    node: UINode,
    verb: str,
    params: dict,
    *,
    settle: float = SETTLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ActOutcome:
    if not node.name and node.actionable:
        _borrow_label(adapter, element, node, clock)
    if verb not in node.verbs:
        return ActOutcome("unsupported_action", node=node)
    on = params.get("on")
    if verb == "toggle" and on is not None and ("checked" in node.states) == bool(on):
        word = "checked" if on else "unchecked"
        return ActOutcome("done", "verified", f"already {word}; nothing to do", node=node)

    effect = adapter.perform(element, verb, node, params)
    if effect.error:
        return ActOutcome("action_failed", detail=effect.error, node=node)

    expectation = _expectation(verb, node, params) if effect.verifiable else None
    if expectation is None:
        detail = _UNVERIFIED[verb]
        if verb == "expand" and not params.get("expand", True):
            detail = "closed (unverified)"
        if effect.warning:
            detail = (f"sent, but {effect.warning}; it may have taken effect anyway, "
                      "so re-read before retrying (unverified)")
        return ActOutcome("done", "unverified", detail, node=node)

    predicate, verified, mismatch = expectation
    target = effect.verify_element if effect.verify_element is not None else element
    deadline = clock() + settle
    while True:
        after = make_node(adapter, target, node.path)
        if predicate(after):
            return ActOutcome("done", "verified", verified, node=node)
        if clock() >= deadline:
            detail = mismatch(after)
            if effect.warning:
                detail += f" ({effect.warning})"
            return ActOutcome("done", "mismatch", detail, node=node)
        sleep(POLL_SECONDS)


def _click_hint(node: UINode | None) -> str:
    if node is None or not node.bounds or node.bounds[2] <= 0 or node.bounds[3] <= 0:
        return ""
    x, y, w, h = node.bounds
    return f" Its bounds are @{x},{y},{w},{h}; click_at({x + w // 2}, {y + h // 2}) clicks its center."


def format_outcome(ref: str, verb: str, outcome: ActOutcome, *, moved: bool = False) -> tuple[bool, str]:
    """``(ok, text)``. A mismatch is ok: the action ran, and the text says it didn't take."""
    tool = f"ui_{verb}"
    node = outcome.node
    label = _label(node) if node is not None else ""
    if outcome.status == "done":
        detail = outcome.detail
        if outcome.verification == "verified":
            detail += " (verified)"
        elif outcome.verification == "mismatch":
            detail = f"mismatch: {detail}"
        text = f"{tool} {ref} — {label} — {detail}"
        if moved:
            text += " — the element had moved; ref updated"
        return True, text
    if outcome.status in ("stale_ref", "ambiguous_ref"):
        text = f"{outcome.status}: {ref} — {outcome.detail}."
        if outcome.candidates:
            heading = "matching elements" if outcome.status == "ambiguous_ref" else "similar elements"
            text += f" {heading}:\n" + "\n".join(f"  {node_line(c)}" for c in outcome.candidates)
            text += "\n"
        else:
            text += " "
        return False, text + "Run ui_find or ui_tree for fresh refs."
    if outcome.status == "unsupported_action":
        can = f"can {', '.join(node.verbs)}" if node and node.verbs else "has no accessibility actions"
        return False, f"unsupported_action: {ref} — {label} {can}, not {verb}.{_click_hint(node)}"
    return False, f"action_failed: {tool} {ref} — {label} — {outcome.detail}.{_click_hint(node)}"
