"""Blockscout (Base) client — v2 API first, legacy Etherscan-style API
as fallback, RPC-only fields degrade to "unknown".

Measured during the build (2026-10-21): base.blockscout.com sits behind
Cloudflare and INTERMITTENTLY answers plain API calls with a "Just a
moment..." challenge page instead of JSON (roughly half the calls from
the build sandbox, fewer with a browser User-Agent). So every method
here returns None on any failure — non-200, challenge HTML, non-JSON,
unexpected shape — and callers mark the affected fields "unknown" or
omit them. Nothing in this service fabricates an explorer field.
"""

from __future__ import annotations

import asyncio

import httpx

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class Blockscout:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self._client = client
        self._base = base_url.rstrip("/")

    async def _get_json(self, url: str, params: dict | None = None):
        try:
            resp = await self._client.get(url, params=params, headers=_UA)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None  # Cloudflare challenge page or other non-JSON

    # --- v2 ------------------------------------------------------------------
    async def _v2(self, path: str, params: dict | None = None):
        return await self._get_json(f"{self._base}/api/v2{path}", params)

    # --- legacy --------------------------------------------------------------
    async def _legacy(self, module: str, action: str, **params):
        body = await self._get_json(
            f"{self._base}/api", {"module": module, "action": action, **params}
        )
        if not isinstance(body, dict) or body.get("status") != "1":
            return None
        return body.get("result")

    # --- public surface --------------------------------------------------------
    async def address_info(self, address: str) -> dict | None:
        """{coin_balance_wei, is_contract} from v2; legacy gives balance
        only. None when both paths fail."""
        body = await self._v2(f"/addresses/{address}")
        if isinstance(body, dict) and "coin_balance" in body:
            try:
                return {
                    "coin_balance_wei": int(body["coin_balance"]),
                    "is_contract": bool(body.get("is_contract")),
                }
            except (TypeError, ValueError):
                pass
        result = await self._legacy("account", "balance", address=address)
        if isinstance(result, str):
            try:
                return {"coin_balance_wei": int(result), "is_contract": None}
            except ValueError:
                return None
        return None

    async def token_balances(self, address: str) -> list[dict] | None:
        """[{address, name, symbol, decimals, balance_raw}] biggest first."""
        body = await self._v2(f"/addresses/{address}/token-balances")
        if isinstance(body, list) and body:
            out = []
            for item in body:
                token = item.get("token") or {}
                try:
                    out.append(
                        {
                            "address": token.get("address_hash") or token.get("address"),
                            "name": token.get("name"),
                            "symbol": token.get("symbol"),
                            "decimals": int(token["decimals"])
                            if token.get("decimals") is not None
                            else None,
                            "balance_raw": str(item.get("value")),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        result = await self._legacy("account", "tokenlist", address=address)
        if isinstance(result, list):
            out = []
            for item in result:
                if not isinstance(item, dict):
                    continue
                try:
                    decimals = int(item.get("tokenDecimal") or 0)
                except ValueError:
                    decimals = None
                out.append(
                    {
                        "address": item.get("contractAddress"),
                        "name": item.get("tokenName"),
                        "symbol": item.get("tokenSymbol"),
                        "decimals": decimals,
                        "balance_raw": str(item.get("balance")),
                    }
                )
            return out
        return None

    async def transactions(
        self, address: str, *, sort: str = "desc", limit: int = 10
    ) -> list[dict] | None:
        """[{hash, timestamp}] newest-first for desc, oldest-first for asc."""
        body = await self._v2(
            f"/addresses/{address}/transactions", {"page_size": limit}
        )
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            items = [
                {"hash": it.get("hash"), "timestamp": it.get("timestamp")}
                for it in body["items"]
                if isinstance(it, dict) and it.get("hash")
            ]
            if sort == "asc":
                items.reverse()
            return items[:limit]
        result = await self._legacy(
            "account",
            "txlist",
            address=address,
            page=1,
            offset=limit,
            sort=sort,
        )
        if isinstance(result, list):
            out = []
            for it in result:
                if not isinstance(it, dict) or not it.get("hash"):
                    continue
                ts = it.get("timeStamp")
                out.append(
                    {
                        "hash": it["hash"],
                        "timestamp_epoch": int(ts) if str(ts).isdigit() else None,
                    }
                )
            return out
        return None

    async def transaction(self, tx_hash: str) -> dict | None:
        """One transaction by hash, normalised toward the RPC shape:
        {from, to, value_wei, status, method, raw_input, block_number,
        gas_used}. v2 first; the legacy gettxinfo action as fallback.
        None when both paths fail (challenged / 404 / bad shape) —
        callers decide what None means alongside their RPC answer.
        The v2 fetch gets two draws: the Cloudflare gate is
        per-request intermittent, so one retry is the difference
        between a degraded answer and none."""
        body = await self._v2(f"/transactions/{tx_hash}")
        if body is None:
            await asyncio.sleep(0.3)
            body = await self._v2(f"/transactions/{tx_hash}")
        if isinstance(body, dict) and body.get("hash"):
            status = {"ok": "success", "error": "reverted"}.get(
                str(body.get("status") or "").lower(), "unknown"
            )
            block = body.get("block_number")
            gas = body.get("gas_used")
            try:
                gas_used = int(gas) if gas is not None else None
            except (TypeError, ValueError):
                gas_used = None
            return {
                "from": (body.get("from") or {}).get("hash"),
                "to": (body.get("to") or {}).get("hash"),
                "value_wei": str(body.get("value"))
                if body.get("value") is not None
                else None,
                "status": status,
                "method": body.get("method"),
                "raw_input": body.get("raw_input"),
                "block_number": block if isinstance(block, int) else None,
                "gas_used": gas_used,
            }
        result = await self._legacy(
            "transaction", "gettxinfo", txhash=tx_hash
        )
        if isinstance(result, dict) and (result.get("from") or result.get("to")):
            raw_value = result.get("value")
            value_wei = None
            if raw_value is not None:
                try:
                    value_wei = str(int(str(raw_value), 0))
                except ValueError:
                    value_wei = str(raw_value)
            gas = result.get("gasUsed")
            try:
                gas_used = int(str(gas), 0) if gas is not None else None
            except ValueError:
                gas_used = None
            return {
                "from": result.get("from"),
                "to": result.get("to"),
                "value_wei": value_wei,
                "status": "unknown",  # gettxinfo carries no status field
                "method": None,
                "raw_input": result.get("input"),
                "block_number": None,
                "gas_used": gas_used,
            }
        return None

    async def transaction_token_transfers(
        self, tx_hash: str
    ) -> list[dict] | None:
        """ERC-20 transfers made BY one transaction:
        [{token, from, to, amount_raw}] (v2 only — the legacy API has
        no per-transaction transfer list). None when the source cannot
        supply the list; an empty list is a real answer (the explorer
        lists the transaction and it made no token transfers). Like
        transaction(), the fetch gets two draws at the intermittent
        Cloudflare gate."""
        body = await self._v2(f"/transactions/{tx_hash}/token-transfers")
        if body is None:
            await asyncio.sleep(0.3)
            body = await self._v2(f"/transactions/{tx_hash}/token-transfers")
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            out = []
            for it in body["items"]:
                if not isinstance(it, dict):
                    continue
                total = it.get("total") or {}
                token = it.get("token") or {}
                out.append(
                    {
                        "token": token.get("address_hash") or token.get("address"),
                        "from": (it.get("from") or {}).get("hash"),
                        "to": (it.get("to") or {}).get("hash"),
                        "amount_raw": str(total.get("value"))
                        if total.get("value") is not None
                        else None,
                    }
                )
            return out
        return None

    async def token_transfers(
        self, address: str, *, limit: int = 25
    ) -> list[dict] | None:
        """Recent ERC-20 transfers touching the address (either direction)."""
        body = await self._v2(
            f"/addresses/{address}/token-transfers", {"page_size": limit}
        )
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            out = []
            for it in body["items"]:
                if not isinstance(it, dict):
                    continue
                total = it.get("total") or {}
                out.append(
                    {
                        "from": (it.get("from") or {}).get("hash"),
                        "to": (it.get("to") or {}).get("hash"),
                        "value_raw": str(total.get("value")),
                        "token_symbol": (it.get("token") or {}).get("symbol"),
                        "tx_hash": it.get("transaction_hash"),
                        "timestamp": it.get("timestamp"),
                    }
                )
            return out
        result = await self._legacy(
            "account", "tokentx", address=address, page=1, offset=limit, sort="desc"
        )
        if isinstance(result, list):
            out = []
            for it in result:
                if not isinstance(it, dict):
                    continue
                out.append(
                    {
                        "from": it.get("from"),
                        "to": it.get("to"),
                        "value_raw": str(it.get("value")),
                        "token_symbol": it.get("tokenSymbol"),
                        "tx_hash": it.get("hash"),
                        "timestamp_epoch": int(it["timeStamp"])
                        if str(it.get("timeStamp")).isdigit()
                        else None,
                    }
                )
            return out
        return None
