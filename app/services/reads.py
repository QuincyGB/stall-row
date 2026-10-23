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


async def tx_decoder(rpc: Rpc, tx_hash: str) -> dict:
    tx = await rpc.eth_tx(tx_hash)
    receipt = await rpc.eth_receipt(tx_hash)

    data = tx.get("input") or "0x"
    selector = data[:10] if len(data) >= 10 else "0x"
    method = _METHODS.get(selector.lower())
    value_wei = int(tx.get("value", "0x0"), 16)
    status = {"0x1": "success", "0x0": "reverted"}.get(
        str(receipt.get("status", "")).lower(), "unknown"
    )

    transfers = []
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

    to_addr = tx.get("to")
    parts = []
    if method:
        parts.append(f"{method[1]}")
    elif selector != "0x" and len(data) > 2:
        parts.append(f"contract call with method id {selector} (not in the known-method table)")
    else:
        parts.append("plain ETH transfer")
    parts.append(f"from {tx.get('from')} to {to_addr}")
    if value_wei:
        parts.append(f"moving {wei_to_eth_str(value_wei)} ETH")
    if transfers:
        parts.append(f"with {len(transfers)} ERC-20 transfer(s)")
    parts.append(f"— status: {status}")
    summary = " ".join(parts) + "."

    return {
        "tx_hash": tx_hash,
        "block_number": int(tx["blockNumber"], 16) if tx.get("blockNumber") else None,
        "from": tx.get("from"),
        "to": to_addr,
        "value_eth": wei_to_eth_str(value_wei),
        "status": status,
        "method_id": selector if len(data) > 2 else None,
        "method": method[0] if method else None,
        "method_description": method[1] if method else None,
        "gas_used": int(receipt["gasUsed"], 16) if receipt.get("gasUsed") else None,
        "erc20_transfers": transfers,
        "summary": summary,
    }


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
