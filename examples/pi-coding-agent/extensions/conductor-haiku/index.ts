// Conductor for Pi on Haiku — Pi extension adapter.
//
// All tool logic lives in ./tools.ts and is shared with mcp-server.ts so
// the same surface can be exposed to Claude Code via MCP. This file is the
// thin wrapper that hands each tool to Pi.
//
// Bypasses the Python MCP server entirely; the Rust toolchain on Haiku
// can't compile pydantic-core (TLS_MAX_KEYS=64 platform ceiling), and
// pip-over-HTTPS reads time out on Haiku's TLS stack.
//
// task_complete is owned by the auto-continue extension; do not register
// it here or Pi will refuse to start with a tool-name conflict.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { TOOLS } from "./tools";

export default function (pi: ExtensionAPI) {
    for (const tool of TOOLS) {
        pi.registerTool({
            name: tool.name,
            label: tool.name,
            description: tool.description,
            parameters: tool.parameters,
            execute: async (_id, params) => {
                const r = await tool.execute(params);
                return { ...r, details: undefined };
            },
        });
    }
}
