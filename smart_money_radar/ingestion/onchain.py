from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from smart_money_radar.config import load_env_file
from smart_money_radar.storage import utc_now_iso


BLOCKSCOUT_INSTANCE_URLS = {
    "base": "https://base.blockscout.com",
}
HYPERSYNC_URLS = {
    "base": "https://base.hypersync.xyz",
}
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
BASE_BLOCKS_PER_DAY = 43_200


class OnchainDataError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JsonApiHttp:
    def __init__(
        self,
        timeout_seconds: int = 30,
        min_delay_seconds: float = 0.2,
        max_retries: int = 2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self._request_json("GET", url, headers=headers)

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self._request_json("POST", url, payload=payload, headers=headers)

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "SmartMoneyRadar/0.3",
            **(headers or {}),
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            request = urllib.request.Request(
                url,
                data=data,
                headers=request_headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
                return result
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise OnchainDataError(
                    f"HTTP {exc.code} for {url}: {detail[:240]}",
                    status_code=exc.code,
                ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._last_request_at = time.monotonic()
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise OnchainDataError(f"Request failed for {url}: {exc}") from exc
        raise OnchainDataError(f"Request failed for {url}")


class BlockscoutClient:
    def __init__(self, http: JsonApiHttp | None = None) -> None:
        self.http = http or JsonApiHttp(min_delay_seconds=0.25)

    def token_snapshot(
        self,
        chain_id: str,
        token_address: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        base_url = BLOCKSCOUT_INSTANCE_URLS.get(chain_id)
        if not base_url:
            raise OnchainDataError(f"Blockscout is not configured for {chain_id}")
        address = token_address.lower()
        address_payload = self.http.get_json(f"{base_url}/api/v2/addresses/{address}")
        token_payload: dict[str, Any] = {}
        try:
            result = self.http.get_json(f"{base_url}/api/v2/tokens/{address}")
            if isinstance(result, dict):
                token_payload = result
        except OnchainDataError as exc:
            if exc.status_code != 404:
                raise
        return normalize_blockscout_snapshot(
            chain_id=chain_id,
            token_address=address,
            observed_at=observed_at or utc_now_iso(),
            address_payload=address_payload,
            token_payload=token_payload,
        )


class MoralisClient:
    def __init__(
        self,
        api_key: str | None = None,
        http: JsonApiHttp | None = None,
    ) -> None:
        load_env_file()
        self.api_key = api_key or os.environ.get("MORALIS_API_KEY")
        self.http = http or JsonApiHttp(min_delay_seconds=0.25)

    def token_holder_snapshot(
        self,
        chain_id: str,
        token_address: str,
        observed_at: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise OnchainDataError("MORALIS_API_KEY is missing")
        query = urllib.parse.urlencode(
            {"chain": moralis_chain_name(chain_id), "limit": max(1, min(limit, 100))}
        )
        address = token_address.lower()
        url = (
            "https://deep-index.moralis.io/api/v2.2/erc20/"
            f"{address}/owners?{query}"
        )
        payload = self.http.get_json(url, headers={"X-API-Key": self.api_key})
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise OnchainDataError("Moralis returned an invalid holder response")
        return normalize_moralis_holder_snapshot(
            chain_id=chain_id,
            token_address=address,
            observed_at=observed_at or utc_now_iso(),
            payload=payload,
        )


class HyperSyncClient:
    def __init__(
        self,
        api_token: str | None = None,
        http: JsonApiHttp | None = None,
    ) -> None:
        load_env_file()
        self.api_token = (
            api_token
            or os.environ.get("HYPERSYNC_API_TOKEN")
            or os.environ.get("ENVIO_API_TOKEN")
        )
        self.http = http or JsonApiHttp(min_delay_seconds=0.05)

    def recent_wallet_transfer_snapshot(
        self,
        chain_id: str,
        token_address: str,
        wallet_addresses: list[str],
        observed_at: str | None = None,
        window_days: int = 7,
    ) -> dict[str, Any]:
        if not self.api_token:
            raise OnchainDataError("HYPERSYNC_API_TOKEN is missing")
        base_url = HYPERSYNC_URLS.get(chain_id)
        if not base_url:
            raise OnchainDataError(f"HyperSync is not configured for {chain_id}")
        headers = {"Authorization": f"Bearer {self.api_token}"}
        height_payload = self.http.get_json(f"{base_url}/height", headers=headers)
        height = int(height_payload["height"])
        wallets = sorted(
            {
                address.lower()
                for address in wallet_addresses
                if is_evm_address(address)
            }
        )
        padded_wallets = [pad_evm_topic(address) for address in wallets]
        from_block = max(0, height - max(1, window_days) * BASE_BLOCKS_PER_DAY)
        payload = {
            "from_block": from_block,
            "to_block": height,
            "logs": [
                {
                    "address": [token_address.lower()],
                    "topics": [[TRANSFER_TOPIC], [], padded_wallets],
                },
                {
                    "address": [token_address.lower()],
                    "topics": [[TRANSFER_TOPIC], padded_wallets, []],
                },
            ],
            "field_selection": {
                "block": ["number", "timestamp"],
                "log": [
                    "block_number",
                    "log_index",
                    "transaction_hash",
                    "address",
                    "topic0",
                    "topic1",
                    "topic2",
                ],
            },
        }
        result = self.http.post_json(
            f"{base_url}/query",
            payload,
            headers=headers,
        )
        return normalize_hypersync_snapshot(
            chain_id=chain_id,
            token_address=token_address.lower(),
            observed_at=observed_at or utc_now_iso(),
            tracked_wallets=wallets,
            from_block=from_block,
            height=height,
            payload=result,
        )


def normalize_blockscout_snapshot(
    chain_id: str,
    token_address: str,
    observed_at: str,
    address_payload: dict[str, Any],
    token_payload: dict[str, Any],
) -> dict[str, Any]:
    is_contract = optional_bool(address_payload.get("is_contract"))
    contract_verified = optional_bool(address_payload.get("is_verified"))
    is_scam = optional_bool(address_payload.get("is_scam"))
    reputation = str(
        address_payload.get("reputation") or token_payload.get("reputation") or ""
    ).lower() or None
    proxy_type = address_payload.get("proxy_type")
    implementations = [
        str(item.get("address_hash")).lower()
        for item in address_payload.get("implementations", [])
        if isinstance(item, dict) and item.get("address_hash")
    ]
    flags = []
    if is_contract is False:
        flags.append("blockscout_not_contract")
    if contract_verified is False:
        flags.append("blockscout_unverified_contract")
    if is_scam or reputation == "scam":
        flags.append("blockscout_scam_reputation")
    if proxy_type:
        flags.append("blockscout_proxy_contract")
    return {
        "chain_id": chain_id,
        "token_address": token_address.lower(),
        "observed_at": observed_at,
        "source": "blockscout",
        "contract_verified": contract_verified,
        "is_contract": is_contract,
        "is_scam": is_scam,
        "reputation": reputation,
        "proxy_type": proxy_type,
        "implementation_addresses": implementations,
        "holder_count": optional_int(token_payload.get("holders_count")),
        "flags": flags,
        "raw": {
            "address": address_payload,
            "token": token_payload,
        },
    }


def normalize_moralis_holder_snapshot(
    chain_id: str,
    token_address: str,
    observed_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    holders = [row for row in payload.get("result", [])[:10] if isinstance(row, dict)]
    top10_ratio = sum(holder_percentage(row) for row in holders)
    eoa_ratio = sum(
        holder_percentage(row)
        for row in holders
        if optional_bool(row.get("is_contract")) is not True
    )
    contract_ratio = sum(
        holder_percentage(row)
        for row in holders
        if optional_bool(row.get("is_contract")) is True
    )
    labeled_ratio = sum(
        holder_percentage(row)
        for row in holders
        if row.get("entity") or row.get("owner_address_label")
    )
    flags = []
    if eoa_ratio >= 0.5:
        flags.append("moralis_high_eoa_concentration")
    if top10_ratio >= 0.8:
        flags.append("moralis_extreme_top10_concentration")
    return {
        "chain_id": chain_id,
        "token_address": token_address.lower(),
        "observed_at": observed_at,
        "source": "moralis",
        "top10_holder_ratio": top10_ratio,
        "top10_eoa_holder_ratio": eoa_ratio,
        "top10_contract_holder_ratio": contract_ratio,
        "labeled_holder_ratio": labeled_ratio,
        "flags": flags,
        "raw": payload,
    }


def normalize_hypersync_snapshot(
    chain_id: str,
    token_address: str,
    observed_at: str,
    tracked_wallets: list[str],
    from_block: int,
    height: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    tracked_topics = {pad_evm_topic(address): address for address in tracked_wallets}
    block_times: dict[int, str] = {}
    logs: dict[tuple[Any, ...], dict[str, Any]] = {}
    for batch in payload.get("data", []):
        if not isinstance(batch, dict):
            continue
        for block in batch.get("blocks", []):
            if not isinstance(block, dict) or block.get("number") is None:
                continue
            timestamp = hex_timestamp_to_iso(block.get("timestamp"))
            if timestamp:
                block_times[int(block["number"])] = timestamp
        for log in batch.get("logs", []):
            if not isinstance(log, dict):
                continue
            key = (
                log.get("transaction_hash"),
                log.get("log_index"),
                log.get("block_number"),
            )
            logs[key] = log

    inbound_wallets = set()
    outbound_wallets = set()
    last_block = None
    transaction_hashes = []
    for log in logs.values():
        topic1 = str(log.get("topic1") or "").lower()
        topic2 = str(log.get("topic2") or "").lower()
        if topic2 in tracked_topics:
            inbound_wallets.add(tracked_topics[topic2])
        if topic1 in tracked_topics:
            outbound_wallets.add(tracked_topics[topic1])
        block_number = optional_int(log.get("block_number"))
        if block_number is not None:
            last_block = max(last_block or block_number, block_number)
        transaction_hash = log.get("transaction_hash")
        if transaction_hash and transaction_hash not in transaction_hashes:
            transaction_hashes.append(transaction_hash)

    flags = []
    if not logs:
        flags.append("hypersync_no_tracked_wallet_transfers")
    return {
        "chain_id": chain_id,
        "token_address": token_address.lower(),
        "observed_at": observed_at,
        "source": "hypersync",
        "inbound_wallet_count": len(inbound_wallets),
        "outbound_wallet_count": len(outbound_wallets),
        "transfer_count": len(logs),
        "last_activity_at": block_times.get(last_block) if last_block else None,
        "flags": flags,
        "raw": {
            "from_block": from_block,
            "to_block": height,
            "next_block": payload.get("next_block"),
            "archive_height": payload.get("archive_height"),
            "total_execution_time": payload.get("total_execution_time"),
            "tracked_wallets": tracked_wallets,
            "inbound_wallets": sorted(inbound_wallets),
            "outbound_wallets": sorted(outbound_wallets),
            "recent_transaction_hashes": transaction_hashes[-20:],
        },
    }


def moralis_chain_name(chain_id: str) -> str:
    names = {"base": "base", "ethereum": "eth", "bsc": "bsc"}
    if chain_id not in names:
        raise OnchainDataError(f"Moralis is not configured for {chain_id}")
    return names[chain_id]


def holder_percentage(holder: dict[str, Any]) -> float:
    value = optional_float(holder.get("percentage_relative_to_total_supply")) or 0.0
    return max(0.0, value / 100.0)


def is_evm_address(value: Any) -> bool:
    text = str(value or "")
    if len(text) != 42 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
        return True
    except ValueError:
        return False


def pad_evm_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def hex_timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(str(value), 16) if str(value).startswith("0x") else int(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return None if number is None else int(number)


def optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None
