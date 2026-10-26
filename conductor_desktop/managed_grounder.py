"""Managed-mode grounder client — talks to the conductor-grounder SaaS gateway
instead of a raw VLM server.

Self-hosted mode (``grounder.Grounder``) puts the whole fleet topology in the
client: it picks a model family, builds that family's system prompt + JSON shape,
and routes point/describe/region across up to three URLs (see
``https://docs.conductor-mcp.io/grounders/ensembles/``). That is exactly the knowledge a SaaS client must
NOT have. In managed mode the gateway owns all of it — capability routing
(holo for click, qwen for describe/region, OmniParser for enumeration), the
dialects, the license gate, the metering — behind two endpoints:

    POST {base}/v1/ground     {target, screenshot, multiple} -> candidates[] (norm1000)
    POST {base}/v1/describe    {screenshot, hint, ...}   -> {description}

So this client carries no dialect, no family, no bbox_key, no axis order. It is a
``Grounder``-shaped object (same ``ground`` / ``describe`` / ``describe_image`` /
``close`` surface) so ``server.py`` and the tool layer consume it unchanged, and
it returns **norm1000** boxes, the space every local dialect returns too, so they
project the same way.

Auth is a Bearer seat token (``cgr_live_…``), held by ``managed.GatewayClient``.
Billing happens gateway-side per workload; the client never sees credits.
"""

from __future__ import annotations

import asyncio
import logging

from .grounder import (
    CoordSpace,
    GroundedElement,
    GrounderError,
    ParseContext,
    image_size,
    is_synthetic_point_box,
)
from .managed import GatewayClient, gateway_bbox

log = logging.getLogger(__name__)


class ManagedGrounder:
    """Grounder-compatible client for the conductor-grounder gateway.

    Drop-in for ``grounder.Grounder``: ``server._build_state`` builds this when
    the deployment is in managed mode, and ``locate`` / ``describe`` call the
    same methods. The 0/1/N policy upstream is unchanged — ``ground`` returns the
    gateway's ``candidates[]`` as a list, an empty list meaning no-match.
    """

    def __init__(self, base_url: str = "", api_key: str = "", timeout: float = 30.0,
                 *, gateway: GatewayClient | None = None,
                 multiple: bool = False) -> None:
        self._gw = gateway or GatewayClient(
            base_url, api_key, timeout, what="managed grounder",
            timeout_env="CONDUCTOR_DESKTOP_GROUNDER_TIMEOUT")
        # The gateway's `multiple` flag. False routes to the point pool (Holo:
        # one click point, no real extent); True routes to the region pool
        # (Qwen: real bboxes, 0/1/N). The gateway can't infer which one a
        # caller needs, so region ops must use `region_view()`.
        self._multiple = multiple

    def region_view(self) -> ManagedGrounder:
        """A sibling for region ops (crop, locate(multiple)) that asks the
        gateway for real bboxes over this client's connection."""
        return ManagedGrounder(gateway=self._gw.borrow(), multiple=True)

    async def close(self) -> None:
        await self._gw.close()

    async def ground(self, image_b64: str, target: str) -> list[GroundedElement]:
        """Return every element matching ``target`` (norm1000 bboxes).

        A 404 from the gateway is an honest no-match (the gateway returns 404
        when zero elements ground), surfaced here as an empty list so the
        caller's 0/1/N policy treats it as no_match — identical to a self-hosted
        grounder returning ``{"detected": []}``.
        """
        r = await self._gw.post("/v1/ground", {"target": target, "screenshot": image_b64,
                                               "multiple": self._multiple},
                                allow=(404,))
        if r.status_code == 404:
            return []
        data = self._gw.body(r, "/v1/ground")
        space = self._gw.coord_space(data.get("coord_system"))
        ctx = ParseContext(0, 0, space)
        if space is not CoordSpace.NORM1000:
            # The gateway is wired norm1000 end to end; if one ever answers in
            # pixels, convert against the image we sent rather than mis-place
            # every click.
            w, h = await asyncio.to_thread(image_size, image_b64)
            if w <= 0 or h <= 0:
                raise GrounderError(
                    f"managed grounder served {space} coordinates and the screenshot "
                    f"that was sent can't be sized, so they can't be placed.")
            ctx = ParseContext(w, h, space)

        # Prefer the full candidate list (0/1/N); fall back to the single top
        # bbox for an older gateway that omits candidates on a single match.
        raw = data.get("candidates") or (
            [{"bbox": data["bbox"], "description": data.get("label", "")}]
            if data.get("bbox") else [])
        out: list[GroundedElement] = []
        for c in raw:
            bbox = gateway_bbox(c.get("bbox")) if isinstance(c, dict) else None
            if bbox is None:
                log.warning("managed grounder: dropping candidate with a malformed bbox: %.200r", c)
                continue
            x1, y1 = ctx.to_norm1000(bbox[0], bbox[1])
            x2, y2 = ctx.to_norm1000(bbox[2], bbox[3])
            # The gateway says nothing about which family served the request,
            # so unlike the self-hosted path — where the point dialects set the
            # flag at the mint site — managed mode infers it from the geometry.
            # See `is_synthetic_point_box` for what that costs.
            out.append(GroundedElement(bbox=(x1, y1, x2, y2),
                                       description=c.get("description", ""),
                                       point=is_synthetic_point_box((x1, y1, x2, y2))))
        return out

    async def describe(self, image_b64: str, hint: str | None = None) -> str:
        """Prose description of a screen capture via the gateway /v1/describe."""
        return await self._describe(image_b64, hint, image_mode=False)

    async def describe_image(self, image_b64: str, mime: str = "image/png",
                             hint: str | None = None) -> str:
        """Describe an arbitrary image (webcam frame, file). Same endpoint with
        ``image_mode=true`` so the gateway picks its image dialect (no desktop
        assumptions)."""
        return await self._describe(image_b64, hint, image_mode=True, mime=mime)

    async def _describe(self, image_b64: str, hint: str | None,
                        image_mode: bool, mime: str = "image/png") -> str:
        payload: dict = {"screenshot": image_b64, "image_mode": image_mode}
        if hint:
            payload["hint"] = hint
        if image_mode:
            payload["mime"] = mime
        r = await self._gw.post("/v1/describe", payload)
        data = self._gw.body(r, "/v1/describe")
        return str(data.get("description") or "").strip()
