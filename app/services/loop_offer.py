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

Next-20 #1 + #5 (2026-11-03): the block now rides EVERY successful
paid delivery on every priced StallRow route (attached once, at the
delivery layer in app/main.py's x402 middleware — per-call,
Recheck-pack, toolkit-pack and Fleet-Pass funding alike), and a
RETURNING payer's block is personalised: a ``returning_payer``
section carries their own recorded history with this stall (calls,
distinct days, total spent — see WatchStore.record_payer_delivery)
and an adaptive offer line priced off that proven spend, with deep
links that pre-bind the pack / webhook offers to their identity.
First-time payers (and payers whose identity the delivery does not
expose, e.g. Solana-payload settlements) get the static block.

Utility shelf (buyer analysis 2026-11-05): deliveries on the utility
routes (UTILITY_ROUTES below) additionally carry the Toolkit Pack
offer — the pack funds that shelf since the same date, so a utility
buyer is finally pitched a prepaid product that pays for what they
buy — and a returning utility payer's block gains an adaptive
``utility_offer`` next to the Recheck one.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

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


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _returning_payer_block(payer: dict, route: str | None) -> dict:
    """The personalised section for a returning payer. `payer` is a
    WatchStore payer-history snapshot (totals AFTER the current
    delivery was recorded); every figure in the adaptive offer line
    is derived from that snapshot or the enforcing constants."""
    base = CANONICAL_BASE
    identity = payer["payer_key"]
    pack_usd = _usd(recheck_svc.PACK_PRICE_UNITS)
    per_check_usd = pack_usd / recheck_svc.PACK_CREDITS
    single_usd = _usd(PRICE_UNITS["/v1/recheck"])
    route_calls = (
        payer.get("routes", {}).get(route, {}).get("calls", 0) if route else 0
    )
    total_spent_usd = _usd(payer["total_units"])
    line = (
        f"Returning payer: this identity has paid for "
        f"{payer['calls']} deliveries at this stall "
        f"({route_calls} on this route, "
        f"{payer['distinct_days']} distinct day(s), "
        f"${total_spent_usd:.3f} total) — the Recheck pack does "
        f"{recheck_svc.PACK_CREDITS} checks for ${pack_usd:.2f} "
        f"(${per_check_usd:.3f} a check instead of ${single_usd:.3f} "
        f"per call)."
    )
    return {
        "identity": identity,
        "identity_kind": (
            "evm_wallet" if identity.startswith("0x") else "funding_credential"
        ),
        "calls": payer["calls"],
        "calls_this_route": route_calls,
        "distinct_days": payer["distinct_days"],
        "total_spent_usd": total_spent_usd,
        "first_seen": _iso(payer["first_seen_epoch"]),
        "last_seen": _iso(payer["last_seen_epoch"]),
        "adaptive_offer": {
            "line": line,
            "offer_id": "recheck_pack",
            "route": "/v1/recheck/pack",
            "price_usd": pack_usd,
            "credits": recheck_svc.PACK_CREDITS,
            "per_check_usd": per_check_usd,
            "per_call_price_usd": single_usd,
            "deep_link": (
                f"{base}/v1/recheck/pack?payer={quote(identity, safe='')}"
            ),
            "webhook_deep_link": (
                f"{base}/v1/recheck/webhooks?payer={quote(identity, safe='')}"
            ),
        },
    }


# Utility-shelf routes (buyer analysis 2026-11-05): the deliveries a
# Tube Search / Page to Markdown / Video Details buyer reads. Their
# block gains the Toolkit Pack offer — since 2026-11-05 the pack
# funds this shelf, so for the first time the offer a utility buyer
# sees is a product that pays for what they were just buying. The
# batch routes carry it too: a batch buyer is the pack's natural
# customer even though the pack never funds the batch itself.
UTILITY_ROUTES = frozenset(
    {
        "/v1/tube-search",
        "/v1/page-to-markdown",
        "/v1/video-details",
        "/v1/tube-search/batch",
        "/v1/page-to-markdown/batch",
    }
)


def _toolkit_pack_offer() -> dict:
    base = CANONICAL_BASE
    pack_usd = _usd(recheck_svc.TOOLKIT_PACK_PRICE_UNITS)
    per_check_usd = pack_usd / recheck_svc.TOOLKIT_PACK_CREDITS
    return {
        "id": "toolkit_pack",
        "route": "/v1/toolkit/pack",
        "method": "POST",
        "price_usd": pack_usd,
        "what_it_is": (
            "125 prepaid calls spendable on Tube Search, Page to "
            "Markdown, Video Details, Sign Reader, Score Book, or "
            "Guard in any mix, bought once and spent via pack_id "
            "(body field or X-Toolkit-Pack header) instead of a "
            "per-call payment."
        ),
        "terms": {
            "credits": recheck_svc.TOOLKIT_PACK_CREDITS,
            "ttl_days": recheck_svc.TOOLKIT_PACK_TTL_DAYS,
            "per_check_usd": per_check_usd,
            "per_call_price_usd": _usd(PRICE_UNITS["/v1/tube-search"]),
            "routes": list(recheck_svc.TOOLKIT_PACK_ROUTES),
            "balance_route": "/v1/toolkit/pack-balance",
            "deep_link": f"{base}/v1/toolkit/pack",
        },
    }


def _utility_offer(payer: dict) -> dict:
    """The returning-payer adaptive form of the Toolkit Pack offer,
    attached when the delivery's route is on the utility shelf."""
    base = CANONICAL_BASE
    identity = payer["payer_key"]
    pack_usd = _usd(recheck_svc.TOOLKIT_PACK_PRICE_UNITS)
    per_check_usd = pack_usd / recheck_svc.TOOLKIT_PACK_CREDITS
    single_usd = _usd(PRICE_UNITS["/v1/tube-search"])
    line = (
        f"Returning payer: the Toolkit Pack does "
        f"{recheck_svc.TOOLKIT_PACK_CREDITS} Tube Search / Page to "
        f"Markdown / Video Details calls for ${pack_usd:.2f} "
        f"(${per_check_usd:.3f} a call instead of ${single_usd:.3f} "
        f"per call) — it funds the shelf this delivery came from."
    )
    return {
        "line": line,
        "offer_id": "toolkit_pack",
        "route": "/v1/toolkit/pack",
        "price_usd": pack_usd,
        "credits": recheck_svc.TOOLKIT_PACK_CREDITS,
        "per_check_usd": per_check_usd,
        "per_call_price_usd": single_usd,
        "deep_link": (
            f"{base}/v1/toolkit/pack?payer={quote(identity, safe='')}"
        ),
    }


def loop_offer(payer: dict | None = None, route: str | None = None) -> dict:
    """The loop block for a StallRow delivery. All repeat products
    named here live on StallRow itself. With a `payer` history
    snapshot (a returning payer), the block gains a personalised
    ``returning_payer`` section; without one it is the static
    Play 1 block, byte-for-byte the original shape."""
    base = CANONICAL_BASE
    single_units = PRICE_UNITS["/v1/recheck"]
    single_usd = _usd(single_units)
    pack_units = recheck_svc.PACK_PRICE_UNITS
    pack_usd = _usd(pack_units)
    per_check_usd = pack_usd / recheck_svc.PACK_CREDITS
    block = {
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
    if route in UTILITY_ROUTES:
        block["offers"].append(_toolkit_pack_offer())
    if payer is not None:
        block["returning_payer"] = _returning_payer_block(payer, route)
        if route in UTILITY_ROUTES:
            block["returning_payer"]["utility_offer"] = _utility_offer(payer)
    return block


def attach_loop_offer(result, payer: dict | None = None, route: str | None = None):
    """Add the loop block to a successful delivery dict. Anything
    that is not a dict (error JSONResponses) passes through
    untouched, so failures never carry the offer."""
    if isinstance(result, dict):
        result[LOOP_KEY] = loop_offer(payer=payer, route=route)
    return result
