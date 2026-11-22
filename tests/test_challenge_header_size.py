"""Regression pin for the 2026-11-22 pre-trade outage class.

The unpaid 402 challenge travels base64-encoded in the
PAYMENT-REQUIRED response header, and the Bazaar extension embeds
the route's output schema in that challenge. Railway's edge drops
responses whose total headers exceed ~16 KB. The W2
next_check/evidence blocks are attached to paid response bodies
only and are NOT in any discovery output schema, so no route's
challenge may approach the limit. This test pins the budget for
every priced route: each PAYMENT-REQUIRED header must stay under
15,000 chars.
"""

from fastapi.testclient import TestClient

from app import main
from app.discovery import ROUTES

HEADER_BUDGET = 15_000


def test_402_challenge_headers_stay_under_edge_limit():
    client = TestClient(main.app, raise_server_exceptions=False)
    for path in sorted(ROUTES):
        payload = ROUTES[path]["input_example"]
        r = client.post(path, json=payload)
        assert r.status_code == 402, (path, r.status_code, r.text[:200])
        header = r.headers.get("payment-required")
        assert header, f"{path}: payment-required header missing"
        assert len(header) < HEADER_BUDGET, (
            f"{path}: payment-required header is {len(header)} chars "
            f"(budget {HEADER_BUDGET}); an oversized challenge header "
            "is dropped by the Railway edge as an empty reply"
        )
