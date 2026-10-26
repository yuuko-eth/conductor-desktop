"""Tests for browser.py — config parsing, browser resolution, tab handles.

Fake endpoints stand in for CDP / BiDi; no browser required.
"""

from __future__ import annotations

import pytest

from conductor_desktop.browser import (
    BidiEndpoint,
    BrowserEndpoint,
    BrowserError,
    BrowserRegistry,
    CdpEndpoint,
    TabInfo,
    parse_browsers,
    select_target,
)


class FakeEndpoint(BrowserEndpoint):
    kind = "fake"

    def __init__(self, slug: str, tabs=(), reachable: bool = True) -> None:
        super().__init__(slug)
        self.targets = list(tabs)  # (id, title, url)
        self.reachable = reachable
        self.closed = False

    async def close(self) -> None:
        if not self.reachable:
            raise BrowserError("down")
        self.closed = True

    @property
    def address(self) -> str:
        return f"fake://{self.slug}"

    async def list_tabs(self):
        return [self._tab(i, title, url, {}) for i, title, url in self.targets]

    async def probe(self) -> str:
        if not self.reachable:
            raise BrowserError("down")
        return "Fake/1"

    # Not used by the registry's resolution rules.
    async def activate(self, tab):
        raise NotImplementedError

    async def open(self, tab):
        raise NotImplementedError

    def observer(self, loop, session):
        raise NotImplementedError


def _reg(*endpoints, default: str = "") -> BrowserRegistry:
    return BrowserRegistry({ep.slug: ep for ep in endpoints}, default)


TWO_TABS = [("ctx-a", "Grafana", "https://grafana.example/d/1"),
            ("ctx-b", "Inbox", "https://mail.example/")]


# ── parse_browsers ──────────────────────────────────────────────────

def test_parse_empty_is_single_chrome_cdp():
    eps = parse_browsers("", cdp_host="10.0.0.5", cdp_port=9333)
    assert list(eps) == ["chrome"]
    assert isinstance(eps["chrome"], CdpEndpoint)
    assert eps["chrome"].address == "10.0.0.5:9333"


def test_parse_mixed_transports():
    eps = parse_browsers(" chrome=cdp://127.0.0.1:9222 , Firefox=bidi://127.0.0.1:9223 ",
                         cdp_host="x", cdp_port=1)
    assert isinstance(eps["chrome"], CdpEndpoint)
    assert isinstance(eps["firefox"], BidiEndpoint)
    assert eps["firefox"].address == "ws://127.0.0.1:9223/session"


def test_parse_bidi_keeps_explicit_path():
    eps = parse_browsers("ff=bidi://host:4444/custom", cdp_host="x", cdp_port=1)
    assert eps["ff"].address == "ws://host:4444/custom"


@pytest.mark.parametrize("spec", [
    "chrome",                              # no =
    "chrome=http://x:1",                   # unsupported scheme
    "Bad Slug=cdp://x:1",                  # slug charset
    "a=cdp://x:1,a=cdp://y:2",             # duplicate
    "a=cdp://x:notaport",                  # bad port
    " , ",                                 # nothing listed
])
def test_parse_rejects_bad_specs(spec):
    with pytest.raises(BrowserError):
        parse_browsers(spec, cdp_host="x", cdp_port=1)


# ── resolve ─────────────────────────────────────────────────────────

async def test_single_browser_is_default_without_probing():
    reg = _reg(FakeEndpoint("chrome", reachable=False))
    assert (await reg.resolve(None)).slug == "chrome"


async def test_configured_default_wins():
    reg = _reg(FakeEndpoint("chrome"), FakeEndpoint("firefox", reachable=False),
               default="Firefox")
    assert (await reg.resolve(None)).slug == "firefox"


async def test_unique_reachable_browser_is_default():
    reg = _reg(FakeEndpoint("chrome", reachable=False), FakeEndpoint("firefox"))
    assert (await reg.resolve(None)).slug == "firefox"


async def test_several_reachable_is_ambiguous():
    reg = _reg(FakeEndpoint("chrome"), FakeEndpoint("firefox"))
    with pytest.raises(BrowserError, match="several browsers"):
        await reg.resolve(None)


async def test_none_reachable_errors():
    reg = _reg(FakeEndpoint("chrome", reachable=False),
               FakeEndpoint("firefox", reachable=False))
    with pytest.raises(BrowserError, match="no configured browser is reachable"):
        await reg.resolve(None)


async def test_unknown_slug_lists_configured():
    reg = _reg(FakeEndpoint("chrome"))
    with pytest.raises(BrowserError, match=r"chrome \(fake\)"):
        await reg.resolve("safari")


async def test_tab_handle_pins_browser():
    reg = _reg(FakeEndpoint("chrome"), FakeEndpoint("firefox"))
    assert (await reg.resolve(None, "firefox:2")).slug == "firefox"


async def test_tab_handle_conflicting_with_browser_errors():
    reg = _reg(FakeEndpoint("chrome"), FakeEndpoint("firefox"))
    with pytest.raises(BrowserError, match="belongs to"):
        await reg.resolve("chrome", "firefox:2")


async def test_host_port_substring_is_not_a_handle():
    reg = _reg(FakeEndpoint("chrome", tabs=[("a", "Dev", "http://localhost:3000/")]))
    _, tab = await reg.select(browser=None, tab="localhost:3000")
    assert tab.id == "a"


# ── handles & select ────────────────────────────────────────────────

async def test_handles_are_stable_across_listings():
    ep = FakeEndpoint("firefox", tabs=TWO_TABS)
    assert {t.id: t.ref for t in await ep.list_tabs()} == {
        "ctx-a": "firefox:1", "ctx-b": "firefox:2"}
    # ctx-a closes, ctx-c opens: survivors keep their number, closed
    # numbers are never reused.
    ep.targets = [("ctx-c", "New", "https://new/"), TWO_TABS[1]]
    assert {t.id: t.ref for t in await ep.list_tabs()} == {
        "ctx-c": "firefox:3", "ctx-b": "firefox:2"}


async def test_select_by_handle():
    ep = FakeEndpoint("firefox", tabs=TWO_TABS)
    await ep.list_tabs()
    _, tab = await _reg(ep).select(browser=None, tab="firefox:2")
    assert tab.id == "ctx-b"


async def test_select_closed_handle_errors():
    ep = FakeEndpoint("firefox", tabs=TWO_TABS)
    with pytest.raises(BrowserError, match="no open tab firefox:9"):
        await _reg(ep).select(browser=None, tab="firefox:9")


async def test_select_match_url_miss_lists_handles():
    ep = FakeEndpoint("firefox", tabs=TWO_TABS)
    with pytest.raises(BrowserError, match=r"match_url='nope'.*\[firefox:1\]"):
        await _reg(ep).select(browser=None, tab=None, match_url="nope")


async def test_select_defaults_to_first_tab():
    ep = FakeEndpoint("firefox", tabs=TWO_TABS)
    _, tab = await _reg(ep).select(browser=None, tab=None)
    assert tab.id == "ctx-a"


async def test_select_no_tabs_errors():
    with pytest.raises(BrowserError, match="No page targets"):
        await _reg(FakeEndpoint("firefox")).select(browser=None, tab=None)


async def test_registry_close_closes_every_endpoint_despite_failures():
    down, up = FakeEndpoint("chrome", reachable=False), FakeEndpoint("firefox")
    await _reg(down, up).close()
    assert up.closed


def test_select_target_accepts_tabinfo():
    tabs = [TabInfo("firefox", 1, "a", "Grafana", "https://grafana/"),
            TabInfo("firefox", 2, "b", "Inbox", "https://mail/")]
    assert select_target(tabs, tab="inbox", match_url=None, match_title=None).id == "b"
    assert select_target(tabs, tab=None, match_url="grafana", match_title=None).id == "a"
