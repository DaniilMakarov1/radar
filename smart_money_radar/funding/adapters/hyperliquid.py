from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    apply_endpoint_identity,
    as_float,
    build_endpoint_identity,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidFundingClient:
    venue = "hyperliquid"

    def __init__(self, http: FundingHttpClient | None = None) -> None:
        self.http = http or FundingHttpClient(
            min_delay_seconds=0.3,
            max_retries=6,
        )
        self.base_url = HYPERLIQUID_INFO_URL
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
        payload = self.http.post_json(
            HYPERLIQUID_INFO_URL,
            {"type": "metaAndAssetCtxs"},
        )
        if not isinstance(payload, list) or len(payload) < 2:
            raise FundingDataError("Invalid Hyperliquid metaAndAssetCtxs response")
        warnings: list[str] = []
        try:
            predicted_payload = self.http.post_json(
                HYPERLIQUID_INFO_URL,
                {"type": "predictedFundings"},
            )
            predicted = hyperliquid_predicted_fundings(predicted_payload)
        except FundingDataError as exc:
            predicted = {}
            warnings.append(f"Hyperliquid predicted funding unavailable: {exc}")
        meta = payload[0] if isinstance(payload[0], dict) else {}
        contexts = payload[1] if isinstance(payload[1], list) else []
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for index, raw in enumerate(universe):
            if not isinstance(raw, dict) or index >= len(contexts):
                continue
            if raw.get("isDelisted"):
                continue
            context = contexts[index] if isinstance(contexts[index], dict) else {}
            symbol = str(raw.get("name") or "")
            canonical_asset = clean_asset_symbol(symbol)
            mark_price = as_float(context.get("markPx"))
            index_price = as_float(context.get("oraclePx"))
            if not symbol or not canonical_asset or not mark_price or not index_price:
                continue
            next_estimate = predicted.get(symbol)
            if next_estimate:
                funding_rate = as_float(next_estimate.get("fundingRate"))
                interval_hours = max(
                    1.0,
                    as_float(next_estimate.get("fundingIntervalHours"), 1.0),
                )
                next_funding_at = hyperliquid_next_funding_time(
                    next_estimate.get("nextFundingTime"),
                    observed_at,
                    interval_hours,
                )
                rate_kind = "published_predicted_next"
            else:
                funding_rate = as_float(context.get("funding"))
                interval_hours = 1.0
                next_funding_at = next_utc_hour(observed_at)
                rate_kind = "published_current_fallback"
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
                    "product_type": "perpetual",
                    "quantity_step": hyperliquid_quantity_step(raw),
                    "status": "active",
                    "source_url": f"https://app.hyperliquid.xyz/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "funding_rate": funding_rate,
                    "normalized_next_funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_kind": rate_kind,
                    "funding_rate_semantics": (
                        "next_settlement"
                        if rate_kind == "published_predicted_next"
                        else "current_interval_fallback"
                    ),
                    "funding_rate_unit": "fraction_of_notional_per_settlement",
                    "funding_sign_convention": "positive_long_pays",
                    "settlement_interval_seconds": interval_hours * 3600.0,
                    "displayed_rate_period_seconds": interval_hours * 3600.0,
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(context.get("openInterest")) * mark_price,
                    "volume_24h_usd": as_float(context.get("dayNtlVlm")) or None,
                    "observed_at": observed_at,
                    "raw": {"context": context, "predicted_funding": next_estimate},
                }
            )
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            warnings,
        )

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self.http.post_json(
            HYPERLIQUID_INFO_URL,
            {"type": "metaAndAssetCtxs"},
        )
        if not isinstance(payload, list) or len(payload) < 2:
            raise FundingDataError("Invalid Hyperliquid metaAndAssetCtxs response")
        try:
            predicted_payload = self.http.post_json(
                HYPERLIQUID_INFO_URL,
                {"type": "predictedFundings"},
            )
            predicted = hyperliquid_predicted_fundings(predicted_payload)
        except FundingDataError:
            predicted = {}
        meta = payload[0] if isinstance(payload[0], dict) else {}
        contexts = payload[1] if isinstance(payload[1], list) else []
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        target_symbol = str(symbol or "").upper()
        target_asset = clean_asset_symbol(canonical_asset)
        for index, raw in enumerate(universe):
            if not isinstance(raw, dict) or index >= len(contexts):
                continue
            if raw.get("isDelisted"):
                continue
            raw_symbol = str(raw.get("name") or "")
            asset = clean_asset_symbol(raw_symbol)
            if raw_symbol.upper() != target_symbol and asset != target_asset:
                continue
            context = contexts[index] if isinstance(contexts[index], dict) else {}
            mark_price = as_float(context.get("markPx"))
            index_price = as_float(context.get("oraclePx"))
            if mark_price <= 0 or index_price <= 0:
                raise FundingDataError(
                    f"Hyperliquid reference prices unavailable for {symbol}"
                )
            next_estimate = predicted.get(raw_symbol)
            if next_estimate:
                funding_rate = as_float(next_estimate.get("fundingRate"))
                interval_hours = max(
                    1.0,
                    as_float(next_estimate.get("fundingIntervalHours"), 1.0),
                )
                next_funding_at = hyperliquid_next_funding_time(
                    next_estimate.get("nextFundingTime"),
                    observed_at,
                    interval_hours,
                )
                rate_kind = "published_predicted_next"
            else:
                funding_rate = as_float(context.get("funding"))
                interval_hours = 1.0
                next_funding_at = next_utc_hour(observed_at)
                rate_kind = "published_current_fallback"
            previous = previous_market or {}
            quantity_step = hyperliquid_quantity_step(raw)
            row = {
                "venue": self.venue,
                "symbol": raw_symbol,
                "canonical_asset": asset,
                "funding_rate": funding_rate,
                "normalized_next_funding_rate": funding_rate,
                "funding_interval_hours": interval_hours,
                "hourly_funding_rate": funding_rate / interval_hours,
                "funding_rate_kind": rate_kind,
                "funding_rate_semantics": (
                    "next_settlement"
                    if rate_kind == "published_predicted_next"
                    else "current_interval_fallback"
                ),
                "funding_rate_unit": "fraction_of_notional_per_settlement",
                "funding_sign_convention": "positive_long_pays",
                "settlement_interval_seconds": interval_hours * 3600.0,
                "displayed_rate_period_seconds": interval_hours * 3600.0,
                "next_funding_at": next_funding_at,
                "mark_price": mark_price,
                "index_price": index_price,
                "open_interest_usd": as_float(context.get("openInterest")) * mark_price,
                "volume_24h_usd": as_float(context.get("dayNtlVlm")) or None,
                "contract_multiplier": previous.get("contract_multiplier") or 1.0,
                "canonical_unit_multiplier": previous.get(
                    "canonical_unit_multiplier",
                    1.0,
                ),
                "product_type": previous.get("product_type", "perpetual"),
                "quantity_step": quantity_step,
                "observed_at": observed_at,
                "raw": {"context": context, "predicted_funding": next_estimate},
            }
            if "taker_fee_rate" in previous:
                row["taker_fee_rate"] = previous["taker_fee_rate"]
            if "maker_fee_rate" in previous:
                row["maker_fee_rate"] = previous["maker_fee_rate"]
            if "fee_source" in previous:
                row["fee_source"] = previous["fee_source"]
            return apply_endpoint_identity([row], self.endpoint_identity)[0]
        raise FundingDataError(f"Hyperliquid symbol not found: {symbol}")

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        payload = self.http.post_json(
            HYPERLIQUID_INFO_URL,
            {"type": "l2Book", "coin": symbol, "nSigFigs": 5},
        )
        if not isinstance(payload, dict):
            raise FundingDataError(f"Invalid Hyperliquid orderbook for {symbol}")
        levels = payload.get("levels", [])
        bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        bid_rows = [[row.get("px"), row.get("sz")] for row in bids if isinstance(row, dict)]
        ask_rows = [[row.get("px"), row.get("sz")] for row in asks if isinstance(row, dict)]
        return normalize_orderbook(
            self.venue,
            symbol,
            bid_rows[:limit],
            ask_rows[:limit],
            observed_at,
            payload,
        )

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        cursor = int(start_time_ms)
        rows_by_time: dict[int, dict[str, Any]] = {}
        for _ in range(10):
            payload = self.http.post_json(
                HYPERLIQUID_INFO_URL,
                {"type": "fundingHistory", "coin": symbol, "startTime": cursor},
            )
            if not isinstance(payload, list):
                raise FundingDataError(f"Invalid Hyperliquid funding history for {symbol}")
            timestamps = []
            for raw in payload:
                if not isinstance(raw, dict) or raw.get("time") is None:
                    continue
                try:
                    timestamp = int(raw["time"])
                except (TypeError, ValueError):
                    continue
                timestamps.append(timestamp)
                rows_by_time[timestamp] = raw
            if not timestamps or len(payload) < 500:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        rows = []
        for timestamp in sorted(rows_by_time):
            raw = rows_by_time[timestamp]
            rate = as_float(raw.get("fundingRate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp),
                    "funding_rate": rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": rate,
                    "mark_price": None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]


def next_utc_hour(observed_at: str) -> str:
    current = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (
        current.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()


def hyperliquid_next_funding_time(
    published_time_ms: Any,
    observed_at: str,
    interval_hours: float,
) -> str:
    """Advance a just-settled predictedFunding timestamp to its next interval."""
    published = iso_from_milliseconds(published_time_ms)
    if not published:
        return next_utc_hour(observed_at)
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    candidate = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    interval = timedelta(hours=max(1.0, float(interval_hours)))
    if candidate <= observed:
        elapsed_intervals = int(
            (observed - candidate).total_seconds() // interval.total_seconds()
        )
        candidate += interval * (elapsed_intervals + 1)
    return candidate.astimezone(UTC).isoformat()


def hyperliquid_quantity_step(row: dict[str, Any]) -> float | None:
    try:
        decimals = int(row.get("szDecimals"))
    except (TypeError, ValueError):
        return None
    if decimals < 0:
        return None
    return 10 ** (-decimals)


def hyperliquid_predicted_fundings(payload: Any) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, list):
        return output
    for row in payload:
        if not isinstance(row, list) or len(row) < 2:
            continue
        symbol = str(row[0] or "")
        venue_rows = row[1] if isinstance(row[1], list) else []
        for venue_row in venue_rows:
            if (
                isinstance(venue_row, list)
                and len(venue_row) >= 2
                and venue_row[0] == "HlPerp"
                and isinstance(venue_row[1], dict)
            ):
                output[symbol] = venue_row[1]
                break
    return output
