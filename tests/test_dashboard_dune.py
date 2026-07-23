from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from smart_money_radar.dashboard import build_handler
from smart_money_radar.storage import SQLiteStore


class DashboardDuneApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "radar.sqlite")
        self.store.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_dune_overview_returns_catalog_and_retention_policy(self) -> None:
        payload = self.get_json("/api/dune")

        query_slugs = {
            row["query_slug"]
            for row in payload["catalog"]["queries"]
        }
        source_labels = {
            row["label"]
            for row in payload["local_sources"]
        }

        self.assertEqual(payload["status"], "ready")
        self.assertIn("funding-route-leaders", query_slugs)
        self.assertIn("Local dex_trades", source_labels)
        self.assertFalse(payload["retention"]["raw_logs_stored"])
        self.assertEqual(payload["retention"]["local_snapshot_retention"], 1)
        self.assertEqual(payload["retention"]["analytics_execution_retention"], 5)

    def test_dune_query_runs_read_only_sql_and_rejects_writes(self) -> None:
        payload = self.post_json(
            "/api/dune/query",
            {"sql": "SELECT 42 AS answer", "limit": 10},
        )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["result"]["columns"], ["answer"])
        self.assertEqual(payload["result"]["rows"][0]["answer"], 42)

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/dune/query",
                {"sql": "SELECT * FROM tokens; DELETE FROM tokens"},
            )

        self.assertEqual(error.exception.code, 400)
        error_payload = json.loads(error.exception.read().decode("utf-8"))
        self.assertIn("single SELECT", error_payload["error"])

    def test_dune_saved_query_prunes_execution_metadata(self) -> None:
        for _ in range(7):
            self.post_json(
                "/api/dune/saved-query",
                {"slug": "funding-route-leaders", "limit": 5},
            )

        with self.store.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM analytics_executions"
            ).fetchone()[0]

        self.assertEqual(count, 5)

    def test_dune_failed_query_prunes_execution_metadata(self) -> None:
        for _ in range(7):
            with self.assertRaises(HTTPError):
                self.post_json(
                    "/api/dune/query",
                    {"sql": "SELECT * FROM missing_analytics_table", "limit": 5},
                )

        with self.store.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM analytics_executions"
            ).fetchone()[0]
            failed = connection.execute(
                """
                SELECT COUNT(*)
                FROM analytics_executions
                WHERE status = 'failed'
                """
            ).fetchone()[0]

        self.assertEqual(count, 5)
        self.assertEqual(failed, 5)

    def test_dune_local_scan_queues_background_job(self) -> None:
        calls = []

        def fake_scan(**kwargs):
            calls.append(kwargs)
            return {
                "status": "completed",
                "tracked_wallet_count": 3,
                "transfer_event_count": 4,
                "swap_event_count": 5,
                "dex_trade_count": 1,
                "dex_rollup_count": 1,
                "observation_count": 1,
                "raw_logs_stored": False,
            }

        with patch("smart_money_radar.dashboard_dune.run_base_local_live_scan", side_effect=fake_scan):
            queued = self.post_json(
                "/api/actions/dune-local-scan",
                {
                    "window_hours": 12,
                    "max_wallets": 4,
                    "max_tokens": 3,
                    "minimum_wallets": 1,
                    "max_hypersync_pages": 1,
                    "max_pairs_per_token": 2,
                    "min_pair_liquidity_usd": 500,
                    "use_cursor": True,
                    "generate_signals": False,
                    "allow_transfer_fallback": False,
                },
                expected_status=202,
            )
            job = self.wait_for_job(queued["job_id"])

        self.assertEqual(job["status"], "success")
        self.assertEqual(job["result"]["dex_trade_count"], 1)
        self.assertFalse(job["result"]["raw_logs_stored"])
        self.assertEqual(calls[0]["window_hours"], 12)
        self.assertEqual(calls[0]["max_wallets"], 4)
        self.assertTrue(calls[0]["use_cursor"])
        self.assertFalse(calls[0]["generate_signals"])
        self.assertFalse(calls[0]["allow_transfer_fallback"])

    def get_json(self, path: str) -> dict[str, object]:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def post_json(
        self,
        path: str,
        payload: dict[str, object],
        expected_status: int = 200,
    ) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, expected_status)
            return json.loads(response.read().decode("utf-8"))

    def wait_for_job(self, job_id: int) -> dict[str, object]:
        deadline = time.time() + 5
        while time.time() < deadline:
            job = self.get_json(f"/api/jobs/{job_id}")
            if job["status"] in {"success", "failed"}:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish")


if __name__ == "__main__":
    unittest.main()
