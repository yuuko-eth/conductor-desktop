"""The one error a platform backend raises.

Backends return plain data (lists, strings, dataclasses) and raise
:class:`BackendError` when they can't do what was asked; the message is
written for the agent. The executor turns it into a failed ``ActionResult``,
and the tool wrapper turns one that reaches it into an error reply.

A return value keeps a single meaning: ``focus_window`` returns ``False``
only when no window matched, ``get_clipboard`` returns ``""`` only for an
empty clipboard. Anything that went wrong raises.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

_log = logging.getLogger("conductor_desktop")


class BackendError(Exception):
    """A backend couldn't do what was asked; the message says why."""


def reason(e: BaseException) -> str:
    """``e``'s message, or its type when it has none (``TimeoutError()``)."""
    return str(e) or type(e).__name__


@contextmanager
def best_effort(what: str, logger: logging.Logger | None = None) -> Iterator[None]:
    """Run an optional step; if it raises, log why at debug level and go on.

    The house rule for probing a desktop, where a call can fail in ways the
    OS doesn't enumerate (a window closing mid-walk, a pyobjc bridge error,
    an app that doesn't answer). Use it only when the caller has a sensible
    fallback — set the default before the block::

        title = ""
        with best_effort("reading the window title", log):
            title = window.title()

    Failures that the agent should hear about raise BackendError instead.
    """
    log = logger or _log
    try:
        yield
    except Exception:
        log.debug("%s failed", what, exc_info=True)
