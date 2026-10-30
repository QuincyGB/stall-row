"""Sign Reader: POST /v1/sign-read — v1 + v2 challenge parsing,
normalization with exact decimals, SDK-compat verdicts (the losbeto
class: a v2 challenge whose network id is not CAIP-2 must fail),
declared-vs-live mismatches incl. method, and failure honesty
(unreachable is a report, SSRF refusal is a ValueError). Fully
offline — MockTransport plays the target."""

import asyncio
import base64
import json

import httpx
import pytest

from app.services import recheck, signread

OWNER = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ORIGIN = "https://93.184.216.34"
URL = ORIGIN + "/v1/thing"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(recheck, "_BACKOFF_S", (0, 0))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _v2(amount="5000", network="eip155:8453", pay_to=OWNER):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": URL,
            "description": "A target service.",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "asset": USDC,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
            }
        ],
    }


def _v2_response(challenge=None, **kw):
    challenge = challenge if challenge is not None else _v2(**kw)
    raw = base64.b64encode(json.dumps(challenge).encode()).decode()
    return httpx.Response(402, headers={"payment-required": raw}, json=challenge)


def _v1(amount="5000"):
    return {
        "x402Version": 1,
        "error": "Payment required",
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": amount,
                "resource": URL,
                "description": "A target service.",
                "mimeType": "application/json",
                "payTo": OWNER,
                "asset": USDC,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_v2_header_challenge_normalized():
    async def go():
        def handler(request):
            return _v2_response()

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is True
    assert report["challenge_source"] == "header"
    assert report["x402_version"] == 2
    accept = report["accepts"][0]
    assert accept["amount_atomic"] == "5000"
    assert accept["amount_decimal"] == "0.005"  # exact, no float dust
    assert accept["payTo"] == OWNER
    assert accept["network"] == "eip155:8453"
    assert report["sdk_compat"]["python"]["parses"] is True
    assert report["sdk_compat"]["typescript"]["parses"] is True
    assert report["mismatches"] == []
    assert report["defects"] == []


def test_v2_body_only_challenge():
    async def go():
        def handler(request):
            return httpx.Response(402, json=_v2())

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is True
    assert report["challenge_source"] == "body"
    assert report["accepts"][0]["amount_decimal"] == "0.005"


def test_v1_body_challenge_normalized():
    async def go():
        def handler(request):
            return httpx.Response(402, json=_v1())

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["x402_version"] == 1
    accept = report["accepts"][0]
    assert accept["amount_atomic"] == "5000"  # from maxAmountRequired
    assert accept["amount_decimal"] == "0.005"
    assert accept["resource"] == URL
    assert report["sdk_compat"]["python"]["parses"] is True


def test_non_caip2_network_breaks_sdk_verdict():
    """The losbeto class: a v2 challenge naming a non-CAIP-2 network
    id must fail the compatibility verdict even though the JSON
    shape parses."""

    async def go():
        def handler(request):
            return _v2_response(network="algorand")

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is True
    assert report["sdk_compat"]["python"]["parses"] is False
    failures = " ".join(report["sdk_compat"]["python"]["failures"])
    assert "CAIP-2" in failures
    assert report["sdk_compat"]["typescript"]["parses"] is False


def test_missing_payto_named_and_breaks_verdict():
    async def go():
        challenge = _v2()
        del challenge["accepts"][0]["payTo"]

        def handler(request):
            return _v2_response(challenge)

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    codes = [d["detail"] for d in report["defects"]]
    assert any("missing payTo" in d for d in codes)
    assert report["sdk_compat"]["python"]["parses"] is False


def test_unknown_asset_decimal_is_unknown_not_guessed():
    async def go():
        challenge = _v2()
        challenge["accepts"][0]["asset"] = OTHER

        def handler(request):
            return _v2_response(challenge)

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["accepts"][0]["amount_atomic"] == "5000"
    assert report["accepts"][0]["amount_decimal"] is None


def test_declared_mismatches_named():
    async def go():
        def handler(request):
            return _v2_response()

        return await signread.sign_read(
            _client(handler),
            URL,
            declared={"method": "POST", "amount": "20000", "payTo": OTHER},
        )

    report = run(go())
    fields = {m["field"] for m in report["mismatches"]}
    assert fields == {"amount", "payTo"}
    amount = next(m for m in report["mismatches"] if m["field"] == "amount")
    assert amount["declared"] == "20000"
    assert amount["live"] == "5000"


def test_method_mismatch_when_declared_get_but_answers_post():
    async def go():
        def handler(request):
            if request.method == "GET":
                return httpx.Response(405)
            return _v2_response()

        return await signread.sign_read(
            _client(handler), URL, declared={"method": "GET"}
        )

    report = run(go())
    assert report["challenge_found"] is True
    method_mm = [m for m in report["mismatches"] if m["field"] == "method"]
    assert method_mm and method_mm[0]["declared"] == "GET"
    assert method_mm[0]["live"] == ["POST"]


def test_no_challenge_is_a_report_not_an_error():
    async def go():
        def handler(request):
            return httpx.Response(200, json={"free": True})

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is False
    assert report["http_status"] == 200
    assert report["defects"][0]["code"] == "no_challenge"


def test_unreachable_is_a_report_not_an_error():
    async def go():
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is False
    assert report["defects"][0]["code"] == "unreachable"


def test_unparseable_402_named():
    async def go():
        def handler(request):
            return httpx.Response(402, content=b"pay me somehow")

        return await signread.sign_read(_client(handler), URL)

    report = run(go())
    assert report["challenge_found"] is False
    assert any(d["code"] == "unparseable_challenge" for d in report["defects"])


def test_ssrf_refusal_raises_valueerror():
    async def go():
        return await signread.sign_read(
            _client(lambda r: httpx.Response(402)), "https://127.0.0.1:8080/x"
        )

    with pytest.raises(ValueError):
        run(go())
