from __future__ import annotations

from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    normalize_orderbook,
)


REYA_API_URL = "https://api.reya.xyz"
REYA_FUNDING_INTERVAL_HOURS = 1.0
REYA_MAKER_FEE_RATE = 0.0001
REYA_TAKER_FEE_RATE = 0.0005
# Reya API returns funding rates in percentage points (0.0015 = 0.0015%).
# Convert to decimal fraction for consistency with other venues.
REYA_FUNDING_RATE_SCALE = 100.0


class ReyaFundingClient:
    venue = "reya"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = REYA_API_URL,
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
        definitions = self.http.get_json(
            f"{self.base_url}/v2/perpMarketDefinitions"
        )
        if not isinstance(definitions, list):
            raise FundingDataError("Invalid Reya perpMarketDefinitions response")

        summaries = self.http.get_json(
            f"{self.base_url}/v2/perpMarkets/summary"
        )
        summary_by_symbol: dict[str, dict[str, Any]] = {}
        if isinstance(summaries, list):
            for row in summaries:
                if isinstance(row, dict) and row.get("symbol"):
                    summary_by_symbol[str(row["symbol"])] = row

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []

        for raw in definitions:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol") or "")
            canonical_asset = _reya_canonical_asset(symbol)
            if not symbol or not canonical_asset:
                continue

            summary = summary_by_symbol.get(symbol, {})
            funding_rate = as_float(summary.get("fundingRate")) / REYA_FUNDING_RATE_SCALE
            oracle_price = as_float(summary.get("throttledOraclePrice"))
            pool_price = as_float(summary.get("throttledPoolPrice"))
            mark_price = oracle_price or pool_price or None
            index_price = oracle_price or None

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USD",
                    "collateral_asset": "RUSD",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.reya.xyz/trade/{symbol}",
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
                    "funding_interval_hours": REYA_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_current_hour_estimate",
                    "next_funding_at": None,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(summary.get("oiQty")) * oracle_price
                    if as_float(summary.get("oiQty")) > 0 and oracle_price
                    else None,
                    "volume_24h_usd": as_float(summary.get("volume24h")) or None,
                    "maker_fee_rate": REYA_MAKER_FEE_RATE,
                    "taker_fee_rate": REYA_TAKER_FEE_RATE,
                    "fee_source": "venue_public_flat_fee",
                    "observed_at": observed_at,
                    "raw": {"definition": raw, "summary": summary},
                }
            )
        return instruments, markets, []

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        # Reya perps use an AMM pool model — no traditional orderbook.
        return normalize_orderbook(self.venue, symbol, [], [], observed_at, {})

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        return []


def _reya_canonical_asset(symbol: str) -> str:
    cleaned = symbol.replace("RUSDPERP", "").replace("RUSD", "")
    return clean_asset_symbol(cleaned)
