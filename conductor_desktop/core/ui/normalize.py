"""What the three platform normalizers do the same way.

`ax.py`, `atspi.py` and `uia.py` each turn one platform's element into the
shared `(role, verbs, states)`. Two thirds of that is the same work — look
the native role up in a table, collect verbs without repeating one, read the
notable states off the `RawNode` — and only verb *derivation* is genuinely
per-platform, because AX names actions, AT-SPI lists them and UIA exposes
patterns. Those three live in their own modules; everything else lives here.
"""

from __future__ import annotations

from collections.abc import Callable

from .model import RawNode


def role_table(
    roles: dict[str, str], subroles: dict[tuple[str, str], str],
) -> Callable[[str, str], str]:
    """A `role_of(native_role, subrole="")` over one platform's tables.

    A subrole is the more specific answer when the platform gives one — a
    macOS radio button whose subrole is AXTabButton is a tab, not a radio —
    so it wins. Anything unmapped is ``"other"``, which is a real answer:
    the element exists and can still carry verbs.
    """
    def role_of(native_role: str, subrole: str = "") -> str:
        return subroles.get((native_role, subrole)) or roles.get(native_role, "other")
    return role_of


class Verbs:
    """The verbs an element supports, in the order they were found, once each.

    Order matters — the formatters print it, and the first verb reads as the
    element's primary one — so this is a list with a membership check rather
    than a set.
    """

    def __init__(self) -> None:
        self._verbs: list[str] = []

    def add(self, verb: str) -> None:
        if verb not in self._verbs:
            self._verbs.append(verb)

    def __contains__(self, verb: object) -> bool:
        return verb in self._verbs

    def __bool__(self) -> bool:
        return bool(self._verbs)

    def list(self) -> list[str]:
        return list(self._verbs)


def states_of(raw: RawNode, role: str) -> list[str]:
    """The notable states of one element.

    Only what is worth saying: ``disabled``, never ``enabled``. That is what
    keeps an outline line short enough to read.

    A checked tab is a *selected* tab — the platforms report tab selection
    through the checked/toggle attribute, and "checked" would read as a
    checkbox. Selection is said once however it was learnt.
    """
    states: list[str] = []
    if raw.enabled is False:
        states.append("disabled")
    if raw.focused:
        states.append("focused")
    if raw.checked == 1:
        states.append("selected" if role == "tab" else "checked")
    elif raw.checked == 2:
        states.append("mixed")
    if raw.selected and "selected" not in states:
        states.append("selected")
    if raw.expanded:
        states.append("expanded")
    if raw.protected:
        states.append("protected")
    return states
