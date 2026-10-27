"""Index-as-subscription: the standing form of the Verdict Index.

POST /v1/index/changes sells ONE diff of the two newest catalog
snapshots for $0.10. Inspector and router operators do not want one
diff — they want the feed: every new snapshot's diff, filtered to
the slice of the catalog they route or inspect, pushed to their
webhook without a human (or an agent loop) remembering to poll.

A subscription registration carries exactly the filters the paid
route accepts (resource_contains, pay_to, service_name, network).
Each time the Index ticker stores a new snapshot, the registration
is evaluated once: the filtered diff of the two newest snapshots is
computed and pushed to the subscriber's webhook. Unlike Rotation
Alerts (which push only on target events), a subscription delivers
the FULL filtered diff every evaluation — including an empty diff,
because for a feed subscriber "nothing changed in your slice" is
itself the heartbeat they are paying for.

Funding is the fleet's standing pattern, priced from the route this
product stands on: registrations are free, and each delivered
evaluation debits $0.10 from the buyer's Fleet Pass — the exact
price of one /v1/index/changes call. No new pricing model: a
subscription is prepaid /v1/index/changes calls, automated. The
first evaluation is a free silent baseline (it sets the snapshot
watermark and pushes nothing — diffs that predate the registration
are not the subscriber's feed). A run that cannot be funded marks
the registration funding_failed and does NOT advance the watermark.
A delivery computed but failed on OUR side refunds the debit.
Freshness is enforced, not assumed: if the newest snapshot has
aged past the Index freshness window (the snapshot ticker is
failing), a would-be delivery is skipped instead — no debit, no
watermark advance — until a fresh snapshot lands.

Cadence honesty: snapshots land about every 6 hours (~4/day), so a
funded subscription spends roughly $0.40/day while snapshots keep
arriving. Email is stored only — StallRow has no mail transport;
the webhook is the live channel, exactly as in Webhook Watch.

SSRF discipline matches Rotation Alerts: the webhook URL is checked
at registration AND again at delivery time; delivery never follows
redirects.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..rpc import is_address
from . import fetch as fetch_svc
from . import indexfeed as indexfeed_svc
from . import recheck as recheck_svc
from .indexfeed import IndexStore

log = logging.getLogger("stall-row.indexsub")

# One delivered evaluation costs exactly one /v1/index/changes call:
# the paid route's listed price (config.PRICE_UNITS). This constant
# is the debit authority and is stated in the registration response.
RUN_PRICE_UNITS = 100_000
RUN_ROUTE_LABEL = "/v1/index/changes"
EVENT_LIMIT = indexfeed_svc.DEFAULT_EVENT_LIMIT


class IndexSubError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise IndexSubError(str(e)) from e


async def register(
    store: recheck_svc.WatchStore,
    resource_contains: str | None,
    pay_to: str | None,
    service_name: str | None,
    network: str | None,
    webhook_url: str | None,
    email: str | None,
    fleet_pass_id: str | None,
) -> dict:
    """Validate everything BEFORE storing: the webhook must be a
    public URL, a named pay_to must be an address, and the Fleet
    Pass must exist and cover at least one evaluation. Filters are
    all optional (an unfiltered subscription is the whole catalog).
    Raises IndexSubError with the refusal; nothing stored on it."""
    resource_contains = (resource_contains or "").strip() or None
    pay_to = (pay_to or "").strip() or None
    service_name = (service_name or "").strip() or None
    network = (network or "").strip() or None
    if pay_to is not None and not is_address(pay_to):
        raise IndexSubError("pay_to must be a 0x EVM address")
    if webhook_url:
        webhook_url = webhook_url.strip()
        await _ssrf(webhook_url)
    if not webhook_url and not email:
        raise IndexSubError("a webhook_url or an email is required")
    if not fleet_pass_id:
        raise IndexSubError("fund runs with a fleet_pass_id")
    fleet_pass = store.get_pass(fleet_pass_id.strip())
    if fleet_pass is None:
        raise IndexSubError("unknown fleet_pass_id", 404)
    if fleet_pass["expired"]:
        raise IndexSubError("this fleet pass has expired", 410)
    if fleet_pass["balance_units"] < RUN_PRICE_UNITS:
        raise IndexSubError("fleet pass balance cannot fund one run", 402)
    reg = store.register_index_sub(
        resource_contains,
        pay_to,
        service_name,
        network,
        webhook_url,
        email,
        fleet_pass_id.strip(),
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["run_price_units"] = RUN_PRICE_UNITS
    reg["note"] = (
        "Standing Verdict Index feed. Each new catalog snapshot "
        "(about every 6 hours) triggers one evaluation: the filtered "
        "diff of the two newest snapshots is pushed to the webhook, "
        "empty diffs included (the heartbeat is part of the feed). "
        "The first evaluation is a free silent baseline; every later "
        "evaluation debits $0.10 from the Fleet Pass — the listed "
        "price of one POST /v1/index/changes call — refunded when "
        "our side fails to compute the diff."
    )
    return reg


def filtered_diff(
    index_store: IndexStore,
    before: dict,
    after: dict,
    reg: dict,
    limit: int = EVENT_LIMIT,
) -> dict:
    """The subscriber's diff for one snapshot pair: indexfeed's pure
    diff under the registration's own filters. No filter is widened
    or narrowed here — what was registered is what is delivered."""
    result = indexfeed_svc.diff_entries(
        index_store.entries(before["id"]),
        index_store.entries(after["id"]),
        resource_contains=reg.get("resource_contains"),
        pay_to=reg.get("pay_to"),
        service_name=reg.get("service_name"),
        network=reg.get("network"),
        limit=limit,
    )
    return {
        "baseline_at": before["captured_at"],
        "current_at": after["captured_at"],
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())
        ),
        **result,
    }


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    diff: dict,
) -> bool:
    webhook = reg.get("webhook_url")
    if not webhook:
        return False
    payload = {"event": "index_diff", "reg_id": reg["reg_id"], **diff}
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook)
        resp = await client.post(webhook, json=payload, follow_redirects=False)
        store.record_delivery(reg["reg_id"], "index_diff", resp.status_code, None)
        return True
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded, not fatal
        store.record_delivery(reg["reg_id"], "index_diff", None, type(e).__name__)
        log.warning("index subscription delivery failed for %s: %s", reg["reg_id"], e)
        return False


async def evaluate(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    index_store: IndexStore,
    reg: dict,
    now_epoch: float | None = None,
    stale_after: float | None = None,
) -> dict:
    """One evaluation of one subscription against the two newest
    snapshots. Funding refusals return as funding_failed (not
    raised), mirroring rotalerts.evaluate. A would-be delivery
    against a STALE newest snapshot returns skipped instead: no
    debit, no watermark advance, no diff — a feed built on an old
    pair would describe a catalog that no longer exists."""
    pair = index_store.latest_two()
    if pair is None:
        return {
            "reg_id": reg["reg_id"],
            "status": "waiting_for_snapshots",
            "detail": (
                "Fewer than two catalog snapshots are stored, so there "
                "is no diff to deliver yet. Nothing was charged."
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
        store.update_index_sub_eval(reg["reg_id"], after["id"], "baseline")
        return {
            "reg_id": reg["reg_id"],
            "status": "baseline",
            "snapshot_id": after["id"],
            "current_at": after["captured_at"],
            "debited_units": 0,
            "webhook_push_attempted": False,
        }
    if indexfeed_svc.snapshot_is_stale(after, now_epoch, stale_after):
        store.update_index_sub_eval(reg["reg_id"], None, "skipped")
        return {
            "reg_id": reg["reg_id"],
            "status": "skipped",
            "reason": (
                "latest index snapshot is stale (captured "
                f"{after['captured_at']}, beyond the freshness window); "
                "refusing to diff against stale data"
            ),
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
        store.update_index_sub_eval(reg["reg_id"], None, "funding_failed")
        return {
            "reg_id": reg["reg_id"],
            "status": "funding_failed",
            "error": e.message,
            "code": e.code,
        }
    try:
        diff = filtered_diff(index_store, before, after, reg)
    except Exception:
        store.refund_debit(debit["debit_id"])
        raise
    pushed = await _deliver(client, store, reg, diff)
    store.update_index_sub_eval(reg["reg_id"], after["id"], "delivered")
    return {
        "reg_id": reg["reg_id"],
        "status": "delivered",
        "diff": diff,
        "baseline_at": before["captured_at"],
        "current_at": after["captured_at"],
        "debited_units": RUN_PRICE_UNITS,
        "webhook_push_attempted": bool(reg.get("webhook_url")),
        "webhook_delivered": pushed,
    }
