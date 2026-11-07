"""Lane 3 (distribution pivot 2026-11-07): the widened cheap utility
shelf — Video Transcript, Page Metadata, Feed Digest, URL Unshorten,
Repo README. The W3 evidence these answer: a roaming stranger keeps
returning for $0.005–$0.01 single-purpose utilities; the shelf grows
in exactly that class, reusing the fetch family's hardened machinery,
with the two $0.01 singles joining the Toolkit Pack family on the
2026-11-05 terms and the $0.005 singles staying out of it (the Repo
Vitals precedent: a $0.008 credit would overcharge them).
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.rpc import UpstreamError
from app.services import fetch, loop_offer, recheck

PAGE_URL = "https://93.184.216.34/story"

# --- fixtures -----------------------------------------------------------------

_TIMEDTEXT = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 2400, "segs": [{"utf8": "Welcome back."}]},
        {
            "tStartMs": 2400,
            "dDurationMs": 3100,
            "segs": [{"utf8": "Today we walk "}, {"utf8": "through x402."}],
        },
        {"tStartMs": 5500, "dDurationMs": 100, "segs": [{"utf8": "\n"}]},
    ]
}

_WATCH = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {"videoId": "dQw4w9WgXcQ", "title": "x402 explained"},
    "captions": {
        "playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {
                    "languageCode": "en",
                    "name": {"simpleText": "English (auto-generated)"},
                    "kind": "asr",
                    "baseUrl": "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=en&kind=asr",
                },
                {
                    "languageCode": "es",
                    "name": {"simpleText": "Spanish"},
                    "baseUrl": "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=es",
                },
            ]
        }
    },
}
WATCH_HTML = (
    "<html><script>var ytInitialPlayerResponse = "
    + json.dumps(_WATCH)
    + ";</script></html>"
)


def _youtube_client():
    def handler(request):
        url = str(request.url)
        if "timedtext" in url:
            return httpx.Response(200, json=_TIMEDTEXT)
        if "youtubei/v1/player" in url:
            return httpx.Response(200, json=_WATCH)
        if "/watch" in url:
            return httpx.Response(200, text=WATCH_HTML)
        return httpx.Response(404, text="unexpected")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


META_HTML = (
    '<html lang="en"><head>'
    "<title>Example Story — Full Title</title>"
    '<meta name="description" content="A story about examples.">'
    '<link rel="canonical" href="https://example.com/story">'
    '<link rel="icon" href="/favicon.ico">'
    '<meta property="og:title" content="Example Story">'
    '<meta property="og:type" content="article">'
    '<meta name="twitter:card" content="summary">'
    "</head><body><h1>Example Story</h1><h2>Part One</h2>"
    + "<p>Some body text for the page to carry a word count.</p>" * 4
    + "</body></html>"
)

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Example Feed</title><link>https://example.com/</link>
<description>News about examples.</description>
<item><title>First Post</title><link>https://example.com/1</link>
<pubDate>Fri, 07 Nov 2026 10:00:00 GMT</pubDate>
<description>The first summary.</description></item>
<item><title>Second Post</title><link>https://example.com/2</link>
<pubDate>Thu, 06 Nov 2026 09:00:00 GMT</pubDate>
<description>Second &amp; final summary.</description></item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom Feed</title><link href="https://example.org/"/>
<subtitle>Atom-flavoured news.</subtitle>
<entry><title>Atom Post</title><link href="https://example.org/a"/>
<updated>2026-11-07T10:00:00Z</updated><summary>Atom summary.</summary>
<author><name>Jo Example</name></author></entry>
</feed>"""


def _page_client(html=META_HTML, ctype="text/html", status=200):
    def handler(request):
        return httpx.Response(status, text=html, headers={"content-type": ctype})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- video transcript -----------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_defaults_to_the_manual_track():
    client = _youtube_client()
    async with client:
        out = await fetch.video_transcript(client, "dQw4w9WgXcQ")
    # The es track is human-made; the en track is asr — manual wins.
    assert out["language_code"] == "es"
    assert out["kind"] is None
    assert out["transcript"] == "Welcome back. Today we walk through x402."
    assert out["segment_count"] == 2
    assert out["segments"][1]["start_ms"] == 2400
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_transcript_honours_an_explicit_language():
    client = _youtube_client()
    async with client:
        out = await fetch.video_transcript(client, "dQw4w9WgXcQ", lang="en")
    assert out["language_code"] == "en"
    assert out["kind"] == "asr"


@pytest.mark.asyncio
async def test_transcript_missing_language_is_a_caller_fault():
    client = _youtube_client()
    async with client:
        with pytest.raises(ValueError) as exc:
            await fetch.video_transcript(client, "dQw4w9WgXcQ", lang="fr")
    assert "en" in str(exc.value) and "es" in str(exc.value)


@pytest.mark.asyncio
async def test_transcript_no_tracks_is_an_upstream_error():
    data = {"playabilityStatus": {"status": "OK"}, "videoDetails": {}}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    )
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.video_transcript(client, "dQw4w9WgXcQ")


@pytest.mark.asyncio
async def test_transcript_empty_track_body_is_an_upstream_error():
    def handler(request):
        if "timedtext" in str(request.url):
            return httpx.Response(200, text="")
        return httpx.Response(200, json=_WATCH)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.video_transcript(client, "dQw4w9WgXcQ")


# --- page metadata ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_metadata_extracts_the_head():
    client = _page_client()
    async with client:
        out = await fetch.page_metadata(client, PAGE_URL)
    assert out["title"] == "Example Story — Full Title"
    assert out["description"] == "A story about examples."
    assert out["canonical"] == "https://example.com/story"
    assert out["lang"] == "en"
    assert out["favicon"] == "https://93.184.216.34/favicon.ico"
    assert out["og"] == {"title": "Example Story", "type": "article"}
    assert out["twitter"] == {"card": "summary"}
    assert out["headings"] == {"h1": ["Example Story"], "h2": ["Part One"]}
    assert out["word_count"] >= 10


@pytest.mark.asyncio
async def test_page_metadata_rejects_non_html():
    client = _page_client(html="%PDF-1.4 binary", ctype="application/pdf")
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.page_metadata(client, PAGE_URL)


# --- feed digest --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_digest_rss():
    client = _page_client(html=RSS_XML, ctype="application/rss+xml")
    async with client:
        out = await fetch.feed_digest(client, PAGE_URL, 10)
    assert out["feed_title"] == "Example Feed"
    assert out["feed_link"] == "https://example.com/"
    assert out["count"] == 2
    first = out["items"][0]
    assert first["title"] == "First Post"
    assert first["published"] == "Fri, 07 Nov 2026 10:00:00 GMT"
    assert first["summary"] == "The first summary."
    assert "# Example Feed" in out["markdown"]
    assert "## Second Post" in out["markdown"]


@pytest.mark.asyncio
async def test_feed_digest_atom_and_item_cap():
    client = _page_client(html=ATOM_XML, ctype="application/atom+xml")
    async with client:
        out = await fetch.feed_digest(client, PAGE_URL, 1)
    assert out["feed_title"] == "Atom Feed"
    assert out["count"] == 1
    assert out["items"][0]["link"] == "https://example.org/a"
    assert out["items"][0]["author"] == "Jo Example"


@pytest.mark.asyncio
async def test_feed_digest_junk_is_an_upstream_error():
    client = _page_client(html="<html><body>not a feed</body></html>")
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.feed_digest(client, PAGE_URL, 10)


# --- URL unshorten --------------------------------------------------------------------


def _chain_client():
    def handler(request):
        url = str(request.url)
        if url.endswith("/short"):
            return httpx.Response(302, headers={"location": "/mid"})
        if url.endswith("/mid"):
            return httpx.Response(
                301, headers={"location": "https://93.184.216.34/story"}
            )
        return httpx.Response(
            200, text="final", headers={"content-type": "text/html"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_resolve_url_walks_the_chain():
    client = _chain_client()
    async with client:
        out = await fetch.resolve_url(client, "https://93.184.216.34/short")
    assert out["redirects"] == 2
    assert out["final_url"] == "https://93.184.216.34/story"
    assert out["final_status"] == 200
    assert [h["status"] for h in out["hops"]] == [302, 301, 200]
    assert out["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_resolve_url_direct_and_final_404_still_resolve():
    def handler(request):
        return httpx.Response(404, text="gone", headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.resolve_url(client, PAGE_URL)
    assert out["redirects"] == 0
    assert out["final_status"] == 404


@pytest.mark.asyncio
async def test_resolve_url_refuses_private_targets():
    client = _chain_client()
    async with client:
        with pytest.raises(ValueError):
            await fetch.resolve_url(client, "https://127.0.0.1/admin")


# --- repo readme ------------------------------------------------------------------------


def _github_client(readme="# Token Verdict\n\nOne-call verdicts."):
    def handler(request):
        url = str(request.url)
        if url.endswith("/readme"):
            return httpx.Response(
                200,
                json={
                    "path": "README.md",
                    "size": len(readme),
                    "html_url": "https://github.com/QuincyGB/token-verdict/blob/main/README.md",
                    "download_url": "https://raw.githubusercontent.com/QuincyGB/token-verdict/main/README.md",
                },
            )
        if "raw.githubusercontent.com" in url:
            return httpx.Response(200, text=readme)
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_repo_readme_happy_path():
    client = _github_client()
    async with client:
        out = await fetch.repo_readme(client, "QuincyGB/token-verdict", None)
    assert out["repo"] == "QuincyGB/token-verdict"
    assert out["path"] == "README.md"
    assert out["markdown"].startswith("# Token Verdict")
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_repo_readme_caps_and_marks_truncation():
    client = _github_client(readme="x" * 25_000)
    async with client:
        out = await fetch.repo_readme(client, "QuincyGB/token-verdict", None)
    assert out["truncated"] is True
    assert len(out["markdown"]) == 20_000
    assert out["chars"] == 25_000


@pytest.mark.asyncio
async def test_repo_readme_missing_is_an_upstream_error():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.repo_readme(client, "QuincyGB/no-such-repo", None)


# --- HTTP: challenges, pack funding, loop offer -----------------------------------------


def test_lane3_unpaid_challenges_carry_prices():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path, payload, amount in (
        ("/v1/video-transcript", {"video": "dQw4w9WgXcQ"}, "10000"),
        ("/v1/page-metadata", {"url": PAGE_URL}, "5000"),
        ("/v1/feed-digest", {"url": PAGE_URL}, "10000"),
        ("/v1/url-unshorten", {"url": PAGE_URL}, "5000"),
        ("/v1/repo-readme", {"repo": "QuincyGB/token-verdict"}, "5000"),
    ):
        r = client.post(path, json=payload)
        assert r.status_code == 402, (path, r.text)
        amounts = [a.get("amount") for a in r.json().get("accepts", [])]
        assert amount in amounts, (path, r.text)


def _http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    monkeypatch.setattr(main, "_clients", lambda: (_youtube_client(), None, None))
    feed_client = _page_client(html=RSS_XML, ctype="application/rss+xml")
    monkeypatch.setattr(main, "_fetch_client", lambda: feed_client)
    return fresh


def test_toolkit_pack_funds_the_new_shelf_singles(monkeypatch, tmp_path):
    fresh = _http_env(monkeypatch, tmp_path)
    pack, _ = fresh.mint_toolkit_pack("pay-lane3-pack")
    client = TestClient(main.app, raise_server_exceptions=False)

    r = client.post(
        "/v1/video-transcript",
        json={"video": "dQw4w9WgXcQ", "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"].startswith("Welcome back.")
    assert body["pack_credits_remaining"] == 124
    offer_ids = [o["id"] for o in body["verdict_row_loop"]["offers"]]
    assert "toolkit_pack" in offer_ids

    r = client.post(
        "/v1/feed-digest",
        json={"url": PAGE_URL, "pack_id": pack["pack_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["pack_credits_remaining"] == 123


def test_loop_block_utility_offer_covers_lane3_routes():
    for route in ("/v1/video-transcript", "/v1/feed-digest"):
        block = loop_offer.loop_offer(route=route)
        ids = [o["id"] for o in block["offers"]]
        assert "toolkit_pack" in ids, route
        tk = next(o for o in block["offers"] if o["id"] == "toolkit_pack")
        assert "/v1/video-transcript" in tk["terms"]["routes"]
        assert "/v1/feed-digest" in tk["terms"]["routes"]


# --- timedtext wire formats (json3 asked for; XML is YouTube's default) --

SRV1_XML = """<?xml version="1.0" encoding="utf-8"?>
<transcript>
<text start="0.5" dur="2.25">Never gonna give you up,</text>
<text start="2.75" dur="2.0">never gonna let you down</text>
</transcript>"""

SRV3_XML = """<?xml version="1.0" encoding="utf-8"?>
<timedtext format="3"><body>
<p t="500" d="2250"><s>Never gonna give you up,</s></p>
<p t="2750" d="2000"><s>never gonna let you down</s></p>
</body></timedtext>"""


def test_timedtext_srv1_xml_seconds_become_ms():
    segs = fetch._parse_timedtext(SRV1_XML)
    assert [s["text"] for s in segs] == [
        "Never gonna give you up,", "never gonna let you down"]
    assert segs[0]["start_ms"] == 500 and segs[0]["duration_ms"] == 2250


def test_timedtext_srv3_xml_ms_passthrough():
    segs = fetch._parse_timedtext(SRV3_XML)
    assert segs[1]["start_ms"] == 2750 and segs[1]["duration_ms"] == 2000


def test_timedtext_empty_or_junk_is_none():
    assert fetch._parse_timedtext("") is None
    assert fetch._parse_timedtext("  ") is None
    assert fetch._parse_timedtext("<html>error</html>") == []
