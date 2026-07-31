from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
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
    parse_timestamp,
)


BACKPACK_API_URL = "https://api.backpack.exchange"


class BackpackFundingClient:
    venue = "backpack"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BACKPACK_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        specs = backpack_rows(
            self.http.get_json(f"{self.base_url}/api/v1/markets?marketType=PERP"),
            "markets",
        )
        prices = backpack_rows(
            self.http.get_json(f"{self.base_url}/api/v1/markPrices?marketType=PERP"),
            "mark prices",
        )
        price_map = {
            str(row.get("symbol") or ""): row for row in prices if row.get("symbol")
        }
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in specs:
            if (
                raw.get("marketType") != "PERP"
                or not bool(raw.get("visible"))
                or raw.get("orderBookState") != "Open"
                or raw.get("quoteSymbol") != "USDC"
            ):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = clean_asset_symbol(raw.get("baseSymbol"))
            price = price_map.get(symbol)
            if not symbol or not asset or not price:
                continue
            interval_hours = max(
                0.25,
                as_float(raw.get("fundingInterval"), 3_600_000.0) / 3_600_000.0,
            )
            funding_rate = as_float(price.get("fundingRate"))
            mark_price = as_float(price.get("markPrice"))
            index_price = as_float(price.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://backpack.exchange/trade/{symbol}",
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
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(
                        price.get("nextFundingTimestamp")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": 0.0002,
                    "taker_fee_rate": 0.0005,
                    "fee_source": "venue_public_tier",
                    "observed_at": observed_at,
                    "raw": {"market": raw, "mark": price},
                }
            )
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"symbol": symbol, "limit": max(5, min(int(limit), 1_000))}
        )
        raw = self.http.get_json(f"{self.base_url}/api/v1/depth?{query}")
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Backpack orderbook for {symbol}")
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
        query = urllib.parse.urlencode({"symbol": symbol})
        prices = backpack_rows(
            self.http.get_json(f"{self.base_url}/api/v1/markPrices?{query}"),
            f"mark price for {symbol}",
        )
        price = next(
            (
                row
                for row in prices
                if str(row.get("symbol") or "").strip().lower()
                == str(symbol).strip().lower()
            ),
            prices[0] if prices else None,
        )
        if not price:
            raise FundingDataError(f"Backpack mark price unavailable for {symbol}")
        previous = previous_market or {}
        interval_hours = max(
            0.25,
            as_float(previous.get("funding_interval_hours"), 1.0),
        )
        funding_rate = as_float(price.get("fundingRate"))
        mark_price = as_float(price.get("markPrice"))
        index_price = as_float(price.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Backpack reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(
                price.get("nextFundingTimestamp")
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": None,
            "volume_24h_usd": previous.get("volume_24h_usd"),
            "maker_fee_rate": previous.get("maker_fee_rate", 0.0002),
            "taker_fee_rate": previous.get("taker_fee_rate", 0.0005),
            "fee_source": previous.get("fee_source") or "venue_public_tier",
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": {"mark": price},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        rows_by_time: dict[int, dict[str, Any]] = {}
        offset = 0
        for _ in range(10):
            query = urllib.parse.urlencode(
                {"symbol": symbol, "limit": 1_000, "offset": offset}
            )
            page = backpack_rows(
                self.http.get_json(f"{self.base_url}/api/v1/fundingRates?{query}"),
                f"funding history for {symbol}",
            )
            oldest: int | None = None
            for raw in page:
                settled_at = parse_timestamp(raw.get("intervalEndTimestamp"))
                if settled_at is None or settled_at > observed:
                    continue
                timestamp = int(settled_at.timestamp() * 1000)
                rows_by_time[timestamp] = raw
                oldest = timestamp if oldest is None else min(oldest, timestamp)
            if len(page) < 1_000 or (oldest is not None and oldest <= start_time_ms):
                break
            offset += len(page)

        timestamps = [value for value in rows_by_time if value >= start_time_ms]
        intervals = inferred_funding_intervals(
            timestamps,
            interval_hours,
            units_per_second=1000.0,
        )
        rows = []
        for timestamp in sorted(timestamps):
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
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows


def backpack_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Backpack {label} unavailable: {detail}")
    return [row for row in payload if isinstance(row, dict)]
