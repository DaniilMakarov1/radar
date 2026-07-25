from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import (
    dict_price_size_levels,
    iso_from_nanoseconds,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    normalize_orderbook,
    parse_timestamp,
)


RISEX_API_URL = "https://api.rise.trade"
RISEX_FUNDING_INTERVAL_HOURS = 1.0
RISEX_FUNDING_PAGE_SIZE = 100


class RiseXFundingClient:
    venue = "risex"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = RISEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.05)
        self.base_url = base_url.rstrip("/")
        self._market_ids: dict[str, str] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        payload = self.http.get_json(f"{self.base_url}/v1/markets")
        data = payload.get("data") if isinstance(payload, dict) else None
        raw_markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(raw_markets, list):
            raise FundingDataError("Invalid RiseX markets response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        market_ids: dict[str, str] = {}

        for raw in raw_markets:
            if not isinstance(raw, dict):
                continue
            if not raw.get("active", False):
                continue
            display_name = str(raw.get("display_name") or "")
            if "deprecated" in display_name.lower():
                continue
            symbol = display_name
            canonical_asset = clean_asset_symbol(symbol.split("/")[0] if "/" in symbol else symbol)
            if not symbol or not canonical_asset:
                continue

            mark_price = as_float(raw.get("mark_price"))
            index_price = as_float(raw.get("index_price"))
            last_price = as_float(raw.get("last_price"))
            if mark_price <= 0 or index_price <= 0 or last_price <= 0:
                continue

            market_id = str(raw.get("market_id") or "")
            if not market_id:
                continue

            # current_funding_rate is already the hourly rate
            hourly_funding_rate = as_float(raw.get("current_funding_rate"))
            funding_rate_8h = as_float(raw.get("funding_rate_8h"))
            interval_hours = RISEX_FUNDING_INTERVAL_HOURS

            next_funding_ns = raw.get("next_funding_time")
            next_funding_at = iso_from_nanoseconds(next_funding_ns)
            if not next_funding_at:
                next_funding_at = next_utc_hour(observed_at)

            open_interest_base = as_float(raw.get("open_interest"))
            open_interest_usd = open_interest_base * mark_price if open_interest_base > 0 else None
            volume_24h = as_float(raw.get("quote_volume_24h")) or None

            config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
            max_leverage = as_float(config.get("max_leverage"))
            maker_fee = risex_maker_fee(max_leverage)
            taker_fee = risex_taker_fee(max_leverage)

            market_ids[symbol] = market_id
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://rise.trade/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "funding_rate": hourly_funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": hourly_funding_rate,
                    "funding_rate_kind": "published_current_hourly",
                    "published_funding_rate": funding_rate_8h,
                    "published_funding_interval_hours": 8.0,
                    "funding_display_note": "hourly settlement; 8h equivalent shown as published",
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": open_interest_usd,
                    "volume_24h_usd": volume_24h,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_tier1",
                    "observed_at": observed_at,
                    "raw": {"market": raw, "config": config},
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
        payload = self.http.get_json(f"{self.base_url}/v1/orderbook?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise FundingDataError(f"Invalid RiseX orderbook for {symbol}")
        bids = dict_price_size_levels(data.get("bids"))
        asks = dict_price_size_levels(data.get("asks"))
        return normalize_orderbook(
            self.venue,
            symbol,
            bids,
            asks,
            observed_at,
            data,
        )

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        market_id = self.market_id(symbol)
        start_ns = int(start_time_ms) * 1_000_000
        rows_by_time: dict[int, dict[str, Any]] = {}

        for page in range(1, 21):
            query = urllib.parse.urlencode(
                {"limit": RISEX_FUNDING_PAGE_SIZE, "page": page}
            )
            payload = self.http.get_json(
                f"{self.base_url}/v1/markets/id/{market_id}/funding-rate-history?{query}"
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            records = data.get("records") if isinstance(data, dict) else None
            if not isinstance(records, list) or not records:
                break

            for raw in records:
                if not isinstance(raw, dict):
                    continue
                end_ns = _parse_ns(raw.get("end_time"))
                if end_ns is None:
                    continue
                if end_ns < start_ns:
                    continue
                rows_by_time[end_ns] = raw

            has_next = bool(data.get("has_next_page"))
            if not has_next:
                break
            oldest_ns = min(
                (_parse_ns(r.get("end_time")) or 0)
                for r in records
                if isinstance(r, dict)
            )
            if oldest_ns <= start_ns:
                break

        rows: list[dict[str, Any]] = []
        for end_ns in sorted(rows_by_time):
            raw = rows_by_time[end_ns]
            rate = as_float(raw.get("funding_rate"))
            funding_at = iso_from_nanoseconds(end_ns)
            if not funding_at:
                continue
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": funding_at,
                    "funding_rate": rate,
                    "funding_interval_hours": RISEX_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": rate,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows

    def market_id(self, symbol: str) -> str:
        market_id = self._market_ids.get(symbol)
        if market_id is None:
            raise FundingDataError(f"RiseX market id unavailable for {symbol}")
        return market_id


def risex_maker_fee(max_leverage: float) -> float:
    """Tier 1 maker fee: 1 bp (0.01%)."""
    return 0.0001


def risex_taker_fee(max_leverage: float) -> float:
    """Tier 1 taker fee: 3 bps (0.03%)."""
    return 0.0003


def next_utc_hour(observed_at: str) -> str | None:
    observed = parse_timestamp(observed_at)
    if observed is None:
        return None
    return (
        observed.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    ).isoformat()


def _parse_ns(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
