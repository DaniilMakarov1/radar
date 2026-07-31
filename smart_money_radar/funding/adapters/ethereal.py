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
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
    parse_timestamp,
)


ETHEREAL_API_URL = "https://api.ethereal.trade"
ETHEREAL_FUNDING_INTERVAL_HOURS = 1.0
ETHEREAL_HISTORY_RANGE = "MONTH"
ETHEREAL_PAGE_SIZE = 100


class EtherealFundingClient:
    venue = "ethereal"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = ETHEREAL_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.08)
        self.base_url = base_url.rstrip("/")
        self._products_by_symbol: dict[str, dict[str, Any]] = {}
        self._products_by_id: dict[str, dict[str, Any]] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        products = self.fetch_products()
        active_products = [
            product for product in products if is_supported_ethereal_product(product)
        ]
        product_ids = [str(product["id"]) for product in active_products]
        prices = ethereal_map_by_product_id(
            self.fetch_batch("/v1/product/market-price", "productIds", product_ids),
        )
        projected_rates = ethereal_map_by_product_id(
            self.fetch_batch("/v1/funding/projected-rate", "productIds", product_ids),
        )

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for product in active_products:
            symbol = str(product.get("ticker") or "")
            product_id = str(product.get("id") or "")
            asset = clean_asset_symbol(product.get("baseTokenName"))
            price = prices.get(product_id, {})
            projected = projected_rates.get(product_id, {})
            best_bid = as_float(price.get("bestBidPrice"))
            best_ask = as_float(price.get("bestAskPrice"))
            index_price = as_float(price.get("oraclePrice"))
            if not symbol or not product_id or not asset:
                continue
            if best_bid <= 0 or best_ask <= 0 or index_price <= 0:
                continue
            mark_price = (best_bid + best_ask) / 2.0
            funding_rate = as_float(
                projected.get("fundingRateProjected1h"),
                as_float(projected.get("fundingRate1h"), as_float(product.get("fundingRate1h"))),
            )
            maker_fee = max(0.0, as_float(product.get("makerFee"), 0.0))
            taker_fee = max(0.0, as_float(product.get("takerFee"), 0.0003))
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "base_asset": asset,
                    "quote_asset": "USD",
                    "collateral_asset": "USDe",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.ethereal.trade/?product={symbol}",
                    "observed_at": observed_at,
                    "raw": {"product": product, "product_id": product_id},
                }
            )
            raw_volume = as_float(product.get("volume24h"))
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": ETHEREAL_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_projected_1h",
                    "next_funding_at": next_hour_after(observed_at),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(product.get("openInterest")) * mark_price
                    or None,
                    "volume_24h_usd": raw_volume * mark_price or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "fee_source": "venue_public_product_config",
                    "observed_at": observed_at,
                    "raw": {
                        "product": product,
                        "market_price": price,
                        "projected_funding": projected,
                        "product_id": product_id,
                        "funding_interval": "hourly",
                    },
                }
            )
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        product = self.product_for_symbol(symbol)
        product_id = str(product.get("id") or "")
        if not product_id:
            raise FundingDataError(f"Ethereal product id missing for {symbol}")
        params = urllib.parse.urlencode({"productId": product_id})
        raw = self.http.get_json(
            f"{self.base_url}/v1/product/market-liquidity?{params}"
        )
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Ethereal orderbook for {symbol}")
        bids = ethereal_levels(raw.get("bids"), limit, reverse=True)
        asks = ethereal_levels(raw.get("asks"), limit, reverse=False)
        return normalize_orderbook(
            self.venue,
            str(product.get("ticker") or symbol),
            bids,
            asks,
            observed_at,
            {"product_id": product_id, "market_liquidity": raw},
        )

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        product = self.product_for_symbol(symbol)
        if not is_supported_ethereal_product(product):
            raise FundingDataError(f"Ethereal product not supported for {symbol}")
        product_id = str(product.get("id") or "")
        price_rows = ethereal_map_by_product_id(
            self.fetch_batch("/v1/product/market-price", "productIds", [product_id])
        )
        projected_rows = ethereal_map_by_product_id(
            self.fetch_batch("/v1/funding/projected-rate", "productIds", [product_id])
        )
        price = price_rows.get(product_id, {})
        projected = projected_rows.get(product_id, {})
        best_bid = as_float(price.get("bestBidPrice"))
        best_ask = as_float(price.get("bestAskPrice"))
        index_price = as_float(price.get("oraclePrice"))
        if best_bid <= 0 or best_ask <= 0 or index_price <= 0:
            raise FundingDataError(f"Ethereal reference prices unavailable for {symbol}")
        mark_price = (best_bid + best_ask) / 2.0
        funding_rate = as_float(
            projected.get("fundingRateProjected1h"),
            as_float(
                projected.get("fundingRate1h"),
                as_float(product.get("fundingRate1h")),
            ),
        )
        previous = previous_market or {}
        maker_fee = max(0.0, as_float(product.get("makerFee"), 0.0))
        taker_fee = max(0.0, as_float(product.get("takerFee"), 0.0003))
        raw_volume = as_float(product.get("volume24h"))
        return {
            "venue": self.venue,
            "symbol": str(product.get("ticker") or symbol),
            "canonical_asset": clean_asset_symbol(canonical_asset),
            "funding_rate": funding_rate,
            "funding_interval_hours": ETHEREAL_FUNDING_INTERVAL_HOURS,
            "hourly_funding_rate": funding_rate,
            "funding_rate_kind": "published_projected_1h",
            "next_funding_at": next_hour_after(observed_at),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(product.get("openInterest")) * mark_price
            or None,
            "volume_24h_usd": raw_volume * mark_price or None,
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
            "fee_source": "venue_public_product_config",
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": {
                "product": product,
                "market_price": price,
                "projected_funding": projected,
                "product_id": product_id,
                "funding_interval": "hourly",
            },
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        product = self.product_for_symbol(symbol)
        product_id = str(product.get("id") or "")
        observed = parse_timestamp(observed_at) or datetime.now(UTC)
        end_time_ms = int(observed.timestamp() * 1_000)
        if not product_id:
            raise FundingDataError(f"Ethereal product id missing for {symbol}")

        rows_by_time: dict[int, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {
                "productId": product_id,
                "range": ETHEREAL_HISTORY_RANGE,
                "limit": ETHEREAL_PAGE_SIZE,
                "order": "desc",
                "orderBy": "createdAt",
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(
                f"{self.base_url}/v1/funding?{urllib.parse.urlencode(params)}"
            )
            page = ethereal_rows(payload, f"funding history for {symbol}")
            oldest: int | None = None
            for raw in page:
                timestamp = int(as_float(raw.get("createdAt")))
                if timestamp <= 0 or timestamp > end_time_ms:
                    continue
                rows_by_time[timestamp] = raw
                oldest = timestamp if oldest is None else min(oldest, timestamp)
            if not bool(payload.get("hasNext")) or not page:
                break
            if oldest is not None and oldest <= start_time_ms:
                break
            next_cursor = payload.get("nextCursor")
            if not next_cursor or str(next_cursor) == cursor:
                break
            cursor = str(next_cursor)

        timestamps = [value for value in rows_by_time if value >= start_time_ms]
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(timestamps):
            raw = rows_by_time[timestamp]
            rate = as_float(raw.get("fundingRate1h"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": str(product.get("ticker") or symbol),
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": ETHEREAL_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": rate,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": {
                        **raw,
                        "product_id": product_id,
                        "history_range": ETHEREAL_HISTORY_RANGE,
                    },
                }
            )
        return rows

    def fetch_products(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": 100,
            "order": "asc",
            "orderBy": "createdAt",
        }
        products = ethereal_rows(
            self.http.get_json(
                f"{self.base_url}/v1/product?{urllib.parse.urlencode(params)}"
            ),
            "products",
        )
        self._products_by_symbol = {
            str(product.get("ticker")): product
            for product in products
            if product.get("ticker") and product.get("id")
        }
        self._products_by_id = {
            str(product.get("id")): product
            for product in products
            if product.get("ticker") and product.get("id")
        }
        return products

    def fetch_batch(
        self,
        path: str,
        param_name: str,
        values: list[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(values), 50):
            chunk = values[offset : offset + 50]
            if not chunk:
                continue
            params = urllib.parse.urlencode(
                [(param_name, value) for value in chunk],
            )
            rows.extend(
                ethereal_rows(
                    self.http.get_json(f"{self.base_url}{path}?{params}"),
                    path,
                )
            )
        return rows

    def product_for_symbol(self, symbol: str) -> dict[str, Any]:
        if not self._products_by_symbol:
            self.fetch_products()
        product = self._products_by_symbol.get(symbol)
        if product is None:
            product = self._products_by_id.get(symbol)
        if product is None:
            params = urllib.parse.urlencode({"ticker": symbol, "limit": 1})
            products = ethereal_rows(
                self.http.get_json(f"{self.base_url}/v1/product?{params}"),
                f"product for {symbol}",
            )
            product = next((row for row in products if row.get("id")), None)
            if product is not None:
                self._products_by_symbol[str(product.get("ticker"))] = product
                self._products_by_id[str(product.get("id"))] = product
        if product is None:
            raise FundingDataError(f"Ethereal product not found for {symbol}")
        return product


def is_supported_ethereal_product(raw: dict[str, Any]) -> bool:
    if str(raw.get("status") or "").upper() != "ACTIVE":
        return False
    if str(raw.get("quoteTokenName") or "").upper() != "USD":
        return False
    if str(raw.get("ticker") or "").strip() == "":
        return False
    if str(raw.get("id") or "").strip() == "":
        return False
    return bool(clean_asset_symbol(raw.get("baseTokenName")))


def ethereal_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        detail = payload.get("message") if isinstance(payload, dict) else "invalid response"
        raise FundingDataError(f"Ethereal {label} unavailable: {detail}")
    return [row for row in rows if isinstance(row, dict)]


def ethereal_map_by_product_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("productId")): row
        for row in rows
        if row.get("productId")
    }


def ethereal_levels(raw_levels: Any, limit: int, *, reverse: bool) -> list[list[float]]:
    if not isinstance(raw_levels, list):
        return []
    levels: list[list[float]] = []
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price = as_float(raw[0])
        size = as_float(raw[1])
        if price > 0 and size > 0:
            levels.append([price, size])
    levels.sort(key=lambda row: row[0], reverse=reverse)
    return levels[: max(1, min(int(limit), 1_000))]


def next_hour_after(observed_at: str) -> str:
    observed = parse_timestamp(observed_at) or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    next_hour = observed.astimezone(UTC).replace(
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(hours=1)
    return next_hour.isoformat()
