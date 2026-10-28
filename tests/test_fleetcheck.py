"""Fleet Check: whole-fleet sweeps for stall owners — probe
classification (OK / DEGRADED / DOWN with the specific defect),
declared-vs-live comparison against the origin's own discovery doc,
origin expansion, definition validation, and the pack mechanics
(mint idempotency, one credit per SWEEP, run-over-run diff, webhook
on change, heartbeat opt-in). Fully offline — MockTransport plays
the buyer's fleet and its webhook receiver; IP-literal URLs keep
the SSRF guard's DNS lookup local."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.services import fleetcheck, fleetpass, recheck
from app.services.recheck import PackError

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
VERDICT = ORIGIN + "/v1/verdict"
CHECK = ORIGIN + "/v1/check"
HOOK = ORIGIN + "/hook"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _challenge(url, amount="20000", pay_to=OWNER):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": url, "description": "A target service.", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
            }
        ],
    }


def _challenge_response(url, **kw) -> httpx.Response:
    challenge = _challenge(url, **kw)
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _manifest(verdict_amount="20000", check_amount="50000", check_pay_to=OWNER):
    def entry(url, price, amount, pay_to=OWNER):
        return {
            "resource": url,
            "method": "POST",
            "price": price,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": USDC,
                    "amount": amount,
                    "payTo": pay_to,
                }
            ],
        }

    return {
        "x402Version": 2,
        "resources": [
            entry(VERDICT, "$0.02", verdict_amount),
            entry(CHECK, "$0.05", check_amount, check_pay_to),
            {"resource": ORIGIN + "/quote", "method": "GET", "price": "$0.00"},
        ],
    }


def fleet_handler(state=None):
    """A two-route fleet whose /v1/check route can be flipped
    between health states; the webhook receiver records pushes."""
    state = state if state is not None else {}

    def handler(request):
        path = request.url.path
        if path == "/.well-known/x402":
            return httpx.Response(200, json=_manifest(**state.get("manifest", {})))
        if path == "/v1/verdict":
            return _challenge_response(VERDICT, amount="20000")
        if path == "/v1/check":
            mode = state.get("check_mode", "ok")
            if mode == "ok":
                return _challenge_response(CHECK, amount="50000")
            if mode == "500":
                return httpx.Response(500, text="boom")
            if mode == "200":
                return httpx.Response(200, json={"free": True})
            if mode == "bad402":
                return httpx.Response(402, text="pay me, somehow")
            if mode == "down":
                raise httpx.ConnectError("connection refused", request=request)
            raise AssertionError(mode)
        if path == "/hook":
            state.setdefault("hooks", []).append(json.loads(request.content))
            return httpx.Response(200, text="ok")
        return httpx.Response(404, text="nope")

    return handler


# --- one-shot sweep: classification ---------------------------------------------

@pytest.mark.asyncio
async def test_sweep_expands_origin_and_grades_ok():
    async with _client(fleet_handler()) as client:
        result = await fleetcheck.sweep(client, {"resources": [], "origins": [ORIGIN]})
    # The free /quote entry is not a priced resource — not swept.
    assert result["summary"] == {
        "resources": 2, "ok": 2, "degraded": 0, "down": 0,
        "origins": 1, "truncated": 0,
    }
    row = next(r for r in result["resources"] if r["url"] == VERDICT)
    assert row["verdict"] == "OK" and row["alive"] is True
    assert row["http_status"] == 402 and row["challenge_ok"] is True
    assert row["accepts"] == [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": USDC,
            "payTo": OWNER,
            "amount": "20000",
        }
    ]
    assert row["defects"] == []
    grade = result["origins"][0]
    assert grade["origin"] == ORIGIN
    assert grade["verdict"] == "TRUSTED" and grade["score"] == 100


@pytest.mark.asyncio
async def test_sweep_explicit_resources_only():
    async with _client(fleet_handler()) as client:
        result = await fleetcheck.sweep(
            client, {"resources": [VERDICT], "origins": []}
        )
    assert [r["url"] for r in result["resources"]] == [VERDICT]
    assert result["summary"]["ok"] == 1


@pytest.mark.asyncio
async def test_amount_mismatch_against_declaration():
    # The origin declares $0.05 (50000) for /v1/check; the live
    # challenge quietly asks 60000 — the exact drift Fleet Check
    # exists to catch.
    state = {"check_mode": "ok"}
    handler = fleet_handler(state)
    base = handler

    def lying_handler(request):
        if request.url.path == "/v1/check":
            return _challenge_response(CHECK, amount="60000")
        return base(request)

    async with _client(lying_handler) as client:
        result = await fleetcheck.sweep(client, {"resources": [], "origins": [ORIGIN]})
    row = next(r for r in result["resources"] if r["url"] == CHECK)
    assert row["verdict"] == "DEGRADED"
    assert [d["code"] for d in row["defects"]] == ["amount_mismatch"]
    # One OK route (full) + one DEGRADED route (half) → score 75.
    assert result["origins"][0]["score"] == 75
    assert result["origins"][0]["verdict"] == "CAUTION"


@pytest.mark.asyncio
async def test_payto_mismatch_against_declaration():
    state = {"manifest": {"check_pay_to": OTHER}}
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(client, {"resources": [], "origins": [ORIGIN]})
    row = next(r for r in result["resources"] if r["url"] == CHECK)
    assert row["verdict"] == "DEGRADED"
    assert [d["code"] for d in row["defects"]] == ["payto_mismatch"]


@pytest.mark.asyncio
async def test_defect_matrix():
    state = {"check_mode": "500"}
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(
            client,
            {
                "resources": [CHECK, ORIGIN + "/v1/missing"],
                "origins": [],
            },
        )
    by_url = {r["url"]: r for r in result["resources"]}
    assert by_url[CHECK]["verdict"] == "DOWN"
    assert by_url[CHECK]["defects"][0]["code"] == "server_error"
    assert by_url[CHECK]["alive"] is True and by_url[CHECK]["http_status"] == 500
    missing = by_url[ORIGIN + "/v1/missing"]
    assert missing["verdict"] == "DEGRADED"
    assert missing["defects"][0]["code"] == "unexpected_status"

    state["check_mode"] = "200"
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(client, {"resources": [CHECK], "origins": []})
    assert result["resources"][0]["verdict"] == "DEGRADED"
    assert result["resources"][0]["defects"][0]["code"] == "no_402"

    state["check_mode"] = "bad402"
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(client, {"resources": [CHECK], "origins": []})
    assert result["resources"][0]["verdict"] == "DEGRADED"
    assert result["resources"][0]["defects"][0]["code"] == "challenge_undecodable"

    state["check_mode"] = "down"
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(client, {"resources": [CHECK], "origins": []})
    row = result["resources"][0]
    assert row["verdict"] == "DOWN" and row["alive"] is False
    assert row["http_status"] is None
    assert row["defects"][0]["code"] == "unreachable"
    assert result["origins"][0]["verdict"] == "DOWN"


# --- definition validation --------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_definition_refusals():
    with pytest.raises(ValueError):
        await fleetcheck.validate_definition([], [])
    with pytest.raises(ValueError):
        await fleetcheck.validate_definition(
            [f"https://93.184.216.34/v1/r{i}" for i in range(41)], []
        )
    with pytest.raises(ValueError):
        await fleetcheck.validate_definition(
            [], [f"https://93.184.216.34/{i}" for i in range(11)]
        )
    # Non-public targets are refused before anything is probed.
    with pytest.raises(ValueError):
        await fleetcheck.validate_definition(["https://127.0.0.1/v1/x"], [])
    ok = await fleetcheck.validate_definition([VERDICT, VERDICT], [ORIGIN + "/"])
    assert ok == {"resources": [VERDICT], "origins": [ORIGIN]}  # deduped + normalized


# --- pack store -------------------------------------------------------------------

def _buy_pack(store, key="pay-fc-1", definition=None, **kw):
    definition = definition or {"resources": [], "origins": [ORIGIN]}
    pack, created = store.mint_fleet_check_pack(
        key, definition, kw.get("webhook_url"), kw.get("email"),
        kw.get("heartbeat", False), kw.get("interval_s", 3600),
        **{k: v for k, v in kw.items() if k in ("credits_total", "ttl_days")},
    )
    assert created
    return pack


def test_mint_fleet_check_pack_shape_and_replay(store):
    pack = _buy_pack(store)
    assert pack["pack_id"].startswith("fcpack_")
    assert len(pack["pack_id"]) == len("fcpack_") + 32
    assert pack["credits_total"] == 100 and pack["credits_remaining"] == 100
    assert pack["definition"] == {"resources": [], "origins": [ORIGIN]}
    assert pack["expired"] is False and pack["last_run_at"] is None
    replay, created = store.mint_fleet_check_pack(
        "pay-fc-1", {"resources": [], "origins": [ORIGIN]}, None, None, False, 3600
    )
    assert created is False and replay["pack_id"] == pack["pack_id"]
    assert store.fleet_check_pack_count() == 1


def test_fleet_check_pack_refusals(store):
    with pytest.raises(PackError) as e:
        store.reserve_fleet_check_credit("fcpack_nope")
    assert e.value.code == "unknown_pack" and e.value.http_status == 404

    small = _buy_pack(store, key="pay-small", credits_total=1)
    store.reserve_fleet_check_credit(small["pack_id"])
    with pytest.raises(PackError) as e:
        store.reserve_fleet_check_credit(small["pack_id"])
    assert e.value.code == "pack_exhausted" and e.value.http_status == 402

    stale = _buy_pack(store, key="pay-stale", ttl_days=0)
    with pytest.raises(PackError) as e:
        store.reserve_fleet_check_credit(stale["pack_id"])
    assert e.value.code == "pack_expired" and e.value.http_status == 410


# --- pack sweeps: debit, diff, webhook ---------------------------------------------

@pytest.mark.asyncio
async def test_pack_sweep_debits_one_credit_per_sweep_not_per_route(store):
    pack = _buy_pack(store)
    async with _client(fleet_handler()) as client:
        out = await fleetcheck.run_pack_sweep(client, store, pack)
    assert out["status"] == "swept"
    assert out["summary"]["resources"] == 2  # two routes swept...
    assert out["credits_remaining"] == 99     # ...for exactly ONE credit
    assert out["diff"]["changed"] is False
    assert sorted(out["diff"]["added"]) == sorted([VERDICT, CHECK])
    latest = store.latest_fleet_check_run(pack["pack_id"])
    assert latest is not None and latest["summary"]["ok"] == 2
    assert store.get_fleet_check_pack(pack["pack_id"])["last_run_at"] is not None


@pytest.mark.asyncio
async def test_pack_diff_webhook_on_failure_and_recovery(store):
    state = {}
    pack = _buy_pack(store, webhook_url=HOOK)
    async with _client(fleet_handler(state)) as client:
        await fleetcheck.run_pack_sweep(client, store, pack)
        assert state.get("hooks", []) == []  # baseline sweep pushes nothing

        state["check_mode"] = "500"
        out = await fleetcheck.run_pack_sweep(client, store, pack)
        assert out["webhook_push_attempted"] is True
        assert out["diff"]["new_failures"] == [
            {"url": CHECK, "from": "OK", "to": "DOWN"}
        ]
        assert len(state["hooks"]) == 1
        assert state["hooks"][0]["event"] == "fleet_check_diff"
        assert state["hooks"][0]["pack_id"] == pack["pack_id"]

        state["check_mode"] = "ok"
        out = await fleetcheck.run_pack_sweep(client, store, pack)
        assert out["diff"]["recoveries"] == [
            {"url": CHECK, "from": "DOWN", "to": "OK"}
        ]
        assert len(state["hooks"]) == 2
        assert state["hooks"][1]["event"] == "fleet_check_diff"
    assert store.get_fleet_check_pack(pack["pack_id"])["credits_remaining"] == 97
    deliveries = store.deliveries_for(pack["pack_id"])
    assert [d["event"] for d in deliveries] == ["fleet_check_diff"] * 2
    assert all(d["http_status"] == 200 for d in deliveries)


@pytest.mark.asyncio
async def test_pack_heartbeat_opt_in(store):
    state = {}
    quiet = _buy_pack(store, key="pay-quiet", webhook_url=HOOK, heartbeat=False)
    loud = _buy_pack(store, key="pay-loud", webhook_url=HOOK, heartbeat=True)
    async with _client(fleet_handler(state)) as client:
        await fleetcheck.run_pack_sweep(client, store, quiet)
        out = await fleetcheck.run_pack_sweep(client, store, quiet)
        assert out["webhook_push_attempted"] is False  # no change, no heartbeat
        await fleetcheck.run_pack_sweep(client, store, loud)
        out = await fleetcheck.run_pack_sweep(client, store, loud)
        assert out["webhook_push_attempted"] is True  # heartbeat on a quiet sweep
    # The heartbeat pack pushes on EVERY quiet sweep, including its
    # first (it proves the webhook works from day one); the quiet
    # pack never pushes.
    events = [h["event"] for h in state["hooks"]]
    assert events == ["fleet_check_heartbeat", "fleet_check_heartbeat"]


@pytest.mark.asyncio
async def test_pack_sweep_refunds_when_our_machinery_fails(store, monkeypatch):
    pack = _buy_pack(store)

    async def boom(client, definition):
        raise RuntimeError("our bug")

    monkeypatch.setattr(fleetcheck, "sweep", boom)
    async with _client(fleet_handler()) as client:
        with pytest.raises(RuntimeError):
            await fleetcheck.run_pack_sweep(client, store, pack)
    # The credit came back and no run was recorded.
    assert store.get_fleet_check_pack(pack["pack_id"])["credits_remaining"] == 100
    assert store.latest_fleet_check_run(pack["pack_id"]) is None


def test_due_fleet_check_packs(store):
    import time as _time

    pack = _buy_pack(store, interval_s=3600)
    due = store.due_fleet_check_packs()
    assert [p["pack_id"] for p in due] == [pack["pack_id"]]
    store.save_fleet_check_run(pack["pack_id"], {"resources": []}, {"changed": False})
    assert store.due_fleet_check_packs() == []
    assert [
        p["pack_id"]
        for p in store.due_fleet_check_packs(now_epoch=_time.time() + 3601)
    ] == [pack["pack_id"]]
    # An exhausted pack is never due — no free silent runs.
    spent = _buy_pack(store, key="pay-spent", credits_total=1)
    assert [p["pack_id"] for p in store.due_fleet_check_packs()] == [
        spent["pack_id"]
    ]
    store.reserve_fleet_check_credit(spent["pack_id"])
    assert store.due_fleet_check_packs() == []


# --- pricing / fleet pass table ------------------------------------------------------

def test_prices_and_pass_table():
    assert fleetcheck.ONE_SHOT_PRICE_UNITS == 100_000
    assert fleetcheck.PACK_PRICE_UNITS == 5_000_000
    assert fleetcheck.PACK_CREDITS == 100
    # A pass can pay for a sweep (a call product) but never for a
    # prepaid pack (the fleet's standing exclusion).
    assert fleetpass.price_for("stall-row", "/v1/fleet-check") == 100_000
    assert fleetpass.price_for("stall-row", "/v1/fleet-check/pack") is None


# --- HTTP surface ---------------------------------------------------------------------

def test_unpaid_challenges_are_exact():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/fleet-check", json={"origins": [ORIGIN]})
    assert r.status_code == 402
    accept = json.loads(base64.b64decode(r.headers["payment-required"]))["accepts"][0]
    assert accept["amount"] == "100000" and accept["payTo"] == OWNER

    r = client.post("/v1/fleet-check/pack", json={"origins": [ORIGIN]})
    assert r.status_code == 402
    accept = json.loads(base64.b64decode(r.headers["payment-required"]))["accepts"][0]
    assert accept["amount"] == "5000000" and accept["payTo"] == OWNER


def _fake_request(nonce="0x" + "43" * 32):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/fleet-check/pack",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "https",
        "client": ("testclient", 50000),
    }
    request = Request(scope)
    request.state.payment_payload = SimpleNamespace(
        payload=SimpleNamespace(
            authorization=SimpleNamespace(
                nonce=nonce, **{"from": "0x1111111111111111111111111111111111111111"}
            ),
            signature="0xdeadbeef",
        )
    )
    return request


def test_buy_handler_mints_and_replay_returns_same_pack(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)

    req = main.FleetCheckPackBuyRequest(origins=[ORIGIN], interval_s=3600)
    out = asyncio.run(main.fleet_check_pack_buy(req, _fake_request()))
    assert out["credits_total"] == 100 and out["credits_remaining"] == 100
    assert out["expiry_days"] == 90 and out["price_units"] == 5_000_000
    assert out["definition"] == {"resources": [], "origins": [ORIGIN]}

    replay = asyncio.run(main.fleet_check_pack_buy(req, _fake_request()))
    assert replay["pack_id"] == out["pack_id"]
    assert "already minted" in replay["note"]
    assert fresh.fleet_check_pack_count() == 1


def test_buy_handler_refuses_non_public_definition(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    req = main.FleetCheckPackBuyRequest(resources=["https://127.0.0.1/v1/x"])
    out = asyncio.run(main.fleet_check_pack_buy(req, _fake_request()))
    assert out.status_code == 422
    assert fresh.fleet_check_pack_count() == 0


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler()))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def test_http_run_and_status(http_env):
    pack, _ = http_env.mint_fleet_check_pack(
        "pay-http", {"resources": [], "origins": [ORIGIN]}, None, None, False, 3600
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post("/v1/fleet-check/run", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "swept" and body["credits_remaining"] == 99

    r = client.post("/v1/fleet-check/status", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["credits_remaining"] == 99
    assert body["latest_run"]["summary"]["ok"] == 2
    assert body["deliveries"] == []

    r = client.post("/v1/fleet-check/run", json={"pack_id": "fcpack_nope"})
    assert r.status_code == 404
    r = client.post("/v1/fleet-check/status", json={"pack_id": "fcpack_nope"})
    assert r.status_code == 404
