"""The signed pinned-route stability manifest at
/.well-known/stability-manifest.json.

The manifest is generated + signed VM-side (fleet stability_manifest.py)
and committed at app/data/stability-manifest.json; the endpoint must
serve those exact bytes, and the document must name the manifest kind
and the fleet agent wallet as signer.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main

AGENT_WALLET = "0xa4f183a6799F0310FcCC545B07F2DcC960223de2"
COMMITTED = (
    Path(__file__).resolve().parent.parent
    / "app" / "data" / "stability-manifest.json"
)


def test_stability_manifest_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/.well-known/stability-manifest.json")
    assert resp.status_code == 200
    assert resp.content == COMMITTED.read_bytes()
    body = json.loads(resp.content)
    assert body["manifest"] == "verdict-row-stability/v1"
    assert body["signer"] == AGENT_WALLET
    assert body["signature"].startswith("0x")
    assert len(body["routes"]) >= 1
