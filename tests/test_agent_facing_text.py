"""What an agent reads about this server, held to what clients actually show.

Claude Desktop drops the server's ``instructions`` and cuts each tool's
description at about 500 characters. So every description's first paragraph
has to stand on its own within that, and parameter detail goes in the input
schema instead of the prose.
"""

from __future__ import annotations

import asyncio

import pytest

from conductor_desktop.config import MCPConfig
from conductor_desktop.instructions import INSTRUCTIONS
from conductor_desktop.server import build_mcp

FIRST_PARAGRAPH_MAX = 500
# Claude Code shows the first 2048 characters and marks the rest truncated.
INSTRUCTIONS_MAX = 2048


def _every_tool():
    config = MCPConfig(grounder_url="", omniparser_url="http://omniparser",
                       mobile_android_enabled=True, describe_image_enabled=True)
    mcp = build_mcp(config)
    return mcp, asyncio.run(mcp.list_tools())


MCP, TOOLS = _every_tool()


def test_every_optional_family_is_in_the_sample():
    names = {t.name for t in TOOLS}
    assert {"enumerate_elements", "mobile_tap", "mobile_shell",
            "describe_image", "web_eval", "exec_shell"} <= names


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_the_first_paragraph_fits_a_truncating_client(tool):
    first = tool.description.split("\n\n", 1)[0]
    assert len(first) <= FIRST_PARAGRAPH_MAX, (
        f"{tool.name}: first paragraph is {len(first)} chars; a client that "
        f"cuts at {FIRST_PARAGRAPH_MAX} loses the rest:\n{first[FIRST_PARAGRAPH_MAX:]}")


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_descriptions_are_dedented(tool):
    assert "\n    " not in tool.description.split("\n\n", 1)[0]


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_parameter_is_described_in_the_schema(tool):
    props = tool.inputSchema.get("properties", {})
    missing = [p for p, spec in props.items() if not spec.get("description")]
    assert not missing, f"{tool.name}: no schema description for {missing}"


def test_instructions_are_sent_and_short():
    assert MCP.instructions == INSTRUCTIONS
    assert len(INSTRUCTIONS) <= INSTRUCTIONS_MAX


def test_instructions_only_name_tools_that_exist():
    import re
    names = {t.name for t in TOOLS}
    mentioned = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", INSTRUCTIONS))
    unknown = {m for m in mentioned if m not in names
               and m not in {"device_id", "image_coords"}}
    assert not unknown, f"instructions name tools that don't exist: {unknown}"
