"""Wallet verdict: sanctions exact-match / no-match / degrade-to-
unknown, poisoning heuristic, age scoring."""

import pytest

from app.blockscout import Blockscout
from app.config import load_settings
from app.rpc import Rpc
from app.services import verdict
from tests.fixtures import ADDR, mock_client

S = load_settings()


def test_snapshot_is_bundled_and_dated():
    assert verdict.SNAPSHOT["count"] >= 50
    assert verdict.SNAPSHOT["snapshot_date"] == "2026-10-21"
    assert "treasury.gov" in (verdict.SNAPSHOT["source_url"] or "")


def test_sanctions_match_for_listed_address():
    listed = sorted(verdict.SNAPSHOT["addresses"])[0]
    out = verdict.sanctions_check(listed)
    assert out["status"] == "matched"
    assert out["matched"] is True


def test_sanctions_no_match_for_clean_address():
    out = verdict.sanctions_check(ADDR)
    assert out["status"] == "no_match"
    assert out["matched"] is False


def test_sanctions_degrades_to_unknown_when_snapshot_empty(monkeypatch):
    monkeypatch.setattr(
        verdict, "SNAPSHOT",
        {"addresses": set(), "source_url": None, "snapshot_date": None, "count": 0},
    )
    out = verdict.sanctions_check(ADDR)
    assert out["status"] == "unknown"
    assert out["matched"] is None  # never a fabricated "clean"


def test_poisoning_detects_zero_value_lookalike():
    me = ADDR
    trusted = "0x1111111111111111111111111111111111111111"
    # lookalike of `trusted`: same first 4 / last 4 hex chars
    poison = "0x1111" + "deadbeef" * 4 + "1111"
    assert poison[:6] == trusted[:6] and poison[-4:] == trusted[-4:] and poison != trusted
    transfers = [
        {"from": trusted, "to": me, "value_raw": "500", "tx_hash": "0x1"},
        {"from": poison, "to": me, "value_raw": "0", "tx_hash": "0x2"},
    ]
    out = verdict.poisoning_check(me, transfers)
    assert out["status"] == "signals_found"
    assert out["signals"][0]["poison_sender"] == poison
    assert out["signals"][0]["mimics"] == trusted


def test_poisoning_unknown_without_history():
    out = verdict.poisoning_check(ADDR, None)
    assert out["status"] == "unknown"


@pytest.mark.asyncio
async def test_wallet_verdict_happy_path():
    client = mock_client()
    async with client:
        out = await verdict.wallet_verdict(
            client, Rpc(client, S.rpc_url), Blockscout(client, S.blockscout_base_url), ADDR
        )
    assert out["sanctions"]["status"] == "no_match"
    assert out["wallet_age_days"] is not None and out["wallet_age_days"] > 300
    assert out["verdict"] in ("SAFE", "CAUTION")
    assert out["score"] >= 75
