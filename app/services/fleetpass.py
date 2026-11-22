"""Fleet Pass: one prepaid USD balance that spends across all five
fleet services.

Sold here (POST /v1/fleet-pass, $5.00, 90-day expiry) because the
ledger already lives here — the same sqlite store that holds the
recheck packs (see recheck.py WatchStore). Sibling services present
the buyer's X-Fleet-Pass header to POST /v1/fleet-pass/debit; this
module's price table is the ONLY authority on what a route costs, so
a caller can never name its own price. Debits are reserve-then-run:
the debit lands atomically before the call runs, and the calling
service refunds by debit_id when its handler fails (fleet rule:
never charge for our failure). An exhausted/expired/unknown pass is
refused plainly (402/410/404) — there is NO silent fallback to a
per-call charge anywhere in the spend path; a client that wants
per-call pays per call explicitly, without the header.
"""

from __future__ import annotations

PASS_PRICE_UNITS = 5_000_000  # $5.00 — mirrors config.PRICE_UNITS
PASS_V2_PRICE_UNITS = 20_000_000  # $20.00 — the framework working pass (same ledger/TTL)
PASS_TTL_DAYS = 90
PASS_HEADER = "x-fleet-pass"

# (service, route) -> price in USDC micro-units. Mirrors each
# service's own config at build time (2026-10-26): token-verdict
# config.py, route-verdict config.py, list-smith config.py,
# lock-smith config.py, stall-row config.PRICE_UNITS. The pack-buy
# and pass-buy routes are deliberately absent — a pass cannot buy
# prepaid products, only spend on calls.
FLEET_ROUTES: dict[tuple[str, str], int] = {
    ("token-verdict", "/v1/verdict"): 20_000,
    ("token-verdict", "/v1/second-opinion"): 30_000,
    ("token-verdict", "/v1/pre-trade"): 30_000,
    ("token-verdict", "/v1/batch-verdict"): 100_000,
    ("route-verdict", "/v1/check"): 30_000,
    ("list-smith", "/v1/grade"): 30_000,
    ("list-smith", "/v1/fix"): 100_000,
    ("lock-smith", "/v1/audit"): 100_000,
    ("lock-smith", "/v1/approval-check"): 30_000,
    ("lock-smith", "/v1/contract-check"): 100_000,
    ("stall-row", "/v1/wallet-snapshot"): 5_000,
    ("stall-row", "/v1/allowance-audit"): 5_000,
    ("stall-row", "/v1/tx-decoder"): 2_000,
    ("stall-row", "/v1/gas-oracle"): 1_000,
    ("stall-row", "/v1/wallet-verdict"): 20_000,
    ("stall-row", "/v1/pre-trade-guard"): 5_000,
    ("stall-row", "/v1/tube-search"): 5_000,
    ("stall-row", "/v1/page-to-markdown"): 5_000,
    ("stall-row", "/v1/video-transcript"): 5_000,
    ("stall-row", "/v1/page-metadata"): 5_000,
    ("stall-row", "/v1/feed-digest"): 10_000,
    ("stall-row", "/v1/url-unshorten"): 5_000,
    ("stall-row", "/v1/repo-readme"): 5_000,
    ("stall-row", "/v1/repo-vitals"): 5_000,
    ("stall-row", "/v1/price-check"): 30_000,
    ("stall-row", "/v1/card-smith"): 30_000,
    ("stall-row", "/v1/job-scout"): 30_000,
    ("stall-row", "/v1/recheck"): 5_000,
    ("stall-row", "/v1/index/changes"): 30_000,
    ("stall-row", "/v1/index/history"): 10_000,
    ("stall-row", "/v1/orchestrate"): 100_000,
    ("stall-row", "/v1/counterparty-report"): 100_000,
    ("stall-row", "/v1/counterparty-report/deep"): 250_000,
    ("stall-row", "/v1/counterparty-watch"): 30_000,
    ("stall-row", "/v1/sanctions-check"): 30_000,
    ("stall-row", "/v1/wallet-ping"): 1_000,
    ("stall-row", "/v1/grade-my-stall"): 5_000,
    ("stall-row", "/v1/receipt-verify"): 2_000,
    ("stall-row", "/v1/flagged-paytos"): 5_000,
    ("stall-row", "/v1/agent-card-verify"): 5_000,
    ("stall-row", "/v1/fleet-check"): 30_000,
    ("stall-row", "/v1/sign-read"): 10_000,
    ("stall-row", "/v1/score-book"): 10_000,
    ("stall-row", "/v1/guard"): 10_000,
    ("stall-row", "/v1/ledger-dataset"): 30_000,
    ("stall-row", "/v1/ledger-dataset/full"): 100_000,
    ("stall-row", "/v1/physical"): 30_000,
    ("stall-row", "/v1/payto-pin/verify"): 10_000,
}


def price_for(service: str, route: str) -> int | None:
    return FLEET_ROUTES.get((service, route))
