"""`upto` scheme (W3 Build 2): accept generation via the installed
x402 SDK, the facilitator gap that keeps the feature flag OFF by
default, the variable-cost models, and the settlement-override
plumbing. Fully offline — no facilitator is contacted."""

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from x402.mechanisms.evm.upto.server import UptoEvmScheme
from x402.schemas import PaymentRequirements, SupportedKind

import app.main as main
from app.services import upto

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
FACILITATOR = "0x1111111111111111111111111111111111111111"


# --- feature flag -----------------------------------------------------------------


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("STALLROW_UPTO_ENABLED", raising=False)
    assert upto.upto_enabled() is False
    monkeypatch.setenv("STALLROW_UPTO_ENABLED", "1")
    assert upto.upto_enabled() is True
    monkeypatch.setenv("STALLROW_UPTO_ENABLED", "true")
    assert upto.upto_enabled() is False  # only the exact "1" enables


def test_upto_routes_are_exactly_the_two_variable_cost_routes():
    assert upto.UPTO_ROUTES == frozenset({"/v1/page-to-markdown", "/v1/tx-decoder"})


# --- accept generation --------------------------------------------------------------


def test_payment_option_shape():
    opt = upto.upto_payment_option(pay_to=OWNER, price="$0.01", network="eip155:8453")
    assert opt.scheme == "upto"
    assert opt.pay_to == OWNER
    assert opt.price == "$0.01"  # the cap IS the listed price
    assert opt.network == "eip155:8453"


def _requirements(price="$0.01"):
    scheme = UptoEvmScheme()
    amt = scheme.parse_price(price, "eip155:8453")
    return scheme, PaymentRequirements(
        scheme="upto",
        network="eip155:8453",
        amount=amt.amount,
        asset=amt.asset,
        pay_to=OWNER,
        max_timeout_seconds=60,
        extra=dict(amt.extra or {}),
    )


def test_sdk_generates_upto_accept_with_facilitator_address():
    scheme, req = _requirements()
    kind = SupportedKind(
        x402_version=2, scheme="upto", network="eip155:8453",
        extra={"facilitatorAddress": FACILITATOR},
    )
    out = scheme.enhance_payment_requirements(req, kind, [])
    assert out.scheme == "upto"
    assert out.amount == "10000"  # $0.01 cap in atomic units
    assert out.pay_to == OWNER
    assert out.extra["assetTransferMethod"] == "permit2"
    assert out.extra["facilitatorAddress"] == FACILITATOR


def test_sdk_accept_generation_fails_without_facilitator_address():
    """The documented gap: the default (Ultravioleta) facilitator
    advertises upto on eip155:8453 with NO extra, and PayAI does not
    advertise upto at all — in both shapes the SDK refuses to build
    the challenge, which is why STALLROW_UPTO_ENABLED defaults OFF."""
    scheme, req = _requirements()
    kind = SupportedKind(x402_version=2, scheme="upto", network="eip155:8453", extra={})
    with pytest.raises(ValueError, match="facilitatorAddress"):
        scheme.enhance_payment_requirements(req, kind, [])


def test_unpaid_challenges_carry_exact_only_by_default():
    """Flag off (the production default): both variable-cost routes
    answer 402 with a single exact accept — byte-shape unchanged."""
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, body in (
        ("/v1/page-to-markdown", {"url": "https://example.com"}),
        ("/v1/tx-decoder", {"tx_hash": "0x" + "ab" * 32}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 402
        challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
        assert {a["scheme"] for a in challenge["accepts"]} == {"exact"}


# --- cost models ---------------------------------------------------------------------


def test_page_actual_scales_with_words_and_caps():
    assert upto.page_actual_units({"word_count": 0}, 10_000) == 2_000
    assert upto.page_actual_units({"word_count": 3_000}, 10_000) == 5_000
    assert upto.page_actual_units({"word_count": 10**9}, 10_000) == 10_000  # cap
    # No word_count: fall back to the delivered markdown itself.
    assert upto.page_actual_units({"markdown": "one two three"}, 10_000) == 2_003


def test_tx_actual_scales_with_transfers_and_caps():
    assert upto.tx_actual_units({"erc20_transfers": []}, 2_000) == 500
    assert upto.tx_actual_units({"erc20_transfers": [{}] * 4}, 2_000) == 1_500
    assert upto.tx_actual_units({"erc20_transfers": [{}] * 100}, 2_000) == 2_000
    assert upto.tx_actual_units({}, 2_000) == 500  # missing list = base only


# --- settlement override plumbing -------------------------------------------------------


def _request_with_scheme(scheme):
    request = SimpleNamespace(state=SimpleNamespace())
    if scheme is not None:
        request.state.payment_payload = SimpleNamespace(
            accepted=SimpleNamespace(scheme=scheme)
        )
    return request


def test_upto_paid_result_gets_settlement_override():
    result = {"word_count": 3_000, "markdown": "x"}
    out = upto.apply_upto_settlement(
        _request_with_scheme("upto"), "/v1/page-to-markdown", result, 10_000
    )
    assert out is not result  # wrapped in a JSONResponse
    header = out.headers.get("settlement-overrides")
    assert header is not None
    assert json.loads(header) == {"amount": "5000"}  # the ACTUAL, not the cap
    assert json.loads(out.body) == result


def test_non_upto_paths_pass_through_untouched():
    result = {"word_count": 3_000}
    for scheme in ("exact", None):
        out = upto.apply_upto_settlement(
            _request_with_scheme(scheme), "/v1/page-to-markdown", result, 10_000
        )
        assert out is result
    # Error results never carry an override even when upto-paid.
    err = {"error": "boom"}
    out = upto.apply_upto_settlement(
        _request_with_scheme("upto"), "/v1/page-to-markdown", err, 10_000
    )
    assert out is err
