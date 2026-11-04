"""Alert Digest Mode (Next-20 #20): the batched delivery shape for
the watch/webhook layer.

A registration chooses delivery_mode="instant" (the default —
today's per-event webhook push, unchanged) or "digest" with
digest_interval "hourly" | "daily". Under digest, events that would
have been pushed instantly are instead queued (store.digest_queue)
and flushed as ONE delivery per interval: a digest envelope with
the period bounds, counts by event class, and the full events list,
POSTed to the registration's webhook and recorded in the shared
watch_deliveries log. Debits are untouched by the mode: every
underlying knock/event was already debited at its product rate when
it happened — digest changes delivery shape only, never pricing.

Surfaces: every watch-cluster product (heartbeat, defect-class,
portfolio, stall watch) plus the older families whose delivery is a
single webhook push per evaluation — Standing Watch (watch.py),
Rotation Alerts, Router Watchlist, and Index Subscriptions (their
_diff_ pushes queue the same way; the debit/evaluation logic is
unchanged). NOT applied to the self-serve Recheck change-webhook
subscriptions (hooksubs.py): those deliveries are HMAC-SHA256 signed
per event with per-event bounded retries and a dead-letter state
machine keyed to the individual event — batching would change the
signing and retry contract subscribers already depend on, so that
surface keeps instant-only delivery. Also not applied to hosted
inboxes: a hosted inbox is an INBOUND mailbox for other stalls'
knocks, not an outbound channel this service sends digests through;
digest registrations deliver to their webhook (email is stored
only, as everywhere on this row — there is no mail transport).

Flush discipline: a queue (one registration + one interval bucket)
is due when its oldest pending event is at least one interval old,
or when flush_digests is called with force=True (the manual /run
and management surfaces use this so a digest can be drained on
demand). A failed webhook POST is recorded and the events stay
queued for the next flush — a digest is never dropped silently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

import httpx

from . import fetch as fetch_svc
from . import recheck as recheck_svc
from .watchfund import DIGEST_INTERVAL_S

log = logging.getLogger("stall-row.digest")

DIGEST_EVENT = "alert_digest"


def queue_key_for(kind: str, reg_id: str, digest_interval: str | None) -> str:
    return f"{kind}:{reg_id}:{digest_interval or 'daily'}"


def maybe_queue(
    store: recheck_svc.WatchStore,
    reg: dict,
    *,
    kind: str,
    reg_id: str,
    payload: dict,
) -> bool:
    """Queue `payload` for digest delivery when the registration is
    in digest mode. Returns True when queued (the caller must NOT
    also push instantly); False when the registration is instant
    (the caller pushes as today)."""
    if (reg.get("delivery_mode") or "instant") != "digest":
        return False
    key = queue_key_for(kind, reg_id, reg.get("digest_interval"))
    store.queue_digest(
        key, kind, reg_id, reg.get("webhook_url"), reg.get("email"), payload
    )
    return True


def _event_class(payload: dict) -> str:
    """The class a digest counts an event under: the payload's own
    event/kind/defect_class, in that order — the fields the watch
    products already put on their pushes."""
    for field in ("event", "kind", "defect_class", "status"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return "event"


def build_digest(queue_key: str, rows: list[dict]) -> dict:
    """The digest envelope for one queue's pending rows."""
    events = [r["payload"] for r in rows]
    counts = Counter(_event_class(p) for p in events)
    first = rows[0]
    kind, reg_id = first["kind"], first["reg_id"]
    interval = queue_key.rsplit(":", 1)[-1]
    return {
        "event": DIGEST_EVENT,
        "kind": kind,
        "reg_id": reg_id,
        "digest_interval": interval,
        "period": {
            "from": recheck_svc._iso_of(rows[0]["queued_epoch"]),
            "to": recheck_svc._iso_of(rows[-1]["queued_epoch"]),
        },
        "generated_at": recheck_svc._now_iso(),
        "events_count": len(events),
        "counts_by_class": dict(sorted(counts.items())),
        "events": events,
        "note": (
            "Digest delivery: these events were debited individually "
            "at their product rates when they occurred; this batched "
            "delivery changes the delivery shape only, never pricing."
        ),
    }


async def _deliver_digest(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    rows: list[dict],
    digest: dict,
) -> bool:
    """POST one digest to its webhook (SSRF re-checked at delivery,
    redirects never followed — the watch family's discipline) and
    record the attempt. Returns True when the receiver answered
    (any HTTP status counts as delivered, exactly as the instant
    pushes record it); False on a transport failure, in which case
    the caller leaves the rows queued."""
    webhook = rows[0].get("webhook_url")
    if not webhook:
        # No webhook (email-only registration): StallRow has no mail
        # transport, so the digest is recorded as the delivery —
        # the rows are the stored digest, readable via status.
        store.record_delivery(rows[0]["reg_id"], DIGEST_EVENT, None, "no_webhook")
        return True
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook)
        resp = await client.post(webhook, json=digest, follow_redirects=False)
        store.record_delivery(rows[0]["reg_id"], DIGEST_EVENT, resp.status_code, None)
        return True
    except Exception as e:  # noqa: BLE001 — recorded, rows stay queued
        store.record_delivery(
            rows[0]["reg_id"], DIGEST_EVENT, None, type(e).__name__
        )
        log.warning("digest delivery failed for %s: %s", rows[0]["reg_id"], e)
        return False


async def flush_digests(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    *,
    queue_key: str | None = None,
    force: bool = False,
    now_epoch: float | None = None,
) -> list[dict]:
    """Flush every due digest queue (or one named queue). A queue is
    due when its oldest pending event has waited at least its
    interval; force flushes regardless (manual drain). Returns one
    result dict per queue attempted."""
    now = time.time() if now_epoch is None else now_epoch
    keys = [queue_key] if queue_key else store.digest_queue_keys()
    results: list[dict] = []
    for key in keys:
        rows = store.pending_digests(key)
        if not rows:
            continue
        interval = key.rsplit(":", 1)[-1]
        wait_s = DIGEST_INTERVAL_S.get(interval, DIGEST_INTERVAL_S["daily"])
        if not force and (now - rows[0]["queued_epoch"]) < wait_s:
            results.append(
                {"queue_key": key, "status": "not_due", "events": len(rows)}
            )
            continue
        digest = build_digest(key, rows)
        delivered = await _deliver_digest(client, store, rows, digest)
        if delivered:
            store.mark_digests_delivered([r["id"] for r in rows], now)
        results.append(
            {
                "queue_key": key,
                "kind": rows[0]["kind"],
                "reg_id": rows[0]["reg_id"],
                "status": "delivered" if delivered else "delivery_failed",
                "events_count": len(rows),
                "counts_by_class": digest["counts_by_class"],
                "digest": digest if delivered else None,
            }
        )
    return results


async def flush_reg_digest(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    kind: str,
    reg: dict,
    *,
    force: bool = True,
) -> dict | None:
    """Drain one registration's digest queue (the /digest surface).
    None when the registration is not in digest mode or its queue
    is empty."""
    if (reg.get("delivery_mode") or "instant") != "digest":
        return None
    reg_id = reg.get("reg_id") or reg.get("watch_reg_id")
    key = queue_key_for(kind, reg_id, reg.get("digest_interval"))
    results = await flush_digests(client, store, queue_key=key, force=force)
    return results[0] if results else None
