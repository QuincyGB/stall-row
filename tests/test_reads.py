"""Reads: wallet snapshot, allowance audit, tx decoder, gas oracle —
happy paths from fixtures, plus the degradation contract (explorer
down → fields 'unknown', never invented; the allowance audit degrades
the same way instead of failing)."""

import jsonschema
import pytest

from app.blockscout import Blockscout
from app.config import load_settings
from app.discovery import ROUTES
from app.rpc import Rpc, UpstreamError
from app.services import reads
from tests.fixtures import ADDR, OTHER, TX_HASH, mock_client

S = load_settings()


def _parts(**kw):
    client = mock_client(**kw)
    return client, Rpc(client, S.rpc_url), Blockscout(client, S.blockscout_base_url)


@pytest.mark.asyncio
async def test_wallet_snapshot_happy_path():
    client, rpc, scout = _parts()
    async with client:
        out = await reads.wallet_snapshot(client, rpc, scout, ADDR)
    assert out["eth_balance"] == "0.042"
    assert out["eth_balance_source"] == "blockscout"
    assert out["outgoing_tx_count_nonce"] == 137
    tokens = out["token_balances"]
    assert isinstance(tokens, list) and tokens[0]["symbol"] == "USDC"
    assert tokens[0]["balance"] == "12.5"
    assert out["first_activity"].startswith("2025-11-02")
    assert out["last_activity"].startswith("2025-11-02")


@pytest.mark.asyncio
async def test_wallet_snapshot_degrades_when_explorer_down():
    client, rpc, scout = _parts(blockscout_down=True)
    async with client:
        out = await reads.wallet_snapshot(client, rpc, scout, ADDR)
    # RPC still answers what it can; explorer-only fields are unknown.
    assert out["eth_balance"] == "0.042"
    assert out["eth_balance_source"] == "rpc"
    assert out["token_balances"] == "unknown"
    assert out["first_activity"] == "unknown"
    assert out["sources"]["token_balances"] == "unknown"


@pytest.mark.asyncio
async def test_allowance_audit_flags_unlimited():
    client, rpc, scout = _parts()
    async with client:
        out = await reads.allowance_audit(client, rpc, scout, ADDR)
    assert out["spenders_checked"] == len(out["spender_coverage"]) >= 8
    assert out["unlimited_approvals"] == 1
    finding = out["active_approvals"][0]
    assert finding["unlimited"] is True
    assert finding["risk"] == "high"
    assert finding["spender_name"].startswith("Permit2")
    assert finding["token_symbol"] == "USDC"
    # Regression: the full-data body validates against the advertised schema.
    jsonschema.validate(out, ROUTES["/v1/allowance-audit"]["output_schema"])


@pytest.mark.asyncio
async def test_allowance_audit_degrades_when_explorer_down():
    """Explorer challenged → no exception: the audit returns a degraded
    body (the route then answers 200) with every token-dependent field
    marked 'unknown', mirroring wallet_snapshot's contract."""
    client, rpc, scout = _parts(blockscout_down=True)
    async with client:
        out = await reads.allowance_audit(client, rpc, scout, ADDR)
    assert out["address"] == ADDR
    assert out["tokens_held_checked"] == "unknown"
    assert out["active_approvals"] == "unknown"
    assert out["unlimited_approvals"] == "unknown"
    assert out["allowance_checks_performed"] == 0
    assert out["spenders_checked"] == len(out["spender_coverage"]) >= 8
    # The summary must not read as a clean bill of health.
    assert "not a finding" in out["summary"]
    assert "unknown" in out["note"]


@pytest.mark.asyncio
async def test_allowance_audit_degraded_body_is_schema_valid():
    """Inspectors compare delivered bodies to the advertised schema —
    the degraded shape must validate against it too."""
    client, rpc, scout = _parts(blockscout_down=True)
    async with client:
        out = await reads.allowance_audit(client, rpc, scout, ADDR)
    jsonschema.validate(out, ROUTES["/v1/allowance-audit"]["output_schema"])


@pytest.mark.asyncio
async def test_tx_decoder_happy_path():
    client, rpc, scout = _parts()
    async with client:
        out = await reads.tx_decoder(rpc, scout, TX_HASH)
    assert out["method"] == "transfer"
    assert out["status"] == "success"
    assert out["block_number"] == 995
    assert len(out["erc20_transfers"]) == 1
    t = out["erc20_transfers"][0]
    assert t["amount_raw"] == "12500000"
    assert t["to"] == ADDR
    assert "ERC-20 transfer" in out["summary"]


@pytest.mark.asyncio
async def test_tx_decoder_not_found_is_a_delivered_answer():
    """Regression for the Settled 502 (2026-10-24): the advertised
    input_example is a synthetic hash, and an inspector buying with
    it must get a truthful schema-valid answer — not a 502. When the
    RPC definitively answers null and the explorer has nothing, the
    decode is 'not found', with every descriptive field null/unknown
    and a summary that says so."""
    client, rpc, scout = _parts(rpc_tx_missing=True, explorer_tx_missing=True)
    async with client:
        out = await reads.tx_decoder(rpc, scout, TX_HASH)
    assert out["status"] == "unknown"
    assert out["value_eth"] == "unknown"
    assert out["from"] is None and out["to"] is None
    assert out["block_number"] is None and out["gas_used"] is None
    assert out["erc20_transfers"] == []
    assert "not found" in out["summary"].lower()
    jsonschema.validate(out, ROUTES["/v1/tx-decoder"]["output_schema"])


@pytest.mark.asyncio
async def test_tx_decoder_falls_back_to_explorer_when_rpc_down():
    """RPC hard-down (503s through its retry) but the explorer has
    the transaction: decode from Blockscout (v2 tx + its token
    transfers) instead of 502ing."""
    client, rpc, scout = _parts(rpc_down=True)
    async with client:
        out = await reads.tx_decoder(rpc, scout, TX_HASH)
    assert out["method"] == "transfer"
    assert out["status"] == "success"
    assert out["block_number"] == 995
    assert out["from"] == OTHER
    assert out["gas_used"] == 51604
    assert len(out["erc20_transfers"]) == 1
    assert out["erc20_transfers"][0]["amount_raw"] == "12500000"
    assert "explorer" in out["note"]
    jsonschema.validate(out, ROUTES["/v1/tx-decoder"]["output_schema"])


@pytest.mark.asyncio
async def test_tx_decoder_502s_only_when_no_source_can_supply():
    """Challenged explorer + down RPC is the one remaining 502: a
    clean retryable error (payment never settles on >= 400) rather
    than an invented decode."""
    client, rpc, scout = _parts(rpc_down=True, blockscout_down=True)
    async with client:
        with pytest.raises(UpstreamError):
            await reads.tx_decoder(rpc, scout, TX_HASH)


@pytest.mark.asyncio
async def test_tx_decoder_pending_receipt_degrades():
    """Tx visible but not yet mined (receipt null): decode what the
    tx itself proves and mark receipt-only fields unknown."""
    client, rpc, scout = _parts(rpc_receipt_missing=True)
    async with client:
        out = await reads.tx_decoder(rpc, scout, TX_HASH)
    assert out["method"] == "transfer"
    assert out["status"] == "unknown"
    assert out["gas_used"] is None
    assert out["erc20_transfers"] == []
    assert "receipt" in out["note"]


@pytest.mark.asyncio
async def test_gas_oracle_shape():
    client, rpc, _ = _parts()
    async with client:
        out = await reads.gas_oracle(rpc)
    assert out["chain"] == "base"
    assert out["latest_block"] == 1000
    assert out["blocks_sampled"] == 20
    assert out["trend"] in ("rising", "falling", "flat")
    assert out["gas_price_gwei"] == "0.0062"
