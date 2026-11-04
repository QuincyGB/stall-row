"""Heartbeat Watch (Next-20 #9): liveness-only standing watch.

Pins:
  * knock classification reuses Fleet Check's classifier: a proper
    402 challenge is up, an HTTP 200 (no payment gate) is degraded,
    a 5xx is down;
  * alerts fire ONLY on transitions (baseline counts) — a steady
    second run pushes nothing;
  * every knock debits exactly $0.001 (1000 units) from the funding
    credential — pass balance arithmetic, and fractional-credit
    pack arithmetic (1000 units < one 4000-unit credit);
  * an unfundable knock pauses the registration (funding_failed)
    and probes nothing further; a pack can only fund the single
    resource it is bound to;
  * registration is free and not a priced route.
"""

import base64
import json

import httpx
import pytest

from app.services import fleetpass, heartbeat, recheck

TARGET = "https://93.184.216.34/v1/thing"
TARGET2 = "https://93.184.216.34/v1/other"
WEBHOOK = "https://93.184.216.35/hook"
OWNER = "0x" + "11" * 20
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _challenge_response(url, amount="5000"):
    challenge = {
        "x402Version": 2,
        "resource": {"url": url, "type": "http"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": amount,
                "payTo": OWNER,
            }
        ],
    }
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _transport(state):
    """state['mode'] per target host behaviour: 'ok' (402), 'open'
    (200), 'down' (500). Webhook deliveries are recorded."""

    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        state["probes"].append(str(request.url))
        mode = state["mode"]
        if mode == "ok":
            return _challenge_response(str(request.url))
        if mode == "open":
            return httpx.Response(200, text="free for all")
        if mode == "down":
            return httpx.Response(500, text="boom")
        raise AssertionError(mode)

    return httpx.MockTransport(handler)


async def _register_pass(store, **over):
    fleet_pass, _ = store.mint_pass(
        "pay-hb", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    kwargs = dict(
        resources=[TARGET, TARGET2],
        webhook_url=WEBHOOK,
        email=None,
        interval_s=None,
        pack_id=None,
        fleet_pass_id=fleet_pass["pass_id"],
    )
    kwargs.update(over)
    reg = await heartbeat.register(store, **kwargs)
    return reg, fleet_pass


@pytest.mark.asyncio
async def test_register_shape_and_price(store):
    reg, _ = await _register_pass(store)
    assert reg["reg_id"].startswith("hb_")
    assert reg["knock_price_units"] == 1000
    assert reg["funding_kind"] == "fleet_pass"
    assert reg["delivery_mode"] == "instant"
    assert reg["resources"] == [TARGET, TARGET2]


@pytest.mark.asyncio
async def test_knock_classification(store):
    state = {"mode": "ok", "deliveries": [], "probes": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.knock(client, TARGET)
        assert out["state"] == "up" and out["challenge_ok"] is True
        state["mode"] = "open"
        out = await heartbeat.knock(client, TARGET)
        assert out["state"] == "degraded" and out["defect"] == "no_402"
        state["mode"] = "down"
        out = await heartbeat.knock(client, TARGET)
        assert out["state"] == "down" and out["defect"] == "server_error"


@pytest.mark.asyncio
async def test_transitions_only_and_exact_debit(store):
    reg, fleet_pass = await _register_pass(store)
    state = {"mode": "ok", "deliveries": [], "probes": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.run_reg(client, store, reg)
        assert out["status"] == "transitions"
        assert out["debited_units"] == 2000  # 2 knocks x 1000
        assert len(state["deliveries"]) == 2  # two baselines
        assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == (
            fleetpass.PASS_PRICE_UNITS - 2000
        )
        # Steady state: knocks still debit, but nothing pushes.
        out = await heartbeat.run_reg(client, store, reg)
        assert out["status"] == "steady"
        assert out["debited_units"] == 2000
        assert len(state["deliveries"]) == 2
        # One target flips down: exactly one transition push.
        state["mode"] = "down"
        out = await heartbeat.run_reg(client, store, reg)
        assert out["status"] == "transitions"
        assert out["events_count"] == 2  # both targets flipped
        assert len(state["deliveries"]) == 4
        body = json.loads(state["deliveries"][-1].content)
        assert body["event"] == "heartbeat_transition"
        assert body["from_state"] == "up" and body["to_state"] == "down"
        assert body["debited_units"] == 1000


@pytest.mark.asyncio
async def test_unfunded_pauses_and_probes_nothing(store):
    fleet_pass, _ = store.mint_pass("pay-hb2", 1500, fleetpass.PASS_TTL_DAYS)
    reg = await heartbeat.register(
        store, [TARGET, TARGET2], WEBHOOK, None, None, None, fleet_pass["pass_id"]
    )
    state = {"mode": "ok", "deliveries": [], "probes": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.run_reg(client, store, reg)
        # First knock funded (1500 -> 500); the second cannot be.
        assert out["status"] == "funding_failed"
        assert out["debited_units"] == 1000
        assert len(state["probes"]) == 1  # second target never probed
        fresh = store.get_heartbeat_reg(reg["reg_id"])
        assert fresh["active"] is False
        assert fresh["last_status"] == "funding_failed"
        # Balance never went negative.
        assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 500
        # A paused registration charges and probes nothing.
        out = await heartbeat.run_reg(client, store, reg)
        assert out["debited_units"] == 0 and len(state["probes"]) == 1


@pytest.mark.asyncio
async def test_pack_funding_is_single_resource_and_fractional(store):
    pack, _ = store.mint_pack("pay-hb3", TARGET)
    with pytest.raises(heartbeat.HeartbeatError):
        await heartbeat.register(
            store, [TARGET, TARGET2], WEBHOOK, None, None, pack["pack_id"], None
        )
    reg = await heartbeat.register(
        store, [TARGET], WEBHOOK, None, None, pack["pack_id"], None
    )
    state = {"mode": "ok", "deliveries": [], "probes": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.run_reg(client, store, reg)
        assert out["debited_units"] == 1000
    # 1000 units = a quarter credit: 249 whole credits + 3000-unit
    # remainder, spendable exactly 999000 more units.
    assert store.pack_spendable_units(pack["pack_id"]) == 1_000_000 - 1000
    view = store.get_pack(pack["pack_id"])
    assert view["credits_remaining"] == 249


@pytest.mark.asyncio
async def test_register_refusals(store):
    fleet_pass, _ = store.mint_pass(
        "pay-hb4", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    with pytest.raises(heartbeat.HeartbeatError):
        await heartbeat.register(
            store, [], WEBHOOK, None, None, None, fleet_pass["pass_id"]
        )
    with pytest.raises(heartbeat.HeartbeatError):
        await heartbeat.register(
            store, [TARGET], None, None, None, None, fleet_pass["pass_id"]
        )
    with pytest.raises(heartbeat.HeartbeatError) as e:
        await heartbeat.register(
            store, [TARGET], WEBHOOK, None, None, None, "fpass_nope"
        )
    assert e.value.http_status == 404
    with pytest.raises(heartbeat.HeartbeatError):
        await heartbeat.register(
            store, ["https://10.0.0.5/x"], WEBHOOK, None, None, None,
            fleet_pass["pass_id"],
        )


def test_heartbeat_routes_are_not_priced():
    from app.config import PRICE_UNITS

    for path in (
        "/v1/heartbeat/register",
        "/v1/heartbeat/status",
        "/v1/heartbeat/run",
        "/v1/heartbeat/pause",
        "/v1/heartbeat/resume",
        "/v1/heartbeat/cancel",
    ):
        assert path not in PRICE_UNITS
