from __future__ import annotations

import unittest

from smart_money_radar.ingestion.binance_announcements import (
    extract_contract_links,
    normalize_announcement,
    should_fetch_listing_detail,
)
from smart_money_radar.storage import is_initial_binance_listing_title


class BinanceAnnouncementNormalizationTest(unittest.TestCase):
    def test_listing_detail_filter(self) -> None:
        self.assertTrue(should_fetch_listing_detail("Binance Will List Example (EX)"))
        self.assertTrue(should_fetch_listing_detail("Example (EX) on Binance Alpha"))
        self.assertFalse(should_fetch_listing_detail("Binance Adds EX to Convert"))

    def test_tokenized_stock_is_not_spot_crypto(self) -> None:
        normalized = normalize_announcement(
            "Binance Exchange Adds 10 bStocks Trading Pair(s) on Binance Spot - 2026-07-07",
            None,
            [],
        )

        self.assertEqual(normalized["category"], "tokenized_stock")
        self.assertFalse(normalized["is_spot_listing"])

    def test_margin_is_not_spot_crypto(self) -> None:
        normalized = normalize_announcement(
            "Binance Margin Will Add New Pairs - 2026-06-30",
            "Spot trading is not relevant noise in the article footer.",
            [],
        )

        self.assertEqual(normalized["category"], "margin")
        self.assertFalse(normalized["is_spot_listing"])

    def test_product_bundle_is_not_spot_crypto(self) -> None:
        normalized = normalize_announcement(
            "Binance Will Add Gram (GRAM) on Earn, Buy Crypto, Convert, VIP Loan, Margin & Futures",
            None,
            [],
        )

        self.assertEqual(normalized["category"], "product_bundle")
        self.assertFalse(normalized["is_spot_listing"])

    def test_crypto_spot_listing_extracts_symbol_and_pair(self) -> None:
        normalized = normalize_announcement(
            "Binance Will List Example Token (EXAMPLE) with Seed Tag Applied",
            "Trading will open for EXAMPLE/USDT at 2026-01-01 10:00 (UTC).",
            [],
        )

        self.assertEqual(normalized["category"], "spot")
        self.assertTrue(normalized["is_spot_listing"])
        self.assertIn("EXAMPLE", normalized["extracted_symbols"])
        self.assertIn("EXAMPLE/USDT", normalized["trading_pairs"])

    def test_distribution_campaign_is_a_listing_bearing_announcement(self) -> None:
        normalized = normalize_announcement(
            "Introducing Example (EX) on Binance HODLer Airdrops!",
            None,
            [],
        )

        self.assertEqual(normalized["category"], "alpha_or_airdrop")
        self.assertTrue(normalized["is_spot_listing"])

    def test_binance_alpha_alone_is_not_a_spot_listing(self) -> None:
        normalized = normalize_announcement(
            "Example (EX) Is Now Live on Binance Alpha",
            None,
            [],
        )

        self.assertEqual(normalized["category"], "alpha_or_airdrop")
        self.assertFalse(normalized["is_spot_listing"])

    def test_contract_links_extract_chain_and_address(self) -> None:
        links = [
            {
                "href": "https://basescan.org/token/0xFbC2051AE2265686a469421b2C5A2D5462FbF5eB",
                "text": "Base",
            },
            {
                "href": "https://bscscan.com/token/0x5feCcD17C393CaF1001D18164236A37E731FCb9d#transactions",
                "text": "BNB Smart Chain",
            },
        ]

        contracts = extract_contract_links(links)

        self.assertEqual(
            contracts,
            [
                {
                    "chain_id": "base",
                    "address": "0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb",
                    "href": links[0]["href"],
                    "text": "Base",
                },
                {
                    "chain_id": "bsc",
                    "address": "0x5feccd17c393caf1001d18164236a37e731fcb9d",
                    "href": links[1]["href"],
                    "text": "BNB Smart Chain",
                },
            ],
        )

    def test_pair_expansion_is_not_a_new_listing_target(self) -> None:
        self.assertFalse(
            is_initial_binance_listing_title(
                "Notice on New Trading Pairs & Trading Bots Services on Binance Spot"
            )
        )
        self.assertFalse(
            is_initial_binance_listing_title(
                "Binance Adds USDC on Earn, Buy Crypto, Convert & Margin"
            )
        )
        self.assertTrue(
            is_initial_binance_listing_title(
                "Binance Will List Example Token (EXAMPLE) with Seed Tag Applied"
            )
        )


if __name__ == "__main__":
    unittest.main()
