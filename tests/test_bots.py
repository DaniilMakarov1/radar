from __future__ import annotations

import unittest
from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock, patch

from smart_money_radar.bots.strategies.base import Opportunity
from smart_money_radar.bots.strategies.funding_carry import FundingCarryStrategy
from smart_money_radar.bots.strategies.spread_arb import SpreadArbStrategy
from smart_money_radar.bots.telegram import fmt_money, fmt_seconds, fmt_signed, tg
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.risex.bot import (
    PaperPosition,
    RiseXBot,
    RiseXBotConfig,
    RISEX_VENUE,
)


def _market(
    venue: str,
    symbol: str,
    asset: str,
    price: float = 50_000.0,
    funding_rate: float = 0.0001,
    interval_hours: float = 8.0,
) -> dict[str, Any]:
    return {
        "venue": venue,
        "symbol": symbol,
        "canonical_asset": asset,
        "mark_price": price,
        "index_price": price,
        "funding_rate": funding_rate,
        "funding_interval_hours": interval_hours,
        "hourly_funding_rate": funding_rate / interval_hours,
        "next_funding_at": "2026-07-25T16:00:00+00:00",
        "volume_24h_usd": 10_000_000.0,
        "contract_type": "perpetual",
        "contract_multiplier": 1.0,
        "canonical_unit_multiplier": 1.0,
    }


def _orderbook(mid: float, depth_usd: float = 100_000.0, spread_pct: float = 0.0001) -> dict[str, Any]:
    """Create a synthetic orderbook.  ``spread_pct`` is the half-spread
    (default 1 bp) so the bid/ask are ``mid * (1 ∓ spread_pct)``."""
    half = depth_usd / 2.0
    bid_price = mid * (1 - spread_pct)
    ask_price = mid * (1 + spread_pct)
    return {
        "mid_price": mid,
        "bid_depth_usd": half,
        "ask_depth_usd": half,
        "bids": [[bid_price, half / bid_price]],
        "asks": [[ask_price, half / ask_price]],
    }


class TelegramFormatterTest(unittest.TestCase):
    def test_tg_escapes_html(self) -> None:
        self.assertEqual(tg("<b>bold</b>"), "&lt;b&gt;bold&lt;/b&gt;")

    def test_tg_none(self) -> None:
        self.assertEqual(tg(None), "-")

    def test_fmt_money(self) -> None:
        self.assertEqual(fmt_money(123.456), "$123.46")
        self.assertEqual(fmt_money(None), "-")

    def test_fmt_signed(self) -> None:
        self.assertEqual(fmt_signed(10.5), "+$10.50")
        self.assertEqual(fmt_signed(-3.2), "-$3.20")

    def test_fmt_seconds(self) -> None:
        self.assertEqual(fmt_seconds(3661), "1h 1m")
        self.assertEqual(fmt_seconds(125), "2m 5s")
        self.assertEqual(fmt_seconds(None), "-")


class OpportunityTest(unittest.TestCase):
    def test_asdict_roundtrip(self) -> None:
        opp = Opportunity(
            strategy="spread_arb",
            canonical_asset="BTC",
            primary_side="long",
            hedge_venue="hyperliquid",
            hedge_side="short",
            notional=500.0,
            spread_bps=15.0,
            net_profit=2.5,
            total_cost=1.0,
            gross_profit=3.5,
            primary_price=50_000.0,
            hedge_price=50_050.0,
            primary_funding_rate=0.0001,
            hedge_funding_rate=0.00005,
            primary_symbol="BTC-PERP",
            hedge_symbol="BTC",
            extra={"primary_venue": "risex", "entry_spread_per_unit": 50.0},
        )
        d = asdict(opp)
        self.assertEqual(d["strategy"], "spread_arb")
        self.assertEqual(d["primary_side"], "long")
        self.assertEqual(d["extra"]["entry_spread_per_unit"], 50.0)
        self.assertEqual(d["hedge_venue"], "hyperliquid")


class SpreadArbStrategyTest(unittest.TestCase):
    def _make_strategy(self) -> SpreadArbStrategy:
        target_client = MagicMock()
        hedge_client = MagicMock()
        config = FundingScanConfig(
            target_notional=500.0,
            basis_reserve_bps=5.0,
            operations_buffer_bps=2.0,
        ).validated()
        return SpreadArbStrategy(
            target_venue="risex",
            target_client=target_client,
            hedge_clients={"hyperliquid": hedge_client},
            scan_config=config,
        )

    def test_no_opportunity_when_prices_equal(self) -> None:
        strategy = self._make_strategy()
        strategy.target_client.catalog_and_markets.return_value = (
            [],
            [_market("risex", "BTC-PERP", "BTC", price=50_000.0)],
            [],
        )
        hedge_client = strategy.hedge_clients["hyperliquid"]
        hedge_client.catalog_and_markets.return_value = (
            [],
            [_market("hyperliquid", "BTC", "BTC", price=50_000.0)],
            [],
        )
        strategy.target_client.orderbook.return_value = _orderbook(50_000.0)
        hedge_client.orderbook.return_value = _orderbook(50_000.0)

        opps = strategy.scan("2026-07-25T12:00:00+00:00")
        self.assertEqual(len(opps), 0)

    def test_opportunity_when_spread_exists(self) -> None:
        strategy = self._make_strategy()
        strategy.target_client.catalog_and_markets.return_value = (
            [],
            [_market("risex", "BTC-PERP", "BTC", price=49_900.0)],
            [],
        )
        hedge_client = strategy.hedge_clients["hyperliquid"]
        hedge_client.catalog_and_markets.return_value = (
            [],
            [_market("hyperliquid", "BTC", "BTC", price=50_100.0)],
            [],
        )
        strategy.target_client.orderbook.return_value = _orderbook(49_900.0, 200_000.0)
        hedge_client.orderbook.return_value = _orderbook(50_100.0, 200_000.0)

        opps = strategy.scan("2026-07-25T12:00:00+00:00")
        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp.strategy, "spread_arb")
        self.assertEqual(opp.canonical_asset, "BTC")
        self.assertEqual(opp.primary_side, "long")
        self.assertEqual(opp.hedge_side, "short")
        self.assertGreater(opp.net_profit, 0)
        self.assertGreater(opp.spread_bps, 0)

    def test_no_opportunity_when_spread_too_small(self) -> None:
        strategy = self._make_strategy()
        strategy.target_client.catalog_and_markets.return_value = (
            [],
            [_market("risex", "BTC-PERP", "BTC", price=50_000.0)],
            [],
        )
        hedge_client = strategy.hedge_clients["hyperliquid"]
        hedge_client.catalog_and_markets.return_value = (
            [],
            [_market("hyperliquid", "BTC", "BTC", price=50_001.0)],
            [],
        )
        strategy.target_client.orderbook.return_value = _orderbook(50_000.0, 200_000.0)
        hedge_client.orderbook.return_value = _orderbook(50_001.0, 200_000.0)

        opps = strategy.scan("2026-07-25T12:00:00+00:00")
        self.assertEqual(len(opps), 0)


class RiseXBotConfigTest(unittest.TestCase):
    def test_scan_config(self) -> None:
        config = RiseXBotConfig(target_notional_per_leg=500.0).validated()
        scan_config = config.scan_config()
        self.assertEqual(scan_config.target_notional, 500.0)
        self.assertEqual(scan_config.horizon_mode, "next_settlement")


class RiseXBotBalanceCheckTest(unittest.TestCase):
    def test_can_open_checks_balance(self) -> None:
        store = MagicMock()
        config = RiseXBotConfig(
            venue_starting_balance=100.0,
            target_notional_per_leg=500.0,
            telegram_enabled=False,
        ).validated()
        bot = RiseXBot(store, config=config)

        opp = {
            "strategy": "spread_arb",
            "canonical_asset": "BTC",
            "primary_side": "long",
            "hedge_venue": "hyperliquid",
            "hedge_side": "short",
            "notional": 500.0,
            "total_cost": 1.0,
        }

        # Drain balance below threshold: fees(1) + notional*0.1(50) = 51
        bot.venue_balances[RISEX_VENUE] = 40.0
        self.assertFalse(bot._can_open(opp))

        # Restore balance
        bot.venue_balances[RISEX_VENUE] = 1000.0
        bot.venue_balances["hyperliquid"] = 1000.0
        self.assertTrue(bot._can_open(opp))

    def test_can_open_unlimited_positions_if_balance_allows(self) -> None:
        store = MagicMock()
        config = RiseXBotConfig(
            venue_starting_balance=10_000.0,
            target_notional_per_leg=500.0,
            telegram_enabled=False,
        ).validated()
        bot = RiseXBot(store, config=config)

        opp = {
            "strategy": "spread_arb",
            "canonical_asset": "BTC",
            "primary_side": "long",
            "hedge_venue": "hyperliquid",
            "hedge_side": "short",
            "notional": 500.0,
            "total_cost": 1.0,
        }

        # Add 10 open positions — should still be able to open more
        for i in range(10):
            bot.positions.append(
                PaperPosition(
                    position_id=i + 1,
                    strategy="spread_arb",
                    canonical_asset=f"ASSET{i}",
                    primary_side="long",
                    hedge_venue="hyperliquid",
                    hedge_side="short",
                    notional_usd=500.0,
                    primary_entry_price=100.0,
                    hedge_entry_price=100.0,
                    opened_at="2026-07-25T12:00:00+00:00",
                    target_close_at="2026-07-25T16:00:00+00:00",
                    primary_funding_rate=0.0001,
                    hedge_funding_rate=0.00005,
                    spread_bps=5.0,
                )
            )
        self.assertTrue(bot._can_open(opp))


if __name__ == "__main__":
    unittest.main()
