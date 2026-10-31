"""Score Book: POST /v1/score-book ($0.01) — a stall's (or a
payTo's) track record, from records this service already holds.

Inspectors judge stalls by history: how long a sign has stood, how
often the price or the payTo moved, whether a stall was seen broken
and later recovered. Every one of those facts already exists in
this deployment's own stores — the recheck history (every completed
check's full response, hash-chained in the watch store) and the
Verdict Index snapshot store (the newest CDP catalogue snapshots).
This route AGGREGATES them. It probes nothing, fetches nothing,
and invents nothing: a target with no records answers found=false
with empty aggregates, and every figure carries the window it was
computed over (the newest HISTORY_WINDOW recorded checks and the
snapshots currently held — older history than the window is
reported as outside the window, never reconstructed).

Since Next-20 #16 the book also folds in Dima's patrol ledger —
through the SAME committed, redacted patrol snapshot the Ledger
Dataset ingests (app/data/patrol-snapshot.json, read via
services/patrolsnap.py; no second pipeline). Url and origin books
gain a `patrol` section: patrol-observed open defects (class,
first/last seen per route) and recoveries (with their signed
receipt ids), each fact under the snapshot's provenance. A target
known ONLY to the patrol is found, with its patrol section as the
whole of its record — the live-check and catalogue sections stay
empty, so the sources never blur. The patrol walks periodically,
not continuously: patrol facts are as of the snapshot's
generated_at / the route's last patrol observation, and the
response's limits text says so — a patrol fact never reads as
live data. The patrol snapshot records no payTo per route, so a
pay_to book's patrol section is explicitly empty rather than
implying coverage the snapshot does not hold.

Three target shapes (exactly one per call):
  url    — one resource's record: check counts by outcome,
           first/last check, the distinct amounts / payTos /
           networks its live sign has carried, its current
           fingerprint, and its catalogue presence across the
           snapshots held, plus its patrol section.
  origin — every recorded resource under one origin, summarized,
           plus the origin's patrol section.
  pay_to — every resource ever observed paying this address, in
           live checks and in the latest catalogue snapshot.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from . import indexfeed
from . import patrolsnap as patrolsnap_svc
from . import recheck as recheck_svc

log = logging.getLogger("stall-row.scorebook")

PRICE_UNITS = 10_000  # $0.01 — mirrors config.PRICE_UNITS (price card 2026-10-30)

HISTORY_WINDOW = 1000  # newest recorded checks the aggregates cover

_LIMITS_BASE = (
    "Aggregates cover only what this service's records hold, from "
    "three named sources: the live-check history (the newest "
    f"{HISTORY_WINDOW} recorded recheck responses), the catalogue "
    "snapshots currently in the Index store (the newest few, "
    "refreshed about every 6 hours), and the patrol snapshot (see "
    "below). A target outside those windows answers found=false or "
    "partial — absence here means 'not in our records', never "
    "'does not exist'. Live-check history begins when the recheck "
    "history table did; catalogue history is limited to the "
    "snapshots held."
)

PATROL_COVERAGE = (
    "Patrol facts are the committed patrol snapshot's, cited in "
    "this section's provenance (snapshot id + generated_at). The "
    "patrol walks periodically, not continuously: an open defect "
    "is as of its last patrol observation (last_seen) and a "
    "recovery as of recovered_at — never live data. A route with "
    "no patrol facts was not necessarily patrolled."
)

PATROL_NO_PAYTO = (
    "The patrol snapshot records defects and recoveries per route "
    "(origin + URL) and holds no payTo data, so no patrol facts "
    "can be attributed to a payTo target: this section is "
    "explicitly empty, not a claim that the patrol cleared this "
    "address."
)


def _limits(patrol_snapshot: dict | None) -> str:
    """The response's window statement, naming every source with
    its window — including the patrol snapshot's own timestamps,
    so a patrol fact can never read as live data."""
    if patrol_snapshot is None:
        return (
            _LIMITS_BASE
            + " Patrol side: no patrol snapshot is held by this "
            "deployment, so the patrol contributes no facts to "
            "this book."
        )
    source = patrol_snapshot.get("source") or {}
    return (
        _LIMITS_BASE
        + " Patrol side: the committed patrol snapshot generated "
        f"at {patrol_snapshot.get('generated_at')} (patrol data "
        f"as of {source.get('patrol_as_of')}, patrol run "
        f"{source.get('patrol_run_id')}). Patrol walks are "
        "periodic, not continuous, so patrol defects and "
        "recoveries are exactly as fresh as that snapshot and "
        "their own last-observation stamps — never live."
    )


def _now_iso() -> str:
    return recheck_svc._now_iso()


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _records(store) -> list[dict]:
    """Recorded check responses in the window, oldest first."""
    envelopes = store.feed_rows(limit=HISTORY_WINDOW, ascending=True)
    out = []
    for env in envelopes:
        record = env.get("record") if isinstance(env, dict) else None
        if isinstance(record, dict) and record.get("url"):
            out.append(record)
    return out


def _url_summary(records: list[dict]) -> dict:
    """Aggregate the records of ONE resource URL."""
    by_status: dict[str, int] = {}
    amounts: list[str] = []
    pay_tos: list[str] = []
    networks: list[str] = []
    current = None
    for record in records:
        status = record.get("status")
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        fingerprint = record.get("current") or {}
        current = fingerprint
        for value, bucket in (
            (fingerprint.get("amount"), amounts),
            (fingerprint.get("payTo"), pay_tos),
            (fingerprint.get("network"), networks),
        ):
            if value is not None and str(value) not in bucket:
                bucket.append(str(value))
    return {
        "checks": len(records),
        "first_checked_at": records[0].get("checked_at") if records else None,
        "last_checked_at": records[-1].get("checked_at") if records else None,
        "by_status": by_status,
        "changed_count": by_status.get("CHANGED", 0),
        "amounts_seen": amounts,
        "pay_tos_seen": pay_tos,
        "networks_seen": networks,
        "price_changes": max(0, len(amounts) - 1),
        "payto_changes": max(0, len(pay_tos) - 1),
        "current_fingerprint": current,
    }


def _catalogue_for_url(index_store, url: str) -> dict:
    snaps = index_store.snapshots()
    present_in: list[str] = []
    latest_entry = None
    for snap in snaps:
        entries = index_store.entries(snap["id"])
        if url in entries:
            present_in.append(snap["captured_at"])
            latest_entry = entries[url]
    return {
        "snapshots_held": len(snaps),
        "present_in_snapshots": len(present_in),
        "first_snapshot_seen_at": present_in[0] if present_in else None,
        "last_snapshot_seen_at": present_in[-1] if present_in else None,
        "in_latest_snapshot": bool(snaps) and latest_entry is not None
        and present_in
        and present_in[-1] == snaps[-1]["captured_at"],
        "latest_entry": latest_entry,
    }


def _patrol_section(snapshot: dict | None, rows: list[dict]) -> dict:
    """The patrol section for the given patrol rows (already
    filtered to the target). Facts are copied field by field from
    the snapshot — nothing derived, nothing merged into the
    live-check or catalogue aggregates, so the sources stay
    distinguishable. A route can carry both an open row and a
    recovered row (defective again since a signed recovery): both
    are listed, each under its own status."""
    open_defects: list[dict] = []
    recoveries: list[dict] = []
    for row in rows:
        if row.get("status") == "open":
            open_defects.append(
                {
                    "url": row.get("url"),
                    "origin": row.get("origin"),
                    "defect_class": row.get("defect_class"),
                    "first_seen": row.get("first_seen"),
                    "last_seen": row.get("last_seen"),
                }
            )
        elif row.get("status") == "recovered":
            recoveries.append(
                {
                    "url": row.get("url"),
                    "origin": row.get("origin"),
                    "defect_class": row.get("defect_class"),
                    "first_seen": row.get("first_seen"),
                    "recovered_at": row.get("recovered_at"),
                    "receipt_id": row.get("receipt_id"),
                }
            )
    return {
        "provenance": patrolsnap_svc.provenance_of(snapshot),
        "coverage": PATROL_COVERAGE,
        "open_defects": open_defects,
        "open_defect_count": len(open_defects),
        "recoveries": recoveries,
        "recovery_count": len(recoveries),
        "receipt_ids": [
            r["receipt_id"] for r in recoveries if r.get("receipt_id")
        ],
    }


def _patrol_rows_for_url(snapshot: dict | None, url: str) -> list[dict]:
    if not snapshot:
        return []
    return [
        row
        for row in snapshot.get("routes") or []
        if isinstance(row, dict)
        and (row.get("url") or "").rstrip("/") == url
    ]


def _patrol_rows_for_origin(snapshot: dict | None, origin: str) -> list[dict]:
    if not snapshot:
        return []
    return [
        row
        for row in snapshot.get("routes") or []
        if isinstance(row, dict)
        and (
            (row.get("origin") or "").rstrip("/") == origin
            or _origin_of(row.get("url") or "") == origin
        )
    ]


def score_book(
    store, index_store, *, url=None, origin=None, pay_to=None,
    patrol_snapshot="default",
) -> dict:
    """The track record for exactly one target. Raises ValueError
    (the handler's 422) when no target — or more than one — is
    named.

    patrol_snapshot: "default" loads the committed patrol snapshot
    via patrolsnap (the same loader the Ledger Dataset uses); None
    builds without the patrol side; a dict is used as-is (tests).
    """
    named = [bool(url), bool(origin), bool(pay_to)]
    if sum(named) != 1:
        raise ValueError("name exactly one target: url, origin, or pay_to")
    if patrol_snapshot == "default":
        patrol_snapshot = patrolsnap_svc.load_snapshot()
    records = _records(store)
    base = {"computed_at": _now_iso(), "limits": _limits(patrol_snapshot)}

    if url:
        url = url.strip().rstrip("/")
        mine = [r for r in records if (r.get("url") or "").rstrip("/") == url]
        patrol_rows = _patrol_rows_for_url(patrol_snapshot, url)
        base.update(
            {
                "target": {"type": "url", "value": url},
                "found": bool(mine),
                "live_checks": _url_summary(mine)
                if mine
                else {
                    "checks": 0,
                    "first_checked_at": None,
                    "last_checked_at": None,
                    "by_status": {},
                    "changed_count": 0,
                    "amounts_seen": [],
                    "pay_tos_seen": [],
                    "networks_seen": [],
                    "price_changes": 0,
                    "payto_changes": 0,
                    "current_fingerprint": None,
                },
                "catalogue": _catalogue_for_url(index_store, url),
                "patrol": _patrol_section(patrol_snapshot, patrol_rows),
            }
        )
        if not mine:
            # Catalogue presence counts, and so does the patrol's:
            # a target known ONLY to the patrol is found, with its
            # patrol section as the whole of its record.
            base["found"] = (
                base["catalogue"]["present_in_snapshots"] > 0
                or bool(patrol_rows)
            )
        return base

    if origin:
        origin = origin.strip().rstrip("/")
        mine = [r for r in records if _origin_of(r.get("url") or "") == origin]
        by_url: dict[str, list[dict]] = {}
        for record in mine:
            by_url.setdefault((record.get("url") or "").rstrip("/"), []).append(record)
        resources = {
            u: _url_summary(rs) for u, rs in sorted(by_url.items())
        }
        patrol_rows = _patrol_rows_for_origin(patrol_snapshot, origin)
        patrol = _patrol_section(patrol_snapshot, patrol_rows)
        base.update(
            {
                "target": {"type": "origin", "value": origin},
                "found": bool(mine) or bool(patrol_rows),
                "resources": resources,
                "totals": {
                    "resources_seen": len(resources),
                    "checks": len(mine),
                    "changed_count": sum(
                        s["changed_count"] for s in resources.values()
                    ),
                    "patrol_routes": len(
                        {r.get("url") for r in patrol_rows}
                    ),
                    "patrol_open_defects": patrol["open_defect_count"],
                    "patrol_recoveries": patrol["recovery_count"],
                },
                "patrol": patrol,
            }
        )
        return base

    # pay_to
    pay_to = pay_to.strip()
    mine = [
        r
        for r in records
        if ((r.get("current") or {}).get("payTo") or "").lower() == pay_to.lower()
    ]
    urls = sorted({(r.get("url") or "").rstrip("/") for r in mine})
    snaps = index_store.snapshots()
    listings_now: list[str] = []
    if snaps:
        entries = index_store.entries(snaps[-1]["id"])
        listings_now = sorted(
            u
            for u, e in entries.items()
            if (e.get("payTo") or "").lower() == pay_to.lower()
        )
    base.update(
        {
            "target": {"type": "pay_to", "value": pay_to},
            "found": bool(mine or listings_now),
            "live_checks": {
                "checks": len(mine),
                "first_seen_at": mine[0].get("checked_at") if mine else None,
                "last_seen_at": mine[-1].get("checked_at") if mine else None,
                "resources_seen": urls,
            },
            "catalogue": {
                "snapshots_held": len(snaps),
                "listings_in_latest_snapshot": listings_now,
            },
            # The patrol snapshot holds no payTo data (its rows are
            # keyed by origin + URL), so no patrol fact can be
            # attributed to this target honestly: the section is
            # present but explicitly empty, with the reason stated,
            # rather than implying patrol coverage of the address.
            "patrol": {
                **_patrol_section(patrol_snapshot, []),
                "coverage": PATROL_NO_PAYTO,
            },
        }
    )
    return base
