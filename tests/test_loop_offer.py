"""Loop offer block (Play 1, organic-buyer plan 2026-11-01): every
paid Recheck delivery carries one additive top-level
``verdict_row_loop`` field whose terms are derived from the
constants that enforce them — and nothing else changes: error
responses and the unpaid 402 challenge never carry it."""

import asyncio
import json
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, loop_offer, recheck

WEB_URL = "https://93.184.216.34/story"
BASE = "https://stall-row-production.up.railway.app"


def _ok_handler(request):
    return httpx.Response(
        200, text="a page", headers={"content-type": "text/plain"}
    )


def _offers_by_id(block):
    return {o["id"]: o for o in block["offers"]}


def test_block_terms_match_the_enforcing_constants():
    block = loop_offer.loop_offer(BASE)
    offers = _offers_by_id(block)

    pack = offers["recheck_pack"]
    assert pack["route"] == "/v1/recheck/pack"
    assert pack["method"] == "POST"
    assert pack["price_usd"] == recheck.PACK_PRICE_UNITS / 1_000_000 == 1.0
    assert pack["terms"]["credits"] == recheck.PACK_CREDITS == 250
    assert pack["terms"]["ttl_days"] == recheck.PACK_TTL_DAYS == 90
    assert pack["terms"]["per_check_usd"] == 1.0 / 250
    assert pack["terms"]["per_call_price_usd"] == 0.005

    hook = offers["recheck_change_webhook"]
    assert hook["route"] == "/v1/recheck/webhooks"
    assert hook["price_usd"] == 0
    assert "pack_id" in hook["terms"]["gate"]
    assert "fleet_pass_id" in hook["terms"]["gate"]
    assert hook["terms"]["registration_debited"] is False
    assert hook["terms"]["deliveries_debited"] is False

    fpass = offers["fleet_pass"]
    assert fpass["route"] == "/v1/fleet-pass"
    assert fpass["price_usd"] == fleetpass.PASS_PRICE_UNITS / 1_000_000 == 5.0
    assert fpass["terms"]["ttl_days"] == fleetpass.PASS_TTL_DAYS == 90

    fpass20 = offers["fleet_pass_20"]
    assert fpass20["route"] == "/v1/fleet-pass-20"
    assert fpass20["price_usd"] == fleetpass.PASS_V2_PRICE_UNITS / 1_000_000 == 20.0

    feed = offers["recheck_feed"]
    assert feed["route"] == "/v1/recheck/feed"
    assert feed["method"] == "GET"
    assert feed["price_usd"] == 0

    assert block["discovery"] == f"{BASE}/.well-known/verdict-row.json"


def test_attach_passes_non_dicts_through():
    sentinel = object()
    assert loop_offer.attach_loop_offer(sentinel, BASE) is sentinel


def _http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def test_pack_funded_delivery_carries_the_block(monkeypatch, tmp_path):
    store = _http_env(monkeypatch, tmp_path)
    pack, _ = store.mint_pack("pay-loop", WEB_URL)
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL, "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The delivery itself is untouched apart from the additive field.
    assert body["status"] == "BASELINE"
    assert body["pack_credits_remaining"] == 249
    block = body["verdict_row_loop"]
    assert _offers_by_id(block)["recheck_pack"]["price_usd"] == 1.0
    assert block["discovery"].endswith("/.well-known/verdict-row.json")


def test_per_call_delivery_carries_the_block(monkeypatch, tmp_path):
    _http_env(monkeypatch, tmp_path)
    req = main.RecheckRequest(url=WEB_URL)
    request = SimpleNamespace(headers={}, base_url="http://testserver/")
    out = asyncio.run(main.recheck(req, request))
    assert isinstance(out, dict)
    assert out["status"] == "BASELINE"
    assert out["verdict_row_loop"]["offers"][0]["id"] == "recheck_pack"


def test_unpaid_challenge_has_no_block():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/recheck", json={"url": WEB_URL})
    assert r.status_code == 402
    assert "verdict_row_loop" not in r.text
    json.loads(r.text)  # the challenge body stays plain JSON as before
