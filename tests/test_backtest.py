from __future__ import annotations

import unittest

from smart_money_radar.backtest import walk_forward_wallet_backtest


class WalletWalkForwardBacktestTest(unittest.TestCase):
    def test_walk_forward_uses_only_mature_prior_targets(self) -> None:
        rows = [
            buy("0xa", "ONE", "2026-01-31T12:00:00+00:00", 5_000),
            buy("0xb", "ONE", "2026-01-31T12:00:00+00:00", 4_000),
            buy("0xa", "TWO", "2026-03-15T12:00:00+00:00", 3_000),
            buy("0xc", "TWO", "2026-03-15T12:00:00+00:00", 2_000),
            buy("0xa", "THREE", "2026-05-01T12:00:00+00:00", 2_500),
        ]
        holding_rows = [holding(row) for row in rows]

        result = walk_forward_wallet_backtest(
            rows,
            holding_rows,
            minimum_history_target_count=1,
        )

        self.assertEqual(result["metrics"]["observed_target_count"], 3)
        self.assertEqual(result["metrics"]["evaluated_target_count"], 2)
        self.assertEqual(result["evaluations"][0]["symbol"], "TWO")
        self.assertEqual(result["evaluations"][0]["history_target_count"], 1)
        self.assertIn("0xa", result["evaluations"][0]["evidence"]["hit_wallets"])
        self.assertNotIn("0xc", result["evaluations"][0]["evidence"]["predicted_wallets"])
        self.assertFalse(result["metrics"]["ready_for_wallet_quality_claims"])
        self.assertFalse(result["metrics"]["ready_for_token_prediction_claims"])


def buy(
    wallet: str,
    symbol: str,
    announced_at: str,
    gross_buy_usd: float,
) -> dict[str, object]:
    suffix = {"ONE": "1", "TWO": "2", "THREE": "3"}[symbol]
    announced_day = announced_at[:10]
    return {
        "chain_id": "base",
        "symbol": symbol,
        "token_address": "0x" + suffix * 40,
        "announced_at": announced_at,
        "wallet_address": wallet,
        "first_buy_at": announced_day[:8] + "01T12:00:00+00:00",
        "last_buy_at": announced_day[:8] + "10T12:00:00+00:00",
        "buy_trade_count": 4,
        "gross_buy_usd": gross_buy_usd,
    }


def holding(row: dict[str, object]) -> dict[str, object]:
    pre_buy = float(row["gross_buy_usd"])
    return {
        "chain_id": row["chain_id"],
        "symbol": row["symbol"],
        "token_address": row["token_address"],
        "announced_at": row["announced_at"],
        "wallet_address": row["wallet_address"],
        "pre_buy_usd": pre_buy,
        "pre_sell_usd": 0,
        "post_buy_usd": 0,
        "post_sell_usd": 0,
        "pre_buy_trades": 4,
        "pre_sell_trades": 0,
        "post_buy_trades": 0,
        "post_sell_trades": 0,
        "net_pre_usd": pre_buy,
        "pre_sell_ratio": 0,
        "post_sell_ratio": 0,
        "holding_label": "strong_accumulator",
    }


if __name__ == "__main__":
    unittest.main()
