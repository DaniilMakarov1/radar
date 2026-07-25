from __future__ import annotations

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
)


GRVT_MARKET_DATA_URL = "https://market-data.grvt.io"
GRVT_MAKER_FEE_RATE = 0.0
GRVT_TAKER_FEE_RATE = 0.00045
# GRVT API returns funding rates in percentage points (0.01 = 0.01%).
# Convert to decimal fraction (0.0001) for consistency with other venues.
GRVT_FUNDING_RATE_SCALE = 100.0


def _ns_to_ms(value: Any) -> int | None:
    try:
        ns = int(value)
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    return ns // 1_000_000


class GrvtFundingClient:
    venue = "grvt"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = GRVT_MARKET_DATA_URL,
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
        raw_instruments = self.http.post_json(
            f"{self.base_url}/full/v1/all_instruments",
            {"is_active": True, "kinds": ["PERPETUAL"]},
        )
        rows = raw_instruments.get("result") if isinstance(raw_instruments, dict) else None
        if not isinstance(rows, list):
            raise FundingDataError("Invalid GRVT all_instruments response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("kind") or "").upper() != "PERPETUAL":
                continue
            symbol = str(raw.get("instrument") or "")
            base = str(raw.get("base") or "")
            canonical_asset = clean_asset_symbol(base)
            if not symbol or not canonical_asset:
                continue
            interval_hours = max(1.0, as_float(raw.get("funding_interval_hours"), 8.0))

            try:
                ticker = self._ticker(symbol)
            except FundingDataError as exc:
                warnings.append(f"GRVT ticker unavailable for {symbol}: {exc}")
                continue

            mark_price = as_float(ticker.get("mark_price"))
            index_price = as_float(ticker.get("index_price"))
            if mark_price <= 0 or index_price <= 0:
                continue

            funding_rate = as_float(ticker.get("funding_rate")) / GRVT_FUNDING_RATE_SCALE
            next_funding_ms = _ns_to_ms(ticker.get("next_funding_time"))
            next_funding_at = iso_from_milliseconds(next_funding_ms) if next_funding_ms else None

            buy_vol = as_float(ticker.get("buy_volume_24h_b"))
            sell_vol = as_float(ticker.get("sell_volume_24h_b"))
            volume_24h = buy_vol + sell_vol if buy_vol or sell_vol else None

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://grvt.io/trade/{symbol}",
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
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_kind": "published_current",
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("open_interest")) * mark_price
                    if as_float(ticker.get("open_interest")) > 0
                    else None,
                    "volume_24h_usd": volume_24h,
                    "maker_fee_rate": GRVT_MAKER_FEE_RATE,
                    "taker_fee_rate": GRVT_TAKER_FEE_RATE,
                    "fee_source": "venue_public_flat_fee",
                    "observed_at": observed_at,
                    "raw": {"instrument": raw, "ticker": ticker},
                }
            )
        return instruments, markets, warnings

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        depth = min(max(int(limit), 10), 500)
        raw = self.http.post_json(
            f"{self.base_url}/full/v1/book",
            {"instrument": symbol, "depth": depth},
        )
        result = raw.get("result") if isinstance(raw, dict) else None
        if not isinstance(result, dict):
            raise FundingDataError(f"Invalid GRVT orderbook for {symbol}")
        bid_rows = [
            [as_float(row.get("price")), as_float(row.get("size"))]
            for row in result.get("bids") or []
            if isinstance(row, dict)
        ]
        ask_rows = [
            [as_float(row.get("price")), as_float(row.get("size"))]
            for row in result.get("asks") or []
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
        start_ns = str(int(start_time_ms) * 1_000_000)
        rows_by_time: dict[int, dict[str, Any]] = {}
        cursor = ""
        for _ in range(10):
            payload: dict[str, Any] = {
                "instrument": symbol,
                "start_time": start_ns,
                "limit": 1000,
                "agg_type": "FUNDING_INTERVAL",
            }
            if cursor:
                payload["cursor"] = cursor
            raw = self.http.post_json(
                f"{self.base_url}/full/v1/funding",
                payload,
            )
            result = raw.get("result") if isinstance(raw, dict) else None
            if not isinstance(result, list):
                raise FundingDataError(f"Invalid GRVT funding history for {symbol}")
            for row in result:
                if not isinstance(row, dict):
                    continue
                funding_ms = _ns_to_ms(row.get("funding_time"))
                if funding_ms is None or funding_ms < start_time_ms:
                    continue
                rows_by_time[funding_ms] = row
            next_cursor = raw.get("next") if isinstance(raw, dict) else None
            if not next_cursor or not result or len(result) < 1000:
                break
            cursor = str(next_cursor)

        rows: list[dict[str, Any]] = []
        for timestamp_ms in sorted(rows_by_time):
            raw = rows_by_time[timestamp_ms]
            rate = as_float(raw.get("funding_rate")) / GRVT_FUNDING_RATE_SCALE
            row_interval = max(1.0, as_float(raw.get("funding_interval_hours"), interval_hours))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp_ms),
                    "funding_rate": rate,
                    "funding_interval_hours": row_interval,
                    "hourly_funding_rate": rate / row_interval,
                    "mark_price": as_float(raw.get("mark_price")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]

    def _ticker(self, symbol: str) -> dict[str, Any]:
        raw = self.http.post_json(
            f"{self.base_url}/full/v1/ticker",
            {"instrument": symbol},
        )
        result = raw.get("result") if isinstance(raw, dict) else None
        if not isinstance(result, dict):
            raise FundingDataError(f"Invalid GRVT ticker for {symbol}")
        return result
