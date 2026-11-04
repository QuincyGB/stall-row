"""The Verdict Row Fleet Status page (Batch 2 #10): HTML + JSON twin
generated VM-side by the fleet's fleet_status.py from the standing
checker's own state, committed at app/data/fleet-status.{html,json}
and served verbatim at /status and /status.json. Docs surfaces only:
no priced route is added and /health's route count must not move.
The page must label its freshness honestly — generated_at, the
checker's last-run time, and the 1-run window the shares cover."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import load_settings
from app.discovery import llms_txt, skill_md

BASE = "https://stall-row.example"
STATUS = "https://stall-row-production.up.railway.app/status"
STATUS_JSON = "https://stall-row-production.up.railway.app/status.json"
DATA = Path(__file__).resolve().parent.parent / "app" / "data"
STALLS = ("token-verdict", "route-verdict", "list-smith", "stall-row", "lock-smith")


def test_fleet_status_files_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.content == (DATA / "fleet-status.html").read_bytes()
        assert "text/html" in resp.headers["content-type"]
        resp = client.get("/status.json")
        assert resp.status_code == 200
        assert resp.content == (DATA / "fleet-status.json").read_bytes()


def test_fleet_status_json_shape_and_totals():
    ds = json.loads((DATA / "fleet-status.json").read_text())
    assert ds["kind"] == "verdict-row-fleet-status/v1"
    assert ds["generated_at"] and ds["checker_last_run"]
    assert ds["window"]["runs_considered"] == 1
    routes = ds["routes"]
    assert ds["totals"]["routes_total"] == len(routes) == 40
    assert ds["totals"]["routes_ok"] == sum(1 for r in routes if r["status"] == "ok")
    assert ds["totals"]["services_total"] == 5
    assert {s["stall"] for s in ds["stalls"]} == set(STALLS)
    for s in ds["stalls"]:
        assert s["routes_total"] == len(s["routes"])
        assert s["routes_ok"] == sum(1 for r in s["routes"] if r["status"] == "ok")
    for r in routes:
        assert r["stall"] in STALLS
        assert r["route"].startswith("/v1/")
        assert r["price"]
        assert r["status"] in ("ok", "failing")
        assert r["last_checked_at"] == ds["checker_last_run"]
        assert r["runs_considered"] == 1
        assert r["ok_share"] in (0.0, 1.0)


def test_fleet_status_html_summary_stalls_and_freshness():
    ds = json.loads((DATA / "fleet-status.json").read_text())
    page = (DATA / "fleet-status.html").read_text()
    t = ds["totals"]
    assert f"{t['routes_ok']}/{t['routes_total']} routes OK" in page
    assert f"checked {ds['checker_last_run']}" in page
    assert ds["generated_at"] in page
    assert "not a live monitor" in page
    assert "no multi-run checker history exists" in page
    for s in ds["stalls"]:
        assert s["name"] in page
        for r in s["routes"]:
            assert r["route"] in page


def test_fleet_status_linked_from_skill_llms_and_tools():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert STATUS in doc
        assert STATUS_JSON in doc
    tools_html = (DATA / "tools-index.html").read_text()
    assert STATUS in tools_html and STATUS_JSON in tools_html


def test_fleet_status_does_not_move_health_route_count():
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["routes"] == 34
