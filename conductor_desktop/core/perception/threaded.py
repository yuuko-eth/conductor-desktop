"""The observer that owns a platform thread.

macOS (a CFRunLoop) and Linux (a GLib mainloop) both need a thread of their
own: the platform delivers its events by running a loop, and neither loop can
share the asyncio thread. `ThreadedObserver` owns that lifecycle — spawn,
wait for the thread to say it is ready, join on stop — and leaves three hooks
to the platform:

    _thread_setup()    register with the platform; raise to fail `start()`
    _thread_serve()    run the platform loop until `_signal_stop` is honoured
    _thread_teardown() unregister; always runs, even after a serve crash
    _signal_stop()     ask the loop to return, from the asyncio thread

The subtle part is restart. `start()` resets the readiness event and the
recorded error before spawning, and drops `_thread` on every failure path, so
a caller that retries after a failed start gets a real attempt rather than the
first attempt's stale verdict. Platforms with their own stop flag reset it in
`_reset_for_start`.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from .observer_base import AsyncObserver

log = logging.getLogger("conductor.perception")

# How long `start()` waits for the thread to report readiness. Setup is a
# handful of local registrations on both platforms; 5 s is already generous,
# and a miss means something is wedged, not slow.
START_TIMEOUT_SECONDS = 5.0

# How long `stop()` waits for the thread to return. The threads are daemons,
# so a wedged one can't hold up process exit either way.
JOIN_TIMEOUT_SECONDS = 3.0


class ThreadedObserver(AsyncObserver):
    """An `AsyncObserver` whose events arrive on a platform loop thread."""

    #: Thread name, for `ps` / a stack dump. Subclasses override.
    thread_name = "conductor-observer"

    def __init__(self, loop: asyncio.AbstractEventLoop, **kw) -> None:
        super().__init__(loop, **kw)
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._start_error: BaseException | None = None

    # ── Lifecycle (asyncio side) ────────────────────────────────────

    async def start(self) -> None:
        if self._thread is not None:
            return
        # Restart: everything the last run left behind goes now, or the
        # readiness wait below returns instantly on a stale event and we
        # report a thread that is already dead as started.
        self._ready_event.clear()
        self._start_error = None
        self._reset_for_start()
        self._thread = threading.Thread(
            target=self._run, name=self.thread_name, daemon=True,
        )
        self._thread.start()
        ready = await asyncio.get_running_loop().run_in_executor(
            None, self._ready_event.wait, START_TIMEOUT_SECONDS,
        )
        if not ready:
            self._forget_thread()
            raise RuntimeError(
                f"{type(self).__name__} thread failed to start within "
                f"{START_TIMEOUT_SECONDS:g}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._forget_thread()
            raise RuntimeError(f"{type(self).__name__} init failed: {error}")

    async def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._signal_stop()
        # Join with a timeout — daemon=True means even a stuck thread won't
        # block process exit.
        await asyncio.get_running_loop().run_in_executor(
            None, thread.join, JOIN_TIMEOUT_SECONDS,
        )
        self._forget_thread()

    def _forget_thread(self) -> None:
        """Drop the thread handle so the next `start()` is a real attempt.

        Runs on every exit path, failures included: leaving `_thread` set
        after a failed start makes the guard at the top of `start()` return
        success for a thread that never came up.
        """
        self._signal_stop()
        self._thread = None
        self._release()

    # ── Observer thread ─────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._thread_setup()
        except BaseException as e:  # the error is reported through start()
            log.exception("%s thread setup failed", type(self).__name__)
            self._start_error = e
            self._ready_event.set()
            return
        self._ready_event.set()
        try:
            self._thread_serve()
        except Exception:
            log.exception("%s loop crashed (observer terminating)",
                          type(self).__name__)
        finally:
            try:
                self._thread_teardown()
            except Exception:
                log.exception("%s teardown errored", type(self).__name__)

    # ── Hooks ───────────────────────────────────────────────────────

    def _reset_for_start(self) -> None:
        """Clear per-run state before the thread spawns. Override when the
        platform keeps its own stop flag."""

    def _thread_setup(self) -> None:
        """Register with the platform, on the thread. Raise to fail start()."""
        raise NotImplementedError

    def _thread_serve(self) -> None:
        """Run the platform loop until `_signal_stop` is honoured."""
        raise NotImplementedError

    def _thread_teardown(self) -> None:
        """Unregister. Runs on the thread, after serve, always."""

    def _signal_stop(self) -> None:
        """Ask the loop to return. Called from the asyncio thread, and safe
        to call when nothing is running."""
        raise NotImplementedError

    def _release(self) -> None:
        """Drop process-wide registrations the observer holds. Called from
        the asyncio thread once the thread is gone."""
