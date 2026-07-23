from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import UTC, datetime, timedelta
import unittest

from smart_money_radar.prediction.intelligence import (
    PREDICTION_EVENT_TOKEN_LINK_SOURCE,
    build_event_token_links,
    score_prediction_wallet,
)
from smart_money_radar.prediction.mappings import (
    load_prediction_verified_mapping_file,
)
from smart_money_radar.prediction.clients import KalshiClient, PredictionDataError
from smart_money_radar.prediction.normalization import (
    fair_market_selection,
    normalize_hyperliquid_catalog,
    normalize_hyperliquid_orderbook,
    normalize_kalshi_catalog,
    normalize_polymarket_catalog,
)
from smart_money_radar.prediction.paper import PaperConfig, simulate_paper_route
from smart_money_radar.prediction.scanner import (
    ScannerConfig,
    build_prediction_route_candidates,
    match_cross_venue_contracts,
    scan_prediction_routes,
)
from smart_money_radar.prediction.service import (
    PredictionScanConfig,
    collect_prediction_catalogs,
    run_prediction_scan,
)
from smart_money_radar.prediction.strategies import prediction_strategy_bucket
from smart_money_radar.dashboard import recover_interrupted_dashboard_work
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

    def test_complete_set_no_basket_is_scanned(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [
            market_row("a", "Alpha", 0),
            market_row("b", "Beta", 0),
            market_row("c", "Gamma", 0),
        ]
        books = [
            book_row("a", yes_ask=0.35, no_ask=0.62),
            book_row("b", yes_ask=0.35, no_ask=0.62),
            book_row("c", yes_ask=0.35, no_ask=0.62),
        ]

        _, _, routes = scan_prediction_routes(
            events,
            markets,
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
        )
        route = next(row for row in routes if row["route_type"] == "complete_set_no")

        self.assertEqual(route["status"], "paper_candidate")
        self.assertEqual(route["guaranteed_payout_per_share"], 2)
        self.assertAlmostEqual(route["expected_gross_profit"], 14)
        self.assertGreater(route["expected_net_profit"], 13)
        self.assertEqual(
            prediction_strategy_bucket(route["route_type"]),
            "complete_set_no_discount",
        )

    def test_complete_set_no_requires_all_no_books(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [market_row("a", "Alpha", 0), market_row("b", "Beta", 0)]
        books = [
            book_row("a", yes_ask=0.45, no_ask=0.40),
            {**book_row("b", yes_ask=0.45, no_ask=0.40), "no_asks": []},
        ]

        _, _, routes = scan_prediction_routes(events, markets, books, NOW)

        self.assertFalse(any(row["route_type"] == "complete_set_no" for row in routes))

    def test_negative_risk_conversion_can_be_paper_candidate(self) -> None:
        event = event_row(exhaustive=True)
        event["neg_risk"] = True
        markets = [
            market_row("a", "Alpha", 0),
            market_row("b", "Beta", 0),
            market_row("c", "Gamma", 0),
        ]
        books = [
            book_row("a", yes_ask=0.80, no_ask=0.20),
            book_row("b", yes_ask=0.46, no_ask=0.55),
            book_row("c", yes_ask=0.46, no_ask=0.55),
        ]

        _, _, routes = scan_prediction_routes(
            [event],
            markets,
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
        )
        route = next(
            row for row in routes
            if row["route_type"] == "negative_risk_conversion"
            and row["status"] == "paper_candidate"
        )

        self.assertGreater(route["expected_net_profit"], 60)
        self.assertEqual(
            route["evidence"]["negative_risk_conversion"]["source_market_id"],
            "a",
        )
        self.assertEqual(
            route["evidence"]["negative_risk_conversion"]["converted_yes_count"],
            2,
        )

    def test_negative_risk_requires_exhaustive_mutually_exclusive_non_augmented_event(self) -> None:
        markets = [market_row("a", "Alpha", 0), market_row("b", "Beta", 0)]
        books = [
            book_row("a", yes_ask=0.80, no_ask=0.20),
            book_row("b", yes_ask=0.46, no_ask=0.55),
        ]
        not_mutually_exclusive = event_row(exhaustive=True)
        not_mutually_exclusive.update(neg_risk=True, mutually_exclusive=False)
        augmented = event_row(exhaustive=True)
        augmented.update(neg_risk=True, augmented_neg_risk=True)

        _, _, non_exclusive_routes = scan_prediction_routes(
            [not_mutually_exclusive],
            markets,
            books,
            NOW,
        )
        _, _, augmented_routes = scan_prediction_routes([augmented], markets, books, NOW)

        self.assertFalse(
            any(
                row["route_type"] == "negative_risk_conversion"
                for row in non_exclusive_routes
            )
        )
        self.assertFalse(
            any(
                row["route_type"] == "negative_risk_conversion"
                for row in augmented_routes
            )
        )

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
        ladder = next(
            row for row in routes if row["route_type"] == "threshold_ladder"
        )

        self.assertEqual(len(constraints), 1)
        self.assertEqual(ladder["status"], "paper_candidate")
        self.assertGreater(ladder["expected_net_profit"], 4)
        self.assertEqual(
            ladder["evidence"]["advisory_risk_flags"],
            ["auto_parsed_contract_semantics"],
        )
        self.assertEqual(
            prediction_strategy_bucket(ladder["route_type"]),
            "threshold_ladder",
        )
        self.assertFalse(
            any(row["route_type"] == "logical_implication" for row in routes)
        )

    def test_threshold_ladder_does_not_mix_hit_by_and_resolution_thresholds(self) -> None:
        markets = [
            market_row(
                "hit-high",
                "Will Bitcoin hit $150,000 by Dec 31?",
                0,
                event_id="btc-hit-high",
            ),
            market_row(
                "close-low",
                "Will Bitcoin be above $100,000 on Dec 31?",
                0,
                event_id="btc-close-low",
            ),
        ]
        books = [
            book_row("hit-high", yes_ask=0.61, no_ask=0.40),
            book_row("close-low", yes_ask=0.55, no_ask=0.46),
        ]

        constraints, _, routes = scan_prediction_routes([], markets, books, NOW)

        self.assertEqual(constraints, [])
        self.assertFalse(any(row["route_type"] == "threshold_ladder" for row in routes))

    def test_threshold_ladder_does_not_mix_reach_event_and_dip_outcome(self) -> None:
        markets = [
            market_row(
                "reach-high",
                "Will Bitcoin reach $67,500 in July?",
                0,
                event_id="btc-reach",
            ),
            market_row(
                "dip-low",
                "Will Bitcoin dip to $37,500 in July?",
                0,
                event_id="btc-dip",
            ),
        ]
        markets[1]["event_title"] = "Will Bitcoin reach $67,500 in July?"
        books = [
            book_row("reach-high", yes_ask=0.61, no_ask=0.40),
            book_row("dip-low", yes_ask=0.35, no_ask=0.66),
        ]

        constraints, _, routes = scan_prediction_routes([], markets, books, NOW)

        self.assertEqual(constraints, [])
        self.assertFalse(any(row["route_type"] == "threshold_ladder" for row in routes))

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
        self.assertIn(
            "cancellation_rules_differ",
            cross[0]["evidence"]["blocking_risk_flags"],
        )

    def test_prediction_candidates_include_positive_contract_review(self) -> None:
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

        _, _, routes = scan_prediction_routes([], [poly, kalshi], books, NOW)
        candidates = build_prediction_route_candidates(routes)

        review = next(
            row for row in candidates
            if row["route_type"] == "cross_venue_complement"
            and row["candidate_status"] == "contract_review"
        )
        self.assertGreater(review["expected_net_profit"], 0)
        self.assertIn("cancellation", review["blocking_reason"].lower())
        self.assertGreater(review["candidate_score"], 0)

    def test_cross_venue_exact_verified_contract_can_be_paper_candidate(self) -> None:
        rules = "Resolves according to FIFA's official result. If canceled, market is void."
        poly = market_row("poly-fr", "Will France win the 2026 World Cup?", 0)
        poly.update(
            venue="polymarket",
            event_id="world-cup-poly",
            event_title="Will France win the 2026 World Cup?",
            outcome_label="France",
            resolution_rules=rules,
            resolution_source="FIFA",
            cancellation_rules="If canceled, market is void.",
            fee_source="polymarket_clob_market_info",
        )
        kalshi = market_row("kalshi-fr", "Will France win the 2026 World Cup?", 0)
        kalshi.update(
            venue="kalshi",
            event_id="world-cup-kalshi",
            event_title="Will France win the 2026 World Cup?",
            outcome_label="France",
            resolution_rules=rules,
            resolution_source="FIFA",
            cancellation_rules="If canceled, market is void.",
            fee_source="kalshi_public_conservative_formula",
        )
        books = [
            {**book_row("poly-fr", yes_ask=0.40, no_ask=0.61), "venue": "polymarket"},
            {**book_row("kalshi-fr", yes_ask=0.60, no_ask=0.50), "venue": "kalshi"},
        ]

        matches = match_cross_venue_contracts([poly, kalshi], NOW)
        _, _, routes = scan_prediction_routes(
            [],
            [poly, kalshi],
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
        )
        paper = [
            row for row in routes
            if row["route_type"] == "cross_venue_complement"
            and row["status"] == "paper_candidate"
        ]

        self.assertEqual(matches[0]["status"], "matched")
        self.assertEqual(matches[0]["evidence"]["blocking_risk_flags"], [])
        self.assertTrue(paper)
        self.assertIn("public_fee_assumptions", paper[0]["risk_flags"])
        self.assertEqual(paper[0]["evidence"]["blocking_risk_flags"], [])
        self.assertIn(
            "public_fee_assumptions",
            paper[0]["evidence"]["advisory_risk_flags"],
        )

    def test_cross_venue_trusted_mapping_bypasses_semantic_review(self) -> None:
        poly = market_row("poly-yamal", "Will Yamal win the Golden Boy award?", 0)
        poly.update(
            venue="polymarket",
            event_id="poly-yamal-event",
            event_title="Golden Boy winner",
            outcome_label="Yamal",
            resolution_rules="Polymarket rules pending manual mapping.",
            resolution_source="manual",
            cancellation_rules=None,
        )
        kalshi = market_row("kalshi-yamal", "Unrelated title from venue API", 0)
        kalshi.update(
            venue="kalshi",
            event_id="kalshi-yamal-event",
            event_title="Different display title",
            outcome_label="Different label",
            resolution_rules="Kalshi rules pending manual mapping.",
            resolution_source="manual",
            cancellation_rules=None,
        )
        trusted_mappings = [
            {
                "venue_a": "polymarket",
                "market_id_a": "poly-yamal",
                "venue_b": "kalshi",
                "market_id_b": "kalshi-yamal",
                "relation_type": "equivalent",
                "status": "active",
                "confidence_score": 1.0,
                "verified_by": "test",
                "verified_at": NOW,
                "rationale": ["Manual review confirmed equivalent outcome."],
            }
        ]
        books = [
            {**book_row("poly-yamal", yes_ask=0.40, no_ask=0.61), "venue": "polymarket"},
            {**book_row("kalshi-yamal", yes_ask=0.60, no_ask=0.50), "venue": "kalshi"},
        ]

        matches = match_cross_venue_contracts(
            [poly, kalshi],
            NOW,
            trusted_contract_mappings=trusted_mappings,
        )
        _, _, routes = scan_prediction_routes(
            [],
            [poly, kalshi],
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
            trusted_contract_mappings=trusted_mappings,
        )
        paper = [
            row for row in routes
            if row["route_type"] == "cross_venue_complement"
            and row["status"] == "paper_candidate"
        ]

        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["evidence"]["trusted_mapping"])
        self.assertEqual(matches[0]["evidence"]["blocking_risk_flags"], [])
        self.assertTrue(paper)
        self.assertTrue(paper[0]["evidence"]["cross_venue_match"]["trusted_mapping"])
        self.assertTrue(
            paper[0]["evidence"]["cross_venue_match"]["contract_terms_verified"]
        )

    def test_cross_venue_trusted_mapping_supports_hyperliquid_hip4(self) -> None:
        rules = "Resolves Yes if BTC is above 65659 at the target expiry."
        poly = market_row("poly-btc", "Will BTC be above $65,659?", 0)
        poly.update(
            venue="polymarket",
            event_id="poly-btc-event",
            event_title="BTC daily close",
            outcome_label="BTC above $65,659",
            resolution_rules=rules,
            resolution_source="manual",
            event_source_url="https://polymarket.com/event/btc-above-65659",
        )
        hyperliquid = market_row("905", "Will BTC be above $65,659?", 0.0006)
        hyperliquid.update(
            venue="hyperliquid_hip4",
            event_id="hyperliquid-905",
            event_title="BTC daily close",
            outcome_label="BTC above $65,659",
            resolution_rules=rules,
            resolution_source="Hyperliquid HIP-4 outcome market",
            event_source_url="https://app.hyperliquid.xyz/trade/%239050",
            fee_source="hyperliquid_public_conservative_fee_assumption",
        )
        trusted_mappings = [
            {
                "venue_a": "polymarket",
                "market_id_a": "poly-btc",
                "venue_b": "hyperliquid_hip4",
                "market_id_b": "905",
                "relation_type": "equivalent",
                "status": "active",
                "confidence_score": 1.0,
                "verified_by": "test",
                "verified_at": NOW,
                "rationale": ["Manual review confirmed equivalent BTC threshold."],
            }
        ]
        books = [
            {**book_row("poly-btc", yes_ask=0.40, no_ask=0.61), "venue": "polymarket"},
            {
                **book_row("905", yes_ask=0.60, no_ask=0.50),
                "venue": "hyperliquid_hip4",
            },
        ]

        matches = match_cross_venue_contracts(
            [poly, hyperliquid],
            NOW,
            trusted_contract_mappings=trusted_mappings,
        )
        _, _, routes = scan_prediction_routes(
            [],
            [poly, hyperliquid],
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
            trusted_contract_mappings=trusted_mappings,
        )
        paper = [
            row for row in routes
            if row["route_type"] == "cross_venue_complement"
            and row["status"] == "paper_candidate"
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["venue_b"], "hyperliquid_hip4")
        self.assertTrue(paper)
        self.assertEqual(paper[0]["venue_scope"], "hyperliquid_hip4+polymarket")
        self.assertGreater(paper[0]["expected_net_profit"], 0)
        self.assertTrue(paper[0]["evidence"]["cross_venue_match"]["trusted_mapping"])

    def test_cross_venue_trusted_mappings_are_deduped_by_market_refs(self) -> None:
        poly = market_row("poly-a", "Will Alpha happen?", 0)
        poly.update(venue="polymarket")
        kalshi_a = market_row("kalshi-a", "Alpha happens", 0)
        kalshi_a.update(venue="kalshi")
        kalshi_b = market_row("kalshi-b", "Another Alpha market", 0)
        kalshi_b.update(venue="kalshi")

        matches = match_cross_venue_contracts(
            [poly, kalshi_a, kalshi_b],
            NOW,
            trusted_contract_mappings=[
                {
                    "venue_a": "polymarket",
                    "market_id_a": "poly-a",
                    "venue_b": "kalshi",
                    "market_id_b": "kalshi-a",
                    "status": "active",
                    "confidence_score": 1.0,
                },
                {
                    "venue_a": "polymarket",
                    "market_id_a": "poly-a",
                    "venue_b": "kalshi",
                    "market_id_b": "kalshi-b",
                    "status": "active",
                    "confidence_score": 0.99,
                },
            ],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["market_id_b"], "kalshi-a")

    def test_cross_venue_date_specific_contract_blocks_tournament_match(self) -> None:
        poly = market_row(
            "poly-ar",
            "Will Argentina win the 2026 Men's World Cup on 2026-07-19?",
            0,
        )
        poly.update(
            venue="polymarket",
            event_title="Argentina 2026 Men's World Cup on 2026-07-19",
            outcome_label="Argentina",
            resolution_rules="Resolves from the official FIFA result on 2026-07-19.",
            resolution_source="FIFA",
            cancellation_rules="If canceled, market is void.",
        )
        kalshi = market_row(
            "kalshi-ar",
            "Will Argentina win the 2026 Men's World Cup?",
            0,
        )
        kalshi.update(
            venue="kalshi",
            event_title="Argentina 2026 Men's World Cup",
            outcome_label="Argentina",
            resolution_rules="Resolves from the official FIFA World Cup winner.",
            resolution_source="FIFA",
            cancellation_rules="If canceled, market is void.",
        )

        matches = match_cross_venue_contracts([poly, kalshi], NOW)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "review")
        self.assertIn(
            "event_date_semantics_differ",
            matches[0]["evidence"]["blocking_risk_flags"],
        )
        self.assertIn(
            "contract_kind_differs",
            matches[0]["evidence"]["blocking_risk_flags"],
        )

    def test_public_kalshi_fee_assumption_is_advisory_not_blocking(self) -> None:
        event = event_row(exhaustive=True)
        event["venue"] = "kalshi"
        markets = [
            market_row("a", "Alpha", 0, event_id="event"),
            market_row("b", "Beta", 0, event_id="event"),
        ]
        for market in markets:
            market["venue"] = "kalshi"
            market["fee_verified"] = True
            market["fee_source"] = "kalshi_public_conservative_formula"
        books = [
            {**book_row("a", yes_ask=0.45), "venue": "kalshi"},
            {**book_row("b", yes_ask=0.45), "venue": "kalshi"},
        ]

        _, _, routes = scan_prediction_routes(
            [event],
            markets,
            books,
            NOW,
            ScannerConfig(minimum_expected_profit=0.1),
        )
        route = next(row for row in routes if row["route_type"] == "complete_set")

        self.assertEqual(route["status"], "paper_candidate")
        self.assertIn("public_fee_assumptions", route["risk_flags"])
        self.assertEqual(route["evidence"]["blocking_risk_flags"], [])
        self.assertIn(
            "public_fee_assumptions",
            route["evidence"]["advisory_risk_flags"],
        )

    def test_prediction_candidates_include_near_miss_complete_set(self) -> None:
        events = [event_row(exhaustive=True)]
        markets = [market_row("a", "Alpha", 0), market_row("b", "Beta", 0)]
        books = [book_row("a", yes_ask=0.499), book_row("b", yes_ask=0.499)]

        _, _, routes = scan_prediction_routes(events, markets, books, NOW)
        route = next(row for row in routes if row["route_type"] == "complete_set")
        candidates = build_prediction_route_candidates(routes)

        near_miss = next(
            row for row in candidates
            if row["route_key"] == route["route_key"]
        )
        self.assertEqual(route["status"], "not_profitable")
        self.assertEqual(near_miss["candidate_status"], "near_miss")
        self.assertEqual(near_miss["strategy_bucket"], "complete_set_discount")
        self.assertEqual(near_miss["screen_reason"], "below_profit_or_edge_gate")
        self.assertGreaterEqual(
            near_miss["evidence"]["needed_net_edge_improvement"],
            0,
        )

    def test_normalized_kalshi_fee_policy_is_public_and_verified_for_paper(self) -> None:
        events, markets = normalize_kalshi_catalog(
            [
                {
                    "event_ticker": "TEST-EVENT",
                    "title": "Test Event",
                    "collateral_return_type": "MECNET",
                    "mutually_exclusive": True,
                    "markets": [
                        {
                            "ticker": "TEST-MARKET",
                            "title": "Test market?",
                            "status": "active",
                            "price_ranges": [{"step": 0.01}],
                        }
                    ],
                }
            ],
            NOW,
            10,
        )

        self.assertEqual(events[0]["venue"], "kalshi")
        self.assertTrue(markets[0]["fee_verified"])
        self.assertEqual(
            markets[0]["fee_source"],
            "kalshi_public_conservative_formula",
        )

    def test_hyperliquid_hip4_catalog_normalizes_questions_and_books(self) -> None:
        events, markets = normalize_hyperliquid_catalog(
            hyperliquid_meta_fixture(),
            {
                "#5100": "0.75",
                "#5101": "0.25",
                "#5110": "0.01",
                "#5111": "0.99",
            },
            NOW,
            10,
        )
        market = next(row for row in markets if row["market_id"] == "510")
        book = normalize_hyperliquid_orderbook(
            market,
            {
                "#5100": hyperliquid_book("#5100", bid=0.74, ask=0.76),
                "#5101": hyperliquid_book("#5101", bid=0.24, ask=0.26),
            },
            NOW,
        )

        self.assertEqual(events[0]["venue"], "hyperliquid_hip4")
        self.assertTrue(events[0]["mutually_exclusive"])
        self.assertTrue(events[0]["exhaustive"])
        self.assertEqual(market["yes_token_id"], "#5100")
        self.assertEqual(market["no_token_id"], "#5101")
        self.assertEqual(
            market["fee_source"],
            "hyperliquid_public_conservative_fee_assumption",
        )
        self.assertTrue(market["accepting_orders"])
        self.assertEqual(book["best_yes_bid"], 0.74)
        self.assertEqual(book["best_yes_ask"], 0.76)
        self.assertEqual(book["best_no_bid"], 0.24)
        self.assertEqual(book["best_no_ask"], 0.26)

    def test_hyperliquid_hip4_binary_complement_can_be_paper_candidate(self) -> None:
        events, markets = normalize_hyperliquid_catalog(
            hyperliquid_binary_meta_fixture(),
            {"#9050": "0.5", "#9051": "0.5"},
            NOW,
            10,
        )
        books = [
            normalize_hyperliquid_orderbook(
                markets[0],
                {
                    "#9050": hyperliquid_book("#9050", bid=0.44, ask=0.45),
                    "#9051": hyperliquid_book("#9051", bid=0.44, ask=0.45),
                },
                NOW,
            )
        ]

        _, _, routes = scan_prediction_routes(events, markets, books, NOW)
        route = next(row for row in routes if row["route_type"] == "binary_complement")

        self.assertEqual(route["venue_scope"], "hyperliquid_hip4")
        self.assertEqual(route["status"], "paper_candidate")
        self.assertGreater(route["expected_net_profit"], 0)

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
        self.assertEqual(dashboard["wallet_summary"]["cache_status"], "not_collected")
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 0)
        self.assertEqual(dashboard["retention_policy"]["prediction_scan_snapshots"], 1)

    def test_sqlite_maintenance_runs_on_initialized_store(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()

            result = store.sqlite_maintenance(vacuum=True)

        self.assertIn("analyze", result["operations"])
        self.assertIn("optimize", result["operations"])
        self.assertIn("vacuum", result["operations"])
        self.assertGreaterEqual(result["after_bytes"], 0)

    def test_prediction_verified_contract_mappings_are_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            inserted = store.upsert_prediction_verified_contract_mappings(
                [
                    {
                        "venue_a": "polymarket",
                        "market_id_a": "poly",
                        "venue_b": "kalshi",
                        "market_id_b": "kalshi",
                        "relation_type": "equivalent",
                        "status": "active",
                        "confidence_score": 0.99,
                        "verified_by": "analyst",
                        "verified_at": NOW,
                        "rationale": ["Rules checked."],
                    }
                ]
            )
            loaded = store.prediction_verified_contract_mappings()

        self.assertEqual(inserted, 1)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["venue_a"], "polymarket")
        self.assertEqual(loaded[0]["rationale"], ["Rules checked."])

    def test_prediction_verified_mapping_file_loads_json_and_clamps_confidence(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.json"
            path.write_text(
                """
                {
                  "mappings": [
                    {
                      "venue_a": "polymarket",
                      "market_id_a": "poly",
                      "venue_b": "kalshi",
                      "market_id_b": "kalshi",
                      "confidence_score": 1.5,
                      "rationale": "Rules checked|Deadline checked"
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )

            rows = load_prediction_verified_mapping_file(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relation_type"], "equivalent")
        self.assertEqual(rows[0]["confidence_score"], 1.0)
        self.assertEqual(rows[0]["rationale"], ["Rules checked", "Deadline checked"])

    def test_prediction_dashboard_returns_route_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            previous_scan_id = store.start_prediction_scan({"test": True})
            store.insert_prediction_route_candidates(
                previous_scan_id,
                [
                    {
                        "route_key": "complete_set:test",
                        "route_type": "complete_set",
                        "venue_scope": "polymarket",
                        "event_id": "event",
                        "title": "Winner",
                        "candidate_status": "near_miss",
                        "route_status": "not_profitable",
                        "candidate_score": 40.0,
                        "expected_net_profit": -0.1,
                        "net_edge_per_share": -0.001,
                        "optimal_size": 100,
                        "max_executable_size": 100,
                        "blocking_reason": None,
                        "screen_reason": "below_profit_or_edge_gate",
                        "observed_at": NOW,
                        "risk_flags": ["multi_leg_execution_unvalidated"],
                        "evidence": {"blocking_reasons": []},
                    }
                ],
            )
            store.finish_prediction_scan(previous_scan_id, "success")
            scan_id = store.start_prediction_scan({"test": True})
            inserted = store.insert_prediction_route_candidates(
                scan_id,
                [
                    {
                        "route_key": "complete_set:test",
                        "route_type": "complete_set",
                        "venue_scope": "polymarket",
                        "event_id": "event",
                        "title": "Winner",
                        "candidate_status": "near_miss",
                        "route_status": "not_profitable",
                        "candidate_score": 42.5,
                        "expected_net_profit": 0.1,
                        "net_edge_per_share": 0.001,
                        "optimal_size": 100,
                        "max_executable_size": 100,
                        "blocking_reason": None,
                        "screen_reason": "below_profit_or_edge_gate",
                        "observed_at": NOW,
                        "risk_flags": ["multi_leg_execution_unvalidated"],
                        "evidence": {"blocking_reasons": []},
                    },
                    {
                        "route_key": "cross_venue:test",
                        "route_type": "cross_venue_complement",
                        "venue_scope": "polymarket+kalshi",
                        "event_id": "event-a|event-b",
                        "title": "Cross venue",
                        "candidate_status": "contract_review",
                        "route_status": "contract_review",
                        "candidate_score": 70.0,
                        "expected_net_profit": 10.0,
                        "net_edge_per_share": 0.01,
                        "optimal_size": 100,
                        "max_executable_size": 100,
                        "blocking_reason": "Manual review",
                        "screen_reason": "manual_contract_review_required",
                        "observed_at": NOW,
                        "risk_flags": ["manual_contract_review_required"],
                        "evidence": {"blocking_reasons": ["Manual review"]},
                    }
                ],
            )
            store.finish_prediction_scan(scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual(inserted, 2)
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 2)
        self.assertEqual(
            dashboard["candidate_summary"]["status_counts"][0]["candidate_status"],
            "contract_review",
        )
        self.assertIn(
            "cross_venue_equivalence",
            {
                row["strategy_bucket"]
                for row in dashboard["candidate_summary"]["strategy_counts"]
            },
        )
        self.assertEqual(
            dashboard["route_candidates"][0]["strategy_bucket"],
            "cross_venue_equivalence",
        )
        self.assertNotIn("candidate_score_delta", dashboard["route_candidates"][1])
        self.assertNotIn("lifecycle_summary", dashboard)
        self.assertEqual(
            dashboard["opportunity_dashboard"]["alerts"][0]["alert_type"],
            "contract_review_positive",
        )
        self.assertEqual(
            dashboard["route_candidates"][1]["candidate_status"],
            "near_miss",
        )
        self.assertEqual(
            dashboard["route_candidates"][1]["risk_flags"],
            ["multi_leg_execution_unvalidated"],
        )

    def test_prediction_dashboard_hides_candidates_from_stale_scan(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"dashboard_freshness_minutes": 15})
            store.insert_prediction_route_candidates(
                scan_id,
                [
                    {
                        "route_key": "complete_set:stale",
                        "route_type": "complete_set",
                        "venue_scope": "polymarket",
                        "event_id": "event",
                        "title": "Winner",
                        "candidate_status": "near_miss",
                        "route_status": "not_profitable",
                        "candidate_score": 42.5,
                        "expected_net_profit": 0.1,
                        "net_edge_per_share": 0.001,
                        "optimal_size": 100,
                        "max_executable_size": 100,
                        "blocking_reason": None,
                        "screen_reason": "below_profit_or_edge_gate",
                        "observed_at": NOW,
                        "risk_flags": [],
                        "evidence": {},
                    }
                ],
            )
            store.finish_prediction_scan(scan_id, "success")
            stale_at = (
                datetime.now(UTC) - timedelta(minutes=60)
            ).replace(microsecond=0).isoformat()
            with store.connect() as connection:
                connection.execute(
                    "UPDATE prediction_scans SET finished_at = ? WHERE prediction_scan_id = ?",
                    (stale_at, scan_id),
                )

            dashboard = store.prediction_dashboard()

        self.assertEqual(dashboard["latest_scan"]["freshness_status"], "stale")
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 0)
        self.assertEqual(dashboard["route_candidates"], [])

    def test_prediction_dashboard_hides_negative_pnl_routes_and_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"test": True})
            positive_route = paper_route_row()
            negative_route = {
                **paper_route_row(),
                "route_key": "complete_set:negative",
                "expected_net_profit": -1.0,
                "net_edge_per_share": -0.01,
            }
            store.insert_prediction_routes(scan_id, [positive_route, negative_route])
            store.insert_prediction_route_candidates(
                scan_id,
                [
                    prediction_candidate_row(
                        "complete_set:paper",
                        expected_net_profit=1.0,
                    ),
                    prediction_candidate_row(
                        "complete_set:negative",
                        expected_net_profit=-1.0,
                    ),
                ],
            )
            store.finish_prediction_scan(scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual([row["route_key"] for row in dashboard["routes"]], ["complete_set:paper"])
        self.assertEqual(
            [row["route_key"] for row in dashboard["route_candidates"]],
            ["complete_set:paper"],
        )
        self.assertEqual(
            dashboard["route_candidates"][0]["source_url"],
            "https://example.com",
        )
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 1)
        self.assertGreater(dashboard["route_candidates"][0]["expected_net_profit"], 0)

    def test_prediction_dashboard_hides_expired_routes_and_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"test": True})
            active_route = paper_route_row()
            expired_route = {
                **paper_route_row(),
                "route_key": "complete_set:expired",
                "expected_net_profit": 5.0,
                "capital_lock_days": 0.0,
            }
            store.insert_prediction_routes(scan_id, [active_route, expired_route])
            store.insert_prediction_route_candidates(
                scan_id,
                [
                    prediction_candidate_row(
                        "complete_set:paper",
                        expected_net_profit=1.0,
                    ),
                    prediction_candidate_row(
                        "complete_set:expired",
                        expected_net_profit=5.0,
                    ),
                ],
            )
            store.finish_prediction_scan(scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual([row["route_key"] for row in dashboard["routes"]], ["complete_set:paper"])
        self.assertEqual(
            [row["route_key"] for row in dashboard["route_candidates"]],
            ["complete_set:paper"],
        )
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 1)

    def test_prediction_dashboard_keeps_completed_snapshot_visible_during_refresh(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            completed_scan_id = store.start_prediction_scan({"test": True})
            store.insert_prediction_route_candidates(
                completed_scan_id,
                [
                    {
                        "route_key": "complete_set:visible",
                        "route_type": "complete_set",
                        "venue_scope": "polymarket",
                        "event_id": "event",
                        "title": "Winner",
                        "candidate_status": "near_miss",
                        "route_status": "not_profitable",
                        "candidate_score": 42.5,
                        "expected_net_profit": 0.1,
                        "net_edge_per_share": 0.001,
                        "optimal_size": 100,
                        "max_executable_size": 100,
                        "blocking_reason": None,
                        "screen_reason": "below_profit_or_edge_gate",
                        "observed_at": NOW,
                        "risk_flags": [],
                        "evidence": {},
                    }
                ],
            )
            store.finish_prediction_scan(completed_scan_id, "success")
            running_scan_id = store.start_prediction_scan({"test": True})

            dashboard = store.prediction_dashboard()

        self.assertEqual(
            dashboard["latest_scan"]["prediction_scan_id"],
            completed_scan_id,
        )
        self.assertEqual(
            dashboard["active_scan"]["prediction_scan_id"],
            running_scan_id,
        )
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 1)
        self.assertEqual(len(dashboard["route_candidates"]), 1)

    def test_prediction_dashboard_prefers_success_snapshot_over_failed_refresh(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            completed_scan_id = store.start_prediction_scan({"test": True})
            store.insert_prediction_route_candidates(
                completed_scan_id,
                [
                    prediction_candidate_row(
                        "complete_set:visible",
                        expected_net_profit=1.0,
                    )
                ],
            )
            store.finish_prediction_scan(completed_scan_id, "success")
            failed_scan_id = store.start_prediction_scan({"test": True})
            store.finish_prediction_scan(
                failed_scan_id,
                "failed",
                error="refresh interrupted",
            )

            dashboard = store.prediction_dashboard()

        self.assertEqual(
            dashboard["latest_scan"]["prediction_scan_id"],
            completed_scan_id,
        )
        self.assertEqual(
            dashboard["latest_failed_scan"]["prediction_scan_id"],
            failed_scan_id,
        )
        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 1)
        self.assertEqual(len(dashboard["route_candidates"]), 1)

    def test_prediction_paper_trader_dashboard_groups_by_strategy(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            quote_scan_id = store.start_prediction_scan({"test": True})
            routes = store.insert_prediction_routes(
                quote_scan_id,
                [paper_route_row()],
            )
            store.finish_prediction_scan(quote_scan_id, "success")
            execution_scan_id = store.start_prediction_scan({"test": True})
            inserted = store.insert_prediction_paper_executions(
                execution_scan_id,
                [
                    {
                        "prediction_route_id": routes[0]["prediction_route_id"],
                        "model_version": "prediction_paper_v2_future_snapshot",
                        "status": "filled_profitable",
                        "requested_size": 100,
                        "filled_size": 100,
                        "fill_ratio": 1,
                        "latency_ms": 750,
                        "depth_haircut": 0.8,
                        "queue_ahead_size": 0,
                        "expected_net_profit": 5,
                        "simulated_net_profit": 4,
                        "simulated_fees": 0,
                        "simulated_slippage": 0,
                        "capital_required": 90,
                        "legs": [],
                        "result": {"deterministic": True},
                    }
                ],
            )
            store.finish_prediction_scan(execution_scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual(inserted, 1)
        self.assertEqual(dashboard["paper_trader"]["strategy_rows"][0]["strategy_bucket"], "complete_set_discount")
        self.assertEqual(dashboard["paper_trader"]["strategy_rows"][0]["profitable_count"], 1)

    def test_prediction_dashboard_returns_near_zero_watch_not_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"test": True})
            route = paper_route_row()
            route.update(
                route_key="complete_set:near-zero",
                status="not_profitable",
                expected_net_profit=-0.02,
                net_edge_per_share=-0.002,
            )
            store.insert_prediction_routes(scan_id, [route])
            store.finish_prediction_scan(scan_id, "success")

            dashboard = store.prediction_dashboard()

        self.assertEqual(dashboard["candidate_summary"]["candidate_count"], 0)
        self.assertEqual(dashboard["route_candidates"], [])
        self.assertEqual(len(dashboard["near_zero_routes"]), 1)
        self.assertEqual(
            dashboard["near_zero_routes"][0]["missing_net_edge_per_share"],
            0.002,
        )

    def test_prediction_candidate_backlog_tracks_seen_and_inactive_rows(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            route = paper_route_row()
            candidate = prediction_candidate_row(
                route["route_key"],
                expected_net_profit=5.0,
            )
            skipped = prediction_candidate_row(
                "complete_set:negative",
                expected_net_profit=-0.01,
            )

            first = store.upsert_prediction_candidate_backlog(
                1,
                [candidate, skipped],
                [route],
            )
            candidate["expected_net_profit"] = 9.0
            second = store.upsert_prediction_candidate_backlog(
                2,
                [candidate, skipped],
                [route],
            )
            inactive = store.upsert_prediction_candidate_backlog(3, [], [route])
            dashboard = store.prediction_dashboard()

        self.assertEqual(first["active_candidates"], 1)
        self.assertEqual(second["active_candidates"], 1)
        self.assertEqual(inactive["active_candidates"], 0)
        self.assertEqual(dashboard["backlog_summary"]["candidate_backlog_count"], 1)
        backlog = dashboard["candidate_backlog"][0]
        self.assertFalse(backlog["active"])
        self.assertEqual(backlog["seen_count"], 2)
        self.assertEqual(backlog["best_expected_net_profit"], 9.0)

    def test_prediction_trade_backlog_stores_paper_execution_rows(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_prediction_scan({"test": True})
            routes = store.insert_prediction_routes(scan_id, [paper_route_row()])
            paper = {
                "prediction_route_id": routes[0]["prediction_route_id"],
                "model_version": "prediction_paper_v2_future_snapshot",
                "status": "filled_profitable",
                "requested_size": 100,
                "filled_size": 100,
                "fill_ratio": 1,
                "latency_ms": 750,
                "depth_haircut": 0.8,
                "queue_ahead_size": 0,
                "expected_net_profit": 5,
                "simulated_net_profit": 4,
                "simulated_fees": 0,
                "simulated_slippage": 0,
                "capital_required": 90,
                "legs": [],
                "result": {"deterministic": True},
            }
            rejected = {
                **paper,
                "status": "rejected",
                "filled_size": 0,
                "fill_ratio": 0,
                "simulated_net_profit": None,
            }

            result = store.upsert_prediction_trade_backlog(
                scan_id,
                [paper, rejected],
                routes,
            )
            dashboard = store.prediction_dashboard()

        self.assertEqual(result["upserted_trades"], 1)
        self.assertEqual(dashboard["backlog_summary"]["trade_backlog_count"], 1)
        self.assertEqual(dashboard["trade_backlog"][0]["status"], "filled_profitable")

    def test_prediction_retention_keeps_only_latest_scan_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            for index in range(3):
                scan_id = store.start_prediction_scan({"index": index})
                store.insert_prediction_route_candidates(
                    scan_id,
                    [
                        {
                            "route_key": f"route:{index}",
                            "route_type": "complete_set",
                            "venue_scope": "polymarket",
                            "event_id": "event",
                            "title": "Winner",
                            "candidate_status": "near_miss",
                            "route_status": "not_profitable",
                            "candidate_score": 10 + index,
                            "expected_net_profit": 0,
                            "net_edge_per_share": 0,
                            "optimal_size": 100,
                            "max_executable_size": 100,
                            "blocking_reason": None,
                            "screen_reason": "below_profit_or_edge_gate",
                            "observed_at": NOW,
                            "risk_flags": [],
                            "evidence": {},
                        }
                    ],
                )
                store.finish_prediction_scan(scan_id, "success")
            result = store.prune_prediction_history(keep_scans=1)
            with store.connect() as connection:
                scan_count = connection.execute(
                    "SELECT COUNT(*) FROM prediction_scans"
                ).fetchone()[0]
                candidate_count = connection.execute(
                    "SELECT COUNT(*) FROM prediction_route_candidates"
                ).fetchone()[0]

        self.assertEqual(result["deleted_scans"], 2)
        self.assertEqual(result["deleted_route_candidates"], 2)
        self.assertEqual(scan_count, 1)
        self.assertEqual(candidate_count, 1)

    def test_prediction_scan_rechecks_previous_candidates_without_persisting_history(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            quote_scan_id = store.start_prediction_scan({"test": True})
            store.insert_prediction_routes(quote_scan_id, [paper_route_row()])
            store.finish_prediction_scan(quote_scan_id, "success")

            result = run_prediction_scan(
                store,
                config=PredictionScanConfig(
                    collect_wallets=False,
                    retention_scans=1,
                    collect_hyperliquid=False,
                ),
                polymarket=EmptyScanPolymarketClient(),  # type: ignore[arg-type]
                kalshi=EmptyScanKalshiClient(),  # type: ignore[arg-type]
            )
            with store.connect() as connection:
                scan_count = connection.execute(
                    "SELECT COUNT(*) FROM prediction_scans"
                ).fetchone()[0]
                paper_count = connection.execute(
                    "SELECT COUNT(*) FROM prediction_paper_executions"
                ).fetchone()[0]

        self.assertEqual(result["paper_checked_route_count"], 1)
        self.assertEqual(result["paper_simulation_count"], 1)
        self.assertEqual(result["paper_execution_count"], 0)
        self.assertEqual(result["paper_trade_count"], 0)
        self.assertEqual(scan_count, 1)
        self.assertEqual(paper_count, 0)

    def test_prediction_scan_can_run_with_hyperliquid_hip4_only(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()

            result = run_prediction_scan(
                store,
                config=PredictionScanConfig(
                    collect_wallets=False,
                    collect_hyperliquid=True,
                    retention_scans=1,
                ),
                polymarket=EmptyScanPolymarketClient(),  # type: ignore[arg-type]
                kalshi=EmptyScanKalshiClient(),  # type: ignore[arg-type]
                hyperliquid=StaticHyperliquidClient(),  # type: ignore[arg-type]
            )
            dashboard = store.prediction_dashboard(route_limit=5)

        self.assertEqual(result["hyperliquid_event_count"], 1)
        self.assertEqual(result["hyperliquid_market_count"], 1)
        self.assertEqual(result["hyperliquid_orderbook_count"], 1)
        self.assertEqual(result["market_count"], 1)
        self.assertEqual(result["orderbook_count"], 1)
        self.assertGreater(result["route_count"], 0)
        self.assertEqual(dashboard["venues"][0]["venue"], "hyperliquid_hip4")

    def test_prediction_event_token_link_replacement_uses_current_source(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            with store.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO tokens (symbol, name, created_at, updated_at)
                    VALUES ('USDT', 'Tether', ?, ?)
                    """,
                    (NOW, NOW),
                )
                token_id = int(cursor.lastrowid)
            store.upsert_prediction_catalog(
                [
                    {
                        "venue": "polymarket",
                        "event_id": "event-usdt",
                        "title": "Will USDT depeg?",
                        "slug": "event-usdt",
                        "category": "crypto",
                        "description": "Crypto market.",
                        "starts_at": None,
                        "closes_at": NOW,
                        "expected_resolution_at": NOW,
                        "resolution_source": "Source",
                        "resolution_rules": "Crypto event mentioning USDT.",
                        "cancellation_rules": None,
                        "mutually_exclusive": False,
                        "exhaustive": False,
                        "neg_risk": False,
                        "augmented_neg_risk": False,
                        "active": True,
                        "source_url": "https://example.com",
                        "observed_at": NOW,
                        "raw": {},
                    }
                ],
                [],
            )
            inserted = store.replace_prediction_event_token_links(
                [
                    {
                        "venue": "polymarket",
                        "event_id": "event-usdt",
                        "token_id": token_id,
                        "chain_id": None,
                        "token_address": None,
                        "token_symbol": "USDT",
                        "relation_type": "direct_mention",
                        "confidence_score": 0.86,
                        "source": PREDICTION_EVENT_TOKEN_LINK_SOURCE,
                        "rationale": ["test"],
                        "created_at": NOW,
                    }
                ],
                source=PREDICTION_EVENT_TOKEN_LINK_SOURCE,
            )
            cleared = store.replace_prediction_event_token_links(
                [],
                source=PREDICTION_EVENT_TOKEN_LINK_SOURCE,
            )
            with store.connect() as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM prediction_event_token_links"
                ).fetchone()[0]

        self.assertEqual(inserted, 1)
        self.assertEqual(cleared, 0)
        self.assertEqual(remaining, 0)

    def test_prediction_catalog_collection_degrades_when_one_venue_fails(self) -> None:
        warnings: list[str] = []

        poly, kalshi = collect_prediction_catalogs(
            StaticPolymarketClient(),
            FailingKalshiClient(),
            PredictionScanConfig(),
            warnings,
        )

        self.assertEqual(len(poly), 1)
        self.assertEqual(kalshi, [])
        self.assertIn("Kalshi catalog skipped", warnings[0])

    def test_prediction_catalog_collection_fails_when_all_venues_fail(self) -> None:
        warnings: list[str] = []

        with self.assertRaises(PredictionDataError):
            collect_prediction_catalogs(
                FailingPolymarketClient(),
                FailingKalshiClient(),
                PredictionScanConfig(),
                warnings,
            )

        self.assertEqual(len(warnings), 2)

    def test_kalshi_catalog_fetch_keeps_volume_priority_after_parallel_event_fetch(self) -> None:
        client = KalshiClient(StaticKalshiHttp())  # type: ignore[arg-type]

        events = client.top_events(
            limit=2,
            market_pages=1,
            seed_event_ids=(),
        )

        self.assertEqual(
            [event["event_ticker"] for event in events],
            ["KHIGH", "KLOW"],
        )
        self.assertEqual(
            [event["markets"][0]["ticker"] for event in events],
            ["KHIGH-MARKET", "KLOW-MARKET"],
        )

    def test_latest_app_job_prefers_active_job_over_newer_completed_job(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            running_id = store.create_app_job("prediction_scan", "Prediction")
            store.update_app_job(
                running_id,
                status="running",
                progress=0.2,
                message="Prediction running",
            )
            completed_id = store.create_app_job("funding_scan", "Funding")
            store.update_app_job(
                completed_id,
                status="success",
                progress=1.0,
                message="Funding completed",
            )

            latest = store.latest_app_job()
            completed = store.app_job(completed_id)

        self.assertEqual(latest["job_id"], running_id)
        self.assertEqual(completed["status"], "success")

    def test_interrupted_prediction_work_is_marked_failed(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            job_id = store.create_app_job("prediction_scan", "Prediction")
            store.update_app_job(
                job_id,
                status="running",
                progress=0.2,
                message="Prediction running",
            )
            scan_id = store.start_prediction_scan({"test": True})

            failed_jobs = store.fail_running_app_jobs("interrupted")
            failed_scans = store.fail_running_prediction_scans("interrupted")
            job = store.app_job(job_id)
            dashboard = store.prediction_dashboard()

        self.assertEqual(failed_jobs, 1)
        self.assertEqual(failed_scans, 1)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(dashboard["latest_scan"]["prediction_scan_id"], scan_id)
        self.assertEqual(dashboard["latest_scan"]["status"], "failed")

    def test_dashboard_recovery_does_not_fail_running_prediction_bot_scan(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            job_id = store.create_app_job("prediction_scan", "Prediction")
            store.update_app_job(
                job_id,
                status="running",
                progress=0.2,
                message="Prediction running",
            )
            scan_id = store.start_prediction_scan({"source": "prediction_bot"})

            recovery_events = []
            recover_interrupted_dashboard_work(store, event_writer=recovery_events.append)
            job = store.app_job(job_id)
            dashboard = store.prediction_dashboard()

        self.assertEqual(job["status"], "failed")
        self.assertEqual(recovery_events, ["Recovered interrupted dashboard work: 1 app jobs"])
        self.assertEqual(dashboard["active_scan"]["prediction_scan_id"], scan_id)
        self.assertEqual(dashboard["active_scan"]["status"], "running")
        self.assertEqual(dashboard["latest_scan"]["prediction_scan_id"], scan_id)
        self.assertEqual(dashboard["latest_scan"]["status"], "running")


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


def hyperliquid_book(
    coin: str,
    *,
    bid: float,
    ask: float,
    size: float = 100.0,
) -> dict[str, object]:
    return {
        "coin": coin,
        "time": 1_784_787_920_000,
        "levels": [
            [{"px": str(bid), "sz": str(size), "n": 1}],
            [{"px": str(ask), "sz": str(size), "n": 1}],
        ],
    }


def hyperliquid_meta_fixture() -> dict[str, object]:
    return {
        "outcomes": [
            {
                "outcome": 509,
                "name": "Fallback",
                "description": "",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                "quoteToken": "USDC",
            },
            {
                "outcome": 510,
                "name": "No change",
                "description": "No change wins.",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                "quoteToken": "USDC",
            },
            {
                "outcome": 511,
                "name": "Decrease",
                "description": "Decrease wins.",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                "quoteToken": "USDC",
            },
        ],
        "questions": [
            {
                "question": 94,
                "name": "July Fed funds decision",
                "description": "Exactly one outcome resolves Yes. metadata=category:economics",
                "fallbackOutcome": 509,
                "namedOutcomes": [510, 511],
                "settledNamedOutcomes": [],
            }
        ],
    }


def hyperliquid_binary_meta_fixture() -> dict[str, object]:
    return {
        "outcomes": [
            {
                "outcome": 905,
                "name": "Recurring",
                "description": (
                    "class:priceBinary|underlying:BTC|expiry:20260724-0600|"
                    "targetPrice:65659|period:1d"
                ),
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                "quoteToken": "USDC",
            }
        ],
        "questions": [],
    }


def paper_route_row() -> dict[str, object]:
    return {
        "route_key": "complete_set:paper",
        "route_type": "complete_set",
        "title": "Winner",
        "venue_scope": "polymarket",
        "event_id": "event",
        "status": "paper_candidate",
        "confidence_score": 98,
        "semantic_match_score": None,
        "guaranteed_payout_per_share": 1,
        "max_executable_size": 100,
        "optimal_size": 100,
        "gross_edge_per_share": 0.1,
        "net_edge_per_share": 0.09,
        "expected_gross_profit": 10,
        "expected_net_profit": 9,
        "capital_required": 90,
        "total_fees": 0,
        "slippage_cost": 0,
        "operations_buffer": 1,
        "capital_lock_days": 1,
        "annualized_return": 36.5,
        "observed_at": NOW,
        "source_url": "https://example.com",
        "legs": [],
        "rationale": ["test"],
        "risk_flags": [],
        "evidence": {},
    }


def prediction_candidate_row(
    route_key: str,
    *,
    expected_net_profit: float,
) -> dict[str, object]:
    return {
        "route_key": route_key,
        "route_type": "complete_set",
        "venue_scope": "polymarket",
        "event_id": "event",
        "title": "Winner",
        "candidate_status": "near_miss",
        "route_status": "not_profitable",
        "candidate_score": 42.5,
        "expected_net_profit": expected_net_profit,
        "net_edge_per_share": expected_net_profit / 100.0,
        "optimal_size": 100,
        "max_executable_size": 100,
        "blocking_reason": None,
        "screen_reason": "below_profit_or_edge_gate",
        "observed_at": NOW,
        "risk_flags": [],
        "evidence": {},
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


class StaticPolymarketClient:
    def events(self, limit: int = 12) -> list[dict[str, object]]:
        return [
            {
                "id": "event-1",
                "title": "Winner",
                "slug": "winner",
                "active": True,
                "closed": False,
                "markets": [polymarket_raw_market("m1")],
            }
        ]


class EmptyScanPolymarketClient:
    def events(self, limit: int = 12) -> list[dict[str, object]]:
        return [
            {
                "id": "event-empty",
                "title": "Empty event",
                "slug": "empty-event",
                "active": True,
                "closed": False,
                "markets": [],
            }
        ]


class EmptyScanKalshiClient:
    def top_events(
        self,
        limit: int = 12,
        market_pages: int = 2,
    ) -> list[dict[str, object]]:
        return []


class StaticHyperliquidClient:
    def outcome_meta(self) -> dict[str, object]:
        return hyperliquid_binary_meta_fixture()

    def all_mids(self) -> dict[str, str]:
        return {"#9050": "0.5", "#9051": "0.5"}

    def l2_books(self, coins: list[str]) -> dict[str, dict[str, object]]:
        return {
            "#9050": hyperliquid_book("#9050", bid=0.44, ask=0.45),
            "#9051": hyperliquid_book("#9051", bid=0.44, ask=0.45),
        }


class StaticKalshiHttp:
    def get_json(self, url: str) -> dict[str, object]:
        if "/markets?" in url:
            return {
                "markets": [
                    {
                        "ticker": "KLOW-MARKET",
                        "event_ticker": "KLOW",
                        "volume_fp": "100",
                    },
                    {
                        "ticker": "KHIGH-MARKET",
                        "event_ticker": "KHIGH",
                        "volume_fp": "500",
                    },
                ],
                "cursor": "",
            }
        if "/events/KHIGH" in url:
            return {
                "event": {
                    "event_ticker": "KHIGH",
                    "title": "High volume",
                    "markets": [{"ticker": "KHIGH-MARKET"}],
                }
            }
        if "/events/KLOW" in url:
            return {
                "event": {
                    "event_ticker": "KLOW",
                    "title": "Low volume",
                    "markets": [{"ticker": "KLOW-MARKET"}],
                }
            }
        raise AssertionError(f"Unexpected URL: {url}")


class FailingPolymarketClient:
    def events(self, limit: int = 12) -> list[dict[str, object]]:
        raise PredictionDataError("polymarket unavailable")


class FailingKalshiClient:
    def top_events(
        self,
        limit: int = 12,
        market_pages: int = 2,
    ) -> list[dict[str, object]]:
        raise PredictionDataError("kalshi unavailable")


if __name__ == "__main__":
    unittest.main()
