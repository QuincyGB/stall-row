"""Inspector Physical (Next-20 #9): the four-leg composition —
service-level semantics (leg order, derived headline, failure
discipline) plus the HTTP surface (unpaid 402 at exactly 50000,
Fleet Pass debiting exactly 50000 once, pack credits refused
cleanly). Fully offline: MockTransport plays the targets."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, guard, physical, recheck
from app.services.indexfeed import IndexStore

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
URL = ORIGIN + "/v1/thing"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def index_store(tmp_path):
    return IndexStore(str(tmp_path / "index.db"))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _v2_response(network="eip155:8453", pay_to=OWNER, amount="5000"):
    challenge = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": URL, "description": "d", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "asset": USDC,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
            }
        ],
    }
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _record(store, url=URL, at="2026-10-30T00:00:00Z"):
    return store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": "x402",
            "status": "BASELINE",
            "checked_at": at,
            "current": {
                "status_code": 402,
                "amount": "5000",
                "asset": USDC,
                "network": "eip155:8453",
                "payTo": OWNER,
            },
        }
    )


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- composition ---------------------------------------------------------------


def test_happy_path_all_sections_and_guard_headline(store, index_store):
    _record(store)  # prior history: book found, guard history known

    async def direct_guard():
        return await guard.guard(
            _client(lambda r: _v2_response()), store, index_store, URL
        )

    expected = run(direct_guard())
    assert expected["verdict"] == "PROCEED"
    before = store.check_history_count()

    async def go():
        return await physical.physical(
            _client(lambda r: _v2_response()), store, index_store, URL
        )

    out = run(go())
    # All four sections, under their own names.
    assert out["sign"]["challenge_found"] is True
    assert out["sign"]["accepts"][0]["amount_atomic"] == "5000"
    assert out["book"]["found"] is True
    assert out["book"]["target"] == {"type": "url", "value": URL}
    # The headline IS the guard leg's verdict, matching a direct
    # guard call on the same fixture state.
    assert out["guard"] == expected
    assert out["verdict"] == expected["verdict"] == "PROCEED"
    assert out["summary"]["verdict"] == "PROCEED"
    assert out["summary"]["recheck_status"] == out["recheck"]["status"]
    # The recheck leg recorded, through the normal path.
    assert out["recheck"]["recorded"] is True
    assert out["recheck"]["status"] in ("BASELINE", "UNCHANGED", "CHANGED")
    assert store.check_history_count() == before + 1
    feed = store.recent_checks(limit=5)
    assert feed[0]["url"] == URL
    assert feed[0]["status"] == out["recheck"]["status"]


def test_unrecorded_target_book_found_false_inside_delivered_physical(
    store, index_store
):
    async def go():
        return await physical.physical(
            _client(lambda r: _v2_response()), store, index_store, URL
        )

    out = run(go())
    assert out["book"]["found"] is False
    assert out["book"]["live_checks"]["checks"] == 0
    # No history when the call arrived caps the guard at CAUTION —
    # the physical's own recheck (recorded after the guard leg ran)
    # must not lift it.
    assert out["verdict"] == "CAUTION"
    assert any(r["code"] == "no_history" for r in out["guard"]["reasons"])
    assert out["recheck"]["status"] == "BASELINE"
    assert out["recheck"]["recorded"] is True


def test_unreachable_target_is_a_delivered_report(store, index_store):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async def go():
        return await physical.physical(
            _client(handler), store, index_store, URL
        )

    out = run(go())
    assert out["sign"]["challenge_found"] is False
    assert any(d["code"] == "unreachable" for d in out["sign"]["defects"])
    assert out["book"]["found"] is False
    assert out["guard"]["verdict"] == "ABORT"
    assert out["verdict"] == "ABORT"
    # The recheck probe failed: rendered as the leg's section,
    # nothing recorded, stored state untouched.
    assert out["recheck"]["status"] == "PROBE_FAILED"
    assert out["recheck"]["recorded"] is False
    assert out["summary"]["recheck_status"] == "PROBE_FAILED"
    assert store.check_history_count() == 0
    assert store.count() == 0


def test_ssrf_refused_target_raises_valueerror(store, index_store):
    async def go():
        return await physical.physical(
            _client(lambda r: _v2_response()),
            store,
            index_store,
            "https://127.0.0.1:8080/x",
        )

    with pytest.raises(ValueError):
        run(go())
    assert store.check_history_count() == 0


def test_non_url_targets_refused_cleanly(store, index_store):
    async def go(target):
        return await physical.physical(
            _client(lambda r: _v2_response()), store, index_store, target
        )

    # Empty, and a bare payTo (a Score Book shape the physical
    # deliberately does not serve): both 422-class ValueErrors.
    with pytest.raises(ValueError):
        run(go("   "))
    with pytest.raises(ValueError):
        run(go(OWNER))


# --- HTTP surface ---------------------------------------------------------------


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: _v2_response())
    )
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def test_unpaid_challenge_is_exactly_50000():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/physical", json={"url": URL})
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert challenge["accepts"][0]["amount"] == "50000"
    assert challenge["accepts"][0]["payTo"] == OWNER


def test_fleet_pass_debits_exactly_50000_once(http_env):
    fleet_pass, created = http_env.mint_pass(
        "pay-phys", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    assert created
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/physical",
        json={"url": URL},
        headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] in ("PROCEED", "CAUTION", "ABORT")
    assert body["recheck"]["recorded"] is True
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == (
        5_000_000 - 50_000
    )
    assert http_env.pass_debit_count(fleet_pass["pass_id"]) == 1


def test_fleet_pass_ssrf_refusal_refunds_the_debit(http_env):
    fleet_pass, _ = http_env.mint_pass(
        "pay-phys-ssrf", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/physical",
        json={"url": "https://127.0.0.1:8080/x"},
        headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
    )
    assert r.status_code == 422
    # Reserve-then-run: our refusal refunds the debit in full.
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000


def test_toolkit_pack_id_refused_cleanly(http_env):
    pack, created = http_env.mint_toolkit_pack("pay-tk")
    assert created
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/physical", json={"url": URL, "pack_id": pack["pack_id"]}
    )
    assert r.status_code == 422
    assert r.json()["code"] == "pack_not_accepted"

    r = client.post(
        "/v1/physical",
        json={"url": URL},
        headers={"X-Toolkit-Pack": pack["pack_id"]},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "pack_not_accepted"

    # Nothing spent, nothing delivered, nothing recorded.
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125
    assert http_env.check_history_count() == 0
