// tools.ts — shared tool implementations for the Haiku conductor extension.
//
// Consumed by:
//   - index.ts        (Pi extension adapter)
//   - mcp-server.ts   (MCP stdio server adapter for Claude Code et al.)
//
// All tool logic lives here. The two adapters are thin wrappers that only
// know how to register tools with their respective host.

import { Type, type TSchema, type Static } from "@sinclair/typebox";
import { spawn } from "node:child_process";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

const HOME = os.homedir();
export const INPUT_SEND = `${HOME}/config/non-packaged/bin/input_send`;
export const WINDOW_LISTER = `${HOME}/config/non-packaged/bin/window_lister`;
export const CONDUCTOR_REPO = "/boot/home/conductor";

// Grounder endpoint — vision-capable VLM (e.g. Qwen3-VL) for `locate` / `read_screen`.
// Override with CONDUCTOR_GROUNDER_URL env var.
export const GROUNDER_URL = process.env.CONDUCTOR_GROUNDER_URL || "http://grounder.lan:9134";

const GROUND_BBOX_SYSTEM =
    "You are a visual grounding assistant. " +
    "Identify the bounding box of the described UI element in pixel coordinates " +
    "of the input image. The origin is at the top-left of the image; " +
    "(x1,y1) names the top-left of the bounding box and (x2,y2) the bottom-right. " +
    "Respond ONLY with JSON in this exact form: {\"bbox_2d\": [x1, y1, x2, y2]}. " +
    "If the described element is not visible, respond with: {\"bbox_2d\": null}.";

const DESCRIBE_SYSTEM =
    "You describe the visible content of a computer screen for a text-only " +
    "agent that cannot see images. Report concretely: which application/window " +
    "is in focus, key UI elements and their state (buttons, tabs, toggles, " +
    "input fields with their current values), any visible text content, and " +
    "any dialogs or overlays. Do not speculate about hidden content. " +
    "Aim for 120-220 words. Plain prose, no markdown headers.";

// ── result types both adapters can emit ────────────────────────────────
export type TextBlock = { type: "text"; text: string };
export type ImageBlock = { type: "image"; data: string; mimeType: string };
export type ContentBlock = TextBlock | ImageBlock;

export interface ToolResult {
    content: ContentBlock[];
    isError?: boolean;
}

// ── tool definition shape ──────────────────────────────────────────────
export interface ToolDef<S extends TSchema = TSchema> {
    name: string;
    description: string;
    parameters: S;
    execute: (params: Static<S>) => Promise<ToolResult>;
}

// ── helpers ────────────────────────────────────────────────────────────
interface RunResult {
    stdout: string;
    stderr: string;
    code: number;
}

function run(cmd: string, args: string[], timeoutMs = 8000, env?: NodeJS.ProcessEnv): Promise<RunResult> {
    return new Promise((resolve, reject) => {
        const proc = spawn(cmd, args, {
            stdio: ["ignore", "pipe", "pipe"],
            env: env ?? process.env,
        });
        let stdout = "";
        let stderr = "";
        const timer = setTimeout(() => {
            proc.kill("SIGKILL");
            reject(new Error(`${cmd} timed out after ${timeoutMs}ms`));
        }, timeoutMs);
        proc.stdout.on("data", (d) => (stdout += d.toString("utf8")));
        proc.stderr.on("data", (d) => (stderr += d.toString("utf8")));
        proc.on("close", (code) => {
            clearTimeout(timer);
            resolve({ stdout, stderr, code: code ?? -1 });
        });
        proc.on("error", (e) => {
            clearTimeout(timer);
            reject(e);
        });
    });
}

async function runPythonBackend(snippet: string): Promise<RunResult> {
    return run("python3", ["-c", snippet], 10000, {
        ...process.env,
        PYTHONPATH: CONDUCTOR_REPO,
    });
}

function textContent(text: string): ToolResult {
    return { content: [{ type: "text", text }] };
}

function errorContent(prefix: string, r: RunResult): ToolResult {
    const detail = (r.stderr || r.stdout || "(no output)").trim().slice(0, 500);
    return {
        content: [{ type: "text", text: `${prefix} — rc=${r.code}: ${detail}` }],
        isError: true,
    };
}

async function captureScreenshotBase64(): Promise<{ b64: string; w: number; h: number } | null> {
    const tmp = path.join(os.tmpdir(), `pi_shot_${Date.now()}.png`);
    await run("screenshot", ["--silent", "--mouse-pointer", tmp], 8000);
    try {
        const buf = await fs.readFile(tmp);
        await fs.unlink(tmp).catch(() => {});
        if (buf.length < 100 || buf.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") return null;
        const w = buf.readUInt32BE(16);
        const h = buf.readUInt32BE(20);
        return { b64: buf.toString("base64"), w, h };
    } catch {
        return null;
    }
}

async function callGrounder(systemPrompt: string, userText: string, imageB64: string): Promise<string | null> {
    const payload = {
        messages: [
            { role: "system", content: systemPrompt },
            {
                role: "user",
                content: [
                    { type: "image_url", image_url: { url: `data:image/png;base64,${imageB64}` } },
                    { type: "text", text: userText },
                ],
            },
        ],
        temperature: 0,
        max_tokens: 256,
    };
    try {
        const res = await fetch(`${GROUNDER_URL}/v1/chat/completions`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(30000),
        });
        if (!res.ok) return null;
        const data: any = await res.json();
        return data?.choices?.[0]?.message?.content ?? null;
    } catch {
        return null;
    }
}

function parseBbox(content: string): [number, number, number, number] | null {
    const m = content.match(/"bbox_2d"\s*:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]/);
    if (!m) return null;
    return [parseInt(m[1]!), parseInt(m[2]!), parseInt(m[3]!), parseInt(m[4]!)];
}

const KEY_TABLE: Record<string, [number, string]> = {
    enter: [0x0a, "\n"],
    return: [0x0a, "\n"],
    tab: [0x09, "\t"],
    esc: [0x1b, "\x1b"],
    escape: [0x1b, "\x1b"],
    backspace: [0x08, "\x08"],
    delete: [0x7f, "\x7f"],
    space: [0x20, " "],
    up: [0x1e, "\x1e"],
    down: [0x1f, "\x1f"],
    left: [0x1c, "\x1c"],
    right: [0x1d, "\x1d"],
    home: [0x01, "\x01"],
    end: [0x04, "\x04"],
    pageup: [0x0b, "\x0b"],
    pagedown: [0x0c, "\x0c"],
};

const MOD_BITS: Record<string, number> = {
    shift: 0x01,
    ctrl: 0x02,
    control: 0x02,
    cmd: 0x02,
    command: 0x02,
    alt: 0x04,
    option: 0x40,
    opt: 0x40,
    menu: 0x80,
};

function resolveKey(name: string): [number, string] {
    const lower = name.toLowerCase();
    if (KEY_TABLE[lower]) return KEY_TABLE[lower];
    if (name.length === 1) return [name.charCodeAt(0), name];
    return [name.charCodeAt(0) || 0, name];
}

const BUTTONS: Record<string, number> = { left: 1, middle: 4, right: 2 };

function sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
}

// ── tools ──────────────────────────────────────────────────────────────
export const TOOLS: ToolDef<any>[] = [
    {
        name: "capture_screen",
        description:
            "Capture the Haiku desktop and return a PNG image. This is NOT your " +
            "default way to observe — prefer `read_screen` for a fast prose read. " +
            "Reach for `capture_screen` only when you need the raw pixels.",
        parameters: Type.Object({}),
        execute: async (): Promise<ToolResult> => {
            const tmp = path.join(os.tmpdir(), `pi_shot_${Date.now()}.png`);
            // Haiku's `screenshot` CLI returns nonzero rc even on success;
            // ignore exit code and verify the PNG was actually written.
            await run("screenshot", ["--silent", "--mouse-pointer", tmp], 8000);
            try {
                const buf = await fs.readFile(tmp);
                await fs.unlink(tmp).catch(() => {});
                if (buf.length < 100 || buf.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") {
                    return textContent(`screenshot wrote ${buf.length} bytes but not a valid PNG`);
                }
                return {
                    content: [{ type: "image", data: buf.toString("base64"), mimeType: "image/png" }],
                };
            } catch (e) {
                return textContent(`screenshot read failed: ${(e as Error).message}`);
            }
        },
    },

    {
        name: "list_windows",
        description:
            "List all visible windows on the current workspace with title, app signature, bounds, and focus state.",
        parameters: Type.Object({}),
        execute: async () => {
            const r = await run(WINDOW_LISTER, [], 5000);
            if (r.code !== 0) return errorContent("window_lister failed", r);
            try {
                const entries: any[] = JSON.parse(r.stdout);
                const lines = entries.map((w) => {
                    const x = w.left, y = w.top;
                    const wd = Math.max(0, w.right - w.left + 1);
                    const ht = Math.max(0, w.bottom - w.top + 1);
                    const focus = w.focused ? "*" : " ";
                    const mini = w.is_mini ? " [min]" : "";
                    return `${focus} ${w.server_token.toString().padStart(4)} ${JSON.stringify(w.name)} (${w.app_signature}) ${x},${y} ${wd}×${ht}${mini}`;
                });
                return textContent(lines.join("\n"));
            } catch (e) {
                return textContent(`window_lister JSON parse: ${(e as Error).message}\n${r.stdout.slice(0, 500)}`);
            }
        },
    },

    {
        name: "focus_window",
        description: "Bring a window matching `title` substring to the front. Returns true on success.",
        parameters: Type.Object({
            title: Type.String({ description: "Substring to match against window title or app signature" }),
        }),
        execute: async (params: { title: string }) => {
            const py = `
import asyncio, sys
sys.path.insert(0, "${CONDUCTOR_REPO}")
import autopilot.backends.haiku as hk
ok = asyncio.run(hk.focus_window(${JSON.stringify(params.title)}))
print("ok" if ok else "no_match")
`.trim();
            const r = await runPythonBackend(py);
            if (r.code !== 0) return errorContent("focus_window failed", r);
            return textContent(r.stdout.trim() === "ok" ? "focused" : `no window matching ${params.title}`);
        },
    },

    {
        name: "click_at",
        description:
            "Synthetic mouse click at absolute screen pixel (x, y). Default left button, single click.",
        parameters: Type.Object({
            x: Type.Integer(),
            y: Type.Integer(),
            button: Type.Optional(Type.Union([Type.Literal("left"), Type.Literal("middle"), Type.Literal("right")])),
            clicks: Type.Optional(Type.Integer({ minimum: 1, maximum: 3 })),
        }),
        execute: async (params: { x: number; y: number; button?: "left" | "middle" | "right"; clicks?: number }) => {
            const btn = BUTTONS[params.button ?? "left"] ?? 1;
            const clicks = params.clicks ?? 1;
            const r = await run(INPUT_SEND, ["clik", String(params.x), String(params.y), String(btn), String(clicks)]);
            if (r.code !== 0) return errorContent("click_at failed", r);
            return textContent(`clicked (${params.x}, ${params.y}) button=${params.button ?? "left"} clicks=${clicks}`);
        },
    },

    {
        name: "mouse_move",
        description: "Move the cursor to (x, y) without clicking.",
        parameters: Type.Object({ x: Type.Integer(), y: Type.Integer() }),
        execute: async (params: { x: number; y: number }) => {
            const r = await run(INPUT_SEND, ["mmov", String(params.x), String(params.y)]);
            if (r.code !== 0) return errorContent("mouse_move failed", r);
            return textContent(`moved to (${params.x}, ${params.y})`);
        },
    },

    {
        name: "scroll",
        description: "Scroll the wheel at the current cursor position. Positive amount = down/right.",
        parameters: Type.Object({
            direction: Type.Union([
                Type.Literal("up"), Type.Literal("down"),
                Type.Literal("left"), Type.Literal("right"),
            ]),
            amount: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
        }),
        execute: async (params: { direction: "up" | "down" | "left" | "right"; amount?: number }) => {
            const amt = params.amount ?? 3;
            let dx = 0, dy = 0;
            if (params.direction === "down") dy = amt;
            else if (params.direction === "up") dy = -amt;
            else if (params.direction === "right") dx = amt;
            else if (params.direction === "left") dx = -amt;
            const r = await run(INPUT_SEND, ["scrl", dx.toFixed(2), dy.toFixed(2)]);
            if (r.code !== 0) return errorContent("scroll failed", r);
            return textContent(`scrolled ${params.direction} by ${amt}`);
        },
    },

    {
        name: "type_text",
        description:
            "Type a UTF-8 string into the focused text field. Click into the field first if not already focused.",
        parameters: Type.Object({ text: Type.String() }),
        execute: async (params: { text: string }) => {
            const r = await run(INPUT_SEND, ["type", params.text]);
            if (r.code !== 0) return errorContent("type_text failed", r);
            return textContent(`typed ${params.text.length} chars`);
        },
    },

    {
        name: "key_press",
        description:
            'Press a single named key. Examples: "enter", "esc", "tab", "backspace", "up", "a", "1", "$".',
        parameters: Type.Object({ key: Type.String() }),
        execute: async (params: { key: string }) => {
            const [raw, bytes] = resolveKey(params.key);
            const down = await run(INPUT_SEND, ["kdwn", String(raw), "0", bytes]);
            if (down.code !== 0) return errorContent("key down failed", down);
            const up = await run(INPUT_SEND, ["kup", String(raw), "0", bytes]);
            if (up.code !== 0) return errorContent("key up failed", up);
            return textContent(`pressed ${params.key}`);
        },
    },

    {
        name: "hotkey",
        description:
            'Press a key chord. Example: ["ctrl", "c"] for copy, ["ctrl", "shift", "s"] for save-as. ' +
            'On Haiku, "ctrl" maps to the Cmd key (the primary shortcut modifier).',
        parameters: Type.Object({
            keys: Type.Array(Type.String(), { minItems: 1, maxItems: 5 }),
        }),
        execute: async (params: { keys: string[] }) => {
            let mods = 0;
            let mainKey: string | null = null;
            for (const k of params.keys) {
                const bit = MOD_BITS[k.toLowerCase()];
                if (bit !== undefined) mods |= bit;
                else mainKey = k;
            }
            if (mainKey === null) return textContent("hotkey error: at least one non-modifier key required");
            const [raw, bytes] = resolveKey(mainKey);
            const down = await run(INPUT_SEND, ["kdwn", String(raw), String(mods), bytes]);
            if (down.code !== 0) return errorContent("hotkey down failed", down);
            const up = await run(INPUT_SEND, ["kup", String(raw), String(mods), bytes]);
            if (up.code !== 0) return errorContent("hotkey up failed", up);
            return textContent(`hotkey ${params.keys.join("+")}`);
        },
    },

    {
        name: "get_clipboard",
        description: "Read the system clipboard text.",
        parameters: Type.Object({}),
        execute: async () => {
            const py = `
import asyncio, sys
sys.path.insert(0, "${CONDUCTOR_REPO}")
import autopilot.backends.haiku as hk
print(asyncio.run(hk.get_clipboard()))
`.trim();
            const r = await runPythonBackend(py);
            if (r.code !== 0) return errorContent("get_clipboard failed", r);
            return textContent(r.stdout);
        },
    },

    {
        name: "set_clipboard",
        description: "Write a string to the system clipboard.",
        parameters: Type.Object({ text: Type.String() }),
        execute: async (params: { text: string }) => {
            const escaped = JSON.stringify(params.text);
            const py = `
import asyncio, sys
sys.path.insert(0, "${CONDUCTOR_REPO}")
import autopilot.backends.haiku as hk
ok = asyncio.run(hk.set_clipboard(${escaped}))
print("ok" if ok else "fail")
`.trim();
            const r = await runPythonBackend(py);
            if (r.code !== 0) return errorContent("set_clipboard failed", r);
            return textContent(r.stdout.trim() === "ok" ? "clipboard set" : "set_clipboard returned False");
        },
    },

    {
        name: "locate",
        description:
            "Find a UI element by description and return its center pixel coordinates. " +
            "Use this when you don't know exact coordinates — pass a description like " +
            "\"the Save button in the toolbar\" or \"the StyledEdit text area\". " +
            "Returns 'x,y' on success or 'no_match' if not visible.",
        parameters: Type.Object({
            target: Type.String({ description: "Natural-language description of what to locate." }),
        }),
        execute: async (params: { target: string }) => {
            const shot = await captureScreenshotBase64();
            if (!shot) return textContent("locate failed: screenshot capture failed");
            const content = await callGrounder(
                GROUND_BBOX_SYSTEM,
                `Identify the bounding box of: ${params.target}`,
                shot.b64,
            );
            if (content === null) return textContent("locate failed: grounder unreachable");
            if (/bbox_2d"\s*:\s*null/.test(content)) {
                return textContent(`no_match: '${params.target}' not visible on screen`);
            }
            const bbox = parseBbox(content);
            if (!bbox) return textContent(`locate parse failed; raw: ${content.slice(0, 200)}`);
            const [x1, y1, x2, y2] = bbox;
            const cx = Math.round((x1 + x2) / 2);
            const cy = Math.round((y1 + y2) / 2);
            return textContent(`${cx},${cy}  (bbox ${x1},${y1} ${x2},${y2}; image ${shot.w}×${shot.h})`);
        },
    },

    {
        name: "read_screen",
        description:
            "Your DEFAULT, fast way to observe: get a prose description of what's " +
            "currently visible on the desktop, from a vision model that can see the " +
            "screen. Prefer this over `capture_screen` for understanding UI state " +
            "without inferring from list_windows alone.",
        parameters: Type.Object({
            hint: Type.Optional(Type.String({
                description: "Optional bias for the description, e.g. 'focus on the rightmost panel'.",
            })),
        }),
        execute: async (params: { hint?: string }) => {
            const shot = await captureScreenshotBase64();
            if (!shot) return textContent("read_screen failed: screen capture failed");
            const content = await callGrounder(
                DESCRIBE_SYSTEM,
                params.hint || "Describe everything visible on the screen.",
                shot.b64,
            );
            if (content === null) return textContent("read_screen failed: grounder unreachable");
            return textContent(content);
        },
    },

    {
        name: "exec_shell",
        description: "Run a shell command and return stdout/stderr. Default timeout 30s.",
        parameters: Type.Object({
            command: Type.String(),
            timeout_seconds: Type.Optional(Type.Integer({ minimum: 1, maximum: 300 })),
        }),
        execute: async (params: { command: string; timeout_seconds?: number }) => {
            const r = await run("/bin/sh", ["-c", params.command], (params.timeout_seconds ?? 30) * 1000);
            const text = `--- exit=${r.code} ---\n--- stdout ---\n${r.stdout}\n--- stderr ---\n${r.stderr}`;
            return textContent(text.length > 8000 ? text.slice(0, 8000) + "\n... (truncated)" : text);
        },
    },

    {
        name: "check_input",
        description:
            "Probe the input pipeline. Returns 'ok' if input_server has loaded the conductor_input " +
            "add-on and is delivering events; returns 'stale' if the add-on's port is not registered " +
            "(observation will still work, but click/type/key are silently dropped). " +
            "If stale, call restart_input_server.",
        parameters: Type.Object({}),
        execute: async () => {
            // input_send exit codes:
            //   0 = delivered (add-on loaded)
            //   1 = port not found (add-on not loaded) — the "input dead" symptom
            //   2 = usage error
            //   3 = flatten/write failure
            try {
                const r = await run(INPUT_SEND, ["ping"], 3000);
                if (r.code === 0) return textContent("ok: conductor_input add-on is registered and responsive");
                if (r.code === 1) {
                    return {
                        content: [{
                            type: "text" as const,
                            text:
                                "stale: input_send reports the conductor_input port is not registered. " +
                                "input_server has not loaded the add-on. Call restart_input_server, " +
                                "or run setup/bounce-input-server.sh manually. " +
                                `Detail: ${r.stderr.trim().slice(0, 200)}`,
                        }],
                        isError: true,
                    };
                }
                return errorContent(`check_input unexpected rc=${r.code}`, r);
            } catch (e) {
                return { content: [{ type: "text" as const, text: `check_input failed to spawn: ${(e as Error).message}` }], isError: true };
            }
        },
    },

    {
        name: "restart_input_server",
        description:
            "Restart Haiku's input_server. Use only when check_input reports 'stale' — input_server " +
            "owns the desktop's keyboard and mouse, so this briefly freezes input until launch_daemon " +
            "respawns it (usually <2s). Verifies recovery via input_send ping before returning.",
        parameters: Type.Object({}),
        execute: async () => {
            // Kill input_server; launch_daemon respawns it (it's a managed system service).
            const killR = await run("killall", ["input_server"], 5000).catch((e: Error) => ({
                stdout: "", stderr: e.message, code: -1,
            }));
            // killall returns 0 even if it killed nothing? On Haiku, rc=0 means killed; rc=1 means no match.
            // Either way, we want to ping until the add-on is back.
            for (let i = 0; i < 10; i++) {
                await sleep(500);
                const ping = await run(INPUT_SEND, ["ping"], 2000).catch(() => null);
                if (ping && ping.code === 0) {
                    return textContent(
                        `restart_input_server ok: input_server respawned and conductor_input is responsive ` +
                        `(after ${(i + 1) * 500}ms; killall rc=${killR.code})`,
                    );
                }
            }
            return {
                content: [{
                    type: "text" as const,
                    text:
                        "restart_input_server: ping never recovered within 5s. input_server may not have " +
                        "been respawned by launch_daemon, or the conductor_input add-on may have been " +
                        `removed from ~/config/non-packaged/add-ons/input_server/devices/. killall rc=${killR.code} stderr=${killR.stderr.trim().slice(0, 200)}`,
                }],
                isError: true,
            };
        },
    },
];
