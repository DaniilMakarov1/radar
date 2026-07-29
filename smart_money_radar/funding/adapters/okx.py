from __future__ import annotations

import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import websocket

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
    endpoint_identity_fields,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


# The canonical www host may return a regional 403 while this official API host remains
# available. The base URL can still be replaced in tests or by a future regional config.
OKX_API_URL = "https://app.okx.com"
OKX_PUBLIC_WEBSOCKET_URL = "wss://ws.okx.com/ws/v5/public"


class OKXFundingClient:
    venue = "okx"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = OKX_API_URL,
        use_websocket: bool | None = None,
        websocket_url: str = OKX_PUBLIC_WEBSOCKET_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.11)
        self.base_url = base_url.rstrip("/")
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.base_url,
            requested_environment="mainnet",
        )
        self.use_websocket = http is None if use_websocket is None else use_websocket
        self.websocket_url = websocket_url
        self._contract_multipliers: dict[str, float] = {}
        self.non_crypto_assets: set[str] = set()

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        instruments_raw = okx_rows(
            self.http.get_json(f"{self.base_url}/api/v5/public/instruments?instType=SWAP"),
            "instruments",
        )
        tickers = okx_rows(
            self.http.get_json(f"{self.base_url}/api/v5/market/tickers?instType=SWAP"),
            "tickers",
        )
        marks = okx_rows(
            self.http.get_json(f"{self.base_url}/api/v5/public/mark-price?instType=SWAP"),
            "mark prices",
        )
        indexes = okx_rows(
            self.http.get_json(f"{self.base_url}/api/v5/market/index-tickers?quoteCcy=USDT"),
            "index prices",
        )
        ticker_map = rows_by_key(tickers, "instId")
        mark_map = rows_by_key(marks, "instId")
        index_map = rows_by_key(indexes, "instId")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        observed = parse_observed_at(observed_at)
        self.non_crypto_assets = {
            asset
            for asset in (
                okx_base_asset(raw)
                for raw in instruments_raw
                if str(raw.get("instCategory") or "") == "3"
            )
            if asset
        }
        supported = [
            raw for raw in instruments_raw if is_supported_okx_swap(raw, observed)
        ]
        funding_map, funding_failures, funding_warnings = self._current_funding_rates(
            [str(raw.get("instId") or "") for raw in supported]
        )
        for raw in supported:
            symbol = str(raw.get("instId") or "")
            underlying = str(raw.get("instFamily") or raw.get("uly") or "")
            base_asset = clean_asset_symbol(underlying.removesuffix("-USDT"))
            ticker = ticker_map.get(symbol)
            mark = mark_map.get(symbol)
            index = index_map.get(underlying)
            multiplier = okx_contract_multiplier(raw, base_asset)
            if not symbol or not base_asset or not ticker or not mark or not index or multiplier <= 0:
                continue
            funding = funding_map.get(symbol)
            if funding is None:
                continue

            funding_time = integer_or_none(funding.get("fundingTime"))
            previous_time = integer_or_none(funding.get("prevFundingTime"))
            interval_hours = interval_between(previous_time, funding_time, 8.0)
            funding_rate = as_float(funding.get("fundingRate"))
            mark_price = as_float(mark.get("markPx"))
            index_price = as_float(index.get("idxPx"))
            last_price = as_float(ticker.get("last"))
            if mark_price <= 0 or index_price <= 0 or last_price <= 0:
                continue
            rules = okx_market_rules(raw, mark_price)
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
                    "contract_kind": "linear_perpetual",
                    "contract_multiplier": multiplier,
                    "quantity_step": rules.get("quantity_step"),
                    "min_quantity": rules.get("min_quantity"),
                    "min_notional_usd": rules.get("min_notional_usd"),
                    "status": "active",
                    "source_url": f"https://www.okx.com/trade-swap/{symbol.lower()}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_kind": "published_current_estimate",
                    "next_funding_at": iso_from_milliseconds(funding_time),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": as_float(ticker.get("volCcy24h")) * last_price or None,
                    "quantity_step": rules.get("quantity_step"),
                    "min_quantity": rules.get("min_quantity"),
                    "min_notional_usd": rules.get("min_notional_usd"),
                    "taker_fee_rate": 0.0005,
                    "observed_at": observed_at,
                    "raw": {
                        "instrument": raw,
                        "ticker": ticker,
                        "mark": mark,
                        "index": index,
                        "funding": funding,
                    },
                }
            )
        warnings = list(funding_warnings)
        if funding_failures:
            examples = ", ".join(funding_failures[:5])
            warnings.append(
                f"OKX funding unavailable for {len(funding_failures)} symbols"
                f" (examples: {examples})."
            )
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            warnings,
        )

    def _current_funding_rates(
        self,
        symbols: list[str],
    ) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
        if self.use_websocket:
            try:
                return okx_funding_snapshot(
                    symbols,
                    websocket_url=self.websocket_url,
                ), [], []
            except FundingDataError as exc:
                results, failures = self._rest_funding_rates(symbols)
                return results, failures, [f"OKX WebSocket fallback to REST: {exc}"]
        results, failures = self._rest_funding_rates(symbols)
        return results, failures, []

    def _rest_funding_rates(
        self,
        symbols: list[str],
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        results: dict[str, dict[str, Any]] = {}
        failures: list[str] = []

        def load(symbol: str) -> dict[str, Any]:
            query = urllib.parse.urlencode({"instId": symbol})
            return okx_first_row(
                self.http.get_json(
                    f"{self.base_url}/api/v5/public/funding-rate?{query}"
                ),
                f"funding rate for {symbol}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_symbols = {
                executor.submit(load, symbol): symbol
                for symbol in symbols
                if symbol
            }
            for future in as_completed(future_symbols):
                symbol = future_symbols[future]
                try:
                    results[symbol] = future.result()
                except FundingDataError:
                    failures.append(symbol)
        return results, sorted(failures)

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"instId": symbol, "sz": max(1, min(int(limit), 400))}
        )
        raw = okx_first_row(
            self.http.get_json(f"{self.base_url}/api/v5/market/books?{query}"),
            f"orderbook for {symbol}",
        )
        multiplier = self._contract_multipliers.get(symbol)
        if multiplier is None:
            raise FundingDataError(f"OKX contract multiplier unavailable for {symbol}")
        bids = contract_levels_to_base(raw.get("bids", []), multiplier)
        asks = contract_levels_to_base(raw.get("asks", []), multiplier)
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
        previous = previous_market or {}
        family = str(previous.get("inst_family") or previous.get("underlying") or "")
        if not family and symbol.endswith("-SWAP"):
            family = symbol.removesuffix("-SWAP")
        funding = okx_first_row(
            self.http.get_json(
                f"{self.base_url}/api/v5/public/funding-rate?"
                f"{urllib.parse.urlencode({'instId': symbol})}"
            ),
            f"funding rate for {symbol}",
        )
        mark = okx_first_row(
            self.http.get_json(
                f"{self.base_url}/api/v5/public/mark-price?"
                f"{urllib.parse.urlencode({'instType': 'SWAP', 'instId': symbol})}"
            ),
            f"mark price for {symbol}",
        )
        index = okx_first_row(
            self.http.get_json(
                f"{self.base_url}/api/v5/market/index-tickers?"
                f"{urllib.parse.urlencode({'instId': family})}"
            ),
            f"index price for {family}",
        )
        ticker = okx_first_row(
            self.http.get_json(
                f"{self.base_url}/api/v5/market/ticker?"
                f"{urllib.parse.urlencode({'instId': symbol})}"
            ),
            f"ticker for {symbol}",
        )
        funding_time = integer_or_none(funding.get("fundingTime"))
        previous_time = integer_or_none(funding.get("prevFundingTime"))
        interval_hours = interval_between(
            previous_time,
            funding_time,
            as_float(previous.get("funding_interval_hours"), 8.0),
        )
        funding_rate = as_float(funding.get("fundingRate"))
        mark_price = as_float(mark.get("markPx"))
        index_price = as_float(index.get("idxPx"))
        last_price = as_float(ticker.get("last"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"OKX reference prices unavailable for {symbol}")
        contract_multiplier = previous.get("contract_multiplier") or self._contract_multipliers.get(symbol)
        if contract_multiplier:
            self._contract_multipliers[symbol] = float(contract_multiplier)
        return {
            "venue": self.venue,
            **endpoint_identity_fields(self.endpoint_identity),
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_current_estimate",
            "next_funding_at": iso_from_milliseconds(funding_time),
            "mark_price": mark_price,
            "index_price": index_price,
            "volume_24h_usd": as_float(ticker.get("volCcy24h")) * last_price or None,
            "taker_fee_rate": 0.0005,
            "observed_at": observed_at,
            "raw": {
                "funding": funding,
                "mark": mark,
                "index": index,
                "ticker": ticker,
            },
            "contract_multiplier": contract_multiplier,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "quantity_step": previous.get("quantity_step"),
            "min_quantity": previous.get("min_quantity"),
            "min_notional_usd": previous.get("min_notional_usd"),
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        rows_by_time: dict[int, dict[str, Any]] = {}
        after: int | None = None
        for _ in range(20):
            query: dict[str, Any] = {"instId": symbol, "limit": 100}
            if after is not None:
                query["after"] = after
            page = okx_rows(
                self.http.get_json(
                    f"{self.base_url}/api/v5/public/funding-rate-history?"
                    f"{urllib.parse.urlencode(query)}"
                ),
                f"funding history for {symbol}",
            )
            timestamps: list[int] = []
            for raw in page:
                timestamp = integer_or_none(raw.get("fundingTime"))
                if timestamp is None:
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps:
                break
            oldest = min(timestamps)
            if oldest < start_time_ms or len(page) < 100 or oldest == after:
                break
            after = oldest

        ordered_times = sorted(rows_by_time)
        rows = []
        for index, timestamp in enumerate(ordered_times):
            if timestamp < start_time_ms:
                continue
            previous_time = ordered_times[index - 1] if index > 0 else None
            actual_interval = interval_between(previous_time, timestamp, interval_hours)
            raw = rows_by_time[timestamp]
            rate = as_float(raw.get("realizedRate") or raw.get("fundingRate"))
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


def okx_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"OKX {label} unavailable: {detail}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FundingDataError(f"Invalid OKX {label} rows")
    return [row for row in rows if isinstance(row, dict)]


def okx_funding_snapshot(
    symbols: list[str],
    websocket_url: str = OKX_PUBLIC_WEBSOCKET_URL,
    timeout_seconds: float = 8.0,
) -> dict[str, dict[str, Any]]:
    requested = {symbol for symbol in symbols if symbol}
    if not requested:
        return {}
    connection = None
    try:
        connection = websocket.create_connection(
            websocket_url,
            timeout=timeout_seconds,
            header=["User-Agent: SmartMoneyRadar-Funding/0.1"],
        )
        subscription = {
            "id": "fundingsnapshot",
            "op": "subscribe",
            "args": [
                {"channel": "funding-rate", "instId": symbol}
                for symbol in sorted(requested)
            ],
        }
        connection.send(json.dumps(subscription, separators=(",", ":")))
        deadline = monotonic() + timeout_seconds
        rows: dict[str, dict[str, Any]] = {}
        while requested - rows.keys():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            connection.settimeout(remaining)
            message = connection.recv()
            if message == "pong":
                continue
            payload = json.loads(message)
            if not isinstance(payload, dict):
                raise FundingDataError("invalid WebSocket payload")
            if payload.get("event") == "error":
                raise FundingDataError(
                    f"subscription error {payload.get('code')}: {payload.get('msg')}"
                )
            for raw in payload.get("data", []):
                if not isinstance(raw, dict):
                    continue
                symbol = str(raw.get("instId") or "")
                if symbol in requested:
                    rows[symbol] = raw
        missing = sorted(requested - rows.keys())
        if missing:
            examples = ", ".join(missing[:5])
            raise FundingDataError(
                f"snapshot missing {len(missing)} symbols (examples: {examples})"
            )
        return rows
    except FundingDataError:
        raise
    except Exception as exc:
        raise FundingDataError(f"snapshot unavailable: {exc}") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def okx_first_row(payload: Any, label: str) -> dict[str, Any]:
    rows = okx_rows(payload, label)
    if not rows:
        raise FundingDataError(f"Empty OKX {label} response")
    return rows[0]


def rows_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(row[key]): row
        for row in rows
        if row.get(key) not in (None, "")
    }


def is_supported_okx_swap(raw: dict[str, Any], observed: datetime) -> bool:
    if raw.get("state") != "live" or raw.get("ctType") != "linear":
        return False
    if str(raw.get("instCategory") or "") == "3":
        return False
    if raw.get("settleCcy") != "USDT":
        return False
    symbol = str(raw.get("instId") or "")
    if not symbol.endswith("-USDT-SWAP"):
        return False
    continuous_at = integer_or_none(raw.get("contTdSwTime"))
    return continuous_at is None or continuous_at <= int(observed.timestamp() * 1000)


def okx_base_asset(raw: dict[str, Any]) -> str:
    underlying = str(raw.get("instFamily") or raw.get("uly") or "")
    return clean_asset_symbol(underlying.removesuffix("-USDT"))


def okx_contract_multiplier(raw: dict[str, Any], base_asset: str) -> float:
    if clean_asset_symbol(raw.get("ctValCcy")) != base_asset:
        return 0.0
    contract_value = as_float(raw.get("ctVal"))
    contract_multiplier = as_float(raw.get("ctMult"), 1.0)
    if contract_value <= 0 or contract_multiplier <= 0:
        return 0.0
    return contract_value * contract_multiplier


def okx_market_rules(raw: dict[str, Any], mark_price: float) -> dict[str, float | None]:
    quantity_step = positive_float(raw.get("lotSz"))
    min_quantity = positive_float(raw.get("minSz"))
    min_notional = min_quantity * mark_price if min_quantity is not None and mark_price > 0 else None
    return {
        "quantity_step": quantity_step,
        "min_quantity": min_quantity,
        "min_notional_usd": min_notional,
    }


def positive_float(value: Any) -> float | None:
    parsed = as_float(value)
    return parsed if parsed > 0 else None


def contract_levels_to_base(levels: Any, multiplier: float) -> list[list[float]]:
    rows: list[list[float]] = []
    if not isinstance(levels, list):
        return rows
    for raw in levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price = as_float(raw[0])
        base_size = as_float(raw[1]) * multiplier
        if price > 0 and base_size > 0:
            rows.append([price, base_size])
    return rows


def interval_between(start_ms: int | None, end_ms: int | None, fallback: float) -> float:
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return max(1.0, float(fallback))
    return max(1.0, (end_ms - start_ms) / 3_600_000.0)


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_observed_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
