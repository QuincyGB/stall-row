"""Hosted inboxes: the mailbox relay for claimed origins.

Pins:
  * a successful claim mints the origin's hosted inbox and returns
    the kit in one view — endpoint, the exact one-line discovery
    snippet, self-host snippets, relay state;
  * the hosted endpoint accepts spec-valid signed knocks with the
    fleet inbox's discipline: malformed 400, forged 403, a knock
    addressed to a different stall 400, an UNCLAIMED origin's key
    404, duplicates deduped, a "final" escalation stored as its own
    entry;
  * opt-outs signed by the origin's published payTo bind (and then
    refuse later knocks); opt-outs from strangers are refused;
  * the owner reads the mail through the claim session only (no
    session 401, a session for another origin 403);
  * the webhook relay fires server-side with the knock payload and
    records the delivery; the email relay is BUILT BUT DISABLED by
    default — a configured address records an honest "disabled"
    delivery and sends nothing;
  * the fleet-internal claims-history upload is secret-gated and,
    once stored, the claim status read serves the snapshot when no
    CLAIM_HISTORY_PATH file is configured (the round-2 caveat fix);
  * none of the new paths is a priced route and discovery does not
    move.

Fully offline: MockTransport plays the claimed origin (discovery
doc) and the webhook target.
"""

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.services import claims as claims_svc
from app.services import hosted_inbox as hosted_svc
from app.services import nudge as nudge_svc
from app.services import recheck

ORIGIN = "https://93.184.216.34"
ROUTE = ORIGIN + "/v1/verdict"
HOOK = ORIGIN + "/hook"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

PAYTO_KEY = "0x" + "44" * 32
PAYTO = Account.from_key(PAYTO_KEY)
SENDER_KEY = "0x" + "77" * 32
SENDER = Account.from_key(SENDER_KEY)
STRANGER_KEY = "0x" + "55" * 32
STRANGER = Account.from_key(STRANGER_KEY)

FIRST_SEEN = "2026-10-29T00:00:00Z"


def _discovery_doc():
    return {
        "x402Version": 2,
        "payTo": PAYTO.address,
        "resources": [
            {
                "resource": ROUTE,
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


def origin_handler(state):
    def handler(request):
        path = request.url.path
        if path == "/.well-known/x402":
            return httpx.Response(200, json=_discovery_doc())
        if path == "/hook":
            state.setdefault("hooks", []).append(request.content)
            return httpx.Response(state.get("hook_status", 200), text="ok")
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
    monkeypatch.setattr(nudge_svc, "RATE_LIMITER", nudge_svc._RateLimiter())
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(origin_handler(state))
    )
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    monkeypatch.delenv("CLAIM_HISTORY_PATH", raising=False)
    monkeypatch.delenv("STALLROW_HOSTED_INBOX_EMAIL_RELAY", raising=False)
    monkeypatch.delenv("FLEET_INTERNAL_SECRET", raising=False)
    monkeypatch.setenv("STALLROW_HOSTED_WEBHOOK_BACKOFF_S", "0,0")
    client = TestClient(main.app, raise_server_exceptions=False)
    client.mock_client = mock_client
    client.store = store
    return client


def _sign_text(message_text: str, key: str) -> str:
    acct = Account.from_key(key)
    sig = acct.sign_message(encode_defunct(text=message_text)).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def _claim(client, origin=ORIGIN) -> dict:
    start = client.post("/v1/claim/start", json={"origin": origin})
    assert start.status_code == 200, start.text
    body = start.json()
    r = client.post(
        "/v1/claim/verify",
        json={
            "origin": origin,
            "method": "sign-message",
            "signature": _sign_text(body["message"], PAYTO_KEY),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _session(claim_body: dict) -> dict:
    return {"X-Claim-Session": claim_body["session_token"]}


def _knock(key=SENDER_KEY, stall=ORIGIN, route=ROUTE, code="no_402",
           first_seen=FIRST_SEEN, escalation=None, signed=True):
    sender = Account.from_key(key)
    msg = {
        "kind": "fleet-health-nudge",
        "protocol": "fleet-health/v1",
        "stall": stall,
        "route": route,
        "defect": {
            "code": code,
            "expected": "an HTTP 402 challenge",
            "observed": "HTTP 200 with a body",
        },
        "severity": "critical",
        "impact": "Agents cannot pay for this route.",
        "action": {
            "summary": "Restore the 402 challenge on this route.",
            "method": "POST",
            "url": route,
            "body": {"resources": [route]},
            "spec": hosted_svc.SPEC_URL,
        },
        "evidence": {
            "probes": [{"at": first_seen, "status": 200}],
            "source": "patrol",
        },
        "first_seen": first_seen,
        "check_url": "https://quincygb.github.io/x402-loop-kits/check.html",
        "sender": sender.address,
    }
    if escalation:
        msg["escalation"] = escalation
    if signed:
        msg["signature"] = _sign_text(nudge_svc.canonical_text(msg), key)
    return msg


def _hosted_path(claim_body: dict) -> str:
    endpoint = claim_body["hosted_inbox"]["endpoint"]
    return "/v1/hosted-inbox/" + endpoint.rsplit("/", 1)[1]


# --- the kit: claim → mint → snippet --------------------------------------


def test_claim_mints_hosted_inbox_with_kit(http_env):
    body = _claim(http_env)
    kit = body["hosted_inbox"]
    assert kit is not None
    assert kit["origin"] == ORIGIN
    key = hosted_svc.origin_key(ORIGIN)
    assert kit["origin_key"] == key
    assert kit["endpoint"].endswith(f"/v1/hosted-inbox/{key}")
    snippet = kit["discovery_snippet"]
    assert snippet["endpoint"] == kit["endpoint"]
    assert snippet["accepts"] == ["fleet-health/v1"]
    assert snippet["spec"] == hosted_svc.SPEC_URL
    # The one-line publish snippet carries the same endpoint.
    assert kit["endpoint"] in kit["publish_line"]
    assert '"extensions"' in kit["publish_line"]
    # Self-host snippets for the three named stacks, marked as ports
    # of the fleet's own receiver.
    snippets = kit["self_host_snippets"]
    for name in ("express", "fastapi", "cloudflare_workers"):
        assert name in snippets
        assert "fleet-health-nudge/v1" in snippets[name]
        assert "stall-row" in snippets[name]
    assert kit["knocks_received"] == 0
    assert kit["webhook"] == {"url": None, "enabled": False}
    assert kit["email"]["relay_enabled"] is False  # disabled by default


def test_status_carries_the_same_kit(http_env):
    body = _claim(http_env)
    r = http_env.get(
        "/v1/claim/status",
        params={"origin": ORIGIN},
        headers=_session(body),
    )
    assert r.status_code == 200, r.text
    assert r.json()["hosted_inbox"]["endpoint"] == body["hosted_inbox"]["endpoint"]


def test_unclaimed_origin_gets_no_inbox(http_env):
    key = hosted_svc.origin_key(ORIGIN)
    r = http_env.post(f"/v1/hosted-inbox/{key}", json=_knock())
    assert r.status_code == 404, r.text
    r = http_env.get("/v1/claim/inbox", params={"origin": ORIGIN})
    assert r.status_code == 401, r.text  # no session at all


# --- the receive discipline -------------------------------------------------


def test_knock_lands_and_owner_reads_it(http_env):
    body = _claim(http_env)
    r = http_env.post(_hosted_path(body), json=_knock())
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True
    assert r.json()["duplicate"] is False
    # The owner reads it back through the claim session.
    r = http_env.get(
        "/v1/claim/inbox", params={"origin": ORIGIN}, headers=_session(body)
    )
    assert r.status_code == 200, r.text
    inbox = r.json()
    assert len(inbox["knocks"]) == 1
    knock = inbox["knocks"][0]["message"]
    assert knock["route"] == ROUTE
    assert knock["defect"]["code"] == "no_402"
    assert inbox["hosted_inbox"]["knocks_received"] == 1


def test_duplicate_knock_deduped_but_escalation_is_distinct(http_env):
    body = _claim(http_env)
    path = _hosted_path(body)
    assert http_env.post(path, json=_knock()).json()["stored"] is True
    again = http_env.post(path, json=_knock())
    assert again.json()["stored"] is False
    assert again.json()["duplicate"] is True
    final = http_env.post(path, json=_knock(escalation="final"))
    assert final.json()["stored"] is True
    r = http_env.get(
        "/v1/claim/inbox", params={"origin": ORIGIN}, headers=_session(body)
    )
    assert len(r.json()["knocks"]) == 2


def test_forged_knock_refused_403(http_env):
    body = _claim(http_env)
    msg = _knock()
    # Re-sign the same body with a stranger's key: the signature no
    # longer matches the claimed sender.
    msg["signature"] = _sign_text(
        nudge_svc.canonical_text(msg), STRANGER_KEY
    )
    r = http_env.post(_hosted_path(body), json=msg)
    assert r.status_code == 403, r.text
    assert http_env.store.hosted_knock_count(ORIGIN) == 0


def test_malformed_knock_refused_400(http_env):
    body = _claim(http_env)
    path = _hosted_path(body)
    r = http_env.post(path, json={"kind": "fleet-health-nudge"})
    assert r.status_code == 400, r.text
    r = http_env.post(path, content=b"not json")
    assert r.status_code == 400, r.text
    r = http_env.post(path, json=["not", "an", "object"])
    assert r.status_code == 400, r.text


def test_knock_for_another_stall_refused_400(http_env):
    body = _claim(http_env)
    msg = _knock(
        stall="https://93.184.216.99",
        route="https://93.184.216.99/v1/verdict",
    )
    r = http_env.post(_hosted_path(body), json=msg)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "stall_mismatch"
    assert http_env.store.hosted_knock_count(ORIGIN) == 0


def test_inbox_read_session_gate(http_env):
    body = _claim(http_env)
    # No session → 401; the claimed session reads only its origin.
    r = http_env.get("/v1/claim/inbox", params={"origin": ORIGIN})
    assert r.status_code == 401
    r = http_env.get(
        "/v1/claim/inbox",
        params={"origin": ORIGIN},
        headers={"X-Claim-Session": "claimsess_nope"},
    )
    assert r.status_code == 401
    # A session minted for ORIGIN cannot read another origin.
    r = http_env.get(
        "/v1/claim/inbox",
        params={"origin": "https://93.184.216.35"},
        headers=_session(body),
    )
    assert r.status_code == 403


# --- opt-outs -----------------------------------------------------------------


def _optout(key, stall=ORIGIN):
    sender = Account.from_key(key)
    msg = {
        "kind": "fleet-health-opt-out",
        "protocol": "fleet-health/v1",
        "stall": stall,
        "sender": sender.address,
    }
    msg["signature"] = _sign_text(nudge_svc.canonical_text(msg), key)
    return msg


def test_optout_by_payto_binds_and_blocks_later_knocks(http_env):
    body = _claim(http_env)
    path = _hosted_path(body)
    r = http_env.post(path, json=_optout(PAYTO_KEY))
    assert r.status_code == 200, r.text
    assert r.json()["opted_out"] is True
    r = http_env.post(path, json=_knock())
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "opted_out"


def test_optout_by_stranger_refused(http_env):
    body = _claim(http_env)
    r = http_env.post(_hosted_path(body), json=_optout(STRANGER_KEY))
    assert r.status_code == 403, r.text
    assert http_env.store.is_opted_out(ORIGIN) is False


# --- relays ---------------------------------------------------------------------


def _configure(client, session, **fields):
    return client.post(
        "/v1/claim/inbox/config",
        json={"origin": ORIGIN, **fields},
        headers=session,
    )


def test_webhook_config_and_delivery(http_env, state):
    import asyncio
    import json as jsonlib

    body = _claim(http_env)
    session = _session(body)
    r = _configure(http_env, session, webhook_url=HOOK, webhook_enabled=True)
    assert r.status_code == 200, r.text
    assert r.json()["hosted_inbox"]["webhook"] == {
        "url": HOOK,
        "enabled": True,
    }
    r = http_env.post(_hosted_path(body), json=_knock())
    assert r.json()["stored"] is True
    # The receive route's background relay pass fires server-side
    # with the knock payload (TestClient runs background tasks
    # before returning, like production's event loop does after).
    assert len(state["hooks"]) == 1
    payload = jsonlib.loads(state["hooks"][0])
    assert payload["kind"] == "fleet-health-hosted-relay"
    assert payload["origin"] == ORIGIN
    assert payload["knock"]["defect"]["code"] == "no_402"
    # The delivery is recorded on the owner's delivery log...
    r = http_env.get(
        "/v1/claim/inbox", params={"origin": ORIGIN}, headers=session
    )
    deliveries = r.json()["deliveries"]
    assert deliveries[0]["channel"] == "webhook"
    assert deliveries[0]["status"] == "delivered"
    assert deliveries[0]["attempts"] == 1
    # ...and a second relay pass finds nothing pending (no
    # double-delivery).
    results = asyncio.run(
        hosted_svc.process_deliveries(
            http_env.mock_client, http_env.store, ORIGIN
        )
    )
    assert results == []
    assert len(state["hooks"]) == 1


def test_webhook_rejects_private_and_http_targets(http_env):
    body = _claim(http_env)
    session = _session(body)
    for bad in ("http://93.184.216.34/hook", "https://127.0.0.1/hook",
                "https://10.0.0.5/hook", "not-a-url"):
        r = _configure(http_env, session, webhook_url=bad, webhook_enabled=True)
        assert r.status_code == 400, (bad, r.text)


def test_email_relay_disabled_by_default(http_env):
    body = _claim(http_env)
    session = _session(body)
    r = _configure(
        http_env, session, email="ops@example.com", email_enabled=True
    )
    assert r.status_code == 200, r.text
    assert r.json()["hosted_inbox"]["email"]["relay_enabled"] is False
    r = http_env.post(_hosted_path(body), json=_knock())
    assert r.json()["stored"] is True
    # The relay pass records the honest "disabled" outcome on the
    # delivery log — visible evidence, zero attempts, nothing sent.
    r = http_env.get(
        "/v1/claim/inbox", params={"origin": ORIGIN}, headers=session
    )
    deliveries = r.json()["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["channel"] == "email"
    assert deliveries[0]["status"] == "disabled"
    assert deliveries[0]["attempts"] == 0
    assert "disabled" in deliveries[0]["detail"]


# --- claims history snapshot (the round-2 caveat) -------------------------------


HISTORY_DOC = {
    "origins": {
        ORIGIN: {
            "defects": [
                {
                    "route": ROUTE,
                    "code": "no_402",
                    "state": "open",
                    "first_seen": "2026-10-28T01:00:00Z",
                    "last_seen": "2026-10-29T01:00:00Z",
                    "expected": "an HTTP 402 challenge",
                    "observed": "HTTP 200",
                }
            ]
        }
    }
}


def test_internal_claims_history_gates_and_serves(http_env, monkeypatch):
    body = _claim(http_env)
    session = _session(body)
    # Before any upload: the honest not-loaded marker (no file set).
    r = http_env.get(
        "/v1/claim/status", params={"origin": ORIGIN}, headers=session
    )
    assert r.json()["history"]["history_not_loaded"] is True
    # No secret configured on the service → the endpoint is absent.
    r = http_env.post("/v1/internal/claims-history", json=HISTORY_DOC)
    assert r.status_code == 404
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", "test-secret")
    r = http_env.post(
        "/v1/internal/claims-history",
        json=HISTORY_DOC,
        headers={"x-fleet-internal-secret": "wrong"},
    )
    assert r.status_code == 403
    r = http_env.post(
        "/v1/internal/claims-history",
        json={"unrelated": True},
        headers={"x-fleet-internal-secret": "test-secret"},
    )
    assert r.status_code == 400
    r = http_env.post(
        "/v1/internal/claims-history",
        json=HISTORY_DOC,
        headers={"x-fleet-internal-secret": "test-secret"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["origins"] == 1
    assert r.json()["defect_rows"] == 1
    # Now the status read serves the stored snapshot.
    r = http_env.get(
        "/v1/claim/status", params={"origin": ORIGIN}, headers=session
    )
    history = r.json()["history"]
    assert history["loaded"] is True
    assert history["summary"]["total"] == 1
    assert history["summary"]["open"] == 1
    assert history["defects"][0]["defect_class"] == "no_402"


# --- pricing / discovery invariants ----------------------------------------------


def test_hosted_paths_are_not_priced_and_discovery_unmoved(http_env):
    key = hosted_svc.origin_key(ORIGIN)
    for path in (
        f"/v1/hosted-inbox/{key}",
        "/v1/claim/inbox",
        "/v1/claim/inbox/config",
        "/v1/internal/claims-history",
    ):
        assert path not in PRICE_UNITS
    body = http_env.get("/.well-known/x402").json()
    assert len(body["resources"]) == 29  # 28 priced + the free /quote
    urls = [e.get("resource") or "" for e in body["resources"]]
    assert not any("hosted-inbox" in u for u in urls)
