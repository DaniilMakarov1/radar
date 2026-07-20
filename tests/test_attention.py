import unittest

from smart_money_radar.attention import (
    acceleration_score,
    period_acceleration_score,
    weighted_available,
)
from smart_money_radar.ingestion.social import gdelt_seen_at


class AttentionGapTest(unittest.TestCase):
    def test_gdelt_timestamp_parser_is_utc_and_fail_closed(self) -> None:
        parsed = gdelt_seen_at("20260712T120000Z")

        self.assertEqual(parsed.isoformat(), "2026-07-12T12:00:00+00:00")
        self.assertIsNone(gdelt_seen_at("not-a-time"))

    def test_public_attention_acceleration_compares_recent_rate(self) -> None:
        self.assertGreater(acceleration_score(20, 26), 90)
        self.assertLess(acceleration_score(1, 61), 50)
        self.assertEqual(acceleration_score(0, 0), 50)

    def test_missing_sources_do_not_count_as_zero_attention(self) -> None:
        score = weighted_available(((None, 0.65), (40.0, 0.35)))
        self.assertEqual(score, 40.0)
        self.assertIsNone(period_acceleration_score(None, 1, 10, 6))


if __name__ == "__main__":
    unittest.main()
