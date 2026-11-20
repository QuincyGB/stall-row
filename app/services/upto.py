"""`upto` scheme support for variable-cost routes (W3 Build 2,
2026-11-19) — feature-flagged, DEFAULT OFF.

With `exact`, the buyer pays the listed price whether the call was
cheap or expensive to serve. With `upto`, the buyer authorises a CAP
(the listed price) and settlement charges the ACTUAL cost, computed
from what was actually delivered, up to that cap — the model CDP,
the Cloudflare Monetization Gateway (API2PDF's variable-cost PDF
generation is the reference case) and AgentCore support for
decode/audit-type calls whose cost varies with input size.

Scope: the two variable-cost routes on this row —

  * POST /v1/page-to-markdown ($0.01 cap): actual = $0.002 base +
    $0.000001 per delivered word (word_count), capped at the cap.
  * POST /v1/tx-decoder ($0.002 cap): actual = $0.0005 base +
    $0.00025 per ERC-20 transfer decoded, capped at the cap.

Every other route keeps its single `exact` accept, unchanged.

SDK / facilitator gap (verified 2026-11-19, the reason for the flag):
the installed x402 SDK (2.25.0, pinned in requirements.txt) CAN
express upto — `x402.mechanisms.evm.upto.server.UptoEvmScheme`
(Permit2, facilitator-bound witness) plus settlement overrides
(`x402.http.middleware.fastapi.set_settlement_overrides`, amount in
atomic units) are both present. What the SDK cannot do alone is
settle: `UptoEvmScheme.enhance_payment_requirements` REQUIRES the
facilitator's /supported document to advertise an `upto` kind for
the network WITH `extra.facilitatorAddress`, and raises ValueError
at challenge-build time without it. Measured live on 2026-11-19:

  * default facilitator (facilitator.ultravioletadao.xyz): DOES
    advertise upto v2 on eip155:8453 — but with NO extra at all, so
    no facilitatorAddress. CONFIRMED by enabling the flag locally
    against it (2026-11-19): startup and non-upto routes are
    unaffected, but both upto routes' 402 challenges answer 500.
  * PayAI (facilitator.payai.network): no upto kinds at all.
  * CDP: documents exact/upto/batch-settlement on EVM (per the
    2026-11-19 ecosystem report) — the plausible first live rail,
    but this deployment's CDP configuration was not re-probed for
    its upto kind extra in this build.

So the wiring is complete but dormant: setting
STALLROW_UPTO_ENABLED=1 registers UptoEvmScheme and adds the upto
PaymentOption (cap = listed price) beside `exact` on the two routes;
handlers for upto-paid calls attach the settlement override with the
computed actual. Enable it ONLY against a facilitator whose
/supported advertises upto + facilitatorAddress for eip155:8453
(CDP expected), and smoke-test one challenge before announcing it.
With the flag off (production default) nothing in this module is
consulted and challenge shapes are byte-identical to before.
"""

from __future__ import annotations

import os

from fastapi.responses import JSONResponse
from x402.http import PaymentOption
from x402.http.middleware.fastapi import set_settlement_overrides

UPTO_ROUTES = frozenset({"/v1/page-to-markdown", "/v1/tx-decoder"})

# Cost model constants (USDC micro-units). Actuals are computed from
# the DELIVERED result only, never from the request — a failed call
# settles nothing at all (the middleware never settles >= 400), and
# a delivered call can never settle above its route's cap.
PAGE_BASE_UNITS = 2_000  # $0.002
PAGE_PER_WORD_UNITS = 1  # $0.000001 per delivered word
TX_BASE_UNITS = 500  # $0.0005
TX_PER_TRANSFER_UNITS = 250  # $0.00025 per decoded ERC-20 transfer


def upto_enabled() -> bool:
    return os.environ.get("STALLROW_UPTO_ENABLED", "").strip() == "1"


def page_actual_units(result: dict, cap_units: int) -> int:
    words = result.get("word_count")
    if not isinstance(words, int) or words < 0:
        # Fall back to the delivered markdown's own length signal.
        markdown = result.get("markdown")
        words = len(markdown.split()) if isinstance(markdown, str) else 0
    return min(cap_units, PAGE_BASE_UNITS + words * PAGE_PER_WORD_UNITS)


def tx_actual_units(result: dict, cap_units: int) -> int:
    transfers = result.get("erc20_transfers")
    count = len(transfers) if isinstance(transfers, list) else 0
    return min(cap_units, TX_BASE_UNITS + count * TX_PER_TRANSFER_UNITS)


_ACTUAL_FOR = {
    "/v1/page-to-markdown": page_actual_units,
    "/v1/tx-decoder": tx_actual_units,
}


def actual_units_for(route: str, result: dict, cap_units: int) -> int | None:
    fn = _ACTUAL_FOR.get(route)
    return fn(result, cap_units) if fn is not None else None


def upto_payment_option(*, pay_to: str, price: str, network: str) -> PaymentOption:
    """The upto accept entry for one route: same payTo, same network,
    price = the CAP (the route's listed price — the buyer authorises
    up to this and never a unit more)."""
    return PaymentOption(scheme="upto", pay_to=pay_to, price=price, network=network)


def request_scheme(request) -> str | None:
    """The scheme of the verified payment behind this request, from
    the payload the x402 middleware stashed on request.state."""
    payload = getattr(request.state, "payment_payload", None)
    accepted = getattr(payload, "accepted", None)
    scheme = getattr(accepted, "scheme", None)
    return scheme if isinstance(scheme, str) else None


def apply_upto_settlement(request, route: str, result, cap_units: int):
    """When this call was paid under the `upto` scheme, wrap a dict
    result in a JSONResponse carrying the settlement override for the
    computed actual (atomic units, as the SDK expects). Any other
    funding path (exact, pack, pass, entitlement) and any non-dict
    result passes through untouched — this is a no-op unless an upto
    payment actually funded the call."""
    if not isinstance(result, dict) or "error" in result:
        return result
    if request_scheme(request) != "upto":
        return result
    actual = actual_units_for(route, result, cap_units)
    if actual is None:
        return result
    response = JSONResponse(result)
    set_settlement_overrides(response, {"amount": str(actual)})
    return response
