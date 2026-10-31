"""Feed v2 filters + backfill (Next-20 #7): ?origin= / ?pay_to= /
?route= select a subset of the SAME hash chain on
GET /v1/recheck/feed — envelopes served exactly as recorded, so
seq gaps under a filter are the excluded records, never drops —
and ?before_seq= pages backwards through the retained history
(the store is append-only; X-Recheck-Feed-Earliest-Seq is the
honest floor). Fully offline: records are seeded straight into
the store, no target is probed."""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import recheck

PAY_A = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PAY_B = "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
PAY_C = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"

ALPHA_CHECK = "https://alpha.example/v1/check"
ALPHA_OTHER = "https://alpha.example/v1/other"
BETA_CHECK = "https://beta.example/v1/check"
ALPHA_PAGE = "https://alpha.example/page"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture()
def http(store, monkeypatch):
    monkeypatch.setattr(main, "_recheck_store", store)
    return TestClient(main.app, raise_server_exceptions=False)


def _seed(store):
    """Five records, seqs 1..5: two origins, three payTos, two
    routes, one web record (no payTo in its fingerprint)."""
    specs = [
        (ALPHA_CHECK, PAY_A),
        (ALPHA_OTHER, PAY_B),
        (BETA_CHECK, PAY_A),
        (ALPHA_PAGE, None),
        (BETA_CHECK, PAY_C),
    ]
    for i, (url, pay_to) in enumerate(specs):
        current = {"status_code": 402, "amount": "100"}
        if pay_to is not None:
            current["payTo"] = pay_to
        store.record_check(
            {
                "watch_id": "w1",
                "url": url,
                "kind": "x402" if pay_to else "web",
                "status": "UNCHANGED",
                "changes": [],
                "current": current,
                "checked_at": f"2026-10-31T00:00:0{i}Z",
                "summary": "synthetic",
            }
        )


def _seqs(response):
    return [json.loads(line)["seq"] for line in response.text.splitlines()]


def _envs(response):
    return [json.loads(line) for line in response.text.splitlines()]


# --- regression: unfiltered v2 (and v1) unchanged -------------------------------


def test_unfiltered_v2_and_v1_unchanged(http, store):
    _seed(store)
    v2 = http.get("/v1/recheck/feed", params={"v": 2})
    assert v2.status_code == 200
    assert _seqs(v2) == [5, 4, 3, 2, 1]  # newest first, as before
    # Body is byte-identical to the envelopes the store serves
    # through the pre-existing unfiltered path.
    expected = "".join(
        json.dumps(e, sort_keys=True) + "\n"
        for e in store.feed_rows(limit=100)
    )
    assert v2.text == expected
    legacy = http.get("/v1/recheck/feed")
    assert legacy.status_code == 200
    lines = [json.loads(line) for line in legacy.text.splitlines()]
    assert len(lines) == 5
    assert all("seq" not in line for line in lines)  # bare records
    assert store.verify_chain() is True


# --- each filter alone ------------------------------------------------------------


def test_origin_filter_single(http, store):
    _seed(store)
    r = http.get(
        "/v1/recheck/feed", params={"v": 2, "origin": "https://alpha.example"}
    )
    assert r.status_code == 200
    assert _seqs(r) == [4, 2, 1]
    assert r.headers["x-recheck-feed-total-matched"] == "3"
    for env in _envs(r):
        assert env["record"]["url"].startswith("https://alpha.example/")


def test_origin_filter_comma_set(http, store):
    _seed(store)
    r = http.get(
        "/v1/recheck/feed",
        params={"v": 2, "origin": "https://alpha.example, https://beta.example"},
    )
    assert _seqs(r) == [5, 4, 3, 2, 1]
    only_beta = http.get(
        "/v1/recheck/feed", params={"origin": "https://beta.example/"}
    )
    assert _seqs(only_beta) == [5, 3]


def test_pay_to_filter_case_insensitive(http, store):
    _seed(store)
    r = http.get(
        "/v1/recheck/feed", params={"v": 2, "pay_to": PAY_A.lower()}
    )
    assert _seqs(r) == [3, 1]
    assert r.headers["x-recheck-feed-total-matched"] == "2"


def test_route_filter_path_and_full_url(http, store):
    _seed(store)
    by_path = http.get("/v1/recheck/feed", params={"v": 2, "route": "/v1/check"})
    assert _seqs(by_path) == [5, 3, 1]
    by_url = http.get(
        "/v1/recheck/feed", params={"v": 2, "route": BETA_CHECK}
    )
    assert _seqs(by_url) == [5, 3]


def test_filter_params_trigger_v2_without_v(http, store):
    _seed(store)
    r = http.get("/v1/recheck/feed", params={"pay_to": PAY_B})
    assert r.status_code == 200
    envs = _envs(r)
    assert [e["seq"] for e in envs] == [2]
    assert "record_hash" in envs[0]  # envelope shape, not legacy


# --- combined filters + cursor -----------------------------------------------------


def test_combined_filters(http, store):
    _seed(store)
    r = http.get(
        "/v1/recheck/feed",
        params={
            "v": 2,
            "origin": "https://alpha.example",
            "route": "/v1/check",
        },
    )
    assert _seqs(r) == [1]
    r2 = http.get(
        "/v1/recheck/feed",
        params={
            "v": 2,
            "origin": "https://beta.example",
            "pay_to": PAY_A,
            "route": "/v1/check",
        },
    )
    assert _seqs(r2) == [3]


def test_filter_with_since_seq_cursor(http, store):
    _seed(store)
    page1 = http.get(
        "/v1/recheck/feed",
        params={"pay_to": PAY_A, "since_seq": 0, "limit": 1},
    )
    assert _seqs(page1) == [1]
    page2 = http.get(
        "/v1/recheck/feed",
        params={"pay_to": PAY_A, "since_seq": 1, "limit": 1},
    )
    assert _seqs(page2) == [3]
    page3 = http.get(
        "/v1/recheck/feed",
        params={"pay_to": PAY_A, "since_seq": 3},
    )
    assert page3.status_code == 200
    assert page3.text == ""


def test_filtered_view_is_unmodified_chain_subset(http, store):
    _seed(store)
    full = {e["seq"]: e for e in store.feed_rows(limit=100)}
    r = http.get("/v1/recheck/feed", params={"v": 2, "pay_to": PAY_A})
    for env in _envs(r):
        assert env["record_hash"] == full[env["seq"]]["record_hash"]
        assert env["prev_hash"] == full[env["seq"]]["prev_hash"]
        assert env["record"] == full[env["seq"]]["record"]
    assert store.verify_chain() is True


# --- empty results -------------------------------------------------------------------


def test_empty_result_filter_is_valid_empty_page(http, store):
    _seed(store)
    r = http.get(
        "/v1/recheck/feed",
        params={"v": 2, "pay_to": "0xDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"},
    )
    assert r.status_code == 200
    assert r.text == ""
    assert r.headers["x-recheck-feed-latest-seq"] == "5"
    assert r.headers["x-recheck-feed-earliest-seq"] == "1"
    assert r.headers["x-recheck-feed-total-matched"] == "0"
    assert "full retained history" in r.headers["x-recheck-feed-retention"]


# --- backfill -------------------------------------------------------------------------


def test_backfill_backwards_paging_walks_full_history(http, store):
    _seed(store)
    seen = []
    before = 6
    pages = 0
    while True:
        r = http.get(
            "/v1/recheck/feed",
            params={"v": 2, "before_seq": before, "limit": 2},
        )
        assert r.status_code == 200
        seqs = _seqs(r)
        if not seqs:
            break
        seen += seqs
        pages += 1
        if "x-recheck-feed-next-before-seq" in r.headers:
            before = int(r.headers["x-recheck-feed-next-before-seq"])
            assert before == seqs[-1]
        else:
            break
    assert pages == 3
    assert seen == [5, 4, 3, 2, 1]  # no duplicates, no gaps
    assert store.earliest_seq() == 1


def test_backfill_forward_from_zero_walks_full_history(http, store):
    _seed(store)
    seen = []
    since = 0
    while True:
        r = http.get(
            "/v1/recheck/feed",
            params={"v": 2, "since_seq": since, "limit": 2},
        )
        seqs = _seqs(r)
        if not seqs:
            break
        seen += seqs
        since = seqs[-1]
    assert seen == [1, 2, 3, 4, 5]


def test_backfill_with_filter(http, store):
    _seed(store)
    page1 = http.get(
        "/v1/recheck/feed",
        params={"pay_to": PAY_A, "before_seq": 99, "limit": 1},
    )
    assert _seqs(page1) == [3]
    assert page1.headers["x-recheck-feed-next-before-seq"] == "3"
    page2 = http.get(
        "/v1/recheck/feed",
        params={"pay_to": PAY_A, "before_seq": 3, "limit": 1},
    )
    assert _seqs(page2) == [1]
    assert "x-recheck-feed-next-before-seq" not in page2.headers
    assert page2.headers["x-recheck-feed-total-matched"] == "1"


def test_since_seq_at_head_returns_zero_new(http, store):
    _seed(store)
    r = http.get("/v1/recheck/feed", params={"v": 2, "since_seq": 5})
    assert r.status_code == 200
    assert r.text == ""
    assert r.headers["x-recheck-feed-latest-seq"] == "5"


# --- invalid values → clean 422s --------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"origin": "notaurl"},
        {"origin": "ftp://alpha.example"},
        {"origin": "https://alpha.example/some/path"},
        {"origin": ""},
        {"pay_to": ""},
        {"pay_to": "   "},
        {"route": "v1/check"},
        {"route": ""},
        {"before_seq": "abc"},
        {"before_seq": -1},
        {"since_seq": 1, "before_seq": 4},
        {"since_seq": "x", "origin": "https://alpha.example"},
    ],
)
def test_invalid_filter_values_are_422(http, store, params):
    _seed(store)
    r = http.get("/v1/recheck/feed", params={"v": 2, **params})
    assert r.status_code == 422
    assert "error" in r.json()
