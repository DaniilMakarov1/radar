import unittest

from smart_money_radar.wallet_research import score_wallet_opportunities


class WalletResearchScoreTest(unittest.TestCase):
    def test_bayesian_shrinkage_penalizes_one_lucky_hit(self) -> None:
        rows = [opportunity("0xlow", True)]
        rows.extend(opportunity("0xcontrol", index < 10) for index in range(100))
        rows.extend(opportunity("0xhigh", index < 30) for index in range(40))

        scores = {
            row["wallet_address"]: row for row in score_wallet_opportunities(rows)
        }
        low = scores["0xlow"]
        high = scores["0xhigh"]

        self.assertLess(low["posterior_hit_rate"], 1.0)
        self.assertEqual(low["label"], "insufficient_denominator")
        self.assertGreater(high["posterior_hit_rate"], high["baseline_rate"])
        self.assertGreater(high["posterior_lower_95"], low["posterior_lower_95"])

    def test_score_includes_misses_pnl_drawdown_turnover_and_exit(self) -> None:
        rows = [
            {
                **opportunity("0xwallet", hit=index < 12),
                "estimated_return": 0.4 if index < 12 else -0.3,
                "estimated_pnl_usd": 400 if index < 12 else -300,
                "max_drawdown_90d": -0.25,
                "turnover_ratio": 1.4,
                "position_to_observed_flow": 0.08,
                "exit_quality_score": 72,
            }
            for index in range(20)
        ]
        score = score_wallet_opportunities(rows)[0]

        self.assertEqual(score["hit_count_90d"], 12)
        self.assertEqual(score["miss_count_90d"], 8)
        self.assertAlmostEqual(score["median_turnover"], 1.4)
        self.assertAlmostEqual(score["median_position_to_flow"], 0.08)
        self.assertAlmostEqual(score["median_exit_quality"], 72)


def opportunity(wallet_address: str, hit: bool) -> dict[str, object]:
    return {
        "chain_id": "base",
        "wallet_address": wallet_address,
        "outcome_matured": True,
        "listed_within_30d": hit,
        "listed_within_60d": hit,
        "listed_within_90d": hit,
    }


if __name__ == "__main__":
    unittest.main()
