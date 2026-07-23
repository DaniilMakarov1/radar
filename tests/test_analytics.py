from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.analytics import (
    AnalyticsError,
    initialize_analytics,
    prune_analytics_executions,
    run_saved_query,
    run_sql,
)
from smart_money_radar.scoring.wallets import score_wallet_rows
from smart_money_radar.storage import SQLiteStore


class AnalyticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "radar.sqlite")
        self.store.init_db()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_initializes_catalog_with_builtin_views_and_queries(self) -> None:
        catalog = initialize_analytics(self.store)

        view_names = {row["name"] for row in catalog["views"]}
        query_slugs = {row["query_slug"] for row in catalog["queries"]}

        self.assertIn("analytics_pre_listing_wallet_flows", view_names)
        self.assertIn("analytics_research_token_snapshots", view_names)
        self.assertIn("pre-listing-wallet-leaders", query_slugs)
        self.assertEqual(catalog["engine"], "sqlite")

    def test_reinitializing_catalog_does_not_rewrite_unchanged_query_timestamps(self) -> None:
        initialize_analytics(self.store)
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE analytics_queries
                SET updated_at = '2026-01-01T00:00:00+00:00'
                WHERE query_slug = 'funding-route-leaders'
                """
            )

        initialize_analytics(self.store)

        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT updated_at
                FROM analytics_queries
                WHERE query_slug = 'funding-route-leaders'
                """
            ).fetchone()

        self.assertEqual(row["updated_at"], "2026-01-01T00:00:00+00:00")

    def test_runs_builtin_pre_listing_wallet_leader_query_locally(self) -> None:
        wallet = "0x" + "1" * 40
        self.store.import_pre_listing_wallet_buys(
            chain_id="base",
            execution_id="local-test",
            rows=[
                buyer_row("AAA", "0x" + "a" * 40, wallet, 2_500),
                buyer_row("BBB", "0x" + "b" * 40, wallet, 1_500),
            ],
        )
        self.store.upsert_wallet_scores(
            score_wallet_rows(
                self.store.pre_listing_wallet_buy_rows(),
                dataset_target_count=2,
            )
        )

        result = run_saved_query(
            self.store,
            "pre-listing-wallet-leaders",
            limit=10,
        )

        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["wallet_address"], wallet)
        self.assertEqual(result["rows"][0]["token_count"], 2)
        self.assertEqual(result["rows"][0]["gross_buy_usd"], 4_000)

    def test_rejects_write_sql(self) -> None:
        with self.assertRaises(AnalyticsError):
            run_sql(self.store, "DROP TABLE tokens")

        with self.assertRaises(AnalyticsError):
            run_sql(self.store, "SELECT * FROM tokens; DELETE FROM tokens")

    def test_allows_read_only_strings_that_look_like_admin_keywords(self) -> None:
        result = run_sql(self.store, "SELECT 'drop update pragma' AS note")

        self.assertEqual(result["rows"][0]["note"], "drop update pragma")

    def test_authorizer_rejects_admin_pragmas_inside_select(self) -> None:
        with self.assertRaises(AnalyticsError):
            run_sql(self.store, "SELECT * FROM pragma_database_list")

    def test_prunes_analytics_execution_metadata(self) -> None:
        for index in range(7):
            run_sql(self.store, f"SELECT {index} AS value")

        pruned = prune_analytics_executions(self.store, keep_latest=5)

        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT analytics_execution_id
                FROM analytics_executions
                ORDER BY started_at DESC, analytics_execution_id DESC
                """
            ).fetchall()

        self.assertEqual(pruned, 2)
        self.assertEqual(len(rows), 5)


def buyer_row(
    symbol: str,
    token_address: str,
    wallet_address: str,
    gross_buy_usd: float,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "token_address": token_address,
        "announced_at": "2025-03-01T00:00:00+00:00",
        "wallet_address": wallet_address,
        "first_buy_at": "2025-02-01T00:00:00+00:00",
        "last_buy_at": "2025-02-02T00:00:00+00:00",
        "buy_trade_count": 2,
        "gross_buy_usd": gross_buy_usd,
    }


if __name__ == "__main__":
    unittest.main()
