"""Index subscriptions: registration validation, the filtered
snapshot-pair diff, and the funded evaluation flow (free silent
baseline, then $0.10 Fleet Pass debit per delivered evaluation —
the exact price of one /v1/index/changes call), the funding_failed
path that must not advance the watermark, and delivery of EMPTY
diffs (the feed's heartbeat). Fully offline — IndexStore snapshots
are crafted rows, the webhook receiver is a MockTransport."""

import httpx
import pytest

from app.services import fleetpass, indexfeed, indexsub, recheck

PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
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


def _pass(store, key="pay-isub"):
    fleet_pass, _ = store.mint_pass(
        key, fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    return fleet_pass


async def _register(store, **over):
    kwargs = dict(
        resource_contains=None,
        pay_to=None,
        service_name=None,
        network=None,
        webhook_url=WEBHOOK,
        email=None,
        fleet_pass_id=_pass(store)["pass_id"],
    )
    kwargs.update(over)
    return await indexsub.register(store, **kwargs)


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(state):
    return httpx.AsyncClient(transport=_transport(state), base_url="https://x.example")


# --- registration ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_validates_and_stores(stores):
    store, _index = stores
    reg = await _register(store, pay_to=PAY_TO)
    assert reg["reg_id"].startswith("isub_")
    assert reg["funding_kind"] == "fleet_pass"
    assert reg["run_price_units"] == 100_000
    assert reg["pay_to"] == PAY_TO
    assert reg["last_eval_snapshot_id"] is None


@pytest.mark.asyncio
async def test_register_refusals(stores):
    store, _index = stores
    with pytest.raises(indexsub.IndexSubError):
        await _register(store, pay_to="not-an-address")
    with pytest.raises(indexsub.IndexSubError):
        await _register(store, webhook_url=PRIVATE)
    with pytest.raises(indexsub.IndexSubError):
        await _register(store, webhook_url=None, email=None)
    with pytest.raises(indexsub.IndexSubError) as e:
        await _register(store, fleet_pass_id="fpass_nope")
    assert e.value.http_status == 404
    with pytest.raises(indexsub.IndexSubError):
        await _register(store, fleet_pass_id=None)
    # A pass that cannot fund one $0.10 evaluation refuses.
    tiny, _ = store.mint_pass("pay-tiny", 50_000, fleetpass.PASS_TTL_DAYS)
    with pytest.raises(indexsub.IndexSubError) as e:
        await _register(store, fleet_pass_id=tiny["pass_id"])
    assert e.value.http_status == 402


# --- evaluation flow --------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_baseline_then_diff_then_no_new(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with _client(state) as client:
        first = await indexsub.evaluate(client, store, index, reg)
        assert first["status"] == "baseline" and first["debited_units"] == 0
        assert state["deliveries"] == []
        reg = store.get_index_sub(reg["reg_id"])
        # New snapshot pair with a price change on RES.
        index.save_snapshot([_row(RES, PAY_TO, amount="25000")], epoch=3_000_000)
        second = await indexsub.evaluate(client, store, index, reg)
        assert second["status"] == "delivered"
        assert second["debited_units"] == 100_000
        events = second["diff"]["events"]
        assert any(
            e["kind"] == "field_changed" and e["field"] == "amount" for e in events
        )
        assert len(state["deliveries"]) == 1
        # Same pair again: no new snapshot, no charge, no push.
        third = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"])
        )
        assert third["status"] == "no_new_snapshot"
        assert len(state["deliveries"]) == 1


@pytest.mark.asyncio
async def test_evaluate_empty_diff_still_delivers_heartbeat(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with _client(state) as client:
        await indexsub.evaluate(client, store, index, reg)
        index.save_snapshot([_row(RES, PAY_TO)], epoch=3_000_000)
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"])
        )
        assert out["status"] == "delivered"
        assert out["diff"]["events"] == []
        assert len(state["deliveries"]) == 1  # heartbeat pushed anyway


@pytest.mark.asyncio
async def test_evaluate_filters_apply(stores):
    store, index = stores
    index.save_snapshot(
        [_row(RES, PAY_TO), _row(RES2, OTHER)], epoch=1_000_000
    )
    index.save_snapshot(
        [_row(RES, PAY_TO), _row(RES2, OTHER)], epoch=2_000_000
    )
    reg = await _register(store, pay_to=PAY_TO)
    state = {"deliveries": []}
    async with _client(state) as client:
        await indexsub.evaluate(client, store, index, reg)
        index.save_snapshot(
            [_row(RES, PAY_TO, amount="99000"), _row(RES2, OTHER, amount="99000")],
            epoch=3_000_000,
        )
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"])
        )
        resources = {e["resource"] for e in out["diff"]["events"]}
        assert resources == {RES}  # RES2 pays OTHER — filtered out


@pytest.mark.asyncio
async def test_evaluate_funding_failure_keeps_watermark(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    # Pass with exactly two evaluations in it; drain one via the
    # subscription itself, then drain the rest directly.
    reg = await _register(store)
    state = {"deliveries": []}
    async with _client(state) as client:
        await indexsub.evaluate(client, store, index, reg)  # baseline
        index.save_snapshot([_row(RES, PAY_TO, amount="30000")], epoch=3_000_000)
        ok = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"])
        )
        assert ok["status"] == "delivered"
        # Drain remaining balance (5.00 - 0.10 = 4.90) below $0.10.
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/orchestrate", 750_000)
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/counterparty-report", 1_000_000)
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/counterparty-report", 1_000_000)
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/counterparty-report", 1_000_000)
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/counterparty-report", 1_000_000)
        store.debit_pass(reg["funding_id"], "stall-row", "/v1/index/changes", 100_000)
        wm = store.get_index_sub(reg["reg_id"])["last_eval_snapshot_id"]
        index.save_snapshot([_row(RES, PAY_TO, amount="40000")], epoch=4_000_000)
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"])
        )
        assert out["status"] == "funding_failed"
        assert store.get_index_sub(reg["reg_id"])["last_eval_snapshot_id"] == wm


@pytest.mark.asyncio
async def test_evaluate_waits_for_two_snapshots(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with _client(state) as client:
        out = await indexsub.evaluate(client, store, index, reg)
        assert out["status"] == "waiting_for_snapshots"
