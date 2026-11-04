"""Heartbeat Watch (Next-20 #9): the breadth tier of the watch
machinery — liveness only, at knock prices.

A registration names a SET of resources (x402 endpoints or plain
URLs). On its interval the ticker knocks each one with an UNPAID
probe — a heartbeat never pays the target — and classifies it with
Fleet Check's own classifier (services/fleetcheck.py _probe_raw /
_classify, the same verdict a paid sweep would give): reachability,
402-presence, and challenge-decodability are all that classifier
needs, so the states are up (verdict OK), degraded (DEGRADED: no
402, unexpected status, undecodable challenge, declared-terms
defect), or down (DOWN: unreachable / 5xx). Alerts fire ONLY on
state transitions (including the first knock's baseline) — steady
state pushes nothing, ever. That is the cheap floor under full
Recheck: a knock learns liveness and challenge shape, never the
field-level diff Recheck sells.

Funding (the spec rate, stated in the registration response): each
knock debits $0.001 (1000 units) from the registrant's prepaid
credential — a Fleet Pass (debit_pass) or a Recheck pack
(fractional-credit accounting at exactly 4000 units per credit, so
the knock rate is exact, never rounded up to a whole credit). A
Recheck pack is bound to ONE endpoint URL, so a pack may only fund
a heartbeat whose resource set is exactly that URL; breadth sets
fund from a Fleet Pass. Debits are reserve-then-probe: a knock
whose probe fails on OUR side (an exception out of the probe
machinery, not a target defect — target defects ARE the finding)
refunds its debit. A knock that cannot be funded pauses the
registration (funding_failed, active off) and probes nothing —
never a free silent knock, never negative, never a per-call x402
fallback.

Delivery shape is Item 20's: instant (default) pushes each
transition to the webhook; digest queues transitions for one
batched delivery per interval.
"""

from __future__ import annotations

import logging

import httpx

from . import digest as digest_svc
from . import fleetcheck as fleetcheck_svc
from . import recheck as recheck_svc
from . import watchfund

log = logging.getLogger("stall-row.heartbeat")

KNOCK_PRICE_UNITS = 1_000  # $0.001 per knock — the spec rate
ROUTE_LABEL = "/v1/heartbeat"
MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 86_400
DEFAULT_INTERVAL_S = 3_600
MAX_RESOURCES = 100

STATE_BY_VERDICT = {"OK": "up", "DEGRADED": "degraded", "DOWN": "down"}


class HeartbeatError(watchfund.ClusterError):
    pass


async def knock(client: httpx.AsyncClient, url: str) -> dict:
    """One unpaid liveness knock: Fleet Check's probe + classifier,
    reduced to a heartbeat state. Returns {state, http_status,
    verdict, defect, challenge_ok}."""
    status, headers, body, error = await fleetcheck_svc._probe_raw(client, url)
    row = fleetcheck_svc._classify(url, status, headers, body, error, None)
    defects = row.get("defects") or []
    return {
        "state": STATE_BY_VERDICT.get(row["verdict"], "down"),
        "http_status": row.get("http_status"),
        "verdict": row["verdict"],
        "defect": defects[0].get("code") if defects else None,
        "challenge_ok": bool(row.get("challenge_ok")),
    }


async def _register_inner(
    store: recheck_svc.WatchStore,
    resources: list[str] | None,
    webhook_url: str | None,
    email: str | None,
    interval_s: int | None,
    pack_id: str | None,
    fleet_pass_id: str | None,
    delivery_mode: str | None = None,
    digest_interval: str | None = None,
    payer: str | None = None,
) -> dict:
    """Validate everything BEFORE storing: resource set shape,
    SSRF on every resource and the webhook, delivery shape, and a
    funding credential that covers at least one knock. Raises
    HeartbeatError; nothing is stored on refusal."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in resources or []:
        url = str(raw).strip()
        if url and url not in seen:
            seen.add(url)
            cleaned.append(url)
    if not cleaned:
        raise HeartbeatError("name at least one resource to knock")
    if len(cleaned) > MAX_RESOURCES:
        raise HeartbeatError(f"at most {MAX_RESOURCES} resources per heartbeat watch")
    for url in cleaned:
        await watchfund.ssrf(url)
    if webhook_url:
        webhook_url = webhook_url.strip()
        await watchfund.ssrf(webhook_url)
    if not webhook_url and not email:
        raise HeartbeatError("a webhook_url or an email is required")
    mode, interval = watchfund.normalize_delivery(delivery_mode, digest_interval)
    interval_s = interval_s or DEFAULT_INTERVAL_S
    if interval_s < MIN_INTERVAL_S or interval_s > MAX_INTERVAL_S:
        raise HeartbeatError(
            f"interval_s must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S}"
        )
    if pack_id and len(cleaned) > 1:
        raise HeartbeatError(
            "a Recheck pack is bound to one endpoint URL and can only "
            "fund a heartbeat watch over exactly that URL — fund a "
            "multi-resource heartbeat with a Fleet Pass"
        )
    kind, funding_id = watchfund.resolve_funding(
        store, pack_id, fleet_pass_id, KNOCK_PRICE_UNITS,
        pack_url=cleaned[0] if pack_id else None,
    )
    reg = store.register_heartbeat(
        cleaned, webhook_url, email, interval_s, kind, funding_id,
        delivery_mode=mode, digest_interval=interval,
        payer_hint=watchfund.normalize_payer_hint(payer),
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["knock_price_units"] = KNOCK_PRICE_UNITS
    reg["note"] = (
        "Heartbeat Watch: each resource is knocked (unpaid probe) on "
        "the interval; every knock debits $0.001 (1000 units) from the "
        "funding credential. Alerts push ONLY on state transitions "
        "(baseline / up / degraded / down) — steady state is silent. "
        "A knock that cannot be funded pauses the registration "
        "(funding_failed) and probes nothing."
    )
    return reg


async def register(*args, **kwargs) -> dict:
    """Public register: shared-helper failures (SSRF, delivery
    shape, funding) surface as HeartbeatError, this service's own
    contract type."""
    try:
        return await _register_inner(*args, **kwargs)
    except watchfund.ClusterError as e:
        raise HeartbeatError(e.message, e.http_status) from e


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    event: dict,
) -> bool:
    """Route one transition event by the registration's delivery
    shape. Returns True when a push was attempted (instant) or the
    event was queued (digest)."""
    if digest_svc.maybe_queue(
        store, reg, kind="heartbeat", reg_id=reg["reg_id"], payload=event
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
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded
        store.record_delivery(reg["reg_id"], event["event"], None, type(e).__name__)
        log.warning("heartbeat delivery failed for %s: %s", reg["reg_id"], e)
        return True


async def run_reg(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
) -> dict:
    """Knock every resource in one registration once. Each knock is
    debited BEFORE its probe; an unfundable knock pauses the whole
    registration and stops the run (later resources are not probed
    unpaid). Transition events deliver per the delivery shape."""
    fresh = store.get_heartbeat_reg(reg["reg_id"])
    if fresh is not None:
        reg = fresh
    if not reg.get("active", True):
        return {
            "reg_id": reg["reg_id"],
            "status": reg.get("last_status") or "paused",
            "detail": "registration is not active; nothing was charged or probed",
            "debited_units": 0,
        }
    knocks: list[dict] = []
    events: list[dict] = []
    debited_total = 0
    pushed = False
    for url in reg["resources"]:
        try:
            debit = watchfund.debit(
                store, reg["funding_kind"], reg["funding_id"],
                KNOCK_PRICE_UNITS, ROUTE_LABEL,
            )
        except recheck_svc.PackError as e:
            store.set_reg_active("heartbeat_regs", reg["reg_id"], False, "funding_failed")
            store.update_heartbeat_run(reg["reg_id"], "funding_failed")
            return {
                "reg_id": reg["reg_id"],
                "status": "funding_failed",
                "error": e.message,
                "code": e.code,
                "knocks": knocks,
                "debited_units": debited_total,
                "webhook_push_attempted": pushed,
            }
        debited_total += KNOCK_PRICE_UNITS
        try:
            result = await knock(client, url)
        except Exception as e:  # noqa: BLE001 — OUR probe machinery failed
            watchfund.refund(
                store, reg["funding_kind"], reg["funding_id"], debit, KNOCK_PRICE_UNITS
            )
            debited_total -= KNOCK_PRICE_UNITS
            knocks.append(
                {"resource": url, "status": "probe_failed", "error": type(e).__name__}
            )
            continue
        before = store.get_heartbeat_state(reg["reg_id"], url)
        after = result["state"]
        store.set_heartbeat_state(reg["reg_id"], url, after, result)
        changed = before != after
        knocks.append(
            {
                "resource": url,
                "state": after,
                "previous_state": before,
                "changed": changed,
                "http_status": result["http_status"],
                "defect": result["defect"],
                "debited_units": KNOCK_PRICE_UNITS,
            }
        )
        if changed:
            event = {
                "event": "heartbeat_baseline" if before is None else "heartbeat_transition",
                "reg_id": reg["reg_id"],
                "resource": url,
                "from_state": before,
                "to_state": after,
                "detail": result,
                "checked_at": recheck_svc._now_iso(),
                "debited_units": KNOCK_PRICE_UNITS,
            }
            events.append(event)
            if await _deliver(client, store, reg, event):
                pushed = True
    status = "transitions" if events else "steady"
    store.update_heartbeat_run(reg["reg_id"], status)
    return {
        "reg_id": reg["reg_id"],
        "status": status,
        "knocks": knocks,
        "events": events,
        "events_count": len(events),
        "debited_units": debited_total,
        "delivery_mode": reg.get("delivery_mode") or "instant",
        "webhook_push_attempted": pushed,
    }
