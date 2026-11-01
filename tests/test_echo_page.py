"""The Verdict Row Catalogue Echo page (Next-20 Item 6): HTML + JSON
twin generated VM-side by the fleet's echo_auditor.py from a fresh
catalogue read against the canonical route registry, committed at
app/data/echo-status.{html,json} and served verbatim at /echo and
/echo.json. Docs surfaces only: no priced route is added and
/health's route count must not move."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import load_settings
from app.discovery import llms_txt, skill_md

BASE = "https://stall-row.example"
ECHO = "https://stall-row-production.up.railway.app/echo"
ECHO_JSON = "https://stall-row-production.up.railway.app/echo.json"
DATA = Path(__file__).resolve().parent.parent / "app" / "data"
SOURCES = ("cdp", "payai", "settled", "x402scan")


def test_echo_files_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/echo")
        assert resp.status_code == 200
        assert resp.content == (DATA / "echo-status.html").read_bytes()
        assert "text/html" in resp.headers["content-type"]
        resp = client.get("/echo.json")
        assert resp.status_code == 200
        assert resp.content == (DATA / "echo-status.json").read_bytes()


def test_echo_json_shape_and_totals():
    ds = json.loads((DATA / "echo-status.json").read_text())
    assert ds["kind"] == "verdict-row-catalogue-echo/v1"
    assert ds["generated_at"] and ds["registry_generated_at"]
    routes = ds["routes"]
    assert len(routes) == 40
    for src in SOURCES:
        t = ds["totals"][src]
        assert t["total"] == 40
        assert (t["echo_ok"] + t["mismatch"] + t["not_listed"]
                + t["unreadable"]) == 40
        assert t["not_listed"] == (t["not_listed_by_design"]
                                   + t["not_listed_unexpected"])
    for r in routes:
        assert set(r["cells"]) == set(SOURCES)
        for c in r["cells"].values():
            assert c["status"] in ("ECHO-OK", "MISMATCH", "NOT-LISTED",
                                   "UNREADABLE")


def test_echo_html_summary_and_linkage():
    ds = json.loads((DATA / "echo-status.json").read_text())
    page = (DATA / "echo-status.html").read_text()
    assert "Catalogue Echo" in page
    assert ds["generated_at"] in page
    for r in ds["routes"]:
        assert r["route"] in page
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert ECHO in doc and ECHO_JSON in doc
    status_html = (DATA / "fleet-status.html").read_text()
    assert ECHO in status_html


def test_echo_does_not_move_health_route_count():
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["routes"] == 33
