"""Utility-shelf upgrades (buyer analysis 2026-11-05): tube-search
depth + filters, video-details, batch shapes, the reader-proxy
fallback for page-to-markdown, toolkit-pack funding of the utility
shelf, and the loop block's utility offer.

The ledger evidence these pin: the $0.01 utility point is the fleet's
volume leader; the largest stranger event was 20 single searches in
~30 s (batch behaviour in one-call clothing); repeat strangers buy
cheap task utilities while the only pack pitched to them funded
inspector tools — until now.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.rpc import UpstreamError
from app.services import fetch, loop_offer, recheck
from tests.fixtures import PAGE_HTML

PAGE_URL = "https://93.184.216.34/story"


def _renderer(vid, title, channel, length, views, published):
    return {
        "videoRenderer": {
            "videoId": vid,
            "title": {"runs": [{"text": title}]},
            "ownerText": {"runs": [{"text": channel}]},
            "lengthText": {"simpleText": length},
            "viewCountText": {"simpleText": views},
            "publishedTimeText": {"simpleText": published},
        }
    }


SEARCH_RENDERERS = [
    _renderer("AAAAAAAAAAA", "Short alpha", "Alpha", "2:30", "1K views", "5 hours ago"),
    _renderer("BBBBBBBBBBB", "Medium beta", "Beta", "10:24", "12K views", "3 days ago"),
    _renderer("CCCCCCCCCCC", "Long alpha", "Alpha", "45:00", "99K views", "2 years ago"),
]

CONTINUATION_JSON = {
    "onResponseReceivedCommands": [
        {
            "appendContinuationItemsAction": {
                "continuationItems": [
                    {
                        "itemSectionRenderer": {
                            "contents": [
                                _renderer(
                                    "DDDDDDDDDDD", "Medium delta", "Delta",
                                    "8:00", "5K views", "1 day ago",
                                )
                            ]
                        }
                    }
                ]
            }
        }
    ]
}


def _search_html(renderers, continuation=None):
    contents = list(renderers)
    if continuation:
        contents.append(
            {
                "continuationItemRenderer": {
                    "continuationEndpoint": {
                        "continuationCommand": {"token": continuation}
                    }
                }
            }
        )
    data = {"contents": {"sections": [{"itemSectionRenderer": {"contents": contents}}]}}
    return (
        "<html><script>var ytInitialData = "
        + json.dumps(data)
        + ";</script>"
        + '<script>ytcfg.set({"INNERTUBE_API_KEY":"TESTKEY",'
        + '"INNERTUBE_CONTEXT_CLIENT_VERSION":"2.0"});</script></html>'
    )


WATCH_DATA = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {
        "videoId": "dQw4w9WgXcQ",
        "title": "x402 explained",
        "author": "Example Channel",
        "channelId": "UCexample",
        "lengthSeconds": "624",
        "viewCount": "12000",
        "shortDescription": "A walkthrough of the standard.",
        "keywords": ["x402", "tutorial"],
        "thumbnail": {
            "thumbnails": [
                {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"},
                {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxres.jpg"},
            ]
        },
        "isLiveContent": False,
    },
    "microformat": {
        "playerMicroformatRenderer": {"publishDate": "2026-10-30T14:00:00Z"}
    },
    "captions": {
        "playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {"languageCode": "en", "name": {"simpleText": "English"}, "kind": "asr"},
                {"languageCode": "es", "name": {"simpleText": "Spanish"}},
            ]
        }
    },
}
WATCH_HTML = (
    "<html><script>var ytInitialPlayerResponse = "
    + json.dumps(WATCH_DATA)
    + ";</script></html>"
)


def _youtube_client():
    def handler(request):
        url = str(request.url)
        if "youtubei/v1/player" in url:
            return httpx.Response(200, json=WATCH_DATA)
        if "youtubei" in url:
            return httpx.Response(200, json=CONTINUATION_JSON)
        if "/watch" in url:
            return httpx.Response(200, text=WATCH_HTML)
        if request.url.params.get("search_query") == "empty":
            return httpx.Response(200, text="<html>no data here</html>")
        return httpx.Response(
            200, text=_search_html(SEARCH_RENDERERS, continuation="TOK1")
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- tube-search: enrichment, filters, depth ---------------------------------


@pytest.mark.asyncio
async def test_tube_search_enriches_results():
    client = _youtube_client()
    async with client:
        out = await fetch.tube_search(client, "x402", 3)
    assert out["count"] == 3
    r = out["results"][0]
    assert r["published"] == "5 hours ago"
    assert r["duration_seconds"] == 150
    assert r["thumbnail"] == "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg"
    assert "filters_applied" not in out


@pytest.mark.asyncio
async def test_tube_search_filters():
    client = _youtube_client()
    async with client:
        short = await fetch.tube_search(client, "x402", 3, duration="short")
        week = await fetch.tube_search(client, "x402", 3, uploaded_within="week")
        alpha = await fetch.tube_search(client, "x402", 3, channel="alpha")
    assert [r["video_id"] for r in short["results"]] == ["AAAAAAAAAAA"]
    assert short["filters_applied"] == {"duration": "short"}
    # Page one leaves 2 of 3 slots filled, so the continuation page is
    # dug and its "1 day ago" result also passes the week filter.
    assert {r["video_id"] for r in week["results"]} == {
        "AAAAAAAAAAA",
        "BBBBBBBBBBB",
        "DDDDDDDDDDD",
    }
    assert {r["video_id"] for r in alpha["results"]} == {"AAAAAAAAAAA", "CCCCCCCCCCC"}


@pytest.mark.asyncio
async def test_tube_search_continuation_deepens():
    client = _youtube_client()
    async with client:
        out = await fetch.tube_search(client, "x402", 5)
    # 3 on page one + 1 from the continuation page.
    assert out["count"] == 4
    assert out["results"][-1]["video_id"] == "DDDDDDDDDDD"


@pytest.mark.asyncio
async def test_tube_search_rejects_bad_filter_values():
    client = _youtube_client()
    async with client:
        with pytest.raises(ValueError):
            await fetch.tube_search(client, "x402", 3, duration="huge")
        with pytest.raises(ValueError):
            await fetch.tube_search(client, "x402", 3, uploaded_within="decade")


@pytest.mark.asyncio
async def test_tube_search_retries_transient_failure():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=_search_html(SEARCH_RENDERERS))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.tube_search(client, "x402", 2)
    assert out["count"] == 2
    assert calls["n"] == 2


# --- video details -------------------------------------------------------------


def test_parse_video_input_shapes():
    assert fetch.parse_video_input("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert (
        fetch.parse_video_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=4")
        == "dQw4w9WgXcQ"
    )
    assert fetch.parse_video_input("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert (
        fetch.parse_video_input("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )
    with pytest.raises(ValueError):
        fetch.parse_video_input("https://example.com/not-a-video")
    with pytest.raises(ValueError):
        fetch.parse_video_input("")


@pytest.mark.asyncio
async def test_video_details_happy_path():
    client = _youtube_client()
    async with client:
        out = await fetch.video_details(
            client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
    assert out["video_id"] == "dQw4w9WgXcQ"
    assert out["title"] == "x402 explained"
    assert out["duration"] == "10:24"
    assert out["duration_seconds"] == 624
    assert out["views"] == 12000
    assert out["published"] == "2026-10-30T14:00:00Z"
    assert out["captions_available"] is True
    assert [c["language_code"] for c in out["captions"]] == ["en", "es"]
    assert out["thumbnail"].endswith("maxres.jpg")


@pytest.mark.asyncio
async def test_video_details_unavailable_is_upstream_error():
    data = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in"}}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    )
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.video_details(client, "dQw4w9WgXcQ")


# --- player identity cascade (YouTube egress fix 2026-11-07) --------------------
#
# The watch page is walled from datacenter egress; the player API
# answers non-WEB identities. The cascade must fall through a refused
# identity to the next, and fail (UpstreamError, no settlement) only
# when every identity refuses.


@pytest.mark.asyncio
async def test_player_cascade_falls_through_to_next_identity():
    seen_identities = []

    def handler(request):
        seen_identities.append(request.headers.get("x-youtube-client-name"))
        if request.headers.get("x-youtube-client-name") == "3":  # ANDROID
            return httpx.Response(
                200,
                json={
                    "playabilityStatus": {
                        "status": "LOGIN_REQUIRED",
                        "reason": "Sign in",
                    }
                },
            )
        return httpx.Response(200, json=WATCH_DATA)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.video_details(client, "dQw4w9WgXcQ")
    assert out["title"] == "x402 explained"
    assert seen_identities == ["3", "5"]  # ANDROID refused, IOS served


@pytest.mark.asyncio
async def test_player_cascade_all_refused_is_upstream_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, text="too many requests")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.video_details(client, "dQw4w9WgXcQ")
    # 3 identities x 2 attempts each (one transient retry per identity).
    assert calls["n"] == 6


def test_timedtext_url_replaces_existing_fmt():
    url = fetch._timedtext_json3_url(
        "https://www.youtube.com/api/timedtext?v=abc&lang=en&fmt=srv3"
    )
    assert url.count("fmt=") == 1
    assert "fmt=json3" in url
    assert "lang=en" in url


def test_timedtext_url_appends_fmt_when_absent():
    url = fetch._timedtext_json3_url(
        "https://www.youtube.com/api/timedtext?v=abc&lang=en"
    )
    assert url.endswith("fmt=json3")


# --- page-to-markdown reader fallback -------------------------------------------


@pytest.mark.asyncio
async def test_page_fallback_reader_proxy_delivers():
    def handler(request):
        if request.url.host == "r.jina.ai":
            return httpx.Response(
                200,
                text="# Recovered Story\n\n" + "recovered word " * 30,
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            403, text="<html>challenge</html>", headers={"content-type": "text/html"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.page_to_markdown(client, PAGE_URL)
    assert out["fallback"] == "reader-proxy"
    assert out["title"] == "Recovered Story"
    assert out["word_count"] >= 15


@pytest.mark.asyncio
async def test_page_fallback_failure_keeps_the_honest_error():
    def handler(request):
        if request.url.host == "r.jina.ai":
            return httpx.Response(500, text="proxy down")
        return httpx.Response(
            403, text="<html>challenge</html>", headers={"content-type": "text/html"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.page_to_markdown(client, PAGE_URL)


# --- batch service shapes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tube_search_batch_partial_failure():
    client = _youtube_client()
    async with client:
        out = await fetch.tube_search_batch(client, ["x402", "empty"], 3)
    assert out["count"] == 2
    assert out["succeeded"] == 1
    assert out["results"][0]["count"] == 3
    assert "error" in out["results"][1]


@pytest.mark.asyncio
async def test_tube_search_batch_all_fail_is_an_error():
    client = _youtube_client()
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.tube_search_batch(client, ["empty"], 3)


@pytest.mark.asyncio
async def test_page_batch_partial_failure():
    def handler(request):
        if request.url.host == "r.jina.ai":
            return httpx.Response(404, text="nope")
        if "good" in str(request.url):
            return httpx.Response(
                200, text=PAGE_HTML, headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="missing", headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.page_to_markdown_batch(
            client,
            ["https://93.184.216.34/good-story", "https://93.184.216.34/missing"],
        )
    assert out["succeeded"] == 1
    assert "main content of the example page" in out["results"][0]["markdown"]
    assert "error" in out["results"][1]


# --- HTTP: pack funding across the utility shelf ---------------------------------


def _http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    monkeypatch.setattr(main, "_clients", lambda: (_youtube_client(), None, None))

    def page_handler(request):
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html"}
        )

    page_client = httpx.AsyncClient(transport=httpx.MockTransport(page_handler))
    monkeypatch.setattr(main, "_fetch_client", lambda: page_client)
    return fresh


def test_toolkit_pack_debits_across_two_utility_routes(monkeypatch, tmp_path):
    fresh = _http_env(monkeypatch, tmp_path)
    pack, _ = fresh.mint_toolkit_pack("pay-utility-pack")
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/tube-search",
        json={"query": "x402", "max_results": 3, "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert body["pack_credits_remaining"] == 124
    # The delivery's loop block names the pack that funds this shelf.
    offer_ids = [o["id"] for o in body["verdict_row_loop"]["offers"]]
    assert "toolkit_pack" in offer_ids
    assert offer_ids[0] == "recheck_pack"

    r = client.post(
        "/v1/page-to-markdown", json={"url": PAGE_URL, "pack_id": pack["pack_id"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "main content of the example page" in body["markdown"]
    assert body["pack_credits_remaining"] == 123


def test_toolkit_pack_funds_video_details(monkeypatch, tmp_path):
    fresh = _http_env(monkeypatch, tmp_path)
    pack, _ = fresh.mint_toolkit_pack("pay-utility-details")
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/video-details",
        json={"video": "dQw4w9WgXcQ", "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["captions_available"] is True
    assert body["pack_credits_remaining"] == 124


def test_batch_routes_refuse_pack_ids_cleanly(monkeypatch, tmp_path):
    _http_env(monkeypatch, tmp_path)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/tube-search/batch",
        json={"queries": ["x402"], "pack_id": "tkpack_whatever"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "pack_not_accepted"
    r = client.post(
        "/v1/page-to-markdown/batch",
        json={"urls": [PAGE_URL], "pack_id": "tkpack_whatever"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "pack_not_accepted"


def test_batch_validation(monkeypatch, tmp_path):
    _http_env(monkeypatch, tmp_path)

    async def passthrough(request, call_next):
        return await call_next(request)

    # Validation lives behind the paywall (an unpaid caller gets the
    # 402 first, by design) — settle the middleware away to reach it.
    monkeypatch.setattr(main, "_payment_middleware", passthrough)
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/tube-search/batch", json={"queries": ["  "]})
    assert r.status_code == 422
    r = client.post(
        "/v1/tube-search/batch", json={"queries": [f"q{i}" for i in range(11)]}
    )
    assert r.status_code == 422


def test_new_routes_unpaid_challenges_carry_prices():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, payload, amount in (
        ("/v1/video-details", {"video": "dQw4w9WgXcQ"}, "10000"),
        ("/v1/tube-search/batch", {"queries": ["x402"]}, "50000"),
        ("/v1/page-to-markdown/batch", {"urls": [PAGE_URL]}, "50000"),
    ):
        r = client.post(path, json=payload)
        assert r.status_code == 402, (path, r.text)
        body = r.json()  # body-mirror: the challenge rides the body too
        amounts = [a.get("amount") for a in body.get("accepts", [])]
        assert amount in amounts, (path, body)


# --- loop offer: utility wiring ---------------------------------------------------


def test_loop_block_utility_offer_only_on_utility_routes():
    utility = loop_offer.loop_offer(route="/v1/tube-search")
    ids = [o["id"] for o in utility["offers"]]
    assert ids[0] == "recheck_pack"
    assert "toolkit_pack" in ids
    tk = next(o for o in utility["offers"] if o["id"] == "toolkit_pack")
    assert tk["price_usd"] == 1.0
    assert tk["terms"]["credits"] == 125
    assert "/v1/tube-search" in tk["terms"]["routes"]
    assert "/v1/page-to-markdown" in tk["terms"]["routes"]

    inspector = loop_offer.loop_offer(route="/v1/score-book")
    assert "toolkit_pack" not in [o["id"] for o in inspector["offers"]]

    plain = loop_offer.loop_offer()
    assert "toolkit_pack" not in [o["id"] for o in plain["offers"]]


def test_loop_block_returning_utility_payer_gets_utility_offer():
    payer = {
        "payer_key": "0x1111111111111111111111111111111111111111",
        "calls": 4,
        "distinct_days": 2,
        "total_units": 40_000,
        "first_seen_epoch": 1_760_000_000.0,
        "last_seen_epoch": 1_760_100_000.0,
        "routes": {"/v1/tube-search": {"calls": 3}},
    }
    block = loop_offer.loop_offer(payer=payer, route="/v1/tube-search")
    uo = block["returning_payer"]["utility_offer"]
    assert uo["offer_id"] == "toolkit_pack"
    assert uo["price_usd"] == 1.0
    assert "payer=0x1111" in uo["deep_link"]

    other = loop_offer.loop_offer(payer=payer, route="/v1/score-book")
    assert "utility_offer" not in other["returning_payer"]
