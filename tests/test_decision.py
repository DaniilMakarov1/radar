from __future__ import annotations

import unittest

from smart_money_radar.decision import (
    build_capital_readiness,
    build_signal_decision,
)


class CapitalDecisionTest(unittest.TestCase):
    def test_empty_research_state_never_allows_capital(self) -> None:
        readiness = build_capital_readiness({})

        self.assertEqual(readiness["capital_state"], "research_only")
        self.assertFalse(readiness["trade_eligible"])
        self.assertFalse(readiness["automatic_execution_enabled"])
        self.assertTrue(readiness["reasons"])

    def test_every_gate_is_required_for_trade_eligibility(self) -> None:
        research = {
            "universe": {"snapshot_count": 100_000},
            "outcomes": {"independent_positive_event_count": 100},
            "wallets": {
                "matured_opportunity_count": 1_000,
                "wallet_count": 50,
            },
            "model_runs": [
                {
                    "status": "completed_ready",
                    "metrics": {"ready_for_research_claims": True},
                }
            ],
        }
        readiness = build_capital_readiness(
            research,
            identity={"coverage_ratio": 0.95},
            shadow={"signal_count": 100, "mark_count": 1_000, "history_days": 60},
        )
        decision = build_signal_decision("candidate", "accumulation", readiness)

        self.assertEqual(readiness["capital_state"], "trade_eligible")
        self.assertTrue(decision["trade_eligible"])
        self.assertEqual(decision["recommended_position_usd"], 0.0)
        self.assertFalse(decision["automatic_execution_enabled"])


if __name__ == "__main__":
    unittest.main()
