"""Counterparty deep profile: the shared computation behind
POST /v1/counterparty-report/deep ($0.25) and the snapshot that
POST /v1/counterparty-watch ($0.03) stores and diffs.

Everything here is read, never invented:
  * wallet snapshot (reads.wallet_snapshot — RPC + Blockscout),
  * the wallet's recent ERC-20 transfers (Blockscout, newest 50) —
    counterparties, per-token volumes, and funding sources (the
    senders of incoming transfers IN THAT SAMPLE) are aggregated
    from exactly those transfers,
  * the bundled OFAC SDN snapshot (verdict.SNAPSHOT) — a
    counterparty that appears verbatim on it is a flagged link,
  * the Verdict Index history legs the standard report already
    reads (rotations in/out across the snapshots held).

Coverage is stated in every profile: the transfer sample size (or
"unknown" when Blockscout cannot supply it), the index window,
and the sanctions snapshot date. A signal that was not read is
never reported as absent — it is reported as not covered.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc
from . import counterparty as counterparty_svc
from . import reads as reads_svc
from . import verdict as verdict_svc
from .indexfeed import IndexStore

TRANSFER_SAMPLE_LIMIT = 50


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))


def _age_days(first_activity) -> int | None:
    if not isinstance(first_activity, str) or first_activity == "unknown":
        return None
    try:
        dt = datetime.fromisoformat(first_activity.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))


def _flagged_set() -> set[str]:
    """The fleet's in-repo flagged-address set for link screening:
    the bundled OFAC SDN snapshot (the same set the flagged-paytos
    feed and the wallet verdict screen against)."""
    return set(verdict_svc.SNAPSHOT["addresses"])


async def profile_snapshot(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    store: IndexStore,
    address: str,
) -> dict:
    """The machine-readable counterparty profile of one wallet.
    This is the unit counterparty-watch stores and diffs, and the
    deep report's extra legs are derived from it."""
    snap = await reads_svc.wallet_snapshot(client, rpc, scout, address)
    sanctions = verdict_svc.sanctions_check(address)
    transfers = await scout.token_transfers(
        address, limit=TRANSFER_SAMPLE_LIMIT
    )
    flagged = _flagged_set()
    me = address.lower()

    counterparties: dict[str, dict] = {}
    funding: dict[str, dict] = {}
    if transfers is not None:
        for t in transfers:
            frm = (t.get("from") or "").lower()
            to = (t.get("to") or "").lower()
            if frm == me and to:
                other, direction = to, "outgoing"
            elif to == me and frm:
                other, direction = frm, "incoming"
            else:
                continue
            entry = counterparties.setdefault(
                other,
                {
                    "address": other,
                    "tx_count": 0,
                    "incoming_count": 0,
                    "outgoing_count": 0,
                    "volumes_raw_by_token": {},
                    "first_seen_in_sample": t.get("timestamp"),
                    "last_seen_in_sample": t.get("timestamp"),
                    "flagged_ofac": other in flagged,
                },
            )
            entry["tx_count"] += 1
            entry[f"{direction}_count"] += 1
            symbol = t.get("token_symbol") or "unknown"
            raw = t.get("value_raw")
            try:
                entry["volumes_raw_by_token"][symbol] = str(
                    int(entry["volumes_raw_by_token"].get(symbol, "0"))
                    + int(raw)
                )
            except (TypeError, ValueError):
                pass
            ts = t.get("timestamp")
            if ts:
                if entry["first_seen_in_sample"] is None or ts < entry["first_seen_in_sample"]:
                    entry["first_seen_in_sample"] = ts
                if entry["last_seen_in_sample"] is None or ts > entry["last_seen_in_sample"]:
                    entry["last_seen_in_sample"] = ts
            if direction == "incoming":
                f = funding.setdefault(
                    other,
                    {
                        "address": other,
                        "tx_count": 0,
                        "volumes_raw_by_token": {},
                        "flagged_ofac": other in flagged,
                    },
                )
                f["tx_count"] += 1
                try:
                    f["volumes_raw_by_token"][symbol] = str(
                        int(f["volumes_raw_by_token"].get(symbol, "0"))
                        + int(raw)
                    )
                except (TypeError, ValueError):
                    pass

    flags: list[dict] = []
    if sanctions.get("matched"):
        flags.append(
            {"type": "ofac_sdn_match", "address": me,
             "detail": "Wallet appears verbatim on the OFAC SDN digital-currency list."}
        )
    for cp in counterparties.values():
        if cp["flagged_ofac"]:
            flags.append(
                {"type": "ofac_flagged_counterparty", "address": cp["address"],
                 "detail": "Counterparty appears verbatim on the OFAC SDN digital-currency list."}
            )

    return {
        "address": address,
        "generated_at": _now_iso(),
        "eth_balance_wei": snap.get("eth_balance_wei"),
        "outgoing_tx_count_nonce": snap.get("outgoing_tx_count_nonce"),
        "is_contract": snap.get("is_contract"),
        "first_activity": snap.get("first_activity"),
        "last_activity": snap.get("last_activity"),
        "wallet_age_days": _age_days(snap.get("first_activity")),
        "token_symbols": sorted(
            {t.get("symbol") for t in (snap.get("token_balances") or [])
             if isinstance(t, dict) and t.get("symbol")}
        ) if isinstance(snap.get("token_balances"), list) else [],
        "sanctions": sanctions,
        "counterparties": counterparties,
        "funding_sources": sorted(funding.values(), key=lambda f: f["address"]),
        "flags": flags,
        "coverage": {
            "token_transfers_sampled": len(transfers) if transfers is not None else "unknown",
            "token_transfer_sample_limit": TRANSFER_SAMPLE_LIMIT,
            "counterparty_window": (
                "newest "
                f"{len(transfers)} ERC-20 transfers from Blockscout"
                if transfers is not None
                else "unavailable — Blockscout could not supply the transfer list"
            ),
            "sanctions_snapshot_date": verdict_svc.SNAPSHOT["snapshot_date"],
            "sanctions_source": verdict_svc.SNAPSHOT["source_url"],
            "wallet_snapshot_sources": snap.get("sources"),
        },
    }


async def run_counterparty_deep(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    store: IndexStore,
    *,
    pay_to: str,
    token_address: str | None = None,
    chain: str = "base",
    service_url: str | None = None,
    listing_url: str | None = None,
    resource: str | None = None,
    secret: str | None = None,
) -> dict:
    """The full tier: everything the standard Counterparty Report
    has, plus the deep legs derived from the profile snapshot."""
    base = await counterparty_svc.run_counterparty(
        client, rpc, scout, store,
        pay_to=pay_to, token_address=token_address, chain=chain,
        service_url=service_url, listing_url=listing_url,
        resource=resource, secret=secret,
    )
    profile = await profile_snapshot(client, rpc, scout, store, pay_to)

    index_leg = (base.get("legs") or {}).get("index") or {}
    index_result = index_leg.get("result") or {}
    rug_signals: list[dict] = []
    if profile["sanctions"].get("matched"):
        rug_signals.append({
            "signal": "ofac_sdn_match",
            "detail": "Wallet appears verbatim on the OFAC SDN digital-currency list.",
        })
    for f in profile["flags"]:
        if f["type"] == "ofac_flagged_counterparty":
            rug_signals.append({
                "signal": "transacts_with_ofac_flagged_address",
                "detail": f"Sampled transfers touch OFAC-listed address {f['address']}.",
            })
    rotations_out = index_result.get("rotations_out") or []
    if rotations_out:
        rug_signals.append({
            "signal": "index_payto_rotations_out",
            "detail": (
                f"{len(rotations_out)} payTo rotation(s) away from this "
                "wallet across the Index snapshots held."
            ),
        })
    removed = index_result.get("listings_removed_in_window") or 0
    if removed:
        rug_signals.append({
            "signal": "index_listings_removed",
            "detail": f"{removed} listing(s) removed in the Index window held.",
        })
    age = profile["wallet_age_days"]
    if age is not None and age < 30:
        rug_signals.append({
            "signal": "young_wallet",
            "detail": f"First observed activity {age} day(s) ago (Blockscout window).",
        })
    if profile["outgoing_tx_count_nonce"] == 0 and profile["eth_balance_wei"] in (None, "0"):
        rug_signals.append({
            "signal": "no_outgoing_activity",
            "detail": "Nonce is 0 and ETH balance is zero at read time.",
        })

    deep = {
        "wallet_age": {
            "first_activity": profile["first_activity"],
            "last_activity": profile["last_activity"],
            "wallet_age_days": profile["wallet_age_days"],
            "source": "Blockscout activity window via the wallet snapshot leg",
        },
        "funding_sources": profile["funding_sources"],
        "flagged_counterparty_links": [
            cp for cp in profile["counterparties"].values() if cp["flagged_ofac"]
        ],
        "counterparties": sorted(
            profile["counterparties"].values(), key=lambda c: c["address"]
        ),
        "rug_history_signals": rug_signals,
        "coverage": profile["coverage"],
    }
    return {
        **base,
        "tier": "deep",
        "deep": deep,
        "profile": profile,
        "limits": (
            base["limits"]
            + " Deep legs: funding sources and counterparty links cover "
            f"only the newest {TRANSFER_SAMPLE_LIMIT} ERC-20 transfers "
            "Blockscout returns (stated in deep.coverage); flagged links "
            "are exact matches against the bundled OFAC SDN snapshot "
            "only — no other drainer/scam list is bundled in this "
            "service, so no link to one is claimed. Rug-history signals "
            "are derived only from the legs actually read above."
        ),
    }
