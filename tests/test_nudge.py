"""Nudge inbox: the two-way leg of Fleet Check.

Pins the convention end of the protocol (the sender engine beside
the fleet checker pins the same canonical form and signature from
the other side):

  * the discovery manifest publishes extensions.nudge and the
    priced route count does NOT move (inbound mail is free);
  * a correctly signed nudge is stored; a re-send of the identical
    message is a duplicate, not a second entry;
  * unsigned / malformed mail is a clean 400, a signature that does
    not recover to the claimed sender is a clean 403 — never a 5xx;
  * the per-sender rate limit answers 429 once the window is full;
  * an opt-out is honoured only when signed by the payTo the named
    stall's own discovery doc publishes;
  * the owner read is fleet-internal-secret gated exactly like the
    siblings' internal routes (unset secret = 404, wrong = 403).
"""

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import app.main as main
from app.services import nudge as nudge_svc
from app.services import recheck

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
STALL = "https://stall.example.com"
ROUTE = STALL + "/v1/check"
CHECK_URL = "https://stall-row-production.up.railway.app/v1/fleet-check"

SENDER_KEY = "0x" + "22" * 32
SENDER = Account.from_key(SENDER_KEY)
OTHER_KEY = "0x" + "33" * 32

# Cross-implementation vector, generated 2026-10-27 with the
# throwaway key "0x" + "11" * 32: the sender engine's test suite
# (fleet-checker/test_nudge.py) pins the same message, canonical
# text, and signature, so the two implementations cannot drift.
VECTOR_KEY = "0x" + "11" * 32
VECTOR_MESSAGE = {
    "kind": "fleet-health-nudge",
    "protocol": "fleet-health/v1",
    "stall": "https://stall.example.com",
    "route": "https://stall.example.com/v1/check",
    "defect": {"code": "amount_mismatch", "expected": "50000", "observed": "70000"},
    "evidence": {
        "probes": [
            {
                "at": "2026-10-27T22:40:00Z",
                "http_status": 402,
                "detail": "live challenge amount 70000 != declared 50000",
            }
        ],
        "source": "fleet-check",
    },
    "first_seen": "2026-10-27T22:40:00Z",
    "check_url": "https://stall-row-production.up.railway.app/v1/fleet-check",
    "sender": "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
    "signature": "0x8c508f6c1049bd01c50914549b146dff3f87975d4a0c9a046c777145a5e8f0863fde40170075939cb8678f78be02f4834be2fb3ae795441eb3e5b2c2e1aeb8cf1b",
}


def _sign(message: dict, key: str = SENDER_KEY) -> dict:
    acct = Account.from_key(key)
    signed = dict(message)
    signed.pop("signature", None)
    sig = acct.sign_message(
        encode_defunct(text=nudge_svc.canonical_text(signed))
    ).signature.hex()
    signed["signature"] = sig if sig.startswith("0x") else "0x" + sig
    return signed


def _nudge(**overrides) -> dict:
    message = {
        "kind": "fleet-health-nudge",
        "protocol": "fleet-health/v1",
        "stall": STALL,
        "route": ROUTE,
        "defect": {
            "code": "amount_mismatch",
            "expected": "50000",
            "observed": "70000",
        },
        "evidence": {"probes": [], "source": "test"},
        "first_seen": "2026-10-27T22:40:00Z",
        "check_url": CHECK_URL,
        "sender": SENDER.address,
    }
    message.update(overrides)
    return _sign(message)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    monkeypatch.setattr(main, "_recheck_store", store)
    monkeypatch.setattr(
        nudge_svc, "RATE_LIMITER", nudge_svc._RateLimiter()
    )
    return TestClient(main.app, raise_server_exceptions=False)


def test_canonical_vector_verifies():
    """The pinned vector: canonical text + signature must verify
    against the claimed sender, byte for byte."""
    nudge_svc.validate_message(VECTOR_MESSAGE)
    nudge_svc.verify_sender(VECTOR_MESSAGE)
    assert nudge_svc.recover_signer(VECTOR_MESSAGE) == VECTOR_MESSAGE["sender"]
    assert Account.from_key(VECTOR_KEY).address == VECTOR_MESSAGE["sender"]


def test_manifest_publishes_inbox_and_route_count_holds(client):
    body = client.get("/.well-known/x402").json()
    ext = body["extensions"]["nudge"]
    assert ext["accepts"] == ["fleet-health/v1"]
    assert ext["endpoint"].endswith("/v1/nudge")
    paid = [r for r in body["resources"] if r["method"] == "POST"]
    assert len(paid) == 25  # inbound mail is free; the count must not move
    from app.config import PRICE_UNITS

    assert "/v1/nudge" not in PRICE_UNITS


def test_valid_nudge_is_stored_and_read_back(client, monkeypatch):
    resp = client.post("/v1/nudge", json=_nudge())
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "kind": "fleet-health-nudge",
        "stored": True,
        "duplicate": False,
    }
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", "test-secret")
    read = client.post(
        "/v1/internal/nudges",
        json={"limit": 10},
        headers={"x-fleet-internal-secret": "test-secret"},
    )
    assert read.status_code == 200
    body = read.json()
    assert body["count"] == 1
    entry = body["nudges"][0]
    assert entry["message"]["route"] == ROUTE
    assert entry["message"]["defect"]["code"] == "amount_mismatch"
    assert entry["message"]["sender"] == SENDER.address
    assert body["optouts"] == []


def test_duplicate_nudge_is_not_stored_twice(client):
    message = _nudge()
    assert client.post("/v1/nudge", json=message).json()["stored"] is True
    again = client.post("/v1/nudge", json=message)
    assert again.status_code == 200
    assert again.json()["stored"] is False
    assert again.json()["duplicate"] is True
    assert main._recheck_store.nudge_count() == 1


def test_unsigned_and_malformed_are_clean_400s(client):
    unsigned = _nudge()
    del unsigned["signature"]
    assert client.post("/v1/nudge", json=unsigned).status_code == 400
    assert client.post("/v1/nudge", json={"kind": "nope"}).status_code == 400
    assert client.post("/v1/nudge", content=b"{not json").status_code == 400
    assert client.post("/v1/nudge", json=["a", "list"]).status_code == 400
    wrong_kind = _nudge(kind="something-else")
    assert client.post("/v1/nudge", json=wrong_kind).status_code == 400
    off_stall = _nudge(route="https://other.example.com/v1/check")
    assert client.post("/v1/nudge", json=off_stall).status_code == 400


def test_wrong_signer_is_a_clean_403(client):
    forged = _nudge()
    forged = _sign(
        {k: v for k, v in forged.items() if k != "signature"}, key=OTHER_KEY
    )
    resp = client.post("/v1/nudge", json=forged)
    assert resp.status_code == 403
    tampered = _nudge()
    tampered["defect"] = {"code": "amount_mismatch", "expected": "1", "observed": "2"}
    assert client.post("/v1/nudge", json=tampered).status_code == 403


def test_rate_limit_answers_429(client, monkeypatch):
    monkeypatch.setenv("STALLROW_NUDGE_RATE_PER_HOUR", "3")
    for i in range(3):
        resp = client.post(
            "/v1/nudge", json=_nudge(first_seen=f"2026-10-27T22:4{i}:00Z")
        )
        assert resp.status_code == 200
    resp = client.post(
        "/v1/nudge", json=_nudge(first_seen="2026-10-27T22:45:00Z")
    )
    assert resp.status_code == 429


def test_owner_read_gate_mirrors_sibling_internal_routes(client, monkeypatch):
    monkeypatch.delenv("FLEET_INTERNAL_SECRET", raising=False)
    assert client.post("/v1/internal/nudges", json={}).status_code == 404
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", "test-secret")
    assert client.post("/v1/internal/nudges", json={}).status_code == 403
    resp = client.post(
        "/v1/internal/nudges",
        json={},
        headers={"x-fleet-internal-secret": "test-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_opt_out_requires_the_stalls_payto_signature(client, monkeypatch):
    # The fake discovery doc first names the fleet's real payTo —
    # the test sender does not control it, so its opt-out is refused.
    async def fake_pay_to(stall):
        return OWNER

    monkeypatch.setattr(main, "_stall_pay_to", fake_pay_to)
    opt_out = {
        "kind": "fleet-health-opt-out",
        "protocol": "fleet-health/v1",
        "stall": STALL,
        "sender": SENDER.address,
    }
    resp = client.post("/v1/nudge", json=_sign(opt_out))
    assert resp.status_code == 403  # SENDER is not the stall's payTo

    # When the stall's published payTo IS the signing wallet, the
    # opt-out is recorded.
    async def fake_pay_to_sender(stall):
        return SENDER.address

    monkeypatch.setattr(main, "_stall_pay_to", fake_pay_to_sender)
    resp = client.post("/v1/nudge", json=_sign(opt_out))
    assert resp.status_code == 200
    assert resp.json()["opted_out"] == STALL
    assert main._recheck_store.is_opted_out(STALL) is True
    optouts = main._recheck_store.list_optouts()
    assert len(optouts) == 1
    assert optouts[0]["stall"] == STALL
    assert optouts[0]["wallet"] == SENDER.address


def test_opt_out_with_unreadable_discovery_is_422(client, monkeypatch):
    async def no_pay_to(stall):
        return None

    monkeypatch.setattr(main, "_stall_pay_to", no_pay_to)
    opt_out = {
        "kind": "fleet-health-opt-out",
        "protocol": "fleet-health/v1",
        "stall": STALL,
        "sender": SENDER.address,
    }
    resp = client.post("/v1/nudge", json=_sign(opt_out))
    assert resp.status_code == 422


def test_store_round_trip_and_optout_registry(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    message = _nudge()
    key = nudge_svc.dedupe_key(message)
    assert store.store_nudge(message, key) is True
    assert store.store_nudge(message, key) is False
    assert store.nudge_count() == 1
    listed = store.list_nudges(10)
    assert listed[0]["message"]["sender"] == SENDER.address
    store.record_optout(STALL + "/", SENDER.address)
    assert store.is_opted_out(STALL) is True
    optouts = store.list_optouts()
    assert len(optouts) == 1
    assert optouts[0]["stall"] == STALL  # trailing slash normalized away
    assert optouts[0]["wallet"] == SENDER.address
    payload = json.dumps(store.list_nudges(1))
    assert ROUTE in payload
