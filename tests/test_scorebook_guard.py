"""Score Book + Guard: track-record aggregates from the stores the
service already holds (recheck history + Index snapshots), and the
composed pay/caution/avoid verdict. Fully offline: fabricated
recorded checks, fabricated snapshots, MockTransport targets."""

import asyncio
import base64
import json

import httpx
import pytest

from app.services import guard, recheck, scorebook, verdict
from app.services.indexfeed import IndexStore

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
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


def _record(store, url=URL, status="UNCHANGED", amount="5000", pay_to=OWNER,
            at="2026-10-30T00:00:00Z"):
    return store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": "x402",
            "status": status,
            "checked_at": at,
            "current": {
                "status_code": 402,
                "amount": amount,
                "asset": USDC,
                "network": "eip155:8453",
                "payTo": pay_to,
            },
        }
    )


def _snapshot(index_store, rows):
    return index_store.save_snapshot(
        [
            {
                "resource": r[0],
                "serviceName": "svc",
                "description": "d",
                "amount": "5000",
                "asset": USDC,
                "network": "eip155:8453",
                "payTo": r[1],
                "lastUpdated": "2026-10-30T00:00:00Z",
                "calls30d": 1,
                "payers30d": 1,
            }
            for r in rows
        ]
    )


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- score book ---------------------------------------------------------------


def test_score_book_url_aggregates(store, index_store):
    _record(store, status="BASELINE", at="2026-10-29T00:00:00Z")
    _record(store, status="CHANGED", amount="6000", at="2026-10-30T00:00:00Z")
    _record(store, status="UNCHANGED", amount="6000", at="2026-10-30T01:00:00Z")
    _snapshot(index_store, [(URL, OWNER)])

    book = scorebook.score_book(store, index_store, url=URL)
    assert book["found"] is True
    live = book["live_checks"]
    assert live["checks"] == 3
    assert live["by_status"] == {"BASELINE": 1, "CHANGED": 1, "UNCHANGED": 1}
    assert live["amounts_seen"] == ["5000", "6000"]
    assert live["price_changes"] == 1
    assert live["payto_changes"] == 0
    assert live["current_fingerprint"]["amount"] == "6000"
    assert book["catalogue"]["present_in_snapshots"] == 1
    assert book["catalogue"]["in_latest_snapshot"] is True


def test_score_book_unknown_url_is_found_false(store, index_store):
    book = scorebook.score_book(store, index_store, url=URL)
    assert book["found"] is False
    assert book["live_checks"]["checks"] == 0
    assert book["live_checks"]["current_fingerprint"] is None


def test_score_book_pay_to(store, index_store):
    _record(store)
    _record(store, url=ORIGIN + "/v1/other")
    _snapshot(index_store, [(URL, OWNER), (ORIGIN + "/v1/x", OTHER)])
    book = scorebook.score_book(store, index_store, pay_to=OWNER)
    assert book["found"] is True
    assert book["live_checks"]["checks"] == 2
    assert book["live_checks"]["resources_seen"] == [ORIGIN + "/v1/other", URL]
    assert book["catalogue"]["listings_in_latest_snapshot"] == [URL]


def test_score_book_origin(store, index_store):
    _record(store)
    _record(store, url=ORIGIN + "/v1/other", status="CHANGED")
    _record(store, url="https://example.com/v1/elsewhere")
    book = scorebook.score_book(store, index_store, origin=ORIGIN)
    assert book["found"] is True
    assert book["totals"]["resources_seen"] == 2
    assert book["totals"]["checks"] == 2
    assert book["totals"]["changed_count"] == 1


def test_score_book_requires_exactly_one_target(store, index_store):
    with pytest.raises(ValueError):
        scorebook.score_book(store, index_store)
    with pytest.raises(ValueError):
        scorebook.score_book(store, index_store, url=URL, pay_to=OWNER)


# --- guard ----------------------------------------------------------------------


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


def test_guard_proceed_when_clean_and_known(store, index_store):
    _record(store)

    async def go():
        return await guard.guard(
            _client(lambda r: _v2_response()), store, index_store, URL
        )

    out = run(go())
    assert out["verdict"] == "PROCEED"
    assert out["terms"]["amount_decimal"] == "0.005"
    assert out["payto_flag"] is None


def test_guard_caution_when_no_history(store, index_store):
    async def go():
        return await guard.guard(
            _client(lambda r: _v2_response()), store, index_store, URL
        )

    out = run(go())
    assert out["verdict"] == "CAUTION"
    assert any(r["code"] == "no_history" for r in out["reasons"])


def test_guard_abort_on_sdk_breaking_sign(store, index_store):
    _record(store)

    async def go():
        return await guard.guard(
            _client(lambda r: _v2_response(network="algorand")),
            store,
            index_store,
            URL,
        )

    out = run(go())
    assert out["verdict"] == "ABORT"
    assert any(r["code"] == "sdk_unparseable" for r in out["reasons"])


def test_guard_abort_on_flagged_payto(store, index_store):
    flagged = sorted(verdict.SNAPSHOT["addresses"])[0]

    async def go():
        return await guard.guard(
            _client(lambda r: _v2_response(pay_to=flagged)),
            store,
            index_store,
            URL,
        )

    out = run(go())
    assert out["verdict"] == "ABORT"
    assert any(r["code"] == "payto_flagged" for r in out["reasons"])
    assert out["payto_flag"]["reasons"] == ["ofac_sdn"]


def test_guard_abort_when_unreachable(store, index_store):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async def go():
        return await guard.guard(_client(handler), store, index_store, URL)

    out = run(go())
    assert out["verdict"] == "ABORT"
    assert any(r["code"] == "no_readable_sign" for r in out["reasons"])
