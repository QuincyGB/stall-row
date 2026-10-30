"""The unified per-resource timeline (GET /v1/resource/timeline).

One URL-keyed read joining the three places the fleet already
records the truth about an x402 resource:

  * Recheck events — StallRow's own feed history (full recorded
    response objects, with their feed seq / record_id).
  * RouteVerdict check events — read from RouteVerdict's own free
    GET /v1/history, because its check store lives inside that
    service; StallRow aggregates over HTTP and NEVER reaches into
    the sibling's database or invents its rows.
  * Catalogue state — StallRow's own CDP index snapshot store
    (services/indexfeed.py): what the latest catalogue snapshot
    holds for this resource, if anything.

Every section names its source and its state ("ok" / "no_records" /
"not_listed" / "unavailable"). A source that fails is reported
unavailable — the timeline degrades honestly instead of filling
the gap. Events merge into one list ordered by checked_at.
"""

from __future__ import annotations

import httpx

from . import indexfeed as indexfeed_svc
from . import orchestrate as orchestrate_svc
from . import recheck as recheck_svc

_RV_TIMEOUT_S = 6.0


def _catalogue_block(index_store, url: str) -> tuple[dict | None, str]:
    """(block, source_state) from the newest index snapshot."""
    try:
        snapshots = index_store.snapshots()
    except Exception:  # noqa: BLE001 - a broken store is 'unavailable'
        return None, "unavailable"
    if not snapshots:
        return None, "unavailable"
    newest = snapshots[-1]
    try:
        entry = index_store.entries(newest["id"]).get(url)
    except Exception:  # noqa: BLE001
        return None, "unavailable"
    if entry is None:
        return None, "not_listed"
    return {
        "source": "cdp_index_snapshot",
        "snapshot_id": newest["id"],
        "captured_at": newest["captured_at"],
        "entry": entry,
    }, "ok"


async def build_timeline(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    index_store: indexfeed_svc.IndexStore,
    url: str,
) -> dict:
    url = url.strip()
    events: list[dict] = []
    sources: dict[str, str] = {}

    rows = store.feed_rows(url=url, limit=1000, ascending=True)
    for env in rows:
        record = env["record"]
        events.append(
            {
                "source": "recheck",
                "seq": env["seq"],
                "record_id": env["record_id"],
                "checked_at": record.get("checked_at"),
                "kind": record.get("kind"),
                "status": record.get("status"),
                "summary": record.get("summary"),
                "changes": record.get("changes", []),
                "field_changes": record.get("field_changes", []),
                "current": record.get("current"),
                # Improvement 10: corrections surface beside the
                # event they amend (the record itself is untouched).
                "corrections": env.get("corrections", []),
            }
        )
    sources["recheck"] = "ok" if rows else "no_records"

    rv_base = orchestrate_svc._base("route_verdict")
    try:
        resp = await client.get(
            f"{rv_base}/v1/history",
            params={"resource_url": url},
            timeout=_RV_TIMEOUT_S,
        )
        resp.raise_for_status()
        checks = resp.json().get("checks") or []
        for check in checks:
            events.append(
                {
                    "source": "route_verdict",
                    "checked_at": check.get("checked_at"),
                    "verdict": check.get("verdict"),
                    "score": check.get("score"),
                    "diff": check.get("diff"),
                    "terms": check.get("terms"),
                    "catalog": check.get("catalog"),
                }
            )
        sources["route_verdict"] = "ok" if checks else "no_records"
    except Exception:  # noqa: BLE001 - degraded, never fabricated
        sources["route_verdict"] = "unavailable"

    catalogue, catalogue_state = _catalogue_block(index_store, url)
    sources["catalogue_index"] = catalogue_state

    events.sort(key=lambda e: e.get("checked_at") or "")
    return {
        "url": url,
        "sources": sources,
        "catalogue": catalogue,
        "events": events,
    }
