"""Orchestrate: one call, four fleet legs, one combined report.

POST /v1/orchestrate ($0.75) fans out to the fleet's internal
unpaid paths (each sibling's /v1/internal/* route, gated by the
FLEET_INTERNAL_SECRET shared secret — see those services' main.py;
the secret travels only in the x-fleet-internal-secret header and
appears in no discovery surface, manifest, or response):

  token_verdict  <- Token Verdict  POST /v1/internal/verdict {address, chain}
  route_verdict  <- RouteVerdict   POST /v1/internal/check   {resource_url=service_url}
  locksmith      <- LockSmith      POST /v1/internal/audit   {endpoint_url=service_url}
  listsmith      <- ListSmith      POST /v1/internal/grade   {resource_url=listing_url}

Failure semantics are the fleet's: a failed leg is MARKED in the
report (status "failed" + the reason) and the other legs still
deliver; only a total failure — every leg down — raises
UpstreamError, so the paid call 502s and never settles. Legs run
concurrently: they are independent calls to other services, and
the caller's wait should be the slowest leg, not the sum.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from ..rpc import UpstreamError

LEG_TIMEOUT_S = 45.0

_DEFAULT_BASES = {
    "token_verdict": "https://token-verdict-production.up.railway.app",
    "route_verdict": "https://route-verdict-production.up.railway.app",
    "locksmith": "https://lock-smith-production.up.railway.app",
    "listsmith": "https://list-smith-production.up.railway.app",
}

_ENV_KEYS = {
    "token_verdict": "ORCHESTRATE_TOKEN_VERDICT_URL",
    "route_verdict": "ORCHESTRATE_ROUTE_VERDICT_URL",
    "locksmith": "ORCHESTRATE_LOCKSMITH_URL",
    "listsmith": "ORCHESTRATE_LISTSMITH_URL",
}


def _base(name: str) -> str:
    return (os.environ.get(_ENV_KEYS[name]) or _DEFAULT_BASES[name]).rstrip("/")


def _headline(name: str, result: dict) -> str | None:
    """One-line takeaway per leg, extracted from fields the sibling
    actually returns; None when the shape doesn't carry them."""
    if not isinstance(result, dict):
        return None
    if name == "token_verdict":
        v, s = result.get("verdict"), result.get("score")
        return f"{v} (risk {s})" if v else None
    if name == "route_verdict":
        return result.get("verdict") or result.get("status")
    if name == "locksmith":
        return result.get("verdict") or result.get("grade")
    if name == "listsmith":
        g = result.get("grade")
        return f"grade {g}" if g is not None else result.get("verdict")
    return None


async def _leg(
    client: httpx.AsyncClient,
    secret: str,
    name: str,
    path: str,
    payload: dict,
) -> dict:
    try:
        resp = await client.post(
            _base(name) + path,
            json=payload,
            headers={"x-fleet-internal-secret": secret},
            timeout=LEG_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 — a dead leg is data, not a crash
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}
    if resp.status_code != 200:
        return {
            "status": "failed",
            "error": f"sibling returned HTTP {resp.status_code}",
            "http_status": resp.status_code,
        }
    try:
        result = resp.json()
    except ValueError:
        return {"status": "failed", "error": "sibling returned non-JSON"}
    return {"status": "ok", "headline": _headline(name, result), "result": result}


async def run_orchestrate(
    client: httpx.AsyncClient,
    *,
    address: str,
    chain: str,
    service_url: str,
    listing_url: str,
    secret: str | None = None,
) -> dict:
    if secret is None:
        secret = os.environ.get("FLEET_INTERNAL_SECRET", "")
    legs = await asyncio.gather(
        _leg(client, secret, "token_verdict", "/v1/internal/verdict",
             {"address": address, "chain": chain}),
        _leg(client, secret, "route_verdict", "/v1/internal/check",
             {"resource_url": service_url}),
        _leg(client, secret, "locksmith", "/v1/internal/audit",
             {"endpoint_url": service_url}),
        _leg(client, secret, "listsmith", "/v1/internal/grade",
             {"resource_url": listing_url}),
    )
    named = dict(zip(("token_verdict", "route_verdict", "locksmith", "listsmith"), legs))
    ok = [n for n, leg in named.items() if leg["status"] == "ok"]
    if not ok:
        raise UpstreamError(
            "orchestrate: all four legs failed ("
            + "; ".join(f"{n}: {named[n].get('error', '?')}" for n in named)
            + ")"
        )
    return {
        "token": {"address": address, "chain": chain},
        "service_url": service_url,
        "listing_url": listing_url,
        "legs_ok": ok,
        "legs_failed": [n for n in named if n not in ok],
        "legs": named,
    }
