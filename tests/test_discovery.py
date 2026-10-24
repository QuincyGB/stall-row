"""StallRow's own cataloging wiring — every one of the 12 routes must
meet the standard ListSmith grades others by:

  * facilitator selection is config-driven (PayAI by URL alone, CDP
    only with URL + both keys, keyless default otherwise);
  * every route description is UNDER 500 chars with a use-when line;
  * every route's bazaar extension carries a full typed output schema,
    passes BOTH x402 SDK validators, and its shipped example validates
    against that schema;
  * the manifest lists all 12 resources with accepts identical to the
    live challenges (exact units, USDC on Base, owner payTo).
"""

import os

import jsonschema
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, Settings, load_settings
from app.discovery import ROUTES, route_extension, x402_manifest

CDP_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
PAYAI_URL = "https://facilitator.payai.network"
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _settings(**env):
    keys = ["X402_FACILITATOR_URL", "CDP_API_KEY_ID", "CDP_API_KEY_SECRET"]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        return load_settings()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_twelve_routes_priced_as_specified():
    assert PRICE_UNITS == {
        "/v1/wallet-snapshot": 5000,
        "/v1/allowance-audit": 5000,
        "/v1/tx-decoder": 2000,
        "/v1/gas-oracle": 1000,
        "/v1/wallet-verdict": 20000,
        "/v1/tube-search": 10000,
        "/v1/page-to-markdown": 10000,
        "/v1/repo-vitals": 5000,
        "/v1/price-check": 50000,
        "/v1/card-smith": 100000,
        "/v1/job-scout": 50000,
        "/v1/recheck": 5000,
    }
    assert sum(PRICE_UNITS.values()) == 263_000  # $0.263 to catalog the row
    assert set(ROUTES) == set(PRICE_UNITS)


def test_payai_selected_by_url_alone():
    s = _settings(X402_FACILITATOR_URL=PAYAI_URL)
    assert s.use_payai is True and s.use_cdp is False


def test_cdp_needs_url_and_both_keys():
    assert _settings(X402_FACILITATOR_URL=CDP_URL).use_cdp is False
    assert _settings(X402_FACILITATOR_URL=CDP_URL, CDP_API_KEY_ID="id").use_cdp is False
    s = _settings(X402_FACILITATOR_URL=CDP_URL, CDP_API_KEY_ID="id", CDP_API_KEY_SECRET="<redacted>")
    assert s.use_cdp is True and s.use_payai is False


def test_defaults_are_stallrow_terms():
    s = _settings()
    assert s.pay_to == OWNER
    assert s.network == "eip155:8453"
    assert s.service_name == "stall-row"
    assert s.prices["/v1/card-smith"] == "$0.1"
    assert s.prices["/v1/gas-oracle"] == "$0.001"


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_description_under_500_with_use_when(path):
    d = ROUTES[path]["description"]
    assert len(d) < 500, f"{path}: {len(d)} chars"
    assert "Use this when" in d, path


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_extension_passes_both_sdk_validators(path):
    from x402.extensions.bazaar import (
        validate_discovery_extension,
        validate_discovery_extension_spec,
    )

    ext = route_extension(path)
    result = validate_discovery_extension(ext)
    assert result.valid, f"{path}: {result.errors}"
    spec = validate_discovery_extension_spec(ext)
    assert spec.valid, f"{path}: {spec.errors}"


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_example_validates_against_output_schema(path):
    r = ROUTES[path]
    jsonschema.validate(r["output_example"], r["output_schema"])


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_output_schema_types_real_fields(path):
    props = ROUTES[path]["output_schema"].get("properties") or {}
    assert len(props) >= 3, f"{path} output schema is thin"
    for name, sub in props.items():
        assert "type" in sub, f"{path}.{name} untyped"


def test_manifest_lists_all_twelve_with_matching_accepts():
    manifest = x402_manifest(main.settings, "https://stall-row.example.com")
    paid = [r for r in manifest["resources"] if r["method"] == "POST"]
    assert len(paid) == 12
    for r in paid:
        path = r["resource"].removeprefix("https://stall-row.example.com")
        a = r["accepts"][0]
        assert a["amount"] == str(PRICE_UNITS[path]), path
        assert a["asset"] == USDC
        assert a["payTo"] == OWNER
        assert a["network"] == "eip155:8453"
        assert len(r["description"]) < 500
        assert r["outputSchema"] == ROUTES[path]["output_schema"]


def test_live_manifest_endpoint_matches():
    client = TestClient(main.app, raise_server_exceptions=False)
    body = client.get("/.well-known/x402").json()
    assert body["service"] == "stall-row"
    assert len([r for r in body["resources"] if r["method"] == "POST"]) == 12
