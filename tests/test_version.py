"""One version number: the package metadata, __version__ and MCP serverInfo agree."""

from importlib.metadata import version

import conductor_desktop
from conductor_desktop.config import MCPConfig
from conductor_desktop.server import build_mcp


def test_metadata_matches_dunder_version():
    assert version("conductor-desktop") == conductor_desktop.__version__


def test_server_info_reports_our_version_not_the_sdks():
    mcp = build_mcp(MCPConfig.from_env())
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_version == conductor_desktop.__version__
