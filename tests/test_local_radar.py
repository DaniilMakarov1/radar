from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from smart_money_radar.local_radar import (
    LOCAL_DEX_SOURCE,
    LOCAL_LIVE_SOURCE,
    LOCAL_MARKET_SOURCE,
    run_base_local_live_scan,
)
from smart_money_radar.storage import SQLiteStore


class LocalRadarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.directory.name) / "radar.sqlite")
        self.store.init_db()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_local_scan_keeps_only_latest_local_snapshot(self) -> None:
        wallet_a = "0x" + "1" * 40
        wallet_b = "0x" + "2" * 40
        token = "0x" + "a" * 40
        self.store.upsert_wallet_scores(
            [
                wallet_score(wallet_a, "strong_candidate", 72, 54),
                wallet_score(wallet_b, "watch_candidate", 61, 50),
            ]
        )
        first = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T00:00:00+00:00",
            hypersync=FakeHyperSync(token, wallet_a, wallet_b),
            market=FakeMarket(token),
            rpc=FakeRpc(),
        )
        second = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T01:00:00+00:00",
            hypersync=FakeHyperSync(token, wallet_a, wallet_b),
            market=FakeMarket(token),
            rpc=FakeRpc(),
        )

        self.assertEqual(first["observation_count"], 1)
        self.assertEqual(second["observation_count"], 1)
        self.assertEqual(second["pruned_observation_count"], 1)
        self.assertEqual(second["pruned_market_snapshot_count"], 1)
        with self.store.connect() as connection:
            observation_rows = connection.execute(
                """
                SELECT observed_at, source, net_buy_usd, evidence_json
                FROM radar_observations
                WHERE source = ?
                """,
                (LOCAL_LIVE_SOURCE,),
            ).fetchall()
            market_rows = connection.execute(
                """
                SELECT observed_at, source
                FROM token_market_snapshots
                WHERE source = ?
                """,
                (LOCAL_MARKET_SOURCE,),
            ).fetchall()

        self.assertEqual(len(observation_rows), 1)
        self.assertEqual(observation_rows[0]["observed_at"], "2026-01-01T01:00:00+00:00")
        self.assertEqual(observation_rows[0]["net_buy_usd"], 1_300)
        self.assertEqual(len(market_rows), 1)
        self.assertEqual(market_rows[0]["observed_at"], "2026-01-01T01:00:00+00:00")

    def test_local_scan_writes_swap_backed_dex_trades_latest_only(self) -> None:
        wallet_a = "0x" + "1" * 40
        wallet_b = "0x" + "2" * 40
        token = "0x" + "a" * 40
        pair = "0x" + "b" * 40
        self.store.upsert_wallet_scores(
            [
                wallet_score(wallet_a, "strong_candidate", 72, 54),
                wallet_score(wallet_b, "watch_candidate", 61, 50),
            ]
        )
        first = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T00:00:00+00:00",
            hypersync=FakeHyperSyncWithSwap(token, pair, wallet_a, wallet_b),
            market=FakeMarket(token, pair),
            rpc=FakeRpc(),
        )
        second = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T01:00:00+00:00",
            hypersync=FakeHyperSyncWithSwap(token, pair, wallet_a, wallet_b),
            market=FakeMarket(token, pair),
            rpc=FakeRpc(),
        )

        self.assertEqual(first["dex_trade_count"], 2)
        self.assertEqual(first["dex_rollup_count"], 1)
        self.assertEqual(first["dex_observation_count"], 1)
        self.assertEqual(first["fallback_observation_count"], 0)
        self.assertEqual(second["dex_trade_count"], 2)
        with self.store.connect() as connection:
            dex_trade_summary = connection.execute(
                """
                SELECT COUNT(*) AS row_count, COUNT(DISTINCT observed_at) AS snapshots
                FROM local_dex_trades
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()
            dex_rollup_summary = connection.execute(
                """
                SELECT COUNT(*) AS row_count, COUNT(DISTINCT observed_at) AS snapshots
                FROM local_dex_rollups
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()
            observation_rows = connection.execute(
                """
                SELECT source, observed_at, net_buy_usd
                FROM radar_observations
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchall()
            fallback_rows = connection.execute(
                """
                SELECT COUNT(*) AS row_count
                FROM radar_observations
                WHERE source = ?
                """,
                (LOCAL_LIVE_SOURCE,),
            ).fetchone()
            cursor = connection.execute(
                """
                SELECT block_number, metadata_json
                FROM local_ingestion_cursors
                WHERE source = ? AND cursor_key = 'candidate_pair_swaps'
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()

        self.assertEqual(dex_trade_summary["row_count"], 2)
        self.assertEqual(dex_trade_summary["snapshots"], 1)
        self.assertEqual(dex_rollup_summary["row_count"], 1)
        self.assertEqual(dex_rollup_summary["snapshots"], 1)
        self.assertEqual(len(observation_rows), 1)
        self.assertEqual(observation_rows[0]["observed_at"], "2026-01-01T01:00:00+00:00")
        self.assertEqual(observation_rows[0]["net_buy_usd"], 1_300)
        self.assertEqual(fallback_rows["row_count"], 0)
        self.assertEqual(cursor["block_number"], 200)

    def test_local_scan_matches_secondary_pair_by_tracked_tx_sender(self) -> None:
        wallet_a = "0x" + "1" * 40
        wallet_b = "0x" + "2" * 40
        token = "0x" + "a" * 40
        main_pair = "0x" + "b" * 40
        secondary_pair = "0x" + "d" * 40
        self.store.upsert_wallet_scores(
            [
                wallet_score(wallet_a, "strong_candidate", 72, 54),
                wallet_score(wallet_b, "watch_candidate", 61, 50),
            ]
        )

        result = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T00:00:00+00:00",
            hypersync=FakeHyperSyncSignerSwap(token, secondary_pair, wallet_a, wallet_b),
            market=FakeMarket(token, main_pair, candidate_pairs=[main_pair, secondary_pair]),
            rpc=FakeRpc(),
        )

        self.assertEqual(result["pair_candidate_count"], 2)
        self.assertEqual(result["pair_token_count"], 2)
        self.assertEqual(result["dex_trade_count"], 1)
        self.assertEqual(result["dex_observation_count"], 1)
        self.assertEqual(result["fallback_observation_count"], 0)
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT pair_address, wallet_address, side, amount_usd, evidence_json
                FROM local_dex_trades
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()
            observation = connection.execute(
                """
                SELECT net_buy_usd
                FROM radar_observations
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()

        self.assertEqual(row["pair_address"], secondary_pair)
        self.assertEqual(row["wallet_address"], wallet_a)
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["amount_usd"], 700)
        self.assertIn("tracked_tx_sender_swap_delta", row["evidence_json"])
        self.assertEqual(observation["net_buy_usd"], 700)

    def test_local_scan_infers_router_recipient_swap_from_market_pair_tokens(
        self,
    ) -> None:
        wallet_a = "0x" + "1" * 40
        wallet_b = "0x" + "2" * 40
        token = "0x" + "a" * 40
        pair = "0x" + "b" * 40
        self.store.upsert_wallet_scores(
            [
                wallet_score(wallet_a, "strong_candidate", 72, 54),
                wallet_score(wallet_b, "watch_candidate", 61, 50),
            ]
        )

        result = run_base_local_live_scan(
            self.store,
            observed_at="2026-01-01T00:00:00+00:00",
            hypersync=FakeHyperSyncRouterRecipientSwap(token, pair, wallet_a, wallet_b),
            market=FakeMarket(token, pair),
            rpc=FakeRpcWithoutPairTokens(),
        )

        self.assertEqual(result["pair_candidate_count"], 1)
        self.assertEqual(result["pair_token_count"], 1)
        self.assertEqual(result["dex_trade_count"], 1)
        self.assertEqual(result["dex_observation_count"], 1)
        self.assertEqual(result["fallback_observation_count"], 0)
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT pair_address, wallet_address, side, amount_usd, evidence_json
                FROM local_dex_trades
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()
            observation = connection.execute(
                """
                SELECT net_buy_usd
                FROM radar_observations
                WHERE source = ?
                """,
                (LOCAL_DEX_SOURCE,),
            ).fetchone()

        self.assertEqual(row["pair_address"], pair)
        self.assertEqual(row["wallet_address"], wallet_a)
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["amount_usd"], 900)
        self.assertIn("tracked_swap_recipient_output_delta", row["evidence_json"])
        self.assertIn('"swap_wallet_field": "recipient"', row["evidence_json"])
        self.assertEqual(observation["net_buy_usd"], 900)


class FakeHyperSync:
    def __init__(self, token: str, wallet_a: str, wallet_b: str) -> None:
        self.token = token
        self.wallet_a = wallet_a
        self.wallet_b = wallet_b

    def recent_wallet_transfer_events(self, **kwargs: Any) -> dict[str, Any]:
        observed_at = kwargs["observed_at"]
        return {
            "chain_id": "base",
            "observed_at": observed_at,
            "from_block": 100,
            "to_block": 200,
            "next_block": 200,
            "page_count": 1,
            "tracked_wallets": [self.wallet_a, self.wallet_b],
            "events": [
                transfer_event(self.token, "0x" + "9" * 40, self.wallet_a, 10, True, False),
                transfer_event(self.token, "0x" + "8" * 40, self.wallet_b, 5, True, False),
                transfer_event(self.token, self.wallet_a, "0x" + "7" * 40, 2, False, True),
            ],
        }


class FakeMarket:
    def __init__(
        self,
        token: str,
        pair: str | None = None,
        candidate_pairs: list[str] | None = None,
    ) -> None:
        self.token = token
        self.pair = pair
        self.candidate_pairs = candidate_pairs or ([pair] if pair else [])

    def token_market_snapshots(
        self,
        chain_id: str,
        token_addresses: list[str],
        observed_at: str | None = None,
        max_pairs_per_token: int = 5,
        minimum_pair_liquidity_usd: float = 1_000.0,
    ) -> list[dict[str, Any]]:
        del token_addresses
        del max_pairs_per_token
        del minimum_pair_liquidity_usd
        candidate_pairs = [
            {
                "rank": index,
                "pairAddress": pair,
                "dexId": "aerodrome",
                "priceUsd": "100",
                "liquidity_usd": 100_000 - index,
                "volume_24h_usd": 50_000,
                "baseToken": {"address": self.token, "symbol": "LOCAL"},
                "quoteToken": {"address": "0x" + "c" * 40, "symbol": "WETH"},
            }
            for index, pair in enumerate(self.candidate_pairs, start=1)
            if pair
        ]
        return [
            {
                "chain_id": chain_id,
                "token_address": self.token,
                "observed_at": observed_at,
                "source": "dexscreener",
                "token_symbol": "LOCAL",
                "pair_address": self.pair,
                "dex_id": "aerodrome",
                "price_usd": 100.0,
                "liquidity_usd": 100_000.0,
                "volume_24h_usd": 50_000.0,
                "social_links": [],
                "raw": {
                    "pairAddress": self.pair,
                    "dexId": "aerodrome",
                    "baseToken": {"address": self.token, "symbol": "LOCAL"},
                    "quoteToken": {"address": "0x" + "c" * 40, "symbol": "WETH"},
                    "candidatePairs": candidate_pairs,
                },
            }
        ]


class FakeHyperSyncWithSwap(FakeHyperSync):
    def __init__(self, token: str, pair: str, wallet_a: str, wallet_b: str) -> None:
        super().__init__(token, wallet_a, wallet_b)
        self.pair = pair

    def recent_pool_swap_events(self, **kwargs: Any) -> dict[str, Any]:
        observed_at = kwargs["observed_at"]
        return {
            "chain_id": "base",
            "observed_at": observed_at,
            "from_block": 100,
            "to_block": 200,
            "next_block": 200,
            "page_count": 1,
            "pair_addresses": [self.pair],
            "events": [
                {
                    "chain_id": "base",
                    "pair_address": self.pair,
                    "block_number": 123,
                    "block_time": "2026-01-01T00:30:00+00:00",
                    "tx_hash": "0xabc",
                    "log_index": 9,
                    "topic0": "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
                    "protocol_shape": "v2",
                    "tx_from": self.wallet_a,
                    "tx_to": "0x" + "e" * 40,
                    "sender": self.wallet_a,
                    "recipient": self.wallet_a,
                    "amount0_in_raw": "0",
                    "amount1_in_raw": "1300",
                    "amount0_out_raw": "13",
                    "amount1_out_raw": "0",
                }
            ],
        }


class FakeHyperSyncSignerSwap(FakeHyperSync):
    def __init__(self, token: str, pair: str, wallet_a: str, wallet_b: str) -> None:
        super().__init__(token, wallet_a, wallet_b)
        self.pair = pair

    def recent_pool_swap_events(self, **kwargs: Any) -> dict[str, Any]:
        observed_at = kwargs["observed_at"]
        return {
            "chain_id": "base",
            "observed_at": observed_at,
            "from_block": 100,
            "to_block": 200,
            "next_block": 200,
            "page_count": 1,
            "pair_addresses": kwargs["pair_addresses"],
            "events": [
                {
                    "chain_id": "base",
                    "pair_address": self.pair,
                    "block_number": 123,
                    "block_time": "2026-01-01T00:30:00+00:00",
                    "tx_hash": "0xsigner",
                    "log_index": 11,
                    "topic0": "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
                    "protocol_shape": "v2",
                    "tx_from": self.wallet_a,
                    "tx_to": "0x" + "e" * 40,
                    "sender": self.wallet_a,
                    "recipient": self.wallet_a,
                    "amount0_in_raw": "0",
                    "amount1_in_raw": "700",
                    "amount0_out_raw": "7",
                    "amount1_out_raw": "0",
                }
            ],
        }


class FakeHyperSyncRouterRecipientSwap(FakeHyperSync):
    def __init__(self, token: str, pair: str, wallet_a: str, wallet_b: str) -> None:
        super().__init__(token, wallet_a, wallet_b)
        self.pair = pair

    def recent_wallet_transfer_events(self, **kwargs: Any) -> dict[str, Any]:
        observed_at = kwargs["observed_at"]
        return {
            "chain_id": "base",
            "observed_at": observed_at,
            "from_block": 100,
            "to_block": 200,
            "next_block": 200,
            "page_count": 1,
            "tracked_wallets": [self.wallet_a, self.wallet_b],
            "events": [
                transfer_event(
                    self.token,
                    "0x" + "9" * 40,
                    self.wallet_a,
                    1,
                    True,
                    False,
                    tx_hash="0xseed",
                )
            ],
        }

    def recent_pool_swap_events(self, **kwargs: Any) -> dict[str, Any]:
        observed_at = kwargs["observed_at"]
        router = "0x" + "e" * 40
        return {
            "chain_id": "base",
            "observed_at": observed_at,
            "from_block": 100,
            "to_block": 200,
            "next_block": 200,
            "page_count": 1,
            "pair_addresses": kwargs["pair_addresses"],
            "events": [
                {
                    "chain_id": "base",
                    "pair_address": self.pair,
                    "block_number": 123,
                    "block_time": "2026-01-01T00:30:00+00:00",
                    "tx_hash": "0xrouter",
                    "log_index": 12,
                    "topic0": "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
                    "protocol_shape": "v2",
                    "tx_from": router,
                    "tx_to": router,
                    "sender": router,
                    "recipient": self.wallet_a,
                    "amount0_in_raw": "0",
                    "amount1_in_raw": "900",
                    "amount0_out_raw": "9",
                    "amount1_out_raw": "0",
                }
            ],
        }


class FakeRpc:
    def erc20_decimals(self, contract_address: str) -> int:
        del contract_address
        return 0

    def pair_tokens(self, pair_address: str) -> tuple[str, str]:
        del pair_address
        return "0x" + "a" * 40, "0x" + "c" * 40


class FakeRpcWithoutPairTokens(FakeRpc):
    def pair_tokens(self, pair_address: str) -> tuple[str, str]:
        del pair_address
        raise ValueError("pair tokens unavailable")


def transfer_event(
    token: str,
    from_address: str,
    to_address: str,
    amount_raw: int,
    to_tracked: bool,
    from_tracked: bool,
    tx_hash: str = "0xabc",
) -> dict[str, Any]:
    return {
        "chain_id": "base",
        "token_address": token,
        "block_number": 123,
        "block_time": "2026-01-01T00:30:00+00:00",
        "tx_hash": tx_hash,
        "log_index": amount_raw,
        "from_address": from_address,
        "to_address": to_address,
        "from_tracked": from_tracked,
        "to_tracked": to_tracked,
        "amount_raw": amount_raw,
    }


def wallet_score(
    wallet: str,
    label: str,
    interest_score: float,
    confidence_score: float,
) -> dict[str, Any]:
    return {
        "wallet_address": wallet,
        "chain_id": "base",
        "model_version": "wallet_score_v1_2",
        "interest_score": interest_score,
        "noise_score": 20,
        "confidence_score": confidence_score,
        "label": label,
        "target_count": 2,
        "symbol_count": 2,
        "total_buy_trades": 3,
        "total_gross_buy_usd": 10_000,
        "avg_trade_usd": 3_333,
        "earliest_buy_at": "2025-01-01T00:00:00+00:00",
        "latest_buy_at": "2025-02-01T00:00:00+00:00",
        "max_first_lead_days": 30,
        "min_last_lead_hours": 24,
        "active_span_days": 31,
        "flags": [],
        "evidence": {},
    }


if __name__ == "__main__":
    unittest.main()
