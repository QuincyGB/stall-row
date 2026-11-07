"""Toolkit packs (Next-20 #8): minting (incl. payment-replay
idempotency), family-bound spend semantics across Sign Reader /
Score Book / Guard (decrement exactly once per delivered call,
never for our failure), the refusal matrix, cross-family refusals
(a toolkit pack funds no Recheck; a Recheck pack funds no toolkit
route), and free balance reads. Fully offline — MockTransport
plays the targets, payment identity is stubbed the way the
middleware exposes it on request.state."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.services import recheck
from app.services.indexfeed import IndexStore
from app.services.recheck import PackError

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
TARGET = "https://93.184.216.34/v1/thing"
WEB_URL = "https://93.184.216.34/story"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _ok_handler(request):
    return httpx.Response(200, text="a page", headers={"content-type": "text/plain"})


def _buy(store, key="pay-1", **kw):
    pack, created = store.mint_toolkit_pack(key, **kw)
    assert created
    return pack


# --- minting -----------------------------------------------------------------

def test_mint_toolkit_pack_shape_and_expiry(store):
    pack = _buy(store)
    assert pack["pack_id"].startswith("tkpack_")
    # 128 bits of entropy behind the prefix: 32 hex chars.
    assert len(pack["pack_id"]) == len("tkpack_") + 32
    # Family binding, not a URL binding.
    assert pack["family"] == "toolkit"
    assert pack["routes"] == [
        "/v1/sign-read",
        "/v1/score-book",
        "/v1/guard",
        "/v1/tube-search",
        "/v1/page-to-markdown",
        "/v1/video-details",
        "/v1/video-transcript",
        "/v1/feed-digest",
    ]
    assert "url" not in pack
    assert pack["credits_total"] == 125
    assert pack["credits_remaining"] == 125
    assert pack["expired"] is False
    assert pack["expires_at"] > pack["created_at"]
    assert store.toolkit_pack_count() == 1


def test_mint_is_idempotent_on_payment_replay(store):
    first, created1 = store.mint_toolkit_pack("nonce-abc")
    second, created2 = store.mint_toolkit_pack("nonce-abc")
    assert created1 is True and created2 is False
    assert second["pack_id"] == first["pack_id"]
    assert store.toolkit_pack_count() == 1
    third, created3 = store.mint_toolkit_pack("nonce-def")
    assert created3 is True and third["pack_id"] != first["pack_id"]
    assert store.toolkit_pack_count() == 2


# --- store spend path ----------------------------------------------------------

def test_reserve_decrements_and_refund_is_capped(store):
    pack = _buy(store)
    reserved = store.reserve_toolkit_credit(pack["pack_id"])
    assert reserved["credits_remaining"] == 124
    store.refund_toolkit_credit(pack["pack_id"])
    assert store.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125
    # A stray second refund cannot inflate the pack past its total.
    store.refund_toolkit_credit(pack["pack_id"])
    assert store.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125


def test_refusals_are_specific_and_decrement_nothing(store):
    pack = _buy(store)
    with pytest.raises(PackError) as e:
        store.reserve_toolkit_credit("tkpack_doesnotexist")
    assert e.value.code == "unknown_pack" and e.value.http_status == 404

    small = _buy(store, key="pay-small", credits_total=1)
    store.reserve_toolkit_credit(small["pack_id"])
    with pytest.raises(PackError) as e:
        store.reserve_toolkit_credit(small["pack_id"])
    assert e.value.code == "pack_exhausted" and e.value.http_status == 402

    stale = _buy(store, key="pay-stale", ttl_days=0)
    with pytest.raises(PackError) as e:
        store.reserve_toolkit_credit(stale["pack_id"])
    assert e.value.code == "pack_expired" and e.value.http_status == 410
    assert store.get_toolkit_pack(stale["pack_id"])["expired"] is True
    # None of the refusals touched the healthy pack.
    assert store.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125


def test_balance_reads_without_spending(store):
    pack = _buy(store)
    view = store.get_toolkit_pack(pack["pack_id"])
    assert view["credits_remaining"] == 125
    assert view["expires_at"] == pack["expires_at"]
    assert store.get_toolkit_pack("tkpack_nope") is None
    assert store.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125


# --- HTTP surface ---------------------------------------------------------------

def _fake_buy_request(nonce="0x" + "43" * 32):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/toolkit/pack",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "https",
        "client": ("testclient", 50000),
    }
    request = Request(scope)
    request.state.payment_payload = SimpleNamespace(
        payload=SimpleNamespace(
            authorization=SimpleNamespace(
                nonce=nonce, **{"from": "0x1111111111111111111111111111111111111111"}
            ),
            signature="0xdeadbeef",
        )
    )
    return request


def test_buy_handler_mints_and_replay_returns_same_pack(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)

    out = asyncio.run(main.toolkit_pack_buy(_fake_buy_request()))
    assert out["credits_total"] == 125 and out["credits_remaining"] == 125
    assert out["family"] == "toolkit"
    assert out["expiry_days"] == 90 and out["price_units"] == 1_000_000

    replay = asyncio.run(main.toolkit_pack_buy(_fake_buy_request()))
    assert replay["pack_id"] == out["pack_id"]
    assert "already minted" in replay["note"]
    assert fresh.toolkit_pack_count() == 1


def test_unpaid_pack_buy_challenge_is_exactly_one_dollar():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/toolkit/pack", json={})
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    accept = challenge["accepts"][0]
    assert accept["amount"] == "1000000"
    assert accept["payTo"] == OWNER


def test_unpaid_toolkit_tools_still_challenge_at_one_cent():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, body in (
        ("/v1/sign-read", {"url": TARGET}),
        ("/v1/score-book", {"url": TARGET}),
        ("/v1/guard", {"url": TARGET}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 402, path
        challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
        assert challenge["accepts"][0]["amount"] == "10000", path


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    monkeypatch.setattr(
        main, "_index_store", IndexStore(str(tmp_path / "index.db"))
    )
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def test_http_funded_calls_debit_one_each_and_drain_in_mix(http_env):
    pack, _ = http_env.mint_toolkit_pack("pay-http")
    client = TestClient(main.app, raise_server_exceptions=False)

    # Sign Reader via the header presentation.
    r = client.post(
        "/v1/sign-read",
        json={"url": TARGET},
        headers={"X-Toolkit-Pack": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["challenge_found"] is False  # a delivered report
    assert body["pack_id"] == pack["pack_id"]
    assert body["pack_credits_remaining"] == 124

    # Score Book via the body presentation (no record = found=false,
    # still a delivered report, still one credit).
    r = client.post(
        "/v1/score-book",
        json={"url": TARGET, "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] is False
    assert body["pack_credits_remaining"] == 123

    # Guard via the body presentation.
    r = client.post("/v1/guard", json={"url": TARGET, "pack_id": pack["pack_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] in ("PROCEED", "CAUTION", "ABORT")
    assert body["pack_credits_remaining"] == 122

    # One more Sign Reader: the mixed sequence drains exactly.
    r = client.post(
        "/v1/sign-read",
        json={"url": TARGET, "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pack_credits_remaining"] == 121
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 121


def test_http_unknown_pack_refused_and_nothing_runs(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/score-book",
        json={"url": TARGET},
        headers={"X-Toolkit-Pack": "tkpack_nope"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_pack"


def test_http_exhausted_pack_returns_clean_402(http_env):
    pack, _ = http_env.mint_toolkit_pack("pay-tiny", credits_total=1)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/score-book", json={"url": TARGET, "pack_id": pack["pack_id"]}
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/v1/score-book", json={"url": TARGET, "pack_id": pack["pack_id"]}
    )
    assert r.status_code == 402
    assert r.json()["code"] == "pack_exhausted"
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 0


def test_http_failed_call_refunds_the_credit(http_env):
    pack, _ = http_env.mint_toolkit_pack("pay-fail")
    client = TestClient(main.app, raise_server_exceptions=False)

    # SSRF refusal: the handler 422s before any delivery.
    r = client.post(
        "/v1/sign-read",
        json={"url": "https://127.0.0.1/secret", "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 422, r.text
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125

    # Validation failure (score-book names no target): also refunded.
    r = client.post("/v1/score-book", json={"pack_id": pack["pack_id"]})
    assert r.status_code == 422, r.text
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125


def test_http_balance_route(http_env):
    pack, _ = http_env.mint_toolkit_pack("pay-bal")
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/toolkit/pack-balance", json={"pack_id": pack["pack_id"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credits_remaining"] == 125
    assert body["family"] == "toolkit"

    r = client.post("/v1/toolkit/pack-balance", json={"pack_id": "tkpack_nope"})
    assert r.status_code == 404
    # Reads never spend.
    assert http_env.get_toolkit_pack(pack["pack_id"])["credits_remaining"] == 125


def test_cross_family_packs_refuse_cleanly(http_env):
    tk_pack, _ = http_env.mint_toolkit_pack("pay-tk")
    recheck_pack, _ = http_env.mint_pack("pay-rc", WEB_URL)
    client = TestClient(main.app, raise_server_exceptions=False)

    # A toolkit pack does NOT fund a Recheck.
    r = client.post(
        "/v1/recheck",
        json={"url": WEB_URL},
        headers={"X-Recheck-Pack": tk_pack["pack_id"]},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_pack"
    assert http_env.count() == 0  # no baseline was recorded

    # A Recheck pack does NOT fund a toolkit route.
    r = client.post(
        "/v1/score-book",
        json={"url": TARGET, "pack_id": recheck_pack["pack_id"]},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_pack"

    # Neither pack lost a credit to the refused cross-family calls.
    assert http_env.get_toolkit_pack(tk_pack["pack_id"])["credits_remaining"] == 125
    assert http_env.get_pack(recheck_pack["pack_id"])["credits_remaining"] == 250
