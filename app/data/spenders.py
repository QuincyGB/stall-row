"""Curated list of well-known Base spender contracts checked by
/v1/allowance-audit. Coverage is honestly bounded: the audit checks
allowance(owner, spender) for the wallet's held tokens against THESE
spenders only, and the response says how many were checked. New routers
deploy over time; an approval to an unlisted spender is invisible here.
Addresses are the canonical Base mainnet deployments (chain id 8453).
"""

from __future__ import annotations

SPENDERS: list[dict[str, str]] = [
    {
        "address": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        "name": "Permit2 (Uniswap)",
        "note": "Signature-based approvals; token allowance here is only the first hop — Permit2 keeps its own internal allowance map.",
    },
    {
        "address": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "name": "Uniswap V3 SwapRouter02",
        "note": "Uniswap V3 router on Base.",
    },
    {
        "address": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "name": "Uniswap Universal Router (v1)",
        "note": "Uniswap Universal Router deployment on Base.",
    },
    {
        "address": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
        "name": "Uniswap Universal Router 2.0",
        "note": "Second-generation Universal Router on Base.",
    },
    {
        "address": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "name": "Aerodrome SlipStream Router",
        "note": "Aerodrome concentrated-liquidity router on Base.",
    },
    {
        "address": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
        "name": "Aerodrome Router (V2)",
        "note": "Aerodrome classic AMM router on Base.",
    },
    {
        "address": "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5",
        "name": "Base SwapRouter (Uniswap V2-style)",
        "note": "V2-style router used by several Base DEX front-ends.",
    },
    {
        "address": "0x111111125421cA6dc452d289314280a0f8842A65",
        "name": "1inch Aggregation Router V6",
        "note": "1inch aggregator router (multi-chain deployment, incl. Base).",
    },
    {
        "address": "0x94cC0AaC535CCDB3C01d6787D6413C739ae12bc4",
        "name": "Odos Router V2",
        "note": "Odos aggregator router on Base.",
    },
    {
        "address": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "name": "KyberSwap Aggregation Router V6",
        "note": "KyberSwap aggregator router (multi-chain, incl. Base).",
    },
]
