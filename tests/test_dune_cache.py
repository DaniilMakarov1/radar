from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_money_radar.ingestion.dune import DuneClient


class FakeDuneClient(DuneClient):
    def __init__(self) -> None:
        self.calls: list[int] = []

    def execution_results(
        self,
        execution_id: str,
        limit: int = 1000,
        offset: int = 0,
        filters: str | None = None,
        columns: list[str] | None = None,
        sort_by: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(offset)
        return {
            "state": "QUERY_STATE_COMPLETED",
            "next_offset": offset + 1 if offset < 2 else None,
            "result": {
                "rows": [{"offset": offset}],
                "metadata": {"total_row_count": 3},
            },
        }


class DunePageCacheTest(unittest.TestCase):
    def test_cached_pages_are_reused_on_retry(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeDuneClient()
            first = client.execution_results_all_cached(
                "execution", Path(directory), limit=1, filters="volume_usd > 0"
            )
            self.assertEqual(sorted(client.calls), [0, 1, 2])
            self.assertEqual(len(first["result"]["rows"]), 3)

            client.calls.clear()
            second = client.execution_results_all_cached(
                "execution", Path(directory), limit=1, filters="volume_usd > 0"
            )
            self.assertEqual(client.calls, [])
            self.assertEqual(len(second["result"]["rows"]), 3)

    def test_incomplete_pagination_is_rejected(self) -> None:
        class IncompleteClient(FakeDuneClient):
            def execution_results(self, *args, **kwargs):
                self.calls.append(int(kwargs.get("offset", 0)))
                return {
                    "state": "QUERY_STATE_COMPLETED",
                    "next_offset": None,
                    "result": {
                        "rows": [{"offset": 0}],
                        "metadata": {"total_row_count": 2},
                    },
                }

        with TemporaryDirectory() as directory:
            client = IncompleteClient()
            with self.assertRaisesRegex(RuntimeError, "pagination does not match"):
                client.execution_results_all_cached(
                    "execution", Path(directory), limit=1
                )

    def test_filtered_pagination_uses_next_offset_not_unfiltered_total(self) -> None:
        class FilteredClient(FakeDuneClient):
            def execution_results(self, *args, **kwargs):
                offset = int(kwargs.get("offset", 0))
                self.calls.append(offset)
                return {
                    "state": "QUERY_STATE_COMPLETED",
                    "next_offset": 1 if offset == 0 else None,
                    "result": {
                        "rows": [{"offset": offset}],
                        "metadata": {"total_row_count": 3},
                    },
                }

        with TemporaryDirectory() as directory:
            client = FilteredClient()
            result = client.execution_results_all_cached(
                "execution", Path(directory), limit=1, filters="included = true"
            )
            self.assertEqual(client.calls, [0, 1])
            self.assertEqual(len(result["result"]["rows"]), 2)
            self.assertEqual(
                result["result"]["metadata"]["unfiltered_total_row_count"],
                3,
            )


if __name__ == "__main__":
    unittest.main()
