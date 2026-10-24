"""Fetch stalls: YouTube parsing, page→Markdown with the SSRF guard,
GitHub repo vitals — including the failure-honesty paths (upstream down
→ UpstreamError, so the handler 502s and no payment settles)."""

import pytest

from app.rpc import UpstreamError
from app.services import fetch
from tests.fixtures import PAGE_HTML, mock_client


@pytest.mark.asyncio
async def test_tube_search_happy_path():
    client = mock_client()
    async with client:
        out = await fetch.tube_search(client, "x402 tutorial", 5)
    assert out["count"] == 1
    r = out["results"][0]
    assert r["video_id"] == "dQw4w9WgXcQ"
    assert r["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert r["channel"] == "Example Channel"
    assert r["duration"] == "10:24"


@pytest.mark.asyncio
async def test_tube_search_no_results_is_an_error_not_fabrication():
    client = mock_client(youtube_empty=True)
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.tube_search(client, "anything", 5)


def test_ssrf_guard_rejects_non_public_targets():
    for url in (
        "http://127.0.0.1/admin",
        "http://localhost:8545/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "ftp://example.com/file",
    ):
        with pytest.raises(ValueError):
            fetch.ssrf_check(url)


def test_ssrf_guard_allows_public_https(monkeypatch):
    # This sandbox's egress DNS maps public hosts into 198.18.0.0/15
    # (RFC 2544 benchmarking space), which the guard rightly rejects —
    # so stub resolution to a real public IP and test the guard logic,
    # not the sandbox's DNS. Production (Railway) resolves real IPs.
    monkeypatch.setattr(
        fetch.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    fetch.ssrf_check("https://www.x402.org/")  # must not raise


@pytest.mark.asyncio
async def test_page_to_markdown_extracts_content():
    import httpx

    def handler(request):
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/story")
    assert out["title"] == "Example Story"
    assert "main content of the example page" in out["markdown"]
    assert out["word_count"] > 20


@pytest.mark.asyncio
async def test_page_to_markdown_rejects_loopback_before_fetching():
    client = mock_client()
    async with client:
        with pytest.raises(ValueError):
            await fetch.page_to_markdown(client, "http://127.0.0.1:3000/secret")


# --- page→markdown hardening (2026-10-24) -----------------------------------
# The three production failure modes: transient upstream 503, bot-filter
# 403, and a plain-text llms.txt scored as "no main content". Plus the
# guard rails around them: no retries on hard failures, redirects SSRF-
# checked per hop, charset decoding, size cap, content-type honesty.

LLMS_TXT = (
    "# Example Docs\n"
    "\n"
    "> A short description of the example documentation set.\n"
    "\n"
    "## Pages\n"
    "\n"
    "- [Intro](https://93.184.216.34/intro): Start here for the overview\n"
)


def _client(handler):
    import httpx

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(fetch, "_BACKOFF_S", (0, 0))


@pytest.mark.asyncio
async def test_page_to_markdown_serves_plain_text_llms():
    import httpx

    def handler(request):
        return httpx.Response(
            200, text=LLMS_TXT, headers={"content-type": "text/plain; charset=utf-8"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/llms.txt")
    assert out["title"] == "Example Docs"
    assert out["markdown"].startswith("# Example Docs")
    assert "short description of the example documentation" in out["markdown"]
    assert out["word_count"] > 10


@pytest.mark.asyncio
async def test_page_to_markdown_retries_transient_503():
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/story")
    assert calls["n"] == 2
    assert "main content of the example page" in out["markdown"]


@pytest.mark.asyncio
async def test_page_to_markdown_persistent_503_fails_after_retries():
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="upstream busy")

    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="HTTP 503"):
            await fetch.page_to_markdown(client, "https://93.184.216.34/story")
    assert calls["n"] == fetch._FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_page_to_markdown_403_retries_with_descriptive_ua():
    import httpx

    seen_uas = []

    def handler(request):
        ua = request.headers.get("user-agent", "")
        seen_uas.append(ua)
        if "StallRow" not in ua:
            return httpx.Response(403, text="bot filter")
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/story")
    assert len(seen_uas) == 2
    assert "StallRow" in seen_uas[1]
    assert "main content of the example page" in out["markdown"]


@pytest.mark.asyncio
async def test_page_to_markdown_hard_404_fails_without_retry():
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="gone")

    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="HTTP 404"):
            await fetch.page_to_markdown(client, "https://93.184.216.34/missing")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_page_to_markdown_redirect_to_loopback_rejected_before_fetch():
    import httpx

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1:3000/secret"}
        )

    async with _client(handler) as client:
        with pytest.raises(ValueError):
            await fetch.page_to_markdown(client, "https://93.184.216.34/redir")
    assert len(calls) == 1  # the loopback target was never requested


@pytest.mark.asyncio
async def test_page_to_markdown_follows_public_redirect():
    import httpx

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/story"})
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/old")
    assert out["final_url"] == "https://93.184.216.34/story"
    assert "main content of the example page" in out["markdown"]


@pytest.mark.asyncio
async def test_page_to_markdown_decodes_meta_charset():
    import httpx

    body = (
        '<html><head><meta charset="iso-8859-1"><title>Café Über</title></head>'
        "<body><article><h1>Café Über</h1>"
        + "<p>Le café est très bon et le résumé est prêt pour les lecteurs.</p>" * 6
        + "</article></body></html>"
    ).encode("iso-8859-1")

    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/cafe")
    assert out["title"] == "Café Über"
    assert "café" in out["markdown"]


@pytest.mark.asyncio
async def test_page_to_markdown_size_cap(monkeypatch):
    import httpx

    monkeypatch.setattr(fetch, "_MAX_PAGE_BYTES", 1024)

    def handler(request):
        return httpx.Response(
            200, text="x" * 4096, headers={"content-type": "text/plain"}
        )

    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="size limit"):
            await fetch.page_to_markdown(client, "https://93.184.216.34/big")


@pytest.mark.asyncio
async def test_page_to_markdown_html_served_as_text_plain_still_extracts():
    import httpx

    def handler(request):
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/plain"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/story")
    assert out["title"] == "Example Story"
    assert "main content of the example page" in out["markdown"]


@pytest.mark.asyncio
async def test_page_to_markdown_unsupported_type_is_an_honest_error():
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers={"content-type": "application/pdf"},
        )

    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="unsupported content type"):
            await fetch.page_to_markdown(client, "https://93.184.216.34/doc.pdf")


@pytest.mark.asyncio
async def test_page_to_markdown_visible_text_fallback(monkeypatch):
    import httpx
    import trafilatura

    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    loose = (
        "<html><head><title>Loose</title></head><body><div>"
        + "Loose text sitting directly in a div with no paragraph structure "
          "at all, just words flowing on for the reader to consume fully. " * 4
        + "</div></body></html>"
    )

    def handler(request):
        return httpx.Response(
            200, text=loose, headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        out = await fetch.page_to_markdown(client, "https://93.184.216.34/loose")
    assert "Loose text sitting directly in a div" in out["markdown"]
    assert out["word_count"] >= fetch._MIN_FALLBACK_WORDS


@pytest.mark.asyncio
async def test_repo_vitals_happy_path():
    client = mock_client()
    async with client:
        out = await fetch.repo_vitals(client, "QuincyGB/token-verdict", None)
    assert out["repo"] == "QuincyGB/token-verdict"
    assert out["stars"] == 3
    assert out["license"] == "MIT"
    assert out["archived"] is False
    assert 0 <= out["health_score"] <= 100


@pytest.mark.asyncio
async def test_repo_vitals_accepts_github_url():
    client = mock_client()
    async with client:
        out = await fetch.repo_vitals(client, "https://github.com/QuincyGB/token-verdict", None)
    assert out["repo"] == "QuincyGB/token-verdict"


@pytest.mark.asyncio
async def test_repo_vitals_404_is_upstream_error():
    client = mock_client(github_404=True)
    async with client:
        with pytest.raises(UpstreamError):
            await fetch.repo_vitals(client, "nobody/nothing", None)


def test_parse_repo_input_rejects_garbage():
    with pytest.raises(ValueError):
        fetch.parse_repo_input("not a repo")
