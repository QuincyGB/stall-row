"""Ledger Dataset: the observation ledger as a two-tier product.

Fully offline: fabricated Recheck records and fabricated catalogue
snapshots in tmp stores. The invariants are the honesty rules: the
build reads only the service's own stores, defect classes are
derived (mismatch / not_listed) and stay null when the data does
not support one, change counts keep their components, provenance
carries source counts and a stable snapshot id, and the summary
tier never leaks per-route records.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import ledger, recheck
from app.services.indexfeed import IndexStore

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
URL = "https://93.184.216.34/v1/thing"
URL2 = "https://93.184.216.34/v1/other"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def index_store(tmp_path):
    return IndexStore(str(tmp_path / "index.db"))


def _record(store, url=URL, status="UNCHANGED", amount="5000", pay_to=OWNER,
            at="2026-10-30T00:00:00Z"):
    return store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": "x402",
            "status": status,
            "checked_at": at,
            "current": {
                "status_code": 402,
                "amount": amount,
                "asset": USDC,
                "network": "eip155:8453",
                "payTo": pay_to,
            },
        }
    )


def _snapshot(index_store, rows):
    return index_store.save_snapshot(
        [
            {
                "resource": r[0],
                "serviceName": "svc",
                "description": "d",
                "amount": r[2] if len(r) > 2 else "5000",
                "asset": USDC,
                "network": "eip155:8453",
                "payTo": r[1],
                "lastUpdated": "2026-10-30T00:00:00Z",
                "calls30d": 7,
                "payers30d": 2,
            }
            for r in rows
        ]
    )


def test_summary_rolls_up_recheck_history(store, index_store):
    _record(store, status="BASELINE")
    _record(store, status="CHANGED")
    _record(store, url=URL2, status="UNCHANGED")
    ds = ledger.build_dataset(store, index_store)
    view = ledger.summary_view(ds)
    assert view["tier"] == "summary"
    assert "records" not in view
    assert view["totals"]["routes"] == 2
    assert view["totals"]["checks"] == 3
    assert view["totals"]["changes"] == 1
    assert view["totals"]["origins"] == 1
    origin = view["origins"][0]
    assert origin["origin"] == "https://93.184.216.34"
    assert origin["routes"] == 2 and origin["checks"] == 3
    prov = view["provenance"]
    assert prov["sources"]["recheck_records"] == 3
    assert prov["sources"]["routes_from_recheck"] == 2
    assert len(prov["snapshot_id"]) == 64
    assert "ANSWERED" in prov["limits"]


def test_snapshot_id_is_stable_for_same_data(store, index_store):
    _record(store)
    first = ledger.build_dataset(store, index_store)["provenance"]["snapshot_id"]
    second = ledger.build_dataset(store, index_store)["provenance"]["snapshot_id"]
    assert first == second


def test_defect_mismatch_from_snapshot_vs_observed(store, index_store):
    _record(store, pay_to=OWNER)
    _snapshot(index_store, [(URL, OTHER)])  # catalogue lists another payTo
    ds = ledger.build_dataset(store, index_store)
    rec = ds["records"][0]
    assert rec["defect_class"] == "catalogue_mismatch"
    assert rec["defect_detail"]["via"] == "snapshot_vs_latest_observed"
    assert rec["defect_detail"]["fields"] == ["payTo"]
    assert rec["catalogue"]["listed"] is True
    assert rec["catalogue"]["calls30d"] == 7


def test_defect_not_listed_when_absent_from_newest_snapshot(store, index_store):
    _record(store)
    _snapshot(index_store, [(URL2, OWNER)])  # catalogue holds only the other route
    ds = ledger.build_dataset(store, index_store)
    by_url = {r["url"]: r for r in ds["records"]}
    assert by_url[URL]["defect_class"] == "not_listed"
    assert by_url[URL2]["defect_class"] is None  # listed, never rechecked


def test_no_defect_claim_without_evidence(store, index_store):
    _record(store, pay_to=OWNER)
    _snapshot(index_store, [(URL, OWNER)])  # listed terms agree with observed
    ds = ledger.build_dataset(store, index_store)
    assert ds["records"][0]["defect_class"] is None
    assert ds["totals"]["defective_routes"] == 0


def test_catalogue_changes_and_recovery_across_snapshots(store, index_store):
    _snapshot(index_store, [(URL, OWNER, "5000")])
    _snapshot(index_store, [(URL2, OWNER, "5000")])          # URL vanishes
    _snapshot(index_store, [(URL, OWNER, "6000"), (URL2, OWNER, "5000")])  # URL returns, repriced
    ds = ledger.build_dataset(store, index_store)
    by_url = {r["url"]: r for r in ds["records"]}
    assert by_url[URL]["recoveries"] == 1
    assert by_url[URL]["catalogue"]["present_in_latest"] is True
    assert by_url[URL]["catalogue"]["snapshots_seen"] == 2
    assert by_url[URL2]["catalogue_changes"] == 0
    assert ds["provenance"]["sources"]["index_snapshots"].__len__() == 3


def test_full_view_carries_records_and_terms(store, index_store):
    _record(store, status="CHANGED", amount="5000")
    ds = ledger.build_dataset(store, index_store)
    view = ledger.full_view(ds)
    assert view["tier"] == "full"
    rec = view["records"][0]
    assert rec["checks"] == 1 and rec["recheck_changes"] == 1
    assert rec["terms"]["amount"] == "5000"
    assert rec["terms"]["payTo"] == OWNER
    assert rec["catalogue"] is None  # no snapshots held: unknown, not "not listed"
    assert rec["defect_class"] is None


def test_http_surface_charges_dataset_prices():
    import base64
    import json

    with TestClient(main.app) as client:
        for path, units in (
            ("/v1/ledger-dataset", "100000"),
            ("/v1/ledger-dataset/full", "1000000"),
        ):
            resp = client.post(path, json={})
            assert resp.status_code == 402, path
            challenge = json.loads(base64.b64decode(resp.headers["payment-required"]))
            amounts = [a["amount"] for a in challenge["accepts"]]
            assert amounts == [units], path
        manifest = client.get("/.well-known/x402").json()
        listed = {r["resource"]: r for r in manifest["resources"]}
        assert listed["https://testserver/v1/ledger-dataset"]["price"] == "$0.1"
        assert listed["https://testserver/v1/ledger-dataset/full"]["price"] == "$1"
