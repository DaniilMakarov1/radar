from __future__ import annotations

import copy
import unittest
import os
import http.client
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from smart_money_radar.funding.adapters.base import FundingDataError, FundingHttpClient
from smart_money_radar.funding.adapters.aevo import AevoFundingClient
from smart_money_radar.funding.adapters.apex import ApexFundingClient
from smart_money_radar.funding.adapters.pacifica import PacificaFundingClient
from smart_money_radar.funding.adapters.reya import ReyaFundingClient
from smart_money_radar.funding.adapters.risex import RiseXFundingClient
from smart_money_radar.funding.adapters.aster import AsterFundingClient
from smart_money_radar.funding.adapters.backpack import BackpackFundingClient
from smart_money_radar.funding.adapters.binance import BinanceFundingClient
from smart_money_radar.funding.adapters.bingx import BingXFundingClient
from smart_money_radar.funding.adapters.bitmart import BitMartFundingClient
from smart_money_radar.funding.adapters.bitunix import BitunixFundingClient
from smart_money_radar.funding.adapters.bitget import BitgetFundingClient
from smart_money_radar.funding.adapters.blofin import BloFinFundingClient
from smart_money_radar.funding.adapters.bybit import BybitFundingClient
from smart_money_radar.funding.adapters.coinex import CoinExFundingClient
from smart_money_radar.funding.adapters.deribit import DeribitFundingClient
from smart_money_radar.funding.adapters.dydx import DydxFundingClient
from smart_money_radar.funding.adapters.drift import DriftFundingClient
from smart_money_radar.funding.adapters.ethereal import EtherealFundingClient
from smart_money_radar.funding.adapters.extended import ExtendedFundingClient
from smart_money_radar.funding.adapters.edgex import EdgexFundingClient
from smart_money_radar.funding.adapters.grvt import GrvtFundingClient
from smart_money_radar.funding.adapters.gate import GateFundingClient
from smart_money_radar.funding.adapters.htx import HTXFundingClient
from smart_money_radar.funding.adapters.hyperliquid import (
    HyperliquidFundingClient,
    hyperliquid_next_funding_time,
    hyperliquid_predicted_fundings,
)
from smart_money_radar.funding.adapters.kucoin import KuCoinFundingClient
from smart_money_radar.funding.adapters.kraken import (
    KrakenFundingClient,
    kraken_relative_funding_rate,
)
from smart_money_radar.funding.adapters.lighter import LighterFundingClient
from smart_money_radar.funding.adapters.mexc import MEXCFundingClient
from smart_money_radar.funding.adapters.nado import (
    NadoFundingClient,
    nado_rate_x18_to_decimal,
)
from smart_money_radar.funding.adapters.okx import (
    OKXFundingClient,
    okx_funding_snapshot,
)
from smart_money_radar.funding.adapters.paradex import ParadexFundingClient
from smart_money_radar.funding.adapters.phemex import PhemexFundingClient
from smart_money_radar.funding.adapters.vertex import (
    VertexFundingClient,
    vertex_response_data,
)
from smart_money_radar.funding.adapters.variational import VariationalFundingClient
from smart_money_radar.funding.adapters.woox import WOOXFundingClient
from smart_money_radar.dashboard import (
    filter_deactivated_funding_dashboard_payload,
    filter_deactivated_funding_paper_payload,
    filter_deactivated_funding_paper_export_rows,
    funding_query_horizon,
    funding_request_config,
)
from smart_money_radar.funding.economics import (
    add_live_settlement_economics,
    build_strategy_evaluation,
    current_schedule_carry_rate,
    evaluate_perp_route,
    displayed_side_capacity,
    fill_notional,
    market_funding_rate_unit_outlier,
    market_fee_rate,
    notional_economics,
    select_live_sizing_row,
    wilson_lower_bound,
)
from smart_money_radar.funding.forecast import (
    build_funding_forecast,
    dynamic_nowcast_weight,
    forecast_component_weights,
    funding_nowcast_source_trusted,
    forward_samples,
    paired_settlement_schedule,
    regime_diagnostics,
    settlement_forward_samples,
    trailing_series,
)
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.liquidity import (
    analyze_depth_sequence,
    estimate_maker_fill,
)
from smart_money_radar.funding.normalization import (
    canonical_quote_asset_symbol,
    canonical_asset_unit_multiplier,
    funding_hourly_buckets,
    funding_persistence,
    inferred_funding_intervals,
    is_linear_contract_type,
    normalize_catalog_canonical_units,
    normalize_orderbook_canonical_units,
    normalized_contract_type,
)
from smart_money_radar.funding.paper import simulate_paper_revalidations
from smart_money_radar.funding.profiles import funding_bot_profile_names
from smart_money_radar.funding.scanner import (
    execution_shortlist,
    quick_price_identity_gap,
    quick_signed_spread_rate,
    rank_perp_pairs,
)
from smart_money_radar.funding.service import (
    DEACTIVATED_FUNDING_VENUES,
    active_default_funding_clients,
    backfill_funding_history,
    client_history_request_start_at,
    executable_history_market_keys,
    remove_non_crypto_funding_rows,
    run_funding_scan,
)
from smart_money_radar.storage import SQLiteStore, utc_now_iso

class FundingRadarTest(unittest.TestCase):
    def test_risky_venues_are_not_active_default_funding_clients(self) -> None:
        venues = {str(client.venue) for client in active_default_funding_clients()}

        self.assertFalse(venues & DEACTIVATED_FUNDING_VENUES)
        self.assertIn("risex", venues)

    def test_core_public_clients_expose_verified_endpoint_identity(self) -> None:
        cases = [
            (BinanceFundingClient(), "binance", "mainnet", "https://fapi.binance.com"),
            (BybitFundingClient(), "bybit", "mainnet", "https://api.bybit.com"),
            (OKXFundingClient(), "okx", "mainnet", "https://app.okx.com"),
            (
                PacificaFundingClient(),
                "pacifica",
                "mainnet",
                "https://api.pacifica.fi/api/v1",
            ),
            (
                NadoFundingClient(),
                "nado",
                "mainnet",
                "https://gateway.prod.nado.xyz/v2",
            ),
            (
                VariationalFundingClient(),
                "variational",
                "mainnet",
                "https://omni-client-api.prod.ap-northeast-1.variational.io",
            ),
            (
                RiseXFundingClient(),
                "risex",
                "testnet",
                "https://api.testnet.rise.trade",
            ),
        ]

        for client, venue, environment, base_url in cases:
            with self.subTest(venue=venue):
                identity = client.endpoint_identity
                self.assertEqual(identity.venue, venue)
                self.assertEqual(identity.environment, environment)
                self.assertEqual(identity.base_url, base_url)
                self.assertTrue(identity.environment_verified)

    def test_core_public_parsers_propagate_endpoint_identity_to_market_rows(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        cases = [
            (BinanceFundingClient(http=FakeBinanceHttp()), "binance", "mainnet"),
            (BybitFundingClient(http=FakeBybitHttp()), "bybit", "mainnet"),
            (
                OKXFundingClient(http=FakeOKXHttp(), use_websocket=False),
                "okx",
                "mainnet",
            ),
            (PacificaFundingClient(http=FakePacificaHttp()), "pacifica", "mainnet"),
            (NadoFundingClient(http=FakeNadoHttp()), "nado", "mainnet"),
            (RiseXFundingClient(http=FakeRiseXHttp()), "risex", "testnet"),
        ]

        for client, venue, environment in cases:
            with self.subTest(venue=venue):
                instruments, markets, _warnings = client.catalog_and_markets(observed_at)
                self.assertGreaterEqual(len(instruments), 1)
                self.assertGreaterEqual(len(markets), 1)
                for row in (instruments[0], markets[0]):
                    self.assertEqual(row["venue"], venue)
                    self.assertEqual(row["environment"], environment)
                    self.assertTrue(row["environment_verified"])
                    self.assertEqual(
                        row["endpoint_identity_provenance"],
                        "official_public_rest",
                    )

    def test_risex_points_profile_is_available_after_adapter_registration(self) -> None:
        self.assertIn("risex_points", funding_bot_profile_names())

    def test_deactivated_venues_are_hidden_from_funding_paper_dashboard(self) -> None:
        payload = filter_deactivated_funding_paper_payload(
            {
                "summary": {
                    "realized_pnl": 15,
                    "closed_trade_count": 2,
                    "win_rate": 1.0,
                },
                "accounts": [
                    {
                        "venue": "binance",
                        "starting_balance": 1_000,
                        "cash_balance": 1_010,
                        "reserved_margin": 0,
                        "realized_pnl": 10,
                    },
                    {
                        "venue": "bitunix",
                        "starting_balance": 1_000,
                        "cash_balance": 980,
                        "reserved_margin": 0,
                        "realized_pnl": -20,
                    },
                ],
                "open_positions": [],
                "closed_positions": [
                    {
                        "long_venue": "binance",
                        "short_venue": "okx",
                        "actual_net_pnl": 10,
                    },
                    {
                        "long_venue": "phemex",
                        "short_venue": "bitunix",
                        "actual_net_pnl": 5,
                    },
                ],
                "events": [
                    {"message": "LONG binance / SHORT okx"},
                    {"message": "LONG phemex / SHORT bitunix"},
                ],
                "trade_events": [{"message": "LONG phemex / SHORT bitunix"}],
                "system_events": [{"message": "healthy"}],
            }
        )

        self.assertEqual([row["venue"] for row in payload["accounts"]], ["binance"])
        self.assertEqual(payload["summary"]["realized_pnl"], 15)
        self.assertEqual(payload["summary"]["closed_trade_count"], 2)
        self.assertEqual(payload["closed_positions"][0]["short_venue"], "okx")
        self.assertEqual(payload["events"], [{"message": "LONG binance / SHORT okx"}])
        self.assertEqual(payload["trade_events"], [])

    def test_deactivated_venues_are_hidden_from_funding_dashboard(self) -> None:
        payload = filter_deactivated_funding_dashboard_payload(
            {
                "routes": [
                    {"status": "paper_candidate", "long_venue": "binance", "short_venue": "okx"},
                    {"status": "paper_candidate", "long_venue": "variational", "short_venue": "gate"},
                ],
                "watch_routes": [
                    {"status": "watch", "long_venue": "aster", "short_venue": "variational"}
                ],
                "maker_routes": [
                    {"status": "watch", "long_venue": "binance", "short_venue": "bybit"}
                ],
                "venues": [{"venue": "binance"}, {"venue": "variational"}],
                "constraint_diagnostics": {
                    "near_misses": [
                        {"long_venue": "variational", "short_venue": "okx"},
                        {"long_venue": "binance", "short_venue": "okx"},
                    ]
                },
                "universe_summary": {
                    "top_routes": [
                        {"long_venue": "variational", "short_venue": "okx"},
                        {"long_venue": "binance", "short_venue": "okx"},
                    ]
                },
            }
        )

        self.assertEqual(len(payload["routes"]), 1)
        self.assertEqual(payload["watch_routes"], [])
        self.assertEqual(payload["venues"], [{"venue": "binance"}])
        self.assertEqual(payload["visible_route_count"], 1)
        self.assertEqual(payload["internal_watch_route_count"], 0)
        self.assertEqual(
            payload["constraint_diagnostics"]["near_misses"],
            [{"long_venue": "binance", "short_venue": "okx"}],
        )
        self.assertEqual(
            payload["universe_summary"]["top_routes"],
            [{"long_venue": "binance", "short_venue": "okx"}],
        )

    def test_deactivated_venues_are_hidden_from_funding_paper_export(self) -> None:
        rows = filter_deactivated_funding_paper_export_rows(
            [
                {"Long": "binance", "Short": "okx", "Маршрут": "LONG binance / SHORT okx"},
                {
                    "Long": "phemex",
                    "Short": "bitunix",
                    "Маршрут": "LONG phemex / SHORT bitunix",
                },
            ]
        )

        self.assertEqual(rows, [{"Long": "binance", "Short": "okx", "Маршрут": "LONG binance / SHORT okx"}])

    def test_http_client_wraps_remote_disconnect_as_venue_error(self) -> None:
        client = FundingHttpClient(max_retries=0)
        with patch(
            "smart_money_radar.http.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaises(FundingDataError):
                client.get_json("https://example.invalid/depth")

    def test_history_fetch_requires_minimum_depth_on_all_four_sides(self) -> None:
        candidate = {
            "long_market": {"venue": "a", "symbol": "BTC-A"},
            "short_market": {"venue": "b", "symbol": "BTC-B"},
        }
        books = {
            ("a", "BTC-A"): {"bid_depth_usd": 500, "ask_depth_usd": 500},
            ("b", "BTC-B"): {"bid_depth_usd": 499, "ask_depth_usd": 10_000},
        }

        self.assertEqual(executable_history_market_keys([candidate], books, 500), set())
        books[("b", "BTC-B")]["bid_depth_usd"] = 500
        self.assertEqual(
            executable_history_market_keys([candidate], books, 500),
            {("a", "BTC-A"), ("b", "BTC-B")},
        )

    def test_non_crypto_asset_filter_removes_asset_across_venues(self) -> None:
        instruments = [
            {
                "venue": "paradex",
                "symbol": "MRVL-USD-PERP",
                "canonical_asset": "MRVL",
                "raw": {"tags": ["RWA"]},
            },
            {
                "venue": "lighter",
                "symbol": "MRVL",
                "canonical_asset": "MRVL",
                "raw": {"detail": {"strategy_index": 5}},
            },
            {
                "venue": "okx",
                "symbol": "BTC-USDT-SWAP",
                "canonical_asset": "BTC",
                "raw": {"instrument": {"instCategory": "1"}},
            },
        ]
        markets = [
            {
                "venue": "paradex",
                "symbol": "MRVL-USD-PERP",
                "canonical_asset": "MRVL",
                "raw": {"market": {"tags": ["RWA"]}},
            },
            {
                "venue": "htx",
                "symbol": "MRVL-USDT",
                "canonical_asset": "MRVL",
                "raw": {"contract": {}},
            },
            {
                "venue": "okx",
                "symbol": "BTC-USDT-SWAP",
                "canonical_asset": "BTC",
                "raw": {"instrument": {"instCategory": "1"}},
            },
        ]

        filtered_instruments, filtered_markets, removed = remove_non_crypto_funding_rows(
            instruments,
            markets,
            explicit_assets={"AMAT"},
        )

        self.assertEqual(removed, {"AMAT", "MRVL"})
        self.assertEqual(
            {row["canonical_asset"] for row in filtered_instruments},
            {"BTC"},
        )
        self.assertEqual(
            {row["canonical_asset"] for row in filtered_markets},
            {"BTC"},
        )

    def test_incremental_history_sync_records_actual_requested_window(self) -> None:
        client = type("IncrementalClient", (), {"incremental_history_lookback_hours": 8})()
        observed = datetime(2026, 7, 14, 12, tzinfo=UTC)

        effective = client_history_request_start_at(
            client,
            observed - timedelta(days=90),
            observed,
        )

        self.assertEqual(effective, "2026-07-14T04:00:00+00:00")

    def test_auto_refresh_reuses_server_config_instead_of_stale_tab_values(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {
                    "target_notional": 10_000,
                    "horizon_mode": "next_settlement",
                    "horizon_hours": 0,
                }
            )
            store.finish_funding_scan(scan_id, "success")

            config = funding_request_config(
                store,
                {
                    "target_notional": 500,
                    "horizon_mode": "fixed",
                    "horizon_hours": 24,
                },
                "auto",
            )

        self.assertEqual(config.target_notional, 10_000)
        self.assertEqual(config.horizon_mode, "next_settlement")
        self.assertEqual(config.horizon_hours, 0)

    def test_auto_refresh_does_not_inherit_saved_orderbook_market_cap(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {
                    "target_notional": 10_000,
                    "horizon_mode": "next_settlement",
                    "horizon_hours": 0,
                    "max_full_depth_orderbook_markets": 300,
                    "orderbook_cache_ttl_seconds": 30,
                    "market_snapshot_cache_ttl_seconds": 30,
                }
            )
            store.finish_funding_scan(scan_id, "success")

            config = funding_request_config(store, {}, "auto")

        self.assertIsNone(config.max_full_depth_orderbook_markets)
        self.assertEqual(config.near_miss_full_depth_routes, 0)
        self.assertEqual(config.orderbook_cache_ttl_seconds, 0)
        self.assertEqual(config.market_snapshot_cache_ttl_seconds, 0)

    def test_manual_refresh_can_change_server_config(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            config = funding_request_config(
                store,
                {
                    "target_notional": 500,
                    "horizon_mode": "fixed",
                    "horizon_hours": 8,
                },
                "manual",
            )

        self.assertEqual(config.target_notional, 500)
        self.assertEqual(config.horizon_mode, "fixed")
        self.assertEqual(config.horizon_hours, 8)
        self.assertEqual(config.near_miss_full_depth_routes, 0)
        self.assertEqual(config.orderbook_cache_ttl_seconds, 0)
        self.assertEqual(config.market_snapshot_cache_ttl_seconds, 0)

    def test_hyperliquid_predicted_funding_selects_hl_perp(self) -> None:
        parsed = hyperliquid_predicted_fundings(
            [
                [
                    "BTC",
                    [
                        ["BinPerp", {"fundingRate": "0.001"}],
                        [
                            "HlPerp",
                            {
                                "fundingRate": "0.0002",
                                "nextFundingTime": 1_784_052_000_000,
                                "fundingIntervalHours": 1,
                            },
                        ],
                    ],
                ]
            ]
        )

        self.assertEqual(parsed["BTC"]["fundingRate"], "0.0002")

    def test_hyperliquid_advances_just_settled_prediction(self) -> None:
        settled = datetime(2026, 7, 15, 18, tzinfo=UTC)

        next_funding = hyperliquid_next_funding_time(
            int(settled.timestamp() * 1_000),
            "2026-07-15T18:06:39+00:00",
            1,
        )

        self.assertEqual(next_funding, "2026-07-15T19:00:00+00:00")

    def test_persistence_aligns_different_funding_intervals(self) -> None:
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        long_rows = history_rows("binance", "BTCUSDT", now, 8, 0.0, 8)
        short_rows = history_rows("hyperliquid", "BTC", now, 1, 0.0001, 48)

        result = funding_persistence(long_rows, short_rows)

        self.assertGreaterEqual(result["history_point_count"], 40)
        self.assertEqual(result["positive_spread_fraction"], 1.0)
        self.assertAlmostEqual(result["historical_median_hourly_spread"], 0.0001)

    def test_settled_history_is_applied_to_preceding_interval_only(self) -> None:
        settled_at = datetime(2026, 7, 14, 8, tzinfo=UTC)
        rows = history_rows("binance", "BTCUSDT", settled_at, 8, 0.0001, 1)

        buckets = funding_hourly_buckets(rows)

        self.assertIn(datetime(2026, 7, 14, 0, tzinfo=UTC), buckets)
        self.assertIn(datetime(2026, 7, 14, 7, tzinfo=UTC), buckets)
        self.assertNotIn(datetime(2026, 7, 14, 8, tzinfo=UTC), buckets)

    def test_scaled_contract_aliases_use_canonical_price_and_quantity(self) -> None:
        instruments, markets = normalize_catalog_canonical_units(
            [
                {
                    "venue": "test",
                    "symbol": "1000PEPEUSDT",
                    "base_asset": "1000PEPE",
                    "canonical_asset": "PEPE",
                }
            ],
            [
                {
                    "venue": "test",
                    "symbol": "1000PEPEUSDT",
                    "canonical_asset": "PEPE",
                    "mark_price": 0.012,
                    "index_price": 0.011,
                }
            ],
        )
        normalized_book = normalize_orderbook_canonical_units(
            {
                "bids": [[0.012, 10]],
                "asks": [[0.013, 20]],
                "mid_price": 0.0125,
            },
            canonical_asset_unit_multiplier("1000PEPE"),
        )

        self.assertEqual(instruments[0]["canonical_unit_multiplier"], 1_000)
        self.assertAlmostEqual(markets[0]["mark_price"], 0.000012)
        self.assertAlmostEqual(normalized_book["bids"][0][0], 0.000012)
        self.assertEqual(normalized_book["bids"][0][1], 10_000)
        self.assertAlmostEqual(normalized_book["bid_depth_usd"], 0.12)

    def test_scaled_contract_alias_can_be_inferred_from_symbol(self) -> None:
        instruments, markets = normalize_catalog_canonical_units(
            [
                {
                    "venue": "bingx",
                    "symbol": "1000BONK-USDT",
                    "base_asset": "BONK",
                    "canonical_asset": "BONK",
                },
                {
                    "venue": "kucoin",
                    "symbol": "1000BONKUSDTM",
                    "base_asset": "BONK",
                    "canonical_asset": "BONK",
                },
                {
                    "venue": "hyperliquid",
                    "symbol": "kBONK",
                    "base_asset": "BONK",
                    "canonical_asset": "BONK",
                },
            ],
            [
                {
                    "venue": "bingx",
                    "symbol": "1000BONK-USDT",
                    "canonical_asset": "BONK",
                    "mark_price": 0.003456,
                    "index_price": 0.003484,
                },
                {
                    "venue": "kucoin",
                    "symbol": "1000BONKUSDTM",
                    "canonical_asset": "BONK",
                    "mark_price": 0.0034601,
                    "index_price": 0.0034848,
                },
                {
                    "venue": "hyperliquid",
                    "symbol": "kBONK",
                    "canonical_asset": "BONK",
                    "mark_price": 0.003465,
                    "index_price": 0.003493,
                },
            ],
        )

        self.assertEqual(
            [row["canonical_unit_multiplier"] for row in instruments],
            [1_000, 1_000, 1_000],
        )
        self.assertTrue(
            all(row["canonical_asset"] == "BONK" for row in instruments)
        )
        self.assertAlmostEqual(markets[0]["mark_price"], 0.000003456)
        self.assertAlmostEqual(markets[1]["mark_price"], 0.0000034601)
        self.assertAlmostEqual(markets[2]["mark_price"], 0.000003465)

    def test_extended_scaled_contract_aliases_are_whitelisted(self) -> None:
        self.assertEqual(canonical_asset_unit_multiplier("MPEPE"), 1_000_000)
        self.assertEqual(canonical_asset_unit_multiplier("1000000BONK"), 1_000_000)
        self.assertEqual(canonical_asset_unit_multiplier("10000SHIB"), 10_000)
        self.assertEqual(canonical_asset_unit_multiplier("MANTA"), 1)
        self.assertEqual(canonical_asset_unit_multiplier("MOVE"), 1)

    def test_quote_and_contract_type_normalization_helpers(self) -> None:
        self.assertEqual(canonical_quote_asset_symbol("USDT"), "USD")
        self.assertEqual(canonical_quote_asset_symbol("USDC"), "USD")
        self.assertEqual(canonical_quote_asset_symbol("FDUSD"), "USD")
        self.assertEqual(normalized_contract_type("inverse_perpetual"), "inverse_perpetual")
        self.assertEqual(normalized_contract_type("linear"), "linear_perpetual")
        self.assertTrue(is_linear_contract_type("USDTM"))
        self.assertFalse(is_linear_contract_type("inverse"))

    def test_route_uses_round_trip_costs_and_total_collateral(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        config = FundingScanConfig(
            target_notional=10_000,
            horizon_mode="fixed",
            horizon_hours=24,
            minimum_history_points=12,
            minimum_persistence=0.5,
        )
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market(
            "hyperliquid", "BTC", "BTC", 0.0005, 1, observed_at
        )

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0005, 720),
            observed_at,
            config,
        )

        self.assertEqual(route["status"], "paper_candidate")
        self.assertEqual(route["long_venue"], "binance")
        self.assertEqual(route["short_venue"], "hyperliquid")
        self.assertAlmostEqual(route["total_fees"], 20.0)
        self.assertAlmostEqual(route["capital_required"], 22_000.2)
        self.assertAlmostEqual(
            route["legs"][0]["base_quantity"],
            route["legs"][1]["base_quantity"],
        )
        self.assertGreater(route["expected_net_profit"], 0)

    def test_route_evidence_contains_strategy_candidates(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        next_funding_at = (now + timedelta(seconds=45)).isoformat()
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market(
            "hyperliquid", "BTC", "BTC", 0.01, 1, observed_at
        )
        long_market["next_funding_at"] = next_funding_at
        short_market["next_funding_at"] = next_funding_at

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.01, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        evidence = route["evidence"]
        names = {row["strategy_name"] for row in evidence["strategy_candidates"]}

        self.assertEqual(len(evidence["strategy_candidates"]), 1)
        self.assertIn(evidence["selected_strategy"]["strategy_name"], names)
        self.assertEqual(
            evidence["strategy_classification"],
            evidence["selected_strategy"],
        )
        self.assertEqual(
            evidence["selected_strategy"]["selection_model"],
            "synchronized_funding_capture_v2",
        )
        self.assertIn(
            evidence["selected_strategy"]["edge_type"],
            {"funding_led", "spread_led", "mixed_edge", "no_positive_edge"},
        )
        self.assertIn("funding_pnl_component", evidence["pnl_components"])
        self.assertIn("spread_pnl_component", evidence["pnl_components"])
        self.assertIn("opportunity_expected_net_pnl", evidence["pnl_components"])

    def test_settlement_capture_strategy_zeros_spread_edge(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 0.5,
                "basis_stress_loss": 0.75,
                "basis_model": {"signed_entry_basis": 0.01},
            },
            current_funding_gross=-0.25,
            current_funding_net=-0.75,
            current_basis_stress_net=-1.50,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        candidate = result["strategy_candidates"][0]

        self.assertIsNone(result["selected_strategy"])
        self.assertFalse(candidate["eligible"])
        self.assertEqual(candidate["edge_type"], "no_positive_edge")
        self.assertAlmostEqual(candidate["spread_pnl_component"], 0.0)
        self.assertAlmostEqual(candidate["expected_net_pnl"], -0.75)
        self.assertAlmostEqual(candidate["basis_stress_net_pnl"], -1.50)

    def test_funding_led_can_be_fragile_when_basis_stress_not_covered(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 0.25,
                "basis_stress_loss": 2.0,
                "basis_model": {"signed_entry_basis": 0.0},
            },
            current_funding_gross=1.50,
            current_funding_net=1.25,
            current_basis_stress_net=-0.75,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        selected = result["selected_strategy"]

        self.assertTrue(selected["eligible"])
        self.assertEqual(
            selected["strategy_name"],
            "funding_only",
        )
        self.assertEqual(selected["edge_type"], "funding_led")
        self.assertEqual(selected["edge_quality"], "fragile")
        self.assertAlmostEqual(selected["expected_net_pnl"], 1.25)
        self.assertAlmostEqual(selected["risk_adjusted_net_pnl"], -0.75)
        self.assertIn(
            "basis_stress_not_covered",
            selected["warnings"],
        )
        self.assertIn(
            "fragile_positive_total_edge",
            selected["warnings"],
        )
        self.assertAlmostEqual(
            result["pnl_components"]["opportunistic_any_net_pnl"],
            1.25,
        )
        self.assertAlmostEqual(
            result["pnl_components"]["opportunistic_risk_adjusted_net_pnl"],
            -0.75,
        )

    def test_clean_funding_led_opportunity_is_selected_directly(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 0.25,
                "basis_stress_loss": 0.25,
                "basis_model": {"signed_entry_basis": 0.0},
            },
            current_funding_gross=2.0,
            current_funding_net=1.75,
            current_basis_stress_net=1.50,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        selected = result["selected_strategy"]

        self.assertTrue(selected["eligible"])
        self.assertEqual(selected["strategy_name"], "funding_only")
        self.assertEqual(selected["edge_type"], "funding_led")
        self.assertEqual(selected["edge_quality"], "clean")

    def test_funding_led_subtracts_negative_spread_but_can_cover_it(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 1.0,
                "basis_stress_loss": 2.0,
                "basis_model": {"signed_entry_basis": -0.004},
            },
            current_funding_gross=6.0,
            current_funding_net=5.0,
            current_basis_stress_net=3.0,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        selected = result["selected_strategy"]

        self.assertTrue(selected["eligible"])
        self.assertEqual(selected["strategy_name"], "funding_only")
        self.assertEqual(selected["edge_type"], "funding_led")
        self.assertAlmostEqual(selected["funding_pnl_component"], 6.0)
        self.assertAlmostEqual(selected["spread_pnl_component"], 0.0)
        self.assertAlmostEqual(selected["expected_net_pnl"], 5.0)
        self.assertAlmostEqual(selected["risk_adjusted_net_pnl"], 3.0)

    def test_spread_led_cannot_make_settlement_capture_eligible(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 1.0,
                "basis_stress_loss": 0.5,
                "basis_model": {"signed_entry_basis": 0.012},
            },
            current_funding_gross=-1.0,
            current_funding_net=-2.0,
            current_basis_stress_net=-2.5,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        candidate = result["strategy_candidates"][0]

        self.assertIsNone(result["selected_strategy"])
        self.assertFalse(candidate["eligible"])
        self.assertEqual(candidate["edge_type"], "no_positive_edge")
        self.assertAlmostEqual(candidate["funding_pnl_component"], -1.0)
        self.assertAlmostEqual(candidate["spread_pnl_component"], 0.0)
        self.assertAlmostEqual(candidate["expected_net_pnl"], -2.0)
        self.assertIn("opportunity_net_below_required_profit", candidate["reasons"])

    def test_positive_spread_opportunity_below_funding_cost_is_blocked(self) -> None:
        result = build_strategy_evaluation(
            {
                "funding_notional": 500.0,
                "execution_cost": 5.0,
                "basis_stress_loss": 0.0,
                "basis_model": {"signed_entry_basis": 0.006},
            },
            current_funding_gross=3.0,
            current_funding_net=-2.0,
            current_basis_stress_net=-2.0,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )

        candidate = result["strategy_candidates"][0]

        self.assertIsNone(result["selected_strategy"])
        self.assertFalse(candidate["eligible"])
        self.assertAlmostEqual(candidate["funding_pnl_component"], 3.0)
        self.assertAlmostEqual(candidate["spread_pnl_component"], 0.0)
        self.assertAlmostEqual(candidate["expected_net_pnl"], -2.0)
        self.assertIn("opportunity_net_below_required_profit", candidate["reasons"])

    def test_live_sizing_uses_funding_only_net_not_full_opportunity_net(self) -> None:
        small = add_live_settlement_economics(
            {
                "notional": 500.0,
                "funding_notional": 500.0,
                "execution_cost": 1.0,
                "basis_stress_loss": 0.0,
                "basis_model": {"signed_entry_basis": 0.0, "tier": "standard"},
                "actionable_profit_threshold": 1.0,
                "fill_complete": True,
            },
            current_settlement_rate=0.006,
        )
        spread_led = add_live_settlement_economics(
            {
                "notional": 1_000.0,
                "funding_notional": 1_000.0,
                "execution_cost": 1.0,
                "basis_stress_loss": 0.0,
                "basis_model": {"signed_entry_basis": 0.01, "tier": "standard"},
                "actionable_profit_threshold": 1.0,
                "fill_complete": True,
            },
            current_settlement_rate=-0.002,
        )

        selected = select_live_sizing_row([small, spread_led], [small, spread_led])

        self.assertEqual(selected["notional"], 500.0)
        self.assertGreater(
            selected["current_nowcast_net"],
            spread_led["current_nowcast_net"],
        )
        self.assertLess(
            selected["current_opportunity_net"],
            spread_led["current_opportunity_net"],
        )

    def test_dashboard_candidate_filter_uses_selected_strategy_net(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        next_funding_at = (now + timedelta(seconds=45)).isoformat()
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market(
            "hyperliquid", "BTC", "BTC", 0.01, 1, observed_at
        )
        long_market["next_funding_at"] = next_funding_at
        short_market["next_funding_at"] = next_funding_at
        route = evaluate_perp_route(
            long_market,
            short_market,
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.01, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        route["expected_net_profit"] = -1.0
        route["evidence"]["current_nowcast_net"] = 0.50
        route["evidence"]["selected_strategy"]["expected_net_pnl"] = 1.25
        route["evidence"]["strategy_classification"] = route["evidence"][
            "selected_strategy"
        ]

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {
                    "horizon_mode": "next_settlement",
                    "scan_mode": "watch",
                    "minimum_net_profit": 1.0,
                }
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(
                scan_id,
                "success",
                route_count=1,
                paper_candidate_count=1,
            )
            dashboard = store.funding_dashboard(include_watch_scans=True)

        self.assertEqual(len(dashboard["routes"]), 1)
        self.assertEqual(
            dashboard["routes"][0]["evidence"]["selected_strategy"][
                "expected_net_pnl"
            ],
            1.25,
        )

    def test_tiny_five_hundred_dollar_profit_is_candidate_with_warning(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0003, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0003, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="fixed",
                horizon_hours=24,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["status"], "paper_candidate")
        self.assertEqual(route["target_notional"], 500)
        self.assertGreater(route["expected_net_profit"], 0)
        self.assertLess(
            route["evidence"]["conservative_net_profit"],
            route["evidence"]["actionable_profit_threshold"],
        )
        self.assertIn("actionable_profit_too_low", route["risk_flags"])
        self.assertIn(
            "actionable_profit_too_low",
            route["evidence"]["advisory_risk_flags"],
        )

    def test_meaningful_five_hundred_dollar_profit_can_be_a_candidate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0011, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0011, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="fixed",
                horizon_hours=24,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["status"], "paper_candidate")
        self.assertGreaterEqual(
            route["evidence"]["conservative_net_profit"],
            route["evidence"]["actionable_profit_threshold"],
        )

    def test_sizing_chooses_profitable_depth_before_expensive_levels(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_book = tiered_book("binance", "BTCUSDT", observed_at)
        short_book = tiered_book("hyperliquid", "BTC", observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0007, 1, observed_at),
            long_book,
            short_book,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0007, 720),
            observed_at,
            FundingScanConfig(
                target_notional=10_000,
                horizon_mode="fixed",
                horizon_hours=24,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["status"], "paper_candidate")
        self.assertGreaterEqual(route["target_notional"], 500)
        self.assertLess(route["target_notional"], route["market_capacity"])
        self.assertGreater(len(route["evidence"]["sizing_trials"]), 2)

    def test_dashboard_omits_routes_below_five_hundred_dollars_capacity(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        thin_long = book("binance", "BTCUSDT", observed_at)
        thin_short = book("hyperliquid", "BTC", observed_at)
        for orderbook in (thin_long, thin_short):
            orderbook["bids"] = [[99.99, 0.5]]
            orderbook["asks"] = [[100.01, 0.5]]
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at),
            thin_long,
            thin_short,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 24),
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 192),
            observed_at,
            FundingScanConfig(minimum_history_points=12),
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertLess(route["market_capacity"], 500)
        self.assertEqual(dashboard["routes"], [])
        self.assertEqual(dashboard["watch_routes"], [])
        self.assertEqual(dashboard["route_counts"], [])
        self.assertEqual(dashboard["minimum_visible_capacity"], 500)

    def test_dashboard_reports_raw_instrument_collapse(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        instruments = [
            {
                "venue": "binance",
                "symbol": "PEPEUSDT",
                "canonical_asset": "PEPE",
                "base_asset": "PEPE",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "contract_multiplier": 1,
                "status": "active",
                "observed_at": observed_at,
            },
            {
                "venue": "binance",
                "symbol": "1000PEPEUSDT",
                "canonical_asset": "PEPE",
                "base_asset": "1000PEPE",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "contract_multiplier": 1000,
                "status": "active",
                "observed_at": observed_at,
            },
            {
                "venue": "bybit",
                "symbol": "PEPEUSDT",
                "canonical_asset": "PEPE",
                "base_asset": "PEPE",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "contract_multiplier": 1,
                "status": "active",
                "observed_at": observed_at,
            },
            {
                "venue": "okx",
                "symbol": "BTC-USDT-SWAP",
                "canonical_asset": "BTC",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "contract_multiplier": 1,
                "status": "active",
                "observed_at": observed_at,
            },
        ]
        market_rows = [
            {
                **row,
                "funding_rate": 0.0001,
                "funding_interval_hours": 8,
                "hourly_funding_rate": 0.0000125,
                "funding_rate_kind": "predicted",
                "next_funding_at": observed_at,
                "mark_price": 1.0,
                "index_price": 1.0,
                "open_interest_usd": 1_000_000,
                "volume_24h_usd": 5_000_000,
            }
            for row in instruments
        ]
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "next_settlement",
                }
            )
            store.upsert_funding_instruments(instruments)
            store.insert_funding_market_snapshots(scan_id, market_rows)
            store.insert_funding_route_universe(
                scan_id,
                [
                    {
                        "canonical_asset": "PEPE",
                        "long_venue": "binance",
                        "long_symbol": "PEPEUSDT",
                        "short_venue": "bybit",
                        "short_symbol": "PEPEUSDT",
                        "current_hourly_spread": 0.00001,
                        "quick_gross_rate": 0.00008,
                        "quick_taker_cost_rate": 0.0002,
                        "quick_maker_cost_rate": 0.0001,
                        "quick_best_case_net_rate": -0.00002,
                        "quick_schedule_ready": True,
                        "execution_eligible": False,
                        "execution_screen_reason": "best_case_carry_below_unavoidable_cost",
                        "observed_at": observed_at,
                    }
                ],
            )
            store.finish_funding_scan(
                scan_id,
                "success",
                instrument_count=len(instruments),
                market_snapshot_count=len(market_rows),
                route_count=1,
            )
            dashboard = store.funding_dashboard("next_settlement")

        audit = dashboard["instrument_collapse_audit"]
        self.assertEqual(audit["raw_market_count"], 4)
        self.assertEqual(audit["canonical_venue_asset_count"], 3)
        self.assertEqual(audit["canonical_asset_count"], 2)
        self.assertEqual(audit["theoretical_route_count"], 1)
        self.assertEqual(audit["collapsed_market_count"], 1)
        self.assertEqual(audit["duplicate_group_count"], 1)
        self.assertEqual(
            audit["top_assets_by_route_count"][0]["canonical_asset"],
            "PEPE",
        )
        self.assertEqual(audit["top_assets_by_route_count"][0]["venue_count"], 2)
        self.assertEqual(audit["top_assets_by_route_count"][0]["route_count"], 1)
        duplicate = audit["duplicate_groups"][0]
        self.assertEqual(duplicate["canonical_asset"], "PEPE")
        self.assertEqual(duplicate["venue"], "binance")
        self.assertEqual(duplicate["instrument_count"], 2)
        self.assertEqual(duplicate["selected_symbols"], ["PEPEUSDT"])
        self.assertEqual(
            duplicate["symbols"],
            ["1000PEPEUSDT", "PEPEUSDT"],
        )

    def test_dashboard_hides_routes_when_median_and_q25_are_negative(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 720),
            observed_at,
            FundingScanConfig(minimum_history_points=12),
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertEqual(route["status"], "watch")
        self.assertGreaterEqual(route["market_capacity"], 500)
        self.assertEqual(dashboard["routes"], [])
        self.assertEqual(dashboard["watch_routes"], [])
        self.assertEqual(dashboard["visible_route_count"], 0)
        self.assertEqual(dashboard["capacity_eligible_route_count"], 1)
        self.assertEqual(dashboard["economics_funnel"]["capacity_eligible"], 1)
        self.assertEqual(
            dashboard["economics_funnel"]["current_raw_gross_covers_full_cost"],
            0,
        )
        self.assertEqual(dashboard["economics_funnel"]["current_raw_actionable"], 0)
        self.assertEqual(
            dashboard["economics_funnel"]["gross_covers_full_cost"],
            0,
        )
        self.assertEqual(dashboard["economics_funnel"]["median_net_positive"], 0)
        self.assertEqual(dashboard["economics_funnel"]["q25_net_positive"], 0)
        self.assertEqual(dashboard["economics_funnel"]["q25_actionable"], 0)
        diagnostics = dashboard["constraint_diagnostics"]
        self.assertEqual(
            diagnostics["primary_constraint"]["stage"],
            "current_spread_below_cost",
        )
        self.assertEqual(
            sum(row["route_count"] for row in diagnostics["exclusive_stages"]),
            1,
        )
        self.assertGreater(diagnostics["median_components"]["break_even_gap"], 0)
        self.assertEqual(diagnostics["execution_quality"]["route_count"], 1)
        self.assertEqual(
            diagnostics["execution_quality"]["median_minimum_snapshots"],
            1,
        )

    def test_research_scan_does_not_replace_operational_dashboard_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            operational_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.finish_funding_scan(operational_id, "success")
            research_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "research"}
            )
            store.finish_funding_scan(research_id, "success")
            watch_id = store.start_funding_scan(
                {"scan_mode": "watch", "horizon_mode": "next_settlement"}
            )
            store.finish_funding_scan(watch_id, "success")

            dashboard = store.funding_dashboard()

        self.assertEqual(
            dashboard["latest_scan"]["funding_scan_id"],
            operational_id,
        )
        self.assertEqual(
            dashboard["latest_research_scan"]["funding_scan_id"],
            research_id,
        )

    def test_focused_watch_scan_does_not_replace_dashboard_universe_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            full_watch_id = store.start_funding_scan(
                {"scan_mode": "watch", "horizon_mode": "next_settlement"}
            )
            store.finish_funding_scan(full_watch_id, "success")
            focused_watch_id = store.start_funding_scan(
                {
                    "scan_mode": "watch",
                    "horizon_mode": "next_settlement",
                    "focused_route_key": "route-one",
                    "focused_route_mode": "direct_symbol_recheck_v1",
                }
            )
            store.finish_funding_scan(focused_watch_id, "success")

            dashboard = store.funding_dashboard(
                horizon_mode="next_settlement",
                include_watch_scans=True,
            )

        self.assertEqual(
            dashboard["latest_scan"]["funding_scan_id"],
            full_watch_id,
        )

    def test_latest_funding_scan_config_ignores_research_snapshots(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            operational_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "next_settlement",
                    "target_notional": 500,
                }
            )
            store.finish_funding_scan(operational_id, "success")
            research_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "research",
                    "target_notional": 50_000,
                }
            )
            store.finish_funding_scan(research_id, "success")
            watch_id = store.start_funding_scan(
                {
                    "scan_mode": "watch",
                    "horizon_mode": "next_settlement",
                    "target_notional": 900,
                }
            )
            store.finish_funding_scan(watch_id, "success")

            config = store.latest_funding_scan_config()

        self.assertEqual(config["horizon_mode"], "next_settlement")
        self.assertEqual(config["target_notional"], 500)

    def test_funding_dashboard_can_select_fixed_horizon_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            next_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "next_settlement",
                    "horizon_hours": 0,
                    "target_notional": 500,
                }
            )
            store.finish_funding_scan(next_id, "success")
            four_hour_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "fixed",
                    "horizon_hours": 4,
                    "target_notional": 500,
                }
            )
            store.finish_funding_scan(four_hour_id, "success")
            next_dashboard = store.funding_dashboard(
                horizon_mode="next_settlement"
            )
            fixed_dashboard = store.funding_dashboard(
                horizon_mode="fixed",
                horizon_hours=4,
            )

        self.assertEqual(next_dashboard["latest_scan"]["funding_scan_id"], next_id)
        self.assertEqual(
            fixed_dashboard["latest_scan"]["funding_scan_id"],
            four_hour_id,
        )
        self.assertEqual(
            fixed_dashboard["latest_scan"]["config"]["horizon_hours"],
            4,
        )

    def test_funding_query_horizon_defaults_to_next_and_accepts_fixed_hours(self) -> None:
        self.assertEqual(funding_query_horizon({}), ("next_settlement", None))
        self.assertEqual(
            funding_query_horizon(
                {"horizon_mode": ["fixed"], "horizon_hours": ["8"]}
            ),
            ("fixed", 8.0),
        )
        self.assertEqual(
            funding_query_horizon(
                {"horizon_mode": ["fixed"], "horizon_hours": ["999"]}
            ),
            ("fixed", 24.0),
        )

    def test_dashboard_candidates_include_actionable_live_nowcast_net(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.006, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertEqual(route["status"], "watch")
        self.assertGreaterEqual(route["evidence"]["current_nowcast_net"], 1.0)
        self.assertEqual(len(dashboard["routes"]), 1)
        self.assertEqual(dashboard["watch_routes"], [])

    def test_dashboard_blockers_exclude_candidate_warnings(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        candidate = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.006, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        candidate["evidence"]["blocking_risk_flags"] = [
            "basis_not_covered_by_live_funding"
        ]
        candidate["risk_flags"] = ["basis_not_covered_by_live_funding"]
        blocked = copy.deepcopy(candidate)
        blocked["route_key"] = "blocked-live-net"
        blocked["status"] = "watch"
        blocked["evidence"]["blocking_risk_flags"] = ["live_net_pnl_not_positive"]
        blocked["risk_flags"] = ["live_net_pnl_not_positive"]
        blocked["evidence"]["current_nowcast_gross"] = 0.25
        blocked["evidence"]["current_nowcast_net"] = -1.0

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [candidate, blocked])
            store.finish_funding_scan(
                scan_id,
                "success",
                route_count=2,
                paper_candidate_count=1,
            )
            dashboard = store.funding_dashboard()

        blockers = {
            row["risk_flag"]: row["route_count"]
            for row in dashboard["blocker_summary"]
        }
        self.assertNotIn("basis_not_covered_by_live_funding", blockers)
        self.assertEqual(blockers["live_net_pnl_not_positive"], 1)
        self.assertIn(
            {"stage": "paper_candidate", "route_count": 1},
            dashboard["constraint_diagnostics"]["exclusive_stages"],
        )

    def test_dashboard_live_hold_columns_ignore_stale_fixed_scan(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.006, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        four_hour = copy.deepcopy(route)
        four_hour["status"] = "watch"
        four_hour["expected_net_profit"] = -2.50
        four_hour["evidence"]["decision_mode"] = "persistent_carry"
        four_hour["evidence"]["current_nowcast_net"] = 1.25
        four_hour["evidence"]["conservative_net_profit"] = -3.25
        four_hour["evidence"]["forecast"]["schedule_nowcast_rate"] = 0.01
        four_hour["evidence"]["funding_notional"] = 500
        four_hour["evidence"]["execution_cost"] = 2
        four_hour["evidence"]["horizon"]["horizon_mode"] = "fixed"
        four_hour["evidence"]["horizon"]["horizon_hours"] = 4.0
        four_hour["evidence"]["horizon"]["horizon_label"] = "4 часа"

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            next_scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(next_scan_id, [route])
            store.finish_funding_scan(next_scan_id, "success", route_count=1)
            fixed_scan_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "fixed",
                    "horizon_hours": 4.0,
                }
            )
            store.insert_funding_routes(fixed_scan_id, [four_hour])
            store.finish_funding_scan(fixed_scan_id, "success", route_count=1)

            dashboard = store.funding_dashboard("next_settlement")

        route_key = dashboard["routes"][0]["route_key"]
        comparison = dashboard["horizon_comparisons"]["4h"]["routes"][route_key]
        self.assertEqual(comparison["status"], route["status"])
        self.assertIsNone(comparison["expected_net_profit"])
        self.assertEqual(
            comparison["evidence"]["live_projection_source"],
            "current_route_snapshot",
        )
        self.assertEqual(
            comparison["evidence"]["live_projection_unavailable_reason"],
            "no_paired_settlement_in_horizon",
        )
        self.assertIsNone(comparison["evidence"]["live_if_persists_net"])

    def test_dashboard_eight_hour_hold_matches_next_for_eight_hour_legs(self) -> None:
        observed_at = "2026-07-14T08:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("bybit", "BTCUSDT", "BTC", -0.008, 8, observed_at),
            market("okx", "BTC-USDT-SWAP", "BTC", 0.0, 8, observed_at),
            book("bybit", "BTCUSDT", observed_at),
            book("okx", "BTC-USDT-SWAP", observed_at),
            history_rows("bybit", "BTCUSDT", now, 8, -0.0001, 90),
            history_rows("okx", "BTC-USDT-SWAP", now, 8, 0.0, 90),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)

            dashboard = store.funding_dashboard("next_settlement")

        route_key = dashboard["routes"][0]["route_key"]
        four_hour = dashboard["horizon_comparisons"]["4h"]["routes"][route_key]
        eight_hour = dashboard["horizon_comparisons"]["8h"]["routes"][route_key]

        self.assertEqual(
            four_hour["evidence"]["live_projection_unavailable_reason"],
            "no_settlement_in_horizon",
        )
        self.assertIsNone(four_hour["evidence"]["live_if_persists_net"])
        self.assertAlmostEqual(
            eight_hour["evidence"]["schedule_nowcast_rate"],
            route["evidence"]["current_nowcast_settlement_rate"],
        )
        self.assertAlmostEqual(
            eight_hour["evidence"]["live_if_persists_net"],
            route["evidence"]["current_nowcast_net"],
        )
        near_misses = dashboard["constraint_diagnostics"]["near_misses"]
        self.assertTrue(near_misses)
        self.assertEqual(
            {leg["side"] for leg in near_misses[0]["legs"]},
            {"long", "short"},
        )
        self.assertEqual(near_misses[0]["legs"][0]["funding_interval_hours"], 8)

    def test_dashboard_projects_fixed_horizons_from_current_route_snapshot(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("lighter", "BONK", "BONK", -0.008, 1, observed_at),
            market("binance", "BONKUSDT", "BONK", -0.004, 4, observed_at),
            book("lighter", "BONK", observed_at),
            book("binance", "BONKUSDT", observed_at),
            history_rows("lighter", "BONK", now, 1, -0.0001, 720),
            history_rows("binance", "BONKUSDT", now, 4, -0.0001, 180),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)

            dashboard = store.funding_dashboard("next_settlement")

        self.assertEqual(len(dashboard["routes"]), 1)
        route_key = dashboard["routes"][0]["route_key"]
        four_hour = dashboard["horizon_comparisons"]["4h"]["routes"][route_key]
        eight_hour = dashboard["horizon_comparisons"]["8h"]["routes"][route_key]

        self.assertEqual(
            four_hour["evidence"]["live_projection_source"],
            "current_route_snapshot",
        )
        self.assertAlmostEqual(
            four_hour["evidence"]["schedule_nowcast_rate"],
            0.028,
        )
        self.assertAlmostEqual(
            eight_hour["evidence"]["schedule_nowcast_rate"],
            0.056,
        )
        self.assertGreater(
            eight_hour["evidence"]["live_if_persists_net"],
            four_hour["evidence"]["live_if_persists_net"],
        )

    def test_dashboard_exposes_maker_setups_separately(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0005, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        maker = route["evidence"]["execution_scenarios"]["maker_entry_taker_exit"]
        maker["setup_visible"] = True
        maker["current_net_profit_if_filled"] = 6.25
        maker["current_expected_attempt_pnl"] = 1.50
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertEqual(route["status"], "watch")
        self.assertLessEqual(route["evidence"]["current_nowcast_net"], 0)
        self.assertEqual(dashboard["routes"], [])
        self.assertEqual(dashboard["watch_routes"], [])
        self.assertEqual(len(dashboard["maker_routes"]), 1)
        self.assertEqual(
            dashboard["maker_routes"][0]["evidence"]["execution_scenarios"][
                "maker_entry_taker_exit"
            ]["current_net_profit_if_filled"],
            6.25,
        )

    def test_route_exposes_needed_improvement_with_verified_vip1_profiles(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)

        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("bybit", "BTCUSDT", "BTC", 0.00035, 8, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("bybit", "BTCUSDT", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("bybit", "BTCUSDT", now, 8, 0.00035 / 8, 90),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        needed = route["evidence"]["needed_improvement"]
        self.assertEqual(needed["model_version"], "needed_improvement_v1")
        self.assertIn("binance", needed["vip1"]["applied_profiles"])
        self.assertIn("bybit", needed["vip1"]["applied_profiles"])
        self.assertGreater(
            needed["vip1"]["improvement_vs_current"],
            0,
        )
        self.assertGreater(
            needed["maker_entry"]["improvement_vs_current"],
            0,
        )

    def test_dashboard_candidates_order_by_live_nowcast_net(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        weak_live = evaluate_perp_route(
            market("binance", "AAAUSDT", "AAA", 0.0, 8, observed_at),
            market("hyperliquid", "AAA", "AAA", 0.006, 1, observed_at),
            book("binance", "AAAUSDT", observed_at),
            book("hyperliquid", "AAA", observed_at),
            history_rows("binance", "AAAUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "AAA", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        strong_live = evaluate_perp_route(
            market("binance", "BBBUSDT", "BBB", 0.0, 8, observed_at),
            market("hyperliquid", "BBB", "BBB", 0.007, 1, observed_at),
            book("binance", "BBBUSDT", observed_at),
            book("hyperliquid", "BBB", observed_at),
            history_rows("binance", "BBBUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BBB", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [weak_live, strong_live])
            store.finish_funding_scan(scan_id, "success", route_count=2)
            dashboard = store.funding_dashboard()

        self.assertEqual(len(dashboard["routes"]), 2)
        self.assertGreater(
            dashboard["routes"][0]["evidence"]["current_nowcast_net"],
            dashboard["routes"][1]["evidence"]["current_nowcast_net"],
        )
        self.assertEqual(dashboard["routes"][0]["canonical_asset"], "BBB")

    def test_dashboard_hides_candidates_below_visible_profit_floor(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "AAAUSDT", "AAA", 0.0, 8, observed_at),
            market("hyperliquid", "AAA", "AAA", 0.0035, 1, observed_at),
            book("binance", "AAAUSDT", observed_at),
            book("hyperliquid", "AAA", observed_at),
            history_rows("binance", "AAAUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "AAA", now, 1, -0.0001, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        route["status"] = "paper_candidate"
        route["evidence"]["decision_mode"] = "settlement_capture"
        route["evidence"]["current_nowcast_net"] = 0.99

        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {
                    "scan_mode": "manual",
                    "horizon_mode": "next_settlement",
                    "minimum_net_profit": 1.0,
                }
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertEqual(route["status"], "paper_candidate")
        self.assertEqual(dashboard["minimum_visible_profit"], 1.0)
        self.assertEqual(dashboard["routes"], [])

    def test_live_settlement_profit_is_not_vetoed_by_negative_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.02, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, -0.0005, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        evidence = route["evidence"]
        self.assertEqual(route["status"], "watch")
        self.assertEqual(evidence["decision_mode"], "settlement_capture")
        self.assertTrue(evidence["history_is_advisory"])
        self.assertGreaterEqual(
            evidence["current_nowcast_net"],
            evidence["actionable_profit_threshold"],
        )
        self.assertLess(evidence["conservative_net_profit"], 0)
        self.assertNotIn(
            "conservative_net_after_costs_too_low",
            evidence["blocking_risk_flags"],
        )
        self.assertIn(
            "conservative_net_after_costs_too_low",
            evidence["advisory_risk_flags"],
        )
        self.assertTrue(
            any("схлоп" in reason for reason in evidence["advisory_reasons"])
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(scan_id, "success", route_count=1)
            dashboard = store.funding_dashboard()

        self.assertEqual(len(dashboard["routes"]), 1)
        self.assertEqual(dashboard["routes"][0]["route_key"], route["route_key"])
        self.assertEqual(
            dashboard["constraint_diagnostics"]["exclusive_stages"],
            [{"stage": "paper_candidate", "route_count": 1}],
        )

    def test_large_basis_history_is_advisory_for_next_settlement(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market("hyperliquid", "BTC", "BTC", 0.0, 1, observed_at)
        long_book = shifted_book("binance", "BTCUSDT", observed_at, 100.0)
        short_book = shifted_book("hyperliquid", "BTC", observed_at, 102.0)
        long_book["_history"] = []
        short_book["_history"] = []

        route = evaluate_perp_route(
            long_market,
            short_market,
            long_book,
            short_book,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.0, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        evidence = route["evidence"]

        self.assertEqual(route["status"], "watch")
        self.assertIn("insufficient_basis_history", evidence["advisory_risk_flags"])
        self.assertNotIn(
            "insufficient_basis_history",
            evidence["blocking_risk_flags"],
        )
        self.assertGreaterEqual(
            evidence["current_opportunity_net"],
            evidence["actionable_profit_threshold"],
        )

    def test_large_basis_stress_does_not_veto_live_spread_candidate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_book = shifted_book("binance", "AAAUSDT", observed_at, 100.0)
        short_book = shifted_book("hyperliquid", "AAA", observed_at, 102.0)
        base_time = datetime.fromisoformat(observed_at)
        long_book["_history"] = []
        short_book["_history"] = []
        for index, short_mid in enumerate(
            [102.0, 104.0, 108.0, 112.0, 116.0, 120.0]
        ):
            timestamp = (base_time - timedelta(minutes=50 - index * 10)).isoformat()
            long_history = shifted_book("binance", "AAAUSDT", timestamp, 100.0)
            short_history = shifted_book("hyperliquid", "AAA", timestamp, short_mid)
            long_history["_history"] = []
            short_history["_history"] = []
            long_book["_history"].append(long_history)
            short_book["_history"].append(short_history)

        route = evaluate_perp_route(
            market("binance", "AAAUSDT", "AAA", 0.0, 8, observed_at),
            market("hyperliquid", "AAA", "AAA", 0.0, 1, observed_at),
            long_book,
            short_book,
            history_rows("binance", "AAAUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "AAA", now, 1, 0.0, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )
        evidence = route["evidence"]

        self.assertEqual(route["status"], "watch")
        self.assertGreater(
            evidence["current_opportunity_net"],
            evidence["actionable_profit_threshold"],
        )
        self.assertLess(evidence["current_opportunity_basis_stress_net"], 0)
        self.assertNotIn(
            "basis_not_covered_by_live_funding",
            evidence["blocking_risk_flags"],
        )
        self.assertIn(
            "basis_not_covered_by_live_funding",
            evidence["advisory_risk_flags"],
        )
        self.assertEqual(evidence["selected_strategy"]["warnings"], [])
        self.assertEqual(evidence["selected_strategy"]["expected_spread_convergence_pnl"], 0.0)
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan(
                {"scan_mode": "manual", "horizon_mode": "next_settlement"}
            )
            store.insert_funding_routes(scan_id, [route])
            store.finish_funding_scan(
                scan_id,
                "success",
                route_count=1,
                paper_candidate_count=1,
            )
            dashboard = store.funding_dashboard()

        funnel = dashboard["economics_funnel"]
        self.assertEqual(funnel["current_raw_gross_covers_full_cost"], 1)
        self.assertEqual(funnel["current_raw_actionable"], 1)
        self.assertEqual(
            dashboard["constraint_diagnostics"]["exclusive_stages"],
            [{"stage": "unclassified_gate", "route_count": 1}],
        )

    def test_route_legs_use_normalized_future_settlement_times(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)

        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.01, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.01, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        long_leg = next(leg for leg in route["legs"] if leg["side"] == "long")
        short_leg = next(leg for leg in route["legs"] if leg["side"] == "short")

        self.assertEqual(long_leg["next_funding_at"], "2026-07-14T20:00:00+00:00")
        self.assertEqual(short_leg["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertEqual(route["long_next_funding_at"], long_leg["next_funding_at"])
        self.assertEqual(route["short_next_funding_at"], short_leg["next_funding_at"])

    def test_orderbook_sequence_is_returned_in_chronological_order(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            first_scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.upsert_funding_instruments(
                [
                    {
                        "venue": "binance",
                        "symbol": "BTCUSDT",
                        "canonical_asset": "BTC",
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "collateral_asset": "USDT",
                        "contract_type": "perpetual",
                        "status": "active",
                        "observed_at": "2026-07-14T12:00:00+00:00",
                        "raw": {},
                    }
                ]
            )
            first = book("binance", "BTCUSDT", "2026-07-14T12:00:00+00:00")
            second = book("binance", "BTCUSDT", "2026-07-14T12:01:00+00:00")
            store.insert_funding_orderbooks(first_scan_id, [first])
            second_scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.insert_funding_orderbooks(second_scan_id, [second])
            rows = store.funding_orderbook_sequences(
                [("binance", "BTCUSDT")],
                limit_per_market=10,
            )[("binance", "BTCUSDT")]

        self.assertEqual(
            [row["observed_at"] for row in rows],
            [first["observed_at"], second["observed_at"]],
        )
        self.assertEqual(rows[0]["bids"], first["bids"])

    def test_latest_funding_orderbooks_returns_recent_cache_hits(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.upsert_funding_instruments(
                [
                    {
                        "venue": "binance",
                        "symbol": "BTCUSDT",
                        "canonical_asset": "BTC",
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "collateral_asset": "USDT",
                        "contract_type": "perpetual",
                        "status": "active",
                        "observed_at": "2026-07-14T12:00:00+00:00",
                        "raw": {},
                    }
                ]
            )
            first = book("binance", "BTCUSDT", "2026-07-14T12:00:00+00:00")
            store.insert_funding_orderbooks(scan_id, [first])

            hit = store.latest_funding_orderbooks(
                [("binance", "BTCUSDT")],
                min_observed_at="2026-07-14T11:59:45+00:00",
            )
            miss = store.latest_funding_orderbooks(
                [("binance", "BTCUSDT")],
                min_observed_at="2026-07-14T12:00:01+00:00",
            )

        self.assertEqual(hit[("binance", "BTCUSDT")]["bids"], first["bids"])
        self.assertEqual(miss, {})

    def test_latest_funding_catalog_by_venue_returns_recent_snapshots(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan({"scan_mode": "manual"})
            store.upsert_funding_instruments(
                [
                    {
                        "venue": "binance",
                        "symbol": "BTCUSDT",
                        "canonical_asset": "BTC",
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "collateral_asset": "USDT",
                        "contract_type": "perpetual",
                        "status": "active",
                        "observed_at": "2026-07-14T12:00:00+00:00",
                        "raw": {},
                    }
                ]
            )
            store.insert_funding_market_snapshots(
                scan_id,
                [
                    market(
                        "binance",
                        "BTCUSDT",
                        "BTC",
                        0.0008,
                        8,
                        "2026-07-14T12:00:00+00:00",
                    )
                ],
            )
            store.finish_funding_scan(scan_id, "success")

            hit = store.latest_funding_catalog_by_venue(
                ["binance"],
                min_observed_at="2026-07-14T11:59:55+00:00",
            )
            miss = store.latest_funding_catalog_by_venue(
                ["binance"],
                min_observed_at="2026-07-14T12:00:01+00:00",
            )

        instruments, markets, warnings = hit["binance"]
        self.assertEqual(warnings, [])
        self.assertEqual(instruments[0]["symbol"], "BTCUSDT")
        self.assertEqual(markets[0]["funding_rate"], 0.0008)
        self.assertEqual(markets[0]["canonical_unit_multiplier"], 1.0)
        self.assertEqual(miss, {})

    def test_dashboard_reports_complete_route_universe(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            store.init_db()
            scan_id = store.start_funding_scan({"scan_mode": "manual"})
            base = {
                "canonical_asset": "BTC",
                "long_venue": "binance",
                "long_symbol": "BTCUSDT",
                "short_venue": "okx",
                "short_symbol": "BTC-USDT-SWAP",
                "current_hourly_spread": 0.00001,
                "quick_gross_rate": 0.00008,
                "quick_taker_cost_rate": 0.001,
                "quick_maker_cost_rate": 0.0005,
                "quick_best_case_net_rate": -0.00042,
                "quick_schedule_ready": True,
                "execution_eligible": False,
                "execution_screen_reason": "best_case_carry_below_unavoidable_cost",
                "observed_at": "2026-07-14T12:00:00+00:00",
            }
            eligible = {
                **base,
                "canonical_asset": "ETH",
                "long_symbol": "ETHUSDT",
                "short_symbol": "ETH-USDT-SWAP",
                "quick_gross_rate": 0.002,
                "quick_best_case_net_rate": 0.0015,
                "execution_eligible": True,
                "execution_screen_reason": "full_execution_required",
            }
            store.insert_funding_route_universe(scan_id, [base, eligible])
            store.finish_funding_scan(scan_id, "success")

            dashboard = store.funding_dashboard()

        self.assertEqual(dashboard["universe_summary"]["route_count"], 2)
        self.assertEqual(
            dashboard["universe_summary"]["execution_shortlist_count"],
            1,
        )
        self.assertEqual(
            dashboard["universe_summary"]["selection_policy"],
            "adaptive_score_with_orderbook_market_budget",
        )

    def test_depth_sequence_measures_persistence_and_refill(self) -> None:
        snapshots = [
            sequence_book("2026-07-14T12:00:00+00:00", 10.0),
            sequence_book("2026-07-14T12:01:00+00:00", 2.0),
        ]
        current = sequence_book("2026-07-14T12:02:00+00:00", 10.0)

        profile = analyze_depth_sequence(snapshots, current, "bids", 500.0)

        self.assertAlmostEqual(profile["executable_fraction"], 2 / 3)
        self.assertEqual(profile["refill_event_count"], 1)
        self.assertEqual(profile["refill_success_count"], 1)
        self.assertEqual(profile["median_refill_seconds"], 60.0)

    def test_maker_fill_uses_price_through_observations(self) -> None:
        history = [
            sequence_book("2026-07-14T12:00:00+00:00", 10.0),
            sequence_book(
                "2026-07-14T12:01:00+00:00",
                10.0,
                best_bid=98.5,
                best_ask=98.9,
            ),
        ]
        current = sequence_book(
            "2026-07-14T12:02:00+00:00",
            10.0,
            best_bid=98.6,
            best_ask=99.0,
        )

        estimate = estimate_maker_fill(
            history,
            current,
            "buy",
            timeout_seconds=120,
            prior_probability=0.55,
            minimum_observations=1,
        )

        self.assertEqual(estimate["trial_count"], 2)
        self.assertGreaterEqual(estimate["success_count"], 1)
        self.assertTrue(estimate["data_ready"])
        self.assertGreater(estimate["adverse_selection_bps_q75"], 0)
        self.assertEqual(
            estimate["probability_source"],
            "orderbook_price_through_proxy",
        )

    def test_unstable_history_cannot_become_paper_candidate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        alternating = history_rows(
            "hyperliquid", "ETH", now, 1, 0.0001, 48, alternating=True
        )

        route = evaluate_perp_route(
            market("binance", "ETHUSDT", "ETH", 0.0, 8, observed_at),
            market("hyperliquid", "ETH", "ETH", 0.0001, 1, observed_at),
            book("binance", "ETHUSDT", observed_at),
            book("hyperliquid", "ETH", observed_at),
            history_rows("binance", "ETHUSDT", now, 8, 0.0, 8),
            alternating,
            observed_at,
            FundingScanConfig(minimum_history_points=12, minimum_persistence=0.75),
        )

        self.assertEqual(route["status"], "watch")
        self.assertIn("funding_direction_unstable", route["risk_flags"])

    def test_route_is_blocked_when_settlement_is_too_close_to_execute(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market(
            "hyperliquid",
            "BTC",
            "BTC",
            0.01,
            1,
            observed_at,
        )
        settlement = (now + timedelta(seconds=60)).isoformat()
        long_market["next_funding_at"] = settlement
        short_market["next_funding_at"] = settlement

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.01, 720),
            observed_at,
            FundingScanConfig(
                horizon_mode="next_settlement",
                minimum_history_points=12,
                minimum_settlement_lead_seconds=120,
            ),
        )

        self.assertEqual(route["status"], "watch")
        self.assertIn("insufficient_settlement_lead_time", route["risk_flags"])
        self.assertEqual(route["evidence"]["settlement_lead_seconds"], 60)

    def test_sparse_history_cannot_satisfy_persistence_gate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        sparse_long = history_rows("binance", "BTCUSDT", now, 8, 0.0, 8)[::2]

        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            sparse_long,
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 64),
            observed_at,
            FundingScanConfig(
                minimum_history_points=12,
                minimum_history_coverage=0.8,
            ),
        )

        self.assertEqual(route["status"], "watch")
        self.assertIn("sparse_funding_history", route["risk_flags"])

    def test_executable_basis_above_absolute_sanity_ceiling_is_a_hard_gate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at)
        short_market["mark_price"] = 101.0
        short_book = shifted_book("hyperliquid", "BTC", observed_at, 101.0)

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("binance", "BTCUSDT", observed_at),
            short_book,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 8),
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 48),
            observed_at,
            FundingScanConfig(
                minimum_history_points=12,
                maximum_research_basis_gap_bps=20,
            ),
        )

        self.assertEqual(route["status"], "watch")
        self.assertIn("basis_divergence", route["risk_flags"])
        self.assertTrue(any("basis" in reason for reason in route["evidence"]["blocking_reasons"]))

    def test_funding_profit_can_cover_unfavorable_signed_basis(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_book = shifted_book("binance", "BTCUSDT", observed_at, 100.5)
        short_book = shifted_book("hyperliquid", "BTC", observed_at, 99.5)
        long_market = market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at)
        short_market = market(
            "hyperliquid", "BTC", "BTC", 0.002, 1, observed_at
        )
        long_market.update({"mark_price": 100.5, "index_price": 100.0})
        short_market.update({"mark_price": 99.5, "index_price": 100.0})

        route = evaluate_perp_route(
            long_market,
            short_market,
            long_book,
            short_book,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", now, 1, 0.002, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="fixed",
                horizon_hours=24,
                minimum_history_points=12,
                maximum_basis_gap_bps=200,
            ),
        )

        basis = route["evidence"]["basis_model"]
        self.assertLess(basis["signed_entry_basis"], 0)
        self.assertGreater(basis["unfavorable_convergence_loss_rate"], 0)
        self.assertNotIn("basis_divergence", route["risk_flags"])
        self.assertGreater(route["evidence"]["conservative_net_profit"], 0)
        self.assertTrue(route["evidence"]["meets_basis_coverage_gate"])
        self.assertGreater(route["evidence"]["basis_stress_net_profit"], 5)
        self.assertEqual(route["status"], "paper_candidate")

    def test_crossed_orderbook_fails_closed(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        crossed = book("hyperliquid", "BTC", observed_at)
        crossed.update({"best_bid": 100.1, "best_ask": 100.0, "mid_price": 100.05})

        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            crossed,
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 8),
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 48),
            observed_at,
            FundingScanConfig(minimum_history_points=12),
        )

        self.assertEqual(route["status"], "watch")
        self.assertIn("invalid_orderbook", route["risk_flags"])
        self.assertEqual(route["market_capacity"], 0)

    def test_horizon_with_one_zero_cashflow_has_no_projected_carry(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("hyperliquid", "BTC", "BTC", 0.0, 1, observed_at)
        short_market = market("binance", "BTCUSDT", "BTC", 0.0008, 8, observed_at)
        long_market["next_funding_at"] = (now + timedelta(hours=1)).isoformat()
        short_market["next_funding_at"] = (now + timedelta(hours=8)).isoformat()

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("hyperliquid", "BTC", observed_at),
            book("binance", "BTCUSDT", observed_at),
            history_rows("hyperliquid", "BTC", now, 1, 0.0, 48),
            history_rows("binance", "BTCUSDT", now, 8, 0.0001, 8),
            observed_at,
            FundingScanConfig(
                horizon_mode="fixed",
                horizon_hours=4,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["expected_gross_funding"], 0)
        self.assertNotIn("funding_schedule_unavailable", route["risk_flags"])
        self.assertFalse(route["evidence"]["both_legs_settle"])
        self.assertTrue(
            route["evidence"]["normalized_interval_projection"]["projection_used"]
        )
        self.assertAlmostEqual(
            route["evidence"]["normalized_interval_projection"]["projection_hours"],
            8.0,
        )

    def test_short_horizon_uses_normalized_projection_for_longer_funding_interval(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        one_hour = market("one-hour", "BTC-1H", "BTC", 0.0, 1, observed_at)
        eight_hour = market("eight-hour", "BTC-8H", "BTC", 0.008, 8, observed_at)
        one_hour["next_funding_at"] = (now + timedelta(hours=1)).isoformat()
        eight_hour["next_funding_at"] = (now + timedelta(hours=8)).isoformat()
        config = FundingScanConfig(horizon_mode="fixed", horizon_hours=4).validated()

        ranked = rank_perp_pairs(
            [one_hour, eight_hour],
            None,
            config=config,
            observed_at=observed_at,
        )
        selected = execution_shortlist(ranked, config=config)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(selected, ranked)
        route = ranked[0]
        self.assertTrue(route["quick_projection_used"])
        self.assertTrue(route["quick_schedule_ready"])
        self.assertAlmostEqual(route["quick_projection_hours"], 8.0)
        self.assertAlmostEqual(route["quick_actual_cashflow_gross_rate"], 0.0)
        self.assertAlmostEqual(route["quick_projected_gross_rate"], 0.008)
        self.assertEqual(
            route["execution_screen_reason"],
            "projected_interval_full_depth",
        )

    def test_short_horizon_does_not_block_when_both_legs_settle_after_window(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("long8h", "BTC-LONG", "BTC", 0.0, 8, observed_at)
        short_market = market("short8h", "BTC-SHORT", "BTC", 0.008, 8, observed_at)
        long_market["next_funding_at"] = (now + timedelta(hours=8)).isoformat()
        short_market["next_funding_at"] = (now + timedelta(hours=8)).isoformat()

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("long8h", "BTC-LONG", observed_at),
            book("short8h", "BTC-SHORT", observed_at),
            history_rows("long8h", "BTC-LONG", now, 8, 0.0, 24),
            history_rows("short8h", "BTC-SHORT", now, 8, 0.008, 24),
            observed_at,
            FundingScanConfig(
                horizon_mode="fixed",
                horizon_hours=4,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["expected_gross_funding"], 0)
        self.assertNotIn("funding_schedule_unavailable", route["risk_flags"])
        self.assertIn("normalized_interval_projection", route["risk_flags"])
        projection = route["evidence"]["normalized_interval_projection"]
        self.assertTrue(projection["projection_used"])
        self.assertEqual(projection["actual_cashflow_event_count"], 0)
        self.assertAlmostEqual(projection["projection_hours"], 8.0)
        self.assertAlmostEqual(projection["projected_gross_rate"], 0.008)

    def test_supported_horizons_cap_fixed_mode_and_keep_research_explicit(self) -> None:
        oversized_fixed = FundingScanConfig(horizon_hours=200).validated()
        research = FundingScanConfig(
            horizon_mode="research",
            horizon_hours=72,
        ).validated()

        self.assertEqual(oversized_fixed.horizon_mode, "fixed")
        self.assertEqual(oversized_fixed.horizon_hours, 24)
        self.assertEqual(research.horizon_mode, "research")
        self.assertEqual(research.horizon_hours, 72)
        self.assertTrue(research.research_only)
        self.assertEqual(
            [
                FundingScanConfig(horizon_mode="fixed", horizon_hours=hours)
                .validated()
                .horizon_hours
                for hours in (4, 8, 24)
            ],
            [4, 8, 24],
        )

    def test_forward_outcomes_never_bridge_missing_hours(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=UTC)
        sparse_series = [
            (start + timedelta(hours=index * 2), 0.0005)
            for index in range(60)
        ]

        self.assertEqual(forward_samples(sparse_series, 4, 0.0005), [])

    def test_hourly_window_floors_observation_minutes(self) -> None:
        observed = datetime(2026, 7, 14, 21, 25, tzinfo=UTC)
        series = [
            (datetime(2026, 7, 13, 22, tzinfo=UTC) + timedelta(hours=index), 1.0)
            for index in range(18)
        ]

        self.assertEqual(len(trailing_series(series, 24, observed)), 18)

    def test_realized_windows_anchor_to_last_completed_settlement(self) -> None:
        settled_at = datetime(2026, 7, 14, 12, tzinfo=UTC)
        observed_at = "2026-07-14T20:00:00+00:00"
        forecast = build_funding_forecast(
            history_rows("binance", "BTCUSDT", settled_at, 8, 0.0, 90),
            history_rows("hyperliquid", "BTC", settled_at, 1, 0.001, 720),
            0.001,
            forecast_schedule(24),
            FundingScanConfig(horizon_mode="fixed", horizon_hours=24).validated(),
            observed_at=observed_at,
        )

        self.assertTrue(forecast["window_readiness"]["realized_24h"])
        self.assertNotEqual(forecast["realized_window_end_at"], observed_at)

    def test_historical_intervals_follow_local_settlement_spacing(self) -> None:
        intervals = inferred_funding_intervals(
            [0, 4 * 3_600_000, 8 * 3_600_000, 16 * 3_600_000],
            8,
            units_per_second=1000,
        )

        self.assertEqual(intervals[0], 4)
        self.assertEqual(intervals[4 * 3_600_000], 4)
        self.assertEqual(intervals[8 * 3_600_000], 4)
        self.assertEqual(intervals[16 * 3_600_000], 8)

    def test_settlement_outcomes_do_not_use_future_rate_as_entry_signal(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=UTC)
        long_history = history_rows("long", "BTC", start + timedelta(hours=48), 1, 0.0, 49)
        short_history = history_rows("short", "BTC", start + timedelta(hours=48), 1, 0.001, 49)
        short_history[1]["funding_rate"] = 0.003
        short_history[1]["hourly_funding_rate"] = 0.003

        samples = settlement_forward_samples(
            long_history,
            short_history,
            1,
            0.001,
        )

        self.assertTrue(samples)
        self.assertAlmostEqual(samples[0]["entry_hourly_spread"], 0.001)
        self.assertAlmostEqual(samples[0]["actual_cumulative_rate"], 0.003)

    def test_live_settlement_outcomes_do_not_use_ninety_day_risk_window(self) -> None:
        end = datetime(2026, 7, 14, 12, tzinfo=UTC)
        long_history = history_rows("long", "BTC", end, 1, 0.0, 2_160)
        short_history = history_rows("short", "BTC", end, 1, 0.001, 2_160)

        samples = settlement_forward_samples(
            long_history,
            short_history,
            24,
            0.001,
        )

        self.assertLessEqual(len(samples), 30)

    def test_regime_survival_conditions_on_current_positive_run_age(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=UTC)
        series: list[tuple[datetime, float]] = []
        cursor = start
        for run_hours in (4, 8, 12, 24):
            for _ in range(run_hours):
                series.append((cursor, 0.0005))
                cursor += timedelta(hours=1)
            series.append((cursor, -0.0001))
            cursor += timedelta(hours=1)
        for _ in range(2):
            series.append((cursor, 0.0005))
            cursor += timedelta(hours=1)

        result = regime_diagnostics(series)

        self.assertEqual(result["age_hours"], 2)
        self.assertEqual(result["duration_sample_count"], 4)
        self.assertEqual(result["remaining_hours_median"], 8)
        self.assertEqual(result["survival_probabilities"]["4"], 0.75)
        self.assertEqual(result["survival_probabilities"]["8"], 0.5)
        self.assertEqual(result["survival_probabilities"]["24"], 0.0)

    def test_probability_gate_uses_conservative_wilson_lower_bound(self) -> None:
        self.assertLess(wilson_lower_bound(20, 20), 1.0)
        self.assertGreater(wilson_lower_bound(20, 20), 0.85)
        self.assertLess(wilson_lower_bound(14, 20), 0.7)

    def test_live_anchor_uses_baseline_45_25_15_10_5_weights(self) -> None:
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        long_history = history_rows("long", "BTC", now, 1, 0.0, 2_160)
        short_history = history_rows("short", "BTC", now, 1, -0.0001, 2_160)
        for row in short_history[-72:]:
            row["funding_rate"] = 0.0005
            row["hourly_funding_rate"] = 0.0005

        forecast = build_funding_forecast(
            long_history,
            short_history,
            0.0005,
            forecast_schedule(24),
            FundingScanConfig(horizon_mode="fixed", horizon_hours=24).validated(),
        )

        weights = forecast["component_weights"]
        self.assertAlmostEqual(weights["next_estimate"], 0.45)
        self.assertAlmostEqual(weights["realized_24h"], 0.25)
        self.assertAlmostEqual(weights["realized_72h"], 0.15)
        self.assertAlmostEqual(weights["persistence_14d"], 0.10)
        self.assertAlmostEqual(weights["regime_30d"], 0.05)
        self.assertAlmostEqual(
            forecast["weighted_expected_hourly_spread"],
            sum(
                float(forecast["component_values"][name]) * float(weights[name])
                for name in (
                    "next_estimate",
                    "realized_24h",
                    "realized_72h",
                    "persistence_14d",
                )
            ),
        )
        self.assertTrue(forecast["regime_30d_is_risk_filter"])
        self.assertGreater(forecast["anchor_hourly_spread"], 0)
        self.assertEqual(
            forecast["lookback_windows"]["recent_72h"]["positive_fraction"],
            1.0,
        )
        self.assertLess(forecast["lookback_windows"]["risk_90d"]["median"], 0)

    def test_next_estimate_weight_rises_with_interval_maturity(self) -> None:
        self.assertAlmostEqual(dynamic_nowcast_weight(0.0), 0.15)
        self.assertAlmostEqual(dynamic_nowcast_weight(0.25), 0.25)
        self.assertAlmostEqual(dynamic_nowcast_weight(0.50), 0.35)
        self.assertAlmostEqual(dynamic_nowcast_weight(0.75), 0.45)
        self.assertAlmostEqual(dynamic_nowcast_weight(1.0), 0.60)

    def test_route_nowcast_weight_uses_less_mature_leg(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        mature = market("binance", "BTCUSDT", "BTC", 0.0008, 8, observed_at)
        immature = market("bybit", "BTCUSDT", "BTC", 0.0008, 8, observed_at)
        mature["next_funding_at"] = (observed + timedelta(hours=1)).isoformat()
        immature["next_funding_at"] = (observed + timedelta(hours=6)).isoformat()

        weights = forecast_component_weights(mature, immature, observed_at)

        self.assertAlmostEqual(weights["long_interval_progress"], 0.875)
        self.assertAlmostEqual(weights["short_interval_progress"], 0.25)
        self.assertAlmostEqual(weights["interval_progress"], 0.25)
        self.assertAlmostEqual(weights["next_estimate"], 0.25)
        self.assertAlmostEqual(sum(value for key, value in weights.items() if key in {
            "next_estimate", "realized_24h", "realized_72h", "persistence_14d", "regime_30d"
        }), 1.0)

    def test_each_leg_uses_its_own_nowcast_maturity(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market("binance", "BTCUSDT", "BTC", 0.0008, 8, observed_at)
        short_market = market("bybit", "BTCUSDT", "BTC", 0.0008, 8, observed_at)
        long_market["next_funding_at"] = (observed + timedelta(hours=1)).isoformat()
        short_market["next_funding_at"] = (observed + timedelta(hours=6)).isoformat()
        history = history_rows("venue", "BTC", observed, 1, 0.0001, 720)

        forecast = build_funding_forecast(
            history,
            history,
            0.0,
            paired_settlement_schedule(
                long_market,
                short_market,
                observed_at,
                FundingScanConfig(horizon_mode="fixed", horizon_hours=8).validated(),
            ),
            FundingScanConfig(horizon_mode="fixed", horizon_hours=8).validated(),
            long_market=long_market,
            short_market=short_market,
            observed_at=observed_at,
        )

        self.assertAlmostEqual(
            forecast["leg_signals"]["long"]["weights"]["next_estimate"],
            0.525,
        )
        self.assertAlmostEqual(
            forecast["leg_signals"]["short"]["weights"]["next_estimate"],
            0.25,
        )

    def test_next_estimate_is_not_repeated_after_first_settlement(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market("long", "BTC", "BTC", 0.0, 1, observed_at)
        short_market = market("short", "BTC", "BTC", 0.01, 1, observed_at)
        zero_history = history_rows("venue", "BTC", observed, 1, 0.0, 720)
        config = FundingScanConfig(
            horizon_mode="fixed",
            horizon_hours=4,
        ).validated()

        forecast = build_funding_forecast(
            zero_history,
            zero_history,
            0.01,
            paired_settlement_schedule(
                long_market,
                short_market,
                observed_at,
                config,
            ),
            config,
            long_market=long_market,
            short_market=short_market,
            observed_at=observed_at,
        )

        self.assertEqual(len(forecast["settlements"]), 4)
        self.assertAlmostEqual(forecast["raw_settlement_rate"], 0.0015)
        self.assertAlmostEqual(
            forecast["settlements"][0]["raw_event_rate"],
            0.0015,
        )
        self.assertTrue(
            all(
                row["raw_event_rate"] == 0
                for row in forecast["settlements"][1:]
            )
        )

    def test_live_nowcast_rate_only_covers_next_revalidation_in_fixed_horizon(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        long_market = market("long", "BTC", "BTC", 0.0, 1, observed_at)
        short_market = market("short", "BTC", "BTC", 0.01, 1, observed_at)
        config = FundingScanConfig(
            horizon_mode="fixed",
            horizon_hours=4,
        ).validated()
        schedule = paired_settlement_schedule(
            long_market,
            short_market,
            observed_at,
            config,
        )

        self.assertEqual(len(schedule["settlements"]), 4)
        self.assertAlmostEqual(
            current_schedule_carry_rate(schedule, long_market, short_market),
            0.01,
        )

    def test_eight_hour_rates_are_multiplied_by_settlement_interval_once(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market("long8h", "BTC-LONG", "BTC", 0.008, 8, observed_at)
        short_market = market("short8h", "BTC-SHORT", "BTC", 0.016, 8, observed_at)
        long_market["next_funding_at"] = (observed + timedelta(hours=8)).isoformat()
        short_market["next_funding_at"] = (observed + timedelta(hours=8)).isoformat()
        config = FundingScanConfig(horizon_mode="next_settlement").validated()
        schedule = paired_settlement_schedule(
            long_market,
            short_market,
            observed_at,
            config,
        )

        self.assertAlmostEqual(
            current_schedule_carry_rate(schedule, long_market, short_market),
            0.008,
        )
        ranked = rank_perp_pairs(
            [long_market, short_market],
            None,
            config=config,
            observed_at=observed_at,
        )
        self.assertAlmostEqual(ranked[0]["quick_actual_cashflow_gross_rate"], 0.008)
        self.assertAlmostEqual(ranked[0]["quick_projected_gross_rate"], 0.008)

    def test_route_flags_funding_rate_at_cap_or_floor(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market("aster", "BTCUSDT", "BTC", 0.0, 1, observed_at)
        short_market = market("binance", "BTCUSDT", "BTC", 0.02, 4, observed_at)
        short_market["funding_rate_cap"] = 0.02
        short_market["funding_rate_floor"] = -0.02

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("aster", "BTCUSDT", observed_at),
            book("binance", "BTCUSDT", observed_at),
            history_rows("aster", "BTCUSDT", observed, 1, 0.0, 720),
            history_rows("binance", "BTCUSDT", observed, 4, 0.02, 180),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="next_settlement",
                minimum_history_points=12,
            ),
        )

        self.assertIn("funding_rate_at_cap_or_floor", route["risk_flags"])
        short_leg = next(leg for leg in route["legs"] if leg["side"] == "short")
        self.assertEqual(short_leg["funding_rate_cap_floor_state"], "cap")

    def test_long_windows_are_context_not_candidate_gates(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.001, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 9),
            history_rows("hyperliquid", "BTC", now, 1, 0.001, 72),
            observed_at,
            FundingScanConfig(minimum_history_points=12),
        )

        self.assertEqual(route["status"], "watch")
        self.assertNotIn("insufficient_forecast_windows", route["risk_flags"])
        self.assertIn("limited_14d_context", route["risk_flags"])
        self.assertIn("limited_30d_regime_context", route["risk_flags"])

    def test_recent_negative_regime_vetoes_positive_long_history(self) -> None:
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        long_history = history_rows("long", "BTC", now, 1, 0.0, 2_160)
        short_history = history_rows("short", "BTC", now, 1, 0.0005, 2_160)
        for row in short_history[-72:]:
            row["funding_rate"] = -0.0001
            row["hourly_funding_rate"] = -0.0001

        forecast = build_funding_forecast(
            long_history,
            short_history,
            0.0005,
            forecast_schedule(24),
            FundingScanConfig(horizon_mode="fixed", horizon_hours=24).validated(),
        )

        self.assertGreater(forecast["lookback_windows"]["risk_90d"]["median"], 0)
        self.assertLess(forecast["lookback_windows"]["recent_72h"]["median"], 0)
        self.assertEqual(forecast["anchor_hourly_spread"], 0)

    def test_next_settlement_stops_at_first_cashflow(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("hyperliquid", "BTC", "BTC", 0.0, 1, observed_at)
        short_market = market("binance", "BTCUSDT", "BTC", 0.004, 8, observed_at)
        long_market["next_funding_at"] = (now + timedelta(hours=1)).isoformat()
        short_market["next_funding_at"] = (now + timedelta(hours=8)).isoformat()

        schedule = paired_settlement_schedule(
            long_market,
            short_market,
            observed_at,
            FundingScanConfig(horizon_mode="next_settlement").validated(),
        )

        self.assertEqual(schedule["horizon_hours"], 1)
        self.assertEqual(schedule["paired_coverage_hours"], 0)
        self.assertEqual(schedule["long_settlement_count"], 1)
        self.assertEqual(schedule["short_settlement_count"], 0)
        self.assertEqual(
            schedule["authorization_valid_until"],
            (now + timedelta(hours=1)).isoformat(),
        )

    def test_asymmetric_settlement_forecasts_the_settling_leg(self) -> None:
        observed_at = "2026-07-14T18:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market(
            "venue-a", "AAA-A", "AAA", -0.004, 4, observed_at
        )
        short_market = market(
            "venue-b", "AAA-B", "AAA", -0.016, 8, observed_at
        )
        long_market["next_funding_at"] = (
            observed + timedelta(hours=2)
        ).isoformat()
        short_market["next_funding_at"] = (
            observed + timedelta(hours=6)
        ).isoformat()
        schedule = paired_settlement_schedule(
            long_market,
            short_market,
            observed_at,
            FundingScanConfig(horizon_mode="next_settlement").validated(),
        )

        forecast = build_funding_forecast(
            history_rows(
                "venue-a", "AAA-A", observed - timedelta(hours=2), 4, -0.001, 181
            ),
            history_rows(
                "venue-b", "AAA-B", observed - timedelta(hours=2), 8, -0.002, 91
            ),
            -0.001,
            schedule,
            FundingScanConfig(horizon_mode="next_settlement").validated(),
            long_market=long_market,
            short_market=short_market,
            observed_at=observed_at,
        )

        self.assertTrue(forecast["settlement_schedule_asymmetric"])
        self.assertGreater(forecast["schedule_nowcast_rate"], 0)
        self.assertGreater(forecast["anchor_hourly_spread"], 0)
        self.assertGreater(forecast["positive_spread_fraction"], 0.9)
        self.assertGreater(forecast["settlements"][0]["forecast_event_rate"], 0)

    def test_three_day_research_route_never_becomes_candidate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("hyperliquid", "BTC", "BTC", 0.001, 1, observed_at),
            book("binance", "BTCUSDT", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 270),
            history_rows("hyperliquid", "BTC", now, 1, 0.001, 2_160),
            observed_at,
            FundingScanConfig(
                horizon_mode="research",
                horizon_hours=72,
                minimum_history_points=12,
            ),
        )

        self.assertEqual(route["status"], "watch")
        self.assertEqual(route["evidence"]["horizon"]["horizon_label"], "3 days · Research")
        self.assertIn("research_only_horizon", route["risk_flags"])

    def test_previous_paper_route_is_pinned_outside_current_top_n(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        markets = [
            market("binance", "AAAUSDT", "AAA", 0.0, 8, observed_at),
            market("hyperliquid", "AAA", "AAA", 0.00008, 1, observed_at),
            market("binance", "BBBUSDT", "BBB", 0.0, 8, observed_at),
            market("hyperliquid", "BBB", "BBB", 0.0002, 1, observed_at),
        ]
        previous = [{"canonical_asset": "AAA", "long_venue": "binance", "short_venue": "hyperliquid"}]

        ranked = rank_perp_pairs(markets, 1, pinned_routes=previous)

        self.assertEqual(len(ranked), 2)
        pinned = next(row for row in ranked if row["canonical_asset"] == "AAA")
        self.assertTrue(pinned["is_pinned_revalidation"])

    def test_pair_generation_remains_generic_across_five_venues(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        venues = ["binance", "bybit", "hyperliquid", "okx", "dydx"]
        markets = [
            market(venue, f"BTC-{venue}", "BTC", index * 0.00001, 1, observed_at)
            for index, venue in enumerate(venues)
        ]

        ranked = rank_perp_pairs(markets, 20)

        self.assertEqual(len(ranked), 10)
        self.assertEqual(
            {row["long_market"]["venue"] for row in ranked},
            set(venues[:-1]),
        )
        self.assertTrue(
            all(
                row["long_market"]["hourly_funding_rate"]
                <= row["short_market"]["hourly_funding_rate"]
                for row in ranked
            )
        )

    def test_unlimited_pair_generation_keeps_complete_route_universe(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        venues = ["binance", "bybit", "hyperliquid", "okx", "dydx", "gate"]
        markets = [
            market(venue, f"ETH-{venue}", "ETH", index * 0.00001, 1, observed_at)
            for index, venue in enumerate(venues)
        ]

        ranked = rank_perp_pairs(markets, None)

        self.assertEqual(len(ranked), 15)
        self.assertEqual(
            FundingScanConfig(max_candidates=5_000).validated().max_candidates,
            5_000,
        )
        self.assertIsNone(FundingScanConfig().validated().max_candidates)

    def test_near_miss_full_depth_can_be_unlimited(self) -> None:
        self.assertIsNone(
            FundingScanConfig().validated().near_miss_full_depth_routes,
        )
        self.assertEqual(
            FundingScanConfig(
                near_miss_full_depth_routes=250
            ).validated().near_miss_full_depth_routes,
            250,
        )
        self.assertEqual(
            FundingScanConfig(
                near_miss_full_depth_routes=0
            ).validated().near_miss_full_depth_routes,
            0,
        )
        self.assertEqual(
            FundingScanConfig(
                near_miss_full_depth_routes=50_000
            ).validated().near_miss_full_depth_routes,
            50_000,
        )

    def test_live_scan_defaults_have_no_orderbook_or_settlement_lead_caps(self) -> None:
        config = FundingScanConfig().validated()

        self.assertIsNone(config.max_full_depth_orderbook_markets)
        self.assertEqual(config.minimum_settlement_lead_seconds, 0)

    def test_execution_shortlist_has_no_numeric_cap(self) -> None:
        candidates = [
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.0032,
                "quick_best_case_net_rate": 0.003,
            }
            for _ in range(250)
        ]
        candidates.append(
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.0001,
                "quick_best_case_net_rate": -0.0001,
            }
        )

        selected = execution_shortlist(candidates, near_miss_limit=0)

        self.assertEqual(len(selected), 250)
        self.assertEqual(
            candidates[-1]["execution_screen_reason"],
            "best_case_carry_below_unavoidable_cost",
        )

    def test_execution_shortlist_keeps_low_positive_quick_profit(self) -> None:
        candidates = [
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.0004,
                "quick_best_case_net_rate": 0.0001,
            }
        ]

        selected = execution_shortlist(
            candidates,
            near_miss_limit=0,
            config=FundingScanConfig(target_notional=500),
        )

        self.assertEqual(selected, candidates)
        self.assertEqual(
            candidates[0]["execution_screen_reason"],
            "full_execution_required",
        )
        self.assertFalse(candidates[0]["quick_actionable_net"])
        self.assertAlmostEqual(
            candidates[0]["quick_best_case_net_profit"],
            0.05,
        )

    def test_pinned_revalidation_must_still_pass_quick_economics(self) -> None:
        candidates = [
            {
                "is_pinned_revalidation": True,
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.0004,
                "quick_best_case_net_rate": -0.0001,
            }
        ]

        selected = execution_shortlist(candidates, near_miss_limit=0)

        self.assertEqual(selected, [])
        self.assertEqual(
            candidates[0]["execution_screen_reason"],
            "best_case_carry_below_unavoidable_cost",
        )

    def test_execution_shortlist_includes_all_near_misses_when_unlimited(self) -> None:
        candidates = [
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.001,
                "quick_best_case_net_rate": -0.0003,
            },
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.001,
                "quick_best_case_net_rate": -0.0001,
            },
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": -0.001,
                "quick_best_case_net_rate": -0.00001,
            },
        ]

        selected = execution_shortlist(candidates)

        self.assertEqual(selected, [candidates[1], candidates[0]])
        self.assertEqual(
            candidates[0]["execution_screen_reason"],
            "adaptive_dynamic_gross_full_depth",
        )
        self.assertEqual(
            candidates[1]["execution_screen_reason"],
            "adaptive_dynamic_gross_full_depth",
        )
        self.assertEqual(
            candidates[2]["execution_screen_reason"],
            "non_positive_settlement_carry",
        )

    def test_execution_shortlist_can_expand_near_miss_research_band(self) -> None:
        candidates = [
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.001,
                "quick_best_case_net_rate": -0.0003,
            },
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.001,
                "quick_best_case_net_rate": -0.0001,
            },
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": -0.001,
                "quick_best_case_net_rate": -0.00001,
            },
        ]

        selected = execution_shortlist(candidates, near_miss_limit=1)

        self.assertEqual(len(selected), 1)
        self.assertIs(selected[0], candidates[1])
        self.assertEqual(
            candidates[1]["execution_screen_reason"],
            "adaptive_dynamic_gross_full_depth",
        )
        self.assertEqual(
            candidates[0]["execution_screen_reason"],
            "queued_for_deep_sweep",
        )
        self.assertEqual(
            candidates[2]["execution_screen_reason"],
            "non_positive_settlement_carry",
        )

    def test_adaptive_near_miss_respects_orderbook_market_budget(self) -> None:
        candidates = []
        for index in range(15):
            candidates.append(
                {
                    "quick_schedule_ready": True,
                    "quick_gross_rate": 0.0008,
                    "quick_best_case_net_rate": -0.0001,
                    "long_market": {
                        "venue": "long",
                        "symbol": f"AAA{index}",
                        "volume_24h_usd": 10_000_000,
                    },
                    "short_market": {
                        "venue": "short",
                        "symbol": f"AAA{index}",
                        "volume_24h_usd": 10_000_000,
                    },
                }
            )

        selected = execution_shortlist(
            candidates,
            config=FundingScanConfig(max_full_depth_orderbook_markets=4),
        )

        self.assertEqual(len(selected), 12)
        self.assertEqual(
            [row["execution_screen_reason"] for row in candidates],
            ["adaptive_dynamic_gross_full_depth"] * 12
            + ["queued_for_deep_sweep"] * 3,
        )

    def test_dynamic_emergency_threshold_does_not_include_min_profit_floor(self) -> None:
        small_position_route = {
            "quick_schedule_ready": True,
            "quick_gross_rate": 0.001,
            "quick_best_case_net_rate": -0.0001,
            "long_market": {
                "venue": "long",
                "symbol": "SMALL",
                "volume_24h_usd": 100_000,
            },
            "short_market": {
                "venue": "short",
                "symbol": "SMALL",
                "volume_24h_usd": 100_000,
            },
        }
        large_position_route = {
            **small_position_route,
            "long_market": {**small_position_route["long_market"], "symbol": "LARGE"},
            "short_market": {**small_position_route["short_market"], "symbol": "LARGE"},
        }

        small_selected = execution_shortlist(
            [small_position_route],
            config=FundingScanConfig(target_notional=500),
        )
        large_selected = execution_shortlist(
            [large_position_route],
            config=FundingScanConfig(target_notional=10_000),
        )

        self.assertEqual(small_selected, [small_position_route])
        self.assertLess(
            small_position_route["quick_dynamic_emergency_gross_bps"],
            100.0,
        )
        self.assertEqual(large_selected, [large_position_route])
        self.assertEqual(
            large_position_route["execution_screen_reason"],
            "adaptive_dynamic_gross_full_depth",
        )

    def test_adaptive_near_miss_queues_weak_positive_gross_routes(self) -> None:
        candidates = [
            {
                "quick_schedule_ready": True,
                "quick_gross_rate": 0.00001,
                "quick_best_case_net_rate": -0.001,
                "long_market": {
                    "venue": "long",
                    "symbol": "DUST",
                    "volume_24h_usd": 100_000_000,
                },
                "short_market": {
                    "venue": "short",
                    "symbol": "DUST",
                    "volume_24h_usd": 100_000_000,
                },
            }
        ]

        selected = execution_shortlist(candidates)

        self.assertEqual(selected, [])
        self.assertEqual(
            candidates[0]["execution_screen_reason"],
            "queued_for_deep_sweep",
        )

    def test_execution_shortlist_rejects_scaled_unit_identity_mismatch(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        long_market = market("lighter", "1000BONK", "BONK", -0.01, 1, observed_at)
        short_market = market("mexc", "1000BONK_USDT", "BONK", 0.01, 1, observed_at)
        for row in (long_market, short_market):
            row["next_funding_at"] = (observed + timedelta(hours=1)).isoformat()
        long_market["mark_price"] = 0.000003
        long_market["index_price"] = 0.000003
        short_market["mark_price"] = 0.003
        short_market["index_price"] = 0.003

        ranked = rank_perp_pairs(
            [long_market, short_market],
            None,
            config=FundingScanConfig(horizon_mode="next_settlement"),
            observed_at=observed_at,
        )
        selected = execution_shortlist(ranked)

        self.assertEqual(selected, [])
        self.assertTrue(ranked[0]["quick_identity_mismatch"])
        self.assertEqual(
            ranked[0]["execution_screen_reason"],
            "unit_identity_mismatch",
        )

    def test_pair_preselection_prefers_quick_net_carry_over_headline_spread(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        expensive_long = market("expensive-a", "AAA-A", "AAA", 0.0, 1, observed_at)
        expensive_short = market("expensive-b", "AAA-B", "AAA", 0.0012, 1, observed_at)
        cheap_long = market("cheap-a", "BBB-A", "BBB", 0.0, 1, observed_at)
        cheap_short = market("cheap-b", "BBB-B", "BBB", 0.0010, 1, observed_at)
        for row in (expensive_long, expensive_short):
            row["taker_fee_rate"] = 0.001
        for row in (cheap_long, cheap_short):
            row["taker_fee_rate"] = 0.0

        ranked = rank_perp_pairs(
            [expensive_long, expensive_short, cheap_long, cheap_short],
            1,
            config=FundingScanConfig(horizon_mode="next_settlement"),
            observed_at=observed_at,
        )

        self.assertEqual(ranked[0]["canonical_asset"], "BBB")
        self.assertGreater(ranked[0]["quick_net_rate"], 0)

    def test_pair_direction_uses_first_settlement_cash_flow(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        lower_hourly = market(
            "venue-a", "AAA-A", "AAA", 0.0008, 8, observed_at
        )
        higher_hourly = market(
            "venue-b", "AAA-B", "AAA", 0.0016, 8, observed_at
        )
        lower_hourly["next_funding_at"] = (
            observed + timedelta(hours=1)
        ).isoformat()
        higher_hourly["next_funding_at"] = (
            observed + timedelta(hours=6)
        ).isoformat()
        for row in (lower_hourly, higher_hourly):
            row["maker_fee_rate"] = 0.0
            row["taker_fee_rate"] = 0.0

        ranked = rank_perp_pairs(
            [lower_hourly, higher_hourly],
            None,
            config=FundingScanConfig(
                horizon_mode="next_settlement",
                basis_reserve_bps=0,
                operations_buffer_bps=0,
            ).validated(),
            observed_at=observed_at,
        )

        self.assertEqual(ranked[0]["long_market"]["venue"], "venue-b")
        self.assertEqual(ranked[0]["short_market"]["venue"], "venue-a")
        self.assertLess(ranked[0]["current_hourly_spread"], 0)
        self.assertAlmostEqual(ranked[0]["quick_gross_rate"], 0.0008)

    def test_pair_preselection_can_choose_spread_led_direction_over_bad_funding(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        observed = datetime.fromisoformat(observed_at)
        cheap_high_funding = market(
            "cheap", "AAA-CHEAP", "AAA", 0.0020, 1, observed_at
        )
        rich_low_funding = market(
            "rich", "AAA-RICH", "AAA", 0.0, 1, observed_at
        )
        for row, price in (
            (cheap_high_funding, 100.0),
            (rich_low_funding, 100.6),
        ):
            row["next_funding_at"] = (observed + timedelta(hours=1)).isoformat()
            row["mark_price"] = price
            row["index_price"] = price
            row["maker_fee_rate"] = 0.0
            row["taker_fee_rate"] = 0.0

        config = FundingScanConfig(
            horizon_mode="next_settlement",
            basis_reserve_bps=0,
            operations_buffer_bps=0,
        ).validated()
        ranked = rank_perp_pairs(
            [cheap_high_funding, rich_low_funding],
            None,
            config=config,
            observed_at=observed_at,
        )

        self.assertEqual(ranked[0]["long_market"]["venue"], "cheap")
        self.assertEqual(ranked[0]["short_market"]["venue"], "rich")
        self.assertLess(ranked[0]["quick_gross_rate"], 0.0)
        self.assertGreater(ranked[0]["quick_signed_spread_rate"], 0.0)
        self.assertGreater(ranked[0]["quick_opportunity_best_case_net_rate"], 0.0)

        selected = execution_shortlist(ranked, near_miss_limit=0, config=config)

        self.assertEqual(selected, [ranked[0]])
        self.assertEqual(
            ranked[0]["execution_screen_reason"],
            "spread_opportunity_full_depth",
        )

    def test_missing_quick_reference_price_is_unknown_not_unit_mismatch(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        missing_price_market = market(
            "needs-book", "AAA", "AAA", 0.0, 1, observed_at
        )
        priced_market = market("priced", "AAAUSDT", "AAA", 0.0015, 1, observed_at)
        missing_price_market["mark_price"] = None
        missing_price_market["index_price"] = None

        self.assertIsNone(
            quick_price_identity_gap(missing_price_market, priced_market)
        )
        self.assertEqual(
            quick_signed_spread_rate(missing_price_market, priced_market),
            0.0,
        )

        ranked = rank_perp_pairs(
            [missing_price_market, priced_market],
            None,
            config=FundingScanConfig(horizon_mode="next_settlement").validated(),
            observed_at=observed_at,
        )

        self.assertEqual(len(ranked), 1)
        self.assertFalse(ranked[0]["quick_identity_mismatch"])

    def test_execution_shortlist_keeps_spread_opportunity_with_negative_funding(self) -> None:
        candidate = {
            "quick_schedule_ready": True,
            "quick_gross_rate": -0.001,
            "quick_signed_spread_rate": 0.006,
            "quick_opportunity_gross_rate": 0.005,
            "quick_best_case_cost_rate": 0.001,
            "quick_opportunity_best_case_net_rate": 0.004,
            "quick_best_case_net_rate": 0.004,
        }

        selected = execution_shortlist(
            [candidate],
            near_miss_limit=0,
            config=FundingScanConfig(),
        )

        self.assertEqual(selected, [candidate])
        self.assertEqual(
            candidate["execution_screen_reason"],
            "spread_opportunity_full_depth",
        )

    def test_execution_shortlist_sends_wide_spread_to_full_depth(self) -> None:
        candidate = {
            "quick_schedule_ready": True,
            "quick_gross_rate": -0.001,
            "quick_signed_spread_rate": 0.20,
            "quick_opportunity_gross_rate": 0.199,
            "quick_best_case_cost_rate": 0.197,
            "quick_opportunity_best_case_net_rate": 0.002,
            "quick_best_case_net_rate": 0.002,
            "quick_liquidity_score": 0.0,
        }

        selected = execution_shortlist(
            [candidate],
            near_miss_limit=0,
            config=FundingScanConfig(target_notional=500),
        )

        self.assertEqual(selected, [candidate])
        self.assertEqual(
            candidate["execution_screen_reason"],
            "spread_opportunity_full_depth",
        )

    def test_execution_shortlist_keeps_tiny_spread_edge_out_of_strict_full_depth(self) -> None:
        candidate = {
            "quick_schedule_ready": True,
            "quick_gross_rate": -0.0001,
            "quick_signed_spread_rate": 0.00035,
            "quick_opportunity_gross_rate": 0.00025,
            "quick_best_case_cost_rate": 0.0001,
            "quick_opportunity_best_case_net_rate": 0.00015,
            "quick_best_case_net_rate": 0.00015,
        }

        selected = execution_shortlist(
            [candidate],
            near_miss_limit=0,
            config=FundingScanConfig(target_notional=500),
        )

        self.assertEqual(selected, [])
        self.assertEqual(
            candidate["execution_screen_reason"],
            "spread_opportunity_below_actionable_threshold",
        )

    def test_paper_revalidation_never_extrapolates_beyond_repriced_size(self) -> None:
        previous = {
            "funding_route_id": 7,
            "route_key": "route",
            "target_notional": 10_000,
            "expected_net_profit": 250,
            "observed_at": "2026-07-14T12:00:00+00:00",
        }
        current = {
            "route_key": "route",
            "target_notional": 1_000,
            "market_capacity": 100_000,
            "expected_net_profit": 20,
            "status": "paper_candidate",
            "observed_at": "2026-07-14T12:01:00+00:00",
        }

        result = simulate_paper_revalidations([previous], [current], 1_000)[0]

        self.assertEqual(result["filled_notional"], 1_000)
        self.assertEqual(result["fill_ratio"], 0.1)
        self.assertEqual(result["repriced_net_profit"], 20)
        self.assertEqual(result["latency_ms"], 60_000)

    def test_paper_revalidation_uses_current_route_id_when_available(self) -> None:
        previous = {
            "funding_route_id": 7,
            "route_key": "route",
            "target_notional": 500,
            "expected_net_profit": 5,
            "observed_at": "2026-07-14T12:00:00+00:00",
        }
        current = {
            "funding_route_id": 99,
            "route_key": "route",
            "target_notional": 500,
            "expected_net_profit": 6,
            "status": "paper_candidate",
            "observed_at": "2026-07-14T12:00:10+00:00",
        }

        result = simulate_paper_revalidations([previous], [current], 1_000)[0]

        self.assertEqual(result["funding_route_id"], 99)
        self.assertEqual(result["status"], "revalidated_profitable")

    def test_paper_route_is_reauthorized_after_a_crossed_settlement(self) -> None:
        previous = {
            "funding_route_id": 8,
            "route_key": "route",
            "target_notional": 1_000,
            "expected_net_profit": 25,
            "observed_at": "2026-07-14T12:00:00+00:00",
            "legs": [
                {
                    "venue": "hyperliquid",
                    "symbol": "BTC",
                    "next_funding_at": "2026-07-14T13:00:00+00:00",
                }
            ],
        }
        current = {
            "route_key": "route",
            "target_notional": 1_000,
            "expected_net_profit": 21,
            "status": "paper_candidate",
            "observed_at": "2026-07-14T13:00:05+00:00",
            "evidence": {
                "horizon": {
                    "authorization_valid_until": "2026-07-14T14:00:00+00:00"
                }
            },
        }

        result = simulate_paper_revalidations([previous], [current], 1_000)[0]

        self.assertEqual(result["status"], "settlement_reauthorized")
        self.assertTrue(result["result"]["reauthorization_required"])
        self.assertEqual(len(result["result"]["settlements_crossed"]), 1)

    def test_bybit_adapter_normalizes_public_linear_market(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BybitFundingClient(http=FakeBybitHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at)
        history = client.funding_history("BTCUSDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual({row["canonical_asset"] for row in instruments}, {"BTC"})
        self.assertEqual({row["canonical_asset"] for row in markets}, {"BTC"})
        self.assertEqual(client.non_crypto_assets, {"MRVL"})
        self.assertEqual(markets[0]["venue"], "bybit")
        self.assertEqual(markets[0]["funding_interval_hours"], 4)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 4)
        self.assertEqual(book_row["best_bid"], 99.9)
        self.assertEqual(len(history), 1)

    def test_binance_adapter_keeps_published_period_rate_and_hourly_rate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BinanceFundingClient(http=FakeBinanceHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "binance")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0008)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0008 / 8)
        self.assertAlmostEqual(markets[0]["funding_rate_cap"], 0.003)
        self.assertAlmostEqual(markets[0]["funding_rate_floor"], -0.003)

    def test_hyperliquid_adapter_normalizes_predicted_period_rate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = HyperliquidFundingClient(http=FakeHyperliquidHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "hyperliquid")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0008)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0008 / 8)

    def test_backpack_adapter_filters_unsettled_history_and_reads_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BackpackFundingClient(http=FakeBackpackHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC_USDC_PERP", observed_at)
        history = client.funding_history("BTC_USDC_PERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "backpack")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0005)
        self.assertEqual(book_row["best_bid"], 99.9)
        self.assertEqual(len(history), 1)
        self.assertLessEqual(history[0]["funding_at"], observed_at)

    def test_aster_adapter_filters_stocks_and_uses_public_fee_tier(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = AsterFundingClient(http=FakeAsterHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at)
        history = client.funding_history("BTCUSDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "aster")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertAlmostEqual(markets[0]["funding_rate_cap"], 0.003)
        self.assertAlmostEqual(markets[0]["funding_rate_floor"], -0.003)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0004)
        self.assertEqual(markets[0]["maker_fee_rate"], 0.0)
        self.assertEqual(book_row["best_bid"], 99.9)
        self.assertEqual(len(history), 1)

    def test_lighter_adapter_normalizes_percent_history_and_aggregates_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = LighterFundingClient(http=FakeLighterHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC", observed_at)
        history = client.funding_history("BTC", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "lighter")
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0)
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], -0.0009)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], -0.0009)
        self.assertAlmostEqual(markets[0]["published_funding_rate"], -0.0072)
        self.assertEqual(markets[0]["published_funding_interval_hours"], 8)
        self.assertEqual(
            markets[0]["funding_rate_kind"],
            "published_8h_equivalent_normalized_hourly",
        )
        self.assertAlmostEqual(book_row["bids"][0][1], 3.0)
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]["funding_rate"], -0.000012)

    def test_variational_adapter_keeps_interval_rate_and_hourly_rate(self) -> None:
        class FakeVariationalHttp:
            def get_json(self, url: str) -> dict[str, Any]:
                assert "/metadata/stats" in url
                return {
                    "listings": [
                        {
                            "ticker": "BTC",
                            "mark_price": "100000",
                            "funding_rate": "0.031145",
                            "funding_interval_s": "28800",
                            "open_interest": {
                                "long_open_interest": "1.5",
                                "short_open_interest": "1.0",
                            },
                            "volume_24h": "1000000",
                            "quotes": {},
                        }
                    ]
                }

        observed_at = "2026-07-14T12:00:00+00:00"
        client = VariationalFundingClient(http=FakeVariationalHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "variational")
        self.assertEqual(markets[0]["environment"], "mainnet")
        self.assertTrue(markets[0]["environment_verified"])
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.00031145)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.00031145 / 8)
        self.assertEqual(
            markets[0]["funding_rate_kind"],
            "published_current_interval_estimate",
        )
        self.assertEqual(markets[0]["execution_model"], "RFQ")
        self.assertFalse(markets[0]["orderbook_depth_available"])
        self.assertEqual(markets[0]["fee_source"], "fee_model_missing")
        self.assertTrue(markets[0]["fee_model_missing"])
        self.assertNotIn("taker_fee_rate", markets[0])
        with self.assertRaises(FundingDataError):
            client.orderbook("BTC", observed_at)

    def test_funding_rate_unit_outlier_blocks_implausible_units(self) -> None:
        self.assertTrue(
            market_funding_rate_unit_outlier(
                {"funding_rate": -0.22, "hourly_funding_rate": -0.22}
            )
        )
        self.assertFalse(
            market_funding_rate_unit_outlier(
                {"funding_rate": -0.052, "hourly_funding_rate": -0.0065}
            )
        )

    def test_kucoin_adapter_converts_contract_depth_and_xbt_alias(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = KuCoinFundingClient(http=FakeKuCoinHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("XBTUSDTM", observed_at)
        history = client.funding_history("XBTUSDTM", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["venue"], "kucoin")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0006)
        self.assertAlmostEqual(book_row["bids"][0][1], 0.002)
        self.assertEqual(len(history), 1)

    def test_mexc_adapter_uses_bulk_schedule_and_converts_contract_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = MEXCFundingClient(http=FakeMEXCHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC_USDT", observed_at)
        history = client.funding_history("BTC_USDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "mexc")
        self.assertEqual(markets[0]["funding_interval_hours"], 4)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0004 / 4)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0002)
        self.assertAlmostEqual(book_row["bids"][0][1], 0.002)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["funding_interval_hours"], 4)

    def test_paradex_adapter_normalizes_continuous_funding_to_completed_hours(self) -> None:
        observed_at = "2026-07-14T11:45:00+00:00"
        client = ParadexFundingClient(http=FakeParadexHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USD-PERP", observed_at)
        history = client.funding_history("BTC-USD-PERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "paradex")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0001)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0002)
        self.assertEqual(book_row["best_bid"], 99.9)
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]["funding_rate"], 0.00015)
        self.assertEqual(history[0]["raw"]["sample_count"], 2)

    def test_round_trip_fees_charge_each_leg_open_and_close_once(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        forecast = {
            "_scenario_outcomes": [{"gross_rate": 0.01, "weight": 1.0}],
            "_historical_outcomes": [],
            "gross_rate_median": 0.01,
            "settlements": [{"forecast_event_rate": 0.01}],
        }

        result = notional_economics(
            500,
            book("binance", "BTCUSDT", observed_at),
            book("aster", "BTCUSDT", observed_at),
            0.0005,
            0.0004,
            forecast,
            8,
            0,
            FundingScanConfig(
                target_notional=500,
                operations_buffer_bps=0,
                basis_reserve_bps=0,
            ).validated(),
        )

        self.assertAlmostEqual(result["total_fees"], 0.9)
        self.assertAlmostEqual(result["expected_gross_funding"], 5.0)

    def test_sizing_hedges_equal_base_quantity_when_venue_prices_differ(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        forecast = {
            "_scenario_outcomes": [{"gross_rate": 0.05, "weight": 1.0}],
            "_historical_outcomes": [],
            "gross_rate_median": 0.05,
            "gross_rate_q25": 0.05,
            "settlements": [{"forecast_event_rate": 0.05}],
        }

        result = notional_economics(
            500,
            shifted_book("long", "BTC", observed_at, 100),
            shifted_book("short", "BTC", observed_at, 102),
            0,
            0,
            forecast,
            8,
            0,
            FundingScanConfig(
                target_notional=500,
                operations_buffer_bps=0,
                basis_reserve_bps=0,
            ).validated(),
        )

        self.assertAlmostEqual(
            result["long_open"]["filled_size"],
            result["short_open"]["filled_size"],
        )
        self.assertGreater(
            result["short_open_notional"],
            result["long_open_notional"],
        )

    def test_depth_walk_uses_multiple_levels_and_computes_vwap(self) -> None:
        asks = [[5.0, 2.0], [5.11, 1_000 / 5.11]]

        fill = fill_notional(asks, 500)
        capacity = displayed_side_capacity(asks)

        self.assertAlmostEqual(fill["filled_notional"], 500)
        self.assertAlmostEqual(fill["vwap"], 5.107752588860902)
        self.assertAlmostEqual(fill["book_walk_bps"], 220)
        self.assertEqual(fill["levels_consumed"], 2)
        self.assertAlmostEqual(capacity, 1_010)

    def test_deep_book_walk_is_visible_but_not_a_capacity_prefilter(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        deep_book = {
            "venue": "test",
            "symbol": "BTC",
            "observed_at": observed_at,
            "bids": [[4.99, 2.0], [4.88, 1_000 / 4.88]],
            "asks": [[5.0, 2.0], [5.11, 1_000 / 5.11]],
            "best_bid": 4.99,
            "best_ask": 5.0,
            "mid_price": 4.995,
            "bid_depth_usd": 1_009.98,
            "ask_depth_usd": 1_010.0,
            "raw": {},
        }

        route = evaluate_perp_route(
            market("binance", "BTCUSDT", "BTC", 0.0, 8, observed_at),
            market("lighter", "BTC", "BTC", 0.05, 1, observed_at),
            {**deep_book, "venue": "binance", "symbol": "BTCUSDT"},
            {**deep_book, "venue": "lighter"},
            history_rows("binance", "BTCUSDT", now, 8, 0.0, 90),
            history_rows("lighter", "BTC", now, 1, 0.05, 720),
            observed_at,
            FundingScanConfig(
                target_notional=500,
                horizon_mode="fixed",
                horizon_hours=24,
                minimum_history_points=12,
            ),
        )

        self.assertGreaterEqual(route["market_capacity"], 500)
        self.assertGreater(route["evidence"]["max_book_walk_bps"], 200)
        self.assertIn("deep_book_walk_dependency", route["risk_flags"])

    def test_drift_requires_fresh_funding_and_real_dlob_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = DriftFundingClient(
            http=FakeDriftHttp(),
            dlob_url="https://drift-dlob.test",
            symbols=("BTC-PERP",),
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-PERP", observed_at)
        history = client.funding_history("BTC-PERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0012 / 100)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 2)

    def test_ethereal_adapter_uses_products_depth_and_hourly_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = EtherealFundingClient(
            http=FakeEtherealHttp(),
            base_url="https://ethereal.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSD", observed_at, limit=2)
        history = client.funding_history("BTCUSD", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "ethereal")
        self.assertEqual(markets[0]["symbol"], "BTCUSD")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.000013)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.000013)
        self.assertAlmostEqual(markets[0]["mark_price"], 100.0)
        self.assertAlmostEqual(markets[0]["index_price"], 99.8)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.0003)
        self.assertAlmostEqual(markets[0]["maker_fee_rate"], 0.0)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(book_row["asks"][0], [100.1, 3.0])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["funding_interval_hours"], 1)
        self.assertAlmostEqual(history[-1]["hourly_funding_rate"], 0.000012)

    def test_extended_adapter_uses_crypto_markets_depth_and_hourly_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = ExtendedFundingClient(
            http=FakeExtendedHttp(),
            base_url="https://extended.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USD", observed_at, limit=2)
        history = client.funding_history("BTC-USD", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "extended")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.000013)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.000013)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.00025)
        self.assertAlmostEqual(markets[0]["maker_fee_rate"], 0.0)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(book_row["asks"][0], [100.1, 3.0])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["funding_interval_hours"], 1)
        self.assertAlmostEqual(history[-1]["hourly_funding_rate"], 0.000012)

    def test_grvt_adapter_uses_per_instrument_ticker_and_nanosecond_timestamps(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = GrvtFundingClient(
            http=FakeGrvtHttp(),
            base_url="https://grvt.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC_USDT_Perp", observed_at, limit=2)
        history = client.funding_history("BTC_USDT_Perp", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "grvt")
        self.assertEqual(markets[0]["symbol"], "BTC_USDT_Perp")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0003 / 100)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0003 / 100 / 8)
        self.assertAlmostEqual(markets[0]["mark_price"], 65038.01)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.00045)
        self.assertEqual(book_row["bids"][0], [65038.01, 3456.78])
        self.assertEqual(book_row["asks"][0], [65038.02, 1234.56])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(history[0]["funding_rate"], 0.0002 / 100)
        self.assertAlmostEqual(history[0]["hourly_funding_rate"], 0.0002 / 100 / 8)

    def test_edgex_adapter_uses_contract_id_ticker_and_4h_funding(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = EdgexFundingClient(
            http=FakeEdgexHttp(),
            base_url="https://edgex.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDC", observed_at, limit=2)
        history = client.funding_history("BTCUSDC", 1, 4, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "edgex")
        self.assertEqual(markets[0]["symbol"], "BTCUSDC")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 4)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.00005)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.00005 / 4)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.00038)
        self.assertEqual(book_row["bids"][0], [64214.3, 1.710])
        self.assertEqual(book_row["asks"][0], [64214.5, 0.032])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["funding_interval_hours"], 4)

    def test_apex_adapter_uses_v3_hourly_funding_and_dash_symbols(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = ApexFundingClient(
            http=FakeApexHttp(),
            base_url="https://apex.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USDT", observed_at, limit=2)
        history = client.funding_history("BTC-USDT", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "apex")
        self.assertEqual(markets[0]["symbol"], "BTC-USDT")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], -0.00001397)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], -0.00001397)
        self.assertEqual(book_row["bids"][0], [64365.2, 4.371])
        self.assertEqual(book_row["asks"][0], [64365.8, 5.112])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["funding_interval_hours"], 1)

    def test_pacifica_adapter_uses_info_endpoint_and_book_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = PacificaFundingClient(
            http=FakePacificaHttp(),
            base_url="https://pacifica.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC", observed_at, limit=2)
        history = client.funding_history("BTC", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "pacifica")
        self.assertEqual(markets[0]["symbol"], "BTC")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0000125)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0000125)
        self.assertEqual(
            markets[0]["funding_rate_kind"],
            "published_next_hour_estimate",
        )
        self.assertEqual(
            markets[0]["next_funding_at"],
            "2026-07-14T13:00:00+00:00",
        )
        self.assertEqual(
            markets[0]["mark_price_kind"],
            "info_prices_mark",
        )
        self.assertEqual(markets[0]["index_price_kind"], "info_prices_oracle")
        self.assertAlmostEqual(markets[0]["mark_price"], 64132.5)
        self.assertAlmostEqual(markets[0]["index_price"], 64131.9)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.0005)
        self.assertEqual(
            markets[0]["fee_source"],
            "info/fees_public_fee_levels_conservative_max",
        )
        self.assertEqual(book_row["bids"][0], [64132.0, 0.785])
        self.assertEqual(book_row["asks"][0], [64133.0, 1.234])
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]["funding_rate"], 0.0000125)

    def test_pacifica_missing_fee_endpoint_is_not_zero_fee(self) -> None:
        class MissingFeeHttp(FakePacificaHttp):
            def get_json(self, url: str) -> Any:
                if "/info/fees" in url:
                    raise FundingDataError("fee endpoint unavailable")
                return super().get_json(url)

        observed_at = "2026-07-14T12:00:00+00:00"
        client = PacificaFundingClient(http=MissingFeeHttp())

        _instruments, markets, warnings = client.catalog_and_markets(observed_at)

        self.assertTrue(any("fee levels unavailable" in row for row in warnings))
        self.assertNotIn("taker_fee_rate", markets[0])
        self.assertEqual(markets[0]["fee_source"], "fee_model_missing")
        self.assertTrue(markets[0]["fee_model_missing"])

    def test_pacifica_full_depth_uses_orderbook_mid_as_reference_proxy(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = PacificaFundingClient(
            http=FakePacificaHttp(),
            base_url="https://pacifica.test",
        )
        _, markets, _ = client.catalog_and_markets(observed_at)
        pacifica_market = markets[0]
        pacifica_book = client.orderbook("BTC", observed_at, limit=2)
        short_market = market(
            "binance",
            "BTCUSDT",
            "BTC",
            0.0002,
            1,
            observed_at,
        )
        short_market["next_funding_at"] = pacifica_market["next_funding_at"]
        short_market["mark_price"] = 64132.5
        short_market["index_price"] = 64132.5
        short_book = shifted_book("binance", "BTCUSDT", observed_at, 64132.5)

        route = evaluate_perp_route(
            pacifica_market,
            short_market,
            pacifica_book,
            short_book,
            [],
            [],
            observed_at,
            FundingScanConfig(
                target_notional=500,
                minimum_market_capacity=500,
                minimum_history_points=0,
                minimum_liquidity_snapshots=0,
                basis_reserve_bps=0,
                operations_buffer_bps=0,
                horizon_mode="next_settlement",
            ).validated(),
        )

        self.assertNotIn("missing_reference_price", route["risk_flags"])
        self.assertEqual(
            route["evidence"]["reference_price_kinds"]["pacifica"],
            "info_prices_mark",
        )

    def test_nado_adapter_uses_gateway_and_archive_public_endpoints(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = NadoFundingClient(http=FakeNadoHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-PERP_USDT0", observed_at, limit=2)
        history = client.funding_history("BTC-PERP_USDT0", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "nado")
        self.assertEqual(markets[0]["symbol"], "BTC-PERP_USDT0")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["environment"], "mainnet")
        self.assertTrue(markets[0]["environment_verified"])
        self.assertEqual(markets[0]["raw_api_rate"], "24000000000000000")
        self.assertEqual(markets[0]["raw_rate_scale"], "1e18")
        self.assertEqual(markets[0]["displayed_rate_period_seconds"], 86400.0)
        self.assertEqual(markets[0]["settlement_interval_seconds"], 3600.0)
        self.assertAlmostEqual(float(markets[0]["funding_rate"]), 0.001)
        self.assertEqual(markets[0]["funding_rate_semantics"], "unclear")
        self.assertEqual(markets[0]["fee_source"], "fee_model_missing")
        self.assertTrue(markets[0]["fee_model_missing"])
        self.assertEqual(book_row["bids"][0], [116215.0, 0.128])
        self.assertEqual(book_row["asks"][0], [116225.0, 0.043])
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(float(history[0]["funding_rate"]), -0.000313157879073748)
        self.assertEqual(history[0]["raw_rate_unit"], "funding_rate_x18_hourly")

    def test_nado_rate_normalization_uses_decimal_x18(self) -> None:
        self.assertEqual(
            str(nado_rate_x18_to_decimal("24000000000000000")),
            "0.024",
        )
        self.assertEqual(
            str(nado_rate_x18_to_decimal("-697407056090986")),
            "-0.000697407056090986",
        )

    def test_reya_adapter_uses_summary_funding_and_empty_book(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = ReyaFundingClient(
            http=FakeReyaHttp(),
            base_url="https://reya.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCRUSDPERP", observed_at, limit=2)
        history = client.funding_history("BTCRUSDPERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "reya")
        self.assertEqual(markets[0]["symbol"], "BTCRUSDPERP")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.00127969 / 100)
        self.assertAlmostEqual(markets[0]["mark_price"], 64349.79)
        self.assertEqual(book_row["bids"], [])
        self.assertEqual(book_row["asks"], [])
        self.assertEqual(history, [])

    def test_risex_adapter_uses_current_interval_rate_and_depth(self) -> None:
        observed_at = "2026-07-14T12:30:00+00:00"
        client = RiseXFundingClient(
            http=FakeRiseXHttp(),
            base_url="https://risex.test",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC/USDC", observed_at, limit=2)
        history = client.funding_history("BTC/USDC", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "risex")
        self.assertEqual(markets[0]["symbol"], "BTC/USDC")
        self.assertEqual(markets[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], -0.001)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], -0.001)
        self.assertAlmostEqual(markets[0]["published_funding_rate"], -0.008)
        self.assertEqual(markets[0]["published_funding_interval_hours"], 8)
        self.assertEqual(
            markets[0]["funding_display_note"],
            "published 8h equivalent; cashflow 1h",
        )
        self.assertEqual(markets[0]["raw"]["funding_rate_8h"], "-0.008")
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertAlmostEqual(markets[0]["maker_fee_rate"], 0.0001)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.0003)
        self.assertEqual(book_row["bids"][0], [100.0, 2.0])
        self.assertEqual(book_row["asks"][0], [100.5, 1.5])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(history[0]["funding_rate"], -0.0008)
        self.assertAlmostEqual(history[0]["hourly_funding_rate"], -0.0008)

    def test_woox_adapter_uses_batch_funding_depth_and_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = WOOXFundingClient(http=FakeWOOXHttp(), base_url="https://woox.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("PERP_BTC_USDT", observed_at, limit=2)
        history = client.funding_history("PERP_BTC_USDT", 1, 4, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "woox")
        self.assertEqual(markets[0]["funding_interval_hours"], 4)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0004)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["funding_interval_hours"], 4)

    def test_coinex_adapter_derives_dynamic_interval_and_reads_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = CoinExFundingClient(http=FakeCoinExHttp(), base_url="https://coinex.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at, limit=2)
        history = client.funding_history("BTCUSDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "coinex")
        self.assertEqual(markets[0]["funding_interval_hours"], 4)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0004 / 4)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0005)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 2)

    def test_bitunix_adapter_converts_percent_funding_rate(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BitunixFundingClient(http=FakeBitunixHttp(), base_url="https://bitunix.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at, limit=2)
        snapshot = client.market_snapshot("BTCUSDT", "BTC", observed_at, markets[0])

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "bitunix")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.007459 / 100)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.007459 / 100 / 8)
        self.assertAlmostEqual(markets[0]["funding_rate_cap"], 0.3 / 100)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertAlmostEqual(snapshot["funding_rate"], 0.007459 / 100)

    def test_bitmart_adapter_converts_contract_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BitMartFundingClient(http=FakeBitMartHttp(), base_url="https://bitmart.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at, limit=2)
        history = client.funding_history("BTCUSDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["contract_multiplier"], 0.001)
        self.assertEqual(markets[0]["venue"], "bitmart")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0008 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 0.002])
        self.assertEqual(len(history), 2)

    def test_blofin_adapter_filters_inverse_and_converts_contract_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BloFinFundingClient(http=FakeBloFinHttp(), base_url="https://blofin.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USDT", observed_at, limit=2)
        history = client.funding_history("BTC-USDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["contract_multiplier"], 0.001)
        self.assertEqual(markets[0]["venue"], "blofin")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0008 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 0.002])
        self.assertEqual(len(history), 2)

    def test_phemex_adapter_computes_next_boundary_and_reads_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = PhemexFundingClient(http=FakePhemexHttp(), base_url="https://phemex.test")

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at, limit=2)
        snapshot = client.market_snapshot("BTCUSDT", "BTC", observed_at, markets[0])

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "phemex")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T16:00:00+00:00")
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0008 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(snapshot["next_funding_at"], "2026-07-14T16:00:00+00:00")

    def test_aevo_adapter_uses_hourly_funding_and_nanosecond_timestamp(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = AevoFundingClient(
            http=FakeAevoHttp(),
            base_url="https://aevo.test",
            max_funding_workers=2,
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-PERP", observed_at, limit=2)
        history = client.funding_history("BTC-PERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "aevo")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 2)

    def test_account_fee_override_replaces_public_tier(self) -> None:
        row = {"venue": "binance", "taker_fee_rate": 0.0005}
        with patch.dict(
            os.environ,
            {"FUNDING_BINANCE_TAKER_FEE": "0.00031"},
            clear=False,
        ):
            self.assertEqual(market_fee_rate(row), 0.00031)

    def test_bitget_adapter_uses_public_fee_interval_depth_and_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BitgetFundingClient(http=FakeBitgetHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTCUSDT", observed_at)
        history = client.funding_history("BTCUSDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "bitget")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0006)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 1)

    def test_gate_adapter_converts_contract_depth_and_filters_stock_contracts(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = GateFundingClient(http=FakeGateHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC_USDT", observed_at)
        history = client.funding_history(
            "BTC_USDT",
            1_783_000_000_000,
            8,
            observed_at,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["contract_multiplier"], 0.01)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.00075)
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 1.0])
        self.assertAlmostEqual(book_row["bid_depth_usd"], 99.9)
        self.assertEqual(len(history), 1)

    def test_bingx_adapter_reads_public_perp_market_depth_and_history(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = BingXFundingClient(http=FakeBingXHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USDT", observed_at)
        history = client.funding_history("BTC-USDT", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "bingx")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0005)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 1)

    def test_htx_adapter_uses_bulk_catalog_and_converts_contract_depth(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = HTXFundingClient(http=FakeHTXHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USDT", observed_at)
        history = client.funding_history(
            "BTC-USDT",
            1_783_000_000_000,
            8,
            observed_at,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["contract_multiplier"], 0.001)
        self.assertEqual(markets[0]["venue"], "htx")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertAlmostEqual(markets[0]["mark_price"], 100)
        self.assertEqual(book_row["bids"][0], [99.9, 1.0])
        self.assertEqual(len(history), 1)

    def test_okx_adapter_converts_contract_depth_to_base_units(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = OKXFundingClient(http=FakeOKXHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USDT-SWAP", observed_at)
        history = client.funding_history("BTC-USDT-SWAP", 1, 8, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual({row["canonical_asset"] for row in instruments}, {"BTC"})
        self.assertEqual({row["canonical_asset"] for row in markets}, {"BTC"})
        self.assertEqual(client.non_crypto_assets, {"MRVL"})
        self.assertEqual(instruments[0]["contract_multiplier"], 0.01)
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001 / 8)
        self.assertEqual(book_row["bids"][0], [99.9, 1.0])
        self.assertAlmostEqual(book_row["bid_depth_usd"], 99.9)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["funding_interval_hours"], 8)

    def test_okx_websocket_collects_one_full_funding_snapshot(self) -> None:
        connection = FakeOKXWebSocket()
        with patch(
            "smart_money_radar.funding.adapters.okx.websocket.create_connection",
            return_value=connection,
        ):
            rows = okx_funding_snapshot(
                ["ETH-USDT-SWAP", "BTC-USDT-SWAP"],
                timeout_seconds=1,
            )

        self.assertEqual(set(rows), {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
        self.assertEqual(rows["BTC-USDT-SWAP"]["fundingRate"], "0.0001")
        self.assertIn('"id":"fundingsnapshot"', connection.sent)
        self.assertTrue(connection.closed)

    def test_dydx_adapter_uses_hourly_funding_and_base_size_book(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        client = DydxFundingClient(http=FakeDydxHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-USD", observed_at)
        history = client.funding_history("BTC-USD", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertIsNone(markets[0]["mark_price"])
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["hourly_funding_rate"], 0.0001)

    def test_kraken_adapter_uses_hourly_prediction_and_sorts_depth(self) -> None:
        observed_at = "2026-07-14T12:15:00+00:00"
        client = KrakenFundingClient(http=FakeKrakenHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("PF_XBTUSD", observed_at, limit=1)
        history = client.funding_history("PF_XBTUSD", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["venue"], "kraken")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.8 / 100)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.8 / 100)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertEqual(book_row["bids"], [[99.9, 2.0]])
        self.assertEqual(book_row["asks"], [[100.1, 3.0]])
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(history[-1]["hourly_funding_rate"], 0.00012)

    def test_kraken_relative_prediction_takes_precedence_over_absolute_prediction(self) -> None:
        rate = kraken_relative_funding_rate(
            {
                "relativeFundingRate": "0.00010",
                "relativeFundingRatePrediction": "0.00020",
                "fundingRatePrediction": "5",
            },
            mark_price=100,
            index_price=100,
        )

        self.assertAlmostEqual(rate, 0.00020)

    def test_kraken_absolute_prediction_is_normalized_by_mark_price(self) -> None:
        rate = kraken_relative_funding_rate(
            {"fundingRatePrediction": "0.8"},
            mark_price=100,
            index_price=100,
        )

        self.assertAlmostEqual(rate, 0.008)

    def test_deribit_adapter_uses_linear_usdc_perps_and_hourly_history(self) -> None:
        observed_at = "2026-07-14T12:15:00+00:00"
        client = DeribitFundingClient(http=FakeDeribitHttp())

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC_USDC-PERPETUAL", observed_at)
        history = client.funding_history(
            "BTC_USDC-PERPETUAL",
            1_784_016_000_000,
            8,
            observed_at,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(markets[0]["venue"], "deribit")
        self.assertEqual(markets[0]["funding_interval_hours"], 8)
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0008)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T16:00:00+00:00")
        self.assertEqual(markets[0]["maker_fee_rate"], 0.0)
        self.assertEqual(markets[0]["taker_fee_rate"], 0.0005)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["funding_interval_hours"], 8.0)
        self.assertAlmostEqual(history[0]["funding_rate"], 0.00088)
        self.assertAlmostEqual(history[0]["hourly_funding_rate"], 0.00011)

    def test_vertex_base_adapter_normalizes_x18_funding_fees_and_depth(self) -> None:
        observed_at = "2026-07-14T12:15:00+00:00"
        client = VertexFundingClient(
            http=FakeVertexHttp(),
            gateway_url="https://vertex-gateway.test/v1",
            indexer_url="https://vertex-indexer.test/v1",
        )

        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        book_row = client.orderbook("BTC-PERP", observed_at)
        history = client.funding_history("BTC-PERP", 1, 1, observed_at)

        self.assertEqual(warnings, [])
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]["venue"], "vertex_base")
        self.assertEqual(instruments[0]["canonical_asset"], "BTC")
        self.assertEqual(markets[0]["funding_interval_hours"], 1)
        self.assertEqual(markets[0]["next_funding_at"], "2026-07-14T13:00:00+00:00")
        self.assertAlmostEqual(markets[0]["funding_rate"], 0.0001)
        self.assertAlmostEqual(markets[0]["hourly_funding_rate"], 0.0001)
        self.assertAlmostEqual(markets[0]["index_price"], 100.0)
        self.assertAlmostEqual(markets[0]["open_interest_usd"], 200.0)
        self.assertAlmostEqual(markets[0]["taker_fee_rate"], 0.0003)
        self.assertAlmostEqual(markets[0]["maker_fee_rate"], 0.0)
        self.assertEqual(book_row["bids"][0], [99.9, 2.0])
        self.assertEqual(book_row["asks"][0], [100.1, 3.0])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["funding_interval_hours"], 1)
        self.assertAlmostEqual(history[-1]["hourly_funding_rate"], 0.00012)
        self.assertEqual(client.http.symbol_calls, 1)

    def test_vertex_response_fails_closed_on_unsuccessful_status(self) -> None:
        with self.assertRaises(FundingDataError):
            vertex_response_data(
                {"status": "failure", "error": "rate limited"},
                "Vertex Base symbols",
            )

    def test_dydx_orderbook_mid_is_used_for_basis_without_mutating_snapshot(self) -> None:
        observed_at = "2026-07-14T12:00:00+00:00"
        now = datetime.fromisoformat(observed_at)
        long_market = market("dydx", "BTC-USD", "BTC", 0.0, 1, observed_at)
        long_market["mark_price"] = None
        long_market["mark_price_kind"] = "orderbook_mid_at_route_evaluation"
        short_market = market("hyperliquid", "BTC", "BTC", 0.0001, 1, observed_at)

        route = evaluate_perp_route(
            long_market,
            short_market,
            book("dydx", "BTC-USD", observed_at),
            book("hyperliquid", "BTC", observed_at),
            history_rows("dydx", "BTC-USD", now, 1, 0.0, 48),
            history_rows("hyperliquid", "BTC", now, 1, 0.0001, 48),
            observed_at,
            FundingScanConfig(minimum_history_points=12),
        )

        self.assertNotIn("missing_reference_price", route["risk_flags"])
        self.assertEqual(route["evidence"]["reference_price_kinds"]["dydx"], "orderbook_mid")
        self.assertIsNone(long_market["mark_price"])

    def test_live_pipeline_persists_routes_and_revalidates_next_scan(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            config = FundingScanConfig(
                max_candidates=2,
                minimum_history_points=6,
                history_refresh_hours=0,
            )
            first = run_funding_scan(
                store,
                config=config,
                binance=FakeClient("binance"),
                hyperliquid=FakeClient("hyperliquid"),
                hydrate_missing_history=True,
            )
            second = run_funding_scan(
                store,
                config=config,
                binance=FakeClient("binance"),
                hyperliquid=FakeClient("hyperliquid"),
                hydrate_missing_history=True,
            )
            dashboard = store.funding_dashboard()

        self.assertEqual(first["paper_candidate_count"], 1)
        self.assertEqual(second["paper_execution_count"], 1)
        self.assertEqual(len(dashboard["routes"]), 1)
        self.assertEqual(dashboard["routes"][0]["canonical_asset"], "BTC")
        self.assertEqual(dashboard["execution_mode"], "paper_only_research")

    def test_live_scan_reuses_recent_orderbook_cache(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            binance = CountingFakeClient("binance")
            hyperliquid = CountingFakeClient("hyperliquid")
            config = FundingScanConfig(
                max_candidates=2,
                minimum_history_points=6,
                history_refresh_hours=0,
                orderbook_cache_ttl_seconds=30,
                market_snapshot_cache_ttl_seconds=30,
            )
            first = run_funding_scan(
                store,
                config=config,
                binance=binance,
                hyperliquid=hyperliquid,
                hydrate_missing_history=False,
            )
            first_orderbook_calls = binance.orderbook_calls + hyperliquid.orderbook_calls
            second = run_funding_scan(
                store,
                config=config,
                binance=binance,
                hyperliquid=hyperliquid,
                hydrate_missing_history=False,
            )

        self.assertEqual(first["orderbook_count"], 2)
        self.assertEqual(second["orderbook_count"], 2)
        self.assertEqual(first["fresh_market_venue_count"], 2)
        self.assertEqual(first["cached_market_venue_count"], 0)
        self.assertEqual(second["fresh_market_venue_count"], 0)
        self.assertEqual(second["cached_market_venue_count"], 2)
        self.assertEqual(first["fresh_orderbook_count"], 2)
        self.assertEqual(first["cached_orderbook_count"], 0)
        self.assertEqual(second["fresh_orderbook_count"], 0)
        self.assertEqual(second["cached_orderbook_count"], 2)
        self.assertEqual(binance.catalog_calls + hyperliquid.catalog_calls, 2)
        self.assertEqual(first_orderbook_calls, 2)
        self.assertEqual(binance.orderbook_calls + hyperliquid.orderbook_calls, 2)
        self.assertTrue(
            any("Reused 2 orderbooks" in warning for warning in second["warnings"])
        )

    def test_one_failed_venue_is_visible_but_does_not_abort_other_pairs(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            result = run_funding_scan(
                store,
                config=FundingScanConfig(
                    max_candidates=2,
                    minimum_history_points=6,
                    history_refresh_hours=0,
                ),
                venue_clients=[
                    FakeClient("binance"),
                    FakeClient("hyperliquid"),
                    FailingClient(),
                ],
            )
            dashboard = store.funding_dashboard()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["available_venues"], ["binance", "hyperliquid"])
        self.assertEqual(len(dashboard["warnings"]), 1)
        self.assertIn("bybit catalog skipped", dashboard["warnings"][0])

    def test_auto_scans_keep_a_bounded_window_without_deleting_manual_scans(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            config = FundingScanConfig(
                max_candidates=2,
                minimum_history_points=6,
                history_refresh_hours=0,
            )
            for _ in range(3):
                run_funding_scan(
                    store,
                    config=config,
                    binance=FakeClient("binance"),
                    hyperliquid=FakeClient("hyperliquid"),
                    scan_mode="auto",
                )
            manual = run_funding_scan(
                store,
                config=config,
                binance=FakeClient("binance"),
                hyperliquid=FakeClient("hyperliquid"),
                scan_mode="manual",
                hydrate_missing_history=True,
            )

            removed = store.prune_funding_auto_scans(keep=2)
            dashboard = store.funding_dashboard()
            with store.connect() as connection:
                scans = connection.execute(
                    "SELECT funding_scan_id, config_json FROM funding_scans "
                    "ORDER BY funding_scan_id"
                ).fetchall()
                history_count = connection.execute(
                    "SELECT COUNT(*) FROM funding_rate_history"
                ).fetchone()[0]

        self.assertEqual(removed, 0)
        self.assertEqual(len(scans), 2)
        self.assertEqual(scans[-1]["funding_scan_id"], manual["funding_scan_id"])
        self.assertGreater(history_count, 0)
        self.assertEqual(dashboard["latest_scan"]["scan_mode"], "manual")
        self.assertIsNotNone(dashboard["latest_scan"]["duration_seconds"])

    def test_auto_scan_never_waits_for_missing_history_backfill(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            result = run_funding_scan(
                store,
                config=FundingScanConfig(max_candidates=2),
                venue_clients=[
                    NoHistoryClient("binance"),
                    NoHistoryClient("hyperliquid"),
                ],
                scan_mode="auto",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["history_row_count"], 0)

    def test_history_backfill_is_resumable_across_market_batches(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            clients = [FakeClient("binance"), FakeClient("hyperliquid")]

            first = backfill_funding_history(
                store,
                days=90,
                limit=1,
                venue_clients=clients,
            )
            second = backfill_funding_history(
                store,
                days=90,
                limit=1,
                venue_clients=clients,
            )
            summary = store.funding_history_sync_summary()

        self.assertEqual(first["synced_market_count"], 1)
        self.assertEqual(first["remaining_market_count"], 1)
        self.assertEqual(second["synced_market_count"], 1)
        self.assertEqual(second["remaining_market_count"], 0)
        self.assertEqual(summary["market_count"], 2)

    def test_history_backfill_can_target_one_venue(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            binance = CountingFakeClient("binance")
            hyperliquid = CountingFakeClient("hyperliquid")
            result = backfill_funding_history(
                store,
                days=90,
                limit=0,
                venue_clients=[binance, hyperliquid],
                target_venues={"binance"},
            )

        self.assertEqual(result["target_venues"], ["binance"])
        self.assertEqual(result["eligible_market_count"], 1)
        self.assertEqual(result["attempted_market_count"], 1)
        self.assertEqual(binance.catalog_calls, 1)
        self.assertEqual(hyperliquid.catalog_calls, 0)

    def test_dashboard_keeps_last_successful_snapshot_during_next_scan(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "radar.sqlite")
            completed = run_funding_scan(
                store,
                config=FundingScanConfig(
                    max_candidates=2,
                    minimum_history_points=6,
                    history_refresh_hours=0,
                ),
                binance=FakeClient("binance"),
                hyperliquid=FakeClient("hyperliquid"),
                hydrate_missing_history=True,
            )
            running_id = store.start_funding_scan({"scan_mode": "auto"})
            dashboard = store.funding_dashboard()

        self.assertGreater(running_id, completed["funding_scan_id"])
        self.assertEqual(
            dashboard["latest_scan"]["funding_scan_id"],
            completed["funding_scan_id"],
        )
        self.assertEqual(len(dashboard["routes"]), 1)

class FakeClient:
    def __init__(self, venue: str) -> None:
        self.venue = venue

    def catalog_and_markets(
        self, observed_at: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        symbol = "BTCUSDT" if self.venue == "binance" else "BTC"
        interval = 8 if self.venue == "binance" else 1
        rate = 0.0 if self.venue == "binance" else 0.0005
        instrument = {
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": "BTC",
            "base_asset": "BTC",
            "quote_asset": "USDT" if self.venue == "binance" else "USD",
            "collateral_asset": "USDT" if self.venue == "binance" else "USDC",
            "contract_type": "linear_perpetual",
            "contract_multiplier": 1,
            "status": "active",
            "source_url": "https://example.com",
            "observed_at": observed_at,
            "raw": {},
        }
        return [instrument], [market(self.venue, symbol, "BTC", rate, interval, observed_at)], []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        return book(self.venue, symbol, observed_at)

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        rate = 0.0 if self.venue == "binance" else 0.0005
        count = 90 if self.venue == "binance" else 720
        return history_rows(
            self.venue,
            symbol,
            now,
            int(interval_hours),
            rate,
            count,
            observed_at=observed_at,
        )

class CountingFakeClient(FakeClient):
    def __init__(self, venue: str) -> None:
        super().__init__(venue)
        self.catalog_calls = 0
        self.orderbook_calls = 0

    def catalog_and_markets(
        self, observed_at: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        self.catalog_calls += 1
        instruments, markets, warnings = super().catalog_and_markets(observed_at)
        future = (
            datetime.fromisoformat(observed_at)
            + timedelta(hours=1)
        ).isoformat()
        for row in markets:
            row["next_funding_at"] = future
        return instruments, markets, warnings

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        self.orderbook_calls += 1
        return super().orderbook(symbol, observed_at, limit)

class NoHistoryClient(FakeClient):
    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        raise AssertionError("auto scan must not fetch historical funding")

class FakeOKXWebSocket:
    def __init__(self) -> None:
        self.sent = ""
        self.closed = False
        self.messages = [
            '{"event":"subscribe","arg":{"channel":"funding-rate"}}',
            '{"data":[{"instId":"BTC-USDT-SWAP","fundingRate":"0.0001"},'
            '{"instId":"ETH-USDT-SWAP","fundingRate":"-0.0002"}]}',
        ]

    def send(self, value: str) -> None:
        self.sent = value

    def settimeout(self, timeout: float) -> None:
        return

    def recv(self) -> str:
        return self.messages.pop(0)

    def close(self) -> None:
        self.closed = True

class FakeBinanceHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/exchangeInfo"):
            return {
                "symbols": [{
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                }]
            }
        if url.endswith("/premiumIndex"):
            return [{
                "symbol": "BTCUSDT",
                "lastFundingRate": "0.0008",
                "nextFundingTime": "1784044800000",
                "markPrice": "100",
                "indexPrice": "100",
            }]
        if url.endswith("/fundingInfo"):
            return [{
                "symbol": "BTCUSDT",
                "fundingIntervalHours": 8,
                "adjustedFundingRateCap": "0.003",
                "adjustedFundingRateFloor": "-0.003",
            }]
        raise AssertionError(url)

class FakeHyperliquidHttp:
    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        if payload == {"type": "metaAndAssetCtxs"}:
            return [
                {"universe": [{"name": "BTC", "isDelisted": False}]},
                [{
                    "markPx": "100",
                    "oraclePx": "100",
                    "openInterest": "1000",
                    "dayNtlVlm": "5000000",
                    "funding": "0.0001",
                }],
            ]
        if payload == {"type": "predictedFundings"}:
            return [[
                "BTC",
                [[
                    "HlPerp",
                    {
                        "fundingRate": "0.0008",
                        "nextFundingTime": 1_784_044_800_000,
                        "fundingIntervalHours": 8,
                    },
                ]],
            ]]
        raise AssertionError((url, payload))

class FakeBybitHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if "instruments-info" in url:
            return bybit_payload(
                [{
                    "symbol": "BTCUSDT",
                    "contractType": "LinearPerpetual",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "fundingInterval": 480,
                    "isPreListing": False,
                },
                {
                    "symbol": "MRVLUSDT",
                    "contractType": "LinearPerpetual",
                    "status": "Trading",
                    "baseCoin": "MRVL",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "fundingInterval": 480,
                    "isPreListing": False,
                    "symbolType": "stock",
                }]
            )
        if "/tickers" in url:
            return bybit_payload(
                [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "nextFundingTime": "1784044800000",
                    "fundingIntervalHour": "4",
                    "markPrice": "100",
                    "indexPrice": "100",
                    "openInterestValue": "1000000",
                    "turnover24h": "5000000",
                },
                {
                    "symbol": "MRVLUSDT",
                    "fundingRate": "-0.004",
                    "nextFundingTime": "1784044800000",
                    "fundingIntervalHour": "8",
                    "markPrice": "185",
                    "indexPrice": "186",
                    "openInterestValue": "1000000",
                    "turnover24h": "5000000",
                }]
            )
        if "/orderbook" in url:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"b": [["99.9", "100"]], "a": [["100.1", "100"]]},
            }
        if "/funding/history" in url:
            return bybit_payload(
                [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "fundingRateTimestamp": "1784016000000",
                }]
            )
        raise AssertionError(url)

class FakeBitgetHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if "/contracts?" in url:
            return bitget_payload(
                [{
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "symbolStatus": "normal",
                    "symbolType": "perpetual",
                    "isRwa": "NO",
                    "fundInterval": "8",
                    "takerFeeRate": "0.0006",
                }]
            )
        if "/tickers?" in url:
            return bitget_payload(
                [{
                    "symbol": "BTCUSDT",
                    "markPrice": "100",
                    "indexPrice": "100",
                    "holdingAmount": "1000",
                    "quoteVolume": "5000000",
                }]
            )
        if "/current-fund-rate?" in url:
            return bitget_payload(
                [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "fundingRateInterval": "8",
                    "nextUpdate": "1784044800000",
                }]
            )
        if "/merge-depth?" in url:
            return {
                "code": "00000",
                "msg": "success",
                "data": {
                    "bids": [["99.9", "2"]],
                    "asks": [["100.1", "3"]],
                },
            }
        if "/history-fund-rate?" in url:
            return bitget_payload(
                [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "fundingTime": "1784016000000",
                }]
            )
        raise AssertionError(url)

class FakeBackpackHttp:
    def get_json(self, url: str) -> Any:
        if "/markets?" in url:
            return [{
                "symbol": "BTC_USDC_PERP",
                "baseSymbol": "BTC",
                "quoteSymbol": "USDC",
                "marketType": "PERP",
                "visible": True,
                "orderBookState": "Open",
                "fundingInterval": 3_600_000,
            }]
        if "/markPrices?" in url:
            return [{
                "symbol": "BTC_USDC_PERP",
                "fundingRate": "0.0001",
                "nextFundingTimestamp": 1_784_044_800_000,
                "markPrice": "100",
                "indexPrice": "100",
            }]
        if "/depth?" in url:
            return {
                "bids": [["99.9", "2"]],
                "asks": [["100.1", "3"]],
            }
        if "/fundingRates?" in url:
            return [
                {
                    "symbol": "BTC_USDC_PERP",
                    "fundingRate": "0.0002",
                    "intervalEndTimestamp": "2026-07-14T13:00:00",
                },
                {
                    "symbol": "BTC_USDC_PERP",
                    "fundingRate": "0.0001",
                    "intervalEndTimestamp": "2026-07-14T11:00:00",
                },
            ]
        raise AssertionError(url)

class FakeAsterHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/exchangeInfo"):
            base = {
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "underlyingType": "COIN",
            }
            return {
                "symbols": [
                    {
                        **base,
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "underlyingSubType": ["Top"],
                    },
                    {
                        **base,
                        "symbol": "AAPLUSDT",
                        "baseAsset": "AAPL",
                        "underlyingSubType": ["STOCK"],
                    },
                ]
            }
        if url.endswith("/premiumIndex"):
            return [{
                "symbol": "BTCUSDT",
                "markPrice": "100",
                "indexPrice": "100",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1_784_044_800_000,
            }]
        if url.endswith("/ticker/24hr"):
            return [{"symbol": "BTCUSDT", "quoteVolume": "123456"}]
        if url.endswith("/fundingInfo"):
            return [{
                "symbol": "BTCUSDT",
                "fundingIntervalHours": 8,
                "fundingFeeCap": 0.003,
                "fundingFeeFloor": -0.003,
            }]
        if "/depth?" in url:
            return {"bids": [["99.9", "2"]], "asks": [["100.1", "3"]]}
        if "/fundingRate?" in url:
            return [{
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "fundingTime": 1_784_016_000_000,
            }]
        raise AssertionError(url)

class FakeLighterHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/orderBooks"):
            return {"order_books": [{
                "symbol": "BTC",
                "market_id": 1,
                "market_type": "perp",
                "status": "active",
                "maker_fee": "0.0000",
                "taker_fee": "0.0000",
            }]}
        if "/orderBookDetails?" in url:
            return {"order_book_details": [{
                "symbol": "BTC",
                "market_id": 1,
                "market_type": "perp",
                "status": "active",
                "mark_price": "100",
                "index_price": "100",
                "open_interest": "10",
                "daily_quote_token_volume": "100000",
            }]}
        if url.endswith("/funding-rates"):
            return {"funding_rates": [{
                "market_id": 1,
                "exchange": "lighter",
                "symbol": "BTC",
                "rate": -0.0072,
            }]}
        if "/orderBookOrders?" in url:
            return {
                "code": 200,
                "bids": [
                    {"price": "99.9", "remaining_base_amount": "1"},
                    {"price": "99.9", "remaining_base_amount": "2"},
                ],
                "asks": [{"price": "100.1", "remaining_base_amount": "3"}],
            }
        if "/fundings?" in url:
            return {
                "code": 200,
                "fundings": [{
                    "timestamp": 1_784_016_000,
                    "rate": "0.0012",
                    "direction": "short",
                    "value": "1.2",
                }],
            }
        raise AssertionError(url)

class FakeKuCoinHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/contracts/active"):
            return kucoin_payload([{
                "symbol": "XBTUSDTM",
                "displayBaseCurrency": "XBT",
                "quoteCurrency": "USDT",
                "settleCurrency": "USDT",
                "status": "Open",
                "expireDate": None,
                "marketType": "CRYPTO",
                "isInverse": False,
                "multiplier": 0.001,
                "markPrice": 100,
                "indexPrice": 100,
                "fundingFeeRate": 0.0001,
                "currentFundingRateGranularity": 28_800_000,
                "nextFundingRateDateTime": 1_784_044_800_000,
                "makerFeeRate": 0.0002,
                "takerFeeRate": 0.0006,
                "openInterest": 1000,
                "turnoverOf24h": 100000,
            }])
        if "/level2/depth100?" in url:
            return kucoin_payload({
                "bids": [[99.9, 2]],
                "asks": [[100.1, 3]],
            })
        if "/contract/funding-rates?" in url:
            return kucoin_payload([{
                "symbol": "XBTUSDTM",
                "fundingRate": 0.0001,
                "timepoint": 1_784_016_000_000,
            }])
        raise AssertionError(url)

def kucoin_payload(data: Any) -> dict[str, Any]:
    return {"code": "200000", "data": data}

class FakeMEXCHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/contract/detail"):
            return mexc_payload([
                {
                    "symbol": "BTC_USDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "futureType": 1,
                    "state": 0,
                    "isHidden": False,
                    "preMarket": False,
                    "conceptPlate": ["layer1"],
                    "contractSize": 0.001,
                    "makerFeeRate": 0,
                    "takerFeeRate": 0.0002,
                },
                {
                    "symbol": "COINBASESTOCK_USDT",
                    "baseCoin": "COINBASESTOCK",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "futureType": 1,
                    "state": 0,
                    "isHidden": False,
                    "preMarket": False,
                    "conceptPlate": ["mc-trade-zone-Stock"],
                    "contractSize": 0.01,
                },
            ])
        if url.endswith("/contract/funding_rate"):
            return mexc_payload([{
                "symbol": "BTC_USDT",
                "fundingRate": 0.0004,
                "collectCycle": 4,
                "nextSettleTime": int(
                    datetime(2026, 7, 14, 16, tzinfo=UTC).timestamp() * 1_000
                ),
                "idxPrice": 100,
                "fairPrice": 100,
            }])
        if url.endswith("/contract/ticker"):
            return mexc_payload([{
                "symbol": "BTC_USDT",
                "holdVol": 1_000,
                "amount24": 100_000,
            }])
        if "/contract/depth/BTC_USDT?" in url:
            return mexc_payload({
                "bids": [[99.9, 2]],
                "asks": [[100.1, 3]],
            })
        if "/contract/funding_rate/history?" in url:
            return mexc_payload({
                "totalPage": 1,
                "resultList": [{
                    "symbol": "BTC_USDT",
                    "fundingRate": 0.0004,
                    "collectCycle": 4,
                    "settleTime": int(
                        datetime(2026, 7, 14, 8, tzinfo=UTC).timestamp() * 1_000
                    ),
                }],
            })
        raise AssertionError(url)

def mexc_payload(data: Any) -> dict[str, Any]:
    return {"success": True, "code": 0, "data": data}

class FakeParadexHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/v1/markets"):
            return paradex_payload([
                {
                    "symbol": "BTC-USD-PERP",
                    "base_currency": "BTC",
                    "quote_currency": "USD",
                    "settlement_currency": "USDC",
                    "asset_kind": "PERP",
                    "expiry_at": 0,
                    "trading_mode": "STANDARD",
                    "funding_period_hours": 8,
                    "fee_config": {
                        "api_fee": {
                            "maker_fee": {"fee": "0"},
                            "taker_fee": {"fee": "0.0002"},
                        }
                    }
                },
                {
                    "symbol": "FUTURE-USD-PERP",
                    "base_currency": "FUTURE",
                    "quote_currency": "USD",
                    "settlement_currency": "USDC",
                    "asset_kind": "PERP",
                    "expiry_at": 0,
                    "trading_mode": "STANDARD",
                    "funding_period_hours": 8,
                    "open_at": int(
                        datetime(2026, 7, 15, tzinfo=UTC).timestamp() * 1_000
                    ),
                },
            ])
        if "/v1/markets/summary?" in url:
            return paradex_payload([
                {
                    "symbol": "BTC-USD-PERP",
                    "mark_price": "100",
                    "underlying_price": "100",
                    "funding_rate": "0.0008",
                    "open_interest": "10",
                    "volume_24h": "100000",
                },
                {
                    "symbol": "FUTURE-USD-PERP",
                    "mark_price": "100",
                    "underlying_price": "100",
                    "funding_rate": "0.0008",
                    "open_interest": "10",
                    "volume_24h": "100000",
                },
            ])
        if "/v1/orderbook/BTC-USD-PERP?" in url:
            return {
                "bids": [["99.9", "2"]],
                "asks": [["100.1", "3"]],
            }
        if "/v1/funding/data?" in url:
            return paradex_payload([
                {
                    "market": "BTC-USD-PERP",
                    "created_at": int(
                        datetime(2026, 7, 14, 10, 10, tzinfo=UTC).timestamp()
                        * 1_000
                    ),
                    "funding_rate_8h": "0.0008",
                    "funding_period_hours": 8,
                },
                {
                    "market": "BTC-USD-PERP",
                    "created_at": int(
                        datetime(2026, 7, 14, 10, 50, tzinfo=UTC).timestamp()
                        * 1_000
                    ),
                    "funding_rate_8h": "0.0016",
                    "funding_period_hours": 8,
                },
                {
                    "market": "BTC-USD-PERP",
                    "created_at": int(
                        datetime(2026, 7, 14, 11, 30, tzinfo=UTC).timestamp()
                        * 1_000
                    ),
                    "funding_rate_8h": "0.0032",
                    "funding_period_hours": 8,
                },
            ])
        raise AssertionError(url)

def paradex_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"next": None, "results": rows}

class FakeDriftHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/stats/markets"):
            return {
                "success": True,
                "markets": [
                    {
                        "symbol": "BTC-PERP",
                        "marketIndex": 0,
                        "marketType": "perp",
                        "status": "active",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "fees": {"maker": "0", "taker": "0.001"},
                        "oraclePrice": "100",
                        "markPrice": "100",
                        "quoteVolume": "5000",
                        "openInterest": {"long": "2", "short": "-3"},
                        "fundingRate": {"long": "-0.0012", "short": "0.0012"},
                        "fundingRateUpdateTs": 1_784_026_800,
                    }
                ],
            }
        if "/fundingRates" in url:
            return {
                "success": True,
                "records": [
                    {
                        "ts": 1_784_026_800,
                        "symbol": "BTC-PERP",
                        "fundingRate": "0.0012",
                        "markPriceTwap": "100",
                        "oraclePriceTwap": "100",
                    },
                    {
                        "ts": 1_784_023_200,
                        "symbol": "BTC-PERP",
                        "fundingRate": "0.0010",
                        "markPriceTwap": "100",
                        "oraclePriceTwap": "100",
                    },
                ],
                "meta": {"nextPage": None},
            }
        if "/l2?" in url:
            return {
                "bids": [{"price": "99900000", "size": "2000000000"}],
                "asks": [{"price": "100100000", "size": "3000000000"}],
            }
        raise AssertionError(url)

class FakeEtherealHttp:
    def get_json(self, url: str) -> Any:
        if "/v1/product?" in url and "market-" not in url:
            return {
                "data": [
                    {
                        "id": "btc-product",
                        "ticker": "BTCUSD",
                        "displayTicker": "BTC-USD",
                        "status": "ACTIVE",
                        "baseTokenName": "BTC",
                        "quoteTokenName": "USD",
                        "makerFee": "0",
                        "takerFee": "0.0003",
                        "volume24h": "5000000",
                        "openInterest": "10",
                        "fundingRate1h": "0.000010",
                    },
                    {
                        "id": "old-product",
                        "ticker": "OLDUSD",
                        "status": "PAUSED",
                        "baseTokenName": "OLD",
                        "quoteTokenName": "USD",
                    },
                ],
                "hasNext": False,
            }
        if "/v1/product/market-price?" in url:
            return {
                "data": [
                    {
                        "productId": "btc-product",
                        "bestBidPrice": "99.9",
                        "bestAskPrice": "100.1",
                        "oraclePrice": "99.8",
                    }
                ],
                "hasNext": False,
            }
        if "/v1/funding/projected-rate?" in url:
            return {
                "data": [
                    {
                        "productId": "btc-product",
                        "fundingRateProjected1h": "0.000013",
                        "fundingRate1h": "0.000011",
                    }
                ],
                "hasNext": False,
            }
        if "/v1/product/market-liquidity?" in url:
            return {
                "productId": "btc-product",
                "timestamp": 1_784_030_400_000,
                "bids": [["99.9", "2"], ["99.8", "1"]],
                "asks": [["100.1", "3"], ["100.2", "1"]],
            }
        if "/v1/funding?" in url:
            return {
                "data": [
                    {"createdAt": 1_784_026_800_000, "fundingRate1h": "0.000011"},
                    {"createdAt": 1_784_030_400_000, "fundingRate1h": "0.000012"},
                ],
                "hasNext": False,
            }
        raise AssertionError(url)

class FakeExtendedHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/api/v1/info/markets"):
            return {
                "status": "OK",
                "data": [
                    {
                        "name": "BTC-USD",
                        "type": "PERPETUAL",
                        "assetName": "BTC",
                        "collateralAssetName": "USD",
                        "active": True,
                        "isRfq": False,
                        "isOffHours": False,
                        "status": "ACTIVE",
                        "category": "Crypto",
                        "marketStats": {
                            "markPrice": "100",
                            "indexPrice": "100",
                            "fundingRate": "0.000013",
                            "nextFundingRate": "1784034000000",
                            "openInterest": "1000000",
                            "dailyVolume": "2000000",
                        },
                    },
                    {
                        "name": "EUR-USD",
                        "type": "PERPETUAL",
                        "assetName": "EUR",
                        "active": True,
                        "isRfq": False,
                        "isOffHours": False,
                        "status": "ACTIVE",
                        "category": "TradFi",
                        "marketStats": {
                            "markPrice": "1.1",
                            "indexPrice": "1.1",
                            "fundingRate": "0.0001",
                        },
                    },
                ],
            }
        if url.endswith("/api/v1/info/markets/BTC-USD/orderbook"):
            return {
                "status": "OK",
                "data": {
                    "market": "BTC-USD",
                    "bid": [{"price": "99.9", "qty": "2"}],
                    "ask": [{"price": "100.1", "qty": "3"}],
                },
            }
        if "/api/v1/info/BTC-USD/funding?" in url:
            return {
                "status": "OK",
                "data": [
                    {"m": "BTC-USD", "f": "0.000011", "T": 1_784_026_800_000},
                    {"m": "BTC-USD", "f": "0.000012", "T": 1_784_030_400_000},
                ],
            }
        raise AssertionError(url)

class FakeNadoHttp:
    def get_json(self, url: str) -> Any:
        if "/pairs?market=perp" in url:
            return [
                {
                    "product_id": 1,
                    "ticker_id": "BTC-PERP_USDT0",
                    "base": "BTC-PERP",
                    "quote": "USDT0",
                }
            ]
        if "/contracts" in url:
            return {
                "BTC-PERP_USDT0": {
                    "product_id": 1,
                    "ticker_id": "BTC-PERP_USDT0",
                    "base_currency": "BTC-PERP",
                    "quote_currency": "USDT0",
                    "last_price": 116220.0,
                    "base_volume": 100.0,
                    "quote_volume": 11622000.0,
                    "product_type": "perpetual",
                    "contract_price": 116220.0,
                    "contract_price_currency": "USD",
                    "open_interest": 1000.0,
                    "open_interest_usd": 116220000.0,
                    "index_price": 116219.0,
                    "mark_price": 116221.0,
                    "funding_rate": 0.024,
                    "next_funding_rate_timestamp": 1784034000,
                    "price_change_percent_24h": 0.1,
                }
            }
        if "/orderbook" in url:
            return {
                "product_id": 1,
                "ticker_id": "BTC-PERP_USDT0",
                "bids": [[116215.0, 0.128], [116214.0, 0.172]],
                "asks": [[116225.0, 0.043], [116226.0, 0.172]],
                "timestamp": 1784030400000,
            }
        raise AssertionError(url)

    def post_json(self, url: str, payload: Any) -> Any:
        if "funding_rates" in payload:
            return {
                "1": {
                    "product_id": 1,
                    "funding_rate_x18": "24000000000000000",
                    "update_time": "1784030400",
                }
            }
        if "funding_rate_history" in payload:
            return {
                "funding_rates": [
                    {
                        "product_id": 1,
                        "timestamp": "1784026800",
                        "funding_rate_x18": "-313157879073748",
                    },
                    {
                        "product_id": 1,
                        "timestamp": "1784030400",
                        "funding_rate_x18": "152340987120453",
                    },
                ]
            }
        raise AssertionError((url, payload))


class FakePacificaHttp:
    def get_json(self, url: str) -> Any:
        if "/info" in url:
            if "/info/prices" in url:
                return {
                    "success": True,
                    "data": [
                        {
                            "symbol": "BTC",
                            "funding": "0.0000100",
                            "next_funding": "0.0000125",
                            "mark": "64132.5",
                            "mid": "64132.5",
                            "oracle": "64131.9",
                            "open_interest": "1000000",
                            "volume_24h": "5000000",
                            "timestamp": 1784030400000,
                        }
                    ],
                }
            if "/info/fees" in url:
                return {
                    "success": True,
                    "data": [
                        {
                            "level": 0,
                            "maker_fee_rate": "0.00020",
                            "taker_fee_rate": "0.00050",
                        },
                        {
                            "level": 1,
                            "maker_fee_rate": "0.00010",
                            "taker_fee_rate": "0.00040",
                        },
                    ],
                }
            return {
                "success": True,
                "data": [
                    {
                        "symbol": "BTC",
                        "base_asset": "BTC",
                        "instrument_type": "perpetual",
                        "funding_rate": "0.0000125",
                        "next_funding_rate": "0.0000125",
                        "max_leverage": 50,
                    },
                    {
                        "symbol": "SOL-USDC",
                        "base_asset": "SOL",
                        "instrument_type": "spot",
                    },
                ],
            }
        if "/book" in url:
            return {
                "success": True,
                "data": {
                    "s": "BTC",
                    "l": [
                        [{"p": "64132", "a": "0.785", "n": 5}],
                        [{"p": "64133", "a": "1.234", "n": 3}],
                    ],
                },
            }
        if "/funding_rate/history" in url:
            return {
                "success": True,
                "data": [
                    {
                        "oracle_price": "64131.9",
                        "bid_impact_price": "64120",
                        "ask_impact_price": "64140",
                        "funding_rate": "0.0000125",
                        "next_funding_rate": "0.000013",
                        "created_at": 1784026800000,
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            }
        raise AssertionError(url)

class FakeReyaHttp:
    def get_json(self, url: str) -> Any:
        if "/v2/perpMarketDefinitions" in url:
            return [
                {
                    "symbol": "BTCRUSDPERP",
                    "marketId": 1,
                    "tickSize": "0.1",
                    "maxLeverage": 50,
                },
            ]
        if "/v2/perpMarkets/summary" in url:
            return [
                {
                    "symbol": "BTCRUSDPERP",
                    "fundingRate": "0.00127969409872345",
                    "throttledOraclePrice": "64349.79",
                    "throttledPoolPrice": "64357.34",
                    "oiQty": "40.84",
                    "volume24h": "96573858.96",
                },
            ]
        raise AssertionError(url)


class FakeRiseXHttp:
    def get_json(self, url: str) -> Any:
        if "/v1/markets/" not in url and "/v1/markets" in url:
            next_funding_ns = int(
                datetime(2026, 7, 14, 13, 0, tzinfo=UTC).timestamp()
                * 1_000_000_000
            )
            return {
                "data": {
                    "markets": [
                        {
                            "market_id": "1",
                            "config": {
                                "name": "BTC/USDC",
                                "unlocked": True,
                            },
                            "base_asset_symbol": "BTC/USDC",
                            "quote_asset_symbol": "USDC",
                            "display_name": "BTC/USDC",
                            "quote_volume_24h": "1000000",
                            "last_price": "100.25",
                            "mark_price": "100.2",
                            "index_price": "100.1",
                            "open_interest": "123.4",
                            "funding_interval": "3600000000000",
                            "next_funding_time": str(next_funding_ns),
                            "current_funding_rate": "-0.001",
                            "funding_rate_8h": "-0.008",
                            "active": True,
                            "post_only": False,
                        },
                        {
                            "market_id": "2",
                            "config": {
                                "name": "ETH/USDC [deprecated-1]",
                                "unlocked": True,
                            },
                            "base_asset_symbol": "ETH/USDC",
                            "funding_interval": "3600000000000",
                            "current_funding_rate": "0.001",
                            "active": True,
                        },
                    ]
                }
            }
        if "/v1/orderbook" in url:
            return {
                "data": {
                    "market_id": "1",
                    "bids": [
                        {"price": "100.0", "quantity": "2.0"},
                        {"price": "99.5", "quantity": "3.0"},
                    ],
                    "asks": [
                        {"price": "100.5", "quantity": "1.5"},
                        {"price": "101.0", "quantity": "4.0"},
                    ],
                }
            }
        if "/funding-rate-history" in url:
            first_start = int(
                datetime(2026, 7, 14, 10, 0, tzinfo=UTC).timestamp()
                * 1_000_000_000
            )
            second_start = int(
                datetime(2026, 7, 14, 11, 0, tzinfo=UTC).timestamp()
                * 1_000_000_000
            )
            one_hour_ns = 3_600_000_000_000
            return {
                "data": {
                    "market_id": "1",
                    "records": [
                        {
                            "funding_rate": "-0.0007",
                            "index_price": "100",
                            "start_time": str(second_start),
                            "end_time": str(second_start + one_hour_ns),
                            "block_time": str(second_start + one_hour_ns),
                        },
                        {
                            "funding_rate": "-0.0008",
                            "index_price": "100",
                            "start_time": str(first_start),
                            "end_time": str(first_start + one_hour_ns),
                            "block_time": str(first_start + one_hour_ns),
                        },
                    ],
                    "has_next_page": False,
                }
            }
        raise AssertionError(url)


class FakeApexHttp:
    def get_json(self, url: str) -> Any:
        if "/v3/config" in url:
            return {
                "data": {
                    "contractConfig": {
                        "perpetualContract": [
                            {
                                "symbol": "BTC-USDT",
                                "symbolDisplayName": "BTCUSDT",
                                "baseTokenId": "BTC",
                                "settleAssetId": "USDT",
                                "enableTrade": True,
                                "enableFundingSettlement": True,
                                "isPrelaunch": False,
                                "contractType": "PERPETUAL_CONTRACT",
                            },
                            {
                                "symbol": "PRE-USDT",
                                "symbolDisplayName": "PREUSDT",
                                "baseTokenId": "PRE",
                                "enableTrade": True,
                                "enableFundingSettlement": True,
                                "isPrelaunch": True,
                            },
                        ],
                    },
                },
            }
        if "/v3/ticker" in url:
            return {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "markPrice": "64356.82",
                        "indexPrice": "64364.11",
                        "fundingRate": "-0.00001397",
                        "predictedFundingRate": "0.0000125",
                        "nextFundingTime": "2026-07-14T13:00:00Z",
                        "openInterest": "1596.202",
                        "turnover24h": "1223380590.95",
                    },
                ],
            }
        if "/v3/depth" in url:
            return {
                "data": {
                    "s": "BTCUSDT",
                    "b": [["64365.2", "4.371"], ["64359.0", "0.515"]],
                    "a": [["64365.8", "5.112"], ["64366.4", "0.514"]],
                },
            }
        if "/v3/history-funding" in url:
            return {
                "data": {
                    "historyFunds": [
                        {
                            "symbol": "BTC-USDT",
                            "rate": "-0.00000798",
                            "price": "64780.25",
                            "fundingTime": 1784001600000,
                        },
                        {
                            "symbol": "BTC-USDT",
                            "rate": "-0.00000009",
                            "price": "65081.0",
                            "fundingTime": 1784005200000,
                        },
                    ],
                    "totalSize": 2,
                },
            }
        raise AssertionError(url)

class FakeEdgexHttp:
    def get_json(self, url: str) -> Any:
        if "/api/v2/public/meta/getMetaData" in url:
            return {
                "code": "SUCCESS",
                "data": {
                    "contractList": [
                        {
                            "contractId": "30000001",
                            "contractName": "BTCUSDC",
                            "baseCoinId": "1001",
                            "quoteCoinId": "1000",
                            "enableTrade": True,
                            "enableDisplay": True,
                            "enableOpenPosition": True,
                            "defaultTakerFeeRate": "0.00038",
                            "defaultMakerFeeRate": "0.00018",
                            "fundingRateIntervalMin": "240",
                        },
                        {
                            "contractId": "30000099",
                            "contractName": "DELISTED",
                            "enableTrade": False,
                            "enableDisplay": False,
                        },
                    ],
                },
            }
        if "/api/v2/public/quote/getTicker" in url:
            return {
                "code": "SUCCESS",
                "data": [
                    {
                        "contractId": "30000001",
                        "contractName": "BTCUSDC",
                        "markPrice": "64227.19",
                        "indexPrice": "64258.25",
                        "fundingRate": "0.00005000",
                        "nextFundingTime": "1784908800000",
                        "openInterest": "1083.013",
                        "value": "50821443.74",
                    },
                ],
            }
        if "/api/v2/public/quote/getDepth" in url:
            return {
                "code": "SUCCESS",
                "data": [
                    {
                        "contractId": "30000001",
                        "contractName": "BTCUSDC",
                        "bids": [{"price": "64214.3", "size": "1.710"}],
                        "asks": [{"price": "64214.5", "size": "0.032"}],
                    },
                ],
            }
        if "/api/v2/public/funding/getFundingRatePage" in url:
            return {
                "code": "SUCCESS",
                "data": {
                    "dataList": [
                        {
                            "contractId": "30000001",
                            "fundingTime": "1784001600000",
                            "fundingRate": "0.00005000",
                            "indexPrice": "64900.0",
                            "fundingRateIntervalMin": "240",
                            "isSettlement": True,
                        },
                        {
                            "contractId": "30000001",
                            "fundingTime": "1784016000000",
                            "fundingRate": "0.00006000",
                            "indexPrice": "65000.0",
                            "fundingRateIntervalMin": "240",
                            "isSettlement": True,
                        },
                    ],
                    "nextPageOffsetData": "",
                },
            }
        raise AssertionError(url)

class FakeGrvtHttp:
    def post_json(self, url: str, payload: Any) -> Any:
        if url.endswith("/full/v1/all_instruments"):
            return {
                "result": [
                    {
                        "instrument": "BTC_USDT_Perp",
                        "base": "BTC",
                        "quote": "USDT",
                        "kind": "PERPETUAL",
                        "funding_interval_hours": 8,
                        "adjusted_funding_rate_cap": 2.5,
                        "adjusted_funding_rate_floor": -2.5,
                    },
                    {
                        "instrument": "AAPL_USDT_Perp",
                        "base": "AAPL",
                        "quote": "USDT",
                        "kind": "FUTURE",
                        "funding_interval_hours": 8,
                    },
                ],
            }
        if url.endswith("/full/v1/ticker"):
            return {
                "result": {
                    "instrument": "BTC_USDT_Perp",
                    "mark_price": "65038.01",
                    "index_price": "65038.01",
                    "funding_rate": "0.0003",
                    "next_funding_time": "1784034000000000000",
                    "open_interest": "100.0",
                    "buy_volume_24h_b": "500000",
                    "sell_volume_24h_b": "500000",
                },
            }
        if url.endswith("/full/v1/book"):
            return {
                "result": {
                    "instrument": "BTC_USDT_Perp",
                    "bids": [{"price": "65038.01", "size": "3456.78", "num_orders": 5}],
                    "asks": [{"price": "65038.02", "size": "1234.56", "num_orders": 3}],
                },
            }
        if url.endswith("/full/v1/funding"):
            return {
                "result": [
                    {
                        "instrument": "BTC_USDT_Perp",
                        "funding_rate": "0.0002",
                        "funding_time": "1784001600000000000",
                        "mark_price": "64900.0",
                        "funding_interval_hours": 8,
                    },
                    {
                        "instrument": "BTC_USDT_Perp",
                        "funding_rate": "0.00025",
                        "funding_time": "1784030400000000000",
                        "mark_price": "65000.0",
                        "funding_interval_hours": 8,
                    },
                ],
                "next": "",
            }
        raise AssertionError(url)

class FakeGateHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/contracts"):
            return [
                {
                    "name": "BTC_USDT",
                    "status": "trading",
                    "in_delisting": False,
                    "is_pre_market": False,
                    "contract_type": "",
                    "quanto_multiplier": "0.01",
                    "mark_price": "100",
                    "index_price": "100",
                    "funding_interval": 28800,
                    "funding_rate_indicative": "0.0001",
                    "funding_next_apply": 1784044800,
                    "position_size": 1000,
                    "taker_fee_rate": "0.00075",
                },
                {
                    "name": "AAPL_USDT",
                    "status": "trading",
                    "in_delisting": False,
                    "is_pre_market": False,
                    "contract_type": "stocks",
                    "quanto_multiplier": "0.01",
                    "mark_price": "200",
                    "index_price": "200",
                },
            ]
        if url.endswith("/tickers"):
            return [{"contract": "BTC_USDT", "volume_24h_quote": "5000000"}]
        if "/order_book?" in url:
            return {
                "bids": [{"p": "99.9", "s": 100}],
                "asks": [{"p": "100.1", "s": 200}],
            }
        if "/funding_rate?" in url:
            return [{"r": "0.0001", "t": 1784016001}]
        raise AssertionError(url)

class FakeBingXHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/contracts"):
            return bingx_payload([
                {
                    "symbol": "BTC-USDT",
                    "asset": "BTC",
                    "currency": "USDT",
                    "status": 1,
                    "apiStateOpen": True,
                    "apiStateClose": True,
                    "makerFeeRate": "0.0002",
                    "takerFeeRate": "0.0005",
                }
            ])
        if url.endswith("/premiumIndex"):
            return bingx_payload([
                {
                    "symbol": "BTC-USDT",
                    "markPrice": "100",
                    "indexPrice": "100",
                    "lastFundingRate": "0.0001",
                    "fundingIntervalHours": "8",
                    "nextFundingTime": "1784044800000",
                }
            ])
        if "/depth?" in url:
            return bingx_payload({
                "bids": [["99.9", "2"]],
                "asks": [["100.1", "3"]],
            })
        if "/fundingRate?" in url:
            return bingx_payload([
                {
                    "symbol": "BTC-USDT",
                    "fundingRate": "0.0001",
                    "fundingTime": "1784016000000",
                    "markPrice": "100",
                }
            ])
        raise AssertionError(url)

def bingx_payload(data: Any) -> dict[str, Any]:
    return {"code": 0, "msg": "", "data": data}

class FakeHTXHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/swap_contract_info"):
            return htx_payload([
                {
                    "symbol": "BTC",
                    "contract_code": "BTC-USDT",
                    "contract_size": 0.001,
                    "contract_status": 1,
                    "trade_partition": "USDT",
                    "settlement_period": "8",
                    "contract_type": "swap",
                    "business_type": "swap",
                }
            ])
        if url.endswith("/swap_batch_funding_rate"):
            return htx_payload([
                {
                    "contract_code": "BTC-USDT",
                    "symbol": "BTC",
                    "estimated_rate": None,
                    "funding_rate": "0.0001",
                    "funding_time": "1784044800000",
                    "trade_partition": "USDT",
                }
            ])
        if url.endswith("/detail/batch_merged"):
            return {
                "status": "ok",
                "ticks": [
                    {
                        "contract_code": "BTC-USDT",
                        "bid": [99.9, 1_000],
                        "ask": [100.1, 1_500],
                        "close": "100",
                        "trade_turnover": "5000000",
                    }
                ],
            }
        if url.endswith("/swap_index"):
            return htx_payload([
                {"contract_code": "BTC-USDT", "index_price": 100}
            ])
        if "/market/depth?" in url:
            return {
                "status": "ok",
                "tick": {
                    "bids": [[99.9, 1_000]],
                    "asks": [[100.1, 2_000]],
                },
            }
        if "/swap_historical_funding_rate?" in url:
            return htx_payload({
                "data": [
                    {
                        "contract_code": "BTC-USDT",
                        "funding_rate": "0.0001",
                        "funding_time": "1784016000000",
                    }
                ]
            })
        raise AssertionError(url)

def htx_payload(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}

class FakeOKXHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if "/public/instruments" in url:
            return okx_payload(
                [{
                    "instId": "BTC-USDT-SWAP",
                    "instFamily": "BTC-USDT",
                    "uly": "BTC-USDT",
                    "state": "live",
                    "ctType": "linear",
                    "ctVal": "0.01",
                    "ctMult": "1",
                    "ctValCcy": "BTC",
                    "settleCcy": "USDT",
                    "contTdSwTime": "1573557408000",
                    "instCategory": "1",
                },
                {
                    "instId": "MRVL-USDT-SWAP",
                    "instFamily": "MRVL-USDT",
                    "uly": "MRVL-USDT",
                    "state": "live",
                    "ctType": "linear",
                    "ctVal": "1",
                    "ctMult": "1",
                    "ctValCcy": "MRVL",
                    "settleCcy": "USDT",
                    "instCategory": "3",
                }]
            )
        if "/market/tickers" in url:
            return okx_payload(
                [{
                    "instId": "BTC-USDT-SWAP",
                    "last": "100",
                    "volCcy24h": "1000",
                },
                {
                    "instId": "MRVL-USDT-SWAP",
                    "last": "185",
                    "volCcy24h": "1000",
                }]
            )
        if "/public/mark-price" in url:
            return okx_payload([
                {"instId": "BTC-USDT-SWAP", "markPx": "100"},
                {"instId": "MRVL-USDT-SWAP", "markPx": "185"},
            ])
        if "/market/index-tickers" in url:
            return okx_payload([
                {"instId": "BTC-USDT", "idxPx": "100"},
                {"instId": "MRVL-USDT", "idxPx": "186"},
            ])
        if "/public/funding-rate-history" in url:
            return okx_payload(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "fundingRate": "0.0001",
                        "realizedRate": "0.0001",
                        "fundingTime": "1784016000000",
                    },
                    {
                        "instId": "BTC-USDT-SWAP",
                        "fundingRate": "0.00008",
                        "realizedRate": "0.00008",
                        "fundingTime": "1783987200000",
                    },
                ]
            )
        if "/public/funding-rate" in url:
            return okx_payload(
                [{
                    "instId": "BTC-USDT-SWAP",
                    "fundingRate": "0.0001",
                    "fundingTime": "1784044800000",
                    "prevFundingTime": "1784016000000",
                }]
            )
        if "/market/books" in url:
            return okx_payload(
                [{
                    "bids": [["99.9", "100", "0", "1"]],
                    "asks": [["100.1", "200", "0", "1"]],
                }]
            )
        raise AssertionError(url)

class FakeDydxHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if url.endswith("/perpetualMarkets"):
            return {
                "markets": {
                    "BTC-USD": {
                        "ticker": "BTC-USD",
                        "status": "ACTIVE",
                        "oraclePrice": "100",
                        "nextFundingRate": "0.0001",
                        "openInterest": "1000",
                        "volume24H": "5000000",
                    }
                }
            }
        if "/orderbooks/perpetualMarket/" in url:
            return {
                "bids": [{"price": "99.9", "size": "2"}],
                "asks": [{"price": "100.1", "size": "3"}],
            }
        if "/historicalFunding/" in url:
            return {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.0001",
                        "price": "100",
                        "effectiveAt": "2026-07-14T12:00:00.000Z",
                    },
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.00008",
                        "price": "100",
                        "effectiveAt": "2026-07-14T11:00:00.000Z",
                    },
                ]
            }
        raise AssertionError(url)

class FakeKrakenHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if url.endswith("/instruments"):
            return kraken_payload(
                "instruments",
                [
                    {
                        "symbol": "PF_XBTUSD",
                        "type": "flexible_futures",
                        "tradeable": True,
                        "base": "XBT",
                        "quote": "USD",
                        "contractSize": 1,
                    },
                    {
                        "symbol": "PI_XBTUSD",
                        "type": "futures_inverse",
                        "tradeable": True,
                        "base": "XBT",
                        "quote": "USD",
                    },
                ],
            )
        if url.endswith("/tickers"):
            return kraken_payload(
                "tickers",
                [
                    {
                        "symbol": "PF_XBTUSD",
                        "tag": "perpetual",
                        "pair": "XBT:USD",
                        "markPrice": 100,
                        "indexPrice": 100,
                        "fundingRate": "0.7",
                        "fundingRatePrediction": "0.8",
                        "openInterest": 10,
                        "volumeQuote": 100000,
                        "suspended": False,
                        "postOnly": False,
                    },
                    {
                        "symbol": "PI_XBTUSD",
                        "tag": "perpetual",
                        "pair": "XBT:USD",
                        "markPrice": 100,
                        "indexPrice": 100,
                    },
                ],
            )
        if "/orderbook?" in url:
            return {
                "result": "success",
                "orderBook": {
                    "bids": [[1, 100], [99.9, 2]],
                    "asks": [[150, 100], [100.1, 3]],
                },
            }
        if "/historical-funding-rates?" in url:
            return kraken_payload(
                "rates",
                [
                    {
                        "timestamp": "2026-07-14T11:00:00Z",
                        "relativeFundingRate": "0.00010",
                    },
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "relativeFundingRate": "0.00012",
                    },
                ],
            )
        raise AssertionError(url)

class FakeDeribitHttp:
    def get_json(self, url: str) -> dict[str, Any]:
        if "/public/get_instruments?" in url:
            return {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "instrument_name": "BTC_USDC-PERPETUAL",
                        "state": "open",
                        "is_active": True,
                        "settlement_period": "perpetual",
                        "instrument_type": "linear",
                        "future_type": "linear",
                        "base_currency": "BTC",
                        "quote_currency": "USDC",
                        "settlement_currency": "USDC",
                        "contract_size": 0.0001,
                        "maker_commission": 0.0,
                        "taker_commission": 0.0005,
                    },
                    {
                        "instrument_name": "BTC-PERPETUAL",
                        "state": "open",
                        "is_active": True,
                        "settlement_period": "perpetual",
                        "instrument_type": "reversed",
                        "future_type": "reversed",
                        "base_currency": "BTC",
                        "quote_currency": "USD",
                        "settlement_currency": "BTC",
                    },
                ],
            }
        if "/public/ticker?" in url:
            return {
                "jsonrpc": "2.0",
                "result": {
                    "instrument_name": "BTC_USDC-PERPETUAL",
                    "state": "open",
                    "mark_price": 100,
                    "index_price": 100,
                    "open_interest": 10,
                    "funding_8h": "0.0008",
                    "current_funding": "0.0001",
                    "stats": {"volume_usd": 100000},
                },
            }
        if "/public/get_order_book?" in url:
            return {
                "jsonrpc": "2.0",
                "result": {
                    "bids": [[99.9, 2]],
                    "asks": [[100.1, 3]],
                },
            }
        if "/public/get_funding_rate_history?" in url:
            return {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "timestamp": 1_784_016_000_000,
                        "index_price": 100,
                        "interest_1h": "0.00011",
                        "interest_8h": "0.00088",
                    }
                ],
            }
        raise AssertionError(url)

class FakeVertexHttp:
    def __init__(self) -> None:
        self.symbol_calls = 0

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        if "vertex-gateway" in url:
            if payload.get("type") == "symbols":
                self.symbol_calls += 1
                return {
                    "status": "success",
                    "data": {
                        "symbols": {
                            "BTC-PERP": {
                                "type": "perp",
                                "product_id": "2",
                                "symbol": "BTC-PERP",
                                "maker_fee_rate_x18": "0",
                                "taker_fee_rate_x18": "300000000000000",
                            },
                            "BTC": {
                                "type": "spot",
                                "product_id": "1",
                                "symbol": "BTC",
                            },
                        }
                    },
                }
            if payload.get("type") == "all_products":
                return {
                    "status": "success",
                    "data": {
                        "perp_products": [
                            {
                                "product_id": 2,
                                "oracle_price_x18": "100000000000000000000",
                                "state": {
                                    "open_interest": "2000000000000000000",
                                },
                            }
                        ]
                    },
                }
            if payload.get("type") == "market_liquidity":
                assert payload["product_id"] == 2
                return {
                    "status": "success",
                    "data": {
                        "timestamp": "1784031300",
                        "bids": [
                            [
                                "99900000000000000000",
                                "2000000000000000000",
                            ]
                        ],
                        "asks": [
                            [
                                "100100000000000000000",
                                "3000000000000000000",
                            ]
                        ],
                    },
                }
        if "vertex-indexer" in url:
            if "funding_rates" in payload:
                return {
                    "2": {
                        "product_id": 2,
                        "funding_rate_x18": "100000000000000",
                        "update_time": "1784031300",
                    }
                }
            if "market_snapshots" in payload:
                return {
                    "snapshots": [
                        {
                            "timestamp": 1784023200,
                            "funding_rates": {"2": "110000000000000"},
                        },
                        {
                            "timestamp": 1784026800,
                            "funding_rates": {"2": "120000000000000"},
                        },
                    ]
                }
        raise AssertionError((url, payload))

class FakeWOOXHttp:
    def get_json(self, url: str) -> Any:
        if "/v3/public/futures" in url:
            return {
                "success": True,
                "data": {
                    "rows": [
                        {
                            "symbol": "PERP_BTC_USDT",
                            "indexPrice": "100",
                            "markPrice": "100",
                            "estFundingRate": "0.0004",
                            "openInterest": "10",
                            "24hAmount": "1000000",
                            "nextFundingTime": 1784044800000,
                        }
                    ]
                },
            }
        if "/v1/public/funding_rates" in url:
            return {
                "success": True,
                "rows": [
                    {
                        "symbol": "PERP_BTC_USDT",
                        "est_funding_rate": 0.0004,
                        "next_funding_time": 1784044800000,
                        "est_funding_rate_interval": 4,
                    }
                ],
            }
        if "/v1/public/funding_rate/PERP_BTC_USDT" in url:
            return {
                "success": True,
                "symbol": "PERP_BTC_USDT",
                "est_funding_rate": 0.0004,
                "next_funding_time": 1784044800000,
                "est_funding_rate_interval": 4,
            }
        if "/v3/public/orderbook?" in url:
            return {
                "success": True,
                "data": {
                    "bids": [{"price": "99.9", "quantity": "2"}],
                    "asks": [{"price": "100.1", "quantity": "3"}],
                },
            }
        if "/v1/public/funding_rate_history?" in url:
            return {
                "success": True,
                "rows": [
                    {
                        "symbol": "PERP_BTC_USDT",
                        "funding_rate": 0.0002,
                        "funding_rate_timestamp": 1784016000000,
                        "mark_price": 100,
                    },
                    {
                        "symbol": "PERP_BTC_USDT",
                        "funding_rate": 0.0004,
                        "funding_rate_timestamp": 1784030400000,
                        "mark_price": 100,
                    },
                ],
            }
        raise AssertionError(url)

class FakeCoinExHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/v2/futures/market"):
            return {
                "code": 0,
                "message": "OK",
                "data": [
                    {
                        "market": "BTCUSDT",
                        "base_ccy": "BTC",
                        "quote_ccy": "USDT",
                        "contract_type": "linear",
                        "status": "online",
                        "is_market_available": True,
                        "is_api_trading_available": True,
                        "maker_fee_rate": "0.0003",
                        "taker_fee_rate": "0.0005",
                    },
                    {
                        "market": "BTCUSD",
                        "base_ccy": "BTC",
                        "quote_ccy": "USD",
                        "contract_type": "inverse",
                        "status": "online",
                        "is_market_available": True,
                        "is_api_trading_available": True,
                    },
                ],
            }
        if "/v2/futures/funding-rate-history?" in url:
            return {
                "code": 0,
                "message": "OK",
                "data": [
                    {
                        "market": "BTCUSDT",
                        "actual_funding_rate": "0.0002",
                        "funding_time": 1784016000000,
                    },
                    {
                        "market": "BTCUSDT",
                        "actual_funding_rate": "0.0004",
                        "funding_time": 1784030400000,
                    },
                ],
            }
        if "/v2/futures/funding-rate" in url:
            return {
                "code": 0,
                "message": "OK",
                "data": [
                    {
                        "market": "BTCUSDT",
                        "next_funding_rate": "0.0004",
                        "latest_funding_time": 1784030400000,
                        "next_funding_time": 1784044800000,
                        "mark_price": "100",
                        "max_funding_rate": "0.003",
                        "min_funding_rate": "-0.003",
                    }
                ],
            }
        if "/v2/futures/ticker" in url:
            return {
                "code": 0,
                "message": "OK",
                "data": [
                    {
                        "market": "BTCUSDT",
                        "mark_price": "100",
                        "index_price": "100",
                        "open_interest_volume": "10",
                        "value": "1000000",
                    }
                ],
            }
        if "/v2/futures/depth?" in url:
            return {
                "code": 0,
                "message": "OK",
                "data": {
                    "depth": {
                        "bids": [["99.9", "2"]],
                        "asks": [["100.1", "3"]],
                    }
                },
            }
        raise AssertionError(url)

class FakeBitunixHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/api/v1/futures/market/trading_pairs"):
            return {
                "code": 0,
                "msg": "Success",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "base": "BTC",
                        "quote": "USDT",
                        "symbolStatus": "OPEN",
                        "isApiSupported": True,
                        "maxFundingRate": "0.3",
                        "minFundingRate": "-0.3",
                    }
                ],
            }
        if url.endswith("/api/v1/futures/market/tickers"):
            return {
                "code": 0,
                "msg": "Success",
                "data": [{"symbol": "BTCUSDT", "markPrice": "100", "quoteVol": "1000000"}],
            }
        if "/api/v1/futures/market/funding_rate/batch" in url:
            return {
                "code": 0,
                "msg": "Success",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "markPrice": "100",
                        "indexPrice": "100",
                        "fundingRate": "0.007459",
                        "fundingInterval": 8,
                        "nextFundingTime": "1784044800000",
                        "maxFundingRate": "0.3",
                        "minFundingRate": "-0.3",
                    }
                ],
            }
        if "/api/v1/futures/market/funding_rate?" in url:
            return {
                "code": 0,
                "msg": "Success",
                "data": {
                    "symbol": "BTCUSDT",
                    "markPrice": "100",
                    "indexPrice": "100",
                    "fundingRate": "0.007459",
                    "fundingInterval": 8,
                    "nextFundingTime": "1784044800000",
                    "maxFundingRate": "0.3",
                    "minFundingRate": "-0.3",
                },
            }
        if "/api/v1/futures/market/depth?" in url:
            return {
                "code": 0,
                "msg": "Success",
                "data": {"bids": [["99.9", "2"]], "asks": [["100.1", "3"]]},
            }
        raise AssertionError(url)

class FakeBitMartHttp:
    def get_json(self, url: str) -> Any:
        if "/contract/public/details" in url:
            return {
                "code": 1000,
                "message": "Ok",
                "data": {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "product_type": 1,
                            "expire_timestamp": 0,
                            "delist_time": 0,
                            "base_currency": "BTC",
                            "quote_currency": "USDT",
                            "contract_size": "0.001",
                            "last_price": "100",
                            "index_price": "100",
                            "expected_funding_rate": "0.0008",
                            "funding_time": 1784044800000,
                            "funding_interval_hours": 8,
                            "open_interest_value": "1000000",
                            "turnover_24h": "2000000",
                            "status": "Trading",
                            "tradfi_info": None,
                        }
                    ]
                },
            }
        if "/contract/public/funding-rate-v2" in url:
            return {
                "code": 1000,
                "message": "Ok",
                "data": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "expected_rate": "0.0008",
                            "rate_value": "0.0007",
                            "funding_time": 1784044800000,
                        }
                    ]
                },
            }
        if "/contract/public/funding-rate?" in url:
            return {
                "code": 1000,
                "message": "Ok",
                "data": {
                    "symbol": "BTCUSDT",
                    "expected_rate": "0.0008",
                    "funding_time": 1784044800000,
                },
            }
        if "/contract/public/depth?" in url:
            return {
                "code": 1000,
                "message": "Ok",
                "data": {"bids": [["99.9", "2"]], "asks": [["100.1", "3"]]},
            }
        if "/contract/public/funding-rate-history?" in url:
            return {
                "code": 1000,
                "message": "Ok",
                "data": {
                    "list": [
                        {"symbol": "BTCUSDT", "funding_rate": "0.0007", "funding_time": "1784016000000"},
                        {"symbol": "BTCUSDT", "funding_rate": "0.0008", "funding_time": "1784044800000"},
                    ]
                },
            }
        raise AssertionError(url)

class FakeBloFinHttp:
    def get_json(self, url: str) -> Any:
        if "/api/v1/market/instruments?" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "baseCurrency": "BTC",
                        "quoteCurrency": "USDT",
                        "settleCurrency": "USDT",
                        "contractValue": "0.001",
                        "instType": "SWAP",
                        "contractType": "linear",
                        "state": "live",
                    },
                    {
                        "instId": "BTC-USD",
                        "baseCurrency": "BTC",
                        "quoteCurrency": "USD",
                        "settleCurrency": "BTC",
                        "contractValue": "1",
                        "instType": "SWAP",
                        "contractType": "inverse",
                        "state": "live",
                    },
                ],
            }
        if "/api/v1/market/funding-rate-history?" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [
                    {"instId": "BTC-USDT", "fundingRate": "0.0007", "fundingTime": "1784016000000"},
                    {"instId": "BTC-USDT", "fundingRate": "0.0008", "fundingTime": "1784044800000"},
                ],
            }
        if "/api/v1/market/funding-rate" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "fundingRate": "0.0008",
                        "fundingTime": "1784044800000",
                    }
                ],
            }
        if "/api/v1/market/mark-price" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [{"instId": "BTC-USDT", "markPrice": "100", "indexPrice": "100"}],
            }
        if "/api/v1/market/tickers?" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [{"instId": "BTC-USDT", "last": "100", "volCurrency24h": "1000000"}],
            }
        if "/api/v1/market/books?" in url:
            return {
                "code": "0",
                "msg": "success",
                "data": [{"bids": [["99.9", "2"]], "asks": [["100.1", "3"]]}],
            }
        raise AssertionError(url)

class FakePhemexHttp:
    def get_json(self, url: str) -> Any:
        if url.endswith("/public/products"):
            return {
                "code": 0,
                "msg": "OK",
                "data": {
                    "perpProductsV2": [
                        {
                            "symbol": "BTCUSDT",
                            "type": "PerpetualV2",
                            "status": "Listed",
                            "baseCurrency": "BTC",
                            "contractUnderlyingAssets": "BTC",
                            "quoteCurrency": "USDT",
                            "settleCurrency": "USDT",
                            "fundingInterval": 28800,
                        }
                    ]
                },
            }
        if url.endswith("/md/v2/ticker/24hr/all"):
            return {
                "error": None,
                "id": 0,
                "result": [
                    {
                        "symbol": "BTCUSDT",
                        "predFundingRateRr": "0.0008",
                        "fundingRateRr": "0.0007",
                        "markPriceRp": "100",
                        "indexPriceRp": "100",
                        "openInterestRv": "10",
                        "turnoverRv": "1000000",
                    }
                ],
            }
        if "/md/v2/ticker/24hr?" in url:
            return {
                "error": None,
                "id": 0,
                "result": {
                    "symbol": "BTCUSDT",
                    "predFundingRateRr": "0.0008",
                    "fundingRateRr": "0.0007",
                    "markPriceRp": "100",
                    "indexPriceRp": "100",
                    "openInterestRv": "10",
                    "turnoverRv": "1000000",
                },
            }
        if "/md/v2/orderbook?" in url:
            return {
                "error": None,
                "id": 0,
                "result": {
                    "orderbook_p": {
                        "bids": [["99.9", "2"]],
                        "asks": [["100.1", "3"]],
                    }
                },
            }
        raise AssertionError(url)

class FakeAevoHttp:
    def get_json(self, url: str) -> Any:
        if "/markets?" in url:
            return [
                {
                    "instrument_id": "1",
                    "instrument_name": "BTC-PERP",
                    "instrument_type": "PERPETUAL",
                    "underlying_asset": "BTC",
                    "quote_asset": "USDC",
                    "mark_price": "100",
                    "index_price": "100",
                    "is_active": True,
                    "is_rwa": False,
                    "market_type": "crypto",
                }
            ]
        if "/funding-history?" in url:
            return {
                "funding_history": [
                    ["BTC-PERP", "1784030400000000000", "0.00008", "100"],
                    ["BTC-PERP", "1784034000000000000", "0.0001", "100"],
                ]
            }
        if "/funding?" in url:
            return {
                "next_epoch": "1784034000000000000",
                "funding_rate": "0.0001",
            }
        if "/orderbook?" in url:
            return {
                "bids": [["99.9", "2"]],
                "asks": [["100.1", "3"]],
            }
        raise AssertionError(url)

class FailingClient:
    venue = "bybit"

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        raise FundingDataError("temporary outage")

def bybit_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": rows, "nextPageCursor": ""},
    }

def bitget_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": "00000", "msg": "success", "data": rows}

def okx_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": "0", "msg": "", "data": rows}

def kraken_payload(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"result": "success", key: rows}

def market(
    venue: str,
    symbol: str,
    asset: str,
    funding_rate: float,
    interval_hours: float,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "venue": venue,
        "environment": "mainnet",
        "environment_verified": True,
        "endpoint_base_url": f"https://api.{venue}.test",
        "endpoint_identity_provenance": f"test-fixture:{venue}:endpoint:v1",
        "endpoint_client_version": "test-client-v1",
        "endpoint_verified_at": observed_at,
        "api_product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "data_enabled": True,
        "strategy_observation_enabled": True,
        "shadow_candidate_enabled": True,
        "paper_enabled": True,
        "live_enabled": False,
        "execution_model": "CLOB",
        "settlement_verification_level": "test_fixture",
        "symbol": symbol,
        "canonical_asset": asset,
        "funding_rate": funding_rate,
        "raw_funding_rate": funding_rate,
        "normalized_next_funding_rate": funding_rate,
        "raw_funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "funding_interval_hours": interval_hours,
        "hourly_funding_rate": funding_rate / interval_hours,
        "funding_rate_kind": "published_next_estimate",
        "next_funding_at": observed_at,
        "mark_price": 100.0,
        "index_price": 100.0,
        "open_interest_usd": 10_000_000,
        "volume_24h_usd": 30_000_000,
        "taker_fee_rate": 0.0005,
        "fee_source": "configured_trusted_fee",
        "fee_evidence": {
            "source_kind": "configured_trusted_fee",
            "source_identifier": f"test-fixture:{venue}:fees:v1",
            "trust_status": "CONFIGURED_TRUSTED",
            "venue": venue,
            "liquidity_role": "taker",
            "observed_at": observed_at,
            "reviewed_at": observed_at,
            "environment": "mainnet",
            "market_type": "linear_perpetual",
            "product_type": "linear_perpetual",
            "applicability": "taker",
            "evidence_version": "test-fee-evidence-v1",
        },
        "fee_observed_at": observed_at,
        "fee_reviewed_at": observed_at,
        "quantity_step": 0.001,
        "min_notional_usd": 5.0,
        "contract_type": "linear_perpetual",
        "contract_kind": "linear_perpetual",
        "supports_perpetuals": True,
        "supports_discrete_funding": True,
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "is_linear_contract": True,
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": f"adapter_{venue}_test",
        "request_started_at": observed_at,
        "response_received_at": observed_at,
        "normalized_at": observed_at,
        "observed_at": observed_at,
        "raw": {},
    }

def book(venue: str, symbol: str, observed_at: str) -> dict[str, Any]:
    return {
        "venue": venue,
        "symbol": symbol,
        "observed_at": observed_at,
        "bids": [[99.99, 2_000]],
        "asks": [[100.01, 2_000]],
        "best_bid": 99.99,
        "best_ask": 100.01,
        "mid_price": 100.0,
        "bid_depth_usd": 199_980,
        "ask_depth_usd": 200_020,
        "request_started_at": observed_at,
        "response_received_at": observed_at,
        "orderbook_event_time": observed_at,
        "raw": {},
    }

def shifted_book(
    venue: str,
    symbol: str,
    observed_at: str,
    mid_price: float,
) -> dict[str, Any]:
    spread = mid_price * 0.0001
    result = {
        "venue": venue,
        "symbol": symbol,
        "observed_at": observed_at,
        "bids": [[mid_price - spread, 2_000]],
        "asks": [[mid_price + spread, 2_000]],
        "best_bid": mid_price - spread,
        "best_ask": mid_price + spread,
        "mid_price": mid_price,
        "bid_depth_usd": (mid_price - spread) * 2_000,
        "ask_depth_usd": (mid_price + spread) * 2_000,
        "request_started_at": observed_at,
        "response_received_at": observed_at,
        "orderbook_event_time": observed_at,
        "raw": {},
    }
    result["_history"] = [
        {
            **result,
            "observed_at": (
                datetime.fromisoformat(observed_at) - timedelta(minutes=10 - index)
            ).isoformat(),
            "_history": [],
        }
        for index in range(10)
    ]
    return result

def tiered_book(venue: str, symbol: str, observed_at: str) -> dict[str, Any]:
    return {
        "venue": venue,
        "symbol": symbol,
        "observed_at": observed_at,
        "bids": [[99.99, 5.0], [99.88, 100.0]],
        "asks": [[100.01, 5.0], [100.12, 100.0]],
        "best_bid": 99.99,
        "best_ask": 100.01,
        "mid_price": 100.0,
        "bid_depth_usd": 10_487.95,
        "ask_depth_usd": 10_512.05,
        "raw": {},
    }

def sequence_book(
    observed_at: str,
    level_size: float,
    best_bid: float = 99.0,
    best_ask: float = 101.0,
) -> dict[str, Any]:
    return {
        "venue": "test",
        "symbol": "TEST",
        "observed_at": observed_at,
        "bids": [[best_bid, level_size]],
        "asks": [[best_ask, level_size]],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": (best_bid + best_ask) / 2.0,
        "bid_depth_usd": best_bid * level_size,
        "ask_depth_usd": best_ask * level_size,
        "raw": {},
    }

def history_rows(
    venue: str,
    symbol: str,
    end: datetime,
    interval_hours: int,
    hourly_rate: float,
    count: int,
    alternating: bool = False,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        timestamp = end - timedelta(hours=(count - index - 1) * interval_hours)
        signed_hourly = hourly_rate * (-1 if alternating and index % 2 else 1)
        rows.append(
            {
                "venue": venue,
                "symbol": symbol,
                "funding_at": timestamp.isoformat(),
                "funding_rate": signed_hourly * interval_hours,
                "funding_interval_hours": interval_hours,
                "hourly_funding_rate": signed_hourly,
                "mark_price": 100,
                "observed_at": observed_at or utc_now_iso(),
                "raw": {},
            }
        )
    return rows

def forecast_schedule(hours: int) -> dict[str, Any]:
    return {
        "horizon_mode": "fixed",
        "horizon_hours": float(hours),
        "horizon_label": f"{hours}h",
        "research_only": False,
        "authorization_valid_until": "2026-07-14T13:00:00+00:00",
        "paired_coverage_hours": float(hours),
        "settlements": [
            {
                "settlement_at": "2026-07-14T20:00:00+00:00",
                "hours_from_start": float(hours),
                "paired_coverage_hours": float(hours),
                "cumulative_paired_coverage_hours": float(hours),
                "settled_sides": ["long", "short"],
            }
        ],
    }

if __name__ == "__main__":
    unittest.main()
