"""Portfolio Watch (Next-20 #11): one registration per OPERATOR
FAMILY.

The registrant names an operator ONCE — as the Index holds it: a
service_name (the operator identity the catalog carries) or,
failing that, an origin. Every route in that family is then
watched under ONE balance (one Fleet Pass), ONE webhook, and
family-level event payloads. The family is expanded from the
Index's current snapshot AT REGISTRATION and re-expanded on EVERY
evaluation: routes the operator adds later join the watch
automatically (and are named as new_routes in that evaluation's
payload), and routes that leave the catalog drop out — that
behaviour is stated in the registration payload, per spec.

Tiers (the registrant's chosen tier per family registration — no
new prices; both rates are the underlying products' own):
  * "heartbeat" — Heartbeat Watch knocks, $0.001 (1000 units) per
    route per run, transition events only.
  * "recheck" — full Recheck per route, $0.005 (5000 units) per
    route per run (the listed Recheck price, debited from the pass
    under the /v1/recheck label), BASELINE/CHANGED events only.
  * "digest" — no probing at all: each evaluation diffs the
    family's Index entries against the previous evaluation's and
    delivers ONE family-level digest of events (added / removed /
    field changes), debiting $0.005 per evaluation (the Rotation
    Alerts run price) — the "one family-level digest" shape in its
    purest form. Its first evaluation is a free silent baseline.

Funding is Fleet-Pass-only: a Recheck pack is bound to one endpoint
URL and cannot fund a family (the same reason Router Watchlist is
pass-funded). A run that cannot fund a route stops there
(funding_failed): routes already run keep their debits, the rest
are not probed unpaid. Delivery shape is Item 20's on top of the
family payload: instant (default) pushes the family payload per
run-with-events; digest queues those payloads for the batched
delivery.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from ..rpc import UpstreamError
from . import digest as digest_svc
from . import heartbeat as heartbeat_svc
from . import indexfeed as indexfeed_svc
from . import recheck as recheck_svc
from . import watchfund

log = logging.getLogger("stall-row.portfolio")

TIERS = ("heartbeat", "recheck", "digest")
DIGEST_RUN_PRICE_UNITS = 5_000
ROUTE_LABEL = "/v1/portfolio-watch"
MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 86_400
DEFAULT_INTERVAL_S = 3_600


class PortfolioError(watchfund.ClusterError):
    pass


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


def expand_family(index_store, operator: str) -> tuple[str, list[str], dict]:
    """(match_kind, routes, entries) for one operator against the
    Index's LATEST snapshot. service_name equality (case-insensitive)
    wins; origin equality is the fallback; no snapshot or no match
    gives ("none", [], {})."""
    if index_store is None:
        return "none", [], {}
    snaps = index_store.snapshots()
    if not snaps:
        return "none", [], {}
    entries = index_store.entries(snaps[-1]["id"])
    by_name = {
        r: row for r, row in entries.items()
        if (row.get("serviceName") or "").lower() == operator.lower()
    }
    if by_name:
        return "service_name", sorted(by_name), by_name
    by_origin = {
        r: row for r, row in entries.items() if _origin_of(r) == operator.rstrip("/")
    }
    if by_origin:
        return "origin", sorted(by_origin), by_origin
    return "none", [], {}


async def _register_inner(
    store: recheck_svc.WatchStore,
    index_store,
    operator: str,
    tier: str,
    webhook_url: str | None,
    email: str | None,
    interval_s: int | None,
    fleet_pass_id: str | None,
    delivery_mode: str | None = None,
    digest_interval: str | None = None,
    payer: str | None = None,
) -> dict:
    """Validate BEFORE storing, then expand the family from the
    Index's current snapshot. An operator with no routes in the
    current snapshot still registers (routes []) — later-added
    family routes are picked up on each evaluation, which is the
    point of the product; the payload says the family is currently
    empty rather than pretending a match."""
    operator = (operator or "").strip()
    if not operator:
        raise PortfolioError("name an operator (service_name or origin)")
    tier = (tier or "heartbeat").strip().lower()
    if tier not in TIERS:
        raise PortfolioError(f"tier must be one of {', '.join(TIERS)}")
    if webhook_url:
        webhook_url = webhook_url.strip()
        await watchfund.ssrf(webhook_url)
    if not webhook_url and not email:
        raise PortfolioError("a webhook_url or an email is required")
    mode, interval = watchfund.normalize_delivery(delivery_mode, digest_interval)
    interval_s = interval_s or DEFAULT_INTERVAL_S
    if interval_s < MIN_INTERVAL_S or interval_s > MAX_INTERVAL_S:
        raise PortfolioError(
            f"interval_s must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S}"
        )
    match_kind, routes, _entries = expand_family(index_store, operator)
    per_route = heartbeat_svc.KNOCK_PRICE_UNITS if tier == "heartbeat" else 5_000
    min_units = DIGEST_RUN_PRICE_UNITS if tier == "digest" else per_route * max(1, len(routes))
    _kind, funding_id = watchfund.resolve_funding(
        store, None, fleet_pass_id, min_units
    )
    reg = store.register_portfolio(
        operator, match_kind, tier, webhook_url, email, interval_s,
        funding_id, routes,
        delivery_mode=mode, digest_interval=interval,
        payer_hint=watchfund.normalize_payer_hint(payer),
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    reg["family_expansion"] = (
        "The family was expanded from the Index's current snapshot "
        "at registration (match_kind says how the operator matched). "
        "Every evaluation re-expands it: routes the operator adds "
        "later join the watch automatically and are named in that "
        "evaluation's new_routes; routes that leave the catalog "
        "drop out."
    )
    reg["rates"] = {
        "heartbeat_knock_units": heartbeat_svc.KNOCK_PRICE_UNITS,
        "recheck_run_units": 5_000,
        "digest_evaluation_units": DIGEST_RUN_PRICE_UNITS,
        "tier": tier,
    }
    reg["note"] = (
        "Portfolio Watch: one registration, one Fleet Pass balance, "
        "one webhook for the operator's whole route family. Debits "
        "are the underlying watch products' own rates for the chosen "
        "tier — no portfolio price exists."
    )
    return reg


async def register(*args, **kwargs) -> dict:
    """Public register: shared-helper failures surface as
    PortfolioError, this service's own contract type."""
    try:
        return await _register_inner(*args, **kwargs)
    except watchfund.ClusterError as e:
        raise PortfolioError(e.message, e.http_status) from e


async def _deliver_family(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    payload: dict,
) -> bool:
    if digest_svc.maybe_queue(
        store, reg, kind="portfolio", reg_id=reg["reg_id"], payload=payload
    ):
        return True
    webhook = reg.get("webhook_url")
    if not webhook:
        return False
    try:
        await watchfund.ssrf(webhook)
        resp = await client.post(webhook, json=payload, follow_redirects=False)
        store.record_delivery(reg["reg_id"], payload["event"], resp.status_code, None)
        return True
    except Exception as e:  # noqa: BLE001
        store.record_delivery(reg["reg_id"], payload["event"], None, type(e).__name__)
        log.warning("portfolio delivery failed for %s: %s", reg["reg_id"], e)
        return True


async def run_reg(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    index_store,
    reg: dict,
) -> dict:
    """One family evaluation at the registration's tier. The family
    is re-expanded FIRST (later-added routes join this very run);
    funding failure stops the run at the unfundable route."""
    fresh = store.get_portfolio_reg(reg["reg_id"])
    if fresh is not None:
        reg = fresh
    if not reg.get("active", True):
        return {
            "reg_id": reg["reg_id"],
            "status": reg.get("last_status") or "paused",
            "detail": "registration is not active; nothing was charged",
            "debited_units": 0,
        }
    match_kind, routes, entries = expand_family(index_store, reg["operator"])
    previous = list(reg.get("routes") or [])
    new_routes = [r for r in routes if r not in previous]
    tier = reg["tier"]
    events: list[dict] = []
    debited = 0
    funding_error: dict | None = None

    if tier == "digest":
        key = f"portfolio\n{reg['reg_id']}"
        stored = store.get(key)
        if stored is None:
            store.save(key, "portfolio", {"entries": entries}, recheck_svc._now_iso())
            store.update_portfolio_run(reg["reg_id"], "baseline", routes=routes)
            return {
                "reg_id": reg["reg_id"], "status": "baseline", "tier": tier,
                "routes": routes, "new_routes": new_routes,
                "events": [], "debited_units": 0, "webhook_push_attempted": False,
            }
        try:
            debit = watchfund.debit(
                store, "fleet_pass", reg["funding_id"],
                DIGEST_RUN_PRICE_UNITS, ROUTE_LABEL,
            )
        except recheck_svc.PackError as e:
            store.update_portfolio_run(reg["reg_id"], "funding_failed")
            return {
                "reg_id": reg["reg_id"], "status": "funding_failed",
                "error": e.message, "code": e.code, "tier": tier,
                "routes": routes, "debited_units": 0,
            }
        _ = debit
        debited = DIGEST_RUN_PRICE_UNITS
        prev_entries = (stored.get("fingerprint") or {}).get("entries") or {}
        diff = indexfeed_svc.diff_entries(prev_entries, entries, limit=500)
        events = diff["events"]
        store.save(key, "portfolio", {"entries": entries}, recheck_svc._now_iso())
    elif tier == "heartbeat":
        for url in routes:
            try:
                debit = watchfund.debit(
                    store, "fleet_pass", reg["funding_id"],
                    heartbeat_svc.KNOCK_PRICE_UNITS, ROUTE_LABEL,
                )
            except recheck_svc.PackError as e:
                funding_error = {"error": e.message, "code": e.code}
                break
            debited += heartbeat_svc.KNOCK_PRICE_UNITS
            try:
                result = await heartbeat_svc.knock(client, url)
            except Exception as e:  # noqa: BLE001 — our probe machinery
                watchfund.refund(store, "fleet_pass", reg["funding_id"], debit,
                                 heartbeat_svc.KNOCK_PRICE_UNITS)
                debited -= heartbeat_svc.KNOCK_PRICE_UNITS
                events.append({"resource": url, "kind": "probe_failed",
                               "error": type(e).__name__})
                continue
            before = store.get_heartbeat_state(reg["reg_id"], url)
            store.set_heartbeat_state(reg["reg_id"], url, result["state"], result)
            if before != result["state"]:
                events.append({
                    "resource": url, "kind": "heartbeat_transition",
                    "from_state": before, "to_state": result["state"],
                    "detail": result,
                })
    else:  # recheck tier
        for url in routes:
            try:
                debit = watchfund.debit(
                    store, "fleet_pass", reg["funding_id"], 5_000, "/v1/recheck"
                )
            except recheck_svc.PackError as e:
                funding_error = {"error": e.message, "code": e.code}
                break
            try:
                result = await recheck_svc.recheck(
                    client, store, url, reg["reg_id"], index_store=index_store
                )
            except (ValueError, UpstreamError) as e:
                watchfund.refund(store, "fleet_pass", reg["funding_id"], debit, 5_000)
                events.append({"resource": url, "kind": "probe_failed",
                               "error": str(e)})
                continue
            debited += 5_000
            if result["status"] in ("BASELINE", "CHANGED"):
                events.append({
                    "resource": url, "kind": result["status"].lower(),
                    "status": result["status"], "changes": result["changes"],
                    "checked_at": result["checked_at"],
                })

    if funding_error is not None:
        store.update_portfolio_run(reg["reg_id"], "funding_failed", routes=routes)
        return {
            "reg_id": reg["reg_id"], "status": "funding_failed",
            **funding_error, "tier": tier, "routes": routes,
            "new_routes": new_routes, "events": events,
            "events_count": len(events), "debited_units": debited,
        }
    status = "events" if events else "no_change"
    store.update_portfolio_run(reg["reg_id"], status, routes=routes)
    pushed = False
    if events or new_routes:
        payload = {
            "event": "portfolio_watch",
            "reg_id": reg["reg_id"],
            "operator": reg["operator"],
            "match_kind": match_kind,
            "tier": tier,
            "routes_count": len(routes),
            "new_routes": new_routes,
            "events": events,
            "events_count": len(events),
            "debited_units": debited,
            "evaluated_at": recheck_svc._now_iso(),
        }
        pushed = await _deliver_family(client, store, reg, payload)
    return {
        "reg_id": reg["reg_id"],
        "status": status,
        "tier": tier,
        "match_kind": match_kind,
        "routes": routes,
        "routes_count": len(routes),
        "new_routes": new_routes,
        "events": events,
        "events_count": len(events),
        "debited_units": debited,
        "delivery_mode": reg.get("delivery_mode") or "instant",
        "webhook_push_attempted": pushed,
    }
