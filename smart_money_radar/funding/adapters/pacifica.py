from __future__ import annotations

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


PACIFICA_API_URL = "https://api.pacifica.fi/api/v1"
PACIFICA_FUNDING_INTERVAL_HOURS = 1.0
PACIFICA_MAKER_FEE_RATE = 0.0001
PACIFICA_TAKER_FEE_RATE = 0.00045


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

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raw = self.http.get_json(f"{self.base_url}/info")
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            raise FundingDataError("Invalid Pacifica /info response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []

        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("instrument_type") or "").lower() != "perpetual":
                continue
            symbol = str(row.get("symbol") or "")
            canonical_asset = clean_asset_symbol(row.get("base_asset") or symbol)
            if not symbol or not canonical_asset:
                continue

            raw_next_funding_rate = row.get("next_funding_rate")
            has_next_funding_rate = (
                raw_next_funding_rate is not None
                and str(raw_next_funding_rate).strip() != ""
            )
            published_rate = (
                as_float(raw_next_funding_rate)
                if has_next_funding_rate
                else as_float(row.get("funding_rate"))
            )

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
                    "status": "active",
                    "source_url": f"https://app.pacifica.fi/trade/{symbol}",
                    "observed_at": observed_at,
                    "raw": row,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "funding_rate": published_rate,
                    "funding_interval_hours": PACIFICA_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": published_rate,
                    "funding_rate_kind": (
                        "published_next_hour_estimate"
                        if has_next_funding_rate
                        else "published_current_hour_estimate"
                    ),
                    "next_funding_at": next_utc_hour(observed_at),
                    "mark_price": None,
                    "mark_price_kind": "orderbook_mid_at_route_evaluation",
                    "index_price": None,
                    "index_price_kind": "orderbook_mid_proxy",
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": PACIFICA_MAKER_FEE_RATE,
                    "taker_fee_rate": PACIFICA_TAKER_FEE_RATE,
                    "fee_source": "venue_public_flat_fee",
                    "observed_at": observed_at,
                    "raw": row,
                }
            )
        return instruments, markets, []

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

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        return []


def next_utc_hour(observed_at: str) -> str:
    observed = parse_timestamp(observed_at) or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return (
        observed.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    ).isoformat()
