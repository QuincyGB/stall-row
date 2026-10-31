"""The Gradeability Self-Test Report at
/.well-known/gradeability-report.json.

The report is generated + signed VM-side (fleet gradeability_report.py:
live discovery + fresh unpaid probes + the standing checker's state)
and committed at app/data/gradeability-report.json; the endpoint must
serve those exact bytes, and the document must name the report kind
and the fleet agent wallet as signer.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main

AGENT_WALLET = "0xa4f183a6799F0310FcCC545B07F2DcC960223de2"
COMMITTED = (
    Path(__file__).resolve().parent.parent
    / "app" / "data" / "gradeability-report.json"
)


def test_gradeability_report_served_verbatim():
    with TestClient(main.app) as client:
        resp = client.get("/.well-known/gradeability-report.json")
    assert resp.status_code == 200
    assert resp.content == COMMITTED.read_bytes()
    body = json.loads(resp.content)
    assert body["report"] == "verdict-row-gradeability/v1"
    assert body["signer"] == AGENT_WALLET
    assert body["signature"].startswith("0x")
    assert body["summary"]["routes_tested"] >= 1
    assert len(body["routes"]) == body["summary"]["routes_tested"]
