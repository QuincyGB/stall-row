"""Fleet Check on the Solana rail: the two Fleet Check routes accept
USDC on Solana mainnet as a second x402 accept, mirroring Token
Verdict's dual-accept implementation (app/solana_rail.py there and
here: same facilitator verify → settle flow, plus the local
pre-verify + replay hooks).

Everything here runs without network writes and without real funds:

- Payment payloads are built with the official x402 SDK's *client*
  scheme from a throwaway in-test keypair (a test fixture holding
  nothing, never persisted, unrelated to any real wallet).
- The facilitator is a stub implementing the SDK facilitator protocol —
  /verify, /settle and /supported are answered locally.
- Blockhash hints are injected via requirements.extra and mint metadata
  is pre-seeded into the client scheme's cache, so no Solana RPC is
  touched and nothing settles anywhere.

What is pinned down: the config values (Phantom payTo, USDC mint,
network id, route scope), exactly two accepts on both Fleet Check
challenges with the Base accept unchanged, a Solana payment reaching
the Solana verification branch (and a settled one never verifying
twice), garbage / wrong-network payments rejected cleanly the same
way Base garbage is, the discovery manifest mirroring the dual
accepts without moving the resource count, and the Solana payment
identity feeding the pack mint's idempotency key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from starlette.requests import Request

from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.mechanisms.svm.constants import TOKEN_PROGRAM_ADDRESS
from x402.mechanisms.svm.exact.client import ExactSvmScheme as ExactSvmClientScheme
from x402.mechanisms.svm.exact.server import ExactSvmScheme as ExactSvmServerScheme
from x402.mechanisms.svm.mint_cache import MintMetadata
from x402.mechanisms.svm.signers import KeypairSigner
from x402.mechanisms.svm.utils import derive_ata
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo, VerifyResponse
from x402.schemas.config import ResourceConfig
from x402.schemas.responses import SettleResponse, SupportedKind, SupportedResponse
from x402.server import x402ResourceServer
from solders.pubkey import Pubkey

import app.main as main
from app import solana_rail
from app.config import PRICE_UNITS, SOLANA_ACCEPT_PATHS
from app.discovery import x402_manifest
from app.services import recheck
from app.solana_rail import (
    SvmPaymentError,
    SvmReplayGuard,
    make_after_settle_hook,
    make_before_verify_hook,
    validate_svm_payment,
)


@pytest.fixture(autouse=True)
def _scrub_proxy_env(monkeypatch):
    # The sandbox exports proxy variables whose IPv6 form the local httpx
    # fork misparses when the solana-py client is CONSTRUCTED (no request
    # is made in these tests). Production (Railway) has no such variables.
    for var in (
        "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "all_proxy", "no_proxy", "NO_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)


NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
BASE_NETWORK = "eip155:8453"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAY_TO = "FeyyAfoCbn1psQ6x8gxN8YQbXZVpTSibZwP6mkXckXUJ"  # Phantom
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
# Stub facilitator's Solana fee payer (the live facilitator's own fee
# payer arrives via its /supported document at middleware init).
FEE_PAYER = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"
BLOCKHASH = "11111111111111111111111111111111"

FLEET_CHECK_AMOUNTS = {"/v1/fleet-check": "100000", "/v1/fleet-check/pack": "5000000"}


# --- builders -----------------------------------------------------------------


def solana_requirements(amount="100000", **over) -> PaymentRequirements:
    fields = dict(
        scheme="exact",
        network=NETWORK,
        asset=MINT,
        amount=amount,
        pay_to=PAY_TO,
        max_timeout_seconds=300,
        extra={"feePayer": FEE_PAYER, "recentBlockhash": BLOCKHASH},
    )
    fields.update(over)
    return PaymentRequirements(**fields)


def make_payload(requirements: PaymentRequirements, payer: Keypair | None = None) -> PaymentPayload:
    """Build a real SVM exact payload with the SDK client scheme (offline)."""
    payer = payer or Keypair()  # throwaway test fixture — holds nothing
    scheme = ExactSvmClientScheme(KeypairSigner(payer))
    scheme._mint_cache[(str(requirements.network), requirements.asset)] = MintMetadata(
        decimals=6,
        token_program=Pubkey.from_string(TOKEN_PROGRAM_ADDRESS),
    )
    inner = scheme.create_payment_payload(requirements)
    return PaymentPayload(x402_version=2, payload=inner, accepted=requirements)


class StubFacilitator:
    """Facilitator protocol stub: knows Base + Solana v2 exact kinds.
    get_supported is synchronous (the resource server calls it from
    initialize()); verify/settle are async. Nothing leaves the process."""

    def __init__(self):
        self.verify_calls = 0
        self.settle_calls = 0

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[
                SupportedKind(x402_version=2, scheme="exact", network=BASE_NETWORK, extra={}),
                SupportedKind(
                    x402_version=2,
                    scheme="exact",
                    network=NETWORK,
                    extra={"feePayer": FEE_PAYER},
                ),
            ]
        )

    async def verify(self, payload, requirements) -> VerifyResponse:
        self.verify_calls += 1
        return VerifyResponse(is_valid=True, payer="stub-payer")

    async def settle(self, payload, requirements) -> SettleResponse:
        self.settle_calls += 1
        return SettleResponse(success=True, transaction="stub-signature", network=requirements.network)


# --- config + route scope -------------------------------------------------------


def test_config_solana_values_and_scope():
    s = main.settings
    assert s.solana_pay_to == PAY_TO
    assert s.solana_network == NETWORK
    assert s.solana_usdc_mint == MINT
    assert solana_rail.is_valid_solana_address(s.solana_pay_to)
    # Exactly the two Fleet Check routes gain the accept — no others.
    assert SOLANA_ACCEPT_PATHS == frozenset(FLEET_CHECK_AMOUNTS)
    # The SVM scheme is registered on the shared resource server.
    assert main.resource_server.has_registered_scheme(NETWORK, "exact")
    assert main.resource_server.has_registered_scheme(BASE_NETWORK, "exact")


def test_routes_carry_dual_accepts_and_others_stay_base_only():
    assert len(main.routes) == len(PRICE_UNITS) == 30
    for path, amount in FLEET_CHECK_AMOUNTS.items():
        accepts = main.routes[f"POST {path}"].accepts
        assert len(accepts) == 2, path
        base, sol = accepts
        # Base accept: the original option, unchanged.
        assert str(base.network) == BASE_NETWORK
        assert base.pay_to == OWNER and base.price == main.settings.prices[path]
        # Solana accept: same USD price, Phantom payTo, Solana network.
        assert str(sol.network) == NETWORK
        assert sol.pay_to == PAY_TO and sol.price == main.settings.prices[path]
        # And the SDK's SVM scheme prices that string to the route's
        # exact smallest-unit amount against the USDC mint.
        parsed = ExactSvmServerScheme().parse_price(sol.price, NETWORK)
        assert parsed.amount == amount and parsed.asset == MINT
    for key, route in main.routes.items():
        if key in {f"POST {p}" for p in FLEET_CHECK_AMOUNTS}:
            continue
        assert [str(a.network) for a in route.accepts] == [BASE_NETWORK], key


# --- the unpaid challenges (HTTP surface) ---------------------------------------


def _challenge(response) -> dict:
    raw = response.headers.get("payment-required")
    assert raw, "402 response must carry a payment-required header"
    return json.loads(base64.b64decode(raw))


@pytest.mark.parametrize("path,amount", sorted(FLEET_CHECK_AMOUNTS.items()))
def test_unpaid_challenge_has_exactly_two_accepts(path, amount):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(path, json={"origins": [ORIGIN]})
    assert r.status_code == 402
    accepts = _challenge(r)["accepts"]
    assert len(accepts) == 2
    base, sol = accepts
    # Base accept byte-for-byte the pre-rail requirements.
    assert base == {
        "scheme": "exact",
        "network": BASE_NETWORK,
        "asset": USDC_BASE,
        "amount": amount,
        "payTo": OWNER,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }
    assert sol["scheme"] == "exact"
    assert sol["network"] == NETWORK
    assert sol["asset"] == MINT
    assert sol["amount"] == amount  # same USD price, 6-decimal units
    assert sol["payTo"] == PAY_TO
    assert sol["maxTimeoutSeconds"] == 300
    # The facilitator's fee payer is advertised (a Solana exact payment
    # cannot even be built without it) — supplied by the configured
    # facilitator's /supported document at middleware init.
    fee_payer = sol["extra"]["feePayer"]
    assert solana_rail.is_valid_solana_address(fee_payer)


# --- Solana payment reaches the Solana verification branch ----------------------


@pytest.mark.asyncio
async def test_solana_payment_verifies_and_settled_one_cannot_verify_twice(monkeypatch):
    # A resource server wired exactly as app/main.py wires the shared
    # one (same schemes, same hooks), but with the stub facilitator:
    # the verifier is mocked, no live chain writes, nothing settles.
    facilitator = StubFacilitator()
    server = x402ResourceServer(facilitator)
    server.register(BASE_NETWORK, ExactEvmScheme())
    server.register(NETWORK, ExactSvmServerScheme())

    calls = []
    real_validate = solana_rail.validate_svm_payment

    def spy(payload, requirements, **kw):
        calls.append((payload, requirements))
        return real_validate(payload, requirements, **kw)

    monkeypatch.setattr(solana_rail, "validate_svm_payment", spy)

    guard = SvmReplayGuard()
    server.on_before_verify(
        make_before_verify_hook(guard, solana_network=NETWORK, usdc_mint=MINT)
    )
    server.on_after_settle(make_after_settle_hook(guard, solana_network=NETWORK))
    server.initialize()

    sol_req = server.build_payment_requirements(
        ResourceConfig(scheme="exact", network=NETWORK, pay_to=PAY_TO, price="$0.10")
    )[0]
    assert sol_req.asset == MINT and sol_req.amount == "100000"
    assert sol_req.extra["feePayer"] == FEE_PAYER

    # A well-formed Solana payment reaches the Solana branch: the local
    # pre-verify hook validates it, THEN the facilitator verify runs.
    sol_req.extra["recentBlockhash"] = BLOCKHASH
    payload = make_payload(sol_req)
    result = await server.verify_payment(payload, sol_req)
    assert result.verify.is_valid is True
    assert len(calls) == 1
    assert str(calls[0][1].network) == NETWORK
    assert facilitator.verify_calls == 1

    settle = await server.settle_payment(payload, sol_req)
    assert settle.success is True
    assert facilitator.settle_calls == 1

    # Replay: the same settled transaction must not verify a second
    # time — the before-verify hook aborts it locally, before the
    # facilitator is called again.
    from x402.schemas.errors import PaymentAbortedError

    with pytest.raises(PaymentAbortedError, match="duplicate_settlement"):
        await server.verify_payment(payload, sol_req)
    assert facilitator.verify_calls == 1


def test_validate_svm_payment_checks_transfer_semantics():
    req = solana_requirements()
    payload = make_payload(req)
    info = validate_svm_payment(payload, req, expected_mint=MINT)
    assert info.amount == 100000
    assert info.mint == MINT
    assert info.fee_payer == FEE_PAYER
    assert info.destination_ata == derive_ata(PAY_TO, MINT, info.token_program)

    # Underpayment for the pack price is refused.
    cheap = make_payload(solana_requirements(amount="100000"))
    with pytest.raises(SvmPaymentError) as e:
        validate_svm_payment(cheap, solana_requirements(amount="5000000"), expected_mint=MINT)
    assert "amount_insufficient" in e.value.reason


# --- garbage / wrong network: clean rejection, same as Base ---------------------


class _Ctx:
    def __init__(self, payload, requirements):
        self.payment_payload = payload
        self.requirements = requirements


def test_before_verify_hook_rejects_garbage_and_wrong_network():
    guard = SvmReplayGuard()
    before = make_before_verify_hook(guard, solana_network=NETWORK, usdc_mint=MINT)

    garbage = PaymentPayload(
        x402_version=2,
        payload={"transaction": "!!!not-base64-a-transaction!!!"},
        accepted=solana_requirements(),
    )
    result = before(_Ctx(garbage, solana_requirements()))
    assert result is not None
    assert result.reason == "invalid_exact_svm_payload_transaction"

    # Wrong network: the payload's accepted network disagrees with the
    # requirements it claims to fulfil — validate_svm_payment refuses
    # it (the hook itself is scoped to the configured mainnet, so this
    # check is exercised at the validator level, as on Token Verdict).
    payload = make_payload(solana_requirements())
    devnet_req = solana_requirements(network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
    with pytest.raises(SvmPaymentError) as e:
        validate_svm_payment(payload, devnet_req, expected_mint=MINT)
    assert "network_mismatch" in e.value.reason

    # EVM requirements pass through the Solana hooks untouched.
    evm_req = PaymentRequirements(
        scheme="exact",
        network=BASE_NETWORK,
        asset=USDC_BASE,
        amount="100000",
        pay_to=OWNER,
        max_timeout_seconds=300,
        extra={},
    )
    assert before(_Ctx(payload, evm_req)) is None


def test_http_garbage_payment_header_rejected_like_base():
    # Existing Base behavior for a garbage payment header is a clean
    # 402 re-issue (the middleware refuses it and re-challenges) — the
    # Fleet Check routes behave identically, never a 500 or a delivery.
    client = TestClient(main.app, raise_server_exceptions=False)
    garbage = base64.b64encode(
        b'{"x402Version":2,"payload":{"transaction":"!!!garbage!!!"}}'
    ).decode()
    base_r = client.post("/v1/gas-oracle", json={}, headers={"payment-signature": garbage})
    for path in FLEET_CHECK_AMOUNTS:
        r = client.post(
            path, json={"origins": [ORIGIN]}, headers={"payment-signature": garbage}
        )
        assert r.status_code == base_r.status_code == 402
        # The re-issued challenge still carries both accepts.
        assert len(_challenge(r)["accepts"]) == 2


# --- discovery: dual accepts mirrored, resource count unchanged -------------------


def test_manifest_mirrors_dual_accepts_and_count_unchanged():
    manifest = x402_manifest(main.settings, "https://stall-row.example.com")
    assert len(manifest["resources"]) == 31  # 30 priced + the free /quote
    by_path = {
        r["resource"].removeprefix("https://stall-row.example.com"): r
        for r in manifest["resources"]
    }
    for path, amount in FLEET_CHECK_AMOUNTS.items():
        accepts = by_path[path]["accepts"]
        assert len(accepts) == 2, path
        assert accepts[0]["network"] == BASE_NETWORK
        assert accepts[0]["amount"] == amount and accepts[0]["payTo"] == OWNER
        assert accepts[1] == {
            "scheme": "exact",
            "network": NETWORK,
            "amount": amount,
            "asset": MINT,
            "payTo": PAY_TO,
            "maxTimeoutSeconds": 300,
        }
    assert len(by_path["/v1/recheck"]["accepts"]) == 1


# --- Solana payment identity -> pack mint idempotency ------------------------------


def _svm_request(transaction="dGVzdC10cmFuc2FjdGlvbg=="):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/fleet-check/pack",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "https",
        "client": ("testclient", 50000),
    }
    request = Request(scope)
    request.state.payment_payload = SimpleNamespace(
        payload={"transaction": transaction}
    )
    return request


def test_payment_key_uses_svm_transaction_identity():
    key = main._payment_key(_svm_request())
    assert key == "svm-tx:" + hashlib.sha256(b"dGVzdC10cmFuc2FjdGlvbg==").hexdigest()


def test_solana_paid_pack_buy_mints_once_and_replay_returns_same_pack(monkeypatch, tmp_path):
    # The handler runs only after the middleware has verified + settled
    # the payment (either rail); driven here with a Solana payment
    # identity, the mint is idempotent exactly as on the Base rail.
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)

    req = main.FleetCheckPackBuyRequest(origins=[ORIGIN], interval_s=3600)
    out = asyncio.run(main.fleet_check_pack_buy(req, _svm_request()))
    assert out["credits_total"] == 100 and out["price_units"] == 5_000_000

    replay = asyncio.run(main.fleet_check_pack_buy(req, _svm_request()))
    assert replay["pack_id"] == out["pack_id"]
    assert "already minted" in replay["note"]
    assert fresh.fleet_check_pack_count() == 1
