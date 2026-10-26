"""Tests for managed (SaaS) mode — ManagedGrounder over the conductor-grounder
gateway, plus the config wiring that selects it.

HTTP is mocked with httpx.MockTransport injected into the client's _http, so
these run with no network and no live gateway.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
import pytest

from conductor_desktop.config import MCPConfig
from conductor_desktop.grounder import GrounderError
from conductor_desktop.managed import gateway_bbox
from conductor_desktop.managed_grounder import ManagedGrounder


def _grounder_with(handler) -> ManagedGrounder:
    """Build a ManagedGrounder whose transport is a MockTransport. The real
    constructor's auth header is preserved by rebuilding the gateway's client with the same
    headers plus the mock transport."""
    g = ManagedGrounder("http://gw.test", "cgr_live_tok", timeout=5.0)
    g._gw._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer cgr_live_tok"},
    )
    return g


# --- pure parsing -----------------------------------------------------------

def test_gateway_bbox_reads_capitalized_gateway_keys():
    # Go fuzzy.Bbox has no json tags → capitalized field names on the wire.
    assert gateway_bbox({"X1": 10, "Y1": 20, "X2": 30, "Y2": 40}) == (10, 20, 30, 40)


def test_gateway_bbox_tolerates_lowercase():
    assert gateway_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4}) == (1, 2, 3, 4)


# --- construction guards ----------------------------------------------------

@pytest.mark.parametrize("bad", [
    {}, {"X1": 10, "Y1": 20, "X2": 30}, {"X1": "10", "Y1": 20, "X2": 30, "Y2": 40},
    {"X1": None, "Y1": 0, "X2": 0, "Y2": 0}, [10, 20, 30, 40], None,
])
def test_gateway_bbox_rejects_incomplete_boxes(bad):
    # A missing coordinate used to become 0, i.e. a click at the top-left corner.
    assert gateway_bbox(bad) is None


async def test_ground_drops_malformed_candidates():
    def handler(request):
        return httpx.Response(200, json={"candidates": [
            {"bbox": {}, "description": "broken"},
            {"bbox": {"X1": 10, "Y1": 20, "X2": 30, "Y2": 40}, "description": "ok"},
        ]})

    els = await _grounder_with(handler).ground("b64", "the button")
    assert [e.description for e in els] == ["ok"]


async def test_non_json_reply_is_a_grounder_error():
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>",
                              headers={"content-type": "text/html"})

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match="non-JSON"):
        await g.ground("b64", "the button")
    with pytest.raises(GrounderError, match="non-JSON"):
        await g.describe("b64")


def test_requires_api_key():
    with pytest.raises(ValueError, match="needs an API key"):
        ManagedGrounder("http://gw.test", "")


# --- ground -----------------------------------------------------------------

async def test_ground_maps_candidates_to_elements():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "category": "semantic",
            "bbox": {"X1": 100, "Y1": 200, "X2": 140, "Y2": 240},
            "label": "top match",
            "coord_system": "norm1000",
            "candidates": [
                {"bbox": {"X1": 100, "Y1": 200, "X2": 140, "Y2": 240}, "description": "the close button"},
                {"bbox": {"X1": 500, "Y1": 500, "X2": 520, "Y2": 520}, "description": "the other one"},
            ],
            "ambiguous": True,
        })

    g = _grounder_with(handler)
    els = await g.ground("imgb64", "close button")
    await g.close()

    assert captured["path"] == "/v1/ground"
    assert captured["auth"] == "Bearer cgr_live_tok"
    assert captured["body"] == {"target": "close button", "screenshot": "imgb64",
                                "multiple": False}
    assert len(els) == 2
    assert els[0].bbox == (100, 200, 140, 240)
    assert els[0].description == "the close button"
    assert els[0].center == (120, 220)  # norm1000 space, projected upstream


async def test_region_view_asks_gateway_for_multiple_and_shares_the_pool():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "coord_system": "norm1000",
            "candidates": [{"bbox": {"X1": 1, "Y1": 2, "X2": 3, "Y2": 4}, "description": "d"}],
        })

    g = _grounder_with(handler)
    region = g.region_view()
    await g.ground("img", "x")
    await region.ground("img", "x")

    assert [b["multiple"] for b in bodies] == [False, True]
    assert region._gw._http is g._gw._http
    await region.close()          # a view never closes the shared pool
    assert not g._gw._http.is_closed
    await g.close()
    assert g._gw._http.is_closed


async def test_ground_404_is_no_match_not_error():
    def handler(request):
        return httpx.Response(404, json={"error": "no match"})

    g = _grounder_with(handler)
    assert await g.ground("img", "nonexistent widget") == []
    await g.close()


async def test_ground_falls_back_to_single_bbox_without_candidates():
    def handler(request):
        return httpx.Response(200, json={
            "bbox": {"X1": 1, "Y1": 2, "X2": 3, "Y2": 4},
            "label": "only match",
            "coord_system": "norm1000",
        })

    g = _grounder_with(handler)
    els = await g.ground("img", "x")
    await g.close()
    assert len(els) == 1
    assert els[0].bbox == (1, 2, 3, 4)
    assert els[0].description == "only match"


async def test_ground_402_raises_actionable_error():
    def handler(request):
        return httpx.Response(402, json={"error": "wallet empty"})

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match="402"):
        await g.ground("img", "x")
    await g.close()


async def test_ground_500_raises_grounder_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match="500"):
        await g.ground("img", "x")
    await g.close()


# --- describe ---------------------------------------------------------------

async def test_describe_screen_and_image_modes():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"description": "  a calculator app  "})

    g = _grounder_with(handler)
    out = await g.describe("img", hint="the toolbar")
    assert out == "a calculator app"  # stripped
    assert seen[-1] == {"screenshot": "img", "image_mode": False, "hint": "the toolbar"}

    await g.describe_image("img2", mime="image/jpeg")
    assert seen[-1] == {"screenshot": "img2", "image_mode": True, "mime": "image/jpeg"}
    await g.close()


# --- config wiring ----------------------------------------------------------

def test_config_managed_mode(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "https://api.conductor.test")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "managed")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_API_KEY", "cgr_live_abc")
    # No describe_url / describe_family needed — gateway owns routing.
    cfg = MCPConfig.from_env()
    assert cfg.managed is True
    assert cfg.api_key == "cgr_live_abc"
    assert cfg.grounder_family == "managed"


def test_config_managed_requires_api_key(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "https://api.conductor.test")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "managed")
    monkeypatch.delenv("CONDUCTOR_DESKTOP_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CONDUCTOR_DESKTOP_API_KEY"):
        MCPConfig.from_env()


def test_config_managed_rejects_non_norm1000_coords(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_URL", "https://api.conductor.test")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "managed")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_API_KEY", "cgr_live_abc")
    monkeypatch.setenv("CONDUCTOR_DESKTOP_COORD_SYSTEM", "image_pixels")
    with pytest.raises(RuntimeError, match="norm1000"):
        MCPConfig.from_env()


# --- tool surface -------------------------------------------------------------

def test_a_fresh_server_registers_every_declared_tool():
    # Guards the regression where a lint autofix dropped the tools.enumerate
    # import and the tool vanished from every build while diagnose still said
    # "on". A fresh interpreter imports only what server.py pulls in, so a
    # module left out of the tools package shows up here as a missing tool.
    import ast
    from pathlib import Path

    import conductor_desktop

    declared = set()
    for path in (Path(conductor_desktop.__file__).parent / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                    "tool" in ast.unparse(d) for d in node.decorator_list):
                declared.add(node.name)
    code = (
        "import asyncio; from conductor_desktop.config import MCPConfig; "
        "from conductor_desktop.server import build_mcp; "
        "cfg = MCPConfig(grounder_url='', mobile_android_enabled=True, "
        "describe_image_enabled=True, omniparser_url='http://omni.test:8918'); "
        "print(sorted(t.name for t in asyncio.run(build_mcp(cfg).list_tools())))"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CONDUCTOR_DESKTOP_")}
    out = subprocess.run([sys.executable, "-c", code], env=clean,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    listed = set(ast.literal_eval(out.stdout.strip().splitlines()[-1]))
    assert "enumerate_elements" in listed
    assert listed == declared


async def test_an_unknown_coord_system_is_an_error():
    def handler(request):
        return httpx.Response(200, json={
            "coord_system": "furlongs",
            "candidates": [{"bbox": {"X1": 1, "Y1": 2, "X2": 3, "Y2": 4}}]})

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match="can't project"):
        await g.ground("Zm9v", "the clock")
    await g.close()


async def test_declared_image_pixels_are_converted_to_norm1000():
    """Managed mode is wired norm1000; if a gateway ever answers in pixels,
    the boxes are converted against the screenshot instead of mis-placed."""
    import base64

    def handler(request):
        return httpx.Response(200, json={
            "coord_system": "image_pixels",
            "candidates": [{"bbox": {"X1": 200, "Y1": 100, "X2": 400, "Y2": 300},
                            "description": "a card"}]})

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = (13).to_bytes(4, "big") + b"IHDR" + (2000).to_bytes(4, "big") + (1000).to_bytes(4, "big")
    shot = base64.b64encode(sig + ihdr + b"\x00" * 8).decode()
    g = _grounder_with(handler)
    els = await g.ground(shot, "the card")
    await g.close()
    assert [e.bbox for e in els] == [(100, 100, 200, 300)]


async def test_ground_429_retries_honouring_retry_after(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr("conductor_desktop.managed._sleep", fake_sleep)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(404, json={})

    g = _grounder_with(handler)
    assert await g.ground("img", "x") == []
    assert waits == [3.0] and len(calls) == 2
    await g.close()


async def test_ground_429_gives_up_with_rate_limit_error(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr("conductor_desktop.managed._sleep", fake_sleep)

    def handler(request):
        return httpx.Response(429)

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match="rate limited"):
        await g.ground("img", "x")
    assert waits == [1.0, 2.0]
    await g.close()


async def test_ground_429_long_retry_after_fails_at_once(monkeypatch):
    async def fake_sleep(s):
        raise AssertionError("should not wait")

    monkeypatch.setattr("conductor_desktop.managed._sleep", fake_sleep)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    g = _grounder_with(handler)
    with pytest.raises(GrounderError, match=r"Retry-After: 120s"):
        await g.ground("img", "x")
    await g.close()
