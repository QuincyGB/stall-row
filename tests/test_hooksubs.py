"""Self-serve Recheck change-webhook subscriptions: full lifecycle.

Offline: tmp stores, the fleet's public-IP test fixture as callback
host (the real SSRF guard passes IP literals without DNS), and a
MockTransport receiver. The invariants: registration validates
before storing (https + SSRF + secret entropy + caps), the secret
never echoes, only CHANGED events fire, filters scope delivery,
deliveries are HMAC-signed with the subscriber's secret and ride
the existing retry/dead-letter pipeline, and management is scoped
to the secret that registered.
"""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import hooksubs, recheck

SECRET = "test-secret-0123456789"
OTHER_SECRET = "other-secret-987654321"
CALLBACK = "https://93.184.216.34/hook"
URL = "https://93.184.216.34/v1/thing"
URL2 = "https://93.184.216.34/v1/other"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("STALLROW_RECHECK_WEBHOOK_BACKOFF_S", "0")


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _changed_envelope(store, url=URL, amount="6000"):
    store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": "x402",
            "status": "BASELINE",
            "checked_at": "2026-10-30T00:00:00Z",
            "current": {"status_code": 402, "amount": "5000", "asset": USDC,
                        "network": "eip155:8453", "payTo": OWNER},
        }
    )
    return store.record_check(
        {
            "watch_id": "w1",
            "url": url,
            "kind": "x402",
            "status": "CHANGED",
            "checked_at": "2026-10-30T01:00:00Z",
            "current": {"status_code": 402, "amount": amount, "asset": USDC,
                        "network": "eip155:8453", "payTo": OWNER},
        }
    )


def _receiver(captured, status=200):
    def handler(request):
        captured.append(request)
        return httpx.Response(status, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_register_list_delete_lifecycle(store):
    view = run(hooksubs.register(store, CALLBACK, SECRET))
    assert view["sub_id"].startswith("sub_")
    assert view["callback_url"] == CALLBACK
    assert view["filter"] == {"url": None, "origin": None}
    assert "secret" not in json.dumps(view)
    listed = hooksubs.list_subs(store, SECRET)
    assert [s["sub_id"] for s in listed] == [view["sub_id"]]
    assert hooksubs.list_subs(store, OTHER_SECRET) == []
    assert hooksubs.delete_sub(store, OTHER_SECRET, view["sub_id"]) is False
    assert hooksubs.delete_sub(store, SECRET, view["sub_id"]) is True
    assert hooksubs.list_subs(store, SECRET) == []


def test_register_refusals(store):
    with pytest.raises(hooksubs.SubsError):
        run(hooksubs.register(store, "http://93.184.216.34/hook", SECRET))
    with pytest.raises(hooksubs.SubsError):
        run(hooksubs.register(store, CALLBACK, "short"))
    with pytest.raises(hooksubs.SubsError):
        run(hooksubs.register(store, "https://127.0.0.1/hook", SECRET))
    with pytest.raises(hooksubs.SubsError):
        run(hooksubs.register(store, CALLBACK, SECRET, url=URL, origin="https://93.184.216.34"))
    assert store.count_webhook_subs() == 0  # nothing stored on refusal


def test_caps_per_secret(store, monkeypatch):
    monkeypatch.setattr(hooksubs, "MAX_PER_SECRET", 2)
    monkeypatch.setattr(hooksubs, "MAX_PER_HOST", 99)
    run(hooksubs.register(store, CALLBACK, SECRET))
    run(hooksubs.register(store, CALLBACK, SECRET))
    with pytest.raises(hooksubs.SubsError):
        run(hooksubs.register(store, CALLBACK, SECRET))
    assert store.count_webhook_subs() == 2


def test_changed_event_delivers_signed(store):
    view = run(hooksubs.register(store, CALLBACK, SECRET))
    envelope = _changed_envelope(store)
    ids = recheck.queue_subscription_events(store, envelope)
    assert len(ids) == 1
    captured: list = []
    result = run(recheck.deliver_change_event(_receiver(captured), store, ids[0]))
    assert result["status"] == "delivered"
    assert len(captured) == 1
    request = captured[0]
    body = request.content
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert request.headers["x-recheck-signature"] == f"sha256={expected}"
    payload = json.loads(body)
    assert payload["event"] == "recheck.changed"
    assert payload["url"] == URL
    assert payload["event_id"].endswith(f"@{view['sub_id']}")
    delivery = store.get_change_delivery(ids[0])
    assert delivery["status"] == "delivered" and delivery["attempts"] == 1


def test_unchanged_and_filters_do_not_fire(store):
    run(hooksubs.register(store, CALLBACK, SECRET, url=URL2))
    envelope = _changed_envelope(store)  # change on URL, sub watches URL2
    assert recheck.queue_subscription_events(store, envelope) == []
    store2_env = store.record_check(
        {
            "watch_id": "w1", "url": URL, "kind": "x402", "status": "UNCHANGED",
            "checked_at": "2026-10-30T02:00:00Z",
            "current": {"status_code": 402, "amount": "6000", "asset": USDC,
                        "network": "eip155:8453", "payTo": OWNER},
        }
    )
    assert recheck.queue_subscription_events(store, store2_env) == []


def test_origin_filter_matches(store):
    run(hooksubs.register(store, CALLBACK, SECRET, origin="https://93.184.216.34"))
    envelope = _changed_envelope(store, url=URL2)
    assert len(recheck.queue_subscription_events(store, envelope)) == 1


def test_removed_sub_dead_letters_readably(store):
    view = run(hooksubs.register(store, CALLBACK, SECRET))
    envelope = _changed_envelope(store)
    ids = recheck.queue_subscription_events(store, envelope)
    assert hooksubs.delete_sub(store, SECRET, view["sub_id"]) is True
    captured: list = []
    result = run(recheck.deliver_change_event(_receiver(captured), store, ids[0]))
    assert result["status"] == "dead_letter"
    assert captured == []  # never sent
    delivery = store.get_change_delivery(ids[0])
    assert delivery["status"] == "dead_letter"
    assert "removed" in (delivery["last_error"] or "")


def test_http_lifecycle():
    import uuid

    secret = f"http-secret-{uuid.uuid4().hex}"
    with TestClient(main.app) as client:
        bad = client.post(
            "/v1/recheck/webhooks",
            json={"callback_url": "http://93.184.216.34/hook", "secret": secret},
        )
        assert bad.status_code == 422
        created = client.post(
            "/v1/recheck/webhooks",
            json={"callback_url": CALLBACK, "secret": secret},
        )
        assert created.status_code == 200
        sub_id = created.json()["subscription"]["sub_id"]
        listed = client.post("/v1/recheck/webhooks/list", json={"secret": secret})
        assert listed.json()["count"] == 1
        deliveries = client.post(
            "/v1/recheck/webhooks/deliveries", json={"secret": secret}
        )
        assert deliveries.json() == {"count": 0, "deliveries": []}
        removed = client.post(
            "/v1/recheck/webhooks/delete",
            json={"secret": secret, "sub_id": sub_id},
        )
        assert removed.json() == {"ok": True, "removed": sub_id}
        gone = client.post(
            "/v1/recheck/webhooks/delete",
            json={"secret": secret, "sub_id": sub_id},
        )
        assert gone.status_code == 404
