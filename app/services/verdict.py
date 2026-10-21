"""/v1/wallet-verdict — is this Base wallet safe to interact with?

Signals: OFAC SDN exact-match against the bundled snapshot, wallet age
(first observed transaction), activity level, and an address-poisoning
heuristic (recent zero-value token transfers FROM lookalike addresses —
the classic poison pattern: a scammer seeds your history with a
zero-value transfer from an address sharing the first/last characters
of one you trust, hoping your next send copies it).

Degradation contract: any signal whose source is unavailable comes back
"unknown" and is EXCLUDED from scoring — sanctions in particular NEVER
returns "clean" off a missing/empty snapshot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc

_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "ofac_crypto_addresses.json"


def _load_snapshot() -> dict:
    try:
        data = json.loads(_SNAPSHOT_PATH.read_text())
        addrs = {a.lower() for a in data.get("addresses", []) if isinstance(a, str)}
        return {
            "addresses": addrs,
            "source_url": data.get("source_url"),
            "snapshot_date": data.get("snapshot_date"),
            "count": len(addrs),
        }
    except (OSError, ValueError):
        return {"addresses": set(), "source_url": None, "snapshot_date": None, "count": 0}


SNAPSHOT = _load_snapshot()


def sanctions_check(address: str) -> dict:
    """Exact-match against the bundled OFAC snapshot. 'unknown' when the
    snapshot is missing/empty — never a fabricated 'clean'."""
    if not SNAPSHOT["addresses"]:
        return {
            "status": "unknown",
            "matched": None,
            "detail": "Sanctions snapshot is missing or empty in this deployment, so no match could be run.",
            "snapshot_date": SNAPSHOT["snapshot_date"],
            "snapshot_source": SNAPSHOT["source_url"],
        }
    matched = address.lower() in SNAPSHOT["addresses"]
    return {
        "status": "matched" if matched else "no_match",
        "matched": matched,
        "detail": (
            "Address appears verbatim on the OFAC SDN digital-currency list."
            if matched
            else "Address does not appear in the bundled OFAC SDN digital-currency snapshot."
        ),
        "snapshot_date": SNAPSHOT["snapshot_date"],
        "snapshot_source": SNAPSHOT["source_url"],
        "snapshot_entries": SNAPSHOT["count"],
    }


def _parse_ts(item: dict) -> datetime | None:
    if item.get("timestamp"):
        try:
            return datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            return None
    epoch = item.get("timestamp_epoch")
    if epoch:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    return None


def _lookalike(a: str, b: str) -> bool:
    """Same first 4 and last 4 hex chars (after 0x), different address —
    the shape poisoning addresses mimic."""
    a, b = a.lower(), b.lower()
    return a != b and len(a) == 42 and len(b) == 42 and a[:6] == b[:6] and a[-4:] == b[-4:]


def poisoning_check(address: str, transfers: list[dict] | None) -> dict:
    if transfers is None:
        return {"status": "unknown", "signals": [], "detail": "Token-transfer history unavailable from the explorer."}
    counterparties = set()
    for t in transfers:
        for side in ("from", "to"):
            v = t.get(side)
            if isinstance(v, str) and v.lower() != address.lower():
                counterparties.add(v.lower())
    signals = []
    for t in transfers:
        frm = (t.get("from") or "").lower()
        raw = str(t.get("value_raw", ""))
        if not frm or frm == address.lower():
            continue
        if raw.isdigit() and int(raw) == 0:
            for cp in counterparties:
                if cp != frm and _lookalike(frm, cp):
                    signals.append(
                        {
                            "poison_sender": frm,
                            "mimics": cp,
                            "tx_hash": t.get("tx_hash"),
                            "detail": "Zero-value token transfer from an address sharing the first/last characters of a real counterparty.",
                        }
                    )
                    break
    return {
        "status": "signals_found" if signals else "none_found",
        "signals": signals[:5],
        "detail": (
            f"{len(signals)} poisoning-shaped transfer(s) in recent history."
            if signals
            else "No zero-value lookalike transfers in the recent history sampled."
        ),
    }


async def wallet_verdict(
    client: httpx.AsyncClient, rpc: Rpc, scout: Blockscout, address: str
) -> dict:
    sanctions = sanctions_check(address)

    txs_asc = await scout.transactions(address, sort="asc", limit=1)
    first_dt = _parse_ts(txs_asc[0]) if txs_asc else None
    age_days = None
    if first_dt is not None:
        age_days = max(0, (datetime.now(timezone.utc) - first_dt).days)
    try:
        nonce = await rpc.eth_tx_count(address)
    except Exception:  # noqa: BLE001 — activity is a soft signal
        nonce = None
    transfers = await scout.token_transfers(address, limit=25)
    poison = poisoning_check(address, transfers)

    reasons: list[dict] = []
    score = 100

    def deduct(name: str, points: int, detail: str) -> None:
        nonlocal score
        reasons.append({"name": name, "points": points, "detail": detail})
        score -= points

    if sanctions["status"] == "matched":
        deduct("OFAC SDN match", 100, sanctions["detail"])
    elif sanctions["status"] == "unknown":
        reasons.append(
            {"name": "sanctions check unavailable", "points": 0, "detail": sanctions["detail"]}
        )

    if age_days is None:
        reasons.append(
            {
                "name": "wallet age unknown",
                "points": 0,
                "detail": "Explorer could not supply the first transaction, so age was not scored.",
            }
        )
    elif age_days < 1:
        deduct("brand-new wallet", 35, "First observed transaction is less than a day old.")
    elif age_days < 7:
        deduct("very young wallet", 20, f"First observed transaction is {age_days} day(s) old.")
    elif age_days < 30:
        deduct("young wallet", 10, f"First observed transaction is {age_days} day(s) old.")

    if nonce is not None and nonce <= 3 and (age_days or 0) < 30:
        deduct("minimal outgoing activity", 10, f"Only {nonce} outgoing transaction(s) ever.")

    if poison["status"] == "signals_found":
        deduct(
            "address-poisoning signals in history",
            25,
            "Zero-value transfers from lookalike addresses appear in this wallet's history — "
            "whoever interacts with it should never copy addresses from its history.",
        )

    score = max(0, score)
    verdict = "AVOID" if score < 40 or sanctions["status"] == "matched" else (
        "CAUTION" if score < 75 else "SAFE"
    )
    return {
        "address": address,
        "verdict": verdict,
        "score": score,
        "sanctions": sanctions,
        "wallet_age_days": age_days,
        "first_activity": first_dt.isoformat() if first_dt else "unknown",
        "outgoing_tx_count_nonce": nonce,
        "poisoning": poison,
        "reasons": reasons,
        "limits": (
            "Age/activity come from Blockscout's index and degrade to unknown when it is "
            "unreachable; sanctions matching is exact-address only against the bundled "
            "snapshot (see snapshot_date) — a no_match is not a guarantee about the person "
            "or entity behind the address."
        ),
    }
