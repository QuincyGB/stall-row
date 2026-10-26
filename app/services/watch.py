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

SSRF discipline (2026-10-25 audit) applies to BOTH URLs: the watched
URL is checked by the recheck probe itself, the webhook URL is
checked at registration AND again at delivery time (DNS can move),
and delivery never follows redirects — a 3xx from the receiver is
recorded as the delivery result, not chased.
"""

from __future__ import annotations

import asyncio
import logging

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
        price = fleetpass_svc.price_for("stall-row", "/v1/recheck")
        assert price is not None
        if fleet_pass["balance_units"] < price:
            raise WatchError("fleet pass balance cannot fund one check", 402)
        kind, funding_id = "fleet_pass", fleet_pass_id.strip()
    reg = store.register_watch(
        url, webhook_url, email, interval, kind, funding_id
    )
    reg["email_delivery"] = (
        "stored only — StallRow has no mail transport; the webhook is "
        "the live delivery channel"
        if email
        else None
    )
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


async def run_watch(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
) -> dict:
    """One funded check for one registration, plus the push when the
    status warrants it. Funding refusals are returned (not raised) as
    {status: funding_failed, error} and recorded on the registration."""
    url = reg["url"]
    debit_id: str | None = None
    try:
        if reg["funding_kind"] == "pack":
            result = await recheck_svc.recheck_with_pack(
                client, store, url, reg["watch_reg_id"], reg["funding_id"]
            )
        else:
            price = fleetpass_svc.price_for("stall-row", "/v1/recheck")
            assert price is not None
            debit = store.debit_pass(
                reg["funding_id"], "stall-row", "/v1/recheck", price
            )
            debit_id = debit["debit_id"]
            result = await recheck_svc.recheck(
                client, store, url, reg["watch_reg_id"]
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
