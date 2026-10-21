"""/v1/job-scout — a digest of open agent jobs, served from the shipped
snapshot at data/jobs.json (repo root). The snapshot is built from our
own platform sweeps and refreshed by scripts/refresh_jobscout.py.
Staleness is reported, never hidden: snapshots older than 24h set
data_stale=true on every response.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "jobs.json"
STALE_AFTER_HOURS = 24


def load_snapshot(path: Path | None = None) -> dict:
    p = path or _DATA_PATH
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"fetched_at": None, "source": "snapshot unreadable", "jobs": []}
    if not isinstance(data, dict):
        return {"fetched_at": None, "source": "snapshot malformed", "jobs": []}
    return data


def _budget_value(job: dict) -> float:
    b = job.get("budget_usd")
    if isinstance(b, (int, float)):
        return float(b)
    return 0.0


def job_scout(capabilities: list[str] | None, limit: int | None, *, path: Path | None = None) -> dict:
    snap = load_snapshot(path)
    jobs = [j for j in snap.get("jobs", []) if isinstance(j, dict)]
    fetched_at = snap.get("fetched_at")
    stale = True
    age_hours = None
    if fetched_at:
        try:
            dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            stale = age_hours > STALE_AFTER_HOURS
        except ValueError:
            stale = True

    wanted = {c.lower() for c in (capabilities or [])}
    if wanted:
        jobs = [
            j
            for j in jobs
            if wanted & {str(c).lower() for c in j.get("capabilities", [])}
            or wanted & {str(t).lower() for t in j.get("tags", [])}
        ]
    jobs.sort(key=_budget_value, reverse=True)
    if limit:
        jobs = jobs[: max(1, min(int(limit), 100))]
    return {
        "snapshot_fetched_at": fetched_at,
        "snapshot_source": snap.get("source"),
        "data_stale": stale,
        "snapshot_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "count": len(jobs),
        "jobs": jobs,
        "note": (
            "Snapshot data from our own platform sweeps; verify a listing is still "
            "open on the platform before bidding. data_stale=true means the snapshot "
            "is over 24h old."
        ),
    }
