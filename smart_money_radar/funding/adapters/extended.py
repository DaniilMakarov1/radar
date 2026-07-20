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


EXTENDED_API_URL = "https://api.starknet.extended.exchange"
EXTENDED_FUNDING_INTERVAL_HOURS = 1.0
EXTENDED_MAKER_FEE_RATE = 0.0
EXTENDED_TAKER_FEE_RATE = 0.00025


class ExtendedFundingClient:
    venue = "extended"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = EXTENDED_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        rows = extended_data(
            self.http.get_json(f"{self.base_url}/api/v1/info/markets"),
            "markets",
        )
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in rows:
            if not is_supported_extended_market(raw):
                continue
            symbol = str(raw.get("name") or "")
            asset = clean_asset_symbol(raw.get("assetName"))
            stats = raw.get("marketStats") if isinstance(raw.get("marketStats"), dict) else {}
            if not symbol or not asset:
                continue
            mark_price = as_float(stats.get("markPrice"))
            index_price = as_float(stats.get("indexPrice"))
            funding_rate = as_float(stats.get("fundingRate"))
            if mark_price <= 0 or index_price <= 0:
                continue
            next_funding_at = iso_from_milliseconds(stats.get("nextFundingRate"))
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USD",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.extended.exchange/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": {"market": raw, "asset_class": raw.get("category")},
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": EXTENDED_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_current_hour_estimate",
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(stats.get("openInterest")) or None,
                    "volume_24h_usd": as_float(stats.get("dailyVolume")) or None,
                    "maker_fee_rate": EXTENDED_MAKER_FEE_RATE,
                    "taker_fee_rate": EXTENDED_TAKER_FEE_RATE,
                    "fee_source": "venue_public_flat_fee",
                    "observed_at": observed_at,
                    "raw": {
                        "market": raw,
                        "asset_class": raw.get("category"),
                        "funding_interval": "hourly",
                    },
                }
            )
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        raw = self.http.get_json(
            f"{self.base_url}/api/v1/info/markets/"
            f"{urllib.parse.quote(symbol)}/orderbook"
        )
        data = extended_dict(raw, f"orderbook for {symbol}")
        bids = extended_levels(data.get("bid"), limit, reverse=True)
        asks = extended_levels(data.get("ask"), limit, reverse=False)
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
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        end_time_ms = int(observed.timestamp() * 1_000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        cursor: int | None = None
        for _ in range(10):
            params: dict[str, Any] = {
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "limit": 1_000,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = self.http.get_json(
                f"{self.base_url}/api/v1/info/"
                f"{urllib.parse.quote(symbol)}/funding?"
                f"{urllib.parse.urlencode(params)}"
            )
            page = extended_data(payload, f"funding history for {symbol}")
            oldest: int | None = None
            for raw in page:
                timestamp = int(as_float(raw.get("T")))
                if timestamp <= 0 or timestamp > end_time_ms:
                    continue
                rows_by_time[timestamp] = raw
                oldest = timestamp if oldest is None else min(oldest, timestamp)
            if len(page) < 1_000 or (oldest is not None and oldest <= start_time_ms):
                break
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            next_cursor = (pagination or {}).get("cursor") if isinstance(pagination, dict) else None
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = int(next_cursor)

        timestamps = [value for value in rows_by_time if value >= start_time_ms]
        intervals = inferred_funding_intervals(
            timestamps,
            EXTENDED_FUNDING_INTERVAL_HOURS,
            units_per_second=1000.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(timestamps):
            raw = rows_by_time[timestamp]
            actual_interval = intervals[timestamp]
            rate = as_float(raw.get("f"))
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


def is_supported_extended_market(raw: dict[str, Any]) -> bool:
    if str(raw.get("type") or "").upper() != "PERPETUAL":
        return False
    if not bool(raw.get("active")) or str(raw.get("status") or "").upper() != "ACTIVE":
        return False
    if bool(raw.get("isRfq")) or bool(raw.get("isOffHours")):
        return False
    if str(raw.get("category") or "").lower() != "crypto":
        return False
    return True


def extended_data(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("status") or "").upper() != "OK":
        detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Extended {label} unavailable: {detail}")
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    raise FundingDataError(f"Invalid Extended {label} rows")


def extended_dict(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("status") or "").upper() != "OK":
        detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Extended {label} unavailable: {detail}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FundingDataError(f"Invalid Extended {label}")
    return data


def extended_levels(raw_levels: Any, limit: int, *, reverse: bool) -> list[list[float]]:
    if not isinstance(raw_levels, list):
        return []
    levels: list[list[float]] = []
    for raw in raw_levels:
        if not isinstance(raw, dict):
            continue
        price = as_float(raw.get("price"))
        size = as_float(raw.get("qty"))
        if price > 0 and size > 0:
            levels.append([price, size])
    levels.sort(key=lambda row: row[0], reverse=reverse)
    return levels[: max(1, min(int(limit), 1_000))]
