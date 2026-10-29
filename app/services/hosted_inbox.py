"""Hosted inboxes — the mailbox relay for stalls whose operator has
claimed the origin (app/services/claims.py) but cannot or does not
want to run a receiver of their own.

The model is the nudge spec's, unchanged (nudge-inbox.md §8): the
operator publishes the same extensions.nudge discovery entry every
self-hosted stall publishes, except the endpoint points here —
POST /v1/hosted-inbox/<origin-key> — and this service receives,
verifies and stores the knock exactly the way the fleet's own
inboxes do (app/services/nudge.py is the reference receiver and this
module reuses its validation, signature verification, dedupe and
rate limit verbatim). The owner reads the mail back through their
claim session and may configure two relays per stored knock:

- webhook: POST the knock payload to an operator URL, bounded
  retries, every attempt logged in the deliveries table;
- email: BUILT BUT DISABLED BY DEFAULT. Relay mail would leave
  under this service's identity, so the channel stays off until the
  fleet operator enables it (STALLROW_HOSTED_INBOX_EMAIL_RELAY=1
  plus a STALLROW_EMAIL_RELAY_URL transport). A configured address
  with the relay off is recorded as a "disabled" delivery — visible
  evidence, never a silent drop and never a send.

Nothing here is priced and nothing here appears in discovery: the
relay stores other operators' health mail; it is not a resource.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from . import claims as claims_svc
from . import fetch as fetch_svc
from . import nudge as nudge_svc
from . import recheck as recheck_svc

SPEC_URL = "https://quincygb.github.io/x402-loop-kits/nudge-inbox.html"

# --- errors ---------------------------------------------------------------------


class HostedError(Exception):
    """A clean 4xx for the hosted-inbox surface, in the receiver
    discipline of the spec: malformed is 400, forged is 403, an
    unclaimed origin is 404 — never a 5xx for sender error."""

    def __init__(self, code: str, detail: str, http_status: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status

    def body(self) -> dict:
        return {"ok": False, "error": self.code, "detail": self.detail}


# --- addressing -------------------------------------------------------------------


def origin_key(origin: str) -> str:
    """The public address component for a claimed origin: host slug
    plus a short digest of the normalized origin, so two origins on
    one host never collide and the key leaks nothing about the
    origin beyond its host. Stable by construction — the address an
    operator publishes must never move."""
    host = urlsplit(origin).hostname or "origin"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:40]
    digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def hosted_endpoint(base_url: str, origin: str) -> str:
    return f"{base_url.rstrip('/')}/v1/hosted-inbox/{origin_key(origin)}"


def discovery_snippet(base_url: str, origin: str) -> dict:
    """The extensions.nudge value the operator publishes — byte-for-
    byte the shape a self-hosted stall publishes (see
    nudge_svc.nudge_extension), with the hosted endpoint."""
    return {
        "endpoint": hosted_endpoint(base_url, origin),
        "accepts": [nudge_svc.PROTOCOL],
        "spec": SPEC_URL,
    }


def publish_line(base_url: str, origin: str) -> str:
    """The exact one-line snippet for the operator's discovery doc."""
    return json.dumps(
        {"extensions": {"nudge": discovery_snippet(base_url, origin)}},
        separators=(", ", ": "),
    )


# --- email relay gate ---------------------------------------------------------------


def email_relay_enabled() -> bool:
    """The email relay is off unless the fleet operator turns it on
    for this deployment. Relay mail leaves under this service's
    identity, so enabling it is the operator's call, not a default."""
    return os.environ.get("STALLROW_HOSTED_INBOX_EMAIL_RELAY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# --- the receive path -----------------------------------------------------------------


async def receive_knock(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    key: str,
    message,
    per_hour: int | None = None,
) -> dict:
    """Accept one knock at a hosted address. Mirrors the fleet's own
    POST /v1/nudge discipline exactly: shape (400) → signature (403)
    → kind handling, with the hosted addition that the knock's stall
    must be the origin this address was minted for (400) — a mailbox
    never accepts mail addressed to someone else. Returns the stored
    body on success; raises HostedError otherwise."""
    inbox = store.get_hosted_by_key(key)
    if inbox is None:
        raise HostedError(
            "unknown_inbox",
            "no hosted inbox exists for this address — the origin "
            "has not been claimed",
            404,
        )
    origin = inbox["origin"]
    if not isinstance(message, dict):
        raise HostedError(
            "message_not_object", "the knock must be a JSON object", 400
        )
    try:
        nudge_svc.validate_message(message)
    except nudge_svc.NudgeError as exc:
        raise HostedError(
            "invalid_message", str(exc), exc.http_status
        ) from exc
    kind = message["kind"]
    stall = str(message["stall"]).rstrip("/")
    if stall != origin:
        raise HostedError(
            "stall_mismatch",
            "this hosted inbox only accepts knocks for its own "
            f"origin ({origin})",
            400,
        )
    try:
        nudge_svc.verify_sender(message)
    except nudge_svc.NudgeError as exc:
        raise HostedError(
            "signature_invalid", str(exc), exc.http_status
        ) from exc

    if kind == nudge_svc.KIND_OPT_OUT:
        # An opt-out binds only when it is signed by the stall's own
        # published payTo — the same check the fleet inbox runs.
        wallet = str(message["sender"]).lower()
        try:
            paytos = await claims_svc.fetch_payto_set(client, origin)
        except claims_svc.ClaimError as exc:
            raise HostedError(exc.code, exc.message, exc.http_status) from exc
        if wallet not in paytos:
            raise HostedError(
                "optout_not_owner",
                "an opt-out must be signed by the stall's published "
                "payTo wallet",
                403,
            )
        store.record_optout(origin, wallet)
        return {"ok": True, "kind": kind, "stall": origin, "opted_out": True}

    if store.is_opted_out(origin):
        raise HostedError(
            "opted_out",
            "this origin has opted out of fleet-health nudges",
            403,
        )
    limit = nudge_svc.rate_limit_per_hour() if per_hour is None else per_hour
    if not nudge_svc.RATE_LIMITER.allow(str(message["sender"]), limit):
        raise HostedError(
            "rate_limited",
            f"more than {limit} verified knocks from this sender in "
            "an hour",
            429,
        )
    dedupe = nudge_svc.dedupe_key(message)
    stored = store.store_hosted_knock(origin, message, dedupe)
    if stored:
        _queue_relays(store, inbox, message, dedupe)
    return {
        "ok": True,
        "kind": kind,
        "stall": origin,
        "stored": stored,
        "duplicate": not stored,
    }


def _queue_relays(store, inbox: dict, message: dict, dedupe: str) -> None:
    """Queue the operator's configured relays for a freshly stored
    knock. process_deliveries does the sending; queueing here keeps
    the knock's 200 independent of relay health."""
    origin = inbox["origin"]
    if inbox.get("webhook_enabled") and inbox.get("webhook_url"):
        store.queue_hosted_delivery(
            origin, dedupe, "webhook", inbox["webhook_url"]
        )
    if inbox.get("email_enabled") and inbox.get("email"):
        store.queue_hosted_delivery(origin, dedupe, "email", inbox["email"])


# --- relay delivery ---------------------------------------------------------------------


def _webhook_backoffs() -> list[float]:
    raw = os.environ.get("STALLROW_HOSTED_WEBHOOK_BACKOFF_S", "1,4")
    try:
        waits = [max(0.0, float(p)) for p in raw.split(",") if p.strip()]
    except ValueError:
        waits = [1.0, 4.0]
    return waits or [0.0]


async def _deliver_webhook(
    client: httpx.AsyncClient, delivery: dict, message: dict
) -> tuple[str, int, str]:
    url = delivery["target"]
    try:
        fetch_svc.ssrf_check(url)
    except Exception as exc:  # noqa: BLE001 - a bad target is a failed delivery
        return "failed", 0, f"target refused by the SSRF guard: {exc}"
    waits = _webhook_backoffs()
    attempts = 0
    detail = ""
    for attempt in range(len(waits) + 1):
        attempts += 1
        try:
            resp = await client.post(
                url,
                json={
                    "kind": "fleet-health-hosted-relay",
                    "origin": delivery["origin"],
                    "knock": message,
                },
                timeout=10.0,
            )
            if 200 <= resp.status_code < 300:
                return "delivered", attempts, f"HTTP {resp.status_code}"
            detail = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - bounded retries, then recorded
            detail = f"{type(exc).__name__}: {exc}"
        if attempt < len(waits):
            await asyncio.sleep(waits[attempt])
    return "failed", attempts, detail


async def _deliver_email(
    client: httpx.AsyncClient, delivery: dict, message: dict
) -> tuple[str, int, str]:
    """The email relay. Disabled by default: with the flag off this
    records the honest 'disabled' outcome and sends NOTHING."""
    if not email_relay_enabled():
        return (
            "disabled",
            0,
            "email relay is disabled on this deployment — it leaves "
            "under the fleet's identity and awaits the operator's word",
        )
    transport = os.environ.get("STALLROW_EMAIL_RELAY_URL", "").strip()
    if not transport:
        return (
            "skipped",
            0,
            "email relay is enabled but no transport is configured "
            "(STALLROW_EMAIL_RELAY_URL)",
        )
    defect = message.get("defect") or {}
    payload = {
        "to": delivery["target"],
        "subject": (
            f"[fleet-health] {defect.get('code', 'defect')} on "
            f"{message.get('route', '')} — {message.get('stall', '')}"
        ),
        "body": json.dumps(message, indent=2, sort_keys=True),
    }
    headers = {}
    secret = os.environ.get("STALLROW_EMAIL_RELAY_SECRET", "").strip()
    if secret:
        headers["X-Relay-Secret"] = secret
    try:
        resp = await client.post(
            transport, json=payload, headers=headers, timeout=10.0
        )
    except Exception as exc:  # noqa: BLE001
        return "failed", 1, f"{type(exc).__name__}: {exc}"
    if 200 <= resp.status_code < 300:
        return "sent", 1, f"HTTP {resp.status_code}"
    return "failed", 1, f"HTTP {resp.status_code}"


async def process_deliveries(
    client: httpx.AsyncClient,
    store: recheck_svc.WatchStore,
    origin: str,
) -> list[dict]:
    """Attempt every pending relay delivery for one origin and
    record each outcome on its delivery row. Called by the receive
    route as a background task after a knock is stored, and directly
    by tests."""
    results = []
    knocks = {
        nudge_svc.dedupe_key(entry["message"]): entry["message"]
        for entry in store.list_hosted_knocks(origin, limit=500)
    }
    for delivery in store.pending_hosted_deliveries(origin):
        message = knocks.get(delivery["dedupe_key"])
        row = {"id": delivery["id"], "channel": delivery["channel"]}
        if message is None:
            store.update_hosted_delivery(
                delivery["id"], "failed", 0, "the knock is no longer stored"
            )
            row.update(status="failed", attempts=0)
            results.append(row)
            continue
        delivery = {**delivery, "origin": origin}
        if delivery["channel"] == "webhook":
            status, attempts, detail = await _deliver_webhook(
                client, delivery, message
            )
        elif delivery["channel"] == "email":
            status, attempts, detail = await _deliver_email(
                client, delivery, message
            )
        else:
            status, attempts, detail = "failed", 0, "unknown channel"
        store.update_hosted_delivery(
            delivery["id"], status, attempts, detail
        )
        row.update(status=status, attempts=attempts, detail=detail)
        results.append(row)
    return results


# --- owner views (claim-session gated) ---------------------------------------------------


def ensure_inbox(
    store: recheck_svc.WatchStore, origin: str
) -> dict | None:
    """Mint the hosted inbox for a claimed origin if it has none.
    Returns None for an unclaimed origin — mailboxes are a claim
    benefit, never minted for strangers."""
    inbox = store.get_hosted_inbox(origin)
    if inbox is not None:
        return inbox
    if store.get_claimed(origin) is None:
        return None
    return store.mint_hosted_inbox(origin, origin_key(origin))


def inbox_block(
    store: recheck_svc.WatchStore, origin: str, base_url: str
) -> dict | None:
    inbox = ensure_inbox(store, origin)
    if inbox is None:
        return None
    return {
        "origin": origin,
        "origin_key": inbox["origin_key"],
        "endpoint": hosted_endpoint(base_url, origin),
        "accepts": [nudge_svc.PROTOCOL],
        "created_at": inbox["created_at"],
        "discovery_snippet": discovery_snippet(base_url, origin),
        "publish_line": publish_line(base_url, origin),
        "knocks_received": store.hosted_knock_count(origin),
        "webhook": {
            "url": inbox["webhook_url"],
            "enabled": inbox["webhook_enabled"],
        },
        "email": {
            "address": inbox["email"],
            "enabled": inbox["email_enabled"],
            "relay_enabled": email_relay_enabled(),
            "note": (
                "email relay is built but disabled on this "
                "deployment — it sends under the fleet's identity "
                "and awaits the operator's word; a configured "
                "address is recorded as a disabled delivery, never "
                "sent"
                if not email_relay_enabled()
                else "email relay is enabled on this deployment"
            ),
        },
        "read_via": "GET /v1/claim/inbox with the claim session",
        "self_host_snippets": self_host_snippets(),
    }


def owner_inbox(
    store: recheck_svc.WatchStore,
    origin_raw,
    session_token,
    base_url: str,
    limit: int = 50,
) -> dict:
    """The owner's mailbox read: hosted-inbox block + stored knocks
    + relay delivery log, behind the same session gate as the claim
    status read."""
    origin, _session = claims_svc.validate_session(
        store, origin_raw, session_token
    )
    block = inbox_block(store, origin, base_url)
    if block is None:
        raise HostedError(
            "not_claimed", "this origin has not been claimed", 404
        )
    return {
        "ok": True,
        "origin": origin,
        "hosted_inbox": block,
        "knocks": store.list_hosted_knocks(origin, limit=limit),
        "deliveries": store.list_hosted_deliveries(origin, limit=limit),
    }


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def update_config(
    store: recheck_svc.WatchStore,
    origin_raw,
    session_token,
    body,
) -> dict:
    """Configure the relays for a claimed origin's mailbox. Webhook
    targets must be public https URLs (the SSRF guard runs here and
    again at delivery); email addresses are stored, and relaying to
    them stays behind the deployment's email-relay flag."""
    origin, _session = claims_svc.validate_session(
        store, origin_raw, session_token
    )
    inbox = ensure_inbox(store, origin)
    if inbox is None:
        raise HostedError(
            "not_claimed", "this origin has not been claimed", 404
        )
    if not isinstance(body, dict):
        raise HostedError("body_not_object", "the body must be a JSON object", 400)
    if "webhook_url" in body or "webhook_enabled" in body:
        url = body.get("webhook_url", inbox["webhook_url"])
        enabled = bool(body.get("webhook_enabled", inbox["webhook_enabled"]))
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                raise HostedError(
                    "webhook_url_invalid", "webhook_url must be a URL or null", 400
                )
            url = url.strip()
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise HostedError(
                    "webhook_url_invalid",
                    "the webhook URL must be a public https URL",
                    400,
                )
            try:
                await asyncio.to_thread(fetch_svc.ssrf_check, url)
            except Exception as exc:  # noqa: BLE001
                raise HostedError(
                    "webhook_url_refused",
                    f"the webhook URL is refused by the SSRF guard: {exc}",
                    400,
                ) from exc
        if enabled and not url:
            raise HostedError(
                "webhook_url_required",
                "enable the webhook only with a URL set",
                400,
            )
        inbox = store.set_hosted_webhook(origin, url, enabled)
    if "email" in body or "email_enabled" in body:
        address = body.get("email", inbox["email"])
        enabled = bool(body.get("email_enabled", inbox["email_enabled"]))
        if address is not None:
            if not isinstance(address, str) or not _EMAIL_RE.match(address.strip()):
                raise HostedError(
                    "email_invalid", "email must be an address or null", 400
                )
            address = address.strip()
        if enabled and not address:
            raise HostedError(
                "email_required", "enable email relay only with an address set", 400
            )
        inbox = store.set_hosted_email(origin, address, enabled)
    return {"ok": True, "origin": origin, "hosted_inbox_config": inbox}


# --- self-host snippets ------------------------------------------------------------------
# Minimal receivers for operators who would rather host the mailbox
# themselves. Each is a faithful minimal port of the fleet's own
# working receiver (app/services/nudge.py in QuincyGB/stall-row, live
# on all five fleet stalls): same canonical signing text, same
# validate → verify → dedupe discipline, same clean-4xx answers.

_EXPRESS_SNIPPET = """\
// Fleet-health nudge inbox — minimal receiver (Express + ethers v6).
// A minimal port of the reference receiver (app/services/nudge.py in
// QuincyGB/stall-row, the inbox the Verdict Row fleet runs itself):
// same canonical signing text, same dedupe, same discipline —
// malformed 400, forged 403, never a 5xx for sender error.
import express from "express";
import { verifyMessage } from "ethers";

const PROTOCOL = "fleet-health/v1";
const seen = new Set(); // swap for durable storage in production

function canonicalText(message) {
  const unsigned = { ...message };
  delete unsigned.signature;
  const sort = (v) =>
    Array.isArray(v)
      ? v.map(sort)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.keys(v).sort().map((k) => [k, sort(v[k])]),
          )
        : v;
  return "fleet-health-nudge/v1\\n" + JSON.stringify(sort(unsigned));
}

export function nudgeInboxApp() {
  const app = express();
  app.use(express.json());
  app.post("/v1/nudge", (req, res) => {
    const m = req.body;
    if (!m || typeof m !== "object" || Array.isArray(m))
      return res.status(400).json({ error: "message must be a JSON object" });
    if (m.kind !== "fleet-health-nudge" && m.kind !== "fleet-health-opt-out")
      return res.status(400).json({ error: "unknown kind" });
    if (m.protocol !== PROTOCOL)
      return res.status(400).json({ error: "protocol must be " + PROTOCOL });
    if (typeof m.stall !== "string" || !m.stall)
      return res.status(400).json({ error: "stall is required" });
    if (!/^0x[0-9a-fA-F]{40}$/.test(m.sender || "") ||
        !/^0x[0-9a-fA-F]{130}$/.test(m.signature || ""))
      return res.status(400).json({ error: "sender/signature malformed" });
    let signer;
    try {
      signer = verifyMessage(canonicalText(m), m.signature);
    } catch {
      return res.status(403).json({ error: "signature does not recover" });
    }
    if (signer.toLowerCase() !== m.sender.toLowerCase())
      return res
        .status(403)
        .json({ error: "signature was not made by the claimed sender" });
    if (m.kind === "fleet-health-opt-out") {
      // Honour only after checking m.sender is YOUR stall's own
      // published payTo — see the spec's opt-out section.
      return res.json({ ok: true, kind: m.kind, opted_out: m.stall });
    }
    const defect = m.defect || {};
    if (typeof m.route !== "string" || !m.route ||
        typeof defect.code !== "string" || !defect.code ||
        typeof m.first_seen !== "string" || !m.first_seen)
      return res.status(400).json({ error: "route/defect.code/first_seen required" });
    const key = [
      m.sender.toLowerCase(), m.route, defect.code, m.first_seen,
      m.escalation || "",
    ].join("|");
    const duplicate = seen.has(key);
    if (!duplicate) seen.add(key); // TODO: persist + act on the knock
    return res.json({ ok: true, kind: m.kind, stored: !duplicate, duplicate });
  });
  return app;
}
"""

_FASTAPI_SNIPPET = """\
# Fleet-health nudge inbox — minimal receiver (FastAPI).
# A minimal port of the reference receiver (app/services/nudge.py in
# QuincyGB/stall-row, the inbox the Verdict Row fleet runs itself):
# same canonical signing text, same dedupe, same discipline —
# malformed 400, forged 403, never a 5xx for sender error.
import json

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROTOCOL = "fleet-health/v1"
SEEN: set[str] = set()  # swap for durable storage in production


def canonical_text(message: dict) -> str:
    unsigned = {k: v for k, v in message.items() if k != "signature"}
    return "fleet-health-nudge/v1\\n" + json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )


def fail(status: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


app = FastAPI()


@app.post("/v1/nudge")
async def nudge(request: Request):
    try:
        m = await request.json()
    except Exception:
        return fail(400, "body must be JSON")
    if not isinstance(m, dict):
        return fail(400, "message must be a JSON object")
    if m.get("kind") not in ("fleet-health-nudge", "fleet-health-opt-out"):
        return fail(400, "unknown kind")
    if m.get("protocol") != PROTOCOL:
        return fail(400, "protocol must be " + PROTOCOL)
    sender = str(m.get("sender") or "")
    signature = str(m.get("signature") or "")
    if len(sender) != 42 or not sender.startswith("0x") or len(signature) != 132:
        return fail(400, "sender/signature malformed")
    try:
        signer = Account.recover_message(
            encode_defunct(text=canonical_text(m)), signature=signature
        )
    except Exception:
        return fail(403, "signature does not recover")
    if signer.lower() != sender.lower():
        return fail(403, "signature was not made by the claimed sender")
    if m["kind"] == "fleet-health-opt-out":
        # Honour only after checking the sender is YOUR stall's own
        # published payTo — see the spec's opt-out section.
        return {"ok": True, "kind": m["kind"], "opted_out": m.get("stall")}
    defect = m.get("defect") or {}
    if not m.get("route") or not defect.get("code") or not m.get("first_seen"):
        return fail(400, "route/defect.code/first_seen required")
    key = "|".join([
        sender.lower(), str(m["route"]), str(defect["code"]),
        str(m["first_seen"]), str(m.get("escalation") or ""),
    ])
    duplicate = key in SEEN
    if not duplicate:
        SEEN.add(key)  # TODO: persist + act on the knock
    return {"ok": True, "kind": m["kind"], "stored": not duplicate,
            "duplicate": duplicate}
"""

_WORKERS_SNIPPET = """\
// Fleet-health nudge inbox — minimal receiver (Cloudflare Workers
// + viem). A minimal port of the reference receiver
// (app/services/nudge.py in QuincyGB/stall-row, the inbox the
// Verdict Row fleet runs itself): same canonical signing text, same
// dedupe, same discipline — malformed 400, forged 403, never a 5xx
// for sender error.
import { verifyMessage } from "viem";

const PROTOCOL = "fleet-health/v1";

function canonicalText(message) {
  const unsigned = { ...message };
  delete unsigned.signature;
  const sort = (v) =>
    Array.isArray(v)
      ? v.map(sort)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.keys(v).sort().map((k) => [k, sort(v[k])]),
          )
        : v;
  return "fleet-health-nudge/v1\\n" + JSON.stringify(sort(unsigned));
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/v1/nudge")
      return json({ error: "not found" }, 404);
    let m;
    try {
      m = await request.json();
    } catch {
      return json({ error: "body must be JSON" }, 400);
    }
    if (!m || typeof m !== "object" || Array.isArray(m))
      return json({ error: "message must be a JSON object" }, 400);
    if (m.kind !== "fleet-health-nudge" && m.kind !== "fleet-health-opt-out")
      return json({ error: "unknown kind" }, 400);
    if (m.protocol !== PROTOCOL)
      return json({ error: "protocol must be " + PROTOCOL }, 400);
    if (!/^0x[0-9a-fA-F]{40}$/.test(m.sender || "") ||
        !/^0x[0-9a-fA-F]{130}$/.test(m.signature || ""))
      return json({ error: "sender/signature malformed" }, 400);
    let ok = false;
    try {
      ok = await verifyMessage({
        address: m.sender,
        message: canonicalText(m),
        signature: m.signature,
      });
    } catch {
      ok = false;
    }
    if (!ok)
      return json(
        { error: "signature was not made by the claimed sender" },
        403,
      );
    if (m.kind === "fleet-health-opt-out") {
      // Honour only after checking m.sender is YOUR stall's own
      // published payTo — see the spec's opt-out section.
      return json({ ok: true, kind: m.kind, opted_out: m.stall });
    }
    const defect = m.defect || {};
    if (typeof m.route !== "string" || !m.route ||
        typeof defect.code !== "string" || !defect.code ||
        typeof m.first_seen !== "string" || !m.first_seen)
      return json(
        { error: "route/defect.code/first_seen required" },
        400,
      );
    const key = [
      m.sender.toLowerCase(), m.route, defect.code, m.first_seen,
      m.escalation || "",
    ].join("|");
    // Durable dedupe: bind a KV namespace as NUDGE_SEEN.
    const seen = env.NUDGE_SEEN
      ? await env.NUDGE_SEEN.get("seen:" + key)
      : null;
    const duplicate = seen !== null;
    if (!duplicate && env.NUDGE_SEEN)
      await env.NUDGE_SEEN.put("seen:" + key, "1");
    // TODO: persist + act on the knock when it is not a duplicate.
    return json({
      ok: true, kind: m.kind, stored: !duplicate, duplicate,
    });
  },
};
"""


def self_host_snippets() -> dict:
    return {
        "note": (
            "Prefer to run the mailbox yourself? These minimal "
            "receivers are ports of the fleet's own working inbox "
            "(app/services/nudge.py in QuincyGB/stall-row). Publish "
            "your endpoint in extensions.nudge exactly as the "
            "hosted snippet does, with your own URL, and the patrol "
            "knocks you there instead — same protocol, same ladder."
        ),
        "express": _EXPRESS_SNIPPET,
        "fastapi": _FASTAPI_SNIPPET,
        "cloudflare_workers": _WORKERS_SNIPPET,
    }
