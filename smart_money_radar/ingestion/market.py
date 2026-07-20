from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

from smart_money_radar.storage import utc_now_iso


DEXSCREENER_API_BASE = "https://api.dexscreener.com"
GOPLUS_API_BASE = "https://api.gopluslabs.io/api/v1"

GOPLUS_CHAIN_IDS = {
    "base": "8453",
    "ethereum": "1",
    "bsc": "56",
}


class MarketDataError(RuntimeError):
    pass


class JsonHttpClient:
    def __init__(
        self,
        timeout_seconds: int = 30,
        min_delay_seconds: float = 0.25,
        max_retries: int = 3,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get_json(self, url: str) -> Any:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "SmartMoneyRadar/0.2",
                },
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
                return payload
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                raise MarketDataError(f"HTTP {exc.code} for {url}: {detail[:300]}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._last_request_at = time.monotonic()
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise MarketDataError(f"Request failed for {url}: {exc}") from exc
        raise MarketDataError(f"Request failed for {url}")


class DexScreenerClient:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def token_market_snapshots(
        self,
        chain_id: str,
        token_addresses: list[str],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = observed_at or utc_now_iso()
        addresses = list(dict.fromkeys(address.lower() for address in token_addresses))
        pairs_by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for start in range(0, len(addresses), 30):
            batch = addresses[start : start + 30]
            if not batch:
                continue
            encoded = ",".join(batch)
            url = f"{DEXSCREENER_API_BASE}/tokens/v1/{chain_id}/{encoded}"
            payload = self.http.get_json(url)
            if not isinstance(payload, list):
                continue
            for pair in payload:
                for address in batch:
                    if pair_contains_token(pair, address):
                        pairs_by_token[address].append(pair)

        snapshots = []
        for address in addresses:
            pairs = pairs_by_token.get(address, [])
            if not pairs:
                continue
            pair = max(pairs, key=pair_liquidity_usd)
            token = token_side(pair, address)
            info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
            websites = info.get("websites") if isinstance(info.get("websites"), list) else []
            socials = info.get("socials") if isinstance(info.get("socials"), list) else []
            website_url = next(
                (
                    item.get("url")
                    for item in websites
                    if isinstance(item, dict) and item.get("url")
                ),
                None,
            )
            snapshots.append(
                {
                    "chain_id": chain_id,
                    "token_address": address,
                    "observed_at": timestamp,
                    "source": "dexscreener",
                    "token_name": token.get("name"),
                    "token_symbol": token.get("symbol"),
                    "pair_address": pair.get("pairAddress"),
                    "dex_id": pair.get("dexId"),
                    "price_usd": optional_float(pair.get("priceUsd")),
                    "liquidity_usd": pair_liquidity_usd(pair),
                    "volume_24h_usd": nested_float(pair, "volume", "h24"),
                    "market_cap_usd": optional_float(pair.get("marketCap")),
                    "fdv_usd": optional_float(pair.get("fdv")),
                    "pair_created_at": epoch_ms_to_iso(pair.get("pairCreatedAt")),
                    "website_url": website_url,
                    "social_links": socials,
                    "boosts_active": int(
                        optional_float(
                            (pair.get("boosts") or {}).get("active")
                            if isinstance(pair.get("boosts"), dict)
                            else 0
                        )
                        or 0
                    ),
                    "raw": pair,
                }
            )
        return snapshots

    def token_promotion_snapshot(
        self,
        chain_id: str,
        token_address: str,
    ) -> dict[str, Any]:
        address = token_address.lower()
        url = f"{DEXSCREENER_API_BASE}/orders/v1/{chain_id}/{address}"
        payload = self.http.get_json(url)
        orders = payload if isinstance(payload, list) else []
        active = [
            row
            for row in orders
            if isinstance(row, dict)
            and str(row.get("status") or "").lower() in {"approved", "processing"}
        ]
        order_types = [str(row.get("type") or "") for row in active]
        return {
            "boosts": 0,
            "ads": sum(order_type in {"tokenAd", "trendingBarAd"} for order_type in order_types),
            "profile": int("tokenProfile" in order_types),
            "community_takeover": int("communityTakeover" in order_types),
            "orders": orders,
        }


class GoPlusClient:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient(min_delay_seconds=0.5)

    def token_risk_snapshots(
        self,
        chain_id: str,
        token_addresses: list[str],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        goplus_chain_id = GOPLUS_CHAIN_IDS.get(chain_id)
        if not goplus_chain_id:
            return []
        timestamp = observed_at or utc_now_iso()
        addresses = list(dict.fromkeys(address.lower() for address in token_addresses))
        snapshots = []
        # GoPlus currently returns only the first Base address from a multi-address
        # request, so query each contract explicitly to avoid silent coverage gaps.
        for address in addresses:
            query = urllib.parse.urlencode({"contract_addresses": address})
            url = f"{GOPLUS_API_BASE}/token_security/{goplus_chain_id}?{query}"
            payload = self.http.get_json(url)
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                continue
            normalized_result = {
                str(result_address).lower(): raw
                for result_address, raw in result.items()
            }
            raw = normalized_result.get(address)
            if not isinstance(raw, dict):
                continue
            snapshots.append(normalize_goplus_risk(chain_id, address, timestamp, raw))
        return snapshots


def normalize_goplus_risk(
    chain_id: str,
    token_address: str,
    observed_at: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    is_honeypot = optional_bool(raw.get("is_honeypot"))
    is_open_source = optional_bool(raw.get("is_open_source"))
    is_proxy = optional_bool(raw.get("is_proxy"))
    is_mintable = optional_bool(raw.get("is_mintable"))
    buy_tax = optional_float(raw.get("buy_tax"))
    sell_tax = optional_float(raw.get("sell_tax"))
    holders = raw.get("holders") if isinstance(raw.get("holders"), list) else []
    top_holder_ratio = sum(
        optional_float(holder.get("percent")) or 0
        for holder in holders[:10]
        if isinstance(holder, dict)
    )
    holder_count = optional_int(raw.get("holder_count"))

    flags = []
    risk_score = 0.0
    if is_honeypot:
        flags.append("honeypot")
        risk_score += 100
    if is_open_source is False:
        flags.append("not_open_source")
        risk_score += 25
    if is_proxy:
        flags.append("proxy_contract")
        risk_score += 5
    if is_mintable:
        flags.append("mintable")
        risk_score += 12
    if optional_bool(raw.get("hidden_owner")):
        flags.append("hidden_owner")
        risk_score += 20
    if optional_bool(raw.get("selfdestruct")):
        flags.append("selfdestruct")
        risk_score += 35
    if optional_bool(raw.get("is_blacklisted")):
        flags.append("blacklist_function")
        risk_score += 12
    if optional_bool(raw.get("transfer_pausable")):
        flags.append("transfer_pausable")
        risk_score += 12
    if optional_bool(raw.get("slippage_modifiable")):
        flags.append("slippage_modifiable")
        risk_score += 15
    if optional_bool(raw.get("cannot_sell_all")):
        flags.append("cannot_sell_all")
        risk_score += 40
    if sell_tax is not None and sell_tax > 0.1:
        flags.append("high_sell_tax")
        risk_score += 30
    elif sell_tax is not None and sell_tax > 0.03:
        flags.append("elevated_sell_tax")
        risk_score += 10
    if buy_tax is not None and buy_tax > 0.1:
        flags.append("high_buy_tax")
        risk_score += 20
    if top_holder_ratio > 0.8:
        flags.append("extreme_top10_concentration")
        risk_score += 25
    elif top_holder_ratio > 0.5:
        flags.append("high_top10_concentration")
        risk_score += 12

    return {
        "chain_id": chain_id,
        "token_address": token_address.lower(),
        "observed_at": observed_at,
        "source": "goplus",
        "risk_score": min(100.0, risk_score),
        "is_honeypot": is_honeypot,
        "is_open_source": is_open_source,
        "is_proxy": is_proxy,
        "is_mintable": is_mintable,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "holder_count": holder_count,
        "top_holder_ratio": top_holder_ratio,
        "flags": flags,
        "raw": raw,
    }


def pair_contains_token(pair: dict[str, Any], address: str) -> bool:
    return any(
        str(token.get("address") or "").lower() == address.lower()
        for token in (pair.get("baseToken") or {}, pair.get("quoteToken") or {})
        if isinstance(token, dict)
    )


def token_side(pair: dict[str, Any], address: str) -> dict[str, Any]:
    for key in ("baseToken", "quoteToken"):
        token = pair.get(key)
        if isinstance(token, dict) and str(token.get("address") or "").lower() == address.lower():
            return token
    return {}


def pair_liquidity_usd(pair: dict[str, Any]) -> float:
    return nested_float(pair, "liquidity", "usd") or 0.0


def nested_float(value: dict[str, Any], key: str, child: str) -> float | None:
    nested = value.get(key)
    if not isinstance(nested, dict):
        return None
    return optional_float(nested.get(child))


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


def epoch_ms_to_iso(value: Any) -> str | None:
    number = optional_float(value)
    if number is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(number / 1000, tz=UTC).isoformat()
