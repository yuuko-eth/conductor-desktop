"""Permission self-tests run on connect: can we capture the screen and inject input?"""

from __future__ import annotations

import base64
import io
import logging

from ..errors import BackendError
from .base import _exc_str

log = logging.getLogger(__name__)


async def _test_screenshot_permissions(platform: str) -> tuple[bool, str]:
    """Test whether screenshots capture actual window content.

    macOS: TCC Screen Recording permission required. Without it,
    screenshots show desktop background + dock + menubar only (all
    windows appear as black/invisible rectangles).

    Linux Wayland: grim/gnome-screenshot may fail or show permission dialog.

    Linux X11 / Windows: no special permissions needed.
    """
    try:
        from ..backends import _common
        try:
            shot = await _common.take_screenshot()
        except BackendError as e:
            return False, str(e)
        img_b64, width, height = shot.png_b64, shot.width, shot.height
        if width < 100 or height < 100:
            return False, f"Screenshot too small ({width}x{height})"

        # Decode and check if it's mostly uniform (TCC failure indicator)
        img_bytes = base64.b64decode(img_b64)
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))

        # Sample pixels from center region (avoid edges/dock/menubar)
        cx, cy = img.width // 2, img.height // 2
        sample_region = img.crop((cx - 50, cy - 50, cx + 50, cy + 50))
        colors = sample_region.getcolors(maxcolors=100)

        if colors and len(colors) == 1:
            # Entire center region is one color — likely TCC failure (all black)
            r, g, b = colors[0][1][:3]
            if r == 0 and g == 0 and b == 0:
                if platform == "macos":
                    return False, (
                        "Screenshot shows all-black windows — Screen Recording "
                        "permission not granted. Go to System Settings → Privacy "
                        "& Security → Screen Recording and add this application."
                    )
                return False, "Screenshot center is all-black — possible permission issue"

        return True, f"OK ({width}x{height})"

    except Exception as e:  # PIL decode of an odd capture, ...
        log.debug("screenshot self-test failed", exc_info=True)
        return False, _exc_str(e)
