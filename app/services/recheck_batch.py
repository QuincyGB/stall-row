"""Survey Batch Recheck: many Recheck targets in one call.

POST /v1/recheck/batch runs services/recheck.py's recheck() once
per unique URL in the submitted list, sequentially, under one shared
watch id (``batch:<batch_id>``) so every completed check lands in the
Recheck feed/history exactly like a single check.

Funding is Fleet-Pass ONLY — Recheck packs are bound to a single URL
and cannot fund a survey. There is no per-call x402 price for this
route and no silent fallback: a batch with no Fleet Pass credential
(body ``fleet_pass_id`` or the X-Fleet-Pass header) is refused, and
an unknown / expired / under-funded pass is refused before any
check runs, with nothing debited.

Pricing: each check costs the single-Recheck Fleet Pass price,
price_for("stall-row", "/v1/recheck") (5000 units, $0.005). A batch
of exactly MAX_BATCH_URLS (100) unique URLs gets a 20% batch
discount — 4000 units per check, 400_000 total ($0.40 per 100).
Fewer than 100 unique URLs pay the full per-check price. Exact
duplicate URLs are deduplicated before pricing: a URL is charged
once, checked once, and the dedupe is reported in the response.

Reserve-then-run: the total is debited ONCE, atomically, via
store.debit_pass under the route name "/v1/recheck/batch" BEFORE
any check runs. debit_pass takes the price from its caller (the
fleetpass price table fixes single-route prices; this module
computes the batch total from that table's /v1/recheck price and
the discount rule above — no existing price is read differently or
changed). A per-URL failure (SSRF refusal, unreachable target,
upstream error) becomes that URL's error row in the results and
does NOT fail the batch and is NOT refunded — an unreachable target
is a delivered report, mirroring single-Recheck semantics. If OUR
machinery raises outside a per-URL check, the batch aborts and the
debit is refunded via store.refund_debit; refund_debit only
supports whole-debit refunds, so a machinery failure refunds the
whole batch debit (the batch as a whole did not deliver) rather
than a partial remainder — no partial-refund primitive exists in
the store, and none is faked here.
"""

from __future__ import annotations

import secrets

import httpx

from ..rpc import UpstreamError
from . import fleetpass as fleetpass_svc
from . import recheck as recheck_svc

MAX_BATCH_URLS = 100
BATCH_DISCOUNT_NUM = 80  # a full batch pays 80% of the per-check price
BATCH_DISCOUNT_DEN = 100

FUNDING_RULE = (
    "Funding is Fleet-Pass ONLY — Recheck packs are bound to a "
    "single URL and cannot fund a survey"
)


class BatchError(Exception):
    """A batch refusal decided before any debit or check: the
    handler maps it to `http_status`. Nothing is charged and no
    check runs on any BatchError path."""

    def __init__(self, message: str, http_status: int = 422, code: str = "batch_refused"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


def batch_pricing(unique_count: int) -> tuple[int, int, bool]:
    """(per_check_units, total_units, discount_applied) for a batch
    of `unique_count` unique URLs, from the single-Recheck price."""
    base = fleetpass_svc.price_for("stall-row", "/v1/recheck")
    assert base is not None  # the single-Recheck route is priced in the table
    discount = unique_count == MAX_BATCH_URLS
    per_check = base * BATCH_DISCOUNT_NUM // BATCH_DISCOUNT_DEN if discount else base
    return per_check, per_check * unique_count, discount


def _error_row(watch_id: str, url: str, error: Exception) -> dict:
    """One URL's delivered failure report, in the Recheck response
    shape: the check was attempted, the target could not be
    fingerprinted, and the reason is stated plainly."""
    message = str(error)
    return {
        "watch_id": watch_id,
        "url": url,
        "kind": None,
        "status": "ERROR",
        "error": message,
        "changes": [],
        "field_changes": [],
        "current": None,
        "checked_at": recheck_svc._now_iso(),
        "summary": f"Check failed: {message}",
    }


async def run_batch(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    urls: list[str],
    fleet_pass_id: str | None,
    index_store=None,
) -> dict:
    """Run one Fleet-Pass-funded survey batch. Raises BatchError
    for pre-funding refusals (empty / oversized / no credential)
    and recheck.PackError for pass refusals (unknown 404 / expired
    410 / insufficient 402) — on both, nothing is debited and no
    check runs."""
    # Dedupe exact duplicates (after the same strip recheck()
    # applies), preserving input order: charged once, checked once.
    unique: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        url = str(raw).strip()
        if url not in seen:
            seen.add(url)
            unique.append(url)
    requested = len(urls or [])
    if not unique or unique == [""]:
        raise BatchError("urls must name at least one resource URL", 422, "empty_batch")
    if len(unique) > MAX_BATCH_URLS:
        raise BatchError(
            f"at most {MAX_BATCH_URLS} unique URLs per batch (got {len(unique)})",
            422,
            "batch_oversized",
        )
    pass_id = (fleet_pass_id or "").strip()
    if not pass_id:
        raise BatchError(
            f"{FUNDING_RULE}: send fleet_pass_id in the body or the "
            "X-Fleet-Pass header",
            402,
            "fleet_pass_required",
        )

    per_check, total, discount = batch_pricing(len(unique))

    # Reserve-then-run: ONE atomic debit of the computed total
    # before any check runs. debit_pass raises PackError (unknown
    # 404 / expired 410 / insufficient 402) debiting nothing.
    debit = store.debit_pass(pass_id, "stall-row", "/v1/recheck/batch", total)

    batch_id = "batch_" + secrets.token_hex(8)
    watch_id = f"batch:{batch_id}"
    results: list[dict] = []
    try:
        for url in unique:
            try:
                results.append(
                    await recheck_svc.recheck(
                        client, store, url, watch_id, index_store=index_store
                    )
                )
            except (ValueError, UpstreamError) as e:
                # A delivered failure report for this URL: the batch
                # continues and this check is not refunded.
                results.append(_error_row(watch_id, url, e))
    except Exception:
        # Our machinery failed outside a per-URL check: refund the
        # debit whole (refund_debit has no partial form) and let the
        # handler answer 502 — never charge for our failure.
        store.refund_debit(debit["debit_id"])
        raise

    pass_after = store.get_pass(pass_id)
    return {
        "batch_id": batch_id,
        "requested": requested,
        "unique": len(unique),
        "duplicates_removed": requested - len(unique),
        "per_check_units": per_check,
        "discount_applied": discount,
        "charged_units": total,
        "pass_balance_units": (
            pass_after["balance_units"] if pass_after is not None else None
        ),
        "results": results,
    }
