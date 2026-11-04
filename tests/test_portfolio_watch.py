"""Portfolio Watch (Next-20 #11): one registration per operator
family, expanded from the Index.

Pins:
  * registration expands the family from the Index's CURRENT
    snapshot (service_name match; origin fallback) and states the
    expansion behaviour in the payload;
  * a route ADDED to the family after registration joins the next
    evaluation automatically (new_routes named in the payload) and
    is watched under the same one balance / one webhook;
  * heartbeat tier debits exactly $0.001 per route knock from the
    family's one Fleet Pass; digest tier baselines free, then
    debits $0.005 once per evaluation that carries family changes;
  * one family-level delivery per run, not one per route;
  * registration is free and not a priced route.
"""

import base64
import json

import httpx
import pytest

from app.services import fleetpass, indexfeed, portfoliowatch, recheck

HOST = "https://93.184.216.34"
R1 = f"{HOST}/v1/alpha"
R2 = f"{HOST}/v1/beta"
R3 = f"{HOST}/v1/gamma"
OTHER = "https://93.184.216.77/v1/zeta"
WEBHOOK = "https://93.184.216.35/hook"
OWNER = "0x" + "22" * 20
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAMILY = "Acme LLM"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def index(tmp_path):
    return indexfeed.IndexStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _row(resource, name=FAMILY, pay_to=OWNER, amount="5000"):
    return {
        "resource": resource,
        "serviceName": name,
        "description": "a route",
        "amount": amount,
        "asset": USDC,
        "network": "eip155:8453",
        "payTo": pay_to,
        "calls30d": 10,
    }


def _challenge_response(url):
    challenge = {
        "x402Version": 2,
        "resource": {"url": url, "type": "http"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": "5000",
                "payTo": OWNER,
            }
        ],
    }
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return _challenge_response(str(request.url))

    return httpx.MockTransport(handler)


async def _register(store, index, **over):
    fleet_pass, _ = store.mint_pass(
        "pay-pf", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    kwargs = dict(
        operator=FAMILY,
        tier="heartbeat",
        webhook_url=WEBHOOK,
        email=None,
        interval_s=None,
        fleet_pass_id=fleet_pass["pass_id"],
    )
    kwargs.update(over)
    reg = await portfoliowatch.register(store, index, **kwargs)
    return reg, fleet_pass


@pytest.mark.asyncio
async def test_family_expansion_at_registration(store, index):
    index.save_snapshot([_row(R1), _row(R2), _row(OTHER, name="Someone Else")], epoch=1_000)
    reg, _ = await _register(store, index)
    assert reg["reg_id"].startswith("pf_")
    assert reg["match_kind"] == "service_name"
    assert reg["routes"] == [R1, R2]
    assert "re-expands" in reg["family_expansion"]
    assert reg["rates"]["heartbeat_knock_units"] == 1000


@pytest.mark.asyncio
async def test_route_added_after_registration_joins(store, index):
    index.save_snapshot([_row(R1), _row(R2)], epoch=1_000)
    reg, fleet_pass = await _register(store, index)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await portfoliowatch.run_reg(client, store, index, reg)
        assert out["routes_count"] == 2
        assert out["debited_units"] == 2000  # 2 knocks x $0.001
        # The family grows: a third route appears in the Index.
        index.save_snapshot([_row(R1), _row(R2), _row(R3)], epoch=2_000)
        out = await portfoliowatch.run_reg(client, store, index, reg)
        assert out["routes_count"] == 3
        assert out["new_routes"] == [R3]
        assert out["debited_units"] == 3000
        fresh = store.get_portfolio_reg(reg["reg_id"])
        assert fresh["routes"] == [R1, R2, R3]
        assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == (
            fleetpass.PASS_PRICE_UNITS - 5000
        )


@pytest.mark.asyncio
async def test_one_family_level_delivery(store, index):
    index.save_snapshot([_row(R1), _row(R2)], epoch=1_000)
    reg, _ = await _register(store, index)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await portfoliowatch.run_reg(client, store, index, reg)
        # Two baseline transitions, ONE family payload.
        assert len(state["deliveries"]) == 1
        body = json.loads(state["deliveries"][0].content)
        assert body["event"] == "portfolio_watch"
        assert body["routes_count"] == 2
        assert len(body["events"]) == 2
        assert body["debited_units"] == 2000
        # Steady state: knocks debit, nothing delivers.
        out = await portfoliowatch.run_reg(client, store, index, reg)
        assert len(state["deliveries"]) == 1


@pytest.mark.asyncio
async def test_digest_tier_baseline_then_change(store, index):
    index.save_snapshot([_row(R1)], epoch=1_000)
    reg, fleet_pass = await _register(store, index, tier="digest")
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await portfoliowatch.run_reg(client, store, index, reg)
        assert out["status"] == "baseline" and out["debited_units"] == 0
        index.save_snapshot(
            [_row(R1, pay_to="0x" + "33" * 20), _row(R2)], epoch=2_000
        )
        out = await portfoliowatch.run_reg(client, store, index, reg)
        assert out["debited_units"] == 5000  # one evaluation, not per route
        assert out["events_count"] >= 1
        assert len(state["deliveries"]) == 1


@pytest.mark.asyncio
async def test_empty_family_registers_and_origin_match(store, index):
    index.save_snapshot([_row(R1)], epoch=1_000)
    reg, _ = await _register(store, index, operator="No Such Operator")
    assert reg["match_kind"] == "none" and reg["routes"] == []
    reg2, _ = await _register(store, index, operator=HOST)
    assert reg2["match_kind"] == "origin" and reg2["routes"] == [R1]
    with pytest.raises(portfoliowatch.PortfolioError):
        await _register(store, index, tier="deluxe")


def test_portfolio_routes_are_not_priced():
    from app.config import PRICE_UNITS

    for path in (
        "/v1/portfolio-watch/register",
        "/v1/portfolio-watch/status",
        "/v1/portfolio-watch/run",
        "/v1/portfolio-watch/pause",
        "/v1/portfolio-watch/resume",
        "/v1/portfolio-watch/cancel",
    ):
        assert path not in PRICE_UNITS
