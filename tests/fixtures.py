"""Offline fixtures: one MockTransport plays every upstream (Base RPC,
Blockscout v2 + legacy, GitHub, YouTube, CDP catalog) so the whole
suite runs with zero network.

Blockscout note: production Blockscout intermittently serves Cloudflare
challenges; `blockscout_down=True` simulates exactly that (HTML body),
and services must degrade — never fabricate.
"""

import json

import httpx

ADDR = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
OTHER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
TX_HASH = "0x" + "ab" * 32

CHALLENGE_HTML = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


def _rpc_result(method: str, params: list):
    if method == "eth_getBalance":
        return hex(42 * 10**15)
    if method == "eth_getTransactionCount":
        return hex(137)
    if method == "eth_gasPrice":
        return hex(6_200_000)  # 0.0062 gwei in wei
    if method == "eth_blockNumber":
        return hex(1_000)
    if method == "eth_getBlockByNumber":
        n = int(params[0], 16)
        # base fee rises 0.0001 gwei per block across the window
        return {"number": hex(n), "baseFeePerGas": hex(5_000_000 + (n % 20) * 100_000)}
    if method == "eth_getTransactionByHash":
        return {
            "hash": TX_HASH,
            "blockNumber": hex(995),
            "from": OTHER,
            "to": USDC,
            "value": "0x0",
            "input": "0xa9059cbb" + "0" * 64,
        }
    if method == "eth_getTransactionReceipt":
        return {
            "status": "0x1",
            "gasUsed": hex(51604),
            "logs": [
                {
                    "address": USDC,
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        "0x" + "0" * 24 + OTHER[2:],
                        "0x" + "0" * 24 + ADDR[2:],
                    ],
                    "data": hex(12_500_000),
                }
            ],
        }
    if method == "eth_call":
        data = params[0]["data"]
        spender = "0x" + data[-40:]
        if spender.lower() == PERMIT2.lower():
            return hex((1 << 256) - 1)  # max uint256 → unlimited
        return hex(0)
    raise AssertionError(f"unmocked RPC method {method}")


def _blockscout_v2(path: str):
    if path.endswith("/token-balances"):
        return [
            {
                "token": {"address_hash": USDC, "name": "USD Coin", "symbol": "USDC", "decimals": "6"},
                "value": "12500000",
            }
        ]
    if path.endswith("/token-transfers"):
        return {
            "items": [
                {
                    "from": {"hash": OTHER},
                    "to": {"hash": ADDR},
                    "total": {"value": "12500000"},
                    "token": {"symbol": "USDC"},
                    "transaction_hash": TX_HASH,
                    "timestamp": "2026-10-20T09:41:52.000000Z",
                }
            ]
        }
    if path.endswith("/transactions"):
        return {
            "items": [
                {"hash": TX_HASH, "timestamp": "2025-11-02T14:03:11.000000Z"}
            ]
        }
    if "/addresses/" in path:
        return {"coin_balance": str(42 * 10**15), "is_contract": False}
    return None


YOUTUBE_HTML = (
    "<html><script>var ytInitialData = "
    + json.dumps(
        {
            "contents": {
                "sections": [
                    {
                        "itemSectionRenderer": {
                            "contents": [
                                {
                                    "videoRenderer": {
                                        "videoId": "dQw4w9WgXcQ",
                                        "title": {"runs": [{"text": "x402 explained"}]},
                                        "ownerText": {"runs": [{"text": "Example Channel"}]},
                                        "lengthText": {"simpleText": "10:24"},
                                        "viewCountText": {"simpleText": "12K views"},
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }
    )
    + ";</script></html>"
)

PAGE_HTML = (
    "<html><head><title>Example Story</title></head><body>"
    "<nav>Home About Contact</nav><article><h1>Example Story</h1>"
    + "<p>This is the main content of the example page, written to be long "
      "enough for extraction heuristics to keep it.</p>" * 6
    + "</article><footer>copyright</footer></body></html>"
)

GITHUB_REPO = {
    "html_url": "https://github.com/QuincyGB/token-verdict",
    "description": "x402 token-risk verdict service",
    "stargazers_count": 3,
    "forks_count": 0,
    "open_issues_count": 1,
    "license": {"spdx_id": "MIT"},
    "pushed_at": "2026-10-21T19:11:00Z",
    "created_at": "2026-10-07T00:00:00Z",
    "archived": False,
}

CDP_CATALOG = {
    "items": [
        {
            "resource": "https://a.example.com/v1/verdict",
            "description": "Token risk verdict for contracts",
            "accepts": [{"scheme": "exact", "amount": "20000", "network": "eip155:8453"}],
            "tags": ["verdict"],
        },
        {
            "resource": "https://b.example.com/v1/check",
            "description": "Token safety check and verdict API",
            "accepts": [{"scheme": "exact", "amount": "50000", "network": "eip155:8453"}],
            "tags": [],
        },
        {
            "resource": "https://c.example.com/v1/weather",
            "description": "Weather data",
            "accepts": [{"scheme": "exact", "amount": "10000", "network": "eip155:8453"}],
            "tags": [],
        },
    ],
    "pagination": {"total": 3},
}


def make_handler(*, blockscout_down: bool = False, github_404: bool = False,
                 cdp_down: bool = False, youtube_empty: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host
        if host == "mainnet.base.org":
            body = json.loads(request.content)
            result = _rpc_result(body["method"], body.get("params", []))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        if host == "base.blockscout.com":
            if blockscout_down:
                return httpx.Response(200, text=CHALLENGE_HTML, headers={"content-type": "text/html"})
            if "/api/v2/" in url:
                data = _blockscout_v2(request.url.path)
                if data is None:
                    return httpx.Response(404, json={"message": "not found"})
                return httpx.Response(200, json=data)
            return httpx.Response(200, json={"status": "0", "message": "not mocked", "result": None})
        if host == "api.github.com":
            if github_404:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=GITHUB_REPO)
        if host == "www.youtube.com":
            return httpx.Response(200, text="<html>no data here</html>" if youtube_empty else YOUTUBE_HTML)
        if host == "api.cdp.coinbase.com":
            if cdp_down:
                return httpx.Response(503, text="down")
            return httpx.Response(200, json=CDP_CATALOG)
        raise AssertionError(f"unmocked URL {url}")

    return handler


def mock_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(make_handler(**kwargs)))
