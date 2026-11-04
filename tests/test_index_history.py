"""Verdict Index History API (Next-20 #6): the Index as a time
series across ALL retained snapshots — first-seen, delisted,
relisted, and price/payTo changes — at $0.01 one-off, Fleet-Pass
funded at the same price for standing queries. Offline: crafted
IndexStore snapshots; the HTTP legs run through TestClient with
the stores monkeypatched to tmp files."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.rpc import UpstreamError
from app.services import fleetpass, indexfeed, recheck

P1 = "0x1111111111111111111111111111111111111111"
P2 = "0x2222222222222222222222222222222222222222"
P3 = "0x3333333333333333333333333333333333333333"
P9 = "0x9999999999999999999999999999999999999999"
R1 = "https://a.example.com/v1/one"
R2 = "https://a.example.com/v1/two"
R3 = "https://b.example.com/v1/three"


def _row(resource, pay_to, amount="10000", name="svc"):
    return {
        "resource": resource,
        "serviceName": name,
        "description": "d",
        "amount": amount,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "eip155:8453",
        "payTo": pay_to,
        "lastUpdated": None,
        "calls30d": 10,
        "payers30d": 2,
    }


def _store(tmp_path):
    """Four snapshots exercising every history event kind:
    s1: R1, R2 appear (first_seen x2)
    s2: R1 price changes; R3 appears; R2 delisted
    s3: R1 payTo rotates; R2 relisted; R3 delisted
    s4: no changes"""
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    store.save_snapshot([_row(R1, P1), _row(R2, P2)], epoch=1_000_000)
    store.save_snapshot(
        [_row(R1, P1, amount="20000"), _row(R3, P3)], epoch=2_000_000
    )
    store.save_snapshot(
        [_row(R1, P9, amount="20000"), _row(R2, P2)], epoch=3_000_000
    )
    store.save_snapshot(
        [_row(R1, P9, amount="20000"), _row(R2, P2)], epoch=4_000_000
    )
    return store


def test_history_walks_all_snapshots(tmp_path):
    out = indexfeed.history(_store(tmp_path))
    assert out["snapshots_held"] == 4
    assert out["earliest_snapshot_at"] < out["latest_snapshot_at"]
    assert out["catalogue"] == "cdp_bazaar"
    kinds = [(e["resource"], e["kind"]) for e in out["events"]]
    assert (R1, "first_seen") in kinds
    assert (R2, "delisted") in kinds
    assert (R2, "relisted") in kinds
    assert (R3, "first_seen") in kinds
    assert (R3, "delisted") in kinds
    changed = [e for e in out["events"] if e["kind"] == "field_changed"]
    fields = {(e["resource"], e["field"]) for e in changed}
    assert (R1, "amount") in fields
    assert (R1, "payTo") in fields
    amount_ev = next(e for e in changed if e["field"] == "amount")
    assert amount_ev["before"] == "10000" and amount_ev["after"] == "20000"
    payto_ev = next(e for e in changed if e["field"] == "payTo")
    assert payto_ev["before"] == P1 and payto_ev["after"] == P9
    # Chronological order across the whole series.
    ats = [e["at"] for e in out["events"]]
    assert ats == sorted(ats)


def test_history_filters(tmp_path):
    store = _store(tmp_path)
    only_r1 = indexfeed.history(store, resource=R1)
    assert {e["resource"] for e in only_r1["events"]} == {R1}
    assert [e["kind"] for e in only_r1["events"]] == [
        "first_seen", "field_changed", "field_changed",
    ]
    by_origin = indexfeed.history(store, origin="https://b.example.com")
    assert {e["resource"] for e in by_origin["events"]} == {R3}
    by_pay_to = indexfeed.history(store, pay_to=P9.lower())
    assert by_pay_to["events"]
    assert all(e["resource"] == R1 for e in by_pay_to["events"])
    assert indexfeed.history(store, resource_contains="nope")["events"] == []
    assert indexfeed.history(store, network="solana:x")["events"] == []


def test_history_gates_and_catalogue(tmp_path):
    empty = indexfeed.IndexStore(str(tmp_path / "empty.db"))
    with pytest.raises(UpstreamError):
        indexfeed.history(empty)
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        indexfeed.history(store, catalogue="payai")
    # Retention is no longer pruned to the newest few.
    for i in range(6):
        store.save_snapshot([_row(R1, P1)], epoch=5_000_000 + i)
    assert len(store.snapshots()) == 10


def _fixture_stores(tmp_path, monkeypatch):
    db = str(tmp_path / "watches.db")
    watch = recheck.WatchStore(db)
    index = _store(tmp_path)
    # _store built its own file; rebuild snapshots into the shared db
    index = indexfeed.IndexStore(db)
    index.save_snapshot([_row(R1, P1), _row(R2, P2)], epoch=1_000_000)
    index.save_snapshot([_row(R1, P1, amount="20000")], epoch=2_000_000)
    monkeypatch.setattr(main, "_recheck_store", watch)
    monkeypatch.setattr(main, "_index_store", index)
    return watch, index


def test_history_unpaid_402s_at_one_cent(tmp_path, monkeypatch):
    _fixture_stores(tmp_path, monkeypatch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        r = client.post("/v1/index/history", json={})
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    amounts = {a["amount"] for a in challenge["accepts"]}
    assert amounts == {"10000"}


def test_history_pass_funded_delivery_debits_one_cent(tmp_path, monkeypatch):
    watch, _index = _fixture_stores(tmp_path, monkeypatch)
    fleet_pass, _ = watch.mint_pass(
        "pay-hist", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        r = client.post(
            "/v1/index/history",
            json={"resource": R1},
            headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshots_held"] == 2
    assert {e["resource"] for e in body["events"]} == {R1}
    after = watch.get_pass(fleet_pass["pass_id"])
    assert after["balance_units"] == fleetpass.PASS_PRICE_UNITS - 10_000
