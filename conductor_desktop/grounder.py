"""Grounding and describe client: one HTTP call to an OpenAI-compatible VLM.

Output protocol: the grounder is asked for ALL elements matching the target,
not the most likely one. The caller applies a 0/1/N policy: 0 → no match,
1 → resolve, N → surface the candidates' spatial anchors so the agent can
describe the target more specifically on retry.

Each model family speaks a dialect (``DIALECTS``): the prompt, the request
knobs, the coordinate space its answer is in, and the parser for its output
shape. A dialect's ``parse`` returns boxes in norm1000 whatever the model
emitted, so everything past ``Grounder.ground`` projects one way. Describe
prompts are per family too (``DESCRIBE_DIALECTS``), one pair per family for
screenshots and for arbitrary images.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

import httpx

log = logging.getLogger(__name__)


class GrounderError(RuntimeError):
    """The remote grounder VLM was unreachable, timed out, or errored.

    A ``RuntimeError`` subclass so the existing ``except RuntimeError`` in
    tools like ``locate`` already treats it as a soft, brain-facing error.
    The message is written for the *calling agent*: it names the URL and the
    likely fix so neither the AI nor the human operator has to go spelunking
    in ``.env`` to discover the grounder is down.
    """


# The MCP tool being served, set by the server's tool wrapper, so an error from
# deep inside a tool can name it (several tools catch RuntimeError and return
# str(e) as their own error).
CURRENT_TOOL: ContextVar[str] = ContextVar("conductor_current_tool", default="this tool")

_NO_GROUNDER_ROUTE = (
    "Without one: ui_find / ui_tree and the ui_* actions work in native apps, web_* "
    "in browser pages, capture_screen shows the screen, and click_at / type_text / key "
    "act on coordinates you already have (bounds from ui_tree or get_scene_graph)."
)
_MISSING = {
    "ground": ("needs a grounder to find things on screen by description, and none is "
               "configured (set CONDUCTOR_DESKTOP_GROUNDER_URL). " + _NO_GROUNDER_ROUTE),
    "region": ("needs a grounder that returns bounding boxes, and none is configured (set "
               "CONDUCTOR_DESKTOP_GROUNDER_URL, or CONDUCTOR_DESKTOP_DESCRIBE_URL with a qwen or gemma4 "
               "model). " + _NO_GROUNDER_ROUTE),
    "describe": ("needs a describe model, and none is configured (set CONDUCTOR_DESKTOP_DESCRIBE_URL, "
                 "or CONDUCTOR_DESKTOP_GROUNDER_URL for one server that does both). Without one: "
                 "capture_screen returns the screen image, and ui_tree reads native apps as text."),
}


class GrounderNotConfigured(GrounderError):
    """A tool needs a grounding or describe backend the server wasn't given."""


class MissingGrounder:
    """Stands in for a role with no backend configured (``ground``, ``region`` or
    ``describe``): the server still starts, and every call raises
    ``GrounderNotConfigured`` naming the tool, the env var and what works instead."""

    def __init__(self, role: str) -> None:
        if role not in _MISSING:
            raise ValueError(f"unknown grounder role {role!r}")
        self.role = role

    def _error(self) -> GrounderNotConfigured:
        return GrounderNotConfigured(f"{CURRENT_TOOL.get()} {_MISSING[self.role]}")

    async def ground(self, *args, **kwargs):
        raise self._error()

    async def describe(self, *args, **kwargs):
        raise self._error()

    async def describe_image(self, *args, **kwargs):
        raise self._error()

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class GroundedElement:
    """One match returned by the grounder.

    `bbox` is in image-pixel coordinates of the input screenshot (Qwen3-VL
    native bbox_2d shape). `description` is a short spatial anchor the brain
    can use to disambiguate when multiple elements match — the brain never
    sees the image, so this textual handle is the only path back to a more
    specific target description on retry.

    `point` marks a bbox synthesized around a single click point rather than
    measured off the element. It rides on the element, not on the configured
    family, because under a managed gateway the client never learns which
    family served the request — `config.grounder_family` is "managed" and
    `family_is_point_only` is False, so a config-derived check goes silent
    exactly where the caveat matters most.
    """
    bbox: tuple[int, int, int, int]
    description: str
    point: bool = False

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


# "pixel coordinates" is not what Qwen3-VL returns: it answers in norm1000
# regardless (a y of 935 on an 800px-tall image), and the dialect reads it as
# norm1000. The wording stays because it is the one the prompt was tuned with:
# with "normalized 0-1000" instead, Qwen3-VL-4B moved 4 of 6 Dock and
# menu-bar targets by 2-22px and put Finder on its neighbour.
_QWEN_GROUND_SYSTEM = (
    "You are a visual grounder for a text-only agent. Find every UI element "
    "on the screen matching the target.\n"
    "\n"
    "Output JSON only:\n"
    '  {"detected": [{"bbox_2d": [x1, y1, x2, y2], "description": "<spatial anchor>"}, ...]}\n'
    "\n"
    "- bbox_2d: pixel coordinates of the input image, [x1, y1, x2, y2].\n"
    "- description: 5-12 words distinguishing this element from siblings "
    "(e.g. \"close button on right pane toolbar\").\n"
    "- Return every match. Multiple candidates let the agent retry with "
    "a more specific target.\n"
    "- If nothing matches, emit {\"detected\": []}."
)

# Gemma 4 natively emits a bare top-level JSON array of
# `{"box_2d": [y1,x1,y2,x2], "label": "..."}` items, with box_2d normalized to
# 0..1000. The dialect stays in that native shape and adds only the two edits
# that earn their keep: (1) multi-match policy — return every candidate so the
# brain can disambiguate on retry; (2) anti-text-grounding — refuse to match a
# target by finding its literal characters rendered as text on screen. The rest
# is left native to keep thinking-mode token pressure low.
_GEMMA4_GROUND_SYSTEM = (
    "You are a visual grounder for a text-only agent. Find every UI element "
    "on the screen matching the target.\n"
    "\n"
    "Output a JSON array, nothing else:\n"
    '  [{"box_2d": [y1, x1, y2, x2], "label": "<spatial anchor>"}, ...]\n'
    "\n"
    "- box_2d: normalized 0-1000, [y1, x1, y2, x2].\n"
    "- label: 5-12 words distinguishing this element from siblings "
    "(e.g. \"close button on right pane toolbar\").\n"
    "- Return every match. Multiple candidates let the agent retry with "
    "a more specific target.\n"
    "- Ground graphical UI only. If a target's literal name appears as text "
    "on screen but the element itself isn't visible, return []. Exception: "
    "the target explicitly asks for text (\"the heading 'Settings'\").\n"
    "- If nothing matches, return []."
)


# HCompany Holo-3.1 — a purpose-built GUI grounder fine-tuned on a single
# localization template that returns ONE click point (not a box, not a list).
# Its native instruction lives in the user message with the ClickAbsoluteAction
# JSON schema inlined; system prompt is empty. Despite the schema describing
# x/y as "pixels", llama.cpp serves the coordinates normalized 0..1000 (verified
# empirically on Q8_0 — tray clock (951,969)→×screen/1000 lands dead-on), so the
# points flow through the same norm1000 scaler as Gemma/LocateAnything. The
# Qwen3 chat template defaults thinking ON, which makes the model burn the token
# budget on <think> and emit a truncated `{"action":"click_absolute"}` with no
# coords — so this dialect MUST disable thinking via chat_template_kwargs.
# A concrete-value example (not a `<int>` placeholder) is load-bearing: with the
# placeholder the model drifts into array-valued coords ("x":[579,481]) or a bare
# `[x,y]` array; concrete scalars anchor it on the flat shape the parser wants.
_HOLO_GROUND_USER = (
    "Localize an element on the GUI image according to the provided target "
    "and output a click position.\n"
    "     * If the target element IS visible, output valid JSON: "
    '{{"action": "click_absolute", "x": 123, "y": 456}}\n'
    "     * If the target element is NOT visible, output exactly: "
    '{{"action": "not_found"}}\n'
    "     Your target is:\n{target}"
)

# Think-inclusive union grammar for Holo careful mode. The Qwen3 template pre-fills
# "<think>\n", so the model GENERATES: <reasoning> </think> <answer>. The grammar
# permits free-form reasoning (any text without '<') so thinking can detect an
# off-screen target, then constrains the FINAL answer to one of two shapes —
# making malformed/array-drift JSON structurally impossible. An eager grammar
# (no <think> modelled) would forbid the reasoning tokens and silently kill
# no-match detection, so the </think> boundary must live in the grammar. This
# grammar REQUIRES enable_thinking=True (it expects the pre-filled <think>).
_HOLO_GRAMMAR = r'''root ::= reasoning "</think>" ws answer
reasoning ::= [^<]*
answer ::= click | notfound
click ::= "{\"action\": \"click_absolute\", \"x\": " num ", \"y\": " num "}"
notfound ::= "{\"action\": \"not_found\"}"
num ::= [0-9] [0-9]? [0-9]? [0-9]?
ws ::= [ \n\t]*'''


# --- Qwen3-VL-based GUI-agent families (validated end to end on llama.cpp) ----
# All four below are built on Qwen3-VL and, served through llama.cpp, emit a
# single click POINT normalized 0..1000 — so they flow through the same
# norm1000 scaler as Holo/Gemma and reuse the Holo point_only region-routing.
# Only the raw output *shape* differs, hence one small parser each.

# UI-Venus 1.5 (inclusionAI, 2B/8B/30B-A3B). Bare `[x,y]` plain text, no system
# prompt; `[-1,-1]` is the native infeasible/no-match signal. No thinking tax.
_VENUS_GROUND_USER = (
    "Output the center point of the position corresponding to the following "
    "instruction: {target}. The output should just be the coordinates of a "
    "point, in the format [x,y]. Additionally, if the task is infeasible "
    "(e.g., the task is not related to the image), the output should be [-1,-1]."
)

# GUI-Owl 1.5 (mPLUG, 2B/4B/8B/32B +Think). Grounds via a Qwen `computer_use`
# tool-call on a *declared 1000x1000 screen* (NOT the `point_2d` JSON the model
# cards imply — confirmed from Mobile-Agent-v3.5 eval_grounding_benchmarks.py
# and the E2E probe). The coordinate is therefore already norm1000. The
# `terminate/failure` tool-call is the native infeasible signal. The system
# prompt embeds literal JSON braces — it is NOT run through str.format (only
# user_template is), so the braces are safe.
_GUIOWL_GROUND_SYSTEM = (
    "# Tools\n\n"
    "You may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n"
    "<tools>\n"
    '{"type": "function", "function": {"name": "computer_use", "description": '
    '"Use a mouse to interact with a computer.\\n'
    "* The screen's resolution is 1000x1000.\\n"
    "* Make sure to click any buttons, links, icons, etc with the cursor tip "
    'in the center of the element.", "parameters": {"properties": {"action": '
    '{"enum": ["mouse_move", "left_click"], "type": "string"}, "coordinate": '
    '{"type": "array"}}, "required": ["action"], "type": "object"}}}\n'
    "</tools>\n\n"
    "For each function call, return a json object with function name and "
    "arguments within <tool_call></tool_call> XML tags:\n"
    "<tool_call>\n"
    '{"name": <function-name>, "arguments": <args-json-object>}\n'
    "</tool_call>\n"
    "Additionally, if the target is not present (e.g. the instruction is "
    "unrelated to the image), return <tool_call>\n"
    '{"name": "computer_use", "arguments": {"action": "terminate", '
    '"status": "failure"}}\n</tool_call>'
)

# MAI-UI (Tongyi-MAI, 2B/8B/32B/235B-A22B). Emits a <grounding_think> reasoning
# block then `<answer>{"coordinate": [x,y]}</answer>`, norm1000 (the eval-code
# "resized-pixel" reading was a red herring — the served model is norm1000).
# Always spends the think block → needs the larger token budget. Its training
# order is text-then-image, so this dialect sets image_first=False.
_MAIUI_GROUND_SYSTEM = (
    "You are a GUI grounding agent. \n"
    "## Task\n"
    "Given a screenshot and the user's grounding instruction. Your task is to "
    "accurately locate a UI element based on the user's instructions.\n"
    "First, you should carefully examine the screenshot and analyze the user's "
    "instructions, translate the user's instruction into a effective reasoning "
    "process, and then provide the final coordinate.\n"
    "## Output Format\n"
    "Return a json object with a reasoning process in "
    "<grounding_think></grounding_think> tags, a [x,y] format coordinate within "
    "<answer></answer> XML tags:\n"
    "<grounding_think>...</grounding_think>\n"
    "<answer>\n"
    '{"coordinate": [x,y]}\n'
    "</answer>\n"
    "## Input instruction\n"
)

# meituan EvoCUA (8B/32B) — a full computer-use agent, not a pure grounder. Used
# in grounding-only mode by asking for a single click; it emits a pyautogui
# code block. `relative` coord_type = norm1000 (≈0..999; the ±1px vs /1000 is
# negligible). No native no-match token in this minimal prompt → unparseable
# output falls through to the 0-match path.
_EVOCUA_GROUND_SYSTEM = (
    "You are a GUI agent operating a computer. For the given instruction, "
    "output the single pyautogui action to perform, as a code block with one "
    "line, e.g.\n"
    "```python\npyautogui.click(x=123, y=456)\n```"
)

# ByteDance UI-TARS-1.5-7B — Qwen2.5-VL-based. Used grounding-only with the
# prompt Simular's Agent S3 pairs with a GPT-5 planner (gui_agents/s3/agents/
# grounding.py). Unlike the Qwen3-VL families above, it answers in ABSOLUTE
# PIXELS of the model's smart_resized image, not norm1000 — see the
# "qwen25_pixels" coord_space and `_qwen25_smart_resize_b64`. No native
# no-match signal → unparseable output falls through to the 0-match path.
_UITARS_GROUND_USER = (
    "Query:{target}\n"
    "Output only the coordinate of one point in your response.\n"
)


# --- Point boxes ------------------------------------------------------------
# Half-size of the synthetic box (in the 0..1000 normalized space) drawn around
# the point so the GroundedElement is non-degenerate and its .center == (x, y).
# ~7/1000 ≈ a 9px box on a 1280px-wide capture — small enough not to skew the
# click, large enough to survive the x2>x1 degeneracy guard. Shared by every
# point-emitting family (Holo, Venus, GUI-Owl, MAI-UI, EvoCUA).
_POINT_BOX_HALF = 7


# Upper bound (norm1000, both axes) on a bbox we treat as a synthesized point
# box. Above the client's 14 and the gateway's measured 16, below a typical
# real control. See `is_synthetic_point_box` for the measurement.
_MAX_POINT_BOX_NORM = 20


def is_synthetic_point_box(bbox: tuple[int, int, int, int]) -> bool:
    """Does this norm1000 bbox look like a box synthesized around a point?

    A HEURISTIC, and the only one available in managed mode: the gateway
    forwards the worker's bbox but deliberately carries no dialect or family
    (see `managed_grounder`), so a point-only hit is indistinguishable from a
    bbox hit except by its size. `_point_to_element` mints a 2*_POINT_BOX_HALF
    square, and edge clamping can only shrink it, so "no larger than that in
    both axes" catches every synthesized box.

    The threshold is NOT `2 * _POINT_BOX_HALF`. Measured against the managed
    gateway, its point hits arrive as 20x12-13px on a
    1280x800 screen — 15.6 x 16.2 norm1000, i.e. a 16-unit box (half=8), while
    this client mints 14 (half=7). Tying the test to the client's own constant
    silently misses every managed point hit, which is exactly the bug this
    function exists to close, so the bound is its own measured constant with
    headroom over both.

    It over-triggers on genuinely tiny elements (20/1000 is ~26px wide on a
    1280px capture — about one small toolbar icon). That direction is
    deliberate: the cost is one extra caveat on a real hit, against a silent
    confident point for an element that does not exist. The durable fix is a
    `point` flag on the gateway's candidate JSON; this stands in until that
    ships.
    """
    x1, y1, x2, y2 = bbox
    return ((x2 - x1) <= _MAX_POINT_BOX_NORM
            and (y2 - y1) <= _MAX_POINT_BOX_NORM)


def _point_to_element(x: int, y: int,
                      description: str = "") -> GroundedElement:
    """Synthesize a GroundedElement from a single norm-0..1000 click point.

    Point-emitting families localize one target but carry no real bbox extent,
    so we clamp the point into the 0..1000 grid and draw a tiny box around it —
    non-degenerate (survives the x2>x1 guard), centered so `.center == (x, y)`,
    and small enough not to skew the click. The synthetic box flows through the
    same norm1000 scaler as every other family; region ops (crop, locate.bbox)
    route to a bbox-capable backend instead (see `family_is_point_only`).
    """
    x = min(1000, max(0, x))
    y = min(1000, max(0, y))
    h = _POINT_BOX_HALF
    x1, x2 = max(0, x - h), min(1000, x + h)
    y1, y2 = max(0, y - h), min(1000, y + h)
    # Edge clamp could collapse a side; nudge to keep x2>x1 / y2>y1.
    if x2 <= x1:
        x1, x2 = max(0, x2 - 1), min(1000, x1 + 1)
    if y2 <= y1:
        y1, y2 = max(0, y2 - 1), min(1000, y1 + 1)
    return GroundedElement(bbox=(x1, y1, x2, y2), description=description,
                           point=True)


# --- Qwen2.5-VL pixel-space families (UI-TARS-1.5) ---------------------------
# Qwen2.5-VL patches are 14px merged 2x2, so resized sides align to 28. The
# bounds keep the client's resize strictly inside llama.cpp's defaults (8..4096
# tokens) AND inside the envelope where UI-TARS-1.5-7B Q8_0 actually grounds on
# llama.cpp (measured sweep): 1024 tokens is llama.cpp's own
# grounding floor; accuracy is 0-3px up to 2560 tokens / 1792px wide, but any
# image >=2072px wide drifts on x by 60-1000px even at only 2146 tokens (1932px
# is borderline). It is a width effect, not batching — a 2560-token image split
# 2048+512 across ubatches was still exact. So cap the long side too.
_QWEN25_FACTOR = 28
_QWEN25_MIN_PIXELS = 1024 * _QWEN25_FACTOR * _QWEN25_FACTOR
_QWEN25_MAX_PIXELS = 2560 * _QWEN25_FACTOR * _QWEN25_FACTOR
_QWEN25_MAX_SIDE = 64 * _QWEN25_FACTOR  # 1792px


def qwen25_smart_resize(width: int, height: int) -> tuple[int, int]:
    """Qwen2.5-VL smart_resize, mirroring llama.cpp's calc_size_preserved_ratio
    (round to the factor, then floor-scale above max / ceil-scale below min),
    plus a long-side cap llama.cpp doesn't have (see _QWEN25_MAX_SIDE).
    Returns (w, h). An already-aligned in-bounds size maps to itself, which is
    what makes the client-side pre-resize a no-op on the server."""
    f = _QWEN25_FACTOR
    h_bar = max(f, round(height / f) * f)
    w_bar = max(f, round(width / f) * f)
    if (h_bar * w_bar > _QWEN25_MAX_PIXELS
            or max(h_bar, w_bar) > _QWEN25_MAX_SIDE):
        beta = max(math.sqrt(height * width / _QWEN25_MAX_PIXELS),
                   max(height, width) / _QWEN25_MAX_SIDE)
        h_bar = max(f, math.floor(height / beta / f) * f)
        w_bar = max(f, math.floor(width / beta / f) * f)
    elif h_bar * w_bar < _QWEN25_MIN_PIXELS:
        beta = math.sqrt(_QWEN25_MIN_PIXELS / (height * width))
        h_bar = math.ceil(height * beta / f) * f
        w_bar = math.ceil(width * beta / f) * f
    return w_bar, h_bar


def _qwen25_smart_resize_b64(image_b64: str) -> tuple[str, int, int]:
    """Resize a base64 screenshot to its Qwen2.5-VL smart_resize size.

    Returns (b64_png, width, height) — the exact dimensions the model will see,
    which `_parse_uitars` needs to turn its pixel answer into norm1000. Skips
    the re-encode when the image is already that size."""
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(image_b64))) as img:
        w, h = qwen25_smart_resize(img.width, img.height)
        if (w, h) == img.size:
            return image_b64, w, h
        resized = img.convert("RGB").resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), w, h


class CoordSpace(StrEnum):
    """What a grounder's raw coordinates are measured in. ``Grounder.ground``
    converts all of them to NORM1000 before returning."""

    #: 0..1000 on both axes, whatever the image size. Every shipped family,
    #: Qwen3-VL included: it emits norm1000 even when asked for pixels.
    NORM1000 = "norm1000"
    #: Pixels of the image the grounder was sent.
    IMAGE_PIXELS = "image_pixels"
    #: Pixels of the image after Qwen2.5-VL's smart_resize. llama.cpp would
    #: apply that resize server-side, to a size the client never learns, so
    #: ``ground`` resizes first and the answer is in the pixels it sent.
    QWEN25_PIXELS = "qwen25_pixels"


class AxisOrder(StrEnum):
    XYXY = "xyxy"  # Qwen: [x1, y1, x2, y2]
    YXYX = "yxyx"  # Gemma: [y1, x1, y2, x2]


@dataclass(frozen=True)
class ParseContext:
    """The image the grounder saw, and the space its answer is in."""
    width: int
    height: int
    coord_space: CoordSpace = CoordSpace.NORM1000

    def to_norm1000(self, x: int, y: int) -> tuple[int, int]:
        if self.coord_space is CoordSpace.NORM1000:
            return x, y
        return round(x * 1000 / self.width), round(y * 1000 / self.height)


#: A point parser's answer for an explicit "the target isn't there".
NO_MATCH: tuple[()] = ()

_THINK_END = "</think>"
_JSON_START = re.compile(r"[\[{]")
_DECODER = json.JSONDecoder()


def _after_think(text: str) -> str:
    """The answer after a reasoning block. Servers that don't split reasoning
    out leave ``…</think>`` in the content, and the reasoning can quote
    coordinates or JSON of its own."""
    return text.rsplit(_THINK_END, 1)[-1]


def _json_values(text: str) -> Iterator[object]:
    """Every JSON array or object in ``text``, left to right, outermost first.
    A span that doesn't decode is skipped one character at a time, so
    ``Sure! {…}`` and a fenced block both work."""
    i = 0
    while m := _JSON_START.search(text, i):
        try:
            value, end = _DECODER.raw_decode(text, m.start())
        except json.JSONDecodeError:
            i = m.start() + 1
            continue
        yield value
        i = end


def _first_dict(text: str) -> dict | None:
    return next((v for v in _json_values(text) if isinstance(v, dict)), None)


@dataclass(frozen=True, kw_only=True)
class GroundingDialect(ABC):
    """How to ask one model family for matches, and how to read its answer.

    Subclasses own the output shape (``_parse``); the fields are the request
    knobs every family has. ``parse`` strips a reasoning block first and
    returns norm1000 boxes: [] for an explicit no-match, None when nothing
    in the answer could be read (the caller logs that as a failure).
    """
    max_tokens: int
    system_prompt: str = ""
    user_template: str = "Find all matches for: {target}"
    #: Merged into the request as ``chat_template_kwargs`` (e.g. thinking on).
    chat_template_kwargs: dict | None = None
    #: A GBNF grammar constraining the raw output.
    grammar: str | None = None
    #: Small Qwen grounders (Qwen3.5-0.8B) can loop at temp 0, repeating one
    #: box until max_tokens so the JSON never closes; 1.1 breaks the loop
    #: without the noisy multi-detections 1.3+ brings.
    repeat_penalty: float | None = None
    #: Image then text in the user message; MAI-UI is trained text-then-image.
    image_first: bool = True
    #: UI-TARS-1.5 was trained without a system message, and even an empty one
    #: costs it accuracy (20/25 vs 22/25 on the same images).
    send_system: bool = True
    coord_space: CoordSpace = CoordSpace.NORM1000
    #: The family answers with a click point, not an extent: ``ground`` wraps
    #: it in a tiny box, fine for clicks and useless for region ops, which the
    #: server routes to a bbox-capable backend (``family_is_point_only``).
    point_only: ClassVar[bool] = False

    def parse(self, text: str, ctx: ParseContext) -> list[GroundedElement] | None:
        return self._parse(_after_think(text), ctx)

    @abstractmethod
    def _parse(self, text: str, ctx: ParseContext) -> list[GroundedElement] | None: ...


def _box(ctx: ParseContext, x1: int, y1: int, x2: int, y2: int,
         description: str) -> GroundedElement | None:
    """A norm1000 element, or None for a degenerate or inverted box (VLMs
    hedge with [0,0,0,0])."""
    if x2 <= x1 or y2 <= y1:
        return None
    x1, y1 = ctx.to_norm1000(x1, y1)
    x2, y2 = ctx.to_norm1000(x2, y2)
    return GroundedElement(bbox=(x1, y1, x2, y2), description=description)


@dataclass(frozen=True, kw_only=True)
class JsonBoxDialect(GroundingDialect):
    """Boxes as JSON: ``{"detected": [{bbox_key: [...], "description": …}]}``
    (Qwen) or a bare array of ``{bbox_key: [...], "label": …}`` (Gemma 4)."""
    bbox_key: str
    axis_order: AxisOrder = AxisOrder.XYXY

    def _parse(self, text: str, ctx: ParseContext) -> list[GroundedElement] | None:
        # The first list of matches, bare or under "detected". A list of
        # anything else is prose that happens to decode ("Found [2] matches").
        loose = []
        for value in _json_values(text):
            # A match object outside any list: Qwen3-VL-4B sometimes sends one
            # bare, and a list cut off at max_tokens decodes item by item.
            if isinstance(value, dict) and "detected" not in value:
                if _BOX_KEYS & value.keys():
                    loose.append(value)
                continue
            if (detected := _matches(value)) is not None:
                break
        else:
            # A missing comma or quote stops an item decoding, but its box is
            # still good. Decoded items keep descriptions in any key order.
            scanned = [{self.bbox_key: [float(n) for n in m.group(1, 2, 3, 4)],
                        "description": m.group(5) or ""}
                       for m in _LOOSE_BOX_RE.finditer(text)]
            detected = loose if len(loose) >= len(scanned) else scanned
            if not detected:
                return None
        out = []
        for raw in detected:
            if not isinstance(raw, dict):
                raw = {self.bbox_key: raw}
            # Quants drift between the two families' keys, and to bare `bbox`.
            bbox = (raw.get(self.bbox_key) or raw.get("bbox_2d") or raw.get("box_2d")
                    or raw.get("bbox"))
            if not (isinstance(bbox, list) and len(bbox) == 4):
                continue
            try:
                a, b, c, d = (int(v) for v in bbox)
            except (TypeError, ValueError):
                continue
            if self.axis_order is AxisOrder.YXYX:
                a, b, c, d = b, a, d, c
            desc = raw.get("description") or raw.get("label") or ""
            el = _box(ctx, a, b, c, d, desc.strip() if isinstance(desc, str) else "")
            if el is not None:
                out.append(el)
        return out


def _matches(value: object) -> list | None:
    """``value`` as a list of matches ([] is an explicit no-match), or None if
    it isn't one. Qwen3-VL-4B drifts to ``{"detected": [x1, y1, x2, y2]}``."""
    if isinstance(value, dict):
        value = value.get("detected")
        # Only under "detected": four numbers alone are too easily prose.
        if _is_box(value):
            return [value]
    if isinstance(value, list) and all(isinstance(m, dict) or _is_box(m) for m in value):
        # Qwen3.5-27B once wrapped its list in a second "detected".
        flat = []
        for m in value:
            inner = _matches(m) if isinstance(m, dict) and "detected" in m else None
            flat.extend([m] if inner is None else inner)
        return flat
    return None


_BOX_KEYS = {"bbox_2d", "box_2d", "bbox"}
_NUM = r"\s*(-?\d+(?:\.\d+)?)\s*"
# "detected" covers Qwen3-VL-4B's bare box with a stray bracket after it.
_LOOSE_BOX_RE = re.compile(
    rf'"(?:bbox_2d|box_2d|bbox|detected)"\s*:\s*\[{_NUM},{_NUM},{_NUM},{_NUM}\]'
    r'(?:[^\[\]{}]*?"(?:description|label)"\s*:\s*"([^"]*)")?')


def _is_box(value: object) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))


# LocateAnything emits matches as control-token spans, not JSON:
#   <ref>LABEL</ref><box><x1><y1><x2><y2></box>
# xyxy, no permute. The <ref> label echoes the queried phrase, so it is a weak
# disambiguation handle for the N-match case.
_LA_RE = re.compile(r"<ref>(.*?)</ref>\s*<box>((?:<\d+>)+)</box>", re.DOTALL)
_LA_NUM_RE = re.compile(r"<(\d+)>")


@dataclass(frozen=True, kw_only=True)
class LocateAnythingDialect(GroundingDialect):
    def _parse(self, text: str, ctx: ParseContext) -> list[GroundedElement] | None:
        # No <box> at all is a failure, not a no-match: typically the server
        # was started without `--special` and stripped the control tokens.
        if "<box>" not in text:
            return None
        out = []
        for label, coords in _LA_RE.findall(text):
            nums = [int(n) for n in _LA_NUM_RE.findall(coords)]
            # Two numbers are a point (the "Point to:" template, unused here).
            if len(nums) == 4 and (el := _box(ctx, *nums, label.strip())):
                out.append(el)
        return out


@dataclass(frozen=True, kw_only=True)
class PointDialect(GroundingDialect):
    """One click point per answer, wrapped in a tiny norm1000 box."""
    point_only: ClassVar[bool] = True

    def _parse(self, text: str, ctx: ParseContext) -> list[GroundedElement] | None:
        found = self._point(text)
        if found is None:
            return None
        if found == NO_MATCH:
            return []
        return [_point_to_element(*ctx.to_norm1000(*found))]

    @abstractmethod
    def _point(self, text: str) -> tuple[int, int] | tuple[()] | None:
        """(x, y) in the dialect's space, NO_MATCH, or None if unreadable."""


_POINT_XY_RE = re.compile(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]")
_FLAT_XY_RE = re.compile(r'"x"\s*:\s*(-?\d+)\s*,\s*"y"\s*:\s*(-?\d+)')
_X_ARRAY_RE = re.compile(r'"x"\s*:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)')
_KW_XY_RE = re.compile(r"\bx\s*=\s*(-?\d+)[\s,]+y\s*=\s*(-?\d+)")
_CALL_XY_RE = re.compile(r"click\w*\(\s*(-?\d+)\s*,\s*(-?\d+)")
_PAREN_XY_RE = re.compile(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_MAIUI_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)
# UI-TARS's unprompted no-match answer. Precise but low-recall: never said about
# a present target (0/25), but only 6/11 absent targets got it — the rest got a
# confident wrong point. Prompting for an explicit NOT_FOUND token instead
# flipped 10/25 present targets to NOT_FOUND, so we only recognise this phrase.
_UITARS_NONE_RE = re.compile(r"\bthere (?:are|is) (?:none|no)\b", re.IGNORECASE)


def _search_xy(text: str, *patterns: re.Pattern) -> tuple[int, int] | None:
    for pattern in patterns:
        if m := pattern.search(text):
            return int(m.group(1)), int(m.group(2))
    return None


@dataclass(frozen=True, kw_only=True)
class HoloDialect(PointDialect):
    """``{"action": "click_absolute", "x": …, "y": …}`` or
    ``{"action": "not_found"}``. Without the grammar the shape drifts: a stray
    colon before a nested object, array-valued ``x``, a bare ``[x, y]``."""

    def _point(self, text):
        obj = _first_dict(text)
        if obj is not None:
            if obj.get("action") == "not_found":
                return NO_MATCH
            x, y = obj.get("x"), obj.get("y")
            if isinstance(x, int) and isinstance(y, int):
                return x, y
        return _search_xy(text, _FLAT_XY_RE, _X_ARRAY_RE, _POINT_XY_RE)


@dataclass(frozen=True, kw_only=True)
class VenusDialect(PointDialect):
    """Bare ``[x, y]``; a negative coordinate (``[-1, -1]``) is its no-match."""

    def _point(self, text):
        xy = _search_xy(text, _POINT_XY_RE)
        if xy is not None and min(xy) < 0:
            return NO_MATCH
        return xy


@dataclass(frozen=True, kw_only=True)
class GuiOwlDialect(PointDialect):
    """A ``computer_use`` tool call on a declared 1000x1000 screen; a
    ``terminate`` action is its no-match."""

    def _point(self, text):
        for value in _json_values(text):
            args = value.get("arguments") if isinstance(value, dict) else None
            if not isinstance(args, dict):
                continue
            if args.get("action") == "terminate":
                return NO_MATCH
            coord = args.get("coordinate")
            if (isinstance(coord, list) and len(coord) == 2
                    and all(isinstance(v, int) for v in coord)):
                return coord[0], coord[1]
        return None


@dataclass(frozen=True, kw_only=True)
class MaiUiDialect(PointDialect):
    """``<answer>{"coordinate": [x, y]}</answer>`` after a
    ``<grounding_think>`` block; a stray ``[x, y]`` in the reasoning mustn't
    win, so the answer block is read first."""

    def _point(self, text):
        answer = _MAIUI_ANSWER_RE.search(text)
        return _search_xy(answer.group(1) if answer else text, _POINT_XY_RE)


@dataclass(frozen=True, kw_only=True)
class EvoCuaDialect(PointDialect):
    """A ``pyautogui.click(x=…, y=…)`` code block, or positional ``click(x, y)``."""

    def _point(self, text):
        return _search_xy(text, _KW_XY_RE, _CALL_XY_RE)


@dataclass(frozen=True, kw_only=True)
class UiTarsDialect(PointDialect):
    """``(x, y)``, bare or wrapped as ``click(start_box='<|box_start|>(x,y)…')``,
    in pixels of the smart-resized image; "There are none." is its no-match."""

    def _point(self, text):
        if _UITARS_NONE_RE.search(text):
            return NO_MATCH
        return _search_xy(text, _PAREN_XY_RE, _KW_XY_RE)


DIALECTS: dict[str, GroundingDialect] = {
    "qwen": JsonBoxDialect(
        system_prompt=_QWEN_GROUND_SYSTEM,
        bbox_key="bbox_2d",
        # One match is ~55 tokens; Qwen3.8-27B lists eight instances in ~400,
        # and at 320 the cut-off list lost every box before salvage existed.
        max_tokens=1024,
        repeat_penalty=1.1,
    ),
    "gemma4": JsonBoxDialect(
        system_prompt=_GEMMA4_GROUND_SYSTEM,
        bbox_key="box_2d",
        axis_order=AxisOrder.YXYX,
        # Gemma 4's chat template thinks by default: 200-500 tokens even at
        # temp 0 before the JSON. 2048 survives verbose targets; turning
        # thinking off costs accuracy, so we pay the budget instead.
        max_tokens=2048,
    ),
    # NVIDIA LocateAnything-3B: control tokens, ~10-15 tokens per box, no
    # thinking. The instruction goes in the user message. Needs the llama.cpp
    # fork started with `--special`, or the server strips the tokens and every
    # locate no-matches. Grounding-only.
    "locateanything": LocateAnythingDialect(
        max_tokens=256,
        user_template="Locate all the instances that match the following description: {target}.",
    ),
    # HCompany Holo-3.1: one point, best match only (no N-match list).
    # Grounding-only. Careful mode: thinking on, so it can tell the target is
    # off-screen, and the grammar keeps the final answer clean. Works best
    # with `--image-min-tokens 1280 --image-max-tokens 1280`.
    "holo": HoloDialect(
        max_tokens=512,
        user_template=_HOLO_GROUND_USER,
        chat_template_kwargs={"enable_thinking": True},
        grammar=_HOLO_GRAMMAR,
    ),
    # UI-Venus 1.5 (2B/8B/30B-A3B). Stock llama.cpp, same flags as qwen.
    "venus": VenusDialect(max_tokens=128, user_template=_VENUS_GROUND_USER),
    # GUI-Owl 1.5 (Instruct + Think). Think variants may want more budget.
    "guiowl": GuiOwlDialect(
        system_prompt=_GUIOWL_GROUND_SYSTEM,
        max_tokens=256,
        user_template="{target}",
    ),
    # MAI-UI: always spends a think block, so it needs headroom.
    "maiui": MaiUiDialect(
        system_prompt=_MAIUI_GROUND_SYSTEM,
        max_tokens=640,
        user_template="{target}\n",
        image_first=False,
    ),
    # meituan EvoCUA, a full CUA agent used grounding-only. The 32B
    # (Qwen3-VL-32B-Thinking based) will want more than 512.
    "evocua": EvoCuaDialect(
        system_prompt=_EVOCUA_GROUND_SYSTEM,
        max_tokens=512,
        user_template="Click on {target}.",
    ),
    # ByteDance UI-TARS-1.5-7B. Serve with `--image-min-tokens 1024` and no
    # --image-max-tokens below 2560, so the server leaves the pre-resized
    # image alone.
    "uitars": UiTarsDialect(
        max_tokens=128,
        user_template=_UITARS_GROUND_USER,
        coord_space=CoordSpace.QWEN25_PIXELS,
        send_system=False,
    ),
}

# Describe is structurally simpler than ground (free-form prose, not JSON)
# but the same per-family considerations apply: Gemma 4 paces well without
# explicit length cuffs and prefers short framing; Qwen tolerates more
# explicit constraints. The hint is now hoisted into the user message as
# `Focus: ...` so the system prompt doesn't have to branch on whether the
# user message contains a focus — it's marked structurally instead.
_QWEN_DESCRIBE_SYSTEM = (
    "You describe a computer screen for a text-only agent that cannot see "
    "images. Report concretely: focused application/window, key UI elements "
    "and their state (buttons, tabs, toggles, input values), visible text, "
    "dialogs, and overlays. Do not speculate about hidden content. Aim for "
    "120-220 words. Plain prose, no markdown.\n"
    "\n"
    "When the user message starts with \"Focus:\", lead with what's true of "
    "that focus, but still surface major UI state (focused window, dialogs, "
    "error banners) so the agent isn't blindsided."
)

_GEMMA4_DESCRIBE_SYSTEM = (
    "You describe a computer screen for a text-only agent that cannot see "
    "images. Be concrete: focused app/window, visible UI elements and their "
    "state, text content, dialogs, overlays. Do not speculate about hidden "
    "content. Plain prose, no markdown (no **bold**, no `*` bullets, no "
    "headers).\n"
    "\n"
    "When the user message starts with \"Focus:\", lead with what's true of "
    "that focus, but still surface major UI state (focused window, dialogs, "
    "error banners) so the agent isn't blindsided."
)


# Image-mode describe prompts — used when the caller passes a file path
# (e.g. a webcam snapshot) instead of a desktop screenshot. The desktop
# prompts above coach the VLM to invent "focused windows" and "dialogs"
# for arbitrary photos, which biases output toward hallucinated UI
# structure; these versions strip the desktop framing and just ask for
# a faithful description of the frame.
_QWEN_IMAGE_DESCRIBE_SYSTEM = (
    "You describe an image for a text-only agent that cannot see images. "
    "Report concretely: subjects and objects in the frame, their layout "
    "and spatial relationships, visible text, lighting and color, notable "
    "details. Do not speculate about what's outside the frame. Aim for "
    "100-200 words. Plain prose, no markdown.\n"
    "\n"
    "When the user message ends with \"(attention: ...)\", lead with what's "
    "true of that focus before describing the rest of the frame."
)

_GEMMA4_IMAGE_DESCRIBE_SYSTEM = (
    "You describe an image for a text-only agent that cannot see images. "
    "Be concrete: subjects, layout, visible text, lighting, colors, notable "
    "details. Do not speculate about what's outside the frame. Plain prose, "
    "no markdown (no **bold**, no `*` bullets, no headers).\n"
    "\n"
    "When the user message ends with \"(attention: ...)\", lead with what's "
    "true of that focus before describing the rest of the frame."
)


# MiniCPM-V (4.0 / 4.5 / 4.6) describe dialect. Validated E2E as the strongest
# non-Qwen prose backend — its OCR reads dense terminal/UI text Qwen glosses.
# Two tuning points earned from that run: (1) it ignores a soft "no markdown"
# instruction and emits `-` bullets, so the anti-markdown framing here is firm
# and up front; (2) it's the agent's only eyes on a text-dense screen, so the
# prompt leans on faithful transcription over summary. No thinking channel, so
# the budget is a plain cap.
_MINICPM_DESCRIBE_SYSTEM = (
    "You describe a computer screen for a text-only agent that cannot see "
    "images — you are its only eyes. Report concretely: focused "
    "application/window, key UI elements and their state (buttons, tabs, "
    "toggles, input values), visible text read faithfully, dialogs, and "
    "overlays. Do not speculate about hidden content or invent details you "
    "cannot read. Aim for 120-220 words.\n"
    "\n"
    "Write ONE flowing paragraph of plain prose. No markdown of any kind: no "
    "bullet lists, no '-' or '*' item markers, no numbered lists, no headings, "
    "no bold or italic. If you would list items, join them into a sentence.\n"
    "\n"
    "When the user message starts with \"Focus:\", lead with what's true of "
    "that focus, but still surface major UI state (focused window, dialogs, "
    "error banners) so the agent isn't blindsided."
)

_MINICPM_IMAGE_DESCRIBE_SYSTEM = (
    "You describe an image for a text-only agent that cannot see images — you "
    "are its only eyes. Report concretely: subjects and objects in the frame, "
    "their layout and spatial relationships, visible text read faithfully, "
    "lighting and color, notable details. Do not speculate about what's "
    "outside the frame or invent details you cannot see. Aim for 100-200 "
    "words.\n"
    "\n"
    "Write ONE flowing paragraph of plain prose. No markdown of any kind: no "
    "bullet lists, no '-' or '*' item markers, no numbered lists, no headings, "
    "no bold or italic.\n"
    "\n"
    "When the user message ends with \"(attention: ...)\", lead with what's "
    "true of that focus before describing the rest of the frame."
)

@dataclass(frozen=True)
class DescribeDialect:
    """One describe prompt: the system prompt, the budget, and the user
    message with and without a hint. Keeping the hint template here keeps
    the system prompt's ``Focus:`` / ``(attention: …)`` contract next to the
    template that produces it."""
    system_prompt: str
    max_tokens: int
    user_no_hint: str
    user_with_hint: str  # contains "{hint}"


@dataclass(frozen=True)
class DescribePair:
    """A family's describe prompts: ``screen`` for screenshots, ``image`` for
    an arbitrary picture (e.g. a webcam frame). The desktop prompts coach the
    model to find "focused windows" and "dialogs", which it then invents for
    photos; the image prompts drop that framing and mark a hint at the end,
    ``(attention: …)``, where the screen prompts lead with ``Focus:``."""
    screen: DescribeDialect
    image: DescribeDialect


DESCRIBE_DIALECTS: dict[str, DescribePair] = {
    "qwen": DescribePair(
        screen=DescribeDialect(
            system_prompt=_QWEN_DESCRIBE_SYSTEM,
            # Covers Qwen3-VL-4B (~200 tokens, no thinking) and the small
            # Qwen3.5 variants (thinking by default, ~1200 before the prose).
            # A cap, not a target: the cheap path still stops early.
            max_tokens=1500,
            user_no_hint="Describe everything visible on the screen.",
            user_with_hint="Focus: {hint}\n\nDescribe the screen with this focus.",
        ),
        image=DescribeDialect(
            system_prompt=_QWEN_IMAGE_DESCRIBE_SYSTEM,
            max_tokens=1500,
            user_no_hint="Describe this image.",
            user_with_hint="Describe this image (attention: {hint}).",
        ),
    ),
    "gemma4": DescribePair(
        screen=DescribeDialect(
            system_prompt=_GEMMA4_DESCRIBE_SYSTEM,
            # Thinking burns 200-500 tokens first, and terse user messages
            # provoke more of it; below 1024 short hints come back empty.
            max_tokens=1024,
            user_no_hint="Describe the screen.",
            user_with_hint="Focus: {hint}\n\nDescribe the screen, leading with this focus.",
        ),
        image=DescribeDialect(
            system_prompt=_GEMMA4_IMAGE_DESCRIBE_SYSTEM,
            max_tokens=1024,
            user_no_hint="Describe this image.",
            user_with_hint="Describe this image (attention: {hint}).",
        ),
    ),
    # MiniCPM-V: describe-only (validated for prose, not coordinates, so it
    # has no grounding dialect). No thinking; the user templates restate
    # "plain prose" because it is markdown-happy.
    "minicpm": DescribePair(
        screen=DescribeDialect(
            system_prompt=_MINICPM_DESCRIBE_SYSTEM,
            max_tokens=1024,
            user_no_hint="Describe everything visible on the screen in plain prose.",
            user_with_hint="Focus: {hint}\n\nDescribe the screen with this focus, in plain prose.",
        ),
        image=DescribeDialect(
            system_prompt=_MINICPM_IMAGE_DESCRIBE_SYSTEM,
            max_tokens=1024,
            user_no_hint="Describe this image in plain prose.",
            user_with_hint="Describe this image in plain prose (attention: {hint}).",
        ),
    ),
}


def family_can_describe(family: str) -> bool:
    """Whether ``family`` can serve describe (False for grounding-only
    families like LocateAnything). Config's split-backend routing keys off it."""
    return family in DESCRIBE_DIALECTS


def family_can_ground(family: str) -> bool:
    """Whether ``family`` can serve ground/locate/click (False for
    describe-only families like minicpm). Config rejects a describe-only
    CONDUCTOR_DESKTOP_GROUNDER_FAMILY with it."""
    return family in DIALECTS


def family_is_point_only(family: str) -> bool:
    """Whether ``family`` answers with a click point and no extent (Holo and
    the other point dialects). Region ops (crop, locate.bbox, N-match) then
    route to a bbox-capable backend. False for an unknown family."""
    d = DIALECTS.get(family)
    return bool(d and d.point_only)


def image_size(image_b64: str) -> tuple[int, int]:
    """Width and height of a base64 image; nothing is decoded."""
    from .imaging import b64_dims

    return b64_dims(image_b64)


class Grounder:
    def __init__(self, url: str, family: str = "qwen", timeout: float = 30.0,
                 coord_space: CoordSpace | None = None) -> None:
        """``coord_space`` overrides a JSON-box family's (CONDUCTOR_DESKTOP_COORD_SYSTEM),
        for a model that answers in pixels, such as Qwen2.5-VL."""
        self._family = family
        # A family may ground, describe, or both; the missing side raises a
        # clear GrounderError when called.
        self._dialect = DIALECTS.get(family)
        self._describe = DESCRIBE_DIALECTS.get(family)
        if self._dialect is None and self._describe is None:
            raise ValueError(
                f"unknown grounder family {family!r}; choose one of: "
                f"{sorted(set(DIALECTS) | set(DESCRIBE_DIALECTS))}"
            )
        if coord_space is not None and isinstance(self._dialect, JsonBoxDialect):
            self._dialect = dataclasses.replace(self._dialect, coord_space=coord_space)
        elif coord_space is CoordSpace.IMAGE_PIXELS and self._dialect is not None:
            # norm1000 is what every other dialect returns anyway, so an
            # explicit norm1000 (the documented setting for years) is harmless.
            raise ValueError(
                f"CONDUCTOR_DESKTOP_COORD_SYSTEM=image_pixels applies to the qwen and gemma4 "
                f"families; {family!r} fixes its own coordinate space")
        # The URL is the server's base: requests go to {url}/v1/chat/completions.
        # An OpenAI-style base that already ends in /v1 means the same server.
        self._url = url.rstrip("/").removesuffix("/v1")
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def _post_chat(self, payload: dict) -> dict:
        """POST a chat-completions request, mapping transport/HTTP failures
        to a ``GrounderError`` whose message tells the agent what to do.

        Catches ``httpx.HTTPError`` — the base for every timeout, connection,
        and ``raise_for_status`` failure — so a slow or down grounder surfaces
        as a clear soft error instead of a raw protocol exception. Note this
        does NOT catch ``asyncio.CancelledError`` (it's BaseException, not
        Exception); a caller-side cancel still propagates, by design.
        """
        url = f"{self._url}/v1/chat/completions"
        try:
            r = await self._http.post(url, json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException as e:
            raise GrounderError(
                f"grounder timed out after {self._http.timeout.read}s talking "
                f"to {self._url} ({type(e).__name__}). The remote VLM is slow "
                f"or wedged — check the grounder server, or raise "
                f"CONDUCTOR_DESKTOP_GROUNDER_TIMEOUT."
            ) from e
        except httpx.HTTPStatusError as e:
            raise GrounderError(
                f"grounder at {self._url} returned HTTP "
                f"{e.response.status_code}. The model server is up but "
                f"rejected the request — check the server logs and that the "
                f"grounder model/family is loaded."
            ) from e
        except httpx.HTTPError as e:
            raise GrounderError(
                f"could not reach the grounder at {self._url} "
                f"({type(e).__name__}: {e}). Check the server is running and "
                f"CONDUCTOR_DESKTOP_GROUNDER_URL points at it."
            ) from e

    async def ground(self, image_b64: str, target: str
                     ) -> list[GroundedElement]:
        """Every element matching ``target``, as norm1000 boxes.

        Empty list = nothing matches. The caller applies the 0/1/N policy.
        """
        if self._dialect is None:
            raise GrounderError(
                f"grounding is not supported on the {self._family!r} backend — "
                f"it is a describe-only family. Point ground/locate/click at a "
                f"grounding-capable backend (qwen, holo, venus, guiowl, ...)."
            )
        d = self._dialect
        width = height = 0
        if d.coord_space is CoordSpace.QWEN25_PIXELS:
            image_b64, width, height = await asyncio.to_thread(_qwen25_smart_resize_b64, image_b64)
        elif d.coord_space is CoordSpace.IMAGE_PIXELS:
            width, height = await asyncio.to_thread(image_size, image_b64)
            if width <= 0 or height <= 0:
                raise GrounderError(
                    f"the {self._family!r} grounder answers in pixels of the image "
                    f"it was sent, and that image can't be sized.")
        img_part = {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{image_b64}"}}
        txt_part = {"type": "text",
                    "text": d.user_template.format(target=target)}
        user_content = ([img_part, txt_part] if d.image_first
                        else [txt_part, img_part])
        messages = [{"role": "user", "content": user_content}]
        if d.send_system:
            messages.insert(0, {"role": "system", "content": d.system_prompt})
        payload = {
            "messages": messages,
            "temperature": 0,
            "max_tokens": d.max_tokens,
        }
        if d.chat_template_kwargs:
            payload["chat_template_kwargs"] = d.chat_template_kwargs
        if d.grammar:
            payload["grammar"] = d.grammar
        if d.repeat_penalty is not None:
            payload["repeat_penalty"] = d.repeat_penalty
        data = await self._post_chat(payload)
        content = (data.get("choices", [{}])[0]
                       .get("message", {})
                       .get("content", "")) or ""
        elements = d.parse(content, ParseContext(width, height, d.coord_space))
        if elements is None:
            log.warning("grounder returned unparseable response for '%s'; raw: %s",
                        target, content[:300])
            return []
        return elements

    async def describe(self, image_b64: str, hint: str | None = None) -> str:
        """Return a prose description of the screenshot for text-only drivers.

        `hint`: optional focus the brain wants the description anchored on
        (e.g. "the rightmost chart"). The dialect's `user_with_hint` template
        wraps the hint in a `Focus: ...` prefix so the system prompt can
        recognize the focus structurally instead of inferring it from prose.
        """
        if self._describe is None:
            raise self._no_describe_error()
        return await self._describe_with(image_b64, "image/png",
                                         self._describe.screen, hint)

    async def describe_image(self, image_b64: str, mime: str = "image/png",
                             hint: str | None = None) -> str:
        """Describe an arbitrary image (not a screen capture).

        Used by the file-ingesting leg of the `read_screen` MCP tool — e.g.
        a webcam frame the agent grabbed via `exec_shell`. Identical
        request shape to `describe`, but routes through the image-mode
        dialect whose prompts don't assume a desktop screenshot (so the
        VLM doesn't invent "focused windows" or "dialogs" for a photo).
        """
        if self._describe is None:
            raise self._no_describe_error()
        return await self._describe_with(image_b64, mime,
                                         self._describe.image, hint)

    def _no_describe_error(self) -> GrounderError:
        return GrounderError(
            f"describe is not supported on the {self._family!r} backend — it "
            f"is a grounding-only model. Route describe/describe_image to a "
            f"Qwen3-VL grounder (set CONDUCTOR_DESKTOP_GROUNDER_FAMILY=qwen, or "
            f"point a separate describe URL at a Qwen3-VL server)."
        )

    async def _describe_with(self, image_b64: str, mime: str,
                             dialect: DescribeDialect,
                             hint: str | None) -> str:
        if hint:
            user_text = dialect.user_with_hint.format(hint=hint)
        else:
            user_text = dialect.user_no_hint
        payload = {
            "messages": [
                {"role": "system", "content": dialect.system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{image_b64}"}},
                    {"type": "text", "text": user_text},
                ]},
            ],
            "temperature": 0.2,
            "max_tokens": dialect.max_tokens,
        }
        data = await self._post_chat(payload)
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            return ""


def bbox_to_px(bbox: tuple[int, int, int, int], screen_w: int, screen_h: int,
               img_w: int, img_h: int, space: CoordSpace | str) -> tuple[int, int, int, int]:
    """Project a box to screen pixels, ordered ``(x1, y1, x2, y2)``.

    ``space`` says what the box is in: NORM1000 (every grounder's output,
    local or managed) or IMAGE_PIXELS of an ``img_w`` x ``img_h`` image
    (OmniParser). Nothing is guessed from the values.
    """
    space = CoordSpace(space)
    if space is CoordSpace.NORM1000:
        from_w = from_h = 1000
    elif space is CoordSpace.IMAGE_PIXELS and img_w > 0 and img_h > 0:
        from_w, from_h = img_w, img_h
    else:
        raise ValueError(f"can't project {space} coordinates from a {img_w}x{img_h} image")
    xs = sorted((bbox[0] * screen_w // from_w, bbox[2] * screen_w // from_w))
    ys = sorted((bbox[1] * screen_h // from_h, bbox[3] * screen_h // from_h))
    return xs[0], ys[0], xs[1], ys[1]
