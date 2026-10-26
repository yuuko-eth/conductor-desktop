"""Parser-level checks for the per-family grounder dialects.

No real VLM here: each dialect's ``parse`` runs against canned responses to
lock in axis permutation, key fallbacks, no-match signals, and the rule that
every answer comes back in norm1000.
"""

from __future__ import annotations

from conductor_desktop.grounder import (
    DESCRIBE_DIALECTS,
    DIALECTS,
    CoordSpace,
    GroundedElement,
    HoloDialect,
    LocateAnythingDialect,
    ParseContext,
    bbox_to_px,
    family_is_point_only,
)

_NORM = ParseContext(0, 0)  # norm1000 answers need no image size


def parse(family, text, ctx=_NORM):
    return DIALECTS[family].parse(text, ctx)

# --- Qwen dialect (default) -----------------------------------------------

def test_qwen_dialect_passes_xyxy_through():
    resp = '{"detected": [{"bbox_2d": [10, 20, 100, 200], "description": "x"}]}'
    out = parse("qwen", resp)
    assert out == [GroundedElement(bbox=(10, 20, 100, 200), description="x")]


def test_qwen_dialect_accepts_bare_bbox_alias():
    """Some quants drift from `bbox_2d` to `bbox`; existing tolerance preserved."""
    resp = '{"detected": [{"bbox": [10, 20, 100, 200], "description": "x"}]}'
    out = parse("qwen", resp)
    assert out and out[0].bbox == (10, 20, 100, 200)


# --- Gemma 4 dialect ------------------------------------------------------

def test_gemma4_dialect_permutes_yxyx_to_canonical_xyxy():
    """Gemma emits [y1, x1, y2, x2]; parser normalizes to (x1, y1, x2, y2)."""
    resp = '{"detected": [{"box_2d": [20, 10, 200, 100], "description": "x"}]}'
    out = parse("gemma4", resp)
    assert out and out[0].bbox == (10, 20, 100, 200)


def test_gemma4_dialect_accepts_qwen_key_when_model_drifts():
    """If a Gemma model accidentally emits `bbox_2d`, still permute correctly."""
    resp = '{"detected": [{"bbox_2d": [20, 10, 200, 100], "description": "x"}]}'
    out = parse("gemma4", resp)
    assert out and out[0].bbox == (10, 20, 100, 200)


def test_gemma4_bare_array_shape_is_native():
    """Gemma 4's native output is a top-level array, no `detected` wrapper."""
    resp = '[{"box_2d": [20, 10, 200, 100], "label": "x"}]'
    out = parse("gemma4", resp)
    assert out == [GroundedElement(bbox=(10, 20, 100, 200), description="x")]


def test_gemma4_label_field_accepted_alongside_description():
    """Gemma natively labels matches with `label`; Qwen with `description`. Both work."""
    label_resp = '[{"box_2d": [20, 10, 200, 100], "label": "battery indicator"}]'
    out = parse("gemma4", label_resp)
    assert out and out[0].description == "battery indicator"


def test_bare_array_empty_is_explicit_no_match():
    """A bare `[]` is a valid no_match, distinct from unparseable garbage."""
    out = parse("gemma4", "[]")
    assert out == []


# --- LocateAnything dialect (control-token output) ------------------------

def test_locateanything_parses_ref_box_to_canonical_xyxy():
    """Native control-token span → (x1, y1, x2, y2), normalized 0..1000."""
    resp = "<ref>wifi icon</ref><box><10><20><100><200></box>"
    out = parse("locateanything", resp)
    assert out == [GroundedElement(bbox=(10, 20, 100, 200), description="wifi icon")]


def test_locateanything_enumerates_multiple_instances():
    resp = (
        "<ref>dock app icon</ref><box><10><900><60><960></box>"
        "<ref>dock app icon</ref><box><70><900><120><960></box>"
    )
    out = parse("locateanything", resp)
    assert len(out) == 2
    assert out[0].bbox == (10, 900, 60, 960)
    assert out[1].bbox == (70, 900, 120, 960)


def test_locateanything_rejects_inverted_box():
    """Same degenerate-bbox policy as the JSON path."""
    resp = "<ref>x</ref><box><100><100><50><50></box>"
    assert parse("locateanything", resp) == []


def test_locateanything_no_box_token_is_unparseable():
    """No <box> at all → None (true failure, e.g. server lacked --special and
    stripped the control tokens, leaving the plain-text label only)."""
    assert parse("locateanything", "Apple logo") is None


def test_locateanything_explicit_no_match_returns_empty():
    """A <box>-bearing response with no valid ref/box span is an explicit
    no-match ([]), distinct from the unparseable None case above."""
    # A lone, malformed box token: '<box>' substring present, but no full
    # <ref>…</ref><box>…</box> span matches → [].
    assert parse("locateanything", "<box></box>") == []


def test_locateanything_dialect_config():
    d = DIALECTS["locateanything"]
    assert isinstance(d, LocateAnythingDialect)
    assert d.system_prompt == ""
    assert "{target}" in d.user_template


# --- Cross-dialect invariants ---------------------------------------------

def test_explicit_no_match_returns_empty_list():
    for family in ("qwen", "gemma4"):
        assert parse(family, '{"detected": []}') == []


def test_inverted_bbox_rejected_in_both_dialects():
    """A box where (x2,y2) <= (x1,y1) is degenerate regardless of input order."""
    qwen_inv = '{"detected": [{"bbox_2d": [100, 100, 50, 50], "description": "x"}]}'
    gemma_inv = '{"detected": [{"box_2d": [100, 100, 50, 50], "description": "x"}]}'
    assert parse("qwen", qwen_inv) == []
    assert parse("gemma4", gemma_inv) == []


def test_unparseable_response_returns_none():
    """Distinguish 'no JSON at all' (None — log a real failure) from
    'JSON parsed but empty array' ([] — explicit no_match)."""
    assert parse("qwen", "hello there, no json") is None


def test_dialect_table_has_known_families():
    assert set(DIALECTS) == {"qwen", "gemma4", "locateanything", "holo",
                             "venus", "guiowl", "maiui", "evocua", "uitars"}
    assert DIALECTS["qwen"].axis_order == "xyxy"
    assert DIALECTS["gemma4"].axis_order == "yxyx"
    # Gemma needs more headroom for the thinking channel; locking in the
    # ordering so a future reduction is a deliberate decision, not drift.
    assert DIALECTS["gemma4"].max_tokens > DIALECTS["qwen"].max_tokens
    # LocateAnything pays no thinking tax — its budget is a fraction of
    # either JSON family's.
    assert DIALECTS["locateanything"].max_tokens < DIALECTS["gemma4"].max_tokens


def test_point_only_flag_is_holo_only():
    """Only Holo emits a click point with no real bbox extent; every other
    family produces real boxes, so region ops can use them directly."""
    assert DIALECTS["holo"].point_only is True
    assert DIALECTS["qwen"].point_only is False
    assert DIALECTS["gemma4"].point_only is False
    assert DIALECTS["locateanything"].point_only is False


def test_family_is_point_only_helper():
    assert family_is_point_only("holo")
    assert not family_is_point_only("qwen")
    assert not family_is_point_only("gemma4")
    assert not family_is_point_only("locateanything")
    # Unknown family has no dialect → not the special point-only case.
    assert not family_is_point_only("bogus")


def test_holo_dialect_careful_mode():
    """Holo careful mode: thinking ON + a think-inclusive union grammar. Thinking
    is load-bearing for no-match detection (no-think never emits not_found), so it
    MUST be enabled, and the grammar MUST model the </think> boundary — an eager
    grammar would forbid the reasoning tokens and silently kill no-match detection.
    The grammar's union includes the not_found shape so absence is expressible."""
    d = DIALECTS["holo"]
    assert isinstance(d, HoloDialect)
    assert d.chat_template_kwargs == {"enable_thinking": True}
    assert d.grammar is not None
    assert "</think>" in d.grammar      # think-inclusive, not eager
    assert "not_found" in d.grammar     # union includes the no-match shape
    assert d.max_tokens >= 256          # room to reason before the final JSON


def test_parse_holo_not_found_is_clean_no_match():
    """An explicit not_found answer parses to [] (clean no-match), distinct from
    None (unparseable). Both route to the caller's 0-match path, but [] avoids the
    spurious 'unparseable' warning."""
    assert parse("holo", '{"action": "not_found"}') == []
    assert parse("holo", "") is None


def test_parse_holo_recovers_all_observed_shapes():
    """Holo's output shape drifts; the parser recovers a single 0..1000 point
    from every shape seen in the wild, and returns None when no coords exist."""

    def center(s):
        els = parse("holo", s)
        return els[0].center if els else None

    # clean flat
    assert center('{"action":"click_absolute","x":951,"y":969}') == (951, 969)
    # malformed stray-colon nested object
    assert center('{"action":"click_absolute":{"x":31,"y":47}}') == (31, 47)
    # array-valued x with scalar y
    assert center('{"action": "click_absolute", "x": [579, 481], "y": 481}') == (579, 481)
    # bare array after stray colon (also truncated)
    assert center('{"action": "click_absolute": [941, 967]') == (941, 967)
    # surrounding prose
    assert center('Sure! {"action":"click_absolute","x":500,"y":500} done') == (500, 500)
    # no coordinates / no integers → no-match (None, not a fabricated point)
    assert parse("holo", '{"action":"click_absolute"}') is None
    assert parse("holo", "no numbers here") is None
    assert parse("holo", "") is None


def test_parse_holo_box_is_non_degenerate_and_centered():
    """The synthetic box must survive the x2>x1/y2>y1 guard. An interior point
    centers exactly; at the 0/1000 edges a clamped box can't center on the edge
    (no negative coords) so it lands within a half-box — a few px, acceptable."""
    from conductor_desktop.grounder import _POINT_BOX_HALF
    # interior: exact center
    el = parse("holo", '{"x":500,"y":500}')[0]
    assert el.bbox[2] > el.bbox[0] and el.bbox[3] > el.bbox[1]
    assert el.center == (500, 500)
    # edges: non-degenerate, center within a half-box of the point, in-bounds
    for s, pt in [('{"x":0,"y":0}', (0, 0)), ('{"x":1000,"y":1000}', (1000, 1000))]:
        x1, y1, x2, y2 = parse("holo", s)[0].bbox
        assert x2 > x1 and y2 > y1
        assert x1 >= 0 and x2 <= 1000 and y1 >= 0 and y2 <= 1000
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        assert abs(cx - pt[0]) <= _POINT_BOX_HALF and abs(cy - pt[1]) <= _POINT_BOX_HALF


# --- Qwen3-VL GUI-agent point families (venus/guiowl/maiui/evocua) --------
# Raw strings below are real completions captured end to end (8B Q4).

def test_parse_venus_point_and_no_match():
    els = parse("venus", "[932, 86]")
    assert len(els) == 1 and els[0].center == (932, 86)
    assert parse("venus", "[-1,-1]") == []        # native infeasible signal
    assert parse("venus", "[10, -1]") == []        # any negative → no-match
    assert parse("venus", "no coordinates here") is None


def test_parse_guiowl_tool_call_and_terminate():
    hit = ('<tool_call>\n{"name": "computer_use", "arguments": '
           '{"action": "left_click", "coordinate": [932, 87]}}\n</tool_call>')
    els = parse("guiowl", hit)
    assert len(els) == 1 and els[0].center == (932, 87)
    miss = ('<tool_call>\n{"name": "computer_use", "arguments": '
            '{"action": "terminate", "status": "failure"}}\n</tool_call>')
    assert parse("guiowl", miss) == []
    assert parse("guiowl", "<tool_call>garbage</tool_call>") is None


def test_parse_maiui_answer_scope():
    # The coord inside <answer> must win over a stray [x,y] in the reasoning.
    raw = ('<grounding_think>\nThought: near [111,222] maybe</grounding_think>'
           '<answer>\n{"coordinate": [934, 87]}\n</answer>')
    els = parse("maiui", raw)
    assert len(els) == 1 and els[0].center == (934, 87)
    # No <answer> wrapper → fall back to the whole content (interior point
    # centers exactly; edge points clamp, per _point_to_element).
    assert parse("maiui", '{"coordinate": [500, 600]}')[0].center == (500, 600)
    assert parse("maiui", "<grounding_think>no coord</grounding_think>") is None


def test_parse_evocua_pyautogui():
    els = parse("evocua", "```python\npyautogui.click(x=934, y=85)\n```")
    assert len(els) == 1 and els[0].center == (934, 85)
    # Positional form also accepted.
    assert parse("evocua", "pyautogui.click(934, 85)")[0].center == (934, 85)
    assert parse("evocua", "no action") is None


def test_point_families_are_point_only_and_grounding_only():
    from conductor_desktop.grounder import family_can_describe
    for f in ("venus", "guiowl", "maiui", "evocua", "uitars"):
        assert DIALECTS[f].point_only is True
        assert family_is_point_only(f) is True
        # Grounding-only: no describe dialect → describe must route elsewhere.
        assert family_can_describe(f) is False


def test_maiui_is_text_first():
    """MAI-UI is trained text-then-image; the rest are image-first."""
    assert DIALECTS["maiui"].image_first is False
    for f in ("venus", "guiowl", "evocua", "qwen", "holo"):
        assert DIALECTS[f].image_first is True


def test_qwen_dialect_carries_repeat_penalty():
    """Tiny Qwen VLMs (Qwen3.5-0.8B) degenerate into a repeat-loop at greedy
    temp=0, leaving the bbox_2d array unterminated → unparseable no-match. The
    qwen dialect carries a mild repeat_penalty to break it; others omit it."""
    assert DIALECTS["qwen"].repeat_penalty == 1.1
    for f in ("gemma4", "holo", "venus", "guiowl", "maiui", "evocua",
              "uitars", "locateanything"):
        assert DIALECTS[f].repeat_penalty is None


# --- UI-TARS-1.5 (Qwen2.5-VL, resized-image pixel coordinates) ---------------

def test_uitars_is_the_only_pixel_space_family():
    assert DIALECTS["uitars"].coord_space == "qwen25_pixels"
    for f, d in DIALECTS.items():
        if f != "uitars":
            assert d.coord_space == "norm1000"


def test_qwen25_smart_resize_matches_llama_cpp():
    """Mirrors llama.cpp's calc_size_preserved_ratio with the client's
    1024..2560-token bounds and 1792px long-side cap."""
    from conductor_desktop.grounder import qwen25_smart_resize
    assert qwen25_smart_resize(1280, 800) == (1288, 812)     # round, in bounds
    assert qwen25_smart_resize(2560, 1600) == (1792, 1120)   # floor-scaled to cap
    assert qwen25_smart_resize(640, 400) == (1148, 728)      # ceil-scaled to floor
    # Wide-but-small images still get the long-side cap (UI-TARS x-drift).
    w, h = qwen25_smart_resize(2072, 812)
    assert w <= 1792 and w * h <= 2560 * 784
    w, h = qwen25_smart_resize(3840, 2160)
    assert w <= 1792 and w * h <= 2560 * 784
    # Aligned in-bounds sizes are fixed points → the server won't resize again.
    for w, h in [(1792, 1120), (1148, 728), (1288, 812), (1288, 1288),
                 qwen25_smart_resize(3840, 2160), qwen25_smart_resize(1080, 2340)]:
        assert qwen25_smart_resize(w, h) == (w, h)
        assert w % 28 == 0 and h % 28 == 0


def test_parse_uitars_shapes_normalize_to_norm1000():
    sent = ParseContext(1932, 1092, CoordSpace.QWEN25_PIXELS)
    assert parse("uitars", "(966,546)", sent)[0].center == (500, 500)
    wrapped = "click(start_box='<|box_start|>(193,1037)<|box_end|>')"
    assert parse("uitars", wrapped, sent)[0].center == (100, 950)
    assert parse("uitars", "x=966 y=546", sent)[0].center == (500, 500)
    assert parse("uitars", "no point", sent) is None
    # Native no-match phrasing → clean [] (not None, which logs a warning).
    assert parse("uitars", "There are none.", sent) == []


def test_only_uitars_omits_system_message():
    assert DIALECTS["uitars"].send_system is False
    for f, d in DIALECTS.items():
        if f != "uitars":
            assert d.send_system is True


def test_uitars_ground_resizes_and_projects_to_norm1000():
    """ground() must send the smart_resized image and normalize the model's
    pixel answer against THAT size, not the original capture."""
    import asyncio
    import base64
    import io

    from PIL import Image

    from conductor_desktop.grounder import Grounder

    buf = io.BytesIO()
    Image.new("RGB", (2560, 1600), "white").save(buf, format="PNG")
    shot = base64.b64encode(buf.getvalue()).decode()

    g = Grounder("http://x", family="uitars")
    seen = {}

    async def fake_post(payload):
        seen["roles"] = [m["role"] for m in payload["messages"]]
        user = payload["messages"][-1]
        url = user["content"][0]["image_url"]["url"]
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as im:
            seen["size"] = im.size
        seen["text"] = user["content"][1]["text"]
        return {"choices": [{"message": {"content": "(896,560)"}}]}

    g._post_chat = fake_post
    els = asyncio.run(g.ground(shot, "the OK button"))
    asyncio.run(g.close())
    assert seen["size"] == (1792, 1120)
    assert seen["roles"] == ["user"]          # no system message, not even empty
    assert seen["text"].startswith("Query:the OK button\n")
    assert len(els) == 1 and els[0].center == (500, 500)


# --- Describe-only families (minicpm) — the mirror of grounding-only ------

def test_minicpm_is_describe_only():
    from conductor_desktop.grounder import family_can_describe, family_can_ground
    assert family_can_describe("minicpm") is True
    assert family_can_ground("minicpm") is False
    assert "minicpm" in DESCRIBE_DIALECTS
    assert "minicpm" not in DIALECTS          # no grounding dialect


def test_family_can_ground_matches_dialect_table():
    from conductor_desktop.grounder import family_can_ground
    for f in DIALECTS:
        assert family_can_ground(f) is True
    assert family_can_ground("minicpm") is False
    assert family_can_ground("nope") is False


def test_describe_only_grounder_constructs_but_ground_raises():
    """A describe-only family builds a Grounder (describe works) but ground()
    fails fast with a clear message instead of a confusing parse error."""
    import asyncio

    import pytest

    from conductor_desktop.grounder import Grounder, GrounderError
    g = Grounder("http://x", family="minicpm")
    assert g._dialect is None and g._describe is not None
    with pytest.raises(GrounderError, match="describe-only"):
        asyncio.run(g.ground("img", "target"))


def test_unknown_family_still_rejected():
    import pytest

    from conductor_desktop.grounder import Grounder
    with pytest.raises(ValueError, match="unknown grounder family"):
        Grounder("http://x", family="not-a-family")


# --- Image-mode describe dialects ----------------------------------------

def test_image_describe_prompts_avoid_desktop_framing():
    """Image-mode prompts must NOT prime the VLM with desktop concepts —
    that's the whole point of the separate dialect. If a webcam frame
    gets described as having "focused windows" or "dialogs" we've
    regressed back to the screen prompt."""
    forbidden = ("focused window", "dialog", "desktop", "overlay", "menu bar")
    for fam, pair in DESCRIBE_DIALECTS.items():
        lower = pair.image.system_prompt.lower()
        for word in forbidden:
            assert word not in lower, (
                f"{fam} image describe prompt leaks desktop framing: {word!r}"
            )


def test_image_describe_user_with_hint_has_placeholder():
    for fam, pair in DESCRIBE_DIALECTS.items():
        assert "{hint}" in pair.image.user_with_hint, (
            f"{fam} image describe user_with_hint missing {{hint}}"
        )


# --- Grounding-only family rejects describe -------------------------------

def test_locateanything_grounder_constructs_without_describe_dialect():
    """A grounding-only family has no describe dialect but must still build."""
    from conductor_desktop.grounder import Grounder

    g = Grounder("http://localhost:9999", family="locateanything")
    assert g._describe is None


def test_locateanything_describe_raises_grounder_error():
    import asyncio

    import pytest

    from conductor_desktop.grounder import Grounder, GrounderError

    g = Grounder("http://localhost:9999", family="locateanything")
    with pytest.raises(GrounderError, match="grounding-only"):
        asyncio.run(g.describe("Zm9v"))
    with pytest.raises(GrounderError, match="grounding-only"):
        asyncio.run(g.describe_image("Zm9v"))


def test_grounder_url_ending_in_v1_is_the_same_server():
    """An OpenAI-style base URL (…/v1) must not become …/v1/v1/chat/completions."""
    from conductor_desktop.grounder import Grounder

    assert Grounder("http://localhost:8080/v1/")._url == "http://localhost:8080"
    assert Grounder("http://localhost:8080")._url == "http://localhost:8080"


# --- File-loader helper (image-mode `describe`) --------------------------

def test_load_image_file_sniffs_png_and_jpeg(tmp_path):
    from conductor_desktop.tools.observation import _load_image_file

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 32

    png = tmp_path / "frame.png"
    jpg = tmp_path / "frame.jpg"
    png.write_bytes(png_bytes)
    jpg.write_bytes(jpg_bytes)

    b64, mime = _load_image_file(str(png))
    assert mime == "image/png"
    assert b64  # non-empty base64 round-trips

    b64, mime = _load_image_file(str(jpg))
    assert mime == "image/jpeg"
    assert b64


def test_load_image_file_rejects_unknown_format(tmp_path):
    import pytest

    from conductor_desktop.tools.observation import _load_image_file

    p = tmp_path / "not-an-image.txt"
    p.write_bytes(b"plain text, no magic bytes here")
    with pytest.raises(ValueError, match="unsupported image format"):
        _load_image_file(str(p))


def test_load_image_file_missing_path_raises(tmp_path):
    import pytest

    from conductor_desktop.tools.observation import _load_image_file

    with pytest.raises(FileNotFoundError):
        _load_image_file(str(tmp_path / "nope.png"))


# --- projection -------------------------------------------------------------

def test_bbox_to_px_projects_norm1000():
    # 2000x1000 screen, 1000x500 grounder image.
    assert bbox_to_px((100, 200, 300, 400), 2000, 1000, 1000, 500, "norm1000") == (200, 200, 600, 400)


def test_bbox_to_px_projects_image_pixels():
    # 2560x1600 screen, 1280x800 image: twice the size.
    assert bbox_to_px((900, 300, 1100, 400), 2560, 1600, 1280, 800,
                      CoordSpace.IMAGE_PIXELS) == (1800, 600, 2200, 800)


def test_bbox_to_px_never_guesses_the_space():
    # The old projection read a coordinate past 1000 as pixels. Grounders now
    # always answer in norm1000, so the space is whatever the caller says.
    assert bbox_to_px((500, 300, 1100, 400), 1000, 1000, 1280, 800,
                      CoordSpace.NORM1000) == (500, 300, 1100, 400)


def test_bbox_to_px_needs_the_image_size_for_pixels():
    import pytest
    with pytest.raises(ValueError, match="can't project"):
        bbox_to_px((1, 2, 3, 4), 100, 100, 0, 0, CoordSpace.IMAGE_PIXELS)


def test_bbox_to_px_orders_corners():
    assert bbox_to_px((300, 400, 100, 200), 1000, 1000, 1000, 1000, "norm1000") == (100, 200, 300, 400)



# --- Reasoning blocks and JSON extraction ------------------------------------

def test_reasoning_is_stripped_before_parsing():
    # A server that doesn't split reasoning out leaves it in the content, and
    # the reasoning can quote JSON or coordinates of its own.
    text = ('<think>maybe {"detected": [{"bbox_2d": [1, 1, 9, 9]}]}?</think>'
            '{"detected": [{"bbox_2d": [10, 20, 100, 200], "description": "ok"}]}')
    assert parse("qwen", text) == [GroundedElement(bbox=(10, 20, 100, 200), description="ok")]


def test_holo_not_found_in_the_reasoning_is_not_a_no_match():
    text = 'is it not_found? no, it is there</think>{"action": "click_absolute", "x": 40, "y": 60}'
    assert parse("holo", text)[0].center == (40, 60)


def test_brackets_in_prose_before_the_json():
    # The old greedy `\[.*\]` grabbed from the first `[` to the last `]`.
    text = 'Found [2] matches: {"detected": [{"bbox_2d": [1, 2, 30, 40], "description": "a"}]}'
    assert parse("qwen", text)[0].bbox == (1, 2, 30, 40)


def test_qwen_bare_box_under_detected():
    # Qwen3-VL-4B's most common miss on the hard set.
    assert parse("qwen", '{"detected": [10, 20, 100, 200]}')[0].bbox == (10, 20, 100, 200)


def test_qwen_bare_box_with_a_stray_bracket():
    assert parse("qwen", '{"detected": [308, 83, 325, 104]]}')[0].bbox == (308, 83, 325, 104)


def test_qwen_nested_detected_is_flattened():
    text = '{"detected": [{"detected": [{"bbox_2d": [916, 384, 946, 417], "description": "x"}]}]}'
    assert parse("qwen", text) == [GroundedElement(bbox=(916, 384, 946, 417), description="x")]


def test_qwen_list_of_bare_boxes():
    out = parse("qwen", '{"detected": [[10, 20, 100, 200], [300, 20, 400, 200]]}')
    assert [e.bbox for e in out] == [(10, 20, 100, 200), (300, 20, 400, 200)]


def test_qwen_single_match_object_without_a_list():
    out = parse("qwen", '{"bbox_2d": [10, 20, 100, 200], "description": "x"}')
    assert out == [GroundedElement(bbox=(10, 20, 100, 200), description="x")]


def test_four_numbers_in_prose_are_not_a_box():
    assert parse("qwen", "Columns [1, 2, 3, 4] are visible.") is None


def test_list_cut_off_at_max_tokens_keeps_complete_boxes():
    # Qwen3.8-27B listing eight instances ran out at max_tokens 320.
    text = ('{"detected": [{"bbox_2d": [10, 20, 100, 200], "description": "a"}, '
            '{"bbox_2d": [110, 20, 200, 200], "description": "b"}, '
            '{"bbox_2d": [210, 20, 3')
    out = parse("qwen", text)
    assert out == [GroundedElement(bbox=(10, 20, 100, 200), description="a"),
                   GroundedElement(bbox=(110, 20, 200, 200), description="b")]


def test_missing_comma_or_quote_still_yields_the_boxes():
    # Swift-Qwen3.8's two hard-set misses: right boxes, broken JSON.
    no_comma = ('{"detected": [{"bbox_2d": [10, 20, 100, 200] "description": "a"}, '
                '{"bbox_2d": [110, 20, 200, 200], "description": "b"}]}')
    assert [e.bbox for e in parse("qwen", no_comma)] == [(10, 20, 100, 200), (110, 20, 200, 200)]
    no_quote = '{"detected": [{"bbox_2d": [10, 20, 100, 200], "description: "a"}]}'
    assert parse("qwen", no_quote)[0].bbox == (10, 20, 100, 200)


def test_salvaged_gemma_box_is_still_permuted():
    text = '[{"box_2d": [20, 10, 200, 100] "label": "x"}]'
    assert parse("gemma4", text) == [GroundedElement(bbox=(10, 20, 100, 200), description="x")]


def test_venus_takes_the_point_not_any_two_integers():
    assert parse("venus", "step 1 of 2: [932, 86]")[0].center == (932, 86)


# --- Coordinate space override ------------------------------------------------

def _png(w, h):
    import base64
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_image_pixels_override_converts_to_norm1000():
    import asyncio

    from conductor_desktop.grounder import Grounder
    g = Grounder("http://x", family="qwen", coord_space=CoordSpace.IMAGE_PIXELS)

    async def fake_post(payload):
        return {"choices": [{"message": {"content":
                '{"detected": [{"bbox_2d": [200, 100, 400, 300], "description": "b"}]}'}}]}

    g._post_chat = fake_post
    els = asyncio.run(g.ground(_png(2000, 1000), "the button"))
    asyncio.run(g.close())
    assert els[0].bbox == (100, 100, 200, 300)


def test_coord_override_on_a_point_family():
    import asyncio

    import pytest

    from conductor_desktop.grounder import Grounder
    # norm1000 is what the point dialects return anyway: harmless.
    asyncio.run(Grounder("http://x", family="holo", coord_space=CoordSpace.NORM1000).close())
    with pytest.raises(ValueError, match="fixes its own coordinate space"):
        Grounder("http://x", family="uitars", coord_space=CoordSpace.IMAGE_PIXELS)


def test_config_validates_the_coord_system(monkeypatch):
    import pytest

    from conductor_desktop.config import MCPConfig
    monkeypatch.delenv("CONDUCTOR_DESKTOP_COORD_SYSTEM", raising=False)
    assert MCPConfig.from_env().coord_system is None
    monkeypatch.setenv("CONDUCTOR_DESKTOP_COORD_SYSTEM", "Image_Pixels")
    assert MCPConfig.from_env().coord_system is CoordSpace.IMAGE_PIXELS
    monkeypatch.setenv("CONDUCTOR_DESKTOP_COORD_SYSTEM", "pixels")
    with pytest.raises(RuntimeError, match="not a coordinate space"):
        MCPConfig.from_env()


def test_project_bbox_gives_the_box_and_its_region():
    from conductor_desktop.runtime.grounding import project_bbox

    # norm1000 box, bottom-right of a 1000x1000 screen.
    assert project_bbox((800, 800, 900, 900), to=(1000, 1000), frm=(0, 0),
                        space=CoordSpace.NORM1000) == ((800, 800, 900, 900), "bottom-right")
    # image-pixel box on a 2x screen.
    assert project_bbox((100, 100, 200, 200), to=(2000, 2000), frm=(1000, 1000),
                        space=CoordSpace.IMAGE_PIXELS) == ((200, 200, 400, 400), "top-left")
