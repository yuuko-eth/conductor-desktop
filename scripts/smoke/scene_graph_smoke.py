"""Phase 2 smoke on ubuntu-gnome GNOME Wayland: real backend → DAG composition."""
import asyncio
import json
import time

from conductor_desktop.core.backends import linux_gnome as gn
from conductor_desktop.core.models.scene_graph import (
    build_scene_graph,
    scene_graph_to_dict,
)


async def main():
    t0 = time.perf_counter()
    windows = await gn.list_windows()
    screens = await gn.list_screens()
    t_fetch = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    sg = build_scene_graph(target="host", windows=windows, screens=screens)
    t_compose = (time.perf_counter() - t0) * 1000

    print(f"fetch: {t_fetch:.1f}ms  /  compose: {t_compose:.2f}ms")
    print(f"target={sg.target}  active_screen={sg.active_screen}")
    print(f"#screens={len(sg.screens)}  #spaces={len(sg.spaces)}  #windows={len(sg.windows)}")
    print()
    print("SCREENS:")
    for sid, scr in sg.screens.items():
        print(f"  {sid}: {scr.name!r} primary={scr.primary} bounds={scr.bounds}")
        print(f"     current_space={scr.current_space!r}  focused_window={scr.focused_window!r}")
        print(f"     spaces={scr.spaces}")
    print()
    print("SPACES (sorted by id):")
    for space_id in sorted(sg.spaces):
        sp = sg.spaces[space_id]
        print(f"  {space_id}:")
        print(f"     label={sp.label!r}  src={sp.label_source!r}  screens={sp.screen_ids}")
        print("     windows (top→bottom):")
        for wid in sp.windows:
            w = sg.windows[wid]
            marker = "★" if w.focused else " "
            print(f"       {marker} z={w.z} m={w.monitor} {w.process_name[:20]:<20} {w.title[:50]!r}")
    print()
    # JSON round-trip check.
    encoded = json.dumps(scene_graph_to_dict(sg), default=str)
    print(f"JSON encode: OK ({len(encoded)} bytes)")


asyncio.run(main())
