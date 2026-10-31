"""Graceful-failure pass (Next-20 #18): caller faults answer in the
fleet {error, code} shape naming the field, and no bare 500 ever
leaves the service.

The paid-path tests bypass the x402 middleware in-process (the
paywall contract itself is pinned elsewhere: unpaid probes of every
shape still get the 402 first).
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture()
def client(monkeypatch):
    async def passthrough(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(main, "_payment_middleware", passthrough)
    return TestClient(main.app, raise_server_exceptions=False)


def test_missing_field_is_named_in_fleet_shape(client):
    r = client.post("/v1/wallet-snapshot", json={})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "'address' is required" in body["error"]


def test_sign_read_missing_url_is_named_in_fleet_shape(client):
    r = client.post("/v1/sign-read", json={})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "'url' is required" in body["error"]


def test_malformed_json_is_named_in_fleet_shape(client):
    r = client.post(
        "/v1/wallet-snapshot",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "JSON" in body["error"]


def test_wrong_type_is_named_in_fleet_shape(client):
    r = client.post("/v1/tx-decoder", json={"tx_hash": {"x": 1}})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "'tx_hash'" in body["error"]


def test_unhandled_failure_is_fleet_shaped_502(client, monkeypatch):
    def boom(pass_id):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(main._recheck_store, "get_pass", boom)
    r = client.post("/v1/fleet-pass/balance", json={"pass_id": "fp_whatever"})
    assert r.status_code == 502
    assert r.json() == {"error": "internal failure", "code": "internal_error"}
