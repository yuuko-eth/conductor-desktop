"""Web tools: drive browser tabs over CDP or WebDriver BiDi.

The transport hides behind one ``browser=`` selector (see ``browser.py``):
- Driver-backed (web_list_tabs, activate_tab, web_eval, web_crop) — CDP for
  Chromium, WebDriver BiDi for Firefox; both need the browser launched with
  ``--remote-debugging-port``.
- Page scripts (web_js_snippet, web_mark) — through a browser connection
  when one is reachable, else typed as a ``javascript:`` URL into the
  frontmost Chrome (no flags needed).

Web tools default to ``include_desktop=False`` so unrelated host AX/AT-SPI
events stay out of the envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Annotated

from mcp.types import TextContent
from pydantic import Field

from ..bidi import BidiError
from ..browser import BROWSER_ERRORS, BrowserRegistry, BrowserUnreachable, TabInfo
from ..browser import select_target as _select_target  # noqa: F401  (tests import it here)
from ..cdp import CDPError, build_resolve_bbox_js
from ..core.errors import best_effort
from ..runtime.capture import emit_png, png_dims, slugify
from ..runtime.observe import DrainScope, drain_deltas, post_action_observation
from ..state import current
from ..tooling import Content, ToolError, tool

log = logging.getLogger(__name__)

# The whole family is on by default (CONDUCTOR_DESKTOP_WEB): web automation is
# common, and without a reachable browser the tools say so when called.


def _resolve_tab_filter(tab: str | None) -> str:
    """Prefer per-call tab filter, fall back to config default."""
    if tab is not None:
        return tab
    return current().config.cdp_default_tab_filter


_registry_cache: tuple[object, BrowserRegistry] | None = None


def _registry() -> BrowserRegistry:
    """The browser registry, built once per config object.

    Endpoints hold long-lived state (BiDi sessions, tab-handle numbering),
    so they must survive across tool calls. Raises BrowserError on a bad
    CONDUCTOR_DESKTOP_BROWSERS — call inside the tool's error handling.
    """
    global _registry_cache
    cfg = current().config
    if _registry_cache is None or _registry_cache[0] is not cfg:
        _registry_cache = (cfg, BrowserRegistry.from_config(cfg))
    return _registry_cache[1]


async def close_browsers() -> None:
    """End long-lived browser sessions; call on server shutdown.

    Firefox keeps a BiDi session after its WebSocket closes and refuses new
    ones, so exiting without session.end locks the browser until restart.
    """
    global _registry_cache
    cache, _registry_cache = _registry_cache, None
    if cache is not None:
        await cache[1].close()


# Parameters shared across the web tools.
Browser = Annotated[str | None, Field(description=(
    'Browser slug from web_list_tabs (e.g. "firefox"). Omit for '
    "CONDUCTOR_DESKTOP_DEFAULT_BROWSER, else the only configured browser, else "
    "the only reachable one."))]
Tab = Annotated[str | None, Field(description=(
    'A handle from web_list_tabs ("firefox:3"; pins browser and tab), or a '
    "case-insensitive substring of the tab's URL + title (first match wins). "
    "Omit for CONDUCTOR_DESKTOP_CDP_TAB_FILTER, else the browser's default tab."))]
MatchUrl = Annotated[str | None, Field(description=(
    "Case-insensitive substring of the tab's URL. Takes precedence over tab's "
    "substring/handle match (a handle still picks the browser); AND-combined "
    "with match_title."))]
MatchTitle = Annotated[str | None, Field(description=(
    "Case-insensitive substring of the tab's title. Takes precedence over "
    "tab's substring/handle match; AND-combined with match_url."))]
IncludeDesktop = Annotated[bool, Field(description=(
    "true: also include host accessibility events (focus, value changes) in "
    "the deltas. Default leaves unrelated desktop noise out; the page's own "
    "events are always included."))]
Selector = Annotated[str | None, Field(description=(
    'CSS selector, e.g. "#revenue-chart". Must match exactly one element.'))]
ContainsText = Annotated[str | None, Field(description=(
    "Visible text: picks the deepest element whose textContent contains it. "
    "For obfuscated class names with a stable label; pair with climb."))]
ElementJs = Annotated[str | None, Field(description=(
    "A JavaScript expression that evaluates to an Element."))]
Climb = Annotated[int, Field(description=(
    "parentElement hops after the locator resolves, e.g. from a heading up "
    "to its card. 0 = the element itself."))]
WaitAfter = Annotated[float, Field(description=(
    "Seconds to wait before the screenshot, so the effect can render."))]
FocusChrome = Annotated[bool, Field(description=(
    "Address-bar fallback only: bring a Chrome window to the front first. "
    "false if you have already focused the right tab."))]


@tool(kind="observing", gate="web_enabled")
async def web_list_tabs(browser: Annotated[str | None, Field(description=(
        "List only this browser (slug). Omit for every configured browser "
        "(CONDUCTOR_DESKTOP_BROWSERS; default just chrome over CDP)."))] = None,
                        ) -> str:
    """List open tabs per browser, each with a handle like [firefox:3] to pass as
    tab= to the other web_* tools; it pins browser and tab and stays stable
    while the server runs. Call this first when you don't know which tab to
    drive. The first tab in each group is that browser's default, and the group
    header is its browser= slug. A browser not running with a debugging port
    shows as unreachable.

    Chrome needs --remote-debugging-port (default 9222) and, on macOS,
    --user-data-dir=/some/path; Firefox needs --remote-debugging-port.
    """
    try:
        reg = _registry()
        endpoints = [reg.get(browser)] if browser else reg.endpoints()
    except BROWSER_ERRORS as e:
        raise ToolError(str(e)) from None
    results = await asyncio.gather(*(ep.list_tabs() for ep in endpoints),
                                   return_exceptions=True)
    sections: list[str] = []
    reachable = 0
    for ep, res in zip(endpoints, results):
        if isinstance(res, BaseException):
            sections.append(f"{ep.slug} ({ep.kind}): unreachable — {res}")
            continue
        reachable += 1
        lines = [f"{ep.slug} ({ep.kind})"]
        if not res:
            lines.append("  (no page targets)")
        lines.extend(f"  [{t.ref}] {t.title or '(untitled)'}\n    {t.url}" for t in res)
        sections.append("\n".join(lines))
    if reachable == 0:
        raise ToolError("\n".join(sections))
    return "\n".join(sections)


@tool(kind="acting", gate="web_enabled")
async def activate_tab(tab: Tab = None,
                       match_url: MatchUrl = None,
                       match_title: MatchTitle = None,
                       browser: Browser = None) -> str:
    """Bring a browser tab to the foreground. Do this before web_crop or a
    screenshot of a tab that isn't visible: background tabs report
    visibilityState "hidden", and many dashboards then render blank or stale.
    It switches tabs inside the browser only; if the browser window is behind
    another app, also focus_window. Tab selection: match_url / match_title,
    else tab, else the default tab; the first match wins.

    Works the same over CDP (Chrome) and WebDriver BiDi (Firefox).
    """
    try:
        ep, target = await _registry().select(browser=browser,
                                              tab=_resolve_tab_filter(tab),
                                              match_url=match_url,
                                              match_title=match_title)
        await ep.activate(target)
    except BROWSER_ERRORS as e:
        raise ToolError(str(e)) from None
    return f"activated tab [{target.ref}]: {target.title or '(untitled)'}\n  {target.url}"


@tool(kind="acting", gate="web_enabled")
async def web_eval(expression: Annotated[str, Field(description=(
                       "JavaScript to evaluate. The value comes back as JSON; "
                       "non-plain objects such as DOM nodes serialize as {}."))],
                   tab: Tab = None,
                   match_url: MatchUrl = None,
                   match_title: MatchTitle = None,
                   browser: Browser = None,
                   await_promise: Annotated[bool, Field(description=(
                       "If the expression returns a Promise, wait for it and "
                       "return the resolved value (bounded by the 30 s "
                       "protocol timeout)."))] = True,
                   wait_for_selector: Annotated[str | None, Field(description=(
                       "After the eval, poll until document.querySelector "
                       "finds this, up to wait_timeout_ms."))] = None,
                   wait_for_network_idle: Annotated[bool, Field(description=(
                       "After the eval, wait until requests have been quiet "
                       "for ~500 ms, up to wait_timeout_ms. WebSocket and "
                       "EventSource connections are ignored."))] = False,
                   wait_timeout_ms: Annotated[int, Field(description=(
                       "Cap for each of the two waits above."))] = 5000,
                   settle_ms: Annotated[int, Field(description=(
                       "Fixed sleep after the other waits, for settling they "
                       "can't see (e.g. an animation). 0 for plain reads."))] = 0,
                   include_desktop: IncludeDesktop = False) -> list[Content]:
    """Evaluate a JavaScript expression in a browser tab and return its JSON
    result; JS exceptions come back as tool errors. The direct way to read or
    change a page (DOM queries, clicks, navigation) without pixels. Optional
    waits run after the eval, in order: wait_for_selector,
    wait_for_network_idle, settle_ms. Tab selection: match_url / match_title,
    else tab, else the default tab; the first match wins, so use a handle from
    web_list_tabs when tabs look alike.

    Each wait's outcome is appended to the result, e.g. "[selector='#x'
    matched; network_idle=TIMEOUT after 5000ms]". Values serialize the same
    in every browser.

    Examples:
        web_eval(expression="document.title")
        web_eval(expression="location.href = 'https://news.ycombinator.com/'",
                 wait_for_network_idle=True)
        web_eval(expression="document.querySelector('#submit').click()",
                 wait_for_selector="#confirmation-dialog")
    """
    cfg = current().config
    deltas: list[TextContent] = []
    try:
        ep, target = await _registry().select(
            browser=browser, tab=_resolve_tab_filter(tab),
            match_url=match_url, match_title=match_title)
        session = await ep.open(target)
        try:
            obs = ep.observer(asyncio.get_running_loop(), session) if cfg.deltas_enabled else None
            wait_notes: list[str] = []
            try:
                if obs is not None:
                    await obs.start()
                value = await session.evaluate(expression, await_promise=await_promise)
                if wait_for_selector:
                    hit = await session.wait_for_selector(
                        wait_for_selector, timeout_ms=wait_timeout_ms
                    )
                    wait_notes.append(
                        f"selector={wait_for_selector!r} "
                        f"{'matched' if hit else f'TIMEOUT after {wait_timeout_ms}ms'}"
                    )
                if wait_for_network_idle:
                    idle = await session.wait_for_network_idle(
                        timeout_ms=wait_timeout_ms
                    )
                    wait_notes.append(
                        f"network_idle={'reached' if idle else f'TIMEOUT after {wait_timeout_ms}ms'}"
                    )
                if settle_ms > 0:
                    await asyncio.sleep(settle_ms / 1000)
            finally:
                deltas = drain_deltas(extra=obs, scope=DrainScope.for_web(include_desktop))
                if obs is not None:
                    await obs.stop()
        finally:
            await session.close()
    except BROWSER_ERRORS as e:
        raise ToolError(str(e), content=deltas) from None
    primary = json.dumps(value, ensure_ascii=False, default=str)
    if wait_notes:
        primary += "  [" + "; ".join(wait_notes) + "]"
    return [TextContent(type="text", text=primary), *deltas]


def _web_crop_resolve_error(resolved: object, *, selector: str | None,
                            contains_text: str | None, element_js: str | None,
                            climb: int) -> str | None:
    """Map the discriminated record from build_resolve_bbox_js to a tool error."""
    locator = (
        f"selector={selector!r}" if selector is not None
        else f"contains_text={contains_text!r}" if contains_text is not None
        else f"element_js={element_js!r}"
    )
    if not isinstance(resolved, dict):
        return f"web_crop: unexpected resolve result for {locator}: {resolved!r}"
    kind = resolved.get("kind")
    if kind == "ok":
        return None
    if kind == "missing":
        return f"web_crop: element not found for {locator}"
    if kind == "ambiguous":
        n = resolved.get("count", "?")
        return (
            f"web_crop: {locator} matched {n} elements — narrow the selector "
            f"(e.g. add an id, :nth-of-type, or scope it to a container)"
        )
    if kind == "oversized":
        w, h = resolved.get("w", "?"), resolved.get("h", "?")
        climb_hint = (
            f" — climb={climb} may have walked past the target; try a smaller climb"
            if climb > 0 else
            " — locator likely picked a giant ancestor; tighten it or add climb=0"
        )
        return (
            f"web_crop: {locator} resolved to a {w}x{h} bbox larger than "
            f"viewport × 6{climb_hint}"
        )
    return f"web_crop: unknown resolve kind {kind!r} for {locator}"


# One crop at a time per tab. Chrome's captureBeyondViewport resizes the view
# for the capture and puts it back after; two overlapping captures put back
# each other's temporary size, and the tab stays stuck at it until reloaded.
_crop_locks: dict[str, asyncio.Lock] = {}


def _crop_lock(tab: TabInfo) -> asyncio.Lock:
    return _crop_locks.setdefault(f"{tab.browser}:{tab.id}", asyncio.Lock())


# Fixed and sticky elements (site headers, cookie bars, dev toolbars) paint
# over a crop of something beneath them. Hide those that are neither inside
# the target nor around it, including inside open shadow roots, and put them
# back after. The resolver marks the target with data-conductor-crop; the
# hidden attribute keeps each element's own inline visibility for the restore.
_ALL_ELEMENTS_JS = r"""
function* allElements(root) {
    for (const el of root.querySelectorAll('*')) {
        yield el;
        if (el.shadowRoot) yield* allElements(el.shadowRoot);
    }
}
""".strip()

_HIDE_OVERLAYS_JS = r"""
(() => {
    __ALL__
    // Ancestry across shadow boundaries: a shadow root's parent is its host.
    const within = (a, b) => {
        for (let n = a; n; n = n.parentNode || n.host) if (n === b) return true;
        return false;
    };
    let target = null;
    for (const el of allElements(document)) {
        if (el.hasAttribute('data-conductor-crop')) { target = el; break; }
    }
    let n = 0;
    for (const el of allElements(document)) {
        const pos = getComputedStyle(el).position;
        if (pos !== 'fixed' && pos !== 'sticky') continue;
        if (target && (within(el, target) || within(target, el))) continue;
        el.setAttribute('data-conductor-hidden', el.style.visibility);
        el.style.visibility = 'hidden';
        n++;
    }
    return n;
})()
""".strip()

_RESTORE_OVERLAYS_JS = (
    "(() => { " + _ALL_ELEMENTS_JS + "\n"
    "for (const el of allElements(document)) {"
    " el.removeAttribute('data-conductor-crop');"
    " if (!el.hasAttribute('data-conductor-hidden')) continue;"
    " el.style.visibility = el.getAttribute('data-conductor-hidden');"
    " el.removeAttribute('data-conductor-hidden'); } })()"
)


_HIDE_OVERLAYS_JS = _HIDE_OVERLAYS_JS.replace("__ALL__", _ALL_ELEMENTS_JS)


async def _crop_once(session, js: str, *, selector: str | None,
                     contains_text: str | None, element_js: str | None,
                     climb: int, padding: int, render_wait_ms: int,
                     max_height: int, hide_fixed: bool):
    """Resolve the element and capture it: the part that must not overlap
    another crop of the same tab. Returns (png, x, y, w, h, truncated_from)."""
    resolved = await session.evaluate(js)
    err = _web_crop_resolve_error(resolved, selector=selector,
                                  contains_text=contains_text,
                                  element_js=element_js,
                                  climb=climb)
    if err is not None:
        raise ToolError(err)
    # The resolver marked the target; the restore removes the mark, so it
    # runs however this ends, not only after a capture.
    try:
        if render_wait_ms > 0:
            await asyncio.sleep(render_wait_ms / 1000)
        x = max(0.0, float(resolved["x"]) - padding)
        y = max(0.0, float(resolved["y"]) - padding)
        w = max(1.0, float(resolved["w"]) + 2 * padding)
        h_raw = max(1.0, float(resolved["h"]) + 2 * padding)
        truncated_from = None
        if max_height > 0 and h_raw > max_height:
            truncated_from = h_raw
            h = float(max_height)
        else:
            h = h_raw
        if w < 1 or h < 1:
            raise ToolError(f"web_crop: degenerate bbox {resolved!r}")
        clip = {"x": x, "y": y, "width": w, "height": h, "scale": 1}
        if hide_fixed:
            await session.evaluate(_HIDE_OVERLAYS_JS)
        png = await session.capture_screenshot(clip=clip, beyond_viewport=True)
    finally:
        await session.evaluate(_RESTORE_OVERLAYS_JS)
    return png, x, y, w, h, truncated_from


@tool(kind="observing", gate="web_enabled")
async def web_crop(
    selector: Selector = None,
    contains_text: ContainsText = None,
    element_js: ElementJs = None,
    climb: Climb = 0,
    tab: Tab = None,
    match_url: MatchUrl = None,
    match_title: MatchTitle = None,
    browser: Browser = None,
    padding: Annotated[int, Field(description=(
        "Pixels added to each side of the element's box."))] = 0,
    scroll_into_view: Annotated[bool, Field(description=(
        "Scroll the element into view and wait two animation frames, which "
        "lazy-rendered charts need before they draw."))] = True,
    render_wait_ms: Annotated[int, Field(description=(
        "Extra wait after scrolling, for widgets that fetch data on scroll "
        "(often 100-300)."))] = 0,
    max_height: Annotated[int, Field(description=(
        "If > 0, clip the capture to this many CSS pixels tall; the result "
        'then says "[truncated h: N → M]". For long tables and articles.'))] = 0,
    hide_fixed: Annotated[bool, Field(description=(
        "Hide fixed and sticky elements outside the element (site headers, "
        "cookie bars) during the capture, so they don't paint over it."))] = True,
    include_desktop: IncludeDesktop = False,
) -> list[Content]:
    """Screenshot one DOM element in a browser tab, pixel-exact, located by
    exactly one of selector, contains_text or element_js (climb walks up to a
    container). It refuses rather than guessing: a selector matching several
    elements, no match, or a box over 6x the viewport (a giant ancestor) is an
    error naming the fix. It scrolls the element into view, waits for paint
    and hides fixed headers over it; a background tab may render blank, so
    activate_tab it first.

    Tab selection is as in web_eval. The image follows the server's
    screenshot mode (inline, file or both); the result's first line gives
    the element's bbox in page CSS pixels and the image size.

    Examples:
        web_crop(selector="#revenue-chart", padding=8)
        web_crop(contains_text="Top countries", climb=3, render_wait_ms=200)
        web_crop(selector="table.results", max_height=1200)
    """
    locator_count = sum(x is not None for x in (selector, contains_text, element_js))
    if locator_count != 1:
        raise ToolError("web_crop: provide exactly one of selector / contains_text / element_js")
    cfg = current().config
    try:
        js = build_resolve_bbox_js(
            selector=selector, contains_text=contains_text, element_js=element_js,
            climb=climb, scroll_into_view=scroll_into_view,
        )
    except ValueError as e:
        raise ToolError(str(e)) from None
    deltas: list[TextContent] = []
    try:
        ep, target = await _registry().select(
            browser=browser, tab=_resolve_tab_filter(tab),
            match_url=match_url, match_title=match_title)
        session = await ep.open(target)
        try:
            obs = ep.observer(asyncio.get_running_loop(), session) if cfg.deltas_enabled else None
            try:
                if obs is not None:
                    await obs.start()
                async with _crop_lock(target):
                    png, x, y, w, h, truncated_from = await _crop_once(
                        session, js, selector=selector, contains_text=contains_text,
                        element_js=element_js, climb=climb, padding=padding,
                        render_wait_ms=render_wait_ms, max_height=max_height,
                        hide_fixed=hide_fixed)
            finally:
                deltas = drain_deltas(extra=obs, scope=DrainScope.for_web(include_desktop))
                if obs is not None:
                    await obs.stop()
        finally:
            await session.close()
    except ToolError as e:
        raise ToolError(str(e), content=deltas) from None
    except BROWSER_ERRORS as e:
        raise ToolError(str(e), content=deltas) from None
    except KeyError as e:
        raise ToolError(f"web_crop: unexpected bbox shape from JS ({e})", content=deltas) from None
    img_w, img_h = png_dims(png)
    label_src = selector or contains_text or element_js or "element"
    bbox_text = (
        f"web_crop: bbox=[{int(x)},{int(y)},{int(x+w)},{int(y+h)}] "
        f"({img_w}x{img_h})"
    )
    if truncated_from is not None:
        bbox_text += f" [truncated h: {int(truncated_from)} → {int(h)}]"
    out: list[Content] = [
        TextContent(type="text", text=bbox_text),
        *deltas,
    ]
    out.extend(await emit_png(png, f"webcrop-{slugify(label_src)}", img_w, img_h))
    return out


# --- URL-bar JS injection (no CDP required) --------------------------------

def _build_mark_js(*, selector: str | None, contains_text: str | None,
                   element_js: str | None, climb: int,
                   color: str, scroll_into_view: bool) -> str:
    """Compact single-line JS that resolves an element, climbs N parents,
    optionally scrolls into view, and paints a visible outline.

    It evaluates to "ok", "missing" (the locator found nothing) or "climbed"
    (climb walked past the document root). URL bars don't handle multi-line
    well, so the output is one line.
    """
    provided = [x is not None for x in (selector, contains_text, element_js)]
    if sum(provided) != 1:
        raise ValueError("exactly one of selector/contains_text/element_js must be set")
    if selector is not None:
        anchor = f"document.querySelector({json.dumps(selector, ensure_ascii=False)})"
    elif contains_text is not None:
        anchor = f"f({json.dumps(contains_text, ensure_ascii=False)})"
    else:
        anchor = f"({element_js})"
    scroll_expr = (
        "try{n.scrollIntoView({block:'center',inline:'center'})}catch(e){}"
        if scroll_into_view else ""
    )
    color_js = json.dumps(color, ensure_ascii=False)
    return (
        "(()=>{"
        "function f(t){if(!document.body||!document.body.textContent.includes(t))return null;"
        "let n=document.body;while(true){"
        "let c=Array.from(n.children).find(x=>x.textContent&&x.textContent.includes(t));"
        "if(!c)return n;n=c;}}"
        f"let n={anchor};if(!n)return 'missing';"
        f"for(let i=0;i<{int(climb)};i++)n=n&&n.parentElement;"
        "if(!n)return 'climbed';"
        f"{scroll_expr}"
        f"n.style.outline='4px solid '+{color_js};"
        "n.style.outlineOffset='0px';"
        "n.setAttribute('data-conductor-mark','1');"
        "return 'ok';"
        "})()"
    )


async def _inject_url_bar_js(js: str, focus_chrome: bool) -> str | None:
    """Type a javascript: URL into Chrome's URL bar and press Enter.

    Chrome strips the "javascript:" prefix from pasted content as an anti-
    phishing measure, so the prefix is typed character-by-character via
    `literal=True`. Everything after the prefix goes through the paste
    fast path. Returns None on success or an error message string.

    Assumes Chrome (or another Chromium browser) is the frontmost app.
    Pacing between inputs is intentionally generous so that the earlier
    "sticky cmd" class of bug (modifier state bleeding across hotkey and
    subsequent key events) cannot re-surface.
    """
    ex = current().executor
    if focus_chrome:
        with best_effort("focusing Chrome", log):
            await ex.focus_window("Chrome")
        await asyncio.sleep(0.15)
    if sys.platform == "darwin":
        url_bar_keys = ["cmd", "l"]
    elif sys.platform == "win32":
        url_bar_keys = ["ctrl", "l"]
    else:
        url_bar_keys = ["Control_L", "l"]
    r = await ex.hotkey(url_bar_keys)
    if not r.success:
        return f"{'+'.join(url_bar_keys)} failed: {r.error}"
    await asyncio.sleep(0.04)
    r = await ex.type_text("javascript:", literal=True)
    if not r.success:
        return f"type 'javascript:' failed: {r.error}"
    await asyncio.sleep(0.04)
    r = await ex.type_text(js + ";void(0);")
    if not r.success:
        return f"paste body failed: {r.error}"
    await asyncio.sleep(0.06)
    r = await ex.key_press("end")
    if not r.success:
        return f"end key failed: {r.error}"
    await asyncio.sleep(0.02)
    r = await ex.key_press("enter")
    if not r.success:
        return f"enter failed: {r.error}"
    return None


async def _run_page_js(js: str, *, browser: str | None,
                       focus_chrome: bool) -> tuple[str | None, str, object]:
    """Fire-and-forget page JS. Returns ``(error, where_it_ran, value)``;
    ``value`` is the script's result over a browser connection and None
    through the address bar, which can't read one back.

    Runs through a browser connection whenever one is available — `browser`
    when named, else the default browser, in its default tab. Only when no
    browser is named and none answers does it fall back to typing a
    javascript: URL into the frontmost Chrome, so hosts without a debugging
    port keep working. Targeting problems (several browsers reachable, an
    unknown slug, no tabs) and a named browser that's down are reported, never
    silently retried in Chrome.
    """
    fallback = False
    try:
        ep, target = await _registry().select(browser=browser, tab=None)
    except BrowserUnreachable:
        fallback = True
    except (CDPError, BidiError) as e:
        # The resolved browser didn't answer.
        if browser is not None:
            return str(e), "", None
        fallback = True
    except BROWSER_ERRORS as e:
        return str(e), "", None
    if fallback:
        err = await _inject_url_bar_js(js, focus_chrome=focus_chrome)
        return err, "the frontmost Chrome (address bar)", None
    try:
        session = await ep.open(target)
        try:
            value = await session.evaluate(js, await_promise=False)
        finally:
            await session.close()
    except BROWSER_ERRORS as e:
        return str(e), "", None
    return None, f"[{target.ref}]", value


@tool(kind="acting", gate="web_enabled")
async def web_js_snippet(js: Annotated[str, Field(description=(
                             'The expression or IIFE body, without a "javascript:" '
                             "prefix."))],
                         wait_after: WaitAfter = 0.5,
                         focus_chrome: FocusChrome = True,
                         browser: Annotated[str | None, Field(description=(
                             "Browser slug from web_list_tabs. A named browser "
                             "never falls back to the address bar: if it is "
                             "unreachable, that's the error."))] = None,
                         ) -> list[Content]:
    """Run fire-and-forget JavaScript in a browser's default tab, then return a
    screenshot. There is no return value: use web_eval to read a result or to
    target another tab. It runs through the browser connection when one is
    reachable; with none it types a javascript: URL into the frontmost Chrome's
    address bar, which needs no debugging port. A named browser= never falls
    back. The reply says where it ran.

    The default tab is Chrome's most recently used tab, or Firefox's visible
    one. window.find scrolls a text match into view in one call, which saves
    many scroll steps.

    Examples:
        web_js_snippet(js="window.scrollTo(0, 0)")
        web_js_snippet(js="window.find('UD-Q4_K_XL')")
    """
    err, where, _ = await _run_page_js(js, browser=browser, focus_chrome=focus_chrome)
    if err:
        raise ToolError(err, content=await post_action_observation())
    if wait_after > 0:
        await asyncio.sleep(wait_after)
    out: list[Content] = [
        TextContent(type="text", text=f"web_js_snippet: ran in {where} ({len(js)} chars)")
    ]
    out.extend(await post_action_observation())
    return out


@tool(kind="acting", gate="web_enabled")
async def web_mark(
    selector: Selector = None,
    contains_text: ContainsText = None,
    element_js: ElementJs = None,
    climb: Climb = 0,
    color: Annotated[str, Field(description=(
        "Outline color, any CSS color (name, hex, rgb())."))] = "magenta",
    scroll_into_view: Annotated[bool, Field(description=(
        "Scroll the element to the middle of the view first."))] = True,
    wait_after: WaitAfter = 0.5,
    focus_chrome: FocusChrome = True,
    browser: Annotated[str | None, Field(description=(
        "Browser slug from web_list_tabs; as in web_js_snippet, a named "
        "browser never falls back to the address bar."))] = None,
) -> list[Content]:
    """Outline a DOM element in a bold color and scroll it into view, so a vision
    grounder can find it: follow with crop(target="the element outlined in
    magenta"). Useful on pages with obfuscated class names but a stable visible
    label (contains_text plus climb). Pick the element with exactly one of
    selector, contains_text or element_js. Runs like web_js_snippet: in the
    default tab through the browser connection if reachable, else via the
    frontmost Chrome's address bar.

    A locator that finds nothing, or a climb past the document root, is an
    error. Through the address bar the result can't be read back, so the
    reply says the outline is unverified: check the screenshot.

    Example (a dashboard card found by its heading):
        web_mark(contains_text="Top countries", climb=3)
        crop(target="the card outlined in magenta")
    """
    try:
        js = _build_mark_js(
            selector=selector, contains_text=contains_text, element_js=element_js,
            climb=climb, color=color, scroll_into_view=scroll_into_view,
        )
    except ValueError as e:
        raise ToolError(str(e)) from None
    err, where, found = await _run_page_js(js, browser=browser, focus_chrome=focus_chrome)
    label = selector or contains_text or element_js or "element"
    if not err and found == "missing":
        err = f"web_mark: element not found for {label!r}; nothing was outlined"
    elif not err and found == "climbed":
        err = (f"web_mark: climb={climb} walked past the document root from "
               f"{label!r}; try a smaller climb")
    if err:
        raise ToolError(err, content=await post_action_observation())
    if wait_after > 0:
        await asyncio.sleep(wait_after)
    note = "" if found == "ok" else " (unverified: check the screenshot)"
    out: list[Content] = [
        TextContent(type="text",
                    text=f"web_mark: outlined {label!r} (+{climb} parents, {color}) "
                         f"in {where}{note}"),
    ]
    out.extend(await post_action_observation())
    return out
