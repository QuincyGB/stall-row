"""Router Watchlist: registration validation, the top-N snapshot
diff (entries, exits, member rotations/price changes), and the
funded evaluation flow — free silent baseline, then $0.005 Fleet
Pass debit per comparison, push only on events, funding_failed
without watermark advance. Mirrors test_rotalerts.py; fully
offline."""

import httpx
import pytest

from app.services import fleetpass, indexfeed, recheck, watchlist

A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
C = "0x3333333333333333333333333333333333333333"
R1 = "https://a.example.com/v1/one"
R2 = "https://b.example.com/v1/two"
R3 = "https://c.example.com/v1/three"
WEBHOOK = "https://93.184.216.35/hook"
PRIVATE = "https://10.0.0.5/hook"


def _row(resource, pay_to, calls, amount="20000", name="svc"):
    return {
        "resource": resource,
        "serviceName": name,
        "description": "d",
        "amount": amount,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "eip155:8453",
        "payTo": pay_to,
        "lastUpdated": None,
        "calls30d": calls,
        "payers30d": 2,
    }


def _entries(*rows):
    return {r["resource"]: r for r in rows}


@pytest.fixture()
def stores(tmp_path):
    db = str(tmp_path / "watches.db")
    return recheck.WatchStore(db), indexfeed.IndexStore(db)


def _pass(store, units=fleetpass.PASS_PRICE_UNITS):
    fleet_pass, _ = store.mint_pass("pay-wl", units, fleetpass.PASS_TTL_DAYS)
    return fleet_pass


async def _register(store, **over):
    kwargs = dict(
        top_n=2,
        webhook_url=WEBHOOK,
        email=None,
        fleet_pass_id=_pass(store)["pass_id"],
    )
    kwargs.update(over)
    return await watchlist.register(store, **kwargs)


# --- compute_events (pure) -----------------------------------------------------

def test_compute_events_entered_left_and_member_changes():
    old = _entries(_row(R1, A, 100), _row(R2, B, 50), _row(R3, C, 10))
    new = _entries(
        _row(R1, B, 120, amount="25000"),  # member: rotated + repriced
        _row(R2, B, 5),                    # falls out of the top-2
        _row(R3, C, 80),                   # enters the top-2
    )
    events = watchlist.compute_events(old, new, top_n=2)
    by = {(e["resource"], e["kind"]) for e in events}
    assert (R3, "entered") in by
    assert (R2, "left") in by
    assert (R1, "payto_rotated") in by
    assert (R1, "price_changed") in by
    entered = next(e for e in events if e["kind"] == "entered")
    assert entered["rank"] == 2
    left = next(e for e in events if e["kind"] == "left")
    assert left["still_listed"] is True


def test_compute_events_quiet_pair():
    rows = _entries(_row(R1, A, 100), _row(R2, B, 50))
    assert watchlist.compute_events(rows, dict(rows), top_n=2) == []


# --- registration ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_validates_and_stores(stores):
    store, _index = stores
    reg = await _register(store)
    assert reg["reg_id"].startswith("wl_")
    assert reg["top_n"] == 2
    assert reg["run_price_units"] == 5_000
    reg = await _register(store, top_n=None)
    assert reg["top_n"] == 100  # default


@pytest.mark.asyncio
async def test_register_refusals(stores):
    store, _index = stores
    with pytest.raises(watchlist.WatchlistError):
        await _register(store, top_n=0)
    with pytest.raises(watchlist.WatchlistError):
        await _register(store, top_n=501)
    with pytest.raises(watchlist.WatchlistError):
        await _register(store, webhook_url=PRIVATE)
    with pytest.raises(watchlist.WatchlistError) as e:
        await _register(store, fleet_pass_id="fpass_nope")
    assert e.value.http_status == 404


# --- evaluation flow --------------------------------------------------------------

def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_evaluate_baseline_then_events(stores):
    store, index = stores
    base = [_row(R1, A, 100), _row(R2, B, 50), _row(R3, C, 10)]
    index.save_snapshot(base, epoch=1_000_000)
    index.save_snapshot(list(base), epoch=2_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watchlist.evaluate(client, store, index, reg, now_epoch=2000000)
        assert out["status"] == "baseline"
        assert out["debited_units"] == 0
        assert state["deliveries"] == []

        # R3 surges into the top-2; R2 drops out.
        index.save_snapshot(
            [_row(R1, A, 100), _row(R2, B, 5), _row(R3, C, 90)],
            epoch=3_000_000,
        )
        reg = store.get_watchlist_reg(reg["reg_id"])
        out = await watchlist.evaluate(client, store, index, reg, now_epoch=3000000)
        assert out["status"] == "events"
        kinds = {(e["resource"], e["kind"]) for e in out["events"]}
        assert (R3, "entered") in kinds and (R2, "left") in kinds
        assert out["debited_units"] == 5_000
        assert len(state["deliveries"]) == 1
    deliveries = store.deliveries_for(reg["reg_id"])
    assert [d["event"] for d in deliveries] == ["watchlist_diff"]


@pytest.mark.asyncio
async def test_evaluate_funding_failure_keeps_watermark(stores):
    store, index = stores
    base = [_row(R1, A, 100), _row(R2, B, 50)]
    index.save_snapshot(base, epoch=1_000_000)
    index.save_snapshot(list(base), epoch=2_000_000)
    tiny = _pass(store, units=5_000)
    reg = await _register(store, fleet_pass_id=tiny["pass_id"])
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watchlist.evaluate(client, store, index, reg, now_epoch=2000000)
        assert out["status"] == "baseline"
        store.debit_pass(tiny["pass_id"], "stall-row", "/v1/recheck", 5_000)
        index.save_snapshot(
            [_row(R1, A, 100), _row(R2, B, 50), _row(R3, C, 500)],
            epoch=3_000_000,
        )
        reg = store.get_watchlist_reg(reg["reg_id"])
        watermark = reg["last_eval_snapshot_id"]
        out = await watchlist.evaluate(client, store, index, reg, now_epoch=3000000)
    assert out["status"] == "funding_failed"
    reg = store.get_watchlist_reg(reg["reg_id"])
    assert reg["last_eval_snapshot_id"] == watermark
    assert state["deliveries"] == []


@pytest.mark.asyncio
async def test_evaluate_skips_a_stale_snapshot_then_delivers_fresh(stores):
    """Regression (2026-10-27): a would-be evaluation against a
    stale newest snapshot must skip — no debit, no watermark
    advance, no push — and the SAME registration must evaluate once
    a fresh snapshot lands. (Before this guard, evaluate() debited
    $0.005 and diffed arbitrarily old pairs; the Verdict Index
    carries the same guard.)"""
    store, index = stores
    base = [_row(R1, A, 100), _row(R2, B, 50), _row(R3, C, 10)]
    index.save_snapshot(base, epoch=1_000_000)
    index.save_snapshot(list(base), epoch=2_000_000)
    reg = await _register(store)
    fleet_pass_id = reg["funding_id"]
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        base_out = await watchlist.evaluate(
            client, store, index, reg, now_epoch=2_000_000
        )
        assert base_out["status"] == "baseline"
        wm = store.get_watchlist_reg(reg["reg_id"])["last_eval_snapshot_id"]
        bal = store.get_pass(fleet_pass_id)["balance_units"]
        # New snapshot exists, but by evaluation time it is stale.
        index.save_snapshot(
            [_row(R1, A, 100), _row(R2, B, 5), _row(R3, C, 90)],
            epoch=3_000_000,
        )
        reg = store.get_watchlist_reg(reg["reg_id"])
        out = await watchlist.evaluate(
            client, store, index, reg, now_epoch=3_000_000 + 25_201
        )
        assert out["status"] == "skipped"
        assert "stale" in out["reason"]
        assert out["debited_units"] == 0
        after = store.get_watchlist_reg(reg["reg_id"])
        assert after["last_eval_snapshot_id"] == wm
        assert after["last_status"] == "skipped"
        assert store.get_pass(fleet_pass_id)["balance_units"] == bal
        assert state["deliveries"] == []
        # A fresh snapshot lands: evaluation resumes and debits.
        # (The store diffs the two newest, so this pair is the
        # stale snapshot vs the fresh one: R2 surges back in.)
        index.save_snapshot(
            [_row(R1, A, 100), _row(R2, B, 95), _row(R3, C, 90)],
            epoch=3_100_000,
        )
        reg = store.get_watchlist_reg(reg["reg_id"])
        out2 = await watchlist.evaluate(
            client, store, index, reg, now_epoch=3_100_000
        )
        assert out2["status"] == "events"
        kinds = {(e["resource"], e["kind"]) for e in out2["events"]}
        assert (R2, "entered") in kinds and (R3, "left") in kinds
        assert out2["debited_units"] == 5_000
        assert len(state["deliveries"]) == 1
