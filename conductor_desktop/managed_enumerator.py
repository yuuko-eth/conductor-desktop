"""Managed-mode enumeration client — the OmniParser full-screen decomposition,
served through the conductor-grounder SaaS gateway instead of a raw OmniParser.

Self-hosted mode (``enumerate.Enumerator``) points ``CONDUCTOR_DESKTOP_OMNIPARSER_URL``
straight at a deployed OmniParser ``/parse`` server. A SaaS client must not know
the parser topology — so in managed mode the gateway owns it (capability routing
to its OmniParser pool, health probing, metering the ``enumerate`` workload)
behind one endpoint:

    POST {base}/v1/enumerate   {screenshot}
        -> {elements:[{kind,bbox,content,interactive}], coord_system:"norm1000"}

This class is a drop-in for ``enumerate.Enumerator``: same ``parse()`` signature
and ``EnumeratedElement`` return type, so ``runtime.grounding.capture_and_enumerate``
is unchanged. The one wrinkle: the gateway answers in norm1000, while
``EnumeratedElement.bbox`` is image-pixel xyxy, so the boxes are converted here
against the PNG we sent and the rest of the path is identical to self-hosted.

Auth is the same Bearer seat token (``cgr_live_…``) as the managed grounder;
billing happens gateway-side per ``enumerate`` call.
"""

from __future__ import annotations

import logging

from .enumerate import EnumeratedElement
from .grounder import CoordSpace, GrounderError
from .imaging import b64_dims
from .managed import GatewayClient, gateway_bbox

log = logging.getLogger(__name__)


class ManagedEnumerator:
    """Enumerator-compatible client over the conductor-grounder gateway."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._gw = GatewayClient(base_url, api_key, timeout,
                                 what="managed enumerator",
                                 timeout_env="CONDUCTOR_DESKTOP_OMNIPARSER_TIMEOUT")

    async def close(self) -> None:
        await self._gw.close()

    async def parse(self, image_b64: str, *, annotate: bool = False,
                    rec: bool = True, os_id: str | None = None,
                    theme: str | None = None
                    ) -> tuple[list[EnumeratedElement], str | None]:
        """Enumerate every element the gateway's OmniParser detects.

        The signature matches ``enumerate.Enumerator.parse`` so the server
        consumes it unchanged, but /v1/enumerate takes the screenshot alone:
        OCR (``rec``) and icon labeling (``os_id`` / ``theme``) are the
        gateway's own decisions, and it doesn't serve the Set-of-Marks image,
        so ``annotate`` can't be honoured and the annotated image is always
        ``None``. Passing them is not an error; they are logged and dropped.

        Returns ``(elements, None)`` with each ``EnumeratedElement.bbox`` in
        image pixels of the screenshot sent. Transport and HTTP failures raise
        ``GrounderError`` (same soft-error contract as the grounder path).
        """
        ignored = [name for name, on in (("annotate", annotate), ("rec", not rec),
                                         ("os_id", os_id), ("theme", theme)) if on]
        if ignored:
            log.debug("managed enumerator: the gateway decides %s itself",
                      ", ".join(ignored))
        r = await self._gw.post("/v1/enumerate", {"screenshot": image_b64})
        data = self._gw.body(r, "/v1/enumerate")
        space = self._gw.coord_space(data.get("coord_system"))
        img_w = img_h = 0
        if space is CoordSpace.NORM1000:
            img_w, img_h = b64_dims(image_b64)
            if img_w <= 0 or img_h <= 0:
                raise GrounderError(
                    "managed enumerator: cannot size the screenshot that was sent, "
                    "so its norm1000 boxes can't be placed on the screen.")

        out: list[EnumeratedElement] = []
        for el in data.get("elements") or []:
            bbox = gateway_bbox(el.get("bbox")) if isinstance(el, dict) else None
            if bbox is None:
                log.warning("managed enumerator: dropping element with a malformed bbox: %.200r", el)
                continue
            x1, y1, x2, y2 = bbox
            if space is CoordSpace.NORM1000:
                x1, x2 = _px(x1, img_w), _px(x2, img_w)
                y1, y2 = _px(y1, img_h), _px(y2, img_h)
            out.append(EnumeratedElement(
                bbox=(x1, y1, x2, y2),
                kind=el.get("kind", "") or "",
                interactive=bool(el.get("interactive")),
                content=(el.get("content") or "").strip(),
            ))
        return out, None


def _px(coord_norm: int, dim: int) -> int:
    """norm1000 (0..1000) -> image pixel against a known dimension."""
    return int(round(coord_norm / 1000.0 * dim))
