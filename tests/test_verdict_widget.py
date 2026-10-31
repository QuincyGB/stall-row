"""Public Verdict Read + Embeddable Verdict Widget (Next-20 #13).

The read serves ONLY recorded state: Recheck history, Score Book
aggregates, standing-watch RouteVerdict summaries on watch_regs,
catalogue snapshots, and the registry's local sources. Guard
verdicts are never persisted, so none is served; a target with no
recorded state answers found=false, verdict=null — and the read
makes ZERO outbound calls (the service module holds no HTTP
client at all, proven below both by source scan and by a socket
guard). Fully offline, fabricated records only.
"""

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS
from app.discovery import ROUTES
from app.services import recheck, verdict, verdictread
from app.services.indexfeed import IndexStore

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
URL = ORIGIN + "/v1/thing"
FLAGGED = sorted(verdict.SNAPSHOT["addresses"])[0]


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def index_store(tmp_path):
    return IndexStore(str(tmp_path / "index.db"))


def _record(store, url=URL, status="UNCHANGED", amount="5000", pay_to=OWNER,
            at="2026-10-30T00:00:00Z", kind="x402", status_code=402):
    current = {"status_code": status_code, "network": "eip155:8453"}
    if kind == "x402":
        current.update({"amount": amount, "asset": USDC, "payTo": pay_to})
    return store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": kind,
            "status": status,
            "changes": [],
            "current": current,
            "checked_at": at,
            "summary": "recorded",
        }
    )


def _rv_watch(store, url=URL, rv_verdict="HEALTHY", score=92,
              status="UNCHANGED"):
    reg = store.register_watch(
        url, None, None, 3600, "fleet_pass", "pass_x",
        check_kind="route_verdict",
    )
    store.update_watch_run(
        reg["watch_reg_id"],
        status,
        last_summary={
            "verdict": rv_verdict,
            "score": score,
            "amount": "5000",
            "pay_to": OWNER,
            "network": "eip155:8453",
        },
    )
    return store.get_watch_reg(reg["watch_reg_id"])


# --- service: recorded target -------------------------------------------------


def test_recorded_target_full_read_with_last_change(store, index_store):
    _record(store, status="BASELINE", at="2026-10-29T00:00:00Z")
    _record(store, status="CHANGED", amount="6000", at="2026-10-30T00:00:00Z")
    _record(store, status="UNCHANGED", amount="6000", at="2026-10-30T01:00:00Z")

    out = verdictread.verdict_read(store, index_store, URL)
    assert out["found"] is True
    assert out["latest_check"]["checked_at"] == "2026-10-30T01:00:00Z"
    assert out["latest_check"]["status"] == "UNCHANGED"
    assert out["last_change_at"] == "2026-10-30T00:00:00Z"
    assert out["terms"]["amount"] == "6000"
    assert out["terms"]["payTo"] == OWNER
    assert out["terms"]["network"] == "eip155:8453"
    assert out["terms_source"] == "latest recorded check"
    assert out["score_book"]["checks"] == 3
    assert out["score_book"]["changed_count"] == 1
    assert out["score_book"]["price_changes"] == 1
    assert out["payto_flag"] is None
    # No recorded RouteVerdict verdict: the band is derived, and
    # the response says so.
    assert out["route_verdict"] is None
    assert out["verdict"] == "PROCEED"
    assert out["verdict_source"] == "derived from recorded checks"
    assert out["verdict_at"] == "2026-10-30T01:00:00Z"
    assert any(
        r["code"] == "recorded_terms_stable" for r in out["verdict_reasons"]
    )


def test_derived_caution_when_payto_moved(store, index_store):
    _record(store, status="BASELINE", pay_to=OWNER, at="2026-10-29T00:00:00Z")
    _record(store, status="CHANGED", pay_to=OTHER, at="2026-10-30T00:00:00Z")
    out = verdictread.verdict_read(store, index_store, URL)
    assert out["verdict"] == "CAUTION"
    assert out["verdict_source"] == "derived from recorded checks"
    assert any(r["code"] == "payto_moved_before" for r in out["verdict_reasons"])
    assert out["last_change_at"] == "2026-10-30T00:00:00Z"


def test_derived_abort_when_recorded_payto_flagged(store, index_store):
    _record(store, pay_to=FLAGGED)
    out = verdictread.verdict_read(store, index_store, URL)
    assert out["verdict"] == "ABORT"
    assert out["payto_flag"]["reasons"] == ["ofac_sdn"]
    assert any(r["code"] == "payto_flagged" for r in out["verdict_reasons"])


def test_recorded_route_verdict_served_verbatim(store, index_store):
    _record(store, status="BASELINE", at="2026-10-29T00:00:00Z")
    reg = _rv_watch(store, rv_verdict="DEGRADED", score=41, status="CHANGED")
    out = verdictread.verdict_read(store, index_store, URL)
    assert out["found"] is True
    assert out["verdict"] == "DEGRADED"
    assert out["verdict_source"] == "recorded route-verdict check"
    assert out["route_verdict"]["score"] == 41
    assert out["route_verdict"]["checked_at"] == reg["last_run_at"]
    assert out["verdict_at"] == reg["last_run_at"]
    # The RV watch's CHANGED run is a recorded change event too.
    assert out["last_change_at"] == reg["last_run_at"]


def test_web_target_has_no_derived_verdict(store, index_store):
    _record(store, kind="web", status_code=200)
    out = verdictread.verdict_read(store, index_store, URL)
    assert out["found"] is True
    assert out["verdict"] is None
    assert out["verdict_source"] is None


# --- service: unrecorded target, zero outbound ---------------------------------


def test_unrecorded_target_found_false_no_verdict(store, index_store):
    out = verdictread.verdict_read(store, index_store, URL)
    assert out["found"] is False
    assert out["verdict"] is None
    assert out["verdict_source"] is None
    assert out["latest_check"] is None
    assert out["last_change_at"] is None
    assert out["terms"] is None
    assert out["score_book"]["checks"] == 0


def test_read_makes_zero_outbound_calls(store, index_store, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("outbound network attempted by verdict read")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    out = verdictread.verdict_read(
        store, index_store, "https://192.0.2.123/never-checked"
    )
    assert out["found"] is False


def test_service_module_has_no_http_client():
    source = Path(verdictread.__file__).read_text()
    assert "import httpx" not in source
    assert "import requests" not in source
    assert "urlopen" not in source
    assert "AsyncClient" not in source


def test_invalid_url_rejected(store, index_store):
    with pytest.raises(ValueError):
        verdictread.verdict_read(store, index_store, "not a url")
    with pytest.raises(ValueError):
        verdictread.verdict_read(store, index_store, "ftp://example.com/x")


# --- HTTP surface ----------------------------------------------------------------


@pytest.fixture()
def http(store, index_store, monkeypatch):
    monkeypatch.setattr(main, "_recheck_store", store)
    monkeypatch.setattr(main, "_index_store", index_store)
    return TestClient(main.app, raise_server_exceptions=False)


def test_http_read_recorded_and_unrecorded(http, store):
    _record(store, status="CHANGED", at="2026-10-30T00:00:00Z")
    r = http.get("/v1/verdict-read", params={"url": URL})
    assert r.status_code == 200  # free: no 402, no payment header sent
    body = r.json()
    assert body["found"] is True
    assert body["last_change_at"] == "2026-10-30T00:00:00Z"

    r = http.get(
        "/v1/verdict-read", params={"url": "https://192.0.2.9/nope"}
    )
    assert r.status_code == 200
    assert r.json()["found"] is False
    assert r.json()["verdict"] is None


def test_http_read_invalid_url_422(http):
    r = http.get("/v1/verdict-read", params={"url": "nope"})
    assert r.status_code == 422
    r = http.get("/v1/verdict-read")
    assert r.status_code == 422


def test_read_surface_is_not_a_priced_route(http):
    assert "/v1/verdict-read" not in ROUTES
    assert "/v1/verdict-read" not in PRICE_UNITS
    assert "/widget/verdict.js" not in ROUTES
    assert "/widget/verdict.html" not in ROUTES


def test_widget_js_serves(http):
    r = http.get("/widget/verdict.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "data-target" in r.text
    assert "/v1/verdict-read" in r.text
    assert "Verdict Row Patrol" in r.text
    assert "patrol@verdictrow.com" in r.text
    assert "attachShadow" in r.text


def test_widget_html_recorded_target(http, store):
    _record(store, status="CHANGED", at="2026-10-30T00:00:00Z")
    r = http.get("/widget/verdict.html", params={"url": URL})
    assert r.status_code == 200
    assert "Verdict: PROCEED" in r.text
    assert "Last change: 2026-10-30T00:00:00Z" in r.text
    assert "Verdict Row Patrol" in r.text
    assert "patrol@verdictrow.com" in r.text


def test_widget_html_unrecorded_target_is_honest(http):
    r = http.get(
        "/widget/verdict.html", params={"url": "https://192.0.2.9/nope"}
    )
    assert r.status_code == 200
    assert "Not yet checked" in r.text


def test_widget_html_escapes_malicious_url(http):
    evil = 'https://example.com/"><script>alert(1)</script>'
    r = http.get("/widget/verdict.html", params={"url": evil})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_docs_document_the_free_surface():
    from app.config import load_settings
    from app.discovery import llms_txt, skill_md

    settings = load_settings()
    skill = skill_md(settings, "https://example.test")
    assert "/v1/verdict-read?url=" in skill
    assert "/widget/verdict.js" in skill
    assert "data-target" in skill
    assert "Not yet checked" in skill
    llms = llms_txt(settings, "https://example.test")
    assert "/v1/verdict-read?url=" in llms
    assert "/widget/verdict.html?url=" in llms
