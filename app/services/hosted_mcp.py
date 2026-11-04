"""Hosted MCP endpoint — the fleet's tools over streamable HTTP MCP
(Next-20 Item 2, next-80 #51).

The fleet's MCP packages (stall-row-mcp etc.) are stdio-only: a cloud
agent runtime cannot spawn them. This module serves the SAME tool set
over HTTP MCP at POST /mcp on StallRow, so a runtime connects with a
URL instead of a local install. It lives as its own module on purpose
— it can be lifted into a standalone hosted service later without
surgery; app/main.py only mounts the transport endpoint.

Tool catalogue: DERIVED, never hand-maintained. The definitions are
Item 4's function-calling pack (25 tools — every fleet route priced
<= $0.05), generated from the canonical route registry and embedded
here verbatim at app/data/function-tools.json (the stall's existing
committed-data pattern). tools/list serves the pack's Anthropic
definitions re-shaped to MCP (name / description / inputSchema), so
the registry, the schema pack and this MCP surface cannot drift
apart; tests/test_hosted_mcp.py pins the served list to the pack.

Payment leg: this server NEVER holds keys and NEVER pays from a fleet
wallet. tools/call PROXIES the call to the target stall's public
route, forwarding the caller's own credentials exactly as the fleet's
existing middleware accepts them:

  * x402 payment headers:  payment-signature / x-payment
  * Recheck pack:          x-recheck-pack   (or pack_id in arguments)
  * Toolkit pack:          x-toolkit-pack   (or pack_id in arguments)
  * Fleet Pass:            x-fleet-pass     (or fleet_pass_id body on
                                             the routes that take one)

Credentials arrive as headers on the POST /mcp request itself, or
per-call in params._meta (a "headers" object, or the allow-listed
keys directly). If the stall answers 402, the challenge is decoded
and returned to the MCP client as a structured tool error carrying
the accepts (amount, payTo, network, asset) — the client signs with
its own wallet and retries; that IS the integration flow. A call
with no credential at all still proxies and surfaces the 402, so
discovering a tool's price is free. No new account system.

Transport: stateless streamable HTTP (MCP 2025-06-18 / 2025-03-26 /
2024-11-05 negotiated at initialize). JSON-RPC 2.0 POST only; GET and
DELETE answer 405 (no server-initiated SSE stream in stateless mode).
/mcp is free transport — it is NOT a priced x402 resource.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

SERVER_INFO = {
    "name": "verdict-row-hosted-mcp",
    "title": "Verdict Row Fleet — Hosted MCP",
    "version": "1.0.0",
}

INSTRUCTIONS = (
    "Verdict Row fleet tools, hosted. Every tool is a paid x402 route "
    "(USDC on Base, exact scheme, priced per its description). This "
    "server holds no keys and never pays for you: call a tool unpaid "
    "to receive its 402 payment challenge as a structured tool error "
    "(accepts carry amount, payTo, network, asset), sign it with your "
    "own wallet, then retry the same tools/call with the payment in "
    "the payment-signature (or x-payment) header of your POST /mcp "
    "request — or fund the call with your own fleet credentials via "
    "the x-recheck-pack, x-toolkit-pack or x-fleet-pass headers (or "
    "per-call in params._meta). Headers are forwarded to the target "
    "stall exactly as its middleware accepts them."
)

# The credential / payment headers a tools/call forwards upstream —
# exactly the names the fleet's middleware already accepts, nothing
# else (a caller's unrelated headers never leak to a stall).
FORWARD_HEADERS = (
    "payment-signature",
    "x-payment",
    "x-recheck-pack",
    "x-toolkit-pack",
    "x-fleet-pass",
)

_PACK_PATH = Path(__file__).resolve().parent.parent / "data" / "function-tools.json"


def _load_pack() -> dict:
    with open(_PACK_PATH, encoding="utf-8") as f:
        return json.load(f)


PACK = _load_pack()

# Tool definitions derived from the pack: the Anthropic entries carry
# name / description / input_schema verbatim; MCP wants inputSchema.
TOOLS: list[dict] = [
    {
        "name": entry["name"],
        "description": entry["description"],
        "inputSchema": copy.deepcopy(entry["input_schema"]),
    }
    for entry in PACK.get("anthropic") or []
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}

# Routing table, also from the pack: name -> the priced route the
# call proxies to (resource is the target stall's public origin URL,
# so cross-stall tools — RouteVerdict check, Token Verdict verdict /
# pre-trade — proxy exactly like StallRow's own).
ROUTE_BY_NAME: dict[str, dict] = {r["name"]: r for r in PACK.get("routes") or []}


def tools_list() -> list[dict]:
    """The MCP tools/list payload: exactly the pack's tool set."""
    return copy.deepcopy(TOOLS)


def hosted_extension(base_url: str) -> dict:
    """The discovery entry for this endpoint, nested under the
    manifest's extensions.mcp block (additive: the stdio server's
    name / manifest stay at the top level, where the registry
    compiler expects them)."""
    return {
        "title": "Verdict Row Fleet — Hosted MCP (streamable HTTP)",
        "transport": "streamable-http",
        "url": f"{base_url}/mcp",
        "endpoint": f"{base_url}/mcp",
        "protocol_versions": list(PROTOCOL_VERSIONS),
        "tools": len(TOOLS),
        "tool_source": (
            "Item 4 function-calling pack (function-tools.json): every "
            "fleet route priced <= $0.05, generated from the canonical "
            "route registry"
        ),
        "auth": (
            "Bring your own x402 payment (payment-signature / x-payment "
            "header on the /mcp request) or fleet credential "
            "(x-recheck-pack / x-toolkit-pack / x-fleet-pass header, or "
            "params._meta); an unpaid tools/call returns the target "
            "route's 402 challenge as a structured tool error. The "
            "server holds no keys and never pays."
        ),
    }


# --- credential forwarding ---------------------------------------------------


def extract_forward_headers(headers: Any) -> dict[str, str]:
    """Pick the allow-listed credential/payment headers out of a
    header mapping (Starlette Headers, dict, httpx.Headers — anything
    with .get), normalised to the canonical wire names."""
    out: dict[str, str] = {}
    if headers is None:
        return out
    for name in FORWARD_HEADERS:
        try:
            value = headers.get(name)
        except Exception:  # noqa: BLE001 — a non-mapping yields nothing
            return out
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    return out


def extract_meta_headers(params: dict | None) -> dict[str, str]:
    """Per-call credentials from params._meta: either a nested
    "headers" object or the allow-listed keys directly on _meta.
    Only allow-listed names pass, same as the HTTP path."""
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return {}
    out: dict[str, str] = {}
    nested = meta.get("headers")
    if isinstance(nested, dict):
        out.update(extract_forward_headers(_CaseInsensitive(nested)))
    out.update(extract_forward_headers(_CaseInsensitive(meta)))
    return out


class _CaseInsensitive:
    """Minimal case-insensitive .get over a plain dict, so
    extract_forward_headers can read _meta mappings whose keys may
    arrive in any case."""

    def __init__(self, data: dict):
        self._data = {str(k).lower(): v for k, v in data.items()}

    def get(self, name: str):
        return self._data.get(name.lower())


# --- 402 challenge decoding ----------------------------------------------------


def parse_402_challenge(response: httpx.Response) -> dict | None:
    """Decode an x402 challenge from a 402 response: the v2
    PAYMENT-REQUIRED header (base64 JSON) first, then a JSON body
    carrying accepts. None when neither parses."""
    raw = response.headers.get("payment-required")
    if raw:
        try:
            decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
            if isinstance(decoded, dict) and decoded.get("accepts"):
                return decoded
        except Exception:  # noqa: BLE001 — fall through to the body
            pass
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — no JSON body, no challenge
        return None
    if isinstance(body, dict) and body.get("accepts"):
        return body
    return None


def _base_accept(accepts: list) -> dict:
    for a in accepts:
        if isinstance(a, dict) and a.get("network") == "eip155:8453":
            return a
    for a in accepts:
        if isinstance(a, dict):
            return a
    return {}


def payment_required_structured(name: str, route: dict, response: httpx.Response) -> dict:
    """The structured tool error for an unpaid call: the decoded
    challenge's accepts verbatim, plus the headline accept's amount /
    payTo / network / asset at the top level so a client can sign
    without digging. When the upstream challenge cannot be parsed,
    the accepts are synthesised from the pack's own registry entry
    for the route (same price/payTo the registry pins) and the fact
    is named, never hidden."""
    challenge = parse_402_challenge(response)
    synthesised = False
    if challenge is None:
        payment = PACK.get("payment") or {}
        challenge = {
            "x402Version": 2,
            "resource": {"url": route["resource"], "method": route.get("method", "POST")},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": payment.get("network"),
                    "amount": str(route.get("amount_units_base")),
                    "asset": payment.get("asset"),
                    "payTo": payment.get("pay_to"),
                    "maxTimeoutSeconds": 300,
                }
            ],
        }
        synthesised = True
    accepts = challenge.get("accepts") or []
    head = _base_accept(accepts)
    return {
        "error": "payment_required",
        "code": "payment_required",
        "status": 402,
        "tool": name,
        "resource": route["resource"],
        "x402Version": challenge.get("x402Version", 2),
        "accepts": accepts,
        "amount": head.get("amount"),
        "payTo": head.get("payTo"),
        "network": head.get("network"),
        "asset": head.get("asset"),
        "challenge_synthesised_from_registry": synthesised,
        "hint": (
            "Pay with your own wallet: sign this x402 challenge (exact "
            "scheme, USDC on Base) and retry the same tools/call with "
            "the payment in the payment-signature (or x-payment) "
            "header of the POST /mcp request, or present a fleet "
            "credential (x-recheck-pack / x-toolkit-pack / x-fleet-pass "
            "header, or params._meta). This server holds no keys and "
            "never pays from a fleet wallet."
        ),
    }


# --- tools/call proxy ------------------------------------------------------------


def _tool_result(structured: dict, *, is_error: bool) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "structuredContent": structured,
        "isError": is_error,
    }


async def execute_tool_call(
    name: str,
    arguments: dict | None,
    forward_headers: dict[str, str] | None = None,
    *,
    http_client: httpx.AsyncClient,
) -> dict:
    """Proxy one tools/call to the target stall route and shape the
    upstream answer as an MCP tool result. Caller must have checked
    `name` against TOOL_BY_NAME. Success bodies pass through as
    structuredContent; a 402 becomes the structured payment_required
    error; any other failure is an honest tool error, never a
    fabricated delivery."""
    route = ROUTE_BY_NAME[name]
    headers = {"content-type": "application/json"}
    headers.update(forward_headers or {})
    try:
        # Per-request timeout override: the shared fetch client is
        # tuned for 15 s page fetches, but a proxied tool call waits
        # on a whole upstream service (bundles compose several legs)
        # — a proxy-side timeout after the upstream settled would
        # strand a paid delivery.
        response = await http_client.post(
            route["resource"], json=arguments or {}, headers=headers, timeout=120.0
        )
    except Exception as e:  # noqa: BLE001 — transport failure is a tool error
        return _tool_result(
            {
                "error": "upstream_unreachable",
                "code": "upstream_unreachable",
                "tool": name,
                "resource": route["resource"],
                "detail": f"{type(e).__name__}: {e}",
            },
            is_error=True,
        )
    if response.status_code == 402:
        return _tool_result(
            payment_required_structured(name, route, response), is_error=True
        )
    try:
        body: Any = response.json()
    except Exception:  # noqa: BLE001 — non-JSON upstream body
        body = None
    if 200 <= response.status_code < 300:
        structured = body if isinstance(body, dict) else {"result": body if body is not None else response.text}
        return _tool_result(structured, is_error=False)
    detail: Any = body if body is not None else response.text[:2000]
    return _tool_result(
        {
            "error": "upstream_error",
            "code": "upstream_error",
            "status": response.status_code,
            "tool": name,
            "resource": route["resource"],
            "detail": detail,
        },
        is_error=True,
    )


# --- JSON-RPC dispatch -------------------------------------------------------------


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def handle_single(
    payload: Any,
    forward_headers: dict[str, str] | None = None,
    *,
    http_client: httpx.AsyncClient,
) -> tuple[int, dict | None]:
    """One JSON-RPC message -> (http_status, response_body | None).
    Notifications answer (202, None) per streamable-HTTP: accepted,
    no body."""
    if not isinstance(payload, dict):
        return 400, _error(None, -32600, "invalid request: expected a JSON-RPC object")
    method = payload.get("method")
    is_notification = "id" not in payload
    if is_notification:
        # notifications/initialized and every other notification:
        # accepted, never answered.
        return 202, None
    request_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return 200, _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO),
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return 200, _result(request_id, {})
    if method == "tools/list":
        return 200, _result(request_id, {"tools": tools_list()})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return 200, _error(request_id, -32602, "invalid params: tools/call requires params.name")
        if name not in TOOL_BY_NAME:
            return 200, _error(request_id, -32602, f"unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return 200, _error(request_id, -32602, "invalid params: tools/call arguments must be an object")
        # Per-call _meta credentials first; the transport request's
        # own headers win on a clash (they are the channel the client
        # deliberately authenticated).
        merged = extract_meta_headers(params)
        merged.update(forward_headers or {})
        result = await execute_tool_call(name, arguments, merged, http_client=http_client)
        return 200, _result(request_id, result)
    return 200, _error(request_id, -32601, f"method not found: {method}")


async def handle_request(
    payload: Any,
    forward_headers: dict[str, str] | None = None,
    *,
    http_client: httpx.AsyncClient,
) -> tuple[int, Any]:
    """The transport entry point: a single message, or a JSON-RPC
    batch (answered as an array; an all-notification batch is a bare
    202)."""
    if isinstance(payload, list):
        if not payload:
            return 400, _error(None, -32600, "invalid request: empty batch")
        responses = []
        for item in payload:
            _status, body = await handle_single(item, forward_headers, http_client=http_client)
            if body is not None:
                responses.append(body)
        if not responses:
            return 202, None
        return 200, responses
    return await handle_single(payload, forward_headers, http_client=http_client)
