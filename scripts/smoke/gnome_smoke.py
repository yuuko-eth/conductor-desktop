"""Phase 1 smoke for GNOME backend on Wayland (ubuntu-gnome)."""
import asyncio
import json
import time
from collections import Counter

from conductor_desktop.core.backends import linux_gnome as gn


async def main():
    t0 = time.perf_counter()
    screens = await gn.list_screens()
    print(f"SCREENS ({(time.perf_counter()-t0)*1000:.0f}ms):", json.dumps(screens))

    t0 = time.perf_counter()
    ws = await gn.list_windows()
    t = (time.perf_counter() - t0) * 1000
    print(f"WINDOWS ({t:.0f}ms / {t/max(len(ws),1):.1f}ms each, {len(ws)} total)")
    print()
    print("Per-desktop:", dict(sorted(Counter(w["desktop"] for w in ws).items())))
    print("Per-monitor:", dict(Counter(w["monitor"] for w in ws)))
    print("Layers:    ", dict(Counter(w["layer"] for w in ws)))
    print("States:    ", dict(Counter(w["state"] for w in ws)))
    print("Focused:   ", [(w["title"][:60], w["desktop"]) for w in ws if w["focused"]])
    print("Sticky:    ", [(w["title"][:40], w["process_name"]) for w in ws if w["sticky"]])
    print()
    print("Z-order top to bottom:")
    for w in sorted(ws, key=lambda x: x["z"]):
        z, d, m = w["z"], w["desktop"], w["monitor"]
        layer = w["layer"]
        state = w["state"]
        title = w["title"][:50]
        proc = w["process_name"][:20]
        print(f"  z={z:>2} d={d:>2} m={m:>2} {layer:<14} {state:<10} {proc:<20} {title}")


asyncio.run(main())
