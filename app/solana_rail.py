"""Solana (SVM) payment rail — x402 v2 "exact" scheme on Solana mainnet.

Mirrored from Token Verdict's app/solana_rail.py (the fleet's existing
dual-accept implementation), adapted only in scope: on StallRow the
second rail exists for the two Fleet Check routes (see
config.SOLANA_ACCEPT_PATHS) — every other route on the row stays
Base-only, exactly as before.

Design mirrors the Base rail exactly:

- The service itself holds NO Solana keys and signs nothing. The caller
  builds a partially-signed SPL TransferChecked transaction (fee payer =
  the facilitator), and the x402 facilitator verifies and settles it —
  same verify → settle → release flow as Base, via the same facilitator
  (the configured facilitator supports x402 v2 exact on
  solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp as well as eip155:8453).
- On top of the facilitator, this module adds a LOCAL pre-verify guard
  (registered as x402 before-verify / after-settle hooks) that decodes the
  transaction with the official x402 SDK utilities and checks the transfer
  semantics before any facilitator call: correct fee payer, correct USDC
  mint, destination == the payTo wallet's USDC ATA, amount >= required,
  payer is not the facilitator — plus a replay registry of settled
  transaction message hashes (the SDK's own duplicate-settlement key: the
  fee-payer signature slot is overwritten by the facilitator before
  broadcast, so wire bytes are NOT a stable key; the message hash is).

This is an off-chain payment-verification layer. There is no on-chain
program (Anchor or native) in this service — settlement is an ordinary
SPL token transfer executed by the facilitator.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from x402.mechanisms.svm.types import ExactSvmPayload, TransactionInfo
from x402.mechanisms.svm.utils import (
    decode_transaction_from_payload,
    derive_ata,
    extract_transaction_info,
    transaction_message_hash,
    validate_svm_address,
)
from x402.schemas.hooks import AbortResult

SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# USDC on Solana mainnet (Circle's SPL mint, 6 decimals).
USDC_MINT_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class SvmPaymentError(ValueError):
    """Local SVM validation failure; `reason` is x402-style machine text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_valid_solana_address(address: str) -> bool:
    return validate_svm_address(address)


def validate_svm_payment(
    payload: Any,
    requirements: Any,
    *,
    expected_mint: str,
    expected_decimals: int = USDC_DECIMALS,
) -> TransactionInfo:
    """Validate an SVM exact payment locally, before the facilitator call.

    `payload` is an x402 v2 PaymentPayload; `requirements` the
    PaymentRequirements it claims to fulfil. Returns the extracted
    TransactionInfo on success; raises SvmPaymentError otherwise.

    Checks (mirroring the facilitator's own static validation):
    scheme/network agreement, transaction decodes, a TransferChecked
    exists, fee payer == the facilitator advertised in requirements.extra,
    mint == the configured USDC mint == requirements.asset, destination ==
    ATA(payTo, mint), decimals match, amount >= required amount, and the
    token authority is not the facilitator itself.
    """
    if getattr(payload.accepted, "scheme", None) != "exact" or requirements.scheme != "exact":
        raise SvmPaymentError("invalid_exact_svm_payload_unsupported_scheme")
    if str(payload.accepted.network) != str(requirements.network):
        raise SvmPaymentError("invalid_exact_svm_payload_network_mismatch")
    if requirements.asset != expected_mint:
        raise SvmPaymentError("invalid_exact_svm_payload_unexpected_asset")

    raw = payload.payload or {}
    try:
        svm_payload = ExactSvmPayload.from_dict(raw)
    except Exception as e:
        raise SvmPaymentError("invalid_exact_svm_payload_transaction") from e
    try:
        tx = decode_transaction_from_payload(svm_payload)
    except Exception as e:
        raise SvmPaymentError("invalid_exact_svm_payload_transaction") from e

    info = extract_transaction_info(tx)
    if info is None:
        raise SvmPaymentError("invalid_exact_svm_payload_transaction_no_transfer")

    fee_payer = (requirements.extra or {}).get("feePayer")
    if not fee_payer:
        raise SvmPaymentError("invalid_exact_svm_payload_fee_payer_missing")
    if info.fee_payer != fee_payer:
        raise SvmPaymentError("invalid_exact_svm_payload_fee_payer_mismatch")
    if info.payer == fee_payer:
        raise SvmPaymentError("invalid_exact_svm_payload_authority_is_facilitator")
    if info.mint != requirements.asset:
        raise SvmPaymentError("invalid_exact_svm_payload_mint_mismatch")
    if info.decimals != expected_decimals:
        raise SvmPaymentError("invalid_exact_svm_payload_decimals_mismatch")

    expected_destination = derive_ata(requirements.pay_to, requirements.asset, info.token_program)
    if info.destination_ata != expected_destination:
        raise SvmPaymentError("invalid_exact_svm_payload_destination_mismatch")

    try:
        required_amount = int(requirements.amount)
    except (TypeError, ValueError) as e:
        raise SvmPaymentError("invalid_exact_svm_payload_requirements_amount") from e
    if info.amount < required_amount:
        raise SvmPaymentError("invalid_exact_svm_payload_amount_insufficient")

    return info


class SvmReplayGuard:
    """Registry of settled SVM payment transactions (replay protection).

    Keyed by the transaction *message* hash (see module docstring). A
    transaction that has already settled once can never buy a second
    sweep or pack: the before-verify hook rejects its message hash on
    sight. The facilitator independently rejects duplicate settlements
    (its own settlement cache + the transaction being finalised
    on-chain); this is the local, zero-cost first line.

    In-memory with a TTL: entries are useless after the transaction's
    blockhash era anyway, and a process restart loses nothing the
    facilitator wouldn't still catch on-chain.
    """

    def __init__(self, ttl_s: float = 3600.0):
        self._ttl_s = ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_for(payload: Any) -> str:
        svm_payload = ExactSvmPayload.from_dict(payload.payload or {})
        tx = decode_transaction_from_payload(svm_payload)
        return transaction_message_hash(tx)

    def _evict(self, now: float) -> None:
        stale = [k for k, ts in self._seen.items() if now - ts > self._ttl_s]
        for k in stale:
            del self._seen[k]

    def is_replay(self, payload: Any) -> bool:
        key = self.key_for(payload)
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            return key in self._seen

    def mark_settled(self, payload: Any) -> None:
        key = self.key_for(payload)
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            self._seen[key] = now


def make_before_verify_hook(guard: SvmReplayGuard, *, solana_network: str, usdc_mint: str):
    """x402 before-verify hook: local SVM validation + replay rejection.

    Scoped to the Solana network only — EVM (Base) payloads pass through
    untouched, exactly as before this rail existed.
    """

    def _hook(context: Any):  # noqa: ANN202 — SDK VerifyContext
        requirements = context.requirements
        if str(getattr(requirements, "network", "")) != solana_network:
            return None
        payload = context.payment_payload
        try:
            validate_svm_payment(payload, requirements, expected_mint=usdc_mint)
        except SvmPaymentError as e:
            return AbortResult(reason=e.reason)
        except Exception:  # noqa: BLE001 — undecodable payloads are invalid, not 500s
            return AbortResult(reason="invalid_exact_svm_payload_transaction")
        if guard.is_replay(payload):
            return AbortResult(reason="duplicate_settlement")
        return None

    return _hook


def make_after_settle_hook(guard: SvmReplayGuard, *, solana_network: str):
    """x402 after-settle hook: record settled SVM transactions as spent."""

    def _hook(context: Any):  # noqa: ANN202 — SDK SettleContext
        requirements = context.requirements
        if str(getattr(requirements, "network", "")) != solana_network:
            return None
        try:
            guard.mark_settled(context.payment_payload)
        except Exception:  # noqa: BLE001 — bookkeeping must never break settlement
            pass
        return None

    return _hook
