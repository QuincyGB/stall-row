"""The committed patrol snapshot (app/data/patrol-snapshot.json).

Exported VM-side from Dima's PC patrol ledger by patrol_snapshot.py
(fleet-checker) and folded into the Ledger Dataset tiers through
services/patrolsnap.py. These tests pin the REAL committed file:
it loads, its rows carry fact fields only (the redaction contract),
its watermarks are self-consistent, and the dataset build against
it surfaces genuine patrol facts for known routes.
"""

import json
from pathlib import Path

from app.services import ledger, patrolsnap, recheck
from app.services.indexfeed import IndexStore

COMMITTED = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "patrol-snapshot.json"
)

ROUTE_KEYS = {
    "url", "origin", "status", "defect_class", "first_seen",
    "last_seen", "recovered_at", "receipt_id",
}


def test_committed_snapshot_loads_and_is_consistent():
    snap = patrolsnap.load_snapshot()
    assert snap is not None
    assert snap["kind"] == "verdict-row-patrol-snapshot/v1"
    assert len(snap["snapshot_id"]) == 64
    routes = snap["routes"]
    assert routes == sorted(routes, key=lambda r: (r["url"], r["status"]))
    open_rows = [r for r in routes if r["status"] == "open"]
    recovered = [r for r in routes if r["status"] == "recovered"]
    assert len(open_rows) == snap["source"]["open_defects"] > 0
    assert len(recovered) == snap["source"]["recoveries"] > 0
    prov = patrolsnap.provenance_of(snap)
    assert prov["id"] == snap["snapshot_id"]
    assert prov["patrol_run_id"] == snap["source"]["patrol_run_id"]


def test_committed_snapshot_rows_are_fact_fields_only():
    snap = json.loads(COMMITTED.read_bytes())
    for row in snap["routes"]:
        assert set(row) <= ROUTE_KEYS, row
        if row["status"] == "open":
            assert row["last_seen"] and "receipt_id" not in row
        else:
            assert row["receipt_id"].startswith("rr-")
            assert row["recovered_at"] and "last_seen" not in row
    # Forbidden data is checked as KEYS (a stall route may legitimately
    # be NAMED e.g. /v1/open-inbox — that is a route fact, not inbox
    # content) plus the signer wallet, which must appear nowhere.
    def keys_of(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys_of(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys_of(item)

    keys = set(keys_of(snap))
    for forbidden in ("signature", "signer", "inboxes", "nudges", "source_note", "probes"):
        assert forbidden not in keys, forbidden
    assert "0xa4f183a6799f0310fcc545b07f2dcc960223de2" not in json.dumps(snap).lower()


def test_dataset_build_folds_the_real_snapshot(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    index_store = IndexStore(str(tmp_path / "index.db"))
    ds = ledger.build_dataset(store, index_store)  # default: committed snapshot
    snap = patrolsnap.load_snapshot()
    assert ds["totals"]["patrol_open_defects"] == snap["source"]["open_defects"]
    assert ds["totals"]["patrol_recoveries"] == snap["source"]["recoveries"]
    assert ds["provenance"]["sources"]["patrol_snapshot"]["id"] == snap["snapshot_id"]
    by_url = {r["url"]: r for r in ds["records"]}
    # A route with a signed recovery receipt in the committed file.
    fixed = by_url["https://402oracle.com/verify/price"]
    assert fixed["patrol"]["status"] == "recovered"
    assert fixed["patrol"]["receipt_id"] == "rr-1e12dde0c299cb395d37"
    # A route the patrol currently lists as open-defective.
    open_rec = by_url["https://1gateway.dev/v1/ai/llm-prices"]
    assert open_rec["patrol"]["status"] == "open"
    assert open_rec["patrol"]["defect_class"] == "unexpected_status"
