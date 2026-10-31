"""PayTo Pin & Change Alert: POST /v1/payto-pin/verify ($0.01)
plus its free pin-management surface (Next-20 #15).

The failure class this exists for is observed, not hypothetical:
Dima's patrol holds open payto_mismatch defects where a stall's
live payTo no longer matches what catalogues recorded — a swap is
found after the fact, if at all. A pin makes the FIRST-SEEN state
the baseline instead:

  * A pin is {pin_id, url, payto_set, created_at, commitment}.
    The payTo set is every distinct payTo across the target's
    live 402 challenge accepts, captured by ONE unpaid probe at
    creation time (the Sign Reader's probe machinery — the
    recheck probe engine underneath it). The commitment is a
    sha256 over the canonical JSON of {pin_id, url, payto_set,
    created_at} (the fleet's canonical-JSON discipline from the
    stability manifest, in a service-side hash-committed variant:
    no wallet signature, but any holder can recompute the
    commitment from the read's own fields and check it — see
    verify_commitment). Pins never expire.
  * The holder secret (16+ chars, mirroring hooksubs) is the
    pin's only credential: it creates, reads, deletes, and
    alert-subscribes the pin, and it is the HMAC key its alert
    webhooks are signed with. It is stored like the webhook
    tables store theirs and never echoes in any view.
  * Verify (the priced route) performs a FRESH unpaid probe and
    compares the live payTo set with the pin: MATCH, MOVED (with
    before/after sets), or ROUTE_GONE (the route is unreachable,
    answers non-402, or its challenge carries no payTo — a
    DELIVERED report at 200, never a 500, so the caller's payment
    settles for a real answer). An unknown pin is a 404 and a
    mismatched secret a 403 — both >= 400, so neither settles.
  * Creation is FREE, two ways: standalone (POST /v1/payto-pin),
    and alongside a paid verify — a verify naming url + secret
    for a route that holder has not pinned creates the pin from
    THAT verify's own probe (one probe total, never two) and
    says so (pin_created=true, status MATCH by construction).

Alert scope — exactly this, nothing wider: a PAYTO_MOVED alert
fires ONLY when a verify call observes a live payTo set that
differs from the pin AND that pin has a webhook registered
(at creation, or later via the alert-subscribe management
call). Delivery reuses the existing change-webhook pipeline
(recheck.queue_change_delivery + deliver_change_event: HMAC-
SHA256 signed with the holder secret, bounded retries, every
attempt recorded, dead_letter terminal) under watch_key
"pin:<pin_id>". A swap alerts ONCE per distinct new set: the
pin records every alerted set's hash, so re-verifying the same
moved set queues nothing, while a second, different swap alerts
again. Recorded Recheck checks and the feed fan-out do NOT fire
pin alerts — a pin is verified when its holder (or their loop)
pays for a verify, which is the moment the answer matters.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets as _secrets
from urllib.parse import urlsplit

import httpx

from . import fetch as fetch_svc
from . import recheck as recheck_svc
from . import signread as signread_svc

log = logging.getLogger("stall-row.paytopin")

PRICE_UNITS = 10_000  # $0.01 — mirrors config.PRICE_UNITS

MIN_SECRET_LEN = 16
MAX_SECRET_LEN = 256

LIMITS = (
    "One unpaid probe per creation or verify, at call time; the "
    "live sign can change the moment after. The pin commits to the "
    "payTo SET first seen (every accept's payTo, normalized and "
    "sorted) — price, asset, network and schema movement are "
    "Recheck's beat, not this pin's. Alerts fire only on "
    "verify-time detection (see the module docstring), once per "
    "distinct new set. Pins never expire."
)


class PinError(Exception):
    """A pin refusal. `code` is machine-stable; the handler maps
    it to `http_status`. Nothing is stored on any PinError path."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --- commitment -----------------------------------------------------------------


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def commitment_for(
    pin_id: str, url: str, payto_set: list[str], created_at: str
) -> str:
    """sha256 over the pin's canonical content. Recomputable from
    the read's own fields — that recomputation IS the verification
    (verify_commitment below)."""
    content = {
        "pin_id": pin_id,
        "url": url,
        "payto_set": list(payto_set),
        "created_at": created_at,
    }
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def verify_commitment(view: dict) -> bool:
    expected = commitment_for(
        view["pin_id"], view["url"], view["payto_set"], view["created_at"]
    )
    return hmac.compare_digest(expected, view.get("commitment") or "")


def set_hash(payto_set: list[str]) -> str:
    return hashlib.sha256(_canonical(list(payto_set)).encode("utf-8")).hexdigest()


def _normalize_payto(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    # EVM addresses compare case-insensitively; anything else
    # (e.g. a Solana base58 payTo) is case-significant and kept.
    return value.lower() if value.startswith("0x") else value


def payto_set_of(accepts: list[dict]) -> list[str]:
    out: set[str] = set()
    for accept in accepts or []:
        if isinstance(accept, dict):
            normalized = _normalize_payto(accept.get("payTo"))
            if normalized:
                out.add(normalized)
    return sorted(out)


# --- views ------------------------------------------------------------------------


def _pin_view(pin: dict) -> dict:
    """The pin as served back — the secret never echoes. The
    commitment rides along with every field it commits to, so the
    holder can verify it from the read alone."""
    return {
        "pin_id": pin["pin_id"],
        "url": pin["url"],
        "payto_set": pin["payto_set"],
        "commitment": pin["commitment"],
        "commitment_verified": verify_commitment(pin),
        "created_at": pin["created_at"],
        "webhook_url": pin.get("webhook_url"),
        "alerts": {
            "registered": bool(pin.get("webhook_url")),
            "sets_alerted": len(pin.get("alerted_sets") or []),
        },
        "last_verified_at": (
            recheck_svc._iso_of(pin["last_verified_epoch"])
            if pin.get("last_verified_epoch")
            else None
        ),
        "last_status": pin.get("last_status"),
        "expires": False,
        "note": "Pins do not expire.",
    }


# --- validation -------------------------------------------------------------------


def _check_secret(secret: str) -> str:
    secret = (secret or "").strip()
    if len(secret) < MIN_SECRET_LEN:
        raise PinError(
            "secret_too_short",
            f"secret must be at least {MIN_SECRET_LEN} characters — "
            "it is both your management credential and the HMAC key "
            "alert deliveries are signed with",
            422,
        )
    if len(secret) > MAX_SECRET_LEN:
        raise PinError(
            "secret_too_long",
            f"secret must be at most {MAX_SECRET_LEN} characters",
            422,
        )
    return secret


def _check_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PinError(
            "bad_url", "url must be a full http(s) resource URL", 422
        )
    return url


async def _check_webhook(webhook_url: str) -> str:
    webhook_url = (webhook_url or "").strip()
    if not webhook_url.startswith("https://"):
        raise PinError(
            "bad_webhook", "webhook_url must be an https URL", 422
        )
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, webhook_url)
    except ValueError as e:
        raise PinError(
            "bad_webhook",
            f"webhook_url refused by the SSRF guard: {e}",
            422,
        ) from e
    return webhook_url


def _authed_pin(store, pin_id: str, secret: str) -> dict:
    """Management auth for one pin: unknown -> 404, wrong secret
    -> 403 (the webhook-subscription management pattern)."""
    pin_id = (pin_id or "").strip()
    if not pin_id:
        raise PinError("pin_required", "pin_id is required", 422)
    pin = store.get_payto_pin(pin_id)
    if pin is None:
        raise PinError("unknown_pin", "unknown pin_id", 404)
    if not hmac.compare_digest(pin["secret"], (secret or "").strip()):
        raise PinError("wrong_secret", "wrong secret for this pin", 403)
    return pin


# --- probing ------------------------------------------------------------------------


async def probe_payto_set(
    client: httpx.AsyncClient, url: str
) -> tuple[list[str], dict]:
    """ONE unpaid probe of the route (Sign Reader machinery) ->
    (payto_set, evidence). An unreachable / non-402 / payTo-less
    route yields an EMPTY set with the reason in the evidence —
    callers decide what an empty set means (creation refuses it;
    verify reports ROUTE_GONE). Only an SSRF refusal raises
    (ValueError, the handler's 422)."""
    report = await signread_svc.sign_read(client, url)
    payto_set = payto_set_of(report.get("accepts") or [])
    evidence = {
        "http_status": report.get("http_status"),
        "challenge_found": report.get("challenge_found"),
        "x402_version": report.get("x402_version"),
        "accepts": report.get("accepts") or [],
        "defects": report.get("defects") or [],
        "read_at": report.get("read_at"),
    }
    return payto_set, evidence


# --- creation -------------------------------------------------------------------------


def _store_pin(
    store, url: str, secret: str, webhook_url: str | None,
    payto_set: list[str],
) -> dict:
    pin_id = "pin_" + _secrets.token_hex(16)
    created_at = recheck_svc._now_iso()
    commitment = commitment_for(pin_id, url, payto_set, created_at)
    pin = store.create_payto_pin(
        pin_id, url, payto_set, commitment, secret, webhook_url,
        created_at,
    )
    if webhook_url:
        # The alert leg rides the existing change-webhook pipeline:
        # config in recheck_webhooks under "pin:<pin_id>", signed
        # with the holder secret, delivered by deliver_change_event.
        store.set_change_webhook(f"pin:{pin_id}", webhook_url, secret, True)
    return pin


async def create_pin(
    client: httpx.AsyncClient,
    store,
    url: str,
    secret: str,
    webhook_url: str | None = None,
) -> tuple[dict, bool]:
    """Free standalone creation: ONE unpaid probe captures the
    first-seen payTo set. Returns (view, created) — a holder who
    already pins this URL gets their existing pin back
    (created=False), never a duplicate. A route whose probe finds
    no payTo set cannot be pinned (422, nothing stored)."""
    secret = _check_secret(secret)
    url = _check_url(url)
    if webhook_url:
        webhook_url = await _check_webhook(webhook_url)
    existing = store.get_payto_pin_by_url_secret(url, secret)
    if existing is not None:
        return _pin_view(existing), False
    payto_set, _evidence = await probe_payto_set(client, url)
    if not payto_set:
        raise PinError(
            "no_payto_found",
            "the route's live probe found no payTo set to pin "
            "(unreachable, non-402, or a challenge with no payTo) — "
            "nothing was pinned",
            422,
        )
    pin = _store_pin(store, url, secret, webhook_url, payto_set)
    return _pin_view(pin), True


# --- management reads -------------------------------------------------------------------


def read_pin(store, pin_id: str, secret: str) -> dict:
    return _pin_view(_authed_pin(store, pin_id, secret))


def delete_pin(store, pin_id: str, secret: str) -> dict:
    pin = _authed_pin(store, pin_id, secret)
    store.delete_payto_pin(pin["pin_id"], pin["secret"])
    return {"ok": True, "deleted": pin["pin_id"], "url": pin["url"]}


async def subscribe_alert(
    store, pin_id: str, secret: str, webhook_url: str
) -> dict:
    """Register/replace the pin's PAYTO_MOVED webhook. The holder
    secret authenticates and becomes the delivery HMAC key."""
    pin = _authed_pin(store, pin_id, secret)
    webhook_url = await _check_webhook(webhook_url)
    store.set_change_webhook(
        f"pin:{pin['pin_id']}", webhook_url, pin["secret"], True
    )
    updated = store.update_payto_pin_webhook(pin["pin_id"], webhook_url)
    assert updated is not None
    return _pin_view(updated)


# --- alerts ------------------------------------------------------------------------------


def _queue_moved_alert(
    client: httpx.AsyncClient, store, pin: dict, after_set: list[str],
    observed_at: str,
) -> int | None:
    """Queue the PAYTO_MOVED delivery for one newly-observed set,
    or None when this set was already alerted (or no webhook is
    registered). Marking happens BEFORE queueing under the store
    lock, so concurrent verifies cannot double-alert. Delivery is
    scheduled on the running loop exactly like the recheck feed's
    fan-out; with no loop the row stays pending and readable."""
    if not pin.get("webhook_url"):
        return None
    digest = set_hash(after_set)
    if not store.mark_payto_pin_alerted(pin["pin_id"], digest):
        return None
    payload = {
        "event": "PAYTO_MOVED",
        "event_id": f"payto_moved_{pin['pin_id']}_{digest[:16]}",
        "pin_id": pin["pin_id"],
        "route": pin["url"],
        "url": pin["url"],
        "before": pin["payto_set"],
        "after": after_set,
        "observed_at": observed_at,
        "commitment": pin["commitment"],
    }
    delivery_id = store.queue_change_delivery(
        payload["event_id"], f"pin:{pin['pin_id']}", None, pin["url"],
        payload,
    )
    try:
        asyncio.get_running_loop().create_task(
            recheck_svc.deliver_change_event(client, store, delivery_id)
        )
    except RuntimeError:
        log.warning(
            "payto pin alert %s queued without a running loop", delivery_id
        )
    return delivery_id


# --- verify ---------------------------------------------------------------------------------


async def verify_pin(
    client: httpx.AsyncClient,
    store,
    pin_id: str | None = None,
    url: str | None = None,
    secret: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """The priced verify. Resolution: pin_id names the pin
    directly (a presented secret must match it); url + secret
    names the caller's pin for that route, creating it from this
    call's own probe when the caller has none. Exactly ONE probe
    runs on every path."""
    pin: dict | None = None
    creating = False
    if pin_id and pin_id.strip():
        pin = store.get_payto_pin(pin_id.strip())
        if pin is None:
            raise PinError("unknown_pin", "unknown pin_id", 404)
        if secret and secret.strip() and not hmac.compare_digest(
            pin["secret"], secret.strip()
        ):
            raise PinError(
                "wrong_secret", "wrong secret for this pin", 403
            )
    elif url and secret and secret.strip():
        secret = _check_secret(secret)
        url = _check_url(url)
        if webhook_url:
            webhook_url = await _check_webhook(webhook_url)
        pin = store.get_payto_pin_by_url_secret(url, secret)
        creating = pin is None
    else:
        raise PinError(
            "pin_or_url_secret_required",
            "name a pin_id, or a url + secret (an unpinned "
            "url + secret creates its pin from this verify)",
            422,
        )

    target_url = pin["url"] if pin is not None else url
    assert target_url is not None
    current_set, evidence = await probe_payto_set(client, target_url)
    observed_at = recheck_svc._now_iso()

    if creating:
        assert secret is not None
        if not current_set:
            return {
                "pin_id": None,
                "url": target_url,
                "status": "ROUTE_GONE",
                "pinned_payto_set": None,
                "current_payto_set": None,
                "before": None,
                "after": None,
                "commitment": None,
                "commitment_verified": None,
                "pin_created": False,
                "alert_queued": False,
                "alert_delivery_id": None,
                "observed_at": observed_at,
                "evidence": evidence,
                "limits": LIMITS,
                "note": (
                    "No pin was created: the route's live probe "
                    "found no payTo set to pin."
                ),
            }
        pin = _store_pin(store, target_url, secret, webhook_url, current_set)
        store.record_payto_pin_verify(pin["pin_id"], "MATCH")
        view = _pin_view(pin)
        return {
            "pin_id": pin["pin_id"],
            "url": target_url,
            "status": "MATCH",
            "pinned_payto_set": pin["payto_set"],
            "current_payto_set": current_set,
            "before": pin["payto_set"],
            "after": current_set,
            "commitment": pin["commitment"],
            "commitment_verified": view["commitment_verified"],
            "pin": view,
            "pin_created": True,
            "alert_queued": False,
            "alert_delivery_id": None,
            "observed_at": observed_at,
            "evidence": evidence,
            "limits": LIMITS,
            "note": (
                "First verify for this url + secret: the pin was "
                "created free from this verify's own probe, so the "
                "live set matches it by construction."
            ),
        }

    assert pin is not None
    if not current_set:
        status = "ROUTE_GONE"
        store.record_payto_pin_verify(pin["pin_id"], status)
        return {
            "pin_id": pin["pin_id"],
            "url": target_url,
            "status": status,
            "pinned_payto_set": pin["payto_set"],
            "current_payto_set": None,
            "before": pin["payto_set"],
            "after": None,
            "commitment": pin["commitment"],
            "commitment_verified": verify_commitment(pin),
            "pin_created": False,
            "alert_queued": False,
            "alert_delivery_id": None,
            "observed_at": observed_at,
            "evidence": evidence,
            "limits": LIMITS,
            "note": (
                "The route is gone or unreadable: its live probe "
                "found no payTo set (unreachable, non-402, or a "
                "challenge with no payTo). The pin is unchanged."
            ),
        }

    if current_set == pin["payto_set"]:
        status = "MATCH"
        delivery_id = None
    else:
        status = "MOVED"
        delivery_id = _queue_moved_alert(
            client, store, pin, current_set, observed_at
        )
    store.record_payto_pin_verify(pin["pin_id"], status)
    return {
        "pin_id": pin["pin_id"],
        "url": target_url,
        "status": status,
        "pinned_payto_set": pin["payto_set"],
        "current_payto_set": current_set,
        "before": pin["payto_set"],
        "after": current_set,
        "commitment": pin["commitment"],
        "commitment_verified": verify_commitment(pin),
        "pin_created": False,
        "alert_queued": delivery_id is not None,
        "alert_delivery_id": delivery_id,
        "observed_at": observed_at,
        "evidence": evidence,
        "limits": LIMITS,
    }
