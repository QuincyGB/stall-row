"""Prepaid entitlement tokens (W3 Build 1, 2026-11-19): the RTA-style
"subscription workaround" for x402, which has no recurrence primitive.

One x402 payment buys a signed Ed25519 entitlement token:

  * POST /v1/entitlement-pass — $9.00: 30 days, 1,800 credits
    (the RTA price point: 9 USDC = 30 days at a $0.005 list call).
  * POST /v1/entitlement-pack — $5.00: 1,000 credits, 90-day expiry.

Subsequent calls present the token in the `X-Entitlement` header
instead of paying per call. The server verifies the Ed25519
signature, the expiry, and the remaining credits, decrements one
credit ATOMICALLY in the WatchStore ledger before the handler runs,
and refunds the credit when our side fails to deliver — the same
reserve-then-run, fail-closed discipline as packs and Fleet Passes.
Per-call pricing stays fully intact alongside: a call that presents
no token pays per call exactly as before, and a call that presents
an INVALID token is refused — never silently converted into a
per-call charge.

SIWX decision (recorded 2026-11-19): the alternative pass pattern in
the ecosystem (SpendTheBits) gates return visits with the
sign-in-with-x (SIWX) extension. We deliberately do NOT use SIWX:
VulnCheck published CVE-2026-113603 (medium, SIWX authentication
bypass) on 2026-10-20 and the fix was still unmerged at the
2026-11-14 ecosystem check. A bearer token whose signature we verify
ourselves, backed by a server-side ledger row (credits, expiry,
revocation), has no dependency on that extension.

Token format: ``ent1.<payload>.<signature>`` where payload is
base64url(canonical JSON claims) and signature is
base64url(Ed25519 signature over the ASCII bytes of
``ent1.<payload>``). Claims: v, jti (ledger row id), kind
("pass"|"pack"), credits_total, iat, exp (epoch seconds), and an
optional payer_hint. The token is a bearer credential — possession
authorises spend, exactly like a pack id — but unlike a pack id it
is self-verifying: tampering with any claim breaks the signature,
and the ledger row (not the token) is the source of truth for
remaining credits and revocation, so a copied token cannot mint
credits and a revoked token dies even before its expiry.

Signing keys come from the environment ONLY, never from code or the
repo:

  * STALLROW_ENTITLEMENT_PRIVATE_KEY_B64 — base64 of the raw 32-byte
    Ed25519 private key (seed). Required to ISSUE tokens.
  * STALLROW_ENTITLEMENT_PUBLIC_KEY_B64 — base64 of the raw 32-byte
    public key. Used to VERIFY when no private key is configured
    (e.g. a verify-only replica); derived from the private key when
    both are present (a mismatch fails closed at startup of the
    verification path).

With no key configured, issuance answers 502 (the x402 middleware
does not settle on >= 400, so nobody pays for an unmintable token)
and a presented token is refused 503 — fail closed in both
directions. Tests generate ephemeral keys locally; no key material
is ever committed.
"""

from __future__ import annotations

import base64
import json
import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

TOKEN_PREFIX = "ent1"
ENTITLEMENT_HEADER = "x-entitlement"

# Products (mirror config.PRICE_UNITS for the two buy routes).
PASS_PRICE_UNITS = 9_000_000  # $9.00
PASS_CREDITS = 1_800
PASS_TTL_DAYS = 30
PACK_PRICE_UNITS = 5_000_000  # $5.00
PACK_CREDITS = 1_000
PACK_TTL_DAYS = 90

KINDS = {
    "pass": {"credits": PASS_CREDITS, "ttl_days": PASS_TTL_DAYS},
    "pack": {"credits": PACK_CREDITS, "ttl_days": PACK_TTL_DAYS},
}


class EntitlementError(Exception):
    """A token verification / spend refusal. `code` is machine-stable;
    the HTTP layer maps it to `http_status`. Nothing is decremented
    on any EntitlementError path."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --- key handling ---------------------------------------------------------------


def _b64decode(raw: str) -> bytes:
    return base64.b64decode(raw.strip(), validate=True)


def load_private_key() -> Ed25519PrivateKey | None:
    raw = os.environ.get("STALLROW_ENTITLEMENT_PRIVATE_KEY_B64", "").strip()
    if not raw:
        return None
    try:
        seed = _b64decode(raw)
    except Exception as e:  # noqa: BLE001 — a bad key must fail closed, loudly
        raise EntitlementError(
            "entitlement_key_invalid",
            "STALLROW_ENTITLEMENT_PRIVATE_KEY_B64 is not valid base64",
            503,
        ) from e
    if len(seed) != 32:
        raise EntitlementError(
            "entitlement_key_invalid",
            "entitlement private key must be a raw 32-byte Ed25519 seed",
            503,
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_public_key() -> Ed25519PublicKey | None:
    """The verification key: derived from the private key when one is
    configured (an additionally configured public key must match it),
    else the configured public key, else None (verification cannot
    run — presented tokens are refused)."""
    private = load_private_key()
    raw = os.environ.get("STALLROW_ENTITLEMENT_PUBLIC_KEY_B64", "").strip()
    configured: Ed25519PublicKey | None = None
    if raw:
        try:
            pub_bytes = _b64decode(raw)
        except Exception as e:  # noqa: BLE001
            raise EntitlementError(
                "entitlement_key_invalid",
                "STALLROW_ENTITLEMENT_PUBLIC_KEY_B64 is not valid base64",
                503,
            ) from e
        if len(pub_bytes) != 32:
            raise EntitlementError(
                "entitlement_key_invalid",
                "entitlement public key must be 32 raw bytes",
                503,
            )
        configured = Ed25519PublicKey.from_public_bytes(pub_bytes)
    if private is not None:
        derived = private.public_key()
        if configured is not None and (
            configured.public_bytes_raw() != derived.public_bytes_raw()
        ):
            raise EntitlementError(
                "entitlement_key_mismatch",
                "configured entitlement public key does not match the private key",
                503,
            )
        return derived
    return configured


def generate_dev_keypair() -> tuple[str, str]:
    """(private_b64, public_b64) for LOCAL TESTS ONLY. Never commit
    the output; production keys are generated by the operator and set
    as environment variables."""
    private = Ed25519PrivateKey.generate()
    return (
        base64.b64encode(private.private_bytes_raw()).decode(),
        base64.b64encode(private.public_key().public_bytes_raw()).decode(),
    )


def signing_available() -> bool:
    try:
        return load_private_key() is not None
    except EntitlementError:
        return False


# --- token encode / decode --------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(
    private_key: Ed25519PrivateKey,
    *,
    jti: str,
    kind: str,
    credits_total: int,
    issued_epoch: float,
    expires_epoch: float,
    payer_hint: str | None = None,
) -> str:
    """Sign a token for one ledger row. The row (minted first, in the
    store) owns credits/revocation; this string is its portable,
    self-verifying presentation."""
    claims = {
        "v": 1,
        "jti": jti,
        "kind": kind,
        "credits_total": int(credits_total),
        "iat": int(issued_epoch),
        "exp": int(expires_epoch),
    }
    if payer_hint:
        claims["payer_hint"] = payer_hint
    payload_part = _b64url_encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    )
    signing_input = f"{TOKEN_PREFIX}.{payload_part}".encode()
    signature = private_key.sign(signing_input)
    return f"{TOKEN_PREFIX}.{payload_part}.{_b64url_encode(signature)}"


def parse_and_verify(
    token: str,
    public_key: Ed25519PublicKey | None,
    now_epoch: float | None = None,
) -> dict:
    """Verify a presented token's shape, signature, and expiry.
    Returns the claims dict. Raises EntitlementError on every failure
    — malformed (422), bad signature (401), expired (410). Ledger
    state (credits remaining, revocation) is checked separately by
    the store spend, which is the fail-closed source of truth."""
    now = time.time() if now_epoch is None else now_epoch
    if public_key is None:
        raise EntitlementError(
            "entitlement_unconfigured",
            "entitlement verification is not configured on this service",
            503,
        )
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise EntitlementError(
            "entitlement_malformed", "entitlement token is malformed", 422
        )
    try:
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except Exception as e:  # noqa: BLE001
        raise EntitlementError(
            "entitlement_malformed", "entitlement token is malformed", 422
        ) from e
    if not isinstance(claims, dict) or claims.get("v") != 1:
        raise EntitlementError(
            "entitlement_malformed", "entitlement token is malformed", 422
        )
    for field in ("jti", "kind", "credits_total", "iat", "exp"):
        if field not in claims:
            raise EntitlementError(
                "entitlement_malformed",
                f"entitlement token is missing its {field} claim",
                422,
            )
    try:
        public_key.verify(signature, f"{TOKEN_PREFIX}.{parts[1]}".encode())
    except InvalidSignature as e:
        raise EntitlementError(
            "entitlement_bad_signature",
            "entitlement token signature does not verify",
            401,
        ) from e
    if now >= float(claims["exp"]):
        raise EntitlementError(
            "entitlement_expired", "this entitlement has expired", 410
        )
    return claims
