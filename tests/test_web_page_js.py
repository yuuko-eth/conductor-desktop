"""Routing for web_js_snippet / web_mark page scripts (tools.web._run_page_js).

A connected browser always wins; the Chrome address-bar fallback is only for
"no browser named and none reachable". Fake registry, no browser required.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conductor_desktop.bidi import BidiError
from conductor_desktop.browser import BrowserError, BrowserUnreachable, TabInfo
from conductor_desktop.cdp import CDPError
from conductor_desktop.tools import web


class FakeSession:
    def __init__(self, value=None) -> None:
        self.evaluated: list[str] = []
        self.closed = False
        self.value = value

    async def evaluate(self, expression, *, await_promise=True):
        self.evaluated.append(expression)
        return self.value

    async def close(self):
        self.closed = True


class FakeEndpoint:
    def __init__(self, value=None) -> None:
        self.session = FakeSession(value)
        self.activated: list[TabInfo] = []

    async def open(self, tab):
        return self.session

    async def activate(self, tab):
        self.activated.append(tab)


class FakeRegistry:
    def __init__(self, *, raises: Exception | None = None, value=None) -> None:
        self.raises = raises
        self.endpoint = FakeEndpoint(value)
        self.calls: list[dict] = []

    async def select(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.endpoint, TabInfo("firefox", 1, "ctx", "Page", "https://example/")


@pytest.fixture
def url_bar(monkeypatch):
    typed: list[str] = []

    async def fake_inject(js, focus_chrome):
        typed.append(js)
        return None

    monkeypatch.setattr(web, "_inject_url_bar_js", fake_inject)
    return typed


def _use(monkeypatch, registry):
    monkeypatch.setattr(web, "_registry", lambda: registry)
    return registry


async def test_default_browser_runs_through_connection(monkeypatch, url_bar):
    reg = _use(monkeypatch, FakeRegistry())
    err, where, _ = await web._run_page_js("1", browser=None, focus_chrome=True)
    assert (err, where) == (None, "[firefox:1]")
    assert reg.endpoint.session.evaluated == ["1"] and reg.endpoint.session.closed
    assert url_bar == []


async def test_no_reachable_browser_falls_back_to_address_bar(monkeypatch, url_bar):
    _use(monkeypatch, FakeRegistry(raises=BrowserUnreachable("none reachable")))
    err, where, _ = await web._run_page_js("1", browser=None, focus_chrome=True)
    assert err is None and "address bar" in where
    assert url_bar == ["1"]


@pytest.mark.parametrize("exc", [CDPError("cannot reach Chrome DevTools"),
                                 BidiError("Cannot reach WebDriver BiDi")])
async def test_unanswering_default_falls_back(monkeypatch, url_bar, exc):
    _use(monkeypatch, FakeRegistry(raises=exc))
    err, _, _ = await web._run_page_js("1", browser=None, focus_chrome=True)
    assert err is None and url_bar == ["1"]


async def test_named_browser_down_is_an_error_not_a_fallback(monkeypatch, url_bar):
    _use(monkeypatch, FakeRegistry(raises=BidiError("Cannot reach WebDriver BiDi")))
    err, _, _ = await web._run_page_js("1", browser="firefox", focus_chrome=True)
    assert "Cannot reach" in err
    assert url_bar == []


async def test_ambiguous_default_is_an_error_not_a_fallback(monkeypatch, url_bar):
    _use(monkeypatch, FakeRegistry(raises=BrowserError("several browsers are reachable")))
    err, _, _ = await web._run_page_js("1", browser=None, focus_chrome=True)
    assert "several browsers" in err
    assert url_bar == []


@pytest.fixture
def tools(monkeypatch, install_state):
    async def no_observation(*_a, **_kw):
        return []

    monkeypatch.setattr(web, "post_action_observation", no_observation)
    monkeypatch.setattr("conductor_desktop.runtime.replies.post_action_observation", no_observation)
    install_state(config=SimpleNamespace(cdp_default_tab_filter="dashboard"))


@pytest.mark.parametrize("found, message", [
    ("missing", "element not found"),
    ("climbed", "walked past the document root"),
])
async def test_web_mark_fails_when_nothing_is_outlined(monkeypatch, tools, url_bar,
                                                       found, message):
    _use(monkeypatch, FakeRegistry(value=found))
    out = await web.web_mark(selector="#nope", climb=2, wait_after=0)
    assert out.isError and message in out.content[0].text


async def test_web_mark_reports_a_found_element(monkeypatch, tools, url_bar):
    _use(monkeypatch, FakeRegistry(value="ok"))
    out = await web.web_mark(selector="#chart", wait_after=0)
    assert not out.isError
    assert "outlined '#chart'" in out.content[0].text
    assert "unverified" not in out.content[0].text


async def test_web_mark_through_the_address_bar_is_unverified(monkeypatch, tools, url_bar):
    _use(monkeypatch, FakeRegistry(raises=BrowserUnreachable("none reachable")))
    out = await web.web_mark(selector="#chart", wait_after=0)
    assert not out.isError and "unverified" in out.content[0].text


async def test_activate_tab_defaults_to_the_configured_filter(monkeypatch, tools):
    reg = _use(monkeypatch, FakeRegistry())
    out = await web.activate_tab()
    assert not out.isError
    assert reg.calls[0]["tab"] == "dashboard"
    assert len(reg.endpoint.activated) == 1
