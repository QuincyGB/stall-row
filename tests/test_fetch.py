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
