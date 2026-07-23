from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import rows_by_key, success_payload
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
)


COINEX_API_URL = "https://api.coinex.com"


class CoinExFundingClient:
    venue = "coinex"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = COINEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        contracts = coinex_rows(
            self.http.get_json(f"{self.base_url}/v2/futures/market"),
            "markets",
        )
        funding_rows = coinex_rows(
            self.http.get_json(f"{self.base_url}/v2/futures/funding-rate"),
            "funding rates",
        )
        ticker_rows = coinex_rows(
            self.http.get_json(f"{self.base_url}/v2/futures/ticker"),
            "tickers",
        )
        funding_by_market = rows_by_key(funding_rows, "market")
        ticker_by_market = rows_by_key(ticker_rows, "market")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in contracts:
            if not is_supported_coinex_contract(raw):
                continue
            symbol = str(raw.get("market") or "")
            base_asset = clean_asset_symbol(raw.get("base_ccy"))
            quote_asset = clean_asset_symbol(raw.get("quote_ccy"))
            funding = funding_by_market.get(symbol, {})
            ticker = ticker_by_market.get(symbol, {})
            if not symbol or not base_asset or not funding or not ticker:
                continue
            interval_hours = coinex_interval_hours(funding)
            funding_rate = as_float(
                funding.get("next_funding_rate"),
                as_float(funding.get("latest_funding_rate")),
            )
            mark_price = as_float(funding.get("mark_price"), as_float(ticker.get("mark_price")))
            index_price = as_float(ticker.get("index_price"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
            maker_fee = max(0.0, as_float(raw.get("maker_fee_rate"), 0.0003))
            taker_fee = max(0.0, as_float(raw.get("taker_fee_rate"), 0.0005))
            open_interest_usd = (
                as_float(ticker.get("open_interest_volume")) * mark_price or None
            )
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "collateral_asset": quote_asset,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://www.coinex.com/futures/{symbol}",
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
                    "funding_rate_cap": as_float(funding.get("max_funding_rate")) or None,
                    "funding_rate_floor": as_float(funding.get("min_funding_rate")) or None,
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(funding.get("next_funding_time")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": open_interest_usd,
                    "volume_24h_usd": as_float(ticker.get("value")) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_contract_tier",
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "funding": funding, "ticker": ticker},
                }
            )
        return instruments, markets, []

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"market": symbol})
        funding = coinex_first_row(
            self.http.get_json(f"{self.base_url}/v2/futures/funding-rate?{query}"),
            f"funding rate for {symbol}",
        )
        ticker = coinex_first_row(
            self.http.get_json(f"{self.base_url}/v2/futures/ticker?{query}"),
            f"ticker for {symbol}",
        )
        previous = previous_market or {}
        interval_hours = coinex_interval_hours(
            funding,
            as_float(previous.get("funding_interval_hours"), 8.0),
        )
        rate = as_float(
            funding.get("next_funding_rate"),
            as_float(funding.get("latest_funding_rate")),
        )
        mark_price = as_float(funding.get("mark_price"), as_float(ticker.get("mark_price")))
        index_price = as_float(ticker.get("index_price"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"CoinEx reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": rate / interval_hours,
            "funding_rate_cap": as_float(funding.get("max_funding_rate")) or None,
            "funding_rate_floor": as_float(funding.get("min_funding_rate")) or None,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(funding.get("next_funding_time")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("open_interest_volume")) * mark_price
            or None,
            "volume_24h_usd": as_float(ticker.get("value")) or None,
            "observed_at": observed_at,
            "raw": {"funding": funding, "ticker": ticker},
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"market": symbol, "limit": max(5, min(int(limit), 50)), "interval": "0"}
        )
        payload = success_payload(
            self.http.get_json(f"{self.base_url}/v2/futures/depth?{query}"),
            f"CoinEx orderbook for {symbol}",
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw = data.get("depth") if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid CoinEx orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            raw.get("bids", []),
            raw.get("asks", []),
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
        rows_by_time: dict[int, dict[str, Any]] = {}
        for page in range(1, 21):
            query = urllib.parse.urlencode(
                {"market": symbol, "limit": 100, "page": page}
            )
            rows = coinex_rows(
                self.http.get_json(
                    f"{self.base_url}/v2/futures/funding-rate-history?{query}"
                ),
                f"funding history for {symbol}",
            )
            timestamps: list[int] = []
            for raw in rows:
                timestamp = integer_or_none(raw.get("funding_time"))
                if timestamp is None:
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or min(timestamps) < start_time_ms or len(rows) < 100:
                break
        eligible = [timestamp for timestamp in rows_by_time if timestamp >= start_time_ms]
        inferred = inferred_funding_intervals(
            eligible,
            interval_hours,
            units_per_second=1_000.0,
        )
        output: list[dict[str, Any]] = []
        for timestamp in sorted(eligible):
            raw = rows_by_time[timestamp]
            actual_interval = inferred.get(timestamp, max(0.25, interval_hours))
            rate = as_float(
                raw.get("actual_funding_rate"),
                as_float(raw.get("theoretical_funding_rate")),
            )
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


def coinex_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    payload = success_payload(payload, f"CoinEx {label}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid CoinEx {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def coinex_first_row(payload: Any, label: str) -> dict[str, Any]:
    rows = coinex_rows(payload, label)
    if not rows:
        raise FundingDataError(f"CoinEx {label} returned no rows")
    return rows[0]


def is_supported_coinex_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("status") == "online"
        and raw.get("contract_type") == "linear"
        and raw.get("is_market_available") is True
        and raw.get("is_api_trading_available") is True
        and clean_asset_symbol(raw.get("quote_ccy")) in {"USDT", "USDC"}
    )


def coinex_interval_hours(
    funding: dict[str, Any],
    fallback_hours: float = 8.0,
) -> float:
    latest = integer_or_none(funding.get("latest_funding_time"))
    next_time = integer_or_none(funding.get("next_funding_time"))
    if latest is not None and next_time is not None and next_time > latest:
        hours = (next_time - latest) / 3_600_000.0
        if 0.25 <= hours <= 12.0:
            return hours
    return max(0.25, float(fallback_hours or 8.0))


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
