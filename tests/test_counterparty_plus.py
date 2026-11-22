"""W2 counterparty family: deep report, counterparty watch
(baseline + delta), sanctions check, wallet ping, and the
response-upgrade blocks. Offline: the fixtures MockTransport plays
the chain upstreams; eth_getCode (not in the shared fixtures) is
answered by a wrapping handler.
"""

import json

import httpx
import pytest

from app.blockscout import Blockscout
from app.config import PRICE_UNITS, load_settings
from app.rpc import Rpc
from app.services import (
    counterparty_deep,
    counterparty_watch,
    indexfeed,
    reads,
    response_upgrades,
    verdict,
)
from tests.fixtures import ADDR, OTHER, make_handler

S = load_settings()
OFAC_ADDR = sorted(verdict.SNAPSHOT["addresses"])[0]


def _client():
    fixtures_handler = make_handler()

    def handler(request):
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        if body.get("method") == "eth_getCode":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id", 1), "result": "0x"}
            )
        return fixtures_handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _parts(tmp_path):
    client = _client()
    rpc = Rpc(client, S.rpc_url)
    scout = Blockscout(client, S.blockscout_base_url)
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    return client, rpc, scout, store


@pytest.mark.asyncio
async def test_profile_snapshot_reads_real_signals(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    async with client:
        profile = await counterparty_deep.profile_snapshot(
            client, rpc, scout, store, ADDR
        )
    assert profile["eth_balance_wei"] == str(42 * 10**15)
    assert profile["outgoing_tx_count_nonce"] == 137
    assert profile["sanctions"]["status"] == "no_match"
    # Fixture: one incoming USDC transfer from OTHER.
    assert OTHER.lower() in profile["counterparties"]
    cp = profile["counterparties"][OTHER.lower()]
    assert cp["incoming_count"] == 1
    assert cp["volumes_raw_by_token"]["USDC"] == "12500000"
    assert [f["address"] for f in profile["funding_sources"]] == [OTHER.lower()]
    assert profile["coverage"]["token_transfers_sampled"] == 1


@pytest.mark.asyncio
async def test_deep_report_includes_standard_and_deep_legs(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    async with client:
        out = await counterparty_deep.run_counterparty_deep(
            client, rpc, scout, store, pay_to=ADDR, secret="<redacted>"
        )
    assert out["tier"] == "deep"
    assert "wallet" in out["legs"]  # the standard report's legs
    deep = out["deep"]
    assert deep["wallet_age"]["first_activity"]
    assert deep["funding_sources"]
    assert deep["flagged_counterparty_links"] == []
    assert "coverage" in deep
    assert "newest 50" in out["limits"]


@pytest.mark.asyncio
async def test_watch_baseline_then_delta(tmp_path):
    client, rpc, scout, store = _parts(tmp_path)
    watch = counterparty_watch.CounterpartyWatchStore(str(tmp_path / "watch.db"))
    async with client:
        first = await counterparty_watch.run_watch(
            client, rpc, scout, store, watch, ADDR
        )
        second = await counterparty_watch.run_watch(
            client, rpc, scout, store, watch, ADDR
        )
    assert first["status"] == "baseline_established"
    assert first["delta"] is None
    assert second["status"] == "delta"
    delta = second["delta"]
    assert delta["new_counterparties"] == []
    assert delta["changed_volumes"] == []
    assert delta["new_flags"] == []
    assert delta["nonce_delta"] == 0


def test_compute_delta_detects_changes():
    prev = {
        "counterparties": {
            "0xaaa": {"volumes_raw_by_token": {"USDC": "100"}},
        },
        "flags": [],
        "eth_balance_wei": "5",
        "outgoing_tx_count_nonce": 3,
        "generated_at": "t1",
    }
    curr = {
        "counterparties": {
            "0xaaa": {"volumes_raw_by_token": {"USDC": "250"}},
            "0xbbb": {"volumes_raw_by_token": {"USDC": "7"}},
        },
        "flags": [{"type": "ofac_sdn_match", "address": "0xbbb"}],
        "eth_balance_wei": "9",
        "outgoing_tx_count_nonce": 8,
        "generated_at": "t2",
    }
    delta = counterparty_watch.compute_delta(prev, curr)
    assert delta["new_counterparties"] == ["0xbbb"]
    assert delta["changed_volumes"] == [
        {
            "counterparty": "0xaaa",
            "token": "USDC",
            "previous_volume_raw": "100",
            "current_volume_raw": "250",
        }
    ]
    assert delta["new_flags"] == [{"type": "ofac_sdn_match", "address": "0xbbb"}]
    assert delta["balance_changed"] is True
    assert delta["nonce_delta"] == 5


def test_sanctions_check_exact_match_and_label():
    hit = verdict.sanctions_check(OFAC_ADDR)
    assert hit["matched"] is True and hit["status"] == "matched"
    miss = verdict.sanctions_check(ADDR)
    assert miss["matched"] is False and miss["status"] == "no_match"
    assert hit["snapshot_date"] == "2026-10-21"
    assert hit["snapshot_source"] == "https://www.treasury.gov/ofac/downloads/sdn.csv"


@pytest.mark.asyncio
async def test_wallet_ping(tmp_path):
    client, rpc, scout, _store = _parts(tmp_path)
    async with client:
        out = await reads.wallet_ping(rpc, scout, ADDR)
    assert out["exists"] is True
    assert out["is_contract"] is False
    assert out["outgoing_tx_count_nonce"] == 137
    assert out["tx_count_band"] == "101-1000"
    assert out["last_activity"] != "unknown"
    assert out["latest_block"] == 1000


def test_response_upgrades_blocks_and_prices():
    result = {}
    out = response_upgrades.attach_upgrades(
        result, "/v1/wallet-ping", {"address": ADDR}
    )
    assert out["next_check"] == {
        "name": "wallet-snapshot",
        "path": "/v1/wallet-snapshot",
        "price_usd": 0.005,
    }
    ev = out["evidence"]
    assert ev["settlement_tx"] is None
    assert len(ev["input_hash"]) == 64
    assert ev["timestamp"].endswith("Z")
    # Every suggested follow-up is priced exactly as config enforces.
    for route, nxt in response_upgrades.NEXT_CHECKS.items():
        assert PRICE_UNITS[nxt["path"]] == round(nxt["price_usd"] * 1_000_000), route
    # Non-dict results pass through untouched.
    assert response_upgrades.attach_upgrades("x", "/v1/wallet-ping", {}) == "x"


def test_new_route_prices():
    assert PRICE_UNITS["/v1/counterparty-report/deep"] == 250_000
    assert PRICE_UNITS["/v1/counterparty-watch"] == 30_000
    assert PRICE_UNITS["/v1/sanctions-check"] == 30_000
    assert PRICE_UNITS["/v1/wallet-ping"] == 1_000
