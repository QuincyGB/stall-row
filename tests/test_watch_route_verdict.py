"""Webhook Watch — RouteVerdict leg (check_kind="route_verdict").

Registration is Fleet-Pass-only (a recheck pack is refused 422),
registration validates the pass covers one $0.05 RouteVerdict check,
and a run POSTs the watched URL to RouteVerdict with the pass as the
X-Fleet-Pass header — RouteVerdict debits the pass itself, so
StallRow must record NO local debit. Change detection is ours,
against the summary stored on the registration: BASELINE first,
CHANGED on a verdict-band flip / payment-term change / score move
>= 10, otherwise UNCHANGED with no push. Fully offline —
MockTransport routes by host: RouteVerdict, the webhook receiver,
and (never for this leg) the target.
"""

import json

import httpx
import pytest

from app.services import fleetpass, recheck, watch

TARGET = "https://93.184.216.34/story"
WEBHOOK = "https://93.184.216.35/hook"
RV_HOST = "route-verdict-production.up.railway.app"
PAY_TO = "0x1111111111111111111111111111111111111111"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _rv_body(state):
    return {
        "resource_url": TARGET,
        "verdict": state["verdict"],
        "score": state["score"],
        "checked_at": "2026-10-30T12:00:00Z",
        "terms": {
            "found": True,
            "amount_units": state["amount"],
            "pay_to": state["pay_to"],
            "network": state["network"],
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "price_usdc": "$0.005",
        },
    }


def _transport(state):
    def handler(request):
        host = request.url.host
        if host == RV_HOST:
            state["rv_requests"].append(request)
            if state.get("rv_raise"):
                raise httpx.ConnectError("rv unreachable", request=request)
            if state["rv_status"] != 200:
                return httpx.Response(
                    state["rv_status"],
                    json={"error": state.get("rv_error", "rv failure")},
                )
            return httpx.Response(200, json=_rv_body(state))
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


def _state(**over):
    state = {
        "verdict": "TRUSTED",
        "score": 90.0,
        "amount": "5000",
        "pay_to": PAY_TO,
        "network": "eip155:8453",
        "rv_status": 200,
        "rv_requests": [],
        "deliveries": [],
    }
    state.update(over)
    return state


async def _register_rv(store, pass_units=fleetpass.PASS_PRICE_UNITS):
    fleet_pass, _ = store.mint_pass(
        "pay-rv", pass_units, fleetpass.PASS_TTL_DAYS
    )
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, None, fleet_pass["pass_id"],
        check_kind="route_verdict",
    )
    return reg, fleet_pass


# --- registration -------------------------------------------------------


@pytest.mark.asyncio
async def test_register_route_verdict_with_pack_is_422(store):
    pack, _ = store.mint_pack("pay-watch", TARGET)
    with pytest.raises(watch.WatchError) as e:
        await watch.register(
            store, TARGET, WEBHOOK, None, None, pack["pack_id"], None,
            check_kind="route_verdict",
        )
    assert e.value.http_status == 422
    assert "Recheck credits" in e.value.message
    assert "Fleet Pass" in e.value.message


@pytest.mark.asyncio
async def test_register_route_verdict_unknown_pass_is_404(store):
    with pytest.raises(watch.WatchError) as e:
        await watch.register(
            store, TARGET, WEBHOOK, None, None, None, "fpass_nope",
            check_kind="route_verdict",
        )
    assert e.value.http_status == 404


@pytest.mark.asyncio
async def test_register_route_verdict_low_balance_pass_is_402(store):
    # 40000 < price_for("route-verdict", "/v1/check") == 50000.
    low, _ = store.mint_pass("pay-low", 40_000, fleetpass.PASS_TTL_DAYS)
    with pytest.raises(watch.WatchError) as e:
        await watch.register(
            store, TARGET, WEBHOOK, None, None, None, low["pass_id"],
            check_kind="route_verdict",
        )
    assert e.value.http_status == 402
    assert fleetpass.price_for("route-verdict", "/v1/check") == 50_000


@pytest.mark.asyncio
async def test_register_route_verdict_stores_kind_and_note(store):
    reg, _fleet_pass = await _register_rv(store)
    assert reg["check_kind"] == "route_verdict"
    assert reg["funding_kind"] == "fleet_pass"
    assert "route_verdict" in reg["note"]
    assert "$0.05" in reg["note"]
    stored = store.get_watch_reg(reg["watch_reg_id"])
    assert stored["check_kind"] == "route_verdict"
    assert stored["last_summary"] is None


@pytest.mark.asyncio
async def test_register_recheck_default_kind_unchanged(store):
    fleet_pass, _ = store.mint_pass(
        "pay-fp", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, None, fleet_pass["pass_id"]
    )
    assert reg["check_kind"] == "recheck"
    assert store.get_watch_reg(reg["watch_reg_id"])["check_kind"] == "recheck"


# --- runs ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_baseline_pushes_and_never_debits_locally(store):
    reg, fleet_pass = await _register_rv(store)
    state = _state()
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "BASELINE"
    assert out["check_kind"] == "route_verdict"
    assert out["webhook_push_attempted"] is True
    # The pass reached RouteVerdict as the X-Fleet-Pass header, with
    # the watched URL as the target resource.
    assert len(state["rv_requests"]) == 1
    rv_req = state["rv_requests"][0]
    assert rv_req.headers["x-fleet-pass"] == fleet_pass["pass_id"]
    assert json.loads(rv_req.content) == {"resource_url": TARGET}
    # StallRow recorded NO local debit: RouteVerdict debits the pass
    # itself, so the ledger here is untouched.
    assert store.pass_debit_count(fleet_pass["pass_id"]) == 0
    assert (
        store.get_pass(fleet_pass["pass_id"])["balance_units"]
        == fleetpass.PASS_PRICE_UNITS
    )
    # BASELINE pushed with the route_verdict shape.
    assert len(state["deliveries"]) == 1
    payload = json.loads(state["deliveries"][0].content)
    assert payload["event"] == "BASELINE"
    assert payload["kind"] == "route_verdict"
    assert payload["url"] == TARGET
    assert payload["current"] == {
        "verdict": "TRUSTED",
        "score": 90.0,
        "amount": "5000",
        "pay_to": PAY_TO,
        "network": "eip155:8453",
    }
    assert payload["checked_at"] == "2026-10-30T12:00:00Z"
    stored = store.get_watch_reg(reg["watch_reg_id"])
    assert stored["last_status"] == "BASELINE"
    assert stored["last_summary"] == payload["current"]


@pytest.mark.asyncio
async def test_change_detection_thresholds_and_verdict_flip(store):
    reg, _fleet_pass = await _register_rv(store)
    state = _state()
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"

        # Identical summary -> UNCHANGED, no push.
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "UNCHANGED"
        assert out["webhook_push_attempted"] is False
        assert len(state["deliveries"]) == 1

        # Score move of 5 (< 10) -> UNCHANGED, no push.
        state["score"] = 85.0
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "UNCHANGED"
        assert len(state["deliveries"]) == 1

        # Score move of 15 (>= 10) -> CHANGED with the score diff.
        state["score"] = 70.0
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "CHANGED"
        assert out["score_change_threshold"] == 10
        assert {"field": "score", "before": 85.0, "after": 70.0} in out["changes"]
        assert len(state["deliveries"]) == 2
        payload = json.loads(state["deliveries"][1].content)
        assert payload["event"] == "CHANGED"
        assert payload["kind"] == "route_verdict"
        assert {"field": "score", "before": 85.0, "after": 70.0} in payload["changes"]

        # Verdict band flip -> CHANGED with the verdict diff.
        state["verdict"] = "AVOID"
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "CHANGED"
        assert {"field": "verdict", "before": "TRUSTED", "after": "AVOID"} in out[
            "changes"
        ]
        assert len(state["deliveries"]) == 3

        # Payment-term change -> CHANGED with the term diff.
        state["pay_to"] = "0x2222222222222222222222222222222222222222"
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "CHANGED"
        assert {
            "field": "pay_to",
            "before": PAY_TO,
            "after": "0x2222222222222222222222222222222222222222",
        } in out["changes"]
    deliveries = store.deliveries_for(reg["watch_reg_id"])
    assert [d["event"] for d in deliveries] == [
        "BASELINE",
        "CHANGED",
        "CHANGED",
        "CHANGED",
    ]


@pytest.mark.asyncio
async def test_route_verdict_402_marks_funding_failed_no_push(store):
    reg, _fleet_pass = await _register_rv(store)
    state = _state(rv_status=402, rv_error="fleet pass balance too low")
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "funding_failed"
    assert len(state["deliveries"]) == 0
    stored = store.get_watch_reg(reg["watch_reg_id"])
    assert stored["last_status"] == "funding_failed"
    assert stored["last_summary"] is None


@pytest.mark.asyncio
async def test_route_verdict_500_is_probe_failed_no_push(store):
    reg, _fleet_pass = await _register_rv(store)
    state = _state(rv_status=500)
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "probe_failed"
    assert len(state["deliveries"]) == 0
    assert store.get_watch_reg(reg["watch_reg_id"])["last_status"] == "probe_failed"


@pytest.mark.asyncio
async def test_route_verdict_transport_failure_is_probe_failed(store):
    reg, _fleet_pass = await _register_rv(store)
    state = _state(rv_raise=True)
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "probe_failed"
    assert len(state["deliveries"]) == 0
