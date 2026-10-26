# conductor-desktop

Desktop / mobile / web automation server speaking the [Model Context Protocol](https://modelcontextprotocol.io).

Add it to any MCP client (Claude Code, Claude Desktop, Codex, Gemini CLI, Cursor, VS Code, opencode, your own agent) and the agent can see and drive the real desktop: read_screen, capture_screen, click, type_text, hotkey, ui_find, web_eval, mobile_tap and the rest.

Three ways to see and act, because no single one covers every app:

- **Visual**: `capture_screen` shows the agent the screen and `click_at` acts on what it sees. With a grounder model configured, `read_screen`, `locate` and `click` also work by description. Works on anything, including canvases, games and remote desktops.
- **Accessibility**: [`ui_tree` / `ui_find` and the `ui_*` actions](https://docs.conductor-mcp.io/guides/native-apps/) read a native app's own control tree and act on it by reference rather than coordinates. No pointer, and a stale reference fails instead of clicking the wrong thing.
- **Web**: [`web_eval` and friends](https://docs.conductor-mcp.io/guides/browser-tabs/) work on the page itself, over CDP (Chrome) or WebDriver BiDi (Firefox).

The server tells the agent how to use all this: it sends MCP `instructions` and each tool describes itself, so there's no manual to paste in.

Because this hands an agent a real keyboard, mouse and shell, every tool call is recorded to a daily [audit log](https://docs.conductor-mcp.io/security/audit-log/): on by default, secrets redacted, pruned after two weeks.

## Quick start with Claude

```bash
git clone https://github.com/yuuko-eth/conductor-desktop && cd conductor-desktop
uv sync                              # creates .venv; Linux: see the setup guide for your desktop first
.venv/bin/conductor-desktop-check        # checks screen capture, input and the backend
```

On macOS, grant Screen Recording and Accessibility to the app that launches the server, or, if macOS asks about it instead, to the Python binary `conductor-desktop-check` names ([setup-macos.md](https://docs.conductor-mcp.io/platforms/macos/)). On Linux, the accessibility tools need the system's `python3-gi`, so follow your desktop's setup guide under [Platforms](#platforms).

**Claude Code:**

```bash
claude mcp add --scope user conductor-desktop -- "$PWD/.venv/bin/conductor-desktop"
```

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), with your own path, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "conductor-desktop": {
      "command": "/absolute/path/to/conductor-desktop/.venv/bin/conductor-desktop"
    }
  }
}
```

That's enough: Claude looks with `capture_screen` and acts with `click_at`, the `ui_*` tools and the `web_*` tools. A screenshot smaller than the screen says so, and `image_coords=true` maps its pixels back. A grounder model is optional. It adds `read_screen` (a cheap text read of the screen) and `locate` / `click` by description, which saves image tokens on long sessions; see [grounder ensembles](https://docs.conductor-mcp.io/grounders/ensembles/). Settings are environment variables, listed in [`examples/mcp-executor.env.example`](examples/mcp-executor.env.example).

Other clients (Codex, Antigravity, Gemini CLI, Grok, Cursor, VS Code, Windsurf, opencode, Kilo, goose, Cline, Zed, Continue) take the same command in their own format: [`examples/clients/`](examples/clients/) has a file for each, and where it goes.

To tell the agent about your machine (its language, the apps it should use, what to leave alone), drop a filled-in [operator briefing](examples/operator-briefing/) into your `CLAUDE.md`.

## Layout

- `conductor_desktop/`: the MCP server, tool handlers, tray, dashboard
- `conductor_desktop/core/`: platform infrastructure (executors, perception, backends)
- Docs live at [docs.conductor-mcp.io](https://docs.conductor-mcp.io/): [ui tools](https://docs.conductor-mcp.io/guides/native-apps/) for driving native apps through the accessibility tree; [web tools](https://docs.conductor-mcp.io/guides/browser-tabs/) for Chrome and Firefox tabs; [grounder ensembles](https://docs.conductor-mcp.io/grounders/ensembles/) for mixing point / describe / region models; [audit log](https://docs.conductor-mcp.io/security/audit-log/); [hardening](https://docs.conductor-mcp.io/security/hardening/) for what is optional and when you'd want it; [platform coverage](https://docs.conductor-mcp.io/platforms/) for what each OS and desktop supports; [design](https://docs.conductor-mcp.io/concepts/architecture/) for the architecture
- `examples/`: [client configs](examples/clients/), the env reference, and [operator briefings](examples/operator-briefing/)
- `setup/`: per-platform dependency installers
- `tests/`: pytest suite

## Status

Pre-release (0.1.0). Known issues:

- On Windows, `read_grid` returns only the rows on screen.
- From macOS, typing into a remote-desktop or VM window sends the wrong characters; drive the remote side with its own conductor-desktop instead.
- The HTTP transport is plain HTTP. It binds to loopback by default; off loopback it requires a bearer token (`CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE`), which crosses the network unencrypted, so tunnel over SSH on a network you don't trust.

## Platforms

macOS, Linux (X11; Wayland on GNOME, KDE Plasma and wlroots/Hyprland), FreeBSD, Windows 10/11, Android (via ADB), iOS (via WebDriverAgent). What each one supports, and where the accessibility modality is degraded: [platform coverage](https://docs.conductor-mcp.io/platforms/). Each desktop has a dependency installer in [`setup/`](setup/) and a setup guide on the [docs site](https://docs.conductor-mcp.io/platforms/): [X11](https://docs.conductor-mcp.io/platforms/linux/x11/), [GNOME Wayland](https://docs.conductor-mcp.io/platforms/linux/gnome-wayland/), [KDE Plasma](https://docs.conductor-mcp.io/platforms/linux/kde-wayland/), [macOS](https://docs.conductor-mcp.io/platforms/macos/), [Windows](https://docs.conductor-mcp.io/platforms/windows/); Linux packages and the `gi` ABI trap are collected in [Linux](https://docs.conductor-mcp.io/platforms/linux/). Haiku R1/beta5 is covered by a separate TypeScript extension, [conductor-haiku](examples/pi-coding-agent/extensions/conductor-haiku/).
