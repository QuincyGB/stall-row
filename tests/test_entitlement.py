"""Prepaid entitlements (W3 Build 1): Ed25519 signed-token issuance
gated by the existing pass-style payment, verification middleware,
atomic credit decrement in the WatchStore ledger, expiry, revocation,
tamper resistance, and refund-on-failure. Fully offline; signing keys
are ephemeral test keys generated in-process — never committed."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.services import entitlement, recheck
from app.services.recheck import PackError

WEB_URL = "https://93.184.216.34/story"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def keys(monkeypatch):
    private_b64, public_b64 = entitlement.generate_dev_keypair()
    monkeypatch.setenv("STALLROW_ENTITLEMENT_PRIVATE_KEY_B64", private_b64)
    monkeypatch.setenv("STALLROW_ENTITLEMENT_PUBLIC_KEY_B64", public_b64)
    return private_b64, public_b64


def _mint_token(store, key="pay-1", kind="pack", now=None):
    """Mint a ledger row + signed token the way the buy handler does."""
    terms = entitlement.KINDS[kind]
    view, created = store.mint_entitlement(
        key, kind, terms["credits"], terms["ttl_days"], now_epoch=now
    )
    assert created
    row = store._ent_row("jti = ?", (view["jti"],))
    token = entitlement.issue_token(
        entitlement.load_private_key(),
        jti=view["jti"],
        kind=kind,
        credits_total=view["credits_total"],
        issued_epoch=row["created_epoch"],
        expires_epoch=row["expires_epoch"],
    )
    return view, token


# --- token verification ---------------------------------------------------------


def test_issue_verify_roundtrip(store, keys):
    view, token = _mint_token(store)
    claims = entitlement.parse_and_verify(token, entitlement.load_public_key())
    assert claims["jti"] == view["jti"]
    assert claims["kind"] == "pack"
    assert claims["credits_total"] == 1000
    assert token.startswith("ent1.")


def test_tampered_payload_fails_signature(store, keys):
    _, token = _mint_token(store)
    head, payload, sig = token.split(".")
    claims = json.loads(entitlement._b64url_decode(payload))
    claims["credits_total"] = 999_999  # inflate credits in the token
    forged = ".".join(
        [head, entitlement._b64url_encode(json.dumps(claims).encode()), sig]
    )
    with pytest.raises(entitlement.EntitlementError) as e:
        entitlement.parse_and_verify(forged, entitlement.load_public_key())
    assert e.value.code == "entitlement_bad_signature"
    assert e.value.http_status == 401


def test_tampered_signature_and_garbage_fail(store, keys):
    _, token = _mint_token(store)
    head, payload, _sig = token.split(".")
    forged = ".".join([head, payload, entitlement._b64url_encode(b"\x00" * 64)])
    with pytest.raises(entitlement.EntitlementError) as e:
        entitlement.parse_and_verify(forged, entitlement.load_public_key())
    assert e.value.code == "entitlement_bad_signature"
    for junk in ("", "ent1", "ent1.x", "a.b.c", "ent1.@@.@@"):
        with pytest.raises(entitlement.EntitlementError):
            entitlement.parse_and_verify(junk, entitlement.load_public_key())


def test_expired_token_refused(store, keys):
    past = time.time() - 100_000
    view, _ = _mint_token(store, now=past)  # pack minted 90d-ago epoch base
    row = store._ent_row("jti = ?", (view["jti"],))
    token = entitlement.issue_token(
        entitlement.load_private_key(),
        jti=view["jti"],
        kind="pack",
        credits_total=view["credits_total"],
        issued_epoch=row["created_epoch"],
        expires_epoch=row["created_epoch"] + 10,  # expired 10s after issue
    )
    with pytest.raises(entitlement.EntitlementError) as e:
        entitlement.parse_and_verify(token, entitlement.load_public_key())
    assert e.value.code == "entitlement_expired"
    assert e.value.http_status == 410


def test_unconfigured_verification_fails_closed(store, monkeypatch):
    monkeypatch.delenv("STALLROW_ENTITLEMENT_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("STALLROW_ENTITLEMENT_PUBLIC_KEY_B64", raising=False)
    assert entitlement.load_public_key() is None
    with pytest.raises(entitlement.EntitlementError) as e:
        entitlement.parse_and_verify("ent1.x.y", None)
    assert e.value.http_status == 503


def test_mismatched_configured_keys_fail_closed(monkeypatch):
    priv_a, _ = entitlement.generate_dev_keypair()
    _, pub_b = entitlement.generate_dev_keypair()
    monkeypatch.setenv("STALLROW_ENTITLEMENT_PRIVATE_KEY_B64", priv_a)
    monkeypatch.setenv("STALLROW_ENTITLEMENT_PUBLIC_KEY_B64", pub_b)
    with pytest.raises(entitlement.EntitlementError) as e:
        entitlement.load_public_key()
    assert e.value.code == "entitlement_key_mismatch"


# --- ledger: mint / spend / refund / revoke --------------------------------------


def test_mint_idempotent_on_payment_replay(store):
    first, c1 = store.mint_entitlement("nonce-1", "pass", 1800, 30)
    second, c2 = store.mint_entitlement("nonce-1", "pass", 1800, 30)
    assert c1 is True and c2 is False
    assert second["jti"] == first["jti"]
    assert first["credits_total"] == 1800 and first["kind"] == "pass"


def test_spend_decrements_atomically_and_records(store, keys):
    view, _ = _mint_token(store)
    out = store.spend_entitlement_credit(view["jti"], "/v1/recheck")
    assert out["credits_remaining"] == 999
    assert out["debit_id"].startswith("entd_")
    assert store.entitlement_spend_count(view["jti"]) == 1
    assert store.get_entitlement(view["jti"])["credits_remaining"] == 999


def test_spend_refusals_decrement_nothing(store, keys):
    view, _ = _mint_token(store)
    with pytest.raises(PackError) as e:
        store.spend_entitlement_credit("ent_nope", "/v1/recheck")
    assert e.value.code == "unknown_entitlement" and e.value.http_status == 404

    stale, _ = store.mint_entitlement("nonce-stale", "pack", 1000, 0)
    with pytest.raises(PackError) as e:
        store.spend_entitlement_credit(stale["jti"], "/v1/recheck")
    assert e.value.code == "entitlement_expired" and e.value.http_status == 410

    tiny, _ = store.mint_entitlement("nonce-tiny", "pack", 1, 90)
    store.spend_entitlement_credit(tiny["jti"], "/v1/recheck")
    with pytest.raises(PackError) as e:
        store.spend_entitlement_credit(tiny["jti"], "/v1/recheck")
    assert e.value.code == "entitlement_exhausted" and e.value.http_status == 402
    assert store.get_entitlement(tiny["jti"])["credits_remaining"] == 0


def test_refund_recredits_exactly_once(store, keys):
    view, _ = _mint_token(store)
    out = store.spend_entitlement_credit(view["jti"], "/v1/recheck")
    assert store.refund_entitlement_credit(out["debit_id"]) is True
    assert store.get_entitlement(view["jti"])["credits_remaining"] == 1000
    assert store.refund_entitlement_credit(out["debit_id"]) is False
    assert store.refund_entitlement_credit("entd_nope") is False


def test_revocation_blocks_spend_immediately(store, keys):
    view, _ = _mint_token(store)
    assert store.revoke_entitlement(view["jti"]) is True
    assert store.revoke_entitlement(view["jti"]) is False  # already revoked
    with pytest.raises(PackError) as e:
        store.spend_entitlement_credit(view["jti"], "/v1/recheck")
    assert e.value.code == "entitlement_revoked" and e.value.http_status == 403
    assert store.get_entitlement(view["jti"])["revoked"] is True


# --- HTTP surface -----------------------------------------------------------------


def _fake_request(nonce="ent-nonce-1"):
    scope = {
        "type": "http", "method": "POST", "path": "/v1/entitlement-pack",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "scheme": "http", "client": ("test", 1), "root_path": "",
        "http_version": "1.1",
    }
    request = Request(scope)
    request.state.payment_payload = SimpleNamespace(
        payload=SimpleNamespace(
            authorization=SimpleNamespace(
                nonce=nonce,
                **{"from": "0x1111111111111111111111111111111111111111"},
            ),
            signature="0xdeadbeef",
        )
    )
    return request


def test_buy_handler_mints_and_replay_returns_same_token(monkeypatch, tmp_path, keys):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    out = asyncio.run(main.entitlement_pack_buy(_fake_request()))
    assert out["credits_total"] == 1000 and out["price_units"] == 5_000_000
    claims = entitlement.parse_and_verify(out["token"], entitlement.load_public_key())
    assert claims["jti"] == out["jti"]
    replay = asyncio.run(main.entitlement_pack_buy(_fake_request()))
    assert replay["jti"] == out["jti"] and replay["token"] == out["token"]
    assert "already minted" in replay["note"]


def test_buy_handler_fails_closed_without_signing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("STALLROW_ENTITLEMENT_PRIVATE_KEY_B64", raising=False)
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    out = asyncio.run(main.entitlement_pass_buy(_fake_request()))
    assert out.status_code == 503  # >= 400: the middleware never settles it


def test_unpaid_entitlement_challenges_carry_product_prices():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, amount in (("/v1/entitlement-pass", "9000000"), ("/v1/entitlement-pack", "5000000")):
        r = client.post(path, json={})
        assert r.status_code == 402
        challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
        assert challenge["accepts"][0]["amount"] == amount


@pytest.fixture()
def http_env(monkeypatch, tmp_path, keys):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    return fresh


def _mock_fetch(monkeypatch):
    def ok_handler(request):
        return httpx.Response(200, text="a page", headers={"content-type": "text/plain"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)


def test_http_token_pays_route_and_balance_and_revoke(http_env, monkeypatch):
    _mock_fetch(monkeypatch)
    view, token = _mint_token(http_env, key="pay-http-1")
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Entitlement": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "BASELINE"
    assert http_env.get_entitlement(view["jti"])["credits_remaining"] == 999

    r = client.post("/v1/entitlement/balance", json={"token": token})
    assert r.status_code == 200
    assert r.json()["credits_remaining"] == 999 and r.json()["spends"] == 1

    r = client.post("/v1/entitlement/revoke", json={"token": token})
    assert r.status_code == 200 and r.json()["revoked"] is True

    # Revoked: refused, no fallback charge, no further decrement.
    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Entitlement": token},
    )
    assert r.status_code == 403 and r.json()["code"] == "entitlement_revoked"
    assert http_env.get_entitlement(view["jti"])["credits_remaining"] == 999


def test_http_invalid_token_refused_never_falls_back(http_env, monkeypatch):
    _mock_fetch(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Entitlement": "ent1.forged.token"},
    )
    # A refusal in the fleet error shape — NOT a 402 per-call
    # challenge: presenting a token never silently becomes a charge.
    assert r.status_code in (401, 422)
    assert "code" in r.json()
