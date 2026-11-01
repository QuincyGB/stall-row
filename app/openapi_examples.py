"""Worked request/response examples for this stall's priced routes,
injected into the served OpenAPI document (Item 3 of the 2026-10-31
fleet build pass).

The example VALUES are this stall's canonical discovery examples
(app/discovery.py — the same input/output examples the Bazaar
extension and the free /sample payloads are built from), so the
OpenAPI pair can never drift from the shapes the stall already
publishes. They are FIXTURE values, marked in-band three ways: the
OpenAPI example object is named "worked_example", its summary says
"fixture values — not live data", and the operation carries
x-worked-example: {fixture: true}. No grader should mistake them
for live results; buy the route for live data.

Only examples are added — no route, price, or schema shape changes.
"""

from __future__ import annotations

import copy

from .discovery import ROUTES

WORKED_EXAMPLES: dict[str, tuple[dict, dict]] = {
    path: (route["input_example"], route["output_example"])
    for path, route in ROUTES.items()
}

FIXTURE_SUMMARY = (
    "Worked example — fixture values, not live data "
    "(same example this stall's discovery doc and free /sample publish)"
)


def add_worked_examples(schema: dict) -> dict:
    """Attach the worked request/response example pair to every
    priced operation in a generated OpenAPI document, in place."""
    paths = schema.get("paths") or {}
    for path, (request_example, response_example) in WORKED_EXAMPLES.items():
        op = (paths.get(path) or {}).get("post")
        if op is None:
            continue
        extension = {
            "fixture": True,
            "note": (
                "The request/response examples on this operation are "
                "worked examples with fixture values — real shapes, "
                "not live data."
            ),
        }
        if "requestBody" in op:
            content = op["requestBody"].setdefault("content", {}).setdefault(
                "application/json", {}
            )
            content["examples"] = {
                "worked_example": {
                    "summary": FIXTURE_SUMMARY,
                    "value": copy.deepcopy(request_example),
                }
            }
        else:
            # Body-less route (a pack/pass buy: the x402 payment alone
            # creates it, the request example is the empty body). No
            # requestBody is fabricated — the worked request example
            # rides in the extension instead.
            extension["request_example"] = copy.deepcopy(request_example)
            extension["request_note"] = (
                "This route takes no request body — the worked "
                "request example is the empty body {}."
            )
        op["x-worked-example"] = extension
        ok = op.setdefault("responses", {}).setdefault(
            "200", {"description": "Successful Response"}
        )
        rcontent = ok.setdefault("content", {}).setdefault(
            "application/json", {}
        )
        rcontent["examples"] = {
            "worked_example": {
                "summary": FIXTURE_SUMMARY,
                "value": copy.deepcopy(response_example),
            }
        }
    return schema
