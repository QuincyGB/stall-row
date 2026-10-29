"""Receipt Verifier demo route: POST /v1/receipt-verify/demo —
the free widget backend. Same verify_receipt as the paid route;
unknown hashes are honest 404s, malformed hashes 422s, and the
demo is never a priced route. Offline: tests.fixtures plays the
RPC."""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, load_settings
from app.rpc import Rpc
from app.services import probe as probe_svc
from tests.fixtures import ADDR, OTHER, TX_HASH, mock_client

S = load_settings()


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(probe_svc, "RATE_LIMITER", probe_svc._RateLimiter())


@pytest.fixture()
def http_env(monkeypatch):
    def fake_clients(**kw):
        client = mock_client(**kw)
        return client, Rpc(client, S.rpc_url), None

    monkeypatch.setattr(main, "_clients", lambda: fake_clients())
    return fake_clients


def test_demo_verifies_fixture_receipt(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/receipt-verify/demo", json={"tx_hash": TX_HASH})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["demo"] is True
    assert body["payer"] == OTHER and body["pay_to"] == ADDR
    assert body["amount_usd"] == "12.5" and body["settled"] is True
    assert body["paid_route"] == "POST /v1/receipt-verify"


def test_demo_unknown_tx_is_404(http_env, monkeypatch):
    client_holder = {}

    def fake_clients():
        c = mock_client(rpc_tx_missing=True)
        return c, Rpc(c, S.rpc_url), None

    monkeypatch.setattr(main, "_clients", fake_clients)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/receipt-verify/demo", json={"tx_hash": TX_HASH})
    assert r.status_code == 404, r.text
    assert r.json()["verified"] is False


def test_demo_malformed_hash_is_422(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/receipt-verify/demo", json={"tx_hash": "0x1234"})
    assert r.status_code == 422, r.text
    assert r.json()["verified"] is False


def test_demo_is_not_a_priced_route(http_env):
    assert "/v1/receipt-verify/demo" not in PRICE_UNITS
    client = TestClient(main.app, raise_server_exceptions=False)
    doc = client.get("/.well-known/x402").json()
    urls = [e.get("resource") or "" for e in doc.get("resources", [])]
    assert not any("/v1/receipt-verify/demo" in u for u in urls)
