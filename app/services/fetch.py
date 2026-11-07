"""The fetch stalls: YouTube search scraping, page→Markdown, GitHub
repo vitals. Honesty rule: an upstream failure raises UpstreamError —
the handler returns 502 and the x402 middleware does not settle, so a
buyer is never charged for undelivered data.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from html import unescape as html_unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..rpc import UpstreamError

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


# --- YouTube search -------------------------------------------------------------
#
# Upgrade (buyer analysis 2026-11-05): the $0.01 utility point is the
# fleet's volume leader, and the route serving it was the shallowest on
# the shelf — page one only, five results by default, no way to narrow.
# Search now returns the publish age + a derived thumbnail for every
# result (both already on the page, previously dropped), accepts
# duration / upload-recency / channel filters applied BEFORE truncation
# (so a filter never eats the result budget), digs one continuation
# page deeper when max_results outruns page one, and retries the fetch
# once on transient failure — the discipline page_to_markdown got on
# 2026-10-24, which this route never received.

_TUBE_CANDIDATE_CAP = 60  # parsed per page before filters truncate
_DURATION_BUCKETS = ("short", "medium", "long")
_UPLOADED_WITHIN_S = {
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}
_AGE_UNIT_S = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}


def _extract_js_object(html: str, marker: str, label: str) -> dict:
    """Brace-scan the JSON object assigned after `marker` in a YouTube
    page (ytInitialData on results pages, ytInitialPlayerResponse on
    watch pages)."""
    idx = html.find(marker)
    if idx < 0:
        raise UpstreamError(f"{label} did not contain {marker}")
    start = html.find("{", idx)
    if start < 0:
        raise UpstreamError(f"{label}: malformed {marker}")
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
        raise UpstreamError(f"{label}: unterminated {marker}")
    try:
        return json.loads(html[start:end])
    except ValueError as e:
        raise UpstreamError(f"{label}: {marker} not JSON: {e}") from e


def _renderer_text(node) -> str:
    """simpleText, or the first run's text, of a YouTube text node."""
    if not isinstance(node, dict):
        return ""
    simple = node.get("simpleText")
    if isinstance(simple, str):
        return simple
    runs = node.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return runs[0].get("text") or ""
    return ""


def _duration_seconds(text: str | None) -> int | None:
    """'10:24' / '1:02:03' -> seconds. Live streams carry no length
    text — None, which any duration filter excludes (documented)."""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) not in (2, 3):
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _published_age_seconds(text: str | None) -> int | None:
    """'3 days ago' / 'Streamed 2 weeks ago' -> age in seconds."""
    if not text:
        return None
    m = re.search(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNIT_S[m.group(2).lower()]


def _parse_video_renderer(vr: dict) -> dict:
    video_id = vr["videoId"]
    duration = _renderer_text(vr.get("lengthText")) or None
    return {
        "title": _renderer_text(vr.get("title")),
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": _renderer_text(vr.get("ownerText")) or None,
        "duration": duration,
        "duration_seconds": _duration_seconds(duration),
        "views": _renderer_text(vr.get("viewCountText")) or None,
        "published": _renderer_text(vr.get("publishedTimeText")) or None,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def _collect_renderers(node, out: list[dict], cap: int) -> None:
    if len(out) >= cap:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            out.append(_parse_video_renderer(vr))
            return
        for v in node.values():
            _collect_renderers(v, out, cap)
    elif isinstance(node, list):
        for v in node:
            _collect_renderers(v, out, cap)


def _find_continuation_token(node) -> str | None:
    if isinstance(node, dict):
        cir = node.get("continuationItemRenderer")
        if isinstance(cir, dict):
            token = (
                ((cir.get("continuationEndpoint") or {}).get("continuationCommand") or {})
                .get("token")
            )
            if isinstance(token, str) and token:
                return token
        for v in node.values():
            found = _find_continuation_token(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_continuation_token(v)
            if found:
                return found
    return None


def parse_youtube_results(html: str, max_results: int) -> list[dict]:
    """Extract video results from a YouTube search page's ytInitialData."""
    data = _extract_js_object(html, "ytInitialData", "YouTube search page")
    results: list[dict] = []
    _collect_renderers(data, results, max_results)
    return results


def _tube_filter(
    results: list[dict],
    *,
    duration: str | None,
    uploaded_within: str | None,
    channel: str | None,
) -> list[dict]:
    out = []
    for r in results:
        if duration is not None:
            secs = r.get("duration_seconds")
            if secs is None:
                continue
            bucket = "short" if secs < 240 else "medium" if secs <= 1200 else "long"
            if bucket != duration:
                continue
        if uploaded_within is not None:
            age = _published_age_seconds(r.get("published"))
            if age is None or age > _UPLOADED_WITHIN_S[uploaded_within]:
                continue
        if channel is not None:
            if channel.lower() not in (r.get("channel") or "").lower():
                continue
        out.append(r)
    return out


async def _youtube_get(
    client: httpx.AsyncClient, url: str, params: dict | None = None
) -> httpx.Response:
    """GET a YouTube page with one retry on transient failure (the
    discipline page_to_markdown's fetch got in Oct 2026; this route
    previously made a single attempt and 502'd the buyer)."""
    last_error: UpstreamError | None = None
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(0.5)
        try:
            resp = await client.get(
                url, params=params, headers=_UA, follow_redirects=True
            )
        except httpx.HTTPError as e:
            last_error = UpstreamError(f"YouTube fetch failed: {e}")
            continue
        if resp.status_code == 200:
            return resp
        last_error = UpstreamError(f"YouTube returned HTTP {resp.status_code}")
    assert last_error is not None
    raise last_error


async def _tube_continuation(
    client: httpx.AsyncClient, first_page_html: str, token: str
) -> list[dict]:
    """One continuation page of search results via the page's own
    Innertube key. Best-effort depth: any failure returns [] and the
    caller keeps the page-one results it already has."""
    key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', first_page_html)
    if not key_m:
        return []
    ver_m = re.search(
        r'"INNERTUBE_CONTEXT_CLIENT_VERSION"\s*:\s*"([^"]+)"', first_page_html
    )
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": ver_m.group(1) if ver_m else "2.20241113.07.00",
                "hl": "en",
            }
        },
        "continuation": token,
    }
    try:
        resp = await client.post(
            "https://www.youtube.com/youtubei/v1/search",
            params={"key": key_m.group(1)},
            json=payload,
            headers=_UA,
        )
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    out: list[dict] = []
    _collect_renderers(data, out, _TUBE_CANDIDATE_CAP)
    return out


async def tube_search(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    duration: str | None = None,
    uploaded_within: str | None = None,
    channel: str | None = None,
) -> dict:
    if duration is not None and duration not in _DURATION_BUCKETS:
        raise ValueError(
            f"duration must be one of {', '.join(_DURATION_BUCKETS)}"
        )
    if uploaded_within is not None and uploaded_within not in _UPLOADED_WITHIN_S:
        raise ValueError(
            f"uploaded_within must be one of {', '.join(_UPLOADED_WITHIN_S)}"
        )
    resp = await _youtube_get(
        client, "https://www.youtube.com/results", params={"search_query": query}
    )
    data = _extract_js_object(resp.text, "ytInitialData", "YouTube search page")
    candidates: list[dict] = []
    _collect_renderers(data, candidates, _TUBE_CANDIDATE_CAP)
    results = _tube_filter(
        candidates,
        duration=duration,
        uploaded_within=uploaded_within,
        channel=channel,
    )
    if len(results) < max_results:
        token = _find_continuation_token(data)
        if token:
            seen = {r["video_id"] for r in candidates}
            for extra in await _tube_continuation(client, resp.text, token):
                if extra["video_id"] not in seen:
                    candidates.append(extra)
                    seen.add(extra["video_id"])
            results = _tube_filter(
                candidates,
                duration=duration,
                uploaded_within=uploaded_within,
                channel=channel,
            )
    results = results[:max_results]
    if not results:
        raise UpstreamError(
            "YouTube search returned no video results matching the request"
        )
    out: dict = {"query": query, "count": len(results), "results": results}
    applied = {
        k: v
        for k, v in (
            ("duration", duration),
            ("uploaded_within", uploaded_within),
            ("channel", channel),
        )
        if v is not None
    }
    if applied:
        out["filters_applied"] = applied
    return out


async def tube_search_batch(
    client: httpx.AsyncClient,
    queries: list[str],
    max_results: int,
    *,
    duration: str | None = None,
    uploaded_within: str | None = None,
    channel: str | None = None,
) -> dict:
    """Batch shape (buyer analysis 2026-11-05): the largest stranger
    event on record was 20 single searches in ~30 seconds — batch
    behaviour in one-call clothing. Each query runs the single-search
    path; a failed query is reported in its own entry instead of
    failing the batch, and the batch only fails (502, no settlement)
    when EVERY query fails."""
    sem = asyncio.Semaphore(3)

    async def one(q: str) -> dict:
        async with sem:
            try:
                r = await tube_search(
                    client,
                    q,
                    max_results,
                    duration=duration,
                    uploaded_within=uploaded_within,
                    channel=channel,
                )
                return {"query": q, "count": r["count"], "results": r["results"]}
            except (UpstreamError, ValueError) as e:
                return {"query": q, "error": str(e)}

    entries = await asyncio.gather(*(one(q) for q in queries))
    succeeded = sum(1 for e in entries if "error" not in e)
    if succeeded == 0:
        raise UpstreamError("every query in the batch failed")
    return {
        "count": len(entries),
        "succeeded": succeeded,
        "results": entries,
    }


# --- YouTube video details --------------------------------------------------------
#
# The natural second call (buyer analysis 2026-11-05): search returns
# ids; everything an agent needs to decide whether a video is worth
# using — full description, publish date, caption availability — sits
# behind a call the shelf didn't sell. Parsed from the watch page's
# ytInitialPlayerResponse, no API key, same parse discipline as search.

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_input(value: str) -> str:
    """Accept an 11-char video id or any common YouTube URL shape
    (watch / youtu.be / shorts / live / embed) and return the id."""
    v = (value or "").strip()
    if _VIDEO_ID_RE.match(v):
        return v
    m = re.search(
        r"(?:youtube\.com/(?:watch\?[^\"'\s]*v=|shorts/|live/|embed/|v/)|youtu\.be/)"
        r"([A-Za-z0-9_-]{11})",
        v,
    )
    if m:
        return m.group(1)
    raise ValueError(
        "video must be an 11-character YouTube video id or a "
        "youtube.com / youtu.be URL"
    )


async def video_details(client: httpx.AsyncClient, video: str) -> dict:
    video_id = parse_video_input(video)
    resp = await _youtube_get(
        client, "https://www.youtube.com/watch", params={"v": video_id}
    )
    pr = _extract_js_object(
        resp.text, "ytInitialPlayerResponse", "YouTube watch page"
    )
    playability = pr.get("playabilityStatus") or {}
    if playability.get("status") != "OK":
        reason = playability.get("reason") or playability.get("status") or "unknown"
        raise UpstreamError(f"video unavailable: {reason}")
    vd = pr.get("videoDetails") or {}
    micro = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}

    duration_seconds = None
    raw_len = vd.get("lengthSeconds")
    if isinstance(raw_len, str) and raw_len.isdigit():
        duration_seconds = int(raw_len)
    elif isinstance(raw_len, int):
        duration_seconds = raw_len

    views = None
    raw_views = vd.get("viewCount")
    if isinstance(raw_views, str) and raw_views.isdigit():
        views = int(raw_views)
    elif isinstance(raw_views, int):
        views = raw_views

    thumbnails = ((vd.get("thumbnail") or {}).get("thumbnails")) or []
    thumbnail = None
    if thumbnails and isinstance(thumbnails[-1], dict):
        thumbnail = thumbnails[-1].get("url")
    if not thumbnail:
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    caption_tracks = (
        ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
        .get("captionTracks")
    ) or []
    captions = []
    for track in caption_tracks:
        if not isinstance(track, dict):
            continue
        captions.append(
            {
                "language_code": track.get("languageCode"),
                "language": _renderer_text(track.get("name")) or None,
                "kind": track.get("kind"),
            }
        )

    description = vd.get("shortDescription") or ""
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": vd.get("title"),
        "channel": vd.get("author"),
        "channel_id": vd.get("channelId"),
        "duration_seconds": duration_seconds,
        "duration": _format_duration(duration_seconds)
        if duration_seconds is not None
        else None,
        "views": views,
        "published": micro.get("publishDate") or micro.get("uploadDate"),
        "description": description[:4000],
        "keywords": (vd.get("keywords") or [])[:15],
        "thumbnail": thumbnail,
        "is_live": bool(vd.get("isLiveContent")),
        "captions_available": bool(captions),
        "captions": captions,
    }


# --- YouTube video transcript ---------------------------------------------------
#
# Lane 3 (distribution pivot 2026-11-07): the shelf's third YouTube
# leg. Video Details reports WHICH caption tracks exist; this route
# returns the words themselves — the full caption text with timed
# segments, fetched from the chosen track's own timedtext URL in
# json3 form. Same honesty rule as every fetch stall: no tracks, an
# unreadable track, or an empty transcript is an UpstreamError (502,
# no settlement), never a fabricated or silently-wrong-language
# delivery — a language the video doesn't carry is a caller fault
# (ValueError, 422) naming the languages it does carry.

_TRANSCRIPT_TEXT_CAP = 60_000
_TRANSCRIPT_SEGMENT_CAP = 1_000


def _pick_caption_track(tracks: list, lang: str | None) -> dict:
    """Choose the caption track to deliver: an explicit language wins
    (exact code, then same primary language); otherwise a human-made
    track beats an auto-generated (asr) one, else the first usable."""
    usable = [
        t for t in tracks if isinstance(t, dict) and t.get("baseUrl")
    ]
    if not usable:
        raise UpstreamError("video has no usable caption tracks")
    if lang is not None:
        want = lang.strip().lower()
        for t in usable:
            if (t.get("languageCode") or "").lower() == want:
                return t
        for t in usable:
            code = (t.get("languageCode") or "").lower()
            if code.split("-")[0] == want.split("-")[0]:
                return t
        available = sorted(
            {t.get("languageCode") for t in usable if t.get("languageCode")}
        )
        raise ValueError(
            f"no caption track in language {lang!r}; "
            f"available: {', '.join(available) or 'none'}"
        )
    manual = [t for t in usable if t.get("kind") != "asr"]
    return (manual or usable)[0]


async def video_transcript(
    client: httpx.AsyncClient, video: str, *, lang: str | None = None
) -> dict:
    video_id = parse_video_input(video)
    resp = await _youtube_get(
        client, "https://www.youtube.com/watch", params={"v": video_id}
    )
    pr = _extract_js_object(
        resp.text, "ytInitialPlayerResponse", "YouTube watch page"
    )
    playability = pr.get("playabilityStatus") or {}
    if playability.get("status") != "OK":
        reason = playability.get("reason") or playability.get("status") or "unknown"
        raise UpstreamError(f"video unavailable: {reason}")
    tracks = (
        ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
        .get("captionTracks")
    ) or []
    if not tracks:
        raise UpstreamError("video has no caption tracks")
    track = _pick_caption_track(tracks, lang)
    base = track["baseUrl"]
    sep = "&" if "?" in base else "?"
    timed = await _youtube_get(client, base + sep + "fmt=json3")
    try:
        data = timed.json()
    except ValueError as e:
        raise UpstreamError("caption track returned unreadable data") from e
    events = data.get("events") if isinstance(data, dict) else None
    if not events:
        raise UpstreamError("caption track returned no transcript data")
    segments: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        text = "".join(
            s.get("utf8", "")
            for s in (event.get("segs") or [])
            if isinstance(s, dict)
        )
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        segments.append(
            {
                "start_ms": event.get("tStartMs"),
                "duration_ms": event.get("dDurationMs"),
                "text": text,
            }
        )
    if not segments:
        raise UpstreamError("caption track returned an empty transcript")
    full_text = " ".join(s["text"] for s in segments)
    truncated = (
        len(full_text) > _TRANSCRIPT_TEXT_CAP
        or len(segments) > _TRANSCRIPT_SEGMENT_CAP
    )
    vd = pr.get("videoDetails") or {}
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": vd.get("title"),
        "language_code": track.get("languageCode"),
        "language": _renderer_text(track.get("name")) or None,
        "kind": track.get("kind"),
        "segment_count": len(segments),
        "char_count": len(full_text),
        "transcript": full_text[:_TRANSCRIPT_TEXT_CAP],
        "segments": segments[:_TRANSCRIPT_SEGMENT_CAP],
        "truncated": truncated,
    }


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


# Fetch hardening (2026-10-24): production paid calls failed three ways —
# a transient upstream 503 (example.com), a bot-filter 403 (Wikipedia),
# and a plain-text llms.txt that the HTML extractor scored as "no main
# content". The machinery below retries transient failures with backoff,
# answers a 403 with the descriptive User-Agent robot policies ask for,
# follows redirects manually so every hop is SSRF-checked before it is
# fetched, caps the body size, decodes charsets explicitly, and serves
# text/plain + text/markdown bodies as text instead of failing them.

# A descriptive UA, used after a 403: Wikimedia's robot policy (and many
# others) refuses impersonating browser UAs from datacenter egress but
# accepts an identified bot.
_DESCRIPTIVE_UA = {
    "User-Agent": (
        "StallRow/1.0 (+https://stall-row-production.up.railway.app; "
        "page-to-markdown fetcher)"
    )
}
_PAGE_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "text/plain;q=0.8,text/markdown;q=0.8,*/*;q=0.5"
)
_PAGE_HEADERS_BASE = {"Accept": _PAGE_ACCEPT, "Accept-Language": "en-US,en;q=0.9"}

_MAX_PAGE_BYTES = 5 * 1024 * 1024  # fail cleanly past 5 MiB
_MAX_REDIRECTS = 5
_FETCH_ATTEMPTS = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_BACKOFF_S = (0.5, 1.5)

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
_MIN_FALLBACK_WORDS = 15


async def _read_capped(resp: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > _MAX_PAGE_BYTES:
            raise UpstreamError(
                f"page exceeds the {_MAX_PAGE_BYTES // (1024 * 1024)} MiB size limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_following_redirects(
    client: httpx.AsyncClient, url: str, headers: dict
) -> tuple[str, int, httpx.Headers, bytes | None]:
    """One GET with manual redirect following: every redirect target is
    SSRF-checked BEFORE it is fetched (client-level following would only
    let us check the final URL, after the target was already contacted).
    The body is read (size-capped) only on a 200."""
    current = url
    for hop in range(_MAX_REDIRECTS + 1):
        async with client.stream(
            "GET", current, headers=headers, follow_redirects=False
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise UpstreamError(
                        f"page returned HTTP {resp.status_code} without a Location header"
                    )
                if hop == _MAX_REDIRECTS:
                    raise UpstreamError("page redirected too many times")
                target = urljoin(current, location)
                # getaddrinfo is blocking DNS — keep it off the event loop.
                await asyncio.to_thread(ssrf_check, target)
                current = target
                continue
            body = await _read_capped(resp) if resp.status_code == 200 else None
            return current, resp.status_code, resp.headers, body
    raise UpstreamError("page redirected too many times")  # pragma: no cover


async def _fetch_page(
    client: httpx.AsyncClient, url: str
) -> tuple[str, httpx.Headers, bytes]:
    """Fetch one page with retries. Transient statuses (429/5xx) and
    transport errors retry with backoff under the browser UA; a 403
    (bot filter) switches the next attempt to the descriptive UA.
    Deterministic failures (hard 404, dead host after retries, …) raise
    UpstreamError, so the handler 502s and no payment settles."""
    use_descriptive = False
    last_error = "page fetch failed"
    for attempt in range(_FETCH_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_BACKOFF_S[attempt - 1])
        headers = {
            **(_DESCRIPTIVE_UA if use_descriptive else _UA),
            **_PAGE_HEADERS_BASE,
        }
        try:
            final_url, status, resp_headers, body = await _get_following_redirects(
                client, url, headers
            )
        except httpx.HTTPError as e:
            last_error = f"page fetch failed: {e}"
            continue
        if status == 200:
            assert body is not None  # read exactly on 200 above
            return final_url, resp_headers, body
        last_error = f"page returned HTTP {status}"
        if status == 403:
            use_descriptive = True
            continue
        if status in _RETRY_STATUSES:
            continue
        raise UpstreamError(last_error)
    raise UpstreamError(last_error)


def _decode_body(body: bytes, headers: httpx.Headers) -> str:
    """Decode with the declared charset, then a <meta charset>, then
    UTF-8; never raise on a bad label or a stray byte."""
    charset = None
    m = re.search(
        r"charset=([\w.:-]+)", headers.get("content-type", ""), re.IGNORECASE
    )
    if m:
        charset = m.group(1)
    if not charset:
        head = body[:4096].decode("ascii", errors="ignore")
        m = re.search(r"<meta[^>]+charset=[\"']?([\w.:-]+)", head, re.IGNORECASE)
        if m:
            charset = m.group(1)
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return head.startswith(("<!doctype html", "<html")) or "<html" in head[:256]


def _normalize_text(text: str) -> str:
    """Light normalization for plain-text deliveries: unify newlines,
    trim trailing spaces, collapse 2+ blank lines to one."""
    lines = [
        ln.rstrip()
        for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks == 1:
                out.append("")
    return "\n".join(out).strip("\n")


def _text_title(normalized: str) -> str | None:
    """A Markdown document's leading '# ' heading is its title."""
    for ln in normalized.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("# "):
            return s[2:].strip() or None
        return None
    return None


class _VisibleText(HTMLParser):
    """Collect visible text with block boundaries as newlines; script,
    style, head and template content is not visible text."""

    _SKIP = frozenset({"script", "style", "head", "template", "svg"})
    _BLOCK = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "dd", "div",
            "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
            "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
            "main", "nav", "ol", "p", "pre", "section", "table", "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _visible_text(html_text: str) -> str:
    parser = _VisibleText()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — malformed HTML: keep what parsed
        pass
    raw = "".join(parser.parts)
    squashed = "\n".join(
        re.sub(r"[ \t]+", " ", ln) for ln in raw.split("\n")
    )
    return _normalize_text(squashed)


def _extract_markdown(text: str) -> str | None:
    """Main-content extraction. CPU-bound (trafilatura heuristics, with
    the visible-text fallback), so page_to_markdown runs it via
    asyncio.to_thread: on the event loop it would stall every other
    route sharing this process for the length of the crunch."""
    import trafilatura

    markdown = trafilatura.extract(
        text, output_format="markdown", include_links=True, include_images=False
    )
    if not markdown:
        # Trafilatura's heuristics reject some real pages (odd structure,
        # link-dense indexes). Fall back to the page's visible text —
        # but only when there is enough of it to be a delivery, not
        # boilerplate dressed up as content.
        fallback = _visible_text(text)
        if len(fallback.split()) >= _MIN_FALLBACK_WORDS:
            markdown = fallback
    return markdown


async def _page_to_markdown_direct(client: httpx.AsyncClient, url: str) -> dict:
    await asyncio.to_thread(ssrf_check, url)  # blocking DNS — off the loop
    final_url, headers, body = await _fetch_page(client, url)
    text = _decode_body(body, headers)
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()

    if ctype in _TEXT_TYPES and not _looks_like_html(text):
        markdown = _normalize_text(text)
        if not markdown:
            raise UpstreamError("page returned no readable text content")
        return {
            "url": url,
            "final_url": final_url,
            "title": _text_title(markdown),
            "markdown": markdown,
            "word_count": len(markdown.split()),
        }

    if (
        ctype
        and ctype not in _HTML_TYPES
        and ctype not in _TEXT_TYPES
        and not _looks_like_html(text)
    ):
        raise UpstreamError(f"unsupported content type: {ctype}")

    markdown = await asyncio.to_thread(_extract_markdown, text)
    if not markdown:
        raise UpstreamError("no main content could be extracted from the page")
    title = None
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        cleaned = html_unescape(re.sub(r"<[^>]+>", "", m.group(1)))
        title = re.sub(r"\s+", " ", cleaned).strip() or None
    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "markdown": markdown,
        "word_count": len(markdown.split()),
    }


# Reader-proxy fallback (buyer analysis 2026-11-05): the one delivery
# failure class the 2026-10-24 hardening could not close is the
# JS-challenge / consent wall — both fetch identities are refused
# before extraction is even reached, and the service runs no headless
# browser. As a LAST resort (direct pipeline failed end-to-end), one
# attempt through a public reader proxy; the delivery is marked
# in-band (fallback="reader-proxy") so it is never mistaken for a
# direct extraction. Best-effort by design: the proxy is outside our
# control, so any failure there simply re-raises the direct error and
# the buyer keeps the honest 502 / no-settlement outcome. The target
# URL was SSRF-checked by the direct path before this ever runs, and a
# ValueError (SSRF / bad URL) never reaches the fallback at all.
_READER_PROXY_PREFIX = "https://r.jina.ai/"


async def _reader_fallback(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(
            _READER_PROXY_PREFIX + url,
            headers={**_UA, "Accept": "text/plain"},
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    markdown = _normalize_text(resp.text)
    if len(markdown.split()) < _MIN_FALLBACK_WORDS:
        return None
    return {
        "url": url,
        "final_url": url,
        "title": _text_title(markdown),
        "markdown": markdown,
        "word_count": len(markdown.split()),
        "fallback": "reader-proxy",
    }


async def page_to_markdown(client: httpx.AsyncClient, url: str) -> dict:
    try:
        return await _page_to_markdown_direct(client, url)
    except UpstreamError:
        fallback = await _reader_fallback(client, url)
        if fallback is not None:
            return fallback
        raise


async def page_to_markdown_batch(client: httpx.AsyncClient, urls: list[str]) -> dict:
    """Batch shape (buyer analysis 2026-11-05) — the page-side twin of
    tube_search_batch: up to 10 URLs converted in one call, each with
    the full single-route pipeline (reader-proxy fallback included).
    A failed URL is reported in its own entry instead of failing the
    batch; the batch only fails (502, no settlement) when EVERY URL
    fails."""
    sem = asyncio.Semaphore(3)

    async def one(u: str) -> dict:
        async with sem:
            try:
                return await page_to_markdown(client, u)
            except (UpstreamError, ValueError) as e:
                return {"url": u, "error": str(e)}

    entries = await asyncio.gather(*(one(u) for u in urls))
    succeeded = sum(1 for e in entries if "error" not in e)
    if succeeded == 0:
        raise UpstreamError("every URL in the batch failed")
    return {
        "count": len(entries),
        "succeeded": succeeded,
        "results": entries,
    }


# --- page metadata ---------------------------------------------------------------
#
# Lane 3 (distribution pivot 2026-11-07): the cheap head-read twin of
# page-to-markdown. Buyers deciding WHETHER a page is worth the $0.01
# conversion get its declared identity first — title, description,
# canonical, OpenGraph / Twitter card fields, language, favicon, and
# the heading outline — through the same hardened, SSRF-checked fetch
# pipeline, parsed in one pass. A non-HTML delivery is not metadata
# and fails honestly (502, no settlement).

class _HeadMeta(HTMLParser):
    """One-pass collector for a page's declared metadata: <title>,
    meta name/property pairs, canonical + icon links, the document
    language, and the h1/h2 outline."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.title: str | None = None
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.favicon: str | None = None
        self.headings: dict[str, list[str]] = {"h1": [], "h2": []}
        self._capture: str | None = None  # "title" | "h1" | "h2"
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang") or self.lang
        elif tag == "title":
            self._capture, self._buf = "title", []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "").strip()
            if not href:
                return
            if rel == "canonical" and self.canonical is None:
                self.canonical = href
            elif (
                rel in ("icon", "shortcut icon", "apple-touch-icon")
                and self.favicon is None
            ):
                self.favicon = href
        elif tag in ("h1", "h2"):
            self._capture, self._buf = tag, []

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or tag != self._capture:
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if tag == "title":
            self.title = text or self.title
        elif text:
            cap = 10 if tag == "h1" else 20
            if len(self.headings[tag]) < cap:
                self.headings[tag].append(text)
        self._capture, self._buf = None, []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)


def _parse_page_metadata(text: str, final_url: str) -> dict:
    parser = _HeadMeta()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 — malformed HTML: keep what parsed
        pass
    meta = parser.meta
    og = {
        k[len("og:"):]: v for k, v in meta.items() if k.startswith("og:")
    }
    twitter = {
        k[len("twitter:"):]: v
        for k, v in meta.items()
        if k.startswith("twitter:")
    }
    description = meta.get("description") or og.get("description")
    canonical = (
        urljoin(final_url, parser.canonical) if parser.canonical else None
    )
    favicon = urljoin(final_url, parser.favicon) if parser.favicon else None
    return {
        "title": parser.title,
        "description": description,
        "canonical": canonical,
        "lang": parser.lang,
        "favicon": favicon,
        "og": og,
        "twitter": twitter,
        "headings": parser.headings,
        "word_count": len(_visible_text(text).split()),
    }


async def page_metadata(client: httpx.AsyncClient, url: str) -> dict:
    await asyncio.to_thread(ssrf_check, url)  # blocking DNS — off the loop
    final_url, headers, body = await _fetch_page(client, url)
    text = _decode_body(body, headers)
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and ctype not in _HTML_TYPES and not _looks_like_html(text):
        raise UpstreamError(f"unsupported content type for metadata: {ctype}")
    parsed = await asyncio.to_thread(_parse_page_metadata, text, final_url)
    return {"url": url, "final_url": final_url, **parsed}


# --- feed digest (RSS / Atom → structured items + Markdown) -----------------------
#
# Lane 3 (distribution pivot 2026-11-07): feeds are the other thing
# agents poll constantly and parse badly. One call fetches the feed
# through the hardened pipeline and returns the feed's identity, its
# newest items in a single normalized shape across RSS 2.0 / Atom /
# RDF, and a ready-to-read Markdown digest of the same items. A
# document that parses as neither feed nor item list is junk and
# fails honestly (502, no settlement).

_FEED_SUMMARY_CAP = 600
_FEED_MARKDOWN_CAP = 40_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem, names: tuple[str, ...]) -> str | None:  # noqa: ANN001
    for child in elem:
        if _local(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
            href = child.get("href")  # Atom <link href="..."/>
            if href:
                return href.strip()
    return None


def _author_of(elem) -> str | None:  # noqa: ANN001
    """Item author across feed dialects: RSS's flat <author> /
    <dc:creator> text, or Atom's <author><name> nesting."""
    for child in elem:
        name = _local(child.tag)
        if name == "creator":
            text = (child.text or "").strip()
            if text:
                return text
        elif name == "author":
            inner = _child_text(child, ("name",))
            if inner:
                return inner
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _strip_markup(text: str) -> str:
    """Feed summaries arrive as anything from plain text to full
    HTML; the visible-text extractor normalizes both."""
    return _visible_text(text) if "<" in text else _normalize_text(text)


def _parse_feed(body_text: str, max_items: int) -> dict:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body_text)
    except ET.ParseError as e:
        raise UpstreamError(f"feed is not well-formed XML: {e}") from e

    channel = None
    item_elems: list = []
    for elem in root.iter():
        name = _local(elem.tag)
        if name == "channel" and channel is None:
            channel = elem
        elif name in ("item", "entry"):
            item_elems.append(elem)
    head = channel if channel is not None else root

    feed_title = _child_text(head, ("title",))
    feed_link = _child_text(head, ("link",))
    feed_desc = _child_text(head, ("description", "subtitle", "tagline"))

    items: list[dict] = []
    for elem in item_elems:
        title = _child_text(elem, ("title",))
        link = _child_text(elem, ("link",))
        if not title and not link:
            continue
        summary_raw = _child_text(
            elem, ("description", "summary", "content", "encoded")
        )
        summary = None
        if summary_raw:
            summary = _strip_markup(summary_raw)[:_FEED_SUMMARY_CAP] or None
        items.append(
            {
                "title": title,
                "link": link,
                "published": _child_text(
                    elem, ("pubdate", "published", "updated", "date")
                ),
                "author": _author_of(elem),
                "summary": summary,
            }
        )
        if len(items) >= max_items:
            break
    if not items:
        raise UpstreamError("no feed items could be parsed from the document")

    lines: list[str] = []
    if feed_title:
        lines.append(f"# {feed_title}")
    if feed_desc:
        lines.append(_strip_markup(feed_desc)[:_FEED_SUMMARY_CAP])
    for it in items:
        if it["title"]:
            lines.append(f"## {it['title']}")
        meta_bits = [b for b in (it["published"], it["link"]) if b]
        if meta_bits:
            lines.append(" · ".join(meta_bits))
        if it["summary"]:
            lines.append(it["summary"])
    markdown = "\n\n".join(lines)[:_FEED_MARKDOWN_CAP]
    return {
        "feed_title": feed_title,
        "feed_link": feed_link,
        "feed_description": (
            _strip_markup(feed_desc)[:_FEED_SUMMARY_CAP] if feed_desc else None
        ),
        "count": len(items),
        "items": items,
        "markdown": markdown,
    }


async def feed_digest(
    client: httpx.AsyncClient, url: str, max_items: int
) -> dict:
    await asyncio.to_thread(ssrf_check, url)  # blocking DNS — off the loop
    final_url, headers, body = await _fetch_page(client, url)
    text = _decode_body(body, headers)
    parsed = await asyncio.to_thread(_parse_feed, text, max_items)
    return {"url": url, "final_url": final_url, **parsed}


# --- URL resolution (unshorten + redirect trace) ----------------------------------
#
# Lane 3 (distribution pivot 2026-11-07): short links are opaque
# until followed, and following them client-side leaks the click.
# This route walks the chain server-side with the same discipline as
# the page fetcher — every hop SSRF-checked BEFORE it is fetched —
# and delivers the full trace: each hop's URL and status, the final
# URL, and its status and content type. The trace is the delivery, so
# a final 404 still resolves honestly (the chain is reported, not
# failed); only an unreachable or unsafe chain fails (502).

async def resolve_url(client: httpx.AsyncClient, url: str) -> dict:
    await asyncio.to_thread(ssrf_check, url)  # blocking DNS — off the loop
    hops: list[dict] = []
    current = url
    for hop in range(_MAX_REDIRECTS + 1):
        try:
            async with client.stream(
                "GET",
                current,
                headers={**_UA, **_PAGE_HEADERS_BASE},
                follow_redirects=False,
            ) as resp:
                status = resp.status_code
                content_type = resp.headers.get("content-type")
                location = resp.headers.get("location")
        except httpx.HTTPError as e:
            raise UpstreamError(
                f"resolution failed at {current}: {e}"
            ) from e
        hops.append({"url": current, "status": status})
        if status in (301, 302, 303, 307, 308):
            if not location:
                raise UpstreamError(
                    f"{current} returned HTTP {status} without a Location header"
                )
            if hop == _MAX_REDIRECTS:
                raise UpstreamError("URL redirected too many times")
            target = urljoin(current, location)
            # getaddrinfo is blocking DNS — keep it off the event loop.
            await asyncio.to_thread(ssrf_check, target)
            current = target
            continue
        return {
            "url": url,
            "final_url": current,
            "final_status": status,
            "content_type": content_type,
            "redirects": len(hops) - 1,
            "hops": hops,
        }
    raise UpstreamError("URL redirected too many times")  # pragma: no cover


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


# --- GitHub repo README ------------------------------------------------------------
#
# Lane 3 (distribution pivot 2026-11-07): Repo Vitals scores a repo;
# this is its companion read — the README itself as Markdown, the
# document an agent actually needs before depending on a project.
# Two GitHub API calls in the repo_vitals pattern (metadata, then the
# raw body); a repo or README that isn't there is an UpstreamError
# (502, no settlement). Oversize READMEs are capped with the
# truncation stated in-band, never silently cut.

_README_CAP = 20_000


async def repo_readme(
    client: httpx.AsyncClient, repo: str, github_token: str | None
) -> dict:
    full = parse_repo_input(repo)
    headers = {"Accept": "application/vnd.github+json", **_UA}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{full}/readme", headers=headers
        )
    except httpx.HTTPError as e:
        raise UpstreamError(f"GitHub fetch failed: {e}") from e
    if resp.status_code == 404:
        raise UpstreamError(f"GitHub repo or README not found: {full}")
    if resp.status_code != 200:
        raise UpstreamError(f"GitHub returned HTTP {resp.status_code}")
    meta = resp.json()
    download_url = meta.get("download_url")
    if not download_url:
        raise UpstreamError(f"GitHub returned no README body for: {full}")
    try:
        raw = await client.get(download_url, headers=_UA)
    except httpx.HTTPError as e:
        raise UpstreamError(f"GitHub README fetch failed: {e}") from e
    if raw.status_code != 200:
        raise UpstreamError(f"GitHub README returned HTTP {raw.status_code}")
    text = raw.content.decode("utf-8", errors="replace")
    return {
        "repo": full,
        "path": meta.get("path"),
        "html_url": meta.get("html_url"),
        "bytes": meta.get("size") if isinstance(meta.get("size"), int) else len(raw.content),
        "chars": len(text),
        "truncated": len(text) > _README_CAP,
        "markdown": text[:_README_CAP],
    }
