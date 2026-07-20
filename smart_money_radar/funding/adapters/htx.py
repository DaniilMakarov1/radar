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


HTX_SWAP_URL = "https://api.hbdm.com"


class HTXFundingClient:
    venue = "htx"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = HTX_SWAP_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.10)
        self.base_url = base_url.rstrip("/")
        self._multipliers: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        contracts = htx_rows(
            self.http.get_json(f"{self.base_url}/linear-swap-api/v1/swap_contract_info"),
            "contract info",
        )
        funding_rows = htx_rows(
            self.http.get_json(f"{self.base_url}/linear-swap-api/v1/swap_batch_funding_rate"),
            "batch funding",
        )
        ticker_rows = htx_ticks(
            self.http.get_json(f"{self.base_url}/linear-swap-ex/market/detail/batch_merged"),
            "batch merged ticker",
        )
        index_rows = htx_rows(
            self.http.get_json(f"{self.base_url}/linear-swap-api/v1/swap_index"),
            "index prices",
        )
        funding_by_symbol = rows_by_contract_code(funding_rows)
        ticker_by_symbol = rows_by_contract_code(ticker_rows)
        index_by_symbol = rows_by_contract_code(index_rows)
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        multipliers: dict[str, float] = {}
        for raw in contracts:
            if not is_supported_htx_contract(raw):
                continue
            symbol = str(raw.get("contract_code") or "")
            asset = canonical_asset_symbol(raw.get("symbol"))
            multiplier = as_float(raw.get("contract_size"))
            if not symbol or not asset or multiplier <= 0:
                continue
            funding = funding_by_symbol.get(symbol)
            ticker = ticker_by_symbol.get(symbol)
            index = index_by_symbol.get(symbol, {})
            if not funding or not ticker:
                continue
            funding_time = integer_or_none(funding.get("funding_time"))
            next_funding_time = integer_or_none(funding.get("next_funding_time"), funding_time)
            next_funding_at = iso_from_milliseconds(next_funding_time)
            interval_hours = max(1.0, as_float(raw.get("settlement_period"), 8.0))
            funding_rate = as_float(
                funding.get("estimated_rate"),
                as_float(funding.get("funding_rate")),
            )
            mark_price = htx_ticker_mid_or_close(ticker)
            index_price = as_float(index.get("index_price"), mark_price)
            if mark_price <= 0 or index_price <= 0:
                continue
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
                    "source_url": f"https://www.htx.com/futures/linear_swap/exchange/#contract_code={symbol}",
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
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": 0.0002,
                    "taker_fee_rate": 0.0005,
                    "fee_source": "public_default",
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "funding": funding, "ticker": ticker, "index": index},
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
        multiplier = self._multipliers.get(symbol, 1.0)
        query = urllib.parse.urlencode({"contract_code": symbol, "type": "step0"})
        raw = htx_tick(
            self.http.get_json(f"{self.base_url}/linear-swap-ex/market/depth?{query}"),
            f"orderbook for {symbol}",
        )
        return normalize_orderbook(
            self.venue,
            symbol,
            contract_levels_to_base(raw.get("bids"), multiplier, limit),
            contract_levels_to_base(raw.get("asks"), multiplier, limit),
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
        rows_by_time: dict[int, dict[str, Any]] = {}
        for page_number in range(1, 21):
            query = urllib.parse.urlencode(
                {
                    "contract_code": symbol,
                    "page_index": page_number,
                    "page_size": 50,
                }
            )
            payload = htx_object(
                self.http.get_json(
                    f"{self.base_url}/linear-swap-api/v1/swap_historical_funding_rate?{query}"
                ),
                f"funding history for {symbol}",
            )
            page = payload.get("data")
            if not isinstance(page, list):
                raise FundingDataError(f"Invalid HTX funding history for {symbol}")
            timestamps: list[int] = []
            for raw in page:
                if not isinstance(raw, dict):
                    continue
                timestamp = integer_or_none(raw.get("funding_time"))
                if timestamp is None or timestamp > int(observed.timestamp() * 1_000):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or min(timestamps) <= start_time_ms or len(page) < 50:
                break
            time.sleep(0.02)
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
            rate = as_float(raw.get("funding_rate"))
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


def htx_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    data = htx_data(payload, label)
    if not isinstance(data, list):
        raise FundingDataError(f"Invalid HTX {label} rows")
    return [row for row in data if isinstance(row, dict)]


def htx_object(payload: Any, label: str) -> dict[str, Any]:
    data = htx_data(payload, label)
    if not isinstance(data, dict):
        raise FundingDataError(f"Invalid HTX {label}")
    return data


def htx_tick(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("status")) != "ok":
        detail = payload.get("err_msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"HTX {label} unavailable: {detail}")
    row = payload.get("tick")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid HTX {label}")
    return row


def htx_ticks(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("status")) != "ok":
        detail = payload.get("err_msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"HTX {label} unavailable: {detail}")
    rows = payload.get("ticks")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid HTX {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def htx_data(payload: Any, label: str) -> Any:
    if not isinstance(payload, dict) or str(payload.get("status")) != "ok":
        detail = payload.get("err_msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"HTX {label} unavailable: {detail}")
    return payload.get("data")


def rows_by_contract_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("contract_code")): row
        for row in rows
        if row.get("contract_code")
    }


def is_supported_htx_contract(raw: dict[str, Any]) -> bool:
    return (
        integer_or_none(raw.get("contract_status"), 0) == 1
        and str(raw.get("trade_partition") or "").upper() == "USDT"
        and str(raw.get("contract_type") or "").lower() == "swap"
    )


def htx_ticker_mid_or_close(raw: dict[str, Any]) -> float:
    bid = raw.get("bid")
    ask = raw.get("ask")
    if isinstance(bid, (list, tuple)) and isinstance(ask, (list, tuple)):
        bid_price = as_float(bid[0] if bid else None)
        ask_price = as_float(ask[0] if ask else None)
        if bid_price > 0 and ask_price > 0:
            return (bid_price + ask_price) / 2.0
    return as_float(raw.get("close"))


def contract_levels_to_base(rows: Any, multiplier: float, limit: int) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(rows, list):
        return output
    for row in rows[: max(1, int(limit))]:
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
