"""PayTo Pin & Change Alert (Next-20 #15): pin creation from a
fixture route (payTo set captured, commitment verifies), verify
MATCH / MOVED / ROUTE_GONE, the once-per-distinct-set signed
PAYTO_MOVED alert riding the existing change-delivery pipeline,
pin-alongside-verify creation, the unknown/wrong-secret codes,
the unpaid 402 at exactly 10000, and a Fleet-Pass-funded verify
debiting 10000 exactly once. Fully offline — MockTransport plays
both the target route and the alert webhook receiver, and the
SSRF guard passes the public-IP fixture host without DNS."""

import asyncio
import base64
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import fleetpass, paytopin, recheck

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
THIRD = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
URL = ORIGIN + "/v1/thing"
WEBHOOK = ORIGIN + "/hook"
SECRET = "pin-secret-0123456789"
OTHER_SECRET = "pin-secret-9876543210"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("STALLROW_RECHECK_WEBHOOK_BACKOFF_S", "0")
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Target:
    """A mutable fixture x402 route: payTo set + reachability."""

    def __init__(self, pay_tos=(OWNER,)):
        self.pay_tos = list(pay_tos)
        self.gone = False
        self.probes = 0

    def handler(self, request):
        if request.url.path == "/hook":
            return httpx.Response(200, json={"ok": True})
        self.probes += 1
        if self.gone:
            raise httpx.ConnectError("gone", request=request)
        accepts = [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC,
                "amount": "5000",
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
            }
            for pay_to in self.pay_tos
        ]
        challenge = {
            "x402Version": 2,
            "error": "Payment required",
            "resource": {"url": URL, "description": "A target."},
            "accepts": accepts,
        }
        raw = base64.b64encode(json.dumps(challenge).encode()).decode()
        return httpx.Response(
            402, headers={"payment-required": raw}, json=challenge
        )


def _client(target) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(target.handler))


def _create(store, target, **kw):
    return run(paytopin.create_pin(_client(target), store, URL, SECRET, **kw))


# --- creation -----------------------------------------------------------------


def test_creation_captures_set_and_commitment_verifies(store):
    target = Target(pay_tos=(OWNER, OTHER))
    view, created = _create(store, target)
    assert created is True
    assert view["pin_id"].startswith("pin_")
    assert view["url"] == URL
    # The set is every accept's payTo, normalized (lowercased) + sorted.
    assert view["payto_set"] == sorted([OWNER.lower(), OTHER.lower()])
    assert view["created_at"]
    assert view["expires"] is False
    assert target.probes == 1  # creation performs ONE unpaid probe
    # The commitment recomputes from the read's own fields.
    recomputed = paytopin.commitment_for(
        view["pin_id"], view["url"], view["payto_set"], view["created_at"]
    )
    assert view["commitment"] == recomputed
    assert view["commitment_verified"] is True
    assert paytopin.verify_commitment(view) is True
    tampered = {**view, "payto_set": [THIRD.lower()]}
    assert paytopin.verify_commitment(tampered) is False
    assert SECRET not in json.dumps(view)


def test_creation_is_idempotent_per_holder(store):
    target = Target()
    first, c1 = _create(store, target)
    second, c2 = _create(store, target)
    assert c1 is True and c2 is False
    assert second["pin_id"] == first["pin_id"]
    assert store.payto_pin_count() == 1
    # A different holder pins the same route independently.
    other, c3 = run(
        paytopin.create_pin(_client(target), store, URL, OTHER_SECRET)
    )
    assert c3 is True and other["pin_id"] != first["pin_id"]


def test_creation_refusals_store_nothing(store):
    target = Target()
    with pytest.raises(paytopin.PinError) as e:
        run(paytopin.create_pin(_client(target), store, URL, "short"))
    assert e.value.http_status == 422
    with pytest.raises(paytopin.PinError) as e:
        run(paytopin.create_pin(_client(target), store, "not-a-url", SECRET))
    assert e.value.http_status == 422
    gone = Target()
    gone.gone = True
    with pytest.raises(paytopin.PinError) as e:
        run(paytopin.create_pin(_client(gone), store, URL, SECRET))
    assert e.value.code == "no_payto_found"
    assert store.payto_pin_count() == 0


# --- management reads -----------------------------------------------------------


def test_read_delete_and_secret_codes(store):
    target = Target()
    view, _ = _create(store, target)
    pin_id = view["pin_id"]
    read = paytopin.read_pin(store, pin_id, SECRET)
    assert read["pin_id"] == pin_id and read["commitment"] == view["commitment"]
    with pytest.raises(paytopin.PinError) as e:
        paytopin.read_pin(store, "pin_nope", SECRET)
    assert e.value.http_status == 404
    with pytest.raises(paytopin.PinError) as e:
        paytopin.read_pin(store, pin_id, OTHER_SECRET)
    assert e.value.http_status == 403
    with pytest.raises(paytopin.PinError) as e:
        paytopin.delete_pin(store, pin_id, OTHER_SECRET)
    assert e.value.http_status == 403
    assert store.payto_pin_count() == 1  # wrong-secret delete removed nothing
    out = paytopin.delete_pin(store, pin_id, SECRET)
    assert out == {"ok": True, "deleted": pin_id, "url": URL}
    with pytest.raises(paytopin.PinError) as e:
        paytopin.read_pin(store, pin_id, SECRET)
    assert e.value.http_status == 404


# --- verify -----------------------------------------------------------------------


def test_verify_match(store):
    target = Target()
    view, _ = _create(store, target)
    report = run(
        paytopin.verify_pin(_client(target), store, pin_id=view["pin_id"])
    )
    assert report["status"] == "MATCH"
    assert report["pinned_payto_set"] == report["current_payto_set"]
    assert report["commitment"] == view["commitment"]
    assert report["commitment_verified"] is True
    assert report["pin_created"] is False
    assert report["alert_queued"] is False


def test_verify_moved_alerts_once_per_distinct_set(store):
    target = Target()
    view, _ = _create(store, target, webhook_url=WEBHOOK)
    pin_id = view["pin_id"]
    # The pin's webhook config rides the existing pipeline's table.
    cfg = store.get_change_webhook(f"pin:{pin_id}")
    assert cfg is not None and cfg["webhook_url"] == WEBHOOK

    target.pay_tos = [OTHER]
    report = run(paytopin.verify_pin(_client(target), store, pin_id=pin_id))
    assert report["status"] == "MOVED"
    assert report["before"] == [OWNER.lower()]
    assert report["after"] == [OTHER.lower()]
    assert report["alert_queued"] is True
    deliveries = store.list_change_deliveries(f"pin:{pin_id}")
    assert len(deliveries) == 1
    payload = deliveries[0]["payload"]
    assert payload["event"] == "PAYTO_MOVED"
    assert payload["pin_id"] == pin_id
    assert payload["route"] == URL
    assert payload["before"] == [OWNER.lower()]
    assert payload["after"] == [OTHER.lower()]
    assert payload["observed_at"]

    # The queued delivery actually sends, HMAC-signed with the
    # holder secret, through deliver_change_event.
    captured: list = []

    def receiver(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    receiver_client = httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    )
    result = run(
        recheck.deliver_change_event(
            receiver_client, store, deliveries[0]["id"]
        )
    )
    assert result["status"] == "delivered"
    assert store.get_change_delivery(deliveries[0]["id"])["status"] == "delivered"
    assert len(captured) == 1
    body = captured[0].content
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert captured[0].headers["x-recheck-signature"] == f"sha256={expected}"
    assert json.loads(body)["event"] == "PAYTO_MOVED"

    # Second verify of the SAME new set: still MOVED, no new alert.
    again = run(paytopin.verify_pin(_client(target), store, pin_id=pin_id))
    assert again["status"] == "MOVED"
    assert again["alert_queued"] is False
    assert len(store.list_change_deliveries(f"pin:{pin_id}")) == 1

    # A second, DIFFERENT swap alerts again.
    target.pay_tos = [THIRD]
    third = run(paytopin.verify_pin(_client(target), store, pin_id=pin_id))
    assert third["status"] == "MOVED" and third["alert_queued"] is True
    assert len(store.list_change_deliveries(f"pin:{pin_id}")) == 2


def test_verify_moved_without_webhook_queues_nothing(store):
    target = Target()
    view, _ = _create(store, target)
    target.pay_tos = [OTHER]
    report = run(
        paytopin.verify_pin(_client(target), store, pin_id=view["pin_id"])
    )
    assert report["status"] == "MOVED" and report["alert_queued"] is False
    assert store.list_change_deliveries(f"pin:{view['pin_id']}") == []


def test_alert_subscribe_registers_webhook(store):
    target = Target()
    view, _ = _create(store, target)
    updated = run(
        paytopin.subscribe_alert(store, view["pin_id"], SECRET, WEBHOOK)
    )
    assert updated["webhook_url"] == WEBHOOK
    assert updated["alerts"]["registered"] is True
    target.pay_tos = [OTHER]
    report = run(
        paytopin.verify_pin(_client(target), store, pin_id=view["pin_id"])
    )
    assert report["alert_queued"] is True


def test_verify_route_gone_is_a_delivered_report(store):
    target = Target()
    view, _ = _create(store, target)
    target.gone = True
    report = run(
        paytopin.verify_pin(_client(target), store, pin_id=view["pin_id"])
    )
    assert report["status"] == "ROUTE_GONE"
    assert report["pinned_payto_set"] == [OWNER.lower()]
    assert report["current_payto_set"] is None
    assert report["commitment"] == view["commitment"]
    # The pin survives a gone route untouched.
    assert paytopin.read_pin(store, view["pin_id"], SECRET)["pin_id"]


def test_verify_unknown_and_wrong_secret(store):
    target = Target()
    view, _ = _create(store, target)
    with pytest.raises(paytopin.PinError) as e:
        run(paytopin.verify_pin(_client(target), store, pin_id="pin_nope"))
    assert e.value.http_status == 404
    with pytest.raises(paytopin.PinError) as e:
        run(
            paytopin.verify_pin(
                _client(target), store, view["pin_id"], None,
                OTHER_SECRET,
            )
        )
    assert e.value.http_status == 403
    with pytest.raises(paytopin.PinError) as e:
        run(paytopin.verify_pin(_client(target), store))
    assert e.value.http_status == 422


def test_verify_alongside_creation_uses_one_probe(store):
    target = Target()
    report = run(
        paytopin.verify_pin(
            _client(target), store, url=URL, secret=SECRET
        )
    )
    assert report["pin_created"] is True
    assert report["status"] == "MATCH"
    assert report["pin_id"].startswith("pin_")
    assert target.probes == 1  # the verify's probe IS the pin's probe
    assert store.payto_pin_count() == 1
    # The created pin is a real pin: readable, and the next verify
    # by pin_id matches without creating anything.
    read = paytopin.read_pin(store, report["pin_id"], SECRET)
    assert read["payto_set"] == [OWNER.lower()]
    again = run(
        paytopin.verify_pin(
            _client(target), store, url=URL, secret=SECRET
        )
    )
    assert again["pin_created"] is False and again["status"] == "MATCH"
    assert store.payto_pin_count() == 1


# --- HTTP surface -------------------------------------------------------------------


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    fresh = recheck.WatchStore(str(tmp_path / "w.db"))
    monkeypatch.setattr(main, "_recheck_store", fresh)
    return fresh


def test_unpaid_verify_is_402_at_exactly_10000():
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post("/v1/payto-pin/verify", json={"pin_id": "pin_x"})
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    accept = challenge["accepts"][0]
    assert accept["amount"] == "10000"
    assert accept["payTo"] == OWNER
    # The free management surface is NOT paywalled.
    r = client.post(
        "/v1/payto-pin/read", json={"pin_id": "pin_x", "secret": SECRET}
    )
    assert r.status_code == 404


def test_http_pin_lifecycle(http_env, monkeypatch):
    target = Target()
    monkeypatch.setattr(main, "_fetch_client", lambda: _client(target))
    client = TestClient(main.app, raise_server_exceptions=False)

    created = client.post(
        "/v1/payto-pin", json={"url": URL, "secret": SECRET}
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["created"] is True
    assert body["payto_set"] == [OWNER.lower()]
    assert body["commitment_verified"] is True
    pin_id = body["pin_id"]

    wrong = client.post(
        "/v1/payto-pin/read",
        json={"pin_id": pin_id, "secret": OTHER_SECRET},
    )
    assert wrong.status_code == 403
    unknown = client.post(
        "/v1/payto-pin/read", json={"pin_id": "pin_nope", "secret": SECRET}
    )
    assert unknown.status_code == 404
    read = client.post(
        "/v1/payto-pin/read", json={"pin_id": pin_id, "secret": SECRET}
    )
    assert read.status_code == 200 and read.json()["pin_id"] == pin_id

    removed = client.post(
        "/v1/payto-pin/delete", json={"pin_id": pin_id, "secret": SECRET}
    )
    assert removed.json() == {"ok": True, "deleted": pin_id, "url": URL}
    gone = client.post(
        "/v1/payto-pin/read", json={"pin_id": pin_id, "secret": SECRET}
    )
    assert gone.status_code == 404


def test_http_fleet_pass_verify_debits_10000_once(http_env, monkeypatch):
    target = Target()
    monkeypatch.setattr(main, "_fetch_client", lambda: _client(target))
    fleet_pass, created = http_env.mint_pass(
        f"pay-pin-{uuid.uuid4().hex}", fleetpass.PASS_PRICE_UNITS,
        fleetpass.PASS_TTL_DAYS,
    )
    assert created
    assert fleetpass.price_for("stall-row", "/v1/payto-pin/verify") == 10_000
    pin, _ = run(
        paytopin.create_pin(_client(target), http_env, URL, SECRET)
    )
    client = TestClient(main.app, raise_server_exceptions=False)
    r = client.post(
        "/v1/payto-pin/verify",
        json={"pin_id": pin["pin_id"]},
        headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "MATCH"
    after = http_env.get_pass(fleet_pass["pass_id"])
    assert after["balance_units"] == 5_000_000 - 10_000
    assert http_env.pass_debit_count(fleet_pass["pass_id"]) == 1
    # An unknown pin under a pass is refused and the debit refunded.
    r = client.post(
        "/v1/payto-pin/verify",
        json={"pin_id": "pin_nope"},
        headers={"X-Fleet-Pass": fleet_pass["pass_id"]},
    )
    assert r.status_code == 404
    after = http_env.get_pass(fleet_pass["pass_id"])
    assert after["balance_units"] == 5_000_000 - 10_000
