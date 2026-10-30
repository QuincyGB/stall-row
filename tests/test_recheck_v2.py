"""Feed v2 + baselines + field-level diffs + change webhooks +
the unified timeline (the 2026-10-29 aicom improvements 1–5).

Fully offline: MockTransport plays targets and webhook receivers;
IP-literal URLs keep the SSRF guard's DNS lookup local.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import indexfeed, recheck, timeline

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
X402_URL = "https://93.184.216.34/v1/verdict"
HOOK_URL = "https://93.184.216.34/hook"
SECRET = "fleet-test-secret"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))
    monkeypatch.setenv("STALLROW_RECHECK_WEBHOOK_BACKOFF_S", "0,0")


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def http(store, monkeypatch):
    monkeypatch.setattr(main, "_recheck_store", store)
    return TestClient(main.app, raise_server_exceptions=False)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _challenge(amount="20000", pay_to=OWNER, schema_type="string"):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": X402_URL,
            "description": "A target service.",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {"type": "http", "method": "POST", "bodyType": "json", "body": {}},
                    "output": {
                        "type": "json",
                        "schema": {
                            "type": "object",
                            "properties": {"verdict": {"type": schema_type}},
                            "required": ["verdict"],
                        },
                    },
                }
            }
        },
    }


def _challenge_response(**kw) -> httpx.Response:
    challenge = _challenge(**kw)
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _record(store, watch_id="w1", url=X402_URL, amount="100", n=0):
    return store.record_check(
        {
            "watch_id": watch_id,
            "url": url,
            "kind": "x402",
            "status": "UNCHANGED",
            "changes": [],
            "field_changes": [],
            "current": {"amount": amount, "payTo": OWNER},
            "checked_at": f"2026-10-29T00:{n % 60:02d}:00Z",
            "summary": "synthetic",
        }
    )


# --- item 2: seq + hash chain ---------------------------------------------------


def test_chain_is_continuous_and_verifies(store):
    envs = [_record(store, n=i) for i in range(3)]
    assert [e["seq"] for e in envs] == [1, 2, 3]
    assert envs[0]["prev_hash"] == "genesis"
    assert envs[1]["prev_hash"] == envs[0]["record_hash"]
    assert envs[2]["prev_hash"] == envs[1]["record_hash"]
    assert all(e["record_id"].startswith("rec_") for e in envs)
    assert store.verify_chain() is True
    assert store.latest_seq() == 3


def test_backfill_is_deterministic_and_idempotent(tmp_path):
    path = str(tmp_path / "watches.db")
    store = recheck.WatchStore(path)
    _record(store, n=1)
    _record(store, n=2)
    before = [(e["seq"], e["record_hash"]) for e in store.feed_rows(ascending=True)]
    store.close()
    # Simulate a pre-v2 store: wipe the chain, keep the records.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE recheck_history SET seq=NULL, record_id=NULL, schema_version=NULL, prev_hash=NULL, record_hash=NULL")
    conn.execute("DELETE FROM feed_meta")
    conn.commit()
    conn.close()
    reopened = recheck.WatchStore(path)
    after = [(e["seq"], e["record_hash"]) for e in reopened.feed_rows(ascending=True)]
    assert after == before
    assert reopened.verify_chain() is True
    reopened.close()
    again = recheck.WatchStore(path)
    assert [(e["seq"], e["record_hash"]) for e in again.feed_rows(ascending=True)] == before


def test_cursor_pagination_over_100_records_has_no_gaps(store):
    for i in range(150):
        _record(store, n=i)
    page1 = store.feed_rows(since_seq=0, limit=100)
    page2 = store.feed_rows(since_seq=page1[-1]["seq"], limit=100)
    seqs = [e["seq"] for e in page1 + page2]
    assert seqs == list(range(1, 151))
    # filters
    assert store.feed_rows(watch_id="w1", url=X402_URL, limit=1000)
    assert store.feed_rows(watch_id="nobody") == []
    assert store.feed_rows(url="https://elsewhere.example/x") == []


def test_feed_endpoint_legacy_shape_and_v2_cursor(http, store):
    for i in range(3):
        _record(store, n=i)
    legacy = http.get("/v1/recheck/feed")
    assert legacy.status_code == 200
    assert legacy.headers["x-recheck-feed-latest-seq"] == "3"
    lines = [json.loads(l) for l in legacy.text.splitlines()]
    assert all("seq" not in l for l in lines)  # bare response objects
    assert [l["checked_at"] for l in lines] == [
        "2026-10-29T00:02:00Z",
        "2026-10-29T00:01:00Z",
        "2026-10-29T00:00:00Z",
    ]
    v2 = http.get("/v1/recheck/feed", params={"since_seq": 1})
    envs = [json.loads(l) for l in v2.text.splitlines()]
    assert [e["seq"] for e in envs] == [2, 3]
    assert envs[0]["record"]["checked_at"] == "2026-10-29T00:01:00Z"
    assert v2.headers["x-recheck-feed-first-seq"] == "2"
    assert v2.headers["x-recheck-feed-last-seq"] == "3"


def test_history_endpoint_and_stateless_compare(http, store):
    _record(store, amount="100", n=1)
    _record(store, amount="200", n=2)
    body = http.get(
        "/v1/recheck/history", params={"watch_id": "w1", "url": X402_URL}
    ).json()
    assert body["count"] == 2
    assert body["latest_seq"] == 2
    compared = http.get(
        "/v1/recheck/history",
        params={"watch_id": "w1", "url": X402_URL, "compare_seq": 1},
    ).json()
    fc = compared["comparison"]["field_changes"]
    assert fc == [
        {
            "path": "amount",
            "op": "value_changed",
            "before": "100",
            "after": "200",
            "severity": "medium",
            "category": "payment",
        }
    ]
    one = http.get(f"/v1/recheck/history/{body['records'][0]['record_id']}")
    assert one.status_code == 200
    assert one.json()["seq"] == 1
    assert http.get("/v1/recheck/history/rec_nope").status_code == 404


# --- item 4: field-level diffs through recheck() ---------------------------------


@pytest.mark.asyncio
async def test_recheck_field_changes_name_the_mutated_fields(store):
    state = {"amount": "20000", "pay_to": OWNER, "schema_type": "string"}

    def handler(request):
        return _challenge_response(**state)

    async with _client(handler) as client:
        first = await recheck.recheck(client, store, X402_URL)
        assert first["field_changes"] == []
        state["amount"] = "25000"
        state["pay_to"] = OTHER
        state["schema_type"] = "number"
        changed = await recheck.recheck(client, store, X402_URL)
    assert changed["status"] == "CHANGED"
    by_path = {fc["path"]: fc for fc in changed["field_changes"]}
    assert by_path["amount"]["before"] == "20000"
    assert by_path["amount"]["after"] == "25000"
    assert by_path["amount"]["category"] == "payment"
    assert by_path["payTo"]["severity"] == "high"
    assert by_path["schema_hash"]["before"] != by_path["schema_hash"]["after"]
    assert by_path["schema_hash"]["category"] == "schema"
    # legacy changes[] untouched in shape
    assert {c["field"] for c in changed["changes"]} == {
        "amount",
        "payTo",
        "schema_hash",
    }


# --- item 3: pins + imports --------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_governs_the_diff_until_unpinned(store):
    state = {"amount": "20000"}

    def handler(request):
        return _challenge_response(amount=state["amount"])

    async with _client(handler) as client:
        await recheck.recheck(client, store, X402_URL)  # seq 1, baseline
        state["amount"] = "25000"
        changed = await recheck.recheck(client, store, X402_URL)
        assert changed["status"] == "CHANGED"
        key = f"{recheck.default_watch_id(X402_URL)}\n{X402_URL}"
        assert store.pin_baseline(key, 1, source="test") is not None
        # Still the mutated state — against LATEST state this would be
        # UNCHANGED; against the pin it must report CHANGED again.
        again = await recheck.recheck(client, store, X402_URL)
        assert again["status"] == "CHANGED"
        assert store.unpin_baseline(key) is True
        settled = await recheck.recheck(client, store, X402_URL)
        assert settled["status"] == "UNCHANGED"


def _importable(schema=None):
    schema = schema or {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    fingerprint = {
        "status_code": 402,
        "amount": "20000",
        "asset": USDC,
        "network": "eip155:8453",
        "payTo": OWNER,
        "description_hash": None,
        "schema_hash": recheck._hash_text(json.dumps(schema, sort_keys=True)),
    }
    return fingerprint, schema


def test_import_validates_by_recomputation(store):
    fingerprint, schema = _importable()
    recheck.validate_imported_snapshot("x402", fingerprint, schema)
    key = f"imported-watch\n{X402_URL}"
    env = store.import_baseline(
        key, "x402", fingerprint, "2026-10-20T00:00:00Z", source="test-import"
    )
    assert env["record"]["imported"] is True
    assert env["record"]["provenance"]["source"] == "test-import"
    assert store.get(key)["fingerprint"] == fingerprint


def test_import_forged_schema_is_refused_loudly(store):
    fingerprint, schema = _importable()
    forged = {**schema, "properties": {"verdict": {"type": "number"}}}
    with pytest.raises(ValueError, match="does not hash"):
        recheck.validate_imported_snapshot("x402", fingerprint, forged)
    with pytest.raises(ValueError, match="must be supplied"):
        recheck.validate_imported_snapshot("x402", fingerprint, None)
    assert store.check_history_count() == 0


def test_internal_mutations_are_secret_gated(http, store, monkeypatch):
    monkeypatch.delenv("FLEET_INTERNAL_SECRET", raising=False)
    r = http.post(
        "/v1/internal/recheck/baseline/pin",
        json={"url": X402_URL, "seq": 1},
    )
    assert r.status_code == 404  # no secret configured, no endpoint
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", SECRET)
    r = http.post(
        "/v1/internal/recheck/baseline/pin",
        json={"url": X402_URL, "seq": 1},
        headers={"x-fleet-internal-secret": "wrong"},
    )
    assert r.status_code == 403
    _record(store, watch_id=recheck.default_watch_id(X402_URL), n=1)
    r = http.post(
        "/v1/internal/recheck/baseline/pin",
        json={"url": X402_URL, "seq": 1},
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert r.status_code == 200
    assert r.json()["pin"]["seq"] == 1
    fingerprint, schema = _importable()
    r = http.post(
        "/v1/internal/recheck/baseline/import",
        json={
            "url": X402_URL,
            "kind": "x402",
            "current": fingerprint,
            "checked_at": "2026-10-20T00:00:00Z",
            "source": "test",
            "schema": {**schema, "properties": {"verdict": {"type": "number"}}},
        },
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert r.status_code == 422  # forged: schema doesn't hash to the claim
    assert store.check_history_count() == 1  # nothing was stored


# --- item 5: change webhooks --------------------------------------------------------


def _watch_key(watch_id="hookwatch"):
    return f"{watch_id}\n{X402_URL}"


def _changed_envelope(store, watch_id="hookwatch"):
    return store.record_check(
        {
            "watch_id": watch_id,
            "url": X402_URL,
            "kind": "x402",
            "status": "CHANGED",
            "changes": [{"field": "amount", "before": "1", "after": "2"}],
            "field_changes": [
                {
                    "path": "amount",
                    "op": "value_changed",
                    "before": "1",
                    "after": "2",
                    "severity": "medium",
                    "category": "payment",
                }
            ],
            "current": {"amount": "2"},
            "checked_at": "2026-10-29T01:00:00Z",
            "summary": "Changed since the previous check: amount.",
        }
    )


@pytest.mark.asyncio
async def test_webhook_delivery_is_signed_and_recorded(store):
    store.set_change_webhook(_watch_key(), HOOK_URL, "whsec_test")
    envelope = _changed_envelope(store)
    delivery_id = recheck.queue_change_event(store, envelope)
    assert delivery_id is not None
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["sig"] = request.headers.get("x-recheck-signature")
        seen["event"] = request.headers.get("x-recheck-event-id")
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        out = await recheck.deliver_change_event(client, store, delivery_id)
    assert out["status"] == "delivered"
    expected = hmac.new(b"whsec_test", seen["body"], hashlib.sha256).hexdigest()
    assert seen["sig"] == f"sha256={expected}"
    payload = json.loads(seen["body"])
    assert payload["event"] == "recheck.changed"
    assert payload["field_changes"][0]["path"] == "amount"
    assert seen["event"] == payload["event_id"]
    row = store.get_change_delivery(delivery_id)
    assert row["status"] == "delivered"
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_webhook_dead_letter_after_bounded_retries(store):
    store.set_change_webhook(_watch_key(), HOOK_URL, "whsec_test")
    delivery_id = recheck.queue_change_event(store, _changed_envelope(store))

    def handler(request):
        return httpx.Response(500, text="no")

    async with _client(handler) as client:
        out = await recheck.deliver_change_event(client, store, delivery_id)
    assert out["status"] == "dead_letter"
    row = store.get_change_delivery(delivery_id)
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 3  # backoff list "0,0" -> 3 attempts
    assert row["last_http_status"] == 500


@pytest.mark.asyncio
async def test_webhook_ssrf_target_dies_without_any_http_attempt(store):
    store.set_change_webhook(
        _watch_key(), "https://169.254.169.254/hook", "whsec_test"
    )
    delivery_id = recheck.queue_change_event(store, _changed_envelope(store))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200)

    async with _client(handler) as client:
        out = await recheck.deliver_change_event(client, store, delivery_id)
    assert out["status"] == "dead_letter"
    assert calls["n"] == 0
    row = store.get_change_delivery(delivery_id)
    assert row["attempts"] == 1
    assert "SSRF" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_recheck_fires_only_on_changed(store):
    store.set_change_webhook(
        f"{recheck.default_watch_id(X402_URL)}\n{X402_URL}",
        HOOK_URL,
        "whsec_test",
    )
    state = {"amount": "20000"}
    hits = {"n": 0}

    def handler(request):
        if str(request.url).endswith("/hook"):
            hits["n"] += 1
            return httpx.Response(200, text="ok")
        return _challenge_response(amount=state["amount"])

    async with _client(handler) as client:
        await recheck.recheck(client, store, X402_URL)  # BASELINE
        await recheck.recheck(client, store, X402_URL)  # UNCHANGED
        assert store.list_change_deliveries() == []
        state["amount"] = "25000"
        await recheck.recheck(client, store, X402_URL)  # CHANGED
        for _ in range(50):
            rows = store.list_change_deliveries()
            if rows and rows[0]["status"] == "delivered":
                break
            await asyncio.sleep(0.02)
    rows = store.list_change_deliveries()
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert hits["n"] == 1


def test_webhook_config_endpoint_rejects_bad_targets(http, monkeypatch):
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", SECRET)
    headers = {"x-fleet-internal-secret": SECRET}
    r = http.post(
        "/v1/internal/recheck/webhooks",
        json={"url": X402_URL, "webhook_url": "http://93.184.216.34/hook", "secret": "s"},
        headers=headers,
    )
    assert r.status_code == 422  # not https
    r = http.post(
        "/v1/internal/recheck/webhooks",
        json={"url": X402_URL, "webhook_url": "https://169.254.169.254/hook", "secret": "s"},
        headers=headers,
    )
    assert r.status_code == 422  # SSRF-refused
    r = http.post(
        "/v1/internal/recheck/webhooks",
        json={"url": X402_URL, "webhook_url": HOOK_URL, "secret": "s3cr3t"},
        headers=headers,
    )
    assert r.status_code == 200
    assert "s3cr3t" not in r.text  # secret never echoed


# --- item 1: unified timeline ---------------------------------------------------


def _rv_history_handler(checks):
    def handler(request):
        if request.url.host == "route-verdict-production.up.railway.app":
            return httpx.Response(
                200, json={"resource_url": X402_URL, "count": len(checks), "checks": checks}
            )
        raise httpx.ConnectError("nope", request=request)

    return handler


@pytest.mark.asyncio
async def test_timeline_merges_all_three_sources(store, tmp_path):
    # Alex's watch, in the shape his Oct 29 records carry.
    store.record_check(
        {
            "watch_id": "modelmarket-survey",
            "url": "https://modelmarket.dev/x402/histor-check",
            "kind": "x402",
            "status": "CHANGED",
            "changes": [{"field": "schema_hash", "before": None, "after": "7cbf"}],
            "field_changes": [],
            "current": {"amount": "1000", "schema_hash": "7cbf"},
            "checked_at": "2026-10-29T19:04:42Z",
            "summary": "Changed since the previous check: schema_hash.",
        }
    )
    url = "https://modelmarket.dev/x402/histor-check"
    rv_checks = [
        {
            "resource_url": url,
            "checked_at": "2026-10-29T19:04:00Z",
            "verdict": "TRUSTED",
            "score": 95.0,
            "diff": {"contract_stability": "unknown"},
            "terms": {"amount_units": "1000"},
            "catalog": {},
        }
    ]

    def handler(request):
        return httpx.Response(
            200, json={"resource_url": url, "count": 1, "checks": rv_checks}
        )

    idx = indexfeed.IndexStore(str(tmp_path / "index.db"))
    idx.save_snapshot(
        [{"resource": url, "serviceName": "aicom", "amount": "1000"}],
        epoch=1760000000.0,
    )
    async with _client(handler) as client:
        out = await timeline.build_timeline(client, store, idx, url)
    assert out["sources"] == {
        "recheck": "ok",
        "route_verdict": "ok",
        "catalogue_index": "ok",
    }
    assert out["catalogue"]["entry"]["serviceName"] == "aicom"
    assert [e["source"] for e in out["events"]] == ["route_verdict", "recheck"]
    recheck_event = out["events"][1]
    assert recheck_event["status"] == "CHANGED"
    assert recheck_event["seq"] == 1


@pytest.mark.asyncio
async def test_timeline_degrades_honestly(store, tmp_path):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    idx = indexfeed.IndexStore(str(tmp_path / "index.db"))
    async with _client(handler) as client:
        out = await timeline.build_timeline(
            client, store, idx, "https://modelmarket.dev/x402/histor-check"
        )
    assert out["sources"]["route_verdict"] == "unavailable"
    assert out["sources"]["recheck"] == "no_records"
    assert out["sources"]["catalogue_index"] == "unavailable"
    assert out["events"] == []
    assert out["catalogue"] is None
