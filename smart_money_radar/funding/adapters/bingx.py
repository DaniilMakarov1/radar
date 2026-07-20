from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    canonical_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


BINGX_API_URL = "https://open-api.bingx.com"


class BingXFundingClient:
    venue = "bingx"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BINGX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        contracts = bingx_rows(
            self.http.get_json(f"{self.base_url}/openApi/swap/v2/quote/contracts"),
            "contracts",
        )
        premium_rows = bingx_rows(
            self.http.get_json(f"{self.base_url}/openApi/swap/v2/quote/premiumIndex"),
            "premium index",
        )
        premium_by_symbol = rows_by_symbol(premium_rows)

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in contracts:
            if not is_supported_bingx_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = canonical_asset_symbol(raw.get("asset"))
            current = premium_by_symbol.get(symbol)
            if not symbol or not asset or not current:
                continue
            mark_price = as_float(current.get("markPrice"))
            index_price = as_float(current.get("indexPrice"))
            interval_hours = max(1.0, as_float(current.get("fundingIntervalHours"), 8.0))
            funding_rate = as_float(current.get("lastFundingRate"))
            if mark_price <= 0 or index_price <= 0:
                continue
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://bingx.com/en/perpetual/{symbol}",
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
                    "funding_rate_kind": "published_current_estimate",
                    "next_funding_at": iso_from_milliseconds(current.get("nextFundingTime")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": max(0.0, as_float(raw.get("makerFeeRate"), 0.0002)),
                    "taker_fee_rate": max(0.0, as_float(raw.get("takerFeeRate"), 0.0005)),
                    "fee_source": "venue_public_contract_tier",
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "premium": current},
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
            {"symbol": symbol, "limit": max(5, min(int(limit), 100))}
        )
        raw = bingx_object(
            self.http.get_json(f"{self.base_url}/openApi/swap/v2/quote/depth?{query}"),
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
        query = urllib.parse.urlencode({"symbol": symbol, "limit": 1000})
        page = bingx_rows(
            self.http.get_json(f"{self.base_url}/openApi/swap/v2/quote/fundingRate?{query}"),
            f"funding history for {symbol}",
        )
        rows: list[dict[str, Any]] = []
        for raw in page:
            timestamp = integer_or_none(raw.get("fundingTime"))
            if timestamp is None or timestamp < start_time_ms:
                continue
            actual_interval = max(1.0, float(interval_hours or 8.0))
            rate = as_float(raw.get("fundingRate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("markPrice")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in sorted(rows, key=lambda item: item["funding_at"] or "") if row["funding_at"]]


def bingx_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or integer_or_none(payload.get("code"), -1) != 0:
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"BingX {label} unavailable: {detail}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid BingX {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def bingx_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or integer_or_none(payload.get("code"), -1) != 0:
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"BingX {label} unavailable: {detail}")
    row = payload.get("data")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid BingX {label}")
    return row


def rows_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("symbol")): row for row in rows if row.get("symbol")}


def is_supported_bingx_contract(raw: dict[str, Any]) -> bool:
    return (
        int(raw.get("status") or 0) == 1
        and str(raw.get("currency") or "").upper() == "USDT"
        and str(raw.get("apiStateOpen") or "").lower() == "true"
        and str(raw.get("apiStateClose") or "").lower() == "true"
    )


def integer_or_none(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
