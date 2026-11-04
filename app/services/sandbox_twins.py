"""Sandbox endpoint twins (Next-20 #19) — FREE rehearsal twins of every
priced route on this stall.

For every priced route `POST /v1/<thing>` there is a twin
`POST /sandbox/v1/<thing>`:

- SAME request schema and validation as the priced route (the twin is
  bound to the priced route's own request model, so invalid requests
  fail with the same 422 validation errors).
- NO payment leg: the twin path is deliberately absent from
  PRICE_UNITS, so the paywall never challenges it, no pack or Fleet
  Pass is debited, nothing settles, and the delivery-layer loop block
  (which keys off PRICE_UNITS) is never attached. A sandbox delivery
  is not a paid delivery and is never recorded in payer memory.
- The response is a DETERMINISTIC FIXTURE in the same shape as the
  real route's delivery: the stall's committed sample body for that
  route (app/data/samples — the same fixtures the /sample payloads
  publish). A twin never does the real work for free: no live probing,
  no chain reads, no upstream fetches. The caller's target value may
  be reflected into the fixture's target fields, but every verdict /
  finding stays a fixture value.
- Every twin body carries an unmissable marker: top-level
  ``"sandbox": true`` plus a ``sandbox_note``, so a rehearsal can
  never be mistaken for a real delivery.

Deterministic edge-case scenarios are selected with the
``X-Sandbox-Scenario`` header (default ``normal``):

- ``rotated_payto``  — the fixture's target payTo is rotated to a
  fixture address (and, where the delivery carries a change list, a
  payTo field-change is recorded and an UNCHANGED status flips to
  CHANGED).
- ``drifted_schema`` — the fixture's target schema shows drift
  (schema hashes replaced, declared schemas marked drifted, a schema
  drift defect / field-change recorded where the shape carries one).
- ``flagged_wallet`` — the fixture's target payTo / wallet is a
  flagged fixture address (sanctions / flag blocks in the shape are
  set to their flagged form).

A scenario that has no meaning for a route returns the normal fixture
with ``sandbox_scenario_applied: false`` and a note — a meaningless
variant is never fabricated.

Discovery: twins are FREE and stay OUT of /.well-known/x402 (the
priced manifest is untouched). They are advertised in llms.txt /
skill.md and listed by the free index at ``GET /sandbox``.

This module is fleet-shared: it is parameterised by the calling
stall's priced-route table, samples manifest and sample loader.
NOTE: this is NOT services/sandbox.py (StallRow's mutable test
fixture for change detection) — the twins are per-route rehearsal
endpoints.
"""

from __future__ import annotations

import copy
import inspect
import typing

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SANDBOX_PREFIX = "/sandbox"
SCENARIO_HEADER = "X-Sandbox-Scenario"
SCENARIOS = ("normal", "rotated_payto", "drifted_schema", "flagged_wallet")

# Deterministic fixture addresses. Deliberately synthetic and distinct
# from every address in the committed samples.
ROTATED_PAYTO = "0x3333333333333333333333333333333333333333"
FLAGGED_PAYTO = "0x4444444444444444444444444444444444444444"
DRIFTED_SCHEMA_HASH = "deadbeef" * 8

_PAYTO_KEYS = ("payTo", "pay_to")
# Fixture fields the caller's own value may be reflected into (the
# target of the check) — verdicts/findings are never reflected.
_REFLECT_KEYS = (
    "target",
    "url",
    "resource_url",
    "endpoint_url",
    "resource",
    "address",
    "token_address",
    "wallet",
    "chain",
    "mode",
)


def _walk(obj):
    """Yield every dict in a nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _replace_paytos(body: dict, new_value: str) -> int:
    n = 0
    for node in _walk(body):
        for key in _PAYTO_KEYS:
            if isinstance(node.get(key), str):
                node[key] = new_value
                n += 1
    return n


def _record_field_change(body: dict, field: str, detail: str) -> bool:
    """Append a change record to the first change list the fixture
    carries (top-level field_changes/changes, or a diff block's)."""
    containers = [body]
    diff = body.get("diff")
    if isinstance(diff, dict):
        containers.append(diff)
    for container in containers:
        for key in ("field_changes", "changes"):
            lst = container.get(key)
            if isinstance(lst, list):
                lst.append({"field": field, "change": detail, "sandbox_fixture": True})
                return True
    return False


def _apply_rotated_payto(body: dict) -> bool:
    if not _replace_paytos(body, ROTATED_PAYTO):
        return False
    _record_field_change(body, "payTo", f"payTo rotated to {ROTATED_PAYTO} (sandbox fixture)")
    if body.get("status") == "UNCHANGED":
        body["status"] = "CHANGED"
        if isinstance(body.get("summary"), str):
            body["summary"] = "Sandbox fixture: the target's payTo has rotated since the previous check."
    return True


def _apply_drifted_schema(body: dict) -> bool:
    applied = False
    for node in _walk(body):
        for key, value in list(node.items()):
            if key == "schema_hash" and isinstance(value, str):
                node[key] = DRIFTED_SCHEMA_HASH
                applied = True
            elif key in ("outputSchema", "inputSchema") and isinstance(value, dict):
                value["sandbox_drifted"] = True
                applied = True
    if _record_field_change(body, "outputSchema", "served schema drifted from the declared schema (sandbox fixture)"):
        applied = True
    defects = body.get("defects")
    if isinstance(defects, list):
        defects.append(
            {
                "name": "schema drift (sandbox fixture)",
                "severity": "major",
                "detail": "Fixture scenario: the target's served schema no longer matches its declared schema.",
            }
        )
        applied = True
    if body.get("status") == "UNCHANGED":
        body["status"] = "CHANGED"
        applied = True
    return applied


def _apply_flagged_wallet(body: dict) -> bool:
    applied = False
    if _replace_paytos(body, FLAGGED_PAYTO):
        applied = True
    for node in _walk(body):
        sanctions = node.get("sanctions")
        if isinstance(sanctions, dict):
            sanctions["sandbox_flagged"] = True
            sanctions["listed"] = True
            applied = True
        if "payto_flag" in node:
            node["payto_flag"] = {
                "flagged": True,
                "pay_to": FLAGGED_PAYTO,
                "source": "sandbox fixture flagged list — not a real flag",
            }
            applied = True
        if node.get("flagged") is False:
            node["flagged"] = True
            applied = True
    if applied:
        body["sandbox_flagged_payto"] = FLAGGED_PAYTO
    return applied


_SCENARIO_APPLIERS = {
    "rotated_payto": _apply_rotated_payto,
    "drifted_schema": _apply_drifted_schema,
    "flagged_wallet": _apply_flagged_wallet,
}


def build_fixture(sample_body: dict, route_path: str, payload: dict, scenario: str) -> dict:
    """The deterministic twin delivery for one call: the committed
    sample body, target-reflected, scenario-shaped, sandbox-marked."""
    body = copy.deepcopy(sample_body)
    # Reflect the caller's target into the fixture's target fields:
    # the request names it under its own key (url / resource_url /
    # address / token_address / ...), the fixture may carry it as
    # target / url / resource_url. Verdicts never reflect.
    target_value = next(
        (
            payload[key]
            for key in ("resource_url", "endpoint_url", "url", "resource", "target", "address", "token_address")
            if isinstance(payload.get(key), str) and payload[key]
        ),
        None,
    )
    if target_value:
        for key in ("target", "url", "resource_url", "endpoint_url", "resource"):
            if isinstance(body.get(key), str):
                body[key] = target_value
    for key in _REFLECT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value and isinstance(body.get(key), str):
            body[key] = value
    body.pop("verdict_row_loop", None)
    applied = True
    if scenario != "normal":
        applied = bool(_SCENARIO_APPLIERS[scenario](body))
    body["sandbox"] = True
    body["live"] = False
    body["sandbox_of"] = f"POST {route_path}"
    body["sandbox_scenario"] = scenario
    body["sandbox_scenario_applied"] = applied
    body["sandbox_note"] = (
        f"SANDBOX TWIN of POST {route_path} — a free rehearsal fixture. "
        "No payment was taken or settled, no pack/pass was debited, "
        "nothing was probed, read on-chain or fetched live, and this "
        "is NOT a real delivery. Verdicts and findings are fixture "
        "values from the route's published sample."
    )
    if not applied:
        body["sandbox_scenario_note"] = (
            f"Scenario '{scenario}' does not apply to this route; the "
            "normal fixture is returned unchanged."
        )
    return body


def _body_model(endpoint) -> type[BaseModel] | None:
    """The priced route handler's request-body model, if it has one."""
    try:
        params = inspect.signature(endpoint).parameters.values()
    except (TypeError, ValueError):
        return None
    try:
        # The stalls use `from __future__ import annotations`, so the
        # raw annotation is a string — resolve it against the
        # endpoint's module globals.
        hints = typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001 — fall back to raw annotations
        hints = {}
    for param in params:
        annotation = hints.get(param.name, param.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
    return None


def _make_handler(model, route_path: str, slug: str, sample_get):
    async def handler(payload=None, request: Request = None):  # noqa: ANN001
        scenario = (request.headers.get(SCENARIO_HEADER) or "normal").strip().lower()
        if scenario not in SCENARIOS:
            return JSONResponse(
                {
                    "error": (
                        f"unknown sandbox scenario '{scenario}'; expected one of: "
                        + ", ".join(SCENARIOS)
                    ),
                    "code": "validation_error",
                },
                status_code=422,
            )
        sample_body = sample_get(slug)
        if sample_body is None:
            return JSONResponse(
                {"error": "no fixture sample for this route", "code": "internal_error"},
                status_code=502,
            )
        data = payload.model_dump() if model is not None else {}
        return build_fixture(sample_body, route_path, data, scenario)

    params = []
    annotations: dict = {}
    if model is not None:
        params.append(
            inspect.Parameter(
                "payload", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=model
            )
        )
        annotations["payload"] = model
    params.append(
        inspect.Parameter(
            "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request
        )
    )
    annotations["request"] = Request
    handler.__signature__ = inspect.Signature(parameters=params)
    handler.__annotations__ = annotations
    handler.__name__ = "sandbox_twin_" + route_path.strip("/").replace("/", "_").replace("-", "_")
    return handler


def register_sandbox_twins(
    app,
    *,
    service: str,
    priced_paths,
    manifest_samples: list[dict],
    sample_get,
) -> list[dict]:
    """Register GET /sandbox plus one POST twin per priced route.
    Call AFTER every priced route is defined (the twins bind to the
    priced routes' own request models). Returns the twin inventory."""
    slug_by_route = {s["route"]: s["slug"] for s in manifest_samples}
    models: dict[str, type[BaseModel] | None] = {}
    for route in list(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path in priced_paths and "POST" in methods and path not in models:
            models[path] = _body_model(route.endpoint)

    twins: list[dict] = []
    for route_path in priced_paths:
        slug = slug_by_route[route_path]
        twin_path = f"{SANDBOX_PREFIX}{route_path}"
        app.post(twin_path)(_make_handler(models.get(route_path), route_path, slug, sample_get))
        twins.append(
            {
                "route": route_path,
                "sandbox_route": twin_path,
                "method": "POST",
                "price": "free",
                "fixture_sample": f"/sample/{slug}",
            }
        )

    def sandbox_index():
        return {
            "service": service,
            "sandbox": True,
            "live": False,
            "note": (
                "FREE sandbox twins of every priced route on this stall "
                "(Next-20 #19): same request schema and validation as the "
                "priced route, no payment leg (no 402, no pack/pass debit, "
                "nothing settled or recorded as a sale), deterministic "
                "fixture deliveries in the real routes' response shapes, "
                "each marked sandbox=true. Twins never do the real work: "
                "no live probing, chain reads or upstream fetches. Select "
                f"an edge-case scenario with the {SCENARIO_HEADER} header: "
                + ", ".join(SCENARIOS)
                + " (a scenario that does not apply to a route returns "
                "its normal fixture with sandbox_scenario_applied=false)."
            ),
            "scenario_header": SCENARIO_HEADER,
            "scenarios": list(SCENARIOS),
            "count": len(twins),
            "twins": twins,
        }

    app.get(SANDBOX_PREFIX)(sandbox_index)
    return twins
