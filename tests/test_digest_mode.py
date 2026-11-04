"""Alert Digest Mode (Next-20 #20): delivery shape, not pricing.

Pins:
  * a digest registration's events ACCUMULATE in the queue — no
    webhook POST happens at event time — while the debits still
    land per underlying knock/event exactly as in instant mode;
  * flush_digests delivers ONE batched POST per queue: the digest
    envelope carries counts by class, the full events list and
    period bounds; a flushed queue stays flushed;
  * a queue is not due before its interval elapses (ticker path,
    force=False) and IS flushed when forced (manual drain);
  * instant remains the default: a registration without
    delivery_mode pushes immediately, exactly as before;
  * the mode also applies to the pre-existing Standing Watch
    family (baseline + change queue, one digest delivery).
"""

import base64
import json
import time

import httpx
import pytest

from app.services import digest as digest_svc
from app.services import fleetpass, heartbeat, recheck, watch

TARGET = "https://93.184.216.34/v1/thing"
WEBHOOK = "https://93.184.216.35/hook"
OWNER = "0x" + "11" * 20
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _challenge_response(url):
    challenge = {
        "x402Version": 2,
        "resource": {"url": url, "type": "http"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": "5000",
                "payTo": OWNER,
            }
        ],
    }
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        if state.get("page") is not None:
            return httpx.Response(
                200, text=state["page"], headers={"content-type": "text/plain"}
            )
        return _challenge_response(str(request.url))

    return httpx.MockTransport(handler)


async def _heartbeat_reg(store, **over):
    fleet_pass, _ = store.mint_pass(
        "pay-dg", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    kwargs = dict(
        resources=[TARGET],
        webhook_url=WEBHOOK,
        email=None,
        interval_s=None,
        pack_id=None,
        fleet_pass_id=fleet_pass["pass_id"],
        delivery_mode="digest",
        digest_interval="hourly",
    )
    kwargs.update(over)
    reg = await heartbeat.register(store, **kwargs)
    return reg, fleet_pass


@pytest.mark.asyncio
async def test_digest_accumulates_then_one_batched_delivery(store):
    reg, fleet_pass = await _heartbeat_reg(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.run_reg(client, store, reg)
        assert out["events_count"] == 1  # baseline transition
        assert out["debited_units"] == 1000  # debit still per knock
        assert state["deliveries"] == []  # ...but nothing pushed
        key = digest_svc.queue_key_for("heartbeat", reg["reg_id"], "hourly")
        assert len(store.pending_digests(key)) == 1

        # Not due yet on the ticker path.
        results = await digest_svc.flush_digests(client, store)
        assert results[0]["status"] == "not_due"
        assert state["deliveries"] == []

        # Forced drain: exactly ONE POST, the digest envelope.
        results = await digest_svc.flush_digests(client, store, force=True)
        assert results[0]["status"] == "delivered"
        assert results[0]["events_count"] == 1
        assert len(state["deliveries"]) == 1
        body = json.loads(state["deliveries"][0].content)
        assert body["event"] == "alert_digest"
        assert body["kind"] == "heartbeat"
        assert body["digest_interval"] == "hourly"
        assert body["events_count"] == 1
        assert body["counts_by_class"] == {"heartbeat_baseline": 1}
        assert body["events"][0]["event"] == "heartbeat_baseline"
        assert body["period"]["from"] <= body["period"]["to"]
        # Queue drained; a second flush finds nothing.
        assert store.pending_digests(key) == []
        assert await digest_svc.flush_digests(client, store, force=True) == []
        # The pass paid for the knock, not for the delivery shape.
        assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == (
            fleetpass.PASS_PRICE_UNITS - 1000
        )


@pytest.mark.asyncio
async def test_instant_is_still_the_default(store):
    reg, _ = await _heartbeat_reg(store, delivery_mode=None, digest_interval=None)
    assert reg["delivery_mode"] == "instant"
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await heartbeat.run_reg(client, store, reg)
        assert out["events_count"] == 1
        assert len(state["deliveries"]) == 1  # pushed immediately
        body = json.loads(state["deliveries"][0].content)
        assert body["event"] == "heartbeat_baseline"


@pytest.mark.asyncio
async def test_standing_watch_digest(store):
    pack, _ = store.mint_pack("pay-dg2", TARGET)
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, pack["pack_id"], None,
        delivery_mode="digest", digest_interval="daily",
    )
    state = {"deliveries": [], "page": "version one"}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "BASELINE"
        state["page"] = "version two"
        out = await watch.run_watch(client, store, reg)
        assert out["status"] == "CHANGED"
        assert state["deliveries"] == []  # both events queued
        results = await digest_svc.flush_digests(client, store, force=True)
        assert len(results) == 1 and results[0]["events_count"] == 2
        assert len(state["deliveries"]) == 1
        body = json.loads(state["deliveries"][0].content)
        assert body["counts_by_class"] == {"BASELINE": 1, "CHANGED": 1}
        assert [e["event"] for e in body["events"]] == ["BASELINE", "CHANGED"]
    # Runs still spent pack credits per check, digest or not.
    assert store.get_pack(pack["pack_id"])["credits_remaining"] == 248


@pytest.mark.asyncio
async def test_digest_interval_due_after_interval(store):
    reg, _ = await _heartbeat_reg(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        await heartbeat.run_reg(client, store, reg)
        later = time.time() + 3601  # past the hourly interval
        results = await digest_svc.flush_digests(client, store, now_epoch=later)
        assert results[0]["status"] == "delivered"
        assert len(state["deliveries"]) == 1
