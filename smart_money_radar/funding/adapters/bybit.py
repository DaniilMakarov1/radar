from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
    parse_timestamp,
)


BYBIT_API_URL = "https://api.bybit.com"


class BybitFundingClient:
    venue = "bybit"

    def __init__(self, http: FundingHttpClient | None = None) -> None:
        self.http = http or FundingHttpClient()
        self.base_url = BYBIT_API_URL
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.base_url,
            requested_environment="mainnet",
        )
        self.non_crypto_assets: set[str] = set()

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        instruments_raw = self._instrument_pages()
        tickers = bybit_result_list(
            self.http.get_json(f"{BYBIT_API_URL}/v5/market/tickers?category=linear"),
            "tickers",
        )
        ticker_map = {
            str(row.get("symbol")): row
            for row in tickers
            if isinstance(row, dict) and row.get("symbol")
        }
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        non_crypto_assets: set[str] = set()
        for raw in instruments_raw:
            if not isinstance(raw, dict):
                continue
            if raw.get("status") != "Trading" or raw.get("contractType") != "LinearPerpetual":
                continue
            if raw.get("quoteCoin") != "USDT" or raw.get("settleCoin") != "USDT":
                continue
            if raw.get("isPreListing"):
                continue
            if str(raw.get("symbolType") or "").strip().lower() == "stock":
                asset = clean_asset_symbol(raw.get("baseCoin"))
                if asset:
                    non_crypto_assets.add(asset)
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("baseCoin"))
            ticker = ticker_map.get(symbol)
            if not symbol or not base_asset or not ticker:
                continue
            instrument_interval_hours = max(
                1.0,
                as_float(raw.get("fundingInterval"), 480.0) / 60.0,
            )
            interval_hours = max(
                1.0,
                as_float(
                    ticker.get("fundingIntervalHour"),
                    instrument_interval_hours,
                ),
            )
            funding_rate = as_float(ticker.get("fundingRate"))
            mark_price = as_float(ticker.get("markPrice"))
            index_price = as_float(ticker.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue
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
                    "source_url": f"https://www.bybit.com/trade/usdt/{symbol}",
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
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(ticker.get("nextFundingTime")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("openInterestValue")) or None,
                    "volume_24h_usd": as_float(ticker.get("turnover24h")) or None,
                    "taker_fee_rate": 0.00055,
                    "observed_at": observed_at,
                    "raw": {"instrument": raw, "ticker": ticker},
                }
            )
        self.non_crypto_assets = non_crypto_assets
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            [],
        )

    def _instrument_pages(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(20):
            query = {"category": "linear", "limit": 1000}
            if cursor:
                query["cursor"] = cursor
            payload = self.http.get_json(
                f"{BYBIT_API_URL}/v5/market/instruments-info?{urllib.parse.urlencode(query)}"
            )
            result = bybit_result(payload, "instruments")
            page = result.get("list", [])
            rows.extend(row for row in page if isinstance(row, dict))
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "category": "linear",
                "symbol": symbol,
                "limit": max(1, min(int(limit), 1000)),
            }
        )
        result = bybit_result(
            self.http.get_json(f"{BYBIT_API_URL}/v5/market/orderbook?{query}"),
            f"orderbook for {symbol}",
        )
        return normalize_orderbook(
            self.venue,
            symbol,
            result.get("b", []),
            result.get("a", []),
            observed_at,
            result,
        )

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"category": "linear", "symbol": symbol})
        rows = bybit_result_list(
            self.http.get_json(f"{BYBIT_API_URL}/v5/market/tickers?{query}"),
            f"ticker for {symbol}",
        )
        ticker = rows[0] if rows else None
        if not isinstance(ticker, dict):
            raise FundingDataError(f"Bybit ticker unavailable for {symbol}")
        previous = previous_market or {}
        interval_hours = max(
            1.0,
            as_float(
                ticker.get("fundingIntervalHour"),
                as_float(previous.get("funding_interval_hours"), 8.0),
            ),
        )
        funding_rate = as_float(ticker.get("fundingRate"))
        mark_price = as_float(ticker.get("markPrice"))
        index_price = as_float(ticker.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Bybit reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(ticker.get("nextFundingTime")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("openInterestValue")) or None,
            "volume_24h_usd": as_float(ticker.get("turnover24h")) or None,
            "maker_fee_rate": 0.0002,
            "taker_fee_rate": 0.00055,
            "fee_source": "venue_public_contract_tier",
            "observed_at": observed_at,
            "raw": {"ticker": ticker},
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get(
                "canonical_unit_multiplier",
                1.0,
            ),
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        end_time_ms = int(observed.timestamp() * 1000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(20):
            query = urllib.parse.urlencode(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": int(start_time_ms),
                    "endTime": end_time_ms,
                    "limit": 200,
                }
            )
            page = bybit_result_list(
                self.http.get_json(f"{BYBIT_API_URL}/v5/market/funding/history?{query}"),
                f"funding history for {symbol}",
            )
            timestamps = []
            for raw in page:
                if not isinstance(raw, dict) or raw.get("fundingRateTimestamp") is None:
                    continue
                try:
                    timestamp = int(raw["fundingRateTimestamp"])
                except (TypeError, ValueError):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or len(page) < 200:
                break
            oldest = min(timestamps)
            if oldest <= start_time_ms or oldest >= end_time_ms:
                break
            end_time_ms = oldest - 1
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
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]


def bybit_result(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        detail = payload.get("retMsg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Bybit {label} unavailable: {detail}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise FundingDataError(f"Invalid Bybit {label} response")
    return result


def bybit_result_list(payload: Any, label: str) -> list[dict[str, Any]]:
    result = bybit_result(payload, label)
    rows = result.get("list", [])
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Bybit {label} rows")
    return rows
