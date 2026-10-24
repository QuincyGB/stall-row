"""StallRow — one x402 service hosting a row of 12 paid micro-services.

Owner's portfolio strategy (2026-10-21): many real stalls, expect one
or two to hit. One deployment, one facilitator configuration, every
route independently priced, listed, and settled.

Paid:   12 POST /v1/* routes (see app/discovery.py ROUTES)
Free:   GET  /, /health, /quote, /.well-known/x402, /skill.md, /llms.txt

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


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await _payment_middleware(request, call_next)


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
        client, rpc, _ = _clients()
        async with client:
            return await reads_svc.tx_decoder(rpc, req.tx_hash)

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


# Recheck watch state: one sqlite file on this deployment's disk.
# Per-deployment by design — a redeploy wipes it and every watch
# re-baselines (BASELINE) on its next call. Stated in the route's
# own description rather than hidden.
_recheck_store = recheck_svc.WatchStore(settings.recheck_db_path)


@app.post("/v1/recheck")
async def recheck(req: RecheckRequest):
    async def go():
        return await recheck_svc.recheck(
            _fetch_client(), _recheck_store, req.url, req.watch_id
        )

    return await _run(go)


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
<p>Twelve small x402 stalls under one roof: Base chain reads, a wallet
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
    return {"ok": True, "service": settings.service_name, "routes": len(PRICE_UNITS)}


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
