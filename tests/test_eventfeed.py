"""Market event feeds (Next-20 #8 Drift Event Feed + #14 New-Stall
Firehose): one feed module, two topics — hash-chained feed-v1
envelopes, cursor paging, the free-first-page / per-event Fleet
Pass funding split at Recheck arithmetic ($0.005/event), and the
Nov 1 agents.tuski.ai payTo-drift records riding the drift feed
from the real committed patrol snapshot. Offline: crafted Index
snapshots + fixture patrol docs, plus one read of the committed
snapshot for the tuski assertion."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import eventfeed, fleetpass, indexfeed, patrolsnap, recheck

P1 = "0x1111111111111111111111111111111111111111"
P2 = "0x2222222222222222222222222222222222222222"
R1 = "https://a.example.com/v1/one"
R2 = "https://b.example.com/v1/two"


def _row(resource, pay_to, amount="10000", desc="d"):
    return {
        "resource": resource,
        "serviceName": "svc",
        "description": desc,
        "amount": amount,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "eip155:8453",
        "payTo": pay_to,
        "lastUpdated": None,
        "calls30d": 10,
        "payers30d": 2,
    }


def _index(tmp_path):
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    store.save_snapshot([_row(R1, P1)], epoch=1_000_000)
    # R1 rotates payTo + changes price + drifts description; R2 appears.
    store.save_snapshot(
        [_row(R1, P2, amount="20000", desc="d2"), _row(R2, P1)], epoch=2_000_000
    )
    return store


PATROL = {
    "kind": "verdict-row-patrol-snapshot/v1",
    "generated_at": "2026-11-02T00:00:00Z",
    "snapshot_id": "fixture-1",
    "source": {},
    "routes": [
        {
            "url": "https://agents.tuski.ai/api/v1/email-validation/jobs",
            "origin": "https://agents.tuski.ai",
            "status": "open",
            "defect_class": "payto_mismatch",
            "first_seen": "2026-10-29T07:05:43Z",
            "last_seen": "2026-10-31T16:05:09Z",
        },
        {
            "url": "https://new.example.com/v1/x",
            "origin": "https://new.example.com",
            "status": "open",
            "defect_class": "amount_mismatch",
            "first_seen": "2026-11-01T00:00:00Z",
            "last_seen": "2026-11-01T01:00:00Z",
        },
    ],
}


def test_drift_events_from_index_and_patrol(tmp_path):
    events = eventfeed.build_events("drift", _index(tmp_path), PATROL)
    kinds = {(e["event"]["resource"], e["event"]["kind"]) for e in events}
    assert (R1, "payto_rotation") in kinds
    assert (R1, "amount_changed") in kinds
    assert (R1, "description_drift") in kinds
    tuski = [e for e in events if "tuski" in (e["event"]["resource"] or "")]
    assert len(tuski) == 1
    assert tuski[0]["event"]["kind"] == "payto_drift"
    assert tuski[0]["event"]["source"] == "patrol"
    # No firehose-only event leaks into drift.
    assert all(e["event"]["topic"] == "drift" for e in events)


def test_firehose_first_seen_and_new_origins(tmp_path):
    events = eventfeed.build_events("firehose", _index(tmp_path), PATROL)
    evs = [e["event"] for e in events]
    first_seen = [e for e in evs if e["kind"] == "first_seen"]
    assert {e["resource"] for e in first_seen} == {R2}
    origins = {e["origin"] for e in evs if e["kind"] == "new_origin"}
    assert origins == {"https://agents.tuski.ai", "https://new.example.com"}
    ats = [e["at"] for e in evs]
    assert ats == sorted(ats)  # chronological


def test_chain_and_cursor_paging(tmp_path):
    index = _index(tmp_path)
    page1 = eventfeed.feed_page("drift", index_store=index, patrol_snapshot=PATROL, since_seq=0, limit=2)
    assert page1["events_returned"] == 2
    assert [e["seq"] for e in page1["events"]] == [1, 2]
    # Hash chain verifies end to end.
    prev = "genesis"
    for env in eventfeed.build_events("drift", index, PATROL):
        assert env["prev_hash"] == prev
        expect = hashlib.sha256(
            f"{prev}\n{json.dumps(env['event'], sort_keys=True, separators=(',', ':'))}".encode()
        ).hexdigest()
        assert env["record_hash"] == expect
        prev = env["record_hash"]
    # Cursor advances without dupes or gaps.
    seen = []
    cursor = 0
    while True:
        page = eventfeed.feed_page(
            "drift", index_store=index, patrol_snapshot=PATROL,
            since_seq=cursor, limit=2,
        )
        if not page["events"]:
            break
        seen += [e["seq"] for e in page["events"]]
        cursor = page["events"][-1]["seq"]
    assert seen == list(range(1, page1["total_events"] + 1))


def test_tuski_records_in_real_patrol_snapshot():
    snap = patrolsnap.load_snapshot()
    assert snap is not None
    events = eventfeed.build_events("drift", None, snap)
    tuski = [e["event"] for e in events if "tuski" in (e["event"]["resource"] or "")]
    assert len(tuski) == 2
    assert {e["defect_class"] for e in tuski} == {"payto_mismatch"}
    # The worked example in the discovery/sample material matches
    # the real records and is fixture-marked.
    for ex in eventfeed.TUSKI_WORKED_EXAMPLE:
        assert ex["fixture"] is True
        assert any(
            e["resource"] == ex["resource"] and e["first_seen"] == ex["first_seen"]
            for e in tuski
        )


def test_tuski_example_in_discovery_and_sample_material():
    from app.discovery import ROUTES, llms_txt, skill_md
    from app.config import load_settings

    example = json.dumps(ROUTES["/v1/index/history"]["output_example"])
    assert "agents.tuski.ai" in example
    assert "fixture" in ROUTES["/v1/index/history"]["output_example"][
        "worked_example_note"
    ].lower()
    settings = load_settings()
    for doc in (skill_md(settings, "https://x.example"), llms_txt(settings, "https://x.example")):
        assert "agents.tuski.ai" in doc
        assert "/v1/drift/feed" in doc
        assert "/v1/firehose/feed" in doc


def _http_fixture(tmp_path, monkeypatch, n_new=0):
    """HTTP fixture: an index store on the SAME db as the watch
    store (production topology), with enough patrol-shaped drift
    to exceed the free page when n_new > 0."""
    db = str(tmp_path / "watches.db")
    watch = recheck.WatchStore(db)
    index = indexfeed.IndexStore(db)
    index.save_snapshot([_row(R1, P1)], epoch=1_000_000)
    rows = [_row(R1, P2)]
    for i in range(n_new):
        rows.append(_row(f"https://n{i}.example.com/v1/x", P1))
    index.save_snapshot(rows, epoch=2_000_000)
    monkeypatch.setattr(main, "_recheck_store", watch)
    monkeypatch.setattr(main, "_index_store", index)
    monkeypatch.setattr(eventfeed, "load_patrol", lambda: None)
    return watch


def test_feed_http_free_first_page(tmp_path, monkeypatch):
    _http_fixture(tmp_path, monkeypatch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        r = client.get("/v1/drift/feed")
        assert r.status_code == 200
        body = r.json()
        assert body["feed"] == "drift" and body["version"] == 1
        assert body["events"] and body["events"][0]["seq"] == 1
        r2 = client.get("/v1/firehose/feed")
        assert r2.status_code == 200
        assert r2.json()["feed"] == "firehose"
        bad = client.get("/v1/drift/feed?since_seq=abc")
        assert bad.status_code == 422


def test_feed_http_beyond_free_page_needs_pass_and_debits_per_event(
    tmp_path, monkeypatch
):
    # FREE_EVENTS is 100; build 130 firehose first-seen events.
    watch = _http_fixture(tmp_path, monkeypatch, n_new=130)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        page = client.get("/v1/firehose/feed?limit=500")
        assert page.status_code == 200
        body = page.json()
        assert body["events_returned"] == eventfeed.FREE_EVENTS
        assert body["funding_required"] is True
        assert body["events_held_back"] == body["total_events"] - eventfeed.FREE_EVENTS
        # With a pass: full page, debited per non-free event at $0.005.
        fleet_pass, _ = watch.mint_pass(
            "pay-feed", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
        )
        paid = client.get(
            "/v1/firehose/feed?limit=500",
            headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
        )
        assert paid.status_code == 200
        body = paid.json()
        assert body["events_returned"] == body["total_events"]
        charged = body["funding_applied"]["events_charged"]
        assert charged == body["total_events"] - eventfeed.FREE_EVENTS
        assert body["funding_applied"]["debited_units"] == charged * 5_000
        after = watch.get_pass(fleet_pass["pass_id"])
        assert after["balance_units"] == (
            fleetpass.PASS_PRICE_UNITS - charged * 5_000
        )
