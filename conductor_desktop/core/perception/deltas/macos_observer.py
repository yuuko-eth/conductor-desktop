"""macOS AX delta observer.

Two-layer design:

1. **App tracker** (poll via NSWorkspace) — detects activation changes,
   drives spawn/teardown of (2). Polling chosen over the NSWorkspace
   notification path because mixing CFRunLoop + NSNotificationCenter
   threads is fiddly; ~500ms latency on app switch is acceptable for
   v1, and the per-PID AX subscription itself stays event-driven.

2. **Per-PID AXObserver** — subscribed to focus / value / selection /
   title / window-state notifications on the active app's AX element.
   Native AX events fire on our observer thread's CFRunLoop;
   `_emit()` bridges to the asyncio loop via call_soon_threadsafe.

The runloop-source omission is the silent no-op trap (per omnigroup
thread): every AXObserverAddNotification must be paired with
CFRunLoopAddSource on the same thread's runloop, otherwise callbacks
never fire even though add succeeded.

Retention: strong Python refs to observer / element / callback are
held in `self._app_observers` so PyObjC doesn't GC them across the
C boundary. Tear-down in stop() reverses the order.

TCC: uses the same Accessibility grant as backends/macos.py's AX
tree-walk, so it adds no new permission prompt.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import sys
import threading
import time
from typing import Any

from ...errors import best_effort
from ..classifier import StateDelta
from ..threaded import ThreadedObserver
from ..view import SpaceTracker, build_view_changed

log = logging.getLogger("conductor.perception.macos")


# ── Private CGS bindings for active-Space ID ────────────────────────
# macOS doesn't expose a public Space UUID API. CGSGetActiveSpace +
# CGSMainConnectionID (or legacy _CGSDefaultConnection) are private but
# yabai, Magnet, AltTab et al. have relied on them since 10.10 — stable
# across all versions we care about. Used as a polling fallback for the
# NSWorkspace notification path which only fires on a thread with a
# main NSRunLoop (Claude Desktop yes; CLI Python no).
#
# Failure to resolve any symbol is non-fatal: _cgs_active_space() returns
# None and the poll becomes a no-op. The notification path still works
# when its preconditions are met.

def _load_cgs_lib():
    """Resolve the CGS symbols we need. Returns the loaded CDLL or None."""
    if sys.platform != "darwin":
        return None
    try:
        path = ctypes.util.find_library("CoreGraphics")
        if not path:
            return None
        lib = ctypes.CDLL(path)
        # Pick whichever connection accessor this macOS exports.
        # Modern (Sonoma 14+) prefers CGSMainConnectionID;
        # legacy _CGSDefaultConnection is also still exported.
        if hasattr(lib, "CGSMainConnectionID"):
            lib.CGSMainConnectionID.restype = ctypes.c_int
            lib.CGSMainConnectionID.argtypes = []
        elif hasattr(lib, "_CGSDefaultConnection"):
            lib._CGSDefaultConnection.restype = ctypes.c_int
            lib._CGSDefaultConnection.argtypes = []
        else:
            return None
        if not hasattr(lib, "CGSGetActiveSpace"):
            return None
        lib.CGSGetActiveSpace.restype = ctypes.c_uint64
        lib.CGSGetActiveSpace.argtypes = [ctypes.c_int]
        return lib
    except OSError:
        return None


_CGS_LIB = _load_cgs_lib()


def _cgs_active_space() -> int | None:
    """Return the active Space's 64-bit ID, or None on any failure."""
    if _CGS_LIB is None:
        return None
    try:
        if hasattr(_CGS_LIB, "CGSMainConnectionID"):
            conn = _CGS_LIB.CGSMainConnectionID()
        else:
            conn = _CGS_LIB._CGSDefaultConnection()
        sid = _CGS_LIB.CGSGetActiveSpace(conn)
        return int(sid) if sid else None
    except Exception:
        log.debug("CGSGetActiveSpace failed", exc_info=True)
        return None


# AX framework imports — hoisted to module top because we need
# AXObserverCreate's metadata at decorator time (objc.callbackFor reads it
# to bind the callback's C signature). On non-darwin or missing
# ApplicationServices, these become None and MacOSObserver.start() raises
# a clear error rather than crashing later inside _setup_app_observer.
try:
    import objc
    from ApplicationServices import (
        AXObserverAddNotification,
        AXObserverCreate,
        AXObserverGetRunLoopSource,
        AXObserverRemoveNotification,
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementCreateSystemWide,
        AXUIElementSetMessagingTimeout,
    )
    from CoreFoundation import (
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFRunLoopRemoveSource,
        CFRunLoopRunInMode,
        kCFRunLoopCommonModes,
        kCFRunLoopDefaultMode,
    )
    from Foundation import NSObject
    _AX_AVAILABLE = True
except ImportError as _ax_import_error:
    _AX_AVAILABLE = False
    _ax_import_failure = _ax_import_error


# AX notifications we care about → StateDelta kind. Names are the string
# values of the kAX*Notification constants; we compare by string at
# callback time because PyObjC hands us the notification name as an
# NSString.
_AX_KIND_MAP: dict[str, str] = {
    "AXFocusedUIElementChanged": "focus_changed",
    "AXValueChanged": "value_changed",
    "AXSelectedTextChanged": "selection_changed",
    "AXTitleChanged": "name_changed",
    "AXFocusedWindowChanged": "window_state_changed",
    "AXMainWindowChanged": "window_state_changed",
    "AXApplicationActivated": "window_state_changed",
    "AXWindowCreated": "node_added",
    "AXUIElementDestroyed": "node_removed",
}


# AX notifications we register on the app element for each subscribed PID.
# Some apps don't support every notification — the registration call
# returns non-zero, which we log at debug and continue.
_AX_NOTIFICATIONS_TO_SUBSCRIBE = list(_AX_KIND_MAP.keys())


# Layer 1 — drop noisy roles. AXScrollArea / AXScrollBar fire value-changed
# constantly during scroll; AXMenu / AXMenuBar fire on every menu open
# (signal already covered by node_added if we track it). AXImage value
# changes are usually "the image data changed" which is meaningless.
_AX_SKIP_ROLES_FOR_VALUE: frozenset[str] = frozenset({
    "AXScrollArea",
    "AXScrollBar",
    "AXImage",
    "AXMenuBar",
    "AXMenu",
})


# Roles whose AXValue can be the entire content of a text buffer (Terminal
# scrollback, IDE editor pane, large textarea). Reading those on every
# value_changed tick allocates a multi-KB bridged NSString that fragments
# Python's heap until RSS climbs unbounded (6.7 MB/s of sustained growth
# from an idle Terminal).
#
# We still read AXValue for these roles, but **only emit the value field
# when the resulting string is short** (≤_AX_VALUE_KEEP_MAX). That preserves
# the URL-bar / search-box / single-line-form-field signal (typically
# <150 chars — the brain uses these to learn "user typed X") while
# dropping Terminal scrollback (tens of KB) and IDE buffers (≫KB).
#
# The check is on string length post-bridge, so the worst case is one
# bridged NSString briefly held within this function's scope before being
# released. That's bounded — the leak only occurred when we propagated the
# string into the StateDelta and the buffer queue downstream of it.
_AX_NO_VALUE_READ_FOR: frozenset[str] = frozenset({
    "AXTextArea",
    "AXTextField",
})

# Max chars to retain from AXValue for the roles above. Tuned for URL
# bars (~80 chars), search boxes (~50), and short form fields (<200).
# Anything longer is treated as "buffer content" and dropped.
_AX_VALUE_KEEP_MAX = 256


# How long we wait for one AX round trip before giving up on it. Handling
# one event makes up to six of them, on the same thread as the app-switch
# and Space polling, so the default (6 s, and AX's own, not ours) lets one
# hung app stall every other thing this observer watches. Set on the
# system-wide element, which is what every element this process creates
# inherits, and again per app element.
_AX_MESSAGING_TIMEOUT_SECONDS = 2.0


# How long the app-activation poller sleeps between checks. 500ms is a
# tradeoff: smaller = quicker subscription on app switch but more wakeups;
# larger = misses brief focus excursions. 500ms is roughly the human-
# perception threshold for "snappy."
_POLL_INTERVAL_SECONDS = 0.5


class MacOSObserver(ThreadedObserver):
    """Subscribes per-PID AXObservers to the focused app, normalizes
    events into StateDelta.

    Lifecycle:
        obs = MacOSObserver(asyncio.get_running_loop())
        await obs.start()       # spawns daemon thread w/ CFRunLoop
        ...
        await obs.stop()        # signals stop, joins thread, cleans AX refs

    Errors during start (e.g. ApplicationServices not installed,
    Accessibility not granted) raise RuntimeError on start() rather than
    silently producing no events.
    """

    thread_name = "conductor-ax-observer"

    def __init__(self, loop: asyncio.AbstractEventLoop, **kw) -> None:
        super().__init__(loop, **kw)
        self._cf_runloop: Any = None
        self._stop_event = threading.Event()
        # pid -> (observer_ref, app_element_ref). Strong refs keep them
        # alive across the C boundary; PyObjC GC would otherwise
        # invalidate the AXObserver and silently stop callbacks.
        self._app_observers: dict[int, tuple[Any, Any]] = {}
        self._current_pid: int | None = None
        # PIDs we've already tried and failed to subscribe to (TCC denial,
        # AX-unsupported app, etc). Without this, the polling loop would
        # retry every 500ms forever, drowning the log.
        self._failed_pids: set[int] = set()
        # NSWorkspaceActiveSpaceDidChangeNotification watcher.
        # Strong-held NSObject subclass instance; deregistered on stop().
        self._space_watcher: Any = None
        # Last Space ID we've seen — primed at setup, updated by both the
        # notification path AND the poll path so whichever observes the
        # transition first emits exactly one delta. The two run on
        # different threads (Apple dispatches the notification wherever it
        # likes), so the check-and-set is behind SpaceTracker's lock.
        self._spaces = SpaceTracker()
        # Monotonic counter for diagnostic source_id suffixing when the
        # CGS path is unavailable. When CGS works, source_id uses the
        # destination Space ID so swipe-storms with the same destination
        # coalesce (per LAST_WINS_KINDS + design's "5 swipes → 1 delta").
        self._space_tx_counter = 0

    # ── Lifecycle (ThreadedObserver hooks) ──────────────────────────

    def _reset_for_start(self) -> None:
        # The stop flag survives a stop(), so a restart would find the new
        # thread's `while not self._stop_event.is_set()` already false.
        self._stop_event.clear()
        # Register in the module-level lookup so the C callback can find us.
        _OBSERVERS_BY_ID[id(self)] = self

    def _signal_stop(self) -> None:
        self._stop_event.set()

    def _release(self) -> None:
        _OBSERVERS_BY_ID.pop(id(self), None)

    def _thread_setup(self) -> None:
        if not _AX_AVAILABLE:
            raise RuntimeError(
                f"ApplicationServices framework unavailable: {_ax_import_failure}. "
                f"Install with: uv add 'pyobjc-framework-applicationservices>=12.1'"
            )
        self._cf_runloop = CFRunLoopGetCurrent()
        # Bound every AX round trip this process makes. The system-wide
        # element carries the default the others inherit.
        with best_effort("setting the AX messaging timeout", log):
            AXUIElementSetMessagingTimeout(
                AXUIElementCreateSystemWide(), _AX_MESSAGING_TIMEOUT_SECONDS,
            )
        # Register the NSWorkspace Space-change watcher (best-effort —
        # only fires on a thread with a main NSRunLoop, e.g. when
        # Conductor is launched from Claude Desktop). The polling
        # fallback in _sync_active_app catches the rest.
        self._setup_workspace_observer()
        # Prime the tracker so the first poll doesn't emit a bogus
        # "transition" from None → current Space.
        self._spaces.prime(_cgs_active_space())

    def _thread_serve(self) -> None:
        # Main loop: poll for app changes + service AX events. CFRunLoop
        # processes any AX callbacks that fire during the 0.5s window.
        # returnAfterSourceHandled=False means it runs the full window;
        # returning early is fine for responsiveness if events are rare.
        while not self._stop_event.is_set():
            # Drain any Cocoa autoreleased objects produced this tick.
            # Bare CFRunLoopRunInMode does NOT manage NSAutoreleasePool
            # (only NSRunLoop does), so without this every _safe_attr →
            # AXUIElementCopyAttributeValue accumulates bridged NSStrings
            # on the thread's pool forever — observed as ~10GB compressed
            # memory growth over a few hours of busy AX traffic.
            with objc.autorelease_pool():
                try:
                    self._sync_active_app()
                except Exception:
                    log.exception("active-app sync error (continuing)")
                try:
                    # Process AX events for up to _POLL_INTERVAL_SECONDS,
                    # returning after handling one source so the loop can
                    # re-check stop / app changes promptly.
                    CFRunLoopRunInMode(
                        kCFRunLoopDefaultMode, _POLL_INTERVAL_SECONDS, True,
                    )
                except Exception:
                    log.exception("CFRunLoop error (continuing)")
                    time.sleep(_POLL_INTERVAL_SECONDS)

    def _thread_teardown(self) -> None:
        # Remove notifications + drop refs.
        for pid in list(self._app_observers):
            self._teardown_app_observer(pid)
        self._teardown_workspace_observer()
        self._cf_runloop = None
        # A restart subscribes from scratch: nothing here should outlive
        # the thread that owns it.
        self._current_pid = None
        self._failed_pids.clear()

    # ── App-activation tracking ─────────────────────────────────────

    def _get_active_pid(self) -> int | None:
        try:
            from AppKit import NSWorkspace
            active = NSWorkspace.sharedWorkspace().activeApplication()
            if active is None:
                return None
            return int(active["NSApplicationProcessIdentifier"])
        except Exception:
            log.debug("failed to read NSWorkspace.activeApplication", exc_info=True)
            return None

    def _sync_active_app(self) -> None:
        # Poll the active Space ID on every tick. This is the reliable
        # path on macOS — the NSWorkspace notification only fires when
        # there's a main NSRunLoop dispatching workspace events
        # (Claude Desktop yes; CLI Python no). _emit_space_changed
        # de-dupes against the tracker so notification + poll never
        # double-emit for the same transition.
        self._poll_active_space()

        pid = self._get_active_pid()
        if pid is None or pid == self._current_pid:
            return
        # App switched. Tear down the prior subscription and set up the
        # new one. This is where the per-PID lifecycle pays off — without
        # it we'd be subscribed to whoever was active at start() time and
        # blind to everything else.
        if self._current_pid is not None:
            self._teardown_app_observer(self._current_pid)
        self._setup_app_observer(pid)
        self._current_pid = pid

    def _poll_active_space(self) -> None:
        """Read the active Space ID via private CGS API; emit
        ``view_changed`` when it differs from the last-seen value.

        Cheap: one private-API call (CGSGetActiveSpace returns immediately
        — no IPC, no allocation). Idempotent against the notification
        path via the tracker's shared de-dupe state.
        """
        new_id = _cgs_active_space()
        if new_id is None:
            return  # CGS unavailable; notification path is our only hope.
        self._emit_space_changed(new_id, origin="poll")

    # ── Per-PID AXObserver setup / teardown ─────────────────────────

    def _setup_app_observer(self, pid: int) -> None:
        if pid in self._app_observers or pid in self._failed_pids:
            return

        # AXObserverCreate's third arg is an OUT pointer — pass None as
        # the placeholder; PyObjC returns the created observer in the
        # tuple's second slot. Without the third arg you get a confusing
        # "Need 3 arguments, got 2" TypeError.
        result, observer = AXObserverCreate(pid, _ax_callback, None)
        if result != 0:
            # 0 = kAXErrorSuccess. Common non-zero: -25204 (cannot complete
            # — usually means the target app has no AX support, system
            # services with restricted access, or TCC denial). Memo the
            # failure so the polling loop doesn't retry every 500ms forever.
            log.info("AXObserverCreate(pid=%d) failed with %d — skipping", pid, result)
            self._failed_pids.add(pid)
            return

        app_element = AXUIElementCreateApplication(pid)
        with best_effort(f"setting the AX messaging timeout for pid={pid}", log):
            AXUIElementSetMessagingTimeout(app_element, _AX_MESSAGING_TIMEOUT_SECONDS)

        # CRITICAL: the runloop source must be added BEFORE
        # AXObserverAddNotification's callbacks can fire. Per the
        # omnigroup-archive thread, this is the single most-overlooked
        # step in the AX subscription dance — silently no-ops everything.
        source = AXObserverGetRunLoopSource(observer)
        CFRunLoopAddSource(self._cf_runloop, source, kCFRunLoopCommonModes)

        # Subscribe to each notification type. The refcon (4th arg) is
        # typed `unsigned long` at the C boundary; pass id(self), look
        # up via _OBSERVERS_BY_ID in the callback.
        self_id = id(self)
        added: list[str] = []
        for notif in _AX_NOTIFICATIONS_TO_SUBSCRIBE:
            res = AXObserverAddNotification(observer, app_element, notif, self_id)
            if res == 0:
                added.append(notif)
            else:
                # -25205 = AXErrorNotificationUnsupported (this app doesn't
                # support this notification). Common for Electron / Java
                # apps. Not fatal.
                log.debug("AXObserverAddNotification(%s, pid=%d) → %d",
                          notif, pid, res)

        # Hold strong refs (the brain-trap).
        self._app_observers[pid] = (observer, app_element)
        log.info("AX observer attached to pid=%d, %d/%d notifications",
                 pid, len(added), len(_AX_NOTIFICATIONS_TO_SUBSCRIBE))

    def _teardown_app_observer(self, pid: int) -> None:
        # Switching away from an app is when a failed subscription gets
        # another chance. The memo exists to stop the 500 ms poll retrying
        # forever, not to blacklist an app for the process lifetime: one
        # that failed because it was still launching, or because TCC hadn't
        # been granted yet, deserves a retry next time it is activated —
        # and a recycled pid doesn't inherit someone else's verdict.
        self._failed_pids.discard(pid)
        entry = self._app_observers.pop(pid, None)
        if entry is None:
            return
        observer, app_element = entry
        try:
            for notif in _AX_NOTIFICATIONS_TO_SUBSCRIBE:
                with best_effort(f"removing {notif} for pid={pid}", log):
                    AXObserverRemoveNotification(observer, app_element, notif)
            source = AXObserverGetRunLoopSource(observer)
            CFRunLoopRemoveSource(self._cf_runloop, source, kCFRunLoopCommonModes)
        except Exception:
            log.debug("teardown for pid=%d encountered errors (continuing)",
                      pid, exc_info=True)
        # Dropping refs lets PyObjC release the Obj-C objects.

    # ── Workspace (Space) change tracking ──────────────────────────

    def _setup_workspace_observer(self) -> None:
        """Register ``NSWorkspaceActiveSpaceDidChangeNotification`` on
        the workspace notification center. Idempotent.

        Memory: strong-holds the ``_SpaceWatcher`` instance in
        ``self._space_watcher``. Without this hold, PyObjC GC would
        collect the NSObject while NSWorkspace's internal observer
        list still references it, breaking notification delivery AND
        leaving a dangling pointer (segfault hazard on next post).

        Called from the observer thread's ``_run`` after
        ``CFRunLoopGetCurrent`` succeeds so the notification fires on
        this thread's runloop. CLI-launched Python doesn't have a main
        NSRunLoop to fall back to, so registering from the main thread
        would silently drop notifications.
        """
        if self._space_watcher is not None:
            return  # idempotent
        if not _AX_AVAILABLE:
            return
        try:
            from AppKit import NSWorkspace
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            watcher = _SpaceWatcher.alloc().initWithOwnerId_(id(self))
            nc.addObserver_selector_name_object_(
                watcher,
                b"activeSpaceDidChange:",
                "NSWorkspaceActiveSpaceDidChangeNotification",
                None,
            )
            self._space_watcher = watcher
            log.info("NSWorkspace Space watcher attached")
        except Exception:
            log.exception("failed to register NSWorkspace Space watcher (continuing)")

    def _teardown_workspace_observer(self) -> None:
        """Deregister the Space watcher and drop the strong ref.

        Memory: order matters. ``removeObserver_`` must complete before
        we null out ``self._space_watcher``; otherwise NSWorkspace's
        observer list still points at a now-freed Obj-C object.
        Idempotent — safe to call from teardown paths that may run
        more than once during shutdown races.
        """
        if self._space_watcher is None:
            return
        try:
            from AppKit import NSWorkspace
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            nc.removeObserver_(self._space_watcher)
        except Exception:
            log.debug("NSWorkspace removeObserver failed (continuing)", exc_info=True)
        self._space_watcher = None

    def _handle_space_changed(self) -> None:
        """NSWorkspaceActiveSpaceDidChange selector callback.

        Runs on whichever thread NSWorkspace dispatches the notification
        to (typically main, sometimes our CFRunLoop depending on macOS
        version + host process). _emit is thread-safe.

        Reads the destination Space ID and delegates to
        _emit_space_changed, which de-dupes against the polling path.
        """
        new_id = _cgs_active_space()
        self._emit_space_changed(new_id, origin="notification")

    def _emit_space_changed(self, new_space_id: int | None, *, origin: str) -> None:
        """Shared emit path for both notification and poll.

        De-dupes through self._spaces so the first observer to see a
        transition wins; the other becomes a no-op. The single-source-of-
        truth invariant: one transition → one view_changed delta.

        ``source_id`` is the destination Space ID when available, giving
        LAST_WINS_KINDS a real coalesce key (5 rapid swipes to space X
        collapse to one delta with repeat_count=5). When CGS is
        unreachable, falls back to a monotonic counter so distinct
        transitions stay distinct.

        Memory: ``_emit`` schedules a thread-safe append on the asyncio
        loop; the StateDelta itself is plain Python (no bridged Obj-C
        objects). origin_scope and extra are small dicts. No retention
        cycle with the observer.
        """
        changed, prev = self._spaces.take(new_space_id)
        if not changed:
            return  # already emitted by the other path
        if new_space_id is None:
            # CGS unavailable but notification fired — still emit something
            # so the brain knows view changed, even if the destination is
            # opaque. Use the monotonic counter for a distinct source_id.
            self._space_tx_counter += 1
            source_id = f"space-tx-{self._space_tx_counter}"
        else:
            source_id = f"space-{new_space_id}"
        self._emit(build_view_changed(
            # "ax" loosely = "macOS observer stack"; NSWorkspace
            # notifications don't have their own DeltaSource value.
            source="ax",
            source_id=source_id,
            space_id=str(new_space_id) if new_space_id is not None else None,
            prev_space_id=str(prev) if prev is not None else None,
            origin=origin,  # diagnostic: "poll" or "notification"
            name="active-space-changed",
        ))

    # ── Event handling ──────────────────────────────────────────────

    def _handle_ax_event(self, element: Any, notification_name: str) -> None:
        """Called from _ax_callback (observer thread). Normalize → emit.

        Cheap: read a few attributes, build StateDelta, _emit. _emit
        bridges to asyncio via call_soon_threadsafe — no blocking work
        on the AX callback thread.
        """
        kind = _AX_KIND_MAP.get(notification_name)
        if kind is None:
            return

        role = _safe_attr(element, "AXRole") or ""
        # Layer 1: drop value-changes from noise roles (scroll bars,
        # menu bars). Other event kinds on the same role are kept —
        # focus_changed on a menu still tells the brain "menu opened".
        if kind == "value_changed" and role in _AX_SKIP_ROLES_FOR_VALUE:
            return

        name = _safe_attr(element, "AXTitle") or _safe_attr(element, "AXDescription") or None
        # Check length BEFORE copying AXValue. Under PyObjC 12.1 every
        # AXUIElementCopyAttributeValue(AXValue) result leaks natively
        # (the CFString is never released, autorelease pool or not) —
        # Terminal scrollback is ~1M chars, so each value_changed tick
        # leaked ~2 MB (18 MB/s, 3 GB in 3 min).
        # AXNumberOfCharacters is a small CFNumber, so reading it is cheap.
        n_chars = _safe_attr(element, "AXNumberOfCharacters")
        if n_chars is not None and int(n_chars) > _AX_VALUE_KEEP_MAX:
            value_raw = None
        else:
            value_raw = _safe_attr(element, "AXValue")
        if value_raw is None:
            value = None
        elif role in _AX_NO_VALUE_READ_FOR:
            s = str(value_raw)
            value = s if len(s) <= _AX_VALUE_KEEP_MAX else None
        else:
            value = str(value_raw)

        bbox = None
        with best_effort("reading the element bbox", log):
            pos = _safe_attr(element, "AXPosition")
            size = _safe_attr(element, "AXSize")
            if pos is not None and size is not None:
                bbox = (int(pos.x), int(pos.y), int(size.width), int(size.height))

        delta = StateDelta(
            kind=kind,
            source="ax",
            monotonic_ts=time.monotonic(),
            source_id=_stable_source_id(self._current_pid, element, role, name),
            role=role or None,
            name=name,
            value=value,
            bbox=bbox,
        )
        self._emit(delta)


# ── Module-level helpers ─────────────────────────────────────────────


# Registry: refcon arrives in the AX callback as `unsigned long` (a void*
# at the C level), so we can't pass a Python object directly. Pass id(self)
# at register time, look up here on each callback. Lives at module scope
# because the callback isn't a method — it's a free function bound to
# AXObserverCreate's signature via objc.callbackFor at import time.
# (Passing a Python object as refcon raises "ValueError: depythonifying
# 'unsigned long', got 'MacOSObserver'".)
_OBSERVERS_BY_ID: dict[int, MacOSObserver] = {}


def _make_ax_callback():
    """Build the PyObjC-decorated AX callback at module import time.

    objc.callbackFor reads AXObserverCreate's metadata to bind the
    callback's C signature — without it, AXObserverCreate rejects the
    callback with "Callable argument is not a PyObjC closure".
    Returns None on non-darwin so module import doesn't blow up.
    """
    if not _AX_AVAILABLE:
        return None

    @objc.callbackFor(AXObserverCreate)
    def _cb(observer, element, notification, refcon):
        try:
            name = str(notification) if notification else ""
            if not name:
                return
            inst = _OBSERVERS_BY_ID.get(int(refcon)) if refcon is not None else None
            if inst is None:
                return
            inst._handle_ax_event(element, name)
        except Exception:
            # NEVER let an exception propagate back across the C boundary.
            # Log and swallow; one buggy event shouldn't kill the observer.
            log.exception("AX callback error (notification=%r)", notification)

    return _cb


_ax_callback = _make_ax_callback()


# ── NSWorkspace Space watcher (NSObject subclass) ─────────────────────


def _make_space_watcher_class():
    """Build the NSObject subclass used as the NSNotificationCenter
    observer for ``NSWorkspaceActiveSpaceDidChangeNotification``.

    Defined inside a factory so module import on non-darwin (where
    NSObject is unavailable) doesn't blow up. Returns None there.

    Memory: the selector ``activeSpaceDidChange_`` takes an NSNotification
    arg but never retains it — we read nothing from it and just dispatch
    to the owner via _OBSERVERS_BY_ID. The selector signature must use
    the ``@objc.typedSelector`` convention or NSObject's auto-binding;
    we rely on auto-binding (`b"activeSpaceDidChange:"` byte-string with
    one arg matches an instance method named ``activeSpaceDidChange_``).
    """
    if not _AX_AVAILABLE:
        return None

    class _SpaceWatcher(NSObject):
        def initWithOwnerId_(self, owner_id):
            # PyObjC's designated-initializer idiom: init() returns the
            # (possibly different) instance to use from here on.
            self = objc.super(_SpaceWatcher, self).init()  # noqa: PLW0642
            if self is None:
                return None
            # Plain Python int — no retention cycle with the owner.
            self.owner_id = int(owner_id)
            return self

        def activeSpaceDidChange_(self, notification):
            owner = _OBSERVERS_BY_ID.get(self.owner_id)
            if owner is None:
                return
            try:
                owner._handle_space_changed()
            except Exception:
                log.exception("space-change handler error")

    return _SpaceWatcher


_SpaceWatcher = _make_space_watcher_class()


def _safe_attr(element: Any, attr_name: str) -> Any:
    """AXUIElementCopyAttributeValue with all errors swallowed.

    Element attributes can fail for many reasons: element destroyed,
    attribute unsupported, app not responding. Returning None on any
    error is the right default — the StateDelta fields are all Optional.
    """
    if not _AX_AVAILABLE:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(element, attr_name, None)
        if err == 0:
            return value
    except Exception:
        # Hot path (every AX event, several attrs each): a plain handler
        # rather than best_effort's generator.
        log.debug("reading %s failed", attr_name, exc_info=True)
    return None


def _stable_source_id(
    pid: int | None, element: Any, role: str, name: str | None,
) -> str:
    """Build a stable-ish coalesce key for an AX element.

    Prefer AXIdentifier when present (apps that set it explicitly —
    rare but unambiguous). Fall back to (pid, role, name) which is
    usually stable across the same element's events within a tool-call
    frame. Worst case: same element appears as multiple keys, coalesce
    just doesn't dedupe — output verbose but correct.
    """
    ax_id = _safe_attr(element, "AXIdentifier")
    if ax_id:
        return f"{pid or '?'}:{ax_id}"
    return f"{pid or '?'}:{role or '?'}:{name or ''}"
