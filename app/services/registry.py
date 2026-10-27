"""Bad-payTo Registry Feed: POST /v1/flagged-paytos ($0.005).

The currently flagged payTos, each with its evidence — assembled
from the SAME two sources Pre-Trade Guard screens against, reused
rather than forked:

  ofac_sdn       — every address in the bundled OFAC SDN snapshot
                   (services/verdict.py's SNAPSHOT, the exact set
                   sanctions_check matches against). Evidence: the
                   snapshot's date and Treasury source URL. The
                   snapshot carries addresses only, so no names are
                   claimed.
  rotated_away   — payTos that the newest Index snapshot pair shows
                   rotated away from a live listing (indexfeed's
                   diff field_changed payTo events; the BEFORE payTo
                   is the flagged one). Evidence: the resources it
                   rotated away from and where they point now.
  vanished_listing — payTos of listings present in the older
                   snapshot and gone from the newer one (diff
                   removed events). Evidence: the vanished resources.

One entry per payTo, with a reasons list and per-reason evidence —
no entry without evidence. Caps: 500 entries total, 5 evidence
examples per reason; anything cut is counted in `truncation`, never
silently dropped. When fewer than two Index snapshots are held,
the Index-derived sections are empty and the feed says so (the
OFAC section stands on its own).
"""

from __future__ import annotations

import time

from . import indexfeed
from . import verdict as verdict_svc

MAX_ENTRIES = 500
MAX_EXAMPLES = 5

LIMITS = (
    "Flags are evidence, not verdicts: an OFAC entry means the "
    "address appears verbatim in the bundled sanctions snapshot; a "
    "rotation or vanishing is a catalog fact from the two newest "
    "Index snapshots, and listings rotate or close for legitimate "
    "reasons too. Verify before acting on any single flag."
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))


def build_feed(index_store: indexfeed.IndexStore) -> dict:
    flagged: dict[str, dict] = {}

    def entry_for(pay_to: str) -> dict:
        key = pay_to.lower()
        if key not in flagged:
            flagged[key] = {"pay_to": key, "reasons": [], "evidence": {}}
        return flagged[key]

    # --- OFAC SDN snapshot (the set Pre-Trade Guard screens against) ---
    snapshot = verdict_svc.SNAPSHOT
    sanctions_count = 0
    for address in sorted(snapshot.get("addresses") or set()):
        entry = entry_for(address)
        entry["reasons"].append("ofac_sdn")
        entry["evidence"]["ofac_sdn"] = {
            "snapshot_date": snapshot.get("snapshot_date"),
            "snapshot_source": snapshot.get("source_url"),
            "detail": (
                "Address appears verbatim in the bundled OFAC SDN "
                "digital-currency snapshot."
            ),
        }
        sanctions_count += 1

    # --- Index-derived flags (rotations away + vanishings) --------------
    pair = index_store.latest_two()
    baseline_at = current_at = None
    rotated_count = 0
    vanished_count = 0
    index_note = None
    if pair is None:
        index_note = (
            "Fewer than two Index snapshots are held, so the "
            "rotation and vanishing sections are empty this run."
        )
    else:
        before, after = pair
        baseline_at = before["captured_at"]
        current_at = after["captured_at"]
        old_entries = index_store.entries(before["id"])
        new_entries = index_store.entries(after["id"])
        # Rotations: indexfeed.diff_entries over the pair (the same
        # diff Pre-Trade Guard's index history is built from; field
        # changes sort first in its capped event list and real
        # rotation counts are small).
        diff = indexfeed.diff_entries(
            old_entries, new_entries, limit=indexfeed.MAX_EVENT_LIMIT
        )
        for event in diff["events"]:
            if event["kind"] == "field_changed" and event["field"] == "payTo":
                old_pay_to = (event.get("before") or "").lower()
                if not old_pay_to:
                    continue
                entry = entry_for(old_pay_to)
                ev = entry["evidence"].setdefault(
                    "rotated_away", {"count": 0, "examples": []}
                )
                if "rotated_away" not in entry["reasons"]:
                    entry["reasons"].append("rotated_away")
                ev["count"] += 1
                rotated_count += 1
                if len(ev["examples"]) < MAX_EXAMPLES:
                    ev["examples"].append(
                        {
                            "resource": event["resource"],
                            "to_pay_to": event.get("after"),
                        }
                    )
        # Vanishings: enumerated over the full entry maps — the
        # same removed set diff_entries counts in its summary. (The
        # diff's capped event list orders removals last and would
        # silently drop them in a busy pair; a registry feed must
        # not undercount.)
        for resource in sorted(set(old_entries) - set(new_entries)):
            pay_to = (old_entries[resource].get("payTo") or "").lower()
            if not pay_to:
                continue
            entry = entry_for(pay_to)
            ev = entry["evidence"].setdefault(
                "vanished_listing", {"count": 0, "examples": []}
            )
            if "vanished_listing" not in entry["reasons"]:
                entry["reasons"].append("vanished_listing")
            ev["count"] += 1
            vanished_count += 1
            if len(ev["examples"]) < MAX_EXAMPLES:
                ev["examples"].append({"resource": resource})

    entries = sorted(flagged.values(), key=lambda e: e["pay_to"])
    truncated_entries = max(0, len(entries) - MAX_ENTRIES)
    entries = entries[:MAX_ENTRIES]
    return {
        "generated_at": _now_iso(),
        "baseline_at": baseline_at,
        "current_at": current_at,
        "counts": {
            "ofac_sdn": sanctions_count,
            "rotated_away_events": rotated_count,
            "vanished_events": vanished_count,
            "flagged_paytos": len(flagged),
            "entries_returned": len(entries),
        },
        "truncation": {
            "entries_capped_at": MAX_ENTRIES,
            "entries_truncated": truncated_entries,
            "evidence_examples_per_reason": MAX_EXAMPLES,
        },
        "index_note": index_note,
        "entries": entries,
        "limits": LIMITS,
    }
