"""StallRow — one x402 service hosting a row of 13 paid micro-services.

Owner's portfolio strategy (2026-10-21): many real stalls, expect one
or two to hit. One deployment, one facilitator configuration, every
route independently priced, listed, and settled.

Paid:   13 POST /v1/* routes (see app/discovery.py ROUTES)
Free:   GET  /, /health, /quote, /.well-known/x402, /skill.md, /llms.txt
        POST /v1/recheck/pack-balance (pack id is the credential)

Recheck packs: POST /v1/recheck presenting a pack id (body field
`pack_id` or X-Recheck-Pack header) spends a prepaid credit instead
of paying per call, so the middleware below lets such requests
through to the handler, which validates the pack against the store
before any check runs — an invalid pack delivers nothing.

Failure honesty: when an upstream cannot supply the data, handlers
raise UpstreamError / return 502 — and the x402 middleware does NOT
settle payment on >= 400 responses, so nobody pays for undelivered data.
"""

from __future__ import annotations

import logging
import os
from typing import Any

# Scrub bracketed-IPv6 no_proxy entries before any x402 SDK client is
# constructed (see list-smith/app/main.py — the SDK's httpx client dies
# on "[::1]" entries and turns every unpaid call into a 500).
for _var in ("no_proxy", "NO_PROXY"):
    _val = os.environ.get(_var)
    if _val:
        _kept = [e for e in _val.split(",") if "[" not in e and "]" not in e]
        os.environ[_var] = ",".join(_kept)

import httpx
import asyncio
import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption, RouteConfig
from x402.http.middleware import fastapi_payment_middleware
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

from .blockscout import Blockscout
from .config import PRICE_UNITS, load_settings
from .discovery import ROUTES, llms_txt, route_extension, skill_md, x402_manifest
from .rpc import Rpc, UpstreamError, is_address, is_tx_hash
from .services import fetch as fetch_svc
from .services import reads as reads_svc
from .services import recheck as recheck_svc
from .services import scout as scout_svc
from .services import seller as seller_svc
from .services import verdict as verdict_svc

log = logging.getLogger("stall-row")
logging.basicConfig(level=logging.INFO)

settings = load_settings()  # raises if payout wallet is a placeholder

# --- x402 wiring (same three-way facilitator branch as the siblings) ---------
if settings.use_cdp:
    from .cdp_auth import cdp_facilitator_config

    assert settings.cdp_api_key_id and settings.cdp_api_key_secret
    facilitator = HTTPFacilitatorClient(
        cdp_facilitator_config(settings.cdp_api_key_id, settings.cdp_api_key_secret)
    )
    log.info("facilitator: CDP (Bazaar cataloging path)")
elif settings.use_payai:
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
    log.info("facilitator: PayAI (PayAI index cataloging path)")
else:
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
    log.info("facilitator: %s (%s)", settings.facilitator_url, settings.network)

resource_server = x402ResourceServer(facilitator)
resource_server.register(settings.network, ExactEvmScheme())

routes = {
    f"POST {path}": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=settings.pay_to,
                price=settings.prices[path],
                network=settings.network,
            )
        ],
        description=r["description"],
        mime_type="application/json",
        service_name=settings.service_name,  # ≤32 chars, forwarded to Bazaar
        tags=r["tags"],
        extensions={"bazaar": route_extension(path)},
    )
    for path, r in ROUTES.items()
}

# --- app ---------------------------------------------------------------------
app = FastAPI(title="stall-row", version="1.0.0")

# Build the payment middleware ONCE. Constructing it runs the SDK's
# initialize(), which synchronously fetches the facilitator's /supported
# document over the network — building it per request (as this module did
# originally) added a full external round-trip, on the event loop, to
# EVERY request including /health and the unpaid 402 challenges.
_payment_middleware = fastapi_payment_middleware(routes, resource_server)

PACK_HEADER = "x-recheck-pack"


async def _request_pack_id(request: Request) -> str:
    """The pack id a recheck call presents, if any: the X-Recheck-Pack
    header, or the body's pack_id field. Reading the body here is
    safe — Starlette's HTTP middleware replays it to the handler."""
    header = request.headers.get(PACK_HEADER)
    if header and header.strip():
        return header.strip()
    try:
        body = await request.body()
        if body:
            data = json.loads(body)
            if isinstance(data, dict):
                pack_id = data.get("pack_id")
                if isinstance(pack_id, str) and pack_id.strip():
                    return pack_id.strip()
    except Exception:  # noqa: BLE001 — an unreadable body is just "no pack"
        pass
    return ""


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    # A recheck call presenting a pack id is paid from the pack, not
    # per call: skip the paywall and let the handler validate the
    # pack against the store (unknown/wrong/expired/exhausted packs
    # are refused there and deliver nothing).
    if request.method == "POST" and request.url.path == "/v1/recheck":
        if await _request_pack_id(request):
            return await call_next(request)
    return await _payment_middleware(request, call_next)


def _payment_key(request: Request) -> str | None:
    """The replay identity of the verified payment behind this
    request, for idempotent pack minting.

    The middleware stashes the verified payment payload on
    request.state. For the EVM exact scheme that payload carries the
    EIP-3009 transfer authorization, whose nonce is single-use
    on-chain — the strongest identity available to the handler (the
    settlement tx hash only exists AFTER the handler returns, when
    the middleware settles). Fallbacks: a hash of the raw payment
    header (equally unique per signed authorization). None when no
    payment identity is reachable at all."""

    def dig(obj, *names):
        for name in names:
            if obj is None:
                return None
            obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        return obj

    payload = getattr(request.state, "payment_payload", None)
    nonce = dig(payload, "payload", "authorization", "nonce")
    if isinstance(nonce, str) and nonce:
        return f"evm3009-nonce:{nonce}"
    payer = dig(payload, "payload", "authorization", "from")
    if isinstance(payer, str) and payer:
        signature = dig(payload, "payload", "signature")
        if isinstance(signature, str) and signature:
            return "evm3009-sig:" + hashlib.sha256(signature.encode()).hexdigest()
    for header in ("payment-signature", "x-payment"):
        raw = request.headers.get(header)
        if raw:
            return "payment-header:" + hashlib.sha256(raw.encode()).hexdigest()
    return None


def _clients() -> tuple[httpx.AsyncClient, Rpc, Blockscout]:
    client = httpx.AsyncClient(timeout=settings.fetch_timeout_s, follow_redirects=True)
    return client, Rpc(client, settings.rpc_url), Blockscout(client, settings.blockscout_base_url)


_shared_fetch_client: httpx.AsyncClient | None = None


def _fetch_client() -> httpx.AsyncClient:
    """Process-wide client for the page-to-markdown stall. _clients()
    builds a fresh AsyncClient per request, discarding the connection
    pool, so every page fetch paid a new DNS + TCP + TLS handshake to
    the target host. The shared client reuses connections across
    requests (and across one fetch's redirect/retry hops). Callers
    must NOT close it."""
    global _shared_fetch_client
    if _shared_fetch_client is None or _shared_fetch_client.is_closed:
        _shared_fetch_client = httpx.AsyncClient(
            timeout=settings.fetch_timeout_s, follow_redirects=True
        )
    return _shared_fetch_client


def _public_base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


async def _run(coro_fn):
    """Run a paid handler body; upstream failures become a 502 JSON
    error (the middleware does not settle on >= 400)."""
    try:
        return await coro_fn()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except UpstreamError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception:  # noqa: BLE001 — never leak tracebacks
        log.exception("paid handler failed")
        return JSONResponse({"error": "internal failure"}, status_code=502)


# --- request models ------------------------------------------------------------

class AddressRequest(BaseModel):
    address: str = Field(description="Base (EVM) address, 0x…")


class TxRequest(BaseModel):
    tx_hash: str = Field(description="Base transaction hash, 0x… (66 chars)")


class EmptyRequest(BaseModel):
    pass


class TubeSearchRequest(BaseModel):
    query: str = Field(description="YouTube search query")
    max_results: int = Field(default=5, ge=1, le=20)


class PageRequest(BaseModel):
    url: str = Field(description="Public http(s) URL to fetch and convert")


class RepoRequest(BaseModel):
    repo: str = Field(description="'owner/name' or a github.com URL")


class PriceCheckRequest(BaseModel):
    keywords: str = Field(description="Keywords/category describing the listing to price")
    description: str | None = Field(default=None, description="Optional longer description (folded into the keyword match)")


class CardSmithRequest(BaseModel):
    mode: str = Field(description="'generate' or 'grade'")
    name: str | None = None
    description: str | None = None
    url: str | None = None
    version: str | None = None
    provider: str | None = None
    provider_url: str | None = None
    wallet: str | None = None
    skills: Any = None
    endpoints: Any = None
    card: dict | None = None


class JobScoutRequest(BaseModel):
    capabilities: list[str] | None = Field(default=None, description="Capability tags to filter by")
    limit: int | None = Field(default=None, ge=1, le=100)


class RecheckRequest(BaseModel):
    url: str = Field(description="Public http(s) URL to watch: an x402 resource or a web page")
    watch_id: str | None = Field(
        default=None,
        description="Optional watch identifier; defaults to a hash of the URL",
    )
    pack_id: str | None = Field(
        default=None,
        description=(
            "Optional recheck pack id (or send the X-Recheck-Pack header): "
            "spends one pack credit instead of the per-call payment"
        ),
    )


class RecheckPackBuyRequest(BaseModel):
    url: str = Field(description="The ONE endpoint URL this pack's credits may check")


class RecheckPackBalanceRequest(BaseModel):
    pack_id: str = Field(description="Pack id returned by POST /v1/recheck/pack")
    url: str | None = Field(
        default=None,
        description="Optional: when given, the pack must be bound to this endpoint",
    )


# --- paid routes ---------------------------------------------------------------

@app.post("/v1/wallet-snapshot")
async def wallet_snapshot(req: AddressRequest):
    if not is_address(req.address):
        return JSONResponse({"error": "address must be a 0x EVM address"}, status_code=422)

    async def go():
        client, rpc, scout = _clients()
        async with client:
            return await reads_svc.wallet_snapshot(client, rpc, scout, req.address)

    return await _run(go)


@app.post("/v1/allowance-audit")
async def allowance_audit(req: AddressRequest):
    if not is_address(req.address):
        return JSONResponse({"error": "address must be a 0x EVM address"}, status_code=422)

    async def go():
        client, rpc, scout = _clients()
        async with client:
            return await reads_svc.allowance_audit(client, rpc, scout, req.address)

    return await _run(go)


@app.post("/v1/tx-decoder")
async def tx_decoder(req: TxRequest):
    if not is_tx_hash(req.tx_hash):
        return JSONResponse({"error": "tx_hash must be a 0x transaction hash"}, status_code=422)

    async def go():
        client, rpc, scout = _clients()
        async with client:
            return await reads_svc.tx_decoder(rpc, scout, req.tx_hash)

    return await _run(go)


@app.post("/v1/gas-oracle")
async def gas_oracle(_req: EmptyRequest | None = None):
    async def go():
        client, rpc, _ = _clients()
        async with client:
            return await reads_svc.gas_oracle(rpc)

    return await _run(go)


@app.post("/v1/wallet-verdict")
async def wallet_verdict(req: AddressRequest):
    if not is_address(req.address):
        return JSONResponse({"error": "address must be a 0x EVM address"}, status_code=422)

    async def go():
        client, rpc, scout = _clients()
        async with client:
            return await verdict_svc.wallet_verdict(client, rpc, scout, req.address)

    return await _run(go)


@app.post("/v1/tube-search")
async def tube_search(req: TubeSearchRequest):
    async def go():
        client, _, _ = _clients()
        async with client:
            return await fetch_svc.tube_search(client, req.query.strip(), req.max_results)

    return await _run(go)


@app.post("/v1/page-to-markdown")
async def page_to_markdown(req: PageRequest):
    async def go():
        return await fetch_svc.page_to_markdown(_fetch_client(), req.url.strip())

    return await _run(go)


@app.post("/v1/repo-vitals")
async def repo_vitals(req: RepoRequest):
    async def go():
        client, _, _ = _clients()
        async with client:
            return await fetch_svc.repo_vitals(client, req.repo, settings.github_token)

    return await _run(go)


@app.post("/v1/price-check")
async def price_check(req: PriceCheckRequest):
    keywords = req.keywords + (" " + req.description if req.description else "")

    async def go():
        client, _, _ = _clients()
        async with client:
            return await seller_svc.price_check(client, keywords)

    return await _run(go)


@app.post("/v1/card-smith")
async def card_smith(req: CardSmithRequest):
    payload = req.model_dump(exclude_none=True)

    async def go():
        return seller_svc.card_smith(payload)

    return await _run(go)


@app.post("/v1/job-scout")
async def job_scout(req: JobScoutRequest | None = None):
    req = req or JobScoutRequest()

    async def go():
        return scout_svc.job_scout(req.capabilities, req.limit)

    return await _run(go)


# Recheck watch state + watch-pack balances: one sqlite file whose
# location config.resolve_recheck_db() picked — under a mounted
# volume it survives redeploys; on the service's own disk a redeploy
# wipes it and every watch re-baselines (BASELINE) on its next call.
# Which mode is live is logged here and reported in /health;
# production currently measures persistent, and the route's own
# description states that verified behavior.
_recheck_store = recheck_svc.WatchStore(
    settings.recheck_db_path, persistent=settings.recheck_db_persistent
)
log.info(
    "recheck store durability: %s (path %s)",
    "persistent" if settings.recheck_db_persistent else "EPHEMERAL",
    settings.recheck_db_path,
)


@app.post("/v1/recheck")
async def recheck(req: RecheckRequest, request: Request):
    pack_id = (req.pack_id or request.headers.get(PACK_HEADER) or "").strip()
    if pack_id:
        try:
            return await recheck_svc.recheck_with_pack(
                _fetch_client(), _recheck_store, req.url, req.watch_id, pack_id
            )
        except recheck_svc.PackError as e:
            return JSONResponse(
                {"error": e.message, "code": e.code, "pack_id": pack_id},
                status_code=e.http_status,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        except UpstreamError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        except Exception:  # noqa: BLE001 — mirror _run's honesty contract
            log.exception("pack recheck failed")
            return JSONResponse({"error": "internal failure"}, status_code=502)

    async def go():
        return await recheck_svc.recheck(
            _fetch_client(), _recheck_store, req.url, req.watch_id
        )

    return await _run(go)


@app.post("/v1/recheck/pack")
async def recheck_pack_buy(req: RecheckPackBuyRequest, request: Request):
    """Mint a watch pack. Runs only after the x402 middleware has
    verified the $1.00 payment; minting is idempotent on the payment's
    identity (see _payment_key), so a re-presented payment returns
    the pack it already bought instead of minting a second one. If
    settlement later fails, the middleware replaces this response
    with its own failure — the buyer never sees an unpaid pack."""
    url = req.url.strip()
    try:
        await asyncio.to_thread(fetch_svc.ssrf_check, url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    payment_key = _payment_key(request)
    if payment_key is None:
        return JSONResponse(
            {"error": "payment identity unavailable; pack cannot be minted safely"},
            status_code=502,
        )
    pack, created = _recheck_store.mint_pack(payment_key, url)
    return {
        **pack,
        "expiry_days": recheck_svc.PACK_TTL_DAYS,
        "price_units": recheck_svc.PACK_PRICE_UNITS,
        "note": (
            "Spend credits with POST /v1/recheck: send this pack_id in "
            "the body or the X-Recheck-Pack header. Read the balance "
            "free at POST /v1/recheck/pack-balance."
            + ("" if created else " This payment already minted this pack; returning it.")
        ),
    }


@app.post("/v1/recheck/pack-balance")
async def recheck_pack_balance(req: RecheckPackBalanceRequest):
    """Free balance read: credits remaining + expiry for a pack.
    Spends nothing. An unknown pack — or a pack bound to a different
    endpoint than the one named — is a plain 404."""
    pack = _recheck_store.get_pack(req.pack_id.strip())
    if pack is None or (req.url is not None and pack["url"] != req.url.strip()):
        return JSONResponse({"error": "unknown pack_id"}, status_code=404)
    return pack


# --- free routes -----------------------------------------------------------------

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StallRow — a row of x402 micro-services</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 820px; margin: 40px auto;
         padding: 0 20px 60px; line-height: 1.55; color: #16202a; }
  code { font-family: ui-monospace, Menlo, monospace; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border-bottom: 1px solid #dde5ec; padding: 6px 8px; text-align: left; vertical-align: top; }
</style>
</head>
<body>
<h1>StallRow</h1>
<p>Thirteen small x402 stalls under one roof: Base chain reads, a wallet
trust verdict, fetch tools, seller tools, a digest of open agent
jobs, and scheduled change detection for x402 resources and web pages. Every stall names its data sources and marks what it cannot
supply as <em>unknown</em> — nothing is fabricated, and if an upstream
fails the call returns 502 and the payment does not settle.
No accounts, no API keys: payment is the authentication.</p>
<p>See <a href="/quote">/quote</a> for prices, <a href="/skill.md">/skill.md</a>
for agent docs, and <a href="/.well-known/x402">/.well-known/x402</a>
for the machine-readable manifest.</p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def landing():
    return LANDING_HTML


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": settings.service_name,
        "routes": len(PRICE_UNITS),
        # Durability of the recheck baseline/pack store, measured at
        # startup (config.resolve_recheck_db) — "persistent" only
        # when the store file lives on a mounted volume.
        "storage": {
            "recheck_store": (
                "persistent" if settings.recheck_db_persistent else "ephemeral"
            ),
            "recheck_db_path": settings.recheck_db_path,
        },
    }


@app.get("/quote")
async def quote():
    return {
        "service": settings.service_name,
        "endpoints": {
            path: {"price": settings.prices[path], "price_units": PRICE_UNITS[path]}
            for path in PRICE_UNITS
        },
        "asset": "USDC",
        "network": settings.network,
        "payTo": settings.pay_to,
        "protocol": "x402 v2 (exact scheme)",
    }


@app.get("/.well-known/x402")
async def well_known(request: Request):
    return JSONResponse(x402_manifest(settings, _public_base(request)))


@app.get("/skill.md", response_class=PlainTextResponse)
async def skill(request: Request):
    return skill_md(settings, _public_base(request))


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms(request: Request):
    return llms_txt(settings, _public_base(request))


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)


if __name__ == "__main__":
    main()
