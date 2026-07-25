from __future__ import annotations

from typing import Any

from smart_money_radar.bots.strategies.base import Opportunity
from smart_money_radar.funding.adapters.base import FundingDataError, FundingVenueClient
from smart_money_radar.funding.economics import (
    directional_slippage,
    fill_quantity,
    market_fee_rate,
)
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import canonical_asset_symbol


class SpreadArbStrategy:
    """Executable price-spread arbitrage between a target venue and hedge
    venues.  Buys on the cheaper side, sells on the more expensive, and
    closes when the spread converges or a time-stop fires."""

    name = "spread_arb"

    def __init__(
        self,
        target_venue: str,
        target_client: FundingVenueClient,
        hedge_clients: dict[str, FundingVenueClient],
        scan_config: FundingScanConfig,
        max_hold_hours: float = 4.0,
        convergence_threshold: float = 0.3,
    ) -> None:
        self.target_venue = target_venue
        self.target_client = target_client
        self.hedge_clients = hedge_clients
        self.scan_config = scan_config
        self.max_hold_hours = max_hold_hours
        self.convergence_threshold = convergence_threshold

    def scan(self, observed_at: str) -> list[Opportunity]:
        try:
            _, target_markets, _ = self.target_client.catalog_and_markets(observed_at)
        except FundingDataError:
            return []

        target_by_asset: dict[str, dict[str, Any]] = {}
        for m in target_markets:
            asset = canonical_asset_symbol(m.get("canonical_asset"))
            if asset:
                target_by_asset[asset] = m

        opportunities: list[Opportunity] = []

        for venue_name, client in self.hedge_clients.items():
            try:
                _, hedge_markets, _ = client.catalog_and_markets(observed_at)
            except FundingDataError:
                continue

            hedge_by_asset: dict[str, dict[str, Any]] = {}
            for m in hedge_markets:
                asset = canonical_asset_symbol(m.get("canonical_asset"))
                if asset:
                    hedge_by_asset[asset] = m

            for asset, target_m in target_by_asset.items():
                hedge_m = hedge_by_asset.get(asset)
                if not hedge_m:
                    continue
                try:
                    target_book = self.target_client.orderbook(
                        target_m["symbol"], observed_at
                    )
                    hedge_book = client.orderbook(hedge_m["symbol"], observed_at)
                except FundingDataError:
                    continue

                opp = self._evaluate(
                    asset,
                    target_m,
                    hedge_m,
                    target_book,
                    hedge_book,
                    venue_name,
                )
                if opp is not None:
                    opportunities.append(opp)

        return opportunities

    def _evaluate(
        self,
        asset: str,
        target_market: dict[str, Any],
        hedge_market: dict[str, Any],
        target_book: dict[str, Any],
        hedge_book: dict[str, Any],
        hedge_venue: str,
    ) -> Opportunity | None:
        target_mid = float(target_book.get("mid_price") or 0)
        hedge_mid = float(hedge_book.get("mid_price") or 0)
        if target_mid <= 0 or hedge_mid <= 0:
            return None

        if target_mid < hedge_mid:
            buy_book, sell_book = target_book, hedge_book
            buy_market, sell_market = target_market, hedge_market
            primary_side, hedge_side = "long", "short"
        else:
            buy_book, sell_book = hedge_book, target_book
            buy_market, sell_market = hedge_market, target_market
            primary_side, hedge_side = "short", "long"

        reference_price = min(target_mid, hedge_mid)
        price_spread_bps = abs(target_mid - hedge_mid) / reference_price * 10_000

        notional = min(self.scan_config.target_notional, reference_price * 1_000_000)
        if notional < 50:
            return None

        target_qty = notional / reference_price
        buy_open = fill_quantity(buy_book.get("asks", []), target_qty)
        sell_open = fill_quantity(sell_book.get("bids", []), target_qty)
        buy_close = fill_quantity(buy_book.get("bids", []), target_qty)
        sell_close = fill_quantity(sell_book.get("asks", []), target_qty)

        fills = [buy_open, sell_open, buy_close, sell_close]
        if not all(f["filled_size"] >= target_qty * 0.999 for f in fills):
            return None

        buy_fee_rate = market_fee_rate(buy_market)
        sell_fee_rate = market_fee_rate(sell_market)

        total_fees = buy_fee_rate * (
            buy_open["filled_notional"] + buy_close["filled_notional"]
        ) + sell_fee_rate * (
            sell_open["filled_notional"] + sell_close["filled_notional"]
        )

        slippage = (
            directional_slippage(buy_open, buy_book.get("mid_price"), "buy")
            + directional_slippage(sell_open, sell_book.get("mid_price"), "sell")
            + directional_slippage(buy_close, buy_book.get("mid_price"), "sell")
            + directional_slippage(sell_close, sell_book.get("mid_price"), "buy")
        )

        funding_notional = (
            buy_open["filled_notional"] + sell_open["filled_notional"]
        ) / 2.0
        basis_reserve = funding_notional * self.scan_config.basis_reserve_bps / 10_000.0
        ops_buffer = (
            funding_notional * self.scan_config.operations_buffer_bps / 10_000.0
        )
        total_cost = total_fees + slippage + basis_reserve + ops_buffer

        entry_spread_per_unit = sell_open["vwap"] - buy_open["vwap"]
        gross_profit = entry_spread_per_unit * target_qty

        target_funding = float(target_market.get("hourly_funding_rate") or 0)
        hedge_funding = float(hedge_market.get("hourly_funding_rate") or 0)
        if primary_side == "long":
            expected_funding = (
                (hedge_funding - target_funding)
                * funding_notional
                * self.max_hold_hours
            )
        else:
            expected_funding = (
                (target_funding - hedge_funding)
                * funding_notional
                * self.max_hold_hours
            )

        net_profit = gross_profit + expected_funding - total_cost

        if net_profit < 0:
            return None

        return Opportunity(
            strategy=self.name,
            canonical_asset=asset,
            primary_side=primary_side,
            hedge_venue=hedge_venue,
            hedge_side=hedge_side,
            notional=funding_notional,
            spread_bps=price_spread_bps,
            net_profit=net_profit,
            total_cost=total_cost,
            gross_profit=gross_profit,
            primary_price=target_mid,
            hedge_price=hedge_mid,
            primary_funding_rate=float(target_market.get("hourly_funding_rate") or 0),
            hedge_funding_rate=float(hedge_market.get("hourly_funding_rate") or 0),
            primary_symbol=str(target_market.get("symbol", "")),
            hedge_symbol=str(hedge_market.get("symbol", "")),
            extra={
                "primary_venue": self.target_venue,
                "entry_spread_per_unit": entry_spread_per_unit,
                "target_quantity": target_qty,
            },
        )
