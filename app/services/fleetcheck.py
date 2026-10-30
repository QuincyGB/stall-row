"""Fleet Check: whole-fleet health sweeps for x402 stall owners.

Tonight's internal fleet checker (hidden_files/fleet-checker/check.py)
proved the shape on our own row: derive a fleet's routes, probe every
one UNPAID, and read the 402 challenges as the health record. This is
that checker as a buyable StallRow product — same discipline, pointed
at the BUYER's stalls instead of ours:

  * POST /v1/fleet-check ($0.10) — one sweep. The buyer names resource
    URLs and/or origins; an origin is expanded from its own
    /.well-known/x402 (priced resources only). Every resource is
    probed unpaid and classified OK / DEGRADED / DOWN with the
    specific defect (unreachable, 5xx, no 402, undecodable challenge,
    or — where the origin's own discovery doc declares the resource —
    a live amount/payTo that disagrees with the declaration). Each
    origin also gets a RouteVerdict-style grade (score + verdict).
  * POST /v1/fleet-check/pack ($5.00) — the standing form, mirroring
    the recheck pack: 100 sweep credits bound to ONE FIXED fleet
    definition, 90-day expiry. Sweeps run on the pack's interval
    (watch ticker) or on demand via POST /v1/fleet-check/run; one
    credit buys one SWEEP, never one route. Each run diffs against
    the previous sweep and POSTs new failures + recoveries to the
    buyer's webhook; a heartbeat push on no-change sweeps is opt-in
    (the Index subscription's pattern). POST /v1/fleet-check/status
    reads the latest table free.

READ-ONLY, structurally: the probe machinery is the recheck stall's
unpaid prober (SSRF check on the target and on every redirect hop,
capped bodies, retry discipline). No code path in this module can
pay, sign, or settle anything — the route descriptions say so,
because a checker that pays the routes it checks would be grading
its own homework with the seller's money.

Failure honesty mirrors the row: a route that cannot be probed is a
DOWN row in the table (the table is the deliverable), never a 502
for the whole sweep; a request whose targets are refused by the
SSRF guard is a 422 before anything runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from ..rpc import UpstreamError
from . import fetch as fetch_svc
from . import recheck as recheck_svc

log = logging.getLogger("stall-row.fleetcheck")

# Pricing lives with the pack constants in recheck.py (the store's
# module) and in config.PRICE_UNITS; these aliases keep this module's
# callers on one name.
ONE_SHOT_PRICE_UNITS = 100_000  # $0.10 — mirrors config.PRICE_UNITS
PACK_PRICE_UNITS = recheck_svc.FLEET_CHECK_PACK_PRICE_UNITS  # $5.00
PACK_CREDITS = recheck_svc.FLEET_CHECK_PACK_CREDITS  # 100 sweeps
PACK_TTL_DAYS = recheck_svc.FLEET_CHECK_PACK_TTL_DAYS  # 90

MAX_RESOURCES = 40  # expanded resources per sweep, hard cap
MAX_ORIGINS = 10
MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 86_400
DEFAULT_INTERVAL_S = 3_600
_PROBE_ATTEMPTS = 2
_PROBE_CONCURRENCY = 5

LIMITS = (
    "Unpaid probes at sweep time: liveness, HTTP status, and the "
    "402 challenge a buyer would meet. Declared-vs-live comparisons "
    "use the origin's own /.well-known/x402 when it publishes one; "
    "an origin that publishes none is checked live-only, never "
    "guessed about. This checker never pays, signs, or settles "
    "any route it probes."
)


def _now_iso() -> str:
    return recheck_svc._now_iso()


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _norm(url: str) -> str:
    return url.strip().rstrip("/")


async def _ssrf(url: str) -> None:
    """The fleet SSRF guard (fetch.ssrf_check): public http(s) only.
    Raises ValueError with the refusal."""
    await asyncio.to_thread(fetch_svc.ssrf_check, url)


# --- fleet definitions ---------------------------------------------------------

def _clean_urls(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        url = str(value).strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def normalize_definition(
    resources: list[str] | None, origins: list[str] | None
) -> dict:
    """Dedupe + cap a buyer-supplied fleet definition. Raises
    ValueError (the handler's 422) on an empty or oversized one."""
    clean_resources = _clean_urls(resources)
    clean_origins = [o.rstrip("/") for o in _clean_urls(origins)]
    if len(clean_resources) > MAX_RESOURCES:
        raise ValueError(
            f"at most {MAX_RESOURCES} resource URLs per sweep "
            f"(got {len(clean_resources)})"
        )
    if len(clean_origins) > MAX_ORIGINS:
        raise ValueError(
            f"at most {MAX_ORIGINS} origins per sweep (got {len(clean_origins)})"
        )
    if not clean_resources and not clean_origins:
        raise ValueError("name at least one resource URL or one origin")
    return {"resources": clean_resources, "origins": clean_origins}


async def validate_definition(
    resources: list[str] | None,
    origins: list[str] | None,
    webhook_url: str | None = None,
) -> dict:
    """Normalize, then SSRF-check every URL the sweep would touch —
    before anything is stored or probed. Raises ValueError."""
    definition = normalize_definition(resources, origins)
    for url in definition["resources"] + definition["origins"]:
        await _ssrf(url)
    if webhook_url:
        await _ssrf(webhook_url.strip())
    return definition


# --- discovery expansion ---------------------------------------------------------

def _price_to_units(price) -> str | None:
    """'$0.005' -> '5000'; None for free/unparseable prices."""
    if price is None:
        return None
    try:
        amount = Decimal(str(price).replace("$", "").strip())
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return str(int(amount * Decimal(1_000_000)))


async def _fetch_discovery(
    client: httpx.AsyncClient, origin: str
) -> tuple[dict, list[str]]:
    """GET {origin}/.well-known/x402 under the probe's SSRF
    discipline. Returns (declared entries by normalized URL, priced
    resource URLs). Any failure returns ({}, []) — a missing or
    broken declaration is data for the sweep, not an error."""
    try:
        status, _headers, body, _redirs = await recheck_svc._get_once(
            client, f"{origin}/.well-known/x402", "GET"
        )
    except Exception:  # noqa: BLE001 — unreachable doc = no declarations
        return {}, []
    if status != 200 or not body:
        return {}, []
    try:
        doc = json.loads(body)
    except ValueError:
        return {}, []
    if not isinstance(doc, dict):
        return {}, []
    entries: dict[str, dict] = {}
    priced_urls: list[str] = []
    for entry in doc.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("resource") or entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        entries[_norm(url)] = entry
        units = None
        accepts = entry.get("accepts") or []
        if accepts and isinstance(accepts[0], dict):
            units = accepts[0].get("amount")
        if units is None:
            units = _price_to_units(entry.get("price"))
        if units is not None:
            priced_urls.append(url)
    return entries, priced_urls


# --- probing ---------------------------------------------------------------------

async def _attempt(client: httpx.AsyncClient, url: str):
    """POST first (x402 resources live behind POST); a 405/501 falls
    back to GET — the recheck prober's discipline, unchanged."""
    status, headers, body, _redirs = await recheck_svc._get_once(client, url, "POST")
    if status in (405, 501):
        status, headers, body, _redirs = await recheck_svc._get_once(client, url, "GET")
    return status, headers, body


async def _probe_raw(client: httpx.AsyncClient, url: str):
    """Probe one resource and return (status, headers, body, error)
    WITHOUT raising for target defects: a 404 or a 500 is a finding
    for the table, not a failed sweep. `error` is set only when no
    HTTP response could be had at all. Unpaid, always."""
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        return None, None, None, f"refused: {e}"
    last_error: str | None = None
    for attempt in range(_PROBE_ATTEMPTS):
        if attempt:
            backoff = recheck_svc._BACKOFF_S
            await asyncio.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
        try:
            status, headers, body = await _attempt(client, url)
        except ValueError as e:  # a redirect hop went non-public
            return None, None, None, f"refused: {e}"
        except UpstreamError as e:  # redirect discipline failure
            last_error = str(e)
            continue
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        if status in recheck_svc._RETRY_STATUSES and attempt + 1 < _PROBE_ATTEMPTS:
            last_error = f"target returned HTTP {status}"
            continue
        return status, headers, body, None
    return None, None, None, last_error or "target probe failed"


def _classify(
    url: str,
    status: int | None,
    headers,
    body: bytes | None,
    error: str | None,
    declared: dict | None,
) -> dict:
    """One route's verdict row. The defect names the specific thing
    that is wrong; an OK row carries the full accept summary a buyer
    would meet."""
    row = {
        "url": url,
        "origin": _origin_of(url),
        "alive": False,
        "http_status": status,
        "challenge_ok": False,
        "accepts": [],
        "verdict": "DOWN",
        "defects": [],
    }
    if error is not None:
        code = "target_refused" if error.startswith("refused:") else "unreachable"
        detail = error[len("refused: "):] if code == "target_refused" else error
        row["defects"] = [{"code": code, "detail": detail}]
        return row
    row["alive"] = True
    assert status is not None
    if status >= 500:
        row["defects"] = [
            {"code": "server_error", "detail": f"target returned HTTP {status}"}
        ]
        return row
    if status == 200:
        row["verdict"] = "DEGRADED"
        row["defects"] = [
            {
                "code": "no_402",
                "detail": "answers HTTP 200 to an unpaid probe — no payment gate",
            }
        ]
        return row
    if status != 402:
        row["verdict"] = "DEGRADED"
        row["defects"] = [
            {
                "code": "unexpected_status",
                "detail": f"answers HTTP {status}, not a 402 payment challenge",
            }
        ]
        return row
    challenge = recheck_svc._parse_challenge(headers, body)
    if challenge is None or not challenge.get("accepts"):
        row["verdict"] = "DEGRADED"
        row["defects"] = [
            {
                "code": "challenge_undecodable",
                "detail": (
                    "answers 402 but the challenge does not parse as "
                    "x402 v2 or carries no accepts"
                ),
            }
        ]
        return row
    row["challenge_ok"] = True
    accepts = []
    for accept in challenge["accepts"]:
        if isinstance(accept, dict):
            accepts.append(
                {
                    "scheme": accept.get("scheme"),
                    "network": accept.get("network"),
                    "asset": accept.get("asset"),
                    "payTo": accept.get("payTo"),
                    "amount": accept.get("amount"),
                }
            )
    row["accepts"] = accepts
    defects = _declared_defects(accepts, declared)
    row["defects"] = defects
    row["verdict"] = "DEGRADED" if defects else "OK"
    return row


def _declared_defects(accepts: list[dict], declared: dict | None) -> list[dict]:
    """Live challenge vs the origin's own declaration of this
    resource. Only facts the declaration actually states are
    compared — an undeclared field is not a defect."""
    if not declared or not accepts:
        return []
    defects: list[dict] = []
    declared_accepts = [
        a for a in (declared.get("accepts") or []) if isinstance(a, dict)
    ]
    if declared_accepts:
        by_network = {a.get("network"): a for a in declared_accepts}
        for live in accepts:
            decl = by_network.get(live.get("network"))
            if decl is None:
                continue
            if (
                decl.get("amount") is not None
                and live.get("amount") is not None
                and str(decl["amount"]) != str(live["amount"])
            ):
                defects.append(
                    {
                        "code": "amount_mismatch",
                        "detail": (
                            f"declared amount {decl['amount']} but the live "
                            f"challenge asks {live['amount']} on "
                            f"{live.get('network')}"
                        ),
                    }
                )
            if (
                decl.get("payTo")
                and live.get("payTo")
                and str(decl["payTo"]).lower() != str(live["payTo"]).lower()
            ):
                defects.append(
                    {
                        "code": "payto_mismatch",
                        "detail": (
                            f"declared payTo {decl['payTo']} but the live "
                            f"challenge pays {live['payTo']} on "
                            f"{live.get('network')}"
                        ),
                    }
                )
        return defects
    units = _price_to_units(declared.get("price"))
    if units is not None and str(accepts[0].get("amount")) != units:
        defects.append(
            {
                "code": "amount_mismatch",
                "detail": (
                    f"declared price {declared.get('price')} ({units} units) "
                    f"but the live challenge asks {accepts[0].get('amount')}"
                ),
            }
        )
    return defects


def _grade(rows: list[dict]) -> dict:
    """RouteVerdict-style grade for one origin: a 0–100 score (an OK
    route counts full, a DEGRADED route half, a DOWN route zero)
    and the verdict band it lands in."""
    counts = {"ok": 0, "degraded": 0, "down": 0}
    for row in rows:
        counts[row["verdict"].lower()] += 1
    total = len(rows)
    if total == 0:
        return {**counts, "resources": 0, "score": None, "verdict": "UNKNOWN"}
    score = int(round(100 * (counts["ok"] + 0.5 * counts["degraded"]) / total))
    if counts["down"] == total:
        verdict = "DOWN"
    elif score >= 90:
        verdict = "TRUSTED"
    elif score >= 60:
        verdict = "CAUTION"
    else:
        verdict = "AT_RISK"
    return {**counts, "resources": total, "score": score, "verdict": verdict}


async def sweep(client: httpx.AsyncClient, definition: dict) -> dict:
    """One full sweep of a fleet definition: expand origins from
    their own discovery docs, probe every resource unpaid, classify,
    grade per origin. Route-level failures are rows in the table —
    this function raises only on our own machinery failing."""
    resources = list(definition.get("resources") or [])
    given_origins = list(definition.get("origins") or [])
    all_origins: list[str] = []
    for origin in given_origins + [_origin_of(u) for u in resources]:
        if origin not in all_origins:
            all_origins.append(origin)
    docs = await asyncio.gather(
        *[_fetch_discovery(client, origin) for origin in all_origins]
    )
    declared_by_origin: dict[str, dict] = {}
    truncated = 0
    for origin, (entries, priced_urls) in zip(all_origins, docs):
        declared_by_origin[origin] = entries
        if origin in given_origins:
            for url in priced_urls:
                if url not in resources:
                    if len(resources) < MAX_RESOURCES:
                        resources.append(url)
                    else:
                        truncated += 1
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(url: str) -> dict:
        async with semaphore:
            status, headers, body, error = await _probe_raw(client, url)
            declared = declared_by_origin.get(_origin_of(url), {}).get(_norm(url))
            return _classify(url, status, headers, body, error, declared)

    rows = await asyncio.gather(*[one(url) for url in resources])
    origin_rows = []
    for origin in all_origins:
        orows = [r for r in rows if r["origin"] == origin]
        if orows or origin in given_origins:
            origin_rows.append({"origin": origin, **_grade(orows)})
    summary = {
        "resources": len(rows),
        "ok": sum(1 for r in rows if r["verdict"] == "OK"),
        "degraded": sum(1 for r in rows if r["verdict"] == "DEGRADED"),
        "down": sum(1 for r in rows if r["verdict"] == "DOWN"),
        "origins": len(origin_rows),
        "truncated": truncated,
    }
    return {
        "checked_at": _now_iso(),
        "resources": rows,
        "origins": origin_rows,
        "summary": summary,
        "limits": LIMITS,
    }


# --- packs: recurring sweeps ------------------------------------------------------

_RANK = {"OK": 0, "DEGRADED": 1, "DOWN": 2}


def diff_sweeps(prev_rows: list[dict] | None, new_rows: list[dict]) -> dict:
    """Run-over-run diff for a pack: verdicts that got worse are new
    failures, verdicts back at OK are recoveries. A route seen for
    the first time is listed under `added` (baseline, not an event)
    unless a previous sweep exists and it arrives already broken."""
    prev = {r["url"]: r["verdict"] for r in prev_rows or []}
    new = {r["url"]: r["verdict"] for r in new_rows}
    new_failures: list[dict] = []
    recoveries: list[dict] = []
    for url, verdict in new.items():
        before = prev.get(url)
        if before is None:
            if prev and verdict != "OK":
                new_failures.append({"url": url, "from": None, "to": verdict})
            continue
        if _RANK[verdict] > _RANK[before]:
            new_failures.append({"url": url, "from": before, "to": verdict})
        elif verdict == "OK" and before != "OK":
            recoveries.append({"url": url, "from": before, "to": verdict})
    added = sorted(set(new) - set(prev))
    removed = sorted(set(prev) - set(new))
    return {
        "new_failures": new_failures,
        "recoveries": recoveries,
        "added": added,
        "removed": removed,
        "changed": bool(new_failures or recoveries or removed),
    }


async def _deliver(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    pack: dict,
    event: str,
    payload: dict,
) -> bool:
    """Webhook push under the watch stall's discipline: the URL is
    SSRF-checked again at delivery time (DNS can move), redirects
    are never followed, and the outcome is recorded either way."""
    webhook = pack.get("webhook_url")
    if not webhook:
        return False
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook)
        resp = await client.post(webhook, json=payload, follow_redirects=False)
        store.record_delivery(pack["pack_id"], event, resp.status_code, None)
        return True
    except Exception as e:  # noqa: BLE001 — a dead receiver is recorded, not fatal
        store.record_delivery(pack["pack_id"], event, None, type(e).__name__)
        log.warning("fleet check delivery failed for %s: %s", pack["pack_id"], e)
        return False


async def run_pack_sweep(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    pack: dict,
) -> dict:
    """One funded sweep for one pack. Reserve-then-run, exactly
    like a recheck pack spend: the credit is taken atomically
    BEFORE the sweep (one credit per sweep, never per route) and
    refunded if our own machinery fails mid-sweep. A sweep whose
    table is all DOWN still delivered — the table is the product.
    Raises PackError for funding refusals (unknown/expired/
    exhausted) without touching anything."""
    pack_id = pack["pack_id"]
    reserved = store.reserve_fleet_check_credit(pack_id)
    try:
        result = await sweep(client, pack["definition"])
    except Exception:
        store.refund_fleet_check_credit(pack_id)
        raise
    prev = store.latest_fleet_check_run(pack_id)
    diff = diff_sweeps(prev["resources"] if prev else None, result["resources"])
    store.save_fleet_check_run(pack_id, result, diff)
    pushed = False
    push_attempted = False
    webhook = pack.get("webhook_url")
    if webhook and diff["changed"]:
        push_attempted = True
        pushed = await _deliver(
            client,
            store,
            pack,
            "fleet_check_diff",
            {
                "event": "fleet_check_diff",
                "pack_id": pack_id,
                "checked_at": result["checked_at"],
                "summary": result["summary"],
                "diff": diff,
            },
        )
    elif webhook and pack.get("heartbeat"):
        push_attempted = True
        pushed = await _deliver(
            client,
            store,
            pack,
            "fleet_check_heartbeat",
            {
                "event": "fleet_check_heartbeat",
                "pack_id": pack_id,
                "checked_at": result["checked_at"],
                "summary": result["summary"],
                "diff": diff,
            },
        )
    return {
        "pack_id": pack_id,
        "status": "swept",
        "checked_at": result["checked_at"],
        "summary": result["summary"],
        "diff": diff,
        "credits_remaining": reserved["credits_remaining"],
        "webhook_push_attempted": push_attempted,
        "webhook_delivered": pushed,
    }


# --- free first sweep (one per wallet) -------------------------------------

_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class FreeSweepError(Exception):
    """A free-sweep refusal with a client-facing shape."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


async def free_sweep(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    wallet: str,
    resources: list[str] | None,
    origins: list[str] | None,
) -> dict:
    """One free Fleet Check sweep per wallet, ever — the first-call
    form of POST /v1/fleet-check. The definition is validated BEFORE
    the claim is taken (a refused definition spends nothing), the
    claim is atomic in the watch store (persistent across restarts),
    and a sweep that fails inside our own machinery releases the
    claim again — the free call is spent only by a delivered table.

    The wallet is self-declared: that is the same trust level as the
    free probe route's per-IP brake, and it is stated here rather
    than dressed up — a determined caller can rotate wallets, at
    the cost of a fresh wallet per sweep, while every honest first
    caller gets exactly one real sweep. Raises FreeSweepError for
    refusals, ValueError for definition refusals (the handler's
    422, raised before any claim)."""
    wallet = (wallet or "").strip()
    if not _WALLET_RE.match(wallet):
        raise FreeSweepError(
            "bad_wallet",
            "wallet must be a 0x-prefixed 40-hex EVM address",
            422,
        )
    definition = await validate_definition(resources, origins)
    if not store.claim_free_fleet_check(wallet):
        raise FreeSweepError(
            "free_call_used",
            "this wallet has already used its free Fleet Check — "
            "further sweeps are POST /v1/fleet-check at $0.10",
            402,
        )
    try:
        result = await sweep(client, definition)
    except Exception:
        store.release_free_fleet_check(wallet)
        raise
    return {
        **result,
        "free": True,
        "wallet": wallet.lower(),
        "note": (
            "First Fleet Check for this wallet, free. Further sweeps: "
            "POST /v1/fleet-check ($0.10) or the $5 pack "
            "(POST /v1/fleet-check/pack, 100 sweeps of one fleet)."
        ),
    }
