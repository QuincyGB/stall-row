"""Hosted MCP endpoint (Next-20 Item 2): the fleet's Item 4 tool pack
served over streamable-HTTP MCP at POST /mcp on StallRow.

Pins:
  * protocol shapes — initialize / ping / tools/list / notifications;
  * the served tool list EQUALS the Item 4 function-calling pack's 24
    tools (embedded copy at app/data/function-tools.json, and the
    canonical pack in the fleet-checker when it is on disk beside the
    repo), so registry, schema pack and MCP can never drift apart;
  * tools/call proxying with a mocked upstream — a funded call passes
    the result through and forwards the caller's own credentials, an
    unpaid call returns the structured 402 tool error carrying the
    decoded accepts (exact amount / payTo / network / asset), and a
    no-credential call still proxies and surfaces that 402;
  * /mcp is free transport: the x402 manifest's priced resources do
    not include it, and the manifest's extensions.mcp gains only the
    additive `hosted` entry (the stdio name/manifest stay put).
"""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, load_settings
from app.discovery import x402_manifest
from app.services import hosted_mcp

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE = "https://stall-row.example"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _rpc(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _post(client, payload, headers=None):
    return client.post("/mcp", json=payload, headers=headers or {})


# --- protocol shapes -----------------------------------------------------------


def test_initialize_negotiates_and_describes_the_server(client):
    r = _post(client, _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-host", "version": "0"},
    }))
    assert r.status_code == 200
    out = r.json()
    assert out["jsonrpc"] == "2.0" and out["id"] == 1
    result = out["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == hosted_mcp.SERVER_INFO["name"]
    assert result["capabilities"]["tools"] == {"listChanged": False}
    # The payment contract is stated up front, not discovered by failing.
    assert "never pays" in result["instructions"]


def test_initialize_falls_back_to_latest_for_unknown_version(client):
    r = _post(client, _rpc("initialize", {"protocolVersion": "1999-01-01"}))
    assert r.json()["result"]["protocolVersion"] == hosted_mcp.LATEST_PROTOCOL_VERSION


def test_notifications_are_accepted_silently(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert not r.content


def test_ping(client):
    r = _post(client, _rpc("ping"))
    assert r.json()["result"] == {}


def test_unknown_method_is_method_not_found(client):
    r = _post(client, _rpc("resources/list"))
    err = r.json()["error"]
    assert err["code"] == -32601


def test_get_and_delete_are_405(client):
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


def test_tools_list_shape(client):
    r = _post(client, _rpc("tools/list"))
    tools = r.json()["result"]["tools"]
    assert len(tools) == 24
    for t in tools:
        assert set(t) >= {"name", "description", "inputSchema"}
        assert t["inputSchema"]["type"] == "object"


# --- the pin: served tools == Item 4 pack ---------------------------------------


def test_served_tools_equal_embedded_pack():
    pack = hosted_mcp.PACK
    assert pack["tool_count"] == 24
    assert hosted_mcp.tools_list() == [
        {
            "name": e["name"],
            "description": e["description"],
            "inputSchema": e["input_schema"],
        }
        for e in pack["anthropic"]
    ]
    # Routes and tools are the same set, and the OpenAI rendering of
    # the pack carries the same names + parameters (no drift between
    # the pack's own two faces either).
    assert {r["name"] for r in pack["routes"]} == set(hosted_mcp.TOOL_BY_NAME)
    assert {
        f["function"]["name"]: f["function"]["parameters"] for f in pack["openai"]
    } == {t["name"]: t["inputSchema"] for t in hosted_mcp.TOOLS}
    # Every pack route is <= $0.05, per the pack's own cap.
    for r in pack["routes"]:
        assert int(r["amount_units_base"]) <= 50_000


def test_served_tools_equal_canonical_pack_when_present():
    canonical = (
        Path(__file__).resolve().parents[2]
        / "goals"
        / "x402-fleet-integration-into-repeat-monitoring-loops"
        / "hidden_files"
        / "fleet-checker"
        / "function-tools.json"
    )
    if not canonical.exists():
        pytest.skip("canonical fleet-checker pack not on disk beside the repo")
    pack = json.loads(canonical.read_text())
    assert {r["name"] for r in pack["routes"]} == set(hosted_mcp.TOOL_BY_NAME)
    assert [
        (e["name"], e["description"], e["input_schema"]) for e in pack["anthropic"]
    ] == [
        (t["name"], t["description"], t["inputSchema"]) for t in hosted_mcp.TOOLS
    ]
    assert {
        r["name"]: r["resource"] for r in pack["routes"]
    } == {
        name: r["resource"] for name, r in hosted_mcp.ROUTE_BY_NAME.items()
    }


# --- tools/call proxying ----------------------------------------------------------

GAS_CHALLENGE = {
    "x402Version": 2,
    "resource": {"url": "https://stall-row-production.up.railway.app/v1/gas-oracle", "method": "POST"},
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "1000",
            "asset": USDC,
            "payTo": OWNER,
            "maxTimeoutSeconds": 300,
        }
    ],
}


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call(client, payload, headers=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(main, "_fetch_client", lambda: client)
    return TestClient(main.app, raise_server_exceptions=False).post(
        "/mcp", json=payload, headers=headers or {}
    )


def test_tools_call_unpaid_returns_structured_402(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        raw = base64.b64encode(json.dumps(GAS_CHALLENGE).encode()).decode()
        return httpx.Response(402, headers={"payment-required": raw}, json=GAS_CHALLENGE)

    mock = _mock_client(handler)
    r = _call(
        mock,
        _rpc("tools/call", {"name": "stall_row_gas_oracle", "arguments": {}}),
        monkeypatch=monkeypatch,
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    structured = result["structuredContent"]
    assert structured["error"] == "payment_required"
    assert structured["status"] == 402
    assert structured["amount"] == "1000"
    assert structured["payTo"] == OWNER
    assert structured["network"] == "eip155:8453"
    assert structured["asset"] == USDC
    assert structured["accepts"] == GAS_CHALLENGE["accepts"]
    assert json.loads(result["content"][0]["text"]) == structured
    # It really proxied to the pack's resource URL with the arguments.
    assert seen["url"] == GAS_CHALLENGE["resource"]["url"]
    assert seen["body"] == {}
    # No credential was presented, so none was forwarded — the 402 is
    # surfaced for free price discovery.
    assert "payment-signature" not in seen["headers"]
    assert "x-fleet-pass" not in seen["headers"]


def test_tools_call_funded_passes_result_and_forwards_credentials(monkeypatch):
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"gas_price_gwei": "0.05", "chain": "base"})

    mock = _mock_client(handler)
    r = _call(
        mock,
        _rpc("tools/call", {"name": "stall_row_gas_oracle", "arguments": {}}),
        headers={
            "payment-signature": "signed-payload",
            "x-fleet-pass": "pass_abc",
            "x-unrelated": "must-not-leak",
        },
        monkeypatch=monkeypatch,
    )
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"gas_price_gwei": "0.05", "chain": "base"}
    assert seen["headers"]["payment-signature"] == "signed-payload"
    assert seen["headers"]["x-fleet-pass"] == "pass_abc"
    assert "x-unrelated" not in seen["headers"]


def test_tools_call_forwards_pack_headers_and_meta_credentials(monkeypatch):
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "BASELINE"})

    mock = _mock_client(handler)
    r = _call(
        mock,
        _rpc("tools/call", {
            "name": "stall_row_recheck",
            "arguments": {"url": "https://www.x402.org/"},
            "_meta": {"headers": {"x-recheck-pack": "pack_meta"}},
        }),
        headers={"x-toolkit-pack": "pack_http"},
        monkeypatch=monkeypatch,
    )
    assert r.json()["result"]["isError"] is False
    assert seen["headers"]["x-recheck-pack"] == "pack_meta"
    assert seen["headers"]["x-toolkit-pack"] == "pack_http"
    assert seen["body"] == {"url": "https://www.x402.org/"}


def test_tools_call_cross_stall_proxies_to_that_stalls_origin(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"verdict": "SAFE", "score": 90})

    mock = _mock_client(handler)
    r = _call(
        mock,
        _rpc("tools/call", {
            "name": "route_verdict_check",
            "arguments": {"resource_url": "https://www.x402.org/"},
        }),
        monkeypatch=monkeypatch,
    )
    assert r.json()["result"]["structuredContent"]["verdict"] == "SAFE"
    assert seen["url"] == "https://route-verdict-production.up.railway.app/v1/check"


def test_tools_call_unknown_tool_and_bad_arguments(client):
    r = _post(client, _rpc("tools/call", {"name": "nope", "arguments": {}}))
    assert r.json()["error"]["code"] == -32602
    r = _post(client, _rpc("tools/call", {"name": "stall_row_gas_oracle", "arguments": [1]}))
    assert r.json()["error"]["code"] == -32602


def test_tools_call_upstream_failure_is_an_honest_tool_error(monkeypatch):
    def handler(request):
        return httpx.Response(502, json={"error": "upstream down", "code": "x"})

    mock = _mock_client(handler)
    r = _call(
        mock,
        _rpc("tools/call", {"name": "stall_row_gas_oracle", "arguments": {}}),
        monkeypatch=monkeypatch,
    )
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["status"] == 502


def test_execute_tool_call_direct_async():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    async def go():
        return await hosted_mcp.execute_tool_call(
            "stall_row_gas_oracle", {}, {}, http_client=_mock_client(handler)
        )

    out = asyncio.run(go())
    assert out["isError"] is False
    assert out["structuredContent"] == {"ok": True}


# --- discovery: free transport, additive hosted entry ------------------------------


def test_mcp_is_not_a_priced_resource_and_manifest_gains_hosted_entry():
    settings = load_settings()
    manifest = x402_manifest(settings, BASE)
    resources = manifest["resources"]
    assert all("/mcp" not in r["resource"] for r in resources)
    assert "/mcp" not in PRICE_UNITS
    mcp = manifest["extensions"]["mcp"]
    # The stdio reference stays exactly where the registry compiler
    # expects it (name + manifest drive its server.json check).
    assert mcp["name"] == "io.github.QuincyGB/stall-row-mcp"
    assert mcp["transport"] == "stdio"
    assert mcp["manifest"].endswith("/server.json")
    hosted = mcp["hosted"]
    assert hosted["transport"] == "streamable-http"
    assert hosted["url"] == f"{BASE}/mcp"
    assert hosted["tools"] == 24
    assert hosted == hosted_mcp.hosted_extension(BASE)


def test_skill_and_llms_name_the_hosted_endpoint():
    settings = load_settings()
    from app.discovery import llms_txt, skill_md

    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert f"{BASE}/mcp" in doc
        assert "Hosted MCP" in doc
