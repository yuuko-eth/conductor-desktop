# pi-coding-agent + Conductor Desktop

Drop-in templates for wiring [pi-coding-agent](https://github.com/earendil-works/pi-coding-agent) (a.k.a. "Pi") to the conductor MCP server, plus the `auto-continue` extension that gives Pi an Auto Mode loop.

## Layout on the target host

Pi's config tree lives at `~/.pi/agent/`:

```
~/.pi/agent/
├── auth.json                       # provider OAuth state (managed by Pi)
├── settings.json                   # changelog ack + npm tool packages
├── models.json                     # provider/model config (NOT checked in — has API keys)
├── mcp.json                        # MCP servers — copy from this dir
├── extensions/
│   ├── auto-continue/              # copy from this dir
│   │   ├── index.ts
│   │   └── tsconfig.json
│   └── show-screen/                # copy from this dir (Discord image relay)
│       ├── index.ts
│       └── tsconfig.json
├── bin/                            # Pi-bundled rg, fd
└── sessions/                       # per-session state
```

## Setup

1. **Install Pi** (one-time, per host):
   ```bash
   npm i -g @earendil-works/pi-coding-agent
   ```

2. **Drop in MCP config**:
   ```bash
   cp examples/pi-coding-agent/mcp.json ~/.pi/agent/mcp.json
   # then edit `command` to point at this repo's .venv
   # and CONDUCTOR_DESKTOP_GROUNDER_URL to your grounder
   ```

3. **Install the auto-continue extension**:
   ```bash
   mkdir -p ~/.pi/agent/extensions/auto-continue
   cp examples/pi-coding-agent/extensions/auto-continue/* ~/.pi/agent/extensions/auto-continue/
   ```

4. **Verify** the agent sees the server:
   ```bash
   pi --version
   # then run pi and check that conductor-desktop tools appear
   ```

## Auto Mode (auto-continue extension)

Drives an autonomous loop that keeps the agent ticking until it explicitly calls `task_complete`:

- `/task <prompt>` — kicks off a task. Pi will keep auto-continuing after each `agent_end`.
- The extension registers a `task_complete` tool the model can call to break the loop.
- Hard ceiling: `MAX_AUTO = 40` consecutive auto-continues with no tool calls (resets whenever a real tool fires, so productive turns are unbounded).

The "Pi + Auto Mode" combo is the small-model autonomous-CUA workflow — Pi feeds tool deltas back in cheaply, conductor-desktop grounds via a local VLM, and the loop only stops when the model declares done or hits the ceiling.

## Show screen (show-screen extension)

For hosts driven over chat via [OpenAB](https://docs.conductor-mcp.io/guides/remote-control/): OpenAB
relays the agent's **text only**, so the user can't see the screen unless the agent
sends an image. This extension registers a `show_screen(path?, caption?)` tool that
POSTs a screenshot straight to the Discord thread the agent is replying in.

```bash
mkdir -p ~/.pi/agent/extensions/show-screen
cp examples/pi-coding-agent/extensions/show-screen/* ~/.pi/agent/extensions/show-screen/
```

How it works (and what it needs):

- **Target thread** is read from the `<sender_context>` block OpenAB injects into each
  message (captured via `pi.on("input")`, which fires `source:"rpc"` under pi-acp). Uses
  `thread_id`, falls back to `channel_id`.
- **Bot token** must be forwarded to the agent — OpenAB consumes `[discord].bot_token`
  and does **not** pass it down. Add to `~/.openab/config.toml`, reusing the same key:
  ```toml
  [agent]
  env = { DISCORD_BOT_TOKEN = "${DISCORD_BOT_TOKEN}" }
  ```
- **Pixels:** prefer passing a `capture_screen` path (conductor saves one when
  `CONDUCTOR_DESKTOP_SCREENSHOT_MODE` is `file` or `both`) — conductor's executor already
  captures correctly on every platform, so the extension just relays the file. `show_screen()`
  with no `path` self-captures as a convenience, dispatched per-OS (macOS `screencapture`;
  Linux tries `gnome-screenshot`/`scrot`/`maim`/`spectacle`/`grim`/`import`, with `DISPLAY`
  defaulted to `:0`). Bot needs `Attach Files`.
- Uses Node 18+ global `fetch`/`FormData`/`Blob` — no extra npm deps beyond typebox.

> **macOS TCC:** `screencapture` from the pi process needs Screen Recording; it inherits
> OpenAB's grant in practice. **Linux:** the self-capture needs
> `DISPLAY`/`XAUTHORITY` in the agent's env (present when OpenAB runs under a
> `graphical-session.target` user service). If a no-arg `show_screen` returns blank, route
> through `capture_screen` instead — that path always works. Validated on macOS
> and Ubuntu GNOME X11.

## `directTools` allowlist

`mcp.json` carries an extra `directTools` array (Pi-specific, not part of the MCP spec). Tools in this list bypass Pi's orchestrator confirmations — fine for high-frequency idempotent ones (read_screen, capture_screen, locate, …), risky for anything destructive. Curate this per host.

## Notes

- `models.json` and `auth.json` carry secrets and are intentionally **not** templated here.
- The Conductor Desktop server itself is identical across Pi and Claude Code — only the wrapper config differs.
