"""Minimal Base JSON-RPC client over an injectable httpx client.

Every service takes an httpx.AsyncClient so tests can play the RPC (and
every other upstream) through httpx.MockTransport — the suite runs with
zero network. RPC failures raise UpstreamError; handlers turn that into
a 502, and the x402 middleware does not settle payment on >= 400
responses ("Don't settle on error responses" in the SDK middleware), so
a failed call never charges the buyer for undelivered data.
"""

from __future__ import annotations

import itertools

import httpx


class UpstreamError(Exception):
    """An upstream (RPC / API / page) failed or returned unusable data."""


class Rpc:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self._client = client
        self._url = url
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list) -> object:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as e:
            raise UpstreamError(f"RPC {method}: transport error: {e}") from e
        if resp.status_code != 200:
            raise UpstreamError(f"RPC {method}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise UpstreamError(f"RPC {method}: non-JSON response") from e
        if not isinstance(body, dict) or "error" in body:
            err = body.get("error") if isinstance(body, dict) else body
            raise UpstreamError(f"RPC {method}: error: {err}")
        if "result" not in body:
            raise UpstreamError(f"RPC {method}: missing result")
        return body["result"]

    # --- typed helpers -------------------------------------------------------
    async def eth_balance(self, address: str) -> int:
        return int(await self.call("eth_getBalance", [address, "latest"]), 16)

    async def eth_tx_count(self, address: str) -> int:
        return int(
            await self.call("eth_getTransactionCount", [address, "latest"]), 16
        )

    async def eth_gas_price(self) -> int:
        return int(await self.call("eth_gasPrice", []), 16)

    async def eth_block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def eth_block(self, number: int, full: bool = False) -> dict:
        block = await self.call("eth_getBlockByNumber", [hex(number), full])
        if not isinstance(block, dict):
            raise UpstreamError("RPC eth_getBlockByNumber: unexpected shape")
        return block

    async def eth_tx(self, tx_hash: str) -> dict:
        tx = await self.call("eth_getTransactionByHash", [tx_hash])
        if not isinstance(tx, dict):
            raise UpstreamError(f"transaction not found: {tx_hash}")
        return tx

    async def eth_receipt(self, tx_hash: str) -> dict:
        receipt = await self.call("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            raise UpstreamError(f"receipt not found (pending?): {tx_hash}")
        return receipt

    async def eth_call(self, to: str, data: str) -> str:
        result = await self.call("eth_call", [{"to": to, "data": data}, "latest"])
        if not isinstance(result, str):
            raise UpstreamError("RPC eth_call: unexpected shape")
        return result


def wei_to_eth_str(wei: int) -> str:
    """Exact decimal string for a wei amount (no float rounding)."""
    eth = wei // 10**18
    frac = wei % 10**18
    if frac == 0:
        return str(eth)
    return f"{eth}.{frac:018d}".rstrip("0")


def gwei_str(wei: int) -> str:
    """Human gwei string with up to 4 decimals, exact integer math."""
    whole = wei // 10**9
    frac = (wei % 10**9) // 10**5
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:04d}".rstrip("0")


def is_address(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("0x")
        and len(value) == 42
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )


def is_tx_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("0x")
        and len(value) == 66
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )
