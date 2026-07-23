from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


EVM_RPC_URLS = {
    "base": "https://mainnet.base.org",
    "bsc": "https://bsc-dataseed.bnbchain.org",
    "ethereum": "https://ethereum-rpc.publicnode.com",
}
ERC20_SYMBOL_SELECTOR = "0x95d89b41"
ERC20_NAME_SELECTOR = "0x06fdde03"
ERC20_DECIMALS_SELECTOR = "0x313ce567"
PAIR_TOKEN0_SELECTOR = "0x0dfe1681"
PAIR_TOKEN1_SELECTOR = "0xd21220a7"


class EvmRpcError(RuntimeError):
    pass


class EvmRpcClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        chain_id: str = "base",
        timeout_seconds: int = 30,
        min_delay_seconds: float = 0.5,
        max_retries: int = 3,
    ) -> None:
        normalized_chain = chain_id.lower()
        env_name = f"{normalized_chain.upper()}_RPC_URL"
        default_url = EVM_RPC_URLS.get(normalized_chain)
        if not rpc_url and not os.environ.get(env_name) and not default_url:
            raise ValueError(f"No RPC endpoint configured for {chain_id}")
        self.chain_id = normalized_chain
        self.rpc_url = rpc_url or os.environ.get(env_name) or str(default_url)
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._request_id = 0

    def erc20_symbol(self, contract_address: str) -> str:
        result = self.call(
            "eth_call",
            [{"to": contract_address, "data": ERC20_SYMBOL_SELECTOR}, "latest"],
        )
        symbol = decode_abi_string(result)
        if not symbol:
            raise EvmRpcError(f"Empty ERC-20 symbol for {contract_address}")
        return symbol

    def erc20_name(self, contract_address: str) -> str:
        result = self.call(
            "eth_call",
            [{"to": contract_address, "data": ERC20_NAME_SELECTOR}, "latest"],
        )
        name = decode_abi_string(result)
        if not name:
            raise EvmRpcError(f"Empty ERC-20 name for {contract_address}")
        return name

    def erc20_decimals(self, contract_address: str) -> int:
        result = self.call(
            "eth_call",
            [{"to": contract_address, "data": ERC20_DECIMALS_SELECTOR}, "latest"],
        )
        decimals = decode_abi_uint(result)
        if decimals is None or decimals < 0 or decimals > 255:
            raise EvmRpcError(f"Invalid ERC-20 decimals for {contract_address}")
        return decimals

    def pair_tokens(self, pair_address: str) -> tuple[str, str]:
        token0 = self.contract_address(pair_address, PAIR_TOKEN0_SELECTOR)
        token1 = self.contract_address(pair_address, PAIR_TOKEN1_SELECTOR)
        if not token0 or not token1:
            raise EvmRpcError(f"Invalid pair tokens for {pair_address}")
        return token0, token1

    def contract_address(self, contract_address: str, selector: str) -> str | None:
        result = self.call(
            "eth_call",
            [{"to": contract_address, "data": selector}, "latest"],
        )
        return decode_abi_address(result)

    def call(self, method: str, params: list[Any]) -> Any:
        payload = None
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            self._request_id += 1
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                self.rpc_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "SmartMoneyRadar/0.2",
                },
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
            finally:
                self._last_request_at = time.monotonic()
            time.sleep(1.5 * (attempt + 1))
        if payload is None:
            raise EvmRpcError(
                f"{self.chain_id} RPC request failed: {last_error}"
            ) from last_error
        if payload.get("error"):
            raise EvmRpcError(f"{self.chain_id} RPC error: {payload['error']}")
        if "result" not in payload:
            raise EvmRpcError(
                f"{self.chain_id} RPC response has no result: {payload}"
            )
        return payload["result"]


def decode_abi_string(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        return ""
    try:
        data = bytes.fromhex(value[2:])
    except ValueError:
        return ""
    if not data:
        return ""

    payload = data
    if len(data) >= 64:
        offset = int.from_bytes(data[:32], "big")
        if 0 <= offset <= len(data) - 32:
            length = int.from_bytes(data[offset : offset + 32], "big")
            start = offset + 32
            end = start + length
            if 0 <= length and end <= len(data):
                payload = data[start:end]
    return payload.rstrip(b"\x00").decode("utf-8", errors="replace").strip()


def decode_abi_uint(value: Any) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(value[2:])
    except ValueError:
        return None
    if not data:
        return None
    return int.from_bytes(data[-32:], "big")


def decode_abi_address(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(value[2:])
    except ValueError:
        return None
    if len(data) < 32:
        return None
    address = "0x" + data[-20:].hex()
    if address == "0x" + "0" * 40:
        return None
    return address


def normalize_token_symbol(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())
