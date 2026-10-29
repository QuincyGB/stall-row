"""Human payment rail: internal Fleet Check pack mint for buyers
who pay $5.00 USDC directly on-chain instead of via x402.

Pins:
  * the fleet-internal secret gate mirrors /v1/internal/nudges
    exactly (unset secret = 404, wrong/missing secret = 403);
  * on-chain verification before minting: wrong amount, wrong
    recipient, and wrong sender are all refused and mint nothing;
  * a tx hash mints exactly once — a replay is a 409 and the pack
    count does not move;
  * the happy path mints a genuine fcpack_ pack (100 credits,
    empty default definition).

Fully offline: a fake RPC plays eth_getTransactionReceipt with
synthetic USDC Transfer logs.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import recheck

PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
BUYER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TX = "0x" + "ab" * 32
SECRET = "test-secret"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def _receipt(*, frm=BUYER, to=PAY_TO, amount=5_000_000, status="0x1"):
    return {
        "status": status,
        "blockNumber": "0x100",
        "logs": [
            {
                "address": USDC,
                "topics": [TRANSFER_TOPIC, _pad(frm), _pad(to)],
                "data": hex(amount),
            }
        ],
    }


class _FakeRpc:
    def __init__(self, receipt):
        self._receipt = receipt

    async def eth_receipt(self, tx_hash):
        return self._receipt


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    monkeypatch.setattr(main, "_recheck_store", store)
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", SECRET)
    state = {"receipt": _receipt()}

    def fake_clients():
        # Fresh client per call, like main._clients() in production
        # (the endpoint closes it with `async with`).
        fresh = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
        return fresh, _FakeRpc(state["receipt"]), None

    monkeypatch.setattr(main, "_clients", fake_clients)
    return TestClient(main.app, raise_server_exceptions=False), store, state


def _mint(client, **overrides):
    body = {"buyer_wallet": BUYER, "tx_hash": TX}
    body.update(overrides)
    return client.post(
        "/v1/internal/fleet-check/pack/mint",
        json=body,
        headers={"x-fleet-internal-secret": SECRET},
    )


def test_secret_required(env, monkeypatch):
    client, _store, _state = env
    body = {"buyer_wallet": BUYER, "tx_hash": TX}
    # No header at all.
    assert client.post("/v1/internal/fleet-check/pack/mint", json=body).status_code == 403
    # Wrong secret.
    resp = client.post(
        "/v1/internal/fleet-check/pack/mint",
        json=body,
        headers={"x-fleet-internal-secret": "nope"},
    )
    assert resp.status_code == 403
    # Secret unset on the server: the route does not exist.
    monkeypatch.delenv("FLEET_INTERNAL_SECRET")
    resp = client.post(
        "/v1/internal/fleet-check/pack/mint",
        json=body,
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert resp.status_code == 404


def test_happy_path_mints(env):
    client, store, _state = env
    resp = _mint(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pack_id"].startswith("fcpack_")
    assert body["created"] is True
    assert body["credits_total"] == 100 and body["credits_remaining"] == 100
    assert body["definition"] == {"resources": [], "origins": []}
    pack = store.get_fleet_check_pack(body["pack_id"])
    assert pack is not None and pack["credits_remaining"] == 100
    assert store.is_tx_consumed(TX) is True
    assert store.consumed_tx(TX)["buyer_wallet"] == BUYER


def test_wrong_amount_rejected(env):
    client, store, state = env
    for amount in (4_990_000, 5_010_000, 1):
        state["receipt"] = _receipt(amount=amount)
        resp = _mint(client)
        assert resp.status_code == 422, (amount, resp.text)
        assert resp.json()["code"] == "wrong_amount"
    assert store.fleet_check_pack_count() == 0
    assert store.consumed_tx_count() == 0


def test_wrong_recipient_rejected(env):
    client, store, state = env
    state["receipt"] = _receipt(to=OTHER)
    resp = _mint(client)
    assert resp.status_code == 422
    assert resp.json()["code"] == "wrong_recipient"
    assert store.fleet_check_pack_count() == 0


def test_wrong_sender_rejected(env):
    client, store, state = env
    state["receipt"] = _receipt(frm=OTHER)
    resp = _mint(client)
    assert resp.status_code == 422
    assert resp.json()["code"] == "wrong_sender"
    assert store.fleet_check_pack_count() == 0


def test_replay_of_same_tx_rejected(env):
    client, store, _state = env
    first = _mint(client)
    assert first.status_code == 200
    replay = _mint(client)
    assert replay.status_code == 409
    assert replay.json()["code"] == "tx_consumed"
    assert store.fleet_check_pack_count() == 1
    assert store.consumed_tx_count() == 1


def test_unconfirmed_tx_is_502_and_mints_nothing(env):
    client, store, state = env
    state["receipt"] = None
    resp = _mint(client)
    assert resp.status_code == 502
    assert store.fleet_check_pack_count() == 0


def test_bad_shapes_are_422(env):
    client, _store, _state = env
    assert _mint(client, buyer_wallet="not-an-address").status_code == 422
    assert _mint(client, tx_hash="0x1234").status_code == 422
