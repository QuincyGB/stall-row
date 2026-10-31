"""Next-20 #17 — Pass Balance & Debit API: the enriched free holder
read at POST /v1/fleet-pass/balance. Funded-pass reads match the
store exactly, debit history comes from the persisted ledger rows
(refunded debits marked on their own row), unknown passes 404,
expired passes still read, one pass never leaks another, and the
history bound is respected. Fully offline, fixture passes only."""

import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, recheck


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    return fresh


@pytest.fixture()
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _mint(store, key, units=fleetpass.PASS_PRICE_UNITS, ttl=fleetpass.PASS_TTL_DAYS):
    fleet_pass, created = store.mint_pass(key, units, ttl)
    assert created
    return fleet_pass


def _balance(client, pass_id):
    r = client.post("/v1/fleet-pass/balance", json={"pass_id": pass_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_balance_read_matches_store_state_exactly(http_env, client):
    fleet_pass = _mint(http_env, "pay-bal-1")
    http_env.debit_pass(
        fleet_pass["pass_id"], "token-verdict", "/v1/verdict", 20_000
    )
    http_env.debit_pass(
        fleet_pass["pass_id"], "stall-row", "/v1/recheck", 5_000
    )
    out = _balance(client, fleet_pass["pass_id"])
    stored = http_env.get_pass(fleet_pass["pass_id"])
    assert out["pass_id"] == fleet_pass["pass_id"]
    assert out["balance_units"] == stored["balance_units"] == 4_975_000
    assert out["balance_usd"] == stored["balance_usd"] == 4.975
    assert out["balance_usd_exact"] == "4.975"
    assert out["tier"] == "fleet-pass"
    assert out["original_credit_units"] == 5_000_000
    assert out["original_credit_usd_exact"] == "5"
    assert out["total_units"] == 5_000_000 and out["total_usd"] == 5.0
    assert out["expires_at"] == stored["expires_at"]
    assert out["created_at"] == stored["created_at"]
    assert out["expired"] is False
    # Totals reconcile exactly against the balance.
    assert out["debits"] == 2 and out["debit_count"] == 2
    assert out["total_debited_units"] == 25_000
    assert out["total_debited_usd_exact"] == "0.025"
    assert out["total_refunded_units"] == 0
    assert out["net_debited_units"] == 25_000
    assert (
        out["original_credit_units"] - out["net_debited_units"]
        == out["balance_units"]
    )


def test_fresh_pass_reads_zero_history(http_env, client):
    fleet_pass = _mint(http_env, "pay-bal-fresh")
    out = _balance(client, fleet_pass["pass_id"])
    assert out["balance_units"] == 5_000_000
    assert out["debit_count"] == 0 and out["debits"] == 0
    assert out["total_debited_units"] == 0
    assert out["debit_history"] == []
    assert out["history_limit"] == 100
    assert out["history_returned"] == 0
    assert out["history_truncated"] is False


def test_v2_pass_tier_and_original_credit(http_env, client):
    fleet_pass = _mint(
        http_env, "pay-bal-v2", units=fleetpass.PASS_V2_PRICE_UNITS
    )
    out = _balance(client, fleet_pass["pass_id"])
    assert out["tier"] == "fleet-pass-20"
    assert out["original_credit_units"] == 20_000_000
    assert out["original_credit_usd_exact"] == "20"
    assert out["balance_usd_exact"] == "20"


def test_debit_history_rows_carry_route_amount_time(http_env, client):
    fleet_pass = _mint(http_env, "pay-bal-hist")
    d1 = http_env.debit_pass(
        fleet_pass["pass_id"], "list-smith", "/v1/fix", 500_000
    )
    d2 = http_env.debit_pass(
        fleet_pass["pass_id"], "stall-row", "/v1/recheck", 5_000
    )
    out = _balance(client, fleet_pass["pass_id"])
    history = out["debit_history"]
    assert out["history_returned"] == 2 and out["history_truncated"] is False
    # Newest first.
    assert [h["debit_id"] for h in history] == [d2["debit_id"], d1["debit_id"]]
    newest = history[0]
    assert newest["kind"] == "debit"
    assert newest["service"] == "stall-row"
    assert newest["route"] == "/v1/recheck"
    assert newest["amount_units"] == 5_000
    assert newest["amount_usd_exact"] == "0.005"
    assert newest["refunded"] is False and newest["status"] == "debited"
    # The timestamp is the ledger row's own time (ISO, ~now).
    assert newest["at"].endswith("Z")
    assert abs(time.time() - _epoch_of(newest["at"])) < 120
    older = history[1]
    assert older["route"] == "/v1/fix" and older["amount_units"] == 500_000
    assert older["amount_usd_exact"] == "0.5"


def _epoch_of(iso_z: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


def test_refunded_debit_is_marked_and_totals_net_out(http_env, client):
    fleet_pass = _mint(http_env, "pay-bal-refund")
    d = http_env.debit_pass(
        fleet_pass["pass_id"], "lock-smith", "/v1/audit", 500_000
    )
    keep = http_env.debit_pass(
        fleet_pass["pass_id"], "stall-row", "/v1/gas-oracle", 1_000
    )
    assert http_env.refund_debit(d["debit_id"]) is True
    out = _balance(client, fleet_pass["pass_id"])
    by_id = {h["debit_id"]: h for h in out["debit_history"]}
    assert by_id[d["debit_id"]]["refunded"] is True
    assert by_id[d["debit_id"]]["status"] == "refunded"
    assert by_id[keep["debit_id"]]["refunded"] is False
    assert out["total_debited_units"] == 501_000  # gross
    assert out["total_refunded_units"] == 500_000
    assert out["total_refunded_usd_exact"] == "0.5"
    assert out["net_debited_units"] == 1_000
    assert out["balance_units"] == 5_000_000 - 1_000


def test_unknown_pass_404_and_no_other_shape(http_env, client):
    _mint(http_env, "pay-bal-known")
    r = client.post("/v1/fleet-pass/balance", json={"pass_id": "fpass_nope"})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown pass_id"}


def test_expired_pass_still_readable_with_expired_true(http_env, client):
    stale, created = http_env.mint_pass("pay-bal-stale", 5_000_000, 0)
    assert created and stale["expired"] is True
    out = _balance(client, stale["pass_id"])
    assert out["expired"] is True
    assert out["balance_units"] == 5_000_000
    assert out["expires_at"] == stale["expires_at"]


def test_one_pass_read_never_leaks_another(http_env, client):
    a = _mint(http_env, "pay-bal-a")
    b = _mint(http_env, "pay-bal-b", units=fleetpass.PASS_V2_PRICE_UNITS)
    da = http_env.debit_pass(a["pass_id"], "stall-row", "/v1/orchestrate", 750_000)
    http_env.debit_pass(b["pass_id"], "stall-row", "/v1/recheck", 5_000)

    out_a = _balance(client, a["pass_id"])
    assert out_a["pass_id"] == a["pass_id"]
    assert out_a["debit_count"] == 1
    assert [h["debit_id"] for h in out_a["debit_history"]] == [da["debit_id"]]
    assert out_a["balance_units"] == 5_000_000 - 750_000
    # Nothing of B's anywhere in A's payload.
    assert b["pass_id"] not in str(out_a)
    assert all(h["route"] != "/v1/recheck" for h in out_a["debit_history"])

    out_b = _balance(client, b["pass_id"])
    assert out_b["debit_count"] == 1
    assert out_b["balance_units"] == 20_000_000 - 5_000
    assert a["pass_id"] not in str(out_b)


def test_history_bound_respected(http_env, client):
    fleet_pass = _mint(http_env, "pay-bal-bound")
    total = recheck.WatchStore.PASS_DEBIT_HISTORY_LIMIT + 5
    for _ in range(total):
        http_env.debit_pass(
            fleet_pass["pass_id"], "stall-row", "/v1/gas-oracle", 1_000
        )
    out = _balance(client, fleet_pass["pass_id"])
    assert out["history_limit"] == recheck.WatchStore.PASS_DEBIT_HISTORY_LIMIT
    assert out["history_returned"] == out["history_limit"]
    assert len(out["debit_history"]) == out["history_limit"]
    assert out["debit_count"] == total and out["debits"] == total
    assert out["history_truncated"] is True
    # Totals cover ALL debits, not just the returned page.
    assert out["total_debited_units"] == total * 1_000
    assert out["balance_units"] == 5_000_000 - total * 1_000


def test_store_history_accessor_directly(http_env):
    fleet_pass = _mint(http_env, "pay-bal-store")
    summary = http_env.pass_debit_history(fleet_pass["pass_id"])
    assert summary["debit_count"] == 0 and summary["history"] == []
    http_env.debit_pass(
        fleet_pass["pass_id"], "route-verdict", "/v1/check", 50_000
    )
    summary = http_env.pass_debit_history(fleet_pass["pass_id"], limit=1)
    assert summary["debit_count"] == 1
    assert summary["total_debited_units"] == 50_000
    assert summary["history"][0]["amount_usd_exact"] == "0.05"
    # An unknown pass id yields empty history (the HTTP layer 404s on
    # get_pass before ever serving this).
    empty = http_env.pass_debit_history("fpass_nope")
    assert empty["debit_count"] == 0 and empty["history"] == []


def test_balance_read_is_free_and_not_a_priced_route(http_env, client):
    # Not in the priced table -> the paywall never challenges it and
    # /health's priced-route count is untouched by this surface.
    assert "/v1/fleet-pass/balance" not in main.PRICE_UNITS
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["routes"] == len(main.PRICE_UNITS)
    fleet_pass = _mint(http_env, "pay-bal-free")
    r = client.post(
        "/v1/fleet-pass/balance", json={"pass_id": fleet_pass["pass_id"]}
    )
    assert r.status_code == 200  # no 402 challenge, no payment headers needed


def test_usd_exact_formatting():
    assert recheck.usd_exact(0) == "0"
    assert recheck.usd_exact(5_000_000) == "5"
    assert recheck.usd_exact(4_500_000) == "4.5"
    assert recheck.usd_exact(20_000) == "0.02"
    assert recheck.usd_exact(1) == "0.000001"
    assert recheck.usd_exact(1_234_567) == "1.234567"
