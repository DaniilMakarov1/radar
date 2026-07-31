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
    canonical_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
    parse_timestamp,
)


KUCOIN_FUTURES_URL = "https://api-futures.kucoin.com"


class KuCoinFundingClient:
    venue = "kucoin"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = KUCOIN_FUTURES_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._multipliers: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        payload = self.http.get_json(f"{self.base_url}/api/v1/contracts/active")
        contracts = kucoin_rows(payload, "active contracts")
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        multipliers: dict[str, float] = {}
        for raw in contracts:
            if not is_supported_kucoin_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = canonical_asset_symbol(
                raw.get("displayBaseCurrency") or raw.get("baseCurrency")
            )
            multiplier = as_float(raw.get("multiplier"))
            mark_price = as_float(raw.get("markPrice"))
            index_price = as_float(raw.get("indexPrice"))
            interval_hours = max(
                0.25,
                as_float(
                    raw.get("currentFundingRateGranularity"),
                    as_float(raw.get("fundingRateGranularity"), 28_800_000.0),
                )
                / 3_600_000.0,
            )
            if (
                not symbol
                or not asset
                or multiplier <= 0
                or mark_price <= 0
                or index_price <= 0
            ):
                continue
            funding_rate = as_float(raw.get("fundingFeeRate"))
            maker_fee = max(0.0, as_float(raw.get("makerFeeRate"), 0.0002))
            taker_fee = max(0.0, as_float(raw.get("takerFeeRate"), 0.0006))
            multipliers[symbol] = multiplier
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": multiplier,
                    "status": "active",
                    "source_url": f"https://www.kucoin.com/futures/trade/{symbol}",
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
                    "next_funding_at": iso_from_milliseconds(
                        raw.get("nextFundingRateDateTime")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(raw.get("openInterest"))
                    * multiplier
                    * mark_price or None,
                    "volume_24h_usd": as_float(raw.get("turnoverOf24h")) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_tier",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        self._multipliers = multipliers
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        multiplier = self._multipliers.get(symbol)
        if multiplier is None:
            raise FundingDataError(f"KuCoin contract multiplier unavailable for {symbol}")
        query = urllib.parse.urlencode({"symbol": symbol})
        payload = self.http.get_json(
            f"{self.base_url}/api/v1/level2/depth100?{query}"
        )
        raw = kucoin_object(payload, f"orderbook for {symbol}")
        bids = contract_levels_to_base(raw.get("bids"), multiplier, limit)
        asks = contract_levels_to_base(raw.get("asks"), multiplier, limit)
        return normalize_orderbook(
            self.venue,
            symbol,
            bids,
            asks,
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
        raw = kucoin_object(
            self.http.get_json(
                f"{self.base_url}/api/v1/contracts/"
                f"{urllib.parse.quote(symbol, safe='')}"
            ),
            f"contract for {symbol}",
        )
        if not is_supported_kucoin_contract(raw):
            raise FundingDataError(f"KuCoin contract not supported for {symbol}")
        multiplier = as_float(raw.get("multiplier"))
        mark_price = as_float(raw.get("markPrice"))
        index_price = as_float(raw.get("indexPrice"))
        if multiplier <= 0:
            raise FundingDataError(f"KuCoin contract multiplier unavailable for {symbol}")
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"KuCoin reference prices unavailable for {symbol}")
        self._multipliers[symbol] = multiplier
        interval_hours = max(
            0.25,
            as_float(
                raw.get("currentFundingRateGranularity"),
                as_float(raw.get("fundingRateGranularity"), 28_800_000.0),
            )
            / 3_600_000.0,
        )
        funding_rate = as_float(raw.get("fundingFeeRate"))
        maker_fee = max(0.0, as_float(raw.get("makerFeeRate"), 0.0002))
        taker_fee = max(0.0, as_float(raw.get("takerFeeRate"), 0.0006))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_current_estimate",
            "next_funding_at": iso_from_milliseconds(
                raw.get("nextFundingRateDateTime")
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(raw.get("openInterest"))
            * multiplier
            * mark_price
            or None,
            "volume_24h_usd": as_float(raw.get("turnoverOf24h")) or None,
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
            "fee_source": "venue_public_tier",
            "contract_multiplier": multiplier,
            "canonical_unit_multiplier": 1.0,
            "observed_at": observed_at,
            "raw": raw,
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        cursor_to = int(observed.timestamp() * 1_000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(10):
            query = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "from": int(start_time_ms),
                    "to": cursor_to,
                }
            )
            payload = self.http.get_json(
                f"{self.base_url}/api/v1/contract/funding-rates?{query}"
            )
            page = kucoin_rows(payload, f"funding history for {symbol}")
            timestamps: list[int] = []
            for raw in page:
                timestamp = integer_or_none(raw.get("timepoint"))
                if timestamp is None or timestamp > int(observed.timestamp() * 1_000):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps:
                break
            oldest = min(timestamps)
            if oldest <= start_time_ms or len(page) < 100:
                break
            next_cursor = oldest - 1
            if next_cursor >= cursor_to:
                break
            cursor_to = next_cursor

        eligible_times = [value for value in rows_by_time if value >= start_time_ms]
        intervals = inferred_funding_intervals(
            eligible_times,
            interval_hours,
            units_per_second=1_000.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(eligible_times):
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
        return [row for row in rows if row["funding_at"]]


def kucoin_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"KuCoin {label} unavailable: {detail}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid KuCoin {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def kucoin_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"KuCoin {label} unavailable: {detail}")
    row = payload.get("data")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid KuCoin {label}")
    return row


def is_supported_kucoin_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("status") == "Open"
        and raw.get("expireDate") is None
        and raw.get("quoteCurrency") == "USDT"
        and raw.get("settleCurrency") == "USDT"
        and raw.get("marketType") == "CRYPTO"
        and not bool(raw.get("isInverse"))
    )


def contract_levels_to_base(
    rows: Any,
    multiplier: float,
    limit: int,
) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(rows, list):
        return output
    for row in rows[: max(1, min(int(limit), 100))]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = as_float(row[0])
        contracts = as_float(row[1])
        if price > 0 and contracts > 0:
            output.append([price, contracts * multiplier])
    return output


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
