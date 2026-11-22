"""Response upgrades for paid StallRow deliveries (W2 build):
every paid response carries two extra top-level blocks.

``next_check`` — one suggested follow-up route (name + path +
price_usd), machine-readable, so a buyer agent can chain the
natural next check without scraping docs. Prices mirror
config.PRICE_UNITS (this stall) at build time.

``evidence`` — {settlement_tx, input_hash, timestamp}:
  * input_hash — sha256 of the canonical request input (the parsed
    request body, JSON with sorted keys and compact separators).
  * timestamp — UTC delivery time (ISO 8601, Z suffix).
  * settlement_tx — NOT available at handler time: the x402
    middleware settles after the handler returns, so it is attached
    as None and the delivery layer (app/main.py's
    _attach_loop_offer, the central paid-delivery path) fills it
    from the settlement receipt header when one is present.
    Fleet-Pass / pack-funded deliveries carry no per-call receipt,
    so their settlement_tx stays None — the debit is their record.

Attached centrally in app/main.py's _attach_loop_offer, which every
paid funding path (per-call, recheck pack, toolkit pack,
entitlement, Fleet Pass) passes through. Error responses and the
unpaid 402 challenge never carry these blocks.

HEADER-BUDGET RULE (2026-11-22 pre-trade outage): these blocks are
attached to paid response BODIES only. They are deliberately NOT
added to discovery.py output schemas — the Bazaar extension embeds
the output schema in the base64 PAYMENT-REQUIRED header and an
oversized schema pushes the challenge past the Railway edge's
~16 KB header limit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# Follow-up suggestions per paid route (subset: the counterparty
# family + the new W2 routes + a few natural chains). Routes not
# listed still carry the evidence block, just no next_check.
NEXT_CHECKS: dict[str, dict] = {
    "/v1/counterparty-report": {
        "name": "counterparty-report-deep",
        "path": "/v1/counterparty-report/deep",
        "price_usd": 0.25,
    },
    "/v1/counterparty-report/deep": {
        "name": "counterparty-watch",
        "path": "/v1/counterparty-watch",
        "price_usd": 0.03,
    },
    "/v1/counterparty-watch": {
        "name": "counterparty-report-deep",
        "path": "/v1/counterparty-report/deep",
        "price_usd": 0.25,
    },
    "/v1/sanctions-check": {
        "name": "counterparty-report",
        "path": "/v1/counterparty-report",
        "price_usd": 0.10,
    },
    "/v1/wallet-ping": {
        "name": "wallet-snapshot",
        "path": "/v1/wallet-snapshot",
        "price_usd": 0.005,
    },
    "/v1/wallet-verdict": {
        "name": "counterparty-report",
        "path": "/v1/counterparty-report",
        "price_usd": 0.10,
    },
    "/v1/pre-trade-guard": {
        "name": "counterparty-report",
        "path": "/v1/counterparty-report",
        "price_usd": 0.10,
    },
    "/v1/gas-oracle": {
        "name": "wallet-ping",
        "path": "/v1/wallet-ping",
        "price_usd": 0.001,
    },
}


def canonical_input_hash(input_obj) -> str:
    """sha256 hex of the canonical JSON encoding of a request input."""
    canonical = json.dumps(input_obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_upgrades(result, route: str, input_obj, settlement_tx=None):
    """Add next_check + evidence to a successful paid delivery dict.
    Anything that is not a dict passes through untouched, so
    failures never carry the blocks."""
    if not isinstance(result, dict):
        return result
    next_check = NEXT_CHECKS.get(route)
    if next_check is not None:
        result["next_check"] = dict(next_check)
    result["evidence"] = {
        "settlement_tx": settlement_tx,
        "input_hash": canonical_input_hash(input_obj),
        "timestamp": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    return result
