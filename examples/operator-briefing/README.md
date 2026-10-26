# Operator briefings

The part of an agent's instructions that only the person running the machine
can write: which OS and display it is, what's installed, the interface
language, what to leave alone. Drop one into the agent's instructions file
on the host it controls: `CLAUDE.md` for Claude Code, `AGENTS.md` for Codex
or pi-coding-agent.

How to use the tools doesn't belong here: the server sends that itself, as
MCP `instructions` and in each tool's description, so every client gets it
and it stays in step with the code.

- [`AGENTS.md`](AGENTS.md): the template. Fill in the `{…}` parts.
- [`macos.md`](macos.md): filled in for a macOS host.
- [`windows.md`](windows.md): filled in for Windows 11.

These are for the agent at the keyboard, not for contributors. To work on
conductor-desktop itself, see the repository's root `CLAUDE.md`.
