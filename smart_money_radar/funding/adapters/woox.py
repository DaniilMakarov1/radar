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


WOOX_API_URL = "https://api.woox.io"


class WOOXFundingClient:
    venue = "woox"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = WOOX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        futures = woox_rows(
            self.http.get_json(f"{self.base_url}/v3/public/futures"),
            "futures",
        )
        funding_rows = woox_rows(
            self.http.get_json(f"{self.base_url}/v1/public/funding_rates"),
            "funding rates",
        )
        funding_by_symbol = rows_by_key(funding_rows, "symbol")
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in futures:
            symbol = str(raw.get("symbol") or "")
            if not symbol.startswith("PERP_") or not symbol.endswith(("_USDT", "_USDC")):
                continue
            funding = funding_by_symbol.get(symbol, {})
            base_asset, quote_asset = woox_symbol_assets(symbol)
            if not base_asset or quote_asset not in {"USDT", "USDC"}:
                continue
            funding_rate = as_float(
                funding.get("est_funding_rate"),
                as_float(raw.get("estFundingRate")),
            )
            interval_hours = max(
                0.25,
                as_float(
                    funding.get("est_funding_rate_interval"),
                    as_float(funding.get("last_funding_rate_interval"), 8.0),
                ),
            )
            mark_price = as_float(raw.get("markPrice"))
            index_price = as_float(raw.get("indexPrice"))
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
                    "source_url": f"https://woox.io/futures/{symbol}",
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
                    "next_funding_at": iso_from_milliseconds(
                        funding.get("next_funding_time")
                        or raw.get("nextFundingTime")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(raw.get("openInterest")) * mark_price
                    or None,
                    "volume_24h_usd": as_float(raw.get("24hAmount")) or None,
                    "observed_at": observed_at,
                    "raw": {"future": raw, "funding": funding},
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
        future = woox_object(
            self.http.get_json(
                f"{self.base_url}/v3/public/futures?"
                f"{urllib.parse.urlencode({'symbol': symbol})}"
            ),
            f"future for {symbol}",
        )
        funding = woox_object(
            self.http.get_json(
                f"{self.base_url}/v1/public/funding_rate/"
                f"{urllib.parse.quote(symbol)}"
            ),
            f"funding rate for {symbol}",
        )
        interval_hours = max(
            0.25,
            as_float(
                funding.get("est_funding_rate_interval"),
                as_float(
                    funding.get("last_funding_rate_interval"),
                    as_float(previous_market.get("funding_interval_hours"), 8.0)
                    if previous_market
                    else 8.0,
                ),
            ),
        )
        rate = as_float(
            funding.get("est_funding_rate"),
            as_float(future.get("estFundingRate")),
        )
        mark_price = as_float(future.get("markPrice"))
        index_price = as_float(future.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"WOOX reference prices unavailable for {symbol}")
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(
                funding.get("next_funding_time") or future.get("nextFundingTime")
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(future.get("openInterest")) * mark_price
            or None,
            "volume_24h_usd": as_float(future.get("24hAmount")) or None,
            "observed_at": observed_at,
            "raw": {"future": future, "funding": funding},
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"symbol": symbol, "maxLevel": max(1, min(int(limit), 500))}
        )
        payload = self.http.get_json(f"{self.base_url}/v3/public/orderbook?{query}")
        raw = woox_object(payload, f"orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            dict_price_size_levels(raw.get("bids")),
            dict_price_size_levels(raw.get("asks")),
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
        query = urllib.parse.urlencode({"symbol": symbol, "size": 500})
        rows = woox_rows(
            self.http.get_json(
                f"{self.base_url}/v1/public/funding_rate_history?{query}"
            ),
            f"funding history for {symbol}",
        )
        rows_by_time: dict[int, dict[str, Any]] = {}
        for raw in rows:
            timestamp = integer_or_none(raw.get("funding_rate_timestamp"))
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
            rate = as_float(raw.get("funding_rate"))
            output.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("mark_price")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in output if row["funding_at"]]


def woox_symbol_assets(symbol: str) -> tuple[str, str]:
    parts = symbol.split("_")
    if len(parts) < 3:
        return "", ""
    return clean_asset_symbol(parts[1]), clean_asset_symbol(parts[-1])


def woox_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise FundingDataError(f"WOOX {label} unavailable")
    rows = payload.get("rows")
    if rows is None and isinstance(payload.get("data"), dict):
        rows = payload["data"].get("rows")
    if rows is None and isinstance(payload.get("data"), list):
        rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid WOOX {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def woox_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise FundingDataError(f"WOOX {label} unavailable")
    row = payload.get("data")
    if isinstance(row, dict) and isinstance(row.get("rows"), list):
        rows = [item for item in row["rows"] if isinstance(item, dict)]
        if not rows:
            raise FundingDataError(f"WOOX {label} returned no rows")
        return rows[0]
    if not isinstance(row, dict):
        row = {key: value for key, value in payload.items() if key not in {"success"}}
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid WOOX {label}")
    return row


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
