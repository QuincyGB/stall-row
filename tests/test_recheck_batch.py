"""Survey Batch Recheck: POST /v1/recheck/batch — Fleet-Pass-only
funding, dedupe, the full-100 batch discount, pre-funding refusals
that debit nothing and run no checks, and per-URL failure rows.
Fully offline: MockTransport plays the targets, IP-literal URLs keep
the SSRF guard's DNS lookup local."""

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, indexfeed, recheck

BASE = "https://93.184.216.34"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    monkeypatch.setattr(
        main, "_index_store", indexfeed.IndexStore(str(tmp_path / "w.db"))
    )

    def handler(request):
        return httpx.Response(
            200,
            text=f"page for {request.url.path}",
            headers={"content-type": "text/plain"},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: mock_client)
    return fresh


def _mint(store, key="pay-batch", total=5_000_000, ttl_days=90):
    fleet_pass, created = store.mint_pass(key, total, ttl_days)
    assert created
    return fleet_pass


def _urls(n, prefix="p"):
    return [f"{BASE}/{prefix}{i}" for i in range(n)]


def _post(client, urls, pass_id=None, header=False):
    kwargs = {}
    body = {"urls": urls}
    if pass_id is not None:
        if header:
            kwargs["headers"] = {"X-Fleet-Pass": pass_id}
        else:
            body["fleet_pass_id"] = pass_id
    return client.post("/v1/recheck/batch", json=body, **kwargs)


# --- happy paths ---------------------------------------------------------------


def test_happy_path_three_urls_charged_once_per_url(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    urls = [f"{BASE}/a", f"{BASE}/b", f"{BASE}/c"]
    r = _post(client, urls, fleet_pass["pass_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"].startswith("batch_")
    assert body["requested"] == 3
    assert body["unique"] == 3
    assert body["duplicates_removed"] == 0
    assert body["per_check_units"] == 5_000
    assert body["discount_applied"] is False
    assert body["charged_units"] == 15_000
    assert body["pass_balance_units"] == 5_000_000 - 15_000
    assert [row["url"] for row in body["results"]] == urls
    assert [row["status"] for row in body["results"]] == ["BASELINE"] * 3
    # One shared batch watch id, filed in the feed/history normally.
    assert {row["watch_id"] for row in body["results"]} == {
        f"batch:{body['batch_id']}"
    }
    assert http_env.check_history_count() == 3
    # The total was debited ONCE.
    assert http_env.pass_debit_count(fleet_pass["pass_id"]) == 1
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 4_985_000


def test_pass_may_come_from_the_header(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, [f"{BASE}/a"], fleet_pass["pass_id"], header=True)
    assert r.status_code == 200, r.text
    assert r.json()["charged_units"] == 5_000


def test_duplicates_charged_and_checked_once(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    a, b = f"{BASE}/a", f"{BASE}/b"
    r = _post(client, [a, a, b, a], fleet_pass["pass_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 4
    assert body["unique"] == 2
    assert body["duplicates_removed"] == 2
    assert body["charged_units"] == 10_000
    assert [row["url"] for row in body["results"]] == [a, b]
    assert http_env.check_history_count() == 2


def test_refused_url_is_an_error_row_and_the_rest_succeed(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    urls = [f"{BASE}/a", "http://127.0.0.1/x", f"{BASE}/b"]
    r = _post(client, urls, fleet_pass["pass_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["status"] for row in body["results"]] == [
        "BASELINE",
        "ERROR",
        "BASELINE",
    ]
    refused = body["results"][1]
    assert refused["url"] == "http://127.0.0.1/x"
    assert refused["error"]
    # The refused check was attempted and delivered as a report: no refund.
    assert body["charged_units"] == 15_000
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 4_985_000


# --- discount math ---------------------------------------------------------------


def test_full_100_gets_the_20_percent_discount(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(100), fleet_pass["pass_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unique"] == 100
    assert body["discount_applied"] is True
    assert body["per_check_units"] == 4_000
    assert body["charged_units"] == 400_000
    assert body["pass_balance_units"] == 5_000_000 - 400_000
    assert len(body["results"]) == 100


def test_99_gets_no_discount(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(99), fleet_pass["pass_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unique"] == 99
    assert body["discount_applied"] is False
    assert body["per_check_units"] == 5_000
    assert body["charged_units"] == 99 * 5_000


# --- pre-funding refusals: nothing debited, no checks run -------------------------


def test_oversized_101_is_422_and_pass_untouched(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(101), fleet_pass["pass_id"])
    assert r.status_code == 422, r.text
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000
    assert http_env.pass_debit_count(fleet_pass["pass_id"]) == 0
    assert http_env.check_history_count() == 0


def test_oversized_beats_unknown_pass(http_env):
    # Validation runs before funding: 101 URLs + an unknown pass is a
    # 422, not a 404.
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(101), "fpass_nope")
    assert r.status_code == 422, r.text
    assert http_env.check_history_count() == 0


def test_empty_list_is_422(http_env):
    fleet_pass = _mint(http_env)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, [], fleet_pass["pass_id"])
    assert r.status_code == 422, r.text
    assert http_env.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000


def test_unknown_pass_is_404_and_untouched(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(3), "fpass_nope")
    assert r.status_code == 404, r.text
    assert http_env.check_history_count() == 0


def test_expired_pass_is_410_and_untouched(http_env):
    stale = _mint(http_env, key="pay-stale", ttl_days=0)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(2), stale["pass_id"])
    assert r.status_code == 410, r.text
    assert http_env.get_pass(stale["pass_id"])["balance_units"] == 5_000_000
    assert http_env.check_history_count() == 0


def test_insufficient_balance_is_402_and_zero_checks_recorded(http_env):
    small = _mint(http_env, key="pay-small", total=10_000)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _post(client, _urls(3), small["pass_id"])  # needs 15_000
    assert r.status_code == 402, r.text
    assert http_env.get_pass(small["pass_id"])["balance_units"] == 10_000
    assert http_env.pass_debit_count(small["pass_id"]) == 0
    assert http_env.check_history_count() == 0


def test_missing_credential_names_the_funding_rule(http_env):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/recheck/batch", json={"urls": _urls(2)})
    assert r.status_code == 402, r.text
    error = r.json()["error"]
    assert "Fleet-Pass ONLY" in error
    assert (
        "Recheck packs are bound to a single URL and cannot fund a survey"
        in error
    )
    assert http_env.check_history_count() == 0


# --- route-count discipline --------------------------------------------------------


def test_batch_is_not_a_priced_route():
    from app.config import PRICE_UNITS
    from app.discovery import ROUTES

    assert "/v1/recheck/batch" not in PRICE_UNITS
    assert "/v1/recheck/batch" not in ROUTES
    assert fleetpass.price_for("stall-row", "/v1/recheck/batch") is None
    # The single-Recheck price the batch derives from is untouched.
    assert fleetpass.price_for("stall-row", "/v1/recheck") == 5_000
    assert PRICE_UNITS["/v1/recheck"] == 5_000
