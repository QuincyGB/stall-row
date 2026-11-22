"""Liquidity Check: POST /v1/liquidity-check ($0.03).

A real DEX depth picture for one token contract, read from
DexScreener's public pair index (https://api.dexscreener.com) —
the DexScreener-style pair source named in every response. (This
service had no DEX pair source before this route: price-check reads
the CDP Bazaar x402 catalog, which carries no pool data, so no
existing in-repo source could answer a depth question honestly.)

What is read, per pair: dex id, pair address, base/quote symbols,
priceUsd, liquidity.usd (and the base/quote reserve amounts when
DexScreener supplies them), 24h volume and market cap / FDV.

Slippage model (stated in every response, never hidden): the pair
is treated as a constant-product pool whose USD quote-side reserve
is estimated as liquidity_usd / 2 (a balanced pool). For a trade of
S USD the estimated price impact / slippage is
    100 * S / (quote_reserve_usd + S)  percent.
That is an estimate from the pair data actually read — not a quote,
not a simulation of the pair's exact curve or fees.

Honesty: no Base pair for the token is a valid delivered answer
("no_live_market_found") — it is data, not a failure. Only an
unreachable / unparseable DexScreener response raises
UpstreamError (502, payment does not settle).
"""

from __future__ import annotations

import time

import httpx

from ..rpc import UpstreamError

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
SOURCE_NAME = "DexScreener public pair index (api.dexscreener.com/latest/dex/tokens)"
DEFAULT_TRADE_SIZE_USD = 1000.0

SLIPPAGE_MODEL = (
    "Constant-product estimate: quote-side USD reserve is estimated as "
    "liquidity_usd / 2 (balanced pool); estimated slippage percent = "
    "100 * trade_size_usd / (quote_reserve_usd_estimate + trade_size_usd). "
    "An estimate from the pair data read, not an executable quote; "
    "pool fees and exact reserves are not modelled."
)

LIMITS = (
    "Pairs are DexScreener's index of DEX pools at read time, filtered "
    "to Base (chainId 'base') and ranked by reported liquidity USD. "
    "Liquidity and price are the values DexScreener reports for the "
    "pair — this service reads no pool reserves directly on-chain. "
    + SLIPPAGE_MODEL
)


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def estimate_slippage_pct(trade_size_usd: float, liquidity_usd: float) -> float | None:
    """The stated constant-product estimate; None when it cannot be
    computed from the data read (no/zero liquidity)."""
    if liquidity_usd is None or liquidity_usd <= 0:
        return None
    quote_reserve = liquidity_usd / 2.0
    if quote_reserve + trade_size_usd <= 0:
        return None
    return round(100.0 * trade_size_usd / (quote_reserve + trade_size_usd), 4)


def _pair_view(pair: dict) -> dict:
    liq = pair.get("liquidity") or {}
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    return {
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
        "chain_id": pair.get("chainId"),
        "pair": f"{base.get('symbol') or '?'}/{quote.get('symbol') or '?'}",
        "base_token": {"address": base.get("address"), "symbol": base.get("symbol"), "name": base.get("name")},
        "quote_token": {"address": quote.get("address"), "symbol": quote.get("symbol"), "name": quote.get("name")},
        "price_usd": _f(pair.get("priceUsd")),
        "price_native": _f(pair.get("priceNative")),
        "liquidity_usd": _f(liq.get("usd")),
        "liquidity_base": _f(liq.get("base")),
        "liquidity_quote": _f(liq.get("quote")),
        "volume_24h_usd": _f((pair.get("volume") or {}).get("h24")),
        "market_cap_usd": _f(pair.get("marketCap")),
        "fdv_usd": _f(pair.get("fdv")),
        "url": pair.get("url"),
    }


async def liquidity_check(
    client: httpx.AsyncClient,
    token_address: str,
    trade_size_usd: float | None = None,
) -> dict:
    size = float(trade_size_usd) if trade_size_usd is not None else DEFAULT_TRADE_SIZE_USD
    size_assumed = trade_size_usd is None
    url = DEXSCREENER_TOKENS_URL.format(address=token_address)
    try:
        resp = await client.get(url, headers={"accept": "application/json"})
    except httpx.HTTPError as e:
        raise UpstreamError(f"DexScreener pair read failed: {e}") from e
    if resp.status_code != 200:
        raise UpstreamError(f"DexScreener pair read returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise UpstreamError("DexScreener pair read returned unparseable JSON") from e
    pairs_raw = data.get("pairs") if isinstance(data, dict) else None
    if pairs_raw is None:
        pairs_raw = []
    if not isinstance(pairs_raw, list):
        raise UpstreamError("DexScreener pair read returned an unexpected shape")

    base_pairs = [
        p for p in pairs_raw
        if isinstance(p, dict) and (p.get("chainId") == "base")
    ]
    views = [_pair_view(p) for p in base_pairs]
    views.sort(key=lambda v: v["liquidity_usd"] or 0.0, reverse=True)

    if not views:
        return {
            "token_address": token_address,
            "chain": "base",
            "status": "no_live_market_found",
            "detail": (
                "No live market found: DexScreener's pair index returned "
                "no Base pair for this token at read time. That is the "
                "answer this call can honestly give — no pair, no depth."
            ),
            "source": SOURCE_NAME,
            "pairs_found": 0,
            "best_pair": None,
            "pairs": [],
            "trade_size_usd": size,
            "trade_size_assumed": size_assumed,
            "estimated_slippage_pct": None,
            "slippage_model": SLIPPAGE_MODEL,
            "as_of_epoch": int(time.time()),
            "limits": LIMITS,
        }

    best = views[0]
    return {
        "token_address": token_address,
        "chain": "base",
        "status": "ok",
        "source": SOURCE_NAME,
        "pairs_found": len(views),
        "best_pair": best,
        "pairs": views[:5],
        "price_usd": best["price_usd"],
        "liquidity_usd": best["liquidity_usd"],
        "trade_size_usd": size,
        "trade_size_assumed": size_assumed,
        "quote_reserve_usd_estimate": (
            round(best["liquidity_usd"] / 2.0, 2) if best["liquidity_usd"] else None
        ),
        "estimated_slippage_pct": estimate_slippage_pct(size, best["liquidity_usd"] or 0.0),
        "slippage_model": SLIPPAGE_MODEL,
        "as_of_epoch": int(time.time()),
        "limits": LIMITS,
    }
