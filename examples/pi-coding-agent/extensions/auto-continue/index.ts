import { Type } from "@sinclair/typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  let consecutiveAutoContinues = 0;
  const MAX_AUTO = 40;          // hard ceiling
  let taskActive = false;

  pi.registerTool({
    name: "task_complete",
    description: "Call this when the assigned task is fully complete. Stops the autonomous loop.",
    parameters: Type.Object({
      summary: Type.String({ description: "Brief summary of what was accomplished" }),
    }),
    execute: async (_id, params) => ({
      content: [{ type: "text", text: `Task complete: ${params.summary}` }],
      terminate: true,
    }),
  });

  pi.registerCommand("/task", async (args, ctx) => {
    taskActive = true;
    consecutiveAutoContinues = 0;
    await pi.sendUserMessage(args.join(" "));
  });

  // Your CUA MCP should expose a `task_complete` tool that returns terminate:true
  pi.on("tool_call", async (event) => {
    if (event.toolName === "task_complete") taskActive = false;
  });

  pi.on("agent_end", async (event, ctx) => {
    if (!taskActive) return;
    if (consecutiveAutoContinues >= MAX_AUTO) {
      ctx.ui.notify("Auto-continue ceiling hit", "warn");
      taskActive = false;
      return;
    }
    consecutiveAutoContinues++;
    // Reset counter whenever a tool call actually happens — handle in tool_result
    pi.sendMessage(
      { customType: "auto-continue", content: "Continue with the task. If complete, call task_complete." },
      { deliverAs: "followUp", triggerTurn: true },
    );
  });

  pi.on("tool_result", async () => {
    consecutiveAutoContinues = 0;  // progress = reset
  });
}
