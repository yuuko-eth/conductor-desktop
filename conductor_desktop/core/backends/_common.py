"""Cross-platform subsystems: shell and screenshot.

These work on all platforms via asyncio.subprocess and mss.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..errors import BackendError

log = logging.getLogger(__name__)

# ── Shell ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int


async def shell_exec(command: str, timeout: float = 30) -> ShellResult:
    """Run ``command`` through the platform shell. A non-zero exit is a result,
    not an error; a command that can't start or runs out of time raises."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise BackendError(f"could not start the shell: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackendError(f"command timed out after {timeout:g}s") from None
    return ShellResult(stdout.decode("utf-8", errors="replace"),
                       stderr.decode("utf-8", errors="replace"),
                       proc.returncode or 0)


# ── Screen attribution ─────────────────────────────────────────────


def attribute_monitor(bounds: list[int] | tuple[int, int, int, int],
                      screens: list[dict]) -> int:
    """Return the index of the screen that contains the window's centerpoint.

    `bounds` is [x, y, w, h] in virtual-desktop coords. `screens` is the
    list returned by a backend's `list_screens()` (each with `bounds`
    and `index`). Returns -1 if no screen contains the centerpoint —
    e.g. window is fully off-screen, on a disconnected monitor, or
    minimized to coords outside the virtual desktop.
    """
    if not bounds or not screens:
        return -1
    x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
    cx = x + w // 2
    cy = y + h // 2
    for s in screens:
        sb = s.get("bounds") or [0, 0, 0, 0]
        sx, sy, sw, sh = sb[0], sb[1], sb[2], sb[3]
        if sx <= cx < sx + sw and sy <= cy < sy + sh:
            return int(s.get("index", -1))
    return -1


# ── Screenshot ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Screenshot:
    png_b64: str
    width: int
    height: int


def _is_blank(pil_img: Image.Image) -> bool:
    """Check if image is all-black (failed capture).

    Only rejects pure black (0,0) — a solid-color desktop wallpaper
    is a valid screenshot, not a capture failure.
    """
    extrema = pil_img.convert("L").getextrema()
    return extrema == (0, 0)  # all-black = failed capture


def _encode(pil_img: Image.Image) -> Screenshot:
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return Screenshot(base64.b64encode(buf.getvalue()).decode("ascii"),
                      pil_img.width, pil_img.height)


def _crop_to_screen(pil_img: Image.Image, screen: dict | None) -> Image.Image:
    """PIL-crop a full-virtual-desktop capture to a single screen's bounds.

    Used for backends with no native per-output flag (spectacle, GNOME
    D-Bus, gnome-screenshot, scrot). Assumes the input image dimensions
    equal the virtual-desktop extent; clamps to the image edges so
    rounding / off-by-one mismatches don't raise.
    """
    if screen is None:
        return pil_img
    x, y, w, h = screen.get("bounds") or [0, 0, pil_img.width, pil_img.height]
    # Translate from virtual-desktop coords to image coords. With mss the
    # virtual desktop origin can be negative (monitor-left-of-primary);
    # other capture paths normalize to (0,0). The mss path bypasses this
    # helper, so assume origin (0,0) here.
    left = max(0, x)
    top = max(0, y)
    right = min(pil_img.width, x + w)
    bottom = min(pil_img.height, y + h)
    if right <= left or bottom <= top:
        return pil_img
    return pil_img.crop((left, top, right, bottom))


def _mss_grab(screen: int | None, display: str | None = None) -> Image.Image:
    """Grab with mss. ``screen`` indexes ``sct.monitors[1:]``; None is the
    whole virtual desktop. Blocking: call through ``asyncio.to_thread``."""
    import mss as _mss

    kwargs = {"display": display} if display else {}
    with _mss.mss(**kwargs) as sct:
        # monitors[0] = full virtual screen (all monitors), [1..] = per-monitor.
        # mss's native per-monitor capture is the only path that handles
        # negative virtual-desktop origins (monitor-left-of-primary) without
        # manual offset math, so prefer it for a single screen.
        if screen is not None and len(sct.monitors) > screen + 1:
            monitor = sct.monitors[screen + 1]
        else:
            monitor = sct.monitors[0]
        img = sct.grab(monitor)
        return Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")


async def _capture_to_file(argv: list[str], ok, target: dict | None) -> Screenshot | None:
    """Run a capture tool that writes a PNG to the path standing in for
    ``{path}`` in ``argv``; None when it fails. The temporary directory goes
    away however the attempt ends. ``ok(returncode, stdout)`` judges success."""
    with tempfile.TemporaryDirectory(prefix="conductor-shot-") as tmpdir:
        path = str(Path(tmpdir) / "shot.png")
        proc = await asyncio.create_subprocess_exec(
            *[path if a == "{path}" else a for a in argv],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.debug("screenshot: %s timed out", argv[0])
            return None
        if not ok(proc.returncode, stdout) or not os.path.exists(path) or not os.path.getsize(path):
            log.debug("screenshot: %s returned %d: %s", argv[0], proc.returncode,
                      (stderr or stdout).decode(errors="replace")[:200])
            return None
        with Image.open(path) as im:
            return _encode(_crop_to_screen(im, target))


async def take_screenshot(screen: int | None = None,
                          screens: list[dict] | None = None) -> Screenshot:
    """Capture the screen as a PNG; raise BackendError when no method works.

    Default (``screen=None``): captures the full virtual desktop spanning
    every monitor. Pass ``screen=<index>`` plus the ``screens`` list returned
    by a backend's :func:`list_screens` to grab one monitor.

    Per-monitor strategy by backend:
    - mss: ``sct.monitors[screen+1]`` (native, fast).
    - grim: ``-o <screens[screen].name>`` (native, fast).
    - spectacle / GNOME D-Bus / gnome-screenshot / scrot: capture full
      virtual desktop, then PIL-crop to ``screens[screen].bounds``.

    Fallback chain: mss → mss on XWayland → grim → spectacle (KDE) →
    GNOME D-Bus → gnome-screenshot → scrot (X11 fallback for platforms mss
    rejects, notably FreeBSD, where it raises ScreenShotError on import).
    """
    # Resolve the single-screen target once; None means "full virtual desktop".
    target: dict | None = None
    if screen is not None:
        if not screens or screen < 0 or screen >= len(screens):
            raise BackendError(f"screen index {screen} out of range "
                               f"(0..{len(screens) - 1 if screens else -1})")
        target = screens[screen]

    # 1. mss (X11, macOS, Windows). 2. mss on XWayland's :0 — GNOME Wayland
    # usually runs one — passed to mss rather than set in os.environ, which
    # would change DISPLAY for everything else in the process.
    attempts = [("mss", None)]
    if not os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        attempts.append(("mss on XWayland", ":0"))
    for label, display in attempts:
        try:
            pil_img = await asyncio.to_thread(_mss_grab, screen if target else None, display)
        except Exception as e:  # mss raises its own ScreenShotError, Xlib errors, ...
            log.debug("screenshot: %s failed: %s", label, e, exc_info=True)
            continue
        if not _is_blank(pil_img):
            return _encode(pil_img)
        log.debug("screenshot: %s returned a blank %dx%d image", label, *pil_img.size)

    # 3. grim (wlroots Wayland) writes the PNG to stdout.
    try:
        grim_args = ["grim", "-t", "png"]
        if target is not None and target.get("name"):
            grim_args += ["-o", target["name"]]
        proc = await asyncio.create_subprocess_exec(
            *grim_args, "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0 and stdout:
            with Image.open(io.BytesIO(stdout)) as im:
                return _encode(im)
        log.debug("screenshot: grim returned %d: %s", proc.returncode, stderr.decode()[:200])
    except (OSError, TimeoutError) as e:
        log.debug("screenshot: grim failed: %s", e)

    file_tools = [
        # 4. spectacle (KDE Plasma Wayland): kwin has no wlr-screencopy, so
        #    grim fails; spectacle uses org.kde.KWin.ScreenShot2 directly.
        #    -b background, -n no notification, -f full virtual desktop.
        (["spectacle", "-b", "-n", "-f", "-o", "{path}"], lambda rc, out: rc == 0),
        # 5. GNOME Shell's D-Bus screenshot (GNOME Wayland).
        (["gdbus", "call", "--session", "--dest", "org.gnome.Shell.Screenshot",
          "--object-path", "/org/gnome/Shell/Screenshot",
          "--method", "org.gnome.Shell.Screenshot.Screenshot", "false", "true", "{path}"],
         lambda rc, out: rc == 0 and b"true" in out.lower()),
        # 6. gnome-screenshot CLI (older GNOME).
        (["gnome-screenshot", "-f", "{path}"], lambda rc, out: rc == 0),
        # 7. scrot — last-resort X11 fallback, used on FreeBSD. --overwrite:
        #    otherwise scrot writes to a NEW name next to an existing path.
        (["scrot", "--overwrite", "-z", "{path}"], lambda rc, out: rc == 0),
    ]
    for argv, ok in file_tools:
        try:
            shot = await _capture_to_file(argv, ok, target)
        except OSError as e:  # the tool isn't installed
            log.debug("screenshot: %s failed: %s", argv[0], e)
            continue
        if shot is not None:
            return shot

    raise BackendError("screenshot failed — no working method (mss, grim, spectacle, "
                       "gdbus, gnome-screenshot, scrot); see the conductor-desktop log")
