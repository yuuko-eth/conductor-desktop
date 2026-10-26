"""Tests for managed-mode enumeration — ManagedEnumerator over the
conductor-grounder gateway's /v1/enumerate.

HTTP is mocked with httpx.MockTransport; no network, no live gateway. The
gateway returns norm1000 bboxes (capitalized X1/Y1/X2/Y2, Go fuzzy.Bbox), and
the client must convert them to the image-pixel xyxy that EnumeratedElement and
grounding.capture_and_enumerate expect.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from conductor_desktop.grounder import GrounderError
from conductor_desktop.managed import gateway_bbox
from conductor_desktop.managed_enumerator import ManagedEnumerator


def _png_b64(w: int, h: int) -> str:
    """A byte string whose PNG IHDR encodes (w, h) — enough for png_dims, which
    reads width/height at offsets 16/20 without decoding pixels."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = (13).to_bytes(4, "big") + b"IHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")
    return base64.b64encode(sig + ihdr + b"\x00" * 8).decode()


def _enum_with(handler) -> ManagedEnumerator:
    e = ManagedEnumerator("http://gw.test", "cgr_live_tok", timeout=5.0)
    e._gw._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer cgr_live_tok"},
    )
    return e


def test_gateway_bbox_reads_capitalized_then_lowercase():
    assert gateway_bbox({"X1": 10, "Y1": 20, "X2": 30, "Y2": 40}) == (10, 20, 30, 40)
    assert gateway_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4}) == (1, 2, 3, 4)


def test_requires_api_key():
    with pytest.raises(ValueError, match="needs an API key"):
        ManagedEnumerator("http://gw.test", "")


async def test_parse_converts_norm1000_to_image_pixels():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "coord_system": "norm1000",
            "worker_id": "omni-1",
            "elements": [
                {"id": "e0", "kind": "text", "interactive": True,
                 "content": "Submit", "bbox": {"X1": 500, "Y1": 250, "X2": 800, "Y2": 750}},
                {"id": "e1", "kind": "icon", "interactive": True,
                 "content": "", "bbox": {"X1": 100, "Y1": 100, "X2": 200, "Y2": 200}},
            ],
        })

    e = _enum_with(handler)
    # 200x100 image → norm1000 500/250/800/750 maps to px 100/25/160/75.
    elements, annotated = await e.parse(_png_b64(200, 100))

    assert captured["path"] == "/v1/enumerate"
    assert captured["auth"] == "Bearer cgr_live_tok"
    assert annotated is None  # /v1/enumerate doesn't serve the SoM PNG
    assert len(elements) == 2
    assert elements[0].bbox == (100, 25, 160, 75)
    assert elements[0].kind == "text" and elements[0].interactive
    assert elements[0].content == "Submit"
    # Contentless interactive icon survives.
    assert elements[1].content == "" and elements[1].interactive
    assert elements[1].bbox == (20, 10, 40, 20)


async def test_parse_empty_elements_returns_empty_list():
    def handler(request):
        return httpx.Response(200, json={"coord_system": "norm1000", "elements": []})

    elements, annotated = await _enum_with(handler).parse(_png_b64(100, 100))
    assert elements == [] and annotated is None


async def test_empty_wallet_raises():
    def handler(request):
        return httpx.Response(402, json={"error": "wallet empty"})

    with pytest.raises(GrounderError, match="402"):
        await _enum_with(handler).parse(_png_b64(100, 100))


async def test_unauthorized_raises():
    def handler(request):
        return httpx.Response(401, json={"error": "bad token"})

    with pytest.raises(GrounderError, match="401"):
        await _enum_with(handler).parse(_png_b64(100, 100))


async def test_server_error_raises():
    def handler(request):
        return httpx.Response(502, text="upstream parser down")

    with pytest.raises(GrounderError, match="502"):
        await _enum_with(handler).parse(_png_b64(100, 100))


# --- what the gateway says its coordinates are ---------------------------

async def test_declared_image_pixels_are_not_scaled_again():
    """The client converts norm1000 to pixels. A gateway that already answers
    in pixels must pass through, not get divided by 1000 a second time."""
    def handler(request):
        return httpx.Response(200, json={
            "coord_system": "image_pixels",
            "elements": [{"bbox": {"X1": 100, "Y1": 50, "X2": 300, "Y2": 150},
                          "kind": "icon", "interactive": True, "content": "Save"}],
        })

    e = _enum_with(handler)
    els, _ = await e.parse(_png_b64(1000, 500))
    await e.close()
    assert [el.bbox for el in els] == [(100, 50, 300, 150)]


async def test_an_unknown_coord_system_is_an_error_not_a_guess():
    # It used to fall through and use norm1000 numbers as pixels, putting
    # every box in the top-left corner of the screen.
    def handler(request):
        return httpx.Response(200, json={"coord_system": "furlongs", "elements": []})

    e = _enum_with(handler)
    with pytest.raises(GrounderError, match="can't project"):
        await e.parse(_png_b64(1000, 500))
    await e.close()


async def test_norm1000_needs_a_sizeable_screenshot():
    def handler(request):
        return httpx.Response(200, json={
            "coord_system": "norm1000",
            "elements": [{"bbox": {"X1": 1, "Y1": 1, "X2": 2, "Y2": 2}}]})

    e = _enum_with(handler)
    with pytest.raises(GrounderError, match="cannot size the screenshot"):
        await e.parse("bm90LWFuLWltYWdl")  # "not-an-image"
    await e.close()


async def test_self_hosted_knobs_are_dropped_not_rejected():
    """capture_and_enumerate passes os_id/theme/annotate on every call; the
    gateway decides them itself, so they must not reach it or raise."""
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"coord_system": "norm1000", "elements": []})

    e = _enum_with(handler)
    els, annotated = await e.parse(_png_b64(800, 600), annotate=True, rec=False,
                                   os_id="macos", theme="dark")
    await e.close()
    assert (els, annotated) == ([], None)
    body = seen["body"].decode()
    assert "screenshot" in body
    for knob in ("annotate", "rec", "os", "theme"):
        assert f'"{knob}"' not in body
