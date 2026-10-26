# Working on conductor-desktop

This file is for agents and people changing conductor-desktop. For the briefing an
agent needs to *drive* a desktop through it, see `examples/operator-briefing/`.

## Commands

```bash
uv sync                  # dev environment (Python 3.11+)
uv run pytest -q         # unit tests; platform-specific ones skip elsewhere
uvx ruff check .         # lint; config lives in pyproject.toml
```

On Linux, AT-SPI tests need the system `python3-gi`, so create the venv with
`uv venv --system-site-packages` and use the Python version the distro's
`python3-gi` was built for.

## Layout

- `conductor_desktop/tooling.py`: the `@tool` decorator and the call contract
  every tool shares (input lock, pause switch, audit log, tray, timing,
  `ToolError` → `isError`).
- `conductor_desktop/tools/`: the MCP tools, one module per family.
- `conductor_desktop/runtime/`: helpers the tools share (capture, grounding, the
  post-action observation, action replies).
- `conductor_desktop/state.py`: the running server's state.
- `conductor_desktop/server.py`: startup and shutdown; builds the FastMCP server
  from the config.
- `conductor_desktop/core/executor/` (input in `executor/input/`) and `core/backends/`: per-platform input,
  capture and window control.
- `conductor_desktop/core/ui/`: the accessibility-tree layer behind the `ui_*` tools.
- `conductor_desktop/core/perception/`: the delta observers (AX, AT-SPI, CDP, BiDi).
- `conductor_desktop/grounder.py` and `managed_*.py`: VLM grounding clients.

## Rules that tests enforce

- A tool is declared with `@tool(kind="acting" | "observing", gate=...)`.
  Acting tools take the desktop input lock by default; mobile tools opt out
  with `lock=False`. A `gate` is an `MCPConfig` flag that leaves the tool off
  the surface when false.
- A tool fails by raising `ToolError` (the reply becomes `isError` with an
  `error:` line). Input actions go through `runtime.replies.action_reply`,
  reads through `output_of`. Don't hand-build `error:` text.
- Every string tool parameter is classified for audit redaction, either in
  `audit.SENSITIVE_ARGS` or in the test's not-sensitive list.
- Strings from the agent never get spliced into script source (AppleScript,
  Shell JS, PowerShell, a device shell). Pass them as argv, `json.dumps`,
  base64 or `shlex.quote` them. See `tests/test_script_quoting.py`.
- What an agent reads about the server is held to what clients show
  (`tests/test_agent_facing_text.py`). Some clients (Claude Desktop) drop the
  server's `instructions` and cut each tool description at about 500
  characters, so:
  - A tool's first paragraph stands alone within 500 characters: what it
    does, when to use it instead of its siblings, and its main gotcha. The
    test checks the length; whether it says the right things is on review.
  - Every parameter is described in the schema with
    `Annotated[T, Field(description=...)]`, not in the docstring prose.
  - `conductor_desktop/instructions.py` holds only what no single tool owns
    (which channel to use, the look–act–check loop), under 2048 characters,
    where Claude Code cuts it. Nothing may live only there. Facts about one machine go in the user's own CLAUDE.md, see
    `examples/operator-briefing/`.
