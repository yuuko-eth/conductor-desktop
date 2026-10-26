"""Conductor Desktop server — tool surface.

The driver (Claude Code, etc.) owns the task loop and history. This server
is stateless per tool call: ground a target, execute, return the post-action
frame (if auto-screenshot is on). No guardian, no budget, no planning.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from . import ENV_PREFIX, LEGACY_ENV_RENAMED, tooling
from . import state as _state
from .audit import from_config as _audit_from_config
from .config import MCPConfig
from .core.executor import create_executor
from .core.models.host_node import HostNode
from .desktop_lock import DesktopLock
from .enumerate import Enumerator
from .grounder import (
    Grounder,
    MissingGrounder,
    family_is_point_only,
)
from .http_auth import BearerAuth, is_loopback
from .instructions import INSTRUCTIONS
from .managed_enumerator import ManagedEnumerator
from .managed_grounder import ManagedGrounder
from .state import State
from .tray_state import TrayStateMachine

log = logging.getLogger("conductor.mcp")


def build_mcp(config: MCPConfig) -> FastMCP:
    """A FastMCP server carrying every tool the config enables."""
    from . import tools  # noqa: F401  (importing declares the tools)

    mcp = FastMCP("conductor-desktop", instructions=INSTRUCTIONS,
                  host=config.host, port=config.port)
    tooling.register(mcp, config)
    return mcp


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def _build_state(config: MCPConfig, lock: DesktopLock) -> State:
    executor = await _connect_executor(config, lock)
    grounder, describe_grounder, region_grounder, enumerator = _build_grounders(config)
    mobile_registry = await _build_mobile_registry(config)
    audit = _audit_from_config(config)
    audit.start(platform=executor.host.platform)
    state = State(
        config=config, executor=executor, grounder=grounder,
        describe_grounder=describe_grounder,
        region_grounder=region_grounder,
        enumerator=enumerator,
        lock=lock, lock_upgrade_mu=asyncio.Lock(),
        mobile=mobile_registry, audit=audit,
    )
    await _start_desktop_observers(state)
    return state


async def _connect_executor(config: MCPConfig, lock: DesktopLock):
    """The host executor, connected, with its permissions checked for the
    role this process plays."""
    host = HostNode(
        label=f"conductor-desktop-{config.backend}",
        host_type=config.backend,  # type: ignore[arg-type]
        connection_uri=config.connection_uri,
        platform=config.platform,
    )
    executor = create_executor(host)
    ok = await executor.connect()
    if not ok:
        raise RuntimeError(
            f"Executor failed to connect (backend={config.backend}). "
            f"Permissions: {executor.permissions.summary()}"
        )
    # Permission requirement is role-dependent. The holder is the one
    # process allowed to inject input, so it needs the full set (screenshot
    # + input). A spectator (a second conductor-desktop on the same host; the
    # lock is already held elsewhere) is observe-only — every input tool is
    # gated behind the desktop input lock — so it must NOT refuse to start
    # just because the input grant is missing. Requiring permissions.ok
    # (screenshot AND input) for spectators too is what made the 2nd+
    # instance fail to boot on macOS when only the first context held the
    # Accessibility (AXIsProcessTrusted) grant. Spectators need only the
    # observe (screenshot) capability; a missing input grant is downgraded
    # to a warning and surfaces at call time if this process later upgrades
    # to holder.
    perms = executor.permissions
    required_ok = perms.ok if lock.is_held() else perms.screenshot_works
    if not required_ok:
        _report_permission_failure(perms)
        raise RuntimeError(
            "conductor-desktop refuses to start: required host permissions are missing. "
            "See stderr for details. Fix the permissions, fully quit and relaunch "
            "the app that spawns this server (a terminal, Claude Desktop, an IDE), "
            "then retry."
        )
    if not lock.is_held() and not perms.input_works:
        log.warning(
            "spectator: input permission missing (%s) — serving observe-only. "
            "Input tools stay disabled (they're gated behind the desktop lock "
            "anyway); if this process later becomes holder, input calls will "
            "surface this as a runtime error.",
            perms.input_detail,
        )
    return executor


def _build_grounders(config: MCPConfig):
    """The perception backends: (ground, describe, region, enumerate).

    Roles can alias one client — the wiring, and why, is below.
    """
    # Managed (SaaS) mode collapses the whole ensemble: the conductor-grounder
    # gateway routes point/describe/region across its own pools internally, so
    # all three roles are one ManagedGrounder over a single endpoint pair. None
    # of the split/region wiring below runs.
    if config.managed:
        grounder = ManagedGrounder(config.grounder_url, config.api_key,
                                   timeout=config.grounder_timeout)
        # The gateway routes point/describe/region internally, so all three
        # roles share one connection — but region ops must still say so
        # (`multiple`), or the gateway serves them from the point-only pool.
        # Enumeration is a distinct gateway endpoint
        # (/v1/enumerate → its OmniParser pool), so it gets its own managed
        # client — but always on, since the gateway owns whether a parser is
        # deployed (no per-host CONDUCTOR_DESKTOP_OMNIPARSER_URL gate in managed mode).
        describe_grounder = grounder
        region_grounder = grounder.region_view()
        enumerator = ManagedEnumerator(config.grounder_url, config.api_key,
                                       timeout=config.omniparser_timeout)
        log.info("managed mode: ground/describe/region/enumerate via gateway %s",
                 config.grounder_url)
    else:
        # A role with no backend gets a MissingGrounder: the server starts, and
        # the tools that need it answer with what's missing and what works instead.
        if config.grounder_configured:
            grounder = Grounder(config.grounder_url,
                                family=config.grounder_family,
                                timeout=config.grounder_timeout,
                                coord_space=config.coord_system)
        else:
            grounder = MissingGrounder("ground")
            log.info("no grounder configured: locate / click / crop / wait_for will say so "
                     "when called; ui_*, web_* and coordinate tools work")
        # Split-backend routing: build a second client only when describe targets
        # a different (url, family) than ground; otherwise reuse the one grounder
        # so there's a single connection pool and a single close.
        if not config.describe_configured:
            describe_grounder = MissingGrounder("describe")
        elif config.describe_backend_is_separate:
            describe_grounder = Grounder(config.effective_describe_url,
                                         family=config.effective_describe_family,
                                         timeout=config.grounder_timeout)
            log.info("describe backend split: ground=%s/%s describe=%s/%s",
                     config.grounder_url, config.grounder_family,
                     config.effective_describe_url, config.effective_describe_family)
        else:
            describe_grounder = grounder
        # Region ops (crop, locate.bbox, N-match) need a real bbox extent. If the
        # click grounder is point-only (Holo), route them to the describe backend
        # — bbox-capable and mandatory in that ensemble (config validation forbids
        # a point-only describe family). Otherwise the ground backend does boxes
        # itself, so region aliases it (unchanged for qwen/LA).
        if not config.region_configured:
            region_grounder = MissingGrounder("region")
        elif not config.grounder_configured or family_is_point_only(config.grounder_family):
            region_grounder = describe_grounder
            log.info("region backend: ground=%s/%s is point-only → regions via "
                     "describe=%s/%s",
                     config.grounder_url, config.grounder_family,
                     config.effective_region_url, config.effective_region_family)
        else:
            region_grounder = grounder
        # Optional enumeration backend — only when an OmniParser URL is configured.
        # Its own protocol/connection pool; closed alongside the grounders.
        if config.omniparser_enabled:
            enumerator = Enumerator(config.omniparser_url,
                                    timeout=config.omniparser_timeout)
            log.info("enumeration backend: OmniParser at %s", config.omniparser_url)
        else:
            enumerator = None
    return grounder, describe_grounder, region_grounder, enumerator


async def _build_mobile_registry(config: MCPConfig):
    """Spin up the mobile registry when either gate is set.

    Per-transport availability is determined by the registry's boot probe
    (adb on PATH + start-server, WDA placeholder for now). Probe failures
    log + disable the gate group; they never crash the server. Returns
    None when both gates are off — no tool group registers.
    """
    if not (config.mobile_android_enabled or config.mobile_ios_enabled):
        return None
    from .core.backends.mobile import MobileRegistry
    reg = MobileRegistry(
        android_enabled=config.mobile_android_enabled,
        ios_enabled=config.mobile_ios_enabled,
        ios_wda_url=config.mobile_ios_wda_url or None,
    )
    await reg.boot_probe()
    return reg


async def _start_desktop_observers(state: State) -> None:
    """Spawn per-session perception observers for the host platform.

    macOS: AX observer (focus / value / window-state on the active app).
    Linux and FreeBSD: AT-SPI observer (desktop-wide via Atspi.EventListener).
    Windows: WinEvent observer (desktop-wide via SetWinEventHook).

    Failures are non-fatal — they log a warning and the MCP starts
    without the observer. The deltas channel just stays silent for
    that source until the underlying problem (missing entitlement,
    framework not installed) is fixed.
    """
    if not state.config.deltas_enabled:
        log.info("Delta channel disabled (CONDUCTOR_DESKTOP_DELTAS=false); "
                 "skipping desktop observer startup.")
        return
    if sys.platform == "darwin":
        try:
            from .core.perception.deltas.macos_observer import MacOSObserver
            obs = MacOSObserver(asyncio.get_running_loop())
            await obs.start()
            state.desktop_observers.append(obs)
            log.info("macOS AX delta observer started.")
        except Exception as e:
            log.warning("macOS AX delta observer failed to start (%s); "
                        "deltas will be CDP-only.", e, exc_info=True)
    elif sys.platform == "win32":
        try:
            from .core.perception.deltas.windows_observer import WindowsObserver
            obs = WindowsObserver(asyncio.get_running_loop())
            await obs.start()
            state.desktop_observers.append(obs)
            log.info("Windows WinEvent delta observer started.")
        except Exception as e:
            log.warning("Windows WinEvent delta observer failed to start (%s); "
                        "deltas will be CDP-only.", e, exc_info=True)
    elif sys.platform.startswith(("linux", "freebsd")):
        # FreeBSD's desktops run the same AT-SPI stack as Linux.
        try:
            from .core.perception.deltas.linux_observer import LinuxObserver
            obs = LinuxObserver(asyncio.get_running_loop())
            await obs.start()
            state.desktop_observers.append(obs)
            log.info("Linux AT-SPI delta observer started.")
        except Exception as e:
            log.warning("Linux AT-SPI delta observer failed to start (%s); "
                        "deltas will be CDP-only.", e, exc_info=True)
        # sway/Hyprland workspace IPC observer. Silently
        # no-ops on hosts without an i3-style IPC (labwc/Wayfire/river/
        # Cosmic, plus every X11 / GNOME / KDE session); it's safe to
        # spawn alongside LinuxObserver unconditionally.
        try:
            from .core.perception.deltas.wlroots_observer import WlrootsObserver
            wobs = WlrootsObserver(asyncio.get_running_loop())
            await wobs.start()
            state.desktop_observers.append(wobs)
            log.info("Wlroots (sway/Hyprland) delta observer started.")
        except Exception as e:
            log.warning("Wlroots delta observer failed to start (%s); "
                        "sway/Hyprland workspace events will be silent.", e,
                        exc_info=True)

    # Optional diagnostic loop. Off by default; turn on with
    # CONDUCTOR_DESKTOP_DELTA_DIAG=true when investigating memory growth.
    if state.config.delta_diag:
        state.delta_diag_task = asyncio.create_task(
            _run_delta_diag(state), name="delta-diag",
        )


async def _run_delta_diag(state: State, interval_s: float = 5.0) -> None:
    """Periodic snapshot of delta-channel queues + process RSS.

    Logs one line every interval_s with: rss, asyncio loop._ready depth,
    total live tasks, wall-clock sleep error (interval drift), and per-
    observer (buffer length, raw events since last tick).

    Read the line as a 4-way decision tree on what's growing:
    - rss climbs but loop._ready and obs.buf stay flat → leak is outside
      the delta channel (PIL caches, KB retriever, etc.). Bisect elsewhere.
    - loop._ready climbs unbounded → call_soon_threadsafe back-pressure;
      AX thread is producing faster than the asyncio loop drains. Each
      pending callback retains a StateDelta + bridged NSStrings.
    - obs.buf pegged at buffer_max with high raw+= per tick → ring is
      cycling correctly, leak is downstream (likely the loop._ready case).
    - slept ≫ interval_s → loop was blocked for that long. Combined with
      growing _ready, confirms the back-pressure hypothesis.

    The `loop._ready` access is private API. Justified for diag-only use:
    asyncio has no public way to introspect the ready queue, and the
    attribute name has been stable since 3.4. Wrapped in getattr() so a
    rename in a future Python version degrades to "ready=?" not a crash.
    """
    try:
        import psutil
        proc = psutil.Process()
    except Exception:
        log.debug("[delta-diag] psutil unavailable; rss not reported", exc_info=True)
        proc = None
    loop = asyncio.get_running_loop()
    last_wall = time.monotonic()
    last_raw: dict[int, int] = {}
    # Dedicated file sink — Claude Code's MCP transport stops capturing
    # stderr after connection-established, so periodic log lines vanish.
    diag_path = state.config.delta_diag_file
    try:
        # Long-lived diag handle: opened once at startup and owned for the
        # life of the process, so neither a context manager nor a thread
        # hop buys anything here.
        diag_fh = open(diag_path, "a", buffering=1)  # noqa: ASYNC230, SIM115
        print(f"[delta-diag] pid={os.getpid()} starting interval={interval_s:.1f}s",
              file=diag_fh)
    except OSError as e:
        log.warning("[delta-diag] could not open %s: %s — falling back to stderr only",
                    diag_path, e)
        diag_fh = None
    log.info("[delta-diag] started (interval=%.1fs, file=%s)", interval_s, diag_path)
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            log.info("[delta-diag] stopped")
            if diag_fh is not None:
                try:
                    print("[delta-diag] stopped", file=diag_fh)
                    diag_fh.close()
                except OSError:
                    pass
            return
        now = time.monotonic()
        slept = now - last_wall
        last_wall = now
        rss_mb = proc.memory_info().rss / (1024 * 1024) if proc is not None else -1
        ready = len(getattr(loop, "_ready", ()))
        ntasks = len(asyncio.all_tasks(loop))
        parts = [
            f"rss={rss_mb:.0f}MB",
            f"loop._ready={ready}",
            f"tasks={ntasks}",
            f"slept={slept:.2f}s",
        ]
        for obs in state.desktop_observers:
            raw = obs.raw_seen
            buf = len(getattr(obs, "_buffer", []))
            delta = raw - last_raw.get(id(obs), raw)
            last_raw[id(obs)] = raw
            parts.append(f"{type(obs).__name__}(buf={buf} raw+={delta})")
        line = " ".join(parts)
        log.info("[delta-diag] %s", line)
        if diag_fh is not None:
            try:
                print(f"{time.strftime('%H:%M:%S')} {line}", file=diag_fh)
            except OSError:
                pass


def _report_permission_failure(perms) -> None:
    """Log the permission report to stderr in human-readable form.

    stderr surfaces through MCP server logs, giving the user an actionable
    message even when the server dies before accepting a single tool call.
    """
    msg = [
        "",
        "=" * 70,
        "conductor-desktop: host permissions are NOT OK.",
        "=" * 70,
    ]
    if not perms.screenshot_works:
        msg.append(f"  Screenshot: FAIL — {perms.screenshot_detail}")
    if not perms.input_works:
        msg.append(f"  Input:      FAIL — {perms.input_detail}")
    if not perms.semantic_works and perms.semantic_detail:
        msg.append(f"  Backend:    {perms.semantic_detail}")
    if sys.platform == "darwin":
        from .check_permissions import macos_grant_hint
        msg.extend(["", *macos_grant_hint()])
    msg.extend([
        "",
        "After granting permissions in System Settings (macOS) or fixing the",
        "backend/group membership (Linux), fully quit and relaunch the terminal",
        "(or Claude Code / MCP host) that spawns this server — new permissions",
        "only apply to newly-launched processes.",
        "",
        "To run the same check standalone at any time:",
        "    python -m conductor_desktop.check_permissions",
        "=" * 70,
        "",
    ])
    print("\n".join(msg), file=sys.stderr, flush=True)


async def _shutdown(state: State) -> None:
    # Cancel diag loop first so its log line during teardown doesn't
    # race the observer-stop messages.
    if state.delta_diag_task is not None and not state.delta_diag_task.done():
        state.delta_diag_task.cancel()
        try:
            await state.delta_diag_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("delta diag task error", exc_info=True)
        state.delta_diag_task = None
    # Stop perception observers first — they hold OS-level subscriptions
    # (AX runloop sources, etc.) that should be released before the rest
    # of the state tears down. gather + return_exceptions so one bad
    # observer doesn't prevent the others from stopping.
    if state.desktop_observers:
        results = await asyncio.gather(
            *(obs.stop() for obs in state.desktop_observers),
            return_exceptions=True,
        )
        for obs, result in zip(state.desktop_observers, results):
            if isinstance(result, Exception):
                log.warning("desktop observer stop error (%s): %s",
                            type(obs).__name__, result)
        state.desktop_observers.clear()
    # End browser sessions: Firefox keeps a BiDi session alive after its
    # socket closes and refuses new ones until restart. Lazy import —
    # tools.web imports this module.
    try:
        from .tools.web import close_browsers
        await close_browsers()
    except Exception as e:
        log.warning("browser session close error: %s", e, exc_info=True)
    try:
        await state.executor.disconnect()
    except Exception as e:
        log.warning("executor disconnect error: %s", e, exc_info=True)
    try:
        # grounder / describe_grounder / region_grounder may be up to three
        # distinct clients, or any two may alias (region aliases describe under
        # Holo, or aliases ground otherwise; describe aliases ground in the
        # single-server case). Close each underlying client exactly once.
        seen: list[Grounder] = []
        for g in (state.grounder, state.describe_grounder, state.region_grounder):
            if not any(g is s for s in seen):
                seen.append(g)
                await g.close()
    except Exception as e:
        log.warning("grounder close error: %s", e, exc_info=True)
    if state.enumerator is not None:
        try:
            await state.enumerator.close()
        except Exception as e:
            log.warning("enumerator close error: %s", e, exc_info=True)
    try:
        state.lock.release()
    except Exception as e:
        log.warning("desktop lock release error: %s", e, exc_info=True)


async def _serve_async(config: MCPConfig, lock: DesktopLock,
                       shutdown_event: asyncio.Event | None = None,
                       tray_state: TrayStateMachine | None = None) -> None:
    """Build state, run the MCP transport, drive shutdown via event or stdin EOF.

    `shutdown_event`, if provided, is awaited concurrently with the MCP
    server task; setting it from another thread (via
    ``loop.call_soon_threadsafe(event.set)``) cancels the transport and
    cleans up. If None, the function only returns when the transport
    completes naturally (e.g. stdio EOF).

    `tray_state`, if provided, receives lifecycle events from the
    tool wrapper (tooling.py) so the tray icon and recent-calls submenu stay
    live. None for headless / spectator runs.
    """
    started_at = time.time()
    st = await _build_state(config, lock)
    st.tray_state = tray_state
    _state.install(st, started_at=started_at)
    mcp = build_mcp(config)
    log.info(
        "conductor-desktop ready: role=%s backend=%s grounder=%s describe=%s transport=%s%s "
        "auto_screenshot=%s text_only=%s",
        "holder" if lock.is_held() else "spectator",
        config.backend, config.grounder_url or "not configured",
        config.effective_describe_url or "not configured", config.transport,
        f" ({config.host}:{config.port})" if config.transport != "stdio" else "",
        config.auto_screenshot, config.text_only,
    )
    if not lock.is_held():
        info = lock.read_holder_info()
        if info is not None:
            log.warning(
                "spectator: input tools disabled — holder is pid=%s, started=%s, "
                "transport=%s. %s still work.",
                info.pid, info.started_at, info.transport, tooling.READ_ONLY_NOTE,
            )
        else:
            log.warning("spectator: input tools disabled — no holder metadata on disk")
    serves_http = config.transport != "stdio" or config.dashboard
    if serves_http and not is_loopback(config.host):
        # config.from_env refuses this bind without a token.
        log.warning(
            "⚠ listening on %s over plain HTTP: anyone holding the auth token "
            "can drive this desktop, and the token crosses the network "
            "unencrypted. Bind to 127.0.0.1 and tunnel over SSH unless the "
            "network is trusted.",
            config.host,
        )
    if serves_http and config.auth_token:
        log.info("HTTP auth on: every route needs the bearer token")
    # Mount the dashboard if enabled (CONDUCTOR_DESKTOP_DASHBOARD). Two paths:
    # - HTTP/SSE: register on the FastMCP uvicorn via @mcp.custom_route
    #   so the dashboard shares the transport's port.
    # - stdio: start a sidecar uvicorn (since stdio doesn't run any
    #   HTTP server). The sidecar binds config.host:config.port using
    #   the same Starlette app shape, so the tray's "Open dashboard"
    #   URL works in either mode.
    dashboard_sidecar_task: asyncio.Task | None = None
    if config.dashboard:
        try:
            from .dashboard.routes import register as _register_dashboard
            if config.transport in ("http", "sse"):
                _register_dashboard(mcp)
            else:
                dashboard_sidecar_task = asyncio.create_task(
                    _run_dashboard_sidecar(config),
                    name="dashboard-sidecar",
                )
        except Exception:
            log.exception("dashboard mount failed; continuing without dashboard")

    try:
        if config.transport == "stdio":
            transport_coro = mcp.run_stdio_async()
        else:
            transport_coro = _serve_http(mcp, config)

        if shutdown_event is None:
            await transport_coro
            return

        server_task = asyncio.create_task(transport_coro, name="mcp-transport")
        shutdown_task = asyncio.create_task(shutdown_event.wait(), name="mcp-shutdown")
        # The dashboard sidecar is a *monitoring* task — it must not drive
        # transport teardown. A spectator (second conductor-desktop on the same
        # box) sees the sidecar fail to bind 8765 (the holder owns it),
        # _run_dashboard_sidecar logs a warning and returns; if this task
        # were in the FIRST_COMPLETED wait set, its early completion would
        # cancel the MCP transport and CC would report "MCP server failed."
        done, pending = await asyncio.wait(
            {server_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Surface any transport exception that wasn't a clean exit.
        if server_task in done:
            exc = server_task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        if dashboard_sidecar_task is not None and not dashboard_sidecar_task.done():
            dashboard_sidecar_task.cancel()
            try:
                await dashboard_sidecar_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning("dashboard sidecar error", exc_info=True)
        if (st := _state.get()) is not None:
            _state.install(None)
            await _shutdown(st)


def _guarded(app, config: MCPConfig):
    """`app` behind the bearer-token check when a token is configured."""
    return BearerAuth(app, config.auth_token) if config.auth_token else app


async def _serve_http(mcp: FastMCP, config: MCPConfig) -> None:
    """FastMCP's run_sse_async / run_streamable_http_async, with the app
    wrapped in the auth check (FastMCP's own auth hook is OAuth-shaped)."""
    import uvicorn

    app = mcp.sse_app() if config.transport == "sse" else mcp.streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(
        _guarded(app, config), host=mcp.settings.host, port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    ))
    await server.serve()


async def _run_dashboard_sidecar(config: MCPConfig) -> None:
    """Run a uvicorn instance hosting just the dashboard.

    Used when MCP transport is stdio (no shared uvicorn available).
    Binds ``config.host:config.port``; logs a warning and exits cleanly
    if the bind fails (e.g. some other process already owns the port).

    Cancellation: callers cancel this task to shut the sidecar down.
    We translate cancellation into ``uvi_server.should_exit = True`` so
    uvicorn drains its lifespan + worker tasks instead of being torn
    down mid-flight (which leaks "Task was destroyed but it is pending"
    warnings).
    """
    import socket

    import uvicorn

    from .dashboard.routes import build_starlette_app

    app = build_starlette_app()
    bind_host = config.host or "127.0.0.1"
    bind_port = config.port or 8765

    # Pre-check the port. uvicorn's own bind failure path varies by version
    # (some swallow the OSError after a noisy log line, some re-raise) and
    # produces a confusing ERROR: in stderr that makes Claude Code's MCP
    # launcher look like it's failing. A throwaway socket is deterministic
    # and silent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        probe.bind((bind_host, bind_port))
    except OSError as e:
        log.warning(
            "dashboard sidecar skipped: port %s:%d already in use (%s) — "
            "tray menu's Open dashboard will not work in this instance. "
            "Set CONDUCTOR_DESKTOP_PORT to a free port if you want to run "
            "two conductor-desktop processes side-by-side.",
            bind_host, bind_port, e,
        )
        probe.close()
        return
    finally:
        probe.close()

    uvi_config = uvicorn.Config(
        _guarded(app, config),
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
        # No startup/shutdown handlers on the dashboard app, and turning
        # lifespan on holds the bind behind a startup handshake some
        # ASGI runtimes don't complete cleanly when the parent loop is
        # already busy (FastMCP stdio).
        lifespan="off",
    )
    uvi_server = uvicorn.Server(uvi_config)
    log.info(
        "dashboard sidecar starting on http://%s:%d/dashboard",
        bind_host, bind_port,
    )
    serve_task = asyncio.create_task(uvi_server.serve(),
                                     name="dashboard-uvicorn-serve")
    try:
        await serve_task
    except asyncio.CancelledError:
        # Graceful uvicorn shutdown: flag, give it a moment, then await.
        uvi_server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            serve_task.cancel()
        except Exception:
            log.debug("dashboard sidecar shutdown failed", exc_info=True)
            serve_task.cancel()
        raise
    except OSError as e:
        log.warning(
            "dashboard sidecar failed to bind %s:%d (%s) — tray menu's "
            "Open dashboard will not work until a different port is "
            "set via CONDUCTOR_DESKTOP_PORT.",
            bind_host, bind_port, e,
        )


def run() -> None:
    """Entry point — read env, acquire desktop lock, branch on holder role."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if LEGACY_ENV_RENAMED:
        log.warning("Deprecated: %s still set; rename to %s* (read as the new names for now).",
                    ", ".join(sorted(LEGACY_ENV_RENAMED)), ENV_PREFIX)
    config = MCPConfig.from_env()

    # Fix up stale DISPLAY / XAUTHORITY from the MCP launcher's pinned env
    # BEFORE pystray, the X11 backend, or any Xlib call sees them. GDM
    # commonly re-numbers across reboots and the resulting "Can't connect
    # to display ":1"" used to crash the whole server inside pystray's
    # import-time Display(). See conductor_desktop/linux_env.py.
    from .linux_env import autodetect_x11_env
    autodetect_x11_env()

    # Acquire the global desktop input lock as early as possible. First-come-
    # first-serve: only the holder may inject input. Spectators still serve
    # read-only tools and surface a clear error from input-mutating ones.
    lock = DesktopLock(config.lock_path or None, transport=config.transport)
    is_holder = lock.try_acquire()

    if is_holder:
        # Holder owns the system tray icon (Quit releases the lock + exits).
        # tray.run_holder runs the asyncio loop on a worker thread so pystray
        # can own the main thread (required by macOS NSApplication).
        from .tray import run_holder
        run_holder(config, lock)
    else:
        # Spectator: run the MCP loop on the main thread; no tray.
        asyncio.run(_serve_async(config, lock))
