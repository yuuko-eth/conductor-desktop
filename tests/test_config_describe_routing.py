"""Split-backend routing: ground vs describe can target different VLMs.

Locks in the decision made when LocateAnything (a grounding-only model) landed:
ground/locate route to grounder_url+family; describe/describe_image route to an
optional separate describe backend, falling back to the ground backend when
unset. A grounding-only ground family with no separate describe backend must
fail fast at config time, not at the agent's first describe() call.
"""

from __future__ import annotations

import pytest

from conductor_desktop import config as config_mod
from conductor_desktop import grounder as grounder_mod
from conductor_desktop.config import MCPConfig
from conductor_desktop.grounder import VenusDialect, family_can_describe


@pytest.fixture(autouse=True)
def _clear_grounder_env(monkeypatch):
    for k in (
        "CONDUCTOR_DESKTOP_GROUNDER_URL",
        "CONDUCTOR_DESKTOP_GROUNDER_FAMILY",
        "CONDUCTOR_DESKTOP_DESCRIBE_URL",
        "CONDUCTOR_DESKTOP_DESCRIBE_FAMILY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "http://ground:8000")


# --- family_can_describe (single source of truth) -------------------------

def test_family_can_describe_matrix():
    assert family_can_describe("qwen")
    assert family_can_describe("gemma4")
    assert not family_can_describe("locateanything")


# --- default: one backend serves both -------------------------------------

def test_describe_defaults_to_ground_backend():
    c = MCPConfig.from_env()
    assert not c.describe_backend_is_separate
    assert c.effective_describe_url == "http://ground:8000"
    assert c.effective_describe_family == "qwen"


# --- LocateAnything ground family requires a separate describe backend ----

def test_locateanything_without_describe_fails_fast(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "locateanything")
    with pytest.raises(RuntimeError, match="grounding-only"):
        MCPConfig.from_env()


def test_locateanything_with_separate_describe_ok(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "locateanything")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    c = MCPConfig.from_env()
    assert c.grounder_family == "locateanything"
    assert c.describe_backend_is_separate
    assert c.effective_describe_url == "http://qwen:8000"
    assert c.effective_describe_family == "qwen"


# --- describe_family must itself be describe-capable -----------------------

def test_describe_family_grounding_only_rejected(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "locateanything")
    with pytest.raises(RuntimeError, match="grounding-only"):
        MCPConfig.from_env()


def test_invalid_describe_family_rejected(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "bogus")
    with pytest.raises(RuntimeError, match="invalid"):
        MCPConfig.from_env()


# --- describe-only families (minicpm) — mirror of the grounding-only case --

def test_minicpm_as_ground_family_rejected(monkeypatch):
    """minicpm is describe-only — setting it as the point backend fails fast."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "minicpm")
    with pytest.raises(RuntimeError, match="describe-only"):
        MCPConfig.from_env()


def test_minicpm_as_describe_family_ok(monkeypatch):
    """minicpm belongs on DESCRIBE_FAMILY, paired with a grounding point
    backend — the intended describe-only ensemble."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "qwen")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://minicpm:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "minicpm")
    c = MCPConfig.from_env()
    assert c.effective_describe_family == "minicpm"
    assert c.describe_backend_is_separate


# --- partial overrides -----------------------------------------------------

def test_describe_url_only_keeps_ground_family(monkeypatch):
    """describe_url alone overrides the URL but keeps grounder_family's
    describe dialect — a separate Qwen box mirroring the ground family."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://describe:8000")
    c = MCPConfig.from_env()
    assert c.describe_backend_is_separate
    assert c.effective_describe_url == "http://describe:8000"
    assert c.effective_describe_family == "qwen"


def test_describe_family_only_keeps_ground_url(monkeypatch):
    """describe_family alone overrides only the dialect on the same URL
    (e.g. a multi-model server)."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "qwen")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "gemma4")
    c = MCPConfig.from_env()
    assert c.describe_backend_is_separate
    assert c.effective_describe_url == "http://ground:8000"
    assert c.effective_describe_family == "gemma4"


# --- region routing (point-only grounder → region via describe) ------------

def test_region_aliases_ground_for_bbox_capable_family():
    """When the ground family does boxes (qwen/gemma/LA), region ops use the
    ground backend directly — unchanged behavior."""
    c = MCPConfig.from_env()  # qwen default
    assert c.effective_region_url == c.grounder_url
    assert c.effective_region_family == "qwen"


def test_region_aliases_describe_for_point_only_grounder(monkeypatch):
    """Holo is point-only — region ops must route to the (mandatory) describe
    backend, which is bbox-capable."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "holo")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    c = MCPConfig.from_env()
    assert c.effective_region_url == "http://qwen:8000"
    assert c.effective_region_family == "qwen"


def test_point_only_grounder_without_describe_fails_fast(monkeypatch):
    """Holo can't describe AND can't do regions — without a separate describe
    backend, config must fail (the describe-capable guard catches it)."""
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "holo")
    with pytest.raises(RuntimeError, match="grounding-only"):
        MCPConfig.from_env()


def test_point_only_grounder_with_describe_ok(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "holo")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    c = MCPConfig.from_env()
    assert c.grounder_family == "holo"
    assert c.effective_region_family == "qwen"


def test_point_only_region_backend_rejected(monkeypatch):
    """The §5.4 defensive guard: a point-only grounder whose region backend is
    ALSO point-only must be rejected. No shipped family is both describe-capable
    and point-only (so the describe-capable guard shadows this today), so we
    simulate a hypothetical future point-only-yet-describe-capable family to
    prove the explicit region guard fires rather than letting crop break."""
    fam = "futurepoint"
    # describe-capable AND point_only.
    monkeypatch.setitem(grounder_mod.DESCRIBE_DIALECTS, fam,
                        grounder_mod.DESCRIBE_DIALECTS["qwen"])
    monkeypatch.setitem(grounder_mod.DIALECTS, fam, VenusDialect(max_tokens=64))
    monkeypatch.setattr(
        config_mod, "_VALID_GROUNDER_FAMILIES",
        config_mod._VALID_GROUNDER_FAMILIES | {fam},
    )
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", fam)
    with pytest.raises(RuntimeError, match="point-only"):
        MCPConfig.from_env()
