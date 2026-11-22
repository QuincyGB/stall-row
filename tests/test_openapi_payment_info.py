"""AgentCash canonical discovery (W8 spec 3b): every paid operation in
the served OpenAPI document carries x-payment-info (x402 protocol,
fixed decimal-USD price matching the settings the route configs
charge) and a declared 402 response; the info block carries
x-guidance and the fleet contact email. Free operations carry none.
"""

from fastapi.testclient import TestClient

import app.main as main
from app.config import PRICE_UNITS, SOLANA_ACCEPT_PATHS

PAID = {
    path: main.settings.price_for(path).strip().lstrip("$") for path in PRICE_UNITS
}

_DOC = None


def openapi_doc():
    global _DOC
    if _DOC is None:
        with TestClient(main.app) as client:
            r = client.get("/openapi.json")
            assert r.status_code == 200
            _DOC = r.json()
    return _DOC


def test_info_guidance_and_contact():
    info = openapi_doc()["info"]
    assert info["x-guidance"]
    assert main.settings.service_name in info["x-guidance"]
    assert info["contact"] == {
        "name": "Verdict Row",
        "email": "patrol@verdictrow.com",
    }


def test_paid_operations_carry_payment_info():
    doc = openapi_doc()
    for path, price in PAID.items():
        op = doc["paths"][path]["post"]
        pay = op["x-payment-info"]
        assert pay["price"] == {
            "mode": "fixed",
            "currency": "USD",
            "amount": price,
        }, path
        proto = pay["protocols"][0]["x402"]
        assert proto["scheme"] == "exact"
        assert proto["network"] == main.settings.network
        assert proto["payTo"] == main.settings.pay_to
        if path in SOLANA_ACCEPT_PATHS:
            assert len(pay["protocols"]) == 2
            assert pay["protocols"][1]["x402"]["network"] == main.settings.solana_network
        assert op["responses"]["402"]["description"] == "Payment Required"


def test_no_other_operation_is_marked_paid():
    doc = openapi_doc()
    for path, methods in doc["paths"].items():
        for method, op in methods.items():
            if path in PAID and method == "post":
                continue
            assert "x-payment-info" not in op, (path, method)
            assert "402" not in (op.get("responses") or {}), (path, method)
