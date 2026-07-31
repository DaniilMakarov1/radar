from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


BITGET_API_URL = "https://api.bitget.com"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"


class BitgetFundingClient:
    venue = "bitget"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = BITGET_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.06)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        product_query = urllib.parse.urlencode({"productType": BITGET_PRODUCT_TYPE})
        contracts = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/contracts?{product_query}"
            ),
            "contracts",
        )
        tickers = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/tickers?{product_query}"
            ),
            "tickers",
        )
        funding_rows = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/current-fund-rate?{product_query}"
            ),
            "current funding rates",
        )
        ticker_map = rows_by_symbol(tickers)
        funding_map = rows_by_symbol(funding_rows)

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in contracts:
            if not is_supported_bitget_contract(raw):
                continue
            symbol = str(raw.get("symbol") or "")
            base_asset = clean_asset_symbol(raw.get("baseCoin"))
            ticker = ticker_map.get(symbol)
            funding = funding_map.get(symbol)
            if not symbol or not base_asset or not ticker or not funding:
                continue
            interval_hours = max(
                1.0,
                as_float(
                    funding.get("fundingRateInterval"),
                    as_float(raw.get("fundInterval"), 8.0),
                ),
            )
            rate = as_float(funding.get("fundingRate"))
            mark_price = as_float(ticker.get("markPrice"))
            index_price = as_float(ticker.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue
            taker_fee = max(0.0, as_float(raw.get("takerFeeRate"), 0.0006))
            open_interest_usd = as_float(ticker.get("holdingAmount")) * mark_price
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://www.bitget.com/futures/usdt/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": rate / interval_hours,
                    "funding_rate_kind": "published_next_estimate",
                    "next_funding_at": iso_from_milliseconds(funding.get("nextUpdate")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": open_interest_usd or None,
                    "volume_24h_usd": as_float(ticker.get("quoteVolume")) or None,
                    "taker_fee_rate": taker_fee,
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "ticker": ticker, "funding": funding},
                }
            )
        return instruments, markets, []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "productType": BITGET_PRODUCT_TYPE,
                "precision": "scale0",
                "limit": max(1, min(int(limit), 150)),
            }
        )
        payload = self.http.get_json(
            f"{self.base_url}/api/v2/mix/market/merge-depth?{query}"
        )
        raw = bitget_object(payload, f"orderbook for {symbol}")
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
        product_query = urllib.parse.urlencode(
            {"productType": BITGET_PRODUCT_TYPE, "symbol": symbol}
        )
        contracts = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/contracts?{product_query}"
            ),
            f"contract for {symbol}",
        )
        contract = contracts[0] if contracts else {}
        if not contract or not is_supported_bitget_contract(contract):
            raise FundingDataError(f"Bitget contract not supported for {symbol}")
        tickers = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/ticker?{product_query}"
            ),
            f"ticker for {symbol}",
        )
        ticker = tickers[0] if tickers else {}
        funding_rows = bitget_rows(
            self.http.get_json(
                f"{self.base_url}/api/v2/mix/market/current-fund-rate?{product_query}"
            ),
            f"funding rate for {symbol}",
        )
        funding = funding_rows[0] if funding_rows else {}
        if not ticker or not funding:
            raise FundingDataError(f"Bitget focused market data unavailable for {symbol}")
        previous = previous_market or {}
        interval_hours = max(
            1.0,
            as_float(
                funding.get("fundingRateInterval"),
                as_float(contract.get("fundInterval"), previous.get("funding_interval_hours") or 8.0),
            ),
        )
        funding_rate = as_float(funding.get("fundingRate"))
        mark_price = as_float(
            ticker.get("markPrice"),
            as_float(ticker.get("lastPr"), as_float(previous.get("mark_price"))),
        )
        index_price = as_float(
            ticker.get("indexPrice"),
            as_float(previous.get("index_price"), mark_price),
        )
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Bitget reference prices unavailable for {symbol}")
        taker_fee = max(0.0, as_float(contract.get("takerFeeRate"), 0.0006))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(funding.get("nextUpdate")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("holdingAmount")) * mark_price
            or None,
            "volume_24h_usd": as_float(ticker.get("quoteVolume")) or None,
            "taker_fee_rate": taker_fee,
            "contract_multiplier": 1.0,
            "canonical_unit_multiplier": 1.0,
            "observed_at": observed_at,
            "raw": {"contract": contract, "ticker": ticker, "funding": funding},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        rows_by_time: dict[int, dict[str, Any]] = {}
        for page_number in range(1, 51):
            query = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "productType": BITGET_PRODUCT_TYPE,
                    "pageSize": 100,
                    "pageNo": page_number,
                }
            )
            page = bitget_rows(
                self.http.get_json(
                    f"{self.base_url}/api/v2/mix/market/history-fund-rate?{query}"
                ),
                f"funding history for {symbol}",
            )
            timestamps = []
            for raw in page:
                timestamp = integer_or_none(raw.get("fundingTime"))
                if timestamp is None:
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or min(timestamps) < start_time_ms or len(page) < 100:
                break

        ordered_times = sorted(rows_by_time)
        rows: list[dict[str, Any]] = []
        for index, timestamp in enumerate(ordered_times):
            if timestamp < start_time_ms:
                continue
            previous = ordered_times[index - 1] if index > 0 else None
            actual_interval = interval_between(previous, timestamp, interval_hours)
            raw = rows_by_time[timestamp]
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


def bitget_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Bitget {label} unavailable: {detail}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Bitget {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def bitget_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Bitget {label} unavailable: {detail}")
    row = payload.get("data")
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid Bitget {label}")
    return row


def rows_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("symbol")): row
        for row in rows
        if row.get("symbol")
    }


def is_supported_bitget_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("symbolStatus") == "normal"
        and raw.get("symbolType") == "perpetual"
        and raw.get("quoteCoin") == "USDT"
        and raw.get("isRwa") != "YES"
    )


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def interval_between(previous: int | None, current: int, fallback: float) -> float:
    if previous is None:
        return max(1.0, float(fallback or 8.0))
    hours = (current - previous) / 3_600_000.0
    return hours if hours >= 1.0 else max(1.0, float(fallback or 8.0))
