"""Pre-Trade Guard: POST /v1/pre-trade-guard — should you pay this payTo?

Everything else on the row vets a seller after you find them; this
route runs BEFORE the payment, on the address the payment is about
to go to. Three legs, all local or cheap reads — no catalog pull and
no sibling call runs inside the paid request:

  index     — the Verdict Index snapshot store (the same sqlite file
              the recheck packs live in): this payTo's listing
              history across the snapshots held — first seen,
              listings now, payTo rotations in or out, listings
              added/removed across the window.
  sanctions — the bundled OFAC SDN snapshot, exact match, via
              services/verdict.py (the wallet-verdict stall's screen;
              a missing/empty snapshot degrades to "unknown", never
              a fabricated "clean").
  wallet    — the payTo's own Base age/activity (first transaction,
              outgoing count) from the explorer + RPC, degrading to
              "unknown" per field like every other chain read here.

Combination mirrors Token Verdict's /v1/pre-trade contract: risk
points add up (bands PROCEED < 35, CAUTION 35-64, ABORT >= 65),
hard aborts override the arithmetic (a sanctions match; the named
resource's catalogued payTo being a DIFFERENT address than the one
presented), and any leg that could not be evaluated caps the
verdict at CAUTION — an unexamined signal is never called safe.
A payTo with no catalog history still gets a verdict: first-seen
is itself a scored signal, and the wallet/sanctions legs still run.

The catalog window is thin by construction — the store holds the
newest 3 snapshots, ~6h apart — so "first seen" always means
"first seen in the window held", and the response says so.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc
from . import verdict as verdict_svc
from .indexfeed import IndexStore

BASE_NETWORK = "eip155:8453"

NOT_CHECKED = (
    "Not checked: the identity or intent of the payTo's operator; whether "
    "goods delivered will match their description (that is a delivery "
    "question — see the recheck stall and Settled delivery stamps); "
    "wallet history on chains other than Base; and listings too new to "
    "appear in any catalog snapshot held."
)

LIMITS = (
    "Listing history covers only the catalog snapshots this service holds "
    "(the newest 3, refreshed about every 6 hours) — a payTo older than "
    "that window can still read as first-seen here. Sanctions matching is "
    "exact-address only against the bundled snapshot (see its date in the "
    "sanctions block). Wallet age/activity are Base-only and degrade to "
    "unknown when the explorer is unreachable. This is an automated "
    "pre-payment screen, not a guarantee about the payee."
)


def _band(risk: int) -> str:
    return "ABORT" if risk >= 65 else ("CAUTION" if risk >= 35 else "PROCEED")


def _amount_str(amount) -> str | None:
    """Normalize a presented amount for comparison with the catalog's
    smallest-unit strings: ints and digit strings compare directly;
    anything else is incomparable (None) and simply not scored."""
    if amount is None:
        return None
    if isinstance(amount, int):
        return str(amount)
    if isinstance(amount, str) and amount.strip().isdigit():
        return str(int(amount.strip()))
    return None


def index_history(store: IndexStore, pay_to: str, resource: str | None = None) -> dict:
    """One payTo's catalog history across the snapshots held. Pure
    read (no network); an empty store is status "unknown", never an
    invented "no history"."""
    target = pay_to.lower()
    snaps = store.snapshots()
    if not snaps:
        return {
            "status": "unknown",
            "snapshots_held": 0,
            "detail": (
                "No catalog snapshots are stored yet, so listing history "
                "could not be checked."
            ),
            "window": None,
            "ever_listed": False,
            "listed_now": 0,
            "first_seen_at": None,
            "first_seen_is_newest": False,
            "history_observed_seconds": None,
            "rotations_in": [],
            "rotations_out": [],
            "listings_added_in_window": 0,
            "listings_removed_in_window": 0,
            "networks": [],
            "calls30d": None,
            "payers30d": None,
            "resource": None,
        }

    per_snap: list[tuple[dict, dict, dict]] = []
    for snap in snaps:
        entries = store.entries(snap["id"])
        mine = {
            r: row
            for r, row in entries.items()
            if (row.get("payTo") or "").lower() == target
        }
        per_snap.append((snap, entries, mine))

    newest_snap, _newest_entries, newest_mine = per_snap[-1]
    first_seen_snap = None
    for snap, _entries, mine in per_snap:
        if mine:
            first_seen_snap = snap
            break
    ever_listed = first_seen_snap is not None

    rotations_in: list[dict] = []
    rotations_out: list[dict] = []
    for (s_old, e_old, _mo), (s_new, e_new, _mn) in zip(per_snap, per_snap[1:]):
        for r, nrow in e_new.items():
            orow = e_old.get(r)
            if orow is None:
                continue
            o = (orow.get("payTo") or "").lower()
            n = (nrow.get("payTo") or "").lower()
            if o == n:
                continue
            if n == target:
                rotations_in.append(
                    {"resource": r, "from_pay_to": orow.get("payTo"), "at": s_new["captured_at"]}
                )
            if o == target:
                rotations_out.append(
                    {"resource": r, "to_pay_to": nrow.get("payTo"), "at": s_new["captured_at"]}
                )

    oldest_mine = per_snap[0][2]
    added = len(set(newest_mine) - set(oldest_mine))
    removed = len(set(oldest_mine) - set(newest_mine))

    def _sum(key: str) -> int | None:
        vals = [row.get(key) for row in newest_mine.values() if row.get(key) is not None]
        return sum(vals) if vals else None

    resource_info = None
    if resource:
        history = []
        for snap, entries, _mine in per_snap:
            row = entries.get(resource)
            if row is not None:
                history.append({"at": snap["captured_at"], "pay_to": row.get("payTo")})
        current = per_snap[-1][1].get(resource)
        distinct = {(h["pay_to"] or "").lower() for h in history}
        resource_info = {
            "resource": resource,
            "seen_in_window": bool(history),
            "listed_now": current is not None,
            "current_pay_to": current.get("payTo") if current else None,
            "pay_to_matches": (
                ((current.get("payTo") or "").lower() == target) if current else None
            ),
            "listed_amount": current.get("amount") if current else None,
            "service_name": current.get("serviceName") if current else None,
            "pay_to_history": history,
            "rotated_in_window": len(distinct) > 1,
        }

    return {
        "status": "ok",
        "snapshots_held": len(snaps),
        "detail": (
            f"History read from {len(snaps)} catalog snapshot(s) held, "
            f"{snaps[0]['captured_at']} to {snaps[-1]['captured_at']}."
        ),
        "window": {
            "oldest_snapshot_at": snaps[0]["captured_at"],
            "newest_snapshot_at": snaps[-1]["captured_at"],
        },
        "ever_listed": ever_listed,
        "listed_now": len(newest_mine),
        "first_seen_at": first_seen_snap["captured_at"] if first_seen_snap else None,
        "first_seen_is_newest": bool(
            first_seen_snap and first_seen_snap["id"] == newest_snap["id"]
        ),
        "history_observed_seconds": (
            int(newest_snap["captured_epoch"] - first_seen_snap["captured_epoch"])
            if first_seen_snap
            else None
        ),
        "rotations_in": rotations_in,
        "rotations_out": rotations_out,
        "listings_added_in_window": added,
        "listings_removed_in_window": removed,
        "networks": sorted(
            {row.get("network") for row in newest_mine.values() if row.get("network")}
        ),
        "calls30d": _sum("calls30d"),
        "payers30d": _sum("payers30d"),
        "resource": resource_info,
    }


async def pre_trade_guard(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    store: IndexStore,
    *,
    pay_to: str,
    amount=None,
    network: str | None = None,
    resource: str | None = None,
) -> dict:
    sanctions = verdict_svc.sanctions_check(pay_to)
    index = index_history(store, pay_to, resource)

    # --- wallet leg (Base only; degrades per source) -------------------
    wallet: dict = {
        "status": "unknown",
        "age_days": None,
        "first_activity": "unknown",
        "outgoing_tx_count_nonce": None,
        "has_activity": None,
        "detail": None,
    }
    age_days: int | None = None
    nonce: int | None = None
    if (network or BASE_NETWORK) != BASE_NETWORK:
        wallet["detail"] = (
            "Wallet age/activity are read on Base only; the network named "
            "is not Base, so this leg was not checked."
        )
    else:
        try:
            txs_asc = await scout.transactions(pay_to, sort="asc", limit=1)
        except Exception:  # noqa: BLE001 — explorer failure degrades, never fabricates
            txs_asc = None
        try:
            nonce = await rpc.eth_tx_count(pay_to)
        except Exception:  # noqa: BLE001 — nonce is a soft signal
            nonce = None
        wallet["outgoing_tx_count_nonce"] = nonce
        if txs_asc is not None:
            wallet["has_activity"] = bool(txs_asc)
            first_dt = verdict_svc._parse_ts(txs_asc[0]) if txs_asc else None
            if first_dt is not None:
                wallet["first_activity"] = first_dt.isoformat()
                age_days = max(0, (datetime.now(timezone.utc) - first_dt).days)
                wallet["age_days"] = age_days
        if txs_asc is not None or nonce is not None:
            wallet["status"] = "ok"
        else:
            wallet["detail"] = (
                "Explorer and RPC both failed to answer, so wallet "
                "age/activity could not be checked."
            )

    # --- scoring ---------------------------------------------------------
    reasons: list[dict] = []
    risk = 0
    hard_abort = False

    def add(name: str, points: int, detail: str) -> None:
        nonlocal risk
        reasons.append({"name": name, "points": points, "detail": detail})
        risk += points

    if sanctions["status"] == "matched":
        hard_abort = True
        add("OFAC SDN match", 100, sanctions["detail"])
    elif sanctions["status"] == "unknown":
        reasons.append(
            {"name": "sanctions check unavailable", "points": 0, "detail": sanctions["detail"]}
        )

    rinfo = index.get("resource")
    if index["status"] == "unknown":
        reasons.append(
            {"name": "catalog history unavailable", "points": 0, "detail": index["detail"]}
        )
    else:
        if rinfo is not None:
            if rinfo["pay_to_matches"] is False:
                hard_abort = True
                add(
                    "resource payTo mismatch",
                    85,
                    "The named resource's catalogued payTo is "
                    f"{rinfo['current_pay_to']}, not the address presented — "
                    "pay the catalogued address or do not pay.",
                )
            elif rinfo["pay_to_matches"] is True:
                reasons.append(
                    {
                        "name": "resource payTo confirmed",
                        "points": 0,
                        "detail": "The named resource's catalogued payTo is the address presented.",
                    }
                )
            elif rinfo["seen_in_window"]:
                add(
                    "named resource left the catalog",
                    15,
                    "The named resource appears in an earlier snapshot held "
                    "but not in the newest — it was delisted or rotated "
                    "inside the window.",
                )
            else:
                add(
                    "named resource not in catalog",
                    10,
                    "The named resource appears in no snapshot held — it "
                    "may be unindexed, brand new, or not an x402 catalog "
                    "resource at all.",
                )

        if not index["ever_listed"]:
            add(
                "no catalog track record",
                30,
                "This payTo appears in none of the catalog snapshots held "
                "— there is no listing history to inspect. That is "
                "first-seen, not proof of bad intent, but nothing here "
                "vouches for it either.",
            )
        else:
            if index["listed_now"] == 0:
                add(
                    "listings have left the catalog",
                    15,
                    "This payTo had listings in an earlier snapshot held "
                    "but has none in the newest.",
                )
            elif index["first_seen_is_newest"] and index["snapshots_held"] > 1:
                add(
                    "first seen in newest snapshot",
                    20,
                    "This payTo's listings were first observed in the "
                    "newest snapshot held — at most one refresh window "
                    "(~6h) of catalog history.",
                )
            elif not index["rotations_in"] and not index["rotations_out"]:
                reasons.append(
                    {
                        "name": "catalog track record",
                        "points": 0,
                        "detail": (
                            f"PayTo appears in the snapshots held with "
                            f"{index['listed_now']} listing(s) in the newest "
                            "and no payTo rotations in the window."
                        ),
                    }
                )
            if rinfo is not None and rinfo["rotated_in_window"] and rinfo["pay_to_matches"]:
                add(
                    "this listing's payTo rotated here",
                    30,
                    "The named resource's payTo changed inside the window "
                    "and now points at the address presented. PayTo "
                    "rotation is sometimes legitimate key hygiene and is "
                    "also the classic rug shape — verify out-of-band "
                    "before paying.",
                )
            elif index["rotations_in"]:
                add(
                    "payTo rotation into this address",
                    20,
                    f"{len(index['rotations_in'])} listing(s) rotated "
                    "their payTo to this address inside the window.",
                )
            if index["rotations_out"]:
                add(
                    "listings rotated away from this payTo",
                    10,
                    f"{len(index['rotations_out'])} listing(s) that paid "
                    "this address in an earlier snapshot now pay a "
                    "different address.",
                )

        presented = _amount_str(amount)
        if (
            presented is not None
            and rinfo is not None
            and rinfo["listed_amount"] is not None
            and presented != str(rinfo["listed_amount"])
        ):
            add(
                "amount differs from catalogued price",
                10,
                f"The amount presented ({presented}) differs from the "
                f"named resource's catalogued amount "
                f"({rinfo['listed_amount']}).",
            )

    if wallet["status"] == "unknown":
        reasons.append(
            {
                "name": "wallet check unavailable",
                "points": 0,
                "detail": wallet["detail"] or "Wallet age/activity could not be checked.",
            }
        )
    else:
        if wallet["has_activity"] is False and nonce == 0:
            add(
                "no on-chain activity at all",
                20,
                "The explorer shows no transactions for this address and "
                "its outgoing count is zero — a payTo that has never "
                "moved on Base.",
            )
        if age_days is not None:
            if age_days < 1:
                add("brand-new wallet", 35, "First observed transaction is less than a day old.")
            elif age_days < 7:
                add("very young wallet", 20, f"First observed transaction is {age_days} day(s) old.")
            elif age_days < 30:
                add("young wallet", 10, f"First observed transaction is {age_days} day(s) old.")
            if nonce is not None and nonce <= 3 and age_days < 30:
                add(
                    "minimal outgoing activity",
                    10,
                    f"Only {nonce} outgoing transaction(s) ever.",
                )

    risk = max(0, min(100, risk))
    unknown_legs = [
        name
        for name, leg in (
            ("sanctions", sanctions["status"]),
            ("index", index["status"]),
            ("wallet", wallet["status"]),
        )
        if leg == "unknown"
    ]
    verdict = _band(risk)
    if hard_abort:
        verdict = "ABORT"
    elif unknown_legs and verdict == "PROCEED":
        verdict = "CAUTION"

    return {
        "pay_to": pay_to,
        "resource": resource,
        "amount": amount,
        "network": network or BASE_NETWORK,
        "verdict": verdict,
        "risk_score": risk,
        "unknown_legs": unknown_legs,
        "sanctions": sanctions,
        "index": index,
        "wallet": wallet,
        "reasons": reasons,
        "not_checked": NOT_CHECKED,
        "limits": LIMITS,
    }
