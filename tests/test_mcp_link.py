"""The MCP manifest link: every discovery surface names the stall's
MCP server honestly — the stdio wrapper's GitHub repo and its
server.json manifest (the server is source-only, run by the caller's
own MCP host; no hosted MCP endpoint is claimed).
"""

from app.config import load_settings
from app.discovery import MCP_SERVER, agent_card, llms_txt, skill_md, x402_manifest

BASE = "https://stall-row.example"
REPO = "https://github.com/QuincyGB/stall-row-mcp"
MANIFEST = "https://github.com/QuincyGB/stall-row-mcp/blob/main/server.json"


def test_mcp_server_reference_is_the_real_repo():
    assert MCP_SERVER["repository"] == REPO
    assert MCP_SERVER["manifest"] == MANIFEST
    assert MCP_SERVER["transport"] == "stdio"
    assert MCP_SERVER["name"] == "io.github.QuincyGB/stall-row-mcp"


def test_manifest_and_card_link_the_mcp_server():
    settings = load_settings()
    manifest = x402_manifest(settings, BASE)
    assert manifest["extensions"]["mcp"]["repository"] == REPO
    assert manifest["extensions"]["mcp"]["manifest"] == MANIFEST
    card = agent_card(settings, BASE)
    assert card["mcp"]["repository"] == REPO
    assert card["mcp"]["manifest"] == MANIFEST


def test_skill_and_llms_name_the_mcp_server():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert REPO in doc
        assert MANIFEST in doc
