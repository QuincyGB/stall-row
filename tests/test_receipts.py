"""Receipt Verifier: normalized receipts from Base RPC reads —
payer/payTo/amount from the USDC Transfer log, settlement and
confirmations from the receipt and chain head, fleet route
candidates by exact price (never overclaimed). Unknown or
unconfirmed transactions raise UpstreamError (unsettled). Offline:
tests.fixtures plays the RPC; one custom transport covers the
fleet-price match."""

import json

import httpx
import pytest

from app.config import load_settings
from app.rpc import Rpc, UpstreamError
from app.services import receipts
from tests.fixtures import ADDR, OTHER, TX_HASH, mock_client

S = load_settings()
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@pytest.mark.asyncio
async def test_verified_usdc_receipt_from_fixtures():
    async with mock_client() as client:
        rpc = Rpc(client, S.rpc_url)
        out = await receipts.verify_receipt(
            client, rpc, tx_hash=TX_HASH, fleet_pay_to=ADDR
        )
    assert out["payer"] == OTHER
    assert out["tx_sender"] == OTHER  # fixture tx is not facilitator-relayed
    assert out["pay_to"] == ADDR
    assert out["amount_units"] == 12_500_000
    assert out["amount_usd"] == "12.5"
    assert out["asset"] == USDC
    assert out["asset_symbol"] == "USDC"
    assert out["settled"] is True
    assert out["network"] == "eip155:8453"
    assert out["block_number"] == 995
    assert out["confirmations"] == 5  # head 1000 - block 995
    assert out["fleet_route_candidates"] == []  # $12.50 matches no route


@pytest.mark.asyncio
async def test_unknown_tx_is_an_honest_error():
    async with mock_client(rpc_tx_missing=True) as client:
        rpc = Rpc(client, S.rpc_url)
        with pytest.raises(UpstreamError):
            await receipts.verify_receipt(
                client, rpc, tx_hash=TX_HASH, fleet_pay_to=ADDR
            )


@pytest.mark.asyncio
async def test_unconfirmed_tx_is_an_honest_error():
    async with mock_client(rpc_receipt_missing=True) as client:
        rpc = Rpc(client, S.rpc_url)
        with pytest.raises(UpstreamError):
            await receipts.verify_receipt(
                client, rpc, tx_hash=TX_HASH, fleet_pay_to=ADDR
            )


@pytest.mark.asyncio
async def test_malformed_hash_refused():
    async with mock_client() as client:
        rpc = Rpc(client, S.rpc_url)
        with pytest.raises(ValueError):
            await receipts.verify_receipt(
                client, rpc, tx_hash="0x1234", fleet_pay_to=ADDR
            )


def _fleet_client(amount_units: int) -> httpx.AsyncClient:
    def handler(request):
        body = json.loads(request.content)
        method = body["method"]
        if method == "eth_getTransactionByHash":
            result = {
                "hash": TX_HASH, "blockNumber": hex(900), "from": OTHER,
                "to": USDC, "value": "0x0", "input": "0x",
            }
        elif method == "eth_getTransactionReceipt":
            result = {
                "status": "0x1",
                "blockNumber": hex(900),
                "logs": [
                    {
                        "address": USDC,
                        "topics": [
                            TRANSFER_TOPIC,
                            "0x" + "0" * 24 + OTHER[2:],
                            "0x" + "0" * 24 + ADDR[2:],
                        ],
                        "data": hex(amount_units),
                    }
                ],
            }
        elif method == "eth_blockNumber":
            result = hex(1000)
        else:
            raise AssertionError(method)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fleet_price_match_lists_candidates():
    async with _fleet_client(1_000_000) as client:
        rpc = Rpc(client, S.rpc_url)
        out = await receipts.verify_receipt(
            client, rpc, tx_hash=TX_HASH, fleet_pay_to=ADDR
        )
    routes = {(c["service"], c["route"]) for c in out["fleet_route_candidates"]}
    assert ("stall-row", "/v1/counterparty-report") in routes
    assert ("stall-row", "/v1/recheck/pack") in routes
    # Every candidate is a $1.00 route — the match is by price alone.
    from app.config import PRICE_UNITS
    from app.services.fleetpass import FLEET_ROUTES

    universe = dict(FLEET_ROUTES)
    for route, price in PRICE_UNITS.items():
        universe.setdefault(("stall-row", route), price)
    assert all(universe[(s, r)] == 1_000_000 for s, r in routes)


@pytest.mark.asyncio
async def test_non_fleet_payto_has_no_candidates():
    async with _fleet_client(1_000_000) as client:
        rpc = Rpc(client, S.rpc_url)
        out = await receipts.verify_receipt(
            client, rpc, tx_hash=TX_HASH, fleet_pay_to=OTHER
        )
    assert out["fleet_route_candidates"] == []
