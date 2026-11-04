"""Watch by Defect Class (Next-20 #10): class-keyed subscriptions
over the confirmed drift/patrol events.

Pins:
  * the class taxonomy is the fleet's own (patrol snapshot classes);
    an invented class is refused;
  * the first evaluation is a FREE silent baseline — events already
    in the book are marked seen, nothing is delivered or debited;
  * a NEW confirmed event in the class is delivered and debited
    exactly $0.005 (5000 units); events outside the class or
    outside the origin scope are not delivered and not debited;
  * a pack-funded subscription must scope to exactly the pack's
    URL (market-wide pack subscriptions are refused);
  * pause stops evaluations (no debits); cancel is terminal
    (resume/cancel-after-cancel semantics per Index Sub v2);
  * registration is free and not a priced route.
"""

import json

import httpx
import pytest

from app.services import defectwatch, fleetpass, recheck

ORIGIN = "https://93.184.216.34"
OTHER_ORIGIN = "https://93.184.216.99"
WEBHOOK = "https://93.184.216.35/hook"


@pytest.fixture()
def store(tmp_path):
    return recheck.WatchStore(str(tmp_path / "watches.db"))


def _snap(rows):
    return {"snapshot_id": "test", "generated_at": "2026-11-02T00:00:00Z", "routes": rows}


def _row(url, defect_class, first="2026-11-01T00:00:00Z", last="2026-11-01T06:00:00Z"):
    return {
        "url": url,
        "origin": url.rsplit("/", 2)[0] if url.count("/") > 2 else ORIGIN,
        "defect_class": defect_class,
        "status": "open",
        "first_seen": first,
        "last_seen": last,
    }


SNAP_V1 = _snap([_row(f"{ORIGIN}/v1/llm", "payto_mismatch")])
SNAP_V2 = _snap(
    [
        _row(f"{ORIGIN}/v1/llm", "payto_mismatch"),
        _row(f"{ORIGIN}/v1/chat", "payto_mismatch", first="2026-11-02T00:00:00Z"),
        _row(f"{OTHER_ORIGIN}/v1/x", "payto_mismatch", first="2026-11-02T01:00:00Z"),
        _row(f"{ORIGIN}/v1/amt", "amount_mismatch", first="2026-11-02T02:00:00Z"),
    ]
)


def _transport(state):
    def handler(request):
        if str(request.url) == WEBHOOK:
            state["deliveries"].append(request)
            return httpx.Response(200, text="ok")
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


async def _register(store, **over):
    fleet_pass, _ = store.mint_pass(
        "pay-dw", fleetpass.PASS_PRICE_UNITS, fleetpass.PASS_TTL_DAYS
    )
    kwargs = dict(
        defect_class="payto_mismatch",
        origin=None,
        resource_contains=None,
        service_name=None,
        webhook_url=WEBHOOK,
        email=None,
        pack_id=None,
        fleet_pass_id=fleet_pass["pass_id"],
    )
    kwargs.update(over)
    reg = await defectwatch.register(store, **kwargs)
    return reg, fleet_pass


@pytest.mark.asyncio
async def test_taxonomy_and_registration_shape(store):
    reg, _ = await _register(store)
    assert reg["reg_id"].startswith("def_")
    assert reg["event_price_units"] == 5000
    assert set(reg["defect_classes"]) == set(defectwatch.DEFECT_CLASSES)
    with pytest.raises(defectwatch.DefectWatchError):
        await _register(store, defect_class="vibes_drift")


@pytest.mark.asyncio
async def test_baseline_then_per_event_debit(store):
    reg, fleet_pass = await _register(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        out = await defectwatch.evaluate(
            client, store, reg, patrol_snapshot=SNAP_V1
        )
        assert out["status"] == "baseline" and out["debited_units"] == 0
        assert state["deliveries"] == []
        out = await defectwatch.evaluate(
            client, store, reg, patrol_snapshot=SNAP_V2
        )
        # Two new payto_mismatch events (llm/chat + other origin);
        # the amount_mismatch row is a different class.
        assert out["status"] == "events" and out["events_count"] == 2
        assert out["debited_units"] == 2 * 5000
        assert len(state["deliveries"]) == 2
        body = json.loads(state["deliveries"][0].content)
        assert body["event"] == "defect_event"
        assert body["defect_class"] == "payto_mismatch"
        assert body["debited_units"] == 5000
        assert store.get_pass(fleet_pass["pass_id"])["balance_units"] == (
            fleetpass.PASS_PRICE_UNITS - 10_000
        )
        # Nothing new: no delivery, no debit.
        out = await defectwatch.evaluate(
            client, store, reg, patrol_snapshot=SNAP_V2
        )
        assert out["status"] == "no_new_events" and out["debited_units"] == 0
        assert len(state["deliveries"]) == 2


@pytest.mark.asyncio
async def test_origin_scope_filters_events(store):
    reg, _ = await _register(store, origin=ORIGIN)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        await defectwatch.evaluate(client, store, reg, patrol_snapshot=SNAP_V1)
        out = await defectwatch.evaluate(
            client, store, reg, patrol_snapshot=SNAP_V2
        )
        # Only the chat route on the scoped origin — the other
        # origin's payto event is out of scope.
        assert out["events_count"] == 1
        assert out["debited_units"] == 5000
        assert len(state["deliveries"]) == 1


@pytest.mark.asyncio
async def test_pack_scope_discipline(store):
    pack, _ = store.mint_pack("pay-dw2", f"{ORIGIN}/v1/llm")
    with pytest.raises(defectwatch.DefectWatchError):
        await defectwatch.register(
            store, "payto_mismatch", None, None, None, WEBHOOK, None,
            pack["pack_id"], None,
        )
    reg = await defectwatch.register(
        store, "payto_mismatch", None, pack["url"], None, WEBHOOK, None,
        pack["pack_id"], None,
    )
    assert reg["funding_kind"] == "pack"


@pytest.mark.asyncio
async def test_pause_and_cancel(store):
    reg, fleet_pass = await _register(store)
    state = {"deliveries": []}
    async with httpx.AsyncClient(transport=_transport(state)) as client:
        await defectwatch.evaluate(client, store, reg, patrol_snapshot=SNAP_V1)
        paused = defectwatch.pause(store, reg["reg_id"])
        assert paused["active"] is False and paused["last_status"] == "paused"
        out = await defectwatch.evaluate(
            client, store, reg, patrol_snapshot=SNAP_V2
        )
        assert out["debited_units"] == 0 and state["deliveries"] == []
        resumed = defectwatch.resume(store, reg["reg_id"])
        assert resumed["active"] is True
        cancelled = defectwatch.cancel(store, reg["reg_id"])
        assert cancelled["active"] is False
        assert cancelled["last_status"] == "cancelled"
        with pytest.raises(defectwatch.DefectWatchError) as e:
            defectwatch.resume(store, reg["reg_id"])
        assert e.value.http_status == 409
        with pytest.raises(defectwatch.DefectWatchError):
            defectwatch.pause(store, reg["reg_id"])
        with pytest.raises(defectwatch.DefectWatchError) as e:
            defectwatch.pause(store, "def_nope")
        assert e.value.http_status == 404


def test_defect_routes_are_not_priced():
    from app.config import PRICE_UNITS

    for path in (
        "/v1/defect-watch/register",
        "/v1/defect-watch/status",
        "/v1/defect-watch/run",
        "/v1/defect-watch/pause",
        "/v1/defect-watch/resume",
        "/v1/defect-watch/cancel",
    ):
        assert path not in PRICE_UNITS
