# conductor-haiku — TS-only conductor for Haiku

A self-contained TypeScript implementation of the conductor tool surface
(read_screen, capture_screen, click, type, key, locate, etc.) that drives the
Haiku desktop via native helpers. Exposes the same tools two ways:

1. **As a Pi extension** — load into `pi-coding-agent` on Haiku for
   small-model autonomous CUA workflows.
2. **As an MCP stdio server** — wire into Claude Code (or any MCP client)
   running on Haiku.

Both adapters share `tools.ts`, so the surface stays in sync.

## Why TypeScript-only

The conventional path (`conductor_desktop` Python server) does not work on
Haiku because:

- The Rust toolchain can't build `pydantic-core` (Haiku's pthread
  `TLS_MAX_KEYS=64` ceiling collapses Rust's thread-local key allocation).
- `pip install` over HTTPS reads time out against pypi.org (Haiku's
  OpenSSL stack — even though TCP-443 is reachable).

Sidestepping Python entirely keeps both blockers off the path. Node.js is
already available because Pi ships it.

## What gets shelled out

```
~/config/non-packaged/bin/input_send                       # mouse/keyboard synth
~/config/non-packaged/bin/window_lister                    # JSON window dump
~/config/non-packaged/add-ons/input_server/devices/conductor_input
                                                           # input_server device add-on
/system/bin/screenshot                                     # Haiku built-in
/system/bin/python3 + autopilot.backends.haiku             # clipboard only
```

The C++ sources for `input_send`, `window_lister`, and `conductor_input`
live in `conductor_desktop/core/backends/native/haiku/` upstream.

## Tool surface

| tool | description |
|------|-------------|
| `read_screen` | Prose read of the screen (VLM) — your default, fast way to observe. |
| `capture_screen` | Capture desktop as a raw PNG. Use only when you need pixels, not to observe. |
| `list_windows` | All visible windows with bounds, focus, app signature. |
| `focus_window` | Bring a matching window to the front. |
| `click_at` | Synthetic click at (x, y). |
| `mouse_move` | Move cursor without clicking. |
| `scroll` | Wheel scroll at current cursor. |
| `type_text` | Type a UTF-8 string. |
| `key_press` | Single named key. |
| `hotkey` | Modified key chord (`ctrl`/`alt`/`shift`/…). |
| `get_clipboard`, `set_clipboard` | Read/write system clipboard. |
| `locate` | Find a UI element by description (VLM grounder). |
| `exec_shell` | Run a shell command. |
| `check_input` | Probe input pipeline health via `input_send ping`. |
| `restart_input_server` | Recover from stale `conductor_input` registration. |

`check_input` + `restart_input_server` exist because Haiku's `input_server`
only scans device add-ons at startup, so a replaced/missed add-on can
leave observation working while click/type silently no-op. `input_send`
returns rc=0 in that state (write_port succeeded, but nobody listens) —
`check_input` distinguishes by sending `ping`, which returns rc=1 if the
port isn't registered. `restart_input_server` kills the team and waits
for `launch_daemon` to respawn it.

There's also a shell script for manual use:
`setup/bounce-input-server.sh` in the repo root.

## Setup — pi-coding-agent

```bash
# One-time
npm i -g @earendil-works/pi-coding-agent

# Drop the extension into Pi's tree
mkdir -p ~/.pi/agent/extensions/conductor-haiku
cp examples/pi-coding-agent/extensions/conductor-haiku/{index.ts,tools.ts,tsconfig.json,package.json} \
   ~/.pi/agent/extensions/conductor-haiku/

# Install runtime deps (typebox; MCP SDK is unused on the Pi side but
# pulled in by package.json — harmless)
cd ~/.pi/agent/extensions/conductor-haiku && npm install --omit=dev

# Also drop in auto-continue (provides /task and task_complete)
cp -r examples/pi-coding-agent/extensions/auto-continue \
      ~/.pi/agent/extensions/

# Optional: point grounder at your VLM server
export CONDUCTOR_GROUNDER_URL=http://your-grounder:9134

pi   # tools should appear
```

## Setup — Claude Code

**Precompile required on Haiku.** The default TS runner `tsx` depends on
`esbuild`, which ships native binaries and has no Haiku build — so we
compile `tools.ts` + `mcp-server.ts` ahead of time on a host that *can*
run `tsc` (any Mac/Linux/Windows with Node) and ship `dist/*.js` to
Haiku. The compiled output is pure ES2022 — Node 20 on Haiku runs it
directly.

```bash
# On your dev host:
cd examples/pi-coding-agent/extensions/conductor-haiku
npm install              # pulls @sinclair/typebox + @modelcontextprotocol/sdk + tsc
npm run build            # emits dist/tools.js + dist/mcp-server.js

# Copy to Haiku (extension dir is fine; node_modules already has the runtime deps):
rsync -av dist/ <haiku-host>:~/.pi/agent/extensions/conductor-haiku/dist/
```

Register with Claude Code on Haiku (`claude mcp add-json` writes to
`~/.claude/settings.json`'s local-scope MCP config):

```bash
claude mcp add-json conductor-haiku '{
  "command": "node",
  "args": ["/boot/home/.pi/agent/extensions/conductor-haiku/dist/mcp-server.js"],
  "env": { "CONDUCTOR_GROUNDER_URL": "http://grounder.lan:9134" }
}'
claude mcp list   # should show: conductor-haiku - ✓ Connected
```

Or for project-scope wiring, drop `.mcp.json` at your project root:

```json
{
  "mcpServers": {
    "conductor-haiku": {
      "command": "node",
      "args": ["/boot/home/.pi/agent/extensions/conductor-haiku/dist/mcp-server.js"],
      "env": { "CONDUCTOR_GROUNDER_URL": "http://grounder.lan:9134" }
    }
  }
}
```

(On dev hosts where `tsx` runs natively, you can skip the precompile and
point `command`/`args` at `tsx mcp-server.ts` directly. That just doesn't
work on Haiku.)

### Skipping the per-call permission prompt

Claude Code asks before each MCP tool call by default. For an autonomous
CUA loop on a sandboxed host, preauthorize the whole server in user
settings (`~/.claude/settings.json`):

```json
{
  "permissions": {
    "allow": ["mcp__conductor-haiku"]
  }
}
```

The bare-server form preauthorizes every tool from `conductor-haiku`. If
your Claude version requires explicit tool entries, list each
`mcp__conductor-haiku__<name>` instead (16 of them — see the table
above). Consider leaving `mcp__conductor-haiku__exec_shell` *out* of the
allow list — it runs arbitrary shell commands, so a per-call prompt is
the only thing between the model and `rm -rf`.

## Caveats

- `task_complete` is intentionally **not** registered here. The Pi
  `auto-continue` extension already provides it; registering it again
  causes Pi to refuse to start with a tool-name conflict. Claude Code
  doesn't need it — Claude Code has no auto-continue loop.
- Image content blocks: both Pi and MCP support `{ type: "image", data:
  <base64>, mimeType: "image/png" }`. The `capture_screen` tool returns that
  shape directly; no per-adapter mangling.
- `focus_window` / `get_clipboard` / `set_clipboard` shell into a small
  Python helper at `/boot/home/conductor/autopilot/backends/haiku.py`.
  After the public release split, that path moved — adjust
  `CONDUCTOR_REPO` in `tools.ts` if your layout differs.
