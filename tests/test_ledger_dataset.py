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
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
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
    first = ledger.build_dataset(store, index_store, patrol_snapshot=None)["provenance"]["snapshot_id"]
    second = ledger.build_dataset(store, index_store, patrol_snapshot=None)["provenance"]["snapshot_id"]
    assert first == second


def test_defect_mismatch_from_snapshot_vs_observed(store, index_store):
    _record(store, pay_to=OWNER)
    _snapshot(index_store, [(URL, OTHER)])  # catalogue lists another payTo
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    rec = ds["records"][0]
    assert rec["defect_class"] == "catalogue_mismatch"
    assert rec["defect_detail"]["via"] == "snapshot_vs_latest_observed"
    assert rec["defect_detail"]["fields"] == ["payTo"]
    assert rec["catalogue"]["listed"] is True
    assert rec["catalogue"]["calls30d"] == 7


def test_defect_not_listed_when_absent_from_newest_snapshot(store, index_store):
    _record(store)
    _snapshot(index_store, [(URL2, OWNER)])  # catalogue holds only the other route
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    by_url = {r["url"]: r for r in ds["records"]}
    assert by_url[URL]["defect_class"] == "not_listed"
    assert by_url[URL2]["defect_class"] is None  # listed, never rechecked


def test_no_defect_claim_without_evidence(store, index_store):
    _record(store, pay_to=OWNER)
    _snapshot(index_store, [(URL, OWNER)])  # listed terms agree with observed
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    assert ds["records"][0]["defect_class"] is None
    assert ds["totals"]["defective_routes"] == 0


def test_catalogue_changes_and_recovery_across_snapshots(store, index_store):
    _snapshot(index_store, [(URL, OWNER, "5000")])
    _snapshot(index_store, [(URL2, OWNER, "5000")])          # URL vanishes
    _snapshot(index_store, [(URL, OWNER, "6000"), (URL2, OWNER, "5000")])  # URL returns, repriced
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    by_url = {r["url"]: r for r in ds["records"]}
    assert by_url[URL]["recoveries"] == 1
    assert by_url[URL]["catalogue"]["present_in_latest"] is True
    assert by_url[URL]["catalogue"]["snapshots_seen"] == 2
    assert by_url[URL2]["catalogue_changes"] == 0
    assert ds["provenance"]["sources"]["index_snapshots"].__len__() == 3


def test_full_view_carries_records_and_terms(store, index_store):
    _record(store, status="CHANGED", amount="5000")
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    view = ledger.full_view(ds)
    assert view["tier"] == "full"
    rec = view["records"][0]
    assert rec["checks"] == 1 and rec["recheck_changes"] == 1
    assert rec["terms"]["amount"] == "5000"
    assert rec["terms"]["payTo"] == OWNER
    assert rec["catalogue"] is None  # no snapshots held: unknown, not "not listed"
    assert rec["defect_class"] is None


PATROL_URL = "https://93.184.216.34/v1/patrolled"
RECOVERED_URL = "https://93.184.216.34/v1/fixed"


def _patrol_snapshot():
    return {
        "kind": "verdict-row-patrol-snapshot/v1",
        "generated_at": "2026-10-31T12:42:27Z",
        "snapshot_id": "a" * 64,
        "source": {
            "patrol": "Dima (PC patrol walker)",
            "patrol_run_id": "20261031T120333Z",
            "patrol_as_of": "2026-10-31T12:03:33Z",
            "patrol_runs_total": 179,
            "patrol_stalls_scanned_total": 11272,
            "patrol_resources_probed_total": 11351,
            "open_defects": 1,
            "recoveries": 1,
            "receipts_index": "recovery-receipts/index.json",
        },
        "routes": [
            {
                "url": PATROL_URL,
                "origin": "https://93.184.216.34",
                "status": "open",
                "defect_class": "unexpected_status",
                "first_seen": "2026-10-30T17:31:18Z",
                "last_seen": "2026-10-31T08:02:59Z",
            },
            {
                "url": RECOVERED_URL,
                "origin": "https://93.184.216.34",
                "status": "recovered",
                "defect_class": "unreachable",
                "first_seen": "2026-10-28T10:47:09Z",
                "recovered_at": "2026-10-30T09:18:15Z",
                "receipt_id": "rr-0123456789abcdef0123",
            },
        ],
    }


def test_patrol_open_defect_and_recovery_fold_in(store, index_store):
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=_patrol_snapshot())
    by_url = {r["url"]: r for r in ds["records"]}
    open_rec = by_url[PATROL_URL]
    assert open_rec["sources"] == ["patrol"]
    assert open_rec["patrol"] == {
        "status": "open",
        "defect_class": "unexpected_status",
        "first_seen": "2026-10-30T17:31:18Z",
        "last_seen": "2026-10-31T08:02:59Z",
        "recovered_at": None,
        "receipt_id": None,
    }
    # The patrol vocabulary never overwrites the derived class.
    assert open_rec["defect_class"] is None
    assert open_rec["first_seen"] == "2026-10-30T17:31:18Z"
    assert open_rec["last_seen"] == "2026-10-31T08:02:59Z"
    fixed = by_url[RECOVERED_URL]
    assert fixed["patrol"]["status"] == "recovered"
    assert fixed["patrol"]["receipt_id"] == "rr-0123456789abcdef0123"
    assert fixed["patrol"]["recovered_at"] == "2026-10-30T09:18:15Z"
    # Patrol recoveries stay apart from catalogue recoveries.
    assert ds["totals"]["patrol_open_defects"] == 1
    assert ds["totals"]["patrol_recoveries"] == 1
    assert ds["totals"]["recoveries"] == 0
    prov = ds["provenance"]["sources"]
    assert prov["routes_from_patrol"] == 2
    assert prov["patrol_snapshot"]["id"] == "a" * 64
    assert prov["patrol_snapshot"]["patrol_run_id"] == "20261031T120333Z"
    assert prov["patrol_snapshot"]["open_defects"] == 1
    origin = ds["origins"][0]
    assert origin["patrol_open"] == 1
    assert origin["patrol_recovered"] == 1
    assert origin["patrol_defect_classes"] == ["unexpected_status"]
    assert "point-in-time export" in ds["provenance"]["limits"]


def test_patrol_facts_merge_with_recheck_route(store, index_store):
    _record(store, url=PATROL_URL, status="UNCHANGED", at="2026-10-29T00:00:00Z")
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=_patrol_snapshot())
    rec = {r["url"]: r for r in ds["records"]}[PATROL_URL]
    assert rec["sources"] == ["patrol", "recheck"]
    assert rec["checks"] == 1
    assert rec["patrol"]["defect_class"] == "unexpected_status"
    # first_seen is the earlier Recheck sighting; last_seen the patrol's.
    assert rec["first_seen"] == "2026-10-29T00:00:00Z"
    assert rec["last_seen"] == "2026-10-31T08:02:59Z"


def test_unknown_origin_unchanged_by_patrol(store, index_store):
    _record(store, status="BASELINE")
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=_patrol_snapshot())
    rec = {r["url"]: r for r in ds["records"]}[URL]
    assert rec["patrol"] is None
    assert "patrol" not in rec["sources"]
    origin = {o["origin"]: o for o in ds["origins"]}["https://93.184.216.34"]
    # The fixture origin also carries the two patrol-only routes.
    assert origin["patrol_open"] == 1 and origin["patrol_recovered"] == 1


def test_no_patrol_snapshot_means_no_patrol_side(store, index_store):
    _record(store)
    ds = ledger.build_dataset(store, index_store, patrol_snapshot=None)
    assert ds["provenance"]["sources"]["patrol_snapshot"] is None
    assert ds["provenance"]["sources"]["routes_from_patrol"] == 0
    assert ds["totals"]["patrol_open_defects"] == 0
    assert ds["records"][0]["patrol"] is None


def test_patrol_payload_is_redacted(store, index_store):
    import json as _json

    ds = ledger.build_dataset(store, index_store, patrol_snapshot=_patrol_snapshot())
    for view in (ledger.summary_view(ds), ledger.full_view(ds)):
        blob = _json.dumps(view).lower()
        for forbidden in (
            "signature", "signer", "inbox", "nudge",
            "source_note", "challenge_summary", "defect_observed",
            "0xa4f183a6799f0310fcc545b07f2dcc960223de2",
        ):
            assert forbidden not in blob, forbidden


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
