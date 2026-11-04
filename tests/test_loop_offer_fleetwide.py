"""Loop block fleet-wide + per-wallet loop memory (Next-20 #1 + #5,
2026-11-03): the verdict_row_loop block now attaches at the x402
middleware delivery layer, so EVERY successful paid delivery on
every priced StallRow route carries it — per-call, Recheck-pack,
toolkit-pack and Fleet-Pass funded alike — and a returning payer's
block is personalised from the store's payer_history (their own
calls / distinct days / total spent, plus an adaptive pack offer
with a wallet-bound deep link). Error responses and unpaid 402
challenges never carry it and are never recorded; prices and 402
amounts are unchanged.

The per-call tests bypass the payment middleware in-process with a
passthrough that stamps the verified payment payload on
request.state exactly as the real middleware does (the paywall
contract itself is pinned by test_paywall.py and the unpaid tests
below, which run against the real middleware)."""

import asyncio
import base64
import json
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.services import fleetpass, loop_offer, recheck

WALLET = "0x1111111111111111111111111111111111111111"
WALLET2 = "0x2222222222222222222222222222222222222222"
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
WEB_URL = "https://93.184.216.34/story"
SCORE_BOOK_UNITS = PRICE_UNITS["/v1/score-book"]


def _payload(wallet):
    return SimpleNamespace(
        payload=SimpleNamespace(
            authorization=SimpleNamespace(**{"from": wallet}),
            signature="0xdeadbeef",
        )
    )


def _settled_client(monkeypatch, tmp_path, wallet=WALLET):
    """TestClient on a fresh store whose payment middleware is a
    passthrough stamping `wallet`'s verified payload — a settled
    per-call delivery, in-process."""
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)

    async def passthrough(request, call_next):
        request.state.payment_payload = _payload(wallet)
        return await call_next(request)

    monkeypatch.setattr(main, "_payment_middleware", passthrough)
    return TestClient(main.app, raise_server_exceptions=False), fresh


def _score_book(client):
    return client.post("/v1/score-book", json={"pay_to": WALLET})


# --- (a) settled deliveries on non-Recheck priced routes ----------------

def test_per_call_delivery_on_non_recheck_route_carries_block(monkeypatch, tmp_path):
    client, _store = _settled_client(monkeypatch, tmp_path)
    r = _score_book(client)
    assert r.status_code == 200, r.text
    block = r.json()["verdict_row_loop"]
    assert block["offers"][0]["id"] == "recheck_pack"
    # First-time payer: the static block, no personalised section.
    assert "returning_payer" not in block


def test_toolkit_pack_funded_delivery_carries_block(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    pack, _ = fresh.mint_toolkit_pack("pay-toolkit-loop")
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/score-book", json={"pay_to": WALLET, "pack_id": pack["pack_id"]}
    )
    assert r.status_code == 200, r.text
    assert "verdict_row_loop" in r.json()
    assert "returning_payer" not in r.json()["verdict_row_loop"]


def test_fleet_pass_funded_delivery_carries_block(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    fpass, _ = fresh.mint_pass(
        "pay-pass-loop", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/score-book",
        json={"pay_to": WALLET},
        headers={"X-Fleet-Pass": fpass["pass_id"]},
    )
    assert r.status_code == 200, r.text
    assert "verdict_row_loop" in r.json()


# --- (b) a returning payer's delivery is personalised --------------------

def test_second_per_call_delivery_is_personalised(monkeypatch, tmp_path):
    client, store = _settled_client(monkeypatch, tmp_path)

    first = _score_book(client)
    assert "returning_payer" not in first.json()["verdict_row_loop"]

    second = _score_book(client)
    assert second.status_code == 200, second.text
    rp = second.json()["verdict_row_loop"]["returning_payer"]
    assert rp["identity"] == WALLET
    assert rp["identity_kind"] == "evm_wallet"
    assert rp["calls"] == 2
    assert rp["calls_this_route"] == 2
    assert rp["distinct_days"] == 1
    assert rp["total_spent_usd"] == 2 * SCORE_BOOK_UNITS / 1_000_000

    offer = rp["adaptive_offer"]
    assert offer["credits"] == recheck.PACK_CREDITS == 250
    assert offer["price_usd"] == recheck.PACK_PRICE_UNITS / 1_000_000 == 1.0
    assert "250" in offer["line"] and "2" in offer["line"]
    assert offer["deep_link"].endswith(f"/v1/recheck/pack?payer={WALLET}")
    assert offer["webhook_deep_link"].endswith(
        f"/v1/recheck/webhooks?payer={WALLET}"
    )

    history = store.payer_history(WALLET)
    assert history["calls"] == 2
    assert history["total_units"] == 2 * SCORE_BOOK_UNITS
    assert history["routes"]["/v1/score-book"]["calls"] == 2


def test_other_wallet_still_gets_the_static_block(monkeypatch, tmp_path):
    client, _store = _settled_client(monkeypatch, tmp_path)
    _score_book(client)
    _score_book(client)

    async def passthrough2(request, call_next):
        request.state.payment_payload = _payload(WALLET2)
        return await call_next(request)

    monkeypatch.setattr(main, "_payment_middleware", passthrough2)
    r = _score_book(client)
    assert r.status_code == 200, r.text
    assert "returning_payer" not in r.json()["verdict_row_loop"]


def test_pack_funded_repeat_delivery_is_personalised(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    pack, _ = fresh.mint_toolkit_pack("pay-toolkit-loop-2")
    client = TestClient(main.app, raise_server_exceptions=False)

    body = {"pay_to": WALLET, "pack_id": pack["pack_id"]}
    first = client.post("/v1/score-book", json=body)
    assert "returning_payer" not in first.json()["verdict_row_loop"]

    second = client.post("/v1/score-book", json=body)
    assert second.status_code == 200, second.text
    rp = second.json()["verdict_row_loop"]["returning_payer"]
    # Pack stores record no buyer wallet: the credential id is the
    # payer identity available, and the deep link pre-binds to it.
    assert rp["identity"] == f"toolkit-pack:{pack['pack_id']}"
    assert rp["identity_kind"] == "funding_credential"
    assert rp["calls"] == 2 and rp["calls_this_route"] == 2


def test_fleet_pass_repeat_delivery_is_personalised(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    fpass, _ = fresh.mint_pass(
        "pay-pass-loop-2", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    client = TestClient(main.app, raise_server_exceptions=False)
    headers = {"X-Fleet-Pass": fpass["pass_id"]}

    client.post("/v1/score-book", json={"pay_to": WALLET}, headers=headers)
    second = client.post(
        "/v1/score-book", json={"pay_to": WALLET}, headers=headers
    )
    assert second.status_code == 200, second.text
    rp = second.json()["verdict_row_loop"]["returning_payer"]
    assert rp["identity"] == f"fleet-pass:{fpass['pass_id']}"
    assert rp["calls"] == 2


def test_recheck_pack_repeat_delivery_is_personalised(monkeypatch, tmp_path):
    import httpx

    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)

    def handler(request):
        return httpx.Response(
            200, text="a page", headers={"content-type": "text/plain"}
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    pack, _ = fresh.mint_pack("pay-recheck-loop", WEB_URL)
    client = TestClient(main.app, raise_server_exceptions=False)

    body = {"url": WEB_URL, "pack_id": pack["pack_id"]}
    first = client.post("/v1/recheck", json=body)
    assert first.status_code == 200, first.text
    assert "returning_payer" not in first.json()["verdict_row_loop"]

    second = client.post("/v1/recheck", json=body)
    assert second.status_code == 200, second.text
    rp = second.json()["verdict_row_loop"]["returning_payer"]
    assert rp["identity"] == f"recheck-pack:{pack['pack_id']}"
    assert rp["calls"] == 2 and rp["calls_this_route"] == 2
    assert rp["total_spent_usd"] == 2 * PRICE_UNITS["/v1/recheck"] / 1_000_000


# --- (c) 402 challenges and error responses carry no block ---------------

def test_unpaid_challenges_carry_no_block_and_record_nothing(
    monkeypatch, tmp_path
):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    client = TestClient(main.app, raise_server_exceptions=False)

    for path, body in (
        ("/v1/score-book", {"pay_to": WALLET}),
        ("/v1/gas-oracle", {}),
        ("/v1/recheck", {"url": WEB_URL}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 402, (path, r.text)
        assert "verdict_row_loop" not in r.text
    assert fresh.payer_history(WALLET) is None


def test_error_responses_carry_no_block(monkeypatch, tmp_path):
    client, store = _settled_client(monkeypatch, tmp_path)

    # Validation error on a priced route (settled passthrough).
    r = client.post("/v1/score-book", json={})
    assert r.status_code == 422, r.text
    assert "verdict_row_loop" not in r.text

    # Unknown pack: refused by the handler, nothing delivered.
    r = client.post(
        "/v1/score-book", json={"pay_to": WALLET, "pack_id": "tpack_nope"}
    )
    assert r.status_code >= 400, r.text
    assert "verdict_row_loop" not in r.text

    # Unknown Fleet Pass: refused by the middleware debit.
    r = client.post(
        "/v1/score-book",
        json={"pay_to": WALLET},
        headers={"X-Fleet-Pass": "fpass_nope"},
    )
    assert r.status_code >= 400, r.text
    assert "verdict_row_loop" not in r.text

    assert store.payer_history(WALLET) is None


def test_oversized_delivery_is_not_rewritten_but_is_recorded(
    monkeypatch, tmp_path
):
    """The streaming guard: a JSON response advertising a body over
    the injection cap (ledger-dataset/full's shape) passes through
    untouched — no buffering, no block — while the settled delivery
    still lands in payer memory."""
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    big = JSONResponse(
        {"dataset": "x"},
        headers={"content-length": str(main._LOOP_MAX_JSON_BYTES + 1)},
    )
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/v1/ledger-dataset/full"),
        state=SimpleNamespace(payment_payload=_payload(WALLET)),
    )
    out = asyncio.run(main._attach_loop_offer(request, big))
    assert out is big
    assert fresh.payer_history(WALLET)["calls"] == 1


# --- payer memory store semantics -----------------------------------------

def test_store_counts_calls_days_and_spend_across_routes(tmp_path):
    store = recheck.WatchStore(str(tmp_path / "w.db"))
    day0 = 1_700_000_000.0

    snap = store.record_payer_delivery(WALLET, "/v1/recheck", 5_000, day0)
    assert snap["prior_calls"] == 0
    assert snap["calls"] == 1 and snap["distinct_days"] == 1

    # Same day, another route: stall-wide days stay 1, spend sums.
    snap = store.record_payer_delivery(WALLET, "/v1/guard", 10_000, day0 + 60)
    assert snap["prior_calls"] == 1
    assert snap["calls"] == 2 and snap["distinct_days"] == 1
    assert snap["total_units"] == 15_000

    # Next UTC day: a new distinct day, per-route and stall-wide.
    snap = store.record_payer_delivery(
        WALLET, "/v1/recheck", 5_000, day0 + 86_400
    )
    assert snap["calls"] == 3 and snap["distinct_days"] == 2
    assert snap["routes"]["/v1/recheck"]["calls"] == 2
    assert snap["routes"]["/v1/recheck"]["total_units"] == 10_000
    assert store.payer_history(WALLET2) is None


# --- (d) prices / 402 amounts unchanged ------------------------------------

def test_unpaid_402_amounts_unchanged():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, units in (
        ("/v1/score-book", SCORE_BOOK_UNITS),
        ("/v1/gas-oracle", PRICE_UNITS["/v1/gas-oracle"]),
        ("/v1/ledger-dataset/full", PRICE_UNITS["/v1/ledger-dataset/full"]),
    ):
        r = client.post(path, json={})
        assert r.status_code == 402, (path, r.text)
        challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
        accept = challenge["accepts"][0]
        assert accept["amount"] == str(units), path
        assert accept["payTo"] == OWNER, path


def test_loop_offer_personalisation_matches_constants():
    snapshot = {
        "payer_key": WALLET,
        "calls": 4,
        "distinct_days": 3,
        "total_units": 20_000,
        "first_seen_epoch": 1_700_000_000.0,
        "last_seen_epoch": 1_700_100_000.0,
        "routes": {"/v1/recheck": {"calls": 4, "total_units": 20_000}},
    }
    block = loop_offer.loop_offer(payer=snapshot, route="/v1/recheck")
    rp = block["returning_payer"]
    assert rp["total_spent_usd"] == 0.02
    offer = rp["adaptive_offer"]
    assert offer["price_usd"] == recheck.PACK_PRICE_UNITS / 1_000_000
    assert offer["per_check_usd"] == 1.0 / recheck.PACK_CREDITS
    assert offer["per_call_price_usd"] == PRICE_UNITS["/v1/recheck"] / 1_000_000
