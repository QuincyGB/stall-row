"""Pre-Trade Guard: the payTo screen — Verdict Index catalog history,
the sanctions screen, wallet age/activity, and the PROCEED / CAUTION /
ABORT combination rules (risk bands mirror Token Verdict's
/v1/pre-trade; an unevaluated leg caps the verdict at CAUTION; hard
aborts override the arithmetic)."""

from datetime import datetime, timezone

import pytest

from app.blockscout import Blockscout
from app.config import load_settings
from app.rpc import Rpc, UpstreamError
from app.services import indexfeed, pretrade, verdict
from tests.fixtures import mock_client

S = load_settings()

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
NEW = "0x2222222222222222222222222222222222222222"
RES = "https://a.example.com/v1/verdict"
RES2 = "https://b.example.com/v1/check"
BASE_EPOCH = 1_762_000_000.0


def _item(resource, pay_to, amount="20000"):
    return {
        "resource": resource,
        "serviceName": "Stall",
        "description": "a stall",
        "accepts": [
            {"scheme": "exact", "network": "eip155:8453", "amount": amount,
             "asset": "0xUSDC", "payTo": pay_to}
        ],
        "lastUpdated": "2026-10-26T00:00:00Z",
        "quality": {"l30DaysTotalCalls": 12, "l30DaysUniquePayers": 3},
    }


def _store(tmp_path, snapshots):
    """snapshots: raw-item lists, oldest first, one refresh window apart."""
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    for i, items in enumerate(snapshots):
        rows = [indexfeed.normalize_item(it) for it in items]
        store.save_snapshot(rows, epoch=BASE_EPOCH + i * 21600)
    return store


def _established(tmp_path):
    snap = [_item(RES, OWNER), _item(RES2, OWNER)]
    return _store(tmp_path, [snap, snap, snap])


class FakeScout:
    def __init__(self, txs):
        self._txs = txs

    async def transactions(self, address, sort="desc", limit=10):
        return self._txs


class FakeRpc:
    def __init__(self, nonce):
        self._nonce = nonce

    async def eth_tx_count(self, address):
        if self._nonce is None:
            raise UpstreamError("rpc down")
        return self._nonce


OLD_TX = [{"hash": "0x1", "timestamp": "2024-06-01T00:00:00.000000Z"}]


def _old_wallet():
    return FakeScout(list(OLD_TX)), FakeRpc(137)


async def _guard(store, scout, rpc, **kw):
    return await pretrade.pre_trade_guard(None, rpc, scout, store, **kw)


def _names(out):
    return [r["name"] for r in out["reasons"]]


@pytest.mark.asyncio
async def test_established_payto_proceeds(tmp_path):
    scout, rpc = _old_wallet()
    out = await _guard(
        _established(tmp_path), scout, rpc,
        pay_to=OWNER, resource=RES, amount=20000,
    )
    assert out["verdict"] == "PROCEED"
    assert out["risk_score"] == 0
    assert out["unknown_legs"] == []
    assert out["index"]["listed_now"] == 2
    assert out["index"]["first_seen_is_newest"] is False
    assert out["index"]["history_observed_seconds"] == 43200
    assert "catalog track record" in _names(out)
    assert "resource payTo confirmed" in _names(out)


@pytest.mark.asyncio
async def test_sanctions_match_aborts_regardless(tmp_path):
    listed = sorted(verdict.SNAPSHOT["addresses"])[0]
    scout, rpc = _old_wallet()
    out = await _guard(_established(tmp_path), scout, rpc, pay_to=listed)
    assert out["verdict"] == "ABORT"
    assert out["risk_score"] == 100
    assert out["sanctions"]["status"] == "matched"


@pytest.mark.asyncio
async def test_resource_payto_mismatch_is_a_hard_abort(tmp_path):
    store = _store(tmp_path, [[_item(RES, OTHER)], [_item(RES, OTHER)]])
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=OWNER, resource=RES)
    assert out["verdict"] == "ABORT"
    assert "resource payTo mismatch" in _names(out)
    assert out["index"]["resource"]["current_pay_to"] == OTHER


@pytest.mark.asyncio
async def test_named_resource_rotated_to_this_payto_cautions(tmp_path):
    store = _store(
        tmp_path,
        [[_item(RES, OTHER)], [_item(RES, OTHER)], [_item(RES, NEW)]],
    )
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=NEW, resource=RES)
    # first-seen in newest snapshot (20) + this listing rotated here (30)
    assert out["verdict"] == "CAUTION"
    assert out["risk_score"] == 50
    assert "this listing's payTo rotated here" in _names(out)
    assert out["index"]["rotations_in"][0]["from_pay_to"] == OTHER


@pytest.mark.asyncio
async def test_generic_rotation_in_cautions(tmp_path):
    store = _store(
        tmp_path,
        [[_item(RES, OTHER)], [_item(RES, OTHER)], [_item(RES, NEW)]],
    )
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=NEW)
    assert out["verdict"] == "CAUTION"
    assert out["risk_score"] == 40  # first-seen 20 + rotation-in 20
    assert "payTo rotation into this address" in _names(out)


@pytest.mark.asyncio
async def test_first_seen_payto_old_wallet_still_proceeds(tmp_path):
    store = _store(tmp_path, [[_item(RES, OTHER)], [_item(RES, OTHER)]])
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=NEW)
    assert out["verdict"] == "PROCEED"  # first-seen alone is a signal, not a verdict
    assert out["risk_score"] == 30
    assert out["index"]["ever_listed"] is False
    assert out["index"]["first_seen_at"] is None
    assert "no catalog track record" in _names(out)


@pytest.mark.asyncio
async def test_first_seen_payto_brand_new_wallet_aborts(tmp_path):
    store = _store(tmp_path, [[_item(RES, OTHER)], [_item(RES, OTHER)]])
    now = datetime.now(timezone.utc).isoformat()
    scout = FakeScout([{"hash": "0x9", "timestamp": now}])
    out = await _guard(store, scout, FakeRpc(1), pay_to=NEW)
    # no track record 30 + brand-new wallet 35 + minimal activity 10
    assert out["verdict"] == "ABORT"
    assert out["risk_score"] == 75


@pytest.mark.asyncio
async def test_listings_rotated_away_scores(tmp_path):
    store = _store(
        tmp_path,
        [[_item(RES, OWNER)], [_item(RES, OTHER)], [_item(RES, OTHER)]],
    )
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=OWNER)
    assert out["index"]["listed_now"] == 0
    assert len(out["index"]["rotations_out"]) == 1
    assert out["risk_score"] == 25  # left-the-catalog 15 + rotated-away 10
    assert out["verdict"] == "PROCEED"


@pytest.mark.asyncio
async def test_amount_mismatch_against_catalogued_price(tmp_path):
    scout, rpc = _old_wallet()
    out = await _guard(
        _established(tmp_path), scout, rpc,
        pay_to=OWNER, resource=RES, amount=99999,
    )
    assert out["risk_score"] == 10
    assert "amount differs from catalogued price" in _names(out)


@pytest.mark.asyncio
async def test_named_resource_never_catalogued(tmp_path):
    scout, rpc = _old_wallet()
    out = await _guard(
        _established(tmp_path), scout, rpc,
        pay_to=OWNER, resource="https://nope.example.com/v1/x",
    )
    assert out["risk_score"] == 10
    assert "named resource not in catalog" in _names(out)


@pytest.mark.asyncio
async def test_empty_store_is_unknown_and_caps_at_caution(tmp_path):
    store = indexfeed.IndexStore(str(tmp_path / "empty.db"))
    scout, rpc = _old_wallet()
    out = await _guard(store, scout, rpc, pay_to=OWNER)
    assert out["index"]["status"] == "unknown"
    assert out["unknown_legs"] == ["index"]
    assert out["risk_score"] == 0
    assert out["verdict"] == "CAUTION"  # unexamined is never called safe


@pytest.mark.asyncio
async def test_missing_sanctions_snapshot_caps_at_caution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verdict, "SNAPSHOT",
        {"addresses": set(), "source_url": None, "snapshot_date": None, "count": 0},
    )
    scout, rpc = _old_wallet()
    out = await _guard(_established(tmp_path), scout, rpc, pay_to=OWNER)
    assert out["sanctions"]["status"] == "unknown"
    assert "sanctions" in out["unknown_legs"]
    assert out["verdict"] == "CAUTION"


@pytest.mark.asyncio
async def test_wallet_sources_down_caps_at_caution(tmp_path):
    scout, rpc = FakeScout(None), FakeRpc(None)
    out = await _guard(_established(tmp_path), scout, rpc, pay_to=OWNER)
    assert out["wallet"]["status"] == "unknown"
    assert "wallet" in out["unknown_legs"]
    assert out["verdict"] == "CAUTION"


@pytest.mark.asyncio
async def test_non_base_network_skips_wallet_leg(tmp_path):
    scout, rpc = _old_wallet()
    out = await _guard(
        _established(tmp_path), scout, rpc, pay_to=OWNER, network="eip155:1"
    )
    assert out["wallet"]["status"] == "unknown"
    assert "Base" in out["wallet"]["detail"]
    assert out["verdict"] == "CAUTION"


@pytest.mark.asyncio
async def test_no_onchain_activity_scores(tmp_path):
    scout, rpc = FakeScout([]), FakeRpc(0)
    out = await _guard(_established(tmp_path), scout, rpc, pay_to=OWNER)
    assert out["wallet"]["has_activity"] is False
    assert out["risk_score"] == 20
    assert "no on-chain activity at all" in _names(out)


def test_index_history_unknown_when_store_empty(tmp_path):
    store = indexfeed.IndexStore(str(tmp_path / "empty.db"))
    out = pretrade.index_history(store, OWNER)
    assert out["status"] == "unknown"
    assert out["listed_now"] == 0 and out["ever_listed"] is False


@pytest.mark.asyncio
async def test_guard_with_real_clients_on_mock_transport(tmp_path):
    store = _established(tmp_path)
    client = mock_client()
    async with client:
        out = await pretrade.pre_trade_guard(
            client,
            Rpc(client, S.rpc_url),
            Blockscout(client, S.blockscout_base_url),
            store,
            pay_to=OWNER,
            resource=RES,
            amount=20000,
        )
    assert out["verdict"] == "PROCEED"
    assert out["wallet"]["outgoing_tx_count_nonce"] == 137
    assert out["wallet"]["age_days"] is not None and out["wallet"]["age_days"] > 30
