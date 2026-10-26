"""The macOS grant hint names both places a grant can be needed."""

from __future__ import annotations

import os
import sys

from conductor_desktop.check_permissions import macos_grant_hint


def test_it_names_the_launching_app_and_this_binary():
    text = "\n".join(macos_grant_hint())
    assert "Claude Desktop" in text and "terminal" in text
    # The real path, not the venv symlink: that's what the settings list shows.
    assert os.path.realpath(sys.executable) in text
