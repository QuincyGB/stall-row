"""Seller tools + scout: price-check comps from the catalog fixture,
card-smith generate/grade, job-scout filtering/sorting/staleness."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.rpc import UpstreamError
from app.services import scout, seller
from tests.fixtures import mock_client


@pytest.mark.asyncio
async def test_price_check_distribution_and_recommendation():
    client = mock_client()
    async with client:
        out = await seller.price_check(client, "token verdict")
    assert out["catalog_status"] == "ok"
    assert out["matches_found"] == 2  # weather listing does not match
    dist = out["price_distribution"]
    assert dist["min_usd"] == 0.02
    assert dist["max_usd"] == 0.05
    assert out["recommended_price_usd"] == pytest.approx(0.035)
    assert len(out["comparables"]) == 2


@pytest.mark.asyncio
async def test_price_check_no_matches_is_honest():
    client = mock_client()
    async with client:
        out = await seller.price_check(client, "underwater basket weaving")
    assert out["matches_found"] == 0
    assert out["recommended_price_usd"] is None
    assert out["price_distribution"] is None


@pytest.mark.asyncio
async def test_price_check_catalog_down_raises():
    client = mock_client(cdp_down=True)
    async with client:
        with pytest.raises(UpstreamError):
            await seller.price_check(client, "token verdict")


def test_card_smith_generate_produces_graded_card():
    out = seller.card_smith(
        {
            "mode": "generate",
            "name": "Ren",
            "description": "AI agent executor for research and data work.",
            "url": "https://example.com/.well-known/agent-card.json",
            "skills": ["research", {"id": "code", "name": "code review", "description": "Reviews code", "tags": ["code"]}],
            "wallet": "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F",
        }
    )
    card = out["card"]
    assert card["name"] == "Ren"
    assert card["skills"][0]["id"] == "skill-1"
    assert card["skills"][1]["id"] == "code"
    assert card["x-payments"]["wallet"].startswith("0x097f")
    assert out["validation"]["score"] >= 70


def test_card_smith_grade_flags_missing_fields():
    out = seller.card_smith({"mode": "grade", "card": {"name": "Bare"}})
    names = [d["name"] for d in out["validation"]["defects"]]
    assert any("missing description" in n for n in names)
    assert any("missing skills" in n for n in names)
    assert out["validation"]["valid_structure"] is False


def test_card_smith_bad_mode():
    with pytest.raises(ValueError):
        seller.card_smith({"mode": "explode"})


def _write_snapshot(tmp_path: Path, fetched_at: str, jobs: list) -> Path:
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps({"fetched_at": fetched_at, "source": "test", "jobs": jobs}))
    return p


def test_job_scout_shipped_snapshot_loads_and_sorts():
    # The shipped snapshot is live board data (scripts/refresh_jobscout.py)
    # — assert shape and ordering, never a fixed job count or board content.
    out = scout.job_scout(None, None)
    assert out["count"] >= 1
    budgets = [j["budget_usd"] or 0 for j in out["jobs"]]
    assert budgets == sorted(budgets, reverse=True)
    assert all(j["platform"] and j["title"] and j["url"] for j in out["jobs"])


def test_job_scout_filters_by_capability(tmp_path):
    p = _write_snapshot(
        tmp_path,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        [
            {"title": "audit", "platform": "X", "budget_usd": 9, "capabilities": ["security-review"], "url": "https://x"},
            {"title": "posts", "platform": "X", "budget_usd": 5, "capabilities": ["marketing"], "url": "https://x"},
        ],
    )
    out = scout.job_scout(["security-review"], None, path=p)
    assert out["count"] == 1
    assert out["jobs"][0]["title"] == "audit"


def test_job_scout_stale_flag(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = _write_snapshot(tmp_path, old, [{"title": "t", "platform": "X", "budget_usd": 1, "capabilities": [], "url": "https://x"}])
    out = scout.job_scout(None, None, path=p)
    assert out["data_stale"] is True
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = _write_snapshot(tmp_path, fresh, [{"title": "t", "platform": "X", "budget_usd": 1, "capabilities": [], "url": "https://x"}])
    out = scout.job_scout(None, None, path=p)
    assert out["data_stale"] is False
