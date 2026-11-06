"""The Verdict Row Public Changelog feed (Batch 2 #8): Atom + JSON
twin generated VM-side by the fleet's changelog_feed.py from live
stability manifests, checker history and build-log backfill, committed
at app/data/changelog.{atom,json} and served verbatim at
/changelog.atom and /changelog.json. Docs surfaces only: no priced
route is added and /health's route count must not move."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import load_settings
from app.discovery import llms_txt, skill_md

BASE = "https://stall-row.example"
ATOM = "https://stall-row-production.up.railway.app/changelog.atom"
JSON_URL = "https://stall-row-production.up.railway.app/changelog.json"
DATA = Path(__file__).resolve().parent.parent / "app" / "data"
NS = {"a": "http://www.w3.org/2005/Atom"}


def test_changelog_files_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/changelog.atom")
        assert resp.status_code == 200
        assert resp.content == (DATA / "changelog.atom").read_bytes()
        assert "application/atom+xml" in resp.headers["content-type"]
        resp = client.get("/changelog.json")
        assert resp.status_code == 200
        assert resp.content == (DATA / "changelog.json").read_bytes()


def test_changelog_json_shape_sorted_unique():
    ds = json.loads((DATA / "changelog.json").read_text())
    assert ds["kind"] == "verdict-row-changelog/v1"
    entries = ds["entries"]
    assert ds["totals"]["entries"] == len(entries) > 0
    ats = [e["at"] for e in entries]
    assert ats == sorted(ats, reverse=True)
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    for e in entries:
        assert e["id"].startswith("urn:verdict-row:changelog:")
        assert e["kind"] in (
            "route_added", "price_changed", "manifest_changed",
            "route_removed", "service_version",
        )
        assert e["summary"] and e["stall"] and e["source"]


def test_changelog_atom_wellformed_required_elements():
    root = ET.parse(DATA / "changelog.atom").getroot()
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    assert root.findtext("a:title", namespaces=NS)
    assert root.findtext("a:id", namespaces=NS) == "urn:verdict-row:changelog"
    assert root.findtext("a:updated", namespaces=NS)
    self_link = root.find("a:link[@rel='self']", NS)
    assert self_link is not None and self_link.get("href") == ATOM
    entries = root.findall("a:entry", NS)
    ds = json.loads((DATA / "changelog.json").read_text())
    assert len(entries) == len(ds["entries"])
    for entry in entries:
        assert entry.findtext("a:id", namespaces=NS)
        assert entry.findtext("a:title", namespaces=NS)
        assert entry.findtext("a:updated", namespaces=NS)
        assert entry.findtext("a:published", namespaces=NS)


def test_changelog_linked_from_skill_llms_and_tools():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert ATOM in doc
        assert JSON_URL in doc
    tools_html = (DATA / "tools-index.html").read_text()
    assert ATOM in tools_html and JSON_URL in tools_html


def test_changelog_does_not_move_health_route_count():
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["routes"] == 37
