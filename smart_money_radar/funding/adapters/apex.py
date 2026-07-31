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
    parse_timestamp,
)


APEX_API_URL = "https://omni.apex.exchange/api"
APEX_FUNDING_INTERVAL_HOURS = 1.0
APEX_MAKER_FEE_RATE = 0.0002
APEX_TAKER_FEE_RATE = 0.0005


class ApexFundingClient:
    venue = "apex"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = APEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(
            min_delay_seconds=0.1,
            max_retries=2,
        )
        self.base_url = base_url.rstrip("/")

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raw_config = self.http.get_json(f"{self.base_url}/v3/config")
        data = raw_config.get("data") if isinstance(raw_config, dict) else None
        config = data.get("contractConfig") if isinstance(data, dict) else None
        contracts = (config or {}).get("perpetualContract") if isinstance(config, dict) else None
        if not isinstance(contracts, list):
            raise FundingDataError("Invalid ApeX v3/config response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []

        for raw in contracts:
            if not isinstance(raw, dict):
                continue
            if not raw.get("enableTrade") or not raw.get("enableFundingSettlement"):
                continue
            if raw.get("isPrelaunch"):
                continue
            symbol_dash = str(raw.get("symbol") or "")
            symbol_display = str(raw.get("symbolDisplayName") or "")
            base_token = str(raw.get("baseTokenId") or "")
            canonical_asset = clean_asset_symbol(base_token)
            if not symbol_dash or not symbol_display or not canonical_asset:
                continue

            try:
                ticker = self._ticker(symbol_display)
            except FundingDataError as exc:
                warnings.append(f"ApeX ticker unavailable for {symbol_display}: {exc}")
                continue

            mark_price = as_float(ticker.get("markPrice"))
            index_price = as_float(ticker.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue

            funding_rate = as_float(ticker.get("fundingRate"))
            next_funding_at = _parse_iso(ticker.get("nextFundingTime"))

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": symbol_dash,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USDT",
                    "collateral_asset": "USDT",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://omni.apex.exchange/trade/{symbol_display}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": symbol_dash,
                    "canonical_asset": canonical_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": APEX_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": funding_rate,
                    "funding_rate_kind": "published_current_hour_estimate",
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("openInterest")) * mark_price
                    if as_float(ticker.get("openInterest")) > 0
                    else None,
                    "volume_24h_usd": as_float(ticker.get("turnover24h")) or None,
                    "maker_fee_rate": APEX_MAKER_FEE_RATE,
                    "taker_fee_rate": APEX_TAKER_FEE_RATE,
                    "fee_source": "venue_public_flat_fee",
                    "observed_at": observed_at,
                    "raw": {"contract": raw, "ticker": ticker},
                }
            )
        return instruments, markets, warnings

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        display_symbol = symbol.replace("-", "")
        raw = self.http.get_json(
            f"{self.base_url}/v3/depth?symbol={display_symbol}"
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise FundingDataError(f"Invalid ApeX orderbook for {symbol}")
        bid_rows = data.get("b") or []
        ask_rows = data.get("a") or []
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
        previous = previous_market or {}
        raw_previous = (
            previous.get("raw") if isinstance(previous.get("raw"), dict) else {}
        )
        contract = (
            raw_previous.get("contract")
            if isinstance(raw_previous.get("contract"), dict)
            else {}
        )
        if not contract:
            contract = self._contract_for_symbol(symbol)
        if (
            not contract.get("enableTrade")
            or not contract.get("enableFundingSettlement")
            or contract.get("isPrelaunch")
        ):
            raise FundingDataError(f"ApeX contract not supported for {symbol}")
        symbol_dash = str(contract.get("symbol") or symbol)
        symbol_display = str(contract.get("symbolDisplayName") or symbol_dash.replace("-", ""))
        ticker = self._ticker(symbol_display)
        mark_price = as_float(ticker.get("markPrice"))
        index_price = as_float(ticker.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"ApeX reference prices unavailable for {symbol}")
        funding_rate = as_float(ticker.get("fundingRate"))
        return {
            "venue": self.venue,
            "symbol": symbol_dash,
            "canonical_asset": clean_asset_symbol(canonical_asset),
            "funding_rate": funding_rate,
            "funding_interval_hours": APEX_FUNDING_INTERVAL_HOURS,
            "hourly_funding_rate": funding_rate,
            "funding_rate_kind": "published_current_hour_estimate",
            "next_funding_at": _parse_iso(ticker.get("nextFundingTime")),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("openInterest")) * mark_price
            if as_float(ticker.get("openInterest")) > 0
            else None,
            "volume_24h_usd": as_float(ticker.get("turnover24h")) or None,
            "maker_fee_rate": APEX_MAKER_FEE_RATE,
            "taker_fee_rate": APEX_TAKER_FEE_RATE,
            "fee_source": "venue_public_flat_fee",
            "contract_multiplier": previous.get("contract_multiplier") or 1.0,
            "canonical_unit_multiplier": previous.get("canonical_unit_multiplier", 1.0),
            "observed_at": observed_at,
            "raw": {"contract": contract, "ticker": ticker},
        }

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        dash_symbol = symbol if "-" in symbol else _to_dash_symbol(symbol)
        raw = self.http.get_json(
            f"{self.base_url}/v3/history-funding?symbol={dash_symbol}"
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        funds = (data or {}).get("historyFunds") if isinstance(data, dict) else None
        if not isinstance(funds, list):
            raise FundingDataError(f"Invalid ApeX funding history for {symbol}")

        rows: list[dict[str, Any]] = []
        for entry in funds:
            if not isinstance(entry, dict):
                continue
            ts = _parse_ms(entry.get("fundingTime"))
            if ts is None or ts < start_time_ms:
                continue
            rate = as_float(entry.get("rate"))
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(ts),
                    "funding_rate": rate,
                    "funding_interval_hours": APEX_FUNDING_INTERVAL_HOURS,
                    "hourly_funding_rate": rate,
                    "mark_price": as_float(entry.get("price")) or None,
                    "observed_at": observed_at,
                    "raw": entry,
                }
            )
        rows.sort(key=lambda r: r["funding_at"] or "")
        return [row for row in rows if row["funding_at"]]

    def _ticker(self, symbol_display: str) -> dict[str, Any]:
        raw = self.http.get_json(
            f"{self.base_url}/v3/ticker?symbol={symbol_display}"
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list) or not data:
            raise FundingDataError(f"Invalid ApeX ticker for {symbol_display}")
        return data[0] if isinstance(data[0], dict) else {}

    def _contract_for_symbol(self, symbol: str) -> dict[str, Any]:
        raw_config = self.http.get_json(f"{self.base_url}/v3/config")
        data = raw_config.get("data") if isinstance(raw_config, dict) else None
        config = data.get("contractConfig") if isinstance(data, dict) else None
        contracts = (
            (config or {}).get("perpetualContract")
            if isinstance(config, dict)
            else None
        )
        if not isinstance(contracts, list):
            raise FundingDataError("Invalid ApeX v3/config response")
        target = str(symbol or "").replace("-", "").upper()
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            candidates = {
                str(contract.get("symbol") or "").replace("-", "").upper(),
                str(contract.get("symbolDisplayName") or "").upper(),
            }
            if target in candidates:
                return contract
        raise FundingDataError(f"ApeX contract not found for {symbol}")


def _parse_ms(value: Any) -> int | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return ms if ms > 0 else None


def _parse_iso(value: Any) -> str | None:
    if not value:
        return None
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else None


def _to_dash_symbol(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT"
    if symbol.endswith("USDC"):
        return f"{symbol[:-4]}-USDC"
    return symbol
