"""Verdict Index: a changelog over the CDP Bazaar catalog.

The catalog is the agent economy's shop window, and it changes
constantly — prices move, payTos rotate, listings appear and vanish —
but nobody sells the diff. This module snapshots the catalog
(public metadata only: resource, service name, price, asset, network,
payTo, description) into the recheck store's sqlite file, and diffs
consecutive snapshots into change events.

Refresh discipline: a full pull is ~80 paginated requests, far too
heavy to run inside a paid call. A background ticker owns refreshes
(startup + every STALLROW_INDEX_REFRESH_MIN_S, default 6h); the paid
route only reads the two newest snapshots. Until two snapshots exist
there is no honest diff to sell, so the route answers 502 — and an
unsettled 502 charges nobody (fleet rule). While under two
snapshots the ticker refreshes on a short bootstrap interval
(STALLROW_INDEX_BOOTSTRAP_MIN_S, default 900s) so a cold start
becomes sellable in about one tick, not one full window.

Diff scope, stated plainly: events are computed over the fields a
buyer's risk actually turns on — amount, asset, network, payTo,
description, serviceName. lastUpdated churn is ignored (it moves on
every indexer touch and would drown the signal). Added/removed
resources are counted in the summary; field changes on surviving
resources are the headline events.
"""

from __future__ import annotations

import json
import sqlite3
import time

import httpx

from ..config import CDP_DISCOVERY_RESOURCES_URL
from ..rpc import UpstreamError

PAGE_LIMIT = 1000
MAX_PAGES = 200
KEEP_SNAPSHOTS = 3
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 500
# The catalog pull identifies itself and takes a per-request timeout
# sized for ~3 MB pages; the proven one-off baseline pull used the
# same shape. The shared fetch client's defaults (generic UA, the
# page-fetch timeout) are sized for single pages, not an 80-page walk.
INDEX_USER_AGENT = "verdict-row-index/1.0"
PAGE_TIMEOUT_S = 60.0
DEFAULT_BOOTSTRAP_INTERVAL_S = 900.0

TRACKED_FIELDS = ("amount", "asset", "network", "payTo", "description", "serviceName")


def _now_iso(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch or time.time()))


def normalize_item(item: dict) -> dict | None:
    """One catalog item -> one stored row (first priced accepts entry
    wins, matching seller.price_check's convention)."""
    if not isinstance(item, dict):
        return None
    resource = item.get("resource")
    if not isinstance(resource, str) or not resource:
        return None
    acc = None
    for a in item.get("accepts") or []:
        if isinstance(a, dict) and str(a.get("amount", "")).isdigit():
            acc = a
            break
    quality = item.get("quality") or {}
    return {
        "resource": resource,
        "serviceName": item.get("serviceName"),
        "description": item.get("description"),
        "amount": acc.get("amount") if acc else None,
        "asset": acc.get("asset") if acc else None,
        "network": acc.get("network") if acc else None,
        "payTo": acc.get("payTo") if acc else None,
        "lastUpdated": item.get("lastUpdated"),
        "calls30d": quality.get("l30DaysTotalCalls"),
        "payers30d": quality.get("l30DaysUniquePayers"),
    }


async def fetch_catalogue(client: httpx.AsyncClient) -> list[dict]:
    """Full public catalog pull, normalized. Any failure is an
    UpstreamError — a partial snapshot is never stored as truth."""
    rows: list[dict] = []
    offset = 0
    pages = 0
    while pages < MAX_PAGES:
        try:
            resp = await client.get(
                CDP_DISCOVERY_RESOURCES_URL,
                params={"offset": offset, "limit": PAGE_LIMIT},
                headers={"User-Agent": INDEX_USER_AGENT},
                timeout=PAGE_TIMEOUT_S,
            )
        except httpx.HTTPError as e:
            raise UpstreamError(f"CDP catalog fetch failed: {e}") from e
        if resp.status_code != 200:
            raise UpstreamError(f"CDP catalog returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise UpstreamError("CDP catalog returned non-JSON") from e
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise UpstreamError("CDP catalog returned an unexpected shape")
        pages += 1
        for it in items:
            row = normalize_item(it)
            if row is not None:
                rows.append(row)
        total = (body.get("pagination") or {}).get("total")
        offset += len(items)
        if not items or (isinstance(total, int) and offset >= total):
            break
    # The catalog can repeat a resource across pages while it churns;
    # last sighting wins.
    deduped: dict[str, dict] = {}
    for r in rows:
        deduped[r["resource"]] = r
    return list(deduped.values())


class IndexStore:
    """Snapshot tables living in the same sqlite file as the recheck
    store (same volume, same durability)."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                captured_epoch REAL NOT NULL,
                resource_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS index_entries (
                snapshot_id INTEGER NOT NULL,
                resource TEXT NOT NULL,
                service_name TEXT,
                description TEXT,
                amount TEXT,
                asset TEXT,
                network TEXT,
                pay_to TEXT,
                last_updated TEXT,
                calls30d INTEGER,
                payers30d INTEGER,
                PRIMARY KEY (snapshot_id, resource)
            );
            """
        )
        self._conn.commit()

    def save_snapshot(self, rows: list[dict], epoch: float | None = None) -> int:
        epoch = epoch or time.time()
        cur = self._conn.execute(
            "INSERT INTO index_snapshots (captured_at, captured_epoch, resource_count)"
            " VALUES (?, ?, ?)",
            (_now_iso(epoch), epoch, len(rows)),
        )
        sid = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT OR REPLACE INTO index_entries (snapshot_id, resource,"
            " service_name, description, amount, asset, network, pay_to,"
            " last_updated, calls30d, payers30d)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sid,
                    r["resource"],
                    r.get("serviceName"),
                    r.get("description"),
                    r.get("amount"),
                    r.get("asset"),
                    r.get("network"),
                    r.get("payTo"),
                    r.get("lastUpdated"),
                    r.get("calls30d"),
                    r.get("payers30d"),
                )
                for r in rows
            ],
        )
        # Prune to the newest KEEP_SNAPSHOTS snapshots.
        old = [
            row["id"]
            for row in self._conn.execute(
                "SELECT id FROM index_snapshots ORDER BY id DESC LIMIT -1 OFFSET ?",
                (KEEP_SNAPSHOTS,),
            )
        ]
        for oid in old:
            self._conn.execute("DELETE FROM index_entries WHERE snapshot_id = ?", (oid,))
            self._conn.execute("DELETE FROM index_snapshots WHERE id = ?", (oid,))
        self._conn.commit()
        return sid

    def snapshots(self) -> list[dict]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, captured_at, captured_epoch, resource_count"
                " FROM index_snapshots ORDER BY id"
            )
        ]

    def entries(self, snapshot_id: int) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in self._conn.execute(
            "SELECT resource, service_name, description, amount, asset,"
            " network, pay_to, last_updated, calls30d, payers30d"
            " FROM index_entries WHERE snapshot_id = ?",
            (snapshot_id,),
        ):
            d = dict(row)
            out[d["resource"]] = {
                "resource": d["resource"],
                "serviceName": d["service_name"],
                "description": d["description"],
                "amount": d["amount"],
                "asset": d["asset"],
                "network": d["network"],
                "payTo": d["pay_to"],
                "lastUpdated": d["last_updated"],
                "calls30d": d["calls30d"],
                "payers30d": d["payers30d"],
            }
        return out

    def latest_two(self) -> tuple[dict, dict] | None:
        snaps = self.snapshots()
        if len(snaps) < 2:
            return None
        return snaps[-2], snaps[-1]


def diff_entries(
    old: dict[str, dict],
    new: dict[str, dict],
    *,
    resource_contains: str | None = None,
    pay_to: str | None = None,
    service_name: str | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> dict:
    """Pure diff over two normalized entry maps. Summary counts are
    pre-filter; the events list is filtered + capped, field changes
    on surviving resources first (the signal), then adds, then
    removals (the noise)."""
    limit = max(1, min(int(limit or DEFAULT_EVENT_LIMIT), MAX_EVENT_LIMIT))
    changed: list[dict] = []
    added: list[dict] = []
    removed: list[dict] = []
    for resource, nrow in new.items():
        orow = old.get(resource)
        if orow is None:
            added.append({"resource": resource, "kind": "added", "serviceName": nrow.get("serviceName")})
            continue
        for field in TRACKED_FIELDS:
            if orow.get(field) != nrow.get(field):
                changed.append(
                    {
                        "resource": resource,
                        "kind": "field_changed",
                        "field": field,
                        "before": orow.get(field),
                        "after": nrow.get(field),
                    }
                )
    for resource, orow in old.items():
        if resource not in new:
            removed.append({"resource": resource, "kind": "removed", "serviceName": orow.get("serviceName")})

    def keep(ev: dict) -> bool:
        row = new.get(ev["resource"]) or old.get(ev["resource"]) or {}
        if resource_contains and resource_contains not in ev["resource"]:
            return False
        if pay_to and (row.get("payTo") or "").lower() != pay_to.lower():
            return False
        if service_name and service_name.lower() not in (row.get("serviceName") or "").lower():
            return False
        return True

    events = [e for e in (changed + added + removed) if keep(e)]
    return {
        "summary": {
            "resources_before": len(old),
            "resources_after": len(new),
            "field_changes": len(changed),
            "added": len(added),
            "removed": len(removed),
        },
        "events_returned": min(len(events), limit),
        "events": events[:limit],
    }


async def refresh_if_stale(
    store: IndexStore,
    client: httpx.AsyncClient,
    min_interval_s: float,
    bootstrap_interval_s: float = DEFAULT_BOOTSTRAP_INTERVAL_S,
) -> bool:
    """Store a fresh snapshot when the newest is older than the
    staleness window (or none exists). Returns True when a snapshot
    was stored. Fetch failures propagate as UpstreamError and store
    nothing.

    Bootstrap rule: while fewer than two snapshots exist there is
    no diff to sell at all, so the window shrinks to
    bootstrap_interval_s — a cold start (or a store wiped back
    under two) reaches a sellable pair in about one tick instead
    of waiting out a full refresh window. Once a pair exists the
    normal window rules, so steady state still pulls ~4x/day."""
    snaps = store.snapshots()
    window = min_interval_s
    if len(snaps) < 2:
        window = min(window, bootstrap_interval_s)
    if snaps and (time.time() - snaps[-1]["captured_epoch"]) < window:
        return False
    rows = await fetch_catalogue(client)
    store.save_snapshot(rows)
    return True


def changes(store: IndexStore, **filters) -> dict:
    """The paid route's read: diff of the two newest snapshots.
    Raises UpstreamError until two snapshots exist — no baseline,
    no diff, no charge."""
    pair = store.latest_two()
    if pair is None:
        raise UpstreamError(
            "Verdict Index baseline is still building: fewer than two catalog"
            " snapshots stored, so there is no honest diff to sell yet."
        )
    before, after = pair
    result = diff_entries(store.entries(before["id"]), store.entries(after["id"]), **filters)
    return {
        "baseline_at": before["captured_at"],
        "current_at": after["captured_at"],
        "generated_at": _now_iso(),
        **result,
    }
