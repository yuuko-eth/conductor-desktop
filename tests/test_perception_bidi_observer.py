"""Tests for perception.deltas.bidi_observer.

A fake BiDi connection records subscriptions and replays event payloads in
the shapes Firefox 152 emits. No browser required.
"""

from __future__ import annotations

import asyncio

from conductor_desktop.bidi import BidiError, BidiTab
from conductor_desktop.core.perception.deltas.bidi_observer import BidiObserver


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self._handlers: dict[str, list] = {}
        self.subscribed: list[tuple[list, list | None]] = []
        self.unsubscribed: list[str | None] = []
        self.fail_subscribes = 0
        # Events the "browser" flushes while processing session.subscribe.
        self.replay: list[tuple[str, dict]] = []

    async def send(self, method, params=None, timeout=30.0):
        self.sent.append((method, params))
        if method == "browsingContext.getTree":
            return {"contexts": [{"context": "top", "children": [
                {"context": "frame-1", "children": []}]}]}
        if method == "script.addPreloadScript":
            return {"script": "preload-1"}
        return {}

    async def subscribe(self, events, contexts=None):
        if self.fail_subscribes:
            self.fail_subscribes -= 1
            raise BidiError("session.subscribe failed: invalid argument")
        for method, params in self.replay:
            self.fire(method, params)
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


async def _started(conn: FakeConn | None = None):
    conn = conn or FakeConn()
    obs = BidiObserver(asyncio.get_running_loop(), BidiTab(conn, "top"))
    await obs.start()
    return conn, obs


async def _drain(obs):
    await asyncio.sleep(0)
    deltas, _, _ = obs.drain_and_filter()
    return deltas


def _rv(item):
    if item is None:
        return {"type": "null"}
    if isinstance(item, int):
        return {"type": "number", "value": item}
    return {"type": "string", "value": item}


def _message(obs, *items, channel=None):
    return {"channel": channel or obs._channel,
            "data": {"type": "array", "value": [_rv(i) for i in items]},
            "source": {"context": "top"}}


# ── lifecycle ───────────────────────────────────────────────────────

async def test_start_subscribes_scoped_to_tab():
    conn, _ = await _started()
    events, contexts = conn.subscribed[0]
    assert contexts == ["top"]
    for ev in ("browsingContext.navigationCommitted", "browsingContext.load",
               "log.entryAdded", "network.responseCompleted", "script.message"):
        assert ev in events


async def test_start_installs_dom_watcher_in_sandbox():
    conn, obs = await _started()
    by_method = {m: p for m, p in conn.sent}
    for method in ("script.addPreloadScript", "script.callFunction"):
        params = by_method[method]
        assert params["arguments"] == [
            {"type": "channel", "value": {"channel": obs._channel}}]
        assert "MutationObserver" in params["functionDeclaration"]
    assert by_method["script.addPreloadScript"]["sandbox"] == "conductor"
    assert by_method["script.callFunction"]["target"] == {
        "context": "top", "sandbox": "conductor"}


async def test_stop_undoes_everything_and_is_idempotent():
    conn, obs = await _started()
    await obs.stop()
    assert conn.unsubscribed == ["sub-1"]
    methods = [m for m, _ in conn.sent]
    assert "script.removePreloadScript" in methods
    assert all(h == [] for h in conn._handlers.values())
    sent_before = len(conn.sent)
    await obs.stop()
    assert len(conn.sent) == sent_before
    assert conn.unsubscribed == ["sub-1", None]


async def test_subscribe_falls_back_to_navigation_started():
    conn = FakeConn()
    conn.fail_subscribes = 1
    conn, obs = await _started(conn)
    events, _ = conn.subscribed[0]
    assert "browsingContext.navigationCommitted" not in events
    assert "browsingContext.navigationStarted" in events
    conn.fire("browsingContext.navigationStarted",
              {"context": "top", "url": "https://a.example/"})
    deltas = await _drain(obs)
    assert [d.kind for d in deltas] == ["navigation"]


# ── navigation & lifecycle ──────────────────────────────────────────

async def test_top_level_navigation_emits():
    conn, obs = await _started()
    conn.fire("browsingContext.navigationCommitted",
              {"context": "top", "url": "https://a.example/x", "navigation": "n1"})
    conn.fire("browsingContext.navigationCommitted",
              {"context": "frame-1", "url": "https://ads.example/"})
    deltas = await _drain(obs)
    assert len(deltas) == 1
    assert deltas[0].source == "bidi"
    assert deltas[0].extra["url"] == "https://a.example/x"


async def test_history_updated_is_spa_navigation():
    conn, obs = await _started()
    conn.fire("browsingContext.historyUpdated",
              {"context": "top", "url": "https://app.example/feed"})
    deltas = await _drain(obs)
    assert deltas[0].kind == "navigation"
    assert deltas[0].extra["spa"] is True


async def test_load_collapses_across_frames_and_ignores_other_tabs():
    conn, obs = await _started()
    for ctx in ("top", "frame-1", "another-tab"):
        conn.fire("browsingContext.load", {"context": ctx, "url": "x"})
    conn.fire("browsingContext.domContentLoaded", {"context": "top", "url": "x"})
    deltas = await _drain(obs)
    by_phase = {d.extra["phase"]: d for d in deltas}
    assert set(by_phase) == {"load", "DOMContentLoaded"}
    assert by_phase["load"].extra["repeat_count"] == 2


# ── log.entryAdded ──────────────────────────────────────────────────

def _log(**kw):
    return {"source": {"context": "top"}, "text": "boom 1", **kw}


async def test_console_error_and_warn_kept_log_dropped():
    conn, obs = await _started()
    conn.fire("log.entryAdded", _log(type="console", method="error", level="error"))
    conn.fire("log.entryAdded", _log(type="console", method="warn", level="warn",
                                     text="careful"))
    conn.fire("log.entryAdded", _log(type="console", method="log", level="info",
                                     text="noise"))
    deltas = await _drain(obs)
    assert sorted(d.extra["level"] for d in deltas) == ["error", "warning"]


async def test_javascript_entry_is_uncaught_error():
    conn, obs = await _started()
    conn.fire("log.entryAdded", _log(type="javascript", level="error",
                                     text="TypeError: x is not a function"))
    deltas = await _drain(obs)
    assert deltas[0].extra == {"level": "error", "uncaught": True}
    assert "TypeError" in deltas[0].value


async def test_log_entries_replayed_during_subscribe_dropped():
    # BiDi flushes buffered log entries when a subscription is created; they
    # predate the tool call. Live E2E saw a previous call's console.error
    # resurface in the next call's envelope.
    conn = FakeConn()
    conn.replay = [("log.entryAdded", _log(type="console", method="error",
                                           level="error", text="old"))]
    conn, obs = await _started(conn)
    conn.fire("log.entryAdded", _log(type="console", method="error",
                                     level="error", text="new"))
    deltas = await _drain(obs)
    assert [d.value for d in deltas] == ["new"]


async def test_firefox_internal_bidi_args_error_dropped():
    # Live E2E (Firefox 152): navigating about:blank → http fired this
    # from the DOM watcher's channel plumbing, not from the page.
    conn, obs = await _started()
    conn.fire("log.entryAdded", _log(
        type="javascript", level="error",
        text='SecurityError: Permission denied to access property "__bidi_args" '
             'on cross-origin object'))
    assert await _drain(obs) == []


async def test_log_from_other_tab_dropped():
    conn, obs = await _started()
    conn.fire("log.entryAdded", {"type": "console", "method": "error",
                                 "source": {"context": "another-tab"}, "text": "x"})
    assert await _drain(obs) == []


# ── network.responseCompleted ───────────────────────────────────────

def _response(status, destination, initiator=None, context="top",
              url="https://a.example/p"):
    return {"context": context,
            "request": {"request": "r", "url": url, "destination": destination,
                        "initiatorType": initiator},
            "response": {"status": status, "url": url}}


async def test_network_retention_matches_cdp_policy():
    conn, obs = await _started()
    conn.fire("network.responseCompleted", _response(200, "document"))
    conn.fire("network.responseCompleted", _response(404, "", "fetch",
                                                     url="https://api.example/missing"))
    conn.fire("network.responseCompleted", _response(200, "", "fetch",
                                                     url="https://api.example/ok"))
    conn.fire("network.responseCompleted", _response(404, "image",
                                                     url="https://a.example/x.png"))
    conn.fire("network.responseCompleted", _response(500, "document",
                                                     context="another-tab"))
    deltas = await _drain(obs)
    assert sorted((d.extra["type"], d.extra["status"]) for d in deltas) == [
        ("Document", 200), ("Fetch", 404)]


# ── DOM watcher channel ─────────────────────────────────────────────

async def test_dom_attr_messages_map_to_cdp_kinds():
    conn, obs = await _started()
    conn.fire("script.message", _message(obs, "attr", 3, "aria-expanded", "true"))
    conn.fire("script.message", _message(obs, "attr", 4, "value", "hello"))
    conn.fire("script.message", _message(obs, "attr", 5, "aria-label", "Submit"))
    deltas = await _drain(obs)
    by_kind = {d.kind: d for d in deltas}
    assert by_kind["state_changed"].source_id == "3"
    assert by_kind["state_changed"].value == "true"
    assert by_kind["value_changed"].value == "hello"
    assert by_kind["name_changed"].value == "Submit"


async def test_dom_dialog_added():
    conn, obs = await _started()
    conn.fire("script.message", _message(obs, "add", 9, "div", "dialog", "Save changes?"))
    deltas = await _drain(obs)
    assert deltas[0].kind == "node_added"
    assert deltas[0].role == "dialog"
    assert deltas[0].name == "Save changes?"


async def test_foreign_channel_ignored():
    conn, obs = await _started()
    conn.fire("script.message", _message(obs, "attr", 3, "aria-expanded", "true",
                                         channel="someone-else"))
    assert await _drain(obs) == []
