"""Self-serve Recheck change-webhook subscriptions.

The per-watch change webhooks (services/recheck.py) are configured
by the fleet operator under the internal secret — right for our own
watches, useless to an inspector who wants OUR feed pushed to THEIR
system. This module is the self-serve counterpart, mirroring the
fleet's other self-serve patterns (watch registration, pack
balance): registration is gated by a live funding credential — a
Recheck pack or a Fleet Pass with balance for at least one Recheck
check (mirroring watch.register's validation) — the callback URL is
SSRF-checked BEFORE anything is stored and again at send time,
deliveries are the existing pipeline's (CHANGED only, HMAC-SHA256
signed, bounded retries, every attempt recorded, dead_letter
terminal), and the caller's own secret is both the management
credential and the signing key. Registration itself is not debited
and deliveries are not debited: the checks that produce events are
funded by their callers as before; the credential is the gate.

A subscription is feed-wide by default, or filtered to exactly one
resource URL or one origin — enough for an inspector watching a
beat, without turning the feed into a firehose configurator. A
pack-funded subscription is the exception: a pack is bound to ONE
endpoint URL, so it may only fund a subscription filtered to
exactly that URL.

Abuse caps: at most MAX_PER_SECRET subscriptions per secret,
MAX_PER_HOST per callback host, MAX_TOTAL overall; a secret must
carry real entropy (MIN_SECRET_LEN). Refusals raise SubsError
with the HTTP status, mirroring watch.WatchError; nothing is
stored on refusal.

Grandfathering: subscriptions created before this gate carry no
funding credential (funding_kind NULL in the store); they keep
working, skip the fan-out lapse check, and are labelled kind
"legacy" in the public view.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from urllib.parse import urlsplit

from . import fetch as fetch_svc
from . import fleetpass as fleetpass_svc
from . import recheck as recheck_svc

MIN_SECRET_LEN = 16
MAX_SECRET_LEN = 256
MAX_PER_SECRET = 10
MAX_PER_HOST = 5
MAX_TOTAL = 500


class SubsError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


async def _ssrf(url: str) -> None:
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        raise SubsError(f"callback_url refused by the SSRF guard: {e}") from e


def _public_view(sub: dict) -> dict:
    """The subscription as served back — the secret never echoes."""
    return {
        "sub_id": sub["sub_id"],
        "callback_url": sub["callback_url"],
        "filter": {
            "url": sub.get("filter_url"),
            "origin": sub.get("filter_origin"),
        },
        "enabled": sub["enabled"],
        "funding": {
            "kind": sub.get("funding_kind") or "legacy",
            "id": sub.get("funding_id"),
        },
        "funding_status": sub.get("funding_status") or "ok",
        "created_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(sub["created_epoch"])
        ),
        "deliveries_url": "/v1/recheck/webhooks/deliveries",
    }


def _check_secret(secret: str) -> str:
    secret = (secret or "").strip()
    if len(secret) < MIN_SECRET_LEN:
        raise SubsError(
            f"secret must be at least {MIN_SECRET_LEN} characters — "
            "it is both your management credential and the HMAC key "
            "deliveries are signed with"
        )
    if len(secret) > MAX_SECRET_LEN:
        raise SubsError(f"secret must be at most {MAX_SECRET_LEN} characters")
    return secret


def _normalize_origin(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SubsError("origin must look like https://host (scheme + host)")
    return f"{parts.scheme}://{parts.netloc}"


async def register(
    store: recheck_svc.WatchStore,
    callback_url: str,
    secret: str,
    url: str | None = None,
    origin: str | None = None,
    pack_id: str | None = None,
    fleet_pass_id: str | None = None,
) -> dict:
    """Validate everything BEFORE storing (SSRF on the callback,
    shape on the filter, funding credential live, caps on the
    book). Returns the public view; raises SubsError with the
    refusal; nothing is stored on any refusal."""
    secret = _check_secret(secret)
    callback_url = (callback_url or "").strip()
    if not callback_url.startswith("https://"):
        raise SubsError("callback_url must be an https URL")
    await _ssrf(callback_url)
    filter_url = None
    filter_origin = None
    if url and origin:
        raise SubsError("filter by url OR origin, not both")
    if url:
        filter_url = url.strip()
        parts = urlsplit(filter_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SubsError("filter url must be a full http(s) resource URL")
    if origin:
        filter_origin = _normalize_origin(origin)
    pack_id = (pack_id or "").strip() or None
    fleet_pass_id = (fleet_pass_id or "").strip() or None
    if pack_id and fleet_pass_id:
        raise SubsError(
            "fund with exactly one credential: a Recheck pack_id "
            "or a fleet_pass_id, not both"
        )
    if not pack_id and not fleet_pass_id:
        raise SubsError(
            "registration requires a funding credential: a Recheck "
            "pack_id or a fleet_pass_id"
        )
    funding_kind: str
    funding_id: str
    if pack_id:
        pack = store.get_pack(pack_id)
        if pack is None:
            raise SubsError("unknown pack_id", 404)
        if pack["expired"]:
            raise SubsError("this pack has expired", 410)
        if pack["credits_remaining"] < 1:
            raise SubsError("this pack has no credits left", 402)
        if filter_origin is not None or filter_url is None:
            raise SubsError(
                "a pack only funds its own URL's changes: a "
                "pack-funded subscription must carry a filter url "
                "exactly equal to the pack's URL (feed-wide and "
                "origin filters need a fleet_pass_id instead)"
            )
        if filter_url != pack["url"]:
            raise SubsError(
                "a pack only funds its own URL's changes: the "
                "filter url must exactly equal the pack's URL"
            )
        funding_kind, funding_id = "pack", pack_id
    else:
        assert fleet_pass_id is not None
        fleet_pass = store.get_pass(fleet_pass_id)
        if fleet_pass is None:
            raise SubsError("unknown fleet_pass_id", 404)
        if fleet_pass["expired"]:
            raise SubsError("this fleet pass has expired", 410)
        price = fleetpass_svc.price_for("stall-row", "/v1/recheck")
        assert price is not None
        if fleet_pass["balance_units"] < price:
            raise SubsError(
                "fleet pass balance cannot fund one Recheck check", 402
            )
        funding_kind, funding_id = "fleet_pass", fleet_pass_id
    if store.count_webhook_subs() >= MAX_TOTAL:
        raise SubsError(
            "the subscription book is full; try again later", 503
        )
    if store.count_webhook_subs(secret=secret) >= MAX_PER_SECRET:
        raise SubsError(
            f"at most {MAX_PER_SECRET} subscriptions per secret"
        )
    host = urlsplit(callback_url).netloc
    if store.count_webhook_subs(callback_host=host) >= MAX_PER_HOST:
        raise SubsError(
            f"at most {MAX_PER_HOST} subscriptions per callback host"
        )
    sub = store.add_webhook_sub(
        f"sub_{uuid.uuid4().hex}",
        callback_url,
        secret,
        filter_url,
        filter_origin,
        funding_kind,
        funding_id,
    )
    return _public_view(sub)


def list_subs(store: recheck_svc.WatchStore, secret: str) -> list[dict]:
    return [_public_view(s) for s in store.list_webhook_subs((secret or "").strip())]


def delete_sub(store: recheck_svc.WatchStore, secret: str, sub_id: str) -> bool:
    return store.delete_webhook_sub((sub_id or "").strip(), (secret or "").strip())


def deliveries(
    store: recheck_svc.WatchStore,
    secret: str,
    sub_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """The caller's own delivery rows (status, attempts, last
    error, full payload) — scoped strictly to subscriptions the
    presented secret owns."""
    subs = store.list_webhook_subs((secret or "").strip())
    if sub_id:
        subs = [s for s in subs if s["sub_id"] == sub_id.strip()]
        if not subs:
            raise SubsError("unknown subscription for this secret", 404)
    out: list[dict] = []
    for sub in subs:
        out.extend(store.list_change_deliveries(f"sub:{sub['sub_id']}", limit))
    out.sort(key=lambda d: d["id"], reverse=True)
    return out[: max(1, min(int(limit), 500))]
