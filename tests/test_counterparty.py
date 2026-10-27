"""Counterparty Report: leg fan-out — local legs (wallet, sanctions,
index) always run; remote legs (Token Verdict, LockSmith, ListSmith)
run only when named, via the fleet-internal paths. Unrequested legs
say not_requested; failed legs are marked; only a total failure
raises (so the paid call 502s unsettled). Offline: one MockTransport
plays the chain upstreams (tests.fixtures) and the three siblings."""

import httpx
import pytest

from app.blockscout import Blockscout
from app.config import load_settings
from app.rpc import Rpc, UpstreamError
from app.services import counterparty, indexfeed
from tests.fixtures import ADDR, OTHER, make_handler

S = load_settings()

SIBLINGS = {
    "token-verdict-production.up.railway.app": ("/v1/internal/verdict", {"verdict": "SOLID", "score": 9.0}),
    "lock-smith-production.up.railway.app": ("/v1/internal/audit", {"verdict": "SECURE"}),
    "list-smith-production.up.railway.app": ("/v1/internal/grade", {"grade": "A"}),
}


def _client(fail_hosts=()):
    fixtures_handler = make_handler()
    def handler(request):
        host = request.url.host
        if host in SIBLINGS:
            if host in fail_hosts:
                return httpx.Response(502, text="sibling down")
            _path, body = SIBLINGS[host]
            return httpx.Response(200, json=body)
        return fixtures_handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _parts(fail_hosts=(), tmp_path=None, with_index=True):
    client = _client(fail_hosts)
    rpc = Rpc(client, S.rpc_url)
    scout = Blockscout(client, S.blockscout_base_url)
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    if with_index:
        store.save_snapshot(
            [
                {
                    "resource": "https://a.example.com/v1/verdict",
                    "serviceName": "svc",
                    "description": "d",
                    "amount": "20000",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "network": "eip155:8453",
                    "payTo": ADDR,
                    "lastUpdated": None,
                    "calls30d": 5,
                    "payers30d": 1,
                }
            ],
            epoch=1_000_000,
        )
    return client, rpc, scout, store


@pytest.mark.asyncio
async def test_full_report_all_legs(tmp_path):
    client, rpc, scout, store = _parts(tmp_path=tmp_path)
    async with client:
        out = await counterparty.run_counterparty(
            client, rpc, scout, store,
            pay_to=ADDR,
            token_address=OTHER,
            service_url="https://a.example.com/v1/verdict",
            listing_url="https://a.example.com/.well-known/x402",
            secret="test-secret",
        )
    assert sorted(out["legs_ok"]) == [
        "index", "listsmith", "locksmith", "token_verdict", "wallet",
    ]
    assert out["legs_not_requested"] == []
    assert out["legs_failed"] == []
    assert out["legs"]["sanctions"]["status"] == "ok"
    assert out["legs"]["sanctions"]["result"]["status"] == "no_match"
    assert out["legs"]["token_verdict"]["headline"] == "SOLID (risk 9.0)"
    assert out["legs"]["wallet"]["result"]["eth_balance"] == "0.042"
    assert out["legs"]["index"]["result"]["listed_now"] == 1


@pytest.mark.asyncio
async def test_minimal_report_marks_unrequested_legs(tmp_path):
    client, rpc, scout, store = _parts(tmp_path=tmp_path)
    async with client:
        out = await counterparty.run_counterparty(
            client, rpc, scout, store, pay_to=ADDR, secret="test-secret"
        )
    assert sorted(out["legs_not_requested"]) == [
        "listsmith", "locksmith", "token_verdict",
    ]
    assert "wallet" in out["legs_ok"] and "index" in out["legs_ok"]


@pytest.mark.asyncio
async def test_failed_remote_leg_is_marked_not_fatal(tmp_path):
    client, rpc, scout, store = _parts(
        fail_hosts={"lock-smith-production.up.railway.app"}, tmp_path=tmp_path
    )
    async with client:
        out = await counterparty.run_counterparty(
            client, rpc, scout, store,
            pay_to=ADDR,
            service_url="https://a.example.com/v1/verdict",
            secret="test-secret",
        )
    assert out["legs_failed"] == ["locksmith"]
    assert out["legs"]["locksmith"]["status"] == "failed"
    assert "wallet" in out["legs_ok"]


@pytest.mark.asyncio
async def test_total_failure_raises(tmp_path, monkeypatch):
    client, rpc, scout, store = _parts(tmp_path=tmp_path, with_index=False)

    async def _boom(*a, **k):
        raise RuntimeError("chain reads down")

    monkeypatch.setattr(counterparty.reads_svc, "wallet_snapshot", _boom)
    async with client:
        with pytest.raises(UpstreamError):
            await counterparty.run_counterparty(
                client, rpc, scout, store, pay_to=ADDR, secret="test-secret"
            )


@pytest.mark.asyncio
async def test_empty_index_store_is_unavailable_not_fatal(tmp_path):
    client, rpc, scout, store = _parts(tmp_path=tmp_path, with_index=False)
    async with client:
        out = await counterparty.run_counterparty(
            client, rpc, scout, store, pay_to=ADDR, secret="test-secret"
        )
    assert out["legs"]["index"]["status"] == "unavailable"
    assert out["legs_unavailable"] == ["index"]
    assert "wallet" in out["legs_ok"]
