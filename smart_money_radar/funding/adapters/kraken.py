from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    canonical_asset_symbol,
    normalize_orderbook,
    parse_timestamp,
)


KRAKEN_FUTURES_URL = "https://futures.kraken.com/derivatives/api/v3"


class KrakenFundingClient:
    venue = "kraken"
    live_history_enabled = False

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = KRAKEN_FUTURES_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        instruments_raw = kraken_rows(
            self.http.get_json(f"{self.base_url}/instruments"),
            "instruments",
            "instruments",
        )
        tickers = kraken_rows(
            self.http.get_json(f"{self.base_url}/tickers"),
            "tickers",
            "tickers",
        )
        instrument_map = {
            str(row.get("symbol") or ""): row
            for row in instruments_raw
            if isinstance(row, dict) and row.get("symbol")
        }
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for ticker in tickers:
            if not is_supported_kraken_ticker(ticker):
                continue
            symbol = str(ticker.get("symbol") or "")
            instrument = instrument_map.get(symbol, {})
            if instrument and not is_supported_kraken_instrument(instrument):
                continue
            base_asset = canonical_asset_symbol(
                instrument.get("base") or kraken_pair_base(ticker.get("pair"))
            )
            mark_price = as_float(ticker.get("markPrice"))
            index_price = as_float(ticker.get("indexPrice"))
            if not symbol or not base_asset or mark_price <= 0 or index_price <= 0:
                continue
            rate = kraken_relative_funding_rate(
                ticker,
                mark_price=mark_price,
                index_price=index_price,
            )
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USD",
                    "collateral_asset": "USD",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": as_float(
                        instrument.get("contractSize"),
                        1.0,
                    )
                    or 1.0,
                    "status": "active",
                    "source_url": f"https://futures.kraken.com/trade/{symbol.lower()}",
                    "observed_at": observed_at,
                    "raw": instrument or ticker,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": rate,
                    "funding_rate_kind": "published_next_hour_prediction",
                    "next_funding_at": next_utc_hour(observed_at),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": (
                        as_float(ticker.get("openInterest")) * mark_price
                    )
                    or None,
                    "volume_24h_usd": as_float(ticker.get("volumeQuote")) or None,
                    "maker_fee_rate": 0.0002,
                    "taker_fee_rate": 0.0005,
                    "fee_source": "venue_public_default",
                    "observed_at": observed_at,
                    "raw": ticker,
                }
            )
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"symbol": symbol})
        raw = kraken_object(
            self.http.get_json(f"{self.base_url}/orderbook?{query}"),
            "orderBook",
            f"orderbook for {symbol}",
        )
        return normalize_orderbook(
            self.venue,
            symbol,
            kraken_levels(raw.get("bids"), limit, reverse=True),
            kraken_levels(raw.get("asks"), limit, reverse=False),
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
        tickers = kraken_rows(
            self.http.get_json(f"{self.base_url}/tickers"),
            "tickers",
            f"ticker for {symbol}",
        )
        ticker = next(
            (
                row
                for row in tickers
                if str(row.get("symbol") or "").upper() == str(symbol or "").upper()
            ),
            None,
        )
        if not ticker or not is_supported_kraken_ticker(ticker):
            raise FundingDataError(f"Kraken ticker unavailable for {symbol}")
        base_asset = canonical_asset_symbol(
            ticker.get("base") or kraken_pair_base(ticker.get("pair"))
        )
        if base_asset != canonical_asset_symbol(canonical_asset):
            raise FundingDataError(f"Kraken symbol {symbol} does not match {canonical_asset}")
        mark_price = as_float(ticker.get("markPrice"))
        index_price = as_float(ticker.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Kraken reference prices unavailable for {symbol}")
        rate = kraken_relative_funding_rate(
            ticker,
            mark_price=mark_price,
            index_price=index_price,
        )
        previous = previous_market or {}
        return {
            "venue": self.venue,
            "symbol": str(ticker.get("symbol") or symbol),
            "canonical_asset": base_asset,
            "funding_rate": rate,
            "funding_interval_hours": 1.0,
            "hourly_funding_rate": rate,
            "funding_rate_kind": "published_next_hour_prediction",
            "next_funding_at": next_utc_hour(observed_at),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("openInterest")) * mark_price
            or None,
            "volume_24h_usd": as_float(ticker.get("volumeQuote")) or None,
            "maker_fee_rate": previous.get("maker_fee_rate", 0.0002),
            "taker_fee_rate": previous.get("taker_fee_rate", 0.0005),
            "fee_source": previous.get("fee_source", "venue_public_default"),
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": ticker,
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"symbol": symbol})
        page = kraken_rows(
            self.http.get_json(
                f"{self.base_url}/historical-funding-rates?{query}"
            ),
            "rates",
            f"funding history for {symbol}",
        )
        start = datetime.fromtimestamp(start_time_ms / 1000.0, tz=UTC)
        rows: list[dict[str, Any]] = []
        for raw in page:
            timestamp = parse_timestamp(raw.get("timestamp"))
            if timestamp is None or timestamp < start:
                continue
            rate = as_float(raw.get("relativeFundingRate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": timestamp.isoformat(),
                    "funding_rate": rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": rate,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows


def kraken_rows(payload: Any, key: str, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Kraken {label} unavailable: {detail}")
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid Kraken {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def kraken_object(payload: Any, key: str, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Kraken {label} unavailable: {detail}")
    row = payload.get(key)
    if not isinstance(row, dict):
        raise FundingDataError(f"Invalid Kraken {label}")
    return row


def is_supported_kraken_ticker(row: dict[str, Any]) -> bool:
    return (
        str(row.get("tag") or "").lower() == "perpetual"
        and str(row.get("symbol") or "").upper().startswith("PF_")
        and not bool(row.get("suspended"))
        and not bool(row.get("postOnly"))
    )


def is_supported_kraken_instrument(row: dict[str, Any]) -> bool:
    return (
        bool(row.get("tradeable"))
        and str(row.get("type") or "") == "flexible_futures"
        and str(row.get("quote") or "").upper() == "USD"
    )


def kraken_pair_base(value: Any) -> str:
    raw = str(value or "").split(":", 1)[0]
    return raw.strip().upper()


def kraken_relative_funding_rate(
    ticker: dict[str, Any],
    *,
    mark_price: float,
    index_price: float,
) -> float:
    relative = as_float(
        ticker.get(
            "relativeFundingRatePrediction",
            ticker.get("relativeFundingRate"),
        )
    )
    if relative:
        return relative
    absolute = as_float(
        ticker.get("fundingRatePrediction"),
        as_float(ticker.get("fundingRate")),
    )
    reference = mark_price if mark_price > 0 else index_price
    if absolute and reference > 0:
        return absolute / reference
    return 0.0


def kraken_levels(
    rows: Any,
    limit: int,
    *,
    reverse: bool,
) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = as_float(row[0])
        size = as_float(row[1])
        if price > 0 and size > 0:
            output.append([price, size])
    output.sort(key=lambda item: item[0], reverse=reverse)
    return output[: max(1, min(int(limit), 500))]


def next_utc_hour(observed_at: str) -> str:
    current = parse_timestamp(observed_at) or datetime.now(UTC)
    return (
        current.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()
