from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.storage import SQLiteStore
from smart_money_radar.wallet_research import score_wallet_opportunities


class ResearchStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "radar.sqlite")
        self.store.init_db()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_signal_stage_change_replaces_previous_classification(self) -> None:
        signal = signal_row("smart_wallet_accumulation")
        self.store.upsert_signals([signal])
        self.store.upsert_signals([{**signal, "signal_type": "accumulation"}])

        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT signal_type FROM signals"
            ).fetchall()
        self.assertEqual([row["signal_type"] for row in rows], ["accumulation"])

    def test_research_tables_accept_a_complete_minimal_round_trip(self) -> None:
        snapshot_at = "2025-01-06T00:00:00+00:00"
        self.assertEqual(
            self.store.replace_research_universe_snapshots(
                "base",
                "test",
                [
                    {
                        "token_address": "0xAbC",
                        "token_symbol": "TEST",
                        "snapshot_at": snapshot_at,
                        "trader_count": 12,
                        "trade_count": 25,
                        "gross_buy_usd": 80_000,
                        "gross_sell_usd": 40_000,
                        "volume_usd": 120_000,
                        "close_price_usd": 1.0,
                    }
                ],
            ),
            1,
        )
        stored_snapshot = self.store.research_universe_rows("base")[0]
        self.assertEqual(stored_snapshot["net_flow_usd"], 40_000)
        self.assertEqual(
            self.store.replace_research_token_outcomes(
                "test_v2",
                [
                    {
                        "chain_id": "base",
                        "token_address": "0xAbC",
                        "snapshot_at": snapshot_at,
                        "listed_within_90d": False,
                        "labels_matured_through": "2025-04-06T00:00:00+00:00",
                    }
                ],
                chain_id="base",
            ),
            1,
        )
        opportunity = {
            "chain_id": "base",
            "wallet_address": "0x" + "1" * 40,
            "token_address": "0xAbC",
            "token_symbol": "TEST",
            "first_buy_at": "2024-01-01T00:00:00+00:00",
            "last_buy_at": "2024-01-01T01:00:00+00:00",
            "gross_buy_usd": 1_000,
            "outcome_matured": True,
            "listed_within_30d": False,
            "listed_within_60d": False,
            "listed_within_90d": False,
        }
        self.assertEqual(
            self.store.replace_wallet_token_opportunities(
                "base", "test", [opportunity]
            ),
            1,
        )
        self.assertEqual(
            self.store.replace_wallet_token_weekly_flows(
                "base",
                "test",
                [
                    {
                        "wallet_address": opportunity["wallet_address"],
                        "token_address": "0xAbC",
                        "snapshot_at": snapshot_at,
                        "gross_buy_usd": 1_000,
                        "net_buy_usd": 1_000,
                    }
                ],
            ),
            1,
        )
        self.assertEqual(
            self.store.upsert_wallet_research_scores(
                score_wallet_opportunities([opportunity])
            ),
            1,
        )
        self.assertEqual(
            self.store.replace_wallet_identity_edges(
                "base",
                "test",
                [
                    {
                        "wallet_address_a": "0x" + "1" * 40,
                        "wallet_address_b": "0x" + "2" * 40,
                        "edge_type": "shared_funder_hop_1",
                        "confidence": 0.8,
                    }
                ],
            ),
            1,
        )
        self.assertEqual(
            self.store.upsert_token_attention_snapshots(
                [
                    {
                        "chain_id": "base",
                        "token_address": "0xAbC",
                        "observed_at": snapshot_at,
                        "source": "test",
                        "attention_gap_score": 70,
                    }
                ]
            ),
            1,
        )


def signal_row(signal_type: str) -> dict[str, object]:
    return {
        "token_symbol": "TEST",
        "chain_id": "base",
        "contract_address": "0xabc",
        "signal_type": signal_type,
        "signal_level": "low",
        "confidence_score": 50,
        "detected_at": "2025-01-01T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
