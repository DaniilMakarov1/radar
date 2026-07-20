import unittest

from smart_money_radar.ml import build_feature_rows, dataset_readiness_metrics


class ResearchMlTest(unittest.TestCase):
    def test_same_contract_address_on_two_chains_stays_independent(self) -> None:
        rows = [training_row("base"), training_row("bsc")]
        smart = {
            ("base", rows[0]["snapshot_at"], "0xabc"): {
                "wallet_count": 3,
                "net_buy_usd": 4_000,
                "top_wallet_share": 0.4,
            },
            ("bsc", rows[1]["snapshot_at"], "0xabc"): {
                "wallet_count": 7,
                "net_buy_usd": 9_000,
                "top_wallet_share": 0.2,
            },
        }

        dataset = build_feature_rows(rows, smart, "listed_within_90d")

        self.assertEqual(len(dataset), 2)
        by_chain = {row["chain_id"]: row for row in dataset}
        self.assertEqual(by_chain["base"]["features"]["smart_wallet_count"], 3)
        self.assertEqual(by_chain["bsc"]["features"]["smart_wallet_count"], 7)

    def test_training_gate_requires_both_snapshots_and_events(self) -> None:
        dataset = [{"label": 0}] * 100_000
        metrics = dataset_readiness_metrics(
            dataset,
            positive_tokens={f"event-{index}" for index in range(99)},
            smart_coverage=1.0,
        )
        self.assertFalse(metrics["dataset_gate_passed"])

        metrics = dataset_readiness_metrics(
            dataset,
            positive_tokens={f"event-{index}" for index in range(100)},
            smart_coverage=0.5,
        )
        self.assertTrue(metrics["dataset_gate_passed"])
        self.assertFalse(metrics["dataset_ready"])


def training_row(chain_id: str) -> dict[str, object]:
    return {
        "chain_id": chain_id,
        "token_address": "0xAbC",
        "snapshot_at": "2025-01-06T00:00:00+00:00",
        "listed_within_90d": 0,
        "is_tradeable": 1,
        "outcome_evidence": {"eligible_for_listing_prediction": True},
        "volume_usd": 100_000,
        "trader_count": 100,
        "trade_count": 500,
        "buyer_count": 60,
        "seller_count": 50,
        "net_flow_usd": 5_000,
        "gross_buy_usd": 55_000,
        "pair_count": 2,
        "dex_count": 2,
        "close_price_usd": 1.0,
    }


if __name__ == "__main__":
    unittest.main()
