from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
    public_fee_evidence,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
    parse_timestamp,
)


PACIFICA_API_URL = "https://api.pacifica.fi/api/v1"
PACIFICA_FUNDING_INTERVAL_HOURS = 1.0


class PacificaFundingClient:
    venue = "pacifica"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = PACIFICA_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(
            min_delay_seconds=0.06,
            max_retries=2,
        )
        self.base_url = base_url.rstrip("/")
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.base_url,
            requested_environment="mainnet",
        )
        self.environment = self.endpoint_identity.environment

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raw = self.http.get_json(f"{self.base_url}/info")
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            raise FundingDataError("Invalid Pacifica /info response")
        price_rows, price_warning = self._prices_by_symbol()
        fee_rates, fee_warning = self._public_fee_rates()

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        if price_warning:
            warnings.append(price_warning)
        if fee_warning:
            warnings.append(fee_warning)

        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("instrument_type") or "").lower() != "perpetual":
                continue
            symbol = str(row.get("symbol") or "")
            canonical_asset = clean_asset_symbol(row.get("base_asset") or symbol)
            if not symbol or not canonical_asset:
                continue

            price_row = price_rows.get(symbol, {})
            raw_next_funding_rate = price_row.get("next_funding") or row.get(
                "next_funding_rate"
            )
            has_next_funding_rate = (
                raw_next_funding_rate is not None
                and str(raw_next_funding_rate).strip() != ""
            )
            published_rate = (
                as_float(raw_next_funding_rate)
                if has_next_funding_rate
                else as_float(price_row.get("funding"), as_float(row.get("funding_rate")))
            )
            source_event_at = iso_from_milliseconds(price_row.get("timestamp"))
            next_funding_at = next_utc_hour(source_event_at or observed_at)

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USD",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "contract_kind": "linear_perpetual",
                    "price_quote_currency": "USD",
                    "settlement_collateral": "USDC",
                    "status": "active",
                    "quantity_step": pacifica_optional_float(row.get("lot_size")),
                    "min_notional_usd": pacifica_optional_float(row.get("min_order_size")),
                    "source_url": f"https://app.pacifica.fi/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": row,
                }
            )
            market = {
                "venue": self.venue,
                "symbol": symbol,
                "canonical_asset": canonical_asset,
                "funding_rate": published_rate,
                "normalized_next_funding_rate": published_rate,
                "funding_interval_hours": PACIFICA_FUNDING_INTERVAL_HOURS,
                "hourly_funding_rate": published_rate,
                "settlement_interval_seconds": 3600.0,
                "displayed_rate_period_seconds": 3600.0,
                "funding_rate_kind": (
                    "published_next_hour_estimate"
                    if has_next_funding_rate
                    else "published_current_hour_estimate"
                ),
                "funding_rate_semantics": "next_settlement",
                "funding_rate_unit": "fraction_of_notional_per_settlement",
                "funding_sign_convention": "positive_long_pays",
                "raw_api_rate": raw_next_funding_rate,
                "raw_rate_unit": "decimal_fraction_of_notional_per_hour",
                "raw_rate_scale": "1",
                "next_funding_at": next_funding_at,
                "next_settlement_source": (
                    "derived_next_utc_hour_from_info_prices_timestamp"
                ),
                "mark_price": pacifica_optional_float(price_row.get("mark")),
                "mark_price_kind": "info_prices_mark",
                "index_price": pacifica_optional_float(price_row.get("oracle")),
                "index_price_kind": "info_prices_oracle",
                "open_interest_usd": pacifica_optional_float(
                    price_row.get("open_interest")
                ),
                "volume_24h_usd": pacifica_optional_float(price_row.get("volume_24h")),
                "quantity_step": pacifica_optional_float(row.get("lot_size")),
                "min_notional_usd": pacifica_optional_float(row.get("min_order_size")),
                "supports_perpetuals": True,
                "is_linear_contract": True,
                "supports_discrete_funding": True,
                "supports_public_shadow_mode": True,
                "position_inclusion_rule": "open_position_during_hourly_funding_epoch",
                "entry_safety_buffer_seconds": 30.0,
                "exit_safety_buffer_seconds": 5.0,
                "timing_policy_source": (
                    "official_hourly_funding_docs_plus_info_prices_timestamp"
                ),
                "source_event_at": source_event_at,
                "observed_at": observed_at,
                "raw": {"info": row, "prices": price_row},
            }
            if fee_rates:
                fee_evidence = pacifica_fee_evidence(
                    self.base_url,
                    observed_at,
                    self.environment,
                )
                market.update(
                    {
                        "maker_fee_rate": fee_rates["maker_fee_rate"],
                        "taker_fee_rate": fee_rates["taker_fee_rate"],
                        "fee_source": "official_public_fee_endpoint",
                        "fee_scope": "public_not_account_specific",
                        "fee_evidence": fee_evidence,
                        "fee_observed_at": observed_at,
                    }
                )
            else:
                market["fee_source"] = "fee_model_missing"
                market["fee_model_missing"] = True
            markets.append(market)
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
        raw = self.http.get_json(f"{self.base_url}/book?symbol={symbol}")
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise FundingDataError(f"Invalid Pacifica orderbook for {symbol}")
        levels = data.get("l") or []
        raw_bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        raw_asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        bid_rows = [
            [as_float(row.get("p")), as_float(row.get("a"))]
            for row in raw_bids
            if isinstance(row, dict)
        ]
        ask_rows = [
            [as_float(row.get("p")), as_float(row.get("a"))]
            for row in raw_asks
            if isinstance(row, dict)
        ]
        return normalize_orderbook(
            self.venue,
            symbol,
            bid_rows[:limit],
            ask_rows[:limit],
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
        info = self._info_for_symbol(symbol, canonical_asset)
        price_rows, _price_warning = self._prices_by_symbol()
        fee_rates, _fee_warning = self._public_fee_rates()
        raw_symbol = str(info.get("symbol") or symbol)
        price_row = price_rows.get(raw_symbol) or price_rows.get(
            clean_asset_symbol(canonical_asset),
            {},
        )
        raw_next_funding_rate = price_row.get("next_funding") or info.get(
            "next_funding_rate"
        )
        has_next_funding_rate = (
            raw_next_funding_rate is not None
            and str(raw_next_funding_rate).strip() != ""
        )
        published_rate = (
            as_float(raw_next_funding_rate)
            if has_next_funding_rate
            else as_float(price_row.get("funding"), as_float(info.get("funding_rate")))
        )
        mark_price = pacifica_optional_float(price_row.get("mark"))
        index_price = pacifica_optional_float(price_row.get("oracle"))
        if not mark_price or not index_price:
            raise FundingDataError(f"Pacifica reference prices unavailable for {symbol}")
        source_event_at = iso_from_milliseconds(price_row.get("timestamp"))
        previous = previous_market or {}
        row = {
            "venue": self.venue,
            "symbol": raw_symbol,
            "canonical_asset": clean_asset_symbol(
                info.get("base_asset") or canonical_asset
            ),
            "funding_rate": published_rate,
            "normalized_next_funding_rate": published_rate,
            "funding_interval_hours": PACIFICA_FUNDING_INTERVAL_HOURS,
            "hourly_funding_rate": published_rate,
            "settlement_interval_seconds": 3600.0,
            "displayed_rate_period_seconds": 3600.0,
            "funding_rate_kind": (
                "published_next_hour_estimate"
                if has_next_funding_rate
                else "published_current_hour_estimate"
            ),
            "funding_rate_semantics": "next_settlement",
            "funding_rate_unit": "fraction_of_notional_per_settlement",
            "funding_sign_convention": "positive_long_pays",
            "raw_api_rate": raw_next_funding_rate,
            "raw_rate_unit": "decimal_fraction_of_notional_per_hour",
            "raw_rate_scale": "1",
            "next_funding_at": next_utc_hour(source_event_at or observed_at),
            "next_settlement_source": "derived_next_utc_hour_from_info_prices_timestamp",
            "mark_price": mark_price,
            "mark_price_kind": "info_prices_mark",
            "index_price": index_price,
            "index_price_kind": "info_prices_oracle",
            "open_interest_usd": pacifica_optional_float(price_row.get("open_interest")),
            "volume_24h_usd": pacifica_optional_float(price_row.get("volume_24h")),
            "quantity_step": pacifica_optional_float(info.get("lot_size")),
            "min_notional_usd": pacifica_optional_float(info.get("min_order_size")),
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "supports_public_shadow_mode": True,
            "position_inclusion_rule": "open_position_during_hourly_funding_epoch",
            "entry_safety_buffer_seconds": 30.0,
            "exit_safety_buffer_seconds": 5.0,
            "timing_policy_source": (
                "official_hourly_funding_docs_plus_info_prices_timestamp"
            ),
            "source_event_at": source_event_at,
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "product_type": previous.get("product_type", "perpetual"),
            "observed_at": observed_at,
            "raw": {"info": info, "prices": price_row},
        }
        if fee_rates:
            fee_evidence = pacifica_fee_evidence(
                self.base_url,
                observed_at,
                self.environment,
            )
            row.update(
                {
                    "maker_fee_rate": fee_rates["maker_fee_rate"],
                    "taker_fee_rate": fee_rates["taker_fee_rate"],
                    "fee_source": "official_public_fee_endpoint",
                    "fee_scope": "public_not_account_specific",
                    "fee_evidence": fee_evidence,
                    "fee_observed_at": observed_at,
                }
            )
        else:
            row["fee_source"] = "fee_model_missing"
            row["fee_model_missing"] = True
        return apply_endpoint_identity([row], self.endpoint_identity)[0]

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"symbol": symbol, "limit": 4000})
        raw = self.http.get_json(f"{self.base_url}/funding_rate/history?{query}")
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            raise FundingDataError(f"Invalid Pacifica funding history for {symbol}")
        rows: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            created_at = pacifica_milliseconds(item.get("created_at"))
            if created_at is None or created_at < int(start_time_ms):
                continue
            rate = as_float(item.get("funding_rate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(created_at),
                    "funding_rate": rate,
                    "funding_interval_hours": max(1.0, float(interval_hours or 1.0)),
                    "hourly_funding_rate": rate / max(1.0, float(interval_hours or 1.0)),
                    "mark_price": pacifica_optional_float(item.get("oracle_price")),
                    "observed_at": observed_at,
                    "raw": item,
                }
            )
        return rows

    def _prices_by_symbol(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        try:
            raw = self.http.get_json(f"{self.base_url}/info/prices")
        except FundingDataError as exc:
            return {}, f"Pacifica prices unavailable: {exc}"
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            return {}, "Pacifica prices unavailable: invalid /info/prices response"
        return {
            str(row.get("symbol")): row
            for row in data
            if isinstance(row, dict) and row.get("symbol")
        }, None

    def _public_fee_rates(self) -> tuple[dict[str, float], str | None]:
        try:
            raw = self.http.get_json(f"{self.base_url}/info/fees")
        except FundingDataError as exc:
            return {}, f"Pacifica fee levels unavailable: {exc}"
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            return {}, "Pacifica fee levels unavailable: invalid /info/fees response"
        maker_rates = [
            value
            for value in (pacifica_non_negative_float(row.get("maker_fee_rate")) for row in data if isinstance(row, dict))
            if value is not None
        ]
        taker_rates = [
            value
            for value in (pacifica_non_negative_float(row.get("taker_fee_rate")) for row in data if isinstance(row, dict))
            if value is not None
        ]
        if not maker_rates or not taker_rates:
            return {}, "Pacifica fee levels unavailable: no parseable fee levels"
        return {
            "maker_fee_rate": max(maker_rates),
            "taker_fee_rate": max(taker_rates),
        }, None

    def _info_for_symbol(self, symbol: str, canonical_asset: str) -> dict[str, Any]:
        raw = self.http.get_json(f"{self.base_url}/info")
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            raise FundingDataError("Invalid Pacifica /info response")
        target_symbol = str(symbol or "").upper()
        target_asset = clean_asset_symbol(canonical_asset)
        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("instrument_type") or "").lower() != "perpetual":
                continue
            raw_symbol = str(row.get("symbol") or "")
            asset = clean_asset_symbol(row.get("base_asset") or raw_symbol)
            if raw_symbol.upper() == target_symbol or asset == target_asset:
                return row
        raise FundingDataError(f"Pacifica market not found for {symbol}")


def next_utc_hour(observed_at: str) -> str:
    observed = parse_timestamp(observed_at) or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return (
        observed.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()


def pacifica_milliseconds(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def pacifica_optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def pacifica_non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def pacifica_fee_evidence(
    base_url: str,
    observed_at: str,
    environment: str,
) -> dict[str, dict[str, Any]]:
    source_identifier = f"{base_url.rstrip('/')}/info/fees"
    return {
        "maker": public_fee_evidence(
            venue="pacifica",
            liquidity_role="maker",
            source_identifier=source_identifier,
            observed_at=observed_at,
            environment=environment,
        ),
        "taker": public_fee_evidence(
            venue="pacifica",
            liquidity_role="taker",
            source_identifier=source_identifier,
            observed_at=observed_at,
            environment=environment,
        ),
    }
