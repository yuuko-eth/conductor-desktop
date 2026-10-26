"""Tests for perception.threaded — the lifecycle the macOS and Linux
observers share.

Cross-platform on purpose: the two real subclasses are behind platform gates
(`test_perception_macos_observer.py`, `test_perception_atspi_observer.py`), so
a macOS run exercises only the macOS one and nothing at all checks the Linux
lifecycle until it reaches a Linux host. The lifecycle is the same code for
both, and it is tested here against a fake platform loop.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from conductor_desktop.core.perception import threaded
from conductor_desktop.core.perception.threaded import ThreadedObserver


class _FakeThreaded(ThreadedObserver):
    """A threaded observer whose 'platform loop' is an Event wait."""

    thread_name = "fake-observer"

    def __init__(self, loop, *, fail_setup: BaseException | None = None,
                 hang_setup: bool = False, **kw):
        super().__init__(loop, **kw)
        self.fail_setup = fail_setup
        self.hang_setup = hang_setup
        self._stop_event = threading.Event()
        self.setups = 0
        self.serves = 0
        self.teardowns = 0
        self.releases = 0
        self.served = threading.Event()

    def _reset_for_start(self) -> None:
        self._stop_event.clear()

    def _signal_stop(self) -> None:
        self._stop_event.set()

    def _release(self) -> None:
        self.releases += 1

    def _thread_setup(self) -> None:
        self.setups += 1
        if self.hang_setup:
            # Never reports ready — the start() timeout path.
            self._stop_event.wait(30)
        if self.fail_setup is not None:
            raise self.fail_setup

    def _thread_serve(self) -> None:
        self.serves += 1
        self.served.set()
        self._stop_event.wait(10)

    def _thread_teardown(self) -> None:
        self.teardowns += 1


def _observer(**kw) -> _FakeThreaded:
    return _FakeThreaded(asyncio.get_running_loop(), **kw)


# ── The happy path ──────────────────────────────────────────────────

async def test_start_runs_setup_then_serve():
    obs = _observer()
    await obs.start()
    assert obs.served.wait(5)
    assert (obs.setups, obs.serves) == (1, 1)
    await obs.stop()
    assert obs.teardowns == 1
    assert obs._thread is None


async def test_double_start_is_idempotent():
    obs = _observer()
    await obs.start()
    first = obs._thread
    await obs.start()
    assert obs._thread is first
    assert obs.setups == 1
    await obs.stop()


async def test_stop_without_start_is_safe():
    obs = _observer()
    await obs.stop()  # must not raise
    assert obs.teardowns == 0


async def test_teardown_runs_even_when_serve_raises():
    obs = _observer()

    def boom():
        raise RuntimeError("the platform loop died")
    obs._thread_serve = boom

    await obs.start()
    await obs.stop()
    assert obs.teardowns == 1


# ── Failure paths ───────────────────────────────────────────────────

async def test_start_propagates_setup_error():
    obs = _observer(fail_setup=RuntimeError("simulated typelib missing"))
    with pytest.raises(RuntimeError, match="simulated typelib missing"):
        await obs.start()
    assert obs.serves == 0


async def test_start_after_a_failed_start_tries_again():
    # The failed start must not leave _thread set: the guard at the top of
    # start() would then report success for a thread that never came up.
    obs = _observer(fail_setup=RuntimeError("no framework"))
    with pytest.raises(RuntimeError, match="no framework"):
        await obs.start()
    assert obs._thread is None

    obs.fail_setup = None
    await obs.start()
    assert obs.served.wait(5)
    assert obs.setups == 2
    await obs.stop()


async def test_a_failed_start_is_not_replayed_on_the_next_one():
    # Nothing used to clear _start_error, so the second start() re-raised
    # the first one's error without the thread having failed at all.
    obs = _observer(fail_setup=RuntimeError("transient"))
    with pytest.raises(RuntimeError):
        await obs.start()
    obs.fail_setup = None
    await obs.start()  # must not raise the stale error
    await obs.stop()


async def test_start_times_out_when_setup_never_reports_ready(monkeypatch):
    monkeypatch.setattr(threaded, "START_TIMEOUT_SECONDS", 0.2)
    obs = _observer(hang_setup=True)
    with pytest.raises(RuntimeError, match="failed to start within"):
        await obs.start()
    # And the wedged attempt doesn't block a later one.
    assert obs._thread is None


# ── Restart ─────────────────────────────────────────────────────────

async def test_restart_actually_restarts():
    # The readiness event and the platform's stop flag both survived a
    # stop(), so after stop() → start() the wait returned instantly and the
    # new thread's loop exited at once: the observer reported started and
    # was dead.
    obs = _observer()
    await obs.start()
    assert obs.served.wait(5)
    await obs.stop()

    obs.served.clear()
    await obs.start()
    assert obs.served.wait(5), "the second run never reached its serve loop"
    assert obs.setups == 2
    assert obs.serves == 2
    await obs.stop()
    assert obs.teardowns == 2


async def test_restart_emits_again():
    from conductor_desktop.core.perception.classifier import StateDelta

    obs = _observer()
    await obs.start()
    await obs.stop()
    await obs.start()
    obs._emit(StateDelta(kind="focus_changed", source="ax", monotonic_ts=0.0))
    await asyncio.sleep(0)
    assert len(obs.drain()) == 1
    await obs.stop()


async def test_stop_releases_process_wide_registrations():
    obs = _observer()
    await obs.start()
    await obs.stop()
    assert obs.releases == 1
