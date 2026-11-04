"""Market event feeds (Next-20 #8 Drift Event Feed + #14 New-Stall
Firehose): one feed module, two topics, one envelope discipline.

Both feeds are built from data this service already holds — no new
collection:

  * drift — payTo rotations, amount/asset/network (terms) changes
    and description/service-name drift from the Verdict Index's
    retained snapshot-pair diffs, plus Dima's patrol drift records
    (payto_mismatch / amount_mismatch / schema_mismatch defect rows
    in the committed patrol snapshot, services/patrolsnap.py).
  * firehose — first-seen events: resources that APPEARED between
    consecutive Index snapshots, plus new origins from the patrol
    snapshot (one event per origin, at its earliest first_seen).

Envelope discipline mirrors the Recheck feed v2 (services/
recheck.py): every event carries a monotonically increasing seq
(chronological order), an event_id, a schema_version, and a hash
chain (prev_hash / record_hash over the event's canonical JSON) so
a reader advancing a ?since_seq= cursor can prove nothing was
silently dropped from the retained data the feed was built from.
The chain is recomputed deterministically from the retained
sources on each read — same sources, same chain.

Funding (stated in the routes' own docs, mirroring the Recheck
feed's split): the first page is free — the first FREE_EVENTS
events of each feed cost nothing to anyone. Standing readers
paging past the free page present a Fleet Pass (X-Fleet-Pass
header) and are debited PER EVENT at Recheck arithmetic:
PER_EVENT_UNITS is exactly the POST /v1/recheck price ($0.005).
Events past the free page requested without a pass are held back
and the response says so (funding_required) — never silently
truncated, never delivered unpaid.
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlsplit

from . import indexfeed as indexfeed_svc
from . import patrolsnap as patrolsnap_svc

FEED_SCHEMA_VERSION = 1
_GENESIS = "genesis"
FREE_EVENTS = 100
PER_EVENT_UNITS = 5_000  # the POST /v1/recheck price — Recheck arithmetic
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
TOPICS = ("drift", "firehose")

# Patrol defect classes that ARE market drift (Item 8's scope).
_PATROL_DRIFT = {
    "payto_mismatch": "payto_drift",
    "amount_mismatch": "amount_drift",
    "schema_mismatch": "schema_drift",
}

# The standing worked example (fixture-marked): the two Nov 1
# agents.tuski.ai payTo-drift records from Dima's patrol snapshot
# (defect_class payto_mismatch in app/data/patrol-snapshot.json).
# These exact rows ride the live drift feed as patrol events; the
# copies below are the discovery/sample material's example and are
# marked fixture so no reader mistakes them for a live read.
TUSKI_WORKED_EXAMPLE = [
    {
        "fixture": True,
        "sample": True,
        "topic": "drift",
        "kind": "payto_drift",
        "source": "patrol",
        "resource": "https://agents.tuski.ai/api/v1/email-validation/jobs",
        "origin": "https://agents.tuski.ai",
        "defect_class": "payto_mismatch",
        "status": "open",
        "first_seen": "2026-10-29T07:05:43Z",
        "at": "2026-10-31T16:05:09Z",
    },
    {
        "fixture": True,
        "sample": True,
        "topic": "drift",
        "kind": "payto_drift",
        "source": "patrol",
        "resource": "https://agents.tuski.ai/zcj/y01zd9v65ez7/api/v1/email-validation/jobs",
        "origin": "https://agents.tuski.ai",
        "defect_class": "payto_mismatch",
        "status": "open",
        "first_seen": "2026-11-01T06:50:19Z",
        "at": "2026-11-02T06:28:53Z",
    },
]


def _now_iso(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch or time.time()))


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


def _index_pair_events(index_store) -> tuple[list[dict], list[dict]]:
    """(drift, firehose) events from every retained consecutive
    Index snapshot pair. Drift takes the pair diff's field changes
    only; firehose takes its additions (first sightings)."""
    drift: list[dict] = []
    firehose: list[dict] = []
    snaps = index_store.snapshots()
    kind_by_field = {
        "payTo": "payto_rotation",
        "amount": "amount_changed",
        "asset": "terms_changed",
        "network": "terms_changed",
        "description": "description_drift",
        "serviceName": "service_name_drift",
    }
    for before, after in zip(snaps, snaps[1:]):
        diff = indexfeed_svc.diff_entries(
            index_store.entries(before["id"]),
            index_store.entries(after["id"]),
            limit=indexfeed_svc.MAX_EVENT_LIMIT,
        )
        for ev in diff["events"]:
            if ev["kind"] == "field_changed":
                drift.append(
                    {
                        "topic": "drift",
                        "kind": kind_by_field.get(ev.get("field") or "", "terms_changed"),
                        "source": "index",
                        "resource": ev["resource"],
                        "origin": _origin_of(ev["resource"]),
                        "field": ev.get("field"),
                        "before": ev.get("before"),
                        "after": ev.get("after"),
                        "at": after["captured_at"],
                        "snapshot_id": after["id"],
                        "catalogue": "cdp_bazaar",
                    }
                )
            elif ev["kind"] == "added":
                firehose.append(
                    {
                        "topic": "firehose",
                        "kind": "first_seen",
                        "source": "index",
                        "resource": ev["resource"],
                        "origin": _origin_of(ev["resource"]),
                        "serviceName": ev.get("serviceName"),
                        "at": after["captured_at"],
                        "snapshot_id": after["id"],
                        "catalogue": "cdp_bazaar",
                    }
                )
    return drift, firehose


def _patrol_events(snapshot: dict | None) -> tuple[list[dict], list[dict]]:
    """(drift, firehose) events from the committed patrol snapshot:
    drift defect rows, and one new-origin event per origin at its
    earliest patrol first_seen."""
    drift: list[dict] = []
    firehose: list[dict] = []
    if not snapshot:
        return drift, firehose
    origins: dict[str, dict] = {}
    for row in snapshot.get("routes") or []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        origin = row.get("origin") or _origin_of(url)
        first_seen = row.get("first_seen")
        if origin and first_seen:
            cur = origins.get(origin)
            if cur is None or first_seen < cur["first_seen"]:
                origins[origin] = {"origin": origin, "first_seen": first_seen, "url": url}
        kind = _PATROL_DRIFT.get(row.get("defect_class") or "")
        if kind:
            drift.append(
                {
                    "topic": "drift",
                    "kind": kind,
                    "source": "patrol",
                    "resource": url,
                    "origin": origin,
                    "defect_class": row.get("defect_class"),
                    "status": row.get("status"),
                    "first_seen": first_seen,
                    "at": row.get("last_seen") or row.get("recovered_at") or first_seen,
                }
            )
    for origin, info in origins.items():
        firehose.append(
            {
                "topic": "firehose",
                "kind": "new_origin",
                "source": "patrol",
                "resource": info["url"],
                "origin": origin,
                "at": info["first_seen"],
            }
        )
    return drift, firehose


def build_events(topic: str, index_store=None, patrol_snapshot: dict | None = None) -> list[dict]:
    """All events for one topic from the held sources, in feed
    order (chronological; ties broken deterministically by source,
    resource, kind) with seq + hash chain assigned. Pure over its
    inputs — the same sources always build the same chain."""
    if topic not in TOPICS:
        raise ValueError(f"unknown feed topic {topic!r}: expected one of {list(TOPICS)}")
    index_drift, index_fire = ([], [])
    if index_store is not None:
        index_drift, index_fire = _index_pair_events(index_store)
    patrol_drift, patrol_fire = _patrol_events(patrol_snapshot)
    events = (index_drift + patrol_drift) if topic == "drift" else (index_fire + patrol_fire)
    events.sort(
        key=lambda e: (
            e.get("at") or "",
            e.get("source") or "",
            e.get("resource") or "",
            e.get("kind") or "",
            json.dumps(e, sort_keys=True),
        )
    )
    out: list[dict] = []
    prev_hash = _GENESIS
    for i, ev in enumerate(events, start=1):
        record_hash = hashlib.sha256(
            f"{prev_hash}\n{json.dumps(ev, sort_keys=True, separators=(',', ':'))}".encode()
        ).hexdigest()
        out.append(
            {
                "seq": i,
                "event_id": f"evt_{record_hash[:24]}",
                "schema_version": FEED_SCHEMA_VERSION,
                "prev_hash": prev_hash,
                "record_hash": record_hash,
                "event": ev,
            }
        )
        prev_hash = record_hash
    return out


def feed_page(
    topic: str,
    *,
    index_store=None,
    patrol_snapshot: dict | None = None,
    since_seq: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """One cursor page of a feed, WITHOUT funding applied (the HTTP
    layer applies the free/pass split to the returned envelopes)."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    envelopes = build_events(topic, index_store, patrol_snapshot)
    page = [e for e in envelopes if e["seq"] > int(since_seq or 0)][:limit]
    return {
        "feed": topic,
        "version": FEED_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "earliest_seq": envelopes[0]["seq"] if envelopes else 0,
        "latest_seq": envelopes[-1]["seq"] if envelopes else 0,
        "total_events": len(envelopes),
        "since_seq": int(since_seq or 0),
        "events_returned": len(page),
        "events": page,
        "funding": {
            "free_events": FREE_EVENTS,
            "per_event_units": PER_EVENT_UNITS,
            "note": (
                f"The first {FREE_EVENTS} events are free to anyone. "
                "Events past the free page are debited per event from a "
                "Fleet Pass (X-Fleet-Pass header) at $0.005 per event — "
                "the POST /v1/recheck price."
            ),
        },
        "sources": {
            "index_snapshots": len(index_store.snapshots()) if index_store is not None else 0,
            "patrol_snapshot_id": (patrol_snapshot or {}).get("snapshot_id"),
            "patrol_generated_at": (patrol_snapshot or {}).get("generated_at"),
        },
    }


def load_patrol() -> dict | None:
    return patrolsnap_svc.load_snapshot()
