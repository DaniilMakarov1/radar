from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.prediction.intelligence import (
    build_event_token_links,
    score_prediction_wallet,
)
from smart_money_radar.prediction.normalization import (
    fair_market_selection,
    normalize_polymarket_catalog,
)
from smart_money_radar.prediction.paper import PaperConfig, simulate_paper_route
from smart_money_radar.prediction.scanner import (
    ScannerConfig,
    match_cross_venue_contracts,
    scan_prediction_routes,
)
from smart_money_radar.storage import SQLiteStore


NOW = "2026-07-12T06:00:00+00:00"


class PredictionRadarTest(unittest.TestCase):
    def test_augmented_negative_risk_is_not_treated_as_exhaustive(self) -> None:
        events, markets = normalize_polymarket_catalog(
            [
                {
                    "id": "event-1",
                    "title": "Winner",
                    "slug": "winner",
                    "active": True,
                    "closed": False,
                    "negRisk": True,
                    "enableNegRisk": True,
                    "negRiskAugmented": True,
                    "markets": [polymarket_raw_market("m1")],
                }
            ],
            NOW,
            10,
        )

        self.assertFalse(events[0]["exhaustive"])
        self.assertTrue(events[0]["augmented_neg_risk"])
        self.assertFalse(markets[0]["event_exhaustive"])

    def test_market_budget_is_allocated_across_events(self) -> None:
        raw_events = [
            {"id": f"event-{index}", "markets": [{"id": f"{index}-{n}"} for n in range(5)]}
            for index in range(3)
        ]

        selected = fair_market_selection(raw_events, max_markets=4)

        self.assertEqual([len(row[1]) for row in selected], [2, 1, 1])

    def test_closed_outcome_blocks_complete_set(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [market_row("a", "Alpha", 0), market_row("b", "Beta", 0)]
        markets[1]["status"] = "closed"
        markets[1]["accepting_orders"] = False
        books = [book_row("a", yes_ask=0.45), book_row("b", yes_ask=0.45)]

        _, _, routes = scan_prediction_routes(events, markets, books, NOW)

        self.assertFalse(any(row["route_type"] == "complete_set" for row in routes))

    def test_bare_short_ticker_without_crypto_context_is_not_linked(self) -> None:
        links = build_event_token_links(
            [
                {
                    "venue": "polymarket",
                    "event_id": "opg-event",
                    "title": "Will OPG publish a report?",
                    "description": "A corporate publication question.",
                    "resolution_rules": "Resolves from the publisher website.",
                }
            ],
            [{"token_id": 1, "symbol": "OPG", "name": "OpenGradient"}],
            NOW,
        )

        self.assertEqual(links, [])

    def test_fees_remove_apparent_complete_set_discount(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [
            market_row("a", "Alpha", 0.05),
            market_row("b", "Beta", 0.05),
        ]
        books = [book_row("a", yes_ask=0.49), book_row("b", yes_ask=0.49)]

        _, _, routes = scan_prediction_routes(events, markets, books, NOW)
        route = next(row for row in routes if row["route_type"] == "complete_set")

        self.assertEqual(route["status"], "not_profitable")
        self.assertGreater(route["expected_gross_profit"], 0)
        self.assertLess(route["expected_net_profit"], 0)

    def test_full_depth_complete_set_is_only_a_paper_candidate(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [market_row("a", "Alpha", 0), market_row("b", "Beta", 0)]
        books = [book_row("a", yes_ask=0.45), book_row("b", yes_ask=0.45)]

        _, _, routes = scan_prediction_routes(
            events,
            markets,
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
        )
        route = next(row for row in routes if row["route_type"] == "complete_set")

        self.assertEqual(route["status"], "paper_candidate")
        self.assertAlmostEqual(route["optimal_size"], 100)
        self.assertGreater(route["expected_net_profit"], 9)

    def test_threshold_implication_route_is_scanned(self) -> None:
        markets = [
            market_row(
                "high",
                "Will Bitcoin be above $150,000 on Dec 31?",
                0,
                event_id="btc-high",
            ),
            market_row(
                "low",
                "Will Bitcoin be above $100,000 on Dec 31?",
                0,
                event_id="btc-low",
            ),
        ]
        books = [
            book_row("high", yes_ask=0.61, no_ask=0.40),
            book_row("low", yes_ask=0.55, no_ask=0.46),
        ]

        constraints, _, routes = scan_prediction_routes([], markets, books, NOW)
        implication = next(
            row for row in routes if row["route_type"] == "logical_implication"
        )

        self.assertEqual(len(constraints), 1)
        self.assertEqual(implication["status"], "contract_review")

    def test_cross_venue_mismatch_is_never_executable(self) -> None:
        poly = market_row("poly-fr", "Will France win the World Cup?", 0)
        poly.update(
            venue="polymarket",
            event_title="World Cup Winner",
            outcome_label="France",
            cancellation_rules="If canceled resolves Other.",
        )
        kalshi = market_row("kalshi-fr", "France wins the World Cup", 0)
        kalshi.update(
            venue="kalshi",
            event_title="FIFA World Cup Winner",
            outcome_label="France",
            cancellation_rules=None,
        )
        books = [
            {**book_row("poly-fr", yes_ask=0.40, no_ask=0.61), "venue": "polymarket"},
            {**book_row("kalshi-fr", yes_ask=0.42, no_ask=0.58), "venue": "kalshi"},
        ]

        matches = match_cross_venue_contracts([poly, kalshi], NOW)
        _, _, routes = scan_prediction_routes([], [poly, kalshi], books, NOW)
        cross = [row for row in routes if row["route_type"] == "cross_venue_complement"]

        self.assertEqual(len(matches), 1)
        self.assertFalse(matches[0]["cancellation_match"])
        self.assertTrue(cross)
        self.assertTrue(all(row["status"] == "contract_review" for row in cross))

    def test_paper_execution_applies_latency_and_depth_haircut(self) -> None:
        route = {
            "prediction_route_id": 7,
            "status": "paper_candidate",
            "observed_at": NOW,
            "optimal_size": 100,
            "expected_net_profit": 10,
            "guaranteed_payout_per_share": 1,
            "legs": [
                {
                    "venue": "polymarket",
                    "market_id": "a",
                    "action": "buy",
                    "outcome": "yes",
                    "liquidity_role": "taker",
                    "levels": [[0.45, 100]],
                    "fee_rate": 0,
                    "fee_verified": True,
                    "tick_size": 0.01,
                    "min_order_size": 1,
                },
                {
                    "venue": "polymarket",
                    "market_id": "b",
                    "action": "buy",
                    "outcome": "yes",
                    "liquidity_role": "taker",
                    "levels": [[0.45, 100]],
                    "fee_rate": 0,
                    "fee_verified": True,
                    "tick_size": 0.01,
                    "min_order_size": 1,
                },
            ],
        }

        result = simulate_paper_route(
            route,
            {
                ("polymarket", "a"): {
                    **book_row("a", yes_ask=0.45),
                    "observed_at": "2026-07-12T06:00:01+00:00",
                },
                ("polymarket", "b"): {
                    **book_row("b", yes_ask=0.45),
                    "observed_at": "2026-07-12T06:00:01+00:00",
                },
            },
            "2026-07-12T06:00:01+00:00",
            PaperConfig(target_size=100, minimum_latency_ms=750, depth_haircut=0.5),
        )

        self.assertEqual(result["status"], "partial_profitable")
        self.assertEqual(result["filled_size"], 50)
        self.assertGreater(result["simulated_net_profit"], 0)

    def test_wallet_score_penalizes_concentrated_pnl(self) -> None:
        positions = [
            {
                "asset_id": str(index),
                "event_slug": str(index),
                "category": "politics",
                "average_price": 0.5,
                "total_bought": 100,
                "realized_pnl": 1_000 if index == 0 else -10,
                "closed_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
            }
            for index in range(10)
        ]

        score = score_prediction_wallet(
            {
                "proxyWallet": "0x" + "1" * 40,
                "rank": "1",
                "userName": "Test",
                "pnl": 910,
                "vol": 10_000,
            },
            positions,
            NOW,
        )

        self.assertGreater(score["profit_concentration"], 0.9)
        self.assertEqual(score["label"], "concentrated_pnl")

    def test_wallet_with_negative_pnl_cannot_receive_watch_label(self) -> None:
        positions = [
            {
                "asset_id": str(index),
                "event_slug": str(index),
                "category": "sports",
                "average_price": 0.5,
                "total_bought": 100,
                "realized_pnl": 10 if index < 8 else -100,
                "closed_at": f"2026-02-{index + 1:02d}T00:00:00+00:00",
                "history_complete": True,
            }
            for index in range(10)
        ]

        score = score_prediction_wallet(
            {
                "proxyWallet": "0x" + "2" * 40,
                "rank": "2",
                "userName": "Negative",
                "pnl": -120,
                "vol": 5_000,
            },
            positions,
            NOW,
        )

        self.assertEqual(score["label"], "negative_sample")

    def test_prediction_storage_initializes_and_returns_dashboard(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"test": True})
            store.finish_prediction_scan(scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual(dashboard["latest_scan"]["prediction_scan_id"], scan_id)
        self.assertEqual(dashboard["execution_mode"], "paper_only_research")


def event_row(exhaustive: bool) -> dict[str, object]:
    return {
        "venue": "polymarket",
        "event_id": "event",
        "title": "Winner",
        "mutually_exclusive": True,
        "exhaustive": exhaustive,
        "neg_risk": False,
        "augmented_neg_risk": False,
        "expected_resolution_at": "2026-07-20T00:00:00+00:00",
        "source_url": "https://example.com",
    }


def market_row(
    market_id: str,
    question: str,
    fee_rate: float,
    event_id: str = "event",
) -> dict[str, object]:
    return {
        "venue": "polymarket",
        "market_id": market_id,
        "event_id": event_id,
        "event_title": question,
        "question": question,
        "outcome_label": question,
        "status": "open",
        "accepting_orders": True,
        "fee_rate": fee_rate,
        "fee_verified": True,
        "maker_fee_rate": 0,
        "tick_size": 0.01,
        "min_order_size": 1,
        "closes_at": "2026-12-31T00:00:00+00:00",
        "expected_resolution_at": "2026-12-31T00:00:00+00:00",
        "resolution_rules": question,
        "cancellation_rules": None,
        "event_source_url": "https://example.com",
    }


def book_row(
    market_id: str,
    yes_ask: float,
    no_ask: float | None = None,
) -> dict[str, object]:
    no_ask = 1 - yes_ask + 0.01 if no_ask is None else no_ask
    return {
        "venue": "polymarket",
        "market_id": market_id,
        "yes_bids": [[max(0, yes_ask - 0.01), 100]],
        "yes_asks": [[yes_ask, 100]],
        "no_bids": [[max(0, no_ask - 0.01), 100]],
        "no_asks": [[no_ask, 100]],
    }


def polymarket_raw_market(market_id: str) -> dict[str, object]:
    return {
        "id": market_id,
        "question": "Will Alpha win?",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": '["yes", "no"]',
        "feeSchedule": {"rate": 0.05},
        "feesEnabled": True,
    }


if __name__ == "__main__":
    unittest.main()
