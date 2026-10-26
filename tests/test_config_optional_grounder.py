"""The grounder URL is optional: the server boots without it, and each role
(ground / describe / region) is configured or not on its own."""

from __future__ import annotations

import pytest

from conductor_desktop.config import MCPConfig


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("CONDUCTOR_DESKTOP_GROUNDER_URL", "CONDUCTOR_DESKTOP_GROUNDER_FAMILY",
              "CONDUCTOR_DESKTOP_DESCRIBE_URL", "CONDUCTOR_DESKTOP_DESCRIBE_FAMILY",
              "CONDUCTOR_DESKTOP_API_KEY", "CONDUCTOR_DESKTOP_COORD_SYSTEM"):
        monkeypatch.delenv(k, raising=False)


def test_boots_with_no_vlm_at_all():
    c = MCPConfig.from_env()
    assert (c.grounder_configured, c.describe_configured, c.region_configured) == (False, False, False)


def test_describe_only_backend_also_serves_regions_when_it_can_box(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    c = MCPConfig.from_env()
    assert not c.grounder_configured
    assert c.describe_configured and c.effective_describe_url == "http://qwen:8000"
    assert c.region_configured and c.effective_region_url == "http://qwen:8000"
    assert c.effective_region_family == "qwen"


def test_describe_only_family_gives_no_regions(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://minicpm:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "minicpm")
    c = MCPConfig.from_env()
    assert c.describe_configured and not c.region_configured


def test_grounding_only_describe_family_is_still_rejected(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://la:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "locateanything")
    with pytest.raises(RuntimeError, match="grounding-only"):
        MCPConfig.from_env()


def test_managed_mode_still_needs_the_gateway_url(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "managed")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_API_KEY", "cgr_live_x")
    with pytest.raises(RuntimeError, match="CONDUCTOR_DESKTOP_GROUNDER_URL"):
        MCPConfig.from_env()


def test_diagnose_reports_unconfigured_roles_without_a_fake_ensemble(monkeypatch):
    from conductor_desktop.tools.diagnostics import _grounding_role_lines

    lines = _grounding_role_lines(MCPConfig.from_env())
    assert [line.split()[0] for line in lines] == ["point:", "describe:", "region:", "ensemble:"]
    assert all("not configured" in line for line in lines[:3])
    assert lines[3].startswith("ensemble: none")

    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_URL", "http://qwen:8000")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "qwen")
    lines = _grounding_role_lines(MCPConfig.from_env())
    assert "not configured" in lines[0] and "http://qwen:8000" in lines[1]
    assert lines[3].startswith("ensemble: qwen")


def test_full_config_is_unchanged(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "http://ground:8000")
    c = MCPConfig.from_env()
    assert c.grounder_configured and c.describe_configured and c.region_configured
    assert c.effective_region_url == "http://ground:8000"


# --- environment validation ---------------------------------------------

def test_a_bad_choice_names_the_variable_and_the_options(monkeypatch):
    from conductor_desktop.config import MCPConfig

    for var, value, word in [("CONDUCTOR_DESKTOP_TRANSPORT", "websocket", "transport"),
                             ("CONDUCTOR_DESKTOP_BACKEND", "teleport", "backend"),
                             ("CONDUCTOR_DESKTOP_AUDIT_ARGS", "everything", "audit_args"),
                             ("CONDUCTOR_DESKTOP_SCREENSHOT_MODE", "jpeg", "screenshot_mode")]:
        monkeypatch.setenv(var, value)
        with pytest.raises(RuntimeError, match=f"{var}={value!r} invalid"):
            MCPConfig.from_env()
        monkeypatch.delenv(var)
        assert word  # the field each one feeds


def test_a_bad_number_is_named_instead_of_silently_defaulted(monkeypatch):
    # _env_int used to swallow this and use the default, so a typo'd port
    # meant the server quietly listened somewhere else.
    from conductor_desktop.config import MCPConfig

    monkeypatch.setenv("CONDUCTOR_DESKTOP_PORT", "87 65")
    with pytest.raises(RuntimeError, match="CONDUCTOR_DESKTOP_PORT='87 65' is not a int"):
        MCPConfig.from_env()
    monkeypatch.delenv("CONDUCTOR_DESKTOP_PORT")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_TIMEOUT", "quick")
    with pytest.raises(RuntimeError, match="is not a float"):
        MCPConfig.from_env()


def test_an_empty_number_falls_back_to_the_default(monkeypatch):
    from conductor_desktop.config import MCPConfig

    monkeypatch.setenv("CONDUCTOR_DESKTOP_PORT", "")
    assert MCPConfig.from_env().port == 8765


def test_delta_diagnostics_are_config_not_a_stray_env_read(monkeypatch):
    from conductor_desktop.config import MCPConfig

    assert MCPConfig.from_env().delta_diag is False
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DELTA_DIAG", "true")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DELTA_DIAG_FILE", "/tmp/diag.log")
    cfg = MCPConfig.from_env()
    assert cfg.delta_diag is True and cfg.delta_diag_file == "/tmp/diag.log"
