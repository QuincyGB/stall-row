"""Grade My Stall: the seller report card. Every check is a live
read or says unverified — the probe target, the origin's well-known
doc, and Settled's MCP are all MockTransport fakes; the CDP check
reads a crafted Index snapshot. Fully offline."""

import base64
import json

import httpx
import pytest

from app.services import gradestall, indexfeed

SELLER = "0x3333333333333333333333333333333333333333"
OTHER = "0x4444444444444444444444444444444444444444"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RES = "https://93.184.216.35/v1/thing"
ORIGIN = "https://93.184.216.35"


def _challenge(description="A fine verdict service", pay_to=SELLER):
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": RES, "description": description, "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": "20000",
                "asset": USDC,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _settled_handler(request, *, down=False, payload=None):
    if down:
        raise httpx.ConnectError("settled unreachable", request=request)
    body = json.loads(request.content)
    method = body.get("method")
    if method == "initialize":
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
        })
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/call":
        data = payload or {
            "url": RES,
            "status": "live",
            "payability": {"verdict": "delivered"},
            "delivery_verified": True,
            "quality": {"total": 88},
        }
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"structuredContent": data, "isError": False},
        })
    raise AssertionError(f"unexpected MCP method {method}")


def _client(*, description="A fine verdict service", pay_to=SELLER,
            well_known=None, settled_down=False, probe_status=402):
    def handler(request):
        host = request.url.host
        if host == "settled.tools":
            return _settled_handler(request, down=settled_down)
        if host == "93.184.216.35":
            path = request.url.path
            if path == "/v1/thing":
                if probe_status == 402:
                    return httpx.Response(
                        402, headers={"payment-required": _challenge(description, pay_to)}
                    )
                return httpx.Response(probe_status, json={"ok": True})
            if path == "/.well-known/x402":
                doc = (
                    {"resources": [{"url": RES, "proof": "sig-abc"}]}
                    if well_known is None
                    else well_known
                )
                return httpx.Response(200, json=doc)
            if path == "/openapi.json":
                return httpx.Response(404, json={})
        raise AssertionError(f"unmocked URL {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _store(tmp_path, *, listed=True):
    store = indexfeed.IndexStore(str(tmp_path / "idx.db"))
    rows = []
    if listed:
        rows.append({
            "resource": RES,
            "serviceName": "thing",
            "description": "d",
            "amount": "20000",
            "asset": USDC,
            "network": "eip155:8453",
            "payTo": SELLER,
            "lastUpdated": None,
            "calls30d": 42,
            "payers30d": 7,
        })
    store.save_snapshot(rows, epoch=1_000_000)
    return store


@pytest.mark.asyncio
async def test_happy_path_all_checks(tmp_path):
    async with _client() as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path), resource_url=RES, pay_to=SELLER
        )
    checks = out["checks"]
    assert checks["answers_402"]["status"] == "ok"
    assert checks["description"]["status"] == "ok"
    assert checks["cdp_listing"]["status"] == "ok"
    assert checks["cdp_listing"]["calls30d"] == 42
    assert checks["settled"]["status"] == "ok"
    assert checks["settled"]["quality_total"] == 88
    assert checks["x402scan"]["status"] == "ok"
    assert checks["payai"]["status"] == "unverified"  # never guessed
    assert checks["payto_consistency"]["status"] == "ok"
    assert out["summary"] == {"ok": 6, "issue": 0, "not_found": 0, "unverified": 1}
    assert out["overall"] == "no_issues_found"


@pytest.mark.asyncio
async def test_long_description_is_an_issue(tmp_path):
    async with _client(description="x" * 501) as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path), resource_url=RES, pay_to=SELLER
        )
    assert out["checks"]["description"]["status"] == "issue"
    assert out["checks"]["description"]["length"] == 501
    assert out["overall"] == "issues_found"


@pytest.mark.asyncio
async def test_not_listed_and_no_proof(tmp_path):
    async with _client(well_known={"resources": [{"url": RES}]}) as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path, listed=False), resource_url=RES
        )
    assert out["checks"]["cdp_listing"]["status"] == "not_found"
    assert out["checks"]["x402scan"]["status"] == "issue"  # declared, no proof
    assert "proof" in out["checks"]["x402scan"]["detail"]


@pytest.mark.asyncio
async def test_settled_down_is_unverified_not_guessed(tmp_path):
    async with _client(settled_down=True) as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path), resource_url=RES, pay_to=SELLER
        )
    assert out["checks"]["settled"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_payto_mismatch_is_an_issue(tmp_path):
    async with _client() as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path), resource_url=RES, pay_to=OTHER
        )
    check = out["checks"]["payto_consistency"]
    assert check["status"] == "issue"
    assert check["sources"]["supplied"] == OTHER
    assert check["sources"]["challenge"] == SELLER


@pytest.mark.asyncio
async def test_open_endpoint_is_an_issue(tmp_path):
    async with _client(probe_status=200) as client:
        out = await gradestall.grade_stall(
            client, _store(tmp_path), resource_url=RES
        )
    assert out["checks"]["answers_402"]["status"] == "issue"
    assert out["checks"]["description"]["status"] == "unverified"
