"""Capability-based region routing (Phase 2).

Region ops (crop, locate(multiple)) need a real bbox extent, so when the click
grounder is point-only (Holo) they route to the region backend (= describe)
instead of the point grounder. click / locate(single) stay on the precise
point grounder. These tests assert which backend each op fires, the
point-extent marking on locate(single), and the diagnose three-role report.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from conductor_desktop.config import MCPConfig
from conductor_desktop.runtime.grounding import ScreenElement
from conductor_desktop.tools import diagnostics, observation


class _FakeGrounder:
    """Records every ground() target so a test can assert which backend fired."""

    def __init__(self, name: str, elements: list) -> None:
        self.name = name
        self._elements = elements
        self.calls: list[str] = []

    async def ground(self, image_b64: str, target: str) -> list:
        self.calls.append(target)
        return list(self._elements)


class _FakeConfig:
    def __init__(self, grounder_family: str = "holo") -> None:
        self.grounder_family = grounder_family
        self.screenshot_max_width = 0
        self.coord_system = "norm1000"
        self.text_only = False
        self.screenshot_mode = "inline"
        self.payload_max_edge = 0


class _FakeState:
    def __init__(self, grounder, region_grounder, describe_grounder,
                 grounder_family="holo") -> None:
        self.grounder = grounder
        self.region_grounder = region_grounder
        self.describe_grounder = describe_grounder
        self.config = _FakeConfig(grounder_family)


def _png_b64(w: int = 100, h: int = 100) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 40, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def wire(monkeypatch, install_state):
    """Wire a fake state into both observation and server modules and stub
    capture so no real screen/grounder is touched. Returns the state."""
    from conductor_desktop.grounder import GroundedElement

    point = _FakeGrounder("point", [GroundedElement(bbox=(490, 490, 510, 510),
                                                    description="")])
    region = _FakeGrounder("region", [GroundedElement(bbox=(100, 100, 400, 300),
                                                      description="a card")])
    describe = region  # in the Holo ensemble region aliases describe
    state = _FakeState(point, region, describe, grounder_family="holo")

    install_state(state)

    async def _fake_capture_b64(device_id, screen=None):
        return _png_b64()

    monkeypatch.setattr(observation, "capture_b64", _fake_capture_b64)
    return state


# --- crop routes to the region backend ------------------------------------

async def test_crop_uses_region_backend_under_holo(wire):
    out = await observation.crop("a card")
    # crop grounded via the region backend, NOT the point grounder.
    assert wire.region_grounder.calls == ["a card"]
    assert wire.grounder.calls == []
    # And it returned a real crop (text pointer + image), not an error.
    assert any("crop:" in c.text for c in out.content if hasattr(c, "text"))


async def test_crop_uses_ground_backend_when_not_point_only(monkeypatch, install_state):
    """For a bbox-capable ground family, region aliases ground — crop fires the
    same backend as click (region_grounder IS grounder)."""
    from conductor_desktop.grounder import GroundedElement

    shared = _FakeGrounder("shared", [GroundedElement(bbox=(100, 100, 400, 300),
                                                      description="a card")])
    state = _FakeState(shared, shared, shared, grounder_family="qwen")
    install_state(state)

    async def _fake_capture_b64(device_id, screen=None):
        return _png_b64()

    monkeypatch.setattr(observation, "capture_b64", _fake_capture_b64)
    await observation.crop("a card")
    assert shared.calls == ["a card"]


# --- locate(multiple) routes to region; locate(single) stays on point ------

async def test_locate_multiple_passes_region_grounder(wire, monkeypatch):
    captured = {}

    async def _fake_g2se(target, device_id=None, screen=None, grounder=None):
        captured["grounder"] = grounder
        return []

    monkeypatch.setattr(observation, "ground_to_screen_elements", _fake_g2se)
    await observation.locate("the tabs", multiple=True)
    assert captured["grounder"] is wire.region_grounder


async def test_locate_single_uses_point_grounder(wire, monkeypatch):
    captured = {}

    async def _fake_g2se(target, device_id=None, screen=None, grounder=None):
        captured["grounder"] = grounder
        return []

    monkeypatch.setattr(observation, "ground_to_screen_elements", _fake_g2se)
    await observation.locate("the close button", multiple=False)
    # None → ground_to_screen_elements defaults to the point grounder.
    assert captured["grounder"] is None


# --- locate(single) marks the bbox as point-extent under Holo --------------

async def test_locate_single_marks_point_extent_under_holo(wire, monkeypatch):

    async def _fake_g2se(target, device_id=None, screen=None, grounder=None):
        return [ScreenElement(bbox=(490, 490, 510, 510), description="")]

    monkeypatch.setattr(observation, "ground_to_screen_elements", _fake_g2se)
    out = await observation.locate("the close button", multiple=False)
    payload = json.loads(out.content[0].text)
    assert payload["x"] == 500 and payload["y"] == 500
    assert payload["extent"] == "point"
    assert "real extent" in payload["note"]


async def test_locate_single_no_extent_marker_for_qwen(monkeypatch, install_state):

    state = _FakeState(_FakeGrounder("g", []), _FakeGrounder("r", []),
                       _FakeGrounder("d", []), grounder_family="qwen")
    install_state(state)

    async def _fake_g2se(target, device_id=None, screen=None, grounder=None):
        return [ScreenElement(bbox=(100, 100, 400, 300), description="x")]

    monkeypatch.setattr(observation, "ground_to_screen_elements", _fake_g2se)
    out = await observation.locate("a card", multiple=False)
    payload = json.loads(out.content[0].text)
    assert "extent" not in payload
    assert payload["bbox"] == [100, 100, 400, 300]


# --- diagnose three-role report -------------------------------------------

@pytest.fixture(autouse=False)
def _clear_env(monkeypatch):
    for k in ("CONDUCTOR_DESKTOP_GROUNDER_URL", "CONDUCTOR_DESKTOP_GROUNDER_FAMILY",
              "CONDUCTOR_DESKTOP_DESCRIBE_URL", "CONDUCTOR_DESKTOP_DESCRIBE_FAMILY"):
        monkeypatch.delenv(k, raising=False)


def test_diagnose_roles_qwen_only(monkeypatch, _clear_env):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "http://q:8000")
    cfg = MCPConfig.from_env()
    lines = diagnostics._grounding_role_lines(cfg)
    text = "\n".join(lines)
    assert "point:" in text and "describe:" in text and "region:" in text
    # Single server → describe and region both shared with point.
    assert "shared with point" in next(ln for ln in lines if ln.startswith("describe"))
    assert "shared with point" in next(ln for ln in lines if ln.startswith("region"))
    assert "ensemble: qwen" in text
    assert "commercial-safe" in text


def test_diagnose_roles_qwen_holo(monkeypatch, _clear_env):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "http://holo:8930")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "holo")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8922")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    cfg = MCPConfig.from_env()
    lines = diagnostics._grounding_role_lines(cfg)
    point_line = next(ln for ln in lines if ln.startswith("point"))
    region_line = next(ln for ln in lines if ln.startswith("region"))
    assert "holo:8930" in point_line and "(holo)" in point_line
    # Region routes to the describe (qwen) server, flagged as shared.
    assert "qwen:8922" in region_line and "shared with describe" in region_line
    text = "\n".join(lines)
    assert "ensemble: qwen+holo" in text
    assert "commercial-safe" in text


def test_diagnose_roles_locateanything_non_commercial(monkeypatch, _clear_env):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "http://la:8921")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "locateanything")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8922")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    cfg = MCPConfig.from_env()
    text = "\n".join(diagnostics._grounding_role_lines(cfg))
    # LA does boxes → region aliases ground (LA), not describe.
    assert "la:8921" in text
    assert "non-commercial" in text
    assert "[non-commercial]" in text  # LA role line shows its license
