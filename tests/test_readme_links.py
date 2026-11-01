"""README fleet links (Item 10): the repo README names the Inspector
Quickstart Pack pages (hosted on StallRow), this stall's MCP server
(repo + server.json manifest), the fleet tools index, and this
stall's discovery bundle / A2A card / llms-full.txt — the same URLs
the discovery surfaces carry (see test_quickstart_link.py,
test_mcp_link.py, test_tools_index_link.py, test_verdict_row_link.py,
test_llms_full_link.py). Offline: reads README.md only."""

from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()

ORIGIN = "https://stall-row-production.up.railway.app"
CURL = "https://stall-row-production.up.railway.app/quickstart/curl"
PYTHON = "https://stall-row-production.up.railway.app/quickstart/python"
TYPESCRIPT = "https://stall-row-production.up.railway.app/quickstart/typescript"
TOOLS = "https://stall-row-production.up.railway.app/tools"
TOOLS_JSON = "https://stall-row-production.up.railway.app/tools.json"
MCP_REPO = "https://github.com/QuincyGB/stall-row-mcp"
MCP_MANIFEST = "https://github.com/QuincyGB/stall-row-mcp/blob/main/server.json"


def test_readme_links_quickstart_pack():
    assert CURL in README
    assert PYTHON in README
    assert TYPESCRIPT in README


def test_readme_links_mcp_server():
    assert MCP_REPO in README
    assert MCP_MANIFEST in README


def test_readme_links_tools_index():
    assert TOOLS in README
    assert TOOLS_JSON in README


def test_readme_links_bundle_a2a_and_llms_full():
    assert ORIGIN + "/.well-known/verdict-row.json" in README
    assert ORIGIN + "/.well-known/a2a-agent-card.json" in README
    assert ORIGIN + "/llms-full.txt" in README
