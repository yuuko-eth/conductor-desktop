"""The `view_changed` delta: one shape, one de-dupe, three platforms.

A Space on macOS, a workspace on GNOME/X11/KWin and a wlroots workspace are
the same event to the agent — "what you are looking at is now something
else" — so they emit one delta shape (design_scene_graph.md §Filter system).
Each platform names its destination differently (`space-7`, `ws:global:2`,
`3@DP-1`), which is all `build_view_changed` asks for.

`SpaceTracker` holds the last-seen destination behind a lock. macOS needs the
lock: the check-and-set runs both from the observer thread's 500 ms poll and
from an NSWorkspace notification that Apple dispatches on whichever thread it
likes. The other platforms are single-threaded by construction and pay only
an uncontended lock for the shared code.
"""

from __future__ import annotations

import threading
import time

from .classifier import StateDelta


class SpaceTracker:
    """Last-seen view id, with a check-and-set several threads can call.

    `take()` is the whole protocol: it answers "is this a transition?" and
    hands back what the previous destination was, having already recorded the
    new one. Two callers racing on the same transition means exactly one of
    them is told it is a transition, so one transition is one delta.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: str | int | None = None

    @property
    def last(self) -> str | int | None:
        return self._last

    def prime(self, value: str | int | None) -> None:
        """Record the starting destination without calling it a transition.

        Primed at setup so the first read doesn't emit a bogus
        "None → wherever we started".
        """
        with self._lock:
            self._last = value

    def take(self, new: str | int | None) -> tuple[bool, str | int | None]:
        """``(is_transition, previous)``, recording ``new`` as the latest.

        An unknown destination (``None``) always counts as a transition: the
        caller saw *something* move and can't say where to, which is still
        worth telling the agent.
        """
        with self._lock:
            previous = self._last
            if new is not None and new == previous:
                return False, previous
            self._last = new
            return True, previous


def build_view_changed(
    *,
    source: str,
    source_id: str,
    space_id: str | None,
    prev_space_id: str | None,
    origin: str,
    name: str = "active-workspace-changed",
    extra: dict | None = None,
) -> StateDelta:
    """One `view_changed` delta.

    ``source_id`` is the coalesce key, so it names the *destination*: five
    swipes to the same space collapse to one delta with ``repeat_count=5``
    (LAST_WINS_KINDS), while distinct destinations stay distinct.
    ``origin`` is diagnostic — which of a platform's several watchers saw it.
    ``extra`` adds per-platform fields (wlroots names the output).
    """
    return StateDelta(
        kind="view_changed",
        source=source,
        monotonic_ts=time.monotonic(),
        source_id=source_id,
        name=name,
        origin_scope={
            "target": "host",
            # Screen is left None: platforms name outputs, not screen
            # indices, and consumers resolve one to the other through the
            # scene graph.
            "screen": None,
            "space_id": space_id,
        },
        extra={
            "scope": "space",
            "from": prev_space_id,
            "to": space_id,
            "target": "host",
            "origin": origin,
            **(extra or {}),
        },
    )
