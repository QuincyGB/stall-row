"""Agent-Card Verifier: POST /v1/agent-card-verify ($0.005).

Input: an ERC-8004 agent id (resolved on Base through the
registry's agentURI, tokenURI as fallback) or a direct agent.json
URL. The card is fetched under the fleet's SSRF discipline and
checked against live reality with the fleet's 402-probe machinery:

  card_parses        — the card fetched and parsed as a JSON object.
  endpoints_answer   — every endpoint URL the card declares (capped
                       at 8) is probed unpaid; 200 or 402 both count
                       as answering (402 is the correct answer for a
                       paid x402 service).
  payto_matches      — when the card declares a wallet/payTo AND a
                       probed endpoint issued a challenge, the two
                       are compared. A card that declares no wallet
                       reports "not declared — not checked", never a
                       guessed verdict.
  prices_match       — when an endpoint declaration carries a price
                       AND its live challenge names an amount, they
                       are compared; otherwise not checked, stated.

An unreachable card (registry resolution failure, empty URI,
transport failure) raises UpstreamError and the payment does not
settle — there is nothing to verify. A card that fetches but does
not parse IS the finding, and is delivered as such.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx
from eth_hash.auto import keccak

from ..rpc import Rpc, UpstreamError
from . import recheck as recheck_svc

REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
MAX_ENDPOINTS = 8
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

LIMITS = (
    "Live reads at report time: registry resolution on Base, the "
    "card fetch, and unpaid probes of the endpoints the card "
    "declares (at most 8). Fields the card does not declare are "
    "reported as not declared, never inferred. This is an "
    "automated consistency check, not an endorsement of the agent."
)


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()[:8]


def _decode_abi_string(result: str) -> str | None:
    """Decode an ABI-encoded string return, or None if malformed."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    raw = bytes.fromhex(result[2:])
    if len(raw) < 64:
        return None
    offset = int.from_bytes(raw[:32], "big")
    if offset + 32 > len(raw):
        return None
    length = int.from_bytes(raw[offset : offset + 32], "big")
    data = raw[offset + 32 : offset + 32 + length]
    if len(data) < length:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def resolve_agent_uri(rpc: Rpc, agent_id: int) -> str:
    """agentURI(agentId) on the ERC-8004 registry, tokenURI as the
    fallback (the registry Ren's own agent 97809 is registered on
    answers tokenURI). Raises UpstreamError when neither resolves."""
    arg = agent_id.to_bytes(32, "big").hex()
    for signature in ("agentURI(uint256)", "tokenURI(uint256)"):
        try:
            result = await rpc.eth_call(REGISTRY, _selector(signature) + arg)
        except UpstreamError:
            continue
        uri = _decode_abi_string(result)
        if uri:
            return uri
    raise UpstreamError(
        f"agent {agent_id} did not resolve to a card URI on the ERC-8004 registry"
    )


def _iter_strings(node, depth=0):
    if depth > 4:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value, depth + 1)


def declared_endpoints(card: dict) -> list[dict]:
    """Endpoint declarations: [{url, price}] from the card's known
    shapes — top-level url/serviceEndpoint, endpoints[] entries
    (strings or objects with url/endpoint + optional price/amount),
    and services[] entries in the ERC-8004 registration shape."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(url, price=None):
        if not isinstance(url, str):
            return
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return
        if url in seen or len(out) >= MAX_ENDPOINTS:
            return
        seen.add(url)
        out.append({"url": url, "declared_price": price})

    for key in ("url", "serviceEndpoint", "endpoint"):
        add(card.get(key))
    for key in ("endpoints", "services"):
        entries = card.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, str):
                    add(entry)
                elif isinstance(entry, dict):
                    add(
                        entry.get("url") or entry.get("endpoint"),
                        entry.get("price") or entry.get("amount"),
                    )
    return out


def declared_wallets(card: dict) -> list[str]:
    """Wallet/payTo addresses the card declares, from known keys at
    any reasonable nesting (wallet, payTo, paymentAddress, owner)."""
    found: list[str] = []

    def visit(node, depth=0):
        if depth > 4 or not isinstance(node, dict):
            return
        for key, value in node.items():
            if (
                isinstance(value, str)
                and key.lower() in ("wallet", "payto", "paymentaddress", "owner", "address")
                and _ADDRESS_RE.match(value.strip())
            ):
                found.append(value.strip())
            elif isinstance(value, dict):
                visit(value, depth + 1)

    visit(card)
    # Case-insensitive dedupe, first spelling wins.
    seen: set[str] = set()
    out = []
    for addr in found:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


async def verify_agent_card(
    client: httpx.AsyncClient,
    rpc: Rpc,
    *,
    agent_id: int | None = None,
    agent_url: str | None = None,
) -> dict:
    if agent_id is not None:
        card_url = await resolve_agent_uri(rpc, agent_id)
        source = "erc8004_registry"
    elif agent_url:
        card_url = agent_url.strip()
        source = "direct_url"
    else:
        raise ValueError("name an agent_id or an agent_url")

    # Fetch the card under the probe's SSRF discipline.
    try:
        status, _headers, body, _redirs = await recheck_svc._get_once(client, card_url, "GET")
    except Exception as e:  # noqa: BLE001 — unreachable card = unsettled error
        raise UpstreamError(f"agent card unreachable: {e}") from e
    if status != 200 or not body:
        raise UpstreamError(f"agent card fetch answered HTTP {status}")

    checks: dict[str, dict] = {}
    try:
        card = json.loads(body)
        if not isinstance(card, dict):
            raise ValueError("card is not a JSON object")
    except ValueError as e:
        return {
            "agent_id": agent_id,
            "card_url": card_url,
            "source": source,
            "checks": {
                "card_parses": {
                    "status": "issue",
                    "detail": f"card fetched but does not parse as a JSON object: {e}",
                }
            },
            "summary": {"ok": 0, "issue": 1, "not_checked": 0},
            "overall": "issues_found",
            "limits": LIMITS,
        }
    checks["card_parses"] = {"status": "ok", "detail": "card fetched and parsed"}
    card_name = card.get("name")

    endpoints = declared_endpoints(card)
    wallets = declared_wallets(card)

    endpoint_results = []
    any_challenge_payto = []
    price_verdicts = []
    for entry in endpoints:
        url = entry["url"]
        try:
            pstatus, pheaders, pbody, _meta = await recheck_svc._probe(client, url)
            challenge = (
                recheck_svc._parse_challenge(pheaders, pbody)
                if pstatus == 402
                else None
            )
            answered = True
        except Exception as e:  # noqa: BLE001 — a dead endpoint is a finding
            endpoint_results.append(
                {"url": url, "answered": False, "error": f"{type(e).__name__}: {e}"[:160]}
            )
            continue
        result = {"url": url, "answered": answered, "http_status": pstatus}
        if challenge is not None:
            accept = (challenge.get("accepts") or [{}])[0]
            result["challenge_pay_to"] = accept.get("payTo")
            result["challenge_amount"] = accept.get("amount")
            if accept.get("payTo"):
                any_challenge_payto.append(accept.get("payTo"))
            declared_price = entry.get("declared_price")
            if declared_price is not None and accept.get("amount") is not None:
                match = str(declared_price) == str(accept.get("amount"))
                price_verdicts.append(
                    {
                        "url": url,
                        "declared": str(declared_price),
                        "live": str(accept.get("amount")),
                        "match": match,
                    }
                )
                result["price_match"] = match
        endpoint_results.append(result)

    if not endpoints:
        checks["endpoints_answer"] = {
            "status": "issue",
            "detail": "the card declares no endpoint URLs to probe",
            "endpoints": [],
        }
    else:
        dead = [r for r in endpoint_results if not r["answered"]]
        checks["endpoints_answer"] = {
            "status": "ok" if not dead else "issue",
            "detail": (
                f"{len(endpoint_results) - len(dead)} of {len(endpoint_results)} "
                "declared endpoints answered an unpaid probe"
            ),
            "endpoints": endpoint_results,
        }

    if not wallets:
        checks["payto_matches"] = {
            "status": "not_checked",
            "detail": "the card declares no wallet/payTo — not checked",
            "declared_wallets": [],
        }
    elif not any_challenge_payto:
        checks["payto_matches"] = {
            "status": "not_checked",
            "detail": "no probed endpoint issued a payment challenge — not checked",
            "declared_wallets": wallets,
        }
    else:
        wallet_set = {w.lower() for w in wallets}
        mismatches = [p for p in any_challenge_payto if p.lower() not in wallet_set]
        checks["payto_matches"] = {
            "status": "ok" if not mismatches else "issue",
            "detail": (
                "every challenge payTo matches a card-declared wallet"
                if not mismatches
                else "a challenge payTo is not among the card-declared wallets"
            ),
            "declared_wallets": wallets,
            "challenge_pay_tos": any_challenge_payto,
        }

    if not price_verdicts:
        checks["prices_match"] = {
            "status": "not_checked",
            "detail": (
                "no endpoint declaration carries a price alongside a live "
                "challenge amount — not checked"
            ),
        }
    else:
        bad = [v for v in price_verdicts if not v["match"]]
        checks["prices_match"] = {
            "status": "ok" if not bad else "issue",
            "detail": (
                "declared prices match the live challenge amounts"
                if not bad
                else "a declared price differs from the live challenge amount"
            ),
            "comparisons": price_verdicts,
        }

    counts = {"ok": 0, "issue": 0, "not_checked": 0}
    for check in checks.values():
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    return {
        "agent_id": agent_id,
        "card_url": card_url,
        "source": source,
        "card_name": card_name if isinstance(card_name, str) else None,
        "checks": checks,
        "summary": counts,
        "overall": "issues_found" if counts["issue"] else "no_issues_found",
        "limits": LIMITS,
    }
