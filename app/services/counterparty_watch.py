"""Counterparty Watch: POST /v1/counterparty-watch ($0.03).

Repeat-buy delta product: each call computes the wallet's
counterparty profile snapshot (counterparty_deep.profile_snapshot),
stores it, and returns the DELTA against the previous stored
snapshot for that wallet — new counterparties, changed per-token
volumes, new flags. The first call for a wallet stores the
baseline and says so (status "baseline_established", delta null).

Store design: one sqlite table, `counterparty_snapshots`, in the
service's existing persistent store file — the same file the
recheck WatchStore resolves (config.resolve_recheck_db: the
Railway volume at /data when mounted, else STALLROW_RECHECK_DB,
else ./recheck.db) — so watch baselines inherit exactly the
recheck store's measured durability, reported in /health. One
row per wallet (address PRIMARY KEY, snapshot JSON, updated_at);
each call atomically replaces the row with the new snapshot
after the delta is computed. No history is kept beyond the
previous snapshot — the delta IS the product.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc
from . import counterparty_deep
from .indexfeed import IndexStore


class CounterpartyWatchStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS counterparty_snapshots (
                    address TEXT PRIMARY KEY,
                    snapshot TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, address: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot FROM counterparty_snapshots WHERE address = ?",
                (address.lower(),),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def put(self, address: str, snapshot: dict) -> None:
        updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO counterparty_snapshots (address, snapshot, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(address) DO UPDATE SET snapshot = excluded.snapshot,"
                " updated_at = excluded.updated_at",
                (address.lower(), json.dumps(snapshot), updated),
            )


def compute_delta(previous: dict, current: dict) -> dict:
    """Crisp, machine-readable diff between two profile snapshots."""
    prev_cps = previous.get("counterparties") or {}
    curr_cps = current.get("counterparties") or {}
    new_counterparties = sorted(set(curr_cps) - set(prev_cps))
    gone_counterparties = sorted(set(prev_cps) - set(curr_cps))
    changed_volumes = []
    for addr in sorted(set(prev_cps) & set(curr_cps)):
        prev_vol = prev_cps[addr].get("volumes_raw_by_token") or {}
        curr_vol = curr_cps[addr].get("volumes_raw_by_token") or {}
        for symbol in sorted(set(prev_vol) | set(curr_vol)):
            if prev_vol.get(symbol) != curr_vol.get(symbol):
                changed_volumes.append(
                    {
                        "counterparty": addr,
                        "token": symbol,
                        "previous_volume_raw": prev_vol.get(symbol),
                        "current_volume_raw": curr_vol.get(symbol),
                    }
                )
    prev_flags = {
        json.dumps(f, sort_keys=True) for f in (previous.get("flags") or [])
    }
    new_flags = [
        f for f in (current.get("flags") or [])
        if json.dumps(f, sort_keys=True) not in prev_flags
    ]
    return {
        "new_counterparties": new_counterparties,
        "counterparties_no_longer_in_sample": gone_counterparties,
        "changed_volumes": changed_volumes,
        "new_flags": new_flags,
        "balance_changed": previous.get("eth_balance_wei") != current.get("eth_balance_wei"),
        "previous_eth_balance_wei": previous.get("eth_balance_wei"),
        "current_eth_balance_wei": current.get("eth_balance_wei"),
        "nonce_delta": (
            (current.get("outgoing_tx_count_nonce") or 0)
            - (previous.get("outgoing_tx_count_nonce") or 0)
            if current.get("outgoing_tx_count_nonce") is not None
            and previous.get("outgoing_tx_count_nonce") is not None
            else None
        ),
        "previous_generated_at": previous.get("generated_at"),
        "current_generated_at": current.get("generated_at"),
    }


async def run_watch(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    index_store: IndexStore,
    watch_store: CounterpartyWatchStore,
    address: str,
) -> dict:
    current = await counterparty_deep.profile_snapshot(
        client, rpc, scout, index_store, address
    )
    previous = watch_store.get(address)
    watch_store.put(address, current)
    if previous is None:
        return {
            "address": address,
            "status": "baseline_established",
            "delta": None,
            "profile": current,
            "note": (
                "First watch call for this wallet: the current profile "
                "was stored as the baseline. Buy again later to receive "
                "the delta against it."
            ),
        }
    return {
        "address": address,
        "status": "delta",
        "delta": compute_delta(previous, current),
        "profile": current,
        "note": (
            "Delta is computed against the previous stored snapshot "
            "for this wallet (one snapshot kept per wallet; the "
            "counterparty window is the newest sampled transfers — "
            "see profile.coverage)."
        ),
    }
