from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)
from smart_money_radar.funding.normalization import (
    clean_asset_symbol,
    inferred_funding_intervals,
    iso_from_milliseconds,
    normalize_orderbook,
)


EDGEX_API_URL = "https://edgex-prod-v2.edgex.exchange"


class EdgexFundingClient:
    venue = "edgex"

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = EDGEX_API_URL,
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
        meta = self.http.get_json(f"{self.base_url}/api/v2/public/meta/getMetaData")
        data = meta.get("data") if isinstance(meta, dict) else None
        contracts = (data or {}).get("contractList") if isinstance(data, dict) else None
        if not isinstance(contracts, list):
            raise FundingDataError("Invalid edgeX getMetaData response")

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []

        for raw in contracts:
            if not isinstance(raw, dict):
                continue
            if not raw.get("enableTrade") or not raw.get("enableDisplay"):
                continue
            contract_id = str(raw.get("contractId") or "")
            contract_name = str(raw.get("contractName") or "")
            canonical_asset = clean_asset_symbol(
                contract_name.replace("USDC", "").replace("USDT", "")
            )
            if not contract_id or not contract_name or not canonical_asset:
                continue

            interval_min = max(1.0, as_float(raw.get("fundingRateIntervalMin"), 240.0))
            interval_hours = interval_min / 60.0
            maker_fee = as_float(raw.get("defaultMakerFeeRate"))
            taker_fee = as_float(raw.get("defaultTakerFeeRate"))

            try:
                ticker = self._ticker(contract_id)
            except FundingDataError as exc:
                warnings.append(f"edgeX ticker unavailable for {contract_name}: {exc}")
                continue

            mark_price = as_float(ticker.get("markPrice"))
            index_price = as_float(ticker.get("indexPrice"))
            if mark_price <= 0 or index_price <= 0:
                continue

            funding_rate = as_float(ticker.get("fundingRate"))
            next_funding_ms = _parse_ms(ticker.get("nextFundingTime"))
            next_funding_at = iso_from_milliseconds(next_funding_ms) if next_funding_ms else None

            instruments.append(
                {
                    "venue": self.venue,
                    "symbol": contract_name,
                    "canonical_asset": canonical_asset,
                    "base_asset": canonical_asset,
                    "quote_asset": "USDC",
                    "collateral_asset": "USDC",
                    "contract_type": "linear_perpetual",
                    "contract_multiplier": 1.0,
                    "status": "active",
                    "source_url": f"https://pro.edgex.exchange/trade/{contract_name}",
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
            markets.append(
                {
                    "venue": self.venue,
                    "symbol": contract_name,
                    "canonical_asset": canonical_asset,
                    "funding_rate": funding_rate,
                    "funding_interval_hours": interval_hours,
                    "hourly_funding_rate": funding_rate / interval_hours,
                    "funding_rate_kind": "published_current",
                    "next_funding_at": next_funding_at,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "open_interest_usd": as_float(ticker.get("openInterest")) * mark_price
                    if as_float(ticker.get("openInterest")) > 0
                    else None,
                    "volume_24h_usd": as_float(ticker.get("value")) or None,
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
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
        contract_id = self._resolve_contract_id(symbol)
        level = 200 if limit > 15 else 15
        raw = self.http.get_json(
            f"{self.base_url}/api/v2/public/quote/getDepth"
            f"?contractId={urllib.parse.quote(contract_id)}&level={level}"
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list) or not data:
            raise FundingDataError(f"Invalid edgeX orderbook for {symbol}")
        book = data[0] if isinstance(data[0], dict) else {}
        bid_rows = [
            [as_float(row.get("price")), as_float(row.get("size"))]
            for row in book.get("bids") or []
            if isinstance(row, dict)
        ]
        ask_rows = [
            [as_float(row.get("price")), as_float(row.get("size"))]
            for row in book.get("asks") or []
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
        if not contract.get("enableTrade") or not contract.get("enableDisplay"):
            raise FundingDataError(f"edgeX contract not supported for {symbol}")
        contract_id = str(contract.get("contractId") or "")
        contract_name = str(contract.get("contractName") or symbol)
        if not contract_id:
            raise FundingDataError(f"edgeX contract id unavailable for {symbol}")
        ticker = self._ticker(contract_id)
        mark_price = as_float(ticker.get("markPrice"))
        index_price = as_float(ticker.get("indexPrice"))
        if mark_price <= 0 or index_price <= 0:
            raise FundingDataError(f"edgeX reference prices unavailable for {symbol}")
        interval_hours = max(
            1.0,
            as_float(
                ticker.get("fundingRateIntervalMin"),
                as_float(contract.get("fundingRateIntervalMin"), 240.0),
            )
            / 60.0,
        )
        funding_rate = as_float(ticker.get("fundingRate"))
        return {
            "venue": self.venue,
            "symbol": contract_name,
            "canonical_asset": clean_asset_symbol(canonical_asset),
            "funding_rate": funding_rate,
            "funding_interval_hours": interval_hours,
            "hourly_funding_rate": funding_rate / interval_hours,
            "funding_rate_kind": "published_current",
            "next_funding_at": (
                iso_from_milliseconds(_parse_ms(ticker.get("nextFundingTime")))
                if _parse_ms(ticker.get("nextFundingTime"))
                else None
            ),
            "mark_price": mark_price,
            "index_price": index_price,
            "open_interest_usd": as_float(ticker.get("openInterest")) * mark_price
            if as_float(ticker.get("openInterest")) > 0
            else None,
            "volume_24h_usd": as_float(ticker.get("value")) or None,
            "maker_fee_rate": as_float(contract.get("defaultMakerFeeRate")),
            "taker_fee_rate": as_float(contract.get("defaultTakerFeeRate")),
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
        contract_id = self._resolve_contract_id(symbol)
        rows_by_time: dict[int, dict[str, Any]] = {}
        offset = ""
        for _ in range(10):
            params: dict[str, str] = {
                "contractId": contract_id,
                "size": "100",
                "filterSettlementFundingRate": "true",
                "filterBeginTimeInclusive": str(start_time_ms),
            }
            if offset:
                params["offsetData"] = offset
            raw = self.http.get_json(
                f"{self.base_url}/api/v2/public/funding/getFundingRatePage"
                f"?{urllib.parse.urlencode(params)}"
            )
            data = raw.get("data") if isinstance(raw, dict) else None
            page = (data or {}).get("dataList") if isinstance(data, dict) else None
            if not isinstance(page, list):
                raise FundingDataError(f"Invalid edgeX funding history for {symbol}")
            for row in page:
                if not isinstance(row, dict):
                    continue
                ts = _parse_ms(row.get("fundingTime"))
                if ts is None or ts < start_time_ms:
                    continue
                rows_by_time[ts] = row
            next_offset = (data or {}).get("nextPageOffsetData") if isinstance(data, dict) else None
            if not next_offset or not page or len(page) < 100:
                break
            offset = str(next_offset)

        timestamps = sorted(rows_by_time)
        intervals = inferred_funding_intervals(
            timestamps,
            interval_hours,
            units_per_second=1000.0,
        )
        rows: list[dict[str, Any]] = []
        for ts in timestamps:
            raw = rows_by_time[ts]
            rate = as_float(raw.get("fundingRate"))
            actual_interval = intervals.get(ts, interval_hours)
            rows.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "funding_at": iso_from_milliseconds(ts),
                    "funding_rate": rate,
                    "funding_interval_hours": actual_interval,
                    "hourly_funding_rate": rate / actual_interval,
                    "mark_price": as_float(raw.get("indexPrice")) or None,
                    "observed_at": observed_at,
                    "raw": raw,
                }
            )
        return [row for row in rows if row["funding_at"]]

    def _ticker(self, contract_id: str) -> dict[str, Any]:
        raw = self.http.get_json(
            f"{self.base_url}/api/v2/public/quote/getTicker"
            f"?contractId={urllib.parse.quote(contract_id)}"
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list) or not data:
            raise FundingDataError(f"Invalid edgeX ticker for contract {contract_id}")
        return data[0] if isinstance(data[0], dict) else {}

    def _contract_for_symbol(self, symbol: str) -> dict[str, Any]:
        meta = self.http.get_json(f"{self.base_url}/api/v2/public/meta/getMetaData")
        data = meta.get("data") if isinstance(meta, dict) else None
        contracts = (data or {}).get("contractList") if isinstance(data, dict) else []
        for contract in contracts:
            if (
                isinstance(contract, dict)
                and str(contract.get("contractName") or "") == symbol
            ):
                return contract
        raise FundingDataError(f"edgeX contract not found for {symbol}")

    def _resolve_contract_id(self, symbol: str) -> str:
        contract = self._contract_for_symbol(symbol)
        return str(contract.get("contractId") or "")


def _parse_ms(value: Any) -> int | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return ms if ms > 0 else None
