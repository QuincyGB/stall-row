"""Reads: wallet snapshot, allowance audit, tx decoder, gas oracle —
happy paths from fixtures, plus the degradation contract (explorer
down → fields 'unknown', never invented; the allowance audit degrades
the same way instead of failing)."""

import jsonschema
import pytest

from app.blockscout import Blockscout
from app.config import load_settings
from app.discovery import ROUTES
from app.rpc import Rpc
from app.services import reads
from tests.fixtures import ADDR, TX_HASH, mock_client

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
    client, rpc, _ = _parts()
    async with client:
        out = await reads.tx_decoder(rpc, TX_HASH)
    assert out["method"] == "transfer"
    assert out["status"] == "success"
    assert out["block_number"] == 995
    assert len(out["erc20_transfers"]) == 1
    t = out["erc20_transfers"][0]
    assert t["amount_raw"] == "12500000"
    assert t["to"] == ADDR
    assert "ERC-20 transfer" in out["summary"]


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
