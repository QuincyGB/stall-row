"""Verdict Index: normalization, snapshot store, and the diff rules."""

import pytest

from app.rpc import UpstreamError
from app.services import indexfeed


def _item(resource, amount="10000", pay_to="0xSeller", desc="a stall", name="Stall"):
    return {
        "resource": resource,
        "serviceName": name,
        "description": desc,
        "accepts": [
            {"scheme": "exact", "network": "eip155:8453", "amount": amount,
             "asset": "0xUSDC", "payTo": pay_to}
        ],
        "lastUpdated": "2026-10-26T00:00:00Z",
        "quality": {"l30DaysTotalCalls": 12, "l30DaysUniquePayers": 3},
    }


def test_normalize_takes_first_priced_accepts():
    row = indexfeed.normalize_item(_item("https://x.example/v1/a"))
    assert row["amount"] == "10000"
    assert row["payTo"] == "0xSeller"
    assert row["calls30d"] == 12 and row["payers30d"] == 3
    assert indexfeed.normalize_item({"serviceName": "no resource"}) is None
    assert indexfeed.normalize_item("junk") is None


def _rows(*items):
    return [indexfeed.normalize_item(i) for i in items]


def _as_map(rows):
    return {r["resource"]: r for r in rows}


def test_diff_field_change_added_removed():
    old = _as_map(_rows(_item("https://x/v1/a"), _item("https://x/v1/b")))
    new = _as_map(_rows(
        _item("https://x/v1/a", amount="20000"),
        _item("https://x/v1/c"),
    ))
    out = indexfeed.diff_entries(old, new)
    assert out["summary"] == {
        "resources_before": 2, "resources_after": 2,
        "field_changes": 1, "added": 1, "removed": 1,
    }
    kinds = [(e["resource"], e["kind"]) for e in out["events"]]
    assert kinds[0] == ("https://x/v1/a", "field_changed")  # signal first
    ev = out["events"][0]
    assert ev["field"] == "amount" and ev["before"] == "10000" and ev["after"] == "20000"


def test_diff_payto_rotation_is_a_field_change_and_filters_apply():
    old = _as_map(_rows(_item("https://x/v1/a", pay_to="0xOld")))
    new = _as_map(_rows(_item("https://x/v1/a", pay_to="0xNew")))
    out = indexfeed.diff_entries(old, new)
    assert out["events"][0]["field"] == "payTo"
    assert indexfeed.diff_entries(old, new, pay_to="0xnew")["events_returned"] == 1
    assert indexfeed.diff_entries(old, new, pay_to="0xOther")["events_returned"] == 0
    assert indexfeed.diff_entries(old, new, resource_contains="nope")["events_returned"] == 0
    assert indexfeed.diff_entries(old, new, service_name="stall")["events_returned"] == 1


def test_store_roundtrip_prune_and_changes_gate(tmp_path):
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    with pytest.raises(UpstreamError):
        indexfeed.changes(store)
    store.save_snapshot(_rows(_item("https://x/v1/a")), epoch=1_000_000)
    with pytest.raises(UpstreamError):  # still only ONE snapshot
        indexfeed.changes(store)
    store.save_snapshot(_rows(_item("https://x/v1/a", amount="5")), epoch=2_000_000)
    out = indexfeed.changes(store)
    assert out["summary"]["field_changes"] == 1
    assert out["baseline_at"] < out["current_at"]
    # Prune keeps only the newest KEEP_SNAPSHOTS.
    for i in range(5):
        store.save_snapshot(_rows(_item("https://x/v1/a")), epoch=3_000_000 + i)
    assert len(store.snapshots()) == indexfeed.KEEP_SNAPSHOTS


@pytest.mark.asyncio
async def test_fetch_catalogue_rejects_bad_upstream():
    import httpx

    def handler(request):
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError):
        await indexfeed.fetch_catalogue(client)
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_catalogue_identifies_itself():
    import httpx

    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"items": [], "pagination": {"total": 0}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rows = await indexfeed.fetch_catalogue(client)
    await client.aclose()
    assert rows == []
    assert seen["ua"] == indexfeed.INDEX_USER_AGENT


def _fake_fetch(monkeypatch, rows):
    calls = {"n": 0}

    async def fake(client):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(indexfeed, "fetch_catalogue", fake)
    return calls


@pytest.mark.asyncio
async def test_refresh_bootstrap_reaches_a_sellable_pair(monkeypatch, tmp_path):
    """Cold start: snapshot #2 must land on the bootstrap interval,
    not after a full refresh window — otherwise a fresh deploy
    cannot sell the route for ~6h (the production bug)."""
    import time

    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    rows = _rows(_item("https://x/v1/a"))
    calls = _fake_fetch(monkeypatch, rows)
    now = time.time()

    # Empty store: refresh immediately.
    assert await indexfeed.refresh_if_stale(store, None, 21600, 900) is True
    assert len(store.snapshots()) == 1

    # One snapshot, younger than the bootstrap interval: wait.
    assert await indexfeed.refresh_if_stale(store, None, 21600, 900) is False
    assert calls["n"] == 1

    # Age the held snapshot past bootstrap (but far inside the
    # normal window): the second snapshot must be taken now.
    store._conn.execute(
        "UPDATE index_snapshots SET captured_epoch = ?", (now - 1000,)
    )
    store._conn.commit()
    assert await indexfeed.refresh_if_stale(store, None, 21600, 900) is True
    assert len(store.snapshots()) == 2

    # Pair complete: bootstrap no longer applies — the pair is
    # inside the normal window, so no third pull.
    assert await indexfeed.refresh_if_stale(store, None, 21600, 900) is False
    assert calls["n"] == 2

    # Past the normal window the pair refreshes as usual.
    store._conn.execute(
        "UPDATE index_snapshots SET captured_epoch = captured_epoch - 21601"
    )
    store._conn.commit()
    assert await indexfeed.refresh_if_stale(store, None, 21600, 900) is True
    assert calls["n"] == 3
