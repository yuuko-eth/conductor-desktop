"""What the CDP and WebDriver BiDi clients share.

Both protocols are JSON commands with ids over one WebSocket, answered by
replies carrying the same id, with events interleaved. ``JsonRpcSocket`` owns
that plumbing: the connection, the reader task, pending replies, event
handlers and teardown. Subclasses say how their protocol frames a command,
tells an event from a reply, and reports an error. ``TabHelpers`` holds the
waits every tab session offers on top of its ``evaluate``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

import websockets
from websockets.exceptions import ConnectionClosed

from .core.errors import best_effort

log = logging.getLogger("conductor.mcp.browser")

# Enough for any single page screenshot (even beyond the viewport on a tall
# page), small enough that the library bounds its per-connection receive
# buffer. max_size=None once let the buffers grow to ~2 MB per frame with the
# DOM and Network domains on: 6 GB resident after ~75 web_crop calls against
# a hot-reloading page.
MAX_MESSAGE = 64 * 1024 * 1024


def js_str(s: str) -> str:
    """JSON-encode a string, safe to interpolate into JS source.

    ensure_ascii=False keeps CJK and other non-ASCII readable in logs and
    error messages; JS handles the literal characters.
    """
    return json.dumps(s, ensure_ascii=False)


class JsonRpcSocket(ABC):
    """One WebSocket carrying commands, their replies, and events."""

    #: Raised for protocol errors, timeouts and a lost connection.
    error: ClassVar[type[RuntimeError]]
    #: Names the protocol in messages ("CDP", "WebDriver BiDi").
    label: ClassVar[str]

    def __init__(self) -> None:
        self._ws: websockets.ClientConnection | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        # Keyed by event method. Handlers survive a reconnect; any protocol
        # subscription that feeds them is the caller's to renew.
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}

    @abstractmethod
    def _frame(self, cmd_id: int, method: str, params: dict) -> dict:
        """The message that sends ``method``."""

    @abstractmethod
    def _event(self, msg: dict) -> tuple[str, dict] | None:
        """``(method, params)`` if ``msg`` is an event, None for a reply."""

    @abstractmethod
    def _result(self, method: str, reply: dict) -> dict:
        """The reply's result; raises ``self.error`` for an error reply."""

    @property
    def connected(self) -> bool:
        return (self._ws is not None and self._reader_task is not None
                and not self._reader_task.done())

    async def _open(self, url: str, **kwargs: Any) -> None:
        """Connect and start the reader. Raises what ``websockets.connect`` does."""
        self._ws = await websockets.connect(url, max_size=MAX_MESSAGE, **kwargs)
        self._reader_task = asyncio.create_task(self._reader())

    async def _teardown(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("%s reader task failed", self.label, exc_info=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            with best_effort(f"closing the {self.label} websocket", log):
                await ws.close()
        self._fail_pending()

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(self.error(f"{self.label} connection closed"))
        self._pending.clear()

    async def _reader(self) -> None:
        """Route replies to pending futures and events to their handlers."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if (event := self._event(msg)) is not None:
                    method, params = event
                    for handler in list(self._event_handlers.get(method, ())):
                        try:
                            handler(params)
                        except Exception as e:
                            # A buggy handler must not end the reader and
                            # starve every pending command.
                            log.warning("%s event handler for %s raised: %s",
                                        self.label, method, e, exc_info=True)
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s reader stopped: %s", self.label, e)  # a closed tab or browser
            log.debug("%s reader traceback", self.label, exc_info=True)
        finally:
            # Nothing will answer them now: fail them instead of letting each
            # wait out its timeout.
            self._fail_pending()

    def on(self, method: str, handler: Callable[[dict], None]) -> None:
        """Subscribe ``handler(params)`` to an event. It runs on the reader
        task, in registration order: keep it cheap. Exceptions are logged."""
        self._event_handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler: Callable[[dict], None]) -> None:
        """Unsubscribe a handler. No-op if absent."""
        handlers = self._event_handlers.get(method)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def _send_raw(self, method: str, params: dict, *, timeout: float) -> dict:
        if not self.connected:
            raise self.error(f"{self.label} connection is closed")
        self._next_id += 1
        cmd_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await self._ws.send(json.dumps(self._frame(cmd_id, method, params)))
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            raise self.error(f"{method} timed out after {timeout}s") from None
        except ConnectionClosed as e:
            raise self.error(f"{method} failed: connection closed ({e})") from e
        finally:
            self._pending.pop(cmd_id, None)
        return self._result(method, reply)


class TabHelpers:
    """Waits built on a tab session's ``evaluate``."""

    error: ClassVar[type[RuntimeError]]

    async def evaluate(self, expression: str, *, await_promise: bool = True,
                       user_activation: bool = True) -> Any:
        """Provided by the session class."""
        raise NotImplementedError

    async def wait_for_selector(self, selector: str, *,
                                timeout_ms: int = 5000,
                                poll_ms: int = 100) -> bool:
        """Poll until ``document.querySelector(selector)`` is non-null. True
        on a hit, False on timeout. Polling every 100 ms instead of a
        MutationObserver leaves nothing in the page to tear down."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        probe = f"document.querySelector({js_str(selector)}) !== null"
        while True:
            try:
                found = await self.evaluate(probe, await_promise=False,
                                            user_activation=False)
            except self.error:
                found = False
            if found:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(poll_ms / 1000)

    @staticmethod
    async def _until_quiet(in_flight: set[str], last_activity: list[float], *,
                           quiet_ms: int, timeout_ms: int) -> bool:
        """True once nothing is in flight and ``last_activity[0]`` is
        ``quiet_ms`` old; False at ``timeout_ms``. The event handlers that
        fill both run on the reader task, on this loop, so no lock."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            now = loop.time()
            if not in_flight and (now - last_activity[0]) >= quiet_ms / 1000:
                return True
            if now >= deadline:
                return False
            await asyncio.sleep(0.05)
