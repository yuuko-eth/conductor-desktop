"""Tests for bidi.py — value parity with CDP, evaluate errors, network idle.

RemoteValue payloads are shapes captured from Firefox 152; expected values
are what Chrome 150's CDP `returnByValue` returns for the same JS. A fake
connection replays events; no browser required.
"""

from __future__ import annotations

import asyncio
import base64
import re

import pytest

from conductor_desktop.bidi import BidiError, BidiTab, remote_value_to_py


def _num(v):
    return {"type": "number", "value": v}


def _str(v):
    return {"type": "string", "value": v}


class FakeConn:
    def __init__(self, responses: dict | None = None) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self.responses = responses or {}
        self._handlers: dict[str, list] = {}
        self.subscribed: list[tuple[list, list | None]] = []
        self.unsubscribed: list[str | None] = []

    async def send(self, method, params=None, timeout=30.0):
        self.sent.append((method, params))
        return self.responses.get(method, {})

    async def subscribe(self, events, contexts=None):
        self.subscribed.append((list(events), contexts))
        return f"sub-{len(self.subscribed)}"

    async def unsubscribe(self, subscription):
        self.unsubscribed.append(subscription)

    def on(self, method, handler):
        self._handlers.setdefault(method, []).append(handler)

    def off(self, method, handler):
        if handler in self._handlers.get(method, []):
            self._handlers[method].remove(handler)

    def fire(self, method, params):
        for handler in list(self._handlers.get(method, ())):
            handler(params)


# ── remote_value_to_py ─────────────────────────────────────────────

@pytest.mark.parametrize("remote, expected", [
    (_str("Probe"), "Probe"),
    ({"type": "boolean", "value": True}, True),
    (_num(1), 1),
    (_num("NaN"), None),
    (_num("-0"), None),
    (_num("Infinity"), None),
    ({"type": "bigint", "value": "10"}, None),
    ({"type": "undefined"}, None),
    ({"type": "null"}, None),
    ({"type": "array", "value": [_num(1), {"type": "undefined"}]}, [1, None]),
    ({"type": "object", "value": [["a", {"type": "undefined"}], ["b", _num(1)]]}, {"b": 1}),
    ({"type": "object", "value": [
        ["a", _num(1)],
        ["b", {"type": "array", "value": [_num(1), _str("x"), {"type": "null"}]}],
        ["c", {"type": "object", "value": [["d", {"type": "boolean", "value": True}]]}],
    ]}, {"a": 1, "b": [1, "x", None], "c": {"d": True}}),
    ({"type": "date", "value": "1970-01-01T00:00:00.000Z"}, {}),
    ({"type": "map", "value": [["k", _num(1)]]}, {}),
    ({"type": "node", "sharedId": "x", "value": {"nodeType": 1, "localName": "div"}}, {}),
    ({"type": "function"}, {}),
    ({"type": "nodelist", "value": [{"type": "node"}, {"type": "node"}]}, {"0": {}, "1": {}}),
    ({"type": "object", "value": [["d", {"type": "date", "value": "x"}], ["n", _num("NaN")]]},
     {"d": {}, "n": None}),
    # Cycle: the back-reference carries only internalId.
    ({"type": "object", "internalId": "i1",
      "value": [["self", {"type": "object", "internalId": "i1"}]]}, {"self": None}),
])
def test_remote_value_matches_cdp_return_by_value(remote, expected):
    assert remote_value_to_py(remote) == expected


# ── evaluate / screenshot ──────────────────────────────────────────

async def test_evaluate_targets_context_and_converts():
    conn = FakeConn({"script.evaluate": {"type": "success", "result": _str("Probe")}})
    tab = BidiTab(conn, "ctx-1")
    assert await tab.evaluate("document.title") == "Probe"
    method, params = conn.sent[-1]
    assert method == "script.evaluate"
    assert params["target"] == {"context": "ctx-1"}
    assert params["awaitPromise"] is True


async def test_evaluate_exception_raises_like_cdp():
    conn = FakeConn({"script.evaluate": {
        "type": "exception",
        "exceptionDetails": {"text": "TypeError: bad", "exception": {"type": "error"}},
    }})
    with pytest.raises(BidiError, match=re.escape("script.evaluate threw: TypeError: bad")):
        await BidiTab(conn, "ctx-1").evaluate("throw new TypeError('bad')")


async def test_capture_screenshot_uses_document_box_clip():
    conn = FakeConn({"browsingContext.captureScreenshot": {
        "data": base64.b64encode(b"png-bytes").decode()}})
    png = await BidiTab(conn, "ctx-1").capture_screenshot(
        clip={"x": 1, "y": 2, "width": 3, "height": 4, "scale": 1})
    assert png == b"png-bytes"
    _, params = conn.sent[-1]
    assert params["origin"] == "document"
    assert params["clip"] == {"type": "box", "x": 1, "y": 2, "width": 3, "height": 4}


# ── wait_for_network_idle ──────────────────────────────────────────

TREE = {"browsingContext.getTree": {"contexts": [
    {"context": "top", "children": [{"context": "frame", "children": []}]}]}}


def _request(rid, context="top", url="https://api.example/x", headers=()):
    return {"context": context,
            "request": {"request": rid, "url": url, "headers": list(headers)}}


async def _until_subscribed(conn):
    for _ in range(100):
        if conn.subscribed:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never subscribed")


async def test_network_idle_waits_for_in_flight_request():
    conn = FakeConn(TREE)
    task = asyncio.create_task(
        BidiTab(conn, "top").wait_for_network_idle(quiet_ms=50, timeout_ms=2000))
    await _until_subscribed(conn)
    conn.fire("network.beforeRequestSent", _request("r1", context="frame"))
    await asyncio.sleep(0.15)
    assert not task.done()
    conn.fire("network.responseCompleted", _request("r1", context="frame"))
    assert await task is True
    assert conn.unsubscribed == ["sub-1"]
    assert conn._handlers["network.beforeRequestSent"] == []


async def test_network_idle_ignores_streams_and_other_tabs():
    conn = FakeConn(TREE)
    task = asyncio.create_task(
        BidiTab(conn, "top").wait_for_network_idle(quiet_ms=50, timeout_ms=1000))
    await _until_subscribed(conn)
    conn.fire("network.beforeRequestSent", _request("ws", url="wss://chat.example/"))
    conn.fire("network.beforeRequestSent", _request(
        "sse", headers=[{"name": "Accept", "value": _str("text/event-stream")}]))
    conn.fire("network.beforeRequestSent", _request("other", context="another-tab"))
    assert await task is True


async def test_network_idle_times_out():
    conn = FakeConn(TREE)
    task = asyncio.create_task(
        BidiTab(conn, "top").wait_for_network_idle(quiet_ms=50, timeout_ms=200))
    await _until_subscribed(conn)
    conn.fire("network.beforeRequestSent", _request("stuck"))
    assert await task is False
