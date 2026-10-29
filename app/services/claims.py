"""Stall claim flow: prove you run a stall, then read its history.

The fleet patrol records defects against stall origins all day, but
that history is the operator's business, not the public's — so
before anyone reads an origin's defect record they must prove they
control the origin. Two proofs are offered, either is sufficient:

  * sign-message — sign an exact EIP-191 message (issued by
    POST /v1/claim/start) with the wallet the origin's own
    /.well-known/x402 publishes as payTo. Control of the payTo is
    control of the stall: the same ground truth the nudge opt-out
    verifier uses.
  * file — serve the challenge token as the exact content of
    <origin>/.well-known/claim-<token>. Control of the origin's
    web root is control of the stall.

A challenge is a CSPRNG token, single-use, expiring after at most
one hour; a successful verify consumes it, records the origin as
claimed in the WatchStore, and returns a claim session token bound
to that ONE origin. GET /v1/claim/status answers only to a session
token issued for the origin named in the request — no session, an
unknown session, an expired session, or a session for a different
origin all refuse, and no path returns one origin's history to
another origin's session.

Free and unpriced by design: this module has no price, is absent
from config.PRICE_UNITS and every discovery surface, and the priced
route count does not move. Abuse brakes mirror the one-link probe:
a per-IP in-memory rate limit on start/verify, and the patrol's
SSRF discipline on every fetch this flow makes of an origin
(https-only for the claim file, no private/loopback targets,
SSRF-checked redirect hops, capped bodies, bounded timeouts).

History source: the status read serves a server-side snapshot the
service can access — a JSON file named by the CLAIM_HISTORY_PATH
environment variable, populated from the patrol record. Three
shapes are understood: the canonical claims-history file
({"origins": {"<origin>": {"defects": [{"route", "defect_class",
"first_seen", "last_seen", "state"}]}}}), the patrol state file
({"open_defects": {"<origin>|<route>": {"code", "first_seen",
"last_seen"}}}), and the outreach hit list ({"entries": [{"origins",
"defects": [{"route", "class", ...}]}]}). No operator's defects are
hardcoded here; with no file configured the history comes back
empty with an honest "history not loaded" marker.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from . import fetch as fetch_svc
from . import recheck as recheck_svc

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
_FETCH_TIMEOUT_S = 10.0
_SESSION_PREFIX = "claimsess_"


def _now_iso(epoch: float | None = None) -> str:
    return recheck_svc._iso_of(time.time() if epoch is None else epoch)


def challenge_ttl_s() -> int:
    """Challenge lifetime: at most one hour, env-tunable downward."""
    try:
        return max(1, min(3600, int(os.environ.get("STALLROW_CLAIM_TTL_S", "3600"))))
    except ValueError:
        return 3600


def session_ttl_s() -> int:
    try:
        return max(60, int(os.environ.get("STALLROW_CLAIM_SESSION_TTL_S", "86400")))
    except ValueError:
        return 86_400


class ClaimError(ValueError):
    """A claim refusal with a clean HTTP status and a machine-stable
    code. Nothing is consumed, marked, or disclosed on any
    ClaimError path."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --- rate limiting -------------------------------------------------------------
# Per-client-IP sliding window, mirroring probe._RateLimiter.

def rate_limit_per_min() -> int:
    try:
        return max(1, int(os.environ.get("STALLROW_CLAIM_RATE_PER_MIN", "20")))
    except ValueError:
        return 20


class _RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > now - 60]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


RATE_LIMITER = _RateLimiter()


# --- origins ---------------------------------------------------------------------

def normalize_origin(value) -> str:
    """A claim names a bare stall origin: an http(s) URL with a host
    and no path (beyond "/"), query, or fragment. Returns the origin
    with the trailing slash stripped. Raises ClaimError (422)."""
    if not isinstance(value, str) or not value.strip():
        raise ClaimError("invalid_origin", "origin is required", 422)
    origin = value.strip().rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ClaimError(
            "invalid_origin", "origin must be an http(s) origin URL", 422
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ClaimError(
            "invalid_origin",
            "origin must be a bare origin (no path, query, or fragment)",
            422,
        )
    return origin


def challenge_message(origin: str, token: str, expires_at: str) -> str:
    """The exact EIP-191 message a payTo wallet signs. It binds the
    signature to this service, this origin, this single-use token,
    and its expiry — a signature made for one origin's challenge
    can never verify another origin's, or a later challenge's."""
    return (
        "StallRow claim v1\n"
        f"Origin: {origin}\n"
        f"Token: {token}\n"
        f"Expires: {expires_at}"
    )


def claim_file_url(origin: str, token: str) -> str:
    return f"{origin}/.well-known/claim-{token}"


# --- challenge lifecycle ---------------------------------------------------------

def start_claim(
    store: recheck_svc.WatchStore,
    origin_raw,
    now_epoch: float | None = None,
) -> dict:
    """Issue a fresh challenge for an origin. Purely local — nothing
    is fetched from the origin until verify, where the SSRF
    discipline applies. Starting a new challenge supersedes older
    unused ones (verify judges the newest)."""
    origin = normalize_origin(origin_raw)
    now = time.time() if now_epoch is None else now_epoch
    expires = now + challenge_ttl_s()
    token = secrets.token_hex(16)
    expires_at = _now_iso(expires)
    message = challenge_message(origin, token, expires_at)
    store.create_claim_challenge(origin, token, message, now, expires)
    file_url = claim_file_url(origin, token)
    return {
        "origin": origin,
        "token": token,
        "expires_at": expires_at,
        "expires_in_s": int(expires - now),
        "message": message,
        "file_url": file_url,
        "file_content": token,
        "proof_options": {
            "sign_message": {
                "method": "sign-message",
                "message": message,
                "sign_with": (
                    "the payTo wallet published in the origin's "
                    "/.well-known/x402 discovery document"
                ),
                "instructions": (
                    "Sign the message exactly as shown (EIP-191 "
                    "personal_sign) with that wallet, then POST "
                    "/v1/claim/verify with method \"sign-message\" "
                    "and the signature."
                ),
            },
            "file": {
                "method": "file",
                "url": file_url,
                "content": token,
                "instructions": (
                    "Serve a file at that URL whose entire content "
                    "is the token, then POST /v1/claim/verify with "
                    "method \"file\". The file must be reachable "
                    "over https from a public address."
                ),
            },
        },
    }


def _active_challenge(
    store: recheck_svc.WatchStore, origin: str, now: float
) -> dict:
    """The challenge a verify is judged against, with the refusal
    ladder in order: none issued (404), already consumed (409 —
    the replay refusal), expired (410)."""
    challenge = store.latest_claim_challenge(origin)
    if challenge is None:
        raise ClaimError(
            "no_challenge",
            "no claim challenge has been issued for this origin; "
            "start one with POST /v1/claim/start",
            404,
        )
    if challenge["used"]:
        raise ClaimError(
            "challenge_used",
            "this challenge has already been consumed; start a new one",
            409,
        )
    if now >= challenge["expires_epoch"]:
        raise ClaimError(
            "challenge_expired",
            "this challenge has expired; start a new one",
            410,
        )
    return challenge


# --- origin fetches (SSRF discipline) --------------------------------------------

def extract_paytos(doc) -> set[str]:
    """Every payTo an origin's discovery document publishes: the
    top-level payTo, plus each resource's accepts (and any direct
    per-resource payTo). Malformed entries are skipped, never
    guessed at."""
    paytos: set[str] = set()
    if not isinstance(doc, dict):
        return paytos
    top = doc.get("payTo")
    if isinstance(top, str) and _ADDRESS_RE.match(top):
        paytos.add(top.lower())
    for entry in doc.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        direct = entry.get("payTo")
        if isinstance(direct, str) and _ADDRESS_RE.match(direct):
            paytos.add(direct.lower())
        for accept in entry.get("accepts") or []:
            if isinstance(accept, dict):
                pay_to = accept.get("payTo")
                if isinstance(pay_to, str) and _ADDRESS_RE.match(pay_to):
                    paytos.add(pay_to.lower())
    return paytos


async def _fetch_public(
    client: httpx.AsyncClient, url: str
) -> tuple[int, bytes | None]:
    """One GET under the patrol's fetch discipline: SSRF-check the
    target before fetching (https is enforced by the caller where
    required), SSRF-check every redirect hop (recheck._get_once),
    cap the body, bound the whole fetch by a timeout."""
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise ClaimError("origin_refused", str(e), 422) from e
    try:
        status, _headers, body = await asyncio.wait_for(
            recheck_svc._get_once(client, url, "GET"),
            timeout=_FETCH_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise ClaimError(
            "origin_unreachable", f"fetch of {url} timed out", 422
        ) from e
    except ValueError as e:  # a redirect hop went non-public
        raise ClaimError("origin_refused", str(e), 422) from e
    except Exception as e:  # noqa: BLE001 — transport failure is data here
        raise ClaimError(
            "origin_unreachable", f"could not fetch {url}: {type(e).__name__}", 422
        ) from e
    return status, body


async def fetch_payto_set(
    client: httpx.AsyncClient, origin: str
) -> set[str]:
    """The payTo set from the origin's own /.well-known/x402.
    An unreadable document or one publishing no payTo proves
    nothing and refuses the verify (422) — never a guess."""
    url = f"{origin}/.well-known/x402"
    status, body = await _fetch_public(client, url)
    if status != 200 or not body:
        raise ClaimError(
            "discovery_unreadable",
            f"the origin's discovery document could not be read (HTTP {status})",
            422,
        )
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ClaimError(
            "discovery_unreadable",
            "the origin's discovery document is not JSON",
            422,
        ) from e
    paytos = extract_paytos(doc)
    if not paytos:
        raise ClaimError(
            "no_payto_published",
            "the origin's discovery document publishes no payTo wallet",
            422,
        )
    return paytos


def recover_signer(message: str, signature) -> str | None:
    """The address that EIP-191-signed `message`, or None when the
    signature is malformed or unrecoverable."""
    if not isinstance(signature, str) or not _SIGNATURE_RE.match(signature):
        return None
    try:
        return Account.recover_message(
            encode_defunct(text=message), signature=signature
        )
    except Exception:  # noqa: BLE001 — a bad signature is data, not a crash
        return None


# --- verify ------------------------------------------------------------------------

_METHOD_ALIASES = {
    "sign-message": "sign-message",
    "sign_message": "sign-message",
    "signmessage": "sign-message",
    "signature": "sign-message",
    "file": "file",
    "file-proof": "file",
    "file_proof": "file",
}


async def verify_claim(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    origin_raw,
    method_raw,
    signature=None,
    now_epoch: float | None = None,
) -> dict:
    """Judge one proof against the origin's active challenge. On
    success the challenge is consumed atomically BEFORE the session
    is minted, the origin is marked claimed, and a session token
    bound to this origin is returned. A failed proof consumes
    nothing — the operator may fix the proof and retry inside the
    challenge's lifetime."""
    origin = normalize_origin(origin_raw)
    method = _METHOD_ALIASES.get(str(method_raw or "").strip().lower())
    if method is None:
        raise ClaimError(
            "unknown_method",
            "method must be \"sign-message\" or \"file\"",
            400,
        )
    now = time.time() if now_epoch is None else now_epoch
    challenge = _active_challenge(store, origin, now)
    wallet: str | None = None
    if method == "sign-message":
        if not signature or not isinstance(signature, str):
            raise ClaimError(
                "signature_required",
                "method \"sign-message\" requires a signature",
                400,
            )
        if not _SIGNATURE_RE.match(signature.strip()):
            raise ClaimError(
                "signature_malformed",
                "signature must be a 65-byte hex EIP-191 signature",
                400,
            )
        paytos = await fetch_payto_set(client, origin)
        signer = recover_signer(challenge["message"], signature.strip())
        if signer is None:
            raise ClaimError(
                "signature_invalid",
                "the signature does not recover to an address over "
                "the challenge message",
                403,
            )
        if signer.lower() not in paytos:
            raise ClaimError(
                "signer_not_payto",
                "the signature was not made by a payTo wallet the "
                "origin publishes",
                403,
            )
        wallet = signer
    else:  # file proof
        if urlsplit(origin).scheme != "https":
            raise ClaimError(
                "https_required",
                "the file proof requires an https origin",
                422,
            )
        file_url = claim_file_url(origin, challenge["token"])
        status, body = await _fetch_public(client, file_url)
        if status != 200 or body is None:
            raise ClaimError(
                "claim_file_unreachable",
                f"the claim file could not be read (HTTP {status})",
                403,
            )
        content = body.decode("utf-8", errors="replace").strip()
        if content != challenge["token"]:
            raise ClaimError(
                "claim_file_mismatch",
                "the claim file's content is not the challenge token",
                403,
            )
    if not store.consume_claim_challenge(challenge["token"]):
        raise ClaimError(
            "challenge_used",
            "this challenge has already been consumed; start a new one",
            409,
        )
    session_token = _SESSION_PREFIX + secrets.token_hex(24)
    session = store.create_claim_session(
        origin, session_token, now, now + session_ttl_s()
    )
    claimed = store.mark_claimed(origin, method, wallet, now)
    return {
        "claimed": True,
        "origin": origin,
        "method": method,
        "wallet": wallet,
        "claimed_at": claimed["claimed_at"],
        "session_token": session_token,
        "session_expires_at": session["expires_at"],
        "note": (
            "Present the session token as the X-Claim-Session header "
            "(or an Authorization: Bearer header) to GET "
            "/v1/claim/status?origin=<this origin>. The session "
            "unlocks only this origin's history."
        ),
    }


# --- history snapshot ---------------------------------------------------------------

def history_path() -> str | None:
    return os.environ.get("CLAIM_HISTORY_PATH", "").strip() or None


def _defect_row(route, defect_class, first_seen, last_seen, state) -> dict:
    return {
        "route": route,
        "defect_class": defect_class,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "state": state,
    }


def _state_of(entry: dict) -> str:
    state = entry.get("state") or entry.get("status")
    if isinstance(state, str) and state.lower() in ("open", "recovered"):
        return state.lower()
    if entry.get("recovered") or entry.get("recovered_at"):
        return "recovered"
    return "open"


def _rows_from_defect_dicts(defects, origin: str) -> list[dict]:
    rows: list[dict] = []
    for d in defects or []:
        if not isinstance(d, dict):
            continue
        rows.append(
            _defect_row(
                d.get("route") or origin,
                d.get("defect_class") or d.get("class") or d.get("code") or "unknown",
                d.get("first_seen"),
                d.get("last_seen"),
                _state_of(d),
            )
        )
    return rows


def _rows_from_document(doc, origin: str) -> list[dict]:
    """Extract one origin's defect rows from any understood history
    document shape. Origins are matched on the normalized origin
    string; another origin's rows are never returned."""
    if not isinstance(doc, dict):
        return []
    origins_map = doc.get("origins")
    if isinstance(origins_map, dict):
        entry = origins_map.get(origin) or origins_map.get(origin + "/")
        if isinstance(entry, list):
            return _rows_from_defect_dicts(entry, origin)
        if isinstance(entry, dict):
            return _rows_from_defect_dicts(
                entry.get("defects") or entry.get("history") or [], origin
            )
        return []
    open_defects = doc.get("open_defects")
    if isinstance(open_defects, dict):
        rows = []
        for key, value in open_defects.items():
            if not isinstance(value, dict):
                continue
            key_origin, _, key_route = str(key).partition("|")
            if key_origin.rstrip("/") != origin:
                continue
            rows.append(
                _defect_row(
                    key_route or value.get("route") or origin,
                    value.get("code") or value.get("defect_class") or "unknown",
                    value.get("first_seen"),
                    value.get("last_seen"),
                    _state_of(value),
                )
            )
        return rows
    entries = doc.get("entries")
    if isinstance(entries, list):
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_origins = [
                str(o).rstrip("/") for o in entry.get("origins") or []
            ]
            if origin not in entry_origins:
                continue
            rows.extend(_rows_from_defect_dicts(entry.get("defects"), origin))
        return rows
    direct = doc.get(origin) or doc.get(origin + "/")
    if isinstance(direct, list):
        return _rows_from_defect_dicts(direct, origin)
    if isinstance(direct, dict):
        return _rows_from_defect_dicts(direct.get("defects") or [], origin)
    return []


def load_history(origin: str) -> dict:
    """One origin's patrol defect history from the configured
    snapshot file. Without a readable file the history is empty and
    says so — `history_not_loaded` is the honest marker, never a
    fabricated clean record."""
    path = history_path()
    if not path:
        return _history_result(
            loaded=False,
            defects=[],
            note=(
                "history not loaded: no claims history file is "
                "configured on this deployment (CLAIM_HISTORY_PATH "
                "is unset), so no patrol record can be shown"
            ),
        )
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _history_result(
            loaded=False,
            defects=[],
            note=(
                "history not loaded: the configured claims history "
                "file could not be read or parsed, so no patrol "
                "record can be shown"
            ),
        )
    defects = _rows_from_document(doc, origin)
    defects.sort(key=lambda r: (str(r["first_seen"] or ""), str(r["route"])))
    note = (
        "patrol defect history from the loaded claims history snapshot"
        if defects
        else "no patrol defects are recorded for this origin in the "
        "loaded claims history snapshot"
    )
    return _history_result(loaded=True, defects=defects, note=note)


def _history_result(loaded: bool, defects: list[dict], note: str) -> dict:
    open_rows = [d for d in defects if d["state"] == "open"]
    recovered_rows = [d for d in defects if d["state"] == "recovered"]
    return {
        "loaded": loaded,
        "history_loaded": loaded,
        "history_not_loaded": not loaded,
        "status": "loaded" if loaded else "not_loaded",
        "note": note,
        "defects": defects,
        "summary": {
            "total": len(defects),
            "open": len(open_rows),
            "recovered": len(recovered_rows),
            "defect_classes": sorted({d["defect_class"] for d in defects}),
            "routes": sorted({d["route"] for d in defects}),
            "first_seen": min(
                (str(d["first_seen"]) for d in defects if d["first_seen"]),
                default=None,
            ),
            "last_seen": max(
                (str(d["last_seen"]) for d in defects if d["last_seen"]),
                default=None,
            ),
        },
    }


# --- enrollment offer -----------------------------------------------------------------

def _client_slug(origin: str) -> str:
    host = urlsplit(origin).hostname or "stall"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    if not slug or not slug[0].isalnum():
        slug = "stall-" + slug
    return slug[:64].strip("-") or "stall"


def enrollment_offer(origin: str, history: dict) -> dict:
    """The Managed Loop enrollment payload for this origin, in the
    exact client schema enroll_client.py writes (clients/<slug>.json):
    the origin, plus its priority routes — the routes with OPEN
    patrol defects first, since those are the ones a loop should
    watch closest. Handing this object to the enrollment flow is
    the operator's own step; nothing here enrolls anyone."""
    defects = history.get("defects") or []
    priority: list[str] = []
    for row in sorted(
        defects, key=lambda d: 0 if d["state"] == "open" else 1
    ):
        route = row["route"]
        if route and route != origin and route not in priority:
            priority.append(route)
    return {
        "client": _client_slug(origin),
        "enrolled_at": _now_iso(),
        "origins": [origin],
        "routes": priority[:40],
        "knock_channel": "email",
        "contact": None,
        "cadence": "daily",
        "enabled": True,
    }


# --- status read -------------------------------------------------------------------------

def claim_status(
    store: recheck_svc.WatchStore,
    origin_raw,
    session_token,
    now_epoch: float | None = None,
) -> dict:
    """The gated history read. The session token must exist, be
    unexpired, and have been issued for exactly the origin named —
    every other case refuses without disclosing anything about the
    origin (including whether it has any history at all)."""
    origin = normalize_origin(origin_raw)
    token = (session_token or "").strip()
    if not token:
        raise ClaimError(
            "session_required",
            "a claim session token is required (X-Claim-Session header)",
            401,
        )
    session = store.get_claim_session(token)
    if session is None:
        raise ClaimError("session_unknown", "unknown claim session token", 401)
    now = time.time() if now_epoch is None else now_epoch
    if now >= session["expires_epoch"]:
        raise ClaimError("session_expired", "this claim session has expired", 401)
    if session["origin"] != origin:
        raise ClaimError(
            "session_wrong_origin",
            "this claim session was issued for a different origin",
            403,
        )
    history = load_history(origin)
    claimed = store.get_claimed(origin)
    return {
        "origin": origin,
        "claimed": claimed is not None,
        "claim": claimed,
        "history": history,
        "enrollment_offer": enrollment_offer(origin, history),
        "enrollment_note": (
            "Managed Loop: hand enrollment_offer to the enrollment "
            "flow (managed-loop/enroll_client.py client schema) to "
            "have this origin's routes re-probed daily, with new "
            "failures and recoveries reported. Nothing is enrolled "
            "by this read."
        ),
    }
