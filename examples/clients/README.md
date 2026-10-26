# Client configs

One file per MCP client, in that client's own format, registering
conductor-desktop as a local (stdio) server. Formats were checked against each
client's documentation on 2026-10-24. Clients change these, so if one is
rejected, check the client's current MCP docs.

In every file:

- Replace the command with the absolute path to `conductor-desktop` in your
  checkout. On Windows, that's `C:\path\to\conductor-desktop\.venv\Scripts\conductor-desktop.exe`,
  with every backslash doubled in JSON. Use the absolute path even when the
  binary is on your `PATH`: a client started from the dock or Start menu
  doesn't get your shell's `PATH`.
- The env block is optional. It points the server at a grounder model, so
  delete it if you don't run one. Every setting is an environment variable,
  listed in [`../mcp-executor.env.example`](../mcp-executor.env.example).
  See [grounder ensembles](https://docs.conductor-mcp.io/grounders/ensembles/) for setting up a grounder.
- The server name is `conductor-desktop`. Use `conductor-<host>` if you
  register several.

## Where each one goes

| Client | File | Goes in | Or add with |
|---|---|---|---|
| Claude Code | [claude-code.json](claude-code.json) | `.mcp.json` in a project; `~/.claude.json` is managed by the CLI | `claude mcp add --scope user conductor-desktop -- <path>` |
| Claude Desktop | [claude-desktop.json](claude-desktop.json) | Settings → Developer → Edit Config (`claude_desktop_config.json`) | |
| OpenAI Codex | [codex.toml](codex.toml) | `~/.codex/config.toml` (CLI, IDE extension and ChatGPT desktop app) | `codex mcp add conductor-desktop -- <path>` |
| Google Antigravity | [antigravity.json](antigravity.json) | `~/.gemini/config/mcp_config.json`, or `.agents/mcp_config.json` in a workspace; in the IDE: agent panel `…` → MCP Servers → Manage MCP Servers → View raw config | |
| Gemini CLI | [gemini-cli.json](gemini-cli.json) | `~/.gemini/settings.json`, or `.gemini/settings.json` in a project | `gemini mcp add --scope user conductor-desktop <path>` |
| Grok Build | [grok.toml](grok.toml) | `~/.grok/config.toml`, or `.grok/config.toml` in a project | `grok mcp add conductor-desktop -- <path>` |
| Cursor | [cursor.json](cursor.json) | `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project | |
| VS Code (Copilot) | [vscode.json](vscode.json) | `.vscode/mcp.json`, or the command **MCP: Open User Configuration** | `code --add-mcp '{"name":"conductor-desktop","type":"stdio","command":"<path>"}'` |
| Windsurf / Devin Desktop | [windsurf.json](windsurf.json) | Cascade `…` menu → MCPs → Open MCP config file | |
| opencode | [opencode.jsonc](opencode.jsonc) | `~/.config/opencode/opencode.json`, or `opencode.json` in a project | |
| Kilo Code | [kilo.jsonc](kilo.jsonc) | `~/.config/kilo/kilo.json`, or `kilo.json` in a project | `kilo mcp add` (interactive) |
| goose | [goose.yaml](goose.yaml) | under `extensions:` in `~/.config/goose/config.yaml` | `goose configure` → Add Extension |
| Cline | [cline.json](cline.json) | MCP Servers icon → Configure → Configure MCP Servers (`~/.cline/data/settings/cline_mcp_settings.json`) | `cline mcp add conductor-desktop --yes -- <path>` |
| Zed | [zed.json](zed.json) | merge into `settings.json` (command **zed: open settings file**) | Settings → AI → MCP Servers → Add Server |
| Continue | [continue.yaml](continue.yaml) | `.continue/mcpServers/conductor-desktop.yaml` in a workspace | |
| pi | [../pi-coding-agent/](../pi-coding-agent/) | `~/.pi/agent/mcp.json`, plus the extensions there | |

Most clients need a restart or a reload of their MCP list after an edit.

## Things that differ between clients

**Timeouts.** A `wait` or `wait_for` call can run for 60 seconds, plus the
screenshot after it. Clients with a shorter limit get a raised one in their
file: Codex and Cline (60 s by default), Zed (60 s, in seconds). Claude Code,
Gemini CLI, Antigravity, goose and opencode allow enough by default. Watch the
units: Cline, Zed, goose and Codex use seconds; Claude Code, Gemini CLI,
opencode and Kilo use milliseconds. In opencode and Kilo, a `timeout` also
caps every tool call, so the files leave it out.

**Images.** Screenshots come back as image content. Claude Code, Claude
Desktop, Gemini CLI, Cursor, Zed and opencode show them to the model.
**Continue rejects them**, so its file runs the server text-only
(`CONDUCTOR_DESKTOP_TEXT_ONLY=true`, which needs a grounder). The other
clients' docs don't say. If a client drops or errors on images, use the same
setting.

**Tool count.** The server exposes about 43 tools by default. Windsurf/Devin's
Cascade agent takes 100 at most and VS Code 128 per request. Older Cursor
builds sent only the first 40; if tools at the end of the list are missing,
turn off ones you don't use in Cursor's MCP settings.

**Environment.** Gemini CLI strips inherited variables whose names look like
secrets (`*TOKEN*`, `*KEY*`, `*SECRET*`, ...), and Codex forwards only what's in
`env` or `env_vars`. Put anything the server needs, such as a grounder API key,
in the file's env block rather than relying on your shell.

**Grok.** Grok Build also reads `~/.claude.json`, `.mcp.json` and
`.cursor/mcp.json`. A server added for Claude Code or Cursor may already show
up in `grok mcp list` without `grok.toml`. The grok.com apps only take remote
connectors on a public URL, not local servers.

**Roo Code** was discontinued in 2026; Kilo Code and Cline are the
maintained forks.
