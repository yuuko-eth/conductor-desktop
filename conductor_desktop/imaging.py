"""Image helpers small enough for any layer to import."""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger(__name__)


def png_dims(data: bytes) -> tuple[int, int]:
    """(width, height) of an image, without decoding its pixels.

    Avoids the multi-MB raw-pixel buffer PIL.open materializes when the
    caller only needs the size. PNG layout: 8-byte signature, then chunks;
    IHDR is always first, with width and height as big-endian uint32 at
    offsets 16-23. Anything else goes through PIL, so the helper stays
    general-purpose (a JPEG webcam frame, say). Bytes that aren't a readable
    image give (0, 0).
    """
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:  # not an image, or truncated
        # (0, 0) is "size unknown": callers that need it say so themselves,
        # and the ones that only scale by it leave the values alone.
        log.debug("could not size a %d-byte image", len(data), exc_info=True)
        return 0, 0


def b64_dims(image_b64: str) -> tuple[int, int]:
    """(width, height) of a base64-encoded image."""
    return png_dims(base64.b64decode(image_b64))
