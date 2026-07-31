from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
)


ASTER_FUTURES_URL = "https://fapi.asterdex.com"


class AsterFundingClient:
    venue = "aster"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = ASTER_FUTURES_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.06)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        exchange = self.http.get_json(f"{self.base_url}/fapi/v1/exchangeInfo")
        premiums = self.http.get_json(f"{self.base_url}/fapi/v1/premiumIndex")
        tickers = self.http.get_json(f"{self.base_url}/fapi/v1/ticker/24hr")
        warnings: list[str] = []
        try:
            funding_info = self.http.get_json(
                f"{self.base_url}/fapi/v1/fundingInfo"
            )
        except FundingDataError as exc:
            funding_info = []
            warnings.append(f"Aster funding interval overrides unavailable: {exc}")

        premium_map = rows_by_symbol(premiums)
        ticker_map = rows_by_symbol(tickers)
        interval_map = {
            str(row.get("symbol")): max(
                0.25,
                as_float(row.get("fundingIntervalHours"), 8.0),
            )
            for row in funding_info
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(funding_info, list) else {}
        cap_map = {
            str(row.get("symbol")): {
                "funding_rate_cap": as_float(row.get("fundingFeeCap")),
                "funding_rate_floor": as_float(row.get("fundingFeeFloor")),
            }
            for row in funding_info
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(funding_info, list) else {}

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        symbols = exchange.get("symbols", []) if isinstance(exchange, dict) else []
        for raw in symbols:
            if not is_supported_aster_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = clean_asset_symbol(raw.get("baseAsset"))
            current = premium_map.get(symbol)
            if not symbol or not asset or not current:
                continue
            mark_price = as_float(current.get("markPrice"))
            index_price = as_float(current.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue
            interval_hours = interval_map.get(symbol, 8.0)
            caps = cap_map.get(symbol, {})
            ticker = ticker_map.get(symbol, {})
            funding_rate = as_float(current.get("lastFundingRate"))
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://www.asterdex.com/en/futures/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_cap": caps.get("funding_rate_cap"),
                    "funding_rate_floor": caps.get("funding_rate_floor"),
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(
                        current.get("nextFundingTime")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": as_float(ticker.get("quoteVolume")) or None,
                    "maker_fee_rate": 0.0,
                    "taker_fee_rate": 0.0004,
                    "fee_source": "venue_public_tier",
                    "observed_at": observed_at,
                    "raw": {"premium": current, "ticker": ticker},
                }
            )
        return instruments, markets, warnings

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"symbol": symbol, "limit": valid_aster_depth_limit(limit)}
        )
        raw = self.http.get_json(f"{self.base_url}/fapi/v1/depth?{query}")
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Aster orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            raw.get("bids", []),
            raw.get("asks", []),
            observed_at,
            raw,
        )

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol_query = urllib.parse.urlencode({"symbol": symbol})
        premium = self.http.get_json(f"{self.base_url}/fapi/v1/premiumIndex?{symbol_query}")
        if isinstance(premium, list):
            premium = rows_by_symbol(premium).get(symbol, {})
        if not isinstance(premium, dict) or not premium:
            raise FundingDataError(f"Invalid Aster premium index for {symbol}")
        ticker = self.http.get_json(f"{self.base_url}/fapi/v1/ticker/24hr?{symbol_query}")
        if isinstance(ticker, list):
            ticker = rows_by_symbol(ticker).get(symbol, {})
        if not isinstance(ticker, dict):
            ticker = {}
        funding_info = self.http.get_json(f"{self.base_url}/fapi/v1/fundingInfo?{symbol_query}")
        funding_rows = rows_by_symbol(funding_info)
        funding = funding_rows.get(symbol, {})
        previous = previous_market or {}
        interval_hours = max(
            0.25,
            as_float(
                funding.get("fundingIntervalHours"),
                as_float(previous.get("funding_interval_hours"), 8.0),
            ),
        )
        funding_rate = as_float(premium.get("lastFundingRate"))
        mark_price = as_float(premium.get("markPrice"))
        index_price = as_float(premium.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Aster reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_cap": as_float(funding.get("fundingFeeCap")) or None,
            "funding_rate_floor": as_float(funding.get("fundingFeeFloor")) or None,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(
                premium.get("nextFundingTime")
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": None,
            "volume_24h_usd": as_float(ticker.get("quoteVolume")) or None,
            "maker_fee_rate": 0.0,
            "taker_fee_rate": 0.0004,
            "fee_source": "venue_public_tier",
            "contract_multiplier": 1.0,
            "canonical_unit_multiplier": 1.0,
            "observed_at": observed_at,
            "raw": {"premium": premium, "ticker": ticker, "funding_info": funding},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        cursor = int(start_time_ms)
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(10):
            query = urllib.parse.urlencode(
                {"symbol": symbol, "startTime": cursor, "limit": 1_000}
            )
            payload = self.http.get_json(
                f"{self.base_url}/fapi/v1/fundingRate?{query}"
            )
            if not isinstance(payload, list):
                raise FundingDataError(f"Invalid Aster funding history for {symbol}")
            timestamps: list[int] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                timestamp = integer_or_none(raw.get("fundingTime"))
                if timestamp is None:
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or len(payload) < 1_000:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        intervals = inferred_funding_intervals(
            list(rows_by_time),
            interval_hours,
            units_per_second=1_000.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            actual_interval = intervals[timestamp]
            rate = as_float(raw.get("fundingRate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("markPrice")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]


def rows_by_symbol(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list):
        return {}
    return {
        str(row.get("symbol")): row
        for row in payload
        if isinstance(row, dict) and row.get("symbol")
    }


def is_supported_aster_contract(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    subtypes = {
        str(value).upper() for value in raw.get("underlyingSubType", [])
    } if isinstance(raw.get("underlyingSubType"), list) else set()
    return (
        raw.get("status") == "TRADING"
        and raw.get("contractType") == "PERPETUAL"
        and raw.get("quoteAsset") == "USDT"
        and raw.get("marginAsset") == "USDT"
        and "STOCK" not in subtypes
    )


def valid_aster_depth_limit(limit: int) -> int:
    requested = max(5, min(int(limit), 1_000))
    return next(value for value in (5, 10, 20, 50, 100, 500, 1_000) if value >= requested)


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
