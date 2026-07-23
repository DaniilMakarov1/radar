from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import next_interval_boundary_iso, rows_by_key
from smart_money_radar.funding.normalization import clean_asset_symbol, normalize_orderbook


PHEMEX_API_URL = "https://api.phemex.com"


class PhemexFundingClient:
    venue = "phemex"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = PHEMEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._interval_seconds: dict[str, int] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        products = self.http.get_json(f"{self.base_url}/public/products")
        product_rows = phemex_product_rows(products)
        tickers = phemex_ticker_rows(
            self.http.get_json(f"{self.base_url}/md/v2/ticker/24hr/all"),
            "tickers",
        )
        ticker_by_symbol = rows_by_key(tickers, "symbol")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        intervals: dict[str, int] = {}
        for raw in product_rows:
            if not is_supported_phemex_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("baseCurrency") or raw.get("contractUnderlyingAssets"))
            quote_asset = clean_asset_symbol(raw.get("quoteCurrency"))
            ticker = ticker_by_symbol.get(symbol, {})
            if not symbol or not base_asset or not ticker:
                continue
            interval_seconds = max(60, integer_or_none(raw.get("fundingInterval"), 28_800) or 28_800)
            interval_hours = interval_seconds / 3_600.0
            funding_rate = as_float(
                ticker.get("predFundingRateRr"),
                as_float(ticker.get("fundingRateRr")),
            )
            mark_price = as_float(ticker.get("markPriceRp"), as_float(ticker.get("closeRp")))
            index_price = as_float(ticker.get("indexPriceRp"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
            intervals[symbol] = interval_seconds
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "collateral_asset": clean_asset_symbol(raw.get("settleCurrency")) or quote_asset,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://phemex.com/futures/trade/{symbol}",
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
                    "next_funding_at": next_interval_boundary_iso(observed_at, interval_seconds),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("openInterestRv")) * mark_price
                    or None,
                    "volume_24h_usd": as_float(ticker.get("turnoverRv")) or None,
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "ticker": ticker},
                }
            )
        self._interval_seconds = intervals
        return instruments, markets, []

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol})
        ticker = phemex_ticker_object(
            self.http.get_json(f"{self.base_url}/md/v2/ticker/24hr?{query}"),
            f"ticker for {symbol}",
        )
        interval_seconds = self._interval_seconds.get(symbol)
        if interval_seconds is None:
            interval_seconds = int(max(60.0, as_float((previous_market or {}).get("funding_interval_hours"), 8.0) * 3_600.0))
        interval_hours = interval_seconds / 3_600.0
        funding_rate = as_float(
            ticker.get("predFundingRateRr"),
            as_float(ticker.get("fundingRateRr")),
        )
        mark_price = as_float(ticker.get("markPriceRp"), as_float(ticker.get("closeRp")))
        index_price = as_float(ticker.get("indexPriceRp"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Phemex reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": next_interval_boundary_iso(observed_at, interval_seconds),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("openInterestRv")) * mark_price
            or None,
            "volume_24h_usd": as_float(ticker.get("turnoverRv")) or None,
            "observed_at": observed_at,
            "raw": ticker,
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol})
        raw = phemex_ticker_object(
            self.http.get_json(f"{self.base_url}/md/v2/orderbook?{query}"),
            f"orderbook for {symbol}",
        )
        book = raw.get("orderbook_p") if isinstance(raw.get("orderbook_p"), dict) else raw
        if not isinstance(book, dict):
            raise FundingDataError(f"Invalid Phemex orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            book.get("bids", [])[: max(1, int(limit))],
            book.get("asks", [])[: max(1, int(limit))],
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
        return []


def phemex_product_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("code") not in {0, None}:
        raise FundingDataError(f"Phemex products unavailable: {payload}")
    data = payload.get("data")
    rows = data.get("perpProductsV2") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FundingDataError("Invalid Phemex perp products")
    return [row for row in rows if isinstance(row, dict)]


def phemex_ticker_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise FundingDataError(f"Phemex {label} unavailable: {payload}")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Phemex {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def phemex_ticker_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise FundingDataError(f"Phemex {label} unavailable: {payload}")
    row = payload.get("result")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid Phemex {label}")
    return row


def is_supported_phemex_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("type") == "PerpetualV2"
        and raw.get("status") == "Listed"
        and clean_asset_symbol(raw.get("quoteCurrency")) in {"USDT", "USDC"}
    )


def integer_or_none(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
