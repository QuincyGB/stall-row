"""One-link probe: the free single-route form of Fleet Check.

Pins:
  * a healthy 402 route classifies ok through Fleet Check's own
    sweep/_classify path (no second classifier);
  * a 403 route classifies as a defect named unexpected_status;
  * a private/loopback target is refused 422 before any probing
    (the fleet SSRF guard, reused);
  * the per-IP rate limit answers 429 once the minute window fills;
  * CORS is granted to the GitHub Pages origin on /v1/probe ONLY —
    other origins get nothing, other routes get nothing;
  * /v1/probe is NOT a priced route: absent from PRICE_UNITS, the
    discovery manifest, and the paywall (an unpaid call is served,
    not 402'd).
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.services import probe as probe_svc
from app.services import recheck
from tests.test_fleetcheck import ORIGIN, VERDICT, fleet_handler

PAGES_ORIGIN = "https://quincygb.github.io"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def http_env(monkeypatch):
    monkeypatch.setattr(probe_svc, "RATE_LIMITER", probe_svc._RateLimiter())
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fleet_handler()))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return TestClient(main.app, raise_server_exceptions=False)


# --- classification (service level, Fleet Check machinery) -----------------------

@pytest.mark.asyncio
async def test_probe_one_ok_on_healthy_402():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fleet_handler())
    ) as client:
        out = await probe_svc.probe_one(client, VERDICT)
    assert out["url"] == VERDICT
    assert out["status"] == "ok"
    assert out["observed_status"] == 402
    assert out["defect_class"] is None
    assert out["verdict"] == "OK"
    assert out["checked_at"]


@pytest.mark.asyncio
async def test_probe_one_defect_on_403_route():
    def handler(request):
        if request.url.path == "/.well-known/x402":
            return httpx.Response(404, text="none")
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        out = await probe_svc.probe_one(client, ORIGIN + "/v1/blocked")
    assert out["status"] == "defect"
    assert out["observed_status"] == 403
    assert out["defect_class"] == "unexpected_status"
    assert "403" in out["detail"]


@pytest.mark.asyncio
async def test_probe_one_origin_expands_priced_routes():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fleet_handler())
    ) as client:
        out = await probe_svc.probe_one(client, ORIGIN)
    assert out["status"] == "ok"
    assert out["summary"]["resources"] == 2
    assert len(out["resources"]) == 2


# --- HTTP surface ------------------------------------------------------------------

def test_http_probe_ok(http_env):
    r = http_env.post("/v1/probe", json={"url": VERDICT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["observed_status"] == 402
    assert body["defect_class"] is None


def test_http_probe_defect(http_env):
    r = http_env.post("/v1/probe", json={"url": ORIGIN + "/v1/missing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "defect"
    assert body["observed_status"] == 404
    assert body["defect_class"] == "unexpected_status"


def test_http_probe_ssrf_target_rejected(http_env):
    for url in ("https://127.0.0.1/v1/x", "http://localhost:3000/v1/x",
                "ftp://93.184.216.34/v1/x"):
        r = http_env.post("/v1/probe", json={"url": url})
        assert r.status_code == 422, (url, r.text)


def test_http_probe_rate_limit_trips(http_env, monkeypatch):
    monkeypatch.setenv("STALLROW_PROBE_RATE_PER_MIN", "3")
    for _ in range(3):
        r = http_env.post("/v1/probe", json={"url": VERDICT})
        assert r.status_code == 200, r.text
    r = http_env.post("/v1/probe", json={"url": VERDICT})
    assert r.status_code == 429


def test_cors_header_for_pages_origin_only(http_env):
    r = http_env.post(
        "/v1/probe", json={"url": VERDICT}, headers={"Origin": PAGES_ORIGIN}
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == PAGES_ORIGIN

    r = http_env.post(
        "/v1/probe", json={"url": VERDICT},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers

    # Scoped to the probe route: /health gets no CORS grant.
    r = http_env.get("/health", headers={"Origin": PAGES_ORIGIN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_for_pages_origin(http_env):
    r = http_env.options(
        "/v1/probe",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == PAGES_ORIGIN
    assert "POST" in (r.headers.get("access-control-allow-methods") or "")


def test_probe_is_not_a_priced_route(http_env):
    assert "/v1/probe" not in PRICE_UNITS
    body = http_env.get("/.well-known/x402").json()
    urls = [e.get("resource") or "" for e in body.get("resources", [])]
    assert not any("/v1/probe" in u for u in urls)
    # Unpaid callers are served, not paywalled.
    r = http_env.post("/v1/probe", json={"url": VERDICT})
    assert r.status_code == 200


# --- batch mode -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_many_mixed_results():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fleet_handler())
    ) as client:
        out = await probe_svc.probe_many(
            client, [VERDICT, ORIGIN + "/v1/missing", VERDICT]
        )
    # duplicates collapse; order preserved
    assert [r["url"] for r in out["results"]] == [VERDICT, ORIGIN + "/v1/missing"]
    assert out["results"][0]["status"] == "ok"
    assert out["results"][1]["status"] == "defect"
    assert out["results"][1]["defect_class"] == "unexpected_status"
    assert out["summary"] == {"total": 2, "ok": 1, "defect": 1,
                              "refused": 0, "error": 0}


@pytest.mark.asyncio
async def test_probe_many_refused_item_is_a_row_not_a_failure():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fleet_handler())
    ) as client:
        out = await probe_svc.probe_many(
            client, ["https://127.0.0.1/v1/x", VERDICT]
        )
    assert out["results"][0]["status"] == "refused"
    assert out["results"][0]["defect_class"] == "target_refused"
    assert out["results"][1]["status"] == "ok"
    assert out["summary"]["refused"] == 1


@pytest.mark.asyncio
async def test_probe_many_rejects_empty_and_oversized():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fleet_handler())
    ) as client:
        with pytest.raises(ValueError):
            await probe_svc.probe_many(client, [])
        with pytest.raises(ValueError):
            await probe_svc.probe_many(
                client, [f"https://x{i}.example.com/v1/r" for i in range(11)]
            )


def test_http_probe_batch_ok(http_env):
    r = http_env.post(
        "/v1/probe/batch",
        json={"urls": [VERDICT, ORIGIN + "/v1/missing"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total"] == 2
    assert body["results"][0]["status"] == "ok"
    assert body["results"][1]["defect_class"] == "unexpected_status"


def test_http_probe_batch_rejects_empty_and_oversized(http_env):
    r = http_env.post("/v1/probe/batch", json={"urls": []})
    assert r.status_code == 422, r.text
    r = http_env.post(
        "/v1/probe/batch",
        json={"urls": [f"https://x{i}.example.com/v1/r" for i in range(11)]},
    )
    assert r.status_code == 422, r.text


def test_http_probe_batch_rate_limit_trips(http_env, monkeypatch):
    monkeypatch.setenv("STALLROW_PROBE_BATCH_RATE_PER_MIN", "1")
    r = http_env.post("/v1/probe/batch", json={"urls": [VERDICT]})
    assert r.status_code == 200, r.text
    r = http_env.post("/v1/probe/batch", json={"urls": [VERDICT]})
    assert r.status_code == 429


def test_probe_batch_is_not_a_priced_route(http_env):
    assert "/v1/probe/batch" not in PRICE_UNITS
    body = http_env.get("/.well-known/x402").json()
    urls = [e.get("resource") or "" for e in body.get("resources", [])]
    assert not any("/v1/probe/batch" in u for u in urls)


def test_cors_preflight_for_batch_path(http_env):
    r = http_env.options(
        "/v1/probe/batch",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == PAGES_ORIGIN
