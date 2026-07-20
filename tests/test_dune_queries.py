import unittest

from smart_money_radar.dune_queries import render_weekly_evm_universe_sql
from smart_money_radar.ingestion.dune import DuneClient


class DuneUniverseQueryTest(unittest.TestCase):
    def test_evm_universe_covers_ninety_day_followup(self) -> None:
        sql = render_weekly_evm_universe_sql("base")
        self.assertIn("SEQUENCE(0, 13)", sql)
        self.assertIn("block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)", sql)
        self.assertIn("is_dense_zero", sql)

    def test_export_plan_accounts_for_projected_columns(self) -> None:
        client = object.__new__(DuneClient)
        plan = client.export_cost_plan(
            {
                "result_metadata": {
                    "total_row_count": 100_000,
                    "column_names": ["a", "b", "c", "d"],
                    "total_result_set_bytes": 40_000_000,
                }
            },
            columns=["a", "b"],
        )
        self.assertEqual(plan["projected_datapoints"], 200_000)
        self.assertEqual(plan["projected_bytes"], 20_000_000)
        self.assertEqual(plan["estimated_export_credits"], 400.0)


if __name__ == "__main__":
    unittest.main()
