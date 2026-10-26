"""Minimal Chrome DevTools Protocol client.

Assumes Chrome was launched with `--remote-debugging-port=PORT` and a
separate `--user-data-dir` (required on macOS). Tab discovery goes through
the HTTP /json endpoint (``browser.CdpEndpoint``); this client attaches to one
page's WebSocket, which is an implicit session, so there is no
Target.attachToTarget / sessionId plumbing.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from .browser_rpc import JsonRpcSocket, TabHelpers, js_str


class CDPError(RuntimeError):
    """CDP command failed (either protocol error or a JS exception)."""


class CDPClient(JsonRpcSocket, TabHelpers):
    """Async CDP client scoped to a single page target:

        async with await CDPClient.connect(tab_info) as cdp:
            r = await cdp.evaluate("window.innerWidth")
            png = await cdp.capture_screenshot(clip={...})
    """

    error = CDPError
    label = "CDP"

    def __init__(self, tab_info: dict) -> None:
        super().__init__()
        self._tab = tab_info

    @classmethod
    async def connect(cls, tab_info: dict) -> CDPClient:
        """Open a WS to an already-discovered page target (a /json/list entry)."""
        client = cls(tab_info)
        await client._open(tab_info["webSocketDebuggerUrl"])
        return client

    async def close(self) -> None:
        await self._teardown()

    async def __aenter__(self) -> CDPClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _frame(self, cmd_id: int, method: str, params: dict) -> dict:
        return {"id": cmd_id, "method": method, **({"params": params} if params else {})}

    def _event(self, msg: dict) -> tuple[str, dict] | None:
        if msg.get("id") is None and msg.get("method"):
            return msg["method"], msg.get("params") or {}
        return None

    def _result(self, method: str, reply: dict) -> dict:
        if "error" in reply:
            err = reply["error"]
            raise CDPError(f"{method} failed: {err.get('message', err)}")
        return reply.get("result", {})

    async def send(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        """Send a CDP command, return its `result` field.

        Raises CDPError on protocol errors. Does NOT inspect per-method
        exception fields (e.g. Runtime.evaluate.exceptionDetails); caller
        handles those.
        """
        return await self._send_raw(method, params or {}, timeout=timeout)

    @property
    def tab(self) -> dict:
        return self._tab

    async def evaluate(self, expression: str, *,
                       await_promise: bool = True,
                       return_by_value: bool = True,
                       user_activation: bool = True) -> Any:
        """Run JS in the attached page, return its JSON-serialized value.

        Raises CDPError if the JS throws — wraps Chrome's exceptionDetails
        into a readable message. `None` is a legitimate return value
        (distinct from exceptions). `user_activation` runs it as a user
        gesture (popups, clipboard, fullscreen); probes turn it off.
        """
        res = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
            "userGesture": user_activation,
        })
        if "exceptionDetails" in res:
            ed = res["exceptionDetails"]
            exc = ed.get("exception", {})
            msg = exc.get("description") or exc.get("value") or ed.get("text", "exception")
            raise CDPError(f"Runtime.evaluate threw: {msg}")
        result = res.get("result", {})
        return result.get("value")

    async def wait_for_network_idle(self, *, quiet_ms: int = 500,
                                    timeout_ms: int = 5000) -> bool:
        """Wait until no in-flight non-streaming requests for `quiet_ms`.

        Tracks `Network.requestWillBeSent` against
        `Network.loadingFinished` / `Network.loadingFailed`. Excludes
        WebSocket, EventSource, and Other request types so pages with
        keepalive connections (Slack, Notion, GitHub, anything with a
        chat widget) don't peg the in-flight counter above zero
        forever. Returns True on idle, False on timeout. Enables the
        Network domain if needed and unregisters its handlers after.
        """
        in_flight: set[str] = set()
        skip_types = {"WebSocket", "EventSource", "Other"}
        loop = asyncio.get_running_loop()
        last_activity = [loop.time()]

        def _on_request(params: dict) -> None:
            if params.get("type") in skip_types:
                return
            rid = params.get("requestId")
            if rid:
                in_flight.add(rid)
                last_activity[0] = loop.time()

        def _on_finished(params: dict) -> None:
            rid = params.get("requestId")
            if rid and rid in in_flight:
                in_flight.discard(rid)
                last_activity[0] = loop.time()

        try:
            await self.send("Network.enable")
        except CDPError:
            pass
        handlers = [("Network.requestWillBeSent", _on_request),
                    ("Network.loadingFinished", _on_finished),
                    ("Network.loadingFailed", _on_finished)]
        for method, handler in handlers:
            self.on(method, handler)
        try:
            return await self._until_quiet(in_flight, last_activity,
                                           quiet_ms=quiet_ms, timeout_ms=timeout_ms)
        finally:
            for method, handler in handlers:
                self.off(method, handler)

    async def capture_screenshot(self, *, clip: dict | None = None,
                                  beyond_viewport: bool = True,
                                  fmt: str = "png") -> bytes:
        """Capture a screenshot. `clip` in CSS-pixel document coords."""
        params: dict[str, Any] = {"format": fmt, "fromSurface": True,
                                   "captureBeyondViewport": beyond_viewport}
        if clip is not None:
            params["clip"] = {**clip, "scale": clip.get("scale", 1)}
        res = await self.send("Page.captureScreenshot", params)
        return base64.b64decode(res["data"])


# --- shared JS helpers -------------------------------------------------------

# Resolve an element and return a discriminated record describing the
# outcome. Possible shapes:
#   {kind: "ok", x, y, w, h}        — element found, bbox in document coords
#   {kind: "missing"}                — locator returned no element
#   {kind: "ambiguous", count: N}    — selector matched N>1 elements
#   {kind: "oversized", w, h}        — bbox area > viewport × OVERSIZED_FACTOR;
#                                       caller likely climbed past the target
#
# The structured return replaces an earlier "bbox or null" shape so that
# `web_crop` can map failures to specific, actionable error strings instead
# of conflating "selector matched 50 elements" with "JS threw" with "element
# bbox is huge enough to make scrollIntoView wedge for 30s".
#
# Oversized check also dodges the 30s `Runtime.evaluate` timeout that used
# to fire when ambiguous selectors picked a giant ancestor: we never even
# attempt scrollIntoView + 2 RAF on those. Likewise, if the element is
# already on-screen we skip the scroll path entirely (the original wedge
# trigger), which is both faster and cheaper.
#
# Kept here (not in server.py) so the module that owns the CDP protocol also
# owns the JS it synthesizes.
RESOLVE_BBOX_TEMPLATE = r"""
(async () => {
    function findDeepestByText(text) {
        if (!document.body || !document.body.textContent.includes(text)) return null;
        let node = document.body;
        while (true) {
            const child = Array.from(node.children).find(
                c => c.textContent && c.textContent.includes(text)
            );
            if (!child) return node;
            node = child;
        }
    }
    // A background tab never runs animation frames; bound the wait so it
    // costs 200 ms instead of the 30 s evaluate timeout.
    const raf = () => Promise.race([
        new Promise(r => requestAnimationFrame(r)),
        new Promise(r => setTimeout(r, 200)),
    ]);
    const OVERSIZED_FACTOR = 6;  // bbox area > viewport×6 → flag, don't scroll
    let resolved = (__RESOLVE__);
    if (resolved.kind !== "ok") return resolved;
    let node = resolved.node;
    for (let i = 0; i < __CLIMB__; i++) { node = node && node.parentElement; }
    if (!node) return {kind: "missing"};
    // web_crop finds the target again by this to keep it and its own fixed
    // parts visible while hiding overlays; it removes the mark afterwards.
    try { node.setAttribute('data-conductor-crop', ''); } catch (e) {}
    const r0 = node.getBoundingClientRect();
    const vw = innerWidth, vh = innerHeight;
    const elArea = Math.max(1, r0.width * r0.height);
    if (elArea > vw * vh * OVERSIZED_FACTOR) {
        return {kind: "oversized",
                w: Math.round(r0.width), h: Math.round(r0.height)};
    }
    if (__SCROLL__) {
        // Skip scrollIntoView when the element already intersects the
        // viewport. The 2-RAF wait below is for IntersectionObserver-gated
        // chart libs, but invoking scrollIntoView on a giant in-view element
        // can wedge the page for tens of seconds before the eval times out.
        const inView = r0.bottom > 0 && r0.top < vh
                    && r0.right > 0 && r0.left < vw;
        if (!inView) {
            try { node.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
            // Wait two animation frames after scrolling so IntersectionObserver
            // callbacks fire AND paint-gated chart libraries (Chart.js, ECharts,
            // Grafana panels) get a chance to draw before we capture.
            await raf();
            await raf();
        }
    }
    const r = node.getBoundingClientRect();
    return {
        kind: "ok",
        x: Math.round(r.x + window.scrollX),
        y: Math.round(r.y + window.scrollY),
        w: Math.round(r.width),
        h: Math.round(r.height),
    };
})()
""".strip()


def build_resolve_bbox_js(*, selector: str | None = None,
                          contains_text: str | None = None,
                          element_js: str | None = None,
                          climb: int = 0,
                          scroll_into_view: bool = True) -> str:
    """Build the bbox-resolution JS for the given locator strategy.

    Exactly one of selector/contains_text/element_js must be provided.

    Returns a JS expression that resolves to a discriminated record (see
    `RESOLVE_BBOX_TEMPLATE` for the kinds). Selector locators get a
    strict count pre-check: N>1 matches → `{kind: "ambiguous"}` instead
    of silently picking the first one and possibly cropping the wrong
    element.
    """
    provided = [x is not None for x in (selector, contains_text, element_js)]
    if sum(provided) != 1:
        raise ValueError(
            "exactly one of selector/contains_text/element_js must be set"
        )
    if selector is not None:
        # IIFE so the multi-line dispatch fits in the __RESOLVE__ slot.
        resolve = (
            "(() => {"
            f" const ns = document.querySelectorAll({js_str(selector)});"
            " if (ns.length === 0) return {kind: \"missing\"};"
            " if (ns.length > 1) return {kind: \"ambiguous\", count: ns.length};"
            " return {kind: \"ok\", node: ns[0]};"
            " })()"
        )
    elif contains_text is not None:
        resolve = (
            "(() => {"
            f" const n = findDeepestByText({js_str(contains_text)});"
            " return n ? {kind: \"ok\", node: n} : {kind: \"missing\"};"
            " })()"
        )
    else:
        # element_js: wrap in parens to allow full expressions; n=null → missing.
        resolve = (
            "(() => {"
            f" const n = ({element_js});"
            " return n ? {kind: \"ok\", node: n} : {kind: \"missing\"};"
            " })()"
        )
    return (RESOLVE_BBOX_TEMPLATE
            .replace("__RESOLVE__", resolve)
            .replace("__CLIMB__", str(int(climb)))
            .replace("__SCROLL__", "true" if scroll_into_view else "false"))
