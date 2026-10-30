"""Stall claim flow: prove control of an origin, read only its history.

Pins:
  * /v1/claim/start issues a single-use, ≤1h challenge carrying BOTH
    proof options (the exact EIP-191 message, and the
    /.well-known/claim-<token> file spec);
  * a signature by a payTo wallet the origin's own discovery doc
    publishes verifies; a signature over a different message, a
    malformed signature, and a signature by a wallet NOT in the
    published payTo set are all refused;
  * a consumed challenge cannot be replayed; an expired challenge
    is refused;
  * the file proof verifies on an exact token match (mocked fetch)
    and refuses wrong content, a missing file, and non-https origins;
  * the history read refuses with no session, an unknown session,
    an expired session, and a session issued for a DIFFERENT origin,
    and returns the origin's defects + a Managed Loop enrollment
    offer (enroll_client.py schema) with the matching session;
  * history is origin-scoped in every understood snapshot shape,
    and honestly marked "history not loaded" when no snapshot file
    is configured — nothing is hardcoded;
  * the claim endpoints are NOT priced routes: absent from
    PRICE_UNITS and the discovery manifest, served unpaid.

Fully offline: MockTransport plays the claimed origin (discovery
doc + claim file). The origin is a public IP literal so the SSRF
guard's DNS lookup stays local, exactly like the Fleet Check tests.
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
from app.services import recheck

ORIGIN = "https://93.184.216.34"
OTHER_ORIGIN = "https://93.184.216.35"
VERDICT = ORIGIN + "/v1/verdict"
CHECK = ORIGIN + "/v1/check"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAGES_ORIGIN = "https://quincygb.github.io"

PAYTO_KEY = "0x" + "44" * 32
PAYTO = Account.from_key(PAYTO_KEY)
SECOND_KEY = "0x" + "66" * 32
SECOND = Account.from_key(SECOND_KEY)
STRANGER_KEY = "0x" + "55" * 32
STRANGER = Account.from_key(STRANGER_KEY)


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
            },
            {
                "resource": CHECK,
                "method": "POST",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:8453",
                        "asset": USDC,
                        "amount": "5000",
                        "payTo": SECOND.address,
                    }
                ],
            },
        ],
    }


def origin_handler(state):
    def handler(request):
        path = request.url.path
        if path == "/.well-known/x402":
            return httpx.Response(200, json=_discovery_doc())
        if path.startswith("/.well-known/claim-"):
            body = state.get("claim_file_body")
            if body is None:
                return httpx.Response(404, text="no claim file")
            return httpx.Response(200, text=body)
        return httpx.Response(404, text="nope")

    return handler


@pytest.fixture()
def state():
    return {}


@pytest.fixture()
def http_env(monkeypatch, tmp_path, state):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    monkeypatch.setattr(main, "_recheck_store", store)
    monkeypatch.setattr(claims_svc, "RATE_LIMITER", claims_svc._RateLimiter())
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(origin_handler(state))
    )
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    monkeypatch.delenv("CLAIM_HISTORY_PATH", raising=False)
    return TestClient(main.app, raise_server_exceptions=False)


def _sign(message: str, key: str = PAYTO_KEY) -> str:
    acct = Account.from_key(key)
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def _start(client, origin=ORIGIN) -> dict:
    r = client.post("/v1/claim/start", json={"origin": origin})
    assert r.status_code == 200, r.text
    return r.json()


def _verify_sign(client, origin=ORIGIN, key=PAYTO_KEY, message=None, signature=None):
    if signature is None:
        signature = _sign(message, key=key)
    return client.post(
        "/v1/claim/verify",
        json={"origin": origin, "method": "sign-message", "signature": signature},
    )


def _claim_via_signature(client) -> dict:
    start = _start(client)
    r = _verify_sign(client, message=start["message"])
    assert r.status_code == 200, r.text
    return r.json()


# --- start -------------------------------------------------------------------------

def test_start_returns_both_proof_options(http_env):
    body = _start(http_env)
    assert body["origin"] == ORIGIN
    token = body["token"]
    assert len(token) == 32
    assert 0 < body["expires_in_s"] <= 3600
    # (a) sign-message: the exact message binds origin + token.
    assert body["message"] == body["proof_options"]["sign_message"]["message"]
    assert ORIGIN in body["message"] and token in body["message"]
    # (b) file: the token served at the well-known claim URL.
    assert body["file_url"] == f"{ORIGIN}/.well-known/claim-{token}"
    assert body["file_content"] == token
    assert body["proof_options"]["file"]["url"] == body["file_url"]
    assert body["proof_options"]["file"]["content"] == token


def test_start_rejects_non_origins(http_env):
    for bad in ("", "https://93.184.216.34/v1/verdict", "ftp://93.184.216.34",
                "not-a-url", "https://93.184.216.34/?x=1"):
        r = http_env.post("/v1/claim/start", json={"origin": bad})
        assert r.status_code == 422, (bad, r.text)


def test_claim_endpoints_are_not_priced_routes(http_env):
    for path in ("/v1/claim/start", "/v1/claim/verify", "/v1/claim/status"):
        assert path not in PRICE_UNITS
    body = http_env.get("/.well-known/x402").json()
    urls = [e.get("resource") or "" for e in body.get("resources", [])]
    assert not any("/v1/claim" in u for u in urls)
    assert len(body["resources"]) == 31  # 30 priced + the free /quote
    # Unpaid callers are served, not paywalled.
    assert http_env.post("/v1/claim/start", json={"origin": ORIGIN}).status_code == 200


def test_claim_cors_scoped_to_pages_origin(http_env):
    r = http_env.post(
        "/v1/claim/start", json={"origin": ORIGIN},
        headers={"Origin": PAGES_ORIGIN},
    )
    assert r.headers.get("access-control-allow-origin") == PAGES_ORIGIN
    r = http_env.post(
        "/v1/claim/start", json={"origin": ORIGIN},
        headers={"Origin": "https://evil.example.com"},
    )
    assert "access-control-allow-origin" not in r.headers


# --- sign-message verify --------------------------------------------------------------

def test_valid_signature_verifies_and_marks_claimed(http_env):
    body = _claim_via_signature(http_env)
    assert body["claimed"] is True
    assert body["origin"] == ORIGIN
    assert body["method"] == "sign-message"
    assert body["wallet"] == PAYTO.address
    assert body["session_token"].startswith("claimsess_")
    claimed = main._recheck_store.get_claimed(ORIGIN)
    assert claimed is not None and claimed["wallet"] == PAYTO.address


def test_signature_from_second_published_payto_verifies(http_env):
    """The payTo SET is what counts: a wallet published only inside
    one resource's accepts (not the top-level payTo) also proves
    control."""
    start = _start(http_env)
    r = _verify_sign(http_env, key=SECOND_KEY, message=start["message"])
    assert r.status_code == 200, r.text
    assert r.json()["wallet"] == SECOND.address


def test_wrong_signature_rejected(http_env):
    start = _start(http_env)
    # Signed the wrong text with the right wallet: recovery over the
    # challenge message cannot land on the payTo.
    r = _verify_sign(http_env, message="StallRow claim v1\nOrigin: somewhere else")
    assert r.status_code == 403
    # A well-formed but unrelated signature is equally refused.
    r = _verify_sign(http_env, signature="0x" + "ab" * 65)
    assert r.status_code == 403
    # A malformed signature is a clean 400, never a 5xx.
    r = _verify_sign(http_env, signature="0x1234")
    assert r.status_code == 400
    assert main._recheck_store.get_claimed(ORIGIN) is None


def test_signature_from_wallet_not_in_payto_set_rejected(http_env):
    start = _start(http_env)
    r = _verify_sign(http_env, key=STRANGER_KEY, message=start["message"])
    assert r.status_code == 403
    assert r.json()["code"] == "signer_not_payto"
    assert main._recheck_store.get_claimed(ORIGIN) is None


def test_replayed_challenge_rejected(http_env):
    start = _start(http_env)
    signature = _sign(start["message"])
    r = _verify_sign(http_env, signature=signature)
    assert r.status_code == 200
    replay = _verify_sign(http_env, signature=signature)
    assert replay.status_code == 409
    assert replay.json()["code"] == "challenge_used"
    assert "session_token" not in replay.json()


def test_verify_without_challenge_is_404(http_env):
    r = _verify_sign(http_env, message="anything")
    assert r.status_code == 404
    assert r.json()["code"] == "no_challenge"


@pytest.mark.asyncio
async def test_expired_challenge_rejected(tmp_path, state):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    start = claims_svc.start_claim(store, ORIGIN, now_epoch=1_000_000.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(origin_handler(state))
    ) as client:
        with pytest.raises(claims_svc.ClaimError) as e:
            await claims_svc.verify_claim(
                client,
                store,
                ORIGIN,
                "sign-message",
                _sign(start["message"]),
                now_epoch=1_000_000.0 + 3601,
            )
    assert e.value.code == "challenge_expired"
    assert e.value.http_status == 410
    assert store.get_claimed(ORIGIN) is None


def test_failed_verify_does_not_consume_challenge(http_env):
    start = _start(http_env)
    bad = _verify_sign(http_env, key=STRANGER_KEY, message=start["message"])
    assert bad.status_code == 403
    good = _verify_sign(http_env, message=start["message"])
    assert good.status_code == 200


# --- file verify -------------------------------------------------------------------------

def test_file_proof_happy_path(http_env, state):
    start = _start(http_env)
    state["claim_file_body"] = start["token"]
    r = http_env.post(
        "/v1/claim/verify", json={"origin": ORIGIN, "method": "file"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"] is True and body["method"] == "file"
    assert body["wallet"] is None
    assert body["session_token"].startswith("claimsess_")
    assert main._recheck_store.get_claimed(ORIGIN)["method"] == "file"


def test_file_proof_wrong_content_rejected(http_env, state):
    _start(http_env)
    state["claim_file_body"] = "not-the-token"
    r = http_env.post(
        "/v1/claim/verify", json={"origin": ORIGIN, "method": "file"}
    )
    assert r.status_code == 403
    assert r.json()["code"] == "claim_file_mismatch"
    assert main._recheck_store.get_claimed(ORIGIN) is None


def test_file_proof_missing_file_rejected(http_env, state):
    _start(http_env)
    state["claim_file_body"] = None  # the origin serves a 404 there
    r = http_env.post(
        "/v1/claim/verify", json={"origin": ORIGIN, "method": "file"}
    )
    assert r.status_code == 403
    assert r.json()["code"] == "claim_file_unreachable"


def test_file_proof_requires_https(http_env, state):
    start = _start(http_env, origin="http://93.184.216.34")
    state["claim_file_body"] = start["token"]
    r = http_env.post(
        "/v1/claim/verify",
        json={"origin": "http://93.184.216.34", "method": "file"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "https_required"


# --- status: the privacy gate --------------------------------------------------------------

def _history_file(tmp_path, doc) -> str:
    path = tmp_path / "claims-history.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _canonical_history():
    return {
        "origins": {
            ORIGIN: {
                "defects": [
                    {
                        "route": VERDICT,
                        "defect_class": "unexpected_status",
                        "first_seen": "2026-10-28T10:00:00Z",
                        "last_seen": "2026-10-29T02:00:00Z",
                        "state": "open",
                    },
                    {
                        "route": CHECK,
                        "defect_class": "unreachable",
                        "first_seen": "2026-10-27T09:00:00Z",
                        "last_seen": "2026-10-28T09:00:00Z",
                        "state": "recovered",
                    },
                ]
            },
            OTHER_ORIGIN: {
                "defects": [
                    {
                        "route": OTHER_ORIGIN + "/v1/secret",
                        "defect_class": "server_error",
                        "first_seen": "2026-10-28T00:00:00Z",
                        "last_seen": "2026-10-28T01:00:00Z",
                        "state": "open",
                    }
                ]
            },
        }
    }


def test_status_refuses_without_session(http_env):
    r = http_env.get("/v1/claim/status", params={"origin": ORIGIN})
    assert r.status_code == 401
    assert "history" not in r.json()


def test_status_refuses_unknown_session(http_env):
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": "claimsess_" + "00" * 24},
    )
    assert r.status_code == 401
    assert "history" not in r.json()


def test_status_refuses_session_for_different_origin(http_env, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CLAIM_HISTORY_PATH", _history_file(tmp_path, _canonical_history())
    )
    claim = _claim_via_signature(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": OTHER_ORIGIN},
        headers={"X-Claim-Session": claim["session_token"]},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "session_wrong_origin"
    assert "history" not in r.json()  # nothing about the other origin leaks


def test_status_refuses_expired_session(http_env):
    claim = _claim_via_signature(http_env)
    store = main._recheck_store
    with store._lock:
        store._conn.execute(
            "UPDATE claim_sessions SET expires_epoch = 0 "
            "WHERE session_token = ?",
            (claim["session_token"],),
        )
        store._conn.commit()
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": claim["session_token"]},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "session_expired"


def test_status_returns_history_and_enrollment_with_matching_session(
    http_env, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "CLAIM_HISTORY_PATH", _history_file(tmp_path, _canonical_history())
    )
    claim = _claim_via_signature(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": claim["session_token"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin"] == ORIGIN and body["claimed"] is True
    history = body["history"]
    assert history["loaded"] is True and history["history_not_loaded"] is False
    # Only THIS origin's defects — the other origin's rows stay private.
    routes = {d["route"] for d in history["defects"]}
    assert routes == {VERDICT, CHECK}
    by_route = {d["route"]: d for d in history["defects"]}
    assert by_route[VERDICT]["defect_class"] == "unexpected_status"
    assert by_route[VERDICT]["state"] == "open"
    assert by_route[VERDICT]["first_seen"] == "2026-10-28T10:00:00Z"
    assert by_route[VERDICT]["last_seen"] == "2026-10-29T02:00:00Z"
    assert by_route[CHECK]["state"] == "recovered"
    assert history["summary"]["total"] == 2
    assert history["summary"]["open"] == 1
    assert history["summary"]["recovered"] == 1
    # Enrollment offer: the enroll_client.py client schema, with the
    # open-defect route as the priority route.
    offer = body["enrollment_offer"]
    assert set(offer) == {
        "client", "enrolled_at", "origins", "routes",
        "knock_channel", "contact", "cadence", "enabled",
    }
    assert offer["origins"] == [ORIGIN]
    assert offer["routes"][0] == VERDICT
    assert offer["cadence"] == "daily" and offer["enabled"] is True


def test_status_history_not_loaded_marker_without_file(http_env):
    claim = _claim_via_signature(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": claim["session_token"]},
    )
    assert r.status_code == 200, r.text
    history = r.json()["history"]
    assert history["loaded"] is False
    assert history["history_not_loaded"] is True
    assert history["status"] == "not_loaded"
    assert "history not loaded" in history["note"]
    assert history["defects"] == []
    assert history["summary"]["total"] == 0
    # The enrollment offer still stands, with no invented routes.
    assert r.json()["enrollment_offer"]["routes"] == []


def test_status_reads_patrol_state_shape_origin_scoped(
    http_env, tmp_path, monkeypatch
):
    patrol_doc = {
        "open_defects": {
            f"{ORIGIN}|{VERDICT}": {
                "code": "amount_mismatch",
                "first_seen": "2026-10-28T08:00:00Z",
                "last_seen": "2026-10-29T01:00:00Z",
            },
            f"{OTHER_ORIGIN}|{OTHER_ORIGIN}/v1/x": {
                "code": "unreachable",
                "first_seen": "2026-10-28T08:00:00Z",
                "last_seen": "2026-10-28T09:00:00Z",
            },
        }
    }
    monkeypatch.setenv(
        "CLAIM_HISTORY_PATH", _history_file(tmp_path, patrol_doc)
    )
    claim = _claim_via_signature(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": claim["session_token"]},
    )
    assert r.status_code == 200, r.text
    defects = r.json()["history"]["defects"]
    assert len(defects) == 1
    assert defects[0]["route"] == VERDICT
    assert defects[0]["defect_class"] == "amount_mismatch"
    assert defects[0]["state"] == "open"


def test_status_accepts_bearer_session(http_env):
    claim = _claim_via_signature(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers={"Authorization": f"Bearer {claim['session_token']}"},
    )
    assert r.status_code == 200
