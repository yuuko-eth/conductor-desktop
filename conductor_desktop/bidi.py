"""Minimal WebDriver BiDi client.

The Firefox counterpart of ``cdp.py``. Firefox launched with
``--remote-debugging-port=PORT`` serves BiDi at ``ws://host:PORT/session``.

Unlike CDP's per-tab WebSockets, BiDi multiplexes every tab over one
session-bound connection, and Firefox allows only one session at a time.
So ``BidiConnection`` is long-lived (owned by the browser registry, reused
across tool calls, reconnected on loss) and ``BidiTab`` is a cheap per-call
view scoped to one browsing context.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from websockets.exceptions import WebSocketException

from .browser_rpc import JsonRpcSocket, TabHelpers

log = logging.getLogger("conductor.mcp.bidi")


class BidiError(RuntimeError):
    """BiDi command failed (protocol error, JS exception, or no connection)."""


class BidiConnection(JsonRpcSocket):
    """One WebDriver BiDi session over one WebSocket, shared by all tabs.

    `send` lazily connects and creates the session, so the registry can hold
    an instance for a browser that isn't running yet.
    """

    error = BidiError
    label = "WebDriver BiDi"

    def __init__(self, ws_url: str) -> None:
        super().__init__()
        self.ws_url = ws_url
        self.capabilities: dict = {}
        self._lock = asyncio.Lock()

    def _frame(self, cmd_id: int, method: str, params: dict) -> dict:
        return {"id": cmd_id, "method": method, "params": params}

    def _event(self, msg: dict) -> tuple[str, dict] | None:
        if msg.get("type") == "event":
            return msg.get("method", ""), msg.get("params") or {}
        return None

    def _result(self, method: str, reply: dict) -> dict:
        if reply.get("type") == "error":
            raise BidiError(f"{method} failed: {reply.get('error')}: {reply.get('message')}")
        return reply.get("result") or {}

    async def ensure(self) -> None:
        """Connect and run `session.new` unless a live session exists."""
        if self.connected:
            return
        async with self._lock:
            if self.connected:
                return
            await self._teardown()
            try:
                await self._open(self.ws_url, open_timeout=3)
            except (TimeoutError, OSError, WebSocketException) as e:
                raise BidiError(
                    f"Cannot reach WebDriver BiDi at {self.ws_url} — is the browser "
                    f"running with `--remote-debugging-port`? ({e})"
                ) from e
            try:
                res = await self._send_raw("session.new", {"capabilities": {}},
                                           timeout=10.0)
            except BidiError as e:
                await self._teardown()
                if "session not created" in str(e):
                    # Verified on Firefox 152: a BiDi-only session survives
                    # its WebSocket closing (clean or aborted) and can't be
                    # reattached — only session.end on the live socket frees it.
                    raise BidiError(
                        f"{e} — Firefox allows one BiDi session and keeps it after its "
                        f"client disconnects without session.end (Playwright, Selenium, "
                        f"or a crashed/killed conductor-desktop). Quit that client, or "
                        f"restart the browser to clear a leaked session"
                    ) from e
                raise
            self.capabilities = res.get("capabilities") or {}

    async def close(self) -> None:
        if self.connected:
            try:
                await self._send_raw("session.end", {}, timeout=2.0)
            except BidiError:
                pass
        await self._teardown()

    async def send(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        """Send a BiDi command, return its `result`. Raises BidiError on error."""
        await self.ensure()
        return await self._send_raw(method, params or {}, timeout=timeout)

    async def subscribe(self, events: list[str],
                        contexts: list[str] | None = None) -> str | None:
        """`session.subscribe`; returns the subscription id for unsubscribe()."""
        params: dict[str, Any] = {"events": events}
        if contexts:
            params["contexts"] = contexts
        res = await self.send("session.subscribe", params)
        return res.get("subscription")

    async def unsubscribe(self, subscription: str | None) -> None:
        """Best-effort removal by id — the connection may already be gone."""
        if not subscription or not self.connected:
            return
        try:
            await self._send_raw("session.unsubscribe",
                                 {"subscriptions": [subscription]}, timeout=5.0)
        except BidiError as e:
            log.warning("BiDi unsubscribe failed: %s", e)


class BidiTab(TabHelpers):
    """Per-call view of one top-level browsing context, with the same
    conveniences as CDPClient so tool code is transport-agnostic."""

    error = BidiError

    def __init__(self, conn: BidiConnection, context: str,
                 tab_info: dict | None = None) -> None:
        self.conn = conn
        self.context = context
        self.tab = tab_info or {}

    async def close(self) -> None:
        """No-op: the connection is shared and owned by the registry."""

    async def context_ids(self) -> set[str]:
        """This context plus its (current) descendant iframes."""
        res = await self.conn.send("browsingContext.getTree", {"root": self.context})
        ids: set[str] = set()
        stack = list(res.get("contexts") or [])
        while stack:
            node = stack.pop()
            ids.add(node.get("context"))
            stack.extend(node.get("children") or [])
        return ids or {self.context}

    async def evaluate(self, expression: str, *, await_promise: bool = True,
                       user_activation: bool = True, timeout: float = 30.0) -> Any:
        """Run JS in the tab, return its value as JSON-compatible Python.

        Raises BidiError if the JS throws, with the same "threw: <message>"
        shape as CDPClient.evaluate. `user_activation` runs it as a user
        gesture; probes and tab listing turn it off.
        """
        res = await self.conn.send("script.evaluate", {
            "expression": expression,
            "target": {"context": self.context},
            "awaitPromise": await_promise,
            "resultOwnership": "none",
            "userActivation": user_activation,
        }, timeout=timeout)
        if res.get("type") == "exception":
            details = res.get("exceptionDetails") or {}
            raise BidiError(f"script.evaluate threw: {details.get('text') or 'exception'}")
        return remote_value_to_py(res.get("result"))

    async def wait_for_network_idle(self, *, quiet_ms: int = 500,
                                    timeout_ms: int = 5000) -> bool:
        """Wait until no in-flight non-streaming requests for `quiet_ms`.

        Same contract as CDPClient.wait_for_network_idle. BiDi carries no
        resource type for WebSocket / EventSource, so those are recognised
        by a ws(s): URL or an `Accept: text/event-stream` header instead.
        """
        loop = asyncio.get_running_loop()
        ids = await self.context_ids()
        in_flight: set[str] = set()
        last_activity = [loop.time()]

        def _request_id(params: dict) -> str | None:
            if params.get("context") not in ids:
                return None
            return (params.get("request") or {}).get("request")

        def _on_request(params: dict) -> None:
            rid = _request_id(params)
            if rid is None or _is_streaming(params.get("request") or {}):
                return
            in_flight.add(rid)
            last_activity[0] = loop.time()

        def _on_finished(params: dict) -> None:
            rid = _request_id(params)
            if rid in in_flight:
                in_flight.discard(rid)
                last_activity[0] = loop.time()

        handlers = [("network.beforeRequestSent", _on_request),
                    ("network.responseCompleted", _on_finished),
                    ("network.fetchError", _on_finished)]
        for method, handler in handlers:
            self.conn.on(method, handler)
        subscription = None
        try:
            subscription = await self.conn.subscribe(
                [m for m, _ in handlers], [self.context])
            return await self._until_quiet(in_flight, last_activity,
                                           quiet_ms=quiet_ms, timeout_ms=timeout_ms)
        finally:
            for method, handler in handlers:
                self.conn.off(method, handler)
            await self.conn.unsubscribe(subscription)

    async def capture_screenshot(self, *, clip: dict | None = None,
                                 beyond_viewport: bool = True,
                                 fmt: str = "png") -> bytes:
        """Capture a screenshot. `clip` in CSS-pixel document coords, as in
        CDPClient (viewport coords when `beyond_viewport=False`)."""
        params: dict[str, Any] = {
            "context": self.context,
            "origin": "document" if beyond_viewport else "viewport",
            "format": {"type": f"image/{fmt}"},
        }
        if clip is not None:
            params["clip"] = {"type": "box", "x": clip["x"], "y": clip["y"],
                              "width": clip["width"], "height": clip["height"]}
        res = await self.conn.send("browsingContext.captureScreenshot", params)
        return base64.b64decode(res["data"])


def _is_streaming(request: dict) -> bool:
    url = (request.get("url") or "").lower()
    if url.startswith(("ws:", "wss:")):
        return True
    for header in request.get("headers") or ():
        if (header.get("name") or "").lower() == "accept":
            value = (header.get("value") or {}).get("value") or ""
            if "text/event-stream" in value:
                return True
    return False


def remote_value_to_py(value: Any) -> Any:
    """Convert a BiDi RemoteValue to what CDP's `returnByValue` would give.

    Parity beats fidelity: the same expression should return the same JSON
    in Chrome and Firefox, so agents don't need per-browser JS. Verified
    against Chrome 150: NaN / ±Infinity / -0 / BigInt → null; undefined
    object properties are dropped; Date, Map, Set, RegExp, DOM nodes,
    errors, promises and functions → {}; a NodeList → {"0": {}, ...}.
    """
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    raw = value.get("value")
    if kind in ("string", "boolean"):
        return raw
    if kind == "number":
        # NaN / ±Infinity / -0 arrive as strings; CDP gives no value for them.
        return raw if isinstance(raw, (int, float)) else None
    if kind in ("undefined", "null", "bigint", "symbol"):
        return None
    if kind == "array":
        return [remote_value_to_py(v) for v in raw] if isinstance(raw, list) else None
    if kind == "object":
        if not isinstance(raw, list):
            return None  # back-reference to an already-serialized object (a cycle)
        out: dict[str, Any] = {}
        for key, item in raw:
            if isinstance(item, dict) and item.get("type") == "undefined":
                continue
            name = key if isinstance(key, str) else str(remote_value_to_py(key))
            out[name] = remote_value_to_py(item)
        return out
    if kind in ("nodelist", "htmlcollection"):
        return {str(i): {} for i in range(len(raw))} if isinstance(raw, list) else {}
    return {}
