"""Dashboard route handlers — exposed via two mount strategies:

- ``register(mcp)`` — attaches each route to the FastMCP instance via
  ``@mcp.custom_route``. Used when the MCP transport is HTTP or SSE,
  so the dashboard shares the same uvicorn.
- ``build_starlette_app()`` — returns a standalone Starlette app
  carrying the same routes. Used as a sidecar when the MCP transport
  is stdio (e.g. conductor-desktop launched as a subprocess by Claude Code,
  the typical operator deployment).

All routes live under ``/dashboard``:

  /dashboard/                      → SPA shell (index.html)
  /dashboard/static/{name}         → app.js, style.css
  /dashboard/api/state             → live state JSON (polled by SPA)
  /dashboard/api/tools/recent      → ring buffer (16 most recent)
  /dashboard/api/config            → effective config (read-only v1)
  /dashboard/api/perm-test  POST   → re-runs check_permissions
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import time
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .. import state as _state

log = logging.getLogger("conductor.mcp.dashboard")


# Static-file MIME type lookup for the few we ship.
_STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
}


def _read_static(name: str) -> bytes:
    path = files(__package__).joinpath("static", name)
    with path.open("rb") as fh:
        return fh.read()


def _state_snapshot() -> dict[str, Any]:
    st = _state.get()
    config = st.config if st else None
    tray = st.tray_state if st else None

    role = "headless"
    if st is not None:
        role = "holder" if st.lock.is_held() else "spectator"

    uptime_s = (
        int(time.time() - _state.holder_start_ts)
        if _state.holder_start_ts is not None
        else 0
    )

    state_label = tray.current_state if tray is not None else "idle"

    return {
        "state": state_label,
        "role": role,
        "backend": getattr(config, "backend", None),
        "grounder_url": getattr(config, "grounder_url", None),
        "transport": getattr(config, "transport", None),
        "listening": (
            f"{config.host}:{config.port}"
            if config and config.transport != "stdio" else "stdio"
        ),
        "input_paused": bool(st and st.input_paused.is_set()),
        "pid": os.getpid(),
        "uptime_s": uptime_s,
    }


def _tool_call_to_dict(call) -> dict[str, Any]:
    return {
        "call_id": call.call_id,
        "tool": call.tool,
        "klass": call.klass,
        "input_summary": call.input_summary,
        "started_at": call.started_at,
        "duration_ms": call.duration_ms,
        "success": call.success,
        "error": call.error,
        "in_flight": call.in_flight,
    }


#: Config fields whose values never leave the process: the gateway seat token
#: and anything else named like a credential.
_SECRET_FIELD = re.compile(r"(^|_)(key|token|secret|password)(_|$)")


def _public_value(name: str, value: Any) -> Any:
    """A config value as the dashboard may show it: secrets masked, URL credentials dropped."""
    if _SECRET_FIELD.search(name):
        return "<set>" if value else ""
    if isinstance(value, str) and "@" in value and "://" in value:
        parts = urlsplit(value)
        if parts.username or parts.password:
            host = parts.hostname or ""
            netloc = f"***@{host}" + (f":{parts.port}" if parts.port else "")
            return urlunsplit(parts._replace(netloc=netloc))
    return value


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _host_of(header: str) -> str:
    """The hostname in a Host header, without port or IPv6 brackets."""
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _loopback_guard(handler):
    """Refuse requests whose Host header isn't loopback while we're bound to loopback.

    Without this, a web page can rebind its own hostname to 127.0.0.1 and
    read the dashboard from the operator's browser (DNS rebinding). A server
    deliberately bound to a LAN address is already reachable directly, so
    the check only applies to a loopback bind.
    """
    @functools.wraps(handler)
    async def guarded(request: Request) -> Response:
        st = _state.get()
        config = st.config if st else None
        bind = getattr(config, "host", "127.0.0.1")
        if bind in _LOOPBACK and _host_of(request.headers.get("host", "")) not in _LOOPBACK:
            return PlainTextResponse("forbidden host", status_code=403)
        return await handler(request)
    return guarded


def _json_safe(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


# ── Handlers (module-level so both mount strategies share them) ──────


async def dashboard_index(request: Request) -> Response:
    try:
        html = _read_static("index.html")
    except FileNotFoundError:
        return PlainTextResponse("dashboard static assets missing",
                                 status_code=500)
    return Response(content=html, media_type=_STATIC_MIME[".html"])


async def dashboard_static(request: Request) -> Response:
    name = request.path_params["name"]
    if "/" in name or ".." in name or name.startswith("."):
        return PlainTextResponse("not found", status_code=404)
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    media = _STATIC_MIME.get(ext, "application/octet-stream")
    try:
        blob = _read_static(name)
    except FileNotFoundError:
        return PlainTextResponse("not found", status_code=404)
    return Response(content=blob, media_type=media)


async def api_state(request: Request) -> Response:
    return JSONResponse(_state_snapshot())


async def api_tools_recent(request: Request) -> Response:
    st = _state.get()
    tray = st.tray_state if st else None
    if tray is None:
        return JSONResponse({"calls": []})
    calls = list(reversed(tray.recent()))
    return JSONResponse({"calls": [_tool_call_to_dict(c) for c in calls]})


async def api_config(request: Request) -> Response:
    st = _state.get()
    config = st.config if st else None
    if config is None:
        return JSONResponse({"output": {}, "connection": {}, "other": {}})
    # Grouped for reading only: nothing changes config while the server
    # runs, so every key takes a restart.
    output_keys = {
        "auto_screenshot", "text_only",
        "payload_max_edge", "screenshot_max_width",
        "tool_timing", "rolling_describe_hint",
    }
    connection_keys = {
        "grounder_url", "backend", "transport",
        "host", "port",
    }
    output, connection, other = {}, {}, {}
    for k, v in vars(config).items():
        if k.startswith("_"):
            continue
        bucket = (output if k in output_keys
                  else connection if k in connection_keys
                  else other)
        v = _public_value(k, v)
        bucket[k] = v if _json_safe(v) else repr(v)
    return JSONResponse({"output": output, "connection": connection, "other": other})


async def api_perm_test(request: Request) -> Response:
    try:
        from ..check_permissions import run_self_test_async
        ok, detail = await run_self_test_async()
        return JSONResponse({"ok": bool(ok), "detail": str(detail)})
    except Exception as e:
        log.exception("perm-test endpoint crashed")
        return JSONResponse(
            {"ok": False, "detail": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


# ── Route table (single source of truth) ─────────────────────────────


_ROUTE_DEFS: list[tuple[str, list[str], Any]] = [
    ("/dashboard/",                       ["GET"],  dashboard_index),
    ("/dashboard",                        ["GET"],  dashboard_index),
    ("/dashboard/static/{name}",          ["GET"],  dashboard_static),
    ("/dashboard/api/state",              ["GET"],  api_state),
    ("/dashboard/api/tools/recent",       ["GET"],  api_tools_recent),
    ("/dashboard/api/config",             ["GET"],  api_config),
    ("/dashboard/api/perm-test",          ["POST"], api_perm_test),
]


# ── Mount strategies ─────────────────────────────────────────────────


def register(mcp) -> None:
    """Attach all dashboard routes to a FastMCP instance.

    Used when MCP transport is HTTP or SSE — the dashboard shares the
    FastMCP uvicorn. Must be called before
    ``mcp.run_streamable_http_async()``.
    """
    for path, methods, handler in _ROUTE_DEFS:
        mcp.custom_route(path, methods=methods,
                         include_in_schema=False)(_loopback_guard(handler))
    log.info("dashboard routes registered on FastMCP (%d routes)",
             len(_ROUTE_DEFS))


def build_starlette_app():
    """Return a standalone Starlette app carrying the dashboard routes.

    Used as a sidecar uvicorn when MCP transport is stdio. Imports
    Starlette lazily so the dashboard module stays import-cheap when
    Starlette isn't on the path (it is, transitively, via FastMCP).
    """
    from starlette.applications import Starlette
    from starlette.routing import Route
    routes = [
        Route(path, _loopback_guard(handler), methods=methods)
        for path, methods, handler in _ROUTE_DEFS
    ]
    return Starlette(routes=routes)
