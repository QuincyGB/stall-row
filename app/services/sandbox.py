"""The Recheck sandbox — a mutable x402 test fixture (improvement 7).

A clearly-labelled TEST resource that answers with a proper x402
402 challenge in the fleet's normal format, so an integrator (the
aicom survey, in the first instance) can validate their change-
detection ingestion at any time without spending money and without
waiting for a real seller to change something.

Discipline:
  * It is NOT a priced route, it is NOT in the discovery manifest's
    resources, and nothing about it is sellable — it exists only as
    a fixture. Its description says so, in the challenge itself.
  * Its terms live in the WatchStore (one sandbox_state row) and
    survive restarts exactly like watch state does.
  * Mutations are fleet-internal-secret gated at the HTTP layer
    (POST /v1/internal/sandbox/mutate); the read is public, exactly
    like a real resource's unpaid challenge.
  * set_payto is operator-only for the same reason: the gate means
    only the fleet operator can point it anywhere, and the operator
    points it only at fleet-controlled addresses (production:
    the fleet payTo and the fleet's own alternate wallets).

State shape: {amount, payTo, network, asset, description, schema}
where schema is the Bazaar output schema object the fingerprint
hashes. The default schema carries two fields — `verdict`
(string) and `score` (number) — so flip_schema_type has something
meaningful to flip.
"""

from __future__ import annotations

import copy

from ..rpc import is_address

# Canonical Base USDC — a public constant, matching the asset every
# fleet challenge names.
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SANDBOX_NETWORK = "eip155:8453"
SANDBOX_DESCRIPTION = (
    "StallRow sandbox — a TEST FIXTURE for Recheck integration "
    "testing. Not a real service; nothing is sold here and no "
    "payment is ever settled against this challenge."
)

_DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "score": {"type": "number"},
    },
    "required": ["verdict"],
}


def default_state(default_pay_to: str) -> dict:
    return {
        "amount": "5000",
        "payTo": default_pay_to,
        "network": SANDBOX_NETWORK,
        "asset": BASE_USDC,
        "description": SANDBOX_DESCRIPTION,
        "schema": copy.deepcopy(_DEFAULT_SCHEMA),
    }


def current_state(store, default_pay_to: str) -> dict:
    """The sandbox's live terms: the persisted state, or the
    defaults (unstored) when nothing has ever mutated it."""
    state = store.get_sandbox_state()
    if state is None:
        return default_state(default_pay_to)
    return state


def build_challenge(state: dict, resource_url: str) -> dict:
    """The x402 v2 challenge the sandbox serves — the same shape
    every fleet resource's unpaid response carries, so Recheck's
    challenge parser fingerprints it exactly like a real target."""
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": resource_url,
            "description": state["description"],
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": state["network"],
                "asset": state["asset"],
                "amount": state["amount"],
                "payTo": state["payTo"],
                "maxTimeoutSeconds": 300,
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {
                        "type": "http",
                        "method": "GET",
                        "bodyType": "json",
                        "body": {},
                    },
                    "output": {
                        "type": "json",
                        "schema": copy.deepcopy(state["schema"]),
                    },
                }
            }
        },
    }


def state_view(state: dict) -> dict:
    """The operator-facing summary a mutation answers with."""
    return {
        "amount": state["amount"],
        "payTo": state["payTo"],
        "network": state["network"],
        "asset": state["asset"],
        "description": state["description"],
        "schema": copy.deepcopy(state["schema"]),
    }


def apply_mutation(store, op: str, params: dict, default_pay_to: str) -> dict:
    """Apply one named mutation and persist the result. Raises
    ValueError with the reason for anything refused — the HTTP
    layer maps that to a loud 422 and nothing is stored."""
    state = current_state(store, default_pay_to)
    if op == "reset":
        state = default_state(default_pay_to)
        store.set_sandbox_state(state)
        return state_view(state)
    if op == "set_price":
        amount = str(params.get("amount") or "")
        if not amount.isdigit() or int(amount) <= 0:
            raise ValueError("set_price requires a positive integer amount (base units)")
        state["amount"] = amount
    elif op == "flip_schema_type":
        field = str(params.get("field") or "")
        props = state["schema"].get("properties") or {}
        if field not in props:
            raise ValueError(f"schema has no field named {field!r}")
        current = props[field].get("type")
        if current == "string":
            props[field]["type"] = "number"
        elif current == "number":
            props[field]["type"] = "string"
        else:
            raise ValueError(
                f"field {field!r} has type {current!r}; only string/number flip"
            )
    elif op == "add_required":
        field = str(params.get("field") or "")
        props = state["schema"].get("properties") or {}
        if field not in props:
            raise ValueError(f"schema has no field named {field!r}")
        required = state["schema"].setdefault("required", [])
        if field not in required:
            required.append(field)
    elif op == "remove_required":
        field = str(params.get("field") or "")
        required = state["schema"].get("required") or []
        if field not in required:
            raise ValueError(f"field {field!r} is not in required")
        state["schema"]["required"] = [f for f in required if f != field]
    elif op == "set_payto":
        pay_to = str(params.get("pay_to") or "")
        if not is_address(pay_to):
            raise ValueError("set_payto requires a valid EVM address")
        state["payTo"] = pay_to
    else:
        raise ValueError(f"unknown sandbox mutation op: {op!r}")
    store.set_sandbox_state(state)
    return state_view(state)
