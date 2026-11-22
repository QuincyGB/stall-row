"""Fleet Card (W2 Build Program, infra item 20): GET /v1/fleet-card
is FREE and returns the whole fleet in one call — every priced route
from the committed fleet data sources (Tools Index for the route
table, the Fleet Discovery Bundle for the totals, this stall's
discovery registry for the worked input examples).

The count pin is the point: the card's route count is asserted
against the bundle's fleet.priced_route_count (and each stall's own
count against its bundle entry), so a regenerated bundle whose
routes the Tools Index has not caught up with fails here instead of
the card silently serving a short fleet.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, load_settings
from app.discovery import ROUTES, agent_card, llms_txt, skill_md, x402_manifest

BASE = "https://stall-row.example"
DATA = Path(__file__).resolve().parent.parent / "app" / "data"

NEW_ROUTES = [
    # Today's 10 new priced routes (W2) must be on the card.
    ("token-verdict", "/v1/second-opinion"),
    ("lock-smith", "/v1/approval-check"),
    ("lock-smith", "/v1/contract-check"),
    ("stall-row", "/v1/counterparty-report/deep"),
    ("stall-row", "/v1/counterparty-watch"),
    ("stall-row", "/v1/sanctions-check"),
    ("stall-row", "/v1/wallet-ping"),
    ("stall-row", "/v1/liquidity-check"),
    ("stall-row", "/v1/pre-trade-pack"),
    ("stall-row", "/v1/due-diligence-pack"),
]


def _bundle() -> dict:
    return json.loads((DATA / "verdict-row.json").read_text(encoding="utf-8"))


def _card(client) -> dict:
    resp = client.get("/v1/fleet-card")
    assert resp.status_code == 200  # free: no 402, no payment headers
    assert "payment-required" not in resp.headers
    return resp.json()


def test_fleet_card_free_and_count_pinned_to_bundle():
    bundle = _bundle()
    with TestClient(main.app) as client:
        card = _card(client)
    assert card["generated_at"]
    assert card["fleet"] == {
        "services": bundle["fleet"]["stall_count"],
        "priced_routes": bundle["fleet"]["priced_route_count"],
    }
    # The anti-staleness pin: card routes == the bundle's priced count.
    assert len(card["routes"]) == bundle["fleet"]["priced_route_count"]
    # ... and per stall, against each stall's own bundle entry.
    per_service: dict[str, int] = {}
    for route in card["routes"]:
        per_service[route["service"]] = per_service.get(route["service"], 0) + 1
    assert per_service == {
        stall["service"]: stall["priced_route_count"] for stall in bundle["stalls"]
    }


def test_fleet_card_route_shape_and_prices():
    settings = load_settings()
    with TestClient(main.app) as client:
        card = _card(client)
    seen = set()
    for route in card["routes"]:
        assert set(route) == {
            "service",
            "method",
            "path",
            "url",
            "price_usd",
            "description",
            "input_example",
        }
        assert route["method"] == "POST"
        assert route["url"].endswith(route["path"])
        assert route["description"]
        key = (route["service"], route["path"])
        assert key not in seen  # no duplicates
        seen.add(key)
        if route["service"] == "stall-row":
            # Prices on the card match this stall's live price table —
            # the same values its 402 challenges charge.
            assert route["price_usd"] == settings.prices[route["path"]].lstrip("$")
    for key in NEW_ROUTES:
        assert key in seen


def test_fleet_card_examples_from_discovery_registry():
    with TestClient(main.app) as client:
        card = _card(client)
    by_key = {(r["service"], r["path"]): r for r in card["routes"]}
    # Every StallRow route carries its discovery input_example verbatim.
    for path, route in ROUTES.items():
        entry = by_key[("stall-row", path)]
        assert entry["input_example"] == route["input_example"]
        assert path in PRICE_UNITS
    # Routes of the other stalls: StallRow carries no examples for
    # them in-repo, so they are null — never invented.
    for (service, _path), entry in by_key.items():
        if service != "stall-row":
            assert entry["input_example"] is None
    # The card is a free route, not a priced one: it does not list
    # itself among the priced routes.
    assert ("stall-row", "/v1/fleet-card") not in by_key


def test_fleet_card_registered_in_discovery_surfaces():
    settings = load_settings()
    manifest = x402_manifest(settings, BASE)
    assert manifest["extensions"]["fleet_card"]["card"] == f"{BASE}/v1/fleet-card"
    # Registration is an extension, not a resource: the priced
    # resource count (51 priced + the free /quote) does not move.
    assert len(manifest["resources"]) == len(ROUTES) + 1
    card_doc = agent_card(settings, BASE)
    assert card_doc["fleet_card"]["card"] == f"{BASE}/v1/fleet-card"
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert "/v1/fleet-card" in doc
