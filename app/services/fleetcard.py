"""Fleet Card — one free call that returns the whole fleet.

GET /v1/fleet-card answers, in a single machine-readable JSON
document, every priced route in the Verdict Row fleet: service,
method, path, price, a short description, and a worked input
example where StallRow carries one.

Single source of truth — nothing here is a second hand-maintained
route list:

* the fleet-wide route table (all five stalls, every priced route
  with its price and description) is read from the committed Tools
  Index at app/data/tools-index.json — the same generated snapshot
  served verbatim at /tools.json;
* the fleet totals (stall count, priced-route count) are read from
  the committed Fleet Discovery Bundle at app/data/verdict-row.json
  — the same signed bundle served verbatim at
  /.well-known/verdict-row.json (tests pin the card's route count
  against the bundle's fleet.priced_route_count, so a stale Tools
  Index fails the suite instead of silently serving a short card);
* worked input examples are joined from this stall's own discovery
  registry (app/discovery.py ROUTES — the same input_example values
  the Bazaar extension, the OpenAPI worked examples and the free
  /sample payloads are built from). StallRow carries examples only
  for its own routes; a route whose example genuinely is not
  available to StallRow is listed with input_example=null rather
  than an invented one.

The card itself is a FREE route, not a priced one, so it is not
listed among the routes it returns: the listing is exactly the
bundle's priced routes.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..discovery import ROUTES

_DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_TOOLS_INDEX = os.path.join(_DATA, "tools-index.json")
_BUNDLE = os.path.join(_DATA, "verdict-row.json")

_BASE_NETWORK = "eip155:8453"


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _short_description(text: str) -> str:
    """The card's short form of a route description: its first
    sentence (the fleet's descriptions all lead with what the route
    does, before the use-when tail)."""
    parts = re.split(r"(?<=[.!])\s+", (text or "").strip(), maxsplit=1)
    return parts[0] if parts else ""


def _price_usd(route: dict) -> str:
    """The route's Base (eip155:8453) price as a decimal-USD string,
    from the Tools Index accept list — the same amount the route's
    live 402 challenge carries on Base. Falls back to the display
    price with its currency sign stripped."""
    accepts = route.get("accepts") or []
    base = next(
        (a for a in accepts if a.get("network") == _BASE_NETWORK),
        accepts[0] if accepts else None,
    )
    if base is not None and base.get("amount_decimal"):
        return str(base["amount_decimal"])
    return str(route.get("price_display") or "").lstrip("$")


def build_card() -> dict:
    """Compose the fleet card from the committed fleet data sources
    described in the module docstring."""
    bundle = _load(_BUNDLE)
    index = _load(_TOOLS_INDEX)
    fleet = bundle.get("fleet") or {}

    routes: list[dict] = []
    for stall in index.get("stalls") or []:
        service = stall.get("slug") or stall.get("service") or ""
        for route in stall.get("priced_routes") or []:
            resource = route.get("resource") or ""
            path = urlsplit(resource).path
            example = None
            if service == "stall-row" and path in ROUTES:
                example = copy.deepcopy(ROUTES[path].get("input_example"))
            routes.append(
                {
                    "service": service,
                    "method": route.get("method") or "POST",
                    "path": path,
                    "url": resource,
                    "price_usd": _price_usd(route),
                    "description": _short_description(route.get("description") or ""),
                    "input_example": example,
                }
            )

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "fleet": {
            "services": fleet.get("stall_count"),
            "priced_routes": fleet.get("priced_route_count"),
        },
        "routes": routes,
    }
