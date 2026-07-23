from __future__ import annotations

from typing import Any
import unittest

from smart_money_radar.ingestion.market import DexScreenerClient


class DexScreenerMarketTest(unittest.TestCase):
    def test_market_snapshot_keeps_compact_top_pair_candidates(self) -> None:
        token = "0x" + "a" * 40
        snapshots = DexScreenerClient(http=FakeDexScreenerHttp(token)).token_market_snapshots(
            "base",
            [token],
            observed_at="2026-01-01T00:00:00+00:00",
            max_pairs_per_token=2,
            minimum_pair_liquidity_usd=1_000,
        )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["pair_address"], "0x" + "2" * 40)
        candidates = snapshots[0]["raw"]["candidatePairs"]
        self.assertEqual([row["pairAddress"] for row in candidates], [
            "0x" + "2" * 40,
            "0x" + "1" * 40,
        ])
        self.assertEqual(candidates[0]["liquidity_usd"], 10_000)
        self.assertNotIn("info", candidates[0])


class FakeDexScreenerHttp:
    def __init__(self, token: str) -> None:
        self.token = token

    def get_json(self, url: str) -> list[dict[str, Any]]:
        del url
        return [
            pair(self.token, "0x" + "1" * 40, 5_000),
            pair(self.token, "0x" + "2" * 40, 10_000),
            pair(self.token, "0x" + "f" * 64, 20_000),
            pair(self.token, "0x" + "3" * 40, 500),
        ]


def pair(token: str, pair_address: str, liquidity_usd: float) -> dict[str, Any]:
    return {
        "chainId": "base",
        "pairAddress": pair_address,
        "dexId": "aerodrome",
        "priceUsd": "100",
        "liquidity": {"usd": liquidity_usd},
        "volume": {"h24": 50_000},
        "pairCreatedAt": 1_700_000_000_000,
        "baseToken": {"address": token, "symbol": "LOCAL", "name": "Local"},
        "quoteToken": {
            "address": "0x" + "b" * 40,
            "symbol": "WETH",
            "name": "Wrapped Ether",
        },
        "info": {"websites": [], "socials": []},
    }


if __name__ == "__main__":
    unittest.main()
