from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import dict_price_size_levels, rows_by_key
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
)


BITMART_API_URL = "https://api-cloud-v2.bitmart.com"


class BitMartFundingClient:
    venue = "bitmart"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BITMART_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._contract_sizes: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        details = bitmart_rows(
            self.http.get_json(f"{self.base_url}/contract/public/details"),
            "contract details",
        )
        funding_rows = bitmart_rows(
            self.http.get_json(f"{self.base_url}/contract/public/funding-rate-v2"),
            "funding rates",
        )
        funding_by_symbol = rows_by_key(funding_rows, "symbol")
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        contract_sizes: dict[str, float] = {}
        for raw in details:
            if not is_supported_bitmart_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("base_currency"))
            quote_asset = clean_asset_symbol(raw.get("quote_currency"))
            contract_size = as_float(raw.get("contract_size"), 1.0)
            funding = funding_by_symbol.get(symbol, raw)
            if not symbol or not base_asset or contract_size <= 0:
                continue
            interval_hours = max(0.25, as_float(raw.get("funding_interval_hours"), 8.0))
            funding_rate = as_float(
                funding.get("expected_rate"),
                as_float(raw.get("expected_funding_rate"), as_float(raw.get("funding_rate"))),
            )
            mark_price = as_float(raw.get("last_price"))
            index_price = as_float(raw.get("index_price"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
            contract_sizes[symbol] = contract_size
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "collateral_asset": quote_asset,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": contract_size,
                    "status": "active",
                    "source_url": f"https://futures.bitmart.com/en-US?symbol={symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_cap": as_float(funding.get("funding_upper_limit")) or None,
                    "funding_rate_floor": as_float(funding.get("funding_lower_limit")) or None,
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(
                        funding.get("funding_time") or raw.get("funding_time")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(raw.get("open_interest_value")) or None,
                    "volume_24h_usd": as_float(raw.get("turnover_24h")) or None,
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "funding": funding},
                }
            )
        self._contract_sizes = contract_sizes
        return instruments, markets, []

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol})
        detail = bitmart_first_row(
            self.http.get_json(f"{self.base_url}/contract/public/details?{query}"),
            f"contract details for {symbol}",
        )
        funding = bitmart_object(
            self.http.get_json(f"{self.base_url}/contract/public/funding-rate?{query}"),
            f"funding rate for {symbol}",
        )
        contract_size = as_float(
            detail.get("contract_size"),
            as_float((previous_market or {}).get("contract_multiplier"), 1.0),
        )
        if contract_size > 0:
            self._contract_sizes[symbol] = contract_size
        interval_hours = max(0.25, as_float(detail.get("funding_interval_hours"), 8.0))
        rate = as_float(
            funding.get("expected_rate"),
            as_float(detail.get("expected_funding_rate"), as_float(detail.get("funding_rate"))),
        )
        mark_price = as_float(detail.get("last_price"))
        index_price = as_float(detail.get("index_price"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"BitMart reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": rate / interval_hours,
            "funding_rate_cap": as_float(funding.get("funding_upper_limit")) or None,
            "funding_rate_floor": as_float(funding.get("funding_lower_limit")) or None,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(
                funding.get("funding_time") or detail.get("funding_time")
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(detail.get("open_interest_value")) or None,
            "volume_24h_usd": as_float(detail.get("turnover_24h")) or None,
            "observed_at": observed_at,
            "raw": {"contract": detail, "funding": funding},
            "contract_multiplier": contract_size,
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        contract_size = self._contract_sizes.get(symbol)
        if contract_size is None:
            query = urllib.parse.urlencode({"symbol": symbol})
            detail = bitmart_first_row(
                self.http.get_json(f"{self.base_url}/contract/public/details?{query}"),
                f"contract details for {symbol}",
            )
            contract_size = as_float(detail.get("contract_size"), 1.0)
            self._contract_sizes[symbol] = contract_size
        query = urllib.parse.urlencode({"symbol": symbol, "limit": max(5, min(int(limit), 100))})
        raw = bitmart_object(
            self.http.get_json(f"{self.base_url}/contract/public/depth?{query}"),
            f"orderbook for {symbol}",
        )
        return normalize_orderbook(
            self.venue,
            symbol,
            dict_price_size_levels(raw.get("bids"), size_multiplier=contract_size),
            dict_price_size_levels(raw.get("asks"), size_multiplier=contract_size),
            observed_at,
            raw,
        )

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"symbol": symbol, "limit": 100})
        rows = bitmart_rows(
            self.http.get_json(
                f"{self.base_url}/contract/public/funding-rate-history?{query}"
            ),
            f"funding history for {symbol}",
        )
        rows_by_time: dict[int, dict[str, Any]] = {}
        for raw in rows:
            timestamp = integer_or_none(raw.get("funding_time"))
            if timestamp is None or timestamp < start_time_ms:
                continue
            rows_by_time[timestamp] = raw
        inferred = inferred_funding_intervals(
            list(rows_by_time),
            interval_hours,
            units_per_second=1_000.0,
        )
        output: list[dict[str, Any]] = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            actual_interval = inferred.get(timestamp, max(0.25, interval_hours))
            rate = as_float(raw.get("funding_rate"))
            output.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in output if row["funding_at"]]


def bitmart_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or int(payload.get("code") or 0) != 1000:
        raise FundingDataError(f"BitMart {label} unavailable: {payload}")
    data = payload.get("data")
    rows = None
    if isinstance(data, dict):
        rows = data.get("symbols") or data.get("list")
    elif isinstance(data, list):
        rows = data
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid BitMart {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def bitmart_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("code") or 0) != 1000:
        raise FundingDataError(f"BitMart {label} unavailable: {payload}")
    row = payload.get("data")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid BitMart {label}")
    return row


def bitmart_first_row(payload: Any, label: str) -> dict[str, Any]:
    rows = bitmart_rows(payload, label)
    if not rows:
        raise FundingDataError(f"BitMart {label} returned no rows")
    return rows[0]


def is_supported_bitmart_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("status") == "Trading"
        and integer_or_none(raw.get("expire_timestamp"), 0) == 0
        and integer_or_none(raw.get("delist_time"), 0) == 0
        and raw.get("tradfi_info") in (None, "")
        and clean_asset_symbol(raw.get("quote_currency")) in {"USDT", "USDC"}
    )


def integer_or_none(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
