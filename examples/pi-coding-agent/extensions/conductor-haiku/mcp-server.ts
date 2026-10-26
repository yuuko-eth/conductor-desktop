#!/usr/bin/env node
// mcp-server.ts — MCP stdio server exposing the Haiku conductor tools.
//
// Same tool surface as the Pi extension (index.ts). Intended for Claude
// Code (or any MCP client) running on Haiku where the Python conductor-desktop
// server can't be installed.
//
// Run:
//   tsx mcp-server.ts           # or: node --import tsx mcp-server.ts
// Claude Code MCP entry:
//   {
//     "mcpServers": {
//       "conductor-haiku": {
//         "command": "tsx",
//         "args": ["/path/to/conductor-haiku/mcp-server.ts"],
//         "env": { "CONDUCTOR_GROUNDER_URL": "http://grounder.lan:9134" }
//       }
//     }
//   }

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
    CallToolRequestSchema,
    ListToolsRequestSchema,
    type CallToolResult,
} from "@modelcontextprotocol/sdk/types.js";
import { TOOLS } from "./tools.js";

const server = new Server(
    { name: "conductor-haiku", version: "0.1.0" },
    { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOLS.map((t) => ({
        name: t.name,
        description: t.description,
        inputSchema: t.parameters,
    })),
}));

server.setRequestHandler(CallToolRequestSchema, async (req): Promise<CallToolResult> => {
    const tool = TOOLS.find((t) => t.name === req.params.name);
    if (!tool) {
        return {
            content: [{ type: "text", text: `Unknown tool: ${req.params.name}` }],
            isError: true,
        };
    }
    try {
        const result = await tool.execute(req.params.arguments ?? {});
        return result as CallToolResult;
    } catch (e) {
        return {
            content: [{ type: "text", text: `${tool.name} threw: ${(e as Error).message}` }],
            isError: true,
        };
    }
});

const transport = new StdioServerTransport();
await server.connect(transport);
