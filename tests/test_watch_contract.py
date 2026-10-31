"""Contract Watch (watch_focus="schema", Next-20 #14): a watch
registration whose detector keys ONLY on the target's output-schema
fingerprint (the recheck fingerprint's schema_hash — the declared
output schema in the target's unpaid 402 challenge). A schema drift
with flat price/payTo pushes SCHEMA_CHANGED; other-field changes
alone push nothing. Funding mirrors the recheck leg exactly (pack
credit / $0.005 Fleet Pass debit per run, funding_failed on a dry
credential). Fully offline — MockTransport plays both the x402
target and the webhook receiver. IP-literal URLs keep the SSRF
check offline (public IPs)."""

import base64
import hashlib
import json

import httpx
import pytest

from app.services import fleetpass, recheck, watch

TARGET = "https://93.184.216.34/contract"
WEBHOOK = "https://93.184.216.35/hook"
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

SCHEMA_V1 = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
}
SCHEMA_V2 = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "score": {"type": "number"},
    },
    "required": ["verdict", "score"],
}


def _schema_hash(schema):
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True).encode("utf-8")
    ).hexdigest()


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _challenge(state):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": TARGET,
            "description": "A target service.",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": state["amount"],
                "payTo": OWNER,
                "maxTimeoutSeconds": 300,
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {
                        "type": "http",
                        "method": "POST",
                        "bodyType": "json",
                        "body": {},
                    },
                    "output": {"type": "json", "schema": state["schema"]},
                }
            }
        },
    }


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(json.loads(request.content))
            return httpx.Response(200, text="ok")
        challenge = _challenge(state)
        raw = base64.b64encode(json.dumps(challenge).encode()).decode()
        return httpx.Response(
            402, headers={"payment-required": raw}, json=challenge
        )

    return httpx.MockTransport(handler)


async def _register(store, **over):
    pack, _ = store.mint_pack("pay-contract", TARGET)
    kwargs = dict(
        url=TARGET,
        webhook_url=WEBHOOK,
        email=None,
        interval_s=None,
        pack_id=pack["pack_id"],
        fleet_pass_id=None,
        watch_focus="schema",
    )
    kwargs.update(over)
    reg = await watch.register(store, **kwargs)
    return reg, pack


@pytest.mark.asyncio
async def test_register_schema_focus_validates(store):
    reg, _pack = await _register(store)
    assert reg["watch_focus"] == "schema"
    assert reg["check_kind"] == "recheck"
    assert "Contract Watch" in reg["note"]
    # A bad focus is a 422, mirroring check_kind validation.
    with pytest.raises(watch.WatchError) as e:
        await _register(store, watch_focus="price")
    assert e.value.http_status == 422
    # Contract Watch is recheck-leg only: the RouteVerdict leg has
    # no schema fingerprint to key on.
    fleet_pass, _ = store.mint_pass(
        "pay-fp", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    with pytest.raises(watch.WatchError) as e:
        await watch.register(
            store, TARGET, WEBHOOK, None, None, None,
            fleet_pass["pass_id"], check_kind="route_verdict",
            watch_focus="schema",
        )
    assert e.value.http_status == 422
    # The new field parses through the existing funding validation:
    # an unknown pass is the existing 404.
    with pytest.raises(watch.WatchError) as e:
        await watch.register(
            store, TARGET, WEBHOOK, None, None, None, "pass_nope",
            watch_focus="schema",
        )
    assert e.value.http_status == 404


@pytest.mark.asyncio
async def test_schema_change_with_flat_terms_pushes_schema_changed(store):
    reg, pack = await _register(store)
    state = {"schema": SCHEMA_V1, "amount": "20000", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        assert out["webhook_push_attempted"] is True
        assert out["schema_fingerprint"] == {
            "before": None,
            "after": _schema_hash(SCHEMA_V1),
        }
        assert len(state["deliveries"]) == 1
        base = state["deliveries"][0]
        assert base["event"] == "BASELINE"
        assert base["schema_fingerprint"]["after"] == _schema_hash(SCHEMA_V1)
        assert store.get_pack(pack["pack_id"])["credits_remaining"] == 249

        # Identical schema: no push.
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "UNCHANGED"
        assert out["webhook_push_attempted"] is False
        assert len(state["deliveries"]) == 1

        # THE MONEY TEST: fields renamed/retyped in the declared
        # output schema while amount and payTo stay exactly flat.
        state["schema"] = SCHEMA_V2
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "SCHEMA_CHANGED"
        assert out["recheck_status"] == "CHANGED"
        assert out["webhook_push_attempted"] is True
        assert len(state["deliveries"]) == 2
    push = state["deliveries"][1]
    assert push["event"] == "SCHEMA_CHANGED"
    assert push["url"] == TARGET
    assert push["watch_focus"] == "schema"
    assert push["checked_at"]
    assert push["schema_fingerprint"] == {
        "before": _schema_hash(SCHEMA_V1),
        "after": _schema_hash(SCHEMA_V2),
    }
    assert push["changes"] == [
        {
            "field": "schema_hash",
            "before": _schema_hash(SCHEMA_V1),
            "after": _schema_hash(SCHEMA_V2),
        }
    ]
    assert push["field_changes"] == [
        {
            "path": "schema_hash",
            "op": "value_changed",
            "before": _schema_hash(SCHEMA_V1),
            "after": _schema_hash(SCHEMA_V2),
            "severity": "medium",
            "category": "schema",
        }
    ]
    deliveries = store.deliveries_for(reg["watch_reg_id"])
    assert [d["event"] for d in deliveries] == ["BASELINE", "SCHEMA_CHANGED"]
    assert store.get_watch_reg(reg["watch_reg_id"])["last_status"] == (
        "SCHEMA_CHANGED"
    )


@pytest.mark.asyncio
async def test_non_schema_change_alone_pushes_nothing(store):
    reg, _pack = await _register(store)
    state = {"schema": SCHEMA_V1, "amount": "20000", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        # Price moves, schema flat: the underlying recheck reports
        # CHANGED, but a Contract Watch stays silent.
        state["amount"] = "30000"
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "UNCHANGED"
        assert out["recheck_status"] == "CHANGED"
        assert out["webhook_push_attempted"] is False
        assert len(state["deliveries"]) == 1
        # And the stored baseline advanced past the price move: a
        # later schema drift still alerts against the v1 fingerprint.
        state["schema"] = SCHEMA_V2
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "SCHEMA_CHANGED"
        assert out["schema_fingerprint"]["before"] == _schema_hash(SCHEMA_V1)


@pytest.mark.asyncio
async def test_fleet_pass_funded_contract_watch_debits_recheck_price(store):
    fleet_pass, _ = store.mint_pass(
        "pay-fp", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, None,
        fleet_pass["pass_id"], watch_focus="schema",
    )
    state = {"schema": SCHEMA_V1, "amount": "20000", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        state["schema"] = SCHEMA_V2
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "SCHEMA_CHANGED"
    # Two runs, two $0.005 recheck debits — no separate price.
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 4_990_000


@pytest.mark.asyncio
async def test_unfundable_run_marks_funding_failed_and_pushes_nothing(store):
    pack, _ = store.mint_pack("pay-tiny", TARGET, credits_total=1)
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, pack["pack_id"], None,
        watch_focus="schema",
    )
    state = {"schema": SCHEMA_V1, "amount": "20000", "deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        state["schema"] = SCHEMA_V2
        out = await watch.run_watch(client, store, reg)
    assert out["status"] == "funding_failed"
    assert out["code"] == "pack_exhausted"
    fresh = store.get_watch_reg(reg["watch_reg_id"])
    assert fresh["last_status"] == "funding_failed"
    # The failed run neither pushed nor overwrote the baseline.
    assert len(state["deliveries"]) == 1
    assert fresh["last_summary"] == {
        "schema_fingerprint": _schema_hash(SCHEMA_V1)
    }
