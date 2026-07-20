from __future__ import annotations

import time
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


MEXC_CONTRACT_URL = "https://contract.mexc.com"


class MEXCFundingClient:
    venue = "mexc"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = MEXC_CONTRACT_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.20)
        self.base_url = base_url.rstrip("/")
        self._multipliers: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        specs = mexc_rows(
            self.http.get_json(f"{self.base_url}/api/v1/contract/detail"),
            "contract details",
        )
        funding_rows = mexc_rows(
            self.http.get_json(f"{self.base_url}/api/v1/contract/funding_rate"),
            "funding rates",
        )
        ticker_rows = mexc_rows(
            self.http.get_json(f"{self.base_url}/api/v1/contract/ticker"),
            "tickers",
        )
        funding_by_symbol = rows_by_symbol(funding_rows)
        ticker_by_symbol = rows_by_symbol(ticker_rows)

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        multipliers: dict[str, float] = {}
        for raw in specs:
            if not is_supported_mexc_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            asset = canonical_asset_symbol(raw.get("baseCoin"))
            current = funding_by_symbol.get(symbol)
            ticker = ticker_by_symbol.get(symbol, {})
            multiplier = as_float(raw.get("contractSize"))
            if not symbol or not asset or not current or multiplier <= 0:
                continue
            mark_price = as_float(
                current.get("fairPrice"),
                as_float(ticker.get("fairPrice")),
            )
            index_price = as_float(
                current.get("idxPrice"),
                as_float(ticker.get("indexPrice")),
            )
            if mark_price <= 0 or index_price <= 0:
                continue
            interval_hours = max(0.25, as_float(current.get("collectCycle"), 8.0))
            funding_rate = as_float(current.get("fundingRate"))
            maker_fee = max(0.0, as_float(raw.get("makerFeeRate"), 0.0))
            taker_fee = max(0.0, as_float(raw.get("takerFeeRate"), 0.0002))
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
                    "source_url": f"https://futures.mexc.com/exchange/{symbol}",
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
                        current.get("nextSettleTime")
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": (
                        as_float(ticker.get("holdVol")) * multiplier * mark_price
                        or None
                    ),
                    "volume_24h_usd": as_float(ticker.get("amount24")) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_contract_tier",
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "funding": current, "ticker": ticker},
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
            raise FundingDataError(f"MEXC contract multiplier unavailable for {symbol}")
        query = urllib.parse.urlencode({"limit": max(5, min(int(limit), 100))})
        raw = mexc_object_with_rate_limit_retry(
            self.http,
            f"{self.base_url}/api/v1/contract/depth/{symbol}?{query}",
            f"orderbook for {symbol}",
        )
        return normalize_orderbook(
            self.venue,
            symbol,
            contract_levels_to_base(raw.get("bids"), multiplier),
            contract_levels_to_base(raw.get("asks"), multiplier),
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
        current = mexc_object(
            self.http.get_json(
                f"{self.base_url}/api/v1/contract/funding_rate/"
                f"{urllib.parse.quote(symbol)}"
            ),
            f"funding rate for {symbol}",
        )
        ticker_query = urllib.parse.urlencode({"symbol": symbol})
        ticker = mexc_object(
            self.http.get_json(
                f"{self.base_url}/api/v1/contract/ticker?{ticker_query}"
            ),
            f"ticker for {symbol}",
        )
        previous = previous_market or {}
        instrument_raw = previous.get("instrument_raw") or {}
        multiplier = as_float(
            previous.get("contract_multiplier"),
            as_float(instrument_raw.get("contractSize"), 1.0),
        )
        if multiplier > 0:
            self._multipliers[symbol] = multiplier
        mark_price = as_float(
            current.get("fairPrice"),
            as_float(ticker.get("fairPrice")),
        )
        index_price = as_float(
            current.get("idxPrice"),
            as_float(ticker.get("indexPrice")),
        )
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"MEXC reference prices unavailable for {symbol}")
        interval_hours = max(
            0.25,
            as_float(
                current.get("collectCycle"),
                as_float(previous.get("funding_interval_hours"), 8.0),
            ),
        )
        funding_rate = as_float(current.get("fundingRate"))
        maker_fee = max(0.0, as_float(instrument_raw.get("makerFeeRate"), 0.0))
        taker_fee = max(0.0, as_float(instrument_raw.get("takerFeeRate"), 0.0002))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_current_estimate",
            "next_funding_at": iso_from_milliseconds(current.get("nextSettleTime")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": (
                as_float(ticker.get("holdVol")) * max(multiplier, 1.0) * mark_price
                or None
            ),
            "volume_24h_usd": as_float(ticker.get("amount24")) or None,
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
            "fee_source": "cached_contract_tier_plus_live_mexc_snapshot",
            "observed_at": observed_at,
            "raw": {"funding": current, "ticker": ticker},
            "contract_multiplier": multiplier,
            "canonical_unit_multiplier": previous.get(
                "canonical_unit_multiplier",
                1.0,
            ),
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
        page_number = 1
        for _ in range(10):
            query = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "page_num": page_number,
                    "page_size": 1_000,
                }
            )
            raw_page = mexc_object_with_rate_limit_retry(
                self.http,
                f"{self.base_url}/api/v1/contract/funding_rate/history?{query}",
                f"funding history for {symbol}",
            )
            page = raw_page.get("resultList")
            if not isinstance(page, list):
                raise FundingDataError(f"Invalid MEXC funding history for {symbol}")
            timestamps: list[int] = []
            for raw in page:
                if not isinstance(raw, dict):
                    continue
                timestamp = integer_or_none(raw.get("settleTime"))
                if timestamp is None or timestamp > int(observed.timestamp() * 1_000):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if (
                not timestamps
                or min(timestamps) <= start_time_ms
                or page_number >= integer_or_none(raw_page.get("totalPage"), page_number)
            ):
                break
            page_number += 1

        eligible_times = [value for value in rows_by_time if value >= start_time_ms]
        inferred = inferred_funding_intervals(
            eligible_times,
            interval_hours,
            units_per_second=1_000.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(eligible_times):
            raw = rows_by_time[timestamp]
            actual_interval = max(
                0.25,
                as_float(raw.get("collectCycle"), inferred[timestamp]),
            )
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


def mexc_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    data = mexc_data(payload, label)
    if not isinstance(data, list):
        raise FundingDataError(f"Invalid MEXC {label} rows")
    return [row for row in data if isinstance(row, dict)]


def mexc_object(payload: Any, label: str) -> dict[str, Any]:
    data = mexc_data(payload, label)
    if not isinstance(data, dict):
        raise FundingDataError(f"Invalid MEXC {label}")
    return data


def mexc_object_with_rate_limit_retry(
    http: FundingHttpClient,
    url: str,
    label: str,
) -> dict[str, Any]:
    for attempt in range(4):
        payload = http.get_json(url)
        try:
            return mexc_object(payload, label)
        except FundingDataError as exc:
            if "too frequent" not in str(exc).lower() or attempt >= 3:
                raise
            time.sleep(0.75 * (attempt + 1))
    raise FundingDataError(f"MEXC {label} unavailable after rate-limit retries")


def mexc_data(payload: Any, label: str) -> Any:
    if not isinstance(payload, dict) or not bool(payload.get("success")):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"MEXC {label} unavailable: {detail}")
    return payload.get("data")


def rows_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("symbol")): row
        for row in rows
        if row.get("symbol")
    }


def is_supported_mexc_contract(raw: dict[str, Any]) -> bool:
    tags = " ".join(str(value).lower() for value in raw.get("conceptPlate", []))
    return (
        integer_or_none(raw.get("state"), -1) == 0
        and integer_or_none(raw.get("futureType"), -1) == 1
        and raw.get("quoteCoin") == "USDT"
        and raw.get("settleCoin") == "USDT"
        and not bool(raw.get("isHidden"))
        and not bool(raw.get("preMarket"))
        and "stock" not in tags
        and "tradfi" not in tags
    )


def contract_levels_to_base(rows: Any, multiplier: float) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = as_float(row[0])
        contracts = as_float(row[1])
        if price > 0 and contracts > 0:
            output.append([price, contracts * multiplier])
    return output


def integer_or_none(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
