"""W2 D2: liquidity-check, pre-trade-pack, due-diligence-pack.

Offline: shared fixtures MockTransport plays RPC/Blockscout; a
DexScreener handler is layered in for the liquidity source, and
eth_getCode is answered like in test_counterparty_plus.
"""

import json

import httpx
import pytest

from app.blockscout import Blockscout
from app.config import PRICE_UNITS, load_settings
from app.rpc import Rpc, UpstreamError
from app.services import counterparty_watch, indexfeed, liquidity, packs, verdict
from tests.fixtures import ADDR, USDC, make_handler

S = load_settings()
OFAC_ADDR = sorted(verdict.SNAPSHOT["addresses"])[0]

DEX_PAIR = {
    "chainId": "base",
    "dexId": "uniswap",
    "pairAddress": "0x1111111111111111111111111111111111111111",
    "baseToken": {"address": USDC, "name": "USD Coin", "symbol": "USDC"},
    "quoteToken": {"address": "0x4200000000000000000000000000000000000006", "name": "Wrapped Ether", "symbol": "WETH"},
    "priceUsd": "1.0001",
    "priceNative": "0.0003",
    "liquidity": {"usd": 500000.0, "base": 250000.0, "quote": 75.0},
    "volume": {"h24": 123456.0},
    "marketCap": 1000000.0,
    "fdv": 1000000.0,
    "url": "https://dexscreener.com/base/0x1111",
}


def _client(*, dex_pairs=None, dex_status=200):
    fixtures_handler = make_handler()

    def handler(request):
        if request.url.host == "api.dexscreener.com":
            if dex_status != 200:
                return httpx.Response(dex_status, text="down")
            return httpx.Response(200, json={"pairs": dex_pairs})
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        if body.get("method") == "eth_getCode":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id", 1), "result": "0x"})
        return fixtures_handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _parts(tmp_path, **kw):
    client = _client(**kw)
    rpc = Rpc(client, S.rpc_url)
    scout = Blockscout(client, S.blockscout_base_url)
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    return client, rpc, scout, store


def test_prices():
    assert PRICE_UNITS["/v1/liquidity-check"] == 30_000
    assert PRICE_UNITS["/v1/pre-trade-pack"] == 100_000
    assert PRICE_UNITS["/v1/due-diligence-pack"] == 100_000


@pytest.mark.asyncio
async def test_liquidity_best_pair_and_slippage(tmp_path):
    client = _client(dex_pairs=[DEX_PAIR])
    async with client:
        out = await liquidity.liquidity_check(client, USDC, 1000.0)
    assert out["status"] == "ok"
    assert out["best_pair"]["dex_id"] == "uniswap"
    assert out["liquidity_usd"] == 500000.0
    assert out["price_usd"] == 1.0001
    # model: 100 * 1000 / (250000 + 1000)
    assert out["estimated_slippage_pct"] == pytest.approx(0.3984, abs=1e-4)
    assert "DexScreener" in out["source"]


@pytest.mark.asyncio
async def test_liquidity_no_pair_is_delivered_answer():
    client = _client(dex_pairs=[])
    async with client:
        out = await liquidity.liquidity_check(client, USDC)
    assert out["status"] == "no_live_market_found"
    assert out["best_pair"] is None
    assert out["estimated_slippage_pct"] is None
    assert out["trade_size_assumed"] is True


@pytest.mark.asyncio
async def test_liquidity_non_base_pairs_ignored():
    pair = dict(DEX_PAIR, chainId="ethereum")
    client = _client(dex_pairs=[pair])
    async with client:
        out = await liquidity.liquidity_check(client, USDC)
    assert out["status"] == "no_live_market_found"


@pytest.mark.asyncio
async def test_liquidity_upstream_failure_raises():
    client = _client(dex_status=503)
    async with client:
        with pytest.raises(UpstreamError):
            await liquidity.liquidity_check(client, USDC)


def test_slippage_formula():
    assert liquidity.estimate_slippage_pct(1000.0, 500000.0) == pytest.approx(0.3984, abs=1e-4)
    assert liquidity.estimate_slippage_pct(1000.0, 0.0) is None


@pytest.mark.asyncio
async def test_pre_trade_pack_composes_three_sections(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    async with client:
        out = await packs.run_pre_trade_pack(
            client, rpc, scout, store, token=USDC, counterparty=ADDR
        )
    assert set(out["sections"]) == {"token_verdict", "counterparty_report", "approval_check"}
    assert out["sections"]["token_verdict"]["status"] == "ok"
    assert out["sections"]["approval_check"]["result"]["relevant_token"] == USDC
    # Fixture: counterparty holds USDC and has an unlimited Permit2 approval for it.
    assert out["sections"]["approval_check"]["result"]["relevant_unlimited_approvals"] == 1
    assert out["combined_verdict"] == "caution"
    assert out["combined_rule"] == packs.PRE_TRADE_RULE


@pytest.mark.asyncio
async def test_pre_trade_pack_sanctions_is_no_go(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    async with client:
        out = await packs.run_pre_trade_pack(
            client, rpc, scout, store, token=OFAC_ADDR, counterparty=ADDR
        )
    assert out["combined_verdict"] == "no-go"


@pytest.mark.asyncio
async def test_due_diligence_pack_five_sections_and_read_only_profile(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    watch = counterparty_watch.CounterpartyWatchStore(str(tmp_path / "watch.db"))
    async with client:
        out = await packs.run_due_diligence_pack(
            client, rpc, scout, store, wallet=ADDR, token=USDC, watch_store=watch
        )
    assert set(out["sections"]) == {
        "counterparty_report", "sanctions_check", "allowance_audit",
        "flagged_paytos", "profile_summary",
    }
    prof = out["sections"]["profile_summary"]["result"]
    assert prof["source"] == "computed_read_only_not_stored"
    assert prof["stored_snapshot_used"] is False
    # Read-only proof: the pack must NOT have written a watch baseline.
    assert watch.get(ADDR) is None
    # OFAC-flagged in the flagged-paytos feed (every OFAC address is) is
    # not true for ADDR; ADDR is the fleet wallet, not flagged.
    assert out["sections"]["flagged_paytos"]["result"]["flagged"] is False
    assert out["combined_verdict"] in ("go", "caution")
    assert out["combined_rule"] == packs.DUE_DILIGENCE_RULE


@pytest.mark.asyncio
async def test_due_diligence_uses_stored_snapshot_when_present(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    watch = counterparty_watch.CounterpartyWatchStore(str(tmp_path / "watch.db"))
    stored = {"address": ADDR, "flags": [], "counterparties": {}, "generated_at": "t0"}
    watch.put(ADDR, stored)
    async with client:
        out = await packs.run_due_diligence_pack(
            client, rpc, scout, store, wallet=ADDR, watch_store=watch
        )
    prof = out["sections"]["profile_summary"]["result"]
    assert prof["source"] == "stored_watch_snapshot"
    assert prof["profile"]["generated_at"] == "t0"


@pytest.mark.asyncio
async def test_due_diligence_ofac_wallet_is_no_go(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    async with client:
        out = await packs.run_due_diligence_pack(
            client, rpc, scout, store, wallet=OFAC_ADDR
        )
    assert out["combined_verdict"] == "no-go"
    assert out["risk_level"] == "high"
    assert out["sections"]["flagged_paytos"]["result"]["flagged"] is True
