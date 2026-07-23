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


BLOFIN_API_URL = "https://openapi.blofin.com"


class BloFinFundingClient:
    venue = "blofin"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BLOFIN_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._contract_values: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        instruments_rows = blofin_rows(
            self.http.get_json(
                f"{self.base_url}/api/v1/market/instruments?instType=SWAP"
            ),
            "instruments",
        )
        funding_rows = blofin_rows(
            self.http.get_json(f"{self.base_url}/api/v1/market/funding-rate"),
            "funding rates",
        )
        mark_rows = blofin_rows(
            self.http.get_json(f"{self.base_url}/api/v1/market/mark-price?instType=SWAP"),
            "mark prices",
        )
        ticker_rows = blofin_rows(
            self.http.get_json(f"{self.base_url}/api/v1/market/tickers?instType=SWAP"),
            "tickers",
        )
        funding_by_symbol = rows_by_key(funding_rows, "instId")
        mark_by_symbol = rows_by_key(mark_rows, "instId")
        ticker_by_symbol = rows_by_key(ticker_rows, "instId")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        contract_values: dict[str, float] = {}
        for raw in instruments_rows:
            if not is_supported_blofin_contract(raw):
                continue
            symbol = str(raw.get("instId") or "")
            base_asset = clean_asset_symbol(raw.get("baseCurrency"))
            quote_asset = clean_asset_symbol(raw.get("quoteCurrency"))
            contract_value = as_float(raw.get("contractValue"), 1.0)
            funding = funding_by_symbol.get(symbol, {})
            mark = mark_by_symbol.get(symbol, {})
            ticker = ticker_by_symbol.get(symbol, {})
            if not symbol or not base_asset or not funding or contract_value <= 0:
                continue
            funding_rate = as_float(funding.get("fundingRate"))
            mark_price = as_float(mark.get("markPrice"), as_float(ticker.get("last")))
            index_price = as_float(mark.get("indexPrice"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
            contract_values[symbol] = contract_value
            interval_hours = 8.0
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "collateral_asset": clean_asset_symbol(raw.get("settleCurrency")) or quote_asset,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": contract_value,
                    "status": "active",
                    "source_url": f"https://blofin.com/futures/{symbol}",
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
                    "next_funding_at": iso_from_milliseconds(funding.get("fundingTime")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": as_float(ticker.get("volCurrency24h")) or None,
                    "observed_at": observed_at,
                    "raw": {
                        "instrument": raw,
                        "funding": funding,
                        "mark": mark,
                        "ticker": ticker,
                    },
                }
            )
        self._contract_values = contract_values
        return instruments, markets, []

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"instId": symbol})
        funding = blofin_first_row(
            self.http.get_json(f"{self.base_url}/api/v1/market/funding-rate?{query}"),
            f"funding rate for {symbol}",
        )
        mark = blofin_first_row(
            self.http.get_json(f"{self.base_url}/api/v1/market/mark-price?{query}"),
            f"mark price for {symbol}",
        )
        contract_value = as_float((previous_market or {}).get("contract_multiplier"), 1.0)
        if contract_value > 0:
            self._contract_values[symbol] = contract_value
        rate = as_float(funding.get("fundingRate"))
        mark_price = as_float(mark.get("markPrice"))
        index_price = as_float(mark.get("indexPrice"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"BloFin reference prices unavailable for {symbol}")
        interval_hours = max(0.25, as_float((previous_market or {}).get("funding_interval_hours"), 8.0))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(funding.get("fundingTime")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": None,
            "volume_24h_usd": None,
            "observed_at": observed_at,
            "raw": {"funding": funding, "mark": mark},
            "contract_multiplier": contract_value,
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        contract_value = self._contract_values.get(symbol)
        if contract_value is None:
            query = urllib.parse.urlencode({"instType": "SWAP"})
            rows = blofin_rows(
                self.http.get_json(f"{self.base_url}/api/v1/market/instruments?{query}"),
                "instruments",
            )
            row = next((item for item in rows if item.get("instId") == symbol), {})
            contract_value = as_float(row.get("contractValue"), 1.0)
            self._contract_values[symbol] = contract_value
        query = urllib.parse.urlencode({"instId": symbol, "size": max(1, min(int(limit), 400))})
        rows = blofin_rows(
            self.http.get_json(f"{self.base_url}/api/v1/market/books?{query}"),
            f"orderbook for {symbol}",
        )
        if not rows:
            raise FundingDataError(f"BloFin orderbook for {symbol} returned no rows")
        raw = rows[0]
        return normalize_orderbook(
            self.venue,
            symbol,
            dict_price_size_levels(raw.get("bids"), size_multiplier=contract_value),
            dict_price_size_levels(raw.get("asks"), size_multiplier=contract_value),
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
        query = urllib.parse.urlencode({"instId": symbol, "limit": 100})
        rows = blofin_rows(
            self.http.get_json(
                f"{self.base_url}/api/v1/market/funding-rate-history?{query}"
            ),
            f"funding history for {symbol}",
        )
        rows_by_time: dict[int, dict[str, Any]] = {}
        for raw in rows:
            timestamp = integer_or_none(raw.get("fundingTime"))
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
            rate = as_float(raw.get("fundingRate"))
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


def blofin_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise FundingDataError(f"BloFin {label} unavailable: {payload}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid BloFin {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def blofin_first_row(payload: Any, label: str) -> dict[str, Any]:
    rows = blofin_rows(payload, label)
    if not rows:
        raise FundingDataError(f"BloFin {label} returned no rows")
    return rows[0]


def is_supported_blofin_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("state") == "live"
        and raw.get("instType") == "SWAP"
        and raw.get("contractType") == "linear"
        and clean_asset_symbol(raw.get("quoteCurrency")) in {"USDT", "USDC"}
    )


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
