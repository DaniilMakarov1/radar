from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from smart_money_radar import cli


class CliAnalyticsRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "radar.sqlite"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_analytics_query_prunes_successful_and_failed_execution_metadata(
        self,
    ) -> None:
        for _ in range(7):
            self.assertEqual(
                self.run_cli(
                    "analytics-query",
                    "--sql",
                    "SELECT 1 AS value",
                    "--json",
                ),
                0,
            )

        rows = self.analytics_execution_rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["status"] for row in rows}, {"completed"})

        for _ in range(7):
            self.assertEqual(
                self.run_cli(
                    "analytics-query",
                    "--sql",
                    "SELECT * FROM missing_analytics_table",
                    "--json",
                ),
                1,
            )

        rows = self.analytics_execution_rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["status"] for row in rows}, {"failed"})

    def run_cli(self, *args: str) -> int:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return cli.main(["--db", str(self.db_path), *args])

    def analytics_execution_rows(self) -> list[sqlite3.Row]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return [
                row
                for row in connection.execute(
                    """
                    SELECT analytics_execution_id, status
                    FROM analytics_executions
                    ORDER BY started_at DESC, analytics_execution_id DESC
                    """
                )
            ]


if __name__ == "__main__":
    unittest.main()
