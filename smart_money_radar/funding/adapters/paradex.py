from __future__ import annotations

import statistics
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
    normalize_orderbook,
    parse_timestamp,
)


PARADEX_API_URL = "https://api.prod.paradex.trade"
PARADEX_HISTORY_LOOKBACK_HOURS = 8
PARADEX_HISTORY_PAGE_SIZE = 5_000


class ParadexFundingClient:
    venue = "paradex"
    incremental_history_lookback_hours = PARADEX_HISTORY_LOOKBACK_HOURS
    live_history_enabled = False

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = PARADEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.12)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        specs = paradex_rows(
            self.http.get_json(f"{self.base_url}/v1/markets"),
            "markets",
        )
        summary_query = urllib.parse.urlencode({"market": "ALL"})
        summaries = paradex_rows(
            self.http.get_json(
                f"{self.base_url}/v1/markets/summary?{summary_query}"
            ),
            "market summaries",
        )
        summary_by_symbol = {
            str(row.get("symbol")): row
            for row in summaries
            if row.get("symbol")
        }

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in specs:
            if not is_supported_paradex_market(raw, observed_at):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = clean_asset_symbol(raw.get("base_currency"))
            summary = summary_by_symbol.get(symbol)
            if not symbol or not asset or not summary:
                continue
            mark_price = as_float(summary.get("mark_price"))
            index_price = as_float(summary.get("underlying_price"))
            native_period = max(
                0.25,
                as_float(raw.get("funding_period_hours"), 8.0),
            )
            native_rate = as_float(summary.get("funding_rate"))
            hourly_rate = native_rate / native_period
            if mark_price <= 0 or index_price <= 0:
                continue
            maker_fee = max(0.0, api_fee(raw, "maker_fee", 0.0))
            taker_fee = max(0.0, api_fee(raw, "taker_fee", 0.0002))
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
                    "source_url": f"https://app.paradex.trade/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    # Paradex accrues continuously. A one-hour equivalent keeps
                    # cash-flow timing comparable with discrete venue histories.
                    "funding_rate": hourly_rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": hourly_rate,
                    "funding_rate_kind": "published_continuous_hourly_equivalent",
                    "next_funding_at": one_hour_after(observed_at),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": (
                        as_float(summary.get("open_interest")) * mark_price or None
                    ),
                    "volume_24h_usd": as_float(summary.get("volume_24h")) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_api_tier",
                    "observed_at": observed_at,
                    "raw": {
                        "market": raw,
                        "summary": summary,
                        "native_funding_rate": native_rate,
                        "native_funding_period_hours": native_period,
                        "normalization": "continuous_hourly_equivalent",
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
        query = urllib.parse.urlencode({"depth": max(1, min(int(limit), 100))})
        raw = self.http.get_json(
            f"{self.base_url}/v1/orderbook/{urllib.parse.quote(symbol)}?{query}"
        )
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Paradex orderbook for {symbol}")
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
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        requested_start = datetime.fromtimestamp(start_time_ms / 1_000.0, UTC)
        effective_start = max(
            requested_start,
            observed - timedelta(hours=PARADEX_HISTORY_LOOKBACK_HOURS),
        )
        base_params = {
            "market": symbol,
            "start_at": int(effective_start.timestamp() * 1_000),
            "end_at": int(observed.timestamp() * 1_000),
            "page_size": PARADEX_HISTORY_PAGE_SIZE,
        }
        samples_by_time: dict[int, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(2):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(
                f"{self.base_url}/v1/funding/data?{urllib.parse.urlencode(params)}"
            )
            page = paradex_rows(payload, f"funding history for {symbol}")
            for raw in page:
                timestamp = integer_or_none(raw.get("created_at"))
                if timestamp is not None:
                    samples_by_time[timestamp] = raw
            next_cursor = payload.get("next") if isinstance(payload, dict) else None
            if not page or not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)

        hourly_samples: dict[datetime, list[float]] = {}
        native_periods: dict[datetime, list[float]] = {}
        for timestamp, raw in samples_by_time.items():
            sampled_at = datetime.fromtimestamp(timestamp / 1_000.0, UTC)
            hour_start = sampled_at.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            if hour_end > observed or sampled_at < effective_start:
                continue
            native_period = max(
                0.25,
                as_float(raw.get("funding_period_hours"), 8.0),
            )
            native_rate = as_float(
                raw.get("funding_rate_8h"),
                as_float(raw.get("funding_rate")),
            )
            hourly_samples.setdefault(hour_end, []).append(native_rate / native_period)
            native_periods.setdefault(hour_end, []).append(native_period)

        rows: list[dict[str, Any]] = []
        for hour_end in sorted(hourly_samples):
            values = hourly_samples[hour_end]
            hourly_rate = statistics.fmean(values)
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": hour_end.isoformat(),
                    "funding_rate": hourly_rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": hourly_rate,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": {
                        "aggregation": "completed_hour_mean",
                        "sample_count": len(values),
                        "native_period_hours": statistics.fmean(
                            native_periods[hour_end]
                        ),
                    },
                }
            )
        return rows


def paradex_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Paradex {label} unavailable: {detail}")
    return [row for row in rows if isinstance(row, dict)]


def is_supported_paradex_market(
    raw: dict[str, Any],
    observed_at: str | None = None,
) -> bool:
    observed = parse_timestamp(observed_at) if observed_at else None
    open_at = integer_or_none(raw.get("open_at"), 0) or 0
    is_open = observed is None or open_at <= int(observed.timestamp() * 1_000)
    return (
        is_open
        and raw.get("asset_kind") == "PERP"
        and raw.get("quote_currency") == "USD"
        and raw.get("settlement_currency") == "USDC"
        and integer_or_none(raw.get("expiry_at"), 0) == 0
        and str(raw.get("trading_mode") or "STANDARD") == "STANDARD"
    )


def api_fee(raw: dict[str, Any], role: str, fallback: float) -> float:
    fee_config = raw.get("fee_config")
    api_config = fee_config.get("api_fee") if isinstance(fee_config, dict) else None
    role_config = api_config.get(role) if isinstance(api_config, dict) else None
    if not isinstance(role_config, dict):
        return fallback
    return as_float(role_config.get("fee"), fallback)


def one_hour_after(observed_at: str) -> str | None:
    observed = parse_timestamp(observed_at)
    return (observed + timedelta(hours=1)).isoformat() if observed else None


def integer_or_none(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
