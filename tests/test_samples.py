"""Free sample responses (Batch 2 #9): one sample per priced route,
served free at GET /samples and GET /sample/<route-slug> from static
committed data (app/data/samples/ + samples-manifest.json).

Integrity rules pinned here: every sample carries sample=true +
live=false in-band; source is "recorded" (a real recorded response,
recorded_at set) or "schema-example" (shape only, recorded_at null);
recorded samples' fields stay within the route's current discovery
outputSchema properties (documented exceptions below).
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import load_settings
from app.discovery import llms_txt, skill_md

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "app" / "data"
WRAPPER = {"sample", "live", "source", "recorded_at", "target", "route", "sample_note"}
BASE = "https://stall.example"


def slug_of(route: str) -> str:
    return route.removeprefix("/v1/").strip("/").replace("/", "-")


def manifest() -> dict:
    return json.loads((DATA / "samples-manifest.json").read_text())


def load_sample(slug: str) -> dict:
    return json.loads((DATA / "samples" / f"{slug}.json").read_text())

from app.config import PRICE_UNITS
from app.discovery import ROUTES

PRICED_ROUTES = set(ROUTES)
assert PRICED_ROUTES == set(PRICE_UNITS), "test expects ROUTES == priced routes"
SCHEMA_PROPS = {
    path: set((r["output_schema"] or {}).get("properties") or {})
    for path, r in ROUTES.items()
}
# Documented exception: the recorded Recheck response is a watch-mode
# record served by the free Recheck feed; the live route also returns
# these three fields in watch/feed records, but the priced route's
# discovery outputSchema does not list them (schema drift, reported
# with Batch 2 #9 — the sample is the real recorded response, served
# verbatim, so the fields stay).
RECORDED_EXTRA_OK = {
    "/v1/recheck": {"catalogue_crosscheck", "field_changes", "probe"},
}
# Documented exception: none — every schema-example is the route's own
# schema-validated discovery output example. Two routes' examples carry
# a body field named like a wrapper field (agent-card-verify "source",
# score-book "target"); the in-band marker wins and the shadowed value
# is recorded in samples-manifest.json.
SCHEMA_EXAMPLE_EXTRA_OK = {}


def test_every_priced_route_has_a_sample_file():
    entries = manifest()["samples"]
    assert {e["route"] for e in entries} == PRICED_ROUTES
    files = {p.stem for p in (DATA / "samples").glob("*.json")}
    assert files == {e["slug"] for e in entries} == {slug_of(r) for r in PRICED_ROUTES}


def test_samples_carry_the_in_band_marker():
    for e in manifest()["samples"]:
        body = load_sample(e["slug"])
        assert body["sample"] is True
        assert body["live"] is False
        assert body["source"] == e["source"]
        assert body["route"] == e["route"]
        assert body["target"] == e["target"]
        assert body["recorded_at"] == e["recorded_at"]
        if e["source"] == "recorded":
            assert e["recorded_at"], "recorded samples must name when the check ran"
            assert e["pulled_from"]
        else:
            assert e["recorded_at"] is None
            assert "not a recorded result" in body["sample_note"]


def test_recorded_samples_match_current_output_schema():
    for e in manifest()["samples"]:
        if e["source"] != "recorded":
            continue
        body = load_sample(e["slug"])
        fields = set(body) - WRAPPER
        props = set(SCHEMA_PROPS[e["route"]])
        extra = fields - props - RECORDED_EXTRA_OK.get(e["route"], set())
        assert not extra, f"{e['route']}: recorded fields outside schema: {extra}"


def test_schema_examples_match_current_output_schema():
    for e in manifest()["samples"]:
        if e["source"] != "schema-example":
            continue
        body = load_sample(e["slug"])
        fields = set(body) - WRAPPER
        props = set(SCHEMA_PROPS[e["route"]])
        extra = fields - props - SCHEMA_EXAMPLE_EXTRA_OK.get(e["route"], set())
        assert not extra, f"{e['route']}: example fields outside schema: {extra}"


def test_samples_endpoints_serve_the_files():
    with TestClient(main.app) as client:
        idx = client.get("/samples")
        assert idx.status_code == 200
        body = idx.json()
        assert body["sample"] is True and body["live"] is False
        assert body["count"] == len(PRICED_ROUTES)
        assert {s["route"] for s in body["samples"]} == PRICED_ROUTES
        for e in manifest()["samples"]:
            resp = client.get(f"/sample/{e['slug']}")
            assert resp.status_code == 200
            assert resp.json() == load_sample(e["slug"])
        assert client.get("/sample/no-such-route").status_code == 404


def test_samples_linked_from_skill_and_llms():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert f"{BASE}/samples" in doc
        assert "/sample/<route-slug>" in doc
