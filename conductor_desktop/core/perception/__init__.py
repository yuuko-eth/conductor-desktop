"""Perception delta channel — between-tool-call structured event observers.

The package exposes a platform-neutral StateDelta + AsyncObserver shape;
per-source observers live under `perception.deltas`.
"""

from .classifier import DeltaKind, DeltaSource, StateDelta, format_delta_block
from .observer_base import AsyncObserver, cap_by_importance, coalesce
from .threaded import ThreadedObserver
from .view import SpaceTracker, build_view_changed

__all__ = [
    "AsyncObserver",
    "DeltaKind",
    "DeltaSource",
    "SpaceTracker",
    "StateDelta",
    "ThreadedObserver",
    "build_view_changed",
    "cap_by_importance",
    "coalesce",
    "format_delta_block",
]
