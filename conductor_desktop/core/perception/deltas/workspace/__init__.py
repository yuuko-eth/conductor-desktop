"""Linux workspace sources for the AT-SPI observer.

Three ways a Linux desktop reports the active workspace, one interface:
`WorkspaceSource`. See `base.py` for why they are plugins rather than four
sources tangled in one class.
"""

from .base import OnWorkspace, SessionBus, WorkspaceSource
from .gnome import GnomeWorkspacePoll
from .kde import KdeWorkspaceWatcher
from .x11 import X11WorkspaceWatcher, is_x11_session

__all__ = [
    "GnomeWorkspacePoll",
    "KdeWorkspaceWatcher",
    "OnWorkspace",
    "SessionBus",
    "WorkspaceSource",
    "X11WorkspaceWatcher",
    "is_x11_session",
]
