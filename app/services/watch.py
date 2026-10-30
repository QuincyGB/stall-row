"""Webhook Watch: the push form of the recheck stall.

A registration binds one watched URL to a webhook (and optionally an
email, stored for the record — StallRow has no mail transport, so
the webhook is the live delivery channel and the registration
response says so). A periodic ticker (and POST /v1/watch/run for an
immediate check) runs the funded recheck:

  * funding = a recheck pack bound to the watched URL (one credit
    per run, reserve-then-run in recheck.recheck_with_pack), or
  * funding = a Fleet Pass (one $0.005 recheck debit per run,
    refunded when our side fails).

When a run reports BASELINE (first sight) or CHANGED (a field diff —
amount, payTo, asset, network, description/schema hash, page body),
the change is pushed to the webhook as JSON. UNCHANGED pushes
nothing. A run that cannot be funded marks the registration
funding_failed and pushes nothing — never a free silent run.

A registration may instead carry check_kind="route_verdict": the
standing form of a RouteVerdict check, funded Fleet-Pass-only and
run through RouteVerdict (see the leg's section below). The
recheck leg above is the default and is unchanged by it.

SSRF discipline (2026-10-25 audit) applies to BOTH URLs: the watched
URL is checked by the recheck probe itself, the webhook URL is
checked at registration AND again at delivery time (DNS can move),
and delivery never follows redirects — a 3xx from the receiver is
recorded as the delivery result, not chased.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from ..rpc import UpstreamError
from . import fetch as fetch_svc
from . import fleetpass as fleetpass_svc
from . import recheck as recheck_svc

log = logging.getLogger("stall-row.watch")

MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 86_400
DEFAULT_INTERVAL_S = 3_600
PUSH_STATUSES = ("BASELINE", "CHANGED")

# --- RouteVerdict standing-watch leg (check_kind="route_verdict") ------
# A second check kind on the same registration: instead of probing
# the watched URL itself, a run asks RouteVerdict to check it
# (POST {base}/v1/check {"resource_url": url}) and presents the
# registration's Fleet Pass as the X-Fleet-Pass header — RouteVerdict
# debits the pass itself (its fleet_pass middleware pattern), so
# StallRow never debits locally for this leg. Funding is Fleet Pass
# ONLY: a recheck pack is bound to one endpoint's Recheck credits and
# cannot fund RouteVerdict checks. Change detection is OURS, against
# the summary stored on the registration (RouteVerdict's own diff
# memory is shared across all its callers, so it cannot be this
# watch's baseline): a run pushes CHANGED only when the verdict band
# changes, a payment term (amount / pay_to / network) changes, or
# the score moves by at least SCORE_CHANGE_THRESHOLD points.
CHECK_KINDS = ("recheck", "route_verdict")
SCORE_CHANGE_THRESHOLD = 10
_ROUTE_VERDICT_DEFAULT_BASE = "https://route-verdict-production.up.railway.app"


def _route_verdict_base() -> str:
    """Sibling base URL, mirroring orchestrate.py's pattern: env
    override (WATCH_ROUTE_VERDICT_URL), production default."""
    return (
        os.environ.get("WATCH_ROUTE_VERDICT_URL") or _ROUTE_VERDICT_DEFAULT_BASE
    ).rstrip("/")


class WatchError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise WatchError(str(e)) from e


async def register(
    store: recheck_svc.WatchStore,
    url: str,
    webhook_url: str | None,
    email: str | None,
    interval_s: int | None,
    pack_id: str | None,
    fleet_pass_id: str | None,
    check_kind: str = "recheck",
) -> dict:
    """Validate everything BEFORE storing: both URLs SSRF-checked,
    the funding credential must exist and actually cover this watch.
    Raises WatchError with the refusal; nothing is stored on refusal."""
    url = url.strip()
    await _ssrf(url)
    if webhook_url:
        webhook_url = webhook_url.strip()
        await _ssrf(webhook_url)
    if not webhook_url and not email:
        raise WatchError("a webhook_url or an email is required")
    check_kind = (check_kind or "recheck").strip()
    if check_kind not in CHECK_KINDS:
        raise WatchError(
            f"check_kind must be one of {', '.join(CHECK_KINDS)}"
        )
    if check_kind == "route_verdict" and pack_id:
        raise WatchError(
            "check_kind=route_verdict cannot be funded by a recheck "
            "pack: packs are bound to one endpoint's Recheck credits "
            "and cannot fund RouteVerdict checks — fund this watch "
            "with a Fleet Pass instead (fleet_pass_id)"
        )
    if bool(pack_id) == bool(fleet_pass_id):
        raise WatchError("fund with exactly one of pack_id or fleet_pass_id")
    interval = interval_s or DEFAULT_INTERVAL_S
    if interval < MIN_INTERVAL_S or interval > MAX_INTERVAL_S:
        raise WatchError(
            f"interval_s must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S}"
        )
    if pack_id:
        pack = store.get_pack(pack_id.strip())
        if pack is None:
            raise WatchError("unknown pack_id", 404)
        if pack["url"] != url:
            raise WatchError("this pack is bound to a different endpoint URL", 403)
        if pack["expired"]:
            raise WatchError("this pack has expired", 410)
        kind, funding_id = "pack", pack_id.strip()
    else:
        assert fleet_pass_id is not None
        fleet_pass = store.get_pass(fleet_pass_id.strip())
        if fleet_pass is None:
            raise WatchError("unknown fleet_pass_id", 404)
        if fleet_pass["expired"]:
            raise WatchError("this fleet pass has expired", 410)
        if check_kind == "route_verdict":
            price = fleetpass_svc.price_for("route-verdict", "/v1/check")
        else:
            price = fleetpass_svc.price_for("stall-row", "/v1/recheck")
        assert price is not None
        if fleet_pass["balance_units"] < price:
            raise WatchError("fleet pass balance cannot fund one check", 402)
        kind, funding_id = "fleet_pass", fleet_pass_id.strip()
    reg = store.register_watch(
        url, webhook_url, email, interval, kind, funding_id,
        check_kind=check_kind,
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
    if check_kind == "route_verdict":
        reg["note"] = (
            "check_kind=route_verdict: each run costs one $0.05 "
            "fleet-pass RouteVerdict check (50000 units, debited by "
            "RouteVerdict itself via the X-Fleet-Pass header — "
            "StallRow does not debit locally). BASELINE and CHANGED "
            "runs push to the webhook; UNCHANGED pushes nothing. A "
            f"score move of at least {SCORE_CHANGE_THRESHOLD} points "
            "counts as CHANGED."
        )
    else:
        reg["note"] = (
            "Each run spends from the funding credential (one pack credit, "
            "or one $0.005 fleet-pass recheck debit). BASELINE and CHANGED "
            "runs push to the webhook; UNCHANGED pushes nothing."
        )
    return reg


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    result: dict,
) -> None:
    webhook = reg.get("webhook_url")
    if not webhook:
        return
    payload = {
        "event": result["status"],
        "watch_reg_id": reg["watch_reg_id"],
        "watch_id": result["watch_id"],
        "url": reg["url"],
        "kind": result["kind"],
        "changes": result["changes"],
        "current": result["current"],
        "checked_at": result["checked_at"],
        "summary": result["summary"],
    }
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook)
        resp = await client.post(webhook, json=payload, follow_redirects=False)
        store.record_delivery(
            reg["watch_reg_id"], result["status"], resp.status_code, None
        )
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded, not fatal
        store.record_delivery(
            reg["watch_reg_id"], result["status"], None, type(e).__name__
        )
        log.warning("watch delivery failed for %s: %s", reg["watch_reg_id"], e)


def _rv_summary(body: dict) -> dict:
    """Our standing-watch summary of one RouteVerdict check: the
    fields this leg diffs on, lifted from the response's verdict /
    score and its terms object (amount_units / pay_to / network —
    the exact names checker.py assembles)."""
    terms = body.get("terms") if isinstance(body.get("terms"), dict) else {}
    return {
        "verdict": body.get("verdict"),
        "score": body.get("score"),
        "amount": terms.get("amount_units"),
        "pay_to": terms.get("pay_to"),
        "network": terms.get("network"),
    }


def _rv_changes(before: dict, after: dict) -> list[dict]:
    """Field/before/after diff between two stored summaries. Score
    counts only at or past SCORE_CHANGE_THRESHOLD points; every
    other field counts on any change."""
    changes: list[dict] = []
    for field in ("verdict", "amount", "pay_to", "network"):
        if before.get(field) != after.get(field):
            changes.append(
                {"field": field, "before": before.get(field), "after": after.get(field)}
            )
    b_score, a_score = before.get("score"), after.get("score")
    if (
        isinstance(b_score, (int, float))
        and isinstance(a_score, (int, float))
        and abs(a_score - b_score) >= SCORE_CHANGE_THRESHOLD
    ):
        changes.append({"field": "score", "before": b_score, "after": a_score})
    return changes


async def _run_route_verdict_watch(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
) -> dict:
    """One RouteVerdict standing check. RouteVerdict debits the pass
    itself on success, so there is NO local debit (and nothing to
    refund) on this leg. A 402 from RouteVerdict is its pass refusal
    (dry / unknown / expired pass) — funding_failed, nothing pushed.
    Any other HTTP error or a transport failure is probe_failed,
    nothing pushed, and the stored summary is left untouched."""
    # Re-read the registration: its last_summary is this leg's memory
    # and the caller's copy may predate the previous run.
    fresh = store.get_watch_reg(reg["watch_reg_id"])
    if fresh is not None:
        reg = fresh
    try:
        resp = await client.post(
            _route_verdict_base() + "/v1/check",
            json={"resource_url": reg["url"]},
            headers={"X-Fleet-Pass": reg["funding_id"]},
        )
    except httpx.HTTPError as e:
        store.update_watch_run(reg["watch_reg_id"], "probe_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "probe_failed",
            "error": str(e),
        }
    if resp.status_code == 402:
        error = "route-verdict refused the fleet pass (HTTP 402)"
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                error = str(body["error"])
        except ValueError:
            pass
        store.update_watch_run(reg["watch_reg_id"], "funding_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "funding_failed",
            "error": error,
            "http_status": 402,
        }
    if resp.status_code != 200:
        store.update_watch_run(reg["watch_reg_id"], "probe_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "probe_failed",
            "error": f"route-verdict returned HTTP {resp.status_code}",
            "http_status": resp.status_code,
        }
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        store.update_watch_run(reg["watch_reg_id"], "probe_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "probe_failed",
            "error": "route-verdict returned a non-JSON response",
        }
    summary = _rv_summary(body)
    checked_at = body.get("checked_at") or recheck_svc._now_iso()
    before = reg.get("last_summary")
    if before is None:
        status, changes = "BASELINE", []
        summary_text = (
            f"RouteVerdict baseline for this watch: verdict "
            f"{summary['verdict']}, score {summary['score']}."
        )
    else:
        changes = _rv_changes(before, summary)
        if changes:
            status = "CHANGED"
            fields = ", ".join(c["field"] for c in changes)
            summary_text = f"RouteVerdict check changed: {fields}."
        else:
            status = "UNCHANGED"
            summary_text = (
                "No material RouteVerdict change since the previous "
                f"check (score moves under {SCORE_CHANGE_THRESHOLD} "
                "points do not count)."
            )
    # Every successful run refreshes the stored summary — the next
    # run diffs against this one.
    store.update_watch_run(
        reg["watch_reg_id"], status, last_summary=summary
    )
    result = {
        "watch_id": reg["watch_reg_id"],
        "url": reg["url"],
        "kind": "route_verdict",
        "status": status,
        "changes": changes,
        "current": summary,
        "checked_at": checked_at,
        "summary": summary_text,
    }
    delivered = False
    if status in PUSH_STATUSES and reg.get("webhook_url"):
        await _deliver(client, store, reg, result)
        delivered = True
    return {
        "watch_reg_id": reg["watch_reg_id"],
        "check_kind": "route_verdict",
        "status": status,
        "changes": changes,
        "current": summary,
        "checked_at": checked_at,
        "score_change_threshold": SCORE_CHANGE_THRESHOLD,
        "webhook_push_attempted": delivered,
    }


async def run_watch(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    index_store=None,
) -> dict:
    """One funded check for one registration, plus the push when the
    status warrants it. Funding refusals are returned (not raised) as
    {status: funding_failed, error} and recorded on the registration."""
    if reg.get("check_kind") == "route_verdict":
        return await _run_route_verdict_watch(client, store, reg)
    url = reg["url"]
    debit_id: str | None = None
    try:
        if reg["funding_kind"] == "pack":
            result = await recheck_svc.recheck_with_pack(
                client, store, url, reg["watch_reg_id"], reg["funding_id"],
                index_store=index_store,
            )
        else:
            price = fleetpass_svc.price_for("stall-row", "/v1/recheck")
            assert price is not None
            debit = store.debit_pass(
                reg["funding_id"], "stall-row", "/v1/recheck", price
            )
            debit_id = debit["debit_id"]
            result = await recheck_svc.recheck(
                client, store, url, reg["watch_reg_id"],
                index_store=index_store,
            )
    except recheck_svc.PackError as e:
        store.update_watch_run(reg["watch_reg_id"], "funding_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "funding_failed",
            "error": e.message,
            "code": e.code,
        }
    except (ValueError, UpstreamError) as e:
        if debit_id:
            store.refund_debit(debit_id)
        store.update_watch_run(reg["watch_reg_id"], "probe_failed")
        return {
            "watch_reg_id": reg["watch_reg_id"],
            "status": "probe_failed",
            "error": str(e),
        }
    store.update_watch_run(reg["watch_reg_id"], result["status"])
    delivered = False
    if result["status"] in PUSH_STATUSES and reg.get("webhook_url"):
        await _deliver(client, store, reg, result)
        delivered = True
    return {
        "watch_reg_id": reg["watch_reg_id"],
        "status": result["status"],
        "changes": result["changes"],
        "checked_at": result["checked_at"],
        "webhook_push_attempted": delivered,
    }
