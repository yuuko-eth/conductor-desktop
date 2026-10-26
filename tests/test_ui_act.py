"""Ref resolution and action verbs over a fake element tree."""

from __future__ import annotations

from dataclasses import dataclass, field

from conductor_desktop.core.ui.act import Effect, format_outcome, run_action
from conductor_desktop.core.ui.ax import normalize
from conductor_desktop.core.ui.model import RawNode
from conductor_desktop.core.ui.refs import Locator
from conductor_desktop.core.ui.resolve import resolve
from conductor_desktop.core.ui.walk import make_node

FAR = float("inf")


@dataclass(eq=False)
class El:
    role: str
    name: str = ""
    id: str = ""
    kids: list = field(default_factory=list)
    subrole: str = ""
    value: str = ""
    actions: list = field(default_factory=list)
    checked: int | None = None
    selected: bool | None = None
    expanded: bool | None = None
    focused: bool | None = None
    settable: bool = False
    protected: bool = False
    bounds: tuple | None = None
    parent: El | None = None


class Fake:
    """ElementSource + ActionAdapter. ``inert`` makes actions do nothing."""

    normalize = staticmethod(normalize)

    def __init__(self, inert=False, error=None, warning=None):
        self.inert, self.error, self.warning = inert, error, warning
        self.described = 0
        self.performed = []

    def describe(self, el):
        self.described += 1
        return RawNode(el.role, subrole=el.subrole, name=el.name, value=el.value, id=el.id,
                       actions=el.actions, checked=el.checked, selected=el.selected,
                       expanded=el.expanded, focused=el.focused, value_settable=el.settable,
                       protected=el.protected, bounds=el.bounds)

    def children(self, el):
        return el.kids

    def child_count(self, el):
        return len(el.kids)

    def perform(self, el, verb, node, params):
        self.performed.append((el, verb, params))
        if self.error:
            return Effect(self.error)
        if self.inert:
            return Effect(warning=self.warning)
        if verb == "toggle":
            el.checked = 0 if el.checked == 1 else 1
        elif verb == "select":
            if el.role == "AXCell":
                el.parent.selected = True
                return Effect(verify_element=el.parent, warning=self.warning)
            el.selected = True
        elif verb == "set_value":
            el.value = params["value"]
        return Effect(warning=self.warning)


def tree(*kids):
    root = El("AXWindow", name="Doc", kids=list(kids))
    for k in kids:
        k.parent = root
    return root


def loc_for(root, el, path):
    node = make_node(Fake(), el, path)
    return Locator(pid=1, window_id=7, root="window", path=path, native_role=node.native_role,
                   name=node.name, id=node.id, window_title="Doc")


def save_button(**kw):
    return El("AXButton", name="Save", actions=["AXPress"], **kw)


def test_toggle_from_mixed_verifies_any_definite_state():
    # GTK 3's "Enable anti-aliasing" starts mixed and goes to unchecked.
    box = El("AXCheckBox", name="Enable anti-aliasing", actions=["AXPress"], checked=2)
    root = tree(box)

    class ToUnchecked(Fake):
        def perform(self, el, verb, node, params):
            el.checked = 0
            return Effect()

    out = run_action(ToUnchecked(), box, make_node(Fake(), box, (0,)), "toggle", {})
    assert (out.verification, out.detail) == ("verified", "no longer mixed")
    assert root.kids == [box]


def test_toggle_that_stays_mixed_is_a_mismatch():
    box = El("AXCheckBox", name="Enable anti-aliasing", actions=["AXPress"], checked=2)
    out = run_action(Fake(inert=True), box, make_node(Fake(), box, (0,)), "toggle", {},
                     settle=0, sleep=lambda s: None)
    assert out.verification == "mismatch" and out.detail.startswith("still mixed")


# --- resolve ----------------------------------------------------------------

def test_path_hit_resolves():
    save = save_button()
    root = tree(El("AXStaticText", value="hi"), save)
    res = resolve(Fake(), root, loc_for(root, save, (1,)), deadline=FAR)
    assert res.status == "ok" and res.element is save


def test_moved_element_is_found_by_search():
    save = save_button()
    loc = loc_for(tree(save), save, (0,))
    root = tree(El("AXStaticText", value="new"), El("AXGroup", kids=[save]))
    res = resolve(Fake(), root, loc, deadline=FAR)
    assert res.status == "moved" and res.node.path == (1, 0)


def test_id_wins_over_a_renamed_title():
    save = save_button(id="save-btn")
    root = tree(save)
    loc = loc_for(root, save, (0,))
    save.name = "Save…"
    assert resolve(Fake(), root, loc, deadline=FAR).status == "ok"


def test_stale_lists_similar_elements():
    save = save_button()
    loc = loc_for(tree(save), save, (0,))
    root = tree(El("AXButton", name="Save As", actions=["AXPress"]), El("AXButton", name="Cancel"))
    res = resolve(Fake(), root, loc, deadline=FAR)
    assert res.status == "stale" and [c.name for c in res.candidates] == ["Save As"]


def test_several_matches_are_ambiguous():
    save = save_button()
    loc = loc_for(tree(save), save, (0,))
    root = tree(El("AXGroup"), El("AXGroup", kids=[save_button()]), save_button())
    res = resolve(Fake(), root, loc, deadline=FAR)
    assert res.status == "ambiguous" and len(res.candidates) == 2


def test_unnamed_element_off_its_path_is_stale_without_a_search():
    area = El("AXTextArea")
    loc = loc_for(tree(area), area, (0,))
    fake = Fake()
    res = resolve(fake, tree(El("AXGroup")), loc, deadline=FAR)
    assert res.status == "stale" and fake.described == 1  # only the path check


def test_search_stops_at_the_deadline():
    save = save_button()
    loc = loc_for(tree(save), save, (0,))
    root = tree(El("AXGroup"), save)
    assert resolve(Fake(), root, loc, deadline=0, clock=lambda: 1).status == "stale"


# --- actions ----------------------------------------------------------------

def _node(el, path=(0,)):
    return make_node(Fake(), el, path)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_toggle_flips_and_verifies():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0)
    out = run_action(Fake(), box, _node(box), "toggle", {})
    assert (out.status, out.verification, out.detail) == ("done", "verified", "now checked")


def test_toggle_to_a_state_that_already_holds_does_nothing():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=1)
    fake = Fake()
    out = run_action(fake, box, _node(box), "toggle", {"on": True})
    assert out.detail == "already checked; nothing to do" and fake.performed == []


def test_inert_action_is_a_mismatch_after_settling():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0)
    clock = _Clock()
    out = run_action(Fake(inert=True), box, _node(box), "toggle", {}, clock=clock, sleep=clock.sleep)
    assert out.verification == "mismatch" and out.detail == "still unchecked after 1s"
    assert clock.t >= 1.0


def test_set_value_reads_back():
    field_ = El("AXTextField", settable=True, focused=False)
    out = run_action(Fake(), field_, _node(field_), "set_value", {"value": "hello"})
    assert out.verification == "verified" and out.detail == 'value is now "hello"'


def test_protected_value_is_not_read_back_or_echoed():
    pw = El("AXTextField", subrole="AXSecureTextField", settable=True, focused=False, protected=True)
    out = run_action(Fake(), pw, _node(pw), "set_value", {"value": "hunter2"})
    ok, text = format_outcome("e5", "set_value", out)
    assert out.verification == "unverified" and ok and "hunter2" not in text


def test_select_can_verify_through_another_element():
    row = El("AXRow", subrole="AXOutlineRow", selected=False)
    cell = El("AXCell", selected=False, actions=["AXOpen"], parent=row)
    row.kids = [cell]
    out = run_action(Fake(), cell, _node(cell), "select", {})
    assert out.verification == "verified" and row.selected


def test_press_is_unverified():
    save = save_button()
    out = run_action(Fake(), save, _node(save), "press", {})
    assert out.verification == "unverified" and out.detail.startswith("pressed")


def test_unsupported_verb_gives_a_click_hint():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0, bounds=(10, 20, 16, 16))
    fake = Fake()
    out = run_action(fake, box, _node(box), "press", {})
    ok, text = format_outcome("e3", "press", out)
    assert not ok and fake.performed == []
    assert text == ('unsupported_action: e3 — checkbox "Wrap" can toggle, not press. '
                    "Its bounds are @10,20,16,16; click_at(18, 28) clicks its center.")


def test_press_with_an_uncertain_error_is_not_a_failure():
    save = save_button()
    out = run_action(Fake(warning="AXOpen answered AX error -25205"), save, _node(save), "press", {})
    ok, text = format_outcome("e15", "press", out)
    assert ok and out.verification == "unverified"
    assert text == ('ui_press e15 — button "Save" — sent, but AXOpen answered AX error -25205; '
                    "it may have taken effect anyway, so re-read before retrying (unverified)")


def test_warning_does_not_block_verification():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0)
    out = run_action(Fake(warning="AXPress answered AX error -25204"), box, _node(box), "toggle", {})
    assert out.verification == "verified"
    stuck = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0)  # box above is now checked
    clock = _Clock()
    inert = run_action(Fake(inert=True, warning="AXPress answered AX error -25204"), stuck, _node(stuck),
                       "toggle", {}, clock=clock, sleep=clock.sleep)
    assert inert.detail == "still unchecked after 1s (AXPress answered AX error -25204)"


def test_action_failure_is_reported():
    save = save_button()
    ok, text = format_outcome("e9", "press", run_action(Fake(error="AXPress failed (AX error -25204)"),
                                                        save, _node(save), "press", {}))
    assert not ok and text == 'action_failed: ui_press e9 — button "Save" — AXPress failed (AX error -25204).'


# --- output -----------------------------------------------------------------

def test_done_text_notes_verification_and_moves():
    box = El("AXCheckBox", name="Wrap", actions=["AXPress"], checked=0)
    out = run_action(Fake(), box, _node(box), "toggle", {})
    assert format_outcome("e3", "toggle", out, moved=True) == (
        True, 'ui_toggle e3 — checkbox "Wrap" — now checked (verified) — the element had moved; ref updated')
    out.verification, out.detail = "mismatch", "still unchecked after 1s"
    assert format_outcome("e3", "toggle", out)[1].endswith("— mismatch: still unchecked after 1s")


def test_stale_text_lists_candidates():
    from conductor_desktop.core.ui.act import ActOutcome
    out = ActOutcome("stale_ref", detail="that element is no longer in the window",
                     candidates=[_node(save_button())])
    ok, text = format_outcome("e4", "press", out)
    assert not ok and text.splitlines() == [
        "stale_ref: e4 — that element is no longer in the window. similar elements:",
        '  button "Save" press',
        "Run ui_find or ui_tree for fresh refs.",
    ]


def test_select_on_a_radio_verifies_through_checked():
    radio = El("AXRadioButton", name="Large", checked=0, actions=["AXPress"])
    tree(radio)

    class Radio(Fake):
        def perform(self, el, verb, node, params):
            el.checked = 1  # radios report selection as checked, never selected
            return Effect()

    out = run_action(Radio(), radio, _node(radio), "select", {})
    assert out.verification == "verified"
