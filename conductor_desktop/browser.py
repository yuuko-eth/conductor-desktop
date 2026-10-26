"""Browser registry for the web_* tools.

One ``browser=`` selector hides the transport, the way ``screen=`` and
``device_id`` do for capture: each configured slug maps to an endpoint that
speaks CDP (Chromium launched with ``--remote-debugging-port``) or WebDriver
BiDi (Firefox launched with ``--remote-debugging-port``). Tool code only sees
``TabInfo`` and a tab session with ``evaluate`` / ``wait_for_*`` /
``capture_screenshot``.

``CONDUCTOR_DESKTOP_BROWSERS`` is comma-separated ``slug=scheme://host:port``::

    chrome=cdp://127.0.0.1:9222,firefox=bidi://127.0.0.1:9223

Unset keeps the pre-registry behavior: one ``chrome`` endpoint at
``CONDUCTOR_DESKTOP_CDP_HOST:CONDUCTOR_DESKTOP_CDP_PORT``.

Tabs get short per-browser handles (``firefox:3``) that stay stable for the
life of the server process, so an agent can pin browser and tab in one string.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import WebSocketException

from .bidi import BidiConnection, BidiError, BidiTab
from .cdp import CDPClient, CDPError
from .core.perception.deltas.bidi_observer import BidiObserver
from .core.perception.deltas.cdp_observer import CDPObserver


class BrowserError(RuntimeError):
    """Browser targeting failed — bad config, unknown slug, stale tab handle."""


class BrowserUnreachable(BrowserError):
    """No browser was named and none of the configured ones is reachable."""


# Every error a web_* tool should report as a tool error rather than crash on.
BROWSER_ERRORS: tuple[type[Exception], ...] = (BrowserError, CDPError, BidiError)

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_HANDLE_RE = re.compile(r"([a-z0-9][a-z0-9_-]*):(\d+)")


@dataclass
class TabInfo:
    browser: str
    handle: int
    id: str
    title: str
    url: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def ref(self) -> str:
        return f"{self.browser}:{self.handle}"


class TabSession(Protocol):
    """What web_* tools need from an attached tab, whatever the transport."""

    async def evaluate(self, expression: str, *, await_promise: bool = True) -> Any: ...
    async def wait_for_selector(self, selector: str, *, timeout_ms: int = 5000) -> bool: ...
    async def wait_for_network_idle(self, *, quiet_ms: int = 500,
                                    timeout_ms: int = 5000) -> bool: ...
    async def capture_screenshot(self, *, clip: dict | None = None,
                                 beyond_viewport: bool = True) -> bytes: ...
    async def close(self) -> None: ...


def select_target(items: list, *, tab: str | None, match_url: str | None,
                  match_title: str | None):
    """Pick one tab: `match_url`/`match_title` (AND) → `tab` merged-substring
    → first item. Accepts TabInfo objects or raw CDP /json/list dicts.
    Returns None only when an explicit selector matched nothing.
    """
    def fields(t) -> tuple[str, str]:
        if isinstance(t, dict):
            return (t.get("url") or "").lower(), (t.get("title") or "").lower()
        return (t.url or "").lower(), (t.title or "").lower()

    url_needle = (match_url or "").lower().strip()
    title_needle = (match_title or "").lower().strip()
    if url_needle or title_needle:
        for t in items:
            url, title = fields(t)
            if url_needle and url_needle not in url:
                continue
            if title_needle and title_needle not in title:
                continue
            return t
        return None
    legacy = (tab or "").lower().strip()
    if legacy:
        for t in items:
            url, title = fields(t)
            if legacy in f"{url} {title}":
                return t
        return None
    return items[0] if items else None


class BrowserEndpoint(ABC):
    """One configured browser. Subclasses implement a transport."""

    kind = ""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        # target id → handle number. Never pruned, so a closed tab's number
        # is never reassigned to a different tab.
        self._handles: dict[str, int] = {}

    @property
    @abstractmethod
    def address(self) -> str: ...

    def _tab(self, target_id: str, title: str, url: str, raw: dict) -> TabInfo:
        handle = self._handles.get(target_id)
        if handle is None:
            handle = len(self._handles) + 1
            self._handles[target_id] = handle
        return TabInfo(self.slug, handle, target_id, title, url, raw)

    @abstractmethod
    async def list_tabs(self) -> list[TabInfo]:
        """Page tabs, default target first."""

    @abstractmethod
    async def activate(self, tab: TabInfo) -> None: ...

    @abstractmethod
    async def open(self, tab: TabInfo) -> TabSession: ...

    @abstractmethod
    def observer(self, loop: asyncio.AbstractEventLoop, session: TabSession):
        """The delta observer for an open tab session."""

    @abstractmethod
    async def probe(self) -> str:
        """Browser name/version; raises if unreachable."""

    @abstractmethod
    async def close(self) -> None:
        """Release long-lived connections."""


class CdpEndpoint(BrowserEndpoint):
    kind = "cdp"

    def __init__(self, slug: str, host: str, port: int) -> None:
        super().__init__(slug)
        self.host = host
        self.port = port
        self._http: httpx.AsyncClient | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def _unreachable(self, e: Exception) -> CDPError:
        return CDPError(
            f"cannot reach Chrome DevTools at {self.address} — start Chrome with "
            f"`--remote-debugging-port={self.port}` and "
            f"`--user-data-dir=/tmp/cdp-chrome`. ({e})"
        )

    async def _get(self, path: str, timeout: float = 5.0) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient()
        try:
            r = await self._http.get(f"http://{self.address}{path}", timeout=timeout)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            raise self._unreachable(e) from e

    async def list_tabs(self) -> list[TabInfo]:
        # /json/list is most-recently-used first, so the default is the
        # tab the user last touched.
        targets = (await self._get("/json/list")).json()
        return [
            self._tab(t.get("id", ""), (t.get("title") or "").strip(),
                      t.get("url") or "", t)
            for t in targets if t.get("type") == "page"
        ]

    async def activate(self, tab: TabInfo) -> None:
        try:
            await self._get(f"/json/activate/{tab.id}")
        except CDPError as e:
            raise CDPError(f"activate failed for target {tab.id}: {e}") from e

    async def open(self, tab: TabInfo) -> CDPClient:
        raw = dict(tab.raw)
        # Chrome omits webSocketDebuggerUrl while a DevTools frontend is
        # attached to the page; the path is predictable, so rebuild it.
        raw.setdefault("webSocketDebuggerUrl",
                       f"ws://{self.address}/devtools/page/{tab.id}")
        try:
            return await CDPClient.connect(raw)
        except (OSError, WebSocketException) as e:
            raise CDPError(f"cannot attach to tab [{tab.ref}]: {e}") from e

    def observer(self, loop, session):
        return CDPObserver(loop, session)

    async def probe(self) -> str:
        return (await self._get("/json/version", timeout=1.5)).json().get("Browser", "?")

    async def close(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()


class BidiEndpoint(BrowserEndpoint):
    kind = "bidi"

    def __init__(self, slug: str, ws_url: str) -> None:
        super().__init__(slug)
        self.conn = BidiConnection(ws_url)

    @property
    def address(self) -> str:
        return self.conn.ws_url

    async def list_tabs(self) -> list[TabInfo]:
        res = await self.conn.send("browsingContext.getTree", {"maxDepth": 0})
        contexts = res.get("contexts") or []

        async def details(ctx: dict) -> tuple[str, bool]:
            # getTree has no titles. A short timeout keeps one unresponsive
            # (e.g. unloaded background) tab from stalling the listing, and
            # reading a title is no reason to grant every tab user activation.
            try:
                title, visibility = await BidiTab(self.conn, ctx["context"]).evaluate(
                    "[document.title, document.visibilityState]",
                    await_promise=False, user_activation=False, timeout=2.0)
                return title or "", visibility == "visible"
            except (BidiError, TypeError, ValueError):
                return "", False

        found = await asyncio.gather(*(details(c) for c in contexts))
        tabs = [(visible, self._tab(c["context"], title.strip(), c.get("url") or "", c))
                for c, (title, visible) in zip(contexts, found)]
        # Foreground tabs first — BiDi lists in tab-strip order, and the
        # visible tab is the sensible default (CDP's MRU-first analogue).
        tabs.sort(key=lambda pair: not pair[0])
        return [tab for _, tab in tabs]

    async def activate(self, tab: TabInfo) -> None:
        await self.conn.send("browsingContext.activate", {"context": tab.id})

    async def open(self, tab: TabInfo) -> BidiTab:
        await self.conn.ensure()
        return BidiTab(self.conn, tab.id, tab.raw)

    def observer(self, loop, session):
        return BidiObserver(loop, session)

    async def probe(self) -> str:
        await self.conn.ensure()
        caps = self.conn.capabilities
        return f"{caps.get('browserName', '?')}/{caps.get('browserVersion', '?')}"

    async def close(self) -> None:
        # session.end matters: Firefox keeps the session (and refuses new
        # ones) after the socket closes without it.
        await self.conn.close()


def parse_browsers(spec: str, *, cdp_host: str, cdp_port: int) -> dict[str, BrowserEndpoint]:
    """Parse CONDUCTOR_DESKTOP_BROWSERS. Empty → one implicit `chrome` CDP endpoint."""
    if not spec.strip():
        return {"chrome": CdpEndpoint("chrome", cdp_host, cdp_port)}
    out: dict[str, BrowserEndpoint] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        slug, sep, url = entry.partition("=")
        slug = slug.strip().lower()
        if not sep or not _SLUG_RE.fullmatch(slug):
            raise BrowserError(
                f"CONDUCTOR_DESKTOP_BROWSERS entry {entry!r}: expected slug=scheme://host:port"
            )
        if slug in out:
            raise BrowserError(f"CONDUCTOR_DESKTOP_BROWSERS: duplicate slug {slug!r}")
        try:
            parts = urlsplit(url.strip())
            port = parts.port
        except ValueError as e:
            raise BrowserError(f"CONDUCTOR_DESKTOP_BROWSERS entry {entry!r}: {e}") from e
        host = parts.hostname or "127.0.0.1"
        scheme = parts.scheme.lower()
        if scheme == "cdp":
            out[slug] = CdpEndpoint(slug, host, port or 9222)
        elif scheme == "bidi":
            path = parts.path or "/session"
            out[slug] = BidiEndpoint(slug, f"ws://{host}:{port or 9222}{path}")
        else:
            raise BrowserError(
                f"CONDUCTOR_DESKTOP_BROWSERS entry {entry!r}: scheme must be cdp or bidi"
            )
    if not out:
        raise BrowserError("CONDUCTOR_DESKTOP_BROWSERS is set but lists no browsers")
    return out


class BrowserRegistry:
    """Slug → endpoint, plus the `browser=` / `tab=` resolution rules."""

    def __init__(self, endpoints: dict[str, BrowserEndpoint], default: str = "") -> None:
        self._endpoints = endpoints
        self.default = default.strip().lower()

    @classmethod
    def from_config(cls, cfg) -> BrowserRegistry:
        return cls(parse_browsers(cfg.browsers, cdp_host=cfg.cdp_host,
                                  cdp_port=cfg.cdp_port),
                   cfg.default_browser)

    def endpoints(self) -> list[BrowserEndpoint]:
        return list(self._endpoints.values())

    async def close(self) -> None:
        await asyncio.gather(*(ep.close() for ep in self._endpoints.values()),
                             return_exceptions=True)

    def _configured(self) -> str:
        return ", ".join(f"{slug} ({ep.kind})" for slug, ep in self._endpoints.items())

    def get(self, slug: str) -> BrowserEndpoint:
        ep = self._endpoints.get(slug.strip().lower())
        if ep is None:
            raise BrowserError(f"unknown browser {slug!r}; configured: {self._configured()}")
        return ep

    def parse_handle(self, tab: str | None) -> tuple[str, int] | None:
        """`firefox:3` → ("firefox", 3). Only for configured slugs, so a
        substring like `localhost:3000` stays a substring."""
        if not tab:
            return None
        m = _HANDLE_RE.fullmatch(tab.strip().lower())
        if m and m.group(1) in self._endpoints:
            return m.group(1), int(m.group(2))
        return None

    async def resolve(self, browser: str | None, tab: str | None = None) -> BrowserEndpoint:
        """Pick the endpoint: `browser` → tab handle's slug → configured
        default → the only configured browser → the only reachable one."""
        handle = self.parse_handle(tab)
        if browser:
            ep = self.get(browser)
            if handle and handle[0] != ep.slug:
                raise BrowserError(
                    f"tab={tab!r} belongs to {handle[0]!r} but browser={browser!r}"
                )
            return ep
        if handle:
            return self._endpoints[handle[0]]
        if self.default:
            return self.get(self.default)
        if len(self._endpoints) == 1:
            return next(iter(self._endpoints.values()))
        eps = self.endpoints()
        probes = await asyncio.gather(*(ep.probe() for ep in eps), return_exceptions=True)
        up = [ep for ep, p in zip(eps, probes) if not isinstance(p, BaseException)]
        if len(up) == 1:
            return up[0]
        if not up:
            raise BrowserUnreachable(
                f"no configured browser is reachable ({self._configured()})"
            )
        raise BrowserError(
            f"several browsers are reachable ({', '.join(ep.slug for ep in up)}) — "
            f"pass browser=, a tab handle like tab='{up[0].slug}:1', "
            f"or set CONDUCTOR_DESKTOP_DEFAULT_BROWSER"
        )

    async def select(self, *, browser: str | None, tab: str | None,
                     match_url: str | None = None,
                     match_title: str | None = None) -> tuple[BrowserEndpoint, TabInfo]:
        ep = await self.resolve(browser, tab)
        tabs = await ep.list_tabs()
        if not tabs:
            raise BrowserError(f"No page targets in {ep.slug} ({ep.address})")
        handle = self.parse_handle(tab)
        if handle and not (match_url or match_title):
            for t in tabs:
                if t.handle == handle[1]:
                    return ep, t
            raise BrowserError(
                f"no open tab {ep.slug}:{handle[1]} — it may have closed; "
                f"run web_list_tabs for current handles"
            )
        target = select_target(tabs, tab=None if handle else tab,
                               match_url=match_url, match_title=match_title)
        if target is None:
            if match_url or match_title:
                criteria = " AND ".join(
                    c for c in (match_url and f"match_url={match_url!r}",
                                match_title and f"match_title={match_title!r}") if c)
            else:
                criteria = f"tab={tab!r}"
            avail = ", ".join(f"[{t.ref}] (title={t.title!r}, url={t.url!r})"
                              for t in tabs[:5])
            raise BrowserError(
                f"No page target matched {criteria} in {ep.slug}. "
                f"Available (first 5): {avail}"
            )
        return ep, target
