"""Stall Watch, self-serve (Next-20 #12): the claim flow + $1 pack
+ Standing Watch composed into one shelf registration.

Pins:
  * registration requires a live claim session FOR the origin
    (no header -> 401; a session for another origin is refused);
  * the pack must be bound to a URL on the claimed origin;
  * one call composes claim + pack + a real Standing Watch
    registration (visible in the watch store, funded by the pack);
  * a patrol recovery for the stall is delivered as a
    stall_recovery event carrying the signed recovery receipt
    (patrol receipt id verbatim + sha256 commitment), exactly
    once;
  * the routes are free (not priced routes).
"""

import json

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.services import claims as claims_svc
from app.services import recheck, stallwatch

ORIGIN = "https://93.184.216.34"
OTHER_ORIGIN = "https://93.184.216.35"
VERDICT = ORIGIN + "/v1/verdict"
WEBHOOK = "https://93.184.216.36/hook"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

PAYTO_KEY = "0x" + "44" * 32
PAYTO = Account.from_key(PAYTO_KEY)
RECEIPT_ID = "rcpt_test_recovery_0001"


def _discovery_doc():
    return {
        "x402Version": 2,
        "payTo": PAYTO.address,
        "resources": [
            {
                "resource": VERDICT,
                "method": "POST",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:8453",
                        "asset": USDC,
                        "amount": "20000",
                        "payTo": PAYTO.address,
                    }
                ],
            }
        ],
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    monkeypatch.setattr(main, "_recheck_store", store)
    monkeypatch.setattr(claims_svc, "RATE_LIMITER", claims_svc._RateLimiter())
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))
    deliveries: list[httpx.Request] = []

    def handler(request):
        if str(request.url) == WEBHOOK:
            deliveries.append(request)
            return httpx.Response(200, text="ok")
        if request.url.path == "/.well-known/x402":
            return httpx.Response(200, json=_discovery_doc())
        return httpx.Response(404, text="nope")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    monkeypatch.delenv("CLAIM_HISTORY_PATH", raising=False)
    client = TestClient(main.app, raise_server_exceptions=False)
    return client, store, mock_client, deliveries


def _claim(client) -> str:
    r = client.post("/v1/claim/start", json={"origin": ORIGIN})
    assert r.status_code == 200, r.text
    message = r.json()["message"]
    sig = (
        Account.from_key(PAYTO_KEY)
        .sign_message(encode_defunct(text=message))
        .signature.hex()
    )
    r = client.post(
        "/v1/claim/verify",
        json={
            "origin": ORIGIN,
            "method": "sign-message",
            "signature": sig if sig.startswith("0x") else "0x" + sig,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


PATROL = {
    "snapshot_id": "test",
    "generated_at": "2026-11-02T00:00:00Z",
    "routes": [
        {
            "url": VERDICT,
            "origin": ORIGIN,
            "defect_class": "payto_mismatch",
            "status": "recovered",
            "first_seen": "2026-11-01T00:00:00Z",
            "recovered_at": "2026-11-02T00:00:00Z",
            "receipt_id": RECEIPT_ID,
        },
        {
            "url": ORIGIN + "/v1/other",
            "origin": ORIGIN,
            "defect_class": "amount_mismatch",
            "status": "open",
            "first_seen": "2026-11-02T01:00:00Z",
            "last_seen": "2026-11-02T02:00:00Z",
        },
    ],
}


def test_register_requires_claim_session(env):
    client, store, _c, _d = env
    pack, _ = store.mint_pack("pay-sw0", VERDICT)
    r = client.post(
        "/v1/stall-watch/register",
        json={"origin": ORIGIN, "pack_id": pack["pack_id"], "webhook_url": WEBHOOK},
    )
    assert r.status_code == 401
    r = client.post(
        "/v1/stall-watch/register",
        json={"origin": ORIGIN, "pack_id": pack["pack_id"], "webhook_url": WEBHOOK},
        headers={"X-Claim-Session": "bogus-token"},
    )
    assert r.status_code == 401


def test_composition_and_recovery_receipt(env):
    client, store, mock_client, deliveries = env
    token = _claim(client)
    pack, _ = store.mint_pack("pay-sw1", VERDICT)
    r = client.post(
        "/v1/stall-watch/register",
        json={"origin": ORIGIN, "pack_id": pack["pack_id"], "webhook_url": WEBHOOK},
        headers={"X-Claim-Session": token},
    )
    assert r.status_code == 200, r.text
    reg = r.json()
    assert reg["reg_id"].startswith("sw_")
    assert reg["origin"] == ORIGIN
    # The composition is real: a Standing Watch registration exists,
    # funded by the owner's pack, on the pack's URL.
    watch_reg = store.get_watch_reg(reg["watch_reg_id"])
    assert watch_reg is not None
    assert watch_reg["url"] == VERDICT
    assert watch_reg["funding_id"] == pack["pack_id"]
    assert reg["claim"]["origin"] == ORIGIN

    import asyncio

    out = asyncio.run(
        stallwatch.evaluate(mock_client, store, reg, patrol_snapshot=PATROL)
    )
    assert out["events_count"] == 2  # one recovery + one open defect
    recovery = [e for e in out["events"] if e["event"] == "stall_recovery"]
    assert len(recovery) == 1
    receipt = recovery[0]["receipt"]
    assert receipt["receipt_id"] == RECEIPT_ID  # patrol's signed id, verbatim
    assert len(receipt["commitment"]) == 64  # sha256 hex
    assert receipt["fact"]["route"] == VERDICT
    pushed = [json.loads(req.content) for req in deliveries]
    assert any(p["event"] == "stall_recovery" for p in pushed)
    # Exactly once: a second evaluation delivers nothing new.
    out = asyncio.run(
        stallwatch.evaluate(mock_client, store, reg, patrol_snapshot=PATROL)
    )
    assert out["events_count"] == 0


def test_pack_must_be_on_claimed_origin(env):
    client, store, _c, _d = env
    token = _claim(client)
    foreign_pack, _ = store.mint_pack("pay-sw2", OTHER_ORIGIN + "/v1/verdict")
    r = client.post(
        "/v1/stall-watch/register",
        json={
            "origin": ORIGIN,
            "pack_id": foreign_pack["pack_id"],
            "webhook_url": WEBHOOK,
        },
        headers={"X-Claim-Session": token},
    )
    assert r.status_code == 403, r.text


def test_status_is_session_gated(env):
    client, store, _c, _d = env
    token = _claim(client)
    pack, _ = store.mint_pack("pay-sw3", VERDICT)
    client.post(
        "/v1/stall-watch/register",
        json={"origin": ORIGIN, "pack_id": pack["pack_id"], "webhook_url": WEBHOOK},
        headers={"X-Claim-Session": token},
    )
    r = client.get("/v1/stall-watch/status", params={"origin": ORIGIN})
    assert r.status_code == 401
    r = client.get(
        "/v1/stall-watch/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == ORIGIN
    assert r.json()["watch"]["url"] == VERDICT


def test_stall_watch_routes_are_not_priced():
    for path in (
        "/v1/stall-watch/register",
        "/v1/stall-watch/status",
        "/v1/stall-watch/evaluate",
        "/v1/stall-watch/pause",
        "/v1/stall-watch/resume",
        "/v1/stall-watch/cancel",
    ):
        assert path not in PRICE_UNITS
