import { Type } from "@sinclair/typebox";
import { readFile } from "node:fs/promises";
import { basename, join } from "node:path";
import { tmpdir } from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const pexec = promisify(execFile);

// show_screen — send a screenshot to the Discord thread the agent is replying in.
//
// openab relays TEXT only (see openab docs/sendimages.md): images must be POSTed
// to the Discord REST API directly. Both inputs come from what openab already
// hands us, so there is nothing new to configure beyond one env line:
//   - target thread  <- the <sender_context> block openab injects into each
//                        human message (arrives as an `input` event, source "rpc").
//   - bot token      <- DISCORD_BOT_TOKEN, forwarded via [agent].env in
//                        ~/.openab/config.toml (reuses the same token.env key
//                        openab itself loads; openab does NOT auto-forward it).
//
// PIXELS: prefer passing `path` from conductor's `capture_screen` — its
// executor already captures correctly on every platform (X11/Wayland/macOS/
// Windows, with DISPLAY/XAUTHORITY autodetect). The no-arg self-capture below is
// only a convenience fallback, dispatched per-OS the same way the executor is.
async function selfCapture(outPath) {
  const plat = process.platform;
  if (plat === "darwin") {
    await pexec("screencapture", ["-x", outPath]);
    return;
  }
  if (plat === "linux") {
    // X11 / Wayland / KDE grabbers — first that succeeds wins.
    const env = { ...process.env, DISPLAY: process.env.DISPLAY || ":0" };
    const tries = [
      ["gnome-screenshot", ["-f", outPath]],
      ["scrot", ["-o", outPath]],
      ["maim", [outPath]],
      ["spectacle", ["-b", "-n", "-o", outPath]],
      ["grim", [outPath]],
      ["import", ["-window", "root", outPath]], // ImageMagick
    ];
    let last;
    for (const [cmd, args] of tries) {
      try {
        await pexec(cmd, args, { env });
        return;
      } catch (e) {
        last = e;
      }
    }
    throw new Error(
      "no working screenshot tool (tried gnome-screenshot/scrot/maim/spectacle/grim/import): " +
        (last && last.message),
    );
  }
  throw new Error("self-capture unsupported on " + plat + " — pass a capture_screen path instead.");
}

export default function (pi) {
  let lastTarget = null; // thread_id (preferred) or channel_id

  pi.on("input", async (event) => {
    const m = event && event.text
      ? event.text.match(/<sender_context>\s*([\s\S]*?)\s*<\/sender_context>/)
      : null;
    if (!m) return;
    try {
      const sc = JSON.parse(m[1]);
      const t = sc.thread_id || sc.channel_id;
      if (t) lastTarget = String(t);
    } catch {
      /* malformed sender_context — ignore, keep last good target */
    }
  });

  pi.registerTool({
    name: "show_screen",
    label: "Show screen",
    description:
      "Show the human a screenshot in the Discord thread you are replying to. " +
      "openab relays your text only, never images, so this is the ONLY way the " +
      "user can actually see the screen. PREFERRED: pass the file path returned " +
      "by capture_screen (conductor captures correctly on every OS). Omit " +
      "`path` to self-capture the screen as a convenience.",
    promptSnippet:
      "show_screen(path?, caption?) - post a screenshot to the user's Discord thread (prefer a capture_screen path; they cannot see your screen otherwise).",
    parameters: Type.Object({
      path: Type.Optional(
        Type.String({ description: "PNG/JPG to send (ideally a capture_screen path). If omitted, self-captures." }),
      ),
      caption: Type.Optional(
        Type.String({ description: "Optional text to accompany the image." }),
      ),
    }),
    execute: async (_id, params) => {
      const token = process.env.DISCORD_BOT_TOKEN;
      if (!token) {
        throw new Error(
          "DISCORD_BOT_TOKEN not in env - add it to [agent].env in ~/.openab/config.toml.",
        );
      }
      if (!lastTarget) {
        throw new Error(
          "no Discord thread captured yet (no <sender_context> seen this session).",
        );
      }

      let path = params.path;
      if (!path) {
        path = join(tmpdir(), "pi-show-screen-" + Date.now() + ".png");
        try {
          await selfCapture(path);
        } catch (e) {
          throw new Error(
            "self-capture failed (" + e.message + "). Pass a capture_screen path instead.",
          );
        }
      }

      let buf;
      try {
        buf = await readFile(path);
      } catch (e) {
        throw new Error("cannot read " + path + ": " + e.message);
      }

      const form = new FormData();
      if (params.caption) form.append("content", params.caption);
      form.append("files[0]", new Blob([buf]), basename(path));

      const res = await fetch(
        "https://discord.com/api/v10/channels/" + lastTarget + "/messages",
        { method: "POST", headers: { Authorization: "Bot " + token }, body: form },
      );
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(
          "Discord upload " + res.status + " " + res.statusText + ": " + body.slice(0, 300),
        );
      }
      return { content: [{ type: "text", text: "Posted " + basename(path) + " to the thread." }] };
    },
  });
}
