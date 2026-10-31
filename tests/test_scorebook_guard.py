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


# --- score book: patrol fold-in (Next-20 #16) ---------------------------------

PATROL_URL = ORIGIN + "/v1/patrolled"
RECOVERED_URL = ORIGIN + "/v1/fixed"
DUAL_URL = ORIGIN + "/v1/dual"


def _patrol_snapshot():
    """Fabricated patrol snapshot in the committed file's shape
    (same fixture family as the Ledger Dataset tests)."""
    return {
        "kind": "verdict-row-patrol-snapshot/v1",
        "generated_at": "2026-10-31T12:42:27Z",
        "snapshot_id": "b" * 64,
        "source": {
            "patrol": "Dima (PC patrol walker)",
            "patrol_run_id": "20261031T120333Z",
            "patrol_as_of": "2026-10-31T12:03:33Z",
            "patrol_runs_total": 179,
            "patrol_stalls_scanned_total": 11272,
            "patrol_resources_probed_total": 11351,
            "open_defects": 3,
            "recoveries": 2,
            "receipts_index": "recovery-receipts/index.json",
        },
        "routes": [
            {
                "url": PATROL_URL,
                "origin": ORIGIN,
                "status": "open",
                "defect_class": "unexpected_status",
                "first_seen": "2026-10-30T17:31:18Z",
                "last_seen": "2026-10-31T08:02:59Z",
            },
            {
                "url": RECOVERED_URL,
                "origin": ORIGIN,
                "status": "recovered",
                "defect_class": "unreachable",
                "first_seen": "2026-10-28T10:47:09Z",
                "recovered_at": "2026-10-30T09:18:15Z",
                "receipt_id": "rr-0123456789abcdef0123",
            },
            {
                "url": DUAL_URL,
                "origin": ORIGIN,
                "status": "recovered",
                "defect_class": "unreachable",
                "first_seen": "2026-10-27T10:47:09Z",
                "recovered_at": "2026-10-29T09:18:15Z",
                "receipt_id": "rr-aaaaaaaaaaaaaaaaaaaa",
            },
            {
                "url": DUAL_URL,
                "origin": ORIGIN,
                "status": "open",
                "defect_class": "sign_broken",
                "first_seen": "2026-10-30T11:00:00Z",
                "last_seen": "2026-10-31T07:00:00Z",
            },
            {
                "url": URL,
                "origin": ORIGIN,
                "status": "open",
                "defect_class": "unexpected_status",
                "first_seen": "2026-10-30T17:31:18Z",
                "last_seen": "2026-10-31T08:02:59Z",
            },
        ],
    }


def test_score_book_patrol_only_url_is_found_with_provenance(store, index_store):
    book = scorebook.score_book(
        store, index_store, url=PATROL_URL, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True
    # The patrol section is the whole of its record — the other
    # sources stay empty so nothing blurs.
    assert book["live_checks"]["checks"] == 0
    assert book["catalogue"]["present_in_snapshots"] == 0
    patrol = book["patrol"]
    assert patrol["open_defect_count"] == 1
    assert patrol["open_defects"] == [
        {
            "url": PATROL_URL,
            "origin": ORIGIN,
            "defect_class": "unexpected_status",
            "first_seen": "2026-10-30T17:31:18Z",
            "last_seen": "2026-10-31T08:02:59Z",
        }
    ]
    assert patrol["recovery_count"] == 0
    prov = patrol["provenance"]
    assert prov["id"] == "b" * 64
    assert prov["generated_at"] == "2026-10-31T12:42:27Z"
    assert prov["patrol_run_id"] == "20261031T120333Z"
    assert prov["patrol_stalls_scanned_total"] == 11272
    assert prov["patrol_resources_probed_total"] == 11351
    assert "not continuously" in patrol["coverage"]


def test_score_book_merges_stores_with_sources_distinguishable(store, index_store):
    _record(store)  # URL in the recheck history AND in the patrol snapshot
    book = scorebook.score_book(
        store, index_store, url=URL, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True
    assert book["live_checks"]["checks"] == 1
    # The patrol defect does NOT leak into the check aggregates.
    assert book["live_checks"]["by_status"] == {"UNCHANGED": 1}
    assert book["patrol"]["open_defect_count"] == 1
    assert book["patrol"]["open_defects"][0]["url"] == URL


def test_score_book_open_defect_and_recovery_both_present(store, index_store):
    book = scorebook.score_book(
        store, index_store, url=DUAL_URL, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True
    patrol = book["patrol"]
    assert patrol["open_defect_count"] == 1
    assert patrol["open_defects"][0]["defect_class"] == "sign_broken"
    assert patrol["recovery_count"] == 1
    assert patrol["recoveries"][0]["recovered_at"] == "2026-10-29T09:18:15Z"
    assert patrol["receipt_ids"] == ["rr-aaaaaaaaaaaaaaaaaaaa"]


def test_score_book_patrol_recovery_carries_receipt(store, index_store):
    book = scorebook.score_book(
        store, index_store, url=RECOVERED_URL, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True
    assert book["patrol"]["receipt_ids"] == ["rr-0123456789abcdef0123"]
    assert book["patrol"]["recoveries"][0]["defect_class"] == "unreachable"


def test_score_book_patrol_only_origin(store, index_store):
    book = scorebook.score_book(
        store, index_store, origin=ORIGIN, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True
    assert book["resources"] == {}
    assert book["totals"]["patrol_routes"] == 4
    assert book["totals"]["patrol_open_defects"] == 3
    assert book["totals"]["patrol_recoveries"] == 2
    assert book["patrol"]["open_defect_count"] == 3
    assert book["patrol"]["recovery_count"] == 2
    assert set(book["patrol"]["receipt_ids"]) == {
        "rr-0123456789abcdef0123",
        "rr-aaaaaaaaaaaaaaaaaaaa",
    }


def test_score_book_unknown_everywhere_still_found_false(store, index_store):
    book = scorebook.score_book(
        store,
        index_store,
        url=ORIGIN + "/v1/never-seen",
        patrol_snapshot=_patrol_snapshot(),
    )
    assert book["found"] is False
    assert book["patrol"]["open_defect_count"] == 0
    assert book["patrol"]["recovery_count"] == 0
    # Provenance is still cited — the snapshot was consulted.
    assert book["patrol"]["provenance"]["id"] == "b" * 64


def test_score_book_window_labelling_names_patrol_snapshot(store, index_store):
    book = scorebook.score_book(
        store, index_store, url=PATROL_URL, patrol_snapshot=_patrol_snapshot()
    )
    limits = book["limits"]
    assert "2026-10-31T12:42:27Z" in limits  # snapshot generated_at
    assert "2026-10-31T12:03:33Z" in limits  # patrol as-of
    assert "20261031T120333Z" in limits  # patrol run id
    assert "periodic, not continuous" in limits
    assert "HISTORY_WINDOW" not in limits and "1000" in limits


def test_score_book_pay_to_patrol_section_explicitly_empty(store, index_store):
    _record(store)
    book = scorebook.score_book(
        store, index_store, pay_to=OWNER, patrol_snapshot=_patrol_snapshot()
    )
    assert book["found"] is True  # from the recheck side, as before
    patrol = book["patrol"]
    assert patrol["open_defects"] == []
    assert patrol["recoveries"] == []
    assert patrol["receipt_ids"] == []
    # The emptiness is explained, not implied coverage.
    assert "no payTo data" in patrol["coverage"]
    assert patrol["provenance"]["id"] == "b" * 64


def test_score_book_no_patrol_snapshot_means_no_patrol_side(store, index_store):
    book = scorebook.score_book(
        store, index_store, url=PATROL_URL, patrol_snapshot=None
    )
    assert book["found"] is False
    assert book["patrol"]["provenance"] is None
    assert book["patrol"]["open_defect_count"] == 0
    assert "no patrol snapshot is held" in book["limits"]


def test_score_book_real_committed_snapshot(store, index_store):
    """Default loading folds the REAL committed patrol snapshot:
    a route with a signed patrol recovery and no other record is
    found, with the receipt id the ledger tests also pin."""
    book = scorebook.score_book(
        store, index_store, url="https://402oracle.com/verify/price"
    )
    assert book["found"] is True
    assert book["live_checks"]["checks"] == 0
    assert book["patrol"]["receipt_ids"] == ["rr-1e12dde0c299cb395d37"]
    assert book["patrol"]["provenance"]["generated_at"] == "2026-10-31T12:42:27Z"
    open_book = scorebook.score_book(
        store, index_store, url="https://1gateway.dev/v1/ai/llm-prices"
    )
    assert open_book["found"] is True
    assert open_book["patrol"]["open_defects"][0]["defect_class"] == (
        "unexpected_status"
    )


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


def test_guard_patrol_only_history_still_caution(store, index_store, monkeypatch):
    """Next-20 #16 interaction: this URL is known ONLY to the
    patrol snapshot (open defect, no recorded checks, no catalogue
    presence), so the Score Book now answers found=true for it.
    Guard's bands were tuned on check history and must NOT
    silently change: patrol knowledge alone does not lift the
    no-history CAUTION, and no patrol-based reason is added."""
    from app.services import patrolsnap

    monkeypatch.setattr(
        patrolsnap, "load_snapshot", lambda path=None: _patrol_snapshot()
    )
    book = scorebook.score_book(store, index_store, url=PATROL_URL)
    assert book["found"] is True
    assert book["live_checks"]["checks"] == 0

    async def go():
        return await guard.guard(
            _client(lambda r: _v2_response()), store, index_store, PATROL_URL
        )

    out = run(go())
    assert out["verdict"] == "CAUTION"
    assert any(r["code"] == "no_history" for r in out["reasons"])
    # The patrol section is passed through for transparency only.
    assert out["history"]["patrol"]["open_defect_count"] == 1


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
