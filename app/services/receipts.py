"""Receipt Verifier: POST /v1/receipt-verify ($0.002).

A transaction hash in, a normalized verified receipt out — read
from Base RPC only (eth_getTransactionByHash, eth_getTransactionReceipt,
eth_blockNumber), the same chain reads the rest of the fleet uses:

  payer      — the transaction's `from`.
  pay_to     — the recipient of the tx's USDC Transfer log (the
               payment itself); when the tx carries no USDC
               transfer, the transaction's own `to` and the asset
               reads as native ETH, stated plainly.
  amount     — the Transfer log's value (or the tx value for ETH).
  settled    — the receipt status is 0x1, with the confirmation
               count computed against the current block.
  fleet route candidates — when the payTo is the fleet's own payTo
               and the amount equals a priced route, the routes at
               exactly that price are listed as CANDIDATES. An
               amount alone never identifies a route (several share
               a price), so the report never claims more than that.

An unknown or unconfirmed transaction raises UpstreamError and
the payment does not settle — an unverifiable receipt is not a
product. Base only: this service holds no Solana machinery, and
the report says so.
"""

from __future__ import annotations

import re

import httpx

from ..config import PRICE_UNITS
from ..discovery import USDC_ASSET
from ..rpc import Rpc, UpstreamError
from . import fleetpass as fleetpass_svc

_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
NETWORK = "eip155:8453"

LIMITS = (
    "Base only (eip155:8453) — Solana signatures are not read by "
    "this service. The payTo and amount come from the transaction's "
    "USDC Transfer log; a transaction with several USDC transfers "
    "lists them all and names the first as the primary payment. "
    "Fleet route matches are candidates at the paid price, not "
    "proof of which route was called."
)


def _topic_address(topic: str) -> str | None:
    if not isinstance(topic, str) or len(topic) < 42:
        return None
    return "0x" + topic[-40:]


def _usdc_transfers(receipt: dict) -> list[dict]:
    out = []
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        if (log.get("address") or "").lower() != USDC_ASSET.lower():
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or (topics[0] or "").lower() != _TRANSFER_TOPIC:
            continue
        try:
            value = int(log.get("data") or "0x0", 16)
        except ValueError:
            continue
        out.append(
            {
                "from": _topic_address(topics[1]),
                "to": _topic_address(topics[2]),
                "amount_units": value,
            }
        )
    return out


async def verify_receipt(
    client: httpx.AsyncClient, rpc: Rpc, *, tx_hash: str, fleet_pay_to: str
) -> dict:
    tx_hash = tx_hash.strip()
    if not _TX_HASH_RE.match(tx_hash):
        raise ValueError("tx_hash must be a 0x-prefixed 64-hex transaction hash")
    tx = await rpc.eth_tx(tx_hash)
    if tx is None:
        raise UpstreamError(
            f"transaction {tx_hash} not found on Base — nothing to verify"
        )
    receipt = await rpc.eth_receipt(tx_hash)
    if receipt is None:
        raise UpstreamError(
            f"transaction {tx_hash} has no receipt yet (unconfirmed or unknown)"
        )
    head = await rpc.eth_block_number()

    transfers = _usdc_transfers(receipt)
    payer = tx.get("from")
    if transfers:
        primary = transfers[0]
        pay_to = primary["to"]
        amount_units = primary["amount_units"]
        asset = USDC_ASSET
        asset_symbol = "USDC"
    else:
        pay_to = tx.get("to")
        try:
            amount_units = int(tx.get("value") or "0x0", 16)
        except ValueError:
            amount_units = 0
        asset = None
        asset_symbol = "ETH (no USDC transfer in this transaction)"

    block_number = receipt.get("blockNumber") or tx.get("blockNumber")
    confirmations = None
    if isinstance(block_number, str):
        try:
            confirmations = max(0, head - int(block_number, 16))
        except ValueError:
            confirmations = None
    settled = receipt.get("status") == "0x1"

    candidates: list[dict] = []
    if (
        pay_to
        and fleet_pay_to
        and pay_to.lower() == fleet_pay_to.lower()
        and asset_symbol == "USDC"
    ):
        # The candidate universe: every Fleet-Pass-debitable route
        # (fleetpass.FLEET_ROUTES, all five services) plus this
        # service's own priced routes — the two purchase routes
        # (fleet-pass, recheck/pack) are not pass-debitable, so they
        # are absent from FLEET_ROUTES but are real priced routes a
        # receipt could be paying for.
        universe = dict(fleetpass_svc.FLEET_ROUTES)
        for route, price in PRICE_UNITS.items():
            universe.setdefault(("stall-row", route), price)
        for (service, route), price in sorted(universe.items()):
            if price == amount_units:
                candidates.append({"service": service, "route": route})

    return {
        "tx_hash": tx_hash,
        "network": NETWORK,
        "chain": "base",
        "payer": payer,
        "pay_to": pay_to,
        "amount_units": amount_units,
        "amount_usd": f"{amount_units / 1_000_000:.6f}".rstrip("0").rstrip(".")
        if amount_units
        else "0",
        "asset": asset,
        "asset_symbol": asset_symbol,
        "settled": settled,
        "receipt_status": receipt.get("status"),
        "block_number": int(block_number, 16)
        if isinstance(block_number, str)
        else None,
        "confirmations": confirmations,
        "usdc_transfers": transfers,
        "fleet_route_candidates": candidates,
        "limits": LIMITS,
    }
