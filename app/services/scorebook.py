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

Three target shapes (exactly one per call):
  url    — one resource's record: check counts by outcome,
           first/last check, the distinct amounts / payTos /
           networks its live sign has carried, its current
           fingerprint, and its catalogue presence across the
           snapshots held.
  origin — every recorded resource under one origin, summarized.
  pay_to — every resource ever observed paying this address, in
           live checks and in the latest catalogue snapshot.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from . import indexfeed
from . import recheck as recheck_svc

log = logging.getLogger("stall-row.scorebook")

PRICE_UNITS = 10_000  # $0.01 — mirrors config.PRICE_UNITS (price card 2026-10-30)

HISTORY_WINDOW = 1000  # newest recorded checks the aggregates cover

LIMITS = (
    "Aggregates cover only what this service's stores hold: the "
    f"newest {HISTORY_WINDOW} recorded recheck responses and the "
    "catalogue snapshots currently in the Index store (the newest "
    "few, refreshed about every 6 hours). A target outside those "
    "windows answers found=false or partial — absence here means "
    "'not in our records', never 'does not exist'. Live-check "
    "history begins when the recheck history table did; catalogue "
    "history is limited to the snapshots held."
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


def score_book(store, index_store, *, url=None, origin=None, pay_to=None) -> dict:
    """The track record for exactly one target. Raises ValueError
    (the handler's 422) when no target — or more than one — is
    named."""
    named = [bool(url), bool(origin), bool(pay_to)]
    if sum(named) != 1:
        raise ValueError("name exactly one target: url, origin, or pay_to")
    records = _records(store)
    base = {"computed_at": _now_iso(), "limits": LIMITS}

    if url:
        url = url.strip().rstrip("/")
        mine = [r for r in records if (r.get("url") or "").rstrip("/") == url]
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
            }
        )
        if not mine:
            base["found"] = base["catalogue"]["present_in_snapshots"] > 0
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
        base.update(
            {
                "target": {"type": "origin", "value": origin},
                "found": bool(mine),
                "resources": resources,
                "totals": {
                    "resources_seen": len(resources),
                    "checks": len(mine),
                    "changed_count": sum(
                        s["changed_count"] for s in resources.values()
                    ),
                },
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
        }
    )
    return base
