"""The recheck stall: scheduled change detection for x402 resources
and plain web pages — the row's first monitoring-loop product.

A loop calls POST /v1/recheck for the same target on a schedule. Each
call probes the target WITHOUT paying it and answers BASELINE,
UNCHANGED, or CHANGED with a field-by-field diff against the last
check stored for that watch:

  * x402 resource — fingerprint is the unpaid 402 challenge: status,
    amount, asset, network, payTo, a hash of the description, and a
    hash of the Bazaar output schema.
  * web page — fingerprint is the HTTP status plus the sha256 of the
    normalized body, read under the same hardened fetch discipline as
    page-to-markdown (SSRF check on the target and on every redirect
    hop, capped body, retry-with-backoff on transient failures).

State is one sqlite file (the RouteVerdict store pattern). It is
PER-DEPLOYMENT: the file lives on the service's disk, so a redeploy
wipes it and every watch answers BASELINE again on its next call —
a v1 limitation the route's own docs state rather than hide.

Failure honesty: a probe that fails (unreachable, or a non-402
4xx/5xx left after the retry discipline) raises UpstreamError, the
handler returns 502, no payment settles, and the stored fingerprint
is NOT overwritten — the next good probe still diffs against the
last good state.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from ..rpc import UpstreamError
from . import fetch as fetch_svc

# Same retry discipline as the page fetcher: transient statuses and
# transport errors retry with backoff; deterministic failures raise.
_ATTEMPTS = fetch_svc._FETCH_ATTEMPTS
_BACKOFF_S = fetch_svc._BACKOFF_S
_RETRY_STATUSES = fetch_svc._RETRY_STATUSES
_PROBE_STATUSES = frozenset({200, 402})  # bodies worth reading/hashing


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def default_watch_id(url: str) -> str:
    """The implicit watch for a URL: first 16 hex chars of its sha256."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- state ---------------------------------------------------------------------

class WatchStore:
    """One row per watch: the last good fingerprint and when it was
    taken. Keyed by watch_id + URL so two watches over one target —
    and one watch id reused across targets — never collide."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, fingerprint_json, checked_at FROM watches "
                "WHERE watch_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            fingerprint = json.loads(row[1])
        except ValueError:
            return None
        return {"kind": row[0], "fingerprint": fingerprint, "checked_at": row[2]}

    def save(self, key: str, kind: str, fingerprint: dict, checked_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO watches "
                "(watch_key, kind, fingerprint_json, checked_at) VALUES (?, ?, ?, ?)",
                (key, kind, json.dumps(fingerprint, sort_keys=True), checked_at),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM watches").fetchone()
        return int(row[0]) if row else 0


# --- probing -------------------------------------------------------------------

async def _get_once(
    client: httpx.AsyncClient, url: str, method: str
) -> tuple[int, httpx.Headers, bytes | None]:
    """One probe with manual redirect following — every hop is
    SSRF-checked BEFORE it is fetched, exactly like the page fetcher.
    The body is read (size-capped) only on 200/402, the statuses whose
    content the fingerprint needs."""
    current = url
    for hop in range(fetch_svc._MAX_REDIRECTS + 1):
        async with client.stream(
            method, current, headers=fetch_svc._UA, follow_redirects=False
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise UpstreamError(
                        f"target returned HTTP {resp.status_code} without a "
                        "Location header"
                    )
                if hop == fetch_svc._MAX_REDIRECTS:
                    raise UpstreamError("target redirected too many times")
                target = urljoin(current, location)
                await asyncio.to_thread(fetch_svc.ssrf_check, target)
                current = target
                if resp.status_code == 303:
                    method = "GET"
                continue
            body = (
                await fetch_svc._read_capped(resp)
                if resp.status_code in _PROBE_STATUSES
                else None
            )
            return resp.status_code, resp.headers, body
    raise UpstreamError("target redirected too many times")  # pragma: no cover


async def _attempt(client: httpx.AsyncClient, url: str):
    """One probe attempt. x402 resources live behind POST; some web
    servers refuse POST outright, so a 405/501 falls back to GET."""
    status, headers, body = await _get_once(client, url, "POST")
    if status in (405, 501):
        status, headers, body = await _get_once(client, url, "GET")
    return status, headers, body


async def _probe(client: httpx.AsyncClient, url: str):
    """Probe with the fetcher's retry discipline. Returns
    (status, headers, body) for a 200 or 402; anything else that
    survives the retries raises UpstreamError."""
    await asyncio.to_thread(fetch_svc.ssrf_check, url)  # blocking DNS — off the loop
    last_error = "target probe failed"
    for attempt in range(_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_BACKOFF_S[attempt - 1])
        try:
            status, headers, body = await _attempt(client, url)
        except httpx.HTTPError as e:
            last_error = f"target fetch failed: {e}"
            continue
        if status in _PROBE_STATUSES:
            return status, headers, body
        last_error = f"target returned HTTP {status}"
        if status in _RETRY_STATUSES:
            continue
        raise UpstreamError(last_error)
    raise UpstreamError(last_error)


# --- fingerprints ----------------------------------------------------------------

def _parse_challenge(headers: httpx.Headers, body: bytes | None) -> dict | None:
    """The x402 v2 challenge, from the PAYMENT-REQUIRED header
    (base64 JSON) or, failing that, a JSON body in the same shape."""
    raw = headers.get("payment-required")
    if raw:
        try:
            data = json.loads(base64.b64decode(raw))
            if isinstance(data, dict) and data.get("accepts"):
                return data
        except ValueError:
            pass
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("accepts"):
                return data
        except ValueError:
            pass
    return None


def _x402_fingerprint(status: int, challenge: dict) -> dict:
    accept = challenge["accepts"][0]
    description = (challenge.get("resource") or {}).get("description") or ""
    bazaar = (challenge.get("extensions") or {}).get("bazaar") or {}
    schema = ((bazaar.get("info") or {}).get("output") or {}).get("schema")
    return {
        "status_code": status,
        "amount": accept.get("amount"),
        "asset": accept.get("asset"),
        "network": accept.get("network"),
        "payTo": accept.get("payTo"),
        "description_hash": _hash_text(description) if description else None,
        "schema_hash": (
            _hash_text(json.dumps(schema, sort_keys=True))
            if schema is not None
            else None
        ),
    }


def _web_fingerprint(status: int, headers: httpx.Headers, body: bytes | None) -> dict:
    text = fetch_svc._decode_body(body or b"", headers)
    normalized = fetch_svc._normalize_text(text)
    return {
        "status_code": status,
        "body_sha256": _hash_text(normalized),
    }


async def _probe_fingerprint(
    client: httpx.AsyncClient, url: str
) -> tuple[str, dict]:
    status, headers, body = await _probe(client, url)
    if status == 402:
        challenge = _parse_challenge(headers, body)
        if challenge is not None:
            return "x402", _x402_fingerprint(status, challenge)
        # A 402 with no parseable challenge is fingerprinted as a page:
        # status + body hash still detect change honestly.
    return "web", _web_fingerprint(status, headers, body)


# --- diff + the check itself ------------------------------------------------------

def _diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for field in sorted(set(before) | set(after)):
        b, a = before.get(field), after.get(field)
        if b != a:
            changes.append({"field": field, "before": b, "after": a})
    return changes


async def recheck(
    client: httpx.AsyncClient,
    store: WatchStore,
    url: str,
    watch_id: str | None = None,
) -> dict:
    """Probe one target and diff it against the watch's stored state.
    Raises ValueError for a refused URL (SSRF guard) and UpstreamError
    for a failed probe — in both cases the stored state is untouched,
    because the save only happens after a successful fingerprint."""
    url = url.strip()
    wid = (watch_id or "").strip() or default_watch_id(url)
    key = f"{wid}\n{url}"
    kind, fingerprint = await _probe_fingerprint(client, url)
    checked_at = _now_iso()
    prior = store.get(key)
    if prior is None:
        store.save(key, kind, fingerprint, checked_at)
        return {
            "watch_id": wid,
            "url": url,
            "kind": kind,
            "status": "BASELINE",
            "changes": [],
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": (
                f"Baseline recorded for this {kind} target; the next call "
                "for this watch reports what changed against it."
            ),
        }
    changes = _diff(prior["fingerprint"], fingerprint)
    if prior["kind"] != kind:
        changes.insert(
            0, {"field": "kind", "before": prior["kind"], "after": kind}
        )
    store.save(key, kind, fingerprint, checked_at)
    if changes:
        fields = ", ".join(c["field"] for c in changes)
        return {
            "watch_id": wid,
            "url": url,
            "kind": kind,
            "status": "CHANGED",
            "changes": changes,
            "current": fingerprint,
            "checked_at": checked_at,
            "summary": f"Changed since the previous check: {fields}.",
        }
    return {
        "watch_id": wid,
        "url": url,
        "kind": kind,
        "status": "UNCHANGED",
        "changes": [],
        "current": fingerprint,
        "checked_at": checked_at,
        "summary": "No material change since the previous check.",
    }
