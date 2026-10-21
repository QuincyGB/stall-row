#!/usr/bin/env python3
"""Refresh data/jobs.json for StallRow's /v1/job-scout from the Synmerco
API — the same read our platform sweeps use.

Auth: the Synmerco API key comes from the SYNMERCO_KEY env var, or from
~/.synmerco/key when present. The key is never printed and never
written into the snapshot.

Usage: python3 scripts/refresh_jobscout.py [--out data/jobs.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://synmerco-escrow.onrender.com"
PLATFORM_URL = "https://synmerco.com"


def load_key() -> str | None:
    val = os.environ.get("SYNMERCO_KEY", "").strip()
    if val:
        return val
    key_file = Path.home() / ".synmerco" / "key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return None


def fetch_jobs(key: str | None) -> list[dict]:
    req = urllib.request.Request(f"{API_BASE}/v1/jobs")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    jobs = body.get("jobs", body) if isinstance(body, dict) else body
    if not isinstance(jobs, list):
        raise RuntimeError("Synmerco /v1/jobs returned an unexpected shape")
    return jobs


def normalize(raw: dict, fetched_at: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if str(raw.get("status", "open")).lower() not in ("open", "active"):
        return None
    budget = raw.get("budgetCents")
    budget_usd = round(budget / 100, 2) if isinstance(budget, (int, float)) else None
    caps = raw.get("capabilities") or raw.get("capability") or []
    if isinstance(caps, str):
        caps = [caps]
    title = raw.get("title") or raw.get("name") or ""
    desc = raw.get("description") or ""
    if not title and desc:
        title = desc.split("\n")[0][:120]
    if not title:
        return None
    return {
        "title": title,
        "platform": "Synmerco",
        "budget_usd": budget_usd,
        "capabilities": [str(c) for c in caps],
        "url": PLATFORM_URL,
        "fetched_at": fetched_at,
        "expires_at": raw.get("deadline") or raw.get("expiresAt"),
        "status": "open",
        "funds_verified": raw.get("fundsVerified"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "jobs.json"))
    args = ap.parse_args()

    key = load_key()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        raw_jobs = fetch_jobs(key)
    except Exception as e:  # noqa: BLE001
        print(f"refresh failed: {e}", file=sys.stderr)
        return 1
    jobs = [j for j in (normalize(r, fetched_at) for r in raw_jobs) if j]
    snapshot = {
        "fetched_at": fetched_at,
        "source": "Synmerco API GET /v1/jobs via scripts/refresh_jobscout.py",
        "jobs": jobs,
    }
    Path(args.out).write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"wrote {len(jobs)} open job(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
