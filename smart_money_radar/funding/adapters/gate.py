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
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


GATE_API_URL = "https://api.gateio.ws"
GATE_SETTLE = "usdt"
GATE_HISTORY_CHUNK_SECONDS = 29 * 24 * 60 * 60


class GateFundingClient:
    venue = "gate"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = GATE_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.06)
        self.base_url = base_url.rstrip("/")
        self._contract_multipliers: dict[str, float] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        contracts = gate_rows(
            self.http.get_json(
                f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/contracts"
            ),
            "contracts",
        )
        tickers = gate_rows(
            self.http.get_json(
                f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/tickers"
            ),
            "tickers",
        )
        ticker_map = {
            str(row.get("contract")): row
            for row in tickers
            if row.get("contract")
        }
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for raw in contracts:
            if not is_supported_gate_contract(raw):
                continue
            symbol = str(raw.get("name") or "")
            base_asset = clean_asset_symbol(symbol.removesuffix("_USDT"))
            multiplier = as_float(raw.get("quanto_multiplier"))
            mark_price = as_float(raw.get("mark_price"))
            index_price = as_float(raw.get("index_price"))
            if (
                not symbol
                or not base_asset
                or multiplier <= 0
                or mark_price <= 0
                or index_price <= 0
            ):
                continue
            ticker = ticker_map.get(symbol, {})
            interval_hours = max(1.0, as_float(raw.get("funding_interval"), 28_800.0) / 3600.0)
            rate = as_float(
                raw.get("funding_rate_indicative"),
                as_float(raw.get("funding_rate")),
            )
            taker_fee = max(0.0, as_float(raw.get("taker_fee_rate"), 0.00075))
            self._contract_multipliers[symbol] = multiplier
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": multiplier,
                    "status": "active",
                    "source_url": f"https://www.gate.com/futures/USDT/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            open_interest_usd = (
                as_float(raw.get("position_size")) * multiplier * mark_price
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
                    "next_funding_at": iso_from_milliseconds(
                        as_float(raw.get("funding_next_apply")) * 1000.0
                    ),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": open_interest_usd or None,
                    "volume_24h_usd": as_float(ticker.get("volume_24h_quote")) or None,
                    "taker_fee_rate": taker_fee,
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "ticker": ticker},
                }
            )
        return instruments, markets, []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "contract": symbol,
                "limit": max(1, min(int(limit), 100)),
                "with_id": "true",
            }
        )
        raw = gate_object(
            self.http.get_json(
                f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/order_book?{query}"
            ),
            f"orderbook for {symbol}",
        )
        multiplier = self._contract_multipliers.get(symbol)
        if multiplier is None:
            raise FundingDataError(f"Gate contract multiplier unavailable for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            gate_contract_levels(raw.get("bids", []), multiplier),
            gate_contract_levels(raw.get("asks", []), multiplier),
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
        contract = gate_object(
            self.http.get_json(
                f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/contracts/"
                f"{urllib.parse.quote(symbol, safe='')}"
            ),
            f"contract for {symbol}",
        )
        if not is_supported_gate_contract(contract):
            raise FundingDataError(f"Gate contract not supported for {symbol}")
        ticker_query = urllib.parse.urlencode({"contract": symbol})
        tickers = gate_rows(
            self.http.get_json(
                f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/tickers?{ticker_query}"
            ),
            f"ticker for {symbol}",
        )
        ticker = tickers[0] if tickers else {}
        multiplier = as_float(contract.get("quanto_multiplier"))
        if multiplier <= 0:
            raise FundingDataError(f"Gate contract multiplier unavailable for {symbol}")
        self._contract_multipliers[symbol] = multiplier
        interval_hours = max(
            1.0,
            as_float(contract.get("funding_interval"), 28_800.0) / 3600.0,
        )
        funding_rate = as_float(
            contract.get("funding_rate_indicative"),
            as_float(contract.get("funding_rate")),
        )
        mark_price = as_float(contract.get("mark_price"))
        index_price = as_float(contract.get("index_price"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Gate reference prices unavailable for {symbol}")
        taker_fee = max(0.0, as_float(contract.get("taker_fee_rate"), 0.00075))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": iso_from_milliseconds(
                as_float(contract.get("funding_next_apply")) * 1000.0
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(contract.get("position_size"))
            * multiplier
            * mark_price
            or None,
            "volume_24h_usd": as_float(ticker.get("volume_24h_quote")) or None,
            "taker_fee_rate": taker_fee,
            "contract_multiplier": multiplier,
            "canonical_unit_multiplier": 1.0,
            "observed_at": observed_at,
            "raw": {"contract": contract, "ticker": ticker},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        end_time = parse_observed_seconds(observed_at)
        cursor = max(1, int(start_time_ms // 1000))
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(8):
            if cursor > end_time:
                break
            chunk_end = min(end_time, cursor + GATE_HISTORY_CHUNK_SECONDS)
            query = urllib.parse.urlencode(
                {
                    "contract": symbol,
                    "from": cursor,
                    "to": chunk_end,
                    "limit": 1000,
                }
            )
            page = gate_rows(
                self.http.get_json(
                    f"{self.base_url}/api/v4/futures/{GATE_SETTLE}/funding_rate?{query}"
                ),
                f"funding history for {symbol}",
            )
            for raw in page:
                timestamp = integer_or_none(raw.get("t"))
                if timestamp is not None:
                    rows_by_time[timestamp] = raw
            cursor = chunk_end + 1

        ordered_times = sorted(rows_by_time)
        rows: list[dict[str, Any]] = []
        for index, timestamp in enumerate(ordered_times):
            previous = ordered_times[index - 1] if index > 0 else None
            actual_interval = interval_between(previous, timestamp, interval_hours)
            raw = rows_by_time[timestamp]
            rate = as_float(raw.get("r"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp * 1000),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]


def gate_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Gate {label} unavailable: {detail}")
    return [row for row in payload if isinstance(row, dict)]


def gate_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or "label" in payload:
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Gate {label} unavailable: {detail}")
    return payload


def is_supported_gate_contract(raw: dict[str, Any]) -> bool:
    return (
        raw.get("status") == "trading"
        and not bool(raw.get("in_delisting"))
        and not bool(raw.get("is_pre_market"))
        and str(raw.get("contract_type") or "") == ""
        and str(raw.get("name") or "").endswith("_USDT")
    )


def gate_contract_levels(raw_levels: Any, multiplier: float) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(raw_levels, list):
        return output
    for raw in raw_levels:
        if not isinstance(raw, dict):
            continue
        price = as_float(raw.get("p"))
        contracts = abs(as_float(raw.get("s")))
        if price > 0 and contracts > 0:
            output.append([price, contracts * multiplier])
    return output


def parse_observed_seconds(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FundingDataError(f"Invalid observed_at for Gate history: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp())


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def interval_between(previous: int | None, current: int, fallback: float) -> float:
    if previous is None:
        return max(1.0, float(fallback or 8.0))
    hours = (current - previous) / 3600.0
    return hours if hours >= 1.0 else max(1.0, float(fallback or 8.0))
