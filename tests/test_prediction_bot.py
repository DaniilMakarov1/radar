from __future__ import annotations

import contextlib
from dataclasses import asdict
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.notifications import NotificationResult
from smart_money_radar.prediction.bot import (
    PredictionBotConfig,
    PredictionRadarBot,
    prediction_status_message,
)
from smart_money_radar.storage import SQLiteStore


NOW = "2026-07-21T06:00:00+00:00"


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> NotificationResult:
        self.messages.append(text)
        return NotificationResult("sent", payload={"ok": True})


class PredictionBotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict[str, object]] = []

    def test_status_message_says_no_candidates_when_empty(self) -> None:
        message = prediction_status_message(
            {
                "prediction_scan_id": 7,
                "market_count": 10,
                "orderbook_count": 8,
                "route_count": 12,
                "executable_route_count": 0,
                "warnings": [],
            },
            {
                "latest_scan": {
                    "prediction_scan_id": 7,
                    "freshness_status": "fresh",
                    "market_count": 10,
                    "orderbook_count": 8,
                    "route_count": 12,
                    "executable_route_count": 0,
                },
                "candidate_summary": {"candidate_count": 0},
                "routes": [],
                "route_candidates": [],
            },
            PredictionBotConfig(),
        )

        self.assertIn("Prediction Radar STATUS", message)
        self.assertIn("Candidates:</b> 0", message)
        self.assertIn("Кандидатов нет", message)

    def test_status_message_hides_near_zero_watch_when_no_candidates(self) -> None:
        message = prediction_status_message(
            {
                "prediction_scan_id": 7,
                "market_count": 10,
                "orderbook_count": 8,
                "route_count": 12,
                "executable_route_count": 0,
                "warnings": [],
            },
            {
                "latest_scan": {
                    "prediction_scan_id": 7,
                    "freshness_status": "fresh",
                    "market_count": 10,
                    "orderbook_count": 8,
                    "route_count": 12,
                    "executable_route_count": 0,
                },
                "candidate_summary": {"candidate_count": 0},
                "routes": [],
                "route_candidates": [],
                "near_zero_routes": [
                    {
                        "route_type": "binary_complement",
                        "venue_scope": "polymarket",
                        "title": "Near zero route",
                        "expected_net_profit": -0.02,
                        "net_edge_per_share": -0.002,
                        "missing_net_edge_per_share": 0.002,
                        "optimal_size": 5,
                        "source_url": "https://example.com/prediction",
                    }
                ],
            },
            PredictionBotConfig(),
        )

        self.assertIn("Кандидатов нет", message)
        self.assertNotIn("НЕ кандидаты", message)
        self.assertNotIn("Current PnL: <b>$-0.02</b>", message)

    def test_run_iteration_sends_first_status_with_candidate_link(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            notifier = FakeNotifier()
            bot = PredictionRadarBot(
                store,
                config=PredictionBotConfig(status_report_interval_seconds=3_600),
                notifier=notifier,  # type: ignore[arg-type]
                scan_runner=fake_prediction_scan,
                event_writer=self.events.append,
            )

            result = bot.run_iteration()

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Prediction Radar STATUS", notifier.messages[0])
        self.assertIn('href="https://example.com/prediction"', notifier.messages[0])
        self.assertIn("Net PnL: <b>$9.00</b>", notifier.messages[0])

    def test_status_report_interval_and_scan_retention(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            notifier = FakeNotifier()
            bot = PredictionRadarBot(
                store,
                config=PredictionBotConfig(status_report_interval_seconds=3_600),
                notifier=notifier,  # type: ignore[arg-type]
                scan_runner=fake_prediction_scan,
                event_writer=self.events.append,
            )

            bot.run_iteration()
            bot.run_iteration()
            with store.connect() as connection:
                scan_count = connection.execute(
                    "SELECT COUNT(*) FROM prediction_scans"
                ).fetchone()[0]

        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(scan_count, 1)

    def test_run_loop_does_not_send_lifecycle_telegram_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            notifier = FakeNotifier()
            bot = PredictionRadarBot(
                store,
                config=PredictionBotConfig(
                    iterations=1,
                    status_report_interval_seconds=3_600,
                ),
                notifier=notifier,  # type: ignore[arg-type]
                scan_runner=fake_prediction_scan,
                event_writer=self.events.append,
            )

            bot.run_loop()

        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Prediction Radar STATUS", notifier.messages[0])
        self.assertNotIn("STARTED", notifier.messages[0])

    def test_crash_notifies_even_when_lifecycle_telegram_is_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            notifier = FakeNotifier()
            bot = PredictionRadarBot(
                store,
                config=PredictionBotConfig(iterations=1),
                notifier=notifier,  # type: ignore[arg-type]
                scan_runner=exploding_prediction_scan,
                event_writer=self.events.append,
            )

            with self.assertRaises(RuntimeError):
                bot.run_loop()

        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Prediction Radar Bot CRASHED", notifier.messages[0])

    def test_config_validation_keeps_requested_defaults(self) -> None:
        config = PredictionBotConfig(
            scan_interval_seconds=1,
            status_report_interval_seconds=-1,
            status_report_max_routes=100,
            events_per_venue=0,
        ).validated()

        self.assertEqual(config.scan_interval_seconds, 30)
        self.assertEqual(config.status_report_interval_seconds, 0)
        self.assertEqual(config.status_report_max_routes, 20)
        self.assertEqual(config.events_per_venue, 1)
        self.assertFalse(config.lifecycle_telegram_enabled)

    def test_event_writer_keeps_test_output_quiet(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            notifier = FakeNotifier()
            bot = PredictionRadarBot(
                store,
                config=PredictionBotConfig(status_report_interval_seconds=3_600),
                notifier=notifier,  # type: ignore[arg-type]
                scan_runner=fake_prediction_scan,
                event_writer=self.events.append,
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = bot.run_iteration()

        self.assertEqual(result["status"], "success")
        self.assertEqual(stdout.getvalue(), "")
        event_types = [event["event_type"] for event in self.events]
        self.assertIn("scan", event_types)
        self.assertIn("status_report", event_types)


def fake_prediction_scan(
    store: SQLiteStore,
    *,
    config=None,
) -> dict[str, object]:
    store.init_db()
    scan_id = store.start_prediction_scan(asdict(config) if config else {"test": True})
    route = prediction_route_row()
    store.insert_prediction_routes(scan_id, [route])
    store.insert_prediction_route_candidates(
        scan_id,
        [prediction_candidate_row(route["route_key"])],
    )
    store.finish_prediction_scan(
        scan_id,
        "success",
        polymarket_event_count=1,
        kalshi_event_count=1,
        market_count=2,
        orderbook_count=2,
        route_count=1,
        executable_route_count=0,
    )
    if config is not None:
        store.prune_prediction_history(keep_scans=max(1, int(config.retention_scans)))
    return {
        "prediction_scan_id": scan_id,
        "status": "success",
        "polymarket_event_count": 1,
        "kalshi_event_count": 1,
        "market_count": 2,
        "orderbook_count": 2,
        "route_count": 1,
        "executable_route_count": 0,
        "paper_execution_count": 0,
        "paper_checked_route_count": 0,
        "paper_simulation_count": 0,
        "paper_trade_count": 0,
        "hyperliquid_event_count": 0,
        "hyperliquid_market_count": 0,
        "hyperliquid_orderbook_count": 0,
        "wallet_score_count": 0,
        "token_link_count": 0,
        "warnings": [],
    }


def exploding_prediction_scan(
    store: SQLiteStore,
    *,
    config=None,
) -> dict[str, object]:
    raise RuntimeError("prediction scan failed")


def prediction_route_row() -> dict[str, object]:
    return {
        "route_key": "complete_set:bot",
        "route_type": "complete_set",
        "title": "Prediction bot test market",
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
        "source_url": "https://example.com/prediction",
        "legs": [],
        "rationale": ["test"],
        "risk_flags": [],
        "evidence": {},
    }


def prediction_candidate_row(route_key: str) -> dict[str, object]:
    return {
        "route_key": route_key,
        "route_type": "complete_set",
        "venue_scope": "polymarket",
        "event_id": "event",
        "title": "Prediction bot test market",
        "candidate_status": "near_miss",
        "route_status": "paper_candidate",
        "candidate_score": 42.5,
        "expected_net_profit": 9.0,
        "net_edge_per_share": 0.09,
        "optimal_size": 100,
        "max_executable_size": 100,
        "blocking_reason": None,
        "screen_reason": "below_profit_or_edge_gate",
        "observed_at": NOW,
        "risk_flags": [],
        "evidence": {},
    }


if __name__ == "__main__":
    unittest.main()
