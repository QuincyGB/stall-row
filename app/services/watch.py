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

Contract Watch (Next-20 #14) is a registration option on the
recheck leg, watch_focus="schema": the change detector keys ONLY
on the target's output-schema fingerprint — the recheck
fingerprint's schema_hash, i.e. the sha256 of the canonical
serialization of the output schema the target DECLARES in its
unpaid 402 challenge (extensions.bazaar.info.output.schema; see
services/fingerprint_spec.py). It is not a hash of an observed
paid response — Recheck never pays the targets it probes. Any
fingerprint change pushes a SCHEMA_CHANGED alert (before/after
fingerprints + the schema field-level diff) even when price,
payTo, verdict and score are all flat; changes to any OTHER field
alone push nothing on this focus. The generic recheck leg already
diffed schema_hash as one field among many (a schema-only drift
did produce a generic CHANGED push); Contract Watch makes schema
drift a first-class, schema-only alert with its own event name.
Funding is identical to the recheck leg: one pack credit or one
$0.005 Fleet Pass recheck debit per run. The RouteVerdict leg has
no schema-fingerprint machinery, so watch_focus="schema" is
recheck-leg only and registration refuses it with route_verdict.

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
WATCH_FOCI = ("all", "schema")
SCHEMA_CHANGED = "SCHEMA_CHANGED"
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
    watch_focus: str = "all",
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
    watch_focus = (watch_focus or "all").strip()
    if watch_focus not in WATCH_FOCI:
        raise WatchError(
            f"watch_focus must be one of {', '.join(WATCH_FOCI)}"
        )
    if watch_focus == "schema" and check_kind == "route_verdict":
        raise WatchError(
            "watch_focus=schema (Contract Watch) is recheck-leg only: "
            "the RouteVerdict leg carries no output-schema fingerprint "
            "to key on — register with check_kind=recheck"
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
        check_kind=check_kind, watch_focus=watch_focus,
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
    elif watch_focus == "schema":
        reg["note"] = (
            "Contract Watch (watch_focus=schema): each run spends from "
            "the funding credential (one pack credit, or one $0.005 "
            "fleet-pass recheck debit — the listed Recheck price; there "
            "is no separate Contract Watch price). The first run pushes "
            "BASELINE with the recorded output-schema fingerprint; a "
            "later run pushes SCHEMA_CHANGED only when that fingerprint "
            "(the declared output schema's hash) changes — regardless "
            "of price/payTo/score movement — and pushes nothing when "
            "only other fields change."
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
    if "schema_fingerprint" in result:
        # Contract Watch pushes name the schema change explicitly:
        # before/after fingerprints + the schema field-level diff.
        payload["watch_focus"] = result.get("watch_focus", "schema")
        payload["schema_fingerprint"] = result["schema_fingerprint"]
        payload["field_changes"] = result.get("field_changes", [])
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


async def _run_schema_focus(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    reg: dict,
    result: dict,
) -> dict:
    """Contract Watch detector, applied to one COMPLETED funded
    recheck result (funding failures never reach here — run_watch's
    existing funding_failed / probe_failed paths return first, with
    the debit/refund discipline unchanged and the stored baseline
    untouched). The registration's own baseline fingerprint lives in
    its last_summary ({"schema_fingerprint": ...}); a target with no
    declared output schema fingerprints as None, and a later
    appearance/disappearance of a schema is a change like any
    other. Every successful run refreshes the stored baseline."""
    fresh = store.get_watch_reg(reg["watch_reg_id"])
    if fresh is not None:
        reg = fresh
    current_fp = result["current"].get("schema_hash")
    stored = reg.get("last_summary")
    has_baseline = isinstance(stored, dict) and "schema_fingerprint" in stored
    before_fp = stored.get("schema_fingerprint") if has_baseline else None
    schema_changes = [
        c for c in result.get("changes", []) if c.get("field") == "schema_hash"
    ]
    schema_field_changes = [
        c
        for c in result.get("field_changes", [])
        if c.get("path") == "schema_hash"
    ]
    if not has_baseline:
        status = "BASELINE"
        summary_text = (
            "Contract Watch baseline recorded: output-schema "
            f"fingerprint {current_fp}."
        )
        schema_changes, schema_field_changes = [], []
    elif current_fp != before_fp:
        status = SCHEMA_CHANGED
        if not schema_changes:
            schema_changes = [
                {"field": "schema_hash", "before": before_fp, "after": current_fp}
            ]
        if not schema_field_changes:
            schema_field_changes = [
                {
                    "path": "schema_hash",
                    "op": "value_changed",
                    "before": before_fp,
                    "after": current_fp,
                    "severity": "medium",
                    "category": "schema",
                }
            ]
        summary_text = (
            "Contract Watch: the target's declared output schema "
            f"changed (fingerprint {before_fp} -> {current_fp}); "
            "alerting on the schema change regardless of price, "
            "payTo, verdict or score movement."
        )
    else:
        status = "UNCHANGED"
        summary_text = (
            "Contract Watch: output-schema fingerprint unchanged "
            f"({current_fp}); other-field changes alone do not push "
            "on a schema-focused watch."
        )
    store.update_watch_run(
        reg["watch_reg_id"],
        status,
        last_summary={"schema_fingerprint": current_fp},
    )
    delivered = False
    if status in ("BASELINE", SCHEMA_CHANGED) and reg.get("webhook_url"):
        await _deliver(
            client,
            store,
            reg,
            {
                "watch_id": result["watch_id"],
                "url": reg["url"],
                "kind": result["kind"],
                "status": status,
                "changes": schema_changes,
                "field_changes": schema_field_changes,
                "current": result["current"],
                "checked_at": result["checked_at"],
                "summary": summary_text,
                "watch_focus": "schema",
                "schema_fingerprint": {
                    "before": before_fp if has_baseline else None,
                    "after": current_fp,
                },
            },
        )
        delivered = True
    return {
        "watch_reg_id": reg["watch_reg_id"],
        "watch_focus": "schema",
        "status": status,
        "changes": schema_changes,
        "field_changes": schema_field_changes,
        "schema_fingerprint": {
            "before": before_fp if has_baseline else None,
            "after": current_fp,
        },
        "recheck_status": result["status"],
        "checked_at": result["checked_at"],
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
    if reg.get("watch_focus") == "schema":
        return await _run_schema_focus(client, store, reg, result)
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
