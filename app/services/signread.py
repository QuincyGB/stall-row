"""Sign Reader: POST /v1/sign-read ($0.01) — read one x402
endpoint's price sign, exactly, before anyone pays it.

Inspectors kept hitting the same two failure classes in the wild:
sellers whose 402 challenge no standard SDK can parse (the
PAYMENT-REQUIRED / network-id break PulseFeed's public corrections
and the losbeto SDK bug are both instances of), and catalogue
entries whose declared terms do not match the live challenge. The
recheck stall reads the sign to fingerprint it; this route reads
the sign to JUDGE it:

  * one UNPAID probe (the recheck probe machinery: SSRF check on
    the target and every redirect hop, capped body, POST-first with
    the GET fallback — and, when the caller declares a method, a
    probe on that method too). No code path here can pay, sign, or
    settle anything.
  * BOTH wire formats parsed: x402 v2 (base64 `payment-required`
    header, falling back to a same-shaped JSON body) and x402 v1
    (JSON body, per-accept `maxAmountRequired` / resource /
    description). Every accept is normalized: scheme, network,
    asset, amount in atomic units AND exact decimal (Decimal, never
    float — and only when the asset's decimals are known; an asset
    whose decimals this service does not know gets amount_decimal
    null and the gap is named, never guessed), payTo, timeout,
    resource.
  * SDK compatibility, honestly based: the Python verdict runs the
    installed x402 SDK's own pydantic models (PaymentRequired /
    PaymentRequiredV1) plus the wire rules the SDK enforces when it
    resolves a payment (CAIP-2 network id, digit-string amount,
    payTo/asset valid for the network family). The TypeScript
    verdict applies the same wire rules and says so — there is no
    TS runtime in this service, so it is labelled structural.
  * Declared-vs-live: the caller may pass the catalogue entry they
    hold (method / amount / payTo / network); every disagreement
    with the live sign is named in `mismatches`.

An unreachable or non-402 target is a RESULT, not an error — the
report is the deliverable (the grade-my-stall precedent). Only a
target the SSRF guard refuses is a 422, raised before any probe.
Nothing is recorded and nothing is charged back here: the paywall's
>= 400 rule already guarantees a failed handler never settles.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..rpc import UpstreamError
from . import fetch as fetch_svc
from . import recheck as recheck_svc

log = logging.getLogger("stall-row.signread")

PRICE_UNITS = 10_000  # $0.01 — mirrors config.PRICE_UNITS (price card 2026-10-30)

# Asset decimals this service KNOWS. Anything else: the decimal
# amount is reported as unknown rather than computed on a guess.
_KNOWN_DECIMALS = {
    # USDC on Base (Circle) and USDC on Solana (Circle SPL mint).
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v": 6,
}

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CAIP2 = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

LIMITS = (
    "One unpaid probe per method tried, at call time; the sign can "
    "change the moment after. The Python SDK verdict runs the "
    "installed x402 SDK's own challenge models plus the wire rules "
    "its payment path enforces; the TypeScript verdict is a "
    "structural application of the same wire rules (no TS runtime "
    "runs here) and is labelled as such. Decimal amounts are exact "
    "only for assets whose decimals this service knows; any other "
    "asset's decimal is reported unknown, never guessed. Declared "
    "terms are compared only when the caller supplies them."
)


def _now_iso() -> str:
    return recheck_svc._now_iso()


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _decimal_of(amount_atomic: str | None, asset: str | None) -> str | None:
    """Exact decimal string for an atomic digit string, or None when
    the amount is not a digit string or the asset's decimals are
    unknown here. Decimal arithmetic only — never float."""
    if amount_atomic is None or not str(amount_atomic).isdigit():
        return None
    if asset is None:
        return None
    decimals = _KNOWN_DECIMALS.get(str(asset).lower())
    if decimals is None:
        return None
    value = Decimal(int(amount_atomic)) / (Decimal(10) ** decimals)
    return format(value, "f")


# --- challenge extraction -----------------------------------------------------


def _decode_header(headers: httpx.Headers) -> tuple[dict | None, str | None]:
    """(challenge, error) from the v2 PAYMENT-REQUIRED header."""
    raw = headers.get("payment-required")
    if not raw:
        return None, None
    try:
        data = json.loads(base64.b64decode(raw, validate=True))
    except ValueError:
        return None, "payment-required header is not base64 JSON"
    if not isinstance(data, dict):
        return None, "payment-required header JSON is not an object"
    return data, None


def _decode_body(body: bytes | None) -> dict | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def extract_challenge(
    headers: httpx.Headers, body: bytes | None
) -> tuple[dict | None, str, list[str]]:
    """(challenge, source, problems). Source is 'header' or 'body'.
    A challenge counts only when it carries a non-empty accepts
    list; anything else is named in problems, not silently dropped."""
    problems: list[str] = []
    header_data, header_error = _decode_header(headers)
    if header_error:
        problems.append(header_error)
    if header_data is not None:
        if header_data.get("accepts"):
            return header_data, "header", problems
        problems.append("payment-required header carries no accepts")
    body_data = _decode_body(body)
    if body_data is not None and body_data.get("accepts"):
        return body_data, "body", problems
    if body_data is not None and header_data is None:
        problems.append("response body is JSON but carries no accepts")
    return None, "none", problems


def challenge_version(challenge: dict) -> int | None:
    version = challenge.get("x402Version")
    if isinstance(version, int) and version in (1, 2):
        return version
    # A v1 body is recognizable by its per-accept amount field even
    # when the version field itself is missing or malformed.
    accepts = challenge.get("accepts") or []
    if accepts and isinstance(accepts[0], dict) and "maxAmountRequired" in accepts[0]:
        return 1
    return None


# --- normalization --------------------------------------------------------------


def normalize_accept(accept: dict, challenge: dict, version: int | None) -> dict:
    """One accept, normalized across v1/v2 shapes. Missing fields
    are None — reported, never defaulted."""
    if version == 1:
        amount = accept.get("maxAmountRequired")
        resource = accept.get("resource")
        description = accept.get("description")
    else:
        amount = accept.get("amount")
        resource = (challenge.get("resource") or {}).get("url")
        description = (challenge.get("resource") or {}).get("description")
    amount_atomic = str(amount) if amount is not None else None
    asset = accept.get("asset")
    return {
        "scheme": accept.get("scheme"),
        "network": accept.get("network"),
        "asset": asset,
        "amount_atomic": amount_atomic,
        "amount_decimal": _decimal_of(amount_atomic, asset),
        "payTo": accept.get("payTo"),
        "max_timeout_seconds": accept.get("maxTimeoutSeconds"),
        "resource": resource,
        "description": description,
    }


def missing_fields(accept: dict, version: int | None) -> list[str]:
    required = ["scheme", "network", "payTo"]
    required.append("maxAmountRequired" if version == 1 else "amount")
    return [f for f in required if accept.get(f) in (None, "")]


# --- SDK compatibility ------------------------------------------------------------


def _wire_failures(normalized: dict, version: int | None) -> list[str]:
    """The wire rules an x402 client enforces when it resolves a
    payment (beyond JSON shape): these are the rules whose violation
    breaks real SDK payment paths on challenges that merely parse."""
    failures: list[str] = []
    network = normalized.get("network")
    if version == 2:
        if not isinstance(network, str) or not _CAIP2.match(network):
            failures.append(
                f"network {network!r} is not a CAIP-2 id — v2 clients "
                "resolve schemes by CAIP-2 and cannot route it"
            )
    family = None
    if isinstance(network, str):
        if network.startswith("eip155:") or network in ("base", "base-sepolia"):
            family = "evm"
        elif network.startswith("solana:") or network == "solana":
            family = "svm"
    amount = normalized.get("amount_atomic")
    if amount is not None and not amount.isdigit():
        failures.append(f"amount {amount!r} is not an atomic digit string")
    for field in ("payTo", "asset"):
        value = normalized.get(field)
        if value is None:
            continue
        if family == "evm" and not _EVM_ADDRESS.match(str(value)):
            failures.append(f"{field} {value!r} is not an EVM address")
        elif family == "svm" and not _B58.match(str(value)):
            failures.append(f"{field} {value!r} is not a Solana address")
    return failures


def sdk_compat(challenge: dict, version: int | None, normalized: list[dict]) -> dict:
    """Per-SDK verdicts over the FIRST accept (the one clients act
    on) plus challenge-level shape. Python runs the installed SDK's
    own models; TypeScript applies the same wire rules."""
    from x402.http.utils import PaymentRequired, PaymentRequiredV1

    model = PaymentRequiredV1 if version == 1 else PaymentRequired
    py_failures: list[str] = []
    try:
        model.model_validate(challenge)
        py_parses = True
    except ValidationError as e:
        py_parses = False
        for err in e.errors()[:5]:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            py_failures.append(f"{loc}: {err.get('msg', 'invalid')}")
    wire: list[str] = []
    if normalized:
        wire = _wire_failures(normalized[0], version)
    if version is None:
        wire.append("x402Version missing or unrecognized")
    python_ok = py_parses and not wire
    ts_failures = list(wire)
    if not py_parses:
        ts_failures.append("challenge shape fails the reference schema")
    return {
        "python": {
            "parses": python_ok,
            "basis": "installed x402 SDK challenge model + payment-path wire rules",
            "failures": py_failures + wire if not python_ok else [],
        },
        "typescript": {
            "parses": not ts_failures,
            "basis": "structural — the same wire rules the TS SDK validates; no TS runtime runs in this service",
            "failures": ts_failures,
        },
    }


# --- the read ---------------------------------------------------------------------


async def _probe_method(client: httpx.AsyncClient, url: str, method: str):
    """One unpaid probe on one method via the recheck probe engine
    (SSRF-checked hops, capped body). Any HTTP status is data here;
    only transport failure raises."""
    return await recheck_svc._get_once(client, url, method)


async def sign_read(
    client: httpx.AsyncClient,
    url: str,
    declared: dict | None = None,
) -> dict:
    """Read one endpoint's 402 sign. `declared` may carry method /
    amount / payTo / network from the caller's catalogue entry.
    Raises ValueError (the handler's 422) only when the SSRF guard
    refuses the target."""
    declared = declared or {}
    await asyncio.to_thread(fetch_svc.ssrf_check, url)

    declared_method = (declared.get("method") or "").strip().upper() or None
    # x402 resources live behind POST; probe POST first, then the
    # declared method if it differs, then GET when POST is refused
    # with a method error — every method actually tried is reported.
    methods: list[str] = []
    for m in ("POST", declared_method, "GET"):
        if m and m not in methods:
            methods.append(m)

    probes: dict[str, tuple] = {}
    transport_error: str | None = None
    for method in methods:
        try:
            status, headers, body, _redirects = await _probe_method(
                client, url, method
            )
        except (UpstreamError, httpx.HTTPError) as e:
            transport_error = str(e)
            continue
        probes[method] = (status, headers, body)
        if status == 402:
            break
        if method == "POST" and status not in (405, 501) and declared_method in (None, "POST"):
            # A definitive non-method answer on the canonical method:
            # no other method will produce a different sign.
            break

    report: dict = {
        "url": url,
        "origin": _origin_of(url),
        "read_at": _now_iso(),
        "methods_probed": [m for m in methods if m in probes],
        "http_status": None,
        "challenge_found": False,
        "challenge_source": None,
        "x402_version": None,
        "accepts": [],
        "sdk_compat": None,
        "declared": declared or None,
        "mismatches": [],
        "defects": [],
        "limits": LIMITS,
    }
    if not probes:
        report["defects"].append(
            {"code": "unreachable", "detail": transport_error or "probe failed"}
        )
        return report

    # Prefer the probe that produced a 402; otherwise the first.
    chosen_method = next(
        (m for m, (s, _h, _b) in probes.items() if s == 402),
        next(iter(probes)),
    )
    status, headers, body = probes[chosen_method]
    report["http_status"] = status
    report["answered_on_method"] = chosen_method

    if status != 402:
        report["defects"].append(
            {
                "code": "no_challenge",
                "detail": f"target answered HTTP {status}, not a 402 challenge",
            }
        )
        if declared_method and declared_method not in probes:
            report["defects"].append(
                {
                    "code": "declared_method_unreachable",
                    "detail": f"declared method {declared_method} could not be probed",
                }
            )
        return report

    challenge, source, problems = extract_challenge(headers, body)
    for problem in problems:
        report["defects"].append({"code": "challenge_problem", "detail": problem})
    if challenge is None:
        report["defects"].append(
            {"code": "unparseable_challenge", "detail": "402 with no readable accepts"}
        )
        return report

    version = challenge_version(challenge)
    report["challenge_found"] = True
    report["challenge_source"] = source
    report["x402_version"] = version
    normalized = [
        normalize_accept(a, challenge, version)
        for a in challenge.get("accepts") or []
        if isinstance(a, dict)
    ]
    report["accepts"] = normalized
    for i, raw in enumerate(challenge.get("accepts") or []):
        if isinstance(raw, dict):
            for field in missing_fields(raw, version):
                report["defects"].append(
                    {
                        "code": "missing_field",
                        "detail": f"accepts[{i}] is missing {field}",
                    }
                )
    report["sdk_compat"] = sdk_compat(challenge, version, normalized)

    # Method check: the declared method must be a method that
    # actually answers 402.
    if declared_method:
        answered = [m for m, (s, _h, _b) in probes.items() if s == 402]
        if declared_method not in answered:
            report["mismatches"].append(
                {
                    "field": "method",
                    "declared": declared_method,
                    "live": answered,
                    "detail": "declared method does not answer 402",
                }
            )
    # Declared terms vs the live first accept.
    if normalized:
        first = normalized[0]
        for field, key in (
            ("amount", "amount_atomic"),
            ("payTo", "payTo"),
            ("network", "network"),
        ):
            declared_value = declared.get(field)
            if declared_value not in (None, "") and str(declared_value) != str(
                first.get(key)
            ):
                report["mismatches"].append(
                    {
                        "field": field,
                        "declared": str(declared_value),
                        "live": first.get(key),
                    }
                )
    return report
