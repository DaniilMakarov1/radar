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
from smart_money_radar.funding.adapters.common import (
    next_interval_boundary_iso,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    parse_timestamp,
)


VARIATIONAL_API_URL = (
    "https://omni-client-api.prod.ap-northeast-1.variational.io"
)
VARIATIONAL_QUOTE_NOTIONAL_TIERS: list[tuple[str, float]] = [
    ("base", 500.0),
    ("size_1k", 1_000.0),
    ("size_100k", 100_000.0),
    ("size_1m", 1_000_000.0),
]


class VariationalFundingClient:
    venue = "variational"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = VARIATIONAL_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.12)
        self.base_url = base_url.rstrip("/")
        self.endpoint_identity = build_endpoint_identity(
            venue=self.venue,
            base_url=self.base_url,
            requested_environment="mainnet",
        )
        self.environment = self.endpoint_identity.environment
        self._quotes: dict[str, dict[str, Any]] = {}

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        payload = self.http.get_json(f"{self.base_url}/metadata/stats")
        listings = payload.get("listings") if isinstance(payload, dict) else None
        if not isinstance(listings, list):
            raise FundingDataError("Invalid Variational stats response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        quotes_cache: dict[str, dict[str, Any]] = {}

        for raw in listings:
            if not isinstance(raw, dict):
                continue
            ticker = str(raw.get("ticker") or "")
            if not ticker:
                continue
            canonical_asset = clean_asset_symbol(ticker)
            if not canonical_asset:
                continue

            mark_price = as_float(raw.get("mark_price"))
            if mark_price <= 0:
                continue

            raw_funding_rate = as_float(raw.get("funding_rate"))
            funding_rate = raw_funding_rate / 100.0
            funding_interval_s = int(as_float(raw.get("funding_interval_s")) or 28800)
            interval_hours = max(funding_interval_s / 3600.0, 1.0)
            hourly_funding_rate = funding_rate / interval_hours

            next_funding_at = next_interval_boundary_iso(observed_at, funding_interval_s)

            oi = raw.get("open_interest")
            long_oi = as_float(oi.get("long_open_interest") if isinstance(oi, dict) else None)
            short_oi = as_float(oi.get("short_open_interest") if isinstance(oi, dict) else None)
            open_interest_usd = (long_oi + short_oi) * mark_price if (long_oi + short_oi) > 0 else None

            volume_24h = as_float(raw.get("volume_24h")) or None

            quotes = raw.get("quotes") if isinstance(raw.get("quotes"), dict) else {}
            quotes_cache[ticker] = quotes

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": ticker,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.variational.io/trade/{ticker}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": ticker,
                    "canonical_asset": canonical_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": hourly_funding_rate,
                    "funding_rate_kind": "published_current_interval_estimate",
                    "published_funding_rate": funding_rate,
                    "published_funding_interval_hours": interval_hours,
                    "raw_funding_rate": raw_funding_rate,
                    "funding_rate_source_unit": "api_percent_points_assumed",
                    "funding_display_note": (
                        f"interval {funding_interval_s}s; "
                        "API rate treated as percent points and stored per interval"
                    ),
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": mark_price,
                    "open_interest_usd": open_interest_usd,
                    "volume_24h_usd": volume_24h,
                    "execution_model": "RFQ",
                    "orderbook_depth_available": False,
                    "fee_source": "fee_model_missing",
                    "fee_model_missing": True,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )

        self._quotes = quotes_cache
        return (
            apply_endpoint_identity(instruments, self.endpoint_identity),
            apply_endpoint_identity(markets, self.endpoint_identity),
            [],
        )

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        del observed_at, limit
        raise FundingDataError(
            f"Variational {symbol} is RFQ-only; public CLOB orderbook unavailable"
        )

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        return []
