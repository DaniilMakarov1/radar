from __future__ import annotations

import unittest

from smart_money_radar.scoring.wallets import MODEL_VERSION, score_wallet_rows


class WalletScoringTest(unittest.TestCase):
    def test_holding_behavior_separates_accumulator_from_flipper(self) -> None:
        buy_rows = [
            wallet_buy_row("0xaccumulator", symbol="ONE"),
            wallet_buy_row("0xflipper", symbol="ONE"),
        ]
        holding_rows = [
            wallet_holding_row(
                "0xaccumulator",
                symbol="ONE",
                pre_buy_usd=10_000,
                pre_sell_usd=0,
                post_sell_usd=0,
                holding_label="strong_accumulator",
            ),
            wallet_holding_row(
                "0xflipper",
                symbol="ONE",
                pre_buy_usd=10_000,
                pre_sell_usd=9_000,
                post_sell_usd=0,
                holding_label="pre_listing_flipper",
            ),
        ]

        scores = by_wallet(
            score_wallet_rows(
                buy_rows,
                holding_rows=holding_rows,
                dataset_target_count=20,
            )
        )

        accumulator = scores["0xaccumulator"]
        flipper = scores["0xflipper"]
        self.assertEqual(accumulator["target_count"], 1)
        self.assertEqual(flipper["target_count"], 0)
        self.assertGreater(accumulator["interest_score"], flipper["interest_score"])
        self.assertLess(accumulator["noise_score"], flipper["noise_score"])

    def test_dust_second_target_does_not_create_repeatability(self) -> None:
        wallet = "0xdustrepeat"
        buy_rows = [
            wallet_buy_row(wallet, symbol="ONE", gross_buy_usd=20_000),
            wallet_buy_row(wallet, symbol="TWO", gross_buy_usd=6, day=20),
        ]
        holding_rows = [
            wallet_holding_row(
                wallet,
                symbol="ONE",
                pre_buy_usd=20_000,
                pre_sell_usd=0,
                post_sell_usd=0,
                holding_label="strong_accumulator",
            ),
            wallet_holding_row(
                wallet,
                symbol="TWO",
                pre_buy_usd=6,
                pre_sell_usd=0,
                post_sell_usd=0,
                holding_label="dust_accumulator",
                day=20,
            ),
        ]

        score = score_wallet_rows(
            buy_rows,
            holding_rows=holding_rows,
            dataset_target_count=20,
        )[0]

        self.assertEqual(score["model_version"], MODEL_VERSION)
        self.assertEqual(score["target_count"], 1)
        self.assertEqual(score["evidence"]["raw_target_count"], 2)
        self.assertEqual(score["evidence"]["meaningful_target_count"], 1)
        self.assertNotEqual(score["label"], "strong_candidate")

    def test_meaningful_repeat_requires_sufficient_dataset_for_strong_label(self) -> None:
        wallet = "0xrealrepeat"
        buy_rows = [
            wallet_buy_row(wallet, symbol="ONE", gross_buy_usd=5_000),
            wallet_buy_row(wallet, symbol="TWO", gross_buy_usd=2_000, day=20),
        ]
        holding_rows = [
            wallet_holding_row(
                wallet,
                symbol="ONE",
                pre_buy_usd=5_000,
                pre_sell_usd=0,
                post_sell_usd=0,
                holding_label="strong_accumulator",
            ),
            wallet_holding_row(
                wallet,
                symbol="TWO",
                pre_buy_usd=2_000,
                pre_sell_usd=0,
                post_sell_usd=0,
                holding_label="strong_accumulator",
                day=20,
            ),
        ]

        small_dataset = score_wallet_rows(
            buy_rows,
            holding_rows=holding_rows,
            dataset_target_count=2,
        )[0]
        sufficient_dataset = score_wallet_rows(
            buy_rows,
            holding_rows=holding_rows,
            dataset_target_count=20,
        )[0]

        self.assertEqual(small_dataset["target_count"], 2)
        self.assertLessEqual(small_dataset["confidence_score"], 55)
        self.assertNotEqual(small_dataset["label"], "strong_candidate")
        self.assertEqual(sufficient_dataset["label"], "strong_candidate")

    def test_post_listing_exit_is_not_treated_as_pre_listing_flip(self) -> None:
        wallet = "0xdisciplinedexit"
        buy_rows = [wallet_buy_row(wallet, symbol="ONE", gross_buy_usd=10_000)]
        holding_rows = [
            wallet_holding_row(
                wallet,
                symbol="ONE",
                pre_buy_usd=10_000,
                pre_sell_usd=0,
                post_sell_usd=9_000,
                holding_label="post_listing_seller",
            )
        ]

        score = score_wallet_rows(
            buy_rows,
            holding_rows=holding_rows,
            dataset_target_count=20,
        )[0]

        self.assertEqual(score["target_count"], 1)
        self.assertIn("post_listing_realization", score["flags"])
        self.assertNotIn("pre_listing_trading_present", score["flags"])


def by_wallet(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["wallet_address"]): row for row in rows}


def wallet_buy_row(
    wallet_address: str,
    symbol: str,
    gross_buy_usd: float = 10_000,
    day: int = 31,
) -> dict[str, object]:
    token_suffix = "1" if symbol == "ONE" else "2"
    return {
        "chain_id": "base",
        "symbol": symbol,
        "token_address": "0x" + token_suffix * 40,
        "announced_at": f"2026-01-{day:02d}T12:00:00+00:00",
        "wallet_address": wallet_address,
        "first_buy_at": "2026-01-01T12:00:00+00:00",
        "last_buy_at": "2026-01-15T12:00:00+00:00",
        "buy_trade_count": 4,
        "gross_buy_usd": gross_buy_usd,
    }


def wallet_holding_row(
    wallet_address: str,
    symbol: str,
    pre_buy_usd: float,
    pre_sell_usd: float,
    post_sell_usd: float,
    holding_label: str,
    day: int = 31,
) -> dict[str, object]:
    token_suffix = "1" if symbol == "ONE" else "2"
    return {
        "chain_id": "base",
        "symbol": symbol,
        "token_address": "0x" + token_suffix * 40,
        "announced_at": f"2026-01-{day:02d}T12:00:00+00:00",
        "wallet_address": wallet_address,
        "pre_buy_usd": pre_buy_usd,
        "pre_sell_usd": pre_sell_usd,
        "post_buy_usd": 0,
        "post_sell_usd": post_sell_usd,
        "pre_buy_trades": 4,
        "pre_sell_trades": 0 if pre_sell_usd == 0 else 3,
        "post_buy_trades": 0,
        "post_sell_trades": 0 if post_sell_usd == 0 else 3,
        "net_pre_usd": pre_buy_usd - pre_sell_usd,
        "pre_sell_ratio": pre_sell_usd / pre_buy_usd,
        "post_sell_ratio": post_sell_usd / pre_buy_usd,
        "holding_label": holding_label,
    }


if __name__ == "__main__":
    unittest.main()
