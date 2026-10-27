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
    ("token-verdict", "/v1/pre-trade"): 50_000,
    ("token-verdict", "/v1/batch-verdict"): 200_000,
    ("route-verdict", "/v1/check"): 50_000,
    ("list-smith", "/v1/grade"): 100_000,
    ("list-smith", "/v1/fix"): 500_000,
    ("lock-smith", "/v1/audit"): 500_000,
    ("stall-row", "/v1/wallet-snapshot"): 5_000,
    ("stall-row", "/v1/allowance-audit"): 5_000,
    ("stall-row", "/v1/tx-decoder"): 2_000,
    ("stall-row", "/v1/gas-oracle"): 1_000,
    ("stall-row", "/v1/wallet-verdict"): 20_000,
    ("stall-row", "/v1/tube-search"): 10_000,
    ("stall-row", "/v1/page-to-markdown"): 10_000,
    ("stall-row", "/v1/repo-vitals"): 5_000,
    ("stall-row", "/v1/price-check"): 50_000,
    ("stall-row", "/v1/card-smith"): 100_000,
    ("stall-row", "/v1/job-scout"): 50_000,
    ("stall-row", "/v1/recheck"): 5_000,
    ("stall-row", "/v1/index/changes"): 100_000,
    ("stall-row", "/v1/orchestrate"): 750_000,
}


def price_for(service: str, route: str) -> int | None:
    return FLEET_ROUTES.get((service, route))
