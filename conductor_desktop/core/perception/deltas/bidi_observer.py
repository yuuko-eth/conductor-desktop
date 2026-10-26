"""WebDriver BiDi-backed delta observer.

The Firefox counterpart of ``cdp_observer``: same Layer 1 retention policy
and the same StateDelta shapes, so the brain reads one envelope format for
every browser. Two BiDi-specific differences:

- BiDi multiplexes every tab over one connection, so handlers filter events
  to this tab's browsing context (and the iframes it had at start()).
- BiDi has no DOM domain. A MutationObserver is injected into a sandbox
  (a preload script for future documents, callFunction for the current one)
  and reports back over a ``script.message`` channel.

Layer 2 (coalesce) and Layer 3 (cap) live in observer_base.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from ....bidi import BidiError, remote_value_to_py
from ..classifier import StateDelta
from ..observer_base import AsyncObserver
from .cdp_observer import CONSOLE_KEEP_LEVELS, KEEP_ATTRS, _url_origin_path

log = logging.getLogger("conductor.perception.bidi")

# Isolated JS realm for the injected watcher — invisible to page scripts.
_SANDBOX = "conductor"

_NAV_COMMITTED = "browsingContext.navigationCommitted"

# Roles surfaced on insertion; mirrors CDPObserver._on_node_inserted.
_ADDED_ROLES = ["dialog", "alertdialog", "alert", "menu", "tooltip"]

_DOM_WATCH_JS = r"""
(send) => {
    const CH = __CHANNEL__;
    const reg = (window.__conductorDom = window.__conductorDom || {});
    if (reg[CH]) return;
    const ids = new WeakMap();
    let next = 1;
    const idOf = (n) => {
        let i = ids.get(n);
        if (!i) { i = next++; ids.set(n, i); }
        return i;
    };
    const ROLES = __ROLES__;
    const visible = (n) => n.checkVisibility ? n.checkVisibility()
                                             : n.getClientRects().length > 0;
    const mo = new MutationObserver((records) => {
        for (const r of records) {
            if (r.type === "attributes") {
                send(["attr", idOf(r.target), r.attributeName,
                      r.target.getAttribute(r.attributeName)]);
                continue;
            }
            for (const n of r.addedNodes) {
                if (n.nodeType !== 1) continue;
                const role = (n.getAttribute("role") || "").toLowerCase();
                if (n.localName === "dialog" || ROLES.includes(role)
                        || n.getAttribute("aria-modal") === "true") {
                    // Pages pre-render closed <dialog>/popover templates
                    // (huggingface.co: 27 hidden of 28). Surface only what a
                    // user can see; a template opening later flips `open`,
                    // which arrives as an attribute delta.
                    if (n.localName === "dialog" ? !n.open : !visible(n)) continue;
                    send(["add", idOf(n), n.localName, role,
                          n.getAttribute("aria-label") || n.getAttribute("title")]);
                }
            }
        }
    });
    mo.observe(document, {subtree: true, childList: true, attributes: true,
                          attributeFilter: __KEEP__});
    reg[CH] = mo;
}
""".strip()


class BidiObserver(AsyncObserver):
    """Subscribes to a BidiTab's connection and emits normalized StateDeltas.

    Lifecycle is per tool call: start() subscribes (scoped to the tab) and
    injects the DOM watcher; stop() undoes both. The connection itself is
    owned by the browser registry.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, session, **kw) -> None:
        super().__init__(loop, **kw)
        self._session = session
        self._conn = session.conn
        self._contexts: set[str] = {session.context}
        self._handlers: list[tuple[str, callable]] = []
        self._subscription: str | None = None
        self._preload: str | None = None
        self._dom_installed = False
        self._channel = f"conductor-dom-{uuid.uuid4().hex[:12]}"
        # False until session.subscribe returns — see _wire.
        self._live = False

    async def start(self) -> None:
        try:
            self._contexts = await self._session.context_ids()
        except BidiError as e:
            log.warning("BiDi getTree failed; filtering on top context only: %s", e)
        self._wire(_NAV_COMMITTED, self._on_navigation)
        self._wire("browsingContext.fragmentNavigated", self._on_spa_navigation)
        self._wire("browsingContext.historyUpdated", self._on_spa_navigation)
        self._wire("browsingContext.domContentLoaded", self._on_dom_content_loaded)
        self._wire("browsingContext.load", self._on_load)
        self._wire("log.entryAdded", self._on_log)
        self._wire("network.responseCompleted", self._on_response)
        self._wire("script.message", self._on_message)
        events = [method for method, _ in self._handlers]
        top = [self._session.context]
        try:
            self._subscription = await self._conn.subscribe(events, top)
        except BidiError as e:
            # Older Firefox builds lack navigationCommitted and reject the
            # whole subscribe; navigationStarted is the closest stand-in.
            log.warning("BiDi subscribe failed (%s); retrying with navigationStarted", e)
            self._wire("browsingContext.navigationStarted", self._on_navigation)
            fallback = [m for m, _ in self._handlers if m != _NAV_COMMITTED]
            self._subscription = await self._conn.subscribe(fallback, top)
        self._live = True
        await self._install_dom_watch()

    async def stop(self) -> None:
        # Idempotent: safe to call multiple times.
        self._live = False
        for method, handler in self._handlers:
            self._conn.off(method, handler)
        self._handlers.clear()
        subscription, self._subscription = self._subscription, None
        await self._conn.unsubscribe(subscription)
        preload, self._preload = self._preload, None
        if preload:
            try:
                await self._conn.send("script.removePreloadScript",
                                      {"script": preload}, timeout=5.0)
            except BidiError as e:
                log.warning("BiDi removePreloadScript failed: %s", e)
        if self._dom_installed:
            self._dom_installed = False
            ch = json.dumps(self._channel)
            # Watchers left in iframes stay connected until those frames
            # navigate; with the subscription gone their messages go nowhere.
            try:
                await self._conn.send("script.evaluate", {
                    "expression": (
                        f"(() => {{ const r = window.__conductorDom;"
                        f" if (r && r[{ch}]) {{ r[{ch}].disconnect(); delete r[{ch}]; }} }})()"
                    ),
                    "target": {"context": self._session.context, "sandbox": _SANDBOX},
                    "awaitPromise": False,
                    "resultOwnership": "none",
                }, timeout=5.0)
            except BidiError as e:
                log.warning("BiDi DOM watcher disconnect failed: %s", e)

    def _wire(self, method: str, handler) -> None:
        def gated(params: dict) -> None:
            # BiDi buffers log entries logged before a subscription exists and
            # flushes them when one is created — ahead of the subscribe
            # response. Seen live: an earlier call's console.error resurfaced
            # in the next call's envelope. Those predate this call; drop them.
            if self._live:
                handler(params)

        self._conn.on(method, gated)
        self._handlers.append((method, gated))

    async def _install_dom_watch(self) -> None:
        fn = (_DOM_WATCH_JS
              .replace("__CHANNEL__", json.dumps(self._channel))
              .replace("__ROLES__", json.dumps(_ADDED_ROLES))
              .replace("__KEEP__", json.dumps(sorted(KEEP_ATTRS))))
        args = [{"type": "channel", "value": {"channel": self._channel}}]
        try:
            res = await self._conn.send("script.addPreloadScript", {
                "functionDeclaration": fn,
                "arguments": args,
                "contexts": [self._session.context],
                "sandbox": _SANDBOX,
            })
            self._preload = res.get("script")
        except BidiError as e:
            log.warning("BiDi addPreloadScript failed: %s", e)
        try:
            await self._conn.send("script.callFunction", {
                "functionDeclaration": fn,
                "arguments": args,
                "target": {"context": self._session.context, "sandbox": _SANDBOX},
                "awaitPromise": False,
                "resultOwnership": "none",
            })
            self._dom_installed = True
        except BidiError as e:
            # e.g. privileged about: pages refuse script — deltas still flow
            # for navigation / console / network.
            log.warning("BiDi DOM watcher install failed: %s", e)

    # ── Normalizers ────────────────────────────────────────────────
    # Each handler runs on the BidiConnection reader task (asyncio loop).

    def _now(self) -> float:
        return time.monotonic()

    def _on_navigation(self, params: dict) -> None:
        # Top-level only, as in CDPObserver — sub-frame navs are ads/analytics.
        if params.get("context") != self._session.context:
            return
        url = params.get("url") or ""
        if not url:
            return
        self._emit(StateDelta(
            kind="navigation",
            source="bidi",
            monotonic_ts=self._now(),
            source_id=url,
            extra={"url": url},
        ))

    def _on_spa_navigation(self, params: dict) -> None:
        """fragmentNavigated / historyUpdated — pushState, replaceState, #hash."""
        if params.get("context") != self._session.context:
            return
        url = params.get("url") or ""
        if not url:
            return
        self._emit(StateDelta(
            kind="navigation",
            source="bidi",
            monotonic_ts=self._now(),
            source_id=url,
            extra={"url": url, "spa": True},
        ))

    def _on_dom_content_loaded(self, params: dict) -> None:
        self._lifecycle(params, "DOMContentLoaded")

    def _on_load(self, params: dict) -> None:
        self._lifecycle(params, "load")

    def _lifecycle(self, params: dict, phase: str) -> None:
        # Collapses across frames via source_id=phase, as in CDPObserver.
        if params.get("context") not in self._contexts:
            return
        self._emit(StateDelta(
            kind="window_state_changed",
            source="bidi",
            monotonic_ts=self._now(),
            source_id=phase,
            extra={"phase": phase},
        ))

    def _on_log(self, params: dict) -> None:
        if (params.get("source") or {}).get("context") not in self._contexts:
            return
        text = params.get("text") or ""
        if params.get("type") == "javascript":
            # Firefox logs failures of its own script-argument plumbing as
            # page errors — seen when a cross-origin navigation races the DOM
            # watcher injection. Ours, not the page's.
            if "__bidi_args" in text:
                return
            # Uncaught exceptions (Firefox also routes CORS/CSP failures
            # here) — always interesting, promote to error.
            self._emit(StateDelta(
                kind="console",
                source="bidi",
                monotonic_ts=self._now(),
                value=text or "exception",
                extra={"level": "error", "uncaught": True},
            ))
            return
        method = params.get("method") or params.get("level") or ""
        level = "warning" if method == "warn" else method
        if level not in CONSOLE_KEEP_LEVELS:
            return
        if not text:
            text = " ".join(str(remote_value_to_py(a)) for a in params.get("args") or [])
        self._emit(StateDelta(
            kind="console",
            source="bidi",
            monotonic_ts=self._now(),
            value=text,
            extra={"level": level},
        ))

    def _on_response(self, params: dict) -> None:
        if params.get("context") not in self._contexts:
            return
        request = params.get("request") or {}
        rtype = _resource_type(request)
        if rtype is None:
            return
        status = (params.get("response") or {}).get("status", 0)
        if rtype != "Document" and status < 400:
            return
        url = request.get("url") or (params.get("response") or {}).get("url", "")
        self._emit(StateDelta(
            kind="network",
            source="bidi",
            monotonic_ts=self._now(),
            source_id=f"{status}:{_url_origin_path(url)}",
            extra={"status": status, "url": url, "type": rtype},
        ))

    def _on_message(self, params: dict) -> None:
        if params.get("channel") != self._channel:
            return
        data = remote_value_to_py(params.get("data"))
        if not isinstance(data, list) or not data:
            return
        if data[0] == "attr" and len(data) >= 4:
            _, node_id, attr, value = data[:4]
            if attr not in KEEP_ATTRS:
                return
            if attr in {"aria-label", "aria-labelledby"}:
                kind = "name_changed"
            elif attr == "value":
                kind = "value_changed"
            else:
                kind = "state_changed"
            self._emit(StateDelta(
                kind=kind,
                source="bidi",
                monotonic_ts=self._now(),
                source_id=str(node_id) if node_id is not None else None,
                value=str(value) if value is not None else None,
                extra={"attr": attr},
            ))
        elif data[0] == "add" and len(data) >= 5:
            _, node_id, local_name, role, name = data[:5]
            self._emit(StateDelta(
                kind="node_added",
                source="bidi",
                monotonic_ts=self._now(),
                source_id=str(node_id) if node_id is not None else None,
                role=role or local_name,
                name=name,
            ))


def _resource_type(request: dict) -> str | None:
    """Map BiDi request metadata onto CDPObserver's NETWORK_KEEP_TYPES.

    Fetch-spec destinations: documents are "document"/"iframe"; fetch() and
    XHR have an empty destination. Anything else (images, scripts, fonts)
    is dropped, as CDPObserver drops non-Document/XHR/Fetch types.
    """
    destination = request.get("destination")
    initiator = request.get("initiatorType")
    if destination in ("document", "iframe"):
        return "Document"
    if initiator == "xmlhttprequest":
        return "XHR"
    if initiator == "fetch" or destination == "":
        return "Fetch"
    return None
