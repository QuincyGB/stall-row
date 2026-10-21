"""Coinbase CDP facilitator auth for Base mainnet.

The public x402.org facilitator only settles testnets (Base Sepolia etc.).
Real USDC on Base mainnet (eip155:8453) requires the CDP facilitator
https://api.cdp.coinbase.com/platform/v2/x402, which authenticates every
verify/settle/supported call with a per-request Ed25519 JWT minted from
the operator's CDP API key.

Uses the OFFICIAL cdp-sdk (pip install cdp-sdk) — no hand-rolled crypto.
"""

from __future__ import annotations

from typing import Any

CDP_HOST = "api.cdp.coinbase.com"
CDP_BASE_PATH = "/platform/v2/x402"


def build_create_headers(api_key_id: str, api_key_secret: str):
    """Return the zero-arg create_headers callable the x402 SDK expects.

    The SDK's CreateHeadersAuthProvider calls it with no arguments and
    expects back {"verify": {...}, "settle": {...}, "supported": {...}} —
    one header map per facilitator operation. A fresh JWT is minted per
    operation because the request path is part of the signed claims.
    """
    try:
        from cdp.auth.utils.jwt import JwtOptions, generate_jwt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "cdp-sdk is required for Base mainnet (CDP facilitator). "
            "Install it with: pip install cdp-sdk"
        ) from e

    def _jwt(method: str, path: str) -> str:
        return generate_jwt(
            JwtOptions(
                api_key_id=api_key_id,
                api_key_secret=api_key_secret,
                request_method=method,
                request_host=CDP_HOST,
                request_path=path,
            )
        )

    def create_headers() -> dict[str, dict[str, str]]:
        return {
            "verify": {"Authorization": f"Bearer {_jwt('POST', CDP_BASE_PATH + '/verify')}"},
            "settle": {"Authorization": f"Bearer {_jwt('POST', CDP_BASE_PATH + '/settle')}"},
            "supported": {"Authorization": f"Bearer {_jwt('GET', CDP_BASE_PATH + '/supported')}"},
        }

    return create_headers


def cdp_facilitator_config(api_key_id: str, api_key_secret: str) -> dict[str, Any]:
    """Config dict for HTTPFacilitatorClient pointing at CDP mainnet."""
    return {
        "url": f"https://{CDP_HOST}{CDP_BASE_PATH}",
        "create_headers": build_create_headers(api_key_id, api_key_secret),
    }
