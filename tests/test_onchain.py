from __future__ import annotations

import unittest
from typing import Any

from smart_money_radar.ingestion.onchain import (
    BlockscoutClient,
    HyperSyncClient,
    MoralisClient,
    TRANSFER_TOPIC,
    pad_evm_topic,
)


class OnchainEnrichmentTest(unittest.TestCase):
    def test_blockscout_normalizes_contract_and_proxy_evidence(self) -> None:
        snapshot = BlockscoutClient(http=FakeBlockscoutHttp()).token_snapshot(
            "base",
            "0x" + "1" * 40,
            observed_at="2026-01-01T00:00:00+00:00",
        )

        self.assertTrue(snapshot["contract_verified"])
        self.assertTrue(snapshot["is_contract"])
        self.assertEqual(snapshot["proxy_type"], "eip1167")
        self.assertEqual(snapshot["holder_count"], 12_345)
        self.assertEqual(snapshot["implementation_addresses"], ["0x" + "2" * 40])

    def test_moralis_separates_eoa_and_contract_concentration(self) -> None:
        snapshot = MoralisClient(
            api_key="test-key",
            http=FakeMoralisHttp(),
        ).token_holder_snapshot(
            "base",
            "0x" + "1" * 40,
            observed_at="2026-01-01T00:00:00+00:00",
        )

        self.assertAlmostEqual(snapshot["top10_holder_ratio"], 0.28)
        self.assertAlmostEqual(snapshot["top10_eoa_holder_ratio"], 0.18)
        self.assertAlmostEqual(snapshot["top10_contract_holder_ratio"], 0.10)
        self.assertAlmostEqual(snapshot["labeled_holder_ratio"], 0.10)

    def test_hypersync_deduplicates_and_classifies_wallet_transfers(self) -> None:
        wallets = ["0x" + "1" * 40, "0x" + "2" * 40]
        snapshot = HyperSyncClient(
            api_token="test-token",
            http=FakeHyperSyncHttp(wallets),
        ).recent_wallet_transfer_snapshot(
            "base",
            "0x" + "3" * 40,
            wallets,
            observed_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(snapshot["transfer_count"], 2)
        self.assertEqual(snapshot["inbound_wallet_count"], 1)
        self.assertEqual(snapshot["outbound_wallet_count"], 1)
        self.assertEqual(snapshot["last_activity_at"], "2025-12-31T23:59:00+00:00")


class FakeBlockscoutHttp:
    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del headers
        if "/addresses/" in url:
            return {
                "is_contract": True,
                "is_verified": True,
                "is_scam": False,
                "reputation": "ok",
                "proxy_type": "eip1167",
                "implementations": [{"address_hash": "0x" + "2" * 40}],
            }
        return {"holders_count": "12345", "reputation": "ok"}


class FakeMoralisHttp:
    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.url = url
        self.headers = headers or {}
        return {
            "result": [
                {
                    "owner_address": "0x" + "4" * 40,
                    "is_contract": False,
                    "percentage_relative_to_total_supply": 18,
                },
                {
                    "owner_address": "0x" + "5" * 40,
                    "is_contract": True,
                    "entity": "Known protocol",
                    "percentage_relative_to_total_supply": 10,
                },
            ],
            "total_supply": "1000000",
        }


class FakeHyperSyncHttp:
    def __init__(self, wallets: list[str]) -> None:
        self.wallets = wallets

    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del url, headers
        return {"height": 1_000_000}

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del url, payload, headers
        inbound = {
            "transaction_hash": "0xaaa",
            "log_index": 1,
            "block_number": 999_990,
            "topic0": TRANSFER_TOPIC,
            "topic1": pad_evm_topic("0x" + "9" * 40),
            "topic2": pad_evm_topic(self.wallets[0]),
        }
        outbound = {
            "transaction_hash": "0xbbb",
            "log_index": 2,
            "block_number": 999_995,
            "topic0": TRANSFER_TOPIC,
            "topic1": pad_evm_topic(self.wallets[1]),
            "topic2": pad_evm_topic("0x" + "8" * 40),
        }
        return {
            "archive_height": 999_999,
            "next_block": 1_000_000,
            "total_execution_time": 3,
            "data": [
                {
                    "blocks": [
                        {"number": 999_990, "timestamp": "0x6955b888"},
                        {"number": 999_995, "timestamp": "0x6955b8c4"},
                    ],
                    "logs": [inbound, outbound],
                },
                {"blocks": [], "logs": [inbound]},
            ],
        }


if __name__ == "__main__":
    unittest.main()
