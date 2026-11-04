"""Sandbox twins (Next-20 #19): every priced route has a free twin at
/sandbox<path> — same validation, marked fixture delivery, no payment
leg, no loop block, no payer-memory record."""

import pytest
from fastapi.testclient import TestClient

from app.main import PRICE_UNITS, app
from app.services.sandbox_twins import _body_model

client = TestClient(app)

ADDR = "0x2821d96B5FC81aEC3763340b375a846a89A3Ec1b"
URL = "https://example.com/v1/verdict"
PRICED = sorted(PRICE_UNITS)


def _model_for(path):
    for route in app.routes:
        if getattr(route, "path", None) == path and "POST" in (route.methods or set()):
            return _body_model(route.endpoint)
    raise AssertionError(f"no priced route {path}")


def valid_payload(path):
    model = _model_for(path)
    if model is None:
        return {}
    body = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        ann = field.annotation
        if "list" in str(ann):
            body[name] = [ADDR]
        elif ann is str or "str" in str(ann):
            if "url" in name or "resource" in name or "endpoint" in name:
                body[name] = URL
            elif "address" in name or "wallet" in name or "spender" in name or "pay" in name:
                body[name] = ADDR
            else:
                body[name] = "sandbox"
        else:
            body[name] = {}
    return body


def test_sandbox_index_lists_every_priced_route():
    resp = client.get("/sandbox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sandbox"] is True
    assert body["count"] == len(PRICED)
    assert sorted(t["route"] for t in body["twins"]) == PRICED
    assert all(t["sandbox_route"] == "/sandbox" + t["route"] for t in body["twins"])
    assert all(t["price"] == "free" for t in body["twins"])


def test_every_priced_route_has_a_registered_twin():
    paths = {getattr(r, "path", None) for r in app.routes}
    for path in PRICED:
        assert "/sandbox" + path in paths


@pytest.mark.parametrize("path", PRICED)
def test_twin_returns_marked_fixture_with_no_payment_leg(path):
    resp = client.post("/sandbox" + path, json=valid_payload(path))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sandbox"] is True
    assert body["live"] is False
    assert body["sandbox_of"] == f"POST {path}"
    assert "SANDBOX TWIN" in body["sandbox_note"]
    assert body["sandbox_scenario"] == "normal"
    # No loop block: the delivery-layer attach keys off PRICE_UNITS,
    # and a sandbox delivery is not a paid delivery.
    assert "verdict_row_loop" not in body


@pytest.mark.parametrize("path", PRICED)
def test_twin_validation_matches_the_real_route(path):
    model = _model_for(path)
    if model is None or not any(f.is_required() for f in model.model_fields.values()):
        pytest.skip("route has no required body fields")
    resp = client.post("/sandbox" + path, json={})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    # Same fleet validation shape the priced route's handler produces.
    assert body["code"] == "validation_error"
    required = [n for n, f in model.model_fields.items() if f.is_required()]
    # The fleet validation formatter strips the reserved loc names
    # (body/query/path) — a field literally named e.g. "query" is
    # reported as 'request' on the real route too; parity is the
    # 422 + shape, so only assert names the formatter can surface.
    displayable = [n for n in required if n not in ("body", "query", "path")]
    if displayable:
        assert any(name in body["error"] for name in displayable)


def test_twin_unknown_scenario_is_a_validation_error():
    path = PRICED[0]
    resp = client.post(
        "/sandbox" + path,
        json=valid_payload(path),
        headers={"X-Sandbox-Scenario": "bogus"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_priced_manifest_is_unchanged_by_twins():
    resp = client.get("/.well-known/x402")
    assert resp.status_code == 200
    from urllib.parse import urlparse

    resources = resp.json()["resources"]
    priced = [r for r in resources if urlparse(r["resource"]).path in PRICE_UNITS]
    assert len(priced) == len(PRICED)
    assert not any("/sandbox" in r["resource"] for r in resources)


def test_llms_and_skill_advertise_sandbox_without_pricing_it():
    for doc in ("/llms.txt", "/skill.md"):
        text = client.get(doc).text
        assert "/sandbox" in text


from app.services.sandbox_twins import DRIFTED_SCHEMA_HASH, FLAGGED_PAYTO, ROTATED_PAYTO
from app import main as _main


def _payer_history_rows() -> int:
    store = _main._recheck_store
    with store._lock:
        return store._conn.execute("SELECT COUNT(*) FROM payer_history").fetchone()[0]


def test_twins_write_no_payer_history_and_debit_nothing():
    before = _payer_history_rows()
    # Even presenting funding credentials, a twin is free and records
    # nothing: the credentials are ignored, not debited.
    resp = client.post(
        "/sandbox/v1/recheck",
        json={"url": URL},
        headers={"X-Recheck-Pack": "pack_that_does_not_exist", "X-Fleet-Pass": "pass_nope"},
    )
    assert resp.status_code == 200
    assert "verdict_row_loop" not in resp.json()
    for path in PRICED:
        r = client.post("/sandbox" + path, json=valid_payload(path))
        assert r.status_code == 200, (path, r.text)
    assert _payer_history_rows() == before


def test_recheck_rotated_payto_scenario():
    body = client.post(
        "/sandbox/v1/recheck",
        json={"url": URL},
        headers={"X-Sandbox-Scenario": "rotated_payto"},
    ).json()
    assert body["sandbox_scenario_applied"] is True
    assert body["current"]["payTo"] == ROTATED_PAYTO
    assert body["status"] == "CHANGED"
    assert any(c.get("field") == "payTo" for c in body["field_changes"])


def test_recheck_drifted_schema_scenario():
    body = client.post(
        "/sandbox/v1/recheck",
        json={"url": URL},
        headers={"X-Sandbox-Scenario": "drifted_schema"},
    ).json()
    assert body["sandbox_scenario_applied"] is True
    assert body["current"]["schema_hash"] == DRIFTED_SCHEMA_HASH


def test_sign_read_rotated_payto_scenario():
    body = client.post(
        "/sandbox/v1/sign-read",
        json={"url": URL},
        headers={"X-Sandbox-Scenario": "rotated_payto"},
    ).json()
    assert body["sandbox_scenario_applied"] is True
    assert body["accepts"][0]["payTo"] == ROTATED_PAYTO


def test_guard_flagged_wallet_scenario():
    body = client.post(
        "/sandbox/v1/guard",
        json={"url": URL},
        headers={"X-Sandbox-Scenario": "flagged_wallet"},
    ).json()
    assert body["sandbox_scenario_applied"] is True
    assert body["terms"]["payTo"] == FLAGGED_PAYTO
    assert body["payto_flag"]["flagged"] is True
    assert body["verdict"] == "CAUTION"  # fixture verdict, unchanged


def test_pre_trade_guard_flagged_wallet_scenario():
    body = client.post(
        "/sandbox/v1/pre-trade-guard",
        json={"resource": URL, "pay_to": ADDR},
        headers={"X-Sandbox-Scenario": "flagged_wallet"},
    ).json()
    assert body["sandbox_scenario_applied"] is True
    assert body["pay_to"] == FLAGGED_PAYTO


def test_tube_search_rotated_payto_scenario_does_not_apply():
    body = client.post(
        "/sandbox/v1/tube-search",
        json={"query": "sandbox"},
        headers={"X-Sandbox-Scenario": "rotated_payto"},
    ).json()
    assert body["sandbox_scenario_applied"] is False
    assert "does not apply" in body["sandbox_scenario_note"]
