"""Text with UTF-16 surrogates must not take the server down.

Windows 11 Notepad's accessible text arrived with half of an emoji's
surrogate pair; the reply couldn't be encoded as UTF-8 and the MCP server
crashed mid-run.
"""
from mcp.types import CallToolResult, ImageContent, TextContent

from conductor_desktop.tooling import _without_surrogates


def _text(s: str) -> str:
    [c] = _without_surrogates([TextContent(type="text", text=s)])
    return c.text


def test_a_split_pair_is_joined_back_into_its_character():
    assert _text("party 🎉 time") == "party 🎉 time"


def test_a_lone_half_becomes_the_replacement_character():
    assert _text("cut at \ud83c") == "cut at �"
    assert _text("\udf89 orphan low half") == "� orphan low half"


def test_clean_text_and_images_pass_through_untouched():
    text = TextContent(type="text", text="ünïcødé 測試 🎉")
    image = ImageContent(type="image", data="AAAA", mimeType="image/png")
    assert _without_surrogates([text, image]) == [text, image]


def test_the_reply_serialises():
    content = _without_surrogates([TextContent(type="text", text="name=\"🎉 \ud83c\"")])
    CallToolResult(content=content).model_dump_json(by_alias=True, exclude_none=True)
