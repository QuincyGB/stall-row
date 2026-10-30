"""The Recheck JSONL feed: GET /v1/recheck/feed serves the recent
recorded check responses — one JSON object per line, newest first,
bounded to recheck.FEED_LIMIT — free, no paywall. Recording happens
inside recheck() itself, so per-call, pack-spent, and scheduled
checks all land in the history exactly once with the exact object
the caller was answered with.

The honesty edges pinned here: an empty store serves an empty body
with 200 (not an error, not a fabricated record); watches that
pre-date the history are seeded with only the fields the watches
table persisted (no invented status/changes/summary), exactly once.
Fully offline: MockTransport plays the target, IP-literal URLs keep
the SSRF guard's DNS lookup local."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import recheck

WEB_URL = "https://93.184.216.34/story"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _page(body: str):
    def handler(request):
        return httpx.Response(
            200, text=body, headers={"content-type": "text/html"}
        )

    return handler


# --- recording ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_checks_are_recorded_newest_first(store):
    state = {"body": "<html><body>v1</body></html>"}

    def handler(request):
        return httpx.Response(
            200, text=state["body"], headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        first = await recheck.recheck(client, store, WEB_URL)
        second = await recheck.recheck(client, store, WEB_URL)
        state["body"] = "<html><body>v2</body></html>"
        third = await recheck.recheck(client, store, WEB_URL)

    records = store.recent_checks()
    assert [r["status"] for r in records] == ["CHANGED", "UNCHANGED", "BASELINE"]
    # The recorded objects are the exact responses the caller got.
    assert records[0] == third
    assert records[1] == second
    assert records[2] == first
    assert store.check_history_count() == 3


@pytest.mark.asyncio
async def test_failed_checks_are_not_recorded(store):
    def handler(request):
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        with pytest.raises(Exception):
            await recheck.recheck(client, store, WEB_URL)
    assert store.recent_checks() == []
    assert store.check_history_count() == 0


def test_recorded_lines_are_one_parseable_object_each(store):
    for i in range(3):
        store.record_check(
            {
                "watch_id": "w",
                "url": WEB_URL,
                "kind": "web",
                "status": "UNCHANGED",
                "changes": [],
                "current": {"status_code": 200, "body_sha256": "x" * 64},
                "checked_at": f"2026-10-29T00:00:0{i}Z",
                "summary": "No material change since the previous check.",
            }
        )
    records = store.recent_checks()
    lines = [json.dumps(r, sort_keys=True) for r in records]
    assert [json.loads(line)["checked_at"] for line in lines] == [
        "2026-10-29T00:00:02Z",
        "2026-10-29T00:00:01Z",
        "2026-10-29T00:00:00Z",
    ]


def test_feed_window_is_bounded(store):
    for i in range(recheck.FEED_LIMIT + 5):
        store.record_check(
            {
                "watch_id": "w",
                "url": WEB_URL,
                "kind": "web",
                "status": "UNCHANGED",
                "changes": [],
                "current": {"status_code": 200, "body_sha256": "y" * 64},
                "checked_at": f"2026-10-29T00:{i // 60:02d}:{i % 60:02d}Z",
                "summary": "No material change since the previous check.",
            }
        )
    records = store.recent_checks()
    assert len(records) == recheck.FEED_LIMIT
    assert store.check_history_count() == recheck.FEED_LIMIT + 5


# --- seeding from pre-existing watches --------------------------------------------


def test_existing_watch_is_seeded_from_stored_state_only(tmp_path):
    path = str(tmp_path / "watches.db")
    first = recheck.WatchStore(path)
    fingerprint = {"status_code": 200, "body_sha256": "z" * 64}
    first.save("wid\n" + WEB_URL, "web", fingerprint, "2026-10-25T17:51:58Z")
    # The save happened AFTER this store opened (its history seed ran
    # at open, over an empty watches table) — simulate the production
    # upgrade by reopening: the new code seeds from the stored state.
    assert first.check_history_count() == 0
    first.close()

    reopened = recheck.WatchStore(path)
    records = reopened.recent_checks()
    assert len(records) == 1
    seed = records[0]
    assert seed["watch_id"] == "wid"
    assert seed["url"] == WEB_URL
    assert seed["kind"] == "web"
    assert seed["current"] == fingerprint
    assert seed["checked_at"] == "2026-10-25T17:51:58Z"
    # The per-check fields were never stored — never reconstructed.
    assert "status" not in seed
    assert "changes" not in seed
    assert "summary" not in seed
    reopened.close()

    # Idempotent: a second reopen seeds nothing new.
    again = recheck.WatchStore(path)
    assert again.check_history_count() == 1
    again.close()


# --- HTTP surface -------------------------------------------------------------------


@pytest.fixture()
def http(store, monkeypatch):
    monkeypatch.setattr(main, "_recheck_store", store)
    return TestClient(main.app, raise_server_exceptions=False)


def test_feed_empty_store_is_empty_body_200(http):
    r = http.get("/v1/recheck/feed")
    assert r.status_code == 200
    assert r.text == ""


def test_feed_serves_jsonl_newest_first(http, store):
    for i, status in enumerate(["BASELINE", "UNCHANGED"]):
        store.record_check(
            {
                "watch_id": "modelmarket-survey",
                "url": "https://93.184.216.34/x402/histor-check",
                "kind": "x402",
                "status": status,
                "changes": [],
                "current": {"status_code": 402, "amount": "1000"},
                "checked_at": f"2026-10-29T19:04:4{i}Z",
                "summary": "recorded",
            }
        )
    r = http.get("/v1/recheck/feed")
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers["content-type"]
    lines = r.text.splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [p["status"] for p in parsed] == ["UNCHANGED", "BASELINE"]
    assert all(p["watch_id"] == "modelmarket-survey" for p in parsed)
