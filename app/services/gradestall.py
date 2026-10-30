"""Grade My Stall: POST /v1/grade-my-stall ($0.005).

A seller-side report card for one x402 resource, assembled from
live reads only:

  answers_402       — an unpaid probe of the resource (the Recheck
                      probe machinery): does it answer 402 with a
                      parseable x402 v2 challenge?
  description       — the challenge's resource description, checked
                      against the 500-character catalogue limit.
  cdp_listing       — is the resource in the newest Verdict Index
                      snapshot (i.e. in the CDP catalog as of that
                      snapshot)?
  settled           — Settled's free MCP `settled_check`: listing
                      status, payability verdict, delivery record.
  x402scan          — is the resource declared in the origin's own
                      /.well-known/x402 or OpenAPI document, and
                      does the declaration carry an ownership proof
                      (the thing x402scan verifies)?
  payai             — no public PayAI catalogue read is available
                      to this service, so this check always reports
                      "unverified" rather than guessing.
  payto_consistency — the challenge's payTo vs the Index entry's
                      payTo vs the pay_to the seller supplied, when
                      more than one is known.

Every check reports ok / issue / not_found / unverified with its
evidence. A check that could not be performed says "unverified" —
it is never inferred from another check. An unreachable resource
is a result, not an error: the report is the deliverable.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from ..rpc import UpstreamError
from . import recheck as recheck_svc
from .indexfeed import IndexStore

SETTLED_MCP_URL = "https://settled.tools/mcp/free"
_MCP_PROTOCOL = "2025-06-18"
_DESCRIPTION_LIMIT = 500
_PROOF_KEYS = ("proof", "signature", "ownership_proof", "ownershipProof")

LIMITS = (
    "Live reads at report time, plus the newest Verdict Index "
    "snapshot for the CDP listing check (snapshots refresh about "
    "every 6 hours, so a just-listed resource may not appear yet). "
    "Checks marked unverified could not be performed and were not "
    "guessed. This is an automated report card, not an endorsement."
)


def _norm(url: str) -> str:
    return url.strip().rstrip("/")


async def _settled_check(client: httpx.AsyncClient, url: str) -> dict:
    """One settled_check over Settled's free MCP. Raises on any
    transport/protocol failure — the caller marks it unverified."""
    session: str | None = None
    seq = 0

    async def call(method: str, params: dict, *, notification: bool = False):
        nonlocal session, seq
        seq += 1
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = seq
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MCP_PROTOCOL,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        resp = await client.post(
            SETTLED_MCP_URL, json=body, headers=headers, timeout=8.0
        )
        new_session = resp.headers.get("Mcp-Session-Id")
        if new_session:
            session = new_session
        if notification:
            if resp.status_code not in (200, 202, 204):
                raise UpstreamError(f"settled MCP notify HTTP {resp.status_code}")
            return None
        if resp.status_code != 200:
            raise UpstreamError(f"settled MCP {method} HTTP {resp.status_code}")
        return _rpc_result(resp, seq)

    await call(
        "initialize",
        {
            "protocolVersion": _MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "stall-row-grade-my-stall", "version": "1"},
        },
    )
    await call("notifications/initialized", {}, notification=True)
    result = await call(
        "tools/call", {"name": "settled_check", "arguments": {"url": url}}
    )
    if not result or result.get("isError"):
        raise UpstreamError("settled_check returned an error result")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text") or "")
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise UpstreamError("settled_check result carried no payload")


def _rpc_result(resp: httpx.Response, request_id: int) -> dict | None:
    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if content_type == "text/event-stream":
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == request_id:
                    return msg.get("result")
        raise UpstreamError("settled MCP stream ended before the result")
    msg = resp.json()
    if isinstance(msg, dict):
        return msg.get("result")
    raise UpstreamError("settled MCP returned a non-object message")


async def _fetch_doc(client: httpx.AsyncClient, url: str) -> tuple[int | None, dict | None]:
    """GET a JSON document under the probe's SSRF discipline.
    Returns (status, parsed_json_or_None); (None, None) on failure."""
    try:
        status, _headers, body, _redirs = await recheck_svc._get_once(client, url, "GET")
    except Exception:  # noqa: BLE001 — unreachable doc = data, not a crash
        return None, None
    parsed = None
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                parsed = data
        except ValueError:
            parsed = None
    return status, parsed


def _find_declaration(doc: dict | None, resource_url: str) -> dict | None:
    """The entry declaring `resource_url` in a well-known/OpenAPI doc."""
    if not doc:
        return None
    target = _norm(resource_url)
    for entry in doc.get("resources") or []:
        if isinstance(entry, dict):
            for key in ("url", "resource", "href"):
                if isinstance(entry.get(key), str) and _norm(entry[key]) == target:
                    return entry
    # OpenAPI shape: paths keys are the resource paths.
    parts = urlsplit(resource_url)
    paths = doc.get("paths")
    if isinstance(paths, dict) and parts.path in paths:
        return {"url": resource_url, "openapi_path": parts.path}
    return None


def _has_proof(entry: dict | None, doc: dict | None) -> bool:
    for source in (entry, doc):
        if isinstance(source, dict):
            for key in _PROOF_KEYS:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return True
    return False


async def grade_stall(
    client: httpx.AsyncClient,
    index_store: IndexStore,
    *,
    resource_url: str,
    pay_to: str | None = None,
) -> dict:
    resource_url = resource_url.strip()
    checks: dict[str, dict] = {}

    # --- live probe ---------------------------------------------------
    challenge = None
    probe_status = None
    try:
        status, headers, body, _meta = await recheck_svc._probe(client, resource_url)
        probe_status = status
        if status == 402:
            challenge = recheck_svc._parse_challenge(headers, body)
    except UpstreamError as e:
        checks["answers_402"] = {
            "status": "issue",
            "detail": f"resource unreachable: {e}",
        }
    if "answers_402" not in checks:
        if probe_status == 402 and challenge is not None:
            checks["answers_402"] = {
                "status": "ok",
                "detail": "answers 402 with a parseable x402 challenge",
                "http_status": 402,
            }
        elif probe_status == 402:
            checks["answers_402"] = {
                "status": "issue",
                "detail": "answers 402 but the challenge does not parse as x402 v2",
                "http_status": 402,
            }
        else:
            checks["answers_402"] = {
                "status": "issue",
                "detail": f"answers HTTP {probe_status}, not a 402 payment challenge",
                "http_status": probe_status,
            }

    accept = (challenge or {}).get("accepts", [{}])[0] if challenge else {}
    challenge_pay_to = accept.get("payTo") if isinstance(accept, dict) else None

    # --- description ----------------------------------------------------
    if challenge is None:
        checks["description"] = {
            "status": "unverified",
            "detail": "no parseable challenge, so no description to measure",
        }
    else:
        description = ((challenge.get("resource") or {}).get("description")) or ""
        if not description:
            checks["description"] = {
                "status": "issue",
                "detail": "the challenge carries no resource description",
                "length": 0,
                "limit": _DESCRIPTION_LIMIT,
            }
        elif len(description) > _DESCRIPTION_LIMIT:
            checks["description"] = {
                "status": "issue",
                "detail": (
                    f"description is {len(description)} characters, over the "
                    f"{_DESCRIPTION_LIMIT}-character catalogue limit"
                ),
                "length": len(description),
                "limit": _DESCRIPTION_LIMIT,
            }
        else:
            checks["description"] = {
                "status": "ok",
                "detail": f"description is {len(description)} of {_DESCRIPTION_LIMIT} characters",
                "length": len(description),
                "limit": _DESCRIPTION_LIMIT,
            }

    # --- CDP listing (newest Index snapshot) -----------------------------
    snaps = index_store.snapshots()
    index_entry = None
    if not snaps:
        checks["cdp_listing"] = {
            "status": "unverified",
            "detail": "no Index snapshot held yet",
        }
    else:
        newest = snaps[-1]
        entries = index_store.entries(newest["id"])
        index_entry = entries.get(resource_url)
        if index_entry is None:
            for key, entry in entries.items():
                if _norm(key) == _norm(resource_url):
                    index_entry = entry
                    break
        if index_entry is not None:
            checks["cdp_listing"] = {
                "status": "ok",
                "detail": "listed in the CDP catalog snapshot",
                "snapshot_at": newest["captured_at"],
                "amount": index_entry.get("amount"),
                "pay_to": index_entry.get("payTo"),
                "calls30d": index_entry.get("calls30d"),
            }
        else:
            checks["cdp_listing"] = {
                "status": "not_found",
                "detail": "not in the newest CDP catalog snapshot",
                "snapshot_at": newest["captured_at"],
            }

    # --- Settled ----------------------------------------------------------
    try:
        payload = await _settled_check(client, resource_url)
        payability = (payload.get("payability") or {}).get("verdict")
        checks["settled"] = {
            "status": "ok" if payload.get("status") not in (None, "unknown") or payability else "not_found",
            "detail": (
                f"Settled status {payload.get('status')!r}, payability "
                f"{payability!r}, delivery_verified "
                f"{payload.get('delivery_verified')!r}"
            ),
            "settled_status": payload.get("status"),
            "payability": payability,
            "delivery_verified": payload.get("delivery_verified"),
            "quality_total": (payload.get("quality") or {}).get("total"),
        }
    except Exception as e:  # noqa: BLE001 — a failed lookup is reported, not hidden
        checks["settled"] = {
            "status": "unverified",
            "detail": f"Settled lookup failed: {type(e).__name__}: {e}"[:200],
        }

    # --- x402scan ownership (origin's own declaration) ---------------------
    parts = urlsplit(resource_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    wk_status, wk_doc = await _fetch_doc(client, f"{origin}/.well-known/x402")
    oa_status, oa_doc = await _fetch_doc(client, f"{origin}/openapi.json")
    entry = _find_declaration(wk_doc, resource_url) or _find_declaration(
        oa_doc, resource_url
    )
    proof = _has_proof(entry, wk_doc) or _has_proof(entry, oa_doc)
    if wk_status is None and oa_status is None:
        checks["x402scan"] = {
            "status": "unverified",
            "detail": "neither /.well-known/x402 nor /openapi.json could be fetched",
        }
    elif entry is not None and proof:
        checks["x402scan"] = {
            "status": "ok",
            "detail": "resource declared by the origin with an ownership proof published",
            "well_known_status": wk_status,
            "openapi_status": oa_status,
        }
    elif entry is not None:
        checks["x402scan"] = {
            "status": "issue",
            "detail": (
                "resource is declared by the origin but no ownership proof "
                "is published with the declaration"
            ),
            "well_known_status": wk_status,
            "openapi_status": oa_status,
        }
    else:
        checks["x402scan"] = {
            "status": "not_found",
            "detail": "resource not declared in the origin's /.well-known/x402 or OpenAPI document",
            "well_known_status": wk_status,
            "openapi_status": oa_status,
        }

    # --- PayAI ---------------------------------------------------------------
    checks["payai"] = {
        "status": "unverified",
        "detail": (
            "PayAI has no public catalogue read available to this "
            "service; listing presence was not checked"
        ),
    }

    # --- payTo consistency -----------------------------------------------------
    sources = {}
    if challenge_pay_to:
        sources["challenge"] = challenge_pay_to
    if index_entry is not None and index_entry.get("payTo"):
        sources["index"] = index_entry["payTo"]
    if pay_to:
        sources["supplied"] = pay_to
    if len(sources) < 2:
        checks["payto_consistency"] = {
            "status": "unverified",
            "detail": "fewer than two payTo sources to compare",
            "sources": sources,
        }
    elif len({v.lower() for v in sources.values()}) == 1:
        checks["payto_consistency"] = {
            "status": "ok",
            "detail": "every known source names the same payTo",
            "sources": sources,
        }
    else:
        checks["payto_consistency"] = {
            "status": "issue",
            "detail": "payTo sources disagree",
            "sources": sources,
        }

    counts = {"ok": 0, "issue": 0, "not_found": 0, "unverified": 0}
    for check in checks.values():
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    return {
        "resource": resource_url,
        "pay_to_supplied": pay_to,
        "checks": checks,
        "summary": counts,
        "overall": "issues_found" if counts["issue"] else "no_issues_found",
        "limits": LIMITS,
    }
