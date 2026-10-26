"""The state folder: ~/.conductor-desktop, or the pre-rename ~/.conductor-mcp."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor_desktop.paths import state_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_fresh_machine_uses_new_folder(home: Path) -> None:
    assert state_dir() == home / ".conductor-desktop"


def test_old_folder_alone_is_kept(home: Path) -> None:
    (home / ".conductor-mcp").mkdir()
    assert state_dir() == home / ".conductor-mcp"


def test_new_folder_wins_once_it_exists(home: Path) -> None:
    (home / ".conductor-mcp").mkdir()
    (home / ".conductor-desktop").mkdir()
    assert state_dir() == home / ".conductor-desktop"
