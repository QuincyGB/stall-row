"""Rotation Alerts: the push form of the Verdict Index.

Webhook Watch probes ONE URL on an interval. Rotation Alerts watch
the catalog itself: a registration names a payTo and/or a single
resource, and each time the Index ticker stores a new catalog
snapshot, the registration is evaluated once against the diff of the
two newest snapshots. Events that touch the named target — a payTo
rotation (the listing now pays a different address), a price change,
or the listing vanishing from the catalog — are pushed to the
buyer's webhook as JSON. No events, no push.

Funding follows Webhook Watch's shape, narrowed to the credential
that fits: registrations are free, and each comparison run debits
$0.005 from the buyer's Fleet Pass (the same per-run price as a
Webhook Watch fleet-pass run), refunded when our side fails. The
first evaluation is a silent baseline — it only sets the snapshot
watermark, debits nothing, and pushes nothing, because changes that
predate the registration are not the buyer's news. A run that
cannot be funded marks the registration funding_failed and does NOT
advance the watermark, so no snapshot's changes are silently
skipped; the next funded evaluation still compares against the last
evaluated snapshot pair the store diffs (the two newest held).

Cadence honesty: snapshots land about every 6 hours (~4/day), so a
funded registration spends roughly $0.02/day while snapshots keep
arriving. The store holds only the newest 3 snapshots, so the
comparison is always the two newest — a registration evaluated less
often than snapshots arrive still sees the latest pair, not a
per-snapshot replay.

SSRF discipline matches Webhook Watch: the webhook URL (and the
resource URL, when one is named) is checked at registration AND the
webhook again at delivery time; delivery never follows redirects.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..rpc import is_address
from . import fetch as fetch_svc
from . import recheck as recheck_svc
from .indexfeed import IndexStore

log = logging.getLogger("stall-row.rotalerts")

# One comparison run costs what one Webhook Watch fleet-pass run
# costs: the fleet's $0.005 probe tier. The price authority for this
# debit is this constant — it is stated in the registration response.
RUN_PRICE_UNITS = 5_000
RUN_ROUTE_LABEL = "/v1/rotation-alerts"

EVENT_KINDS = ("payto_rotated", "price_changed", "vanished", "added")


class RotationError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise RotationError(str(e)) from e


async def register(
    store: recheck_svc.WatchStore,
    pay_to: str | None,
    resource: str | None,
    webhook_url: str | None,
    email: str | None,
    fleet_pass_id: str | None,
) -> dict:
    """Validate everything BEFORE storing: the target must name a
    payTo and/or a resource, the webhook must be a public URL, and
    the Fleet Pass must exist and cover at least one run. Raises
    RotationError with the refusal; nothing is stored on refusal."""
    pay_to = (pay_to or "").strip() or None
    resource = (resource or "").strip() or None
    if pay_to is None and resource is None:
        raise RotationError("name a pay_to and/or a resource to watch")
    if pay_to is not None and not is_address(pay_to):
        raise RotationError("pay_to must be a 0x EVM address")
    if resource is not None:
        await _ssrf(resource)
    if webhook_url:
        webhook_url = webhook_url.strip()
        await _ssrf(webhook_url)
    if not webhook_url and not email:
        raise RotationError("a webhook_url or an email is required")
    if not fleet_pass_id:
        raise RotationError("fund runs with a fleet_pass_id")
    fleet_pass = store.get_pass(fleet_pass_id.strip())
    if fleet_pass is None:
        raise RotationError("unknown fleet_pass_id", 404)
    if fleet_pass["expired"]:
        raise RotationError("this fleet pass has expired", 410)
    if fleet_pass["balance_units"] < RUN_PRICE_UNITS:
        raise RotationError("fleet pass balance cannot fund one run", 402)
    reg = store.register_rotation(
        pay_to, resource, webhook_url, email, fleet_pass_id.strip()
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
        "evaluation comparing the two newest snapshots. The first "
        "evaluation is a free silent baseline; every later evaluation "
        "debits $0.005 from the Fleet Pass (refunded when our side "
        "fails) and pushes to the webhook only when an event touches "
        "the watched payTo/resource: payTo rotation, price change, "
        "vanishing, or a new listing for the watched payTo."
    )
    return reg


def compute_events(
    old: dict[str, dict],
    new: dict[str, dict],
    *,
    pay_to: str | None = None,
    resource: str | None = None,
) -> list[dict]:
    """The events in one snapshot pair that touch the watched target.
    Pure function over two normalized entry maps (indexfeed.entries).

    A resource-named registration watches exactly that resource. A
    payTo registration watches every resource that paid the address
    in either snapshot — so a rotation AWAY from the address and a
    rotation INTO it are both events, as is a listing for the address
    appearing or vanishing."""
    if resource is not None:
        targets = [resource]
    else:
        assert pay_to is not None
        target = pay_to.lower()
        targets = [
            r
            for r in set(old) | set(new)
            if ((old.get(r) or {}).get("payTo") or "").lower() == target
            or ((new.get(r) or {}).get("payTo") or "").lower() == target
        ]
    events: list[dict] = []
    for r in sorted(targets):
        orow, nrow = old.get(r), new.get(r)
        name = (nrow or orow or {}).get("serviceName")
        if orow is not None and nrow is None:
            events.append(
                {
                    "resource": r,
                    "kind": "vanished",
                    "serviceName": name,
                    "pay_to": orow.get("payTo"),
                    "amount": orow.get("amount"),
                }
            )
        elif nrow is not None and orow is None:
            events.append(
                {
                    "resource": r,
                    "kind": "added",
                    "serviceName": name,
                    "pay_to": nrow.get("payTo"),
                    "amount": nrow.get("amount"),
                }
            )
        elif orow is not None and nrow is not None:
            if (orow.get("payTo") or "").lower() != (nrow.get("payTo") or "").lower():
                events.append(
                    {
                        "resource": r,
                        "kind": "payto_rotated",
                        "serviceName": name,
                        "from_pay_to": orow.get("payTo"),
                        "to_pay_to": nrow.get("payTo"),
                    }
                )
            if orow.get("amount") != nrow.get("amount"):
                events.append(
                    {
                        "resource": r,
                        "kind": "price_changed",
                        "serviceName": name,
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
        "event": "rotation_alert",
        "reg_id": reg["reg_id"],
        "pay_to": reg.get("pay_to"),
        "resource": reg.get("resource"),
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
        store.record_delivery(reg["reg_id"], "rotation_alert", resp.status_code, None)
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded, not fatal
        store.record_delivery(
            reg["reg_id"], "rotation_alert", None, type(e).__name__
        )
        log.warning("rotation delivery failed for %s: %s", reg["reg_id"], e)


async def evaluate(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    index_store: IndexStore,
    reg: dict,
) -> dict:
    """One evaluation of one registration against the two newest
    snapshots. Returns a status dict; funding refusals are returned
    (not raised) as funding_failed, mirroring watch.run_watch."""
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
        # Silent baseline: set the watermark only. Changes in this
        # pair predate the registration and are not the buyer's news.
        store.update_rotation_eval(reg["reg_id"], after["id"], "baseline")
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
        store.update_rotation_eval(reg["reg_id"], None, "funding_failed")
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
            pay_to=reg.get("pay_to"),
            resource=reg.get("resource"),
        )
    except Exception:
        store.refund_debit(debit["debit_id"])
        raise
    status = "events" if events else "no_change"
    store.update_rotation_eval(reg["reg_id"], after["id"], status)
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
