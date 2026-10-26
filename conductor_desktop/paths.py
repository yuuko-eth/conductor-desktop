"""The per-user folder for logs, the desktop lock and screenshots."""

from __future__ import annotations

from pathlib import Path


def state_dir() -> Path:
    """``~/.conductor-desktop``, or ``~/.conductor-mcp`` on a machine that ran
    the server before the rename and has no new folder yet: its audit logs
    stay in one place, and an older server still running there contends for
    the same desktop lock."""
    new, old = Path.home() / ".conductor-desktop", Path.home() / ".conductor-mcp"
    return old if old.is_dir() and not new.exists() else new
