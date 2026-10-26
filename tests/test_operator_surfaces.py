"""The dashboard, tray and audit log must not hand out what the audit policy hides."""

from __future__ import annotations

import os
import stat
import sys
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from conductor_desktop import state as state_mod
from conductor_desktop.audit import SENSITIVE_ARGS, AuditLog
from conductor_desktop.config import MCPConfig
from conductor_desktop.dashboard.routes import _host_of, build_starlette_app
from conductor_desktop.tray_state import TrayStateMachine


def _client(monkeypatch, *, bind: str = "127.0.0.1", host_header: str = "127.0.0.1:8765", **cfg):
    config = MCPConfig(grounder_url="", host=bind, **cfg)
    monkeypatch.setattr(state_mod, "_current", SimpleNamespace(
        config=config, tray_state=TrayStateMachine(), lock=SimpleNamespace(is_held=lambda: True),
    ))
    return TestClient(build_starlette_app(), base_url=f"http://{host_header}")


def test_config_endpoint_masks_secrets(monkeypatch):
    client = _client(monkeypatch, api_key="cgr_live_supersecret",
                     connection_uri="vnc://user:hunter2@10.0.0.50:5900")
    body = client.get("/dashboard/api/config").text
    assert "supersecret" not in body
    assert "hunter2" not in body
    assert "***@10.0.0.50:5900" in body


@pytest.mark.parametrize(("bind", "host_header", "status"), [
    ("127.0.0.1", "127.0.0.1:8765", 200),
    ("127.0.0.1", "localhost:8765", 200),
    ("127.0.0.1", "attacker.example:8765", 403),   # DNS rebinding
    ("0.0.0.0", "192.168.1.20:8765", 200),         # deliberate LAN bind
])
def test_dashboard_refuses_foreign_host_on_loopback_bind(monkeypatch, bind, host_header, status):
    client = _client(monkeypatch, bind=bind, host_header=host_header)
    assert client.get("/dashboard/api/config").status_code == status


@pytest.mark.parametrize(("header", "host"), [
    ("127.0.0.1:8765", "127.0.0.1"), ("localhost", "localhost"),
    ("[::1]:8765", "::1"), ("::1", "::1"), ("evil.example:80", "evil.example"),
])
def test_host_header_parsing(header, host):
    # TestClient can't send an IPv6 Host, so the parser is checked on its own.
    assert _host_of(header) == host


def test_tray_and_dashboard_redact_typed_text(monkeypatch):
    client = _client(monkeypatch)
    tray = state_mod._current.tray_state
    tray.end(tray.start("type_text", kwargs={"text": "hunter2"}), success=True)
    tray.end(tray.start("click", kwargs={"target": "Save button"}), success=True)

    body = client.get("/dashboard/api/tools/recent").text
    assert "hunter2" not in body
    assert "<redacted:7 chars>" in body
    assert "Save button" in body


def test_audit_redacts_element_js(tmp_path):
    log = AuditLog(enabled=True, directory=tmp_path)
    log.record(tool="web_mark", klass="acting", ok=True, elapsed=0.0,
               kwargs={"element_js": "document.cookie"})
    log.close()
    (logfile,) = tmp_path.glob("audit-*.jsonl")
    assert "document.cookie" not in logfile.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_audit_files_are_owner_only(tmp_path):
    log = AuditLog(enabled=True, directory=tmp_path / "logs")
    log.record(tool="type_text", klass="acting", ok=True, elapsed=0.0, kwargs={"text": "x"})
    log.close()
    (logfile,) = (tmp_path / "logs").glob("audit-*.jsonl")
    assert stat.S_IMODE(os.stat(tmp_path / "logs").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(logfile).st_mode) == 0o600


# Tool parameters that carry a target, selector or identifier rather than a
# payload. Everything else that is string-typed must be in SENSITIVE_ARGS.
NOT_SENSITIVE = {
    "browser", "button", "color", "contains_text", "device_id", "direction", "format",
    "from_target", "hint", "id", "image_path", "key", "keys", "kind", "match", "match_title",
    "match_url", "name", "package_or_intent", "query", "ref", "role", "scope",
    "screen", "selector", "sort", "states", "tab", "target", "title", "to_target",
    "wait_for_selector",
}


def _string_tool_params() -> dict[str, str]:
    import inspect

    import conductor_desktop.tools  # noqa: F401  (declares the tools)
    from conductor_desktop import tooling

    found: dict[str, str] = {}
    for spec in tooling.specs().values():
        for param in inspect.signature(spec.fn).parameters.values():
            if "str" in str(param.annotation):
                found.setdefault(param.name, spec.name)
    return found


def test_every_string_tool_param_is_classified_for_redaction():
    params = _string_tool_params()
    assert params, "found no tools — the AST walk is broken"
    unclassified = {p: tool for p, tool in params.items()
                    if p not in SENSITIVE_ARGS and p not in NOT_SENSITIVE}
    assert not unclassified, (
        f"classify these tool params as sensitive (audit.SENSITIVE_ARGS) or not "
        f"(NOT_SENSITIVE here): {unclassified}")


def test_dashboard_is_opt_in(monkeypatch):
    from conductor_desktop.config import MCPConfig

    monkeypatch.delenv("CONDUCTOR_DESKTOP_DASHBOARD", raising=False)
    assert MCPConfig.from_env().dashboard is False
    monkeypatch.setenv("CONDUCTOR_DESKTOP_DASHBOARD", "1")
    assert MCPConfig.from_env().dashboard is True


def test_recent_calls_are_snapshots_not_live_objects():
    tray = TrayStateMachine()
    call_id = tray.start("click", kwargs={"x": 1})
    before = tray.recent()
    tray.end(call_id, success=True)
    assert before[0].in_flight and not tray.recent()[0].in_flight


def test_revision_moves_on_every_change_the_menu_shows():
    tray = TrayStateMachine()
    seen = [tray.revision]
    call_id = tray.start("click")
    seen.append(tray.revision)
    tray.end(call_id, success=True)
    seen.append(tray.revision)
    tray.set_paused(True)
    seen.append(tray.revision)
    assert len(set(seen)) == 4


async def test_dashboard_errors_hide_redacted_arguments(install_state):
    from conductor_desktop.tooling import ToolError, _wrap

    tray = TrayStateMachine()
    install_state(tray_state=tray)

    async def leaky(text: str) -> str:
        raise ToolError(f"could not type {text!r}")

    await _wrap(leaky, "leaky", "observing", False)(text="hunter2")
    (call,) = tray.recent()
    assert "hunter2" not in call.error


async def test_permission_probe_disconnects_its_executor(monkeypatch):
    from conductor_desktop import check_permissions

    events: list[str] = []

    class FakeExecutor:
        permissions = SimpleNamespace(ok=True)

        async def connect(self):
            events.append("connect")

        async def disconnect(self):
            events.append("disconnect")

    monkeypatch.setattr(check_permissions, "create_executor", lambda host: FakeExecutor())
    await check_permissions._probe_permissions()
    assert events == ["connect", "disconnect"]
