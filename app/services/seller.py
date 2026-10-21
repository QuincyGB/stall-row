"""Seller tools: /v1/price-check (Bazaar pricing comps) and
/v1/card-smith (agent-card generate/grade)."""

from __future__ import annotations

import re
import statistics

import httpx

from ..config import CDP_DISCOVERY_RESOURCES_URL
from ..rpc import UpstreamError

# --- price-check ------------------------------------------------------------------

def _resource_price_units(resource: dict) -> int | None:
    for acc in resource.get("accepts") or []:
        if isinstance(acc, dict) and str(acc.get("amount", "")).isdigit():
            return int(acc["amount"])
    return None


def _matches(resource: dict, terms: list[str]) -> bool:
    hay = " ".join(
        str(resource.get(k) or "") for k in ("description", "resource", "serviceName")
    ).lower()
    tags = " ".join(str(t) for t in resource.get("tags") or []).lower()
    return any(t in hay or t in tags for t in terms)


async def price_check(client: httpx.AsyncClient, keywords: str, max_pages: int = 5) -> dict:
    """Comps from the CDP Bazaar public catalog. The catalog's own filter
    params are unreliable (cdp-sdk #765), so pages are fetched unfiltered
    and matched client-side. Catalog unreachable → UpstreamError (502,
    no charge) — never invented comps."""
    terms = [t.strip().lower() for t in re.split(r"[,\s]+", keywords) if t.strip()]
    if not terms:
        raise ValueError("keywords must contain at least one term")
    matched: list[dict] = []
    seen_pages = 0
    offset = 0
    limit = 100
    while seen_pages < max_pages:
        try:
            resp = await client.get(
                CDP_DISCOVERY_RESOURCES_URL, params={"offset": offset, "limit": limit}
            )
        except httpx.HTTPError as e:
            raise UpstreamError(f"CDP catalog fetch failed: {e}") from e
        if resp.status_code != 200:
            raise UpstreamError(f"CDP catalog returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise UpstreamError("CDP catalog returned non-JSON") from e
        # Live shape (verified 2026-10-21): {"items": [...], "pagination":
        # {"total": 28423}} — the list key is "items", not "resources",
        # and the endpoint serves ~20 per page regardless of limit.
        resources = None
        if isinstance(body, dict):
            resources = body.get("items") or body.get("resources")
        if not isinstance(resources, list):
            raise UpstreamError("CDP catalog returned an unexpected shape")
        seen_pages += 1
        for r in resources:
            if isinstance(r, dict) and _matches(r, terms):
                units = _resource_price_units(r)
                if units is not None:
                    matched.append(
                        {
                            "resource": r.get("resource"),
                            "description": (r.get("description") or "")[:160],
                            "price_units": units,
                            "price_usd": units / 1_000_000,
                        }
                    )
        total = ((body.get("pagination") or {}).get("total")) if isinstance(body, dict) else None
        offset += len(resources)
        if not resources or (isinstance(total, int) and offset >= total):
            break

    if not matched:
        return {
            "keywords": keywords,
            "catalog_status": "ok",
            "matches_found": 0,
            "price_distribution": None,
            "comparables": [],
            "recommended_price_usd": None,
            "rationale": (
                "No cataloged resources matched those keywords in the pages scanned, "
                "so there is no honest comp set — broaden the keywords or treat the "
                "category as unpriced white space."
            ),
        }

    prices = sorted(m["price_units"] for m in matched)
    median = statistics.median(prices)
    p90 = prices[min(len(prices) - 1, int(0.9 * len(prices)))]
    comps = sorted(matched, key=lambda m: abs(m["price_units"] - median))[:5]
    return {
        "keywords": keywords,
        "catalog_status": "ok",
        "matches_found": len(matched),
        "price_distribution": {
            "min_usd": prices[0] / 1_000_000,
            "median_usd": median / 1_000_000,
            "p90_usd": p90 / 1_000_000,
            "max_usd": prices[-1] / 1_000_000,
        },
        "comparables": comps,
        "recommended_price_usd": median / 1_000_000,
        "rationale": (
            f"Median of {len(matched)} cataloged match(es); the Bazaar ranks by unique "
            "payers, not price, so pricing at the category median keeps the route "
            "inside buyer agents' typical per-call budgets."
        ),
    }


# --- card-smith ---------------------------------------------------------------------

_REQUIRED_TOP = ("name", "description", "url", "version", "capabilities", "skills")


def _skill_list(raw) -> list[dict]:
    skills = []
    for i, s in enumerate(raw or []):
        if isinstance(s, str):
            skills.append({"id": f"skill-{i + 1}", "name": s, "description": s, "tags": []})
        elif isinstance(s, dict):
            skills.append(
                {
                    "id": str(s.get("id") or f"skill-{i + 1}"),
                    "name": str(s.get("name") or s.get("id") or f"skill-{i + 1}"),
                    "description": str(s.get("description") or ""),
                    "tags": list(s.get("tags") or []),
                }
            )
    return skills


def generate_card(fields: dict) -> dict:
    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValueError("generate mode requires a 'name'")
    skills_in = fields.get("skills") or fields.get("capabilities_list") or []
    skills = _skill_list(skills_in)
    card = {
        "name": name,
        "description": str(fields.get("description") or ""),
        "url": str(fields.get("url") or ""),
        "version": str(fields.get("version") or "1.0.0"),
        "provider": {
            "organization": str(fields.get("provider") or name),
            "url": str(fields.get("provider_url") or ""),
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/markdown"],
        "skills": skills,
    }
    if fields.get("wallet"):
        card["x-payments"] = {
            "wallet": str(fields["wallet"]),
            "network": "eip155:8453",
            "asset": "USDC",
        }
    if fields.get("endpoints"):
        card["x-endpoints"] = fields["endpoints"]
    grade = grade_card(card)
    return {"card": card, "validation": grade}


def grade_card(card: dict) -> dict:
    if not isinstance(card, dict):
        raise ValueError("card must be a JSON object")
    defects: list[dict] = []
    score = 100

    def defect(name: str, severity: str, points: int, detail: str) -> None:
        nonlocal score
        defects.append({"name": name, "severity": severity, "detail": detail})
        score -= points

    for key in _REQUIRED_TOP:
        if key not in card or card.get(key) in (None, "", [], {}):
            defect(f"missing {key}", "major", 15, f"Top-level '{key}' is absent or empty.")
    caps = card.get("capabilities")
    if not isinstance(caps, dict):
        defect("capabilities not an object", "major", 10, "'capabilities' should be an object of boolean flags.")
    skills = card.get("skills")
    if isinstance(skills, list):
        if not skills:
            defect("no skills", "major", 10, "The card lists no skills — buyers match on skills.")
        for i, s in enumerate(skills):
            if not isinstance(s, dict):
                defect(f"skill {i} not an object", "minor", 4, "Each skill should be an object with id/name/description/tags.")
                continue
            for k in ("id", "name", "description"):
                if not s.get(k):
                    defect(f"skill {i} missing {k}", "minor", 4, f"Skill entry {i} has no '{k}'.")
    url = card.get("url")
    if url and not str(url).startswith("https://"):
        defect("url not https", "minor", 5, "Card URL should be https so registries can fetch it.")
    desc = card.get("description")
    if isinstance(desc, str) and len(desc) < 40:
        defect("thin description", "minor", 5, "Description under 40 chars rarely matches buyer searches.")
    return {
        "valid_structure": not any(d["severity"] == "major" for d in defects),
        "score": max(0, score),
        "defects": defects,
    }


def card_smith(payload: dict) -> dict:
    mode = str(payload.get("mode") or "").lower()
    if mode == "generate":
        out = generate_card(payload)
        return {"mode": "generate", **out}
    if mode == "grade":
        card = payload.get("card")
        if card is None:
            raise ValueError("grade mode requires a 'card' object")
        return {"mode": "grade", "validation": grade_card(card)}
    raise ValueError("mode must be 'generate' or 'grade'")
