from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.research_validity import (
    OUTCOME_METHODOLOGY_VERSION,
    binary_event_label,
    build_event_coverage,
    build_token_outcomes,
)
from smart_money_radar.storage import SQLiteStore


class ResearchValidityTest(unittest.TestCase):
    def test_point_in_time_label_does_not_use_immature_future(self) -> None:
        snapshot = datetime(2025, 1, 1, tzinfo=UTC)
        as_of = datetime(2025, 2, 1, tzinfo=UTC)

        self.assertIsNone(binary_event_label(snapshot, as_of, None, 60))
        self.assertFalse(
            binary_event_label(
                snapshot,
                datetime(2025, 4, 1, tzinfo=UTC),
                None,
                60,
            )
        )
        self.assertTrue(
            binary_event_label(
                snapshot,
                as_of,
                datetime(2025, 1, 20, tzinfo=UTC),
                60,
            )
        )

    def test_zero_flow_listing_event_is_preserved(self) -> None:
        target = {
            "listing_event_id": 7,
            "chain_id": "base",
            "contract_address": "0xAbC",
            "symbol": "TEST",
            "announced_at": "2025-03-01T00:00:00+00:00",
        }
        coverage = build_event_coverage([target], [], [])

        self.assertEqual(len(coverage), 1)
        self.assertFalse(coverage[0]["pre_announcement_flow_observed"])
        self.assertTrue(coverage[0]["evidence"]["zero_flow_event_preserved"])

    def test_missing_future_snapshots_are_unknown_not_a_collapse(self) -> None:
        outcomes = build_token_outcomes(
            [universe_row("base", "0xAbC")],
            [],
            datetime(2025, 5, 1, tzinfo=UTC),
        )

        self.assertIsNone(outcomes[0]["activity_collapse_30d"])
        self.assertIsNone(outcomes[0]["liquidity_collapse_30d"])
        self.assertIsNone(outcomes[0]["rug_proxy_30d"])
        self.assertFalse(outcomes[0]["evidence"]["future_30d_terminal_observed"])

    def test_solana_case_is_preserved_and_chain_replacement_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            as_of = datetime(2025, 5, 1, tzinfo=UTC)
            base_rows = build_token_outcomes(
                [universe_row("base", "0xAbC")], [], as_of
            )
            solana_rows = build_token_outcomes(
                [universe_row("solana", "AbCdEf123")], [], as_of
            )
            store.replace_research_token_outcomes(
                OUTCOME_METHODOLOGY_VERSION, base_rows, chain_id="base"
            )
            store.replace_research_token_outcomes(
                OUTCOME_METHODOLOGY_VERSION, solana_rows, chain_id="solana"
            )
            store.replace_research_token_outcomes(
                OUTCOME_METHODOLOGY_VERSION, base_rows, chain_id="base"
            )

            with store.connect() as connection:
                rows = connection.execute(
                    "SELECT chain_id, token_address FROM research_token_outcomes "
                    "ORDER BY chain_id"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(dict(rows[1])["token_address"], "AbCdEf123")


def universe_row(chain_id: str, token_address: str) -> dict[str, object]:
    return {
        "chain_id": chain_id,
        "token_address": token_address,
        "snapshot_at": "2025-01-06T00:00:00+00:00",
        "close_price_usd": 1.0,
        "volume_usd": 10_000,
        "trader_count": 10,
    }


if __name__ == "__main__":
    unittest.main()
