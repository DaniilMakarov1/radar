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
    normalize_orderbook,
    parse_timestamp,
)


DYDX_INDEXER_URL = "https://indexer.dydx.trade/v4"


class DydxFundingClient:
    venue = "dydx"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = DYDX_INDEXER_URL,
    ) -> None:
        self.http = http or FundingHttpClient()
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        payload = self.http.get_json(f"{self.base_url}/perpetualMarkets")
        raw_markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(raw_markets, dict):
            raise FundingDataError("Invalid dYdX perpetualMarkets response")
        next_funding_at = next_utc_hour(observed_at)
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        for market_key, raw in raw_markets.items():
            if not isinstance(raw, dict) or raw.get("status") != "ACTIVE":
                continue
            symbol = str(raw.get("ticker") or market_key or "")
            if not symbol.endswith("-USD"):
                continue
            base_asset = clean_asset_symbol(symbol.removesuffix("-USD"))
            oracle_price = as_float(raw.get("oraclePrice"))
            if not base_asset or oracle_price <= 0:
                continue
            funding_rate = as_float(raw.get("nextFundingRate"))
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": "USD",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://dydx.trade/trade/{symbol}",
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
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_next_hour",
                    "next_funding_at": next_funding_at,
                    "mark_price": None,
                    "mark_price_kind": "orderbook_mid_at_route_evaluation",
                    "index_price": oracle_price,
                    "open_interest_usd": as_float(raw.get("openInterest")) * oracle_price or None,
                    "volume_24h_usd": as_float(raw.get("volume24H")) or None,
                    "taker_fee_rate": 0.0006,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return instruments, markets, []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        encoded = urllib.parse.quote(symbol, safe="")
        payload = self.http.get_json(
            f"{self.base_url}/orderbooks/perpetualMarket/{encoded}"
        )
        if not isinstance(payload, dict):
            raise FundingDataError(f"Invalid dYdX orderbook for {symbol}")
        bids = dydx_levels(payload.get("bids"), limit)
        asks = dydx_levels(payload.get("asks"), limit)
        return normalize_orderbook(
            self.venue,
            symbol,
            bids,
            asks,
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
        encoded = urllib.parse.quote(symbol, safe="")
        start = datetime.fromtimestamp(start_time_ms / 1000.0, tz=UTC)
        before: str | None = None
        rows_by_time: dict[str, dict[str, Any]] = {}
        for _ in range(24):
            query: dict[str, Any] = {"limit": 100}
            if before:
                query["effectiveBeforeOrAt"] = before
            payload = self.http.get_json(
                f"{self.base_url}/historicalFunding/{encoded}?"
                f"{urllib.parse.urlencode(query)}"
            )
            page = payload.get("historicalFunding") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise FundingDataError(f"Invalid dYdX funding history for {symbol}")
            timestamps = []
            for raw in page:
                if not isinstance(raw, dict):
                    continue
                effective_at = str(raw.get("effectiveAt") or "")
                parsed = parse_timestamp(effective_at)
                if not effective_at or parsed is None:
                    continue
                timestamps.append(parsed)
                rows_by_time[effective_at] = raw
            if not timestamps:
                break
            oldest = min(timestamps)
            if oldest < start or len(page) < 100:
                break
            next_before = oldest.isoformat()
            if next_before == before:
                break
            before = next_before

        rows = []
        for effective_at, raw in sorted(
            rows_by_time.items(),
            key=lambda item: parse_timestamp(item[0]) or datetime.min.replace(tzinfo=UTC),
        ):
            parsed = parse_timestamp(effective_at)
            if parsed is None or parsed < start:
                continue
            rate = as_float(raw.get("rate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": parsed.isoformat(),
                    "funding_rate": rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": rate,
                    "mark_price": as_float(raw.get("price")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return rows


def dydx_levels(raw_levels: Any, limit: int) -> list[list[Any]]:
    if not isinstance(raw_levels, list):
        return []
    return [
        [row.get("price"), row.get("size")]
        for row in raw_levels[: max(1, int(limit))]
        if isinstance(row, dict)
    ]


def next_utc_hour(observed_at: str) -> str:
    current = parse_timestamp(observed_at)
    if current is None:
        current = datetime.now(UTC)
    return (
        current.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()
