"""The bearer token on every HTTP route, and the refusal to serve a
non-loopback bind without one."""

from __future__ import annotations

import os

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from conductor_desktop.config import MCPConfig
from conductor_desktop.http_auth import COOKIE, BearerAuth, cookie_value, is_loopback

TOKEN = "s3cret-token-0123456789"


def _client() -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"]),
                            Route("/dashboard/", ok), Route("/dashboard/api/state", ok)])
    return TestClient(BearerAuth(app, TOKEN), follow_redirects=False)


def test_mcp_route_needs_the_token():
    c = _client()
    assert c.post("/mcp").status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.text == "ok"


def test_dashboard_logs_in_with_a_query_token_then_a_cookie():
    c = _client()
    assert c.get("/dashboard/").status_code == 401
    r = c.get("/dashboard/", params={"token": TOKEN})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/"
    set_cookie = r.headers["set-cookie"]
    assert TOKEN not in set_cookie and "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie and "Path=/dashboard" in set_cookie
    assert c.get("/dashboard/api/state").status_code == 200   # cookie kept


def test_wrong_query_token_is_refused():
    assert _client().get("/dashboard/", params={"token": "x" * 20}).status_code == 401


def test_dashboard_cookie_does_not_open_the_mcp_route():
    c = _client()
    c.cookies.set(COOKIE, cookie_value(TOKEN))
    assert c.get("/dashboard/api/state").status_code == 200
    assert c.post("/mcp").status_code == 401


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("localhost", True), ("::1", True),
    ("[::1]", True), ("0.0.0.0", False), ("::", False), ("", False),
    ("192.168.1.5", False), ("box.lan", False),
])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


@pytest.fixture
def env(monkeypatch):
    for k in ("CONDUCTOR_DESKTOP_TRANSPORT", "CONDUCTOR_DESKTOP_HOST", "CONDUCTOR_DESKTOP_DASHBOARD",
              "CONDUCTOR_DESKTOP_AUTH_TOKEN", "CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE",
              "CONDUCTOR_DESKTOP_GROUNDER_URL"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_lan_bind_without_a_token_refuses_to_start(env):
    env.setenv("CONDUCTOR_DESKTOP_TRANSPORT", "http")
    env.setenv("CONDUCTOR_DESKTOP_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="needs a token"):
        MCPConfig.from_env()


def test_lan_dashboard_sidecar_without_a_token_refuses_to_start(env):
    env.setenv("CONDUCTOR_DESKTOP_DASHBOARD", "1")
    env.setenv("CONDUCTOR_DESKTOP_HOST", "192.168.1.5")
    with pytest.raises(RuntimeError, match="needs a token"):
        MCPConfig.from_env()


def test_stdio_without_dashboard_ignores_the_host(env):
    env.setenv("CONDUCTOR_DESKTOP_HOST", "0.0.0.0")
    assert MCPConfig.from_env().auth_token == ""


def test_loopback_http_needs_no_token(env):
    env.setenv("CONDUCTOR_DESKTOP_TRANSPORT", "http")
    assert MCPConfig.from_env().auth_token == ""


def test_token_is_read_and_popped_from_the_environment(env):
    env.setenv("CONDUCTOR_DESKTOP_TRANSPORT", "http")
    env.setenv("CONDUCTOR_DESKTOP_HOST", "0.0.0.0")
    env.setenv("CONDUCTOR_DESKTOP_AUTH_TOKEN", TOKEN)
    assert MCPConfig.from_env().auth_token == TOKEN
    assert "CONDUCTOR_DESKTOP_AUTH_TOKEN" not in os.environ


def test_token_file(env, tmp_path):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    env.setenv("CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE", str(f))
    assert MCPConfig.from_env().auth_token == TOKEN
    assert "CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE" not in os.environ


def test_short_token_and_both_sources_are_refused(env, tmp_path):
    env.setenv("CONDUCTOR_DESKTOP_AUTH_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 16"):
        MCPConfig.from_env()
    env.setenv("CONDUCTOR_DESKTOP_AUTH_TOKEN", TOKEN)
    env.setenv("CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE", str(tmp_path / "t"))
    with pytest.raises(RuntimeError, match="not both"):
        MCPConfig.from_env()


def test_dashboard_masks_the_token():
    from conductor_desktop.dashboard.routes import _public_value
    assert _public_value("auth_token", TOKEN) != TOKEN
