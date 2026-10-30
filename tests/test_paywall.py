"""The paywall contract for the whole row: every unpaid call gets a 402
whose accepts carry that route's exact price (USDC on Base, owner
payTo) and the route's full Bazaar discovery extension; free routes
stay open."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"

BODIES = {
    "/v1/wallet-snapshot": {"address": OWNER},
    "/v1/allowance-audit": {"address": OWNER},
    "/v1/tx-decoder": {"tx_hash": "0x" + "ab" * 32},
    "/v1/gas-oracle": {},
    "/v1/wallet-verdict": {"address": OWNER},
    "/v1/pre-trade-guard": {"pay_to": OWNER},
    "/v1/tube-search": {"query": "x402"},
    "/v1/page-to-markdown": {"url": "https://www.x402.org/"},
    "/v1/repo-vitals": {"repo": "QuincyGB/token-verdict"},
    "/v1/price-check": {"keywords": "token verdict"},
    "/v1/card-smith": {"mode": "grade", "card": {"name": "x"}},
    "/v1/job-scout": {},
    "/v1/recheck": {"url": "https://www.x402.org/"},
    "/v1/index/changes": {},
    "/v1/orchestrate": {"address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631", "service_url": "https://www.x402.org/", "listing_url": "https://www.x402.org/"},
    "/v1/counterparty-report": {"pay_to": OWNER},
    "/v1/grade-my-stall": {"resource_url": "https://www.x402.org/"},
    "/v1/receipt-verify": {"tx_hash": "0x" + "ab" * 32},
    "/v1/flagged-paytos": {},
    "/v1/agent-card-verify": {"agent_id": 97809},
    "/v1/recheck/pack": {"url": "https://www.x402.org/"},
    "/v1/fleet-check": {"origins": ["https://www.x402.org"]},
    "/v1/fleet-check/pack": {"origins": ["https://www.x402.org"]},
    "/v1/fleet-pass": {},
    "/v1/fleet-pass-20": {},
    "/v1/sign-read": {"url": "https://www.x402.org/"},
    "/v1/score-book": {"url": "https://www.x402.org/"},
    "/v1/guard": {"url": "https://www.x402.org/"},
}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _challenge(response):
    raw = response.headers.get("payment-required")
    assert raw, "402 response must carry a payment-required header"
    return json.loads(base64.b64decode(raw))


SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAY_TO = "FeyyAfoCbn1psQ6x8gxN8YQbXZVpTSibZwP6mkXckXUJ"
# The two Fleet Check routes carry a second, Solana accept (same USD
# price) — every other route stays single-accept Base, exactly as before.
DUAL_ACCEPT_PATHS = {"/v1/fleet-check", "/v1/fleet-check/pack"}


@pytest.mark.parametrize("path", sorted(PRICE_UNITS))
def test_unpaid_route_is_paywalled_at_exact_price(client, path):
    r = client.post(path, json=BODIES[path])
    assert r.status_code == 402, f"{path} -> {r.status_code}"
    challenge = _challenge(r)
    assert challenge["x402Version"] == 2
    accepts = challenge["accepts"]
    assert len(accepts) == (2 if path in DUAL_ACCEPT_PATHS else 1)
    a = accepts[0]
    assert a["scheme"] == "exact"
    assert a["network"] == "eip155:8453"
    assert a["asset"] == USDC
    assert a["amount"] == str(PRICE_UNITS[path]), f"{path} amount {a['amount']}"
    assert a["payTo"] == main.settings.pay_to == OWNER
    if path in DUAL_ACCEPT_PATHS:
        s = accepts[1]
        assert s["scheme"] == "exact"
        assert s["network"] == SOLANA_NETWORK
        assert s["asset"] == SOLANA_USDC
        assert s["amount"] == str(PRICE_UNITS[path])
        assert s["payTo"] == SOLANA_PAY_TO


@pytest.mark.parametrize("path", sorted(PRICE_UNITS))
def test_402_carries_full_bazaar_extension(client, path):
    r = client.post(path, json=BODIES[path])
    out = _challenge(r)["extensions"]["bazaar"]["info"]["output"]
    assert "schema" in out, f"{path} extension output has no schema (hollow)"
    assert "properties" in out["schema"], f"{path} output schema types no fields"


def test_free_routes_stay_open(client):
    assert client.get("/health").status_code == 200
    quote = client.get("/quote")
    assert quote.status_code == 200
    body = quote.json()
    assert set(body["endpoints"]) == set(PRICE_UNITS)
    assert client.get("/.well-known/x402").status_code == 200
    assert client.get("/skill.md").status_code == 200
    assert client.get("/llms.txt").status_code == 200
