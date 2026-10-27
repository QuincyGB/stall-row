"""Router Watchlist: scheduled drift delivery for the top-N catalog.

Why this is not a recheck pack: StallRow's pack machinery binds one
pack to ONE endpoint URL (the pack's credits spend against a single
resource's drift). A watchlist is the opposite shape — the top-N
services of the whole catalog by 30d calls, recomputed from each
new Verdict Index snapshot. So the product is built the way
Rotation Alerts are: a free registration on the watch store,
evaluated once per new snapshot by the ticker, funded per run from
the buyer's Fleet Pass at the same $0.005 run price.

Each evaluation diffs the two newest snapshots restricted to the
watchlist (the top-N of either side): resources that ENTERED the
top-N, resources that LEFT it, and — for members present in both —
payTo rotations and price changes. The diff is pushed to the
buyer's webhook as JSON; no events, no push. The first evaluation
is a free silent baseline (watermark only); a run that cannot be
funded marks the registration funding_failed without advancing the
watermark. top_n defaults to 100 and is capped at 500.

Cadence honesty: snapshots land about every 6 hours (~4/day), so a
funded registration spends roughly $0.02/day while snapshots keep
arriving. Email is stored only (no mail transport); the webhook is
the live delivery channel. SSRF discipline matches the rest of the
watch family: checked at registration and again at delivery;
delivery never follows redirects.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import fetch as fetch_svc
from . import recheck as recheck_svc
from .indexfeed import IndexStore

log = logging.getLogger("stall-row.watchlist")

RUN_PRICE_UNITS = 5_000
RUN_ROUTE_LABEL = "/v1/watchlist"
DEFAULT_TOP_N = 100
MAX_TOP_N = 500
EVENT_NAME = "watchlist_diff"


class WatchlistError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise WatchlistError(str(e)) from e


async def register(
    store: recheck_svc.WatchStore,
    top_n: int | None,
    webhook_url: str | None,
    email: str | None,
    fleet_pass_id: str | None,
) -> dict:
    """Validate everything BEFORE storing: top_n in range, the
    webhook a public URL, and the Fleet Pass able to fund one run.
    Raises WatchlistError with the refusal; nothing is stored."""
    top_n = DEFAULT_TOP_N if top_n is None else int(top_n)
    if top_n < 1 or top_n > MAX_TOP_N:
        raise WatchlistError(f"top_n must be between 1 and {MAX_TOP_N}")
    if webhook_url:
        webhook_url = webhook_url.strip()
        await _ssrf(webhook_url)
    if not webhook_url and not email:
        raise WatchlistError("a webhook_url or an email is required")
    if not fleet_pass_id:
        raise WatchlistError("fund runs with a fleet_pass_id")
    fleet_pass = store.get_pass(fleet_pass_id.strip())
    if fleet_pass is None:
        raise WatchlistError("unknown fleet_pass_id", 404)
    if fleet_pass["expired"]:
        raise WatchlistError("this fleet pass has expired", 410)
    if fleet_pass["balance_units"] < RUN_PRICE_UNITS:
        raise WatchlistError("fleet pass balance cannot fund one run", 402)
    reg = store.register_watchlist(
        top_n, webhook_url, email, fleet_pass_id.strip()
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["run_price_units"] = RUN_PRICE_UNITS
    reg["note"] = (
        "Each new catalog snapshot (about every 6 hours) triggers one "
        "evaluation diffing the two newest snapshots, restricted to "
        f"the top-{top_n} resources by 30d calls on either side. The "
        "first evaluation is a free silent baseline; every later "
        "evaluation debits $0.005 from the Fleet Pass (refunded when "
        "our side fails) and pushes to the webhook only when the "
        "watchlist changed: entries, exits, payTo rotations, or "
        "price changes among members."
    )
    return reg


def _ranked(entries: dict[str, dict], top_n: int) -> dict[str, int]:
    """resource -> 1-based rank, for the top_n by calls30d.
    Ties break on the resource string so ranks are deterministic."""
    ordered = sorted(
        entries.items(),
        key=lambda kv: (-(kv[1].get("calls30d") or 0), kv[0]),
    )
    return {resource: i + 1 for i, (resource, _row) in enumerate(ordered[:top_n])}


def compute_events(
    old: dict[str, dict],
    new: dict[str, dict],
    *,
    top_n: int,
) -> list[dict]:
    """The watchlist diff of one snapshot pair. Pure function over
    two normalized entry maps (indexfeed.entries)."""
    old_ranks = _ranked(old, top_n)
    new_ranks = _ranked(new, top_n)
    events: list[dict] = []
    for resource in sorted(set(new_ranks) - set(old_ranks)):
        row = new[resource]
        events.append(
            {
                "resource": resource,
                "kind": "entered",
                "serviceName": row.get("serviceName"),
                "rank": new_ranks[resource],
                "calls30d": row.get("calls30d"),
                "pay_to": row.get("payTo"),
                "amount": row.get("amount"),
            }
        )
    for resource in sorted(set(old_ranks) - set(new_ranks)):
        row = old[resource]
        events.append(
            {
                "resource": resource,
                "kind": "left",
                "serviceName": row.get("serviceName"),
                "rank": old_ranks[resource],
                "calls30d": row.get("calls30d"),
                "pay_to": row.get("payTo"),
                "amount": row.get("amount"),
                "still_listed": resource in new,
            }
        )
    for resource in sorted(set(old_ranks) & set(new_ranks)):
        orow, nrow = old[resource], new[resource]
        if (orow.get("payTo") or "").lower() != (nrow.get("payTo") or "").lower():
            events.append(
                {
                    "resource": resource,
                    "kind": "payto_rotated",
                    "serviceName": nrow.get("serviceName"),
                    "rank": new_ranks[resource],
                    "from_pay_to": orow.get("payTo"),
                    "to_pay_to": nrow.get("payTo"),
                }
            )
        if orow.get("amount") != nrow.get("amount"):
            events.append(
                {
                    "resource": resource,
                    "kind": "price_changed",
                    "serviceName": nrow.get("serviceName"),
                    "rank": new_ranks[resource],
                    "pay_to": nrow.get("payTo"),
                    "from_amount": orow.get("amount"),
                    "to_amount": nrow.get("amount"),
                }
            )
    return events


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    events: list[dict],
    before: dict,
    after: dict,
) -> None:
    webhook = reg.get("webhook_url")
    if not webhook:
        return
    payload = {
        "event": EVENT_NAME,
        "reg_id": reg["reg_id"],
        "top_n": reg["top_n"],
        "events": events,
        "baseline_at": before["captured_at"],
        "current_at": after["captured_at"],
        "evaluated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())
        ),
    }
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook)
        resp = await client.post(webhook, json=payload, follow_redirects=False)
        store.record_delivery(reg["reg_id"], EVENT_NAME, resp.status_code, None)
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded, not fatal
        store.record_delivery(reg["reg_id"], EVENT_NAME, None, type(e).__name__)
        log.warning("watchlist delivery failed for %s: %s", reg["reg_id"], e)


async def evaluate(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    index_store: IndexStore,
    reg: dict,
) -> dict:
    """One evaluation of one watchlist registration against the two
    newest snapshots. Mirrors rotalerts.evaluate's funding contract."""
    pair = index_store.latest_two()
    if pair is None:
        return {
            "reg_id": reg["reg_id"],
            "status": "waiting_for_snapshots",
            "detail": (
                "Fewer than two catalog snapshots are stored, so there "
                "is no diff to evaluate yet. Nothing was charged."
            ),
        }
    before, after = pair
    if (
        reg["last_eval_snapshot_id"] is not None
        and reg["last_eval_snapshot_id"] >= after["id"]
    ):
        return {
            "reg_id": reg["reg_id"],
            "status": "no_new_snapshot",
            "snapshot_id": after["id"],
        }
    if reg["last_eval_snapshot_id"] is None:
        store.update_watchlist_eval(reg["reg_id"], after["id"], "baseline")
        return {
            "reg_id": reg["reg_id"],
            "status": "baseline",
            "snapshot_id": after["id"],
            "current_at": after["captured_at"],
            "debited_units": 0,
            "webhook_push_attempted": False,
        }
    try:
        debit = store.debit_pass(
            reg["funding_id"], "stall-row", RUN_ROUTE_LABEL, RUN_PRICE_UNITS
        )
    except recheck_svc.PackError as e:
        store.update_watchlist_eval(reg["reg_id"], None, "funding_failed")
        return {
            "reg_id": reg["reg_id"],
            "status": "funding_failed",
            "error": e.message,
            "code": e.code,
        }
    try:
        events = compute_events(
            index_store.entries(before["id"]),
            index_store.entries(after["id"]),
            top_n=reg["top_n"],
        )
    except Exception:
        store.refund_debit(debit["debit_id"])
        raise
    status = "events" if events else "no_change"
    store.update_watchlist_eval(reg["reg_id"], after["id"], status)
    pushed = False
    if events and reg.get("webhook_url"):
        await _deliver(client, store, reg, events, before, after)
        pushed = True
    return {
        "reg_id": reg["reg_id"],
        "status": status,
        "events": events,
        "events_count": len(events),
        "baseline_at": before["captured_at"],
        "current_at": after["captured_at"],
        "debited_units": RUN_PRICE_UNITS,
        "webhook_push_attempted": pushed,
    }
