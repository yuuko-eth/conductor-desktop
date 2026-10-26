"""Tests for perception.deltas.cdp_observer.

Uses a fake CDPClient that records handlers and lets the test replay
synthetic CDP event payloads. No Chrome required.
"""

from __future__ import annotations

import asyncio

from conductor_desktop.core.perception.deltas.cdp_observer import (
    KEEP_ATTRS,
    CDPObserver,
    _attr_dict,
)


class FakeCDPClient:
    """Drop-in stand-in for CDPClient — only `send`, `on`, `off` used."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self._handlers: dict[str, list] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        return {}

    def on(self, method: str, handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler) -> None:
        h = self._handlers.get(method)
        if h:
            try:
                h.remove(handler)
            except ValueError:
                pass

    def fire(self, method: str, params: dict) -> None:
        """Synthesize an event — call all subscribed handlers."""
        for h in self._handlers.get(method, ()):
            h(params)


# ── helpers ─────────────────────────────────────────────────────────

async def _drain_after_fire(obs: CDPObserver) -> list:
    # _emit schedules via call_soon_threadsafe; let the loop process it
    # before draining.
    await asyncio.sleep(0)
    deltas, _, _ = obs.drain_and_filter()
    return deltas


# ── lifecycle ───────────────────────────────────────────────────────

async def test_start_enables_required_domains():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    methods = [m for m, _ in client.sent]
    assert "Page.enable" in methods
    assert "DOM.enable" in methods
    assert "Runtime.enable" in methods
    assert "Network.enable" in methods


async def test_stop_unregisters_handlers():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    assert client._handlers["Page.frameNavigated"]
    await obs.stop()
    assert client._handlers["Page.frameNavigated"] == []


async def test_stop_is_idempotent():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    await obs.stop()
    await obs.stop()  # must not raise


# ── Page.frameNavigated ─────────────────────────────────────────────

async def test_frame_navigated_main_frame_emits_navigation():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Page.frameNavigated", {
        "frame": {"id": "frame-1", "url": "https://example.com/x", "parentId": None},
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "navigation"
    assert deltas[0].extra["url"] == "https://example.com/x"
    # source_id is the URL (not frame_id) so duplicate fires for the same
    # transition collapse — see test_navigation_same_url_collapses.
    assert deltas[0].source_id == "https://example.com/x"


async def test_frame_navigated_subframe_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Page.frameNavigated", {
        "frame": {"id": "ad-iframe", "url": "https://ads.example/x",
                  "parentId": "main-frame"},
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


# ── Page.lifecycleEvent ─────────────────────────────────────────────

async def test_lifecycle_load_emits_window_state():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Page.lifecycleEvent", {"name": "load", "frameId": "f1"})
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "window_state_changed"
    assert deltas[0].extra["phase"] == "load"


async def test_lifecycle_perf_phases_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for phase in ("init", "firstPaint", "networkIdle", "firstContentfulPaint"):
        client.fire("Page.lifecycleEvent", {"name": phase, "frameId": "f1"})
    deltas = await _drain_after_fire(obs)
    assert deltas == []


# ── DOM.attributeModified ───────────────────────────────────────────

async def test_aria_invalid_emits_state_changed():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.attributeModified", {
        "nodeId": 42, "name": "aria-invalid", "value": "true",
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "state_changed"
    assert deltas[0].source_id == "42"
    assert deltas[0].value == "true"


async def test_value_attr_emits_value_changed():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.attributeModified", {
        "nodeId": 99, "name": "value", "value": "hello",
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "value_changed"
    assert deltas[0].value == "hello"


async def test_aria_label_emits_name_changed():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.attributeModified", {
        "nodeId": 7, "name": "aria-label", "value": "Submit",
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "name_changed"
    assert deltas[0].value == "Submit"


async def test_uninteresting_attr_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    # The firehose: class, style, data-*, transform, etc.
    for attr in ("class", "style", "data-key", "data-state", "transform",
                 "id", "tabindex"):
        assert attr not in KEEP_ATTRS
        client.fire("DOM.attributeModified", {
            "nodeId": 1, "name": attr, "value": "anything",
        })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


async def test_repeated_value_changes_coalesce_to_one():
    # The Sheets-typing-storm scenario: many value_changed on the same
    # input collapse via Layer 2 to one delta with repeat_count.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for v in ["h", "he", "hel", "hell", "hello"]:
        client.fire("DOM.attributeModified", {
            "nodeId": 5, "name": "value", "value": v,
        })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].value == "hello"
    assert deltas[0].extra["repeat_count"] == 5


# ── DOM.childNodeInserted ───────────────────────────────────────────

async def test_dialog_node_inserted_emits():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.childNodeInserted", {
        "node": {
            "nodeId": 100, "nodeType": 1, "nodeName": "DIV",
            "attributes": ["role", "dialog", "aria-label", "Save changes?"],
        },
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "node_added"
    assert deltas[0].role == "dialog"
    assert deltas[0].name == "Save changes?"


async def test_plain_div_insertion_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.childNodeInserted", {
        "node": {"nodeId": 50, "nodeType": 1, "nodeName": "DIV", "attributes": []},
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


async def test_text_node_insertion_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("DOM.childNodeInserted", {
        "node": {"nodeId": 51, "nodeType": 3, "nodeName": "#text"},
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


# ── Runtime.consoleAPICalled & exceptionThrown ─────────────────────

async def test_console_error_kept():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Runtime.consoleAPICalled", {
        "type": "error",
        "args": [{"type": "string", "value": "Failed to load resource"}],
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "console"
    assert deltas[0].extra["level"] == "error"
    assert "Failed to load" in (deltas[0].value or "")


async def test_console_log_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Runtime.consoleAPICalled", {
        "type": "log",
        "args": [{"type": "string", "value": "noise"}],
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


async def test_uncaught_exception_promoted_to_error():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Runtime.exceptionThrown", {
        "exceptionDetails": {
            "exception": {"description": "TypeError: x is not a function"},
        },
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "console"
    assert deltas[0].extra["level"] == "error"
    assert deltas[0].extra["uncaught"] is True


# ── Network.responseReceived ────────────────────────────────────────

async def test_network_document_response_kept():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Network.responseReceived", {
        "type": "Document",
        "response": {"status": 200, "url": "https://a.com/page"},
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].kind == "network"
    assert deltas[0].extra["status"] == 200


async def test_network_xhr_4xx_kept():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Network.responseReceived", {
        "type": "XHR",
        "response": {"status": 404, "url": "https://api.example/missing"},
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].extra["status"] == 404


async def test_network_xhr_200_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Network.responseReceived", {
        "type": "XHR",
        "response": {"status": 200, "url": "https://api.example/ok"},
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


async def test_network_retry_storm_coalesces():
    # Six near-identical signed URLs (different query params, same path)
    # → should collapse to one delta with repeat_count=6 thanks to the
    # source_id = host+path coalesce key. This is the videoplayback
    # storm from the live YouTube smoke (2026-05-01).
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    base = "https://rr2.googlevideo.com/videoplayback"
    for i in range(6):
        client.fire("Network.responseReceived", {
            "type": "XHR",
            "response": {"status": 403, "url": f"{base}?sig={i}&exp={i*100}"},
        })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].extra["status"] == 403
    assert deltas[0].extra["repeat_count"] == 6


async def test_network_different_paths_dont_collapse():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for path in ("/api/users", "/api/posts", "/api/comments"):
        client.fire("Network.responseReceived", {
            "type": "XHR",
            "response": {"status": 500, "url": f"https://api.example.com{path}"},
        })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 3


async def test_network_different_status_dont_collapse():
    # Same URL but 200 vs 404 are different signals — should not coalesce.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for status in (200, 404):
        client.fire("Network.responseReceived", {
            "type": "Document",
            "response": {"status": status, "url": "https://a.com/p"},
        })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 2


async def test_lifecycle_multi_phase_coalesces_per_phase():
    # 3× DOMContentLoaded + 3× load (simulating iframes/workers in YT).
    # Should collapse to 2 deltas (one per phase) with repeat_count=3 each.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for _ in range(3):
        client.fire("Page.lifecycleEvent", {"name": "DOMContentLoaded", "frameId": "f1"})
        client.fire("Page.lifecycleEvent", {"name": "load", "frameId": "f1"})
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 2
    phases = sorted(d.extra["phase"] for d in deltas)
    assert phases == ["DOMContentLoaded", "load"]
    for d in deltas:
        assert d.extra["repeat_count"] == 3


async def test_lifecycle_collapses_across_frames():
    # Different frames firing the same phase collapse — YouTube fires
    # DOMContentLoaded/load from the main page + ad iframes + workers
    # in one transition. Brain doesn't care which frame; "the surface
    # is settling" is the signal. Live smoke (2026-05-01) showed 6-line
    # storms; this test enforces the 2-line collapsed shape.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Page.lifecycleEvent", {"name": "load", "frameId": "main"})
    client.fire("Page.lifecycleEvent", {"name": "load", "frameId": "iframe-ad"})
    client.fire("Page.lifecycleEvent", {"name": "load", "frameId": "iframe-embed"})
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].extra["phase"] == "load"
    assert deltas[0].extra["repeat_count"] == 3


async def test_navigation_same_url_collapses():
    # Page.frameNavigated and Page.navigatedWithinDocument fire back-to-
    # back for the same redirect transition (live smoke saw this twice
    # at t=10s on YouTube → accounts.google.com signin). With navigation
    # in LAST_WINS_KINDS keyed by URL, duplicates collapse.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    url = "https://accounts.google.com/v3/signin/identifier?continue=x"
    client.fire("Page.frameNavigated", {
        "frame": {"id": "main", "url": url, "parentId": None},
    })
    client.fire("Page.navigatedWithinDocument", {
        "frameId": "main", "url": url,
    })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 1
    assert deltas[0].extra["repeat_count"] == 2


async def test_navigation_different_urls_dont_collapse():
    # Brain still sees the journey when the user actually navigates.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    for path in ("/", "/account", "/feed/subscriptions"):
        client.fire("Page.navigatedWithinDocument", {
            "frameId": "main", "url": f"https://www.youtube.com{path}",
        })
    deltas = await _drain_after_fire(obs)
    assert len(deltas) == 3
    urls = [d.extra["url"] for d in deltas]
    assert urls == [
        "https://www.youtube.com/",
        "https://www.youtube.com/account",
        "https://www.youtube.com/feed/subscriptions",
    ]


async def test_network_image_dropped():
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    client.fire("Network.responseReceived", {
        "type": "Image",
        "response": {"status": 200, "url": "https://a.com/x.png"},
    })
    deltas = await _drain_after_fire(obs)
    assert deltas == []


# ── handler exception isolation ────────────────────────────────────

async def test_buggy_handler_does_not_crash_observer():
    # The cdp.py reader catches handler exceptions and warns. The observer
    # itself shouldn't deadlock or stop processing subsequent events.
    # We can't easily simulate the cdp.py reader in a unit test, but we
    # *can* verify the handler raising doesn't propagate through fire().
    # (That's a property of the FakeCDPClient — production has cdp.py's
    # try/except.) So instead, verify that valid events after a failure
    # still flow.
    loop = asyncio.get_running_loop()
    client = FakeCDPClient()
    obs = CDPObserver(loop, client)
    await obs.start()
    # Fire something benign first.
    client.fire("Page.frameNavigated", {
        "frame": {"id": "f", "url": "x", "parentId": None},
    })
    # Fire something that has a malformed payload — the handler should
    # not raise (it tolerates missing fields).
    client.fire("Page.frameNavigated", {})  # no frame key
    # Then another benign event.
    client.fire("Page.frameNavigated", {
        "frame": {"id": "g", "url": "y", "parentId": None},
    })
    deltas = await _drain_after_fire(obs)
    # Both well-formed events landed; the malformed one was tolerated.
    urls = [d.extra["url"] for d in deltas]
    assert urls == ["x", "y"]


# ── _attr_dict helper ──────────────────────────────────────────────

def test_attr_dict_pairs_flat_list():
    out = _attr_dict(["role", "dialog", "aria-label", "Save"])
    assert out == {"role": "dialog", "aria-label": "Save"}


def test_attr_dict_handles_odd_length():
    # Trailing key with no value — drop it cleanly, don't raise.
    out = _attr_dict(["role", "dialog", "orphan"])
    assert out == {"role": "dialog"}


def test_attr_dict_empty():
    assert _attr_dict([]) == {}
