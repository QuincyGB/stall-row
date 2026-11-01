"""OpenAPI worked-example pack (Item 3): every priced operation in
the served OpenAPI document carries a worked request example AND a
worked response example, marked as fixtures in-band, and each
example validates against the operation's declared schemas — the
request example against the OpenAPI request-body schema, the
response example against the route's declared discovery
outputSchema (the OpenAPI 200 schema itself is the framework's
empty schema, as before this change).

The example values are the stall's canonical discovery examples
(app/openapi_examples.py), never hand-copied here.
"""

from fastapi.testclient import TestClient

import app.main as main
from app.openapi_examples import WORKED_EXAMPLES
from app.discovery import ROUTES

PRICED_ROUTES = set(ROUTES)
OUTPUT_SCHEMAS = {p: r["output_schema"] for p, r in ROUTES.items()}

_DOC = None


def openapi_doc():
    global _DOC
    if _DOC is None:
        with TestClient(main.app) as client:
            r = client.get("/openapi.json")
            assert r.status_code == 200
            _DOC = r.json()
    return _DOC


def _pair(path):
    op = openapi_doc()["paths"][path]["post"]
    if "requestBody" in op:
        req = op["requestBody"]["content"]["application/json"]["examples"][
            "worked_example"
        ]
    else:
        # Body-less route: the worked request example (the empty
        # body) rides in the x-worked-example extension.
        req = {
            "summary": "fixture",
            "value": op["x-worked-example"]["request_example"],
        }
    resp = op["responses"]["200"]["content"]["application/json"]["examples"][
        "worked_example"
    ]
    return op, req, resp


def _request_schema(path):
    op = openapi_doc()["paths"][path]["post"]
    if "requestBody" not in op:
        return None
    return op["requestBody"]["content"]["application/json"].get("schema")

# --- stdlib structural JSON-Schema check (no new dependencies) -------------

def _resolve(schema, root):
    while isinstance(schema, dict) and "$ref" in schema:
        node = root
        for part in schema["$ref"][2:].split("/"):
            node = node[part]
        schema = node
    return schema


def _type_ok(value, t):
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": value is None,
    }.get(t, True)


def schema_errors(value, schema, root, path="$"):
    errs = []
    if not isinstance(schema, dict):
        return errs
    schema = _resolve(schema, root)
    for sub in schema.get("allOf", []):
        errs += schema_errors(value, sub, root, path)
    for kw in ("anyOf", "oneOf"):
        if kw in schema and not any(
            not schema_errors(value, s, root, path) for s in schema[kw]
        ):
            errs.append(f"{path}: no {kw} branch matched")
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if types and not any(_type_ok(value, tt) for tt in types):
        errs.append(f"{path}: type {type(value).__name__} not in {types}")
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required {req}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], root, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += schema_errors(v, schema["items"], root, f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than maxItems")
    return errs


def test_worked_examples_cover_exactly_the_priced_routes():
    assert set(WORKED_EXAMPLES) == PRICED_ROUTES
    for path in PRICED_ROUTES:
        assert path in openapi_doc()["paths"], path


def test_every_priced_operation_has_a_marked_example_pair():
    for path in sorted(PRICED_ROUTES):
        op, req, resp = _pair(path)
        assert op["x-worked-example"]["fixture"] is True, path
        for ex in (req, resp):
            assert "fixture" in ex["summary"].lower(), path
            assert isinstance(ex["value"], dict), path
        want_req, want_resp = WORKED_EXAMPLES[path]
        assert req["value"] == want_req, path
        assert resp["value"] == want_resp, path


def test_request_examples_validate_against_the_request_schemas():
    doc = openapi_doc()
    for path in sorted(PRICED_ROUTES):
        _, req, _ = _pair(path)
        schema = _request_schema(path)
        if schema is None:
            # Body-less route: the only valid request body is empty.
            assert req["value"] == {}, path
            continue
        errs = schema_errors(req["value"], schema, doc)
        assert not errs, f"{path}: {errs[:5]}"


def test_response_examples_validate_against_the_output_schemas():
    for path in sorted(PRICED_ROUTES):
        _, _, resp = _pair(path)
        schema = OUTPUT_SCHEMAS[path]
        errs = schema_errors(resp["value"], schema, schema)
        assert not errs, f"{path}: {errs[:5]}"
