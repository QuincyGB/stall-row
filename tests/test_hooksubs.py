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
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, hooksubs, recheck

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


def _pass(store, units=5_000_000, ttl_days=90):
    fleet_pass, created = store.mint_pass(
        f"pay-hook-{uuid.uuid4().hex}", units, ttl_days
    )
    assert created
    return fleet_pass


def _register(store, *args, **kwargs):
    """Register with a fresh funded Fleet Pass unless the caller
    supplies its own funding credential."""
    if "pack_id" not in kwargs and "fleet_pass_id" not in kwargs:
        kwargs["fleet_pass_id"] = _pass(store)["pass_id"]
    return run(hooksubs.register(store, *args, **kwargs))


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
    view = _register(store, CALLBACK, SECRET)
    assert view["sub_id"].startswith("sub_")
    assert view["callback_url"] == CALLBACK
    assert view["filter"] == {"url": None, "origin": None}
    assert view["funding"]["kind"] == "fleet_pass"
    assert view["funding"]["id"].startswith("fpass_")
    assert view["funding_status"] == "ok"
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
    _register(store, CALLBACK, SECRET)
    _register(store, CALLBACK, SECRET)
    with pytest.raises(hooksubs.SubsError):
        _register(store, CALLBACK, SECRET)
    assert store.count_webhook_subs() == 2


def test_changed_event_delivers_signed(store):
    view = _register(store, CALLBACK, SECRET)
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
    _register(store, CALLBACK, SECRET, url=URL2)
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
    _register(store, CALLBACK, SECRET, origin="https://93.184.216.34")
    envelope = _changed_envelope(store, url=URL2)
    assert len(recheck.queue_subscription_events(store, envelope)) == 1


def test_removed_sub_dead_letters_readably(store):
    view = _register(store, CALLBACK, SECRET)
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
    secret = f"http-secret-{uuid.uuid4().hex}"
    fleet_pass, _ = main._recheck_store.mint_pass(
        f"pay-hook-http-{uuid.uuid4().hex}", 5_000_000, 90
    )
    with TestClient(main.app) as client:
        bad = client.post(
            "/v1/recheck/webhooks",
            json={"callback_url": "http://93.184.216.34/hook", "secret": secret},
        )
        assert bad.status_code == 422
        unfunded = client.post(
            "/v1/recheck/webhooks",
            json={"callback_url": CALLBACK, "secret": secret},
        )
        assert unfunded.status_code == 422
        assert "funding credential" in unfunded.json()["error"]
        created = client.post(
            "/v1/recheck/webhooks",
            json={
                "callback_url": CALLBACK,
                "secret": secret,
                "fleet_pass_id": fleet_pass["pass_id"],
            },
        )
        assert created.status_code == 200
        assert created.json()["subscription"]["funding"] == {
            "kind": "fleet_pass",
            "id": fleet_pass["pass_id"],
        }
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


# --- pack / Fleet Pass funding gate ------------------------------------


def test_no_credential_is_422_and_stores_nothing(store):
    before = store.count_webhook_subs()
    with pytest.raises(hooksubs.SubsError) as e:
        run(hooksubs.register(store, CALLBACK, SECRET))
    assert e.value.http_status == 422
    assert "registration requires a funding credential" in e.value.message
    assert "pack_id" in e.value.message and "fleet_pass_id" in e.value.message
    assert store.count_webhook_subs() == before


def test_both_credentials_is_422_and_stores_nothing(store):
    fleet_pass = _pass(store)
    pack, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store,
                CALLBACK,
                SECRET,
                url=URL,
                pack_id=pack["pack_id"],
                fleet_pass_id=fleet_pass["pass_id"],
            )
        )
    assert e.value.http_status == 422
    assert store.count_webhook_subs() == 0


def test_unknown_pass_is_404(store):
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, fleet_pass_id="fpass_nope"
            )
        )
    assert e.value.http_status == 404
    assert store.count_webhook_subs() == 0


def test_expired_pass_is_410(store):
    stale, _ = store.mint_pass(f"pay-hook-{uuid.uuid4().hex}", 5_000_000, 0)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, fleet_pass_id=stale["pass_id"]
            )
        )
    assert e.value.http_status == 410
    assert store.count_webhook_subs() == 0


def test_pass_below_one_check_is_402(store):
    price = fleetpass.price_for("stall-row", "/v1/recheck")
    assert price == 5000
    small = _pass(store, units=price - 1)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, fleet_pass_id=small["pass_id"]
            )
        )
    assert e.value.http_status == 402
    assert store.count_webhook_subs() == 0


def test_valid_pass_registers_with_funding_view(store):
    fleet_pass = _pass(store)
    view = run(
        hooksubs.register(
            store, CALLBACK, SECRET, fleet_pass_id=fleet_pass["pass_id"]
        )
    )
    assert view["funding"] == {
        "kind": "fleet_pass",
        "id": fleet_pass["pass_id"],
    }
    assert view["funding_status"] == "ok"
    # Registration is a gate, not a debit.
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 5_000_000
    assert store.pass_debit_count(fleet_pass["pass_id"]) == 0


def test_pack_feed_wide_filter_is_422(store):
    pack, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, pack_id=pack["pack_id"]
            )
        )
    assert e.value.http_status == 422
    assert "only funds its own URL" in e.value.message
    assert store.count_webhook_subs() == 0


def test_pack_origin_filter_is_422(store):
    pack, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store,
                CALLBACK,
                SECRET,
                origin="https://93.184.216.34",
                pack_id=pack["pack_id"],
            )
        )
    assert e.value.http_status == 422
    assert store.count_webhook_subs() == 0


def test_pack_mismatched_filter_url_is_422(store):
    pack, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store,
                CALLBACK,
                SECRET,
                url=URL2,
                pack_id=pack["pack_id"],
            )
        )
    assert e.value.http_status == 422
    assert "only funds its own URL" in e.value.message
    assert store.count_webhook_subs() == 0


def test_pack_matching_filter_url_registers(store):
    pack, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL)
    view = run(
        hooksubs.register(
            store, CALLBACK, SECRET, url=URL, pack_id=pack["pack_id"]
        )
    )
    assert view["funding"] == {"kind": "pack", "id": pack["pack_id"]}
    assert view["filter"] == {"url": URL, "origin": None}
    # Registration spends no pack credit.
    assert store.get_pack(pack["pack_id"])["credits_remaining"] == 250


def test_pack_unknown_expired_exhausted_statuses(store):
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, url=URL, pack_id="pack_nope"
            )
        )
    assert e.value.http_status == 404
    stale, _ = store.mint_pack(f"pay-hook-{uuid.uuid4().hex}", URL, ttl_days=0)
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, url=URL, pack_id=stale["pack_id"]
            )
        )
    assert e.value.http_status == 410
    tiny, _ = store.mint_pack(
        f"pay-hook-{uuid.uuid4().hex}", URL, credits_total=0
    )
    with pytest.raises(hooksubs.SubsError) as e:
        run(
            hooksubs.register(
                store, CALLBACK, SECRET, url=URL, pack_id=tiny["pack_id"]
            )
        )
    assert e.value.http_status == 402
    assert store.count_webhook_subs() == 0


def test_lapsed_pass_disables_sub_and_queues_nothing(store):
    fleet_pass = _pass(store, units=5000)
    view = run(
        hooksubs.register(
            store, CALLBACK, SECRET, fleet_pass_id=fleet_pass["pass_id"]
        )
    )
    # Drain the pass below one Recheck check (5000 units).
    store.debit_pass(
        fleet_pass["pass_id"], "stall-row", "/v1/recheck", 5000
    )
    assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == 0
    envelope = _changed_envelope(store)
    assert recheck.queue_subscription_events(store, envelope) == []
    sub = store.get_webhook_sub(view["sub_id"])
    assert sub["enabled"] is False
    assert sub["funding_status"] == "lapsed"
    listed = hooksubs.list_subs(store, SECRET)
    assert listed[0]["enabled"] is False
    assert listed[0]["funding_status"] == "lapsed"


def test_legacy_sub_without_funding_still_delivers(store):
    # The pre-gate registration path: add_webhook_sub's old
    # signature stores funding_kind NULL — grandfathered legacy.
    sub = store.add_webhook_sub(
        f"sub_{uuid.uuid4().hex}", CALLBACK, SECRET, None, None
    )
    assert sub["funding_kind"] is None
    view = hooksubs.list_subs(store, SECRET)[0]
    assert view["funding"] == {"kind": "legacy", "id": None}
    envelope = _changed_envelope(store)
    ids = recheck.queue_subscription_events(store, envelope)
    assert len(ids) == 1
    # And the legacy sub is NOT lapsed/disabled by the fan-out check.
    after = store.get_webhook_sub(sub["sub_id"])
    assert after["enabled"] is True
    assert after["funding_status"] == "ok"
