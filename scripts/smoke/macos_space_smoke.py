"""Live smoke for Phase 4a-i: macOS NSWorkspace Space watcher.

Run this script, then in macOS swipe between Spaces (Mission Control,
Ctrl+→ / Ctrl+←, F3). The observer's drain should report `view_changed`
deltas for each Space transition.

Also confirms:
- The watcher's NSObject is strong-held (we sleep 10s; if PyObjC GC
  collected it during that window, no notifications would arrive).
- Teardown deregisters cleanly (stop() should not crash, and a second
  stop() must be a no-op).
- repeat_count rises when the user swipes quickly (LAST_WINS_KINDS).
"""
import asyncio
import os
import time


async def main():
    from conductor_desktop.core.perception.deltas.macos_observer import MacOSObserver

    duration = float(os.environ.get("SMOKE_SECONDS", "12"))
    loop = asyncio.get_running_loop()
    obs = MacOSObserver(loop)
    print(f"starting observer; will run for {duration:.0f}s — please switch Spaces (Ctrl+→ / Ctrl+←)")
    await obs.start()
    print(f"observer started. space_watcher={obs._space_watcher}")

    t_end = time.monotonic() + duration
    n_drains = 0
    last_summary = ""
    while time.monotonic() < t_end:
        await asyncio.sleep(1.0)
        deltas, raw, dropped = obs.drain_and_filter()
        if raw == 0:
            continue
        n_drains += 1
        # Summarize this drain
        vc = [d for d in deltas if d.kind == "view_changed"]
        others = [d for d in deltas if d.kind != "view_changed"]
        line = f"  drain #{n_drains}: raw={raw} kept={len(deltas)} view_changed={len(vc)} other={len(others)}"
        if vc:
            for d in vc:
                rep = d.extra.get("repeat_count", 1)
                rep_str = f" (x{rep})" if rep > 1 else ""
                line += f"\n    ★ view_changed src={d.source_id}{rep_str} scope={d.origin_scope}"
        if line != last_summary:
            print(line)
            last_summary = line

    print()
    print("tearing down...")
    await obs.stop()
    # Idempotency check.
    await obs.stop()
    print(f"observer stopped. space_watcher={obs._space_watcher} (should be None)")
    assert obs._space_watcher is None, "watcher not cleaned up!"
    print("OK — clean teardown.")


asyncio.run(main())
