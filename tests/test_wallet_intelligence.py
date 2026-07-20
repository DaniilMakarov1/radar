from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smart_money_radar.scoring.wallets import MODEL_VERSION
from smart_money_radar.storage import SQLiteStore, normalize_dune_timestamp
from smart_money_radar.wallet_intelligence import rebuild_wallet_clusters


class WalletIntelligenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temporary_directory.name) / "radar.sqlite")
        self.store.init_db()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_shared_non_service_funder_forms_a_conservative_cluster(self) -> None:
        first = "0x" + "1" * 40
        second = "0x" + "2" * 40
        self.store.upsert_wallet_scores(
            [
                score_row(first, targets=[target("ONE"), target("TWO", day=2)]),
                score_row(second, targets=[target("ONE"), target("TWO", day=2)]),
            ]
        )
        self.store.import_wallet_funding(
            "base",
            [
                {"wallet_address": first, "funder_address": "0x" + "a" * 40},
                {"wallet_address": second, "funder_address": "0x" + "a" * 40},
            ],
        )

        result = rebuild_wallet_clusters(self.store)
        clusters = self.store.dashboard_clusters()

        self.assertEqual(result["linked_cluster_count"], 1)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["member_count"], 2)
        self.assertIn("shared_first_funder", clusters[0]["methods"])
        self.assertLess(clusters[0]["independence_score"], 1)

    def test_repeated_synchronous_entries_link_without_common_funder(self) -> None:
        first = "0x" + "3" * 40
        second = "0x" + "4" * 40
        self.store.upsert_wallet_scores(
            [
                score_row(first, targets=[target("ONE"), target("TWO", day=2)]),
                score_row(second, targets=[target("ONE"), target("TWO", day=2)]),
            ]
        )

        rebuild_wallet_clusters(self.store)
        clusters = self.store.dashboard_clusters()

        self.assertEqual(clusters[0]["member_count"], 2)
        self.assertIn("repeated_synchronous_entry", clusters[0]["methods"])

    def test_exchange_entity_is_excluded_from_candidate_queries(self) -> None:
        wallet = "0x" + "5" * 40
        self.store.upsert_wallet_scores([score_row(wallet)])
        self.store.import_wallet_entities(
            "base",
            [{"wallet_address": wallet, "cex_name": "Example Exchange"}],
        )

        self.assertEqual(self.store.wallet_score_rows(labels=("watch_candidate",)), [])

    def test_dune_timestamp_is_canonicalized_to_utc(self) -> None:
        self.assertEqual(
            normalize_dune_timestamp("2025-10-03 09:51:47.646"),
            "2025-10-03T09:51:47.646000+00:00",
        )


def score_row(
    wallet_address: str,
    targets: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "wallet_address": wallet_address,
        "chain_id": "base",
        "model_version": MODEL_VERSION,
        "interest_score": 72,
        "noise_score": 12,
        "confidence_score": 55,
        "label": "watch_candidate",
        "target_count": len(targets or [target("ONE")]),
        "symbol_count": len(targets or [target("ONE")]),
        "total_buy_trades": 3,
        "total_gross_buy_usd": 5_000,
        "avg_trade_usd": 1_666,
        "earliest_buy_at": "2025-01-01T10:00:00+00:00",
        "latest_buy_at": "2025-01-02T10:00:00+00:00",
        "max_first_lead_days": 30,
        "min_last_lead_hours": 24,
        "active_span_days": 1,
        "flags": [],
        "evidence": {
            "targets": targets or [target("ONE")],
            "why_smart_money": ["Test evidence"],
            "counter_evidence": [],
        },
    }


def target(symbol: str, day: int = 1) -> dict[str, object]:
    suffix = "1" if symbol == "ONE" else "2"
    return {
        "symbol": symbol,
        "token_address": "0x" + suffix * 40,
        "announced_at": f"2025-02-{day + 10:02d}T10:00:00+00:00",
        "first_buy_at": f"2025-01-{day:02d}T10:00:00+00:00",
        "last_buy_at": f"2025-01-{day:02d}T11:00:00+00:00",
        "is_meaningful": True,
    }


if __name__ == "__main__":
    unittest.main()
