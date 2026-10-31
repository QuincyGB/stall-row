"""The Verdict Row Tools Index: hosted on StallRow at /tools (HTML)
and /tools.json (machine-readable), generated VM-side from the five
stalls' live discovery docs and committed at app/data/tools-index.*;
the endpoints must serve those exact bytes, and skill.md / llms.txt
must link them."""

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import load_settings
from app.discovery import llms_txt, skill_md

BASE = "https://stall-row.example"
TOOLS = "https://stall-row-production.up.railway.app/tools"
TOOLS_JSON = "https://stall-row-production.up.railway.app/tools.json"
DATA = Path(__file__).resolve().parent.parent / "app" / "data"


def test_tools_index_pages_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/tools")
        assert resp.status_code == 200
        assert resp.content == (DATA / "tools-index.html").read_bytes()
        resp = client.get("/tools.json")
        assert resp.status_code == 200
        assert resp.content == (DATA / "tools-index.json").read_bytes()


def test_tools_index_link_in_skill_and_llms():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert TOOLS in doc
        assert TOOLS_JSON in doc
