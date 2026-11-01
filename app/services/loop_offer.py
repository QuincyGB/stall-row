"""Verdict Row loop offer: the machine-readable block attached to
paid Recheck deliveries (Play 1 of the organic-buyer plan,
2026-11-01).

The fleet's proven repeat payers are agent wallets that pay per call
for the same check again and again; nobody at a human inbox reads
our pitches, but every one of those wallets reads this payload. So
the delivery itself carries the repeat products, stated exactly as
the code enforces them — every number below is DERIVED from the
constants that enforce it (services/recheck.py PACK_*,
services/fleetpass.py PASS_*, config.PRICE_UNITS), never restated
by hand, so the offer cannot drift from the terms a buyer would
actually get.

Additive only: the HTTP layer adds one top-level
``verdict_row_loop`` field to a successful delivery dict. Error
responses and the unpaid 402 challenge never carry it.
"""

from __future__ import annotations

import os

from ..config import PRICE_UNITS
from . import fleetpass as fleetpass_svc
from . import recheck as recheck_svc

LOOP_KEY = "verdict_row_loop"

# This stall's canonical public origin — the same URL its committed
# discovery/status files publish. Deliberately NOT derived from the
# incoming request: a paid delivery's request has passed through
# payment-middleware dispatch, and its base URL is not guaranteed to
# be the public origin (observed live 2026-11-01: a settled
# delivery's request base arrived as "testserver"). An env override
# exists for staging, mirroring the fleet's other URL knobs.
CANONICAL_BASE = os.environ.get(
    "STALLROW_PUBLIC_BASE", "https://stall-row-production.up.railway.app"
).rstrip("/")


def _usd(units: int) -> float:
    return units / 1_000_000


def loop_offer() -> dict:
    """The loop block for a StallRow delivery. All repeat products
    named here live on StallRow itself."""
    base = CANONICAL_BASE
    single_units = PRICE_UNITS["/v1/recheck"]
    single_usd = _usd(single_units)
    pack_units = recheck_svc.PACK_PRICE_UNITS
    pack_usd = _usd(pack_units)
    per_check_usd = pack_usd / recheck_svc.PACK_CREDITS
    return {
        "about": (
            "Paying per call for the same check on a loop? The Recheck "
            "pack does 250 checks of one endpoint for $1.00, and the "
            "change webhook tells you when the answer changes — so "
            "you only pay when it matters."
        ),
        "discovery": f"{base}/.well-known/verdict-row.json",
        "offers": [
            {
                "id": "recheck_pack",
                "route": "/v1/recheck/pack",
                "method": "POST",
                "price_usd": pack_usd,
                "what_it_is": (
                    "250 Recheck change-checks against ONE endpoint "
                    "URL, bought once and spent via POST /v1/recheck "
                    "with the pack_id (body field or X-Recheck-Pack "
                    "header) instead of a per-call payment."
                ),
                "terms": {
                    "credits": recheck_svc.PACK_CREDITS,
                    "ttl_days": recheck_svc.PACK_TTL_DAYS,
                    "per_check_usd": per_check_usd,
                    "per_call_price_usd": single_usd,
                    "bound_to": "one endpoint URL, fixed at purchase",
                    "balance_route": "/v1/recheck/pack-balance",
                },
            },
            {
                "id": "recheck_change_webhook",
                "route": "/v1/recheck/webhooks",
                "method": "POST",
                "price_usd": 0,
                "what_it_is": (
                    "Register a callback for Recheck CHANGED events "
                    "(feed-wide, one resource URL, or one origin) and "
                    "get HMAC-SHA256-signed deliveries when a watched "
                    "resource changes, instead of polling with paid "
                    "checks."
                ),
                "terms": {
                    "gate": (
                        "exactly one funding credential: a live "
                        "Recheck pack_id, or a fleet_pass_id with "
                        f"balance for at least one Recheck check "
                        f"(${single_usd:.3f}); a pack-funded "
                        "subscription must filter to exactly the "
                        "pack's URL"
                    ),
                    "registration_debited": False,
                    "deliveries_debited": False,
                },
            },
            {
                "id": "fleet_pass",
                "route": "/v1/fleet-pass",
                "method": "POST",
                "price_usd": _usd(fleetpass_svc.PASS_PRICE_UNITS),
                "what_it_is": (
                    "A prepaid USDC balance that spends across all "
                    "five Verdict Row stalls at their listed per-route "
                    "prices — present the pass id in the X-Fleet-Pass "
                    "header and calls debit the balance instead of "
                    "settling per call."
                ),
                "terms": {
                    "ttl_days": fleetpass_svc.PASS_TTL_DAYS,
                    "balance_route": "/v1/fleet-pass/balance",
                },
            },
            {
                "id": "fleet_pass_20",
                "route": "/v1/fleet-pass-20",
                "method": "POST",
                "price_usd": _usd(fleetpass_svc.PASS_V2_PRICE_UNITS),
                "what_it_is": (
                    "The $20 Fleet Pass: the same prepaid balance "
                    "ledger and terms as /v1/fleet-pass, sized as a "
                    "framework working balance."
                ),
                "terms": {
                    "ttl_days": fleetpass_svc.PASS_TTL_DAYS,
                    "balance_route": "/v1/fleet-pass/balance",
                },
            },
            {
                "id": "recheck_feed",
                "route": "/v1/recheck/feed",
                "method": "GET",
                "price_usd": 0,
                "what_it_is": (
                    "Free public JSONL feed of recent Recheck "
                    "results — poll it instead of re-paying for "
                    "checks already run."
                ),
                "terms": {},
            },
        ],
    }


def attach_loop_offer(result):
    """Add the loop block to a successful delivery dict. Anything
    that is not a dict (error JSONResponses) passes through
    untouched, so failures never carry the offer."""
    if isinstance(result, dict):
        result[LOOP_KEY] = loop_offer()
    return result
