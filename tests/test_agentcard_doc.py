"""The published agent card at /.well-known/agent-card.json.

The card is derived from the x402 manifest, so the invariants are
about the derivation: one skill per priced route, prices in the
services[] entries identical to the manifest's Base accepts, the
fleet payTo declared once in the x402 block, and no capability
claimed that the service does not have. The HTTP surface is a free
200 JSON document.
"""

import os

from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, load_settings
from app.discovery import agent_card, x402_manifest


def _card():
    settings = load_settings()
    return agent_card(settings, "https://stall-row.example"), settings


def test_card_shape_and_provider():
    card, settings = _card()
    assert card["name"] == "StallRow"
    assert card["url"] == "https://stall-row.example"
    assert card["provider"]["organization"] == "Verdict Row"
    assert card["documentationUrl"] == "https://stall-row.example/llms.txt"
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    }
    assert card["x402"]["payTo"] == settings.pay_to
    assert card["x402"]["discovery"] == "https://stall-row.example/.well-known/x402"


def test_card_skills_mirror_priced_routes():
    card, settings = _card()
    manifest = x402_manifest(settings, "https://stall-row.example")
    priced = [r for r in manifest["resources"] if r.get("method") == "POST"]
    assert len(card["skills"]) == len(priced) == len(PRICE_UNITS)
    assert len(card["services"]) == len(priced)
    by_endpoint = {s["endpoint"]: s for s in card["services"]}
    for r in priced:
        base_accept = next(
            a for a in r["accepts"] if a["network"] == settings.network
        )
        entry = by_endpoint[r["resource"]]
        # The amount a verifier compares against the live challenge.
        assert entry["amount"] == str(base_accept["amount"])
        assert base_accept["payTo"] == settings.pay_to


def test_card_http_surface_is_free_json():
    with TestClient(main.app) as client:
        resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "StallRow"
    assert len(body["skills"]) == len(PRICE_UNITS)
    os.environ.pop("X402_FACILITATOR_URL", None)  # no-op guard, env hygiene
