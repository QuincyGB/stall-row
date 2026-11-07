"""Fleet delivery ledger (2026-11-07): the hub-side store,
verification, and endpoints behind the fleet's exact
payment->route attribution.

Owner's rules under test:
  * a per-call record is accepted only when its settlement tx
    provably carries the claimed USDC transfer (payer -> payTo,
    amount) — a wrong or unknown tx is REJECTED, so the delivery
    that claimed it fails loudly instead of being mis-recorded;
  * an RPC outage accepts the record as pending (verified=0) —
    the fleet does not halt on a public RPC, and reconciliation
    settles the record later;
  * pass/pack records must name a funding row this hub's own
    ledger holds;
  * one settlement tx records once — duplicates return the
    existing row, never a twin;
  * the hub's own delivery glue (_record_delivery_or_raise)
    propagates record failures — fail-closed, never silent.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.discovery import USDC_ASSET
from app.rpc import UpstreamError
from app.services import deliveryledger as dl
from app.services.receipts import _TRANSFER_TOPIC

PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
PAYER = "0x1111111111111111111111111111111111111111"
TX = "0x" + "cd" * 32
AMOUNT = 10000


def _receipt(payer=PAYER, to=PAY_TO, amount=AMOUNT):
    def topic_addr(a):
        return "0x" + "0" * 24 + a[2:].lower()

    return {
        "status": "0x1",
        "logs": [
            {
                "address": USDC_ASSET,
                "topics": [_TRANSFER_TOPIC, topic_addr(payer), topic_addr(to)],
                "data": hex(amount),
            }
        ],
    }


class _FakeRpc:
    receipt = None
    raises = False

    def __init__(self, client, url):
        pass

    async def eth_receipt(self, tx_hash):
        if _FakeRpc.raises:
            raise UpstreamError("rpc down")
        return _FakeRpc.receipt


def _payload(**over):
    base = {
        "service": "stall-row",
        "route": "/v1/tube-search",
        "payer": PAYER,
        "amount_units": AMOUNT,
        "tx": TX,
        "network": "eip155:8453",
        "funding": "per_call",
        "ref": None,
    }
    base.update(over)
    return base


def _store(tmp_path):
    return dl.DeliveryLedgerStore(str(tmp_path / "ledger.db"))


def _write(store, payload):
    return asyncio.run(
        dl.write_record(store, payload, rpc_url="http://rpc.invalid", pay_to=PAY_TO)
    )


def test_verified_per_call_record(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = _receipt(), False
    rec = _write(_store(tmp_path), _payload())
    assert rec["verified"] == 1
    assert rec["route"] == "/v1/tube-search"
    rows = _store(tmp_path).recent()
    assert [r["tx"] for r in rows] == [TX]


def test_wrong_amount_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = _receipt(amount=9999), False
    with pytest.raises(dl.RecordRejected):
        _write(_store(tmp_path), _payload())
    assert _store(tmp_path).recent() == []


def test_unknown_tx_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = None, False
    with pytest.raises(dl.RecordRejected):
        _write(_store(tmp_path), _payload())


def test_rpc_outage_records_pending_not_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = None, True
    rec = _write(_store(tmp_path), _payload())
    assert rec["verified"] == 0


def test_duplicate_tx_records_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = _receipt(), False
    store = _store(tmp_path)
    first = _write(store, _payload())
    second = _write(store, _payload())
    assert second["id"] == first["id"]
    assert second["duplicate"] is True
    assert len(store.recent()) == 1


def test_fleet_pass_ref_must_exist(tmp_path):
    store = _store(tmp_path)
    payload = _payload(funding="fleet_pass", tx=None, network=None,
                       payer="fleet-pass:fpass_x", ref="fpd_nope")
    with pytest.raises(dl.RecordRejected):
        _write(store, payload)
    # A debit row in the hub's own ledger makes the same record
    # verifiable. (Same DB file: the debits table is the fleet-pass
    # store's; create its shape and the row directly.)
    store._conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_pass_debits "
        "(debit_id TEXT PRIMARY KEY, pass_id TEXT, service TEXT, "
        "route TEXT, price_units INTEGER, created_epoch REAL, "
        "refunded INTEGER DEFAULT 0)"
    )
    store._conn.execute(
        "INSERT INTO fleet_pass_debits (debit_id, pass_id, service, "
        "route, price_units, created_epoch) VALUES "
        "('fpd_ok', 'fpass_x', 'stall-row', '/v1/tube-search', 10000, 0)"
    )
    store._conn.commit()
    rec = _write(store, _payload(funding="fleet_pass", tx=None, network=None,
                                 payer="fleet-pass:fpass_x", ref="fpd_ok"))
    assert rec["verified"] == 1


def test_endpoints_record_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = _receipt(), False
    monkeypatch.setattr(main, "_delivery_store", lambda: _store(tmp_path))
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/delivery-ledger/record", json=_payload())
    assert r.status_code == 200
    assert r.json()["recorded"] is True
    got = client.get("/v1/delivery-ledger/recent").json()["records"]
    assert len(got) == 1
    assert got[0]["tx"] == TX and got[0]["route"] == "/v1/tube-search"
    # An unverifiable record is refused at the endpoint too.
    bad = client.post(
        "/v1/delivery-ledger/record", json=_payload(amount_units=1)
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "delivery_record_rejected"


def test_hub_delivery_glue_raises_on_record_failure(monkeypatch):
    """The negative test for StallRow's own fail-closed hook: when
    the record cannot be verified/written, _record_delivery_or_raise
    propagates — the delivery fails loudly, never silently."""
    monkeypatch.setattr(dl, "Rpc", _FakeRpc)
    _FakeRpc.receipt, _FakeRpc.raises = None, False  # tx unknown -> reject
    request = SimpleNamespace(
        method="POST", url=SimpleNamespace(path="/v1/tube-search"), headers={}
    )
    receipt_b64 = __import__("base64").b64encode(
        __import__("json").dumps(
            {"payer": PAYER, "transaction": TX, "network": "eip155:8453"}
        ).encode()
    ).decode()
    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": "application/json",
                 "payment-response": receipt_b64},
    )
    with pytest.raises(dl.RecordRejected):
        asyncio.run(main._record_delivery_or_raise(request, response))
