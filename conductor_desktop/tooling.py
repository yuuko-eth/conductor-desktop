"""Tool declaration and the call contract every tool shares.

A tool module declares each tool once::

    @tool(kind="acting")                 # takes the desktop input lock
    async def click(target: str) -> list[Content]: ...

    @tool(kind="observing", gate="web_enabled")
    async def web_crop(...) -> list[Content]: ...

``kind`` feeds the operator surfaces (the tray's acting/observing states, the
pause switch) and the MCP ``readOnlyHint``. ``lock`` defaults to
``kind == "acting"``: acting tools run only in the process that holds the
desktop input lock. ``gate`` names a boolean ``MCPConfig`` attribute; a tool
whose gate is off is left off the surface rather than registered and failing.

Declaring a tool doesn't register it. :func:`register` adds the enabled ones
to a FastMCP instance at startup, once the config is known.

Contract: a tool returns content (a ``str``, one content block or a list)
or raises :class:`ToolError` (a backend's ``BackendError`` and a
``GrounderError`` are reported the same way). Either way the caller gets a
``CallToolResult``; a ``ToolError`` becomes ``isError=True`` with an
``error:`` line followed by any content the error carries (typically the
post-action screenshot). The wrapper also audits the call, drives the tray,
honours the operator's pause switch and appends the timing line.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import state as _state
from .audit import scrub_error
from .config import MCPConfig
from .core.errors import BackendError
from .grounder import CURRENT_TOOL, GrounderError

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("conductor.mcp.tooling")

Kind = Literal["acting", "observing"]
Content = TextContent | ImageContent

#: What a spectator (an instance without the desktop lock) can still use.
READ_ONLY_NOTE = (
    "Read-only tools (read_screen, capture_screen, locate, crop, web_crop, "
    "web_list_tabs, clipboard_get, list_windows, diagnose)"
)


class ToolError(Exception):
    """A tool failed in a way the caller should read and act on.

    ``content`` rides along after the ``error:`` line, e.g. the screenshot
    taken after a failed click, so the agent can see why.
    """

    def __init__(self, message: str, *, content: Sequence[Content] = ()) -> None:
        super().__init__(message)
        self.content = list(content)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Awaitable[CallToolResult]]
    kind: Kind
    lock: bool
    gate: str | None

    def enabled(self, config: MCPConfig) -> bool:
        return self.gate is None or bool(getattr(config, self.gate))


_SPECS: dict[str, ToolSpec] = {}


def tool(*, kind: Kind, lock: bool | None = None, gate: str | None = None):
    """Declare the decorated coroutine as an MCP tool. See the module docstring."""
    if kind not in ("acting", "observing"):
        raise ValueError(f"kind must be 'acting' or 'observing', not {kind!r}")
    if gate is not None and not hasattr(MCPConfig, gate):
        raise ValueError(f"gate {gate!r} is not an MCPConfig attribute")
    needs_lock = kind == "acting" if lock is None else lock

    def decorate(fn: Callable[..., Awaitable[Any]]):
        name = fn.__name__
        if name in _SPECS:
            raise ValueError(f"tool {name!r} declared twice")
        wrapped = _wrap(fn, name, kind, needs_lock)
        _SPECS[name] = ToolSpec(name=name, fn=wrapped, kind=kind, lock=needs_lock, gate=gate)
        return wrapped

    return decorate


def specs() -> dict[str, ToolSpec]:
    """Every declared tool, enabled or not. Import ``conductor_desktop.tools`` first."""
    return dict(_SPECS)


def register(mcp: FastMCP, config: MCPConfig) -> list[str]:
    """Add every tool whose gate is on to ``mcp``. Returns the names added."""
    added = []
    for spec in _SPECS.values():
        if not spec.enabled(config):
            continue
        mcp.add_tool(
            spec.fn,
            name=spec.name,
            # Dedented: a client that cuts descriptions at a fixed length
            # (Claude Desktop, ~500 chars) shouldn't spend it on indentation.
            description=inspect.cleandoc(spec.fn.__doc__ or ""),
            annotations=ToolAnnotations(readOnlyHint=spec.kind == "observing"),
            # Replies are content blocks (text, screenshots). Without this
            # FastMCP derives an output schema from the return annotation
            # and sends every reply a second time as structuredContent.
            structured_output=False,
        )
        added.append(spec.name)
    return added


# ── The call wrapper ────────────────────────────────────────────────


def _content(result: Any) -> list[Content]:
    if isinstance(result, str):
        return [TextContent(type="text", text=result)]
    if isinstance(result, TextContent | ImageContent):
        return [result]
    return list(result)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _without_surrogates(content: list[Content]) -> list[Content]:
    """Text blocks with their UTF-16 surrogates made whole.

    Text read from the desktop can arrive with an emoji's surrogate pair as
    two code points, or cut between them (an accessible name cut at a UTF-16
    length). Either can't be encoded as UTF-8, and the reply failing to
    serialise takes the whole server down. Pairs are joined into the one
    character; a half on its own becomes U+FFFD.
    """
    out = []
    for c in content:
        if isinstance(c, TextContent) and _SURROGATE.search(c.text):
            c = c.model_copy(update={"text": c.text.encode("utf-16", "surrogatepass")
                                                   .decode("utf-16", "replace")})
        out.append(c)
    return out


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.2f}s"
    m = int(seconds // 60)
    return f"{m}m{seconds - m * 60:.1f}s"


async def _ensure_input_lock(st: _state.State) -> None:
    """Raise ToolError unless this process holds the desktop input lock.

    A spectator first tries to take the lock over, in case the holder quit
    since startup; the error names the holder so the user knows which
    instance to quit.
    """
    if st.lock.is_held():
        return
    mu = st.lock_upgrade_mu
    assert mu is not None, "lock_upgrade_mu must be initialized before tool calls"
    async with mu:
        if st.lock.is_held():
            return
        if st.lock.try_acquire():
            log.info("desktop input lock acquired opportunistically — "
                     "this MCP server is now the holder.")
            return
        info = st.lock.read_holder_info()
        detail = (f"pid={info.pid}, started={info.started_at}, transport={info.transport}"
                  if info is not None else "another conductor-desktop")
        raise ToolError(
            f"desktop input lock held by {detail}. Quit that instance from its "
            f"tray icon (or kill the PID) to free the desktop. {READ_ONLY_NOTE} "
            f"still work from this instance."
        )


def _wrap(fn: Callable[..., Awaitable[Any]], name: str, kind: Kind, needs_lock: bool):
    # functools.wraps keeps the signature (inspect follows __wrapped__), which
    # is what FastMCP builds the input schema from.
    @functools.wraps(fn)
    async def call(*args: Any, **kwargs: Any) -> CallToolResult:
        st = _state.current()
        if kind == "acting" and st.input_paused.is_set():
            # Audited: "the agent tried to act while paused" is exactly what
            # an operator reviews later.
            st.audit.record(tool=name, klass=kind, ok=False, elapsed=0.0,
                            kwargs=kwargs, error="input_paused")
            return CallToolResult(isError=True, content=[TextContent(
                type="text",
                text=(f"error: input is paused via the conductor-desktop tray icon. "
                      f"Toggle 'Pause input' off to allow {name}.")),
            ])
        t0 = time.perf_counter()
        tray = st.tray_state
        call_id = (tray.start(name, args=args, kwargs=kwargs, klass=kind)
                   if tray is not None else None)
        # Lets a missing-backend error name the tool (grounder.MissingGrounder).
        # Each MCP request runs in its own task, so no reset is needed.
        CURRENT_TOOL.set(name)
        error: str | None = None
        try:
            if needs_lock:
                await _ensure_input_lock(st)
            content = _content(await fn(*args, **kwargs))
        except (ToolError, BackendError, GrounderError) as e:
            # BackendError: the platform backend couldn't do it (a bad screen=,
            # a failed capture). GrounderError: the grounder was unreachable,
            # timed out or 5xx'd. Both are reported in-band, so the agent (and
            # whoever watches it) sees an actionable message instead of a
            # protocol-level crash.
            error = str(e)
            content = [TextContent(type="text", text=f"error: {error}"),
                       *getattr(e, "content", ())]
        except BaseException as e:
            # BaseException so a cancel (the client gave up mid-grounder-call)
            # still ends the tray entry; otherwise the call id stays in flight
            # and pins the tray to "acting". A cancel isn't a failure.
            cancelled = isinstance(e, asyncio.CancelledError)
            if call_id is not None:
                tray.end(call_id, success=cancelled,
                         error=None if cancelled else type(e).__name__)
            st.audit.record(tool=name, klass=kind, ok=False,
                            elapsed=time.perf_counter() - t0, kwargs=kwargs,
                            error="cancelled" if cancelled else type(e).__name__)
            raise
        elapsed = time.perf_counter() - t0
        if call_id is not None:
            # The dashboard shows this error; hide what the call's summary hides.
            tray.end(call_id, success=error is None,
                     error=scrub_error(error, kwargs, "redacted") if error else None)
        st.audit.record(tool=name, klass=kind, ok=error is None, elapsed=elapsed,
                        kwargs=kwargs, error=error)
        if st.config.tool_timing:
            content.append(TextContent(type="text", text=f"[{_fmt_elapsed(elapsed)}]"))
        return CallToolResult(content=_without_surrogates(content), isError=error is not None)

    return call
