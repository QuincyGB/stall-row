"""Fleet Check free first call: one free sweep per wallet via
POST /v1/fleet-check/free. The claim is persistent and atomic, a
refused definition spends nothing, a machinery failure releases
the claim, a second call gets an honest 402 naming the paid
route, and the free route is never a priced route. Offline —
MockTransport plays the fleet; IP-literal URLs keep the SSRF
guard's DNS lookup local (same fixtures as test_fleetcheck)."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.services import fleetcheck, probe as probe_svc, recheck

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
VERDICT = ORIGIN + "/v1/verdict"
CHECK = ORIGIN + "/v1/check"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(probe_svc, "RATE_LIMITER", probe_svc._RateLimiter())


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
            {"resource": ORIGIN + "/quote", "method": "GET", "price": "$0.00"},
        ],
    }


def fleet_handler(request):
    path = request.url.path
    if path == "/.well-known/x402":
        return httpx.Response(200, json=_manifest())
    if path == "/v1/verdict":
        return _challenge_response(VERDICT, amount="20000")
    if path == "/v1/check":
        return _challenge_response(CHECK, amount="50000")
    return httpx.Response(404, text="nope")


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


# --- store -----------------------------------------------------------------

def test_claim_is_once_only_and_persistent(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "w.db"))
    assert store.claim_free_fleet_check(WALLET) is True
    assert store.claim_free_fleet_check(WALLET) is False
    # Wallet keys are case-insensitive.
    assert store.claim_free_fleet_check(WALLET.upper().replace("0X", "0x")) is False
    assert store.free_fleet_check_used(WALLET) is True
    assert store.free_fleet_check_used(WALLET2) is False
    # A reopened store still knows.
    store2 = recheck.WatchStore(str(tmp_path / "w.db"))
    assert store2.claim_free_fleet_check(WALLET) is False
    store2.release_free_fleet_check(WALLET)
    assert store2.claim_free_fleet_check(WALLET) is True


# --- service ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_free_sweep_first_ok_second_refused(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "w.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler)) as client:
        out = await fleetcheck.free_sweep(client, store, WALLET, None, [ORIGIN])
        assert out["free"] is True
        assert out["summary"]["resources"] == 2 and out["summary"]["ok"] == 2
        with pytest.raises(fleetcheck.FreeSweepError) as exc:
            await fleetcheck.free_sweep(client, store, WALLET, None, [ORIGIN])
        assert exc.value.code == "free_call_used"
        assert exc.value.http_status == 402
        # A different wallet still gets its own free sweep.
        out2 = await fleetcheck.free_sweep(client, store, WALLET2, [VERDICT], None)
        assert out2["free"] is True


@pytest.mark.asyncio
async def test_refused_definition_spends_nothing(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "w.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler)) as client:
        with pytest.raises(ValueError):
            await fleetcheck.free_sweep(
                client, store, WALLET, ["https://127.0.0.1/v1/x"], None
            )
        assert store.free_fleet_check_used(WALLET) is False


@pytest.mark.asyncio
async def test_bad_wallet_refused_before_any_claim(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "w.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler)) as client:
        with pytest.raises(fleetcheck.FreeSweepError) as exc:
            await fleetcheck.free_sweep(client, store, "not-a-wallet", [VERDICT], None)
        assert exc.value.code == "bad_wallet"
        assert exc.value.http_status == 422


# --- HTTP surface -------------------------------------------------------------

def test_http_free_first_then_402(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/fleet-check/free", json={"wallet": WALLET, "origins": [ORIGIN]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["free"] is True and body["summary"]["ok"] == 2

    r = client.post(
        "/v1/fleet-check/free", json={"wallet": WALLET, "origins": [ORIGIN]}
    )
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["code"] == "free_call_used"
    assert body["paid_route"] == "POST /v1/fleet-check"


def test_http_free_route_is_not_priced(http_env):
    assert "/v1/fleet-check/free" not in PRICE_UNITS
    client = TestClient(main.app, raise_server_exceptions=False)
    doc = client.get("/.well-known/x402").json()
    urls = [e.get("resource") or "" for e in doc.get("resources", [])]
    assert not any("/v1/fleet-check/free" in u for u in urls)
