from __future__ import annotations

import os
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


DRIFT_DATA_API_URL = "https://data.api.drift.trade"
VELOCITY_STATS_API_URL = "https://data.velocity.exchange"
VELOCITY_PUBLIC_DLOB_URL = "https://dlob.velocity.exchange"


class DriftFundingClient:
    venue = "drift"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        data_url: str = DRIFT_DATA_API_URL,
        stats_url: str = VELOCITY_STATS_API_URL,
        dlob_url: str | None = None,
        symbols: tuple[str, ...] | None = None,
        maximum_live_age_hours: float = 3.0,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.data_url = data_url.rstrip("/")
        self.stats_url = stats_url.rstrip("/")
        self.dlob_url = (
            dlob_url or os.getenv("DRIFT_DLOB_URL") or VELOCITY_PUBLIC_DLOB_URL
        ).rstrip("/")
        self.symbols = symbols
        self.maximum_live_age_hours = maximum_live_age_hours

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            stats_rows = velocity_stats_rows(
                self.http.get_json(f"{self.stats_url}/stats/markets"),
                "market stats",
            )
        except FundingDataError as exc:
            warnings.append(f"Velocity stats unavailable, falling back to legacy Drift data: {exc}")
        else:
            instruments, markets, stats_warnings = self.catalog_from_velocity_stats(
                stats_rows,
                observed_at,
                observed,
            )
            warnings.extend(stats_warnings)
            if markets:
                return instruments, markets, warnings
            warnings.append("Velocity stats returned no usable perp funding rows.")

        legacy_symbols = self.symbols or ("SOL-PERP", "BTC-PERP", "ETH-PERP")
        for symbol in legacy_symbols:
            try:
                records = drift_records(
                    self.http.get_json(
                        f"{self.data_url}/market/{urllib.parse.quote(symbol)}/fundingRates?limit=2"
                    ),
                    f"funding for {symbol}",
                )
            except FundingDataError as exc:
                warnings.append(str(exc))
                continue
            if not records:
                warnings.append(f"Drift {symbol} has no funding records.")
                continue
            latest = max(records, key=lambda row: int(row.get("ts") or 0))
            latest_at = datetime.fromtimestamp(int(latest.get("ts") or 0), UTC)
            age_hours = (observed - latest_at).total_seconds() / 3600.0
            if age_hours < -0.25 or age_hours > self.maximum_live_age_hours:
                warnings.append(
                    f"Drift {symbol} funding is stale ({max(0.0, age_hours):.1f}h); excluded."
                )
                continue
            asset = clean_asset_symbol(symbol.removesuffix("-PERP"))
            mark_price = as_float(latest.get("markPriceTwap"))
            index_price = as_float(latest.get("oraclePriceTwap"))
            if not asset or mark_price <= 0 or index_price <= 0:
                continue
            # Drift's Data API formats funding in percentage points (e.g. .0012%).
            funding_rate = as_float(latest.get("fundingRate")) / 100.0
            next_funding = observed.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
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
                    "source_url": f"https://app.drift.trade/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": latest,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_last_settlement",
                    "next_funding_at": next_funding.isoformat(),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": 0.0,
                    "taker_fee_rate": 0.001,
                    "fee_source": "public_default",
                    "observed_at": observed_at,
                    "raw": latest,
                }
            )
        if markets and not self.dlob_url:
            warnings.append(
                "Drift funding connected, but executable DLOB depth requires DRIFT_DLOB_URL."
            )
        return instruments, markets, warnings

    def catalog_from_velocity_stats(
        self,
        rows: list[dict[str, Any]],
        observed_at: str,
        observed: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        symbol_filter = {symbol.upper() for symbol in self.symbols or ()}
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in rows:
            if str(raw.get("marketType") or "").lower() != "perp":
                continue
            symbol = str(raw.get("symbol") or "").upper()
            if symbol_filter and symbol not in symbol_filter:
                continue
            if str(raw.get("status") or "").lower() not in {"active", "initialized"}:
                continue
            asset = clean_asset_symbol(
                str(raw.get("baseAsset") or symbol.removesuffix("-PERP"))
            )
            if not asset or not symbol.endswith("-PERP"):
                continue
            update_ts = integer_or_none(raw.get("fundingRateUpdateTs"))
            latest_at = (
                datetime.fromtimestamp(update_ts, UTC)
                if update_ts is not None
                else observed
            )
            age_hours = (observed - latest_at).total_seconds() / 3600.0
            if age_hours < -0.25 or age_hours > self.maximum_live_age_hours:
                warnings.append(
                    f"Velocity {symbol} funding is stale ({max(0.0, age_hours):.1f}h); excluded."
                )
                continue
            funding_rate = velocity_canonical_funding_rate(raw.get("fundingRate"))
            mark_price = as_float(raw.get("markPrice"), as_float(raw.get("price")))
            index_price = as_float(raw.get("oraclePrice"))
            if not asset or mark_price <= 0 or index_price <= 0:
                continue
            fees = raw.get("fees") if isinstance(raw.get("fees"), dict) else {}
            long_oi = abs(as_float(nested_value(raw, ("openInterest", "long"))))
            short_oi = abs(as_float(nested_value(raw, ("openInterest", "short"))))
            open_interest = (long_oi + short_oi) * index_price if index_price > 0 else None
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": str(raw.get("quoteAsset") or "USDT").upper(),
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.velocity.exchange/trade/{symbol}",
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
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_velocity_hourly_side_cashflow",
                    "published_funding_rate": funding_rate,
                    "published_funding_interval_hours": 1.0,
                    "funding_display_note": (
                        "Velocity stats side cashflow normalized to CEX sign"
                    ),
                    "next_funding_at": next_velocity_funding_time(latest_at, observed),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": open_interest,
                    "volume_24h_usd": as_float(raw.get("quoteVolume")) or None,
                    "maker_fee_rate": as_float(fees.get("maker")),
                    "taker_fee_rate": as_float(fees.get("taker"), 0.00035),
                    "fee_source": "velocity_public_stats",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return instruments, markets, warnings

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not self.dlob_url:
            raise FundingDataError(
                "Drift executable DLOB depth unavailable; configure DRIFT_DLOB_URL"
            )
        query = urllib.parse.urlencode(
            {
                "marketName": symbol,
                "marketType": "perp",
                "depth": max(1, min(int(limit), 100)),
            }
        )
        raw = self.http.get_json(f"{self.dlob_url}/l2?{query}")
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Drift DLOB for {symbol}")
        bids = drift_dlob_levels(raw.get("bids"))
        asks = drift_dlob_levels(raw.get("asks"))
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
        rows_by_time: dict[int, dict[str, Any]] = {}
        page: str | None = None
        for _ in range(10):
            query: dict[str, Any] = {"limit": 750}
            if page:
                query["page"] = page
            payload = self.http.get_json(
                f"{self.data_url}/market/{urllib.parse.quote(symbol)}/fundingRates?"
                f"{urllib.parse.urlencode(query)}"
            )
            records = drift_records(payload, f"funding history for {symbol}")
            for raw in records:
                timestamp = int(raw.get("ts") or 0)
                if timestamp > 0:
                    rows_by_time[timestamp] = raw
            if not records or min(rows_by_time, default=0) * 1000 <= start_time_ms:
                break
            meta = payload.get("meta") if isinstance(payload, dict) else None
            next_page = str((meta or {}).get("nextPage") or "")
            if not next_page or next_page == page:
                break
            page = next_page

        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        timestamps = [
            value
            for value in rows_by_time
            if value * 1000 >= start_time_ms
            and datetime.fromtimestamp(value, UTC) <= observed
        ]
        intervals = inferred_funding_intervals(timestamps, interval_hours)
        rows = []
        for timestamp in sorted(timestamps):
            raw = rows_by_time[timestamp]
            actual_interval = intervals[timestamp]
            rate = as_float(raw.get("fundingRate")) / 100.0
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("markPriceTwap")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows


def drift_records(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not bool(payload.get("success")):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Drift {label} unavailable: {detail}")
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Drift {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def velocity_stats_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not bool(payload.get("success")):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Velocity {label} unavailable: {detail}")
    rows = payload.get("markets")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Velocity {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def velocity_canonical_funding_rate(raw: Any) -> float:
    if isinstance(raw, dict):
        long_cashflow = as_float(raw.get("long"))
        if long_cashflow:
            # Velocity stats expose side cashflow in percentage points. Positive
            # long cashflow means longs receive, while the scanner's canonical
            # convention is positive funding means longs pay shorts.
            return -long_cashflow / 100.0
        short_cashflow = as_float(raw.get("short"))
        if short_cashflow:
            return short_cashflow / 100.0
    return as_float(raw) / 100.0


def next_velocity_funding_time(latest_at: datetime, observed: datetime) -> str:
    candidate = latest_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    candidate += timedelta(hours=1)
    while candidate <= observed.astimezone(UTC):
        candidate += timedelta(hours=1)
    return candidate.isoformat()


def integer_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def nested_value(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def drift_dlob_levels(raw_levels: Any) -> list[list[float]]:
    if not isinstance(raw_levels, list):
        return []
    output: list[list[float]] = []
    for raw in raw_levels:
        if isinstance(raw, dict):
            price = as_float(raw.get("price")) / 1_000_000.0
            size = as_float(raw.get("size")) / 1_000_000_000.0
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price = as_float(raw[0])
            size = as_float(raw[1])
        else:
            continue
        if price > 0 and size > 0:
            output.append([price, size])
    return output
