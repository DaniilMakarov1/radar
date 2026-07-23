from __future__ import annotations

import unittest
from typing import Any

from smart_money_radar.ingestion.onchain import (
    BlockscoutClient,
    HyperSyncClient,
    MoralisClient,
    TRANSFER_TOPIC,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V3_SWAP_TOPIC,
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

    def test_hypersync_decodes_pool_swap_events(self) -> None:
        pair = "0x" + "7" * 40
        result = HyperSyncClient(
            api_token="test-token",
            http=FakeHyperSyncSwapHttp(pair),
        ).recent_pool_swap_events(
            "base",
            [pair],
            observed_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(len(result["events"]), 2)
        v2 = result["events"][0]
        v3 = result["events"][1]
        self.assertEqual(v2["protocol_shape"], "v2")
        self.assertEqual(v2["amount0_in_raw"], "10")
        self.assertEqual(v2["amount1_out_raw"], "20")
        self.assertEqual(v2["block_time"], "2025-12-31T23:57:56+00:00")
        self.assertEqual(v2["tx_from"], "0x" + "a" * 40)
        self.assertEqual(v2["tx_to"], "0x" + "b" * 40)
        self.assertEqual(v3["protocol_shape"], "v3")
        self.assertEqual(v3["amount0_delta_raw"], "-3")
        self.assertEqual(v3["amount1_delta_raw"], "4")


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


class FakeHyperSyncSwapHttp:
    def __init__(self, pair: str) -> None:
        self.pair = pair

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
        return {
            "next_block": 1_000_000,
            "data": [
                {
                    "blocks": [
                        {"number": 999_990, "timestamp": "0x6955b884"},
                    ],
                    "logs": [
                        {
                            "transaction_hash": "0xaaa",
                            "log_index": 1,
                            "block_number": 999_990,
                            "address": self.pair,
                            "topic0": UNISWAP_V2_SWAP_TOPIC,
                            "topic1": pad_evm_topic("0x" + "1" * 40),
                            "topic2": pad_evm_topic("0x" + "2" * 40),
                            "data": "0x"
                            + abi_word(10)
                            + abi_word(0)
                            + abi_word(0)
                            + abi_word(20),
                        },
                        {
                            "transaction_hash": "0xbbb",
                            "log_index": 2,
                            "block_number": 999_990,
                            "address": self.pair,
                            "topic0": UNISWAP_V3_SWAP_TOPIC,
                            "topic1": pad_evm_topic("0x" + "3" * 40),
                            "topic2": pad_evm_topic("0x" + "4" * 40),
                            "data": "0x" + abi_word(-3) + abi_word(4),
                        },
                    ],
                    "transactions": [
                        {
                            "hash": "0xaaa",
                            "from": "0x" + "a" * 40,
                            "to": "0x" + "b" * 40,
                        },
                        {
                            "hash": "0xbbb",
                            "from": "0x" + "c" * 40,
                            "to": "0x" + "d" * 40,
                        },
                    ],
                },
            ],
        }


def abi_word(value: int) -> str:
    if value < 0:
        value += 2**256
    return value.to_bytes(32, "big").hex()


if __name__ == "__main__":
    unittest.main()
