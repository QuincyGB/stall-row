"""Patrol snapshot: the shared loader for Dima's patrol export.

The patrol (Dima, the PC patrol walker) keeps the fleet's defect
ledger on the operator's side; this service cannot read it at
runtime. The VM-side exporter
(hidden_files/fleet-checker/patrol_snapshot.py, in the fleet-
integration goal) distils it into ONE redacted snapshot committed
at app/data/patrol-snapshot.json — the same committed-artifact
pattern as the stability manifest and the gradeability report —
and this module is the single reader of that file.

Consumers: the Ledger Dataset (services/ledger.py, Next-20 #6)
folds it into both dataset tiers; the Score Book (services/
scorebook.py, Next-20 #16) reads the same snapshot through this
module — one export, one loader, no second format.

Snapshot shape (kind "verdict-row-patrol-snapshot/v1"):

    {"kind", "generated_at", "snapshot_id",
     "source": {"patrol", "patrol_run_id", "patrol_as_of",
                "patrol_runs_total", "patrol_stalls_scanned_total",
                "patrol_resources_probed_total",
                "open_defects", "recoveries", "receipts_index"},
     "routes": [{"url", "origin", "status": "open"|"recovered",
                 "defect_class", "first_seen",
                 "last_seen" (open) |
                 "recovered_at" + "receipt_id" (recovered)}]}

Redaction is by construction at export time: stall/route facts
only — no operator contacts, no inbox contents, no nudge text, no
signatures. A missing or unreadable file loads as None (callers
answer without the patrol side and say so in provenance — the
patrol facts are never fabricated).
"""

from __future__ import annotations

import json
import os

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "patrol-snapshot.json",
)

_cache: dict | None = None
_cache_stamp: tuple[float, int] | None = None


def load_snapshot(path: str | None = None) -> dict | None:
    """The committed patrol snapshot, or None when absent/broken.

    The default path is mtime-cached (the file only changes on a
    redeploy); an explicit path is read fresh (tests)."""
    global _cache, _cache_stamp
    if path is not None:
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        return doc if _valid(doc) else None
    try:
        stat = os.stat(SNAPSHOT_PATH)
    except OSError:
        return None
    stamp = (stat.st_mtime, stat.st_size)
    if _cache is not None and _cache_stamp == stamp:
        return _cache
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        _cache, _cache_stamp = None, stamp
        return None
    _cache = doc if _valid(doc) else None
    _cache_stamp = stamp
    return _cache


def _valid(doc: object) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("kind") == "verdict-row-patrol-snapshot/v1"
        and isinstance(doc.get("routes"), list)
    )


def routes_by_url(snapshot: dict | None) -> dict[str, dict]:
    """url -> patrol route row ({} when no snapshot)."""
    if not snapshot:
        return {}
    out: dict[str, dict] = {}
    for row in snapshot.get("routes") or []:
        if isinstance(row, dict) and row.get("url"):
            out[row["url"]] = row
    return out


def provenance_of(snapshot: dict | None) -> dict | None:
    """The provenance block dataset/score payloads cite: snapshot
    identity + patrol watermarks, or None when no snapshot is held."""
    if not snapshot:
        return None
    source = snapshot.get("source") or {}
    return {
        "id": snapshot.get("snapshot_id"),
        "generated_at": snapshot.get("generated_at"),
        "patrol_run_id": source.get("patrol_run_id"),
        "patrol_as_of": source.get("patrol_as_of"),
        "patrol_runs_total": source.get("patrol_runs_total"),
        "patrol_stalls_scanned_total": source.get("patrol_stalls_scanned_total"),
        "patrol_resources_probed_total": source.get("patrol_resources_probed_total"),
        "open_defects": source.get("open_defects"),
        "recoveries": source.get("recoveries"),
    }
