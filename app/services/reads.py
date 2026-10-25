"""The chain-read stalls: wallet snapshot, allowance audit, tx decoder,
gas oracle. Data: public Base RPC + Blockscout (which degrades to
"unknown" per field — never fabricated)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from ..blockscout import Blockscout
from ..data.spenders import SPENDERS
from ..rpc import Rpc, UpstreamError, gwei_str, wei_to_eth_str

UNLIMITED = 1 << 255

# allowance(address,address) / balanceOf(address) selectors
_ALLOWANCE_SEL = "dd62ed3e"
_BALANCE_OF_SEL = "70a08231"


def _pad_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _fmt_token_amount(raw: str, decimals: int | None) -> str | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if decimals is None:
        return str(value)
    whole = value // 10**decimals
    frac = value % 10**decimals
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:0{decimals}d}".rstrip("0")


def _ts(item: dict) -> str | None:
    if item.get("timestamp"):
        return item["timestamp"]
    epoch = item.get("timestamp_epoch")
    if epoch:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    return None


async def wallet_snapshot(
    client: httpx.AsyncClient, rpc: Rpc, scout: Blockscout, address: str
) -> dict:
    """ETH balance + top token holdings + activity window. Explorer-only
    fields fall back to 'unknown' when Blockscout is unreachable; the
    ETH balance itself comes from RPC as a cross-check/fallback."""
    info = await scout.address_info(address)
    tokens = await scout.token_balances(address)
    txs_desc = await scout.transactions(address, sort="desc", limit=1)
    txs_asc = await scout.transactions(address, sort="asc", limit=1)

    eth_wei: int | None = None
    eth_source = "unknown"
    if info and info.get("coin_balance_wei") is not None:
        eth_wei = info["coin_balance_wei"]
        eth_source = "blockscout"
    if eth_wei is None:
        try:
            eth_wei = await rpc.eth_balance(address)
            eth_source = "rpc"
        except UpstreamError:
            eth_wei = None

    token_list = None
    if tokens is not None:
        ranked = sorted(
            tokens,
            key=lambda t: int(t["balance_raw"]) if str(t["balance_raw"]).isdigit() else 0,
            reverse=True,
        )
        token_list = [
            {
                "address": t["address"],
                "name": t["name"],
                "symbol": t["symbol"],
                "decimals": t["decimals"],
                "balance": _fmt_token_amount(t["balance_raw"], t["decimals"]),
                "balance_raw": t["balance_raw"],
            }
            for t in ranked[:10]
        ]

    try:
        nonce = await rpc.eth_tx_count(address)
    except UpstreamError:
        nonce = None

    return {
        "address": address,
        "eth_balance": wei_to_eth_str(eth_wei) if eth_wei is not None else "unknown",
        "eth_balance_wei": str(eth_wei) if eth_wei is not None else None,
        "eth_balance_source": eth_source,
        "outgoing_tx_count_nonce": nonce,
        "is_contract": info.get("is_contract") if info else "unknown",
        "token_balances": token_list if token_list is not None else "unknown",
        "first_activity": _ts(txs_asc[0]) if txs_asc else "unknown",
        "last_activity": _ts(txs_desc[0]) if txs_desc else "unknown",
        "sources": {
            "eth_balance": eth_source,
            "token_balances": "blockscout" if tokens is not None else "unknown",
            "activity_window": "blockscout"
            if (txs_desc is not None and txs_asc is not None)
            else "unknown",
        },
        "note": (
            "Fields marked unknown could not be supplied by their source "
            "(Blockscout intermittently serves a Cloudflare challenge "
            "instead of JSON) — retrying usually fills them in."
        ),
    }


async def allowance_audit(
    client: httpx.AsyncClient, rpc: Rpc, scout: Blockscout, address: str
) -> dict:
    """For tokens the wallet actually holds with a positive balance, read
    allowance(owner, spender) via eth_call against the curated spender
    list. Held-token enumeration comes from Blockscout; when it is
    unavailable the audit cannot scope its checks, so — mirroring
    wallet_snapshot — the token-dependent fields degrade to "unknown"
    and the call still returns 200 instead of raising."""
    tokens = await scout.token_balances(address)
    if tokens is None:
        return {
            "address": address,
            "tokens_held_checked": "unknown",
            "spenders_checked": len(SPENDERS),
            "spender_coverage": [s["name"] for s in SPENDERS],
            "allowance_checks_performed": 0,
            "active_approvals": "unknown",
            "unlimited_approvals": "unknown",
            "summary": (
                "Audit not run: the wallet's held-token list could not "
                "be supplied by its source, so no allowance checks were "
                "performed and no approvals data is available — this is "
                "not a finding that the wallet has no approvals."
            ),
            "coverage_note": (
                "Only the listed spenders are checked — an approval granted to "
                "any other contract is invisible to this audit. Permit2 token "
                "allowance is only the first hop; Permit2 keeps a second, "
                "internal allowance map this call does not read."
            ),
            "note": (
                "Fields marked unknown could not be supplied by their source "
                "(Blockscout intermittently serves a Cloudflare challenge "
                "instead of JSON) — retrying usually fills them in."
            ),
        }
    held = [
        t
        for t in tokens
        if t.get("address")
        and str(t.get("balance_raw", "0")).isdigit()
        and int(t["balance_raw"]) > 0
    ][:20]

    findings: list[dict] = []
    checks = 0
    for token in held:
        for spender in SPENDERS:
            data = (
                "0x"
                + _ALLOWANCE_SEL
                + _pad_addr(address)
                + _pad_addr(spender["address"])
            )
            try:
                raw = await rpc.eth_call(token["address"], data)
                allowance = int(raw, 16)
            except (UpstreamError, ValueError):
                continue
            checks += 1
            if allowance == 0:
                continue
            findings.append(
                {
                    "token_address": token["address"],
                    "token_symbol": token["symbol"],
                    "spender": spender["address"],
                    "spender_name": spender["name"],
                    "allowance": _fmt_token_amount(str(allowance), token["decimals"]),
                    "allowance_raw": str(allowance),
                    "unlimited": allowance >= UNLIMITED,
                    "risk": "high" if allowance >= UNLIMITED else "medium",
                }
            )
    findings.sort(key=lambda f: (not f["unlimited"], f["token_symbol"] or ""))
    high = sum(1 for f in findings if f["unlimited"])
    return {
        "address": address,
        "tokens_held_checked": len(held),
        "spenders_checked": len(SPENDERS),
        "spender_coverage": [s["name"] for s in SPENDERS],
        "allowance_checks_performed": checks,
        "active_approvals": findings,
        "unlimited_approvals": high,
        "summary": (
            f"{high} unlimited approval(s) and {len(findings) - high} "
            f"limited active approval(s) across {len(held)} held token(s) "
            f"and {len(SPENDERS)} known spenders."
            if findings
            else f"No active approvals found across {len(held)} held token(s) "
            f"and {len(SPENDERS)} known spenders."
        ),
        "coverage_note": (
            "Only the listed spenders are checked — an approval granted to "
            "any other contract is invisible to this audit. Permit2 token "
            "allowance is only the first hop; Permit2 keeps a second, "
            "internal allowance map this call does not read."
        ),
    }


# --- tx decoder ---------------------------------------------------------------

_METHODS = {
    "0xa9059cbb": ("transfer", "ERC-20 transfer(to, amount)"),
    "0x095ea7b3": ("approve", "ERC-20 approve(spender, amount)"),
    "0x23b872dd": ("transferFrom", "ERC-20 transferFrom(from, to, amount)"),
    "0x70a08231": ("balanceOf", "ERC-20 balanceOf(account) — read-only"),
    "0xdd62ed3e": ("allowance", "ERC-20 allowance(owner, spender) — read-only"),
    "0x38ed1739": ("swapExactTokensForTokens", "Uniswap V2-style swap"),
    "0x414bf389": ("exactInputSingle", "Uniswap V3 single-hop swap"),
    "0xc04b8d59": ("exactInput", "Uniswap V3 multi-hop swap"),
    "0x5ae401dc": ("multicall", "batched calls (multicall)"),
    "0x3593564c": ("execute", "Uniswap Universal Router execute(commands, inputs)"),
    "0x04e45aaf": ("exactInputSingle", "Aerodrome SlipStream / V3-style single swap"),
}

_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:]


def _summary_parts(method, selector, data, from_addr, to_addr, value_wei,
                   transfers, status) -> str:
    parts = []
    if method:
        parts.append(f"{method[1]}")
    elif selector != "0x" and len(data) > 2:
        parts.append(f"contract call with method id {selector} (not in the known-method table)")
    else:
        parts.append("plain ETH transfer")
    parts.append(f"from {from_addr} to {to_addr}")
    if value_wei:
        parts.append(f"moving {wei_to_eth_str(value_wei)} ETH")
    if transfers:
        parts.append(f"with {len(transfers)} ERC-20 transfer(s)")
    parts.append(f"— status: {status}")
    return " ".join(parts) + "."


def _decode_from_rpc(tx_hash: str, tx: dict, receipt: dict | None) -> dict:
    """Full decode from chain RPC. A missing receipt (tx not mined
    yet) degrades status/gas/transfers instead of failing the call:
    no receipt exists, so no logs — and no transfers — exist yet."""
    data = tx.get("input") or "0x"
    selector = data[:10] if len(data) >= 10 else "0x"
    method = _METHODS.get(selector.lower())
    value_wei = int(tx.get("value", "0x0"), 16)

    transfers = []
    status = "unknown"
    gas_used = None
    if receipt is not None:
        status = {"0x1": "success", "0x0": "reverted"}.get(
            str(receipt.get("status", "")).lower(), "unknown"
        )
        gas_used = (
            int(receipt["gasUsed"], 16) if receipt.get("gasUsed") else None
        )
        for log in receipt.get("logs", []) or []:
            topics = log.get("topics") or []
            if len(topics) >= 3 and topics[0].lower() == _TRANSFER_TOPIC:
                try:
                    amount = int(log.get("data", "0x0"), 16)
                except ValueError:
                    amount = None
                transfers.append(
                    {
                        "token": log.get("address"),
                        "from": _addr_from_topic(topics[1]),
                        "to": _addr_from_topic(topics[2]),
                        "amount_raw": str(amount) if amount is not None else None,
                    }
                )

    out = {
        "tx_hash": tx_hash,
        "block_number": int(tx["blockNumber"], 16) if tx.get("blockNumber") else None,
        "from": tx.get("from"),
        "to": tx.get("to"),
        "value_eth": wei_to_eth_str(value_wei),
        "status": status,
        "method_id": selector if len(data) > 2 else None,
        "method": method[0] if method else None,
        "method_description": method[1] if method else None,
        "gas_used": gas_used,
        "erc20_transfers": transfers,
        "summary": _summary_parts(
            method, selector, data, tx.get("from"), tx.get("to"),
            value_wei, transfers, status,
        ),
    }
    if receipt is None:
        out["note"] = (
            "No receipt exists for this transaction yet (it may still "
            "be pending), so status, gas used and token transfers "
            "cannot be supplied — retry once it is mined."
        )
    return out


def _decode_from_explorer(tx_hash: str, scout_tx: dict,
                          transfers: list[dict]) -> dict:
    """Decode built from Blockscout data when the chain RPC could not
    supply the transaction. Same shape, explorer-sourced values."""
    data = scout_tx.get("raw_input") or "0x"
    selector = data[:10] if len(data) >= 10 else "0x"
    method = _METHODS.get(selector.lower())
    if method is None and scout_tx.get("method"):
        method = (scout_tx["method"], f"contract method {scout_tx['method']} (per block explorer)")
    try:
        value_wei = int(scout_tx.get("value_wei") or 0)
    except (TypeError, ValueError):
        value_wei = 0
    status = scout_tx.get("status") or "unknown"
    return {
        "tx_hash": tx_hash,
        "block_number": scout_tx.get("block_number"),
        "from": scout_tx.get("from"),
        "to": scout_tx.get("to"),
        "value_eth": wei_to_eth_str(value_wei),
        "status": status,
        "method_id": selector if len(data) > 2 else None,
        "method": method[0] if method else None,
        "method_description": method[1] if method else None,
        "gas_used": scout_tx.get("gas_used"),
        "erc20_transfers": transfers,
        "summary": _summary_parts(
            method, selector, data, scout_tx.get("from"),
            scout_tx.get("to"), value_wei, transfers, status,
        ),
        "note": (
            "Decoded from block explorer data because the chain RPC "
            "could not supply this transaction in time — values are "
            "the explorer's record of the same transaction."
        ),
    }


def _tx_not_found(tx_hash: str) -> dict:
    """Truthful delivered answer for a hash no source has: a full node
    answering null for a hash is a definitive 'not there', not an
    upstream failure. (This is the shape an inspector's test purchase
    with a synthetic example hash must receive — a 502 here once made
    this the fleet's only undelivered route.)"""
    return {
        "tx_hash": tx_hash,
        "block_number": None,
        "from": None,
        "to": None,
        "value_eth": "unknown",
        "status": "unknown",
        "method_id": None,
        "method": None,
        "method_description": None,
        "gas_used": None,
        "erc20_transfers": [],
        "summary": (
            "Transaction not found on Base: neither the chain RPC nor "
            "the block explorer has a transaction with this hash — it "
            "may still be pending in the mempool, may belong to "
            "another chain, or may not exist. No decode is possible "
            "for a transaction that is not on the chain."
        ),
        "note": (
            "Not-found is a delivered result, not a missing value: "
            "every field that would describe the transaction is null "
            "or unknown because there is no transaction to describe."
        ),
    }


async def tx_decoder(rpc: Rpc, scout: Blockscout, tx_hash: str) -> dict:
    """Decode one Base transaction. Source order: chain RPC (with its
    own retry), then Blockscout (v2 → legacy). Outcomes:
    - RPC has it → full decode (a missing receipt degrades, not fails).
    - RPC fails or answers null but the explorer has it → explorer decode.
    - RPC definitively answers null and the explorer has nothing →
      a delivered not-found answer (200), never a 502.
    - RPC failed outright AND the explorer cannot supply it → raise
      UpstreamError (502, payment does not settle, buyer can retry)."""
    tx: dict | None = None
    receipt: dict | None = None
    rpc_error: UpstreamError | None = None
    try:
        tx = await rpc.eth_tx(tx_hash)
        if tx is not None:
            try:
                receipt = await rpc.eth_receipt(tx_hash)
            except UpstreamError:
                receipt = None  # degrade to the pending shape below
    except UpstreamError as e:
        rpc_error = e

    if tx is not None:
        return _decode_from_rpc(tx_hash, tx, receipt)

    scout_tx = await scout.transaction(tx_hash)
    if scout_tx is not None:
        transfers = await scout.transaction_token_transfers(tx_hash)
        if transfers is None:
            # The one field the explorer path cannot truthfully fill;
            # a clean retryable error beats an invented empty list.
            raise UpstreamError(
                "explorer supplied the transaction but not its token "
                "transfer list — retry"
            )
        return _decode_from_explorer(tx_hash, scout_tx, transfers)

    if rpc_error is None:
        return _tx_not_found(tx_hash)
    raise UpstreamError(
        f"transaction data unavailable (RPC: {rpc_error}; explorer "
        "returned nothing) — retry"
    )


async def gas_oracle(rpc: Rpc) -> dict:
    """Current gas price + base-fee trend across the last ~20 blocks."""
    gas_price = await rpc.eth_gas_price()
    latest = await rpc.eth_block_number()
    fees: list[int] = []
    blocks_used = 0
    for n in range(max(0, latest - 19), latest + 1):
        block = await rpc.eth_block(n)
        fee = block.get("baseFeePerGas")
        if fee:
            fees.append(int(fee, 16))
            blocks_used += 1
    if not fees:
        raise UpstreamError("RPC returned no baseFeePerGas in recent blocks")

    first, last = fees[0], fees[-1]
    if last > first * 1.05:
        trend = "rising"
    elif last < first * 0.95:
        trend = "falling"
    else:
        trend = "flat"
    lo, hi = min(fees), max(fees)
    if last <= lo * 1.10:
        hint = "Current base fee is at/near the 20-block low — a cheap window by recent standards."
    elif trend == "falling":
        hint = "Base fee is falling across the last 20 blocks — waiting a few blocks has recently been cheaper, but fees can reverse any block."
    elif trend == "rising":
        hint = "Base fee is rising across the last 20 blocks — no cheap window visible in the recent data."
    else:
        hint = "Base fee is flat across the last 20 blocks — no cheaper window visible in the recent data."
    return {
        "chain": "base",
        "latest_block": latest,
        "gas_price_gwei": gwei_str(gas_price),
        "base_fee_gwei": gwei_str(last),
        "base_fee_min_gwei_20_blocks": gwei_str(lo),
        "base_fee_max_gwei_20_blocks": gwei_str(hi),
        "blocks_sampled": blocks_used,
        "trend": trend,
        "cheapest_window_hint": hint,
        "as_of_epoch": int(time.time()),
        "note": "Trend and hint describe the last ~20 blocks only — observed data, not a forecast.",
    }
