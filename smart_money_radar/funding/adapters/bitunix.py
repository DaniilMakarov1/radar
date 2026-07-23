from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import rows_by_key
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


BITUNIX_API_URL = "https://fapi.bitunix.com"


class BitunixFundingClient:
    venue = "bitunix"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BITUNIX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        pairs = bitunix_rows(
            self.http.get_json(f"{self.base_url}/api/v1/futures/market/trading_pairs"),
            "trading pairs",
        )
        tickers = bitunix_rows(
            self.http.get_json(f"{self.base_url}/api/v1/futures/market/tickers"),
            "tickers",
        )
        funding_rows = bitunix_rows(
            self.http.get_json(
                f"{self.base_url}/api/v1/futures/market/funding_rate/batch"
            ),
            "funding rates",
        )
        ticker_by_symbol = rows_by_key(tickers, "symbol")
        funding_by_symbol = rows_by_key(funding_rows, "symbol")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in pairs:
            if not is_supported_bitunix_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("base"))
            quote_asset = clean_asset_symbol(raw.get("quote"))
            ticker = ticker_by_symbol.get(symbol, {})
            funding = funding_by_symbol.get(symbol, {})
            if not symbol or not base_asset or not funding:
                continue
            interval_hours = max(0.25, as_float(funding.get("fundingInterval"), 8.0))
            funding_rate = bitunix_decimal_rate(funding.get("fundingRate"))
            mark_price = as_float(funding.get("markPrice"), as_float(ticker.get("markPrice")))
            index_price = as_float(funding.get("indexPrice"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
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
                    "source_url": f"https://www.bitunix.com/futures/{symbol}",
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
                    "funding_rate_cap": bitunix_decimal_rate(funding.get("maxFundingRate")),
                    "funding_rate_floor": bitunix_decimal_rate(funding.get("minFundingRate")),
                    "funding_rate_kind": "published_next_estimate_percent_api",
                    "next_funding_at": iso_from_milliseconds(funding.get("nextFundingTime")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": as_float(ticker.get("quoteVol")) or None,
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
        query = urllib.parse.urlencode({"symbol": symbol})
        funding = bitunix_object(
            self.http.get_json(
                f"{self.base_url}/api/v1/futures/market/funding_rate?{query}"
            ),
            f"funding rate for {symbol}",
        )
        interval_hours = max(
            0.25,
            as_float(
                funding.get("fundingInterval"),
                as_float((previous_market or {}).get("funding_interval_hours"), 8.0),
            ),
        )
        rate = bitunix_decimal_rate(funding.get("fundingRate"))
        mark_price = as_float(funding.get("markPrice"))
        index_price = as_float(funding.get("indexPrice"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Bitunix reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": rate / interval_hours,
            "funding_rate_cap": bitunix_decimal_rate(funding.get("maxFundingRate")),
            "funding_rate_floor": bitunix_decimal_rate(funding.get("minFundingRate")),
            "funding_rate_kind": "published_next_estimate_percent_api",
            "next_funding_at": iso_from_milliseconds(funding.get("nextFundingTime")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": None,
            "volume_24h_usd": None,
            "observed_at": observed_at,
            "raw": funding,
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        allowed_limit = bitunix_depth_limit(limit)
        query = urllib.parse.urlencode({"symbol": symbol, "limit": allowed_limit})
        raw = bitunix_object(
            self.http.get_json(f"{self.base_url}/api/v1/futures/market/depth?{query}"),
            f"orderbook for {symbol}",
        )
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
        return []


def bitunix_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise FundingDataError(f"Bitunix {label} unavailable: {payload}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Bitunix {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def bitunix_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise FundingDataError(f"Bitunix {label} unavailable: {payload}")
    row = payload.get("data")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid Bitunix {label}")
    return row


def bitunix_decimal_rate(value: Any) -> float:
    # Bitunix public futures API returns funding rates as percentages: 0.01 means
    # 0.01%, while the scanner stores decimal rates where 0.0001 means 0.01%.
    return as_float(value) / 100.0


def bitunix_depth_limit(limit: int) -> str:
    requested = max(1, int(limit or 1))
    for allowed in (1, 5, 15, 50):
        if requested <= allowed:
            return str(allowed)
    return "max"


def is_supported_bitunix_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("symbolStatus") == "OPEN"
        and raw.get("isApiSupported") is True
        and clean_asset_symbol(raw.get("quote")) in {"USDT", "USDC"}
    )
