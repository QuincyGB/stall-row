"""Improvements 6–10 (2026-10-29): fingerprint spec + vectors,
the mutable sandbox fixture, probe-quality metadata, catalogue
cross-checks frozen into records, and the corrections log.

Fully offline: MockTransport plays targets; IP-literal URLs keep
the SSRF guard's DNS lookup local.
"""

import base64
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import discovery
from app.services import fetch, fingerprint_spec, indexfeed, recheck, sandbox, timeline

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
X402_URL = "https://93.184.216.34/v1/verdict"
SANDBOX_URL = "https://93.184.216.34/v1/sandbox/x402"
SECRET = "fleet-test-" + "secret"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


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


def _record(store, watch_id="w1", url=X402_URL, amount="100"):
    return store.record_check(
        {
            "watch_id": watch_id,
            "url": url,
            "kind": "x402",
            "status": "UNCHANGED",
            "changes": [],
            "field_changes": [],
            "current": {"amount": amount, "payTo": OWNER},
            "checked_at": "2026-10-29T00:00:00Z",
            "summary": "synthetic",
        }
    )


# --- item 6: fingerprint spec + test vectors ------------------------------------


def test_spec_vectors_recompute_with_production_functions():
    vectors = fingerprint_spec.TEST_VECTORS
    assert len(vectors) >= 5
    for vector in vectors:
        inp = vector["input"]
        if vector["kind"] == "x402":
            got = recheck._x402_fingerprint(inp["status_code"], inp["challenge"])
        else:
            headers = httpx.Headers({"content-type": inp["content_type"]})
            body = inp["body_text"].encode("utf-8")
            got = recheck._web_fingerprint(inp["status_code"], headers, body)
            if "normalized_text" in vector:
                normalized = fetch._normalize_text(fetch._decode_body(body, headers))
                assert normalized == vector["normalized_text"]
        assert got == vector["expected"], vector["name"]


def test_fingerprint_spec_endpoint(http):
    r = http.get("/v1/recheck/fingerprint-spec")
    assert r.status_code == 200
    doc = r.json()
    assert doc["name"] == "stallrow-recheck-fingerprint"
    assert doc["hash"]["algorithm"] == "SHA-256"
    assert doc["test_vectors"] == fingerprint_spec.TEST_VECTORS
    assert "json.dumps(schema, sort_keys=True)" in doc["x402_fingerprint"]["canonicalization"]["schema_serialization"]


# --- item 7: the sandbox fixture --------------------------------------------------


def test_sandbox_challenge_shape_and_labelling(http):
    r = http.get("/v1/sandbox/x402")
    assert r.status_code == 402
    challenge = r.json()
    assert challenge == _decode_header(r)
    accept = challenge["accepts"][0]
    assert accept["amount"] == "5000"
    assert accept["payTo"] == main.settings.pay_to
    assert accept["network"] == "eip155:8453"
    schema = challenge["extensions"]["bazaar"]["info"]["output"]["schema"]
    assert schema["properties"]["verdict"]["type"] == "string"
    assert "TEST FIXTURE" in challenge["resource"]["description"]


def _decode_header(response):
    raw = response.headers["payment-required"]
    return json.loads(base64.b64decode(raw))


def test_sandbox_is_not_priced_or_listed():
    assert "/v1/sandbox/x402" not in discovery.ROUTES
    manifest = discovery.x402_manifest(main.settings, "https://stall-row.example")
    assert "sandbox" not in json.dumps(manifest)


def test_sandbox_mutate_gate(http, monkeypatch):
    monkeypatch.delenv("FLEET_INTERNAL_SECRET", raising=False)
    r = http.post("/v1/internal/sandbox/mutate", json={"op": "reset"})
    assert r.status_code == 404
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", SECRET)
    r = http.post(
        "/v1/internal/sandbox/mutate",
        json={"op": "set_price", "amount": "9000"},
        headers={"x-fleet-internal-secret": "wrong"},
    )
    assert r.status_code == 403
    r = http.post(
        "/v1/internal/sandbox/mutate",
        json={"op": "set_price", "amount": "9000"},
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert r.status_code == 200
    assert r.json()["sandbox"]["amount"] == "9000"
    r = http.post(
        "/v1/internal/sandbox/mutate",
        json={"op": "set_price", "amount": "abc"},
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert r.status_code == 422


def test_sandbox_mutation_validation(store):
    with pytest.raises(ValueError):
        sandbox.apply_mutation(store, "flip_schema_type", {"field": "nope"}, OWNER)
    with pytest.raises(ValueError):
        sandbox.apply_mutation(store, "set_payto", {"pay_to": "0x123"}, OWNER)
    with pytest.raises(ValueError):
        sandbox.apply_mutation(store, "remove_required", {"field": "score"}, OWNER)
    with pytest.raises(ValueError):
        sandbox.apply_mutation(store, "explode", {}, OWNER)
    # Nothing was stored by the refused mutations.
    assert store.get_sandbox_state() is None


@pytest.mark.asyncio
async def test_sandbox_recheck_change_cycle(store):
    def handler(request):
        state = sandbox.current_state(store, OWNER)
        challenge = sandbox.build_challenge(state, SANDBOX_URL)
        raw = base64.b64encode(json.dumps(challenge).encode()).decode()
        return httpx.Response(402, headers={"payment-required": raw}, json=challenge)

    async with _client(handler) as client:
        first = await recheck.recheck(client, store, SANDBOX_URL)
        assert first["status"] == "BASELINE"
        assert first["current"]["amount"] == "5000"
        baseline_schema_hash = first["current"]["schema_hash"]

        sandbox.apply_mutation(store, "flip_schema_type", {"field": "verdict"}, OWNER)
        second = await recheck.recheck(client, store, SANDBOX_URL)
        assert second["status"] == "CHANGED"
        assert second["current"]["schema_hash"] != baseline_schema_hash
        assert any(fc["path"] == "schema_hash" for fc in second["field_changes"])

        sandbox.apply_mutation(store, "set_price", {"amount": "9000"}, OWNER)
        third = await recheck.recheck(client, store, SANDBOX_URL)
        assert third["status"] == "CHANGED"
        assert {
            "path": "amount",
            "op": "value_changed",
            "before": "5000",
            "after": "9000",
            "severity": "medium",
            "category": "payment",
        } in third["field_changes"]

        sandbox.apply_mutation(store, "set_payto", {"pay_to": OTHER}, OWNER)
        fourth = await recheck.recheck(client, store, SANDBOX_URL)
        assert fourth["status"] == "CHANGED"
        assert any(
            fc["path"] == "payTo" and fc["after"] == OTHER and fc["severity"] == "high"
            for fc in fourth["field_changes"]
        )

        view = sandbox.apply_mutation(store, "reset", {}, OWNER)
        assert view == sandbox.state_view(sandbox.default_state(OWNER))
        fifth = await recheck.recheck(client, store, SANDBOX_URL)
        assert fifth["status"] == "CHANGED"  # terms moved back to defaults
        sixth = await recheck.recheck(client, store, SANDBOX_URL)
        assert sixth["status"] == "UNCHANGED"
        assert sixth["current"]["schema_hash"] == baseline_schema_hash


# --- item 8: probe-quality metadata -----------------------------------------------


@pytest.mark.asyncio
async def test_probe_metadata_direct_and_retry(store):
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503)
        return _challenge_response()

    async with _client(handler) as client:
        result = await recheck.recheck(client, store, X402_URL)
    probe = result["probe"]
    assert probe["attempts"] == 2
    assert probe["redirect_count"] == 0
    assert probe["error_class"] is None
    assert isinstance(probe["latency_ms"], int) and probe["latency_ms"] >= 0
    assert probe["flapping"] == {"flapping": False, "window": 1, "alternations": 0}
    # The recorded history object carries the same probe block.
    env = store.feed_rows(watch_id=result["watch_id"], url=X402_URL)[0]
    assert env["record"]["probe"] == probe


@pytest.mark.asyncio
async def test_probe_metadata_redirect(store):
    def handler(request):
        if request.url.path == "/v1/verdict":
            return httpx.Response(302, headers={"location": "/v1/verdict-final"})
        return _challenge_response()

    async with _client(handler) as client:
        result = await recheck.recheck(client, store, X402_URL)
    assert result["status"] == "BASELINE"
    assert result["probe"]["redirect_count"] == 1
    assert result["probe"]["attempts"] == 1


def test_assess_flapping_patterns():
    a, b = {"amount": "1"}, {"amount": "2"}
    assert recheck.assess_flapping([a, b, a])["flapping"] is True
    assert recheck.assess_flapping([a, b, a])["alternations"] == 1
    assert recheck.assess_flapping([a, b, a, b])["alternations"] == 2
    assert recheck.assess_flapping([a, a, b])["flapping"] is False
    assert recheck.assess_flapping([a])["window"] == 1


@pytest.mark.asyncio
async def test_flapping_flag_on_alternating_target(store):
    state = {"amount": "100"}

    def handler(request):
        return _challenge_response(amount=state["amount"])

    async with _client(handler) as client:
        await recheck.recheck(client, store, X402_URL)
        state["amount"] = "200"
        await recheck.recheck(client, store, X402_URL)
        state["amount"] = "100"
        third = await recheck.recheck(client, store, X402_URL)
    assert third["probe"]["flapping"]["flapping"] is True
    assert third["probe"]["flapping"]["window"] == 3


# --- item 9: catalogue cross-checks -------------------------------------------------


def _index_store(tmp_path, *items):
    tmp_path.mkdir(parents=True, exist_ok=True)
    idx = indexfeed.IndexStore(str(tmp_path / "index.db"))
    if items:
        rows = [indexfeed.normalize_item(i) for i in items]
        idx.save_snapshot(rows, epoch=1_700_000_000)
    return idx


def _catalogue_item(resource=X402_URL, amount="20000", pay_to=OWNER, network="eip155:8453"):
    return {
        "resource": resource,
        "serviceName": "Target",
        "description": "A target service.",
        "accepts": [
            {"scheme": "exact", "network": network, "amount": amount,
             "asset": USDC, "payTo": pay_to}
        ],
        "lastUpdated": "2026-10-29T00:00:00Z",
        "quality": {"l30DaysTotalCalls": 1, "l30DaysUniquePayers": 1},
    }


@pytest.mark.asyncio
async def test_crosscheck_match_mismatch_notlisted_unknown(store, tmp_path):
    def handler(request):
        return _challenge_response()

    async with _client(handler) as client:
        match_idx = _index_store(tmp_path / "a", _catalogue_item())
        result = await recheck.recheck(client, store, X402_URL, index_store=match_idx)
        cc = result["catalogue_crosscheck"]
        assert cc["listed"] is True and cc["verdict"] == "match"
        assert cc["fields"] == {"amount": "match", "payTo": "match", "network": "match"}
        assert cc["snapshot_id"] == 1 and cc["source"] == "cdp_index_snapshot"

        mismatch_idx = _index_store(tmp_path / "b", _catalogue_item(amount="99999"))
        result = await recheck.recheck(
            client, store, X402_URL, watch_id="w-mismatch", index_store=mismatch_idx
        )
        cc = result["catalogue_crosscheck"]
        assert cc["fields"]["amount"] == "mismatch" and cc["verdict"] == "mismatch"

        absent_idx = _index_store(
            tmp_path / "c", _catalogue_item(resource="https://93.184.216.34/other")
        )
        result = await recheck.recheck(
            client, store, X402_URL, watch_id="w-absent", index_store=absent_idx
        )
        cc = result["catalogue_crosscheck"]
        assert cc["listed"] is False and cc["verdict"] == "not_listed"
        assert set(cc["fields"].values()) == {"not_listed"}

        empty_idx = _index_store(tmp_path / "d")
        result = await recheck.recheck(
            client, store, X402_URL, watch_id="w-empty", index_store=empty_idx
        )
        cc = result["catalogue_crosscheck"]
        assert cc["listed"] == "unknown" and cc["verdict"] == "unknown"

        legacy = await recheck.recheck(client, store, X402_URL, watch_id="w-legacy")
        assert "catalogue_crosscheck" not in legacy


@pytest.mark.asyncio
async def test_crosscheck_is_frozen_at_check_time(store, tmp_path):
    idx = _index_store(tmp_path, _catalogue_item())

    def handler(request):
        return _challenge_response()

    async with _client(handler) as client:
        await recheck.recheck(client, store, X402_URL, index_store=idx)
    # A newer snapshot now disagrees; the recorded crosscheck must not move.
    idx.save_snapshot(
        [indexfeed.normalize_item(_catalogue_item(amount="1"))],
        epoch=1_700_000_100,
    )
    env = store.feed_rows(watch_id=recheck.default_watch_id(X402_URL), url=X402_URL)[0]
    assert env["record"]["catalogue_crosscheck"]["verdict"] == "match"


# --- item 10: corrections log -------------------------------------------------------


def _raw_record_json(store, record_id):
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            "SELECT response_json FROM recheck_history WHERE record_id = ?",
            (record_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


def test_correction_store_level(store):
    env = _record(store)
    before = _raw_record_json(store, env["record_id"])
    correction = store.add_correction(
        env["record_id"],
        "amount was recorded in the wrong units",
        {"current.amount": "100000"},
        source="test",
    )
    assert correction["correction_id"].startswith("cor_")
    assert correction["record_hash"] == env["record_hash"]
    # The original record's stored bytes are untouched; the chain verifies.
    assert _raw_record_json(store, env["record_id"]) == before
    assert store.verify_chain() is True
    # …and every envelope read surfaces the amendment beside the record.
    reread = store.record_by_id(env["record_id"])
    assert reread["corrections"] == [correction]
    assert reread["record"] == env["record"]
    assert store.add_correction("rec_missing", "x", {"a": 1}) is None


def test_corrections_http_flow(http, store, monkeypatch):
    env = _record(store)
    monkeypatch.delenv("FLEET_INTERNAL_SECRET", raising=False)
    r = http.post(
        "/v1/internal/recheck/corrections",
        json={"record_id": env["record_id"], "reason": "typo",
              "corrected_fields": {"summary": "corrected"}},
    )
    assert r.status_code == 404
    monkeypatch.setenv("FLEET_INTERNAL_SECRET", SECRET)
    r = http.post(
        "/v1/internal/recheck/corrections",
        json={"record_id": env["record_id"], "reason": "typo",
              "corrected_fields": {"summary": "corrected"}},
        headers={"x-fleet-internal-secret": "wrong"},
    )
    assert r.status_code == 403
    r = http.post(
        "/v1/internal/recheck/corrections",
        json={"record_id": env["record_id"], "reason": "summary misstated the change",
              "corrected_fields": {"summary": "corrected"}, "source": "test"},
        headers={"x-fleet-internal-secret": SECRET},
    )
    assert r.status_code == 200
    correction = r.json()["correction"]

    r = http.get("/v1/recheck/corrections")
    assert r.status_code == 200 and r.json()["count"] == 1
    assert r.json()["corrections"][0] == correction
    r = http.get("/v1/recheck/corrections", params={"record_id": "rec_nope"})
    assert r.json()["count"] == 0

    # The default (legacy) feed line for the corrected record carries
    # the amendment; uncorrected records keep the legacy shape.
    _record(store, watch_id="w2")
    lines = [json.loads(ln) for ln in http.get("/v1/recheck/feed").text.splitlines() if ln]
    corrected = [ln for ln in lines if ln["watch_id"] == "w1"]
    plain = [ln for ln in lines if ln["watch_id"] == "w2"]
    assert corrected[0]["corrections"] == [correction]
    assert "corrections" not in plain[0]

    # The v2 envelope + single-record read surface it too.
    r = http.get(f"/v1/recheck/history/{env['record_id']}")
    assert r.json()["corrections"] == [correction]
    assert store.verify_chain() is True


@pytest.mark.asyncio
async def test_corrections_surface_in_timeline(store, tmp_path):
    env = _record(store)
    store.add_correction(env["seq"], "reason", {"summary": "fixed"}, source="test")

    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    idx = indexfeed.IndexStore(str(tmp_path / "index.db"))
    async with _client(handler) as client:
        out = await timeline.build_timeline(client, store, idx, X402_URL)
    events = [e for e in out["events"] if e["source"] == "recheck"]
    assert events and events[0]["corrections"][0]["reason"] == "reason"
    assert out["sources"]["route_verdict"] == "unavailable"
