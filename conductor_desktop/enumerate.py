"""Optional enumeration backend — a full-screen UI decomposition service.

Where the grounder answers "where is *this* target?" (single/region ground)
and describe answers "what's on screen?" in prose, the enumerator answers
"give me *every* UI element": a flat list of detected icons and text boxes
with pixel bounding boxes and OCR/caption content. It's the fourth perception
capability, and unlike the grounder it is OPTIONAL — only wired up when
``CONDUCTOR_DESKTOP_OMNIPARSER_URL`` points at a deployed OmniParser server, since
not every host runs that extra model.

Protocol note: OmniParser is NOT an OpenAI chat-completions VLM, so it does
not fit ``Grounder``'s ``GroundingDialect`` abstraction (system+user messages,
per-family bbox dialects, ``/v1/chat/completions``). It's a multipart file
upload to ``POST /parse`` returning a JSON list. Hence a separate, tiny client
rather than another ``DIALECTS`` entry.

Response shape (an OmniParser server's ``/parse``):
    {"image": "" | "data:image/png;base64,...",
     "parsed_content": [{"type": "text"|"icon", "bbox": [x1,y1,x2,y2],
                         "interactivity": bool, "content": str}, ...],
     "time_used": float}
``bbox`` is image-pixel xyxy (the server runs with output_coord_in_ratio=False),
and is projected to the screen with ``bbox_to_px(..., CoordSpace.IMAGE_PIXELS)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .grounder import GrounderError
from .imaging import png_dims

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnumeratedElement:
    """One element OmniParser detected on the screen.

    `bbox` is image-pixel xyxy of the input screenshot (same frame as the
    grounder's GroundedElement). `kind` is "icon" or "text". `interactive`
    is OmniParser's interactivity flag — True for clickable controls, the
    default filter for CUA callers. `content` is the OCR text (for text
    boxes) or the icon caption (when the server runs with local semantics).
    """
    bbox: tuple[int, int, int, int]
    kind: str
    interactive: bool
    content: str

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


class Enumerator:
    """Thin client for the OmniParser ``/parse`` endpoint.

    One method, one multipart POST — mirrors ``Grounder``'s shape (own
    httpx client, ``close()``, transport errors mapped to ``GrounderError``
    so the existing ``except RuntimeError`` soft-error path in tools treats
    an unreachable enumerator the same as an unreachable grounder).
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def parse(self, image_b64: str, *, annotate: bool = False,
                    rec: bool = True, os_id: str | None = None,
                    theme: str | None = None
                    ) -> tuple[list[EnumeratedElement], str | None]:
        """Enumerate every element OmniParser detects in the screenshot.

        `annotate=False` (the default) takes the server's CUA fast path: skip
        the SoM-label drawing + PNG/base64 encode (~700 ms) and return only
        the element list; the annotated image comes back as None. Pass
        `annotate=True` to also get the Set-of-Marks labeled PNG (returned as
        a base64 string, data-URL prefix stripped) for visual debugging.

        `rec=False` (RKNN backend only) skips OCR text recognition — text
        boxes still come back with their bbox but content="".

        `os_id` / `theme` drive OmniParser's VLM-free icon labeling: the server
        fills each icon's `content` from its ref library, gated to that
        platform + icon theme (see conductor_desktop.core.platform_detect
        .detect_icon_env). Both are no-ops server-side unless its
        OMNIPARSER_ICON_REFS is configured. `os_id` is REQUIRED for any
        labeling — the server skips it entirely when `os` is absent; `theme`
        further narrows same-platform variants and is safe to omit (None → the
        server keeps every theme eligible). Send a theme slug ONLY if it matches
        the library's tag exactly, or it excludes that platform's themed refs.

        Returns ``(elements, annotated_b64_or_None)``. Transport/HTTP failures
        raise ``GrounderError`` with an operator-facing message.
        """
        import base64

        url = f"{self._url}/parse"
        png_bytes = base64.b64decode(image_b64)
        # OmniParser reads `annotate`/`rec`/`os`/`theme` as query params and the
        # image as a multipart file field named "file". httpx serializes Python
        # bools to "true"/"false", which FastAPI parses back to bool.
        files = {"file": ("screen.png", png_bytes, "image/png")}
        params: dict = {"annotate": annotate, "rec": rec}
        if os_id:
            params["os"] = os_id        # gates icon labeling; omit → labeling off
        if theme:
            params["theme"] = theme     # narrows to a theme; omit → all eligible
        try:
            r = await self._http.post(url, files=files, params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.TimeoutException as e:
            raise GrounderError(
                f"OmniParser timed out after {self._http.timeout.read}s talking "
                f"to {self._url} ({type(e).__name__}). The parser is slow or "
                f"wedged — check the server, or raise "
                f"CONDUCTOR_DESKTOP_OMNIPARSER_TIMEOUT."
            ) from e
        except httpx.HTTPStatusError as e:
            raise GrounderError(
                f"OmniParser at {self._url} returned HTTP "
                f"{e.response.status_code}. The server is up but rejected the "
                f"request — check its logs and that the model weights loaded "
                f"(GET {self._url}/health)."
            ) from e
        except httpx.HTTPError as e:
            raise GrounderError(
                f"could not reach OmniParser at {self._url} "
                f"({type(e).__name__}: {e}). Check the server is running and "
                f"CONDUCTOR_DESKTOP_OMNIPARSER_URL points at it."
            ) from e

        # OmniParser deployments disagree on bbox units: some emit image-pixel
        # xyxy, others normalized 0..1 ratio. Scale
        # ratio → pixel against the image we sent so EnumeratedElement.bbox is
        # always image-pixel xyxy, which the IMAGE_PIXELS projection expects.
        img_w, img_h = png_dims(png_bytes)
        elements = _parse_content(data.get("parsed_content"), img_w, img_h)
        image = data.get("image") or ""
        annotated = None
        if annotate and image:
            # The server returns "data:image/png;base64,<...>"; strip the
            # prefix so callers get the raw base64 the PNG helpers expect.
            annotated = image.split(",", 1)[-1] if "," in image else image
        return elements, annotated


def _parse_content(parsed, img_w: int = 0, img_h: int = 0
                   ) -> list[EnumeratedElement]:
    """Map OmniParser's ``parsed_content`` list into EnumeratedElements.

    `img_w`/`img_h` are the dimensions of the image sent to the parser; they
    scale normalized-ratio bboxes (0..1, as some deployments emit) back into
    image pixels. A bbox is treated as ratio when all four of its coordinates
    are <= 1.0 — a pixel box on any real screenshot always has x2/y2 far above
    1, so the test is unambiguous per element. Pixel-unit deployments pass
    through unscaled.

    Tolerant by design: skips any entry missing a well-formed 4-number bbox
    or with a degenerate/inverted box (same policy as the grounder
    dialects), so a malformed row can't crash enumeration.
    """
    if not isinstance(parsed, list):
        return []
    out: list[EnumeratedElement] = []
    for raw in parsed:
        if not isinstance(raw, dict):
            continue
        bbox = raw.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        try:
            a, b, c, d = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        # Normalized 0..1 ratio → image pixels. Only when dims are known;
        # otherwise leave as-is (a pixel-unit deployment, or an unreadable PNG).
        if img_w > 0 and img_h > 0 and max(a, b, c, d) <= 1.0:
            a, c = a * img_w, c * img_w
            b, d = b * img_h, d * img_h
        x1, y1, x2, y2 = (int(round(v)) for v in (a, b, c, d))
        if x2 <= x1 or y2 <= y1:
            continue
        kind = raw.get("type")
        kind = kind if isinstance(kind, str) else ""
        content = raw.get("content")
        content = content.strip() if isinstance(content, str) else ""
        out.append(EnumeratedElement(
            bbox=(x1, y1, x2, y2),
            kind=kind,
            interactive=bool(raw.get("interactivity")),
            content=content,
        ))
    return out
