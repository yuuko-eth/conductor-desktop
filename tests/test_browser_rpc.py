"""The shared CDP/BiDi socket, over a fake WebSocket: routing, a lost
connection, and which calls run as a user gesture."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError

from conductor_desktop.bidi import BidiError
from conductor_desktop.browser import BidiEndpoint, CdpEndpoint
from conductor_desktop.cdp import CDPClient, CDPError


class FakeWS:
    """Replays inbound messages from a queue; None ends the stream (the
    socket closed under us)."""

    def __init__(self, send_error: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.send_error = send_error

    async def send(self, raw: str) -> None:
        if self.send_error:
            raise self.send_error
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.inbox.get()
        if msg is None:
            raise StopAsyncIteration
        return json.dumps(msg)


def _client(ws: FakeWS) -> CDPClient:
    client = CDPClient({"id": "t1"})
    client._ws = ws
    client._reader_task = asyncio.create_task(client._reader())
    return client


async def test_replies_and_events_are_routed():
    ws = FakeWS()
    client = _client(ws)
    seen = []
    client.on("Page.loadEventFired", seen.append)
    pending = asyncio.create_task(client.send("Page.enable"))
    await asyncio.sleep(0)
    ws.inbox.put_nowait({"method": "Page.loadEventFired", "params": {"t": 1}})
    ws.inbox.put_nowait({"id": ws.sent[0]["id"], "result": {"ok": True}})
    assert await pending == {"ok": True}
    assert seen == [{"t": 1}]
    assert ws.sent == [{"id": 1, "method": "Page.enable"}]  # no empty params
    await client.close()


async def test_a_lost_connection_fails_pending_commands_at_once():
    # CDP used to leave them waiting out the 30 s timeout.
    ws = FakeWS()
    client = _client(ws)
    pending = asyncio.create_task(client.send("Runtime.evaluate", timeout=30))
    await asyncio.sleep(0)
    ws.inbox.put_nowait(None)
    with pytest.raises(CDPError, match="connection closed"):
        await asyncio.wait_for(pending, 1)
    with pytest.raises(CDPError, match="connection is closed"):
        await client.send("Runtime.evaluate")
    await client.close()


async def test_a_send_on_a_closed_socket_is_a_protocol_error():
    ws = FakeWS(send_error=ConnectionClosedError(None, None))
    client = _client(ws)
    with pytest.raises(CDPError, match="connection closed"):
        await client.send("Page.enable")
    assert client._pending == {}
    await client.close()


async def test_error_replies_raise():
    ws = FakeWS()
    client = _client(ws)
    pending = asyncio.create_task(client.send("Nope.method"))
    await asyncio.sleep(0)
    ws.inbox.put_nowait({"id": 1, "error": {"message": "'Nope.method' wasn't found"}})
    with pytest.raises(CDPError, match="wasn't found"):
        await pending
    await client.close()


async def test_probes_are_not_user_gestures():
    ws = FakeWS()
    client = _client(ws)

    async def answer():
        while True:
            await asyncio.sleep(0)
            for msg in ws.sent:
                if not msg.get("answered"):
                    msg["answered"] = True
                    ws.inbox.put_nowait({"id": msg["id"], "result": {"result": {"value": True}}})

    responder = asyncio.create_task(answer())
    assert await client.wait_for_selector("#go", timeout_ms=500)
    await client.evaluate("document.title")
    responder.cancel()
    gestures = [m["params"]["userGesture"] for m in ws.sent]
    assert gestures == [False, True]  # the probe, then a real evaluate
    await client.close()


async def test_bidi_tab_listing_grants_no_user_activation():
    ep = BidiEndpoint("firefox", "ws://127.0.0.1:1/session")
    sent = []

    async def send(method, params=None, timeout=30.0):
        sent.append((method, params))
        if method == "browsingContext.getTree":
            return {"contexts": [{"context": "c1", "url": "https://a"}]}
        return {"type": "success", "result": {"type": "array", "value": [
            {"type": "string", "value": "A"}, {"type": "string", "value": "visible"}]}}

    ep.conn.send = send
    tabs = await ep.list_tabs()
    assert [t.title for t in tabs] == ["A"]
    evaluate = next(p for m, p in sent if m == "script.evaluate")
    assert evaluate["userActivation"] is False


async def test_bidi_connection_reports_a_closed_socket():
    ep = BidiEndpoint("firefox", "ws://127.0.0.1:1/session")
    with pytest.raises(BidiError, match="connection is closed"):
        await ep.conn._send_raw("session.status", {}, timeout=1)


async def test_cdp_endpoint_reuses_one_http_client():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"Browser": "Chrome/150"})

    ep = CdpEndpoint("chrome", "127.0.0.1", 9222)
    ep._http = client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await ep.probe() == "Chrome/150"
    assert await ep.probe() == "Chrome/150"
    assert ep._http is client and calls == ["/json/version", "/json/version"]
    await ep.close()
    assert ep._http is None and client.is_closed
