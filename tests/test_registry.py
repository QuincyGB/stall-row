"""Bad-payTo Registry Feed: entries only with evidence — the OFAC
snapshot's own addresses, payTos rotated away from live listings,
and payTos of vanished listings, all from the same two sources
Pre-Trade Guard screens against. Offline: crafted Index snapshots;
the sanctions side reads the real bundled snapshot."""

import pytest

from app.services import indexfeed, registry, verdict

A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
C = "0x3333333333333333333333333333333333333333"
R1 = "https://a.example.com/v1/one"
R2 = "https://b.example.com/v1/two"
R3 = "https://c.example.com/v1/three"


def _row(resource, pay_to, calls=10):
    return {
        "resource": resource,
        "serviceName": "svc",
        "description": "d",
        "amount": "20000",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "eip155:8453",
        "payTo": pay_to,
        "lastUpdated": None,
        "calls30d": calls,
        "payers30d": 1,
    }


def _store(tmp_path, *, two=True):
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    store.save_snapshot([_row(R1, A), _row(R2, C), _row(R3, B)], epoch=1_000_000)
    if two:
        # R1 rotates A -> B; R2 (paying C) vanishes; R3 unchanged.
        store.save_snapshot([_row(R1, B), _row(R3, B)], epoch=2_000_000)
    return store


def test_feed_flags_all_three_reasons_with_evidence(tmp_path):
    feed = registry.build_feed(_store(tmp_path))
    by_pay_to = {e["pay_to"]: e for e in feed["entries"]}

    rotated = by_pay_to[A.lower()]
    assert rotated["reasons"] == ["rotated_away"]
    ev = rotated["evidence"]["rotated_away"]
    assert ev["count"] == 1
    assert ev["examples"] == [{"resource": R1, "to_pay_to": B}]

    vanished = by_pay_to[C.lower()]
    assert vanished["reasons"] == ["vanished_listing"]
    assert vanished["evidence"]["vanished_listing"]["examples"] == [
        {"resource": R2}
    ]

    listed = sorted(verdict.SNAPSHOT["addresses"])[0]
    ofac = by_pay_to[listed]
    assert "ofac_sdn" in ofac["reasons"]
    assert ofac["evidence"]["ofac_sdn"]["snapshot_date"] == "2026-10-21"
    assert "treasury.gov" in ofac["evidence"]["ofac_sdn"]["snapshot_source"]

    assert feed["counts"]["ofac_sdn"] == verdict.SNAPSHOT["count"]
    assert feed["counts"]["rotated_away_events"] == 1
    assert feed["counts"]["vanished_events"] == 1
    assert feed["counts"]["entries_returned"] == len(feed["entries"])
    assert feed["truncation"]["entries_truncated"] == 0
    assert feed["baseline_at"] is not None and feed["current_at"] is not None
    # B gained a listing but is not itself flagged.
    assert B.lower() not in by_pay_to


def test_feed_with_one_snapshot_says_so(tmp_path):
    feed = registry.build_feed(_store(tmp_path, two=False))
    assert feed["index_note"] is not None
    assert feed["counts"]["rotated_away_events"] == 0
    assert feed["counts"]["vanished_events"] == 0
    assert feed["counts"]["ofac_sdn"] >= 50  # the OFAC section stands alone


def test_every_entry_carries_evidence(tmp_path):
    feed = registry.build_feed(_store(tmp_path))
    for entry in feed["entries"]:
        assert entry["reasons"], entry
        for reason in entry["reasons"]:
            assert reason in entry["evidence"], entry
