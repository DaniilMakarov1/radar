from __future__ import annotations

import urllib.parse
import os
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
)
from smart_money_radar.funding.adapters.common import iso_from_nanoseconds
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    normalize_orderbook,
    parse_timestamp,
)
from smart_money_radar.funding.adapter_contracts import USD_MAJOR_STABLE


RISEX_MAINNET_API_URL = "https://api.rise.trade"
RISEX_TESTNET_API_URL = "https://api.testnet.rise.trade"
RISEX_API_URL = RISEX_MAINNET_API_URL
RISEX_MAKER_FEE_RATE = 0.0001
RISEX_TAKER_FEE_RATE = 0.0003
RISEX_DEFAULT_FUNDING_INTERVAL_HOURS = 1.0
RISEX_NON_CRYPTO_ASSETS = {"XAU", "XAG", "CL", "BZ"}


class RiseXFundingClient:
    venue = "risex"
    non_crypto_assets = RISEX_NON_CRYPTO_ASSETS

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str | None = None,
        environment: str | None = None,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        requested_environment = str(
            environment or os.environ.get("RISEX_ENVIRONMENT") or ""
        ).strip().lower()
        if base_url is None:
            base_url = (
                RISEX_TESTNET_API_URL
                if requested_environment == "testnet"
                else RISEX_MAINNET_API_URL
            )
        self.base_url = base_url.rstrip("/")
        self.environment = risex_environment(environment, self.base_url)
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.base_url,
            requested_environment=self.environment,
        )
        self._markets_by_symbol: dict[str, dict[str, Any]] = {}
        self._markets_by_id: dict[str, dict[str, Any]] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raw_markets = self.fetch_markets(force_refresh=False)
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []

        for raw in raw_markets:
            if not is_supported_risex_market(raw):
                continue
            config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
            symbol = str(config.get("name") or raw.get("display_name") or "")
            market_id = str(raw.get("market_id") or "")
            asset = risex_base_asset(raw)
            if not symbol or not market_id or not asset:
                continue
            interval_hours = risex_interval_hours(raw.get("funding_interval"))
            funding_rate = as_float(raw.get("current_funding_rate"))
            hourly_funding_rate = funding_rate / interval_hours
            published_funding_rate = as_float(
                raw.get("funding_rate_8h"),
                funding_rate * 8.0,
            )
            if market_funding_looks_implausible(funding_rate, interval_hours):
                warnings.append(
                    f"RiseX {symbol} funding rate looked implausible and was skipped."
                )
                continue

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "environment": self.environment,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "contract_kind": "linear_perpetual",
                    "price_quote_currency": "USDC",
                    "settlement_collateral": "USDC",
                    "collateral_family": USD_MAJOR_STABLE,
                    "supports_perpetuals": True,
                    "is_linear_contract": True,
                    "status": "active",
                    "source_url": f"https://rise.trade/market/{market_id}",
                    "observed_at": observed_at,
                    "raw": {"market": raw, "market_id": market_id},
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": hourly_funding_rate,
                    "funding_rate_kind": "published_current_interval_rate",
                    "normalized_next_funding_rate": funding_rate,
                    "funding_rate_semantics": "next_settlement",
                    "funding_rate_unit": "fraction_of_notional_per_settlement",
                    "funding_sign_convention": "positive_long_pays",
                    "published_funding_rate": published_funding_rate,
                    "published_funding_interval_hours": 8.0,
                    "funding_display_note": "published 8h equivalent; cashflow 1h",
                    "next_funding_at": iso_from_nanoseconds(raw.get("next_funding_time")),
                    "mark_price": as_float(raw.get("mark_price")) or None,
                    "index_price": as_float(raw.get("index_price")) or None,
                    "open_interest_usd": risex_open_interest_usd(raw),
                    "volume_24h_usd": as_float(raw.get("quote_volume_24h")) or None,
                    "quantity_step": risex_quantity_step(raw),
                    "min_quantity": risex_min_quantity(raw),
                    "min_notional_usd": risex_min_notional(raw),
                    "maker_fee_rate": RISEX_MAKER_FEE_RATE,
                    "taker_fee_rate": RISEX_TAKER_FEE_RATE,
                    "fee_source": "venue_public_tier1",
                    "funding_rate_cap": 0.04 * interval_hours,
                    "funding_rate_floor": -0.04 * interval_hours,
                    "environment": self.environment,
                    "contract_kind": "linear_perpetual",
                    "price_quote_currency": "USDC",
                    "settlement_collateral": "USDC",
                    "collateral_family": USD_MAJOR_STABLE,
                    "supports_perpetuals": True,
                    "is_linear_contract": True,
                    "supports_discrete_funding": True,
                    "supports_public_shadow_mode": True,
                    "position_inclusion_rule": raw.get("position_inclusion_rule"),
                    "entry_safety_buffer_seconds": raw.get("entry_safety_buffer_seconds"),
                    "exit_safety_buffer_seconds": raw.get("exit_safety_buffer_seconds"),
                    "timing_policy_source": raw.get(
                        "timing_policy_source",
                        "adapter_risex_next_funding_time",
                    ),
                    "realized_history_semantics": raw.get(
                        "realized_history_semantics",
                        "generic_history_unverified",
                    ),
                    "observed_at": observed_at,
                    "raw": {
                        "market": raw,
                        "market_id": market_id,
                        "funding_interval": raw.get("funding_interval"),
                        "funding_rate_8h": raw.get("funding_rate_8h"),
                        "funding_rate_units": (
                            "current_funding_rate is per actual settlement interval; "
                            "funding_rate_8h is audit-only"
                        ),
                    },
                }
            )
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            warnings,
        )

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        market = self.market_for_symbol(symbol)
        market_id = str(market.get("market_id") or "")
        if not market_id:
            raise FundingDataError(f"RiseX market id missing for {symbol}")
        query = urllib.parse.urlencode(
            {
                "market_id": market_id,
                "limit": max(1, min(int(limit), 250)),
            }
        )
        raw = risex_data(
            self.http.get_json(f"{self.base_url}/v1/orderbook?{query}"),
            f"orderbook for {symbol}",
        )
        bids = risex_levels(raw.get("bids"), limit)
        asks = risex_levels(raw.get("asks"), limit)
        return normalize_orderbook(
            self.venue,
            str((market.get("config") or {}).get("name") or symbol),
            bids,
            asks,
            observed_at,
            {"market_id": market_id, "orderbook": raw},
        )

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        market = self.market_for_symbol(symbol)
        raw_symbol = str((market.get("config") or {}).get("name") or symbol)
        asset = risex_base_asset(market) or canonical_asset
        if clean_asset_symbol(asset) != clean_asset_symbol(canonical_asset):
            raise FundingDataError(f"RiseX symbol {symbol} does not match {canonical_asset}")
        interval_hours = risex_interval_hours(market.get("funding_interval"))
        funding_rate = as_float(market.get("current_funding_rate"))
        hourly_funding_rate = funding_rate / interval_hours
        published_funding_rate = as_float(
            market.get("funding_rate_8h"),
            funding_rate * 8.0,
        )
        if market_funding_looks_implausible(funding_rate, interval_hours):
            raise FundingDataError(f"RiseX funding rate looked implausible for {symbol}")
        mark_price = as_float(market.get("mark_price"))
        index_price = as_float(market.get("index_price"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"RiseX reference prices unavailable for {symbol}")
        market_id = str(market.get("market_id") or "")
        if not market_id:
            raise FundingDataError(f"RiseX market id missing for {symbol}")
        previous = previous_market or {}
        row = {
            "venue": self.venue,
            "symbol": raw_symbol,
            "canonical_asset": clean_asset_symbol(asset),
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": hourly_funding_rate,
            "funding_rate_kind": "published_current_interval_rate",
            "normalized_next_funding_rate": funding_rate,
            "funding_rate_semantics": "next_settlement",
            "funding_rate_unit": "fraction_of_notional_per_settlement",
            "funding_sign_convention": "positive_long_pays",
            "published_funding_rate": published_funding_rate,
            "published_funding_interval_hours": 8.0,
            "funding_display_note": "published 8h equivalent; cashflow 1h",
            "next_funding_at": iso_from_nanoseconds(market.get("next_funding_time")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": risex_open_interest_usd(market),
            "volume_24h_usd": as_float(market.get("quote_volume_24h")) or None,
            "quantity_step": risex_quantity_step(market),
            "min_quantity": risex_min_quantity(market),
            "min_notional_usd": risex_min_notional(market),
            "maker_fee_rate": RISEX_MAKER_FEE_RATE,
            "taker_fee_rate": RISEX_TAKER_FEE_RATE,
            "fee_source": "venue_public_tier1",
            "funding_rate_cap": 0.04 * interval_hours,
            "funding_rate_floor": -0.04 * interval_hours,
            "environment": self.environment,
            "contract_kind": previous.get("contract_kind", "linear_perpetual"),
            "price_quote_currency": previous.get("price_quote_currency", "USDC"),
            "settlement_collateral": previous.get("settlement_collateral", "USDC"),
            "collateral_family": previous.get("collateral_family", USD_MAJOR_STABLE),
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "supports_public_shadow_mode": True,
            "position_inclusion_rule": market.get("position_inclusion_rule"),
            "entry_safety_buffer_seconds": market.get("entry_safety_buffer_seconds"),
            "exit_safety_buffer_seconds": market.get("exit_safety_buffer_seconds"),
            "timing_policy_source": market.get(
                "timing_policy_source",
                "adapter_risex_next_funding_time",
            ),
            "realized_history_semantics": market.get(
                "realized_history_semantics",
                "generic_history_unverified",
            ),
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": {
                "market": market,
                "market_id": market_id,
                "funding_interval": market.get("funding_interval"),
                "funding_rate_8h": market.get("funding_rate_8h"),
                "funding_rate_units": (
                    "current_funding_rate is per actual settlement interval; "
                    "funding_rate_8h is audit-only"
                ),
            },
        }
        return apply_endpoint_identity([row], self.endpoint_identity)[0]

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        market = self.market_for_symbol(symbol)
        market_id = str(market.get("market_id") or "")
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        end_time_ns = int(observed.timestamp() * 1_000_000_000)
        start_time_ns = max(0, int(start_time_ms) * 1_000_000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        page = 1
        for _ in range(8):
            query = urllib.parse.urlencode(
                {
                    "start_time": start_time_ns,
                    "end_time": end_time_ns,
                    "page": page,
                    "limit": 1_000,
                }
            )
            payload = risex_data(
                self.http.get_json(
                    f"{self.base_url}/v1/markets/id/{market_id}/"
                    f"funding-rate-history?{query}"
                ),
                f"funding history for {symbol}",
            )
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                break
            for raw in records:
                if not isinstance(raw, dict):
                    continue
                timestamp = risex_history_timestamp(raw)
                if timestamp <= 0 or timestamp > end_time_ns:
                    continue
                rows_by_time[timestamp] = raw
            if not bool(payload.get("has_next_page")) or not records:
                break
            page += 1

        rows: list[dict[str, Any]] = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            actual_interval = risex_history_interval_hours(raw, interval_hours)
            rate = as_float(raw.get("funding_rate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": str((market.get("config") or {}).get("name") or symbol),
                    "funding_at": iso_from_nanoseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("index_price")) or None,
                    "observed_at": observed_at,
                    "raw": {"market_id": market_id, **raw},
                }
            )
        return rows

    def fetch_markets(self, *, force_refresh: bool) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"force_refresh": str(force_refresh).lower()})
        payload = risex_data(
            self.http.get_json(f"{self.base_url}/v1/markets?{query}"),
            "markets",
        )
        rows = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FundingDataError("Invalid RiseX markets response")
        markets = [row for row in rows if isinstance(row, dict)]
        self._markets_by_id = {
            str(row.get("market_id")): row
            for row in markets
            if row.get("market_id") not in {None, ""}
        }
        self._markets_by_symbol = {}
        for row in markets:
            config = row.get("config") if isinstance(row.get("config"), dict) else {}
            for key in (
                config.get("name"),
                row.get("display_name"),
                row.get("display_base_asset_symbol"),
                row.get("base_asset_symbol"),
            ):
                if key:
                    self._markets_by_symbol[str(key).upper()] = row
        return markets

    def market_for_symbol(self, symbol: str) -> dict[str, Any]:
        normalized = str(symbol or "").upper()
        if not self._markets_by_symbol:
            self.fetch_markets(force_refresh=False)
        market = self._markets_by_symbol.get(normalized) or self._markets_by_id.get(
            normalized
        )
        if market is None:
            asset = risex_symbol_to_asset(normalized)
            for row in self._markets_by_symbol.values():
                if risex_base_asset(row) == asset and is_supported_risex_market(row):
                    return row
        if market is None or not is_supported_risex_market(market):
            raise FundingDataError(f"RiseX symbol not found: {symbol}")
        return market


def risex_data(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FundingDataError(f"Invalid RiseX {label} response")
    if "error" in payload:
        raise FundingDataError(f"RiseX {label} unavailable: {payload.get('error')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FundingDataError(f"Invalid RiseX {label} payload")
    return data


def is_supported_risex_market(row: dict[str, Any]) -> bool:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    name = str(config.get("name") or row.get("display_name") or "")
    if not bool(row.get("active", True)):
        return False
    if not bool(config.get("unlocked")):
        return False
    if bool(row.get("post_only")):
        return False
    if "deprecated" in name.lower():
        return False
    if risex_interval_hours(row.get("funding_interval")) <= 0:
        return False
    return bool(risex_base_asset(row))


def risex_base_asset(row: dict[str, Any]) -> str:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    for value in (
        row.get("display_base_asset_symbol"),
        row.get("base_asset_symbol"),
        row.get("underlying"),
        config.get("name"),
        row.get("display_name"),
    ):
        asset = risex_symbol_to_asset(value)
        if asset:
            return asset
    return ""


def risex_symbol_to_asset(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.split("[", 1)[0].strip()
    for separator in ("/", "-", "_"):
        if separator in text:
            text = text.split(separator, 1)[0]
            break
    return clean_asset_symbol(text.replace("PERP", ""))


def risex_interval_hours(value: Any) -> float:
    try:
        seconds = float(value) / 1_000_000_000.0
    except (TypeError, ValueError):
        return RISEX_DEFAULT_FUNDING_INTERVAL_HOURS
    if seconds <= 0:
        return RISEX_DEFAULT_FUNDING_INTERVAL_HOURS
    return seconds / 3600.0


def risex_environment(value: str | None, base_url: str) -> str:
    configured = str(value or os.environ.get("RISEX_ENVIRONMENT") or "").strip().lower()
    base = str(base_url or "").lower()
    base_is_testnet = "testnet" in base
    if configured in {"mainnet", "testnet"}:
        if configured == "mainnet" and base_is_testnet:
            raise FundingDataError("RiseX mainnet cannot use a testnet base_url")
        if configured == "testnet" and base and not base_is_testnet:
            raise FundingDataError("RiseX testnet requires an explicit testnet base_url")
        return configured
    return "testnet" if base_is_testnet else "mainnet"


def risex_quantity_step(row: dict[str, Any]) -> float | None:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    for key in ("quantity_step", "min_order_increment", "order_size_increment", "lot_size"):
        value = as_float(row.get(key), as_float(config.get(key)))
        if value > 0:
            return value
    return None


def risex_min_quantity(row: dict[str, Any]) -> float | None:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    for key in ("min_quantity", "min_order_size", "minimum_order_size"):
        value = as_float(row.get(key), as_float(config.get(key)))
        if value > 0:
            return value
    return None


def risex_min_notional(row: dict[str, Any]) -> float | None:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    for key in ("min_notional", "min_notional_usd", "minimum_notional"):
        value = as_float(row.get(key), as_float(config.get(key)))
        if value > 0:
            return value
    return None


def market_funding_looks_implausible(
    funding_rate: float,
    interval_hours: float,
) -> bool:
    if interval_hours <= 0:
        return True
    return abs(funding_rate / interval_hours) > 0.04 + 1e-12


def risex_open_interest_usd(row: dict[str, Any]) -> float | None:
    open_interest = as_float(row.get("open_interest"))
    index_price = as_float(row.get("index_price"), as_float(row.get("mark_price")))
    if open_interest > 0 and index_price > 0:
        return open_interest * index_price
    return None


def risex_levels(raw_levels: Any, limit: int) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(raw_levels, list):
        return output
    for raw in raw_levels:
        if not isinstance(raw, dict):
            continue
        price = as_float(raw.get("price"))
        quantity = as_float(raw.get("quantity"))
        if price > 0 and quantity > 0:
            output.append([price, quantity])
    return output[: max(1, min(int(limit), 250))]


def risex_history_timestamp(raw: dict[str, Any]) -> int:
    for key in ("block_time", "end_time", "start_time"):
        try:
            timestamp = int(raw.get(key))
        except (TypeError, ValueError):
            continue
        if timestamp > 0:
            return timestamp
    return 0


def risex_history_interval_hours(raw: dict[str, Any], fallback: float) -> float:
    try:
        start = int(raw.get("start_time"))
        end = int(raw.get("end_time"))
    except (TypeError, ValueError):
        return max(float(fallback or RISEX_DEFAULT_FUNDING_INTERVAL_HOURS), 1e-9)
    if end <= start:
        return max(float(fallback or RISEX_DEFAULT_FUNDING_INTERVAL_HOURS), 1e-9)
    return max((end - start) / 1_000_000_000.0 / 3600.0, 1e-9)
