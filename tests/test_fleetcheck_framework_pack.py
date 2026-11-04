"""Fleet Check Framework Packs (Next-20 Item 13): framework_log
run-log entries on every Fleet Check delivery shape (single sweep,
free sweep, pack run, pack status) and renewal terms derived from
the FLEET_CHECK_PACK_* constants on pack purchase/status payloads.

Additive only: the original resources table, summary, diff and
webhook behaviour are untouched — the pack-diff webhook test in
test_fleetcheck.py still pins those. Fully offline, MockTransport,
$0 spent (same harness shape as test_fleetcheck.py)."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.services import fleetcheck, recheck

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
VERDICT = ORIGIN + "/v1/verdict"
CHECK = ORIGIN + "/v1/check"


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


def _manifest():
    def entry(url, price, amount):
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
                    "payTo": OWNER,
                }
            ],
        }

    return {
        "x402Version": 2,
        "resources": [
            entry(VERDICT, "$0.02", "20000"),
            entry(CHECK, "$0.05", "50000"),
        ],
    }


def fleet_handler(state=None):
    state = state if state is not None else {}

    def handler(request):
        path = request.url.path
        if path == "/.well-known/x402":
            return httpx.Response(200, json=_manifest())
        if path == "/v1/verdict":
            return _challenge_response(VERDICT, amount="20000")
        if path == "/v1/check":
            if state.get("check_mode") == "500":
                return httpx.Response(500, text="boom")
            return _challenge_response(CHECK, amount="50000")
        return httpx.Response(404, text="nope")

    return handler


# --- framework_log: pure shape ---------------------------------------------------


def test_framework_log_entry_shape_for_ok_and_defect_rows():
    rows = [
        {
            "url": VERDICT, "verdict": "OK", "http_status": 402,
            "defects": [],
        },
        {
            "url": CHECK, "verdict": "DOWN", "http_status": 500,
            "defects": [{"code": "server_error", "detail": "target returned HTTP 500"}],
        },
    ]
    log = fleetcheck.framework_log_for_rows(rows, "2026-11-04T00:00:00Z")
    assert len(log) == 2
    ok, down = log
    assert ok["tool"] == "fleet_check_route_probe"
    assert ok["action"] == "fleet_check_route_probe"
    assert ok["resource"] == VERDICT
    assert ok["verdict"] == "OK"
    assert ok["defect"] is None and ok["defects"] == []
    assert ok["checked_at"] == "2026-11-04T00:00:00Z"
    assert VERDICT in ok["message"] and "OK" in ok["message"]
    assert "\n" not in ok["message"]  # one-line human message
    assert down["verdict"] == "DOWN"
    assert down["defect"] == "server_error"
    assert down["defects"] == ["server_error"]
    assert "server_error" in down["message"]


# --- framework_log: sweep deliveries ----------------------------------------------


@pytest.mark.asyncio
async def test_single_sweep_result_carries_framework_log():
    state = {"check_mode": "500"}
    async with _client(fleet_handler(state)) as client:
        result = await fleetcheck.sweep(
            client, {"resources": [], "origins": [ORIGIN]}
        )
    # Original table untouched and still present.
    assert result["summary"]["resources"] == 2
    log = result["framework_log"]
    assert [e["resource"] for e in log] == [r["url"] for r in result["resources"]]
    by_url = {e["resource"]: e for e in log}
    assert by_url[VERDICT]["verdict"] == "OK"
    assert by_url[CHECK]["verdict"] == "DOWN"
    assert by_url[CHECK]["defect"] == "server_error"
    assert all(e["checked_at"] == result["checked_at"] for e in log)


@pytest.mark.asyncio
async def test_pack_run_result_carries_framework_log_and_renewal(store):
    pack, created = store.mint_fleet_check_pack(
        "pay-fx-1", {"resources": [], "origins": [ORIGIN]}, None, None, False, 3600
    )
    assert created
    async with _client(fleet_handler()) as client:
        out = await fleetcheck.run_pack_sweep(client, store, pack)
    assert out["status"] == "swept"
    assert [e["verdict"] for e in out["framework_log"]] == ["OK", "OK"]
    assert out["renewal"]["renewal_cycle_days"] == recheck.FLEET_CHECK_PACK_TTL_DAYS
    assert out["renewal"]["renewal_price_units"] == recheck.FLEET_CHECK_PACK_PRICE_UNITS


# --- renewal terms: derived from the constants -------------------------------------


def test_pack_view_renewal_terms_derive_from_constants(store):
    pack, _ = store.mint_fleet_check_pack(
        "pay-fx-2", {"resources": [], "origins": [ORIGIN]}, None, None, False, 3600
    )
    renewal = pack["renewal"]
    assert renewal["renewal_cycle_days"] == recheck.FLEET_CHECK_PACK_TTL_DAYS == 90
    assert renewal["renewal_price_units"] == recheck.FLEET_CHECK_PACK_PRICE_UNITS == 5_000_000
    assert renewal["renewal_price_usd"] == "$5.00"
    assert renewal["credits_per_pack"] == recheck.FLEET_CHECK_PACK_CREDITS == 100
    assert renewal["days_remaining"] == 90
    assert renewal["expires_at"] == pack["expires_at"]
    assert renewal["renewal_endpoint"] == "POST /v1/fleet-check/pack"
    # A pack minted with a shorter TTL reports fewer days remaining —
    # days_remaining tracks the pack's real expiry, not a restated 90.
    short, _ = store.mint_fleet_check_pack(
        "pay-fx-3", {"resources": [], "origins": [ORIGIN]}, None, None,
        False, 3600, ttl_days=10,
    )
    assert short["renewal"]["days_remaining"] == 10
    assert short["renewal"]["renewal_cycle_days"] == 90


# --- HTTP surface: buy + status ------------------------------------------------------


def _fake_request(nonce="0x" + "44" * 32):
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


def test_buy_handler_payload_carries_renewal(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    req = main.FleetCheckPackBuyRequest(origins=[ORIGIN], interval_s=3600)
    out = asyncio.run(main.fleet_check_pack_buy(req, _fake_request()))
    assert out["renewal"]["renewal_cycle_days"] == 90
    assert out["renewal"]["renewal_price_units"] == 5_000_000
    assert out["renewal"]["days_remaining"] == 90


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler()))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def test_http_status_carries_framework_log_and_renewal(http_env):
    pack, _ = http_env.mint_fleet_check_pack(
        "pay-http-fx", {"resources": [], "origins": [ORIGIN]}, None, None, False, 3600
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    # Before any run: renewal present, framework_log honestly empty.
    r = client.post("/v1/fleet-check/status", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["framework_log"] == []
    assert body["renewal"]["days_remaining"] == 90

    r = client.post("/v1/fleet-check/run", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 200, r.text
    run_body = r.json()
    assert len(run_body["framework_log"]) == 2
    assert run_body["renewal"]["renewal_price_units"] == 5_000_000

    r = client.post("/v1/fleet-check/status", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 200
    body = r.json()
    assert [e["resource"] for e in body["framework_log"]] == [
        r["url"] for r in body["latest_run"]["resources"]
    ]
    assert all(e["verdict"] == "OK" for e in body["framework_log"])
    assert body["renewal"]["renewal_cycle_days"] == 90
