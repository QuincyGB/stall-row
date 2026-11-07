"""Fleet delivery ledger (2026-11-07): the fleet's ONE durable
record of what every settled payment bought.

Why this exists: an on-chain settlement names only a payer, an
amount, and the fleet payTo. Several routes share a price, so an
amount alone can never identify which route a payment bought (the
receipt-verify service has always said so). Until now the stalls
kept only best-effort, per-service diaries (payer memory, accuracy
ledger) — none of them carrying the settlement tx, and all of them
allowed to fail silently. The owner ruled that unacceptable: every
transaction must be identifiable, by construction.

Design (owner correction, 2026-11-07 — fail-closed, no silent gaps):

  * The record is written as part of the delivery flow itself,
    BEFORE the delivery response is released: each stall's delivery
    layer posts the record here after settlement and before
    returning the delivery. If the record cannot be written, the
    delivery fails loudly (502) — a settled-but-unrecorded delivery
    must never pass silently.
  * Records land in ONE durable store: this hub's sqlite database
    (the same /data-volume store discipline as the fleet-pass
    ledger — StallRow already hosts the fleet's shared state).
  * Records are accepted only when independently verifiable:
      - per-call: the settlement tx must carry a USDC transfer on
        Base matching (payer -> fleet payTo, amount). Verified
        against the chain at write time (receipts._usdc_transfers);
        a tx that is not found, or carries no matching transfer,
        is REJECTED (the delivery then fails loudly and the
        checker-side reconciliation investigates the transfer).
        If the RPC itself is unreachable, the record is accepted
        as unverified-pending rather than blocking the fleet on a
        public RPC outage; reconciliation settles it later and
        alarms if it never verifies.
      - fleet-pass / pack-funded: the funding ref (debit id / pack
        id) must exist in this hub's own ledger tables.
      - Solana rail: recorded as-is (the Base reconciliation never
        sees that payTo); marked unverified, rail-labelled.
  * The checker harvests GET /v1/delivery-ledger/recent into a
    durable ledger file and reconciles on-chain transfers to
    payTo against these records; any transfer with no record is
    an open incident until identified — never an amount guess.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
import time

import httpx

from ..rpc import Rpc, UpstreamError
from .receipts import _usdc_transfers

log = logging.getLogger("stall-row.deliveryledger")

BASE_NETWORK = "eip155:8453"

# Funding kinds whose ref names a row in this hub's own tables:
# funding -> (table, primary-key column)
_REF_TABLES = {
    "fleet_pass": ("fleet_pass_debits", "debit_id"),
    "recheck_pack": ("recheck_packs", "pack_id"),
    "toolkit_pack": ("toolkit_packs", "pack_id"),
}


class RecordRejected(Exception):
    """The record could not be verified — refuse it (and with it,
    loudly, the delivery it belongs to)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DeliveryLedgerStore:
    """Delivery records in the hub's shared sqlite store (same DB
    file as the recheck/fleet-pass store; its own table)."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    service TEXT NOT NULL,
                    route TEXT NOT NULL,
                    payer TEXT NOT NULL,
                    amount_units INTEGER NOT NULL,
                    tx TEXT,
                    network TEXT,
                    funding TEXT NOT NULL,
                    ref TEXT,
                    verified INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS delivery_records_tx "
                "ON delivery_records(tx) WHERE tx IS NOT NULL"
            )
            self._conn.commit()

    def insert(self, record: dict) -> tuple[int, bool]:
        """Insert a record; returns (id, created). A tx that is
        already recorded is a duplicate delivery attempt of the
        same settlement — return the existing row, never a twin."""
        with self._lock:
            if record.get("tx"):
                row = self._conn.execute(
                    "SELECT id FROM delivery_records WHERE tx = ?",
                    (record["tx"],),
                ).fetchone()
                if row is not None:
                    return int(row["id"]), False
            cur = self._conn.execute(
                """
                INSERT INTO delivery_records
                    (ts, service, route, payer, amount_units, tx,
                     network, funding, ref, verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["ts"],
                    record["service"],
                    record["route"],
                    record["payer"],
                    int(record["amount_units"]),
                    record.get("tx"),
                    record.get("network"),
                    record["funding"],
                    record.get("ref"),
                    int(record.get("verified") or 0),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid), True

    def recent(self, limit: int = 500, since_id: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_records
                WHERE id > ? ORDER BY id ASC LIMIT ?
                """,
                (int(since_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def ref_exists(self, table: str, column: str, value: str) -> bool:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                return False
            row = self._conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",  # noqa: S608 — table/column from the fixed _REF_TABLES map only
                (value,),
            ).fetchone()
        return row is not None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _verify_on_chain(payload: dict, rpc_url: str, pay_to: str) -> int:
    """1 when the settlement tx provably carries the claimed USDC
    transfer; 0 when the chain could not be asked (RPC unreachable —
    accept pending, reconciliation settles it). Raises
    RecordRejected when the chain answers and the claim is wrong
    or the tx does not exist."""
    tx = payload["tx"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            rpc = Rpc(client, rpc_url)
            receipt = await rpc.eth_receipt(tx)
    except (httpx.HTTPError, UpstreamError) as exc:
        log.warning(
            "delivery ledger: chain check unreachable for tx %s (%s) — "
            "recording as pending; reconciliation must settle it",
            tx,
            exc,
        )
        return 0
    if receipt is None:
        raise RecordRejected(
            f"settlement tx {tx} not found on Base — nothing settled, "
            "no record (a delivery claiming it must fail loudly)"
        )
    for transfer in _usdc_transfers(receipt):
        if (
            (transfer["to"] or "").lower() == pay_to.lower()
            and (transfer["from"] or "").lower() == payload["payer"].lower()
            and int(transfer["amount_units"]) == int(payload["amount_units"])
        ):
            return 1
    raise RecordRejected(
        f"settlement tx {tx} carries no USDC transfer matching "
        f"payer/amount/payTo — record refused"
    )


async def write_record(
    store: DeliveryLedgerStore, payload: dict, *, rpc_url: str, pay_to: str
) -> dict:
    """Verify, then durably record one delivery. Returns the stored
    record (with id). Raises RecordRejected on a failed verification
    and lets store errors propagate — both are loud by design."""
    funding = (payload.get("funding") or "").strip()
    record = {
        "ts": _now_iso(),
        "service": (payload.get("service") or "").strip(),
        "route": (payload.get("route") or "").strip(),
        "payer": (payload.get("payer") or "").strip(),
        "amount_units": int(payload.get("amount_units") or 0),
        "tx": (payload.get("tx") or None),
        "network": (payload.get("network") or None),
        "funding": funding,
        "ref": (payload.get("ref") or None),
        "verified": 0,
    }
    if not record["service"] or not record["route"] or not record["payer"]:
        raise RecordRejected("record is missing service, route, or payer")
    if funding == "per_call":
        if not record["tx"]:
            raise RecordRejected("per-call record without a settlement tx")
        if (record["network"] or "").startswith("solana"):
            record["verified"] = 0  # Base chain check does not apply
        else:
            record["verified"] = await _verify_on_chain(record, rpc_url, pay_to)
    elif funding in _REF_TABLES:
        table, column = _REF_TABLES[funding]
        if not record["ref"] or not store.ref_exists(table, column, record["ref"]):
            raise RecordRejected(
                f"funding ref {record['ref']!r} is not a {funding} row "
                "in the hub ledger — record refused"
            )
        record["verified"] = 1
    else:
        raise RecordRejected(f"unknown funding kind {funding!r}")
    rid, created = store.insert(record)
    record["id"] = rid
    record["duplicate"] = not created
    return record


# --- payload construction (shared shape; each stall's delivery layer
# builds this from its settled response) -------------------------------


def extract_receipt(response) -> dict | None:
    """Decode the base64 `payment-response` settlement receipt the
    x402 SDK sets on a settled response, if any."""
    raw = response.headers.get("payment-response")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def build_payload(
    request,
    response,
    *,
    service: str,
    price_units: dict,
    credential: tuple[str, str] | None = None,
    debit_id: str | None = None,
) -> dict | None:
    """The delivery-record payload for a response, or None when the
    response is not a paid delivery (unpaid, error, unpriced path).
    Mirrors the loop-block eligibility: POST to a priced route with
    a 2xx JSON response."""
    if (
        request.method != "POST"
        or request.url.path not in price_units
        or not (200 <= response.status_code < 300)
    ):
        return None
    content_type = response.headers.get("content-type") or ""
    if "application/json" not in content_type:
        return None
    base = {
        "service": service,
        "route": request.url.path,
        "amount_units": int(price_units[request.url.path]),
    }
    if credential is not None:
        kind, ref = credential
        funding = {
            "fleet-pass": "fleet_pass",
            "recheck-pack": "recheck_pack",
            "toolkit-pack": "toolkit_pack",
        }.get(kind, kind)
        return {
            **base,
            "payer": f"{kind}:{ref}",
            "tx": None,
            "network": None,
            "funding": funding,
            "ref": debit_id or ref,
        }
    if debit_id is not None:
        # Pass-funded on a stall whose layer tracks the debit id
        # rather than a credential tuple (token-verdict/lock-smith
        # shape): the payer identity is the pass in the header.
        pass_id = (request.headers.get("x-fleet-pass") or "").strip()
        return {
            **base,
            "payer": f"fleet-pass:{pass_id}",
            "tx": None,
            "network": None,
            "funding": "fleet_pass",
            "ref": debit_id,
        }
    receipt = extract_receipt(response)
    if receipt is None:
        # A settled per-call delivery MUST carry its receipt. Its
        # absence is exactly the silent gap this ledger exists to
        # kill — surface it as a record with no tx so write_record
        # rejects it loudly instead of passing unrecorded.
        return {**base, "payer": "", "tx": None, "network": None,
                "funding": "per_call", "ref": None}
    return {
        **base,
        "payer": receipt.get("payer") or "",
        "tx": receipt.get("transaction"),
        "network": receipt.get("network"),
        "funding": "per_call",
        "ref": None,
    }
