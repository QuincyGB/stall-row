"""Shared funding + delivery-shape helpers for the watch cluster
(Next-20 #9/#10/#11/#12/#20).

Funding: the cluster's products are priced in UNITS ($0.001 knock,
$0.005 event) and funded from a prepaid credential — a Fleet Pass
(its balance IS units: store.debit_pass) or a Recheck pack (its
balance is credits: store.debit_pack_units, fractional-credit
accounting at exactly PACK_PRICE_UNITS / PACK_CREDITS = 4000 units
per credit, so a $0.001 knock never overcharges). Exactly one
credential funds a registration; validation happens BEFORE anything
is stored, mirroring watch.register. A debit that cannot be covered
raises recheck.PackError and the caller marks the registration
funding_failed / paused — balances never go negative and nothing
ever falls back to a per-call x402 charge.

Delivery shape (Item 20): registrations choose delivery_mode
"instant" (default — today's per-event webhook push, unchanged) or
"digest" with digest_interval "hourly" | "daily". Digest events are
queued in the store's digest_queue and flushed as ONE batched
delivery per interval by services/digest.py. Debits always happen
per underlying event/knock at the product's rate, whichever mode —
digest changes delivery shape only, never pricing.
"""

from __future__ import annotations

import asyncio

from . import fetch as fetch_svc
from . import recheck as recheck_svc

DELIVERY_MODES = ("instant", "digest")
DIGEST_INTERVALS = ("hourly", "daily")
DIGEST_INTERVAL_S = {"hourly": 3600, "daily": 86400}


class ClusterError(Exception):
    """A registration/run refusal in the watch family's shape."""

    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def normalize_payer_hint(raw) -> str | None:
    """The Item 5 rider identity: a presented payer (deep-link
    ?payer= value or body field), trimmed and length-capped, or
    None when nothing usable was presented (absence changes
    nothing)."""
    if not isinstance(raw, str):
        return None
    hint = raw.strip()
    if not hint:
        return None
    return hint[:256]


def normalize_delivery(delivery_mode, digest_interval) -> tuple[str, str | None]:
    """Validate a delivery-shape choice. Instant is the default and
    carries no interval; digest requires hourly|daily (default
    daily when a digest registration names none)."""
    mode = (delivery_mode or "instant").strip().lower()
    if mode not in DELIVERY_MODES:
        raise ClusterError(
            f"delivery_mode must be one of {', '.join(DELIVERY_MODES)}"
        )
    if mode == "instant":
        return "instant", None
    interval = (digest_interval or "daily").strip().lower()
    if interval not in DIGEST_INTERVALS:
        raise ClusterError(
            f"digest_interval must be one of {', '.join(DIGEST_INTERVALS)}"
        )
    return "digest", interval


async def ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise ClusterError(str(e)) from e


def resolve_funding(
    store: recheck_svc.WatchStore,
    pack_id: str | None,
    fleet_pass_id: str | None,
    min_units: int,
    *,
    pack_url: str | None = None,
) -> tuple[str, str]:
    """Validate exactly one funding credential that can cover
    `min_units` of spend. Returns (kind, funding_id) where kind is
    "pack" or "fleet_pass". When `pack_url` is given, a pack must be
    bound to exactly that URL (the Recheck pack discipline — a pack
    funds checks of its own endpoint; unit-priced cluster products
    scoped to that endpoint inherit the same binding). Raises
    ClusterError; nothing is stored on refusal."""
    pack_id = (pack_id or "").strip() or None
    fleet_pass_id = (fleet_pass_id or "").strip() or None
    if bool(pack_id) == bool(fleet_pass_id):
        raise ClusterError("fund with exactly one of pack_id or fleet_pass_id")
    if pack_id:
        pack = store.get_pack(pack_id)
        if pack is None:
            raise ClusterError("unknown pack_id", 404)
        if pack_url is not None and pack["url"] != pack_url:
            raise ClusterError(
                "this pack is bound to a different endpoint URL", 403
            )
        if pack["expired"]:
            raise ClusterError("this pack has expired", 410)
        spendable = store.pack_spendable_units(pack_id)
        if spendable is None or spendable < min_units:
            raise ClusterError("pack balance cannot fund this watch", 402)
        return "pack", pack_id
    assert fleet_pass_id is not None
    fleet_pass = store.get_pass(fleet_pass_id)
    if fleet_pass is None:
        raise ClusterError("unknown fleet_pass_id", 404)
    if fleet_pass["expired"]:
        raise ClusterError("this fleet pass has expired", 410)
    if fleet_pass["balance_units"] < min_units:
        raise ClusterError("fleet pass balance cannot fund this watch", 402)
    return "fleet_pass", fleet_pass_id


def debit(
    store: recheck_svc.WatchStore,
    funding_kind: str,
    funding_id: str,
    units: int,
    route_label: str,
) -> dict:
    """Debit `units` from the registration's credential at the
    product's spec rate. Fleet Pass debits carry the product's route
    label in the pass ledger; pack debits use fractional-credit
    accounting. Raises recheck.PackError when unfundable."""
    if funding_kind == "fleet_pass":
        return store.debit_pass(funding_id, "stall-row", route_label, units)
    return store.debit_pack_units(funding_id, units)


def refund(
    store: recheck_svc.WatchStore,
    funding_kind: str,
    funding_id: str,
    debit_result: dict,
    units: int,
) -> None:
    """Undo one debit after our side failed to deliver (fleet rule:
    never charge for our failure)."""
    if funding_kind == "fleet_pass":
        debit_id = debit_result.get("debit_id")
        if debit_id:
            store.refund_debit(debit_id)
    else:
        store.refund_pack_units(funding_id, units)


def manage(
    store: recheck_svc.WatchStore,
    table: str,
    getter,
    reg_id: str,
    action: str,
) -> dict:
    """Console-free pause/resume/cancel for a cluster registration,
    in the Index Subscription v2 style: the registration id is the
    credential, pause holds state, cancel is terminal. `getter` is
    the store's view function for the registration."""
    reg = getter((reg_id or "").strip())
    if reg is None:
        raise ClusterError("unknown reg_id", 404)
    key = reg.get("reg_id") or reg.get("watch_reg_id")
    if action == "pause":
        if reg.get("last_status") == "cancelled":
            raise ClusterError("a cancelled registration cannot be paused", 409)
        store.set_reg_active(table, key, False, "paused")
    elif action == "resume":
        if reg.get("last_status") == "cancelled":
            raise ClusterError(
                "a cancelled registration cannot be resumed; register a new one",
                409,
            )
        store.set_reg_active(table, key, True, "resumed")
    elif action == "cancel":
        store.set_reg_active(table, key, False, "cancelled")
    else:  # pragma: no cover — the HTTP layer only sends the three
        raise ClusterError(f"unknown management action {action!r}")
    out = getter(key)
    assert out is not None
    return out


def spendable_units(
    store: recheck_svc.WatchStore, funding_kind: str, funding_id: str
) -> int | None:
    if funding_kind == "fleet_pass":
        fleet_pass = store.get_pass(funding_id)
        return None if fleet_pass is None else int(fleet_pass["balance_units"])
    return store.pack_spendable_units(funding_id)
