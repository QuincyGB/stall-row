"""Fleet Pass: minting (payment-replay idempotent), the debit
ledger (table-priced, atomic, refundable exactly once), the refusal
matrix, free balance reads, and the HTTP surface — the unpaid 402
challenge, the free debit/refund/balance routes, and a pass-paid
StallRow call through the middleware bypass. Fully offline."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.services import fleetpass, recheck
from app.services.recheck import PackError

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
WEB_URL = "https://93.184.216.34/story"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _mint(store, key="pay-1", **kw):
    fleet_pass, created = store.mint_pass(
        key, fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS, **kw
    )
    assert created
    return fleet_pass


# --- minting -----------------------------------------------------------------

def test_mint_pass_shape(store):
    fleet_pass = _mint(store)
    assert fleet_pass["pass_id"].startswith("fpass_")
    assert len(fleet_pass["pass_id"]) == len("fpass_") + 32
    assert fleet_pass["balance_units"] == 5_000_000
    assert fleet_pass["balance_usd"] == 5.0
    assert fleet_pass["expired"] is False


def test_mint_pass_idempotent_on_replay(store):
    first, c1 = store.mint_pass("nonce-1", 5_000_000, 90)
    second, c2 = store.mint_pass("nonce-1", 5_000_000, 90)
    assert c1 is True and c2 is False
    assert second["pass_id"] == first["pass_id"]


# --- debits --------------------------------------------------------------------

def test_debit_charges_table_price_and_records(store):
    fleet_pass = _mint(store)
    price = fleetpass.price_for("token-verdict", "/v1/verdict")
    out = store.debit_pass(fleet_pass["pass_id"], "token-verdict", "/v1/verdict", price)
    assert out["debited_units"] == 20_000
    assert out["balance_units"] == 5_000_000 - 20_000
    assert out["debit_id"].startswith("fpd_")
    assert store.pass_debit_count(fleet_pass["pass_id"]) == 1


def test_refund_recredits_exactly_once(store):
    fleet_pass = _mint(store)
    out = store.debit_pass(fleet_pass["pass_id"], "lock-smith", "/v1/audit", 500_000)
    assert store.refund_debit(out["debit_id"]) is True
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000
    assert store.refund_debit(out["debit_id"]) is False
    assert store.refund_debit("fpd_nope") is False
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000


def test_debit_refusals_debit_nothing(store):
    fleet_pass = _mint(store)
    with pytest.raises(PackError) as e:
        store.debit_pass("fpass_nope", "stall-row", "/v1/recheck", 5_000)
    assert e.value.code == "unknown_pass" and e.value.http_status == 404

    stale, _ = store.mint_pass("nonce-stale", 5_000_000, 0)
    with pytest.raises(PackError) as e:
        store.debit_pass(stale["pass_id"], "stall-row", "/v1/recheck", 5_000)
    assert e.value.code == "pass_expired" and e.value.http_status == 410

    small, _ = store.mint_pass("nonce-small", 4_999, 90)
    with pytest.raises(PackError) as e:
        store.debit_pass(small["pass_id"], "stall-row", "/v1/recheck", 5_000)
    assert e.value.code == "pass_insufficient" and e.value.http_status == 402
    assert store.get_pass(small["pass_id"])["balance_units"] == 4_999
    assert store.pass_debit_count(fleet_pass["pass_id"]) == 0


def test_price_table_covers_all_five_services():
    services = {svc for (svc, _route) in fleetpass.FLEET_ROUTES}
    assert services == {
        "token-verdict", "route-verdict", "list-smith", "stall-row", "lock-smith"
    }
    # A pass cannot buy prepaid products.
    assert fleetpass.price_for("stall-row", "/v1/recheck/pack") is None
    assert fleetpass.price_for("stall-row", "/v1/fleet-pass") is None
    assert fleetpass.price_for("nope", "/v1/verdict") is None


# --- HTTP surface ---------------------------------------------------------------

def _fake_request(nonce="fp-nonce-1"):
    scope = {
        "type": "http", "method": "POST", "path": "/v1/fleet-pass",
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


def test_buy_handler_mints_and_replay_returns_same_pass(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    out = asyncio.run(main.fleet_pass_buy(_fake_request()))
    assert out["balance_units"] == 5_000_000 and out["price_units"] == 5_000_000
    replay = asyncio.run(main.fleet_pass_buy(_fake_request()))
    assert replay["pass_id"] == out["pass_id"]
    assert "already minted" in replay["note"]


def test_unpaid_fleet_pass_challenge_is_exactly_five_dollars():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/fleet-pass", json={})
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    accept = challenge["accepts"][0]
    assert accept["amount"] == "5000000"
    assert accept["payTo"] == OWNER


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    return fresh


def test_http_debit_balance_refund_routes(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/fleet-pass/debit",
        json={
            "pass_id": fleet_pass["pass_id"],
            "service": "list-smith",
            "route": "/v1/fix",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["balance_units"] == 4_500_000
    debit_id = r.json()["debit_id"]

    # The price comes from the table, never the body: an unknown
    # route is refused even with a valid pass.
    r = client.post(
        "/v1/fleet-pass/debit",
        json={"pass_id": fleet_pass["pass_id"], "service": "x", "route": "/y"},
    )
    assert r.status_code == 422

    r = client.post(
        "/v1/fleet-pass/balance", json={"pass_id": fleet_pass["pass_id"]}
    )
    assert r.status_code == 200
    assert r.json()["balance_usd"] == 4.5 and r.json()["debits"] == 1

    r = client.post("/v1/fleet-pass/refund", json={"debit_id": debit_id})
    assert r.json()["refunded"] is True
    r = client.post(
        "/v1/fleet-pass/balance", json={"pass_id": fleet_pass["pass_id"]}
    )
    assert r.json()["balance_units"] == 5_000_000

    r = client.post("/v1/fleet-pass/balance", json={"pass_id": "fpass_nope"})
    assert r.status_code == 404


def test_http_pass_pays_stallrow_route_and_failure_refunds(http_env, monkeypatch):
    fleet_pass = _mint(http_env)

    def ok_handler(request):
        return httpx.Response(
            200, text="a page", headers={"content-type": "text/plain"}
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "BASELINE"
    # Recheck is $0.005: exactly one table debit.
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 4_995_000

    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Fleet-Pass": "fpass_nope"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_pass"


# --- Fleet Pass v2 ($20 working pass) ------------------------------------------


def test_mint_pass_v2_denomination(store):
    fleet_pass, created = store.mint_pass(
        "pay-v2", fleetpass.PASS_V2_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    assert created
    assert fleet_pass["balance_units"] == 20_000_000
    assert fleet_pass["balance_usd"] == 20.0
    assert fleet_pass["total_usd"] == 20.0
    # Same ledger: a v2 pass debits at listed prices, no discounts.
    debit = store.debit_pass(
        fleet_pass["pass_id"], "stall-row", "/v1/index/changes", 100_000
    )
    assert debit["balance_units"] == 20_000_000 - 100_000


def test_fleet_pass_20_unpaid_402(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/fleet-pass-20", json={})
    assert r.status_code == 402
    required = r.headers.get("payment-required")
    assert required
    challenge = json.loads(base64.b64decode(required))
    amounts = {a["amount"] for a in challenge["accepts"]}
    assert "20000000" in amounts
