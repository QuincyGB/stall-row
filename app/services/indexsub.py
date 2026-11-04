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
from . import digest as digest_svc
from . import fetch as fetch_svc
from . import indexfeed as indexfeed_svc
from . import recheck as recheck_svc
from . import watchfund
from .indexfeed import IndexStore

log = logging.getLogger("stall-row.indexsub")

# One delivered evaluation costs exactly one /v1/index/changes call:
# the paid route's listed price (config.PRICE_UNITS). This constant
# is the debit authority and is stated in the registration response.
RUN_PRICE_UNITS = 100_000
RUN_ROUTE_LABEL = "/v1/index/changes"
EVENT_LIMIT = indexfeed_svc.DEFAULT_EVENT_LIMIT

# Index Subscription v2 (Next-20 #7): per-catalogue slicing. The
# Index's sources today are exactly the catalogues indexfeed holds
# (CDP Bazaar); a subscription's `catalogue` filter names the
# catalogue(s) it cares about — a comma-separated subset of
# CATALOGUES, or None for all (the v1 default, unchanged).
CATALOGUES = indexfeed_svc.CATALOGUES


def normalize_catalogue(raw: str | None) -> str | None:
    """Canonical catalogue slice: None (= all) or a comma-joined
    subset of CATALOGUES. Unknown names refuse the registration —
    a slice naming a catalogue the Index does not hold would
    silently deliver nothing forever."""
    if raw is None:
        return None
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return None
    unknown = [p for p in parts if p not in CATALOGUES]
    if unknown:
        raise IndexSubError(
            f"unknown catalogue(s) {unknown}: the Index holds {list(CATALOGUES)}"
        )
    return ",".join(dict.fromkeys(parts))


def catalogue_includes(reg: dict) -> bool:
    """Whether this registration's slice includes the Index's own
    catalogue (the only one held today). A v1 row (catalogue None)
    includes everything."""
    raw = reg.get("catalogue")
    if not raw:
        return True
    return indexfeed_svc.DEFAULT_CATALOGUE in [p.strip() for p in str(raw).split(",")]


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
    catalogue: str | None = None,
    delivery_mode: str | None = None,
    digest_interval: str | None = None,
    payer: str | None = None,
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
    catalogue = normalize_catalogue(catalogue)
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
    try:
        mode_v, interval_v = watchfund.normalize_delivery(delivery_mode, digest_interval)
    except watchfund.ClusterError as e:
        raise IndexSubError(e.message, e.http_status) from e
    reg = store.register_index_sub(
        resource_contains,
        pay_to,
        service_name,
        network,
        webhook_url,
        email,
        fleet_pass_id.strip(),
        catalogue=catalogue,
        delivery_mode=mode_v,
        digest_interval=interval_v,
        payer_hint=watchfund.normalize_payer_hint(payer),
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
    if not catalogue_includes(reg):
        # The slice names catalogues other than the one the Index
        # holds: the honest diff for this subscriber is empty (the
        # heartbeat still delivers — their slice simply has no
        # source feeding it yet).
        result = {
            "summary": {
                "resources_before": 0,
                "resources_after": 0,
                "field_changes": 0,
                "added": 0,
                "removed": 0,
                "by_network": {},
                "catalogue_filter": reg.get("catalogue"),
            },
            "events_returned": 0,
            "events": [],
        }
        return {
            "baseline_at": before["captured_at"],
            "current_at": after["captured_at"],
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())
            ),
            **result,
        }
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
    payload = {"event": "index_diff", "reg_id": reg["reg_id"], **diff}
    if digest_svc.maybe_queue(
        store, reg, kind="index_sub", reg_id=reg["reg_id"], payload=payload
    ):
        return True
    if not webhook:
        return False
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
    if not reg.get("active", True):
        # v2 management state: a paused (or cancelled) registration
        # is never evaluated — no debit, no watermark advance, no
        # push. The ticker already skips inactive rows; this covers
        # the manual /run path.
        return {
            "reg_id": reg["reg_id"],
            "status": reg.get("last_status") or "paused",
            "detail": (
                "This subscription is not active (paused or cancelled); "
                "nothing was charged. Resume it to continue the feed."
            ),
            "debited_units": 0,
            "webhook_push_attempted": False,
        }
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


# --- v2 management (Next-20 #7) ----------------------------------------------
# Console-free management API backing: pause / resume / cancel and
# filter updates, authenticated by the registration id alone (the
# credential v1 issued — see recheck.py's store notes). All state
# transitions go through the store so the ticker, /status and /run
# can never disagree about a registration's state.


def _must_get(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = store.get_index_sub((reg_id or "").strip())
    if reg is None:
        raise IndexSubError("unknown reg_id", 404)
    return reg


def pause(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    if reg.get("last_status") == "cancelled":
        raise IndexSubError("a cancelled subscription cannot be paused", 409)
    out = store.set_index_sub_active(reg["reg_id"], False, "paused")
    assert out is not None
    out["run_price_units"] = RUN_PRICE_UNITS
    return out


def resume(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    if reg.get("last_status") == "cancelled":
        raise IndexSubError(
            "a cancelled subscription cannot be resumed; register a new one", 409
        )
    out = store.set_index_sub_active(reg["reg_id"], True, "resumed")
    assert out is not None
    out["run_price_units"] = RUN_PRICE_UNITS
    return out


def cancel(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    out = store.set_index_sub_active(reg["reg_id"], False, "cancelled")
    assert out is not None
    out["run_price_units"] = RUN_PRICE_UNITS
    out["note"] = (
        "Cancelled: no further evaluations will run or debit the "
        "Fleet Pass. Cancellation is terminal — register a new "
        "subscription to restart the feed."
    )
    return out


async def update_filters(
    store: recheck_svc.WatchStore, reg_id: str, fields: dict
) -> dict:
    """Apply a partial filter update: exactly the fields the caller
    named change (a named field set to None clears that filter).
    Validation mirrors registration: a named pay_to must be an
    address, a named catalogue must be one the Index holds, and a
    named webhook must pass the SSRF check."""
    _must_get(store, reg_id)
    cleaned: dict = {}
    for name, value in fields.items():
        if name == "delivery_mode":
            mode = (value or "instant").strip().lower() if isinstance(value, str) else value
            if mode not in watchfund.DELIVERY_MODES:
                raise IndexSubError(
                    f"delivery_mode must be one of {', '.join(watchfund.DELIVERY_MODES)}"
                )
            cleaned[name] = mode
        elif name == "digest_interval":
            interval = (value or "").strip().lower() or None if isinstance(value, str) or value is None else value
            if interval is not None and interval not in watchfund.DIGEST_INTERVALS:
                raise IndexSubError(
                    f"digest_interval must be one of {', '.join(watchfund.DIGEST_INTERVALS)}"
                )
            cleaned[name] = interval
        elif name == "catalogue":
            cleaned[name] = normalize_catalogue(value)
        elif name == "pay_to":
            value = (value or "").strip() or None
            if value is not None and not is_address(value):
                raise IndexSubError("pay_to must be a 0x EVM address")
            cleaned[name] = value
        elif name == "webhook_url":
            value = (value or "").strip() or None
            if value:
                await _ssrf(value)
            cleaned[name] = value
        else:
            cleaned[name] = (value or "").strip() or None if isinstance(value, str) else value
    out = store.update_index_sub_filters((reg_id or "").strip(), cleaned)
    assert out is not None
    out["run_price_units"] = RUN_PRICE_UNITS
    return out
