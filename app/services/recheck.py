"""The recheck stall: scheduled change detection for x402 resources
and plain web pages — the row's first monitoring-loop product.

A loop calls POST /v1/recheck for the same target on a schedule. Each
call probes the target WITHOUT paying it and answers BASELINE,
UNCHANGED, or CHANGED with a field-by-field diff against the last
check stored for that watch:

  * x402 resource — fingerprint is the unpaid 402 challenge: status,
    amount, asset, network, payTo, a hash of the description, and a
    hash of the Bazaar output schema.
  * web page — fingerprint is the HTTP status plus the sha256 of the
    normalized body, read under the same hardened fetch discipline as
    page-to-markdown (SSRF check on the target and on every redirect
    hop, capped body, retry-with-backoff on transient failures).

State is one sqlite file (the RouteVerdict store pattern) holding
BOTH the watch baselines and the watch-pack balances. Durability
follows the file's location (config.resolve_recheck_db): under a
mounted volume the state survives redeploys; on the service's own
disk a redeploy wipes it and every watch answers BASELINE again.
Which mode is live is measured at startup, logged, and reported in
/health; production currently reports persistent storage, and the
route's own docs state that verified behavior.

Failure honesty: a probe that fails (unreachable, or a non-402
4xx/5xx left after the retry discipline) raises UpstreamError, the
handler returns 502, no payment settles, and the stored fingerprint
is NOT overwritten — the next good probe still diffs against the
last good state.

Feed: every COMPLETED check's response is also appended to a
history table in the same store, and GET /v1/recheck/feed serves
the recent ones as JSONL, free — the observatory surface promised
to the aicom survey (alexar76/aicom#13), so other graders can read
our own check responses next to their results. History begins when
the table does: watches that already existed are seeded with ONE
record built only from what the watches table persisted (watch id,
URL, kind, the stored fingerprint as `current`, checked_at). The
per-check status/changes/summary of those earlier checks were
never stored, so seeded records omit them — the feed serves what
the store holds, never a reconstruction.

Feed v2 + baselines + webhooks (2026-10-29, aicom improvements
1–5): every history record also carries a seq, a record_id, and a
hash chain (see FEED_SCHEMA_VERSION); the default feed body is
unchanged, the envelope rides the v2 parameters. A baseline PIN
(one recorded snapshot, internal-secret gated at the HTTP layer)
governs a watch's diff while it exists; an imported baseline is
validated by recomputation before it is stored and carries its
provenance. Results carry a structured field_changes array beside
the legacy changes[]. A watch may register one change webhook
(internal-secret gated): CHANGED results — never BASELINE or
UNCHANGED — are delivered as HMAC-signed events with bounded
retries and a readable dead-letter. The unified per-resource
timeline lives in services/timeline.py.

Improvements 6–10 (2026-10-29): the fingerprint specification is
published as implemented (services/fingerprint_spec.py, served at
GET /v1/recheck/fingerprint-spec, test vectors asserted against
these very functions); a mutable sandbox fixture lives in
services/sandbox.py (state in this store, mutations internal-
secret gated); every completed check's record carries a `probe`
object (latency / attempts / redirects / flapping — only what the
probe engine actually measures; failed probes still raise and are
never recorded, so their metadata exists nowhere) and, when the
caller supplies the catalogue index store, a `catalogue_crosscheck`
frozen at check time; and history records can be CORRECTED through
an append-only corrections table — the original record is never
mutated, corrections live outside the feed hash chain, and every
read surfaces them beside the record they amend.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from ..rpc import UpstreamError
from . import fetch as fetch_svc

log = logging.getLogger("stall-row.recheck")

# Same retry discipline as the page fetcher: transient statuses and
# transport errors retry with backoff; deterministic failures raise.
_ATTEMPTS = fetch_svc._FETCH_ATTEMPTS
_BACKOFF_S = fetch_svc._BACKOFF_S
_RETRY_STATUSES = fetch_svc._RETRY_STATUSES
_PROBE_STATUSES = frozenset({200, 402})  # bodies worth reading/hashing


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _iso_of(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# --- watch packs ---------------------------------------------------------------
#
# A pack is the prepaid shape of this stall: one $1.00 payment buys
# PACK_CREDITS change-checks against ONE endpoint URL, presented by
# pack id instead of a per-call payment. Packs live in the SAME sqlite
# store as the watch baselines (WatchStore below) — no parallel state.
# A pack expires PACK_TTL_DAYS after purchase; expiry is stated in the
# buy response and the listing copy, and an expired pack refuses
# plainly rather than silently falling back to a per-call charge.

PACK_CREDITS = 250
PACK_PRICE_UNITS = 1_000_000  # $1.00 — mirrors config.PRICE_UNITS
PACK_TTL_DAYS = 90
_PACK_ID_PREFIX = "pack_"

# The public feed's window: GET /v1/recheck/feed serves at most
# this many of the most recent recorded check responses.
FEED_LIMIT = 100

# --- feed v2: sequence + hash chain -------------------------------------
# Every history record also carries a monotonically increasing seq,
# a record_id, a schema_version, and a hash chain (prev_hash /
# record_hash over the record's canonical JSON). A poller reading
# with ?since_seq= can advance a cursor and PROVE nothing was
# silently dropped: seqs are continuous and each record_hash
# commits to its content and its predecessor. The legacy default
# feed shape (bare response objects) is unchanged; the envelope is
# served when a v2 parameter is present (see main.py).
FEED_SCHEMA_VERSION = 1
_FEED_GENESIS = "genesis"


def _canonical_record_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _chain_hash(prev_hash: str, record: dict) -> str:
    return hashlib.sha256(
        f"{prev_hash}\n{_canonical_record_json(record)}".encode("utf-8")
    ).hexdigest()

# --- fleet check packs -----------------------------------------------------------
#
# The standing form of Fleet Check (services/fleetcheck.py): one
# $5.00 payment buys FLEET_CHECK_PACK_CREDITS sweep credits bound to
# ONE FIXED fleet definition (the buyer's resource/origin list),
# stored in the same sqlite file as everything else here. A sweep
# credit buys a whole sweep of the definition — never one route —
# so a 40-route fleet and a 2-route fleet spend identically per run.
# The constants live in this module (like PACK_* above) because the
# store enforces them; fleetcheck.py aliases them for its callers.

FLEET_CHECK_PACK_CREDITS = 100
FLEET_CHECK_PACK_PRICE_UNITS = 5_000_000  # $5.00 — mirrors config.PRICE_UNITS
FLEET_CHECK_PACK_TTL_DAYS = 90
_FC_PACK_ID_PREFIX = "fcpack_"


def new_pack_id() -> str:
    """128 bits of CSPRNG entropy, hex-encoded behind a readable
    prefix. The id is the only credential — it is never derived from
    the endpoint, the buyer, or the payment."""
    return _PACK_ID_PREFIX + secrets.token_hex(16)


class PackError(Exception):
    """A pack spend/balance refusal. `code` is machine-stable; the
    handler maps it to `http_status`. Nothing is charged or
    decremented on any PackError path."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _pack_view(row: dict, now_epoch: float | None = None) -> dict:
    now = time.time() if now_epoch is None else now_epoch
    return {
        "pack_id": row["pack_id"],
        "url": row["url"],
        "credits_total": row["credits_total"],
        "credits_remaining": row["credits_remaining"],
        "created_at": _iso_of(row["created_epoch"]),
        "expires_at": _iso_of(row["expires_epoch"]),
        "expired": now >= row["expires_epoch"],
    }


def default_watch_id(url: str) -> str:
    """The implicit watch for a URL: first 16 hex chars of its sha256."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- state ---------------------------------------------------------------------

class WatchStore:
    """One row per watch: the last good fingerprint and when it was
    taken. Keyed by watch_id + URL so two watches over one target —
    and one watch id reused across targets — never collide. Watch
    packs (below) share the same file and connection.

    `persistent` is the caller's MEASURED durability of `path`
    (config.resolve_recheck_db): True only when the file sits on a
    mounted volume. It is carried for reporting (startup log,
    /health); the store itself behaves identically either way."""

    def __init__(self, path: str, persistent: bool | None = None):
        self._path = path
        self.persistent = persistent
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(path) and os.path.getsize(path) > 0
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if existed:
            log.info(
                "recheck store: reopened %s (durability: %s)",
                path,
                "persistent" if persistent else "ephemeral",
            )
        else:
            # Never fabricate continuity: a fresh file means fresh
            # state, and the log says so in plain terms.
            log.warning(
                "recheck store: no existing store at %s — starting "
                "EMPTY: baselines re-baseline and no packs carry over "
                "(durability: %s)",
                path,
                "persistent" if persistent else "ephemeral",
            )
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_packs (
                    pack_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    credits_total INTEGER NOT NULL,
                    credits_remaining INTEGER NOT NULL,
                    created_epoch REAL NOT NULL,
                    expires_epoch REAL NOT NULL,
                    payment_key TEXT NOT NULL UNIQUE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_passes (
                    pass_id TEXT PRIMARY KEY,
                    balance_units INTEGER NOT NULL,
                    total_units INTEGER NOT NULL,
                    created_epoch REAL NOT NULL,
                    expires_epoch REAL NOT NULL,
                    payment_key TEXT NOT NULL UNIQUE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_pass_debits (
                    debit_id TEXT PRIMARY KEY,
                    pass_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    route TEXT NOT NULL,
                    price_units INTEGER NOT NULL,
                    created_epoch REAL NOT NULL,
                    refunded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watch_regs (
                    watch_reg_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    webhook_url TEXT,
                    email TEXT,
                    interval_s INTEGER NOT NULL,
                    funding_kind TEXT NOT NULL,
                    funding_id TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    last_run_epoch REAL,
                    last_status TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watch_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_reg_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    delivered_epoch REAL NOT NULL,
                    http_status INTEGER,
                    error TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rotation_regs (
                    reg_id TEXT PRIMARY KEY,
                    pay_to TEXT,
                    resource TEXT,
                    webhook_url TEXT,
                    email TEXT,
                    funding_id TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    last_eval_snapshot_id INTEGER,
                    last_status TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist_regs (
                    reg_id TEXT PRIMARY KEY,
                    top_n INTEGER NOT NULL,
                    webhook_url TEXT,
                    email TEXT,
                    funding_id TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    last_eval_snapshot_id INTEGER,
                    last_status TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_subs (
                    reg_id TEXT PRIMARY KEY,
                    resource_contains TEXT,
                    pay_to TEXT,
                    service_name TEXT,
                    network TEXT,
                    webhook_url TEXT,
                    email TEXT,
                    funding_id TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    last_eval_snapshot_id INTEGER,
                    last_status TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_check_packs (
                    pack_id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    webhook_url TEXT,
                    email TEXT,
                    heartbeat INTEGER NOT NULL DEFAULT 0,
                    interval_s INTEGER NOT NULL,
                    credits_total INTEGER NOT NULL,
                    credits_remaining INTEGER NOT NULL,
                    created_epoch REAL NOT NULL,
                    expires_epoch REAL NOT NULL,
                    last_run_epoch REAL,
                    payment_key TEXT NOT NULL UNIQUE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_check_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id TEXT NOT NULL,
                    ran_epoch REAL NOT NULL,
                    result_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_check_free_claims (
                    wallet TEXT PRIMARY KEY,
                    claimed_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_payment_txs (
                    tx_hash TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    buyer_wallet TEXT NOT NULL,
                    consumed_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nudges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    sender TEXT NOT NULL,
                    stall TEXT NOT NULL,
                    route TEXT NOT NULL,
                    defect_code TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    received_epoch REAL NOT NULL,
                    message_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nudge_optouts (
                    stall TEXT PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    received_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_challenges (
                    token TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    expires_epoch REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_sessions (
                    session_token TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    expires_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claimed_origins (
                    origin TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    wallet TEXT,
                    claimed_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_inboxes (
                    origin TEXT PRIMARY KEY,
                    origin_key TEXT NOT NULL UNIQUE,
                    created_epoch REAL NOT NULL,
                    webhook_url TEXT,
                    webhook_enabled INTEGER NOT NULL DEFAULT 0,
                    email TEXT,
                    email_enabled INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_knocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    route TEXT NOT NULL,
                    defect_code TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    received_epoch REAL NOT NULL,
                    message_json TEXT NOT NULL,
                    UNIQUE (origin, dedupe_key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_epoch REAL NOT NULL,
                    detail TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claims_history_snapshots (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    uploaded_epoch REAL NOT NULL,
                    doc_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_key TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    response_json TEXT NOT NULL
                )
                """
            )
            # Feed v2 (seq + hash chain), baseline pins, and the
            # per-watch change webhooks. All additive: existing rows
            # are backfilled deterministically by _backfill_chain.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feed_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_baseline_pins (
                    watch_key TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    record_id TEXT NOT NULL,
                    pinned_at TEXT NOT NULL,
                    source TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_webhooks (
                    watch_key TEXT PRIMARY KEY,
                    webhook_url TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_epoch REAL NOT NULL,
                    updated_epoch REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    watch_key TEXT NOT NULL,
                    seq INTEGER,
                    url TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_http_status INTEGER,
                    last_error TEXT,
                    created_epoch REAL NOT NULL,
                    updated_epoch REAL NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_webhook_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id INTEGER NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    epoch REAL NOT NULL,
                    http_status INTEGER,
                    error TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_webhook_subs (
                    sub_id TEXT PRIMARY KEY,
                    callback_url TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    filter_url TEXT,
                    filter_origin TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_epoch REAL NOT NULL,
                    updated_epoch REAL NOT NULL
                )
                """
            )
            # Corrections (improvement 10): append-only amendments
            # to history records. The original record's stored JSON
            # is NEVER touched — a correction is its own row, linked
            # to the record_id + record_hash it amends, and lives
            # outside the feed hash chain (the chain commits to the
            # records as recorded; corrections are served beside
            # them, never inside them).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recheck_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    correction_id TEXT NOT NULL UNIQUE,
                    record_id TEXT NOT NULL,
                    seq INTEGER,
                    record_hash TEXT,
                    reason TEXT NOT NULL,
                    corrected_fields_json TEXT NOT NULL,
                    source TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Sandbox fixture state (improvement 7): one row, the
            # mutable terms of the clearly-labelled test resource
            # in services/sandbox.py. Mutations are internal-secret
            # gated at the HTTP layer.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sandbox_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL
                )
                """
            )
            cols = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(recheck_history)"
                )
            }
            for col, decl in (
                ("seq", "INTEGER"),
                ("record_id", "TEXT"),
                ("schema_version", "INTEGER"),
                ("prev_hash", "TEXT"),
                ("record_hash", "TEXT"),
            ):
                if col not in cols:
                    self._conn.execute(
                        f"ALTER TABLE recheck_history ADD COLUMN {col} {decl}"
                    )
            self._conn.commit()
        self._seed_history_from_watches()
        self._backfill_chain()

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, fingerprint_json, checked_at FROM watches "
                "WHERE watch_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            fingerprint = json.loads(row[1])
        except ValueError:
            return None
        return {"kind": row[0], "fingerprint": fingerprint, "checked_at": row[2]}

    def save(self, key: str, kind: str, fingerprint: dict, checked_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO watches "
                "(watch_key, kind, fingerprint_json, checked_at) VALUES (?, ?, ?, ?)",
                (key, kind, json.dumps(fingerprint, sort_keys=True), checked_at),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM watches").fetchone()
        return int(row[0]) if row else 0

    # --- check history (the public JSONL feed) ------------------------
    # Every completed check appends its exact response object here;
    # GET /v1/recheck/feed serves the recent window as JSONL. The
    # watches table above keeps only the LAST fingerprint per watch,
    # so without this table the responses themselves were lost the
    # moment the next check overwrote the baseline.

    def _seed_history_from_watches(self) -> None:
        """One history record per pre-existing watch, built ONLY
        from what the watches table persisted — the per-check
        status/changes/summary of those earlier checks were never
        stored, so seeded records omit them rather than reconstruct
        them. Idempotent: a watch that already has history rows is
        never re-seeded, so restarts add nothing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT watch_key, kind, fingerprint_json, checked_at "
                "FROM watches"
            ).fetchall()
            for watch_key, kind, fingerprint_json, checked_at in rows:
                (existing,) = self._conn.execute(
                    "SELECT COUNT(*) FROM recheck_history "
                    "WHERE watch_key = ?",
                    (watch_key,),
                ).fetchone()
                if existing:
                    continue
                try:
                    fingerprint = json.loads(fingerprint_json)
                except ValueError:
                    continue
                watch_id, _, url = watch_key.partition("\n")
                record = {
                    "watch_id": watch_id,
                    "url": url,
                    "kind": kind,
                    "current": fingerprint,
                    "checked_at": checked_at,
                }
                self._conn.execute(
                    "INSERT INTO recheck_history "
                    "(watch_key, checked_at, response_json) "
                    "VALUES (?, ?, ?)",
                    (watch_key, checked_at, json.dumps(record, sort_keys=True)),
                )
            self._conn.commit()

    # --- feed v2: chain state --------------------------------------
    # feed_meta carries the chain head: next_seq + last record_hash.
    # Backfill and live appends share _chain_locked so the chain is
    # built exactly one way.

    def _feed_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM feed_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_feed_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO feed_meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _backfill_chain(self) -> None:
        """Assign seq/record_id/hashes to every history row that
        lacks them, oldest first. Deterministic: the hash input is
        the stored response JSON itself, so re-running (or running
        on a copy of the store) reproduces the identical chain, and
        a store whose rows are already chained is untouched."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, response_json, seq, record_hash "
                "FROM recheck_history ORDER BY id ASC"
            ).fetchall()
            next_seq = self._feed_meta("next_seq")
            seq = int(next_seq) if next_seq is not None else None
            prev_hash = self._feed_meta("last_hash") or _FEED_GENESIS
            if seq is None:
                chained = [r for r in rows if r[2] is not None]
                seq = (max(r[2] for r in chained) + 1) if chained else 1
                if chained:
                    head = max(chained, key=lambda r: r[2])
                    prev_hash = head[3] or _FEED_GENESIS
            for row_id, raw, row_seq, _rh in rows:
                if row_seq is not None:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                record_hash = _chain_hash(prev_hash, record)
                record_id = f"rec_{record_hash[:24]}"
                self._conn.execute(
                    "UPDATE recheck_history SET seq = ?, record_id = ?, "
                    "schema_version = ?, prev_hash = ?, record_hash = ? "
                    "WHERE id = ?",
                    (
                        seq,
                        record_id,
                        FEED_SCHEMA_VERSION,
                        prev_hash,
                        record_hash,
                        row_id,
                    ),
                )
                prev_hash = record_hash
                seq += 1
            self._set_feed_meta("next_seq", str(seq))
            self._set_feed_meta("last_hash", prev_hash)
            self._conn.commit()

    _HISTORY_COLS = (
        "seq, record_id, schema_version, prev_hash, record_hash, response_json"
    )

    def _corrections_for_locked(self, record_id: str) -> list[dict]:
        """Corrections amending one record, oldest first. Caller
        holds the store lock (every envelope read path does)."""
        rows = self._conn.execute(
            "SELECT correction_id, record_id, seq, record_hash, "
            "reason, corrected_fields_json, source, created_at "
            "FROM recheck_corrections WHERE record_id = ? "
            "ORDER BY id ASC",
            (record_id,),
        ).fetchall()
        return [
            {
                "correction_id": r[0],
                "record_id": r[1],
                "seq": r[2],
                "record_hash": r[3],
                "reason": r[4],
                "corrected_fields": json.loads(r[5]),
                "source": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    def _row_envelope(self, row) -> dict | None:
        try:
            record = json.loads(row[5])
        except ValueError:
            return None
        return {
            "seq": row[0],
            "record_id": row[1],
            "schema_version": row[2],
            "prev_hash": row[3],
            "record_hash": row[4],
            "record": record,
            # Improvement 10: amendments ride BESIDE the record in
            # every envelope read — the record itself is untouched.
            "corrections": self._corrections_for_locked(row[1]),
        }

    def record_check(self, response: dict) -> dict:
        """Append one COMPLETED check's response to the history and
        return its feed envelope ({seq, record_id, schema_version,
        prev_hash, record_hash, record}). Called by recheck()
        itself, so per-call, pack-spent, and scheduled-watch checks
        are all recorded exactly once, with the exact object the
        caller was answered with."""
        key = f"{response['watch_id']}\n{response['url']}"
        with self._lock:
            next_seq = self._feed_meta("next_seq")
            seq = int(next_seq) if next_seq is not None else 1
            prev_hash = self._feed_meta("last_hash") or _FEED_GENESIS
            record_hash = _chain_hash(prev_hash, response)
            record_id = f"rec_{record_hash[:24]}"
            self._conn.execute(
                "INSERT INTO recheck_history "
                "(watch_key, checked_at, response_json, seq, record_id, "
                "schema_version, prev_hash, record_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    response["checked_at"],
                    json.dumps(response, sort_keys=True),
                    seq,
                    record_id,
                    FEED_SCHEMA_VERSION,
                    prev_hash,
                    record_hash,
                ),
            )
            self._set_feed_meta("next_seq", str(seq + 1))
            self._set_feed_meta("last_hash", record_hash)
            self._conn.commit()
        return {
            "seq": seq,
            "record_id": record_id,
            "schema_version": FEED_SCHEMA_VERSION,
            "prev_hash": prev_hash,
            "record_hash": record_hash,
            "record": response,
            "corrections": [],
        }

    def recent_checks(self, limit: int = FEED_LIMIT) -> list[dict]:
        """The feed window: the most recent recorded check
        responses, newest first. A record that carries corrections
        surfaces them under a `corrections` key attached at serve
        time — the stored JSON is never rewritten, and uncorrected
        records keep the exact legacy shape."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT response_json, record_id FROM recheck_history "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        out = []
        for raw, record_id in rows:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record_id:
                corrections = self.corrections_for(record_id)
                if corrections:
                    record["corrections"] = corrections
            out.append(record)
        return out

    def check_history_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM recheck_history"
            ).fetchone()
        return int(row[0]) if row else 0

    def latest_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM recheck_history"
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    @staticmethod
    def _like_escape(text: str) -> str:
        return (
            text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )

    def feed_rows(
        self,
        watch_id: str | None = None,
        url: str | None = None,
        since_seq: int | None = None,
        limit: int = FEED_LIMIT,
        ascending: bool = False,
    ) -> list[dict]:
        """History envelopes, filtered. Cursor mode (since_seq set)
        is ascending by construction — the caller advances its
        cursor to the last seq seen; continuity of seq across pages
        is the no-silent-gap proof. Default order is newest first,
        like the legacy feed."""
        clauses: list[str] = []
        params: list = []
        if watch_id is not None and url is not None:
            clauses.append("watch_key = ?")
            params.append(f"{watch_id}\n{url}")
        elif watch_id is not None:
            clauses.append("watch_key LIKE ? ESCAPE '\\'")
            params.append(f"{self._like_escape(watch_id)}\n%")
        elif url is not None:
            clauses.append("watch_key LIKE ? ESCAPE '\\'")
            params.append(f"%\n{self._like_escape(url)}")
        if since_seq is not None:
            clauses.append("seq > ?")
            params.append(int(since_seq))
            ascending = True
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if ascending else "DESC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._HISTORY_COLS} FROM recheck_history "
                f"{where} ORDER BY seq {order} LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        out = []
        for row in rows:
            env = self._row_envelope(row)
            if env is not None:
                out.append(env)
        return out

    def record_by_seq(self, seq: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._HISTORY_COLS} FROM recheck_history "
                "WHERE seq = ?",
                (int(seq),),
            ).fetchone()
        return self._row_envelope(row) if row else None

    def record_by_id(self, record_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._HISTORY_COLS} FROM recheck_history "
                "WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._row_envelope(row) if row else None

    def verify_chain(self) -> bool:
        """Recompute the whole chain from the stored records; True
        only when every seq is continuous from 1 and every hash
        links. The feed's integrity claim, checkable in-process."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._HISTORY_COLS} FROM recheck_history "
                "ORDER BY seq ASC"
            ).fetchall()
        prev_hash = _FEED_GENESIS
        for expected_seq, row in enumerate(rows, start=1):
            env = self._row_envelope(row)
            if env is None or env["seq"] != expected_seq:
                return False
            if env["prev_hash"] != prev_hash:
                return False
            if env["record_hash"] != _chain_hash(prev_hash, env["record"]):
                return False
            prev_hash = env["record_hash"]
        return True

    # --- baseline pins + imports ------------------------------------
    # A pin makes one recorded snapshot THE baseline a watch diffs
    # against (recheck() consults pins before the latest-state row).
    # Pins and imports are mutations: the HTTP layer gates them
    # behind the fleet-internal secret (see main.py); the store just
    # keeps the state honestly, provenance included.

    def pin_baseline(self, watch_key: str, seq: int, source: str) -> dict | None:
        env = self.record_by_seq(seq)
        if env is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT watch_key FROM recheck_history WHERE seq = ?",
                (int(seq),),
            ).fetchone()
            if row is None or row[0] != watch_key:
                return None
            self._conn.execute(
                "INSERT OR REPLACE INTO recheck_baseline_pins "
                "(watch_key, seq, record_id, pinned_at, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    watch_key,
                    int(seq),
                    env["record_id"],
                    _now_iso(),
                    source,
                ),
            )
            self._conn.commit()
        return env

    def get_pin(self, watch_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, record_id, pinned_at, source FROM "
                "recheck_baseline_pins WHERE watch_key = ?",
                (watch_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "seq": row[0],
            "record_id": row[1],
            "pinned_at": row[2],
            "source": row[3],
        }

    def unpin_baseline(self, watch_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM recheck_baseline_pins WHERE watch_key = ?",
                (watch_key,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def pinned_baseline(self, watch_key: str) -> dict | None:
        """The pinned record's stored state in the watches-table
        shape ({kind, fingerprint, checked_at}), or None."""
        pin = self.get_pin(watch_key)
        if pin is None:
            return None
        env = self.record_by_seq(pin["seq"])
        if env is None:
            return None
        record = env["record"]
        fingerprint = record.get("current")
        if not isinstance(fingerprint, dict):
            return None
        return {
            "kind": record.get("kind"),
            "fingerprint": fingerprint,
            "checked_at": record.get("checked_at"),
        }

    def import_baseline(
        self,
        watch_key: str,
        kind: str,
        fingerprint: dict,
        checked_at: str,
        source: str,
    ) -> dict:
        """Store an externally supplied snapshot as the watch's
        baseline: it lands in the history as a record carrying its
        provenance (imported=true, source, imported_at) AND becomes
        the watch's stored state, so the next check diffs against
        it. Validation (fingerprint recomputation) happens in
        validate_imported_snapshot BEFORE this is called."""
        watch_id, _, url = watch_key.partition("\n")
        record = {
            "watch_id": watch_id,
            "url": url,
            "kind": kind,
            "status": "BASELINE",
            "changes": [],
            "field_changes": [],
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": (
                f"Baseline imported from {source}; the next check for "
                "this watch diffs against this snapshot."
            ),
            "imported": True,
            "provenance": {"source": source, "imported_at": _now_iso()},
        }
        envelope = self.record_check(record)
        self.save(watch_key, kind, fingerprint, checked_at)
        return envelope

    # --- per-watch change webhooks -----------------------------------
    # Config is a mutation (fleet-internal secret at the HTTP
    # layer); delivery state is fully recorded — every attempt, and
    # a terminal dead_letter after the bounded retries.

    def set_change_webhook(
        self,
        watch_key: str,
        webhook_url: str,
        secret: str,
        enabled: bool = True,
    ) -> dict:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO recheck_webhooks (watch_key, webhook_url, "
                "secret, enabled, created_epoch, updated_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(watch_key) DO "
                "UPDATE SET webhook_url = excluded.webhook_url, "
                "secret = excluded.secret, enabled = excluded.enabled, "
                "updated_epoch = excluded.updated_epoch",
                (watch_key, webhook_url, secret, 1 if enabled else 0, now, now),
            )
            self._conn.commit()
        return self.get_change_webhook(watch_key) or {}

    def get_change_webhook(self, watch_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT webhook_url, secret, enabled FROM "
                "recheck_webhooks WHERE watch_key = ?",
                (watch_key,),
            ).fetchone()
        if row is None:
            return None
        return {"webhook_url": row[0], "secret": row[1], "enabled": bool(row[2])}

    def queue_change_delivery(
        self, event_id: str, watch_key: str, seq: int | None, url: str, payload: dict
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO recheck_webhook_deliveries (event_id, "
                "watch_key, seq, url, status, created_epoch, "
                "updated_epoch, payload_json) VALUES (?, ?, ?, ?, "
                "'pending', ?, ?, ?)",
                (
                    event_id,
                    watch_key,
                    seq,
                    url,
                    now,
                    now,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_change_delivery(self, delivery_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, event_id, watch_key, seq, url, status, "
                "attempts, last_http_status, last_error, payload_json "
                "FROM recheck_webhook_deliveries WHERE id = ?",
                (int(delivery_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "event_id": row[1],
            "watch_key": row[2],
            "seq": row[3],
            "url": row[4],
            "status": row[5],
            "attempts": row[6],
            "last_http_status": row[7],
            "last_error": row[8],
            "payload": json.loads(row[9]),
        }

    def record_change_attempt(
        self,
        delivery_id: int,
        http_status: int | None,
        error: str | None,
    ) -> int:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM recheck_webhook_deliveries "
                "WHERE id = ?",
                (int(delivery_id),),
            ).fetchone()
            attempt_no = int(row[0]) + 1 if row else 1
            self._conn.execute(
                "INSERT INTO recheck_webhook_attempts (delivery_id, "
                "attempt_no, epoch, http_status, error) VALUES "
                "(?, ?, ?, ?, ?)",
                (int(delivery_id), attempt_no, now, http_status, error),
            )
            self._conn.execute(
                "UPDATE recheck_webhook_deliveries SET attempts = ?, "
                "last_http_status = ?, last_error = ?, "
                "updated_epoch = ? WHERE id = ?",
                (attempt_no, http_status, error, now, int(delivery_id)),
            )
            self._conn.commit()
        return attempt_no

    def finish_change_delivery(self, delivery_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE recheck_webhook_deliveries SET status = ?, "
                "updated_epoch = ? WHERE id = ?",
                (status, time.time(), int(delivery_id)),
            )
            self._conn.commit()

    def list_change_deliveries(
        self, watch_key: str | None = None, limit: int = 50
    ) -> list[dict]:
        clause = "WHERE watch_key = ?" if watch_key else ""
        params: tuple = (watch_key,) if watch_key else ()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM recheck_webhook_deliveries "
                f"{clause} ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            d
            for d in (self.get_change_delivery(r[0]) for r in rows)
            if d is not None
        ]

    # --- self-serve change webhook subscriptions ----------------------
    # The self-serve counterpart of the per-watch webhooks above
    # (services/hooksubs.py is the validation layer): anyone may
    # register a callback for Recheck CHANGED events — feed-wide or
    # filtered to one resource URL / one origin — with NO fleet
    # operator gate. The caller's secret is the management
    # credential (list/delete present it) AND the HMAC signing key
    # for deliveries, which is why it is stored — exactly like the
    # per-watch table. Deliveries ride the same queue: a
    # subscription's deliveries carry watch_key "sub:<sub_id>" and
    # event_id "<event>@<sub_id>" (the deliveries table's event_id
    # is UNIQUE, and one event can fan out to many subscribers).

    def add_webhook_sub(
        self,
        sub_id: str,
        callback_url: str,
        secret: str,
        filter_url: str | None,
        filter_origin: str | None,
    ) -> dict:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO recheck_webhook_subs (sub_id, "
                "callback_url, secret, filter_url, filter_origin, "
                "enabled, created_epoch, updated_epoch) VALUES "
                "(?, ?, ?, ?, ?, 1, ?, ?)",
                (sub_id, callback_url, secret, filter_url, filter_origin, now, now),
            )
            self._conn.commit()
        return self.get_webhook_sub(sub_id) or {}

    def get_webhook_sub(self, sub_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT sub_id, callback_url, secret, filter_url, "
                "filter_origin, enabled, created_epoch FROM "
                "recheck_webhook_subs WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "sub_id": row[0],
            "callback_url": row[1],
            "secret": row[2],
            "filter_url": row[3],
            "filter_origin": row[4],
            "enabled": bool(row[5]),
            "created_epoch": row[6],
        }

    def list_webhook_subs(self, secret: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sub_id FROM recheck_webhook_subs WHERE "
                "secret = ? ORDER BY created_epoch",
                (secret,),
            ).fetchall()
        return [
            s
            for s in (self.get_webhook_sub(r[0]) for r in rows)
            if s is not None
        ]

    def delete_webhook_sub(self, sub_id: str, secret: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM recheck_webhook_subs WHERE sub_id = ? "
                "AND secret = ?",
                (sub_id, secret),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def count_webhook_subs(
        self, secret: str | None = None, callback_host: str | None = None
    ) -> int:
        with self._lock:
            if secret is not None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM recheck_webhook_subs "
                    "WHERE secret = ?",
                    (secret,),
                ).fetchone()
                return int(row[0])
            if callback_host is not None:
                rows = self._conn.execute(
                    "SELECT callback_url FROM recheck_webhook_subs"
                ).fetchall()
                return sum(
                    1
                    for (u,) in rows
                    if urlsplit(u).netloc == callback_host
                )
            row = self._conn.execute(
                "SELECT COUNT(*) FROM recheck_webhook_subs"
            ).fetchone()
            return int(row[0])

    def enabled_webhook_subs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sub_id FROM recheck_webhook_subs WHERE "
                "enabled = 1 ORDER BY created_epoch"
            ).fetchall()
        return [
            s
            for s in (self.get_webhook_sub(r[0]) for r in rows)
            if s is not None
        ]

    def get_sub_webhook(self, watch_key: str) -> dict | None:
        """Delivery-time config for a subscription delivery row,
        in the same shape get_change_webhook returns."""
        if not watch_key.startswith("sub:"):
            return None
        sub = self.get_webhook_sub(watch_key[4:])
        if sub is None:
            return None
        return {
            "webhook_url": sub["callback_url"],
            "secret": sub["secret"],
            "enabled": sub["enabled"],
        }

    # --- corrections (append-only amendments, improvement 10) --------
    # A correction NEVER mutates the record it amends: the stored
    # response JSON keeps its exact bytes (the feed hash chain stays
    # verifiable), and the amendment lives in its own table, linked
    # by record_id + the record_hash it corrects. The mutation is
    # internal-secret gated at the HTTP layer; reads are public.

    def corrections_for(self, record_id: str) -> list[dict]:
        with self._lock:
            return self._corrections_for_locked(record_id)

    def add_correction(
        self,
        record_ref: str | int,
        reason: str,
        corrected_fields: dict,
        source: str | None = None,
    ) -> dict | None:
        """Append a correction for the record named by record_id
        (str) or seq (int). Returns the correction, or None when no
        such record exists. The original record is not touched."""
        if isinstance(record_ref, int) or (
            isinstance(record_ref, str) and record_ref.isdigit()
        ):
            env = self.record_by_seq(int(record_ref))
        else:
            env = self.record_by_id(str(record_ref))
        if env is None:
            return None
        created_at = _now_iso()
        digest = hashlib.sha256(
            (
                f"{env['record_hash']}\n"
                f"{_canonical_record_json(corrected_fields)}\n"
                f"{reason}\n{created_at}"
            ).encode("utf-8")
        ).hexdigest()
        correction = {
            "correction_id": f"cor_{digest[:24]}",
            "record_id": env["record_id"],
            "seq": env["seq"],
            "record_hash": env["record_hash"],
            "reason": reason,
            "corrected_fields": corrected_fields,
            "source": source,
            "created_at": created_at,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO recheck_corrections (correction_id, "
                "record_id, seq, record_hash, reason, "
                "corrected_fields_json, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    correction["correction_id"],
                    correction["record_id"],
                    correction["seq"],
                    correction["record_hash"],
                    reason,
                    json.dumps(corrected_fields, sort_keys=True),
                    source,
                    created_at,
                ),
            )
            self._conn.commit()
        return correction

    def list_corrections(
        self, record_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        clause = "WHERE record_id = ?" if record_id else ""
        params: tuple = (record_id,) if record_id else ()
        with self._lock:
            rows = self._conn.execute(
                "SELECT correction_id, record_id, seq, record_hash, "
                "reason, corrected_fields_json, source, created_at "
                f"FROM recheck_corrections {clause} "
                "ORDER BY id ASC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            {
                "correction_id": r[0],
                "record_id": r[1],
                "seq": r[2],
                "record_hash": r[3],
                "reason": r[4],
                "corrected_fields": json.loads(r[5]),
                "source": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    # --- sandbox fixture state (improvement 7) ------------------------

    def get_sandbox_state(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM sandbox_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set_sandbox_state(self, state: dict) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sandbox_state (id, state_json) "
                "VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET "
                "state_json = excluded.state_json",
                (json.dumps(state, sort_keys=True),),
            )
            self._conn.commit()
        return state

    # --- watch packs ---------------------------------------------------
    # Every pack mutation runs under the same lock as the watch state
    # and commits before returning, so a spend is atomic in-process:
    # two concurrent spends can never both take the last credit.

    _PACK_COLS = (
        "pack_id, url, credits_total, credits_remaining, "
        "created_epoch, expires_epoch, payment_key"
    )

    def _pack_row(self, where: str, params: tuple) -> dict | None:
        row = self._conn.execute(
            f"SELECT {self._PACK_COLS} FROM recheck_packs WHERE {where}", params
        ).fetchone()
        if row is None:
            return None
        return {
            "pack_id": row[0],
            "url": row[1],
            "credits_total": row[2],
            "credits_remaining": row[3],
            "created_epoch": row[4],
            "expires_epoch": row[5],
            "payment_key": row[6],
        }

    def mint_pack(
        self,
        payment_key: str,
        url: str,
        credits_total: int = PACK_CREDITS,
        ttl_days: int = PACK_TTL_DAYS,
        now_epoch: float | None = None,
    ) -> tuple[dict, bool]:
        """Create a pack for one settled payment. Idempotent on
        payment_key: a re-presented payment returns the pack it
        already minted (created=False) instead of minting a second.
        Returns (pack_view, created)."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            for _attempt in range(3):
                pack_id = new_pack_id()
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO recheck_packs "
                    "(pack_id, url, credits_total, credits_remaining, "
                    "created_epoch, expires_epoch, payment_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        pack_id,
                        url,
                        credits_total,
                        credits_total,
                        now,
                        now + ttl_days * 86_400,
                        payment_key,
                    ),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    row = self._pack_row("pack_id = ?", (pack_id,))
                    assert row is not None
                    return _pack_view(row, now), True
                existing = self._pack_row("payment_key = ?", (payment_key,))
                if existing is not None:
                    return _pack_view(existing, now), False
                # Neither inserted nor found by payment key: the ignore
                # was a pack_id collision (astronomically unlikely) —
                # loop and mint a fresh id.
            raise RuntimeError("could not mint a unique pack id")

    def get_pack(self, pack_id: str) -> dict | None:
        with self._lock:
            row = self._pack_row("pack_id = ?", (pack_id,))
        return _pack_view(row) if row is not None else None

    def pack_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM recheck_packs"
            ).fetchone()
        return int(row[0]) if row else 0

    def reserve_credit(
        self, pack_id: str, url: str, now_epoch: float | None = None
    ) -> dict:
        """Atomically take one credit for a check about to run.
        Validation and decrement happen in one locked step, so the
        refusal reasons are exact and concurrent spends cannot
        overdraw. Raises PackError (unknown / wrong endpoint /
        expired / exhausted) without decrementing anything."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            row = self._pack_row("pack_id = ?", (pack_id,))
            if row is None:
                raise PackError(
                    "unknown_pack", "unknown pack_id", 404
                )
            if row["url"] != url:
                raise PackError(
                    "wrong_endpoint",
                    "this pack is bound to a different endpoint URL",
                    403,
                )
            if now >= row["expires_epoch"]:
                raise PackError(
                    "pack_expired",
                    "this pack has expired; buy a new pack or pay per call",
                    410,
                )
            if row["credits_remaining"] <= 0:
                raise PackError(
                    "pack_exhausted",
                    "this pack has no credits left; buy a new pack or pay per call",
                    402,
                )
            cur = self._conn.execute(
                "UPDATE recheck_packs SET credits_remaining = "
                "credits_remaining - 1 WHERE pack_id = ? AND "
                "credits_remaining > 0",
                (pack_id,),
            )
            self._conn.commit()
            if cur.rowcount != 1:  # pragma: no cover — lock makes this unreachable
                raise PackError(
                    "pack_exhausted",
                    "this pack has no credits left; buy a new pack or pay per call",
                    402,
                )
            row = self._pack_row("pack_id = ?", (pack_id,))
            assert row is not None
            return _pack_view(row, now)

    def refund_credit(self, pack_id: str) -> None:
        """Return a reserved credit after OUR side failed to deliver
        the check (fleet rule: never charge for our failure). Capped
        at credits_total so a stray refund cannot inflate a pack."""
        with self._lock:
            self._conn.execute(
                "UPDATE recheck_packs SET credits_remaining = "
                "MIN(credits_remaining + 1, credits_total) WHERE pack_id = ?",
                (pack_id,),
            )
            self._conn.commit()

    # --- fleet check packs ----------------------------------------------
    # Same discipline as watch packs — CSPRNG id as the only
    # credential, idempotent mint on the payment identity, atomic
    # reserve under the lock, refund when our side fails — but the
    # credit buys a SWEEP of a fixed fleet definition, not a check
    # of one endpoint, so there is no URL binding to validate: the
    # definition inside the pack IS the binding. Runs are stored so
    # /status can serve the latest table and the next run can diff
    # against it. Webhook deliveries reuse watch_deliveries, keyed
    # by pack_id.

    _FC_PACK_COLS = (
        "pack_id, definition_json, webhook_url, email, heartbeat, "
        "interval_s, credits_total, credits_remaining, created_epoch, "
        "expires_epoch, last_run_epoch, payment_key"
    )

    def _fc_pack_row(self, where: str, params: tuple) -> dict | None:
        row = self._conn.execute(
            f"SELECT {self._FC_PACK_COLS} FROM fleet_check_packs "
            f"WHERE {where}",
            params,
        ).fetchone()
        if row is None:
            return None
        return {
            "pack_id": row[0],
            "definition_json": row[1],
            "webhook_url": row[2],
            "email": row[3],
            "heartbeat": row[4],
            "interval_s": row[5],
            "credits_total": row[6],
            "credits_remaining": row[7],
            "created_epoch": row[8],
            "expires_epoch": row[9],
            "last_run_epoch": row[10],
            "payment_key": row[11],
        }

    @staticmethod
    def _fc_pack_view(row: dict, now_epoch: float | None = None) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        return {
            "pack_id": row["pack_id"],
            "definition": json.loads(row["definition_json"]),
            "webhook_url": row["webhook_url"],
            "email": row["email"],
            "heartbeat": bool(row["heartbeat"]),
            "interval_s": row["interval_s"],
            "credits_total": row["credits_total"],
            "credits_remaining": row["credits_remaining"],
            "created_at": _iso_of(row["created_epoch"]),
            "expires_at": _iso_of(row["expires_epoch"]),
            "expired": now >= row["expires_epoch"],
            "last_run_at": (
                _iso_of(row["last_run_epoch"]) if row["last_run_epoch"] else None
            ),
        }

    def mint_fleet_check_pack(
        self,
        payment_key: str,
        definition: dict,
        webhook_url: str | None,
        email: str | None,
        heartbeat: bool,
        interval_s: int,
        credits_total: int = FLEET_CHECK_PACK_CREDITS,
        ttl_days: int = FLEET_CHECK_PACK_TTL_DAYS,
        now_epoch: float | None = None,
    ) -> tuple[dict, bool]:
        """Create a Fleet Check pack for one settled payment.
        Idempotent on payment_key, exactly like mint_pack: a
        re-presented payment returns the pack it already minted
        (created=False). Returns (pack_view, created)."""
        now = time.time() if now_epoch is None else now_epoch
        definition_json = json.dumps(definition, sort_keys=True)
        with self._lock:
            for _attempt in range(3):
                pack_id = _FC_PACK_ID_PREFIX + secrets.token_hex(16)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fleet_check_packs "
                    "(pack_id, definition_json, webhook_url, email, "
                    "heartbeat, interval_s, credits_total, "
                    "credits_remaining, created_epoch, expires_epoch, "
                    "payment_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        pack_id,
                        definition_json,
                        webhook_url,
                        email,
                        1 if heartbeat else 0,
                        interval_s,
                        credits_total,
                        credits_total,
                        now,
                        now + ttl_days * 86_400,
                        payment_key,
                    ),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    row = self._fc_pack_row("pack_id = ?", (pack_id,))
                    assert row is not None
                    return self._fc_pack_view(row, now), True
                existing = self._fc_pack_row("payment_key = ?", (payment_key,))
                if existing is not None:
                    return self._fc_pack_view(existing, now), False
            raise RuntimeError("could not mint a unique fleet check pack id")

    # --- free first Fleet Check (one per wallet) ---------------------------

    def claim_free_fleet_check(
        self, wallet: str, now_epoch: float | None = None
    ) -> bool:
        """Atomically record a wallet's one free Fleet Check.
        Returns True when this call claimed it (first time), False
        when the wallet had already used its free sweep. The claim
        is persistent — a restart does not hand out a second one."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fleet_check_free_claims "
                "(wallet, claimed_epoch) VALUES (?, ?)",
                (wallet.lower(), now),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def release_free_fleet_check(self, wallet: str) -> None:
        """Undo a claim whose sweep never delivered (our machinery
        failed after the claim) — the free call is only spent when
        a sweep actually runs, mirroring reserve-then-refund."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM fleet_check_free_claims WHERE wallet = ?",
                (wallet.lower(),),
            )
            self._conn.commit()

    def free_fleet_check_used(self, wallet: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM fleet_check_free_claims WHERE wallet = ?",
                (wallet.lower(),),
            ).fetchone()
        return row is not None

    def get_fleet_check_pack(self, pack_id: str) -> dict | None:
        with self._lock:
            row = self._fc_pack_row("pack_id = ?", (pack_id,))
        return self._fc_pack_view(row) if row is not None else None

    def fleet_check_pack_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM fleet_check_packs"
            ).fetchone()
        return int(row[0]) if row else 0

    def reserve_fleet_check_credit(
        self, pack_id: str, now_epoch: float | None = None
    ) -> dict:
        """Atomically take one sweep credit for a sweep about to
        run. Raises PackError (unknown / expired / exhausted)
        without decrementing anything."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            row = self._fc_pack_row("pack_id = ?", (pack_id,))
            if row is None:
                raise PackError("unknown_pack", "unknown pack_id", 404)
            if now >= row["expires_epoch"]:
                raise PackError(
                    "pack_expired",
                    "this pack has expired; buy a new pack or pay per sweep",
                    410,
                )
            if row["credits_remaining"] <= 0:
                raise PackError(
                    "pack_exhausted",
                    "this pack has no sweep credits left; buy a new pack "
                    "or pay per sweep",
                    402,
                )
            cur = self._conn.execute(
                "UPDATE fleet_check_packs SET credits_remaining = "
                "credits_remaining - 1 WHERE pack_id = ? AND "
                "credits_remaining > 0",
                (pack_id,),
            )
            self._conn.commit()
            if cur.rowcount != 1:  # pragma: no cover — lock makes this unreachable
                raise PackError(
                    "pack_exhausted",
                    "this pack has no sweep credits left; buy a new pack "
                    "or pay per sweep",
                    402,
                )
            row = self._fc_pack_row("pack_id = ?", (pack_id,))
            assert row is not None
            return self._fc_pack_view(row, now)

    def refund_fleet_check_credit(self, pack_id: str) -> None:
        """Return a reserved sweep credit after OUR side failed to
        deliver the sweep. Capped at credits_total."""
        with self._lock:
            self._conn.execute(
                "UPDATE fleet_check_packs SET credits_remaining = "
                "MIN(credits_remaining + 1, credits_total) WHERE pack_id = ?",
                (pack_id,),
            )
            self._conn.commit()

    def save_fleet_check_run(
        self,
        pack_id: str,
        result: dict,
        diff: dict,
        now_epoch: float | None = None,
    ) -> None:
        """Store one completed sweep (the table + its diff) and mark
        the pack's last-run time — the ticker's due math and the
        next run's diff baseline both read from here."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            self._conn.execute(
                "INSERT INTO fleet_check_runs "
                "(pack_id, ran_epoch, result_json, diff_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    pack_id,
                    now,
                    json.dumps(result, sort_keys=True),
                    json.dumps(diff, sort_keys=True),
                ),
            )
            self._conn.execute(
                "UPDATE fleet_check_packs SET last_run_epoch = ? "
                "WHERE pack_id = ?",
                (now, pack_id),
            )
            self._conn.commit()

    def latest_fleet_check_run(self, pack_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ran_epoch, result_json, diff_json FROM "
                "fleet_check_runs WHERE pack_id = ? ORDER BY id DESC "
                "LIMIT 1",
                (pack_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row[1])
        return {
            "ran_at": _iso_of(row[0]),
            "checked_at": result.get("checked_at"),
            "summary": result.get("summary"),
            "resources": result.get("resources"),
            "origins": result.get("origins"),
            "diff": json.loads(row[2]),
        }

    def due_fleet_check_packs(
        self, now_epoch: float | None = None
    ) -> list[dict]:
        """Packs whose next automatic sweep is due: funded (credits
        left), unexpired, and never run or past their interval. An
        unfundable pack is simply never due — the ticker must not
        deliver free silent runs."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            rows = self._conn.execute(
                "SELECT pack_id FROM fleet_check_packs WHERE "
                "credits_remaining > 0 AND expires_epoch > ? AND "
                "(last_run_epoch IS NULL OR "
                "last_run_epoch + interval_s <= ?)",
                (now, now),
            ).fetchall()
        return [
            pack
            for r in rows
            if (pack := self.get_fleet_check_pack(r[0])) is not None
        ]

    # --- human payment rail: consumed tx hashes -------------------------
    # A direct on-chain USDC payment (the human rail, for buyers who
    # cannot pay via x402) may mint exactly ONE Fleet Check pack.
    # The tx hash is the replay identity: once recorded here it can
    # never mint again, even across restarts/redeploys (same sqlite
    # file / durability story as the packs themselves).

    def consumed_tx(self, tx_hash: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tx_hash, pack_id, buyer_wallet, consumed_epoch "
                "FROM consumed_payment_txs WHERE tx_hash = ?",
                (tx_hash.lower(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "tx_hash": row[0],
            "pack_id": row[1],
            "buyer_wallet": row[2],
            "consumed_at": _iso_of(row[3]),
        }

    def is_tx_consumed(self, tx_hash: str) -> bool:
        return self.consumed_tx(tx_hash) is not None

    def record_consumed_tx(
        self,
        tx_hash: str,
        pack_id: str,
        buyer_wallet: str,
        now_epoch: float | None = None,
    ) -> bool:
        """Record a tx hash as consumed for a mint. Returns False
        when the hash was already consumed (the INSERT was ignored)
        — the caller must treat that as a replay, not a new mint."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO consumed_payment_txs "
                "(tx_hash, pack_id, buyer_wallet, consumed_epoch) "
                "VALUES (?, ?, ?, ?)",
                (tx_hash.lower(), pack_id, buyer_wallet, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def consumed_tx_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM consumed_payment_txs"
            ).fetchone()
        return int(row[0]) if row else 0

    # --- nudges ----------------------------------------------------------
    # The fleet-health nudge inbox (app/services/nudge.py): signed
    # inbound mail about broken routes, stored verbatim, deduped on
    # (sender, route, defect, first_seen). Opt-outs live beside the
    # mail: a stall whose payTo signed an opt-out is never nudged
    # again, and enforcement reads this table (the sender engine
    # syncs it into its own registry before every send).

    def store_nudge(self, message: dict, dedupe_key: str) -> bool:
        """Store one verified nudge. Returns False when the dedupe
        key already exists — a re-sent identical nudge is a
        duplicate delivery, not a second entry."""
        now = time.time()
        defect = message.get("defect") or {}
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO nudges (dedupe_key, sender, "
                "stall, route, defect_code, first_seen, "
                "received_epoch, message_json) VALUES (?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    dedupe_key,
                    str(message["sender"]),
                    str(message["stall"]),
                    str(message.get("route", "")),
                    str(defect.get("code", "")),
                    str(message.get("first_seen", "")),
                    now,
                    json.dumps(message, sort_keys=True),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def nudge_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM nudges").fetchone()
        return int(row[0])

    def nudges_since(self, sender: str, since_epoch: float) -> int:
        """Verified nudges received from one sender after an epoch —
        the rate limiter's count (duplicates included: a re-send is
        still a knock)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM nudges WHERE lower(sender) = "
                "lower(?) AND received_epoch > ?",
                (sender, since_epoch),
            ).fetchone()
        return int(row[0])

    def list_nudges(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT received_epoch, message_json FROM nudges "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [
            {
                "received_at": _iso_of(row[0]),
                "message": json.loads(row[1]),
            }
            for row in rows
        ]

    def record_optout(self, stall: str, wallet: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO nudge_optouts (stall, wallet, "
                "received_epoch) VALUES (?, ?, ?)",
                (stall.rstrip("/"), wallet, time.time()),
            )
            self._conn.commit()

    def list_optouts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stall, wallet, received_epoch FROM "
                "nudge_optouts ORDER BY stall"
            ).fetchall()
        return [
            {"stall": row[0], "wallet": row[1], "recorded_at": _iso_of(row[2])}
            for row in rows
        ]

    def is_opted_out(self, stall: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM nudge_optouts WHERE stall = ?",
                (stall.rstrip("/"),),
            ).fetchone()
        return row is not None

    # --- stall claims ----------------------------------------------------
    # The claim flow (app/services/claims.py): an operator proves
    # control of a stall origin — by signing with the origin's
    # published payTo wallet, or by serving a token file under the
    # origin's /.well-known/ — and only then may read that origin's
    # patrol defect history. Challenges are single-use and expire;
    # a successful verify consumes the challenge, records the origin
    # as claimed, and mints a session token bound to that ONE origin.
    # The session token is the only credential the history read
    # accepts, and it never unlocks another origin's history.

    _CLAIM_CHALLENGE_COLS = (
        "token, origin, message, created_epoch, expires_epoch, used"
    )

    def create_claim_challenge(
        self,
        origin: str,
        token: str,
        message: str,
        created_epoch: float,
        expires_epoch: float,
    ) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO claim_challenges "
                "(token, origin, message, created_epoch, expires_epoch, used) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (token, origin, message, created_epoch, expires_epoch),
            )
            self._conn.commit()
        row = self._claim_challenge_row("token = ?", (token,))
        assert row is not None
        return row

    def _claim_challenge_row(self, where: str, params: tuple) -> dict | None:
        row = self._conn.execute(
            f"SELECT {self._CLAIM_CHALLENGE_COLS} FROM claim_challenges "
            f"WHERE {where}",
            params,
        ).fetchone()
        if row is None:
            return None
        return {
            "token": row[0],
            "origin": row[1],
            "message": row[2],
            "created_epoch": row[3],
            "expires_epoch": row[4],
            "used": bool(row[5]),
            "created_at": _iso_of(row[3]),
            "expires_at": _iso_of(row[4]),
        }

    def latest_claim_challenge(self, origin: str) -> dict | None:
        """The newest challenge issued for an origin — the one a
        verify call is judged against. Starting a new challenge
        supersedes older unused ones for verification purposes."""
        with self._lock:
            return self._claim_challenge_row(
                "origin = ? ORDER BY created_epoch DESC, rowid DESC LIMIT 1",
                (origin,),
            )

    def consume_claim_challenge(self, token: str) -> bool:
        """Atomically mark a challenge used. Returns False when it
        was already consumed — a replayed verify can never succeed
        twice, even concurrently."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE claim_challenges SET used = 1 "
                "WHERE token = ? AND used = 0",
                (token,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def create_claim_session(
        self,
        origin: str,
        session_token: str,
        created_epoch: float,
        expires_epoch: float,
    ) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO claim_sessions "
                "(session_token, origin, created_epoch, expires_epoch) "
                "VALUES (?, ?, ?, ?)",
                (session_token, origin, created_epoch, expires_epoch),
            )
            self._conn.commit()
        return {
            "session_token": session_token,
            "origin": origin,
            "created_at": _iso_of(created_epoch),
            "expires_at": _iso_of(expires_epoch),
            "expires_epoch": expires_epoch,
        }

    def get_claim_session(self, session_token: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_token, origin, created_epoch, expires_epoch "
                "FROM claim_sessions WHERE session_token = ?",
                (session_token,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_token": row[0],
            "origin": row[1],
            "created_at": _iso_of(row[2]),
            "expires_at": _iso_of(row[3]),
            "expires_epoch": row[3],
        }

    def mark_claimed(
        self,
        origin: str,
        method: str,
        wallet: str | None,
        now_epoch: float | None = None,
    ) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO claimed_origins "
                "(origin, method, wallet, claimed_epoch) VALUES (?, ?, ?, ?)",
                (origin, method, wallet, now),
            )
            self._conn.commit()
        return {
            "origin": origin,
            "method": method,
            "wallet": wallet,
            "claimed_at": _iso_of(now),
        }

    def get_claimed(self, origin: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT origin, method, wallet, claimed_epoch "
                "FROM claimed_origins WHERE origin = ?",
                (origin,),
            ).fetchone()
        if row is None:
            return None
        return {
            "origin": row[0],
            "method": row[1],
            "wallet": row[2],
            "claimed_at": _iso_of(row[3]),
        }

    # --- hosted inboxes ------------------------------------------------
    # The hosted-mailbox relay (app/services/hosted_inbox.py): an
    # origin its operator has CLAIMED gets a mailbox on this service —
    # knocks addressed to it are stored per-origin here, apart from
    # the fleet's own inbox mail (the `nudges` table), because this
    # mail belongs to another operator and is read back only through
    # that origin's claim session. Deliveries (webhook / email relay)
    # are logged per knock so a relay failure is evidence, not a
    # silent drop.

    _HOSTED_COLS = (
        "origin, origin_key, created_epoch, webhook_url, "
        "webhook_enabled, email, email_enabled"
    )

    def _hosted_row(self, where: str, params: tuple) -> dict | None:
        row = self._conn.execute(
            f"SELECT {self._HOSTED_COLS} FROM hosted_inboxes WHERE {where}",
            params,
        ).fetchone()
        if row is None:
            return None
        return {
            "origin": row[0],
            "origin_key": row[1],
            "created_at": _iso_of(row[2]),
            "webhook_url": row[3],
            "webhook_enabled": bool(row[4]),
            "email": row[5],
            "email_enabled": bool(row[6]),
        }

    def mint_hosted_inbox(self, origin: str, origin_key: str) -> dict:
        """Idempotent: the first mint wins; re-minting a claimed
        origin returns the existing mailbox unchanged (the published
        address must never move under an operator)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO hosted_inboxes (origin, "
                "origin_key, created_epoch) VALUES (?, ?, ?)",
                (origin, origin_key, time.time()),
            )
            self._conn.commit()
        return self._hosted_row("origin = ?", (origin,))

    def get_hosted_inbox(self, origin: str) -> dict | None:
        with self._lock:
            return self._hosted_row("origin = ?", (origin,))

    def get_hosted_by_key(self, origin_key: str) -> dict | None:
        with self._lock:
            return self._hosted_row("origin_key = ?", (origin_key,))

    def set_hosted_webhook(
        self, origin: str, url: str | None, enabled: bool
    ) -> dict | None:
        with self._lock:
            self._conn.execute(
                "UPDATE hosted_inboxes SET webhook_url = ?, "
                "webhook_enabled = ? WHERE origin = ?",
                (url, 1 if enabled else 0, origin),
            )
            self._conn.commit()
            return self._hosted_row("origin = ?", (origin,))

    def set_hosted_email(
        self, origin: str, email: str | None, enabled: bool
    ) -> dict | None:
        with self._lock:
            self._conn.execute(
                "UPDATE hosted_inboxes SET email = ?, "
                "email_enabled = ? WHERE origin = ?",
                (email, 1 if enabled else 0, origin),
            )
            self._conn.commit()
            return self._hosted_row("origin = ?", (origin,))

    def store_hosted_knock(
        self, origin: str, message: dict, dedupe_key: str
    ) -> bool:
        """Store one verified knock in a hosted mailbox. Dedupe is
        scoped per origin: the same knock re-sent is a duplicate
        delivery, not a second entry (mirrors store_nudge)."""
        defect = message.get("defect") or {}
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO hosted_knocks (origin, "
                "dedupe_key, sender, route, defect_code, first_seen, "
                "received_epoch, message_json) VALUES (?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    origin,
                    dedupe_key,
                    str(message["sender"]),
                    str(message.get("route", "")),
                    str(defect.get("code", "")),
                    str(message.get("first_seen", "")),
                    time.time(),
                    json.dumps(message, sort_keys=True),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_hosted_knocks(
        self, origin: str, limit: int = 50
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT received_epoch, message_json FROM "
                "hosted_knocks WHERE origin = ? ORDER BY id DESC "
                "LIMIT ?",
                (origin, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "received_at": _iso_of(row[0]),
                "message": json.loads(row[1]),
            }
            for row in rows
        ]

    def hosted_knock_count(self, origin: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM hosted_knocks WHERE origin = ?",
                (origin,),
            ).fetchone()
        return int(row[0])

    def queue_hosted_delivery(
        self,
        origin: str,
        dedupe_key: str,
        channel: str,
        target: str,
    ) -> int:
        """Queue one relay delivery (status 'pending'); the relay
        worker (hosted_inbox.process_deliveries) attempts it and
        updates the row in place, so the row is the delivery's whole
        audit trail."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO hosted_deliveries (origin, dedupe_key, "
                "channel, target, status, attempts, updated_epoch, "
                "detail) VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)",
                (origin, dedupe_key, channel, target, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_hosted_delivery(
        self,
        delivery_id: int,
        status: str,
        attempts: int,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE hosted_deliveries SET status = ?, "
                "attempts = ?, updated_epoch = ?, detail = ? "
                "WHERE id = ?",
                (status, attempts, time.time(), detail, delivery_id),
            )
            self._conn.commit()

    def pending_hosted_deliveries(self, origin: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, dedupe_key, channel, target FROM "
                "hosted_deliveries WHERE origin = ? AND status = "
                "'pending' ORDER BY id",
                (origin,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "dedupe_key": row[1],
                "channel": row[2],
                "target": row[3],
            }
            for row in rows
        ]

    def list_hosted_deliveries(
        self, origin: str, limit: int = 50
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dedupe_key, channel, target, status, "
                "attempts, updated_epoch, detail FROM "
                "hosted_deliveries WHERE origin = ? ORDER BY id DESC "
                "LIMIT ?",
                (origin, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "dedupe_key": row[0],
                "channel": row[1],
                "target": row[2],
                "status": row[3],
                "attempts": row[4],
                "updated_at": _iso_of(row[5]),
                "detail": row[6],
            }
            for row in rows
        ]

    # --- claims history snapshots ---------------------------------------
    # The claims-history document the claim status read serves when no
    # CLAIM_HISTORY_PATH file is configured: the fleet operator
    # uploads it through the fleet-internal gate (the patrol census
    # lives off-service), and it is stored verbatim — one row, each
    # upload replaces the last. The loader in claims.py understands
    # the same three document shapes from either source.

    def save_claims_history_snapshot(self, doc: dict) -> dict:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO claims_history_snapshots "
                "(id, uploaded_epoch, doc_json) VALUES (1, ?, ?)",
                (now, json.dumps(doc)),
            )
            self._conn.commit()
        return {"uploaded_at": _iso_of(now)}

    def get_claims_history_snapshot(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT doc_json FROM claims_history_snapshots "
                "WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            doc = json.loads(row[0])
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def claims_history_snapshot_meta(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT uploaded_epoch FROM claims_history_snapshots "
                "WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return {"uploaded_at": _iso_of(row[0])}

    # --- fleet passes --------------------------------------------------
    # A Fleet Pass is a USD-denominated prepaid balance (micro-units)
    # spendable on ANY fleet route: the ledger lives here, sibling
    # services debit it over HTTP (POST /v1/fleet-pass/debit) and this
    # store is the single source of truth. Same discipline as packs:
    # CSPRNG id as the only credential, atomic reserve under the lock,
    # refund by debit_id when our side fails, no silent fallback.

    _PASS_COLS = (
        "pass_id, balance_units, total_units, created_epoch, "
        "expires_epoch, payment_key"
    )

    def _pass_row(self, where: str, params: tuple) -> dict | None:
        row = self._conn.execute(
            f"SELECT {self._PASS_COLS} FROM fleet_passes WHERE {where}", params
        ).fetchone()
        if row is None:
            return None
        return {
            "pass_id": row[0],
            "balance_units": row[1],
            "total_units": row[2],
            "created_epoch": row[3],
            "expires_epoch": row[4],
            "payment_key": row[5],
        }

    @staticmethod
    def _pass_view(row: dict, now_epoch: float | None = None) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        return {
            "pass_id": row["pass_id"],
            "balance_units": row["balance_units"],
            "balance_usd": row["balance_units"] / 1_000_000,
            "total_units": row["total_units"],
            "total_usd": row["total_units"] / 1_000_000,
            "created_at": _iso_of(row["created_epoch"]),
            "expires_at": _iso_of(row["expires_epoch"]),
            "expired": now >= row["expires_epoch"],
        }

    def mint_pass(
        self,
        payment_key: str,
        total_units: int,
        ttl_days: int,
        now_epoch: float | None = None,
    ) -> tuple[dict, bool]:
        """Create a Fleet Pass for one settled payment. Idempotent on
        payment_key, exactly like mint_pack."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            for _attempt in range(3):
                pass_id = "fpass_" + secrets.token_hex(16)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fleet_passes "
                    "(pass_id, balance_units, total_units, created_epoch, "
                    "expires_epoch, payment_key) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pass_id,
                        total_units,
                        total_units,
                        now,
                        now + ttl_days * 86_400,
                        payment_key,
                    ),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    row = self._pass_row("pass_id = ?", (pass_id,))
                    assert row is not None
                    return self._pass_view(row, now), True
                existing = self._pass_row("payment_key = ?", (payment_key,))
                if existing is not None:
                    return self._pass_view(existing, now), False
            raise RuntimeError("could not mint a unique pass id")

    def get_pass(self, pass_id: str) -> dict | None:
        with self._lock:
            row = self._pass_row("pass_id = ?", (pass_id,))
        return self._pass_view(row) if row is not None else None

    def debit_pass(
        self,
        pass_id: str,
        service: str,
        route: str,
        price_units: int,
        now_epoch: float | None = None,
    ) -> dict:
        """Atomically debit `price_units` for one fleet call about to
        run. The CALLER (fleetpass price table) fixes the price — the
        buyer never names it. Raises PackError (unknown 404 / expired
        410 / insufficient 402) without debiting anything. Returns the
        pass view plus the debit_id a refund must cite."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            row = self._pass_row("pass_id = ?", (pass_id,))
            if row is None:
                raise PackError("unknown_pass", "unknown pass_id", 404)
            if now >= row["expires_epoch"]:
                raise PackError(
                    "pass_expired",
                    "this fleet pass has expired; buy a new pass or pay per call",
                    410,
                )
            if row["balance_units"] < price_units:
                raise PackError(
                    "pass_insufficient",
                    "fleet pass balance is below this route's price; "
                    "buy a new pass or pay per call",
                    402,
                )
            debit_id = "fpd_" + secrets.token_hex(12)
            self._conn.execute(
                "UPDATE fleet_passes SET balance_units = balance_units - ? "
                "WHERE pass_id = ? AND balance_units >= ?",
                (price_units, pass_id, price_units),
            )
            self._conn.execute(
                "INSERT INTO fleet_pass_debits "
                "(debit_id, pass_id, service, route, price_units, "
                "created_epoch, refunded) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (debit_id, pass_id, service, route, price_units, now),
            )
            self._conn.commit()
            row = self._pass_row("pass_id = ?", (pass_id,))
            assert row is not None
            view = self._pass_view(row, now)
            view["debit_id"] = debit_id
            view["debited_units"] = price_units
            return view

    def refund_debit(self, debit_id: str) -> bool:
        """Re-credit one debit after our side failed to deliver.
        Idempotent: a second refund of the same debit_id is a no-op.
        Returns True when a refund actually landed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT pass_id, price_units, refunded FROM "
                "fleet_pass_debits WHERE debit_id = ?",
                (debit_id,),
            ).fetchone()
            if row is None or row[2]:
                return False
            self._conn.execute(
                "UPDATE fleet_passes SET balance_units = "
                "MIN(balance_units + ?, total_units) WHERE pass_id = ?",
                (row[1], row[0]),
            )
            self._conn.execute(
                "UPDATE fleet_pass_debits SET refunded = 1 WHERE debit_id = ?",
                (debit_id,),
            )
            self._conn.commit()
            return True

    def pass_debit_count(self, pass_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM fleet_pass_debits WHERE pass_id = ?",
                (pass_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # --- webhook watch registrations ------------------------------------
    # A registration binds one watched URL to a delivery target and a
    # funding credential (a recheck pack bound to that URL, or a Fleet
    # Pass). Each periodic run spends from that credential — a run
    # that cannot be funded marks the registration funding_failed and
    # delivers nothing, never a free silent run.

    def register_watch(
        self,
        url: str,
        webhook_url: str | None,
        email: str | None,
        interval_s: int,
        funding_kind: str,
        funding_id: str,
        now_epoch: float | None = None,
    ) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        watch_reg_id = "watch_" + secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO watch_regs "
                "(watch_reg_id, url, webhook_url, email, interval_s, "
                "funding_kind, funding_id, created_epoch, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    watch_reg_id,
                    url,
                    webhook_url,
                    email,
                    interval_s,
                    funding_kind,
                    funding_id,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_watch_reg(watch_reg_id)  # type: ignore[return-value]

    def get_watch_reg(self, watch_reg_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT watch_reg_id, url, webhook_url, email, interval_s, "
                "funding_kind, funding_id, created_epoch, last_run_epoch, "
                "last_status, active FROM watch_regs WHERE watch_reg_id = ?",
                (watch_reg_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "watch_reg_id": row[0],
            "url": row[1],
            "webhook_url": row[2],
            "email": row[3],
            "interval_s": row[4],
            "funding_kind": row[5],
            "funding_id": row[6],
            "created_at": _iso_of(row[7]),
            "last_run_at": _iso_of(row[8]) if row[8] else None,
            "last_status": row[9],
            "active": bool(row[10]),
        }

    def due_watch_regs(self, now_epoch: float | None = None) -> list[dict]:
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            rows = self._conn.execute(
                "SELECT watch_reg_id FROM watch_regs WHERE active = 1 AND "
                "(last_run_epoch IS NULL OR last_run_epoch + interval_s <= ?)",
                (now,),
            ).fetchall()
        return [
            reg
            for r in rows
            if (reg := self.get_watch_reg(r[0])) is not None
        ]

    def update_watch_run(
        self, watch_reg_id: str, status: str, now_epoch: float | None = None
    ) -> None:
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            self._conn.execute(
                "UPDATE watch_regs SET last_run_epoch = ?, last_status = ? "
                "WHERE watch_reg_id = ?",
                (now, status, watch_reg_id),
            )
            self._conn.commit()

    def record_delivery(
        self,
        watch_reg_id: str,
        event: str,
        http_status: int | None,
        error: str | None,
        now_epoch: float | None = None,
    ) -> None:
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            self._conn.execute(
                "INSERT INTO watch_deliveries "
                "(watch_reg_id, event, delivered_epoch, http_status, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (watch_reg_id, event, now, http_status, error),
            )
            self._conn.commit()

    def deliveries_for(self, watch_reg_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event, delivered_epoch, http_status, error FROM "
                "watch_deliveries WHERE watch_reg_id = ? ORDER BY id",
                (watch_reg_id,),
            ).fetchall()
        return [
            {
                "event": r[0],
                "delivered_at": _iso_of(r[1]),
                "http_status": r[2],
                "error": r[3],
            }
            for r in rows
        ]

    # --- rotation alert registrations ----------------------------------
    # A rotation registration watches the Verdict Index snapshot stream
    # (not a live probe): it names a payTo and/or one resource, and is
    # evaluated once per new catalog snapshot — the diff between the
    # two newest snapshots is checked for events touching the named
    # target. Deliveries reuse the watch_deliveries table above (the
    # reg_id is the key). Evaluation state (the snapshot watermark)
    # lives on the registration row, so a restart resumes exactly
    # where the last evaluation left off.

    _ROTATION_COLS = (
        "reg_id, pay_to, resource, webhook_url, email, funding_id, "
        "created_epoch, last_eval_snapshot_id, last_status, active"
    )

    def _rotation_view(self, row) -> dict:
        return {
            "reg_id": row[0],
            "pay_to": row[1],
            "resource": row[2],
            "webhook_url": row[3],
            "email": row[4],
            "funding_kind": "fleet_pass",
            "funding_id": row[5],
            "created_at": _iso_of(row[6]),
            "last_eval_snapshot_id": row[7],
            "last_status": row[8],
            "active": bool(row[9]),
        }

    def register_rotation(
        self,
        pay_to: str | None,
        resource: str | None,
        webhook_url: str | None,
        email: str | None,
        funding_id: str,
        now_epoch: float | None = None,
    ) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        reg_id = "rot_" + secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO rotation_regs "
                "(reg_id, pay_to, resource, webhook_url, email, "
                "funding_id, created_epoch, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (reg_id, pay_to, resource, webhook_url, email, funding_id, now),
            )
            self._conn.commit()
        return self.get_rotation_reg(reg_id)  # type: ignore[return-value]

    def get_rotation_reg(self, reg_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._ROTATION_COLS} FROM rotation_regs "
                "WHERE reg_id = ?",
                (reg_id,),
            ).fetchone()
        return self._rotation_view(row) if row is not None else None

    def rotation_regs_to_eval(self, newest_snapshot_id: int) -> list[dict]:
        """Active registrations whose watermark is behind the newest
        snapshot (or not yet set)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT reg_id FROM rotation_regs WHERE active = 1 AND "
                "(last_eval_snapshot_id IS NULL OR "
                "last_eval_snapshot_id < ?)",
                (newest_snapshot_id,),
            ).fetchall()
        return [
            reg
            for r in rows
            if (reg := self.get_rotation_reg(r[0])) is not None
        ]

    def update_rotation_eval(
        self,
        reg_id: str,
        snapshot_id: int | None,
        status: str,
    ) -> None:
        """Record an evaluation outcome. snapshot_id=None records the
        status without moving the watermark (a funding failure must
        not silently skip a snapshot's changes)."""
        with self._lock:
            if snapshot_id is None:
                self._conn.execute(
                    "UPDATE rotation_regs SET last_status = ? WHERE reg_id = ?",
                    (status, reg_id),
                )
            else:
                self._conn.execute(
                    "UPDATE rotation_regs SET last_eval_snapshot_id = ?, "
                    "last_status = ? WHERE reg_id = ?",
                    (snapshot_id, status, reg_id),
                )
            self._conn.commit()

    # --- router watchlist registrations -------------------------------------
    # A watchlist registration tracks the top-N catalog resources by
    # 30d calls (top_n on the row) and is evaluated once per new
    # Index snapshot by services/watchlist.py. Deliveries reuse
    # watch_deliveries (keyed by reg_id, event "watchlist_diff").

    _WATCHLIST_COLS = (
        "reg_id, top_n, webhook_url, email, funding_id, "
        "created_epoch, last_eval_snapshot_id, last_status, active"
    )

    def _watchlist_view(self, row) -> dict:
        return {
            "reg_id": row[0],
            "top_n": row[1],
            "webhook_url": row[2],
            "email": row[3],
            "funding_kind": "fleet_pass",
            "funding_id": row[4],
            "created_at": _iso_of(row[5]),
            "last_eval_snapshot_id": row[6],
            "last_status": row[7],
            "active": bool(row[8]),
        }

    def register_watchlist(
        self,
        top_n: int,
        webhook_url: str | None,
        email: str | None,
        funding_id: str,
        now_epoch: float | None = None,
    ) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        reg_id = "wl_" + secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO watchlist_regs "
                "(reg_id, top_n, webhook_url, email, "
                "funding_id, created_epoch, active) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (reg_id, top_n, webhook_url, email, funding_id, now),
            )
            self._conn.commit()
        return self.get_watchlist_reg(reg_id)  # type: ignore[return-value]

    def get_watchlist_reg(self, reg_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._WATCHLIST_COLS} FROM watchlist_regs "
                "WHERE reg_id = ?",
                (reg_id,),
            ).fetchone()
        return self._watchlist_view(row) if row is not None else None

    def watchlist_regs_to_eval(self, newest_snapshot_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT reg_id FROM watchlist_regs WHERE active = 1 AND "
                "(last_eval_snapshot_id IS NULL OR "
                "last_eval_snapshot_id < ?)",
                (newest_snapshot_id,),
            ).fetchall()
        return [
            reg
            for r in rows
            if (reg := self.get_watchlist_reg(r[0])) is not None
        ]

    def update_watchlist_eval(
        self,
        reg_id: str,
        snapshot_id: int | None,
        status: str,
    ) -> None:
        with self._lock:
            if snapshot_id is None:
                self._conn.execute(
                    "UPDATE watchlist_regs SET last_status = ? WHERE reg_id = ?",
                    (status, reg_id),
                )
            else:
                self._conn.execute(
                    "UPDATE watchlist_regs SET last_eval_snapshot_id = ?, "
                    "last_status = ? WHERE reg_id = ?",
                    (snapshot_id, status, reg_id),
                )
            self._conn.commit()

    # --- Index subscriptions -------------------------------------------------
    # An Index subscription is the standing form of POST
    # /v1/index/changes: a registration carries the same filters the
    # paid route accepts, and each evaluation delivers the filtered
    # diff of the two newest snapshots to the subscriber's webhook.
    # Deliveries reuse watch_deliveries (keyed by reg_id, event
    # "index_diff"). Funding is a Fleet Pass debit at the paid
    # route's own price ($0.10) per delivered evaluation.

    _INDEX_SUB_COLS = (
        "reg_id, resource_contains, pay_to, service_name, network, "
        "webhook_url, email, funding_id, created_epoch, "
        "last_eval_snapshot_id, last_status, active"
    )

    def _index_sub_view(self, row) -> dict:
        return {
            "reg_id": row[0],
            "resource_contains": row[1],
            "pay_to": row[2],
            "service_name": row[3],
            "network": row[4],
            "webhook_url": row[5],
            "email": row[6],
            "funding_kind": "fleet_pass",
            "funding_id": row[7],
            "created_at": _iso_of(row[8]),
            "last_eval_snapshot_id": row[9],
            "last_status": row[10],
            "active": bool(row[11]),
        }

    def register_index_sub(
        self,
        resource_contains: str | None,
        pay_to: str | None,
        service_name: str | None,
        network: str | None,
        webhook_url: str | None,
        email: str | None,
        funding_id: str,
        now_epoch: float | None = None,
    ) -> dict:
        now = time.time() if now_epoch is None else now_epoch
        reg_id = "isub_" + secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO index_subs "
                "(reg_id, resource_contains, pay_to, service_name, "
                "network, webhook_url, email, funding_id, "
                "created_epoch, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    reg_id,
                    resource_contains,
                    pay_to,
                    service_name,
                    network,
                    webhook_url,
                    email,
                    funding_id,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_index_sub(reg_id)  # type: ignore[return-value]

    def get_index_sub(self, reg_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._INDEX_SUB_COLS} FROM index_subs "
                "WHERE reg_id = ?",
                (reg_id,),
            ).fetchone()
        return self._index_sub_view(row) if row is not None else None

    def index_subs_to_eval(self, newest_snapshot_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT reg_id FROM index_subs WHERE active = 1 AND "
                "(last_eval_snapshot_id IS NULL OR "
                "last_eval_snapshot_id < ?)",
                (newest_snapshot_id,),
            ).fetchall()
        return [
            reg for r in rows if (reg := self.get_index_sub(r[0])) is not None
        ]

    def update_index_sub_eval(
        self,
        reg_id: str,
        snapshot_id: int | None,
        status: str,
    ) -> None:
        with self._lock:
            if snapshot_id is None:
                self._conn.execute(
                    "UPDATE index_subs SET last_status = ? WHERE reg_id = ?",
                    (status, reg_id),
                )
            else:
                self._conn.execute(
                    "UPDATE index_subs SET last_eval_snapshot_id = ?, "
                    "last_status = ? WHERE reg_id = ?",
                    (snapshot_id, status, reg_id),
                )
            self._conn.commit()


# --- probing -------------------------------------------------------------------
#
# Probe budget (2026-10-28): the check a recheck call — or a watch-pack
# credit — buys runs under a hard budget. Before this, a probe inherited
# the shared fetch client's 15s timeout PER REQUEST across 3 attempts
# (plus backoff, plus up to 6 redirect hops per attempt): measured
# against local targets, a slow-but-retryable target (2s delay + 503)
# took 8.1s to fail and a hanging target 47s. That is the signature
# Settled's catalogue probe recorded against the pack — ~8s latency and
# an intermittent hard failure — while the service itself was healthy.
# The bounded path below caps one probe at _PROBE_BUDGET_S total with a
# per-request timeout of _PROBE_REQUEST_TIMEOUT_S; retries still happen
# inside the budget, and the failure contract is unchanged (a failed
# probe raises UpstreamError — 502 per-call, credit refunded for pack
# spends — it just fails in bounded time). Callers that probe on their
# own contracts (grade-my-stall, agent-card) keep the legacy unbounded
# path by not passing a deadline.

_PROBE_REQUEST_TIMEOUT_S = 1.5
_PROBE_BUDGET_S = 2.8
_PROBE_MIN_ATTEMPT_S = 0.5  # never start (or sleep toward) an attempt with less left


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


async def _ssrf_checked(url: str, deadline: float | None) -> None:
    """The probe's SSRF guard (blocking DNS — always off the loop).
    Under a deadline the wait is bounded too; a ValueError refusal
    propagates exactly as before."""
    if deadline is None:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
        return
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise UpstreamError("target probe timed out before it started")
    try:
        await asyncio.wait_for(
            asyncio.to_thread(fetch_svc.ssrf_check, url), timeout=remaining
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise UpstreamError("target probe timed out during the SSRF check")


async def _get_once(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    deadline: float | None = None,
) -> tuple[int, httpx.Headers, bytes | None, int]:
    """One probe with manual redirect following — every hop is
    SSRF-checked BEFORE it is fetched, exactly like the page fetcher.
    The body is read (size-capped) only on 200/402, the statuses whose
    content the fingerprint needs. With a deadline, each request gets
    min(_PROBE_REQUEST_TIMEOUT_S, remaining) as its timeout. Returns
    (status, headers, body, redirect_count) — the count of redirect
    hops actually followed (probe-quality metadata, improvement 8)."""
    current = url
    redirects = 0
    for hop in range(fetch_svc._MAX_REDIRECTS + 1):
        kwargs: dict = {}
        if deadline is not None:
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise UpstreamError("target probe timed out")
            kwargs["timeout"] = httpx.Timeout(
                min(_PROBE_REQUEST_TIMEOUT_S, remaining)
            )
        async with client.stream(
            method, current, headers=fetch_svc._UA, follow_redirects=False, **kwargs
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise UpstreamError(
                        f"target returned HTTP {resp.status_code} without a "
                        "Location header"
                    )
                if hop == fetch_svc._MAX_REDIRECTS:
                    raise UpstreamError("target redirected too many times")
                target = urljoin(current, location)
                if deadline is None:
                    await asyncio.to_thread(fetch_svc.ssrf_check, target)
                else:
                    await _ssrf_checked(target, deadline)
                current = target
                redirects += 1
                if resp.status_code == 303:
                    method = "GET"
                continue
            body = (
                await fetch_svc._read_capped(resp)
                if resp.status_code in _PROBE_STATUSES
                else None
            )
            return resp.status_code, resp.headers, body, redirects
    raise UpstreamError("target redirected too many times")  # pragma: no cover


async def _attempt(
    client: httpx.AsyncClient, url: str, deadline: float | None = None
):
    """One probe attempt. x402 resources live behind POST; some web
    servers refuse POST outright, so a 405/501 falls back to GET —
    under a deadline only when a real attempt's worth of budget is
    still left. Returns (status, headers, body, redirect_count)
    summed across the POST and any GET fallback leg."""
    status, headers, body, redirects = await _get_once(
        client, url, "POST", deadline
    )
    if status in (405, 501) and (
        deadline is None or _remaining(deadline) >= _PROBE_MIN_ATTEMPT_S
    ):
        status, headers, body, more = await _get_once(
            client, url, "GET", deadline
        )
        redirects += more
    return status, headers, body, redirects


async def _probe(
    client: httpx.AsyncClient, url: str, deadline: float | None = None
):
    """Probe with the fetcher's retry discipline. Returns
    (status, headers, body, meta) for a 200 or 402, where meta is
    the probe-quality record {attempts, redirect_count} — attempts
    actually started (1 = no retry was needed) and redirect hops
    followed across all attempts. Anything else that survives the
    retries raises UpstreamError. With a deadline, the retries
    share the one budget: an attempt only starts while at least
    _PROBE_MIN_ATTEMPT_S remains, and backoff sleeps are capped so
    the next attempt still gets its minimum."""
    await _ssrf_checked(url, deadline)
    last_error = "target probe failed"
    attempts = 0
    redirect_count = 0
    for attempt in range(_ATTEMPTS):
        if attempt:
            backoff = _BACKOFF_S[attempt - 1]
            if deadline is not None:
                room = _remaining(deadline) - _PROBE_MIN_ATTEMPT_S
                if room <= 0:
                    break
                backoff = min(backoff, room)
            await asyncio.sleep(backoff)
            if deadline is not None and _remaining(deadline) < _PROBE_MIN_ATTEMPT_S:
                break
        attempts += 1
        try:
            status, headers, body, redirects = await _attempt(
                client, url, deadline
            )
            redirect_count += redirects
        except httpx.HTTPError as e:
            last_error = f"target fetch failed: {e}"
            continue
        if status in _PROBE_STATUSES:
            return status, headers, body, {
                "attempts": attempts,
                "redirect_count": redirect_count,
            }
        last_error = f"target returned HTTP {status}"
        if status in _RETRY_STATUSES:
            continue
        raise UpstreamError(last_error)
    raise UpstreamError(last_error)


# --- fingerprints ----------------------------------------------------------------

def _parse_challenge(headers: httpx.Headers, body: bytes | None) -> dict | None:
    """The x402 v2 challenge, from the PAYMENT-REQUIRED header
    (base64 JSON) or, failing that, a JSON body in the same shape."""
    raw = headers.get("payment-required")
    if raw:
        try:
            data = json.loads(base64.b64decode(raw))
            if isinstance(data, dict) and data.get("accepts"):
                return data
        except ValueError:
            pass
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("accepts"):
                return data
        except ValueError:
            pass
    return None


def _x402_fingerprint(status: int, challenge: dict) -> dict:
    accept = challenge["accepts"][0]
    description = (challenge.get("resource") or {}).get("description") or ""
    bazaar = (challenge.get("extensions") or {}).get("bazaar") or {}
    schema = ((bazaar.get("info") or {}).get("output") or {}).get("schema")
    return {
        "status_code": status,
        "amount": accept.get("amount"),
        "asset": accept.get("asset"),
        "network": accept.get("network"),
        "payTo": accept.get("payTo"),
        "description_hash": _hash_text(description) if description else None,
        "schema_hash": (
            _hash_text(json.dumps(schema, sort_keys=True))
            if schema is not None
            else None
        ),
    }


def _web_fingerprint(status: int, headers: httpx.Headers, body: bytes | None) -> dict:
    text = fetch_svc._decode_body(body or b"", headers)
    normalized = fetch_svc._normalize_text(text)
    return {
        "status_code": status,
        "body_sha256": _hash_text(normalized),
    }


async def _probe_fingerprint(
    client: httpx.AsyncClient, url: str
) -> tuple[str, dict, dict]:
    # The recheck execution path — per-call checks and pack spends —
    # always probes under the hard budget (see the probing section).
    # Returns (kind, fingerprint, probe) where probe is the
    # probe-quality metadata (improvement 8): latency measured
    # around the whole probe, attempts/redirects from the probe
    # itself, error_class None — a FAILED probe raises and is never
    # recorded, so probe metadata exists only for completed checks.
    started = time.monotonic()
    deadline = started + _PROBE_BUDGET_S
    status, headers, body, meta = await _probe(client, url, deadline=deadline)
    probe = {
        "latency_ms": int(round((time.monotonic() - started) * 1000)),
        "attempts": meta["attempts"],
        "redirect_count": meta["redirect_count"],
        "error_class": None,
    }
    if status == 402:
        challenge = _parse_challenge(headers, body)
        if challenge is not None:
            return "x402", _x402_fingerprint(status, challenge), probe
        # A 402 with no parseable challenge is fingerprinted as a page:
        # status + body hash still detect change honestly.
    return "web", _web_fingerprint(status, headers, body), probe


# --- probe-quality: flapping (improvement 8) --------------------------------
# A target whose fingerprint oscillates (A -> B -> A) is a different
# operational fact from one that changed once: the flapping flag
# lets a consumer tell "the seller's server blinks" from "the
# seller changed their terms". Computed over the watch's recent
# history — the last FLAPPING_WINDOW completed checks INCLUDING the
# current one — from the fingerprints the store already keeps.

FLAPPING_WINDOW = 6


def assess_flapping(fingerprints: list[dict]) -> dict:
    """{flapping, window, alternations} over the given fingerprint
    sequence (oldest first, current last). flapping is True when
    some value recurs after a different value intervened (an A->B->A
    pattern anywhere in the window); alternations counts the
    positions where the value returns to one seen before the
    immediately preceding position."""
    window = fingerprints[-FLAPPING_WINDOW:]
    keys = [json.dumps(fp, sort_keys=True) for fp in window]
    alternations = 0
    for i in range(2, len(keys)):
        if keys[i] != keys[i - 1] and keys[i] in keys[: i - 1]:
            alternations += 1
    return {
        "flapping": alternations > 0,
        "window": len(window),
        "alternations": alternations,
    }


# --- catalogue cross-check (improvement 9) -----------------------------------
# Frozen into each new record at check time: what the newest CDP
# index snapshot held for this resource when the check ran, and how
# its listed terms compare with the terms the live probe just
# observed. Sources are limited to snapshots StallRow actually
# holds locally (the same IndexStore the timeline reads) — a source
# that cannot be read answers "unknown", never a guess. Seeded /
# legacy records predate the crosscheck and simply lack the field.

_CROSSCHECK_FIELDS = ("amount", "payTo", "network")


def catalogue_crosscheck(
    index_store, url: str, kind: str, fingerprint: dict
) -> dict:
    base: dict = {
        "source": "cdp_index_snapshot",
        "snapshot_id": None,
        "captured_at": None,
        "listed": "unknown",
        "fields": {f: "unknown" for f in _CROSSCHECK_FIELDS},
        "verdict": "unknown",
    }
    try:
        snapshots = index_store.snapshots()
    except Exception:  # noqa: BLE001 - unreadable store = unknown
        return base
    if not snapshots:
        return base
    newest = snapshots[-1]
    base["snapshot_id"] = newest["id"]
    base["captured_at"] = newest["captured_at"]
    try:
        entry = index_store.entries(newest["id"]).get(url)
    except Exception:  # noqa: BLE001
        return base
    if entry is None:
        base["listed"] = False
        base["fields"] = {f: "not_listed" for f in _CROSSCHECK_FIELDS}
        base["verdict"] = "not_listed"
        return base
    base["listed"] = True
    if kind == "x402":
        for field in _CROSSCHECK_FIELDS:
            listed_value = entry.get(field)
            observed_value = fingerprint.get(field)
            if listed_value is None or observed_value is None:
                base["fields"][field] = "unknown"
            elif str(listed_value) == str(observed_value):
                base["fields"][field] = "match"
            else:
                base["fields"][field] = "mismatch"
    verdicts = set(base["fields"].values())
    if "mismatch" in verdicts:
        base["verdict"] = "mismatch"
    elif verdicts == {"match"}:
        base["verdict"] = "match"
    else:
        base["verdict"] = "unknown"
    return base


# --- diff + the check itself ------------------------------------------------------

def _diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for field in sorted(set(before) | set(after)):
        b, a = before.get(field), after.get(field)
        if b != a:
            changes.append({"field": field, "before": b, "after": a})
    return changes


# Structured field-level changes (additive to the legacy changes[]):
# {path, op, before, after, severity, category} with the same op /
# category vocabulary as RouteVerdict's diff. Derived from the
# fingerprint components the store actually keeps. For schema_hash
# the entry carries the hash before/after — the fingerprint stores
# the schema's hash, not the schema itself, so field-level detail
# INSIDE the target's schema is not available from Recheck (that is
# RouteVerdict's contract map's job, and the timeline joins them).
_FINGERPRINT_FIELD_CHANGES = {
    # fingerprint field -> (category, severity)
    "amount": ("payment", "medium"),
    "payTo": ("payment", "high"),
    "network": ("payment", "medium"),
    "asset": ("payment", "medium"),
    "schema_hash": ("schema", "medium"),
    "description_hash": ("schema", "low"),
    "body_sha256": ("schema", "medium"),
    "status_code": ("listing", "high"),
    "kind": ("schema", "medium"),
}


def fingerprint_field_changes(before: dict, after: dict) -> list[dict]:
    out: list[dict] = []
    for field in sorted(set(before) | set(after)):
        b, a = before.get(field), after.get(field)
        if b == a:
            continue
        category, severity = _FINGERPRINT_FIELD_CHANGES.get(
            field, ("schema", "low")
        )
        out.append(
            {
                "path": field,
                "op": "value_changed",
                "before": b,
                "after": a,
                "severity": severity,
                "category": category,
            }
        )
    return out


_HEX64 = frozenset("0123456789abcdef")


def validate_imported_snapshot(
    kind: str, fingerprint: dict, schema: dict | None = None
) -> None:
    """Validate a snapshot offered for baseline import by
    RECOMPUTING what can be recomputed from its own fields. Raises
    ValueError with the reason; a forged snapshot (e.g. a schema
    that does not hash to the claimed schema_hash) never reaches
    the store. For an x402 snapshot carrying a schema_hash, the raw
    schema must be supplied and must hash to it — that is the
    fingerprint component an importer can actually prove. A web
    snapshot's body hash cannot be recomputed without the body, so
    web imports are validated structurally only (documented limit).
    """
    if kind not in ("x402", "web"):
        raise ValueError(f"kind must be 'x402' or 'web', got {kind!r}")
    if not isinstance(fingerprint, dict):
        raise ValueError("fingerprint must be an object")
    status = fingerprint.get("status_code")
    if not isinstance(status, int):
        raise ValueError("fingerprint.status_code must be an integer")
    if kind == "x402":
        for field in (
            "amount",
            "asset",
            "network",
            "payTo",
            "description_hash",
            "schema_hash",
        ):
            if field not in fingerprint:
                raise ValueError(f"fingerprint is missing {field!r}")
        for field in ("description_hash", "schema_hash"):
            value = fingerprint.get(field)
            if value is not None and not (
                isinstance(value, str)
                and len(value) == 64
                and all(c in _HEX64 for c in value.lower())
            ):
                raise ValueError(f"fingerprint.{field} must be a sha256 hex digest or null")
        if fingerprint.get("schema_hash") is not None:
            if schema is None:
                raise ValueError(
                    "this snapshot claims a schema_hash, so the raw "
                    "schema must be supplied to recompute it"
                )
            recomputed = _hash_text(json.dumps(schema, sort_keys=True))
            if recomputed != fingerprint["schema_hash"]:
                raise ValueError(
                    "supplied schema does not hash to the snapshot's "
                    "schema_hash — import refused"
                )
    else:
        body_hash = fingerprint.get("body_sha256")
        if not (
            isinstance(body_hash, str)
            and len(body_hash) == 64
            and all(c in _HEX64 for c in body_hash.lower())
        ):
            raise ValueError("fingerprint.body_sha256 must be a sha256 hex digest")


async def recheck(
    client: httpx.AsyncClient,
    store: WatchStore,
    url: str,
    watch_id: str | None = None,
    index_store=None,
) -> dict:
    """Probe one target and diff it against the watch's stored state.
    Raises ValueError for a refused URL (SSRF guard) and UpstreamError
    for a failed probe — in both cases the stored state is untouched,
    because the save only happens after a successful fingerprint.
    When index_store is supplied (the HTTP layer passes the service's
    catalogue index store), the result also carries the catalogue
    cross-check frozen at check time (improvement 9)."""
    url = url.strip()
    wid = (watch_id or "").strip() or default_watch_id(url)
    key = f"{wid}\n{url}"
    kind, fingerprint, probe = await _probe_fingerprint(client, url)
    checked_at = _now_iso()
    # Flapping: this watch's recent fingerprints (history order,
    # oldest first) with the new one appended — the assessment the
    # record carries is computed over that window, current included.
    recent = store.feed_rows(watch_id=wid, url=url, limit=1000, ascending=True)
    recent_fps = [
        env["record"]["current"]
        for env in recent
        if isinstance(env["record"].get("current"), dict)
    ]
    probe["flapping"] = assess_flapping([*recent_fps, fingerprint])
    crosscheck = (
        catalogue_crosscheck(index_store, url, kind, fingerprint)
        if index_store is not None
        else None
    )
    # A pinned baseline governs the diff while a pin exists; the
    # latest state still records every check, and unpinning returns
    # the watch to diffing against its latest state.
    prior = store.pinned_baseline(key) or store.get(key)
    if prior is None:
        store.save(key, kind, fingerprint, checked_at)
        result = {
            "watch_id": wid,
            "url": url,
            "kind": kind,
            "status": "BASELINE",
            "changes": [],
            "field_changes": [],
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": (
                f"Baseline recorded for this {kind} target; the next call "
                "for this watch reports what changed against it."
            ),
        }
        result["probe"] = probe
        if crosscheck is not None:
            result["catalogue_crosscheck"] = crosscheck
        store.record_check(result)
        return result
    changes = _diff(prior["fingerprint"], fingerprint)
    if prior["kind"] != kind:
        changes.insert(
            0, {"field": "kind", "before": prior["kind"], "after": kind}
        )
    field_changes = fingerprint_field_changes(prior["fingerprint"], fingerprint)
    if prior["kind"] != kind:
        field_changes.insert(
            0,
            {
                "path": "kind",
                "op": "value_changed",
                "before": prior["kind"],
                "after": kind,
                "severity": "medium",
                "category": "schema",
            },
        )
    store.save(key, kind, fingerprint, checked_at)
    if changes:
        fields = ", ".join(c["field"] for c in changes)
        result = {
            "watch_id": wid,
            "url": url,
            "kind": kind,
            "status": "CHANGED",
            "changes": changes,
            "field_changes": field_changes,
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": f"Changed since the previous check: {fields}.",
        }
    else:
        result = {
            "watch_id": wid,
            "url": url,
            "kind": kind,
            "status": "UNCHANGED",
            "changes": [],
            "field_changes": field_changes,
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": "No material change since the previous check.",
        }
    result["probe"] = probe
    if crosscheck is not None:
        result["catalogue_crosscheck"] = crosscheck
    envelope = store.record_check(result)
    if result["status"] == "CHANGED":
        _fire_change_webhook(client, store, envelope)
    return result


async def recheck_with_pack(
    client: httpx.AsyncClient,
    store: WatchStore,
    url: str,
    watch_id: str | None,
    pack_id: str,
    index_store=None,
) -> dict:
    """One change-check paid from a pack instead of per-call.

    Reserve-then-run: the credit is taken atomically BEFORE the probe
    (so concurrent spends cannot overdraw and refusals are exact), and
    refunded if the check itself fails — a ValueError refusal or an
    UpstreamError 502 is OUR side not delivering, and the fleet rule
    is never to charge for that. A completed check — including one
    that reports UNCHANGED — has delivered and keeps the credit."""
    url = url.strip()
    reserved = store.reserve_credit(pack_id, url)
    try:
        result = await recheck(client, store, url, watch_id, index_store)
    except Exception:
        store.refund_credit(pack_id)
        raise
    result["pack_id"] = pack_id
    result["pack_credits_remaining"] = reserved["credits_remaining"]
    result["pack_expires_at"] = reserved["expires_at"]
    return result


# --- change webhooks ------------------------------------------------------
# A watch may carry one webhook (config is fleet-internal-secret
# gated at the HTTP layer). When a check for that watch answers
# CHANGED — never BASELINE or UNCHANGED — the recorded history
# envelope is delivered as a signed event: HMAC-SHA256 over the
# exact raw body bytes, keyed with the watch's secret, carried in
# X-Recheck-Signature beside X-Recheck-Event-Id. Delivery reuses
# the fleet's SSRF discipline (public addresses only, re-checked
# at send time), retries on a bounded backoff, records EVERY
# attempt, and lands in dead_letter after the final failure, where
# the authenticated deliveries read can see it.


def _change_webhook_backoffs() -> list[float]:
    raw = os.environ.get("STALLROW_RECHECK_WEBHOOK_BACKOFF_S", "1,4")
    try:
        waits = [max(0.0, float(p)) for p in raw.split(",") if p.strip()]
    except ValueError:
        waits = [1.0, 4.0]
    return waits or [0.0]


def change_event_payload(envelope: dict) -> dict:
    record = envelope["record"]
    return {
        "event_id": f"evt_{envelope['record_id'][4:]}",
        "event": "recheck.changed",
        "seq": envelope["seq"],
        "record_id": envelope["record_id"],
        "record_hash": envelope["record_hash"],
        "watch_id": record["watch_id"],
        "url": record["url"],
        "checked_at": record["checked_at"],
        "field_changes": record.get("field_changes", []),
        "record": record,
    }


def queue_change_event(store: WatchStore, envelope: dict) -> int | None:
    """Queue a CHANGED delivery if this watch has an enabled
    webhook. Returns the delivery id, or None (no config / disabled
    / not a change) — in which case nothing is recorded, because
    nothing was owed."""
    record = envelope["record"]
    if record.get("status") != "CHANGED":
        return None
    key = f"{record['watch_id']}\n{record['url']}"
    cfg = store.get_change_webhook(key)
    if cfg is None or not cfg["enabled"]:
        return None
    payload = change_event_payload(envelope)
    return store.queue_change_delivery(
        payload["event_id"], key, envelope["seq"], record["url"], payload
    )


def subscription_matches(sub: dict, url: str) -> bool:
    """A subscription fires feed-wide, or on its one filter: exact
    resource URL, or origin (scheme://host) of the changed URL."""
    if sub.get("filter_url"):
        return sub["filter_url"] == url
    if sub.get("filter_origin"):
        parts = urlsplit(url)
        return (
            bool(parts.scheme and parts.netloc)
            and f"{parts.scheme}://{parts.netloc}" == sub["filter_origin"]
        )
    return True


def queue_subscription_events(store: WatchStore, envelope: dict) -> list[int]:
    """Queue a CHANGED delivery for every enabled self-serve
    subscription whose filter matches (services/hooksubs.py
    registers them). Delivery ids returned; a subscription removed
    or disabled before send dead-letters readably at delivery time,
    exactly like a removed per-watch config."""
    record = envelope["record"]
    if record.get("status") != "CHANGED":
        return []
    payload = change_event_payload(envelope)
    out: list[int] = []
    for sub in store.enabled_webhook_subs():
        if not subscription_matches(sub, record["url"]):
            continue
        sub_payload = dict(payload)
        sub_payload["event_id"] = f"{payload['event_id']}@{sub['sub_id']}"
        sub_payload["subscription_id"] = sub["sub_id"]
        out.append(
            store.queue_change_delivery(
                sub_payload["event_id"],
                f"sub:{sub['sub_id']}",
                envelope["seq"],
                record["url"],
                sub_payload,
            )
        )
    return out


async def deliver_change_event(
    client: httpx.AsyncClient, store: WatchStore, delivery_id: int
) -> dict:
    """Attempt one queued delivery under the bounded retry
    discipline, recording every attempt on the delivery row. A
    target the SSRF guard refuses is dead on the first attempt —
    retrying a deterministic refusal buys nothing."""
    delivery = store.get_change_delivery(delivery_id)
    if delivery is None:
        return {"delivery_id": delivery_id, "status": "missing"}
    cfg = store.get_change_webhook(delivery["watch_key"])
    if cfg is None:
        cfg = store.get_sub_webhook(delivery["watch_key"])
    if cfg is None:
        store.record_change_attempt(
            delivery_id, None, "webhook configuration removed before delivery"
        )
        store.finish_change_delivery(delivery_id, "dead_letter")
        return {"delivery_id": delivery_id, "status": "dead_letter"}
    payload = delivery["payload"]
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = hmac.new(
        cfg["secret"].encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-recheck-event-id": payload["event_id"],
        "x-recheck-signature": f"sha256={signature}",
    }
    url = cfg["webhook_url"]
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except Exception as exc:  # noqa: BLE001 - refusal is a recorded outcome
        store.record_change_attempt(
            delivery_id, None, f"target refused by the SSRF guard: {exc}"
        )
        store.finish_change_delivery(delivery_id, "dead_letter")
        return {"delivery_id": delivery_id, "status": "dead_letter"}
    waits = _change_webhook_backoffs()
    for attempt in range(len(waits) + 1):
        try:
            resp = await client.post(
                url,
                content=body,
                headers=headers,
                timeout=10.0,
                follow_redirects=False,
            )
            ok = 200 <= resp.status_code < 300
            store.record_change_attempt(
                delivery_id,
                resp.status_code,
                None if ok else f"HTTP {resp.status_code}",
            )
            if ok:
                store.finish_change_delivery(delivery_id, "delivered")
                return {"delivery_id": delivery_id, "status": "delivered"}
        except Exception as exc:  # noqa: BLE001 - recorded, then retried
            store.record_change_attempt(
                delivery_id, None, f"{type(exc).__name__}: {exc}"
            )
        if attempt < len(waits):
            await asyncio.sleep(waits[attempt])
    store.finish_change_delivery(delivery_id, "dead_letter")
    return {"delivery_id": delivery_id, "status": "dead_letter"}


def _fire_change_webhook(
    client: httpx.AsyncClient, store: WatchStore, envelope: dict
) -> None:
    """Queue + schedule delivery from inside recheck(). Delivery
    health never fails the check that produced the event: queueing
    is synchronous (so the delivery row exists before the caller
    returns), sending is a background task on the caller's client.
    Both webhook families are served: the per-watch config
    (fleet-internal) and self-serve subscriptions (hooksubs)."""
    delivery_ids: list[int] = []
    try:
        delivery_id = queue_change_event(store, envelope)
        if delivery_id is not None:
            delivery_ids.append(delivery_id)
    except Exception:  # noqa: BLE001 - the check already answered
        log.exception("change webhook queueing failed")
    try:
        delivery_ids.extend(queue_subscription_events(store, envelope))
    except Exception:  # noqa: BLE001 - the check already answered
        log.exception("subscription webhook queueing failed")
    for did in delivery_ids:
        try:
            asyncio.get_running_loop().create_task(
                deliver_change_event(client, store, did)
            )
        except RuntimeError:  # no running loop — row stays pending, readable
            log.warning(
                "change webhook delivery %s queued without a running loop",
                did,
            )
