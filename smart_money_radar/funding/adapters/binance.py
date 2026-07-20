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


BINANCE_FUTURES_URL = "https://fapi.binance.com"


class BinanceFundingClient:
    venue = "binance"

    def __init__(self, http: FundingHttpClient | None = None) -> None:
        self.http = http or FundingHttpClient()

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        exchange = self.http.get_json(f"{BINANCE_FUTURES_URL}/fapi/v1/exchangeInfo")
        premiums = self.http.get_json(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex")
        warnings: list[str] = []
        try:
            funding_info = self.http.get_json(
                f"{BINANCE_FUTURES_URL}/fapi/v1/fundingInfo"
            )
        except FundingDataError as exc:
            funding_info = []
            warnings.append(f"Binance funding interval overrides unavailable: {exc}")

        premium_map = {
            str(row.get("symbol")): row
            for row in premiums
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(premiums, list) else {}
        interval_map = {
            str(row.get("symbol")): max(1.0, as_float(row.get("fundingIntervalHours"), 8.0))
            for row in funding_info
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(funding_info, list) else {}
        cap_map = {
            str(row.get("symbol")): {
                "funding_rate_cap": as_float(row.get("adjustedFundingRateCap")),
                "funding_rate_floor": as_float(row.get("adjustedFundingRateFloor")),
            }
            for row in funding_info
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(funding_info, list) else {}

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        symbols = exchange.get("symbols", []) if isinstance(exchange, dict) else []
        for raw in symbols:
            if not isinstance(raw, dict):
                continue
            if raw.get("status") != "TRADING" or raw.get("contractType") != "PERPETUAL":
                continue
            if raw.get("quoteAsset") != "USDT" or raw.get("marginAsset") != "USDT":
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("baseAsset"))
            current = premium_map.get(symbol)
            if not symbol or not base_asset or not current:
                continue
            interval_hours = interval_map.get(symbol, 8.0)
            caps = cap_map.get(symbol, {})
            funding_rate = as_float(current.get("lastFundingRate"))
            mark_price = as_float(current.get("markPrice"))
            index_price = as_float(current.get("indexPrice"))
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://www.binance.com/en/futures/{symbol}",
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
                    "funding_rate_cap": caps.get("funding_rate_cap"),
                    "funding_rate_floor": caps.get("funding_rate_floor"),
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(current.get("nextFundingTime")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "observed_at": observed_at,
                    "raw": current,
                }
            )
        return instruments, markets, warnings

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"symbol": symbol, "limit": max(5, min(int(limit), 1000))}
        )
        payload = self.http.get_json(
            f"{BINANCE_FUTURES_URL}/fapi/v1/depth?{query}"
        )
        if not isinstance(payload, dict):
            raise FundingDataError(f"Invalid Binance orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            payload.get("bids", []),
            payload.get("asks", []),
            observed_at,
            payload,
        )

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
                {"symbol": symbol, "startTime": cursor, "limit": 1000}
            )
            payload = self.http.get_json(
                f"{BINANCE_FUTURES_URL}/fapi/v1/fundingRate?{query}"
            )
            if not isinstance(payload, list):
                raise FundingDataError(f"Invalid Binance funding history for {symbol}")
            timestamps = []
            for raw in payload:
                if not isinstance(raw, dict) or raw.get("fundingTime") is None:
                    continue
                try:
                    timestamp = int(raw["fundingTime"])
                except (TypeError, ValueError):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or len(payload) < 1000:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        inferred_intervals = inferred_funding_intervals(
            list(rows_by_time),
            interval_hours,
            units_per_second=1000.0,
        )
        rows = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            rate = as_float(raw.get("fundingRate"))
            actual_interval = inferred_intervals[timestamp]
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
