from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
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
    parse_timestamp,
)


DERIBIT_API_URL = "https://www.deribit.com/api/v2"
DERIBIT_LINEAR_CURRENCY = "USDC"
DERIBIT_HISTORY_CHUNK_MS = 30 * 24 * 60 * 60 * 1000


class DeribitFundingClient:
    venue = "deribit"
    live_history_enabled = False

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = DERIBIT_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.06)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        query = urllib.parse.urlencode(
            {
                "currency": DERIBIT_LINEAR_CURRENCY,
                "kind": "future",
                "expired": "false",
            }
        )
        instruments_raw = deribit_result_rows(
            self.http.get_json(f"{self.base_url}/public/get_instruments?{query}"),
            "instruments",
        )
        supported = [
            row for row in instruments_raw if is_supported_deribit_linear_perp(row)
        ]
        tickers = self._tickers([str(row.get("instrument_name")) for row in supported])
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in supported:
            symbol = str(raw.get("instrument_name") or "")
            ticker = tickers.get(symbol)
            if not ticker or str(ticker.get("state") or "") != "open":
                continue
            base_asset = canonical_asset_symbol(raw.get("base_currency"))
            mark_price = as_float(ticker.get("mark_price"))
            index_price = as_float(ticker.get("index_price"))
            if not symbol or not base_asset or mark_price <= 0 or index_price <= 0:
                continue
            funding_8h = as_float(ticker.get("funding_8h"))
            maker_fee = as_float(raw.get("maker_commission"), 0.0)
            taker_fee = as_float(raw.get("taker_commission"), 0.0005)
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": as_float(raw.get("contract_size"), 1.0)
                    or 1.0,
                    "status": "active",
                    "source_url": f"https://www.deribit.com/futures/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": funding_8h,
                    "funding_interval_hours": 8.0,
                    "hourly_funding_rate": funding_8h / 8.0,
                    "funding_rate_kind": "published_8h_estimate",
                    "next_funding_at": next_deribit_eight_hour(observed_at),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("open_interest")) * mark_price
                    or None,
                    "volume_24h_usd": as_float(
                        (ticker.get("stats") or {}).get("volume_usd")
                        if isinstance(ticker.get("stats"), dict)
                        else None
                    )
                    or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_instrument",
                    "observed_at": observed_at,
                    "raw": {"instrument": raw, "ticker": ticker},
                }
            )
        return instruments, markets, []

    def _tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}

        def load(symbol: str) -> dict[str, Any]:
            query = urllib.parse.urlencode({"instrument_name": symbol})
            return deribit_result_object(
                self.http.get_json(f"{self.base_url}/public/ticker?{query}"),
                f"ticker for {symbol}",
            )

        with ThreadPoolExecutor(max_workers=min(8, len(symbols) or 1)) as executor:
            future_symbols = {
                executor.submit(load, symbol): symbol
                for symbol in symbols
                if symbol
            }
            for future in as_completed(future_symbols):
                symbol = future_symbols[future]
                try:
                    output[symbol] = future.result()
                except FundingDataError:
                    continue
        return output

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "instrument_name": symbol,
                "depth": max(1, min(int(limit), 1000)),
            }
        )
        raw = deribit_result_object(
            self.http.get_json(f"{self.base_url}/public/get_order_book?{query}"),
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

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"instrument_name": symbol})
        ticker = deribit_result_object(
            self.http.get_json(f"{self.base_url}/public/ticker?{query}"),
            f"ticker for {symbol}",
        )
        if str(ticker.get("state") or "") != "open":
            raise FundingDataError(f"Deribit ticker not open for {symbol}")
        previous = previous_market or {}
        previous_raw = (
            previous.get("raw") if isinstance(previous.get("raw"), dict) else {}
        )
        instrument = (
            previous_raw.get("instrument")
            if isinstance(previous_raw.get("instrument"), dict)
            else {}
        )
        base_asset = canonical_asset_symbol(
            instrument.get("base_currency")
            or ticker.get("base_currency")
            or canonical_asset
        )
        if base_asset != canonical_asset_symbol(canonical_asset):
            raise FundingDataError(f"Deribit symbol {symbol} does not match {canonical_asset}")
        mark_price = as_float(ticker.get("mark_price"))
        index_price = as_float(ticker.get("index_price"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Deribit reference prices unavailable for {symbol}")
        funding_8h = as_float(ticker.get("funding_8h"))
        maker_fee = as_float(instrument.get("maker_commission"), 0.0)
        taker_fee = as_float(instrument.get("taker_commission"), 0.0005)
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": base_asset,
            "funding_rate": funding_8h,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": funding_8h / 8.0,
            "funding_rate_kind": "published_8h_estimate",
            "next_funding_at": next_deribit_eight_hour(observed_at),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("open_interest")) * mark_price
            or None,
            "volume_24h_usd": as_float(
                (ticker.get("stats") or {}).get("volume_usd")
                if isinstance(ticker.get("stats"), dict)
                else None
            )
            or None,
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
            "fee_source": "venue_public_instrument",
            "contract_multiplier": as_float(
                instrument.get("contract_size"),
                previous.get("contract_multiplier") or 1.0,
            )
            or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": {"instrument": instrument, "ticker": ticker},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        end_ms = int(observed.timestamp() * 1000)
        cursor = max(0, int(start_time_ms))
        rows_by_time: dict[int, dict[str, Any]] = {}
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + DERIBIT_HISTORY_CHUNK_MS)
            query = urllib.parse.urlencode(
                {
                    "instrument_name": symbol,
                    "start_timestamp": cursor,
                    "end_timestamp": chunk_end,
                }
            )
            page = deribit_result_rows(
                self.http.get_json(
                    f"{self.base_url}/public/get_funding_rate_history?{query}"
                ),
                f"funding history for {symbol}",
            )
            for raw in page:
                timestamp = integer_or_none(raw.get("timestamp"))
                if timestamp is not None and timestamp >= start_time_ms:
                    rows_by_time[timestamp] = raw
            if chunk_end >= end_ms:
                break
            cursor = chunk_end + 1

        rows: list[dict[str, Any]] = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            rate_8h = as_float(raw.get("interest_8h"))
            rate_1h = as_float(raw.get("interest_1h"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate_8h,
                    "funding_interval_hours": 8.0,
                    "hourly_funding_rate": rate_1h,
                    "mark_price": as_float(raw.get("index_price")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]


def deribit_result_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    result = deribit_result(payload, label)
    if not isinstance(result, list):
        raise FundingDataError(f"Invalid Deribit {label} rows")
    return [row for row in result if isinstance(row, dict)]


def deribit_result_object(payload: Any, label: str) -> dict[str, Any]:
    result = deribit_result(payload, label)
    if not isinstance(result, dict):
        raise FundingDataError(f"Invalid Deribit {label}")
    return result


def deribit_result(payload: Any, label: str) -> Any:
    if not isinstance(payload, dict) or "error" in payload:
        detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Deribit {label} unavailable: {detail}")
    if "result" not in payload:
        raise FundingDataError(f"Invalid Deribit {label} response")
    return payload["result"]


def is_supported_deribit_linear_perp(row: dict[str, Any]) -> bool:
    return (
        bool(row.get("is_active"))
        and str(row.get("state") or "") == "open"
        and str(row.get("settlement_period") or "") == "perpetual"
        and str(row.get("instrument_type") or "") == "linear"
        and str(row.get("future_type") or "") == "linear"
        and str(row.get("quote_currency") or "").upper() == "USDC"
        and str(row.get("settlement_currency") or "").upper() == "USDC"
    )


def next_deribit_eight_hour(observed_at: str) -> str:
    current = parse_timestamp(observed_at) or datetime.now(UTC)
    current = current.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hour = ((current.hour // 8) + 1) * 8
    if hour >= 24:
        return (current.replace(hour=0) + timedelta(days=1)).isoformat()
    return current.replace(hour=hour).isoformat()


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
