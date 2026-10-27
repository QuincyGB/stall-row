"""Agent-Card Verifier: ERC-8004 registry resolution (agentURI via
eth_call), card fetch under SSRF discipline, and the live-reality
checks — declared endpoints probed, challenge payTo vs declared
wallets, declared prices vs live amounts. Undeclared fields report
not_checked; an unreachable card raises (unsettled). Offline: one
MockTransport plays Base RPC and the card host."""

import base64
import json

import httpx
import pytest

from app.config import load_settings
from app.rpc import Rpc, UpstreamError
from app.services import agentcard

S = load_settings()
WALLET = "0x5555555555555555555555555555555555555555"
OTHER = "0x6666666666666666666666666666666666666666"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CARD_URL = "https://93.184.216.35/agent.json"
EP = "https://93.184.216.35/v1/service"


def _abi_string(value: str) -> str:
    raw = value.encode()
    pad = (32 - len(raw) % 32) % 32
    return (
        "0x"
        + (32).to_bytes(32, "big").hex()
        + len(raw).to_bytes(32, "big").hex()
        + (raw + b"\x00" * pad).hex()
    )


def _challenge(pay_to=WALLET, amount="20000"):
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": EP, "description": "svc", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": amount,
                "asset": USDC,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _client(card, *, card_status=200, challenge_pay_to=WALLET,
            challenge_amount="20000", rpc_down=False):
    def handler(request):
        host = request.url.host
        if host == "mainnet.base.org":
            if rpc_down:
                return httpx.Response(503, text="rpc down")
            body = json.loads(request.content)
            if body["method"] == "eth_call":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"],
                    "result": _abi_string(CARD_URL),
                })
            raise AssertionError(body["method"])
        if host == "93.184.216.35":
            if request.url.path == "/agent.json":
                if card_status != 200:
                    return httpx.Response(card_status, text="nope")
                if isinstance(card, str):
                    return httpx.Response(200, text=card)
                return httpx.Response(200, json=card)
            if request.url.path == "/v1/service":
                return httpx.Response(
                    402,
                    headers={
                        "payment-required": _challenge(
                            challenge_pay_to, challenge_amount
                        )
                    },
                )
        raise AssertionError(f"unmocked URL {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _card(**over):
    card = {
        "name": "Test Agent",
        "wallet": WALLET,
        "endpoints": [{"url": EP, "price": "20000"}],
    }
    card.update(over)
    return card


@pytest.mark.asyncio
async def test_registry_resolution_and_full_match():
    async with _client(_card()) as client:
        rpc = Rpc(client, S.rpc_url)
        out = await agentcard.verify_agent_card(client, rpc, agent_id=97809)
    assert out["card_url"] == CARD_URL
    assert out["source"] == "erc8004_registry"
    checks = out["checks"]
    assert checks["card_parses"]["status"] == "ok"
    assert checks["endpoints_answer"]["status"] == "ok"
    assert checks["payto_matches"]["status"] == "ok"
    assert checks["prices_match"]["status"] == "ok"
    assert out["overall"] == "no_issues_found"


@pytest.mark.asyncio
async def test_direct_url_and_payto_mismatch():
    async with _client(_card(), challenge_pay_to=OTHER) as client:
        rpc = Rpc(client, S.rpc_url)
        out = await agentcard.verify_agent_card(client, rpc, agent_url=CARD_URL)
    assert out["source"] == "direct_url"
    check = out["checks"]["payto_matches"]
    assert check["status"] == "issue"
    assert check["challenge_pay_tos"] == [OTHER]
    assert out["overall"] == "issues_found"


@pytest.mark.asyncio
async def test_price_mismatch_is_an_issue():
    async with _client(_card(), challenge_amount="99999") as client:
        rpc = Rpc(client, S.rpc_url)
        out = await agentcard.verify_agent_card(client, rpc, agent_url=CARD_URL)
    check = out["checks"]["prices_match"]
    assert check["status"] == "issue"
    assert check["comparisons"][0]["declared"] == "20000"
    assert check["comparisons"][0]["live"] == "99999"


@pytest.mark.asyncio
async def test_undeclared_fields_are_not_checked():
    card = {"name": "Bare", "url": EP}
    async with _client(card) as client:
        rpc = Rpc(client, S.rpc_url)
        out = await agentcard.verify_agent_card(client, rpc, agent_url=CARD_URL)
    assert out["checks"]["payto_matches"]["status"] == "not_checked"
    assert out["checks"]["prices_match"]["status"] == "not_checked"
    assert out["checks"]["endpoints_answer"]["status"] == "ok"


@pytest.mark.asyncio
async def test_unreachable_card_is_an_honest_error():
    async with _client(_card(), card_status=404) as client:
        rpc = Rpc(client, S.rpc_url)
        with pytest.raises(UpstreamError):
            await agentcard.verify_agent_card(client, rpc, agent_url=CARD_URL)


@pytest.mark.asyncio
async def test_unparseable_card_is_a_delivered_finding():
    async with _client("<html>not a card</html>") as client:
        rpc = Rpc(client, S.rpc_url)
        out = await agentcard.verify_agent_card(client, rpc, agent_url=CARD_URL)
    assert out["checks"]["card_parses"]["status"] == "issue"
    assert out["overall"] == "issues_found"


def test_declared_endpoints_shapes():
    card = {
        "url": "https://a.example.com/x",
        "endpoints": [
            "https://b.example.com/y",
            {"endpoint": "https://c.example.com/z", "amount": "100"},
            {"url": "https://a.example.com/x"},  # dupe of the top-level url
            {"url": "ftp://nope.example.com/x"},
        ],
    }
    out = agentcard.declared_endpoints(card)
    assert [e["url"] for e in out] == [
        "https://a.example.com/x",
        "https://b.example.com/y",
        "https://c.example.com/z",
    ]
    assert out[2]["declared_price"] == "100"
