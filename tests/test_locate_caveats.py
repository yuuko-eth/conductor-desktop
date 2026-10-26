"""Trust signals on a single `locate` hit.

A single-best (point-only) grounder answers every query with one guess: it
never emits N candidates, so the ambiguity refusal cannot fire, and it rarely
emits no-match, so an absent target still returns a confident point. Observed
live 2026-09-08 on the managed ensemble — `locate("the Blender 3D modelling
viewport")` with Blender not running returned three different dock coordinates
across three runs. These tests pin the caveats that make that visible, and the
element-level `point` flag they depend on, which is the only point-only signal
that survives a managed gateway.
"""

from __future__ import annotations

import json

import pytest

from conductor_desktop.grounder import GroundedElement
from conductor_desktop.runtime.grounding import ScreenElement
from conductor_desktop.tools import grounded, observation

pytestmark = pytest.mark.anyio


class _FakeConfig:
    def __init__(self, family: str) -> None:
        self.grounder_family = family


class _FakeState:
    def __init__(self, family: str = "managed") -> None:
        self.config = _FakeConfig(family)


@pytest.fixture
def managed(monkeypatch, install_state):
    """A managed gateway: the client cannot know which family served us."""
    install_state(_FakeState("managed"))


# --- the flag rides on the element, not the config ------------------------

def test_point_flag_survives_managed_gateway(managed):
    """`family_is_point_only("managed")` is False, so a config-derived check
    goes silent exactly where the caveat matters most."""
    from conductor_desktop.grounder import family_is_point_only
    assert not family_is_point_only("managed")
    assert observation.is_point_hit(
        ScreenElement(bbox=(490, 490, 510, 510), description="", point=True))


def test_bbox_grounder_under_managed_gets_no_point_caveat(managed):
    e = ScreenElement(bbox=(100, 100, 400, 300), description="a card")
    assert not observation.is_point_hit(e)
    assert observation.grounding_caveats(False, {"focused": True},
                                          {"w": []}) == []


def test_synthesized_point_box_is_flagged_at_the_source():
    """`_point_to_element` is the one place a fake bbox is minted."""
    from conductor_desktop.grounder import _point_to_element
    assert _point_to_element(500, 500).point is True
    assert GroundedElement(bbox=(0, 0, 1, 1), description="").point is False


# --- the three caveats ----------------------------------------------------

def test_point_only_hit_warns_that_a_result_is_not_existence(managed):
    e = ScreenElement(bbox=(490, 490, 510, 510), description="", point=True)
    warn = observation.grounding_caveats(observation.is_point_hit(e),
                                          {"focused": True}, {"w": []})
    assert any("not evidence the target exists" in w for w in warn)


def test_hit_outside_every_window_is_flagged(managed):
    """The Blender signature: a confident point owned by no window."""
    ScreenElement(bbox=(906, 758, 926, 771), description="", point=True)
    warn = observation.grounding_caveats(True, {}, {"w": []})
    assert any("no known window" in w for w in warn)


def test_hit_in_a_background_window_names_the_app_and_the_fix(managed):
    scene = {"windows": {"1": {"id": "1", "focused": True}, "2": {"id": "2", "focused": False}}}
    warn = observation.grounding_caveats(
        True, {"w": "2", "app": "Terminal", "focused": False}, scene)
    assert any("background window (Terminal)" in w and "focus_window" in w
               for w in warn)


def test_no_background_warning_when_focus_is_on_an_unlisted_overlay(managed):
    """Windows Start menu: it holds focus but isn't in the window list, so the
    Terminal under a tile's point isn't a wrong-window hit (pi run 2026-09-15)."""
    scene = {"windows": {"2": {"id": "2", "focused": False}}}
    warn = observation.grounding_caveats(
        True, {"w": "2", "app": "WindowsTerminal.exe", "focused": False}, scene)
    assert not any("background window" in w for w in warn)


def test_blocked_owner_is_flagged(managed):
    warn = observation.grounding_caveats(
        True, {"app": "Finder", "focused": True, "blk": 1}, {"w": []})
    assert any("blocked by a modal" in w for w in warn)


def test_no_scene_means_no_window_caveats(managed):
    """Mobile / failed snapshot: absent context must not read as bad context."""
    warn = observation.grounding_caveats(True, {}, None)
    assert not any("no known window" in w for w in warn)


# --- the payload ----------------------------------------------------------

async def test_locate_payload_carries_candidates_and_warn(managed, monkeypatch):
    async def _fake_g2se(target, device_id=None, screen=None, grounder=None):
        return [ScreenElement(bbox=(490, 490, 510, 510), description="",
                               point=True)]

    async def _fake_scene(device_id):
        return None

    monkeypatch.setattr(observation, "ground_to_screen_elements", _fake_g2se)
    monkeypatch.setattr(observation, "scene_snapshot", _fake_scene)
    monkeypatch.setattr(observation, "screen_origin", lambda scene, screen: (0, 0))

    out = await observation.locate("the Blender 3D modelling viewport")
    payload = json.loads(out.content[0].text)
    # 1 candidate means the grounder claimed uniqueness — not that uniqueness
    # was verified. The caveat is what carries that distinction.
    assert payload["candidates"] == 1
    assert payload["extent"] == "point"
    assert any("not evidence the target exists" in w for w in payload["warn"])


# --- the action path ------------------------------------------------------

class _FakeResult:
    success = True
    output = "ok"


class _HeldLock:
    def is_held(self):
        return True


async def test_click_emits_the_caveat_it_gathered_before_acting(monkeypatch, install_state):
    """A click on an absent target still lands somewhere and still reports
    success; the caveat is the only thing that makes it visible."""
    calls: list[str] = []

    class _Executor:
        async def click(self, x, y, button="left", clicks=1, screen=None):
            calls.append(f"click({x},{y})")
            return _FakeResult()

    class _State:
        config = _FakeConfig("managed")
        executor = _Executor()
        lock = _HeldLock()

    hit = ScreenElement(bbox=(906, 758, 926, 771), description="", point=True)

    async def _fake_resolve(target, device_id=None, screen=None):
        return hit

    async def _fake_caveats(e, screen=None, device_id=None, scene=None):
        assert not calls, "caveats must be gathered before the action fires"
        return ["single-best grounder: ... not evidence the target exists"]

    async def _fake_obs(device_id=None):
        return []

    install_state(_State())
    monkeypatch.setattr(grounded, "resolve_one", _fake_resolve)
    monkeypatch.setattr(grounded, "hit_caveats", _fake_caveats)
    monkeypatch.setattr(grounded, "post_action_observation", _fake_obs)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", _fake_obs)

    out = await grounded.click("the Blender 3D modelling viewport")
    text = "\n".join(c.text for c in out.content)
    assert "click(left,1) at (916,764)" in text
    assert "warn: single-best grounder" in text


async def test_clean_hit_emits_no_warn_lines(monkeypatch, install_state):
    class _Executor:
        async def click(self, x, y, button="left", clicks=1, screen=None):
            return _FakeResult()

    class _State:
        config = _FakeConfig("qwen")
        executor = _Executor()
        lock = _HeldLock()

    async def _fake_resolve(target, device_id=None, screen=None):
        return ScreenElement(bbox=(100, 100, 400, 300), description="Submit")

    async def _fake_caveats(e, screen=None, device_id=None, scene=None):
        return []

    async def _fake_obs(device_id=None):
        return []

    install_state(_State())
    monkeypatch.setattr(grounded, "resolve_one", _fake_resolve)
    monkeypatch.setattr(grounded, "hit_caveats", _fake_caveats)
    monkeypatch.setattr(grounded, "post_action_observation", _fake_obs)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", _fake_obs)

    out = await grounded.click("the Submit button")
    assert not any("warn:" in c.text for c in out.content)


# --- managed mode infers `point` from geometry ----------------------------

def test_gateway_point_box_is_recognised():
    """Real bboxes from the staging gateway, 1280x800, 2026-09-08 — a 16-unit
    box, wider than the 14 this client mints. Tying the bound to
    `_POINT_BOX_HALF` would miss all of these."""
    from conductor_desktop.grounder import is_synthetic_point_box

    def to_norm(x1, y1, x2, y2):
        return (round(x1 / 1280 * 1000), round(y1 / 800 * 1000),
                round(x2 / 1280 * 1000), round(y2 / 800 * 1000))

    for px in [(791, 760, 811, 772), (906, 758, 926, 771),
               (366, 758, 386, 771), (3, 532, 24, 545)]:
        assert is_synthetic_point_box(to_norm(*px)), px


def test_a_real_element_bbox_is_not_a_point_box():
    from conductor_desktop.grounder import is_synthetic_point_box
    # The address-bar hit from the same session: 88x19px -> 69x24 norm1000.
    assert not is_synthetic_point_box((706, 31, 775, 55))
    assert not is_synthetic_point_box((100, 100, 400, 300))


def test_managed_grounder_flags_point_hits():
    """The gateway says nothing about which family served a request, so the
    managed client infers a point hit from the box's size."""
    import asyncio

    import httpx

    from conductor_desktop.managed import GatewayClient
    from conductor_desktop.managed_grounder import ManagedGrounder

    def handler(request):
        return httpx.Response(200, json={"coord_system": "norm1000", "candidates": [
            {"bbox": {"X1": 618, "Y1": 950, "X2": 634, "Y2": 965}, "description": ""},
            {"bbox": {"X1": 100, "Y1": 100, "X2": 400, "Y2": 300}, "description": "a card"},
        ]})

    g = ManagedGrounder(gateway=GatewayClient(
        "http://gw.test", "cgr_live_tok", 5.0, what="managed grounder",
        timeout_env="X", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    els = asyncio.run(g.ground("Zm9v", "the clock"))
    assert [e.point for e in els] == [True, False]
