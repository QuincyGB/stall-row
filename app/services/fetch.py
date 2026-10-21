"""The fetch stalls: YouTube search scraping, page→Markdown, GitHub
repo vitals. Honesty rule: an upstream failure raises UpstreamError —
the handler returns 502 and the x402 middleware does not settle, so a
buyer is never charged for undelivered data.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import httpx

from ..rpc import UpstreamError

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


# --- YouTube search -------------------------------------------------------------

def parse_youtube_results(html: str, max_results: int) -> list[dict]:
    """Extract video results from a YouTube search page's ytInitialData."""
    marker = "ytInitialData"
    idx = html.find(marker)
    if idx < 0:
        raise UpstreamError("YouTube search page did not contain ytInitialData")
    start = html.find("{", idx)
    if start < 0:
        raise UpstreamError("YouTube search page: malformed ytInitialData")
    depth = 0
    end = None
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise UpstreamError("YouTube search page: unterminated ytInitialData")
    try:
        data = json.loads(html[start:end])
    except ValueError as e:
        raise UpstreamError(f"YouTube search page: ytInitialData not JSON: {e}") from e

    results: list[dict] = []

    def walk(node):
        if len(results) >= max_results:
            return
        if isinstance(node, dict):
            vr = node.get("videoRenderer")
            if isinstance(vr, dict) and vr.get("videoId"):
                title = ""
                try:
                    title = vr["title"]["runs"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    title = (vr.get("title") or {}).get("simpleText", "")
                channel = ""
                try:
                    channel = vr["ownerText"]["runs"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    pass
                results.append(
                    {
                        "title": title,
                        "video_id": vr["videoId"],
                        "url": f"https://www.youtube.com/watch?v={vr['videoId']}",
                        "channel": channel or None,
                        "duration": (vr.get("lengthText") or {}).get("simpleText"),
                        "views": (vr.get("viewCountText") or {}).get("simpleText"),
                    }
                )
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return results


async def tube_search(client: httpx.AsyncClient, query: str, max_results: int) -> dict:
    try:
        resp = await client.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers=_UA,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        raise UpstreamError(f"YouTube fetch failed: {e}") from e
    if resp.status_code != 200:
        raise UpstreamError(f"YouTube returned HTTP {resp.status_code}")
    results = parse_youtube_results(resp.text, max_results)
    if not results:
        raise UpstreamError("YouTube page parsed but contained no video results")
    return {"query": query, "count": len(results), "results": results}


# --- page → markdown ---------------------------------------------------------------

def ssrf_check(url: str) -> None:
    """Reject non-http(s) URLs and any host resolving to a private,
    loopback, link-local, reserved, or multicast address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"host does not resolve: {host}") from e
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"URL host resolves to a non-public address ({ip})")


async def page_to_markdown(client: httpx.AsyncClient, url: str) -> dict:
    ssrf_check(url)
    try:
        resp = await client.get(url, headers=_UA, follow_redirects=True)
    except httpx.HTTPError as e:
        raise UpstreamError(f"page fetch failed: {e}") from e
    if resp.status_code != 200:
        raise UpstreamError(f"page returned HTTP {resp.status_code}")
    final_url = str(resp.url)
    if final_url != url:
        ssrf_check(final_url)  # redirect targets get the same treatment
    html = resp.text
    import trafilatura

    markdown = trafilatura.extract(
        html, output_format="markdown", include_links=True, include_images=False
    )
    if not markdown:
        raise UpstreamError("no main content could be extracted from the page")
    title = None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip() or None
    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "markdown": markdown,
        "word_count": len(markdown.split()),
    }


# --- GitHub repo vitals -------------------------------------------------------------

def parse_repo_input(repo: str) -> str:
    repo = repo.strip()
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", repo)
    if m:
        return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    parts = [p for p in repo.split("/") if p]
    if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
        return repo
    raise ValueError("repo must be 'owner/name' or a github.com URL")


async def repo_vitals(
    client: httpx.AsyncClient, repo: str, github_token: str | None
) -> dict:
    full = parse_repo_input(repo)
    headers = {"Accept": "application/vnd.github+json", **_UA}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        resp = await client.get(f"https://api.github.com/repos/{full}", headers=headers)
    except httpx.HTTPError as e:
        raise UpstreamError(f"GitHub fetch failed: {e}") from e
    if resp.status_code == 404:
        raise UpstreamError(f"GitHub repo not found: {full}")
    if resp.status_code != 200:
        raise UpstreamError(f"GitHub returned HTTP {resp.status_code}")
    body = resp.json()

    stars = body.get("stargazers_count") or 0
    forks = body.get("forks_count") or 0
    issues = body.get("open_issues_count") or 0
    archived = bool(body.get("archived"))
    pushed_at = body.get("pushed_at")

    score = 50
    notes = []
    if archived:
        score -= 40
        notes.append("Repository is archived (read-only).")
    if stars >= 1000:
        score += 20
        notes.append("Well-starred (≥1,000).")
    elif stars >= 100:
        score += 10
        notes.append("Moderately starred (≥100).")
    elif stars < 10:
        score -= 5
        notes.append("Very few stars (<10).")
    if forks >= 100:
        score += 5
    score = max(0, min(100, score))
    if archived:
        verdict = "Repository is archived — no ongoing development."
    elif score >= 70:
        verdict = "Healthy, established repository."
    elif score >= 45:
        verdict = "Active-looking repository with a modest footprint."
    else:
        verdict = "Thin footprint — small or quiet repository."
    return {
        "repo": full,
        "url": body.get("html_url"),
        "description": body.get("description"),
        "stars": stars,
        "forks": forks,
        "open_issues": issues,
        "license": (body.get("license") or {}).get("spdx_id"),
        "pushed_at": pushed_at,
        "created_at": body.get("created_at"),
        "archived": archived,
        "health_score": score,
        "health_notes": notes,
        "verdict": verdict,
    }
