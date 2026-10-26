"""Pre-rename CONDUCTOR_MCP_* variables keep working as CONDUCTOR_DESKTOP_*."""

from __future__ import annotations

import json
import os
import subprocess
import sys

_PROBE = (
    "import json, os, conductor_desktop as c; "
    "print(json.dumps({'env': {k: v for k, v in os.environ.items() if k.startswith('CONDUCTOR_')}, "
    "'renamed': c.LEGACY_ENV_RENAMED}))"
)


def _import_with(env: dict[str, str]) -> dict:
    base = {k: v for k, v in os.environ.items() if not k.startswith("CONDUCTOR_")}
    out = subprocess.run([sys.executable, "-c", _PROBE], env={**base, **env},
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_old_name_moves_to_new_name() -> None:
    r = _import_with({"CONDUCTOR_MCP_GROUNDER_URL": "http://g:8000"})
    assert r["env"] == {"CONDUCTOR_DESKTOP_GROUNDER_URL": "http://g:8000"}
    assert r["renamed"] == ["CONDUCTOR_MCP_GROUNDER_URL"]


def test_new_name_wins_and_old_is_still_removed() -> None:
    r = _import_with({"CONDUCTOR_MCP_AUTH_TOKEN": "old", "CONDUCTOR_DESKTOP_AUTH_TOKEN": "new"})
    assert r["env"] == {"CONDUCTOR_DESKTOP_AUTH_TOKEN": "new"}


def test_nothing_to_rename() -> None:
    r = _import_with({"CONDUCTOR_DESKTOP_DELTAS": "true"})
    assert r["env"] == {"CONDUCTOR_DESKTOP_DELTAS": "true"}
    assert r["renamed"] == []
