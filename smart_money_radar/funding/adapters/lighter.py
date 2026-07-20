from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    normalize_orderbook,
    parse_timestamp,
)


LIGHTER_API_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_HISTORY_PAGE_SIZE = 750
LIGHTER_CURRENT_FUNDING_PERIOD_HOURS = 8.0


class LighterFundingClient:
    venue = "lighter"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = LIGHTER_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._market_ids: dict[str, int] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        books_payload = self.http.get_json(f"{self.base_url}/api/v1/orderBooks")
        details_payload = self.http.get_json(
            f"{self.base_url}/api/v1/orderBookDetails?filter=perp"
        )
        rates_payload = self.http.get_json(f"{self.base_url}/api/v1/funding-rates")
        specs = lighter_rows(books_payload, "order_books", "markets")
        details = lighter_rows(
            details_payload,
            "order_book_details",
            "market details",
        )
        rates = lighter_rows(rates_payload, "funding_rates", "funding rates")
        detail_map = {integer_or_none(row.get("market_id")): row for row in details}
        rate_map = {
            integer_or_none(row.get("market_id")): row
            for row in rates
            if str(row.get("exchange") or "").lower() == self.venue
        }

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        market_ids: dict[str, int] = {}
        for raw in specs:
            if raw.get("market_type") != "perp" or raw.get("status") != "active":
                continue
            symbol = str(raw.get("symbol") or "")
            asset = clean_asset_symbol(symbol)
            market_id = integer_or_none(raw.get("market_id"))
            detail = detail_map.get(market_id)
            funding = rate_map.get(market_id)
            if not symbol or not asset or market_id is None or not detail or not funding:
                continue
            mark_price = as_float(detail.get("mark_price"))
            index_price = as_float(detail.get("index_price"))
            if mark_price <= 0 or index_price <= 0:
                continue
            published_rate = as_float(funding.get("rate"))
            hourly_funding_rate = published_rate / LIGHTER_CURRENT_FUNDING_PERIOD_HOURS
            interval_hours = 1.0
            maker_fee = percentage_points_to_decimal(raw.get("maker_fee"))
            taker_fee = percentage_points_to_decimal(raw.get("taker_fee"))
            market_ids[symbol] = market_id
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
                    "source_url": f"https://app.lighter.xyz/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": hourly_funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": hourly_funding_rate,
                    "funding_rate_kind": "published_8h_equivalent_normalized_hourly",
                    "published_funding_rate": published_rate,
                    "published_funding_interval_hours": (
                        LIGHTER_CURRENT_FUNDING_PERIOD_HOURS
                    ),
                    "funding_display_note": "published 8h equivalent; cashflow hourly",
                    "next_funding_at": next_utc_hour(observed_at),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(detail.get("open_interest"))
                    * mark_price or None,
                    "volume_24h_usd": as_float(
                        detail.get("daily_quote_token_volume")
                    ) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_account_type",
                    "observed_at": observed_at,
                    "raw": {
                        "spec": raw,
                        "detail": detail,
                        "funding": funding,
                        "published_funding_rate": published_rate,
                        "published_funding_period_hours": (
                            LIGHTER_CURRENT_FUNDING_PERIOD_HOURS
                        ),
                        "normalization": "published_8h_rate_divided_by_8",
                    },
                }
            )
        self._market_ids = market_ids
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        market_id = self.market_id(symbol)
        query = urllib.parse.urlencode(
            {"market_id": market_id, "limit": max(5, min(int(limit), 100))}
        )
        raw = self.http.get_json(
            f"{self.base_url}/api/v1/orderBookOrders?{query}"
        )
        if not isinstance(raw, dict) or int(as_float(raw.get("code"))) != 200:
            raise FundingDataError(f"Invalid Lighter orderbook for {symbol}")
        bids = aggregate_lighter_orders(raw.get("bids"))
        asks = aggregate_lighter_orders(raw.get("asks"))
        return normalize_orderbook(
            self.venue,
            symbol,
            bids,
            asks,
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
        market_id = self.market_id(symbol)
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        cursor_end_ms = int(observed.timestamp() * 1_000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(5):
            query = urllib.parse.urlencode(
                {
                    "market_id": market_id,
                    "resolution": "1h",
                    "start_timestamp": int(start_time_ms),
                    "end_timestamp": cursor_end_ms,
                    "count_back": LIGHTER_HISTORY_PAGE_SIZE,
                }
            )
            payload = self.http.get_json(f"{self.base_url}/api/v1/fundings?{query}")
            page = lighter_rows(payload, "fundings", f"funding history for {symbol}")
            timestamps: list[int] = []
            for raw in page:
                timestamp = integer_or_none(raw.get("timestamp"))
                if timestamp is None:
                    continue
                timestamp_ms = timestamp * 1_000
                if timestamp_ms <= int(observed.timestamp() * 1_000):
                    timestamps.append(timestamp)
                    rows_by_time[timestamp] = raw
            if not timestamps:
                break
            oldest_ms = min(timestamps) * 1_000
            if oldest_ms <= start_time_ms or len(page) < LIGHTER_HISTORY_PAGE_SIZE:
                break
            next_cursor = oldest_ms - 1
            if next_cursor >= cursor_end_ms:
                break
            cursor_end_ms = next_cursor

        eligible_times = [
            timestamp
            for timestamp in rows_by_time
            if timestamp * 1_000 >= start_time_ms
        ]
        intervals = inferred_funding_intervals(
            eligible_times,
            interval_hours,
            units_per_second=1.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(eligible_times):
            raw = rows_by_time[timestamp]
            actual_interval = intervals[timestamp]
            magnitude = abs(as_float(raw.get("rate"))) / 100.0
            direction = str(raw.get("direction") or "").lower()
            rate = -magnitude if direction == "short" else magnitude
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": datetime.fromtimestamp(timestamp, UTC)
                    .replace(microsecond=0)
                    .isoformat(),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows

    def market_id(self, symbol: str) -> int:
        market_id = self._market_ids.get(symbol)
        if market_id is None:
            raise FundingDataError(f"Lighter market id unavailable for {symbol}")
        return market_id


def lighter_rows(payload: Any, field: str, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise FundingDataError(f"Invalid Lighter {label} response")
    rows = payload.get(field)
    if not isinstance(rows, list):
        detail = payload.get("message") or payload.get("code") or "invalid rows"
        raise FundingDataError(f"Lighter {label} unavailable: {detail}")
    return [row for row in rows if isinstance(row, dict)]


def percentage_points_to_decimal(value: Any) -> float:
    return max(0.0, as_float(value)) / 100.0


def aggregate_lighter_orders(rows: Any) -> list[list[float]]:
    amounts: dict[float, float] = {}
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = as_float(row.get("price"))
        size = as_float(row.get("remaining_base_amount"))
        if price > 0 and size > 0:
            amounts[price] = amounts.get(price, 0.0) + size
    return [[price, size] for price, size in amounts.items()]


def next_utc_hour(observed_at: str) -> str | None:
    observed = parse_timestamp(observed_at)
    if observed is None:
        return None
    return (
        observed.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    ).isoformat()


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
