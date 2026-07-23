from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.adapters.common import iso_from_nanoseconds
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    iso_from_milliseconds,
    normalize_orderbook,
)


AEVO_API_URL = "https://api.aevo.xyz"


class AevoFundingClient:
    venue = "aevo"
    live_history_enabled = True

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = AEVO_API_URL,
        max_funding_workers: int = 12,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.02)
        self.base_url = base_url.rstrip("/")
        self.max_funding_workers = max(1, int(max_funding_workers))

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        rows = self.http.get_json(
            f"{self.base_url}/markets?"
            f"{urllib.parse.urlencode({'instrument_type': 'PERPETUAL'})}"
        )
        if not isinstance(rows, list):
            raise FundingDataError("Invalid Aevo markets rows")
        instruments: list[dict[str, Any]] = []
        market_sources: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict) or not is_supported_aevo_market(raw):
                continue
            symbol = str(raw.get("instrument_name") or "")
            base_asset = clean_asset_symbol(raw.get("underlying_asset"))
            quote_asset = clean_asset_symbol(raw.get("quote_asset"))
            mark_price = as_float(raw.get("mark_price"))
            index_price = as_float(raw.get("index_price"), mark_price)
            if not symbol or not base_asset or mark_price <= 0 or index_price <= 0:
                continue
            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "collateral_asset": quote_asset,
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://app.aevo.xyz/perpetual/{symbol}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            market_sources.append(raw)

        funding_by_symbol: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        with ThreadPoolExecutor(max_workers=min(self.max_funding_workers, len(market_sources) or 1)) as executor:
            futures = {
                executor.submit(
                    self._funding_for_symbol,
                    str(raw.get("instrument_name") or ""),
                ): str(raw.get("instrument_name") or "")
                for raw in market_sources
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    funding_by_symbol[symbol] = future.result()
                except FundingDataError as exc:
                    warnings.append(f"Aevo {symbol} funding skipped: {exc}")

        markets: list[dict[str, Any]] = []
        for raw in market_sources:
            symbol = str(raw.get("instrument_name") or "")
            funding = funding_by_symbol.get(symbol)
            if not funding:
                continue
            base_asset = clean_asset_symbol(raw.get("underlying_asset"))
            mark_price = as_float(raw.get("mark_price"))
            index_price = as_float(raw.get("index_price"), mark_price)
            funding_rate = as_float(funding.get("funding_rate"))
            interval_hours = 1.0
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "canonical_asset": base_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_next_hour_estimate",
                    "next_funding_at": iso_from_nanoseconds(funding.get("next_epoch")),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": None,
                    "volume_24h_usd": None,
                    "maker_fee_rate": 0.0005,
                    "taker_fee_rate": 0.0008,
                    "fee_source": "venue_public_contract_spec",
                    "observed_at": observed_at,
                    "raw": {"market": raw, "funding": funding},
                }
            )
        return instruments, markets, warnings

    def market_snapshot(
        self,
        symbol: str,
        canonical_asset: str,
        observed_at: str,
        previous_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        funding = self._funding_for_symbol(symbol)
        market = self._market_for_symbol(symbol)
        mark_price = as_float(market.get("mark_price"))
        index_price = as_float(market.get("index_price"), mark_price)
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"Aevo reference prices unavailable for {symbol}")
        rate = as_float(funding.get("funding_rate"))
        return {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "funding_rate": rate,
            "funding_interval_hours": 1.0,
            "hourly_funding_rate": rate,
            "funding_rate_kind": "published_next_hour_estimate",
            "next_funding_at": iso_from_nanoseconds(funding.get("next_epoch")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": None,
            "volume_24h_usd": None,
            "maker_fee_rate": 0.0005,
            "taker_fee_rate": 0.0008,
            "fee_source": "venue_public_contract_spec",
            "observed_at": observed_at,
            "raw": {"market": market, "funding": funding},
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        query = urllib.parse.urlencode({"instrument_name": symbol})
        raw = self.http.get_json(f"{self.base_url}/orderbook?{query}")
        if not isinstance(raw, dict):
            raise FundingDataError(f"Invalid Aevo orderbook for {symbol}")
        return normalize_orderbook(
            self.venue,
            symbol,
            raw.get("bids", [])[: max(1, int(limit))],
            raw.get("asks", [])[: max(1, int(limit))],
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
        query = urllib.parse.urlencode({"instrument_name": symbol})
        payload = self.http.get_json(f"{self.base_url}/funding-history?{query}")
        rows = payload.get("funding_history") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FundingDataError(f"Invalid Aevo funding history for {symbol}")
        output: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, list) or len(raw) < 3:
                continue
            timestamp_ms = nanoseconds_to_milliseconds(raw[1])
            if timestamp_ms is None or timestamp_ms < start_time_ms:
                continue
            rate = as_float(raw[2])
            output.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(timestamp_ms),
                    "funding_rate": rate,
                    "funding_interval_hours": 1.0,
                    "hourly_funding_rate": rate,
                    "mark_price": as_float(raw[3]) if len(raw) > 3 else None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in output if row["funding_at"]]

    def _funding_for_symbol(self, symbol: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"instrument_name": symbol})
        payload = self.http.get_json(f"{self.base_url}/funding?{query}")
        if not isinstance(payload, dict) or payload.get("funding_rate") is None:
            raise FundingDataError(f"Invalid Aevo funding for {symbol}")
        return payload

    def _market_for_symbol(self, symbol: str) -> dict[str, Any]:
        rows = self.http.get_json(
            f"{self.base_url}/markets?"
            f"{urllib.parse.urlencode({'instrument_type': 'PERPETUAL'})}"
        )
        if not isinstance(rows, list):
            raise FundingDataError("Invalid Aevo markets rows")
        market = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and row.get("instrument_name") == symbol
            ),
            None,
        )
        if not isinstance(market, dict):
            raise FundingDataError(f"Aevo market unavailable for {symbol}")
        return market


def is_supported_aevo_market(raw: dict[str, Any]) -> bool:
    return (
        raw.get("instrument_type") == "PERPETUAL"
        and raw.get("is_active") is True
        and raw.get("is_rwa") is not True
        and raw.get("market_type") == "crypto"
        and clean_asset_symbol(raw.get("quote_asset")) in {"USDC", "USDT"}
    )


def nanoseconds_to_milliseconds(value: Any) -> int | None:
    try:
        return int(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return None
