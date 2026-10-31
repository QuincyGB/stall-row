"""The Inspector Quickstart Pack pages at /quickstart/<lang>.

The canonical pages are committed text at app/data/quickstart-*.txt;
the endpoints must serve those exact bytes (the stability-manifest
serving pattern), and each page must carry the live integration
facts it promises: the Base network id, the pinned routes' prices,
and the feed v2 cursor.
"""

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main

DATA = Path(__file__).resolve().parent.parent / "app" / "data"
PAGES = ("curl", "python", "typescript")


def test_quickstart_pages_served_verbatim():
    with TestClient(main.app) as client:
        for name in PAGES:
            resp = client.get(f"/quickstart/{name}")
            assert resp.status_code == 200
            assert resp.content == (DATA / f"quickstart-{name}.txt").read_bytes()
            assert b"eip155:8453" in resp.content
            assert b"since_seq" in resp.content
            assert b"/v1/recheck" in resp.content
            assert b"/v1/check" in resp.content
