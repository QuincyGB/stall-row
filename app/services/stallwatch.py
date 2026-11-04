"""Stall Watch, self-serve (Next-20 #12): the owner watch as a
shelf product.

Play 5's offer — an owner watches their own stall — rebuilt as one
self-serve registration call that COMPOSES machinery that already
exists, instead of an outreach flow:

  claim (services/claims.py) — the caller presents a claim session
    token for the origin; the origin must be CLAIMED. This is the
    ownership proof; there is no other registration path.
  pack (services/recheck.py) — the owner funds a $1 Recheck pack
    whose bound URL belongs to the claimed origin. The pack id is
    recorded on the registration as its funding pointer.
  watch (services/watch.py) — a Standing Watch is registered for
    the pack's URL, funded by that pack under the existing watch
    discipline (one pack credit per run, BASELINE/CHANGED pushes).
  receipts (the patrol's signed recovery receipts) — evaluations
    read the held patrol snapshot + claims history for the origin:
    newly seen OPEN defects push stall_defect alerts, and newly
    seen RECOVERIES push stall_recovery alerts carrying the
    recovery receipt: the patrol receipt_id verbatim when the
    patrol issued one (those receipts are the signed artifacts in
    the patrol's recovery-receipts index), plus a sha256 commitment
    over the recovery fact so the alert is verifiable against the
    held snapshot even when a receipt id has not reached us yet.

Funding honesty: the standing $0.005-debit stream is the underlying
Standing Watch's runs, spent from the $1 pack exactly as Webhook
Watch spends (pack credits at the Recheck rate). The patrol-side
evaluation reads data this service already holds and debits
nothing extra — stated here and in the registration payload,
never dressed up as a second charge. First evaluation delivers
the origin's CURRENT open defects and recoveries (no silent
baseline): the patrol book is historical fact about the owner's
own stall — the claims status read already discloses it — and an
owner registering to learn their stall is broken should not wait
for a second snapshot to hear it.

The deliverable is the shelf surface only: registration, status
(behind the same claim session), evaluation, pause/cancel. No
outreach, no sends to anyone but the registrant's own webhook.
Delivery shape is Item 20's (instant default, or digest).
"""

from __future__ import annotations

import hashlib
import json
import logging
from urllib.parse import urlsplit

import httpx

from . import claims as claims_svc
from . import digest as digest_svc
from . import patrolsnap as patrolsnap_svc
from . import recheck as recheck_svc
from . import watch as watch_svc
from . import watchfund

log = logging.getLogger("stall-row.stallwatch")


class StallWatchError(watchfund.ClusterError):
    pass


def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def recovery_receipt(origin: str, row: dict) -> dict:
    """The recovery receipt attached to a stall_recovery event:
    the patrol's signed receipt id verbatim when held, and a sha256
    commitment over the canonical recovery fact either way."""
    fact = {
        "origin": origin,
        "route": row.get("url") or row.get("route"),
        "defect_class": row.get("defect_class"),
        "first_seen": row.get("first_seen"),
        "recovered_at": row.get("recovered_at") or row.get("last_seen"),
        "receipt_id": row.get("receipt_id"),
    }
    commitment = hashlib.sha256(
        json.dumps(fact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "receipt_id": row.get("receipt_id"),
        "receipt_source": (
            "patrol signed recovery receipt (recovery-receipts index)"
            if row.get("receipt_id")
            else "no patrol receipt id in the held snapshot yet — the "
            "commitment below binds this alert to the recorded fact"
        ),
        "commitment": commitment,
        "fact": fact,
    }


def _patrol_rows(origin: str, patrol_snapshot: dict | None) -> list[dict]:
    if not patrol_snapshot:
        return []
    rows = []
    for row in patrol_snapshot.get("routes") or []:
        if not isinstance(row, dict):
            continue
        row_origin = (row.get("origin") or "").rstrip("/") or _origin_of(row.get("url"))
        if row_origin == origin:
            rows.append(row)
    return rows


def _history_rows(origin: str, store: recheck_svc.WatchStore) -> list[dict]:
    """Defect rows for the origin from the claims history (file if
    configured, else the snapshot stored on this service) — the
    same source GET /v1/claim/status serves."""
    history = claims_svc.load_history(origin)
    if not history["loaded"]:
        stored = store.get_claims_history_snapshot()
        if stored is not None:
            history = claims_svc.load_history_from_doc(origin, stored)
    return history.get("defects") or []


async def _register_inner(
    store: recheck_svc.WatchStore,
    origin_raw,
    session_token,
    pack_id: str,
    webhook_url: str | None,
    email: str | None,
    delivery_mode: str | None = None,
    digest_interval: str | None = None,
    payer: str | None = None,
) -> dict:
    """The one-call composition. Session first (claims discipline:
    the token must exist, be live, and belong to this origin), then
    claimed-state, then the pack (must exist, be live, hold credits,
    and be bound to a URL ON the claimed origin), then the Standing
    Watch composition. Raises ClaimError for session failures and
    StallWatchError / watch.WatchError for the rest; on any refusal
    nothing is registered."""
    origin, _session = claims_svc.validate_session(store, origin_raw, session_token)
    claimed = store.get_claimed(origin)
    if claimed is None:
        raise StallWatchError(
            "this origin is not claimed; complete the claim flow first", 403
        )
    pack_id = (pack_id or "").strip()
    pack = store.get_pack(pack_id) if pack_id else None
    if pack is None:
        raise StallWatchError("unknown pack_id", 404)
    if pack["expired"]:
        raise StallWatchError("this pack has expired", 410)
    if pack["credits_remaining"] < 1:
        raise StallWatchError("this pack has no credits left", 402)
    if _origin_of(pack["url"]) != origin:
        raise StallWatchError(
            "the pack must be bound to a URL on the claimed origin", 403
        )
    if webhook_url:
        webhook_url = webhook_url.strip()
        await watchfund.ssrf(webhook_url)
    if not webhook_url and not email:
        raise StallWatchError("a webhook_url or an email is required")
    mode, interval = watchfund.normalize_delivery(delivery_mode, digest_interval)
    # Compose the Standing Watch: watch.register re-validates the
    # pack binding and both URLs under its own discipline.
    watch_reg = await watch_svc.register(
        store, pack["url"], webhook_url, email, None, pack_id, None,
        delivery_mode=mode, digest_interval=interval,
        payer=watchfund.normalize_payer_hint(payer),
    )
    reg = store.register_stall_watch(
        origin, pack_id, watch_reg["watch_reg_id"], webhook_url, email,
        delivery_mode=mode, digest_interval=interval,
        payer_hint=watchfund.normalize_payer_hint(payer),
    )
    reg["claim"] = claimed
    reg["pack"] = pack
    reg["watch"] = watch_reg
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["note"] = (
        "Stall Watch: claim-verified owner watch over this origin. "
        "The composed Standing Watch runs full Recheck on the pack's "
        "URL, funded by the $1 pack (one credit per run — the "
        "standing $0.005 stream). Evaluations also read the held "
        "patrol record for the whole origin: new open defects push "
        "stall_defect, new recoveries push stall_recovery with the "
        "signed recovery receipt attached. Patrol-side evaluations "
        "debit nothing extra."
    )
    return reg


async def register(*args, **kwargs) -> dict:
    """Public register: shared-helper failures surface as
    StallWatchError; ClaimError (session discipline) and WatchError
    (the composed watch's own refusals) pass through untouched."""
    try:
        return await _register_inner(*args, **kwargs)
    except watchfund.ClusterError as e:
        if isinstance(e, StallWatchError):
            raise
        raise StallWatchError(e.message, e.http_status) from e


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    event: dict,
) -> bool:
    if digest_svc.maybe_queue(
        store, reg, kind="stall_watch", reg_id=reg["reg_id"], payload=event
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
        log.warning("stall watch delivery failed for %s: %s", reg["reg_id"], e)
        return True


async def evaluate(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    *,
    patrol_snapshot: dict | None = None,
    run_watch: bool = False,
) -> dict:
    """One patrol-side evaluation for the origin: newly seen open
    defects and recoveries from the held patrol snapshot + claims
    history become events (recoveries carry their receipt). With
    run_watch=True the composed Standing Watch also runs now (its
    own pack-funded discipline, pushes its own BASELINE/CHANGED)."""
    fresh = store.get_stall_watch(reg["reg_id"])
    if fresh is not None:
        reg = fresh
    if not reg.get("active", True):
        return {
            "reg_id": reg["reg_id"],
            "status": reg.get("last_status") or "paused",
            "detail": "registration is not active; nothing was evaluated",
        }
    origin = reg["origin"]
    if patrol_snapshot is None:
        patrol_snapshot = patrolsnap_svc.load_snapshot()
    seen = list(reg.get("seen_recoveries") or [])
    seen_set = set(seen)
    events: list[dict] = []
    pushed = False

    def _key(kind: str, route, defect_class, stamp) -> str:
        return f"{kind}|{route}|{defect_class}|{stamp}"

    for row in _patrol_rows(origin, patrol_snapshot):
        route = row.get("url")
        if row.get("status") == "recovered":
            key = _key("patrol-recovered", route, row.get("defect_class"),
                       row.get("recovered_at") or row.get("receipt_id"))
            if key in seen_set:
                continue
            seen.append(key)
            seen_set.add(key)
            event = {
                "event": "stall_recovery",
                "reg_id": reg["reg_id"],
                "origin": origin,
                "route": route,
                "defect_class": row.get("defect_class"),
                "first_seen": row.get("first_seen"),
                "recovered_at": row.get("recovered_at"),
                "receipt": recovery_receipt(origin, row),
            }
            events.append(event)
            if await _deliver(client, store, reg, event):
                pushed = True
        elif row.get("status") == "open":
            key = _key("patrol-open", route, row.get("defect_class"), row.get("first_seen"))
            if key in seen_set:
                continue
            seen.append(key)
            seen_set.add(key)
            event = {
                "event": "stall_defect",
                "reg_id": reg["reg_id"],
                "origin": origin,
                "route": route,
                "defect_class": row.get("defect_class"),
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
            }
            events.append(event)
            if await _deliver(client, store, reg, event):
                pushed = True
    for row in _history_rows(origin, store):
        route = row.get("route")
        state = row.get("state")
        if state == "recovered":
            key = _key("history-recovered", route, row.get("defect_class"), row.get("last_seen"))
            if key in seen_set:
                continue
            seen.append(key)
            seen_set.add(key)
            event = {
                "event": "stall_recovery",
                "reg_id": reg["reg_id"],
                "origin": origin,
                "route": route,
                "defect_class": row.get("defect_class"),
                "first_seen": row.get("first_seen"),
                "recovered_at": row.get("last_seen"),
                "receipt": recovery_receipt(origin, row),
            }
            events.append(event)
            if await _deliver(client, store, reg, event):
                pushed = True
        elif state == "open":
            key = _key("history-open", route, row.get("defect_class"), row.get("first_seen"))
            if key in seen_set:
                continue
            seen.append(key)
            seen_set.add(key)
            event = {
                "event": "stall_defect",
                "reg_id": reg["reg_id"],
                "origin": origin,
                "route": route,
                "defect_class": row.get("defect_class"),
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
            }
            events.append(event)
            if await _deliver(client, store, reg, event):
                pushed = True
    status = "events" if events else "no_new_events"
    store.update_stall_watch(reg["reg_id"], status, seen_recoveries=seen)
    out: dict = {
        "reg_id": reg["reg_id"],
        "origin": origin,
        "status": status,
        "events": events,
        "events_count": len(events),
        "delivery_mode": reg.get("delivery_mode") or "instant",
        "webhook_push_attempted": pushed,
    }
    if run_watch and reg.get("watch_reg_id"):
        watch_reg = store.get_watch_reg(reg["watch_reg_id"])
        if watch_reg is not None:
            out["watch_run"] = await watch_svc.run_watch(client, store, watch_reg)
    return out


def status(
    store: recheck_svc.WatchStore, origin_raw, session_token
) -> dict:
    """The owner's status read, behind the same claim session gate
    as GET /v1/claim/status: registration + composed watch + pack +
    the origin's patrol history summary."""
    origin, _session = claims_svc.validate_session(store, origin_raw, session_token)
    reg = store.stall_watch_for_origin(origin)
    if reg is None:
        raise StallWatchError("no stall watch registered for this origin", 404)
    watch_reg = (
        store.get_watch_reg(reg["watch_reg_id"]) if reg.get("watch_reg_id") else None
    )
    if watch_reg is not None:
        watch_reg = {
            **watch_reg,
            "deliveries": store.deliveries_for(reg["watch_reg_id"]),
        }
    return {
        **reg,
        "deliveries": store.deliveries_for(reg["reg_id"]),
        "pack": store.get_pack(reg["pack_id"]),
        "watch": watch_reg,
        "claim": store.get_claimed(origin),
    }
