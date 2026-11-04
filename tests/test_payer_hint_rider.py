"""Item 5 rider: ?payer= pre-binding is honoured at mint and at
registration.

Pins:
  * a pack minted with a payer identity presented carries it as
    payer_hint (store view + pack-balance surface shape);
  * a pack minted WITHOUT it is unchanged (payer_hint None);
  * the same binding lands on Fleet Passes, watch registrations,
    defect subscriptions and self-serve webhook subscriptions;
  * the binding is recorded, never required: every surface keeps
    working with no payer presented anywhere.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import defectwatch, fleetpass, hooksubs, recheck, watch

TARGET = "https://93.184.216.34/v1/thing"
WEBHOOK = "https://93.184.216.35/hook"
PAYER = "0x1234567890abcdef1234567890abcdef12345678"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def test_store_level_pack_and_pass_hints(store):
    pack, _ = store.mint_pack("pay-r1", TARGET, payer_hint=PAYER)
    assert pack["payer_hint"] == PAYER
    assert store.get_pack(pack["pack_id"])["payer_hint"] == PAYER
    plain, _ = store.mint_pack("pay-r2", TARGET)
    assert plain["payer_hint"] is None
    fleet_pass, _ = store.mint_pass(
        "pay-r3", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS,
        payer_hint=PAYER,
    )
    assert fleet_pass["payer_hint"] == PAYER
    assert store.get_pass(fleet_pass["pass_id"])["payer_hint"] == PAYER
    tk, _ = store.mint_toolkit_pack("pay-r4", payer_hint=PAYER)
    assert tk["payer_hint"] == PAYER


@pytest.mark.asyncio
async def test_registration_hints(store):
    pack, _ = store.mint_pack("pay-r5", TARGET)
    reg = await watch.register(
        store, TARGET, WEBHOOK, None, None, pack["pack_id"], None, payer=PAYER
    )
    assert reg["payer_hint"] == PAYER
    reg2 = await watch.register(
        store, TARGET, WEBHOOK, None, None, pack["pack_id"], None
    )
    assert reg2["payer_hint"] is None

    fleet_pass, _ = store.mint_pass(
        "pay-r6", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    sub = await defectwatch.register(
        store, "payto_mismatch", None, None, None, WEBHOOK, None,
        None, fleet_pass["pass_id"], payer=PAYER,
    )
    assert sub["payer_hint"] == PAYER


@pytest.mark.asyncio
async def test_hooksub_hint(store):
    pack, _ = store.mint_pack("pay-r7", TARGET)
    view = await hooksubs.register(
        store, WEBHOOK, "a-secret-of-some-length", url=TARGET,
        pack_id=pack["pack_id"], payer=PAYER,
    )
    assert view["payer_hint"] == PAYER
    stored = store.get_webhook_sub(view["sub_id"])
    assert stored["payer_hint"] == PAYER


@pytest.fixture()
def http_env(monkeypatch, tmp_path):
    store = recheck.WatchStore(str(tmp_path / "watches.db"))
    monkeypatch.setattr(main, "_recheck_store", store)
    return TestClient(main.app, raise_server_exceptions=False), store


def test_pack_mint_honours_query_payer(http_env, monkeypatch):
    """HTTP surface: the deep link is ?payer= on the (paid) mint
    route — exercised at the handler's level by simulating the
    post-payment state the middleware produces is out of scope;
    instead pin the helper + store composition the handler uses."""
    client, store = http_env
    from fastapi import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/recheck/pack",
        "query_string": f"payer={PAYER}".encode(),
        "headers": [],
    }
    request = Request(scope)
    assert main._payer_of(request) == PAYER
    assert main._payer_of(request, "0xbody") == "0xbody"  # body wins
    empty = Request({**scope, "query_string": b""})
    assert main._payer_of(empty) is None
    assert main._payer_of(empty, "   ") is None
