"""Watch by Defect Class (Next-20 #10): subscribe to a defect
CLASS market-wide, not to one route.

The defect taxonomy is the one the fleet already uses — enumerated
from the in-repo data, not invented here: the patrol snapshot's
defect classes (app/data/patrol-snapshot.json: payto_mismatch,
amount_mismatch, schema_mismatch, challenge_undecodable,
unreachable, server_error, no_402, unexpected_status) plus Fleet
Check's target_refused (an SSRF-guard refusal at probe time, which
only exists live, never in the snapshot). The drift side of the
same book (eventfeed kinds payto_drift / amount_drift /
schema_drift, and Index field changes) is FOLDED into the patrol
classes so a subscriber names one vocabulary: an Index payTo field
change is a payto_mismatch-class event, an amount change an
amount_mismatch, a schema/description drift a schema_mismatch.

A subscription names one class and may scope it: by origin
("payTo rotations among LLM routes" is honestly expressible as
class payto_mismatch + origin / resource_contains / service_name
scope — the Index holds serviceName, not a route-family tag
taxonomy, so tag-based scoping stops at service_name substring;
that limit is stated in the registration response). Events are the
eventfeed's confirmed events (services/eventfeed.py build_events
over the Index snapshot diffs + the committed patrol snapshot):
an evaluation delivers every matching event the subscription has
not yet seen, debiting $0.005 (5000 units — Recheck arithmetic,
the eventfeed's own per-event price) per confirmed event from the
subscriber's pack/pass. The first evaluation is a free silent
baseline: everything already in the book predates the subscription
and is marked seen without delivery or debit. An event that cannot
be funded is NOT marked seen and NOT delivered — the subscription
marks funding_failed and the event waits for funding, so nothing
is silently skipped. Registration is free; management
(pause/resume/cancel) is console-free, authenticated by the
registration id alone, in the Index Subscription v2 style.
Delivery shape is Item 20's (instant default, or digest).
"""

from __future__ import annotations

import hashlib
import json
import logging
from urllib.parse import urlsplit

import httpx

from . import digest as digest_svc
from . import eventfeed as eventfeed_svc
from . import patrolsnap as patrolsnap_svc
from . import recheck as recheck_svc
from . import watchfund

log = logging.getLogger("stall-row.defectwatch")

EVENT_PRICE_UNITS = 5_000  # $0.005 per confirmed event — Recheck arithmetic
ROUTE_LABEL = "/v1/defect-watch"

# The defect taxonomy, from the in-repo data (see module docstring).
DEFECT_CLASSES = (
    "payto_mismatch",
    "amount_mismatch",
    "schema_mismatch",
    "challenge_undecodable",
    "unreachable",
    "server_error",
    "no_402",
    "unexpected_status",
    "target_refused",
)

# eventfeed envelope kinds -> defect class (the fold described above).
_KIND_TO_CLASS = {
    "payto_drift": "payto_mismatch",
    "payto_rotation": "payto_mismatch",
    "amount_drift": "amount_mismatch",
    "amount_changed": "amount_mismatch",
    "schema_drift": "schema_mismatch",
    "terms_changed": "amount_mismatch",
    "description_drift": "schema_mismatch",
    "service_name_drift": "schema_mismatch",
}


class DefectWatchError(watchfund.ClusterError):
    pass


def event_class(envelope_event: dict) -> str | None:
    """The defect class of one eventfeed event (the inner `event`
    dict of an envelope), or None when the event is not a defect
    event at all (firehose first-seens are not defects)."""
    defect = envelope_event.get("defect_class")
    if isinstance(defect, str) and defect:
        return defect
    return _KIND_TO_CLASS.get(envelope_event.get("kind") or "")


def _origin_of(resource: str | None) -> str | None:
    if not resource:
        return None
    try:
        parts = urlsplit(resource)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def matches(reg: dict, envelope_event: dict) -> bool:
    """Whether one confirmed event falls inside a subscription:
    class must match; each named scope filter must also match
    (origin exact, resource_contains substring, service_name
    substring — the scopes the held data honestly supports)."""
    if event_class(envelope_event) != reg["defect_class"]:
        return False
    if reg.get("origin"):
        origin = envelope_event.get("origin") or _origin_of(envelope_event.get("resource"))
        if (origin or "").rstrip("/") != reg["origin"].rstrip("/"):
            return False
    if reg.get("resource_contains"):
        if reg["resource_contains"] not in (envelope_event.get("resource") or ""):
            return False
    if reg.get("service_name"):
        name = envelope_event.get("serviceName") or ""
        if reg["service_name"].lower() not in str(name).lower():
            return False
    return True


def confirmed_events(index_store=None, patrol_snapshot=None) -> list[dict]:
    """Every confirmed defect event the feed machinery currently
    holds, as {key, event} rows in feed order. Sources are exactly
    eventfeed's: Index snapshot-pair diffs + the patrol snapshot
    (loaded when not supplied).

    `key` is a hash of the EVENT CONTENT, not the envelope's
    event_id: the feed's event_id is chain-position-dependent (the
    record hash chains over the previous envelope), so an event
    inserted ahead of an old one would re-id everything after it
    and a subscription keyed on event_id would re-deliver — and
    re-debit — old events. Content is the stable identity."""
    if patrol_snapshot is None:
        patrol_snapshot = patrolsnap_svc.load_snapshot()
    out: list[dict] = []
    for topic in ("drift",):
        for env in eventfeed_svc.build_events(
            topic, index_store=index_store, patrol_snapshot=patrol_snapshot
        ):
            digest = hashlib.sha256(
                json.dumps(env["event"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            out.append(
                {"key": f"ev_{digest[:24]}", "seq": env["seq"], "event": env["event"]}
            )
    return out


async def _register_inner(
    store: recheck_svc.WatchStore,
    defect_class: str,
    origin: str | None,
    resource_contains: str | None,
    service_name: str | None,
    webhook_url: str | None,
    email: str | None,
    pack_id: str | None,
    fleet_pass_id: str | None,
    delivery_mode: str | None = None,
    digest_interval: str | None = None,
    payer: str | None = None,
) -> dict:
    """Validate everything BEFORE storing: the class must be one
    the fleet's data actually uses, scopes must be well-formed,
    the webhook SSRF-clean, and the funding credential able to
    cover at least one event. A pack-funded subscription follows
    the pack discipline: a pack is bound to one endpoint, so a
    pack may only fund a subscription scoped to exactly that URL
    (resource_contains == the pack's URL)."""
    defect_class = (defect_class or "").strip()
    if defect_class not in DEFECT_CLASSES:
        raise DefectWatchError(
            f"defect_class must be one of {', '.join(DEFECT_CLASSES)}"
        )
    origin = (origin or "").strip().rstrip("/") or None
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise DefectWatchError("origin must look like https://host")
    resource_contains = (resource_contains or "").strip() or None
    service_name = (service_name or "").strip() or None
    if webhook_url:
        webhook_url = webhook_url.strip()
        await watchfund.ssrf(webhook_url)
    if not webhook_url and not email:
        raise DefectWatchError("a webhook_url or an email is required")
    mode, interval = watchfund.normalize_delivery(delivery_mode, digest_interval)
    kind, funding_id = watchfund.resolve_funding(
        store, pack_id, fleet_pass_id, EVENT_PRICE_UNITS
    )
    if kind == "pack":
        pack = store.get_pack(funding_id)
        assert pack is not None
        if resource_contains != pack["url"] or origin is not None or service_name is not None:
            raise DefectWatchError(
                "a Recheck pack only funds its own URL's events: a "
                "pack-funded subscription must set resource_contains "
                "to exactly the pack's URL and no other scope — "
                "market-wide class subscriptions fund from a Fleet Pass"
            )
    reg = store.register_defect_sub(
        defect_class, origin, resource_contains, service_name,
        webhook_url, email, kind, funding_id,
        delivery_mode=mode, digest_interval=interval,
        payer_hint=watchfund.normalize_payer_hint(payer),
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["event_price_units"] = EVENT_PRICE_UNITS
    reg["defect_classes"] = list(DEFECT_CLASSES)
    reg["note"] = (
        f"Watch by Defect Class: confirmed {defect_class} events "
        "market-wide (Index snapshot diffs + Dima's patrol records, "
        "as the drift feed confirms them) are delivered as they are "
        "confirmed; each delivered event debits $0.005 (5000 units) "
        "from the funding credential. The first evaluation is a free "
        "silent baseline. Scoping stops at origin / resource_contains "
        "/ service_name: the Index holds service names, not a "
        "route-family tag taxonomy, so family scoping is by "
        "service_name substring."
    )
    return reg


async def register(*args, **kwargs) -> dict:
    """Public register: shared-helper failures surface as
    DefectWatchError, this service's own contract type."""
    try:
        return await _register_inner(*args, **kwargs)
    except watchfund.ClusterError as e:
        raise DefectWatchError(e.message, e.http_status) from e


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    event: dict,
) -> bool:
    if digest_svc.maybe_queue(
        store, reg, kind="defect_class", reg_id=reg["reg_id"], payload=event
    ):
        return True
    webhook = reg.get("webhook_url")
    if not webhook:
        return False
    try:
        await watchfund.ssrf(webhook)
        resp = await client.post(webhook, json=event, follow_redirects=False)
        store.record_delivery(reg["reg_id"], event["event"], resp.status_code, None)
        return True
    except Exception as e:  # noqa: BLE001
        store.record_delivery(reg["reg_id"], event["event"], None, type(e).__name__)
        log.warning("defect delivery failed for %s: %s", reg["reg_id"], e)
        return True


async def evaluate(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    *,
    index_store=None,
    patrol_snapshot=None,
) -> dict:
    """One evaluation: deliver every confirmed event in scope that
    this subscription has not seen, debiting per event. Baseline
    (first evaluation) marks the current book seen for free. A
    funding failure mid-stream stops the evaluation: events already
    delivered keep their debits, the unfunded event and everything
    after it stay unseen for the next funded evaluation."""
    fresh = store.get_defect_sub(reg["reg_id"])
    if fresh is not None:
        reg = fresh
    if not reg.get("active", True):
        return {
            "reg_id": reg["reg_id"],
            "status": reg.get("last_status") or "paused",
            "detail": "subscription is not active; nothing was charged",
            "debited_units": 0,
        }
    events = confirmed_events(index_store, patrol_snapshot)
    seen = list(reg.get("seen_keys") or [])
    seen_set = set(seen)
    matching = [row for row in events if matches(reg, row["event"])]
    # Never evaluated yet: no status, or paused/resumed before its
    # first evaluation with nothing seen — the baseline is still owed.
    if reg.get("last_status") is None or (
        not seen and reg.get("last_status") in ("paused", "resumed")
    ):
        store.update_defect_sub(
            reg["reg_id"], "baseline", seen_keys=[r["key"] for r in matching]
        )
        return {
            "reg_id": reg["reg_id"],
            "status": "baseline",
            "events_in_book": len(matching),
            "debited_units": 0,
            "webhook_push_attempted": False,
        }
    new_rows = [r for r in matching if r["key"] not in seen_set]
    delivered: list[dict] = []
    debited = 0
    pushed = False
    for row in new_rows:
        try:
            debit = watchfund.debit(
                store, reg["funding_kind"], reg["funding_id"],
                EVENT_PRICE_UNITS, ROUTE_LABEL,
            )
        except recheck_svc.PackError as e:
            store.update_defect_sub(reg["reg_id"], "funding_failed", seen_keys=seen)
            return {
                "reg_id": reg["reg_id"],
                "status": "funding_failed",
                "error": e.message,
                "code": e.code,
                "events": delivered,
                "events_count": len(delivered),
                "debited_units": debited,
                "webhook_push_attempted": pushed,
            }
        event = {
            "event": "defect_event",
            "reg_id": reg["reg_id"],
            "defect_class": reg["defect_class"],
            "event_key": row["key"],
            "seq": row["seq"],
            **row["event"],
            "debited_units": EVENT_PRICE_UNITS,
        }
        _ = debit
        seen.append(row["key"])
        delivered.append(event)
        debited += EVENT_PRICE_UNITS
        if await _deliver(client, store, reg, event):
            pushed = True
    status = "events" if delivered else "no_new_events"
    store.update_defect_sub(reg["reg_id"], status, seen_keys=seen)
    return {
        "reg_id": reg["reg_id"],
        "status": status,
        "events": delivered,
        "events_count": len(delivered),
        "debited_units": debited,
        "delivery_mode": reg.get("delivery_mode") or "instant",
        "webhook_push_attempted": pushed,
    }


# --- console-free management (Index Subscription v2 style) -------------------

def _must_get(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = store.get_defect_sub((reg_id or "").strip())
    if reg is None:
        raise DefectWatchError("unknown reg_id", 404)
    return reg


def pause(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    if reg.get("last_status") == "cancelled":
        raise DefectWatchError("a cancelled subscription cannot be paused", 409)
    store.set_reg_active("defect_subs", reg["reg_id"], False, "paused")
    out = store.get_defect_sub(reg["reg_id"])
    assert out is not None
    out["event_price_units"] = EVENT_PRICE_UNITS
    return out


def resume(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    if reg.get("last_status") == "cancelled":
        raise DefectWatchError(
            "a cancelled subscription cannot be resumed; register a new one", 409
        )
    store.set_reg_active("defect_subs", reg["reg_id"], True, "resumed")
    out = store.get_defect_sub(reg["reg_id"])
    assert out is not None
    out["event_price_units"] = EVENT_PRICE_UNITS
    return out


def cancel(store: recheck_svc.WatchStore, reg_id: str) -> dict:
    reg = _must_get(store, reg_id)
    store.set_reg_active("defect_subs", reg["reg_id"], False, "cancelled")
    out = store.get_defect_sub(reg["reg_id"])
    assert out is not None
    out["event_price_units"] = EVENT_PRICE_UNITS
    out["note"] = (
        "Cancelled: no further evaluations will run or debit the "
        "funding credential. Cancellation is terminal."
    )
    return out
