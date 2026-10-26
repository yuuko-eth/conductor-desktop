"""Export the tool surface and the environment variables for the docs site.

    .venv/bin/python scripts/docs/export_reference.py ~/conductor-docs/src/data

writes two files there:

- ``tools.json``: every declared tool, enabled or not, as the server would
  register it: name, the module declaring it, kind (acting/observing), the
  setting that turns it on when it's gated, the description an MCP client
  sees and the input schema FastMCP derives from the signature. Which page
  and heading each tool goes under is the site's call (tool-groups.yaml).
- ``env.json``: every ``CONDUCTOR_DESKTOP_*`` variable the package reads, with
  the files that read it. The site pairs it with its own hand-written
  ``config.yaml`` and fails the build when a variable has no entry there.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import conductor_desktop.tools  # noqa: F401  (declares the tools)
from conductor_desktop import tooling

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "conductor_desktop"

# The setting behind each gate, as a reader would set it.
GATES = {
    "web_enabled": "CONDUCTOR_DESKTOP_WEB (on by default)",
    "shell_enabled": "CONDUCTOR_DESKTOP_SHELL (on by default)",
    "describe_image_enabled": "CONDUCTOR_DESKTOP_DESCRIBE_IMAGE=1",
    "mobile_enabled": "CONDUCTOR_DESKTOP_MOBILE_ANDROID=1 or CONDUCTOR_DESKTOP_MOBILE_IOS=1",
    "mobile_shell_enabled": "a phone transport on, and CONDUCTOR_DESKTOP_SHELL",
    "enumerate_enabled": "CONDUCTOR_DESKTOP_OMNIPARSER_URL, or the managed gateway",
}

# Prefixes built at runtime (CONDUCTOR_DESKTOP_ + name), not variables themselves.
_ENV = re.compile(r"\bCONDUCTOR_DESKTOP_[A-Z0-9_]*[A-Z0-9]\b")


class _AllGatesOn:
    def __getattr__(self, name: str) -> bool:
        return True


def tools() -> list[dict]:
    mcp = FastMCP("export")
    tooling.register(mcp, _AllGatesOn())
    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    out = []
    for name, spec in tooling.specs().items():
        t = listed[name]
        out.append({
            "name": name,
            "module": spec.fn.__module__.rsplit(".", 1)[-1],
            "kind": spec.kind,
            "gate": GATES[spec.gate] if spec.gate else None,
            "description": t.description,
            "schema": t.inputSchema,
        })
    missing = {s.gate for s in tooling.specs().values() if s.gate and s.gate not in GATES}
    if missing:
        sys.exit(f"no GATES entry for {sorted(missing)}")
    return out


def env() -> list[dict]:
    seen: dict[str, set[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        for name in _ENV.findall(path.read_text(encoding="utf-8")):
            seen.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return [{"name": n, "files": sorted(f)} for n, f in sorted(seen.items())]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    t, e = tools(), env()
    (out / "tools.json").write_text(json.dumps(t, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "env.json").write_text(json.dumps(e, indent=1) + "\n", encoding="utf-8")
    print(f"{len(t)} tools, {len(e)} variables -> {out}")


if __name__ == "__main__":
    main()
