"""CDP-backed delta observer.

Subscribes to a small set of high-signal CDP events on an existing
CDPClient connection, normalizes them into StateDelta, and feeds the
AsyncObserver buffer.

Layer 1 (per-kind retention) lives here: most CDP events never become
StateDeltas because the source is firehose-grade (DOM.attributeModified
on every class flip, Network.responseReceived on every analytics beacon).
The retention policy is deliberately conservative — surface the events
that change *meaning*, not the events that change *bytes*.

Layer 2 (coalesce) and Layer 3 (cap) live in observer_base.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..classifier import StateDelta
from ..observer_base import AsyncObserver

log = logging.getLogger("conductor.perception.cdp")


# Layer 1 — DOM attribute retention.
# The vast majority of attributeModified events are class-name flips,
# style mutations, or framework-generated data-* attributes. These keep
# the stream from drowning the buffer. ARIA attributes are kept because
# they signal accessibility-relevant state changes (validation, expanded
# state, hidden); `value` is kept for inputs; `disabled` for buttons.
KEEP_ATTRS: frozenset[str] = frozenset({
    "aria-invalid",
    "aria-expanded",
    "aria-checked",
    "aria-pressed",
    "aria-selected",
    "aria-hidden",
    "aria-disabled",
    "aria-label",
    "aria-labelledby",
    "disabled",
    "hidden",
    "value",
    "checked",
    "open",  # <details>, <dialog>
})

# Layer 1 — Network retention.
# Skip everything except: (a) main-frame document responses (page-level
# navigation signal beyond Page.frameNavigated), (b) any 4xx/5xx (errors
# the brain should be able to see), (c) XHR/fetch with JSON response
# (likely an API call that affects app state).
NETWORK_KEEP_TYPES: frozenset[str] = frozenset({"Document", "XHR", "Fetch"})

# Console-level retention: drop log/info/debug; keep error/warning and
# unhandled rejections. Brain doesn't need to see every console.log.
CONSOLE_KEEP_LEVELS: frozenset[str] = frozenset({"error", "warning", "assert"})


class CDPObserver(AsyncObserver):
    """Subscribes to a CDPClient and emits normalized StateDeltas.

    Lifecycle is bound to the CDPClient connection: start() enables the
    necessary CDP domains and registers handlers; stop() unregisters
    them. The CDPClient itself is owned by the caller (typically the
    web_* tool that opened the connection).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, client, **kw) -> None:
        super().__init__(loop, **kw)
        self._client = client
        # Track our handler refs so stop() can unregister cleanly. Each
        # entry is (method, callable).
        self._handlers: list[tuple[str, callable]] = []

    async def start(self) -> None:
        # Enable the domains we read from. All idempotent — safe to call
        # even if another consumer enabled them first.
        for domain in ("Page", "DOM", "Runtime", "Network"):
            try:
                await self._client.send(f"{domain}.enable")
            except Exception as e:
                # Some domains (Network) require a permission on certain
                # target types; log and continue rather than fail start.
                log.warning("CDP %s.enable failed: %s", domain, e, exc_info=True)
        # Page.enable alone does NOT enable Page.lifecycleEvent — that
        # needs an explicit opt-in. Without this, "load" / "DOMContentLoaded" never fire.
        try:
            await self._client.send("Page.setLifecycleEventsEnabled",
                                    {"enabled": True})
        except Exception as e:
            log.warning("Page.setLifecycleEventsEnabled failed: %s", e, exc_info=True)
        self._wire("Page.frameNavigated", self._on_frame_navigated)
        self._wire("Page.navigatedWithinDocument", self._on_within_doc_nav)
        self._wire("Page.lifecycleEvent", self._on_lifecycle)
        self._wire("DOM.attributeModified", self._on_attr_modified)
        self._wire("DOM.childNodeInserted", self._on_node_inserted)
        self._wire("DOM.childNodeRemoved", self._on_node_removed)
        self._wire("Runtime.consoleAPICalled", self._on_console)
        self._wire("Runtime.exceptionThrown", self._on_exception)
        self._wire("Network.responseReceived", self._on_response)

    async def stop(self) -> None:
        # Idempotent: safe to call multiple times.
        for method, handler in self._handlers:
            self._client.off(method, handler)
        self._handlers.clear()

    def _wire(self, method: str, handler) -> None:
        self._client.on(method, handler)
        self._handlers.append((method, handler))

    # ── Normalizers ────────────────────────────────────────────────
    # Each handler runs on the CDPClient reader task (asyncio loop).
    # Keep them allocation-light: filter early, build StateDelta, _emit.

    def _now(self) -> float:
        return time.monotonic()

    def _on_frame_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        # Only main-frame nav is interesting; sub-frame navs are usually
        # ad iframes and analytics. Brain cares about "where am I now?"
        if frame.get("parentId") is not None:
            return
        url = frame.get("url") or ""
        frame_id = frame.get("id")
        # Malformed event with no useful data — drop rather than emit
        # an empty-URL navigation that would mislead the brain.
        if not url and not frame_id:
            return
        # source_id = url so duplicate fires for the same transition
        # (frameNavigated + navigatedWithinDocument firing back-to-back
        # on a redirect) collapse via Layer 2.
        self._emit(StateDelta(
            kind="navigation",
            source="cdp",
            monotonic_ts=self._now(),
            source_id=url or frame_id,
            extra={"url": url},
        ))

    def _on_within_doc_nav(self, params: dict) -> None:
        """Page.navigatedWithinDocument — SPA pushState/replaceState/hashchange.

        Modern web apps (YouTube, Gmail, Sheets, Discord, every React-router
        site) navigate within a single document; Page.frameNavigated never
        fires. Without this handler the observer is silent on SPA nav.
        """
        url = params.get("url") or ""
        frame_id = params.get("frameId")
        if not url and not frame_id:
            return
        self._emit(StateDelta(
            kind="navigation",
            source="cdp",
            monotonic_ts=self._now(),
            source_id=url or frame_id,
            extra={"url": url, "spa": True},
        ))

    def _on_lifecycle(self, params: dict) -> None:
        # Only "load" and "DOMContentLoaded" — the other lifecycle phases
        # (init, firstPaint, networkIdle) are useful for perf timing,
        # not for the brain's "did the page settle?" question.
        name = params.get("name")
        if name not in ("load", "DOMContentLoaded"):
            return
        # source_id = phase only (drop frameId). YouTube and similar pages
        # have many iframes (ads, embeds, workers) that all fire their
        # own DOMContentLoaded/load — 3+3 on a single page transition is
        # typical. The agent doesn't care which frame; "the surface is
        # settling" is the signal. Collapsing across frames turns a 6-line
        # storm into 2 lines with (×3) each.
        self._emit(StateDelta(
            kind="window_state_changed",
            source="cdp",
            monotonic_ts=self._now(),
            source_id=name,
            extra={"phase": name},
        ))

    def _on_attr_modified(self, params: dict) -> None:
        attr = params.get("name")
        if attr not in KEEP_ATTRS:
            return
        node_id = params.get("nodeId")
        value = params.get("value")
        # State-bit attributes flip booleans → state_changed.
        # Value/text/label changes → value_changed (or name_changed for label).
        if attr in {"aria-label", "aria-labelledby"}:
            kind = "name_changed"
        elif attr == "value":
            kind = "value_changed"
        else:
            kind = "state_changed"
        self._emit(StateDelta(
            kind=kind,
            source="cdp",
            monotonic_ts=self._now(),
            source_id=str(node_id) if node_id is not None else None,
            value=str(value) if value is not None else None,
            extra={"attr": attr},
        ))

    def _on_node_inserted(self, params: dict) -> None:
        # CDP delivers the inserted node inline; we look at its nodeName
        # and attributes to decide whether it's interesting. Skip text
        # nodes (nodeType == 3) — those are content edits, not structural.
        node = params.get("node") or {}
        if node.get("nodeType") != 1:  # 1 = ELEMENT_NODE
            return
        node_name = (node.get("nodeName") or "").lower()
        # Retention: only surface dialogs / alerts / form-roles / common
        # interactive containers. Otherwise the brain drowns in <span>
        # appearances on every dynamic page.
        attrs = _attr_dict(node.get("attributes") or [])
        role = attrs.get("role", "").lower()
        is_interesting = (
            node_name == "dialog"
            or role in {"dialog", "alertdialog", "alert", "menu", "tooltip"}
            or attrs.get("aria-modal") == "true"
        )
        if not is_interesting:
            return
        self._emit(StateDelta(
            kind="node_added",
            source="cdp",
            monotonic_ts=self._now(),
            source_id=str(node.get("nodeId")) if node.get("nodeId") else None,
            role=role or node_name,
            name=attrs.get("aria-label") or attrs.get("title"),
        ))

    def _on_node_removed(self, params: dict) -> None:
        # We don't have rich element data here (it's destroyed); just the
        # nodeId. Without prior tracking we can't tell if it was a dialog
        # or a span — skip emitting for v1. node_added gives us the modal-
        # appeared signal; modal-dismissed is usually accompanied by a
        # focus_changed back to the underlying surface, which is enough.
        return

    def _on_console(self, params: dict) -> None:
        level = params.get("type", "")
        if level not in CONSOLE_KEEP_LEVELS:
            return
        args = params.get("args") or []
        text = " ".join(_console_arg_str(a) for a in args)
        # Use `value` for the message (to_text renders it as value="…").
        # `level` lives in extra for the inline `level="error"` annotation.
        self._emit(StateDelta(
            kind="console",
            source="cdp",
            monotonic_ts=self._now(),
            value=text,
            extra={"level": level},
        ))

    def _on_exception(self, params: dict) -> None:
        # Uncaught exceptions are always interesting — promote to
        # console-error level regardless of where they came from.
        ed = params.get("exceptionDetails") or {}
        exc = ed.get("exception") or {}
        text = exc.get("description") or exc.get("value") or ed.get("text", "exception")
        self._emit(StateDelta(
            kind="console",
            source="cdp",
            monotonic_ts=self._now(),
            value=str(text),
            extra={"level": "error", "uncaught": True},
        ))

    def _on_response(self, params: dict) -> None:
        rtype = params.get("type", "")
        if rtype not in NETWORK_KEEP_TYPES:
            return
        resp = params.get("response") or {}
        status = resp.get("status", 0)
        url = resp.get("url", "")
        # Drop successful XHR/Fetch — only flag errors, plus all Document
        # responses regardless of status (they're page-level signal).
        if rtype != "Document" and status < 400:
            return
        # source_id = host+path (drop query/fragment) so signed-URL retry
        # storms (videoplayback chunks, OAuth token refreshes, etc.)
        # collapse via coalesce. Brain sees "this resource keeps 4xx-ing"
        # via repeat_count, not 6 near-identical 1.5KB URL dumps.
        # Note: Network.responseReceived doesn't carry the request method
        # (that's on Network.requestWillBeSent). Skip it for v1.
        self._emit(StateDelta(
            kind="network",
            source="cdp",
            monotonic_ts=self._now(),
            source_id=f"{status}:{_url_origin_path(url)}",
            extra={"status": status, "url": url, "type": rtype},
        ))


# ── helpers ──────────────────────────────────────────────────────────

def _attr_dict(attrs: list) -> dict[str, str]:
    """CDP attributes come as flat [name, value, name, value, ...]. Pair them up."""
    out: dict[str, str] = {}
    it = iter(attrs)
    for k in it:
        try:
            v = next(it)
        except StopIteration:
            break
        out[k] = v
    return out


def _console_arg_str(arg: dict) -> str:
    """Render a Runtime.RemoteObject for console output. Cheap form."""
    if "value" in arg:
        return str(arg["value"])
    return str(arg.get("description") or arg.get("type") or "")


def _url_origin_path(url: str) -> str:
    """Return scheme://host/path from a URL, dropping query + fragment.

    Used as the network-delta coalesce key so retry storms with different
    signed query params collapse onto one entry. Returns the input
    unchanged if it doesn't look like a URL (defensive — bad URLs
    shouldn't crash the observer).
    """
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    return url
