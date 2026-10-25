"""Agent-discovery surfaces for the whole row: x402 manifest, skill.md,
llms.txt, and one Bazaar discovery extension per route.

Standing rules (inherited from the sibling services, all test-pinned):
every route's extension carries a FULL typed output schema (never the
hollow {"type", "example"} shape), every description is under the
500-char CDP limit and carries a "use when" sentence, and the manifest
accepts mirror the live 402 challenges exactly.
"""

from __future__ import annotations

from .config import PRICE_UNITS, Settings, units_to_price

USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

_S = {"type": "string"}
_N = {"type": ["string", "null"]}
_I = {"type": "integer"}
_IS = {"type": ["integer", "string"]}
_IN = {"type": ["integer", "null"]}
_B = {"type": "boolean"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# Route table: path -> (description, input schema, output schema, examples,
# tags). Descriptions MUST stay < 500 chars (asserted at import time and
# in tests) and contain a "Use ... when" sentence.
# ---------------------------------------------------------------------------

ROUTES: dict[str, dict] = {
    "/v1/wallet-snapshot": {
        "description": (
            "Snapshot one Base wallet in a single call: ETH balance, top token "
            "holdings, first and last activity, and outgoing transaction count. "
            "Use this when an agent needs a wallet's shape before deciding to "
            "pay, trust, or transact with it. Explorer fields that cannot be "
            "supplied come back marked unknown, never invented."
        ),
        "tags": ["base", "wallet", "balances", "data", "agents"],
        "input_schema": _obj({"address": _S}, ["address"]),
        "input_example": {"address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"},
        "output_schema": _obj(
            {
                "address": _S,
                "eth_balance": _S,
                "eth_balance_wei": _N,
                "eth_balance_source": _S,
                "outgoing_tx_count_nonce": _IN,
                "is_contract": {"type": ["boolean", "string", "null"]},
                "token_balances": {
                    "type": ["array", "string"],
                    "items": _obj(
                        {
                            "address": _N,
                            "name": _N,
                            "symbol": _N,
                            "decimals": _IN,
                            "balance": _N,
                            "balance_raw": _N,
                        },
                        ["address", "name", "symbol", "decimals", "balance", "balance_raw"],
                    ),
                },
                "first_activity": _S,
                "last_activity": _S,
                "sources": {"type": "object"},
                "note": _S,
            },
            ["address", "eth_balance", "sources"],
        ),
        "output_example": {
            "address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F",
            "eth_balance": "0.0042",
            "eth_balance_wei": "4200000000000000",
            "eth_balance_source": "blockscout",
            "outgoing_tx_count_nonce": 137,
            "is_contract": False,
            "token_balances": [
                {
                    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "decimals": 6,
                    "balance": "12.5",
                    "balance_raw": "12500000",
                }
            ],
            "first_activity": "2025-11-02T14:03:11.000000Z",
            "last_activity": "2026-10-20T09:41:52.000000Z",
            "sources": {"eth_balance": "blockscout", "token_balances": "blockscout", "activity_window": "blockscout"},
            "note": "Fields marked unknown could not be supplied by their source.",
        },
    },
    "/v1/allowance-audit": {
        "description": (
            "Audit one Base wallet's ERC-20 approvals against a curated list of "
            "major spenders (Permit2, Uniswap, Aerodrome, aggregators): active "
            "approvals are listed and unlimited ones flagged high risk. Use this "
            "when an agent or its owner wants to know what a wallet has already "
            "authorized before it holds or moves funds. When the explorer "
            "cannot supply the held-token list, the token-dependent fields "
            "come back marked unknown rather than the call failing."
        ),
        "tags": ["base", "security", "approvals", "allowance", "agents"],
        "input_schema": _obj({"address": _S}, ["address"]),
        "input_example": {"address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"},
        "output_schema": _obj(
            {
                "address": _S,
                "tokens_held_checked": _IS,
                "spenders_checked": _I,
                "spender_coverage": {"type": "array", "items": _S},
                "allowance_checks_performed": _I,
                "active_approvals": {
                    "type": ["array", "string"],
                    "items": _obj(
                        {
                            "token_address": _N,
                            "token_symbol": _N,
                            "spender": _S,
                            "spender_name": _S,
                            "allowance": _N,
                            "allowance_raw": _S,
                            "unlimited": _B,
                            "risk": _S,
                        },
                        ["spender", "spender_name", "allowance_raw", "unlimited", "risk"],
                    ),
                },
                "unlimited_approvals": _IS,
                "summary": _S,
                "coverage_note": _S,
                "note": _S,
            },
            ["address", "tokens_held_checked", "spenders_checked", "active_approvals", "unlimited_approvals", "summary"],
        ),
        "output_example": {
            "address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F",
            "tokens_held_checked": 2,
            "spenders_checked": 10,
            "spender_coverage": ["Permit2 (Uniswap)", "Uniswap V3 SwapRouter02"],
            "allowance_checks_performed": 20,
            "active_approvals": [],
            "unlimited_approvals": 0,
            "summary": "No active approvals found across 2 held token(s) and 10 known spenders.",
            "coverage_note": "Only the listed spenders are checked.",
        },
    },
    "/v1/tx-decoder": {
        "description": (
            "Decode one Base transaction into plain English: sender, receiver, "
            "ETH value, status, the known method behind the selector, every "
            "ERC-20 transfer it made, and a one-sentence summary. Use this "
            "when an agent needs to explain or verify what a transaction "
            "actually did before acting on it."
        ),
        "tags": ["base", "transactions", "decode", "data", "agents"],
        "input_schema": _obj({"tx_hash": _S}, ["tx_hash"]),
        "input_example": {"tx_hash": "0x" + "ab" * 32},
        "output_schema": _obj(
            {
                "tx_hash": _S,
                "block_number": _IN,
                "from": _N,
                "to": _N,
                "value_eth": _S,
                "status": _S,
                "method_id": _N,
                "method": _N,
                "method_description": _N,
                "gas_used": _IN,
                "erc20_transfers": {
                    "type": "array",
                    "items": _obj(
                        {"token": _N, "from": _S, "to": _S, "amount_raw": _N},
                        ["from", "to"],
                    ),
                },
                "summary": _S,
            },
            ["tx_hash", "value_eth", "status", "erc20_transfers", "summary"],
        ),
        "output_example": {
            "tx_hash": "0x" + "ab" * 32,
            "block_number": 22104511,
            "from": "0x1111111111111111111111111111111111111111",
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "value_eth": "0",
            "status": "success",
            "method_id": "0xa9059cbb",
            "method": "transfer",
            "method_description": "ERC-20 transfer(to, amount)",
            "gas_used": 51604,
            "erc20_transfers": [
                {
                    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "from": "0x1111111111111111111111111111111111111111",
                    "to": "0x2222222222222222222222222222222222222222",
                    "amount_raw": "12500000",
                }
            ],
            "summary": "ERC-20 transfer(to, amount) from 0x1111111111111111111111111111111111111111 to 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 with 1 ERC-20 transfer(s) — status: success.",
        },
    },
    "/v1/gas-oracle": {
        "description": (
            "Current Base gas in one call: the live gas price, the current "
            "base fee, the base-fee range across the last 20 blocks, whether "
            "fees are rising, falling, or flat, and a cheap-window hint drawn "
            "from that data. Use this when an agent wants to time a "
            "transaction instead of paying whatever the moment charges."
        ),
        "tags": ["base", "gas", "fees", "data", "agents"],
        "input_schema": _obj({}, []),
        "input_example": {},
        "output_schema": _obj(
            {
                "chain": _S,
                "latest_block": _I,
                "gas_price_gwei": _S,
                "base_fee_gwei": _S,
                "base_fee_min_gwei_20_blocks": _S,
                "base_fee_max_gwei_20_blocks": _S,
                "blocks_sampled": _I,
                "trend": {"type": "string", "enum": ["rising", "falling", "flat"]},
                "cheapest_window_hint": _S,
                "as_of_epoch": _I,
                "note": _S,
            },
            ["chain", "latest_block", "gas_price_gwei", "base_fee_gwei", "trend", "cheapest_window_hint"],
        ),
        "output_example": {
            "chain": "base",
            "latest_block": 22104511,
            "gas_price_gwei": "0.0062",
            "base_fee_gwei": "0.0058",
            "base_fee_min_gwei_20_blocks": "0.0051",
            "base_fee_max_gwei_20_blocks": "0.0079",
            "blocks_sampled": 20,
            "trend": "flat",
            "cheapest_window_hint": "Base fee is flat across the last 20 blocks.",
            "as_of_epoch": 1761000000,
            "note": "Trend and hint describe the last ~20 blocks only.",
        },
    },
    "/v1/wallet-verdict": {
        "description": (
            "A trust verdict for one Base wallet: OFAC SDN exact-match against "
            "a dated snapshot, wallet age, activity level, and an "
            "address-poisoning scan of its recent token transfers, combined "
            "into SAFE, CAUTION, or AVOID with a 0-100 score and itemized "
            "reasons. Use this when an agent must decide whether a wallet is "
            "safe to pay or take instructions from."
        ),
        "tags": ["base", "security", "sanctions", "verdict", "agents"],
        "input_schema": _obj({"address": _S}, ["address"]),
        "input_example": {"address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"},
        "output_schema": _obj(
            {
                "address": _S,
                "verdict": {"type": "string", "enum": ["SAFE", "CAUTION", "AVOID"]},
                "score": _I,
                "sanctions": {"type": "object"},
                "wallet_age_days": _IN,
                "first_activity": _S,
                "outgoing_tx_count_nonce": _IN,
                "poisoning": {"type": "object"},
                "reasons": {
                    "type": "array",
                    "items": _obj({"name": _S, "points": _I, "detail": _S}, ["name", "points", "detail"]),
                },
                "limits": _S,
            },
            ["address", "verdict", "score", "sanctions", "poisoning", "reasons"],
        ),
        "output_example": {
            "address": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F",
            "verdict": "SAFE",
            "score": 100,
            "sanctions": {"status": "no_match", "matched": False, "snapshot_date": "2026-10-21"},
            "wallet_age_days": 352,
            "first_activity": "2025-11-02T14:03:11+00:00",
            "outgoing_tx_count_nonce": 137,
            "poisoning": {"status": "none_found", "signals": []},
            "reasons": [],
            "limits": "Sanctions matching is exact-address only against the bundled snapshot.",
        },
    },
    "/v1/tube-search": {
        "description": (
            "YouTube search results as clean JSON: title, video id, URL, "
            "channel, duration, and views for each result, scraped from the "
            "public results page with no API key. Use this when an agent "
            "needs to find a video mid-task and cannot drive a browser or "
            "hold a Google API key."
        ),
        "tags": ["search", "youtube", "video", "data", "agents"],
        "input_schema": _obj(
            {"query": _S, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
            ["query"],
        ),
        "input_example": {"query": "base blockchain x402 tutorial", "max_results": 5},
        "output_schema": _obj(
            {
                "query": _S,
                "count": _I,
                "results": {
                    "type": "array",
                    "items": _obj(
                        {
                            "title": _S,
                            "video_id": _S,
                            "url": _S,
                            "channel": _N,
                            "duration": _N,
                            "views": _N,
                        },
                        ["title", "video_id", "url"],
                    ),
                },
            },
            ["query", "count", "results"],
        ),
        "output_example": {
            "query": "base blockchain x402 tutorial",
            "count": 1,
            "results": [
                {
                    "title": "x402 explained in 10 minutes",
                    "video_id": "dQw4w9WgXcQ",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "channel": "Example Channel",
                    "duration": "10:24",
                    "views": "12K views",
                }
            ],
        },
    },
    "/v1/page-to-markdown": {
        "description": (
            "Fetch one web page and return its main content as clean "
            "Markdown with title and word count, extracted server-side. "
            "Private and loopback targets are refused. Use this when an "
            "agent needs to read a page inside a workflow without a browser "
            "and without wading through navigation and boilerplate."
        ),
        "tags": ["fetch", "markdown", "web", "reading", "agents"],
        "input_schema": _obj({"url": _S}, ["url"]),
        "input_example": {"url": "https://www.x402.org/"},
        "output_schema": _obj(
            {
                "url": _S,
                "final_url": _S,
                "title": _N,
                "markdown": _S,
                "word_count": _I,
            },
            ["url", "final_url", "markdown", "word_count"],
        ),
        "output_example": {
            "url": "https://www.x402.org/",
            "final_url": "https://www.x402.org/",
            "title": "x402 — internet-native payments",
            "markdown": "# x402\n\nAn open standard for internet-native payments.",
            "word_count": 8,
        },
    },
    "/v1/repo-vitals": {
        "description": (
            "Vital signs for one GitHub repository in a single call: stars, "
            "forks, open issues, license, last push, archived flag, and a "
            "0-100 health score with a verdict line. Use this when an agent "
            "is choosing a dependency, a repo to contribute to, or a project "
            "to trust, and needs the quick read before the deep one."
        ),
        "tags": ["github", "repos", "data", "agents"],
        "input_schema": _obj({"repo": _S}, ["repo"]),
        "input_example": {"repo": "QuincyGB/token-verdict"},
        "output_schema": _obj(
            {
                "repo": _S,
                "url": _N,
                "description": _N,
                "stars": _I,
                "forks": _I,
                "open_issues": _I,
                "license": _N,
                "pushed_at": _N,
                "created_at": _N,
                "archived": _B,
                "health_score": _I,
                "health_notes": {"type": "array", "items": _S},
                "verdict": _S,
            },
            ["repo", "stars", "forks", "open_issues", "archived", "health_score", "verdict"],
        ),
        "output_example": {
            "repo": "QuincyGB/token-verdict",
            "url": "https://github.com/QuincyGB/token-verdict",
            "description": "x402 token-risk verdict service",
            "stars": 3,
            "forks": 0,
            "open_issues": 1,
            "license": "MIT",
            "pushed_at": "2026-10-21T19:11:00Z",
            "created_at": "2026-10-07T00:00:00Z",
            "archived": False,
            "health_score": 45,
            "health_notes": ["Very few stars (<10)."],
            "verdict": "Active-looking repository with a modest footprint.",
        },
    },
    "/v1/price-check": {
        "description": (
            "Pricing comps for an x402 listing, read live from the CDP Bazaar "
            "public catalog: how many resources match your keywords, their "
            "price distribution, the closest comparables, and a recommended "
            "listing price with its rationale. Use this when you operate an "
            "x402 route and are setting or changing its price."
        ),
        "tags": ["x402", "bazaar", "pricing", "sellers", "agents"],
        "input_schema": _obj({"keywords": _S, "description": _S}, ["keywords"]),
        "input_example": {"keywords": "token risk verdict"},
        "output_schema": _obj(
            {
                "keywords": _S,
                "catalog_status": _S,
                "matches_found": _I,
                "price_distribution": {
                    "type": ["object", "null"],
                    "properties": {
                        "min_usd": {"type": "number"},
                        "median_usd": {"type": "number"},
                        "p90_usd": {"type": "number"},
                        "max_usd": {"type": "number"},
                    },
                },
                "comparables": {
                    "type": "array",
                    "items": _obj(
                        {"resource": _N, "description": _S, "price_units": _I, "price_usd": {"type": "number"}},
                        ["description", "price_units", "price_usd"],
                    ),
                },
                "recommended_price_usd": {"type": ["number", "null"]},
                "rationale": _S,
            },
            ["keywords", "catalog_status", "matches_found", "comparables", "rationale"],
        ),
        "output_example": {
            "keywords": "token risk verdict",
            "catalog_status": "ok",
            "matches_found": 3,
            "price_distribution": {"min_usd": 0.01, "median_usd": 0.02, "p90_usd": 0.05, "max_usd": 0.05},
            "comparables": [
                {"resource": "https://example.com/v1/verdict", "description": "Token verdict", "price_units": 20000, "price_usd": 0.02}
            ],
            "recommended_price_usd": 0.02,
            "rationale": "Median of 3 cataloged match(es).",
        },
    },
    "/v1/card-smith": {
        "description": (
            "Generate or grade an agent card (the /.well-known/agent-card.json "
            "an agent publishes for A2A and AgentWorld-style registries). "
            "Generate builds a structurally valid card from your fields; grade "
            "scores a supplied card and itemizes its defects. Use this when "
            "you are registering an agent and want the card right the first "
            "time."
        ),
        "tags": ["agents", "a2a", "agent-card", "identity", "sellers"],
        "input_schema": _obj(
            {
                "mode": {"type": "string", "enum": ["generate", "grade"]},
                "name": _S,
                "description": _S,
                "skills": {"type": "array"},
                "card": {"type": "object"},
            },
            ["mode"],
        ),
        "input_example": {"mode": "generate", "name": "Ren", "description": "Research agent", "skills": ["research"]},
        "output_schema": _obj(
            {
                "mode": _S,
                "card": {"type": ["object", "null"]},
                "validation": _obj(
                    {
                        "valid_structure": _B,
                        "score": _I,
                        "defects": {
                            "type": "array",
                            "items": _obj(
                                {"name": _S, "severity": _S, "detail": _S},
                                ["name", "severity", "detail"],
                            ),
                        },
                    },
                    ["valid_structure", "score", "defects"],
                ),
            },
            ["mode", "validation"],
        ),
        "output_example": {
            "mode": "generate",
            "card": {"name": "Ren", "description": "Research agent", "url": "", "version": "1.0.0", "skills": []},
            "validation": {"valid_structure": False, "score": 55, "defects": [{"name": "missing url", "severity": "major", "detail": "Top-level 'url' is absent or empty."}]},
        },
    },
    "/v1/job-scout": {
        "description": (
            "A digest of currently open agent jobs from the marketplaces we "
            "sweep daily, filtered by the capabilities you name and sorted by "
            "budget: platform, budget, capability tags, deadline, and source "
            "snapshot time on every row, with a stale flag when the snapshot "
            "ages past 24 hours. Use this when your agent is looking for "
            "paid work."
        ),
        "tags": ["jobs", "bounties", "agents", "work", "digest"],
        "input_schema": _obj(
            {"capabilities": {"type": "array", "items": _S}, "limit": _I},
            [],
        ),
        "input_example": {"capabilities": ["research"], "limit": 5},
        "output_schema": _obj(
            {
                "snapshot_fetched_at": _N,
                "snapshot_source": _N,
                "data_stale": _B,
                "snapshot_age_hours": {"type": ["number", "null"]},
                "count": _I,
                "jobs": {
                    "type": "array",
                    "items": _obj(
                        {
                            "title": _S,
                            "platform": _S,
                            "budget_usd": {"type": ["number", "null"]},
                            "capabilities": {"type": "array", "items": _S},
                            "url": _S,
                        },
                        ["title", "platform", "url"],
                    ),
                },
                "note": _S,
            },
            ["data_stale", "count", "jobs", "note"],
        ),
        "output_example": {
            "snapshot_fetched_at": "2026-10-21T19:49:00Z",
            "snapshot_source": "Ren's Synmerco sweep",
            "data_stale": False,
            "snapshot_age_hours": 2.0,
            "count": 1,
            "jobs": [
                {
                    "title": "FDIC bank health snapshot, 10 Florida banks",
                    "platform": "Synmerco",
                    "budget_usd": 12.0,
                    "capabilities": ["research", "data-analysis"],
                    "url": "https://synmerco.com",
                    "fetched_at": "2026-10-21T19:49:00Z",
                    "expires_at": "2026-10-23T16:15:00Z",
                    "status": "open",
                }
            ],
            "note": "Snapshot data from our own platform sweeps.",
        },
    },
    "/v1/recheck": {
        "description": (
            "Change detection for anything with a URL, built for monitoring "
            "loops: probe one x402 resource or web page WITHOUT paying it "
            "and answer BASELINE, UNCHANGED, or CHANGED with a field-by-field "
            "diff (price, asset, payTo, schema, page body) against the last "
            "check for that watch. Use this when an agent runs on a schedule "
            "and needs to know what changed before spending money or "
            "attention. Watch and pack state persist across redeploys on "
            "the service volume. Packs: $1 for 250 checks on one endpoint."
        ),
        "tags": ["monitoring", "x402", "change-detection", "diff", "agents"],
        "input_schema": _obj({"url": _S, "watch_id": _S, "pack_id": _S}, ["url"]),
        "input_example": {
            "url": "https://token-verdict-production.up.railway.app/v1/verdict",
            "watch_id": "token-verdict-main",
        },
        "output_schema": _obj(
            {
                "watch_id": _S,
                "url": _S,
                "kind": {"type": "string", "enum": ["x402", "web"]},
                "status": {
                    "type": "string",
                    "enum": ["BASELINE", "UNCHANGED", "CHANGED"],
                },
                "changes": {
                    "type": "array",
                    "items": _obj(
                        {
                            "field": _S,
                            "before": {"type": ["string", "integer", "null"]},
                            "after": {"type": ["string", "integer", "null"]},
                        },
                        ["field", "before", "after"],
                    ),
                },
                "current": {"type": "object"},
                "checked_at": _S,
                "summary": _S,
            },
            ["watch_id", "url", "kind", "status", "changes", "current", "checked_at", "summary"],
        ),
        "output_example": {
            "watch_id": "token-verdict-main",
            "url": "https://token-verdict-production.up.railway.app/v1/verdict",
            "kind": "x402",
            "status": "CHANGED",
            "changes": [{"field": "amount", "before": "20000", "after": "25000"}],
            "current": {
                "status_code": 402,
                "amount": "25000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "network": "eip155:8453",
                "payTo": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F",
                "description_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "schema_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
            "checked_at": "2026-10-24T16:00:00Z",
            "summary": "Changed since the previous check: amount.",
        },
    },
    "/v1/recheck/pack": {
        "description": (
            "Recheck watch pack: one $1.00 payment buys 250 "
            "change-checks for ONE endpoint ($0.004 per check instead "
            "of $0.005), good for 90 days. Pack credits persist across "
            "redeploys on the service volume. Returns an unguessable "
            "pack id — spend credits via POST /v1/recheck (body pack_id "
            "or X-Recheck-Pack header), and read the balance free at "
            "POST /v1/recheck/pack-balance. Use this when a monitoring "
            "loop checks the same endpoint on a schedule and wants one "
            "payment instead of hundreds."
        ),
        "tags": ["monitoring", "x402", "change-detection", "prepaid", "agents"],
        "input_schema": _obj({"url": _S}, ["url"]),
        "input_example": {
            "url": "https://token-verdict-production.up.railway.app/v1/verdict",
        },
        # Generated from a real local run of the buy handler (payment
        # identity stubbed with a fixed authorization nonce, the way
        # tests/test_recheck_pack.py drives it) — pinned ≤1,450 bytes
        # serialized by test_discovery.py (Settled sample-size cap).
        "output_schema": _obj(
            {
                "pack_id": _S,
                "url": _S,
                "credits_total": _I,
                "credits_remaining": _I,
                "created_at": _S,
                "expires_at": _S,
                "expired": _B,
                "expiry_days": _I,
                "price_units": _I,
                "note": _S,
            },
            ["pack_id", "url", "credits_total", "credits_remaining", "expires_at"],
        ),
        "output_example": {
            "pack_id": "pack_9c73fa7ea40a6411b6517bd8cb3465a6",
            "url": "https://token-verdict-production.up.railway.app/v1/verdict",
            "credits_total": 250,
            "credits_remaining": 250,
            "created_at": "2026-10-25T05:36:48Z",
            "expires_at": "2027-01-23T05:36:48Z",
            "expired": False,
            "expiry_days": 90,
            "price_units": 1000000,
            "note": "Spend credits with POST /v1/recheck: send this pack_id in the body or the X-Recheck-Pack header. Read the balance free at POST /v1/recheck/pack-balance.",
        },
    },
}

for _path, _r in ROUTES.items():
    assert len(_r["description"]) < 500, f"{_path} description too long: {len(_r['description'])}"
    assert "Use this when" in _r["description"] or "Use this when" in _r["description"], _path


def route_extension(path: str) -> dict:
    """Canonical v2 Bazaar discovery extension for one route (same shape
    the sibling services ship — `info` carries values, `schema` is the
    JSON Schema OF the info object)."""
    r = ROUTES[path]
    input_schema = r["input_schema"]
    output_schema = r["output_schema"]
    return {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": r["input_example"],
            },
            "output": {
                "type": "json",
                "example": r["output_example"],
                "schema": output_schema,
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": input_schema,
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": output_schema,
                        "schema": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


def _price_str(path: str, settings: Settings) -> str:
    return settings.prices.get(path) or units_to_price(PRICE_UNITS[path])


def x402_manifest(settings: Settings, base_url: str) -> dict:
    def accepts(path: str) -> list[dict]:
        return [
            {
                "scheme": "exact",
                "network": settings.network,
                "amount": str(PRICE_UNITS[path]),
                "asset": USDC_ASSET,
                "payTo": settings.pay_to,
                "maxTimeoutSeconds": 300,
            }
        ]

    resources = []
    for path, r in ROUTES.items():
        resources.append(
            {
                "resource": f"{base_url}{path}",
                "method": "POST",
                "price": _price_str(path, settings),
                "scheme": "exact",
                "mimeType": "application/json",
                "description": r["description"],
                "tags": r["tags"],
                "accepts": accepts(path),
                "inputSchema": r["input_schema"],
                "outputSchema": r["output_schema"],
            }
        )
    resources.append(
        {
            "resource": f"{base_url}/quote",
            "method": "GET",
            "price": "$0.00",
            "description": "Free price quote for every route on the row",
        }
    )
    return {
        "x402Version": 2,
        "service": settings.service_name,
        "description": (
            "A row of small x402 stalls in one service: Base chain "
            "reads (wallet snapshot, allowance audit, transaction decoder, "
            "gas oracle), a wallet trust verdict, fetch tools (YouTube "
            "search, page-to-Markdown, GitHub repo vitals), seller tools "
            "(Bazaar price-check, agent-card smith), a digest of open "
            "agent jobs, and scheduled change detection for x402 "
            "resources and web pages, with prepaid check packs. Use this when an agent needs one cheap, honest "
            "fact mid-task and cannot run the source itself."
        ),
        "network": settings.network,
        "payTo": settings.pay_to,
        "resources": resources,
    }


def skill_md(settings: Settings, base_url: str) -> str:
    lines = [
        f"# {settings.service_name} — a row of x402 micro-services",
        "",
        f"{len(ROUTES)} paid endpoints in one deployment. No API key; payment is "
        "the authentication (x402 v2, exact scheme, USDC on Base). Every "
        "route states its data sources and marks fields it cannot supply "
        "as unknown — nothing is fabricated, and an upstream failure "
        "returns 502 so the payment does not settle.",
        "",
        "Recheck packs: buy POST /v1/recheck/pack once ($1.00 = 250 "
        "checks on one endpoint URL, 90-day expiry), then call POST "
        "/v1/recheck with the pack id in the body (pack_id) or the "
        "X-Recheck-Pack header to spend credits instead of paying per "
        "call. Check a pack's balance free at POST "
        "/v1/recheck/pack-balance.",
        "",
        "## Paid endpoints",
        "",
    ]
    for path, r in ROUTES.items():
        lines.append(f"### `POST {base_url}{path}` — {_price_str(path, settings)} USDC")
        lines.append("")
        lines.append(r["description"])
        lines.append("")
        lines.append(f"Input example: `{r['input_example']}`")
        lines.append("")
    lines += [
        "## Free endpoints",
        "",
        f"- `GET {base_url}/quote` — every route's price, network, payTo",
        f"- `GET {base_url}/health` — liveness",
        f"- `GET {base_url}/.well-known/x402` — machine-readable manifest",
        f"- `POST {base_url}/v1/recheck/pack-balance` — a pack's credits + expiry (pack id required)",
    ]
    return "\n".join(lines) + "\n"


def llms_txt(settings: Settings, base_url: str) -> str:
    lines = [
        f"# {settings.service_name}",
        "",
        f"A row of {len(ROUTES)} paid x402 micro-services. Network {settings.network}, x402 v2.",
        "",
        "## Endpoints",
        "",
    ]
    for path in ROUTES:
        lines.append(f"- POST {base_url}{path} — {_price_str(path, settings)} USDC")
    lines += [
        f"- GET {base_url}/quote — free price quote",
        f"- GET {base_url}/health — free liveness check",
        f"- GET {base_url}/.well-known/x402 — manifest",
        f"- POST {base_url}/v1/recheck/pack-balance — free pack balance (pack id required)",
    ]
    return "\n".join(lines) + "\n"
