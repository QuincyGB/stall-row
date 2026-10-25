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
    "/v1/tube-search": {"query": "x402"},
    "/v1/page-to-markdown": {"url": "https://www.x402.org/"},
    "/v1/repo-vitals": {"repo": "QuincyGB/token-verdict"},
    "/v1/price-check": {"keywords": "token verdict"},
    "/v1/card-smith": {"mode": "grade", "card": {"name": "x"}},
    "/v1/job-scout": {},
    "/v1/recheck": {"url": "https://www.x402.org/"},
    "/v1/recheck/pack": {"url": "https://www.x402.org/"},
}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _challenge(response):
    raw = response.headers.get("payment-required")
    assert raw, "402 response must carry a payment-required header"
    return json.loads(base64.b64decode(raw))


@pytest.mark.parametrize("path", sorted(PRICE_UNITS))
def test_unpaid_route_is_paywalled_at_exact_price(client, path):
    r = client.post(path, json=BODIES[path])
    assert r.status_code == 402, f"{path} -> {r.status_code}"
    challenge = _challenge(r)
    assert challenge["x402Version"] == 2
    accepts = challenge["accepts"]
    assert len(accepts) == 1
    a = accepts[0]
    assert a["scheme"] == "exact"
    assert a["network"] == "eip155:8453"
    assert a["asset"] == USDC
    assert a["amount"] == str(PRICE_UNITS[path]), f"{path} amount {a['amount']}"
    assert a["payTo"] == main.settings.pay_to == OWNER


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
