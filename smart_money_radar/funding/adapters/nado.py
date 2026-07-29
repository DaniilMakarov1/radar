from __future__ import annotations

import urllib.parse
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
)
from smart_money_radar.funding.normalization import clean_asset_symbol, normalize_orderbook


NADO_GATEWAY_V2_URL = "https://gateway.prod.nado.xyz/v2"
NADO_ARCHIVE_V2_URL = "https://archive.prod.nado.xyz/v2"
NADO_ARCHIVE_V1_URL = "https://archive.prod.nado.xyz/v1"
NADO_FUNDING_RATE_SCALE = Decimal("1000000000000000000")
NADO_DISPLAYED_RATE_PERIOD_SECONDS = 86_400.0
NADO_SETTLEMENT_INTERVAL_SECONDS = 3_600.0


class NadoFundingClient:
    venue = "nado"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        gateway_url: str = NADO_GATEWAY_V2_URL,
        archive_v2_url: str = NADO_ARCHIVE_V2_URL,
        archive_v1_url: str = NADO_ARCHIVE_V1_URL,
        environment: str = "mainnet",
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.06, max_retries=2)
        self.gateway_url = gateway_url.rstrip("/")
        self.archive_v2_url = archive_v2_url.rstrip("/")
        self.archive_v1_url = archive_v1_url.rstrip("/")
        self.base_url = self.gateway_url
        self.environment = environment
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.gateway_url,
            requested_environment=environment,
        )
        self._product_id_by_symbol: dict[str, int] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        pairs = nado_list(
            self.http.get_json(f"{self.gateway_url}/pairs?market=perp"),
            "Nado pairs",
        )
        contracts = nado_mapping(
            self.http.get_json(f"{self.archive_v2_url}/contracts?edge=false"),
            "Nado contracts",
        )
        product_ids = [
            int(row["product_id"])
            for row in pairs
            if isinstance(row, dict) and nado_positive_int(row.get("product_id")) is not None
        ]
        funding_rates, funding_warning = self._funding_rates_by_product_id(product_ids)
        warnings = [funding_warning] if funding_warning else []
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            product_id = nado_positive_int(pair.get("product_id"))
            ticker_id = str(pair.get("ticker_id") or "")
            contract = contracts.get(ticker_id)
            if product_id is None or not ticker_id or not isinstance(contract, dict):
                continue
            if str(contract.get("product_type") or "").lower() != "perpetual":
                continue
            quote = str(pair.get("quote") or contract.get("quote_currency") or "")
            if quote.upper() != "USDT0":
                continue
            base_asset = nado_canonical_base(pair.get("base") or contract.get("base_currency"))
            if not base_asset:
                continue
            self._product_id_by_symbol[ticker_id] = product_id
            raw_rate = funding_rates.get(product_id) or {}
            raw_x18 = raw_rate.get("funding_rate_x18")
            daily_rate = nado_rate_x18_to_decimal(raw_x18)
            if daily_rate is None:
                daily_rate = nado_decimal(contract.get("funding_rate"))
            hourly_rate = (
                daily_rate / Decimal("24")
                if daily_rate is not None
                else Decimal("0")
            )
            source_event_at = nado_epoch_seconds_iso(raw_rate.get("update_time"))
            next_funding_at = nado_epoch_seconds_iso(
                contract.get("next_funding_rate_timestamp")
            )
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": ticker_id,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USDT0",
                    "collateral_asset": "USDT0",
                    "contract_type": "linear_perpetual",
                    "contract_kind": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "price_quote_currency": "USDT0",
                    "settlement_collateral": "USDT0",
                    "collateral_family": "USD_OTHER_STABLE",
                    "status": "active",
                    "source_url": f"https://app.nado.xyz/trade/{ticker_id}",
                    "observed_at": observed_at,
                    "raw": {"pair": pair, "contract": contract},
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": ticker_id,
                    "canonical_asset": base_asset,
                    "funding_rate": str(hourly_rate),
                    "normalized_next_funding_rate": str(hourly_rate),
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": str(hourly_rate),
                    "settlement_interval_seconds": NADO_SETTLEMENT_INTERVAL_SECONDS,
                    "displayed_rate_period_seconds": (
                        NADO_DISPLAYED_RATE_PERIOD_SECONDS
                    ),
                    "funding_rate_kind": "published_latest_24h_x18",
                    "funding_rate_semantics": "unclear",
                    "funding_rate_unit": "fraction_of_notional_per_settlement",
                    "funding_sign_convention": "positive_long_pays",
                    "raw_api_rate": raw_x18,
                    "raw_rate_unit": "funding_rate_x18_24h",
                    "raw_rate_scale": "1e18",
                    "normalized_rate_decimal": str(hourly_rate),
                    "next_funding_at": next_funding_at,
                    "next_settlement_source": (
                        "archive_v2_contracts.next_funding_rate_timestamp"
                    ),
                    "source_event_at": source_event_at,
                    "source_sequence": raw_rate.get("update_time"),
                    "mark_price": as_float(contract.get("mark_price")) or None,
                    "index_price": as_float(contract.get("index_price")) or None,
                    "open_interest_usd": as_float(contract.get("open_interest_usd")) or None,
                    "volume_24h_usd": as_float(contract.get("quote_volume")) or None,
                    "supports_perpetuals": True,
                    "is_linear_contract": True,
                    "supports_discrete_funding": False,
                    "supports_public_shadow_mode": True,
                    "fee_source": "fee_model_missing",
                    "fee_model_missing": True,
                    "observed_at": observed_at,
                    "raw": {
                        "pair": pair,
                        "contract": contract,
                        "funding_rate": raw_rate,
                    },
                }
            )
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            warnings,
        )

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"ticker_id": symbol, "depth": max(1, min(int(limit), 250))}
        )
        raw = self.http.get_json(f"{self.gateway_url}/orderbook?{query}")
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Nado orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            nado_levels(raw.get("bids")),
            nado_levels(raw.get("asks")),
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
        product_id = self._product_id_by_symbol.get(symbol)
        if product_id is None:
            self.catalog_and_markets(observed_at)
            product_id = self._product_id_by_symbol.get(symbol)
        if product_id is None:
            raise FundingDataError(f"Nado product id missing for {symbol}")
        payload = {
            "funding_rate_history": {
                "product_id": product_id,
                "start_time": int(int(start_time_ms) / 1000),
                "limit": 1000,
            }
        }
        raw = self.http.post_json(self.archive_v1_url, payload)
        rows = raw.get("funding_rates") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise FundingDataError(f"Invalid Nado funding history for {symbol}")
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            timestamp = nado_positive_int(row.get("timestamp"))
            rate = nado_rate_x18_to_decimal(row.get("funding_rate_x18"))
            if timestamp is None or rate is None:
                continue
            interval = nado_decimal(interval_hours) or Decimal("1")
            output.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": nado_epoch_seconds_iso(timestamp),
                    "funding_rate": str(rate),
                    "funding_interval_hours": str(max(Decimal("1"), interval)),
                    "hourly_funding_rate": str(rate),
                    "observed_at": observed_at,
                    "raw_rate_unit": "funding_rate_x18_hourly",
                    "raw_rate_scale": "1e18",
                    "raw": row,
                }
            )
        return output

    def _funding_rates_by_product_id(
        self,
        product_ids: list[int],
    ) -> tuple[dict[int, dict[str, Any]], str | None]:
        if not product_ids:
            return {}, None
        try:
            raw = self.http.post_json(
                self.archive_v1_url,
                {"funding_rates": {"product_ids": product_ids}},
            )
        except FundingDataError as exc:
            return {}, f"Nado funding rates unavailable: {exc}"
        if not isinstance(raw, dict):
            return {}, "Nado funding rates unavailable: invalid response"
        output: dict[int, dict[str, Any]] = {}
        for key, value in raw.items():
            product_id = nado_positive_int(key)
            if product_id is not None and isinstance(value, dict):
                output[product_id] = value
        return output, None


def nado_list(payload: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise FundingDataError(f"Invalid {label} response")
    return [row for row in payload if isinstance(row, dict)]


def nado_mapping(payload: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise FundingDataError(f"Invalid {label} response")
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def nado_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def nado_rate_x18_to_decimal(value: Any) -> Decimal | None:
    parsed = nado_decimal(value)
    if parsed is None:
        return None
    return parsed / NADO_FUNDING_RATE_SCALE


def nado_epoch_seconds_iso(value: Any) -> str | None:
    timestamp = nado_positive_int(value)
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def nado_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def nado_canonical_base(value: Any) -> str:
    text = str(value or "").upper().replace("-PERP", "")
    return clean_asset_symbol(text.split("_", 1)[0])


def nado_levels(raw_levels: Any) -> list[list[float]]:
    levels: list[list[float]] = []
    if not isinstance(raw_levels, list):
        return levels
    for row in raw_levels:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            price = as_float(row[0])
            quantity = as_float(row[1])
            if price > 0 and quantity > 0:
                levels.append([price, quantity])
    return levels
