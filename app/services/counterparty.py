"""Counterparty Report: POST /v1/counterparty-report ($1.00).

One call about one seller, assembled from the legs that apply —
local legs read this service's own stores and chain reads; remote
legs reuse Orchestrate's fleet-internal fan-out (FLEET_INTERNAL_SECRET
header, never in discovery):

  wallet     — StallRow wallet snapshot of the payTo (local): ETH
               balance, top token holdings, activity window, nonce.
  sanctions  — the bundled OFAC SDN exact-match screen (local).
  index      — Verdict Index history for the payTo (local store):
               first seen, listings now, rotations in/out across
               the snapshots held.
  token_verdict — Token Verdict on a token contract, ONLY when the
               buyer names one (a seller is not always a token).
  locksmith  — LockSmith audit of the seller's service endpoint,
               ONLY when a service URL is given.
  listsmith  — ListSmith grade of the seller's listing/manifest,
               ONLY when a listing URL is given.

Honesty rules, same as Orchestrate's: a leg that was not requested
says "not_requested" (never silently omitted); a leg that ran and
failed says "failed" with the reason; a local leg whose data source
is empty says "unavailable". The report delivers when at least one
substantive leg delivered (wallet, index, or any requested remote
leg); when nothing delivered, the call raises UpstreamError and
the payment does not settle. Partial reports are the normal case
and are labelled with exactly which legs are missing.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc, UpstreamError
from . import orchestrate as orchestrate_svc
from . import pretrade as pretrade_svc
from . import reads as reads_svc
from . import verdict as verdict_svc
from .indexfeed import IndexStore

LIMITS = (
    "Legs marked not_requested were not asked for and were not run. "
    "Legs marked failed or unavailable delivered nothing — the rest "
    "of the report stands on its own. Index history covers only the "
    "catalog snapshots this service holds (the newest 3, refreshed "
    "about every 6 hours). Sanctions matching is exact-address only "
    "against the bundled snapshot. This is an automated report, not "
    "a guarantee about the counterparty."
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))


async def _wallet_leg(client, rpc, scout, pay_to: str) -> dict:
    try:
        result = await reads_svc.wallet_snapshot(client, rpc, scout, pay_to)
    except Exception as e:  # noqa: BLE001 — a failed leg is data, not a crash
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}
    return {"status": "ok", "result": result}


async def run_counterparty(
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
    # --- local legs (no network beyond the wallet leg's chain reads) ---
    sanctions_raw = verdict_svc.sanctions_check(pay_to)
    sanctions_leg = {
        "status": "unavailable" if sanctions_raw["status"] == "unknown" else "ok",
        "result": sanctions_raw,
    }
    index_raw = pretrade_svc.index_history(store, pay_to, resource or listing_url)
    index_leg = {
        "status": "ok" if index_raw["status"] == "ok" else "unavailable",
        "result": index_raw,
    }

    # --- legs that run concurrently --------------------------------------
    async def _not_requested() -> dict:
        return {"status": "not_requested"}

    wallet_coro = _wallet_leg(client, rpc, scout, pay_to)
    if secret is None:
        secret = os.environ.get("FLEET_INTERNAL_SECRET", "")
    token_coro = (
        orchestrate_svc._leg(
            client, secret, "token_verdict", "/v1/internal/verdict",
            {"address": token_address, "chain": chain},
        )
        if token_address
        else _not_requested()
    )
    locksmith_coro = (
        orchestrate_svc._leg(
            client, secret, "locksmith", "/v1/internal/audit",
            {"endpoint_url": service_url},
        )
        if service_url
        else _not_requested()
    )
    listsmith_coro = (
        orchestrate_svc._leg(
            client, secret, "listsmith", "/v1/internal/grade",
            {"resource_url": listing_url},
        )
        if listing_url
        else _not_requested()
    )
    wallet_leg, token_leg, locksmith_leg, listsmith_leg = await asyncio.gather(
        wallet_coro, token_coro, locksmith_coro, listsmith_coro
    )

    legs = {
        "wallet": wallet_leg,
        "sanctions": sanctions_leg,
        "index": index_leg,
        "token_verdict": token_leg,
        "locksmith": locksmith_leg,
        "listsmith": listsmith_leg,
    }
    delivered = [
        name
        for name in ("wallet", "index", "token_verdict", "locksmith", "listsmith")
        if legs[name]["status"] == "ok"
    ]
    if not delivered:
        raise UpstreamError(
            "counterparty report: no leg delivered ("
            + "; ".join(
                f"{n}: {legs[n].get('error', legs[n]['status'])}"
                for n in ("wallet", "index", "token_verdict", "locksmith", "listsmith")
                if legs[n]["status"] != "not_requested"
            )
            + ")"
        )
    return {
        "pay_to": pay_to,
        "token_address": token_address,
        "service_url": service_url,
        "listing_url": listing_url,
        "generated_at": _now_iso(),
        "legs_ok": delivered,
        "legs_failed": [
            n for n, leg in legs.items() if leg["status"] == "failed"
        ],
        "legs_unavailable": [
            n for n, leg in legs.items() if leg["status"] == "unavailable"
        ],
        "legs_not_requested": [
            n for n, leg in legs.items() if leg["status"] == "not_requested"
        ],
        "legs": legs,
        "limits": LIMITS,
    }
