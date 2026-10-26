"""Wlroots Wayland delta observer (sway / Hyprland IPC).

Subscribes to the compositor's structured event stream to emit
``view_changed`` deltas when the focused workspace transitions. Unlike
the macOS / linux-AT-SPI / X11 observers — which bridge native C
callbacks onto a GLib or CFRunLoop daemon thread — wlroots IPC is
plain line-based stdio (sway) or a unix socket (Hyprland). An asyncio
task is sufficient; no daemon thread, no ``call_soon_threadsafe``
bridge, no run-loop interleaving.

Three runtime paths:

1. **sway** — ``swaymsg -t subscribe -m '["workspace"]'`` streams
   newline-delimited JSON. Each event has ``change`` ∈ {focus, init,
   empty, rename, move, urgent, reload}; we emit only on ``focus``.
2. **Hyprland** — connects to ``$XDG_RUNTIME_DIR/hypr/$HIS/.socket2.sock``
   and reads ``event>>data\\n`` text lines. We emit on
   ``workspace>>`` and ``workspacev2>>`` events.
3. **neither** (labwc / Wayfire / river / Cosmic / other wlroots
   compositors without i3-style IPC) — silent no-op. ``start()``
   returns cleanly, the observer just never emits. The
   wlr-foreign-toplevel-management fallback in
   ``backends/linux_wlroots.py`` is the same shape: code present,
   features degraded to defaults.

GNOME-Wayland is covered by ``linux_observer.LinuxObserver`` (4a-iii
GNOME workspace poll); KDE-Plasma-Wayland is its own future slice
(``kwin`` D-Bus signals, not wlroots IPC). This observer is *only*
sway/Hyprland.

Delta shape matches every other view_changed source:
``source_id="workspace-<ident>"``, ``origin_scope.space_id="ws:<output>:<ident>"``
— the latter matching ``scene_graph._space_id_for_window`` so the brain
can take a delta and ``get_scene_graph()`` straight into the
corresponding ``SpaceNode``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from ..observer_base import AsyncObserver
from ..view import SpaceTracker, build_view_changed

log = logging.getLogger("conductor.perception.wlroots")


# ── Compositor detection ────────────────────────────────────────────


def _detect_compositor() -> str | None:
    """Return "sway", "hyprland", or None.

    Detection order matches user prevalence: sway → Hyprland → none.
    Both compositors expose env vars at session start (``SWAYSOCK`` and
    ``HYPRLAND_INSTANCE_SIGNATURE`` respectively); we trust those over
    ``$WAYLAND_DISPLAY`` alone since the latter is set by any Wayland
    session including KDE-Plasma, GNOME-Wayland, labwc, and Wayfire.
    """
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        return "sway"
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    return None


def _hyprland_socket_path() -> str | None:
    """Resolve the Hyprland socket2 path. None if HIS is unset or the
    socket file is missing.

    Newer Hyprland (≥ 0.40) puts the socket under XDG_RUNTIME_DIR;
    older versions used /tmp. Try both.
    """
    his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not his:
        return None
    candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(os.path.join(xdg, "hypr", his, ".socket2.sock"))
    candidates.append(os.path.join("/tmp", "hypr", his, ".socket2.sock"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


# ── Observer ────────────────────────────────────────────────────────


class WlrootsObserver(AsyncObserver):
    """Subscribe to sway/Hyprland workspace IPC; emit view_changed.

    Lifecycle:
        obs = WlrootsObserver(asyncio.get_running_loop())
        await obs.start()       # detect compositor, spawn task
        ...
        await obs.stop()        # cancel task, terminate subprocess /
                                # close socket

    On hosts running labwc / Wayfire / river / Cosmic / anything
    without sway- or Hyprland-style IPC, ``start()`` returns
    successfully but the observer never emits. This mirrors the
    fallback pattern in ``backends/linux_wlroots.py`` — code present,
    features absent. The wlroots-protocol-based per-toplevel events
    are not enough for workspace-transition detection without a per-
    compositor adapter; see ``https://docs.conductor-mcp.io/concepts/scene-graph/`` for the
    pywayland-client follow-up that would cover this gap uniformly.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, **kw) -> None:
        super().__init__(loop, **kw)
        self._task: asyncio.Task | None = None
        # Subprocess handle for sway path; None on Hyprland / no-op.
        self._sway_proc: asyncio.subprocess.Process | None = None
        # Stream handles for Hyprland path; None otherwise.
        self._hypr_reader: asyncio.StreamReader | None = None
        self._hypr_writer: asyncio.StreamWriter | None = None
        # Compositor detected at start time; None means "no-op observer".
        self._compositor: str | None = None
        # Last workspace identity we emitted on. Per-output workspaces
        # are stringly-keyed; we key on ``ws:<output>:<ident>`` so a
        # workspace named "2" on two different outputs stays distinct.
        # None until first event seen.
        self._spaces = SpaceTracker()

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return  # idempotent
        self._compositor = _detect_compositor()
        if self._compositor is None:
            log.info(
                "Wlroots observer: no sway/Hyprland IPC detected "
                "(SWAYSOCK / HYPRLAND_INSTANCE_SIGNATURE unset). "
                "Silent no-op — labwc/Wayfire/river/Cosmic have no "
                "structured workspace-event source today."
            )
            return

        if self._compositor == "sway":
            self._task = asyncio.create_task(
                self._run_sway(), name="conductor-wlroots-sway-observer",
            )
        else:  # hyprland
            self._task = asyncio.create_task(
                self._run_hyprland(), name="conductor-wlroots-hyprland-observer",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("wlroots observer task ended with error", exc_info=True)
        self._task = None

        # Subprocess cleanup (sway). Terminate is sufficient — swaymsg
        # exits on SIGTERM, and the pipe close shortly after.
        if self._sway_proc is not None:
            try:
                if self._sway_proc.returncode is None:
                    self._sway_proc.terminate()
                    await asyncio.wait_for(self._sway_proc.wait(), timeout=2.0)
            except (TimeoutError, ProcessLookupError):
                # Already gone or hung; best-effort kill.
                try:
                    self._sway_proc.kill()
                except OSError:
                    pass
            except Exception:
                log.debug("swaymsg cleanup errored", exc_info=True)
            self._sway_proc = None

        # Unix socket cleanup (Hyprland).
        if self._hypr_writer is not None:
            try:
                self._hypr_writer.close()
                await self._hypr_writer.wait_closed()
            except Exception:
                log.debug("hyprland socket cleanup errored", exc_info=True)
            self._hypr_writer = None
            self._hypr_reader = None

    # ── sway path ───────────────────────────────────────────────────

    async def _run_sway(self) -> None:
        """Subscribe to sway workspace events.

        ``swaymsg -t subscribe -m '["workspace"]'`` writes one JSON
        object per line, no trailing comma, no array wrapper. Stays
        connected until killed. Stays on stderr-silent so log lines
        don't intermix with diag output.
        """
        try:
            self._sway_proc = await asyncio.create_subprocess_exec(
                "swaymsg", "-t", "subscribe", "-m", "[\"workspace\"]",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.warning("swaymsg vanished between detection and exec; no-op")
            return
        except Exception:
            log.exception("swaymsg subscribe failed to spawn")
            return

        assert self._sway_proc.stdout is not None
        log.info("Wlroots observer attached (sway, pid=%d)", self._sway_proc.pid)
        try:
            while True:
                line = await self._sway_proc.stdout.readline()
                if not line:
                    rc = self._sway_proc.returncode
                    log.info("swaymsg subscribe stream closed (rc=%s)", rc)
                    return
                try:
                    payload = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.debug("swaymsg malformed line dropped: %r", line[:120])
                    continue
                self._handle_sway_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Catch-all so a transient parse error doesn't kill the
            # subscription. Drop the line, keep listening.
            log.exception("sway event loop errored; observer terminating")

    def _handle_sway_event(self, payload: dict) -> None:
        """Normalize a sway workspace event into a view_changed delta.

        sway emits one event per workspace state change; we filter to
        ``change == "focus"`` because that's the destination-transition
        signal. ``init`` fires on workspace creation (not a transition);
        ``empty`` on the last window leaving; ``move`` on output reattach.
        """
        if payload.get("change") != "focus":
            return
        current = payload.get("current") or {}
        old = payload.get("old") or {}

        output = current.get("output") or "?"
        ident = current.get("name") if current.get("name") is not None \
            else str(current.get("num", 0))
        new_space_id = f"ws:{output}:{ident}"

        prev_output = old.get("output") if old else None
        prev_ident = None
        if old:
            prev_ident = old.get("name") if old.get("name") is not None \
                else str(old.get("num", 0))
        prev_space_id = (
            f"ws:{prev_output}:{prev_ident}"
            if prev_output is not None and prev_ident is not None else None
        )

        self._emit_workspace_changed(
            new_space_id=new_space_id,
            prev_space_id=prev_space_id,
            output=output,
            ident=str(ident),
            origin="sway-ipc",
        )

    # ── Hyprland path ───────────────────────────────────────────────

    async def _run_hyprland(self) -> None:
        """Subscribe to Hyprland's socket2 event stream.

        socket2 is a unix-domain stream socket that emits one
        ``event>>data\\n`` line per state change. Workspace events:

          - ``workspace>>NAME`` — focused workspace changed (name only)
          - ``workspacev2>>ID,NAME`` — same, but with workspace ID
          - ``focusedmon>>MONITOR,WORKSPACE`` — monitor focus + workspace

        We emit on ``workspace>>`` and ``workspacev2>>``; the latter is
        preferred when present (id + name).
        """
        path = _hyprland_socket_path()
        if not path:
            log.warning(
                "Hyprland detected (HIS set) but socket2 not found at any "
                "candidate path; no-op",
            )
            return
        try:
            self._hypr_reader, self._hypr_writer = (
                await asyncio.open_unix_connection(path)
            )
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            log.exception("hyprland socket2 connect failed (%s)", path)
            return

        log.info("Wlroots observer attached (Hyprland, socket=%s)", path)
        try:
            while True:
                line = await self._hypr_reader.readline()
                if not line:
                    log.info("hyprland socket2 stream closed")
                    return
                try:
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                except UnicodeError:
                    continue
                self._handle_hyprland_line(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hyprland event loop errored; observer terminating")

    def _handle_hyprland_line(self, text: str) -> None:
        """Parse one ``event>>data`` line from socket2 and emit.

        Hyprland workspaces are per-output but the event stream doesn't
        always include the monitor — we track focusedmon separately
        when present and use it on subsequent workspace events. When
        unknown, output defaults to ``"?"`` (matching the wlroots
        fallback shape elsewhere).
        """
        if ">>" not in text:
            return
        event, _, data = text.partition(">>")
        if event == "focusedmon":
            # data is "MONITOR,WORKSPACE"
            parts = data.split(",", 1)
            if len(parts) == 2:
                mon = parts[0]
                # Emit only the workspace transition — the monitor swap
                # alone isn't a view_changed event (it's the same
                # workspace just being on a different output).
                self._track_hyprland_monitor(mon)
            return
        if event == "workspacev2":
            # data is "ID,NAME"
            parts = data.split(",", 1)
            ident = parts[1] if len(parts) == 2 else parts[0]
        elif event == "workspace":
            ident = data
        else:
            return  # not a workspace event we care about
        output = self._hypr_current_monitor or "?"
        new_space_id = f"ws:{output}:{ident}"
        self._emit_workspace_changed(
            new_space_id=new_space_id,
            prev_space_id=self._spaces.last,
            output=output,
            ident=ident,
            origin="hyprland-socket2",
        )

    # Hyprland tracks the focused monitor in a separate event stream;
    # cache it so workspace events can attribute correctly. Defaults
    # to None until the first focusedmon event.
    @property
    def _hypr_current_monitor(self) -> str | None:
        return getattr(self, "_hypr_mon_cache", None)

    def _track_hyprland_monitor(self, monitor: str) -> None:
        self._hypr_mon_cache = monitor

    # ── Shared emit ─────────────────────────────────────────────────

    def _emit_workspace_changed(
        self,
        *,
        new_space_id: str,
        prev_space_id: str | None,
        output: str,
        ident: str,
        origin: str,
    ) -> None:
        """Emit a view_changed delta. Idempotent on identical
        destinations — repeated events for the same space_id collapse.

        Mirrors macos_observer._emit_space_changed and
        linux_observer._on_workspace in shape so the brain sees the same
        delta format across all platforms.
        """
        changed, last = self._spaces.take(new_space_id)
        if not changed:
            return
        prev = last if prev_space_id is None else prev_space_id
        self._emit(build_view_changed(
            source="wlroots",
            source_id=f"workspace-{ident}",
            space_id=new_space_id,
            prev_space_id=prev,
            origin=origin,
            extra={"output": output},
        ))
