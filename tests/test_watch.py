"""Webhook Watch: registration validation (SSRF on both URLs,
funding must cover the watch), funded runs (pack credit / fleet-pass
debit), push-on-change delivery, and the funding_failed path.
Fully offline — MockTransport plays both the target and the webhook
receiver. IP-literal URLs keep the SSRF check offline (public IPs)."""

import httpx
import pytest

from app.services import fleetpass, recheck, watch

TARGET = "https://93.184.216.34/story"
WEBHOOK = "https://93.184.216.35/hook"
PRIVATE = "https://10.0.0.5/hook"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return httpx.Response(
            200, text=state["page"], headers={"content-type": "text/plain"}
        )

    return httpx.MockTransport(handler)


async def _register(store, **over):
    pack, _ = store.mint_pack("pay-watch", TARGET)
    kwargs = dict(
        url=TARGET,
        webhook_url=WEBHOOK,
        email=None,
        interval_s=None,
        pack_id=pack["pack_id"],
        fleet_pass_id=None,
    )
    kwargs.update(over)
    reg = await watch.register(store, **kwargs)
    return reg, pack


@pytest.mark.asyncio
async def test_register_validates_and_stores(store):
    reg, _pack = await _register(store)
    assert reg["watch_reg_id"].startswith("watch_")
    assert reg["funding_kind"] == "pack"
    assert reg["interval_s"] == watch.DEFAULT_INTERVAL_S
    assert reg["active"] is True


@pytest.mark.asyncio
async def test_register_refuses_private_urls_and_bad_funding(store):
    pack, _ = store.mint_pack("pay-x", TARGET)
    with pytest.raises(watch.WatchError):
        await watch.register(
            store, PRIVATE, WEBHOOK, None, None, pack["pack_id"], None
        )
    with pytest.raises(watch.WatchError):
        await watch.register(
            store, TARGET, PRIVATE, None, None, pack["pack_id"], None
        )
    with pytest.raises(watch.WatchError) as e:
        await watch.register(store, TARGET, WEBHOOK, None, None, "pack_nope", None)
    assert e.value.http_status == 404
    with pytest.raises(watch.WatchError):
        await watch.register(store, TARGET, WEBHOOK, None, None, None, None)
    # Interval bounds come from the route model too, but the service
    # refuses out-of-range values on its own.
    with pytest.raises(watch.WatchError):
        await watch.register(
            store, TARGET, WEBHOOK, None, 60, pack["pack_id"], None
        )


@pytest.mark.asyncio
async def test_pack_funded_run_pushes_baseline_then_change_only(store):
    reg, pack = await _register(store)
    state = {"page": "version one", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        assert out["webhook_push_attempted"] is True
        assert len(state["deliveries"]) == 1
        # One pack credit spent for the delivered check.
        assert store.get_pack(pack["pack_id"])["credits_remaining"] == 249

        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "UNCHANGED"
        assert out["webhook_push_attempted"] is False
        assert len(state["deliveries"]) == 1

        state["page"] = "version two"
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "CHANGED"
        assert len(state["deliveries"]) == 2
    deliveries = store.deliveries_for(reg["watch_reg_id"])
    assert [d["event"] for d in deliveries] == ["BASELINE", "CHANGED"]
    assert all(d["http_status"] == 200 for d in deliveries)


@pytest.mark.asyncio
async def test_fleet_pass_funded_run_debits_recheck_price(store):
    fleet_pass, _ = store.mint_pass(
        "pay-fp", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, None, fleet_pass["pass_id"]
    )
    state = {"page": "v1", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "BASELINE"
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 4_995_000


@pytest.mark.asyncio
async def test_exhausted_funding_marks_funding_failed(store):
    pack, _ = store.mint_pack("pay-tiny", TARGET, credits_total=1)
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, pack["pack_id"], None
    )
    state = {"page": "v1", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "funding_failed"
    assert out["code"] == "pack_exhausted"
    assert store.get_watch_reg(reg["watch_reg_id"])["last_status"] == "funding_failed"
    # Nothing pushed for the unfunded run.
    assert len(state["deliveries"]) == 1
