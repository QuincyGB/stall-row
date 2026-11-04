"""Index Subscription v2 (Next-20 #7): per-catalogue slicing,
console-free management (pause/resume/cancel/filter-update,
authenticated by the registration id alone), and v1 compatibility
— a v1-shaped registration (no catalogue, no management calls)
behaves exactly as before. Offline, mirroring test_indexsub.py."""

import httpx
import pytest

from app.services import fleetpass, indexfeed, indexsub, recheck

PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
RES = "https://a.example.com/v1/verdict"
WEBHOOK = "https://93.184.216.35/hook"


def _row(resource, pay_to, amount="20000"):
    return {
        "resource": resource,
        "serviceName": "svc",
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


def _pass(store, key="pay-isub2"):
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


def _client(state):
    def handler(request):
        state["deliveries"].append(request)
        return httpx.Response(200, text="ok")

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://x.example"
    )


# --- catalogue slicing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_register_with_catalogue_slice(stores):
    store, _index = stores
    reg = await _register(store, catalogue="cdp_bazaar")
    assert reg["catalogue"] == "cdp_bazaar"
    reg2 = await _register(store, fleet_pass_id=_pass(store, "pay-b")["pass_id"])
    assert reg2["catalogue"] is None  # v1 shape: all catalogues
    with pytest.raises(indexsub.IndexSubError):
        await _register(
            store,
            catalogue="payai",
            fleet_pass_id=_pass(store, "pay-c")["pass_id"],
        )


@pytest.mark.asyncio
async def test_v1_registration_keeps_working(stores):
    """A v1 registration evaluates and delivers exactly as v1 did."""
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    state = {"deliveries": []}
    async with _client(state) as client:
        first = await indexsub.evaluate(client, store, index, reg, now_epoch=2_000_000)
        assert first["status"] == "baseline"
        index.save_snapshot([_row(RES, PAY_TO, amount="25000")], epoch=3_000_000)
        second = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"]),
            now_epoch=3_000_000,
        )
        assert second["status"] == "delivered"
        assert second["debited_units"] == 100_000
        assert len(state["deliveries"]) == 1


# --- management ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_blocks_evaluation_and_resume_restores(stores):
    store, index = stores
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    reg = await _register(store)
    paused = indexsub.pause(store, reg["reg_id"])
    assert paused["active"] is False and paused["last_status"] == "paused"
    # The ticker's due set excludes paused registrations.
    assert store.index_subs_to_eval(2) == []
    state = {"deliveries": []}
    async with _client(state) as client:
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"]),
            now_epoch=2_000_000,
        )
        assert out["status"] == "paused" and out["debited_units"] == 0
    resumed = indexsub.resume(store, reg["reg_id"])
    assert resumed["active"] is True
    async with _client(state) as client:
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"]),
            now_epoch=2_000_000,
        )
        assert out["status"] == "baseline"


@pytest.mark.asyncio
async def test_cancel_is_terminal(stores):
    store, _index = stores
    reg = await _register(store)
    cancelled = indexsub.cancel(store, reg["reg_id"])
    assert cancelled["active"] is False and cancelled["last_status"] == "cancelled"
    with pytest.raises(indexsub.IndexSubError) as e:
        indexsub.resume(store, reg["reg_id"])
    assert e.value.http_status == 409
    with pytest.raises(indexsub.IndexSubError):
        indexsub.pause(store, "isub_nope")


@pytest.mark.asyncio
async def test_update_filters_partial_and_clearing(stores):
    store, index = stores
    reg = await _register(store)
    out = await indexsub.update_filters(
        store, reg["reg_id"], {"catalogue": "cdp_bazaar", "network": "eip155:8453"}
    )
    assert out["catalogue"] == "cdp_bazaar" and out["network"] == "eip155:8453"
    assert out["resource_contains"] is None  # untouched fields stay
    out = await indexsub.update_filters(store, reg["reg_id"], {"network": None})
    assert out["network"] is None and out["catalogue"] == "cdp_bazaar"
    with pytest.raises(indexsub.IndexSubError):
        await indexsub.update_filters(store, reg["reg_id"], {"pay_to": "nope"})
    with pytest.raises(indexsub.IndexSubError):
        await indexsub.update_filters(store, reg["reg_id"], {"catalogue": "payai"})
    # The updated filter governs the delivered diff.
    index.save_snapshot([_row(RES, PAY_TO)], epoch=1_000_000)
    index.save_snapshot([_row(RES, PAY_TO)], epoch=2_000_000)
    state = {"deliveries": []}
    async with _client(state) as client:
        reg_now = store.get_index_sub(reg["reg_id"])
        await indexsub.evaluate(client, store, index, reg_now, now_epoch=2_000_000)
        index.save_snapshot([_row(RES, PAY_TO, amount="99000")], epoch=3_000_000)
        out = await indexsub.evaluate(
            client, store, index, store.get_index_sub(reg["reg_id"]),
            now_epoch=3_000_000,
        )
        assert out["status"] == "delivered"
        assert out["diff"]["events"], "sliced subscription still delivers its diff"
