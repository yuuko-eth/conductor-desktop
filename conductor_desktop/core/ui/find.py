"""``ui_find``: exact filters, then fuzzy ranking over a ``UIRead``.

Scores are 0..1. Equality (case-insensitive, trailing "…" ignored) is 1.00;
whole-word containment beats plain substring, which beats a typo-level fuzzy
ratio. ``rapidfuzz`` (extra ``ui-fuzzy``) speeds the ratio up; stdlib
``difflib`` is the fallback because Haiku and FreeBSD can't install wheels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .format import node_line, target_label
from .model import UINode, UIRead

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:
    _fuzz = None

MATCH_MODES = ("auto", "fuzzy", "exact", "semantic")
AMBIGUITY_MARGIN = 0.10
MIN_SCORE = 0.5
PATH_SEGMENTS = 3
# Unnamed containers that add nothing to a path ("menu bar › File", not "… › File › menu").
_GENERIC_ROLES = frozenset({"group", "other", "scroll_area", "menu"})
_LABEL_MAX = 30


@dataclass
class Criteria:
    query: str | None = None
    role: str | None = None
    name: str | None = None
    value: str | None = None
    id: str | None = None
    actionable: bool | None = None
    states: list[str] = field(default_factory=list)

    @property
    def ranked(self) -> bool:
        return bool(self.query or self.name or self.value)

    @property
    def empty(self) -> bool:
        return not (self.ranked or self.role or self.id or self.actionable is not None or self.states)


@dataclass
class Candidate:
    node: UINode
    score: float
    path: str


@dataclass
class FindResult:
    candidates: list[Candidate]
    matched: int
    ranked: bool
    ambiguous: bool


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".…").rstrip()


def _ratio(a: str, b: str) -> float:
    sa, sb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    if _fuzz is not None:
        return max(_fuzz.ratio(a, b), _fuzz.ratio(sa, sb)) / 100
    return max(SequenceMatcher(None, a, b).ratio(), SequenceMatcher(None, sa, sb).ratio())


def similarity(needle: str, hay: str, exact: bool = False) -> float:
    n, h = _norm(needle), _norm(hay)
    if not n or not h:
        return 0.0
    if n == h:
        return 1.0
    if exact:
        return 0.0
    if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", h):
        return 0.7 + 0.25 * len(n) / len(h)
    if n in h:
        return 0.55 + 0.2 * len(n) / len(h)
    return 0.8 * _ratio(n, h)


_NUMBER_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _text_similarity(needle: str, hay: str, exact: bool) -> float:
    """``similarity``, also trying a single digit as its word: Windows
    Calculator names its keys "Four", not "4"."""
    score = similarity(needle, hay, exact)
    word = _NUMBER_WORDS.get(needle.strip())
    return max(score, similarity(word, hay, exact)) if word else score


def _role_ok(node: UINode, role: str) -> bool:
    wanted = {r.strip().lower() for r in role.split(",") if r.strip()}
    return node.role in wanted or node.native_role.lower() in wanted


def _states_ok(node: UINode, states: list[str]) -> bool:
    for state in states:
        state = state.strip().lower()
        if state.startswith("!"):
            if state[1:] in node.states:
                return False
        elif state not in node.states:
            return False
    return True


def _passes(node: UINode, c: Criteria) -> bool:
    if c.role and not _role_ok(node, c.role):
        return False
    if c.id is not None and node.id != c.id:
        return False
    if c.actionable is not None and node.actionable != c.actionable:
        return False
    return _states_ok(node, c.states)


def _score(node: UINode, ancestors: list[str], c: Criteria, exact: bool) -> float | None:
    parts: list[float] = []
    if c.name:
        parts.append(_text_similarity(c.name, node.title, exact))
    if c.value:
        parts.append(similarity(c.value, node.value, exact))
    if c.query:
        on_name = _text_similarity(c.query, node.title, exact)
        on_value = similarity(c.query, node.value, exact)
        if exact:
            parts.append(max(on_name, on_value))
        else:
            score = max(on_name, 0.95 * on_value)
            # Ancestor context only for multi-word queries ("sidebar documents");
            # for one word it would match every child of a same-named container.
            if len(c.query.split()) > 1 and ancestors and node.title:
                score = max(score, 0.8 * similarity(c.query, " ".join([*ancestors, node.title])))
            parts.append(score)
    if not parts:
        return 1.0
    score = min(parts)
    return score if score >= (1.0 if exact else MIN_SCORE) else None


def _label(node: UINode) -> str | None:
    if node.title:
        return node.title if len(node.title) <= _LABEL_MAX else node.title[:_LABEL_MAX] + "…"
    return None if node.role in _GENERIC_ROLES else node.role


def _path_label(ancestors: list[str]) -> str:
    if len(ancestors) > PATH_SEGMENTS:
        return "… › " + " › ".join(ancestors[-PATH_SEGMENTS:])
    return " › ".join(ancestors)


def _collect(node, ancestors, c, exact, found, is_root=False) -> None:
    # A text search is about what's inside the window; the root (already in the
    # header) only competes when a role filter asks for it. A Save sheet named
    # "save" otherwise ties with its own Save button.
    if (is_root and c.ranked and not c.role) or node.is_label:
        pass  # a label text is found through its control
    elif _passes(node, c):
        score = _score(node, ancestors, c, exact)
        if score is not None:
            found.append(Candidate(node, score, _path_label(ancestors)))
    label = None if is_root else _label(node)
    # Skip a label equal to its parent's (Chrome nests a group named like the page).
    below = [*ancestors, label] if label and (not ancestors or ancestors[-1] != label) else ancestors
    for child in node.children:
        _collect(child, below, c, exact, found)


def find(roots: list[tuple[str, UINode]], c: Criteria, *, limit: int = 10,
         match: str = "auto") -> FindResult:
    exact = match == "exact"
    found: list[Candidate] = []
    for kind, root in roots:
        _collect(root, ["menu bar"] if kind == "menubar" else [], c, exact, found, is_root=True)
    if c.ranked:
        # Stable sort: ties keep tree order, actionable elements first.
        found.sort(key=lambda cand: (-cand.score, not cand.node.actionable))
    ambiguous = (c.ranked and len(found) >= 2
                 and found[0].score - found[1].score < AMBIGUITY_MARGIN
                 # A static label beside its control isn't a second target.
                 and not (found[0].node.actionable and not found[1].node.actionable))
    return FindResult(found[:limit], len(found), c.ranked, ambiguous)


def format_find(read: UIRead, result: FindResult) -> str:
    n = result.matched
    head = f"{n} match{'es' if n != 1 else ''} in {target_label(read)}"
    if n > len(result.candidates):
        head += f" — showing top {len(result.candidates)}"
    if read.budget_hit:
        head += f" — walk budget hit after {read.timeout:g}s, partial search"
    lines = [head]
    for i, cand in enumerate(result.candidates, 1):
        line = f"{i}. {node_line(cand.node)}"
        if result.ranked:
            line += f"  score {cand.score:.2f}"
        if cand.path:
            line += f"  path: {cand.path}"
        lines.append(line)
    if result.ambiguous:
        lines.append(f"⚠ top-2 scores within {AMBIGUITY_MARGIN:.2f} — ambiguous; "
                     "narrow with role/title or read ui_tree")
    if n == 0:
        lines.append("no element matched; try other wording, fewer filters, "
                     "include_menubar=true, or read ui_tree")
    return "\n".join(lines)
