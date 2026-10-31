"""Ledger Dataset — the fleet's observation ledger, sold as data.

Two tiers over the same build (see discovery ROUTES): a per-origin
summary (POST /v1/ledger-dataset, $0.10) and the full per-route
dataset (POST /v1/ledger-dataset/full, $1.00). The prices follow the
fleet's own price-card evidence: directory trust datasets sell at
$0.10 for the rollup and $1.00 for the full data pull (PulseFeed's
/trust/dataset $0.10 and /data/full $1.00 are the named comps), and
this dataset's per-route change history exists in no directory.

Sources — the stores this service already keeps, plus the one
committed patrol export, all public endpoint observation data
(no operator contacts, no private data):

  * the Recheck history: every recorded check of an x402 resource
    or web page (terms observed, CHANGED/BASELINE status, the
    catalogue crosscheck frozen at check time);
  * the CDP Index snapshots this service holds (the newest
    KEEP_SNAPSHOTS captures): listing state, listed terms, and
    usage counters exactly as the catalogue published them;
  * the patrol snapshot committed at app/data/patrol-snapshot.json
    (kind verdict-row-patrol-snapshot/v1, exported VM-side from
    Dima's PC patrol ledger by patrol_snapshot.py and read here
    through services/patrolsnap.py): per-route open defects by
    class with first/last seen, and recoveries carrying their
    signed-receipt ids. It is a point-in-time export — provenance
    names its id, generation time and the patrol run it reflects;
    between exports the patrol side is exactly that old, never
    fresher, and a missing snapshot folds in as no patrol side
    (provenance says so) rather than an invented one.

Honesty rules, in line with the rest of the fleet:

  * failed probes are not in the Recheck history (the store records
    answered checks only), so absence here never claims a route is
    down — `limits` says so in every payload;
  * defect_class is DERIVED, never asserted: "catalogue_mismatch"
    when listed terms and live-observed terms disagree (frozen
    crosscheck or a direct comparison of the newest snapshot entry
    against the latest observed terms), "not_listed" when a route
    Recheck has observed is absent from the newest catalogue
    snapshot, otherwise null — unknown stays null;
  * change counts keep their components separate (Recheck CHANGED
    events vs catalogue tracked-field changes across the held
    snapshot pairs) so the sum is never mistaken for either;
  * recovery keeps its two meanings separate: `recoveries` counts
    a resource that disappears from the catalogue and later
    reappears across the held snapshots, while a route's `patrol`
    block carries the patrol's own recovery (status "recovered"
    with the signed receipt id) — the two are never summed into
    one number (totals keep patrol_open_defects /
    patrol_recoveries apart from the catalogue figures);
  * the patrol defect class lives in the route's `patrol` block
    under its own patrol vocabulary (unexpected_status,
    amount_mismatch, payto_mismatch, server_error,
    challenge_undecodable, unreachable, no_402) and never
    overwrites the derived `defect_class`, which stays a
    Recheck/catalogue derivation only.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import indexfeed as indexfeed_svc
from . import patrolsnap as patrolsnap_svc

LIMITS = (
    "Observation data only: the Recheck history records ANSWERED "
    "checks (failed probes are never recorded, so a missing route "
    "is unobserved, not down), and the catalogue side is limited to "
    "the snapshots this service currently holds (provenance lists "
    "them). Defect classes are derived from stored observations — "
    "catalogue_mismatch (listed terms disagree with observed terms) "
    "and not_listed (observed live, absent from the newest "
    "catalogue snapshot) — never asserted beyond the data. The "
    "patrol side is a point-in-time export of the PC patrol ledger "
    "(provenance names the snapshot id and the patrol run it "
    "reflects): patrol defects and recoveries are exactly as fresh "
    "as that export, and patrol coverage is the origins the patrol "
    "has walked — a route with no patrol block was not necessarily "
    "patrolled. No operator contacts or private data are included."
)


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url


def _terms_of(current: dict | None) -> dict | None:
    if not isinstance(current, dict):
        return None
    return {
        "status_code": current.get("status_code"),
        "amount": current.get("amount"),
        "asset": current.get("asset"),
        "network": current.get("network"),
        "payTo": current.get("payTo"),
    }


def _empty_route(url: str) -> dict:
    return {
        "url": url,
        "origin": _origin_of(url),
        "sources": set(),
        "checks": 0,
        "recheck_changes": 0,
        "catalogue_changes": 0,
        "recoveries": 0,
        "first_seen_epoch": None,
        "last_seen_epoch": None,
        "recheck_first_epoch": None,
        "recheck_last_epoch": None,
        "catalogue_first_epoch": None,
        "catalogue_last_epoch": None,
        "latest_status": None,
        "latest_terms": None,
        "latest_crosscheck": None,
        "catalogue": None,
        "patrol": None,
        "patrol_first_epoch": None,
        "patrol_last_epoch": None,
        "_cat_seen": [],  # snapshot indexes the resource appears in
        "_cat_entries": {},  # snapshot index -> entry
    }


def _epoch_of(iso: object) -> float | None:
    if isinstance(iso, (int, float)):
        return float(iso)
    if not isinstance(iso, str) or not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(iso, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _fold_recheck(store, routes: dict[str, dict]) -> int:
    """Fold the whole recorded Recheck history into route rows.
    Returns the number of records read."""
    read = 0
    cursor = 0
    while True:
        envelopes = store.feed_rows(limit=1000, since_seq=cursor)
        if not envelopes:
            break
        for env in envelopes:
            read += 1
            cursor = int(env["seq"])
            payload = env.get("record") if isinstance(env, dict) else None
            if not isinstance(payload, dict):
                continue
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                continue
            rec = routes.setdefault(url, _empty_route(url))
            rec["sources"].add("recheck")
            epoch = _epoch_of(payload.get("checked_at"))
            rec["checks"] += 1
            status = payload.get("status")
            if status == "CHANGED":
                rec["recheck_changes"] += 1
            # Cursor mode is ascending => the last fold wins for latest_*.
            rec["latest_status"] = status
            terms = _terms_of(payload.get("current"))
            if terms is not None:
                rec["latest_terms"] = terms
            cross = payload.get("catalogue_crosscheck")
            if isinstance(cross, dict):
                rec["latest_crosscheck"] = cross
            if epoch is not None:
                if rec["recheck_first_epoch"] is None:
                    rec["recheck_first_epoch"] = epoch
                rec["recheck_last_epoch"] = epoch
        if len(envelopes) < 1000:
            break
    return read


def _fold_index(index_store, routes: dict[str, dict]) -> tuple[list[dict], set[str] | None]:
    """Fold the held catalogue snapshots into route rows. Returns
    (snapshot provenance, resource set of the newest snapshot —
    None when no snapshot is held)."""
    try:
        snaps = index_store.snapshots()
    except Exception:  # noqa: BLE001 - unreadable store = no catalogue side
        return [], None
    if not snaps:
        return [], None
    provenance = []
    newest_urls: set[str] = set()
    for idx, snap in enumerate(snaps):
        provenance.append(
            {
                "id": snap.get("id"),
                "captured_at": snap.get("captured_at"),
                "resource_count": snap.get("resource_count"),
            }
        )
        epoch = snap.get("captured_epoch")
        try:
            entries = index_store.entries(snap["id"])
        except Exception:  # noqa: BLE001
            entries = {}
        if idx == len(snaps) - 1:
            newest_urls = set(entries)
        for url, entry in entries.items():
            rec = routes.setdefault(url, _empty_route(url))
            rec["sources"].add("cdp_index")
            rec["_cat_seen"].append(idx)
            rec["_cat_entries"][idx] = entry
            if isinstance(epoch, (int, float)):
                if rec["catalogue_first_epoch"] is None:
                    rec["catalogue_first_epoch"] = epoch
                rec["catalogue_last_epoch"] = epoch
    last_idx = len(snaps) - 1
    for rec in routes.values():
        seen = rec["_cat_seen"]
        if not seen:
            continue
        entries = rec["_cat_entries"]
        # Field changes + recoveries across consecutive held pairs.
        for idx in range(1, len(snaps)):
            before = entries.get(idx - 1)
            now = entries.get(idx)
            if before is not None and now is not None:
                if any(
                    str(before.get(f)) != str(now.get(f))
                    for f in indexfeed_svc.TRACKED_FIELDS
                ):
                    rec["catalogue_changes"] += 1
            elif now is not None and any(s < idx - 1 for s in seen):
                rec["recoveries"] += 1
        newest = entries.get(last_idx)
        if newest is not None:
            rec["catalogue"] = {
                "listed": True,
                "service_name": newest.get("serviceName"),
                "amount": newest.get("amount"),
                "asset": newest.get("asset"),
                "network": newest.get("network"),
                "payTo": newest.get("payTo"),
                "last_updated": newest.get("lastUpdated"),
                "calls30d": newest.get("calls30d"),
                "payers30d": newest.get("payers30d"),
                "snapshots_seen": len(seen),
                "present_in_latest": True,
            }
        else:
            rec["catalogue"] = {
                "listed": False,
                "service_name": None,
                "amount": None,
                "asset": None,
                "network": None,
                "payTo": None,
                "last_updated": None,
                "calls30d": None,
                "payers30d": None,
                "snapshots_seen": len(seen),
                "present_in_latest": False,
            }
    return provenance, newest_urls


def _fold_patrol(snapshot: dict | None, routes: dict[str, dict]) -> dict | None:
    """Fold the committed patrol snapshot into route rows. Returns
    the patrol provenance block (None when no snapshot is held).
    Patrol rows are facts only, copied field by field — nothing
    derived. A route can carry BOTH rows (recovered once, defective
    again since): they merge into one block whose status is the
    current one ("open") while the recovery fact (receipt id +
    recovered_at) is kept alongside."""
    provenance = patrolsnap_svc.provenance_of(snapshot)
    if provenance is None:
        return None
    for row in snapshot.get("routes") or []:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        url = row["url"]
        rec = routes.setdefault(url, _empty_route(url))
        rec["sources"].add("patrol")
        block = rec["patrol"] or {
            "status": None,
            "defect_class": None,
            "first_seen": None,
            "last_seen": None,
            "recovered_at": None,
            "receipt_id": None,
        }
        first_seen = row.get("first_seen")
        if first_seen and (block["first_seen"] is None or first_seen < block["first_seen"]):
            block["first_seen"] = first_seen
        if row.get("status") == "open":
            block["status"] = "open"
            block["defect_class"] = row.get("defect_class")
            block["last_seen"] = row.get("last_seen")
        elif row.get("status") == "recovered":
            block["recovered_at"] = row.get("recovered_at")
            block["receipt_id"] = row.get("receipt_id")
            if block["status"] != "open":
                block["status"] = "recovered"
                block["defect_class"] = row.get("defect_class")
        rec["patrol"] = block
        first = _epoch_of(block["first_seen"])
        if first is not None and (
            rec["patrol_first_epoch"] is None or first < rec["patrol_first_epoch"]
        ):
            rec["patrol_first_epoch"] = first
        for stamp in (block["last_seen"], block["recovered_at"]):
            last = _epoch_of(stamp)
            if last is not None and (
                rec["patrol_last_epoch"] is None or last > rec["patrol_last_epoch"]
            ):
                rec["patrol_last_epoch"] = last
    return provenance


def _defect_of(rec: dict, newest_urls: set[str] | None) -> tuple[str | None, dict | None]:
    """Derive the defect class from stored observations only."""
    cross = rec["latest_crosscheck"] or {}
    terms = rec["latest_terms"] or {}
    cat = rec["catalogue"] or {}
    if cross.get("verdict") == "mismatch":
        return "catalogue_mismatch", {
            "via": "recheck_crosscheck",
            "fields": cross.get("fields"),
        }
    if cat.get("listed") and rec["checks"]:
        differing = [
            f
            for f in ("amount", "payTo")
            if cat.get(f) is not None
            and terms.get(f) is not None
            and str(cat[f]) != str(terms[f])
        ]
        if differing:
            return "catalogue_mismatch", {
                "via": "snapshot_vs_latest_observed",
                "fields": differing,
            }
    if cross.get("verdict") == "not_listed":
        return "not_listed", {"via": "recheck_crosscheck"}
    if rec["checks"] and newest_urls is not None and rec["url"] not in newest_urls:
        return "not_listed", {"via": "absent_from_newest_snapshot"}
    return None, None


def build_dataset(recheck_store, index_store, patrol_snapshot="default") -> dict:
    """The full dataset build (both tiers shape from this).

    patrol_snapshot: "default" loads the committed snapshot via
    patrolsnap; None builds without the patrol side; a dict is
    used as-is (tests)."""
    if patrol_snapshot == "default":
        patrol_snapshot = patrolsnap_svc.load_snapshot()
    routes: dict[str, dict] = {}
    records_read = _fold_recheck(recheck_store, routes)
    snap_provenance, newest_urls = _fold_index(index_store, routes)
    patrol_provenance = _fold_patrol(patrol_snapshot, routes)

    records: list[dict] = []
    origins: dict[str, dict] = {}
    total_changes = 0
    total_recoveries = 0
    patrol_open = 0
    patrol_recovered = 0
    for url in sorted(routes):
        rec = routes[url]
        firsts = [
            e
            for e in (
                rec["recheck_first_epoch"],
                rec["catalogue_first_epoch"],
                rec["patrol_first_epoch"],
            )
            if e
        ]
        rec["first_seen_epoch"] = min(firsts) if firsts else None
        lasts = [
            e
            for e in (
                rec["recheck_last_epoch"],
                rec["catalogue_last_epoch"],
                rec["patrol_last_epoch"],
            )
            if e
        ]
        rec["last_seen_epoch"] = max(lasts) if lasts else None
        patrol = rec["patrol"]
        if patrol is not None:
            if patrol["status"] == "open":
                patrol_open += 1
            if patrol["receipt_id"] is not None:
                patrol_recovered += 1
        defect, defect_detail = _defect_of(rec, newest_urls)
        changes = rec["recheck_changes"] + rec["catalogue_changes"]
        total_changes += changes
        total_recoveries += rec["recoveries"]
        record = {
            "url": url,
            "origin": rec["origin"],
            "sources": sorted(rec["sources"]),
            "first_seen": _iso(rec["first_seen_epoch"]),
            "last_seen": _iso(rec["last_seen_epoch"]),
            "checks": rec["checks"],
            "latest_status": rec["latest_status"],
            "changes": changes,
            "recheck_changes": rec["recheck_changes"],
            "catalogue_changes": rec["catalogue_changes"],
            "recoveries": rec["recoveries"],
            "defect_class": defect,
            "defect_detail": defect_detail,
            "terms": rec["latest_terms"],
            "catalogue": rec["catalogue"],
            "patrol": patrol,
        }
        records.append(record)
        org = origins.setdefault(
            rec["origin"],
            {
                "origin": rec["origin"],
                "routes": 0,
                "checks": 0,
                "changes": 0,
                "recoveries": 0,
                "defect_classes": set(),
                "patrol_open": 0,
                "patrol_recovered": 0,
                "patrol_defect_classes": set(),
                "last_seen_epoch": None,
            },
        )
        org["routes"] += 1
        org["checks"] += rec["checks"]
        org["changes"] += changes
        org["recoveries"] += rec["recoveries"]
        if defect:
            org["defect_classes"].add(defect)
        if patrol is not None:
            if patrol["status"] == "open":
                org["patrol_open"] += 1
                if patrol["defect_class"]:
                    org["patrol_defect_classes"].add(patrol["defect_class"])
            if patrol["receipt_id"] is not None:
                org["patrol_recovered"] += 1
        if rec["last_seen_epoch"] and (
            org["last_seen_epoch"] is None or rec["last_seen_epoch"] > org["last_seen_epoch"]
        ):
            org["last_seen_epoch"] = rec["last_seen_epoch"]

    origin_rows = [
        {
            "origin": o["origin"],
            "routes": o["routes"],
            "checks": o["checks"],
            "changes": o["changes"],
            "recoveries": o["recoveries"],
            "defect_classes": sorted(o["defect_classes"]),
            "patrol_open": o["patrol_open"],
            "patrol_recovered": o["patrol_recovered"],
            "patrol_defect_classes": sorted(o["patrol_defect_classes"]),
            "last_seen": _iso(o["last_seen_epoch"]),
        }
        for o in (origins[k] for k in sorted(origins))
    ]

    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    snapshot_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    provenance = {
        "generated_at": _iso(time.time()),
        "snapshot_id": snapshot_id,
        "sources": {
            "recheck_records": records_read,
            "index_snapshots": snap_provenance,
            "patrol_snapshot": patrol_provenance,
            "routes_from_recheck": sum(1 for r in records if "recheck" in r["sources"]),
            "routes_from_index": sum(1 for r in records if "cdp_index" in r["sources"]),
            "routes_from_patrol": sum(1 for r in records if "patrol" in r["sources"]),
        },
        "limits": LIMITS,
    }
    totals = {
        "routes": len(records),
        "origins": len(origin_rows),
        "checks": sum(r["checks"] for r in records),
        "changes": total_changes,
        "recoveries": total_recoveries,
        "defective_routes": sum(1 for r in records if r["defect_class"]),
        "patrol_open_defects": patrol_open,
        "patrol_recoveries": patrol_recovered,
    }
    return {"provenance": provenance, "totals": totals, "origins": origin_rows, "records": records}


def summary_view(dataset: dict) -> dict:
    return {
        "tier": "summary",
        "provenance": dataset["provenance"],
        "totals": dataset["totals"],
        "origins": dataset["origins"],
    }


def full_view(dataset: dict) -> dict:
    return {
        "tier": "full",
        "provenance": dataset["provenance"],
        "totals": dataset["totals"],
        "origins": dataset["origins"],
        "records": dataset["records"],
    }
