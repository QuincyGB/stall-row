"""Rotation Alerts: registration validation, the snapshot-pair event
computation, the funded evaluation flow (free silent baseline, then
$0.005 Fleet Pass debit per comparison, push only on events), and
the funding_failed path that must not advance the watermark.
Fully offline — IndexStore snapshots are crafted rows, the webhook
receiver is a MockTransport."""

import httpx
import pytest

from app.services import fleetpass, indexfeed, recheck, rotalerts

PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
THIRD = "0x2222222222222222222222222222222222222222"
RES = "https://a.example.com/v1/verdict"
RES2 = "https://b.example.com/v1/check"
WEBHOOK = "https://93.184.216.35/hook"
PRIVATE = "https://10.0.0.5/hook"


def _row(resource, pay_to, amount="20000", name="svc"):
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


@pytest.fixture()
def stores(tmp_path):
    db = str(tmp_path / "watches.db")
    return recheck.WatchStore(db), indexfeed.IndexStore(db)


def _pass(store):
    fleet_pass, _ = store.mint_pass(
        "pay-rot", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    return fleet_pass


async def _register(store, **over):
    kwargs = dict(
        pay_to=PAY_TO,
        resource=None,
        webhook_url=WEBHOOK,
        email=None,
        fleet_pass_id=_pass(store)["pass_id"],
    )
    kwargs.update(over)
    return await rotalerts.register(store, **kwargs)


# --- compute_events (pure) -----------------------------------------------------

def test_compute_events_rotation_both_directions():
    old = {RES: _row(RES, PAY_TO), RES2: _row(RES2, OTHER)}
    new = {RES: _row(RES, OTHER), RES2: _row(RES2, PAY_TO)}
    events = rotalerts.compute_events(old, new, pay_to=PAY_TO)
    kinds = {(e["resource"], e["kind"]) for e in events}
    assert (RES, "payto_rotated") in kinds
    assert (RES2, "payto_rotated") in kinds
    rot = next(e for e in events if e["resource"] == RES)
    assert rot["from_pay_to"] == PAY_TO and rot["to_pay_to"] == OTHER


def test_compute_events_price_vanish_add():
    old = {RES: _row(RES, PAY_TO, amount="20000"), RES2: _row(RES2, PAY_TO)}
    new = {RES: _row(RES, PAY_TO, amount="25000")}
    events = rotalerts.compute_events(old, new, pay_to=PAY_TO)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["price_changed"]["from_amount"] == "20000"
    assert by_kind["price_changed"]["to_amount"] == "25000"
    assert by_kind["vanished"]["resource"] == RES2
    # And the reverse pair reads as an addition.
    events = rotalerts.compute_events(new, old, pay_to=PAY_TO)
    assert {e["kind"] for e in events} == {"price_changed", "added"}


def test_compute_events_ignores_other_paytos_and_resource_scope():
    old = {RES: _row(RES, OTHER), RES2: _row(RES2, THIRD)}
    new = {RES: _row(RES, THIRD), RES2: _row(RES2, THIRD, amount="99000")}
    assert rotalerts.compute_events(old, new, pay_to=PAY_TO) == []
    scoped = rotalerts.compute_events(old, new, resource=RES2)
    assert [e["kind"] for e in scoped] == ["price_changed"]


# --- registration ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_validates_and_stores(stores):
    store, _index = stores
    reg = await _register(store)
    assert reg["reg_id"].startswith("rot_")
    assert reg["funding_kind"] == "fleet_pass"
    assert reg["run_price_units"] == 5_000
    assert reg["last_eval_snapshot_id"] is None


@pytest.mark.asyncio
async def test_register_refusals(stores):
    store, _index = stores
    with pytest.raises(rotalerts.RotationError):
        await _register(store, pay_to=None, resource=None)
    with pytest.raises(rotalerts.RotationError):
        await _register(store, pay_to="not-an-address")
    with pytest.raises(rotalerts.RotationError):
        await _register(store, webhook_url=PRIVATE)
    with pytest.raises(rotalerts.RotationError) as e:
        await _register(store, fleet_pass_id="fpass_nope")
    assert e.value.http_status == 404
    with pytest.raises(rotalerts.RotationError):
        await _register(store, fleet_pass_id=None)


# --- evaluation flow --------------------------------------------------------------

def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_evaluate_baseline_then_events_then_no_new(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    fleet_pass_id = reg["funding_id"]
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await rotalerts.evaluate(client, store, index, reg)
        assert out["status"] == "baseline"
        assert out["debited_units"] == 0
        # Baseline is silent and free.
        assert state["deliveries"] == []
        assert store.get_pass(fleet_pass_id)["balance_units"] == 5_000_000

        reg = store.get_rotation_reg(reg["reg_id"])
        out = await rotalerts.evaluate(client, store, index, reg)
        assert out["status"] == "no_new_snapshot"

        # A new snapshot lands with a rotation away from the payTo.
        index.save_snapshot([_row(RES, OTHER)], epoch=3_000_000)
        reg = store.get_rotation_reg(reg["reg_id"])
        out = await rotalerts.evaluate(client, store, index, reg)
        assert out["status"] == "events"
        assert [e["kind"] for e in out["events"]] == ["payto_rotated"]
        assert out["debited_units"] == 5_000
        assert store.get_pass(fleet_pass_id)["balance_units"] == 4_995_000
        assert len(state["deliveries"]) == 1
    deliveries = store.deliveries_for(reg["reg_id"])
    assert [d["event"] for d in deliveries] == ["rotation_alert"]
    assert deliveries[0]["http_status"] == 200


@pytest.mark.asyncio
async def test_evaluate_no_change_debits_but_pushes_nothing(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        await rotalerts.evaluate(client, store, index, reg)  # baseline
        index.save_snapshot([_row(RES, PAY_TO)], epoch=3_000_000)
        reg = store.get_rotation_reg(reg["reg_id"])
        out = await rotalerts.evaluate(client, store, index, reg)
    assert out["status"] == "no_change"
    assert out["events"] == []
    assert state["deliveries"] == []
    assert store.get_pass(reg["funding_id"])["balance_units"] == 4_995_000


@pytest.mark.asyncio
async def test_evaluate_funding_failure_keeps_watermark(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    tiny, _ = store.mint_pass("pay-tiny", 5_000, fleetpass.PASS_TTL_DAYS)
    reg = await _register(store, fleet_pass_id=tiny["pass_id"])
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await rotalerts.evaluate(client, store, index, reg)
        assert out["status"] == "baseline"  # free even on a tiny pass
        # Drain the pass elsewhere, then land a new snapshot.
        store.debit_pass(tiny["pass_id"], "stall-row", "/v1/recheck", 5_000)
        index.save_snapshot([_row(RES, OTHER)], epoch=3_000_000)
        reg = store.get_rotation_reg(reg["reg_id"])
        watermark = reg["last_eval_snapshot_id"]
        out = await rotalerts.evaluate(client, store, index, reg)
    assert out["status"] == "funding_failed"
    reg = store.get_rotation_reg(reg["reg_id"])
    assert reg["last_eval_snapshot_id"] == watermark  # not advanced
    assert reg["last_status"] == "funding_failed"
    assert state["deliveries"] == []


@pytest.mark.asyncio
async def test_evaluate_waits_for_two_snapshots(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    reg = await _register(store)
    async with httpx.AsyncClient(transport=_transport({"deliveries": []})) as client:
        out = await rotalerts.evaluate(client, store, index, reg)
    assert out["status"] == "waiting_for_snapshots"
