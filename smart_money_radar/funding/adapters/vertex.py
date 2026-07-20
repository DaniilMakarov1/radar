from __future__ import annotations

import math
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
    iso_from_milliseconds,
    normalize_orderbook,
    parse_timestamp,
)


VERTEX_BASE_GATEWAY_URL = "https://gateway.base-prod.vertexprotocol.com/v1"
VERTEX_BASE_INDEXER_URL = "https://archive.base-prod.vertexprotocol.com/v1"
VERTEX_FUNDING_INTERVAL_HOURS = 1.0
VERTEX_MARKET_SNAPSHOT_GRANULARITY_SECONDS = 3_600
VERTEX_MARKET_SNAPSHOT_PAGE_SIZE = 1_000


class VertexFundingClient:
    venue = "vertex_base"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        gateway_url: str = VERTEX_BASE_GATEWAY_URL,
        indexer_url: str = VERTEX_BASE_INDEXER_URL,
        venue: str = "vertex_base",
        source_label: str = "Vertex Base",
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.gateway_url = gateway_url.rstrip("/")
        self.indexer_url = indexer_url.rstrip("/")
        self.venue = venue
        self.source_label = source_label
        self._product_ids_by_symbol: dict[str, int] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        symbols_payload = self.engine_query(
            {"type": "symbols", "product_type": "perp", "product_ids": None},
            "symbols",
        )
        raw_symbols = vertex_symbols(symbols_payload)
        perp_symbols = [
            raw
            for raw in raw_symbols
            if str(raw.get("type") or "").lower() == "perp"
            or str(raw.get("symbol") or "").upper().endswith("-PERP")
        ]
        if not perp_symbols:
            raise FundingDataError(f"{self.source_label} returned no perp symbols")
        self._product_ids_by_symbol.update(
            {
                str(raw.get("symbol") or "").upper(): int(raw["product_id"])
                for raw in perp_symbols
                if positive_int(raw.get("product_id")) is not None
            }
        )

        products_payload = self.engine_query({"type": "all_products"}, "all products")
        products = vertex_perp_products(products_payload)
        product_by_id = {
            int(product["product_id"]): product
            for product in products
            if product.get("product_id") is not None
        }
        product_ids = [
            int(raw["product_id"])
            for raw in perp_symbols
            if positive_int(raw.get("product_id")) is not None
        ]
        funding_payload = self.indexer_query(
            {"funding_rates": {"product_ids": product_ids}},
            "funding rates",
        )
        funding_by_id = vertex_funding_rates(funding_payload)
        next_funding_at = next_utc_hour(observed_at)

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in perp_symbols:
            product_id = positive_int(raw.get("product_id"))
            symbol = str(raw.get("symbol") or "").upper()
            if product_id is None or not symbol.endswith("-PERP"):
                continue
            asset = clean_asset_symbol(symbol.removesuffix("-PERP"))
            if not asset:
                continue
            product = product_by_id.get(product_id, {})
            oracle_price = vertex_x18(product.get("oracle_price_x18"))
            funding = funding_by_id.get(product_id)
            if funding is None:
                warnings.append(f"{self.source_label} {symbol} funding missing; skipped.")
                continue
            funding_rate = vertex_x18(funding.get("funding_rate_x18"))
            open_interest = vertex_amount(
                nested_get(product, ("state", "open_interest"))
            )
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.vertexprotocol.com/?market={symbol}",
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
                    "funding_interval_hours": VERTEX_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_latest_hour_x18",
                    "next_funding_at": next_funding_at,
                    "mark_price": None,
                    "mark_price_kind": "orderbook_mid_at_route_evaluation",
                    "index_price": oracle_price or None,
                    "open_interest_usd": (
                        open_interest * oracle_price
                        if open_interest > 0 and oracle_price > 0
                        else None
                    ),
                    "volume_24h_usd": None,
                    "maker_fee_rate": vertex_x18(raw.get("maker_fee_rate_x18")),
                    "taker_fee_rate": vertex_x18(raw.get("taker_fee_rate_x18")),
                    "fee_source": "venue_public_symbols_x18",
                    "observed_at": observed_at,
                    "raw": {"symbol": raw, "product": product, "funding": funding},
                }
            )
        return instruments, markets, warnings

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        product_id = self.product_id_for_symbol(symbol)
        raw = self.engine_query(
            {
                "type": "market_liquidity",
                "product_id": product_id,
                "depth": max(1, min(int(limit), 200)),
            },
            f"orderbook for {symbol}",
        )
        bids = vertex_levels(raw.get("bids") if isinstance(raw, dict) else None)
        asks = vertex_levels(raw.get("asks") if isinstance(raw, dict) else None)
        return normalize_orderbook(
            self.venue,
            symbol,
            bids[:limit],
            asks[:limit],
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
        product_id = self.product_id_for_symbol(symbol)
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        start_seconds = max(0, int(start_time_ms / 1000))
        cursor = int(observed.timestamp())
        rows_by_time: dict[int, Any] = {}
        for _ in range(6):
            payload = self.indexer_query(
                {
                    "market_snapshots": {
                        "interval": {
                            "count": VERTEX_MARKET_SNAPSHOT_PAGE_SIZE,
                            "granularity": VERTEX_MARKET_SNAPSHOT_GRANULARITY_SECONDS,
                            "max_time": cursor,
                        },
                        "product_ids": [product_id],
                    }
                },
                f"market snapshots for {symbol}",
            )
            snapshots = (
                payload.get("snapshots") if isinstance(payload, dict) else None
            )
            if not isinstance(snapshots, list) or not snapshots:
                break
            timestamps: list[int] = []
            for raw in snapshots:
                if not isinstance(raw, dict):
                    continue
                timestamp = vertex_timestamp_seconds(raw.get("timestamp"))
                if timestamp <= 0:
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or min(timestamps) <= start_seconds:
                break
            next_cursor = min(timestamps) - 1
            if next_cursor >= cursor:
                break
            cursor = next_cursor

        timestamps = [
            timestamp
            for timestamp in rows_by_time
            if timestamp >= start_seconds
            and datetime.fromtimestamp(timestamp, UTC) <= observed
        ]
        intervals = inferred_funding_intervals(
            timestamps,
            VERTEX_FUNDING_INTERVAL_HOURS,
            units_per_second=1.0,
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(timestamps):
            raw = rows_by_time[timestamp]
            funding_rates = raw.get("funding_rates") if isinstance(raw, dict) else None
            rate = vertex_dict_value(funding_rates, product_id)
            if rate is None:
                continue
            actual_interval = intervals[timestamp]
            funding_rate = vertex_x18(rate)
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                    "funding_rate": funding_rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": funding_rate / actual_interval,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows

    def product_id_for_symbol(self, symbol: str) -> int:
        normalized_symbol = str(symbol or "").upper()
        cached = self._product_ids_by_symbol.get(normalized_symbol)
        if cached:
            return cached
        payload = self.engine_query(
            {"type": "symbols", "product_type": "perp", "product_ids": None},
            "symbols",
        )
        for raw in vertex_symbols(payload):
            raw_symbol = str(raw.get("symbol") or "").upper()
            product_id = positive_int(raw.get("product_id"))
            if product_id is not None:
                self._product_ids_by_symbol[raw_symbol] = product_id
            if raw_symbol == normalized_symbol:
                return product_id
        raise FundingDataError(f"{self.source_label} symbol not found: {symbol}")

    def engine_query(self, payload: dict[str, Any], label: str) -> Any:
        return vertex_response_data(
            self.http.post_json(f"{self.gateway_url}/query", payload),
            f"{self.source_label} {label}",
        )

    def indexer_query(self, payload: dict[str, Any], label: str) -> Any:
        return vertex_response_data(
            self.http.post_json(self.indexer_url, payload),
            f"{self.source_label} {label}",
        )


def vertex_response_data(payload: Any, label: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    status = str(payload.get("status") or "").lower()
    if status and status != "success":
        detail = payload.get("error") or payload.get("message") or "unsuccessful"
        raise FundingDataError(f"{label} unavailable: {detail}")
    if "data" in payload:
        return payload["data"]
    return payload


def vertex_symbols(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        symbols = payload.get("symbols")
        if isinstance(symbols, dict):
            return [row for row in symbols.values() if isinstance(row, dict)]
        if isinstance(symbols, list):
            return [row for row in symbols if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def vertex_perp_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_products = payload.get("perp_products")
        if isinstance(raw_products, list):
            return [
                row
                for row in raw_products
                if isinstance(row, dict) and row.get("product_id") is not None
            ]
        if isinstance(raw_products, dict):
            return [
                row
                for row in raw_products.values()
                if isinstance(row, dict) and row.get("product_id") is not None
            ]
    return []


def vertex_funding_rates(payload: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    output: dict[int, dict[str, Any]] = {}
    for key, value in payload.items():
        product_id = positive_int(key)
        if product_id is None or not isinstance(value, dict):
            continue
        output[product_id] = value
    return output


def vertex_levels(raw_levels: Any) -> list[list[float]]:
    if not isinstance(raw_levels, list):
        return []
    levels: list[list[float]] = []
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price = vertex_price(raw[0])
        size = vertex_amount(raw[1])
        if price > 0 and size > 0:
            levels.append([price, size])
    return levels


def vertex_price(value: Any) -> float:
    return vertex_x18(value)


def vertex_amount(value: Any) -> float:
    number = as_float(value)
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return number / 1e18 if abs(number) >= 1e12 else number


def vertex_x18(value: Any) -> float:
    number = as_float(value)
    if not math.isfinite(number):
        return 0.0
    return number / 1e18 if abs(number) >= 1e12 else number


def positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def vertex_dict_value(payload: Any, key: int) -> Any:
    if not isinstance(payload, dict):
        return None
    for candidate in (key, str(key)):
        if candidate in payload:
            return payload[candidate]
    return None


def vertex_timestamp_seconds(value: Any) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return 0
    if timestamp > 1_000_000_000_000:
        iso = iso_from_milliseconds(timestamp)
        parsed = parse_timestamp(iso)
        return int(parsed.timestamp()) if parsed else 0
    return timestamp


def next_utc_hour(observed_at: str) -> str:
    current = parse_timestamp(observed_at) or datetime.now(UTC)
    return (
        current.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()
