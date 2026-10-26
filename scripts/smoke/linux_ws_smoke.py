"""Phase 4a-iii smoke on ubuntu-gnome GNOME Wayland.

Run with unsafe_mode enabled. Switch workspaces during the 12s window.
We should see view_changed deltas with source_id="workspace-<n>" and
origin_scope.space_id="ws:global:<n>".
"""
import asyncio
import os
import time


async def main():
    from conductor_desktop.core.perception.deltas.linux_observer import LinuxObserver

    duration = float(os.environ.get("SMOKE_SECONDS", "12"))
    loop = asyncio.get_running_loop()
    obs = LinuxObserver(loop)
    print(f"starting observer; will run for {duration:.0f}s — please switch workspaces")
    await obs.start()
    print("observer started.")
    print(f"  dbus_bus={obs._dbus_bus}")
    print(f"  poll_source_id={obs._ws_poll_source_id}")
    print(f"  last_workspace={obs._last_workspace}")

    t_end = time.monotonic() + duration
    n_drains = 0
    while time.monotonic() < t_end:
        await asyncio.sleep(1.0)
        deltas, raw, dropped = obs.drain_and_filter()
        if raw == 0:
            continue
        n_drains += 1
        vc = [d for d in deltas if d.kind == "view_changed"]
        others = [d for d in deltas if d.kind != "view_changed"]
        line = f"  drain #{n_drains}: raw={raw} kept={len(deltas)} view_changed={len(vc)} other={len(others)}"
        for d in vc:
            rep = d.extra.get("repeat_count", 1)
            rep_str = f" (x{rep})" if rep > 1 else ""
            line += (
                f"\n    ★ view_changed src={d.source_id}{rep_str}"
                f"\n      scope={d.origin_scope}"
                f"\n      extra={d.extra}"
            )
        print(line)

    print()
    print("tearing down...")
    await obs.stop()
    print(f"observer stopped. dbus_bus={obs._dbus_bus} (should be None)")
    print(f"  poll_source_id={obs._ws_poll_source_id} (should be 0)")


asyncio.run(main())
