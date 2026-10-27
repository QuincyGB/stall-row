"""Orchestrate: fan-out semantics — partial reports mark failed
legs; only a total failure raises (so the paid call 502s unsettled).
Sibling traffic is faked with httpx MockTransport; the secret must
ride in the x-fleet-internal-secret header on every leg."""

import httpx
import pytest

from app.rpc import UpstreamError
from app.services import orchestrate

ARGS = dict(
    address="0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    chain="base",
    service_url="https://stall.example/v1/call",
    listing_url="https://stall.example/.well-known/x402",
    secret="test-secret",
)

OK_BODIES = {
    "/v1/internal/verdict": {"verdict": "SOLID", "score": 12.0},
    "/v1/internal/check": {"verdict": "as_advertised"},
    "/v1/internal/audit": {"verdict": "SECURE"},
    "/v1/internal/grade": {"grade": "A"},
}


def _client(fail_paths=(), seen_headers=None):
    def handler(request):
        if seen_headers is not None:
            seen_headers.append(request.headers.get("x-fleet-internal-secret"))
        if request.url.path in fail_paths:
            return httpx.Response(502, text="sibling down")
        return httpx.Response(200, json=OK_BODIES[request.url.path])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_all_legs_ok_with_secret_header():
    seen = []
    out = await orchestrate.run_orchestrate(_client(seen_headers=seen), **ARGS)
    assert out["legs_failed"] == []
    assert sorted(out["legs_ok"]) == ["listsmith", "locksmith", "route_verdict", "token_verdict"]
    assert out["legs"]["token_verdict"]["headline"] == "SOLID (risk 12.0)"
    assert out["legs"]["listsmith"]["headline"] == "grade A"
    assert seen == ["test-secret"] * 4


@pytest.mark.asyncio
async def test_partial_failure_marks_the_leg():
    out = await orchestrate.run_orchestrate(
        _client(fail_paths={"/v1/internal/audit"}), **ARGS
    )
    assert out["legs_failed"] == ["locksmith"]
    assert out["legs"]["locksmith"]["status"] == "failed"
    assert "502" in out["legs"]["locksmith"]["error"]
    assert len(out["legs_ok"]) == 3


@pytest.mark.asyncio
async def test_total_failure_raises_upstream():
    client = _client(fail_paths=set(OK_BODIES))
    with pytest.raises(UpstreamError):
        await orchestrate.run_orchestrate(client, **ARGS)


@pytest.mark.asyncio
async def test_transport_error_is_a_failed_leg_not_a_crash():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError):  # all four legs refused = total failure
        await orchestrate.run_orchestrate(client, **ARGS)
