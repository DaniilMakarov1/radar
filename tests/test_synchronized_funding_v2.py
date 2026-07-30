from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smart_money_radar.funding.profiles import funding_bot_profile
from smart_money_radar.funding.readiness_policy import EvaluationMode
from smart_money_radar.funding.fees import (
    fee_evidence_status,
    fee_override,
    fee_rate_value,
    funding_fee_rate,
    parsed_fee,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    FundingSettlementPlanner,
    STRATEGY_NAME,
    build_settlement_capture_opportunity,
    basis_duration_floor_bps,
    entry_underwriting,
    entry_window_passed,
    gross_funding_pnl,
    hold_economics,
    initial_entry_economics,
    settlement_alignment_passed,
    synchronized_strategy_candidate,
    validate_focused_observation,
)
from smart_money_radar.funding.venue_capabilities import (
    VenueCapability,
    capability_from_market,
    synchronized_capability_rejection,
    synchronized_paper_eligible,
    synchronized_route_capability_check,
    venue_inventory_rows,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.funding.service import active_default_funding_clients
from smart_money_radar.funding.stablecoins import (
    StablecoinPrice,
    StaticStablecoinPriceProvider,
    evaluate_stablecoin_route,
)
from smart_money_radar.paper_bot.accounting import executable_paper_pnl
from smart_money_radar.paper_bot.cycle_manager import evaluate_hold_history_reliability
from smart_money_radar.paper_bot.execution import entry_fill_state
from smart_money_radar.paper_bot.helpers import route_entry_key
from smart_money_radar.paper_bot.risk import common_price_move_telemetry, hard_risk_triggered
from smart_money_radar.paper_bot.settlement import (
    cycle_reconciled,
    funding_reconciliation_pnl,
    settlement_reconciliation_key,
)
from smart_money_radar.storage import SQLiteStore


def _trusted_fee_evidence(venue: str, observed_at: str) -> dict:
    return {
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
    }


def _trusted_paper_market_fields(venue: str, observed_at: str) -> dict:
    return {
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
        "settlement_verification_level": "adapter_contract",
        "fee_source": "configured_trusted_fee",
        "fee_evidence": _trusted_fee_evidence(venue, observed_at),
        "fee_observed_at": observed_at,
        "fee_reviewed_at": observed_at,
    }


def test_default_profile_uses_all_active_venues_and_sync_strategy() -> None:
    profile = funding_bot_profile("default")

    assert profile.venue_set is None
    assert profile.strategy_set == (STRATEGY_NAME,)


def test_deactivated_venues_are_excluded_from_default_clients() -> None:
    venues = {client.venue for client in active_default_funding_clients()}

    assert not venues.intersection(DEACTIVATED_FUNDING_VENUES)


def test_active_incompatible_venue_stays_research_only() -> None:
    rows = venue_inventory_rows(
        registered_venues=["continuousdex"],
        active_venues=["continuousdex"],
        sample_markets=[
            {
                "venue": "continuousdex",
                "funding_rate_kind": "published_continuous_hourly_equivalent",
                "collateral_asset": "USDC",
                "quote_asset": "USDC",
                "next_funding_at": "2026-07-19T12:00:00+00:00",
                "mark_price": 100.0,
                "index_price": 100.0,
                "volume_24h_usd": 25_000_000.0,
                "open_interest_usd": 10_000_000.0,
                "taker_fee_rate": 0.0005,
                "quantity_step": 0.001,
                "min_notional_usd": 5.0,
            }
        ],
    )

    assert rows[0]["status"] == "RESEARCH_ONLY"
    assert "continuous_or_unclear_funding" in rows[0]["reason"]


def test_missing_min_notional_blocks_paper_eligibility() -> None:
    capability = capability_from_market(
        {
            "venue": "example",
            "funding_rate_kind": "published_next_estimate",
            "collateral_asset": "USDT",
            "quote_asset": "USDT",
            "next_funding_at": "2026-07-19T12:00:00+00:00",
            "mark_price": 100.0,
            "index_price": 100.0,
            "volume_24h_usd": 25_000_000.0,
            "open_interest_usd": 10_000_000.0,
            "taker_fee_rate": 0.0005,
            "quantity_step": 0.001,
        }
    )

    assert "min_notional_missing" in synchronized_capability_rejection(capability)
    assert not synchronized_paper_eligible(capability)


def test_spread_convergence_is_not_expected_profit_for_sync_strategy() -> None:
    candidate = synchronized_strategy_candidate(
        {"funding_notional": 500.0, "execution_cost": 0.25, "basis_stress_loss": 0.0},
        current_funding_gross=0.0,
        actionable_profit_threshold=1.0,
        blocking_risk_flags=[],
        decision_mode="settlement_capture",
    )

    assert not candidate["eligible"]
    assert candidate["expected_spread_convergence_pnl"] == 0.0
    assert "conservative_funding_below_minimum" in candidate["reasons"]


def test_initial_funding_formula_matches_perp_cashflows() -> None:
    gross = gross_funding_pnl(
        quantity=5.0,
        long_mark=100.0,
        short_mark=100.0,
        long_funding_rate=0.001,
        short_funding_rate=0.004,
    )

    assert gross == pytest.approx(1.50)


def test_initial_entry_window_boundaries() -> None:
    assert not entry_window_passed(36)
    assert entry_window_passed(35)
    assert entry_window_passed(25)
    assert not entry_window_passed(24)


def test_settlement_skew_boundaries() -> None:
    base = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    assert settlement_alignment_passed(base, base + timedelta(seconds=1.0))
    assert not settlement_alignment_passed(base, base + timedelta(seconds=1.001))


def test_initial_entry_economics_thresholds() -> None:
    passed = initial_entry_economics(
        conservative_funding_gross=5.0,
        baseline_round_trip_book_cost=0.5,
        total_round_trip_fee_estimate=1.0,
        entry_basis_reserve_usd=0.5,
        entry_legging_reserve_usd=0.5,
        reference_notional=500.0,
    )
    failed = initial_entry_economics(
        conservative_funding_gross=2.0,
        baseline_round_trip_book_cost=0.5,
        total_round_trip_fee_estimate=1.0,
        entry_basis_reserve_usd=0.5,
        entry_legging_reserve_usd=0.5,
        reference_notional=500.0,
    )

    assert passed["eligible"]
    assert passed["expected_spread_convergence_pnl"] == 0.0
    assert not failed["eligible"]


@pytest.mark.parametrize(
    ("wait_seconds", "expected_bps"),
    [(3600, 25.0), (7200, 50.0), (10_800, 75.0), (14_400, 100.0)],
)
def test_hold_basis_duration_floor(wait_seconds: int, expected_bps: float) -> None:
    assert basis_duration_floor_bps(wait_seconds) == expected_bps


def test_hold_economics_does_not_recharge_open_fees() -> None:
    economics = hold_economics(
        next_conservative_funding_gross=10.0,
        current_close_fees=2.0,
        reference_notional=500.0,
        wait_seconds=3600,
        entry_basis_reserve_bps=25.0,
        adverse_basis_change_30s_bps=[0.0] * 10,
    )

    assert economics["incremental_hold_net_pnl"] == pytest.approx(6.8)
    assert economics["hold_cost_coverage_ratio"] == pytest.approx(3.125)


def test_current_executable_pnl_excludes_pending_funding() -> None:
    pnl = executable_paper_pnl(
        quantity=5.0,
        long_entry_price=100.10,
        long_exit_price=100.00,
        short_entry_price=100.20,
        short_exit_price=100.30,
        long_taker_fee=0.0005,
        short_taker_fee=0.0005,
        confirmed_funding_pnl=1.50,
    )

    assert pnl["paper_long_price_pnl"] == pytest.approx(-0.50)
    assert pnl["paper_short_price_pnl"] == pytest.approx(-0.50)
    assert pnl["paper_close_fees"] == pytest.approx(0.50075)
    assert pnl["paper_net_if_exit_now"] == pytest.approx(-0.00075)


def test_settlement_reconciliation_is_per_cycle_and_idempotent_shape() -> None:
    key = settlement_reconciliation_key(7, "binance", "2026-07-19T12:00:00+00:00")
    long_pnl = funding_reconciliation_pnl(
        side="long",
        quantity=5.0,
        settlement_mark_price=100.0,
        confirmed_funding_rate=0.001,
    )

    assert key == (7, "binance", "2026-07-19T12:00:00+00:00")
    assert long_pnl == pytest.approx(-0.50)
    assert cycle_reconciled(
        [
            {"reconciliation_status": "RATE_AND_MARK_RECONCILED"},
            {"reconciliation_status": "RATE_AND_MARK_RECONCILED"},
        ]
    )


def test_common_price_move_is_warning_not_automatic_close() -> None:
    telemetry = common_price_move_telemetry(
        {
            "price_move_tracking": True,
            "long_move_fraction": 0.10,
            "short_move_fraction": 0.095,
        },
        alert_fraction=0.05,
        critical_fraction=0.10,
    )

    assert telemetry["level"] == "critical"
    assert telemetry["requires_fresh_risk_recalculation"]


def test_hard_risk_boundaries() -> None:
    assert not hard_risk_triggered(basis_deterioration_bps=39.99, active_risk_budget_bps=40.0)[0]
    assert hard_risk_triggered(basis_deterioration_bps=40.0, active_risk_budget_bps=40.0)[0]
    assert not hard_risk_triggered(liquidation_distance_fraction=0.2001)[0]
    assert hard_risk_triggered(liquidation_distance_fraction=0.20)[0]
    assert not hard_risk_triggered(margin_safety_ratio=3.01)[0]
    assert hard_risk_triggered(margin_safety_ratio=3.0)[0]
    assert hard_risk_triggered(snapshot_age_seconds=5.01)[1] == "risk_data_hard_stale"


def test_partial_fill_and_quantity_mismatch_are_not_open() -> None:
    state = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=9.0,
        target_quantity=10.0,
    )
    mismatch = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=9.9899,
        target_quantity=10.0,
    )

    assert state["state"] == "PARTIALLY_HEDGED"
    assert state["quantity_mismatch_fraction"] == pytest.approx(0.10)
    assert mismatch["quantity_mismatch_fraction"] > 0.001


def test_multi_cycle_storage_tables_exist(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()

    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert {
        "funding_capture_positions",
        "funding_capture_cycles",
        "funding_capture_observations",
        "funding_paper_orders",
        "funding_settlement_reconciliations",
    }.issubset(tables)


def test_settlement_reconciliation_storage_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    position_id = store.upsert_funding_capture_position(
        {
            "position_id": "pos-1",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "quantity": 0.01,
            "target_notional": 500.0,
            "opened_at": "2026-07-19T12:00:00+00:00",
        }
    )
    cycle_id = store.upsert_funding_capture_cycle(
        {
            "position_id": position_id,
            "cycle_number": 1,
            "scheduled_funding_at": "2026-07-19T13:00:00+00:00",
            "state": "SETTLEMENT_CROSSED",
        }
    )
    payload = {
        "position_id": position_id,
        "cycle_id": cycle_id,
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "short",
        "scheduled_funding_at": "2026-07-19T13:00:00+00:00",
        "status": "RATE_AND_MARK_RECONCILED",
        "confirmed_funding_rate": 0.001,
        "settlement_mark_price": 100_000.0,
        "funding_pnl": 1.0,
        "evidence": {"source": "test"},
    }

    first_id = store.upsert_funding_settlement_reconciliation(payload)
    second_id = store.upsert_funding_settlement_reconciliation(
        {**payload, "funding_pnl": 1.25}
    )
    rows = store.funding_settlement_reconciliation_rows(position_id, cycle_id=cycle_id)

    assert first_id == second_id
    assert len(rows) == 1
    assert rows[0]["funding_pnl"] == pytest.approx(1.25)
    assert rows[0]["evidence"] == {"source": "test"}


def test_capability_contract_happy_path() -> None:
    capability = VenueCapability(
        venue="example",
        supports_perpetuals=True,
        is_linear_contract=True,
        contract_kind="linear_perpetual",
        collateral_asset="USDT",
        quote_asset="USDT",
        supports_discrete_funding=True,
        funding_rate_semantics="next_settlement",
        funding_rate_unit="fraction_of_notional_per_settlement",
        funding_sign_convention="positive_long_pays",
        supports_next_funding_timestamp=True,
        supports_mark_price=True,
        supports_index_price=True,
        supports_orderbook_timestamp=True,
        supports_orderbook_depth=True,
        supports_24h_quote_volume=True,
        supports_open_interest=True,
        supports_taker_fee=True,
        supports_quantity_step=True,
        supports_min_notional=True,
        normalized_next_funding_rate_present=True,
        position_inclusion_rule="perp_position_at_settlement",
        position_inclusion_rule_verified=True,
        entry_safety_buffer_seconds=20,
        exit_safety_buffer_seconds=20,
        timing_policy_source="adapter_example_test",
    )

    assert synchronized_paper_eligible(capability)
    assert math.isinf(
        synchronized_strategy_candidate(
            {"funding_notional": 500.0, "execution_cost": 0.0},
            current_funding_gross=5.0,
            actionable_profit_threshold=1.0,
            blocking_risk_flags=[],
            decision_mode="settlement_capture",
        )["coverage_ratio"]
    )


def test_close_decision_enforces_max_settlements(tmp_path) -> None:
    from smart_money_radar.paper_bot.position import close_decision
    from smart_money_radar.funding.trader import PaperBotConfig
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        max_settlements_per_position=2,
        max_position_age_seconds=86_400,
    ).validated()
    now = datetime(2026, 7, 19, 13, 0, 1, tzinfo=UTC)
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_settlement_at": "2026-07-19T13:00:00+00:00",
        "short_settlement_at": "2026-07-19T13:00:00+00:00",
        "max_settlement_at": "2026-07-19T13:00:00+00:00",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 1.0,
        "expected_live_net": 5.0,
        "expected_live_gross": 6.0,
        "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "next_funding_at": "2026-07-19T13:00:00+00:00", "funding_rate": 0.001, "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "next_funding_at": "2026-07-19T13:00:00+00:00", "funding_rate": 0.004, "funding_interval_hours": 1.0, "hourly_funding_rate": 0.004},
        ],
        "entry_evidence": {},
        "notes": {
            "accrued_settlement_count": 2,
            "accrued_funding_pnl": 3.0,
        },
    }
    result = close_decision(position, now, store, config)
    assert result["status"] == "close"
    assert result["close"]["close_reason"] == "max_settlements_reached"
    assert result["close"]["actual_funding_pnl"] == 3.0
    assert result["close"]["settlement"]["current_funding_pnl"] == 0.0
    assert not result["close"]["settlement"]["current_settlement_funding_included"]


def test_close_decision_enforces_max_position_age(tmp_path) -> None:
    from smart_money_radar.paper_bot.position import close_decision
    from smart_money_radar.funding.trader import PaperBotConfig
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        max_settlements_per_position=10,
        max_position_age_seconds=3600,
    ).validated()
    now = datetime(2026, 7, 19, 14, 0, 1, tzinfo=UTC)
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_settlement_at": "2026-07-19T14:00:00+00:00",
        "short_settlement_at": "2026-07-19T14:00:00+00:00",
        "max_settlement_at": "2026-07-19T14:00:00+00:00",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 1.0,
        "expected_live_net": 5.0,
        "expected_live_gross": 6.0,
        "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "next_funding_at": "2026-07-19T14:00:00+00:00", "funding_rate": 0.001, "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "next_funding_at": "2026-07-19T14:00:00+00:00", "funding_rate": 0.004, "funding_interval_hours": 1.0, "hourly_funding_rate": 0.004},
        ],
        "entry_evidence": {},
        "notes": {
            "accrued_settlement_count": 1,
            "accrued_funding_pnl": 1.5,
        },
    }
    result = close_decision(position, now, store, config)
    assert result["status"] == "close"
    assert result["close"]["close_reason"] == "max_position_age_reached"
    assert result["close"]["actual_funding_pnl"] == 1.5
    assert result["close"]["settlement"]["current_funding_pnl"] == 0.0
    assert not result["close"]["settlement"]["current_settlement_funding_included"]


def test_position_hold_decision_detects_interval_mismatch() -> None:
    """Different nominal intervals (1h vs 4h) but different next timestamps:
    funding_interval_hours is NOT a schedule gate; only timestamp alignment matters.
    Since position_hold_decision does not check timestamp alignment (that's entry-time),
    this scenario now holds (intervals alone don't close)."""
    from smart_money_radar.paper_bot.position import position_hold_decision
    from smart_money_radar.funding.trader import PaperBotConfig
    config = PaperBotConfig().validated()
    now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "notes": {},
    }
    route = {
        "status": "paper_candidate",
        "risk_flags": [],
        "observed_at": "2026-07-19T12:29:59+00:00",
        "legs": [
            {
                "side": "long",
                "venue": "binance",
                "environment": "mainnet",
                "symbol": "BTCUSDT",
                "next_funding_at": "2026-07-19T13:00:00+00:00",
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.001,
                "funding_rate": 0.001,
            },
            {
                "side": "short",
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "next_funding_at": "2026-07-19T16:00:00+00:00",
                "funding_interval_hours": 4.0,
                "hourly_funding_rate": 0.0005,
                "funding_rate": 0.002,
            },
        ],
        "evidence": {
            "selected_strategy": {
                "selection_model": "opportunity_engine_v1",
                "strategy_name": "synchronized_funding_capture",
                "eligible": True,
                "expected_net_pnl": 5.0,
                "funding_pnl_component": 5.0,
                "edge_type": "funding_led",
            },
        },
    }
    result = position_hold_decision(position, route, now, config)
    # funding_interval_hours mismatch alone no longer blocks hold
    assert "funding_interval_mismatch" not in result["reasons"]


def test_time_based_retention_prunes_old_history(tmp_path) -> None:
    from datetime import timedelta
    from smart_money_radar.funding.retention import (
        count_old_funding_history_by_age,
        prune_funding_history_by_age,
    )
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime.now(UTC)
    old_date = (now - timedelta(days=30)).isoformat(timespec="seconds")
    recent_date = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    observed = now.isoformat(timespec="seconds")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO funding_instruments
            (venue, symbol, canonical_asset, base_asset, quote_asset,
             collateral_asset, contract_type, status, observed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("binance", "BTCUSDT", "BTC", "BTC", "USDT", "USDT",
             "perpetual", "active", observed, observed),
        )
        conn.execute(
            """
            INSERT INTO funding_rate_history
            (venue, symbol, funding_at, funding_rate, funding_interval_hours,
             hourly_funding_rate, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("binance", "BTCUSDT", old_date, 0.001, 8.0, 0.000125, observed),
        )
        conn.execute(
            """
            INSERT INTO funding_rate_history
            (venue, symbol, funding_at, funding_rate, funding_interval_hours,
             hourly_funding_rate, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("binance", "BTCUSDT", recent_date, 0.002, 8.0, 0.00025, observed),
        )
    with store.connect() as conn:
        old_count = count_old_funding_history_by_age(conn, keep_older_than_seconds=7 * 86_400)
        assert old_count >= 1
        deleted = prune_funding_history_by_age(conn, keep_older_than_seconds=7 * 86_400)
        assert deleted >= 1
        remaining = conn.execute("SELECT COUNT(*) FROM funding_rate_history").fetchone()[0]
        assert remaining >= 1


# ---------------------------------------------------------------------------
# New tests for synchronized_funding_capture_v2 runtime constraints
# ---------------------------------------------------------------------------


def test_build_strategy_evaluation_not_called_by_synchronized_scanner(tmp_path, monkeypatch) -> None:
    """The synchronized path must not use legacy build_strategy_evaluation."""
    from smart_money_radar.funding.economics import evaluate_perp_route
    from smart_money_radar.funding import economics as economics_module
    from smart_money_radar.funding.models import FundingScanConfig

    def raise_if_called(*args, **kwargs):
        raise AssertionError("build_strategy_evaluation must not be called for settlement_capture")

    monkeypatch.setattr(
        economics_module,
        "build_strategy_evaluation",
        raise_if_called,
    )

    config = FundingScanConfig(horizon_mode="next_settlement").validated()
    long_market = {
        "venue": "binance",
        "environment": "mainnet",
        **_trusted_paper_market_fields("binance", "2026-07-28T15:59:30+00:00"),
        "symbol": "BTCUSDT",
        "canonical_asset": "BTC",
        "hourly_funding_rate": 0.0001,
        "funding_rate": 0.0008,
        "normalized_next_funding_rate": 0.0008,
        "funding_interval_hours": 8.0,
        "next_funding_at": "2026-07-28T16:00:00+00:00",
        "mark_price": 100_000.0,
        "index_price": 100_000.0,
        "volume_24h_usd": 1_000_000_000.0,
        "open_interest_usd": 500_000_000.0,
        "taker_fee_rate": 0.0005,
        "quantity_step": 0.001,
        "min_notional_usd": 5.0,
        "funding_rate_kind": "published_next_estimate",
        "contract_kind": "linear_perpetual",
        "supports_discrete_funding": True,
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": "adapter_binance_test",
        "observed_at": "2026-07-28T15:59:30+00:00",
    }
    short_market = {
        "venue": "bybit",
        "environment": "mainnet",
        **_trusted_paper_market_fields("bybit", "2026-07-28T15:59:30+00:00"),
        "symbol": "BTCUSDT",
        "canonical_asset": "BTC",
        "hourly_funding_rate": 0.0004,
        "funding_rate": 0.0032,
        "normalized_next_funding_rate": 0.0032,
        "funding_interval_hours": 8.0,
        "next_funding_at": "2026-07-28T16:00:00+00:00",
        "mark_price": 100_000.0,
        "index_price": 100_000.0,
        "volume_24h_usd": 800_000_000.0,
        "open_interest_usd": 400_000_000.0,
        "taker_fee_rate": 0.00055,
        "quantity_step": 0.001,
        "min_notional_usd": 5.0,
        "funding_rate_kind": "published_next_estimate",
        "contract_kind": "linear_perpetual",
        "supports_discrete_funding": True,
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": "adapter_bybit_test",
        "observed_at": "2026-07-28T15:59:30+00:00",
    }
    long_book = {
        "bids": [[99_990.0, 1.0], [99_980.0, 2.0]],
        "asks": [[100_010.0, 1.0], [100_020.0, 2.0]],
        "mid_price": 100_000.0,
        "observed_at": "2026-07-28T15:59:30+00:00",
        "response_received_at": "2026-07-28T15:59:30+00:00",
        "orderbook_event_time": "2026-07-28T15:59:30+00:00",
    }
    short_book = {
        "bids": [[99_990.0, 1.0], [99_980.0, 2.0]],
        "asks": [[100_010.0, 1.0], [100_020.0, 2.0]],
        "mid_price": 100_000.0,
        "observed_at": "2026-07-28T15:59:30+00:00",
        "response_received_at": "2026-07-28T15:59:30+00:00",
        "orderbook_event_time": "2026-07-28T15:59:30+00:00",
    }
    result = evaluate_perp_route(
        long_market,
        short_market,
        long_book,
        short_book,
        [],
        [],
        "2026-07-28T15:59:30+00:00",
        config,
    )
    assert result["status"] == "watch"
    evidence = result.get("evidence") or {}
    selected = evidence.get("selected_strategy")
    if selected is not None:
        assert selected.get("spread_pnl_component") == 0.0
        assert selected.get("signed_spread_pnl_component") == 0.0
        assert selected.get("expected_spread_convergence_pnl") == 0.0


def test_zero_funding_positive_spread_not_paper_candidate() -> None:
    """Zero funding + positive spread convergence must not become paper_candidate."""
    candidate = synchronized_strategy_candidate(
        {"funding_notional": 500.0, "execution_cost": 0.25, "basis_stress_loss": 0.0},
        current_funding_gross=0.0,
        actionable_profit_threshold=1.0,
        blocking_risk_flags=[],
        decision_mode="settlement_capture",
    )
    assert not candidate["eligible"]
    assert candidate["expected_spread_convergence_pnl"] == 0.0
    assert candidate["spread_pnl_component"] == 0.0
    assert candidate["signed_spread_pnl_component"] == 0.0
    assert "conservative_funding_below_minimum" in candidate["reasons"]


def test_settlement_capture_sizing_uses_funding_only_net_not_spread() -> None:
    from smart_money_radar.funding.economics import select_live_sizing_row

    funding_led = {
        "fill_complete": True,
        "current_nowcast_net": 2.0,
        "current_basis_stress_net_profit": 1.5,
        "current_opportunity_net": 2.0,
        "current_opportunity_basis_stress_net": 1.5,
        "notional": 500.0,
    }
    spread_inflated = {
        "fill_complete": True,
        "current_nowcast_net": 1.25,
        "current_basis_stress_net_profit": 0.75,
        "current_opportunity_net": 10.0,
        "current_opportunity_basis_stress_net": 9.0,
        "notional": 500.0,
    }

    assert select_live_sizing_row(
        [funding_led, spread_inflated],
        [funding_led, spread_inflated],
    ) is funding_led


def test_capability_fail_closed_missing_semantics() -> None:
    capability = VenueCapability(
        venue="unknown",
        collateral_asset="USDT",
        quote_asset="USDT",
    )
    rejections = synchronized_capability_rejection(capability)
    assert "funding_semantics_not_next_settlement" in rejections
    assert "funding_rate_unit_not_fraction_per_settlement" in rejections
    assert "funding_sign_convention_not_positive_long_pays" in rejections
    assert "contract_kind_not_linear_perpetual" in rejections
    assert not synchronized_paper_eligible(capability)


def test_capability_fail_closed_missing_unit_and_sign() -> None:
    capability = VenueCapability(
        venue="partial",
        supports_perpetuals=True,
        is_linear_contract=True,
        contract_kind="linear_perpetual",
        collateral_asset="USDT",
        quote_asset="USDT",
        supports_discrete_funding=True,
        funding_rate_semantics="next_settlement",
        supports_next_funding_timestamp=True,
        supports_mark_price=True,
        supports_index_price=True,
        supports_orderbook_timestamp=True,
        supports_orderbook_depth=True,
        supports_24h_quote_volume=True,
        supports_open_interest=True,
        supports_taker_fee=True,
        supports_quantity_step=True,
        supports_min_notional=True,
        normalized_next_funding_rate_present=True,
        position_inclusion_rule="perp_position_at_settlement",
        entry_safety_buffer_seconds=20,
        exit_safety_buffer_seconds=20,
        timing_policy_source="adapter_venue_a_test",
    )
    assert "funding_rate_unit_not_fraction_per_settlement" in synchronized_capability_rejection(capability)
    assert "funding_sign_convention_not_positive_long_pays" in synchronized_capability_rejection(capability)
    assert not synchronized_paper_eligible(capability)


def test_capability_from_market_does_not_infer_critical_contract_fields() -> None:
    capability = capability_from_market(
        {
            "venue": "optimistic",
            "funding_rate_kind": "published_next_estimate",
            "contract_type": "linear_perpetual",
            "is_linear_contract": True,
            "collateral_asset": "USDT",
            "quote_asset": "USDT",
            "next_funding_at": "2026-07-19T12:00:00+00:00",
            "mark_price": 100.0,
            "index_price": 100.0,
            "volume_24h_usd": 25_000_000.0,
            "open_interest_usd": 10_000_000.0,
            "taker_fee_rate": 0.0005,
            "quantity_step": 0.001,
            "min_notional_usd": 5.0,
            "orderbook_response_received_at": "2026-07-19T11:59:58+00:00",
            "orderbook_depth_available": True,
        }
    )

    rejections = synchronized_capability_rejection(capability)
    assert "contract_kind_not_linear_perpetual" in rejections
    assert "continuous_or_unclear_funding" in rejections
    assert "funding_semantics_not_next_settlement" in rejections
    assert "funding_rate_unit_not_fraction_per_settlement" in rejections
    assert "funding_sign_convention_not_positive_long_pays" in rejections
    assert not synchronized_paper_eligible(capability)


def test_usdt_vs_usdc_collateral_share_usd_route_universe() -> None:
    long_cap = VenueCapability(
        venue="venue_a",
        supports_perpetuals=True,
        is_linear_contract=True,
        contract_kind="linear_perpetual",
        collateral_asset="USDT",
        quote_asset="USDT",
        supports_discrete_funding=True,
        funding_rate_semantics="next_settlement",
        funding_rate_unit="fraction_of_notional_per_settlement",
        funding_sign_convention="positive_long_pays",
        supports_next_funding_timestamp=True,
        supports_mark_price=True,
        supports_index_price=True,
        supports_orderbook_timestamp=True,
        supports_orderbook_depth=True,
        supports_24h_quote_volume=True,
        supports_open_interest=True,
        supports_taker_fee=True,
        supports_quantity_step=True,
        supports_min_notional=True,
        normalized_next_funding_rate_present=True,
        position_inclusion_rule="perp_position_at_settlement",
        position_inclusion_rule_verified=True,
        entry_safety_buffer_seconds=20,
        exit_safety_buffer_seconds=20,
        timing_policy_source="adapter_venue_b_test",
    )
    short_cap = VenueCapability(
        venue="venue_b",
        supports_perpetuals=True,
        is_linear_contract=True,
        contract_kind="linear_perpetual",
        collateral_asset="USDC",
        quote_asset="USDC",
        supports_discrete_funding=True,
        funding_rate_semantics="next_settlement",
        funding_rate_unit="fraction_of_notional_per_settlement",
        funding_sign_convention="positive_long_pays",
        supports_next_funding_timestamp=True,
        supports_mark_price=True,
        supports_index_price=True,
        supports_orderbook_timestamp=True,
        supports_orderbook_depth=True,
        supports_24h_quote_volume=True,
        supports_open_interest=True,
        supports_taker_fee=True,
        supports_quantity_step=True,
        supports_min_notional=True,
        normalized_next_funding_rate_present=True,
        position_inclusion_rule="perp_position_at_settlement",
        position_inclusion_rule_verified=True,
        entry_safety_buffer_seconds=20,
        exit_safety_buffer_seconds=20,
        timing_policy_source="adapter_venue_b_test",
    )
    check = synchronized_route_capability_check(long_cap, short_cap)
    assert check["paper_eligible"]
    assert "collateral_asset_mismatch" not in check["cross_venue_reasons"]
    assert "quote_asset_mismatch" not in check["cross_venue_reasons"]


def test_response_skew_boundary_1_000_valid_1_001_invalid() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    funding_at = "2026-07-28T16:00:00+00:00"
    base_observation = {
        "long_age_seconds": 1.0,
        "short_age_seconds": 1.0,
        "long_next_funding_at": funding_at,
        "short_next_funding_at": funding_at,
        "long_book_executable": True,
        "short_book_executable": True,
        "capabilities_passed": True,
        "gross_funding_pnl": 1.0,
        "observed_at": now.isoformat(),
    }
    valid = validate_focused_observation(
        {
            **base_observation,
            "long_response_received_at": "2026-07-28T15:59:29.000+00:00",
            "short_response_received_at": "2026-07-28T15:59:30.000+00:00",
        },
        now=now,
    )
    assert valid["valid"], f"Expected valid but got reasons: {valid['reasons']}"

    invalid = validate_focused_observation(
        {
            **base_observation,
            "long_response_received_at": "2026-07-28T15:59:28.999+00:00",
            "short_response_received_at": "2026-07-28T15:59:30.000+00:00",
        },
        now=now,
    )
    assert not invalid["valid"]
    assert any("response_skew" in r for r in invalid["reasons"])


def test_focused_observation_age_boundary_5_000_valid_5_001_invalid() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    funding_at = "2026-07-28T16:00:00+00:00"
    base_observation = {
        "long_response_received_at": now.isoformat(),
        "short_response_received_at": now.isoformat(),
        "long_next_funding_at": funding_at,
        "short_next_funding_at": funding_at,
        "long_book_executable": True,
        "short_book_executable": True,
        "capabilities_passed": True,
        "gross_funding_pnl": 1.0,
        "observed_at": now.isoformat(),
    }

    valid = validate_focused_observation(
        {
            **base_observation,
            "long_age_seconds": 5.0,
            "short_age_seconds": 5.0,
        },
        now=now,
    )
    invalid = validate_focused_observation(
        {
            **base_observation,
            "long_age_seconds": 5.001,
            "short_age_seconds": 5.001,
        },
        now=now,
    )

    assert valid["valid"], f"Expected valid but got reasons: {valid['reasons']}"
    assert not invalid["valid"]
    assert any("age_exceeds_5.0s" in reason for reason in invalid["reasons"])


def test_nine_observations_no_entry() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    observations = [
        {
            "gross_funding_pnl": 1.0,
            "observed_at": (now - timedelta(seconds=20 - i * 2)).isoformat(),
        }
        for i in range(9)
    ]
    result = entry_underwriting(observations, now=now)
    assert not result["eligible"]
    assert any("insufficient_observations" in r for r in result["reasons"])


def test_ten_observations_span_19_9_no_entry() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    observations = [
        {
            "gross_funding_pnl": 1.0,
            "observed_at": (now - timedelta(seconds=19.9 - i * (19.9 / 9))).isoformat(),
        }
        for i in range(10)
    ]
    result = entry_underwriting(observations, now=now)
    assert not result["eligible"]
    assert any("observation_span" in r for r in result["reasons"])


def test_ten_observations_span_20_eligible() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    observations = [
        {
            "gross_funding_pnl": 1.0,
            "observed_at": (now - timedelta(seconds=20 - i * (20 / 9))).isoformat(),
        }
        for i in range(10)
    ]
    result = entry_underwriting(observations, now=now)
    assert result["eligible"], f"Expected eligible but got reasons: {result['reasons']}"
    assert result["observation_count"] == 10
    assert result["observation_span_seconds"] >= 20.0
    assert result["conservative_funding_gross"] == pytest.approx(0.9)


def test_discovered_position_payload_creates_correct_state() -> None:
    from smart_money_radar.paper_bot.cycle_manager import discovered_position_payload
    now = datetime(2026, 7, 28, 15, 59, 0, tzinfo=UTC)
    route = {
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "target_notional": 500.0,
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT"},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT"},
        ],
    }
    payload = discovered_position_payload(route, now=now)
    assert payload["state"] == "DISCOVERED"
    assert payload["quantity"] == 0.0
    assert payload["long_venue"] == "binance"
    assert payload["short_venue"] == "bybit"


def test_arm_decision_requires_ten_observations() -> None:
    from smart_money_radar.paper_bot.cycle_manager import arm_decision
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    position = {"state": "DISCOVERED"}
    route = {
        "legs": [
            {
                "side": "long",
                "venue": "binance",
                "environment": "mainnet",
                "symbol": "BTCUSDT",
                "next_funding_at": "2026-07-28T16:00:00+00:00",
            },
            {
                "side": "short",
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "next_funding_at": "2026-07-28T16:00:00+00:00",
            },
        ],
    }
    observations = [
        {
            "gross_funding_pnl": 1.0,
            "observed_at": (now - timedelta(seconds=20 - i * 2)).isoformat(),
        }
        for i in range(9)
    ]
    result = arm_decision(position, route, observations, now=now)
    assert not result["armed"]
    assert any("insufficient_observations" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# V2 execution, ledger, and settlement reconciliation tests
# ---------------------------------------------------------------------------


def test_entry_window_boundaries_t36_no_t35_yes_t25_yes_t24_no() -> None:
    """Entry window: T-36 no, T-35 yes, T-25 yes, T-24 no."""
    assert not entry_window_passed(36)
    assert entry_window_passed(35)
    assert entry_window_passed(25)
    assert not entry_window_passed(24)


def test_late_fill_after_t20_never_opens_and_unwinds() -> None:
    """Both legs filled after T-20 deadline: must not become OPEN."""
    from smart_money_radar.paper_bot.execution import t20_deadline_passed
    settlement = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    fill_at = datetime(2026, 7, 28, 15, 59, 50, tzinfo=UTC)
    assert not t20_deadline_passed(fill_at, settlement)
    fill_at_ok = datetime(2026, 7, 28, 15, 59, 39, tzinfo=UTC)
    assert t20_deadline_passed(fill_at_ok, settlement)


def test_one_leg_filled_never_open() -> None:
    """Only one leg filled: state must be PARTIALLY_HEDGED, never OPEN."""
    state = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=0.0,
        target_quantity=10.0,
    )
    assert state["state"] == "PARTIALLY_HEDGED"
    assert state["state"] != "OPEN"


def test_partial_fill_long_100_short_90_partially_hedged() -> None:
    """Long 100%, short 90% => PARTIALLY_HEDGED, unwind recorded."""
    state = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=9.0,
        target_quantity=10.0,
    )
    assert state["state"] == "PARTIALLY_HEDGED"
    assert state["long_fill_ratio"] == pytest.approx(1.0)
    assert state["short_fill_ratio"] == pytest.approx(0.9)
    assert state["quantity_mismatch_fraction"] == pytest.approx(0.1)


def test_quantity_mismatch_0100_allowed_0101_unwinds() -> None:
    """Quantity mismatch 0.100% allowed; 0.101% triggers unwind."""
    allowed = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=10.0 - 10.0 * 0.001,
        target_quantity=10.0,
    )
    assert allowed["state"] == "OPEN"

    unwind = entry_fill_state(
        long_filled_quantity=10.0,
        short_filled_quantity=10.0 - 10.0 * 0.00101,
        target_quantity=10.0,
    )
    assert unwind["state"] == "PARTIALLY_HEDGED"
    assert unwind["quantity_mismatch_fraction"] > 0.001


def test_pre_settlement_close_no_funding_pnl_no_ledger_entry(tmp_path) -> None:
    """Pre-settlement close has no funding PnL and no funding ledger entry."""
    from smart_money_radar.paper_bot.position import build_close_payload
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_settlement_at": "2026-07-19T13:00:00+00:00",
        "short_settlement_at": "2026-07-19T13:00:00+00:00",
        "max_settlement_at": "2026-07-19T13:00:00+00:00",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 1.0,
        "expected_live_net": 5.0,
        "expected_live_gross": 6.0,
        "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004},
        ],
        "entry_evidence": {},
        "notes": {},
    }
    close = build_close_payload(
        position,
        {"long": None, "short": None},
        None,
        use_entry_estimate_for_missing=False,
        close_reason="pre_settlement_risk_exit",
        include_current_settlement_funding=False,
    )
    assert close["settlement"]["current_funding_pnl"] == 0.0
    assert not close["settlement"]["current_settlement_funding_included"]
    assert close["actual_funding_pnl"] == 0.0


def test_missing_history_v2_no_phantom_funding(tmp_path) -> None:
    """After settlement with missing history => funding_pnl NULL, balance unchanged."""
    from smart_money_radar.paper_bot.position import build_close_payload
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_settlement_at": "2026-07-19T13:00:00+00:00",
        "short_settlement_at": "2026-07-19T13:00:00+00:00",
        "max_settlement_at": "2026-07-19T13:00:00+00:00",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 1.0,
        "expected_live_net": 5.0,
        "expected_live_gross": 6.0,
        "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004},
        ],
        "entry_evidence": {},
        "notes": {},
    }
    close = build_close_payload(
        position,
        {"long": None, "short": None},
        None,
        use_entry_estimate_for_missing=False,
        close_reason="settlement_capture_complete",
        v2_no_phantom_funding=True,
    )
    assert close["settlement"]["funding_pnl_null"] is True
    assert close["settlement"]["v2_no_phantom_funding"] is True
    assert close["settlement"]["history_missing_fallback"] is False
    assert close["actual_funding_pnl"] is None
    assert close["actual_net_pnl"] is None


def test_reconciliation_idempotency_duplicate_event_once(tmp_path) -> None:
    """Duplicate public event changes balance only once."""
    from smart_money_radar.paper_bot.accounting import funding_event_key, make_ledger_entry
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    key = funding_event_key("pos-1", "binance", "2026-07-19T13:00:00+00:00")
    entry = make_ledger_entry(
        key,
        position_id="pos-1",
        venue="binance",
        event_type="funding",
        cash_delta=1.50,
    )
    first = store.upsert_paper_event_ledger(entry)
    second = store.upsert_paper_event_ledger(entry)
    rows = store.paper_event_ledger_rows("pos-1")
    assert first == key
    assert second is None
    assert len(rows) == 1
    assert rows[0]["cash_delta"] == pytest.approx(1.50)


def test_rate_without_mark_no_balance_change() -> None:
    """Rate without mark: PUBLIC_RATE_CONFIRMED, funding_pnl NULL, no balance change."""
    from smart_money_radar.paper_bot.settlement import reconcile_leg
    result = reconcile_leg(
        public_rate=0.001,
        public_mark=None,
        side="long",
        quantity=0.01,
    )
    assert result["status"] == "PUBLIC_RATE_CONFIRMED"
    assert result["funding_pnl"] is None
    assert result["confirmed_funding_rate"] == pytest.approx(0.001)
    assert result["settlement_mark_price"] is None


def test_v2_storage_lifecycle_full(tmp_path) -> None:
    """V2 storage lifecycle: capture position, cycle, observations, orders, reconciliation."""
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    position_id = store.upsert_funding_capture_position({
        "position_id": "fc-test-1",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 0.01,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now.isoformat(),
    })
    cycle_id = store.upsert_funding_capture_cycle({
        "position_id": position_id,
        "cycle_number": 1,
        "scheduled_funding_at": "2026-07-28T16:00:00+00:00",
        "state": "SETTLEMENT_CROSSED",
    })
    obs_id = store.upsert_funding_capture_observation({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "phase": "entry",
        "observed_at": now.isoformat(),
        "snapshot_valid": True,
    })
    long_order_id = store.upsert_funding_paper_order({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "leg_side": "long",
        "order_intent": "entry",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "decision_at": now.isoformat(),
        "submitted_at": now.isoformat(),
        "filled_at": now.isoformat(),
        "filled_quantity": 0.01,
        "average_fill_price": 100_000.0,
        "fee": 0.05,
        "state": "FILLED",
    })
    short_order_id = store.upsert_funding_paper_order({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "leg_side": "short",
        "order_intent": "entry",
        "venue": "bybit",
        "symbol": "BTCUSDT",
        "decision_at": now.isoformat(),
        "submitted_at": now.isoformat(),
        "filled_at": now.isoformat(),
        "filled_quantity": 0.01,
        "average_fill_price": 100_000.0,
        "fee": 0.055,
        "state": "FILLED",
    })
    long_exit_id = store.upsert_funding_paper_order({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "leg_side": "long",
        "order_intent": "exit",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "decision_at": now.isoformat(),
        "filled_quantity": 0.01,
        "average_fill_price": 100_100.0,
        "state": "FILLED",
    })
    short_exit_id = store.upsert_funding_paper_order({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "leg_side": "short",
        "order_intent": "exit",
        "venue": "bybit",
        "symbol": "BTCUSDT",
        "decision_at": now.isoformat(),
        "filled_quantity": 0.01,
        "average_fill_price": 100_100.0,
        "state": "FILLED",
    })
    recon_long = store.upsert_funding_settlement_reconciliation({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "long",
        "scheduled_funding_at": "2026-07-28T16:00:00+00:00",
        "status": "RATE_AND_MARK_RECONCILED",
        "confirmed_funding_rate": 0.001,
        "settlement_mark_price": 100_000.0,
        "funding_pnl": -1.0,
    })
    recon_short = store.upsert_funding_settlement_reconciliation({
        "position_id": position_id,
        "cycle_id": cycle_id,
        "venue": "bybit",
        "symbol": "BTCUSDT",
        "side": "short",
        "scheduled_funding_at": "2026-07-28T16:00:00+00:00",
        "status": "RATE_AND_MARK_RECONCILED",
        "confirmed_funding_rate": 0.004,
        "settlement_mark_price": 100_000.0,
        "funding_pnl": 4.0,
    })
    orders = store.funding_paper_order_rows(position_id)
    recon_rows = store.funding_settlement_reconciliation_rows(position_id)
    assert position_id == "fc-test-1"
    assert len(orders) == 4
    assert len(recon_rows) == 2
    assert all(r["status"] == "RATE_AND_MARK_RECONCILED" for r in recon_rows)


def test_monkeypatch_open_funding_paper_position_v2_passes(tmp_path, monkeypatch) -> None:
    """Monkeypatch open_funding_paper_position to raise; v2 lifecycle must pass."""
    from smart_money_radar.paper_bot.settlement import build_settlement_crossing_rows
    from smart_money_radar.paper_bot.accounting import (
        funding_event_key,
        make_ledger_entry,
    )
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()

    def raise_on_legacy(*args, **kwargs):
        raise AssertionError("open_funding_paper_position must not be called in v2 path")

    monkeypatch.setattr(store, "open_funding_paper_position", raise_on_legacy)

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    capture_id = store.upsert_funding_capture_position({
        "position_id": "fc-v2-only-1",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 0.01,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now.isoformat(),
    })
    cycle_id = store.upsert_funding_capture_cycle({
        "position_id": capture_id,
        "cycle_number": 1,
        "scheduled_funding_at": "2026-07-28T16:00:00+00:00",
        "state": "SETTLEMENT_CROSSED",
    })
    rows = build_settlement_crossing_rows(
        position_id=capture_id,
        cycle_id=cycle_id,
        long_venue="binance",
        long_symbol="BTCUSDT",
        short_venue="bybit",
        short_symbol="BTCUSDT",
        scheduled_funding_at="2026-07-28T16:00:00+00:00",
        quantity=0.01,
    )
    for row in rows:
        store.upsert_funding_settlement_reconciliation(row)
    key = funding_event_key(capture_id, "binance", "2026-07-28T16:00:00+00:00")
    entry = make_ledger_entry(
        key,
        position_id=capture_id,
        venue="binance",
        event_type="funding",
        cash_delta=0.0,
    )
    store.upsert_paper_event_ledger(entry)
    recon_rows = store.funding_settlement_reconciliation_rows(capture_id)
    ledger_rows = store.paper_event_ledger_rows(capture_id)
    assert len(recon_rows) == 2
    assert all(r["status"] == "PENDING" for r in recon_rows)
    assert len(ledger_rows) == 1


def test_simulate_marketable_ioc_haircut() -> None:
    """simulate_marketable_ioc applies 0.40 haircut to each level."""
    from smart_money_radar.paper_bot.execution import simulate_marketable_ioc
    levels = [[100.0, 10.0], [101.0, 5.0]]
    result = simulate_marketable_ioc(levels, "buy", 6.0, 0.40)
    assert result["filled_quantity"] == pytest.approx(6.0)
    assert result["unfilled_quantity"] == pytest.approx(0.0)
    assert result["average_fill_price"] > 0

    levels_small = [[100.0, 1.0]]
    result_small = simulate_marketable_ioc(levels_small, "buy", 5.0, 0.40)
    assert result_small["filled_quantity"] == pytest.approx(0.4)
    assert result_small["unfilled_quantity"] == pytest.approx(4.6)


def test_venue_latency_tracker_blocks_high_p95() -> None:
    """Venue with p95 > 3000ms is blocked."""
    from smart_money_radar.paper_bot.execution import VenueLatencyTracker
    tracker = VenueLatencyTracker()
    for _ in range(20):
        tracker.record_rtt_ms(3500.0)
    assert tracker.is_blocked()
    assert tracker.simulated_latency_ms() == 3000.0

    tracker_ok = VenueLatencyTracker()
    for _ in range(20):
        tracker_ok.record_rtt_ms(500.0)
    assert not tracker_ok.is_blocked()
    assert tracker_ok.simulated_latency_ms() == 750.0


def test_exit_close_gate_requires_both_legs_or_residual() -> None:
    """EXIT_SUBMITTED -> CLOSED_PENDING_RECONCILIATION gate."""
    from smart_money_radar.paper_bot.cycle_manager import exit_close_gate
    both_closed = exit_close_gate(
        long_filled_quantity=10.0,
        short_filled_quantity=10.0,
        target_quantity=10.0,
    )
    assert both_closed["allowed"]
    assert both_closed["new_state"] == "CLOSED_PENDING_RECONCILIATION"

    partial_no_residual = exit_close_gate(
        long_filled_quantity=10.0,
        short_filled_quantity=5.0,
        target_quantity=10.0,
        residual_recorded=False,
    )
    assert not partial_no_residual["allowed"]
    assert partial_no_residual["new_state"] == "PARTIALLY_CLOSED"

    partial_with_residual = exit_close_gate(
        long_filled_quantity=10.0,
        short_filled_quantity=5.0,
        target_quantity=10.0,
        residual_recorded=True,
    )
    assert partial_with_residual["allowed"]


def test_reconcile_leg_rate_and_mark() -> None:
    """Rate + mark => RATE_AND_MARK_RECONCILED with computed funding_pnl."""
    from smart_money_radar.paper_bot.settlement import reconcile_leg
    result = reconcile_leg(
        public_rate=0.001,
        public_mark=100_000.0,
        side="long",
        quantity=0.01,
    )
    assert result["status"] == "RATE_AND_MARK_RECONCILED"
    assert result["funding_pnl"] == pytest.approx(-1.0)

    result_short = reconcile_leg(
        public_rate=0.004,
        public_mark=100_000.0,
        side="short",
        quantity=0.01,
    )
    assert result_short["status"] == "RATE_AND_MARK_RECONCILED"
    assert result_short["funding_pnl"] == pytest.approx(4.0)


def test_reconcile_leg_timeout() -> None:
    """No rate => UNRECONCILED."""
    from smart_money_radar.paper_bot.settlement import reconcile_leg
    result = reconcile_leg(
        public_rate=None,
        public_mark=None,
        side="long",
        quantity=0.01,
    )
    assert result["status"] == "UNRECONCILED"
    assert result["funding_pnl"] is None


def test_settlement_crossing_decision_detects_crossing() -> None:
    """Settlement crossing detection works correctly."""
    from smart_money_radar.paper_bot.cycle_manager import settlement_crossing_decision
    now = datetime(2026, 7, 28, 16, 0, 1, tzinfo=UTC)
    position = {
        "state": "OPEN",
        "max_settlement_at": "2026-07-28T16:00:00+00:00",
    }
    result = settlement_crossing_decision(position, now=now)
    assert result["crossed"]
    assert result["new_state"] == "SETTLEMENT_CROSSED"

    before = datetime(2026, 7, 28, 15, 59, 59, tzinfo=UTC)
    result_before = settlement_crossing_decision(position, now=before)
    assert not result_before["crossed"]


def test_v2_paper_accounting_no_basis_pnl_on_top() -> None:
    """V2 accounting: paper_net_if_exit_now = price_pnl + funding - fees, no extra basis."""
    from smart_money_radar.paper_bot.accounting import executable_paper_pnl
    pnl = executable_paper_pnl(
        quantity=0.01,
        long_entry_price=100_000.0,
        long_exit_price=100_100.0,
        short_entry_price=100_200.0,
        short_exit_price=100_100.0,
        long_taker_fee=0.0005,
        short_taker_fee=0.0005,
        confirmed_funding_pnl=3.0,
        paper_open_fees=1.0,
    )
    assert pnl["paper_long_price_pnl"] == pytest.approx(1.0)
    assert pnl["paper_short_price_pnl"] == pytest.approx(1.0)
    assert pnl["paper_price_pnl"] == pytest.approx(2.0)
    assert pnl["paper_net_if_exit_now"] == pytest.approx(
        2.0 + 3.0 - 1.0 - pnl["paper_close_fees"]
    )


# ---------------------------------------------------------------------------
# V2 runtime integration tests
# ---------------------------------------------------------------------------


def _v2_runtime_route(
    now: datetime,
    *,
    lead_seconds: float = 30.0,
    next_lead_seconds: float | None = None,
    short_next_lead_seconds: float | None = None,
    route_key: str = "BTC:binance:bybit",
) -> dict:
    next_at = now + timedelta(seconds=lead_seconds if next_lead_seconds is None else next_lead_seconds)
    short_next_at = now + timedelta(
        seconds=lead_seconds
        if short_next_lead_seconds is None and next_lead_seconds is None
        else next_lead_seconds
        if short_next_lead_seconds is None
        else short_next_lead_seconds
    )
    response_at = now
    return {
        "status": "watch",
        "route_key": route_key,
        "canonical_asset": "BTC",
        "target_notional": 500.0,
        "observed_at": now.isoformat(),
        "risk_flags": [],
        "long_venue": "binance",
        "short_venue": "bybit",
        "long_symbol": "BTCUSDT",
        "short_symbol": "BTCUSDT",
        "legs": [
            {
                "side": "long",
                "venue": "binance",
                "environment": "mainnet",
                **_trusted_paper_market_fields("binance", response_at.isoformat()),
                "canonical_asset": "BTC",
                "symbol": "BTCUSDT",
                "next_funding_at": next_at.isoformat(),
                "funding_rate": -0.008,
                "normalized_next_funding_rate": -0.008,
                "funding_rate_kind": "published_next_estimate",
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.008,
                "mark_price": 100.0,
                "index_price": 100.0,
                "open_vwap": 100.02,
                "close_vwap": 99.98,
                "best_bid": 99.98,
                "best_ask": 100.02,
                "fee_rate": 0.0005,
                "base_quantity": 5.0,
                "quantity_step": 0.01,
                "min_quantity": 0.01,
                "min_notional": 10.0,
                "contract_status": "active",
                "contract_kind": "linear_perpetual",
                "supports_perpetuals": True,
                "is_linear_contract": True,
                "supports_discrete_funding": True,
                "funding_rate_semantics": "next_settlement",
                "funding_rate_unit": "fraction_of_notional_per_settlement",
                "funding_sign_convention": "positive_long_pays",
                "collateral_asset": "USDT",
                "quote_asset": "USDT",
                "position_inclusion_rule": "perp_position_at_settlement",
                "entry_safety_buffer_seconds": 20,
                "exit_safety_buffer_seconds": 20,
                "timing_policy_source": "adapter_binance_test",
                "response_received_at": response_at.isoformat(),
                "source_event_at": response_at.isoformat(),
                "orderbook_response_received_at": response_at.isoformat(),
                "asks": [[100.02, 20.0]],
                "bids": [[99.98, 20.0]],
            },
            {
                "side": "short",
                "venue": "bybit",
                "environment": "mainnet",
                **_trusted_paper_market_fields("bybit", (response_at + timedelta(milliseconds=500)).isoformat()),
                "canonical_asset": "BTC",
                "symbol": "BTCUSDT",
                "next_funding_at": short_next_at.isoformat(),
                "funding_rate": 0.010,
                "normalized_next_funding_rate": 0.010,
                "funding_rate_kind": "published_next_estimate",
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.010,
                "mark_price": 100.0,
                "index_price": 100.0,
                "open_vwap": 99.98,
                "close_vwap": 100.02,
                "best_bid": 99.98,
                "best_ask": 100.02,
                "fee_rate": 0.0005,
                "base_quantity": 5.0,
                "quantity_step": 0.01,
                "min_quantity": 0.01,
                "min_notional": 10.0,
                "contract_status": "active",
                "contract_kind": "linear_perpetual",
                "supports_perpetuals": True,
                "is_linear_contract": True,
                "supports_discrete_funding": True,
                "funding_rate_semantics": "next_settlement",
                "funding_rate_unit": "fraction_of_notional_per_settlement",
                "funding_sign_convention": "positive_long_pays",
                "collateral_asset": "USDT",
                "quote_asset": "USDT",
                "position_inclusion_rule": "perp_position_at_settlement",
                "entry_safety_buffer_seconds": 20,
                "exit_safety_buffer_seconds": 20,
                "timing_policy_source": "adapter_bybit_test",
                "response_received_at": (response_at + timedelta(milliseconds=500)).isoformat(),
                "source_event_at": (response_at + timedelta(milliseconds=500)).isoformat(),
                "orderbook_response_received_at": (response_at + timedelta(milliseconds=500)).isoformat(),
                "asks": [[100.02, 20.0]],
                "bids": [[99.98, 20.0]],
            },
        ],
        "evidence": {
            "synchronized_capability_passed": True,
            "capability_rejections": [],
            "selected_strategy": {
                "selection_model": "synchronized_funding_capture_v2",
                "strategy_name": STRATEGY_NAME,
                "strategy_version": "synchronized_funding_capture_v2",
                "eligible": True,
                "expected_net_pnl": 4.0,
                "funding_pnl_component": 9.0,
                "spread_pnl_component": 0.0,
                "expected_spread_convergence_pnl": 0.0,
            },
        },
    }


def _valid_v2_observation(
    at: datetime,
    next_at: datetime,
    *,
    phase: str = "entry",
    short_next_at: datetime | None = None,
    gross_funding_pnl: float = 9.0,
) -> dict:
    short_next = short_next_at or next_at
    return {
        "phase": phase,
        "observed_at": at.isoformat(),
        "long_response_received_at": at.isoformat(),
        "short_response_received_at": (at + timedelta(milliseconds=500)).isoformat(),
        "long_age_seconds": 0.5,
        "short_age_seconds": 0.5,
        "cross_venue_skew_seconds": 0.5,
        "cross_venue_skew_ms": 500.0,
        "long_mark": 100.0,
        "short_mark": 100.0,
        "long_index": 100.0,
        "short_index": 100.0,
        "long_next_funding_at": next_at.isoformat(),
        "short_next_funding_at": short_next.isoformat(),
        "long_next_funding_rate": -0.008,
        "short_next_funding_rate": 0.010,
        "gross_funding_pnl": gross_funding_pnl,
        "long_open_vwap": 100.02,
        "short_open_vwap": 99.98,
        "long_close_vwap": 99.98,
        "short_close_vwap": 100.02,
        "current_exit_spread": 0.04,
        "paper_net_if_exit_now": None,
        "long_book_executable": True,
        "short_book_executable": True,
        "capabilities_passed": True,
        "snapshot_valid": True,
    }


def _open_v2_runtime_position(tmp_path, monkeypatch, now: datetime):
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    route = _v2_runtime_route(now)
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - index * 2), settlement_at)
        for index in range(9)
    ]

    def legacy_open_raises(*args, **kwargs):
        raise AssertionError("legacy open_funding_paper_position must not be called")

    def legacy_position_builder_raises(*args, **kwargs):
        raise AssertionError("legacy build_position_from_route must not be called")

    monkeypatch.setattr(store, "open_funding_paper_position", legacy_open_raises)
    monkeypatch.setattr(trader_module, "build_position_from_route", legacy_position_builder_raises)
    opened = bot.process_entry_candidates([route], recheck_before_open=False)
    _install_targeted_refresh_clients(bot, route["route_key"], route)
    return store, bot, route, capture_id, opened


def _seed_v2_entry_observations(
    bot,
    route: dict,
    now: datetime,
    *,
    long_next_at: datetime,
    short_next_at: datetime | None = None,
    gross_funding_pnl: float = 9.0,
) -> None:
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(
            now - timedelta(seconds=20 - index * 2),
            long_next_at,
            short_next_at=short_next_at,
            gross_funding_pnl=gross_funding_pnl,
        )
        for index in range(9)
    ]


def _fresh_route_for_open_position(route: dict, observed_at: datetime) -> dict:
    fresh = {**route, "observed_at": observed_at.astimezone(UTC).isoformat()}
    fresh_legs: list[dict] = []
    for leg in route.get("legs") or []:
        fresh_leg = dict(leg)
        fresh_leg["response_received_at"] = observed_at.astimezone(UTC).isoformat()
        fresh_leg["source_event_at"] = observed_at.astimezone(UTC).isoformat()
        fresh_leg["orderbook_response_received_at"] = observed_at.astimezone(UTC).isoformat()
        fresh_legs.append(fresh_leg)
    fresh["legs"] = fresh_legs
    return fresh


def _paper_bot_for_route(tmp_path, now: datetime):
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
    ).validated()
    return store, PaperBot(store, config, clock=FakeClock(now))


def _install_fresh_hot_route(bot, route: dict, *, next_lead_seconds: float = 3600.0) -> dict:
    current = bot.clock.now()
    fresh = _v2_runtime_route(
        current,
        next_lead_seconds=next_lead_seconds,
        short_next_lead_seconds=next_lead_seconds,
        route_key=route["route_key"],
    )
    bot.hot_routes[route["route_key"]] = fresh
    return fresh


class _TargetedRefreshClient:
    thread_safe = False

    def __init__(self, venue: str, route_provider):
        self.venue = venue
        self._route_provider = route_provider
        self.market_snapshot_calls = 0
        self.orderbook_calls = 0

    def _leg(self) -> dict:
        route = self._route_provider()
        for leg in route.get("legs") or []:
            if str(leg.get("venue") or "").lower() == self.venue:
                return dict(leg)
        raise AssertionError(f"missing targeted refresh leg for {self.venue}")

    def market_snapshot(self, symbol: str, asset: str, observed_at: str, previous: dict):
        self.market_snapshot_calls += 1
        leg = self._leg()
        return {
            **leg,
            "venue": self.venue,
            "symbol": symbol,
            "canonical_asset": asset,
            "status": leg.get("status") or "active",
            "contract_status": leg.get("contract_status") or "active",
            "contract_kind": leg.get("contract_kind") or "linear_perpetual",
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "funding_rate_kind": leg.get("funding_rate_kind") or "published_next_estimate",
            "funding_rate_semantics": leg.get("funding_rate_semantics") or "next_settlement",
            "funding_rate_unit": leg.get("funding_rate_unit") or "fraction_of_notional_per_settlement",
            "funding_sign_convention": leg.get("funding_sign_convention") or "positive_long_pays",
            "collateral_asset": leg.get("collateral_asset") or "USDT",
            "quote_asset": leg.get("quote_asset") or "USDT",
            "position_inclusion_rule": leg.get("position_inclusion_rule") or "perp_position_at_settlement",
            "entry_safety_buffer_seconds": leg.get("entry_safety_buffer_seconds") or 20,
            "exit_safety_buffer_seconds": leg.get("exit_safety_buffer_seconds") or 20,
            "timing_policy_source": leg.get("timing_policy_source") or f"adapter_{self.venue}_test",
            "observed_at": observed_at,
            "response_received_at": observed_at,
            "source_event_at": observed_at,
            "venue_server_time": observed_at,
        }

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100):
        self.orderbook_calls += 1
        leg = self._leg()
        return {
            "venue": self.venue,
            "symbol": symbol,
            "observed_at": observed_at,
            "bids": leg.get("bids") or [],
            "asks": leg.get("asks") or [],
            "best_bid": leg.get("best_bid"),
            "best_ask": leg.get("best_ask"),
            "mid_price": leg.get("mark_price"),
            "response_received_at": observed_at,
            "orderbook_event_time": observed_at,
        }

    def catalog_and_markets(self, observed_at: str):
        market = self.market_snapshot(self._leg().get("symbol", ""), "BTC", observed_at, {})
        return [], [market], []

    def funding_history(self, symbol: str, start_time_ms: int, interval_hours: float, observed_at: str):
        raise AssertionError("targeted refresh must not fetch history")


def _install_targeted_refresh_clients(bot, route_key: str, fallback_route: dict | None = None):
    def route_provider():
        return bot.hot_routes.get(route_key) or fallback_route or {}

    clients = [
        _TargetedRefreshClient("binance", route_provider),
        _TargetedRefreshClient("bybit", route_provider),
        _TargetedRefreshClient("okx", route_provider),
    ]
    bot.build_venue_clients = lambda: clients  # type: ignore[method-assign]
    return clients


def _boost_next_cycle_funding(route: dict, *, long_rate: float = -0.018, short_rate: float = 0.020) -> dict:
    long_leg = next(leg for leg in route["legs"] if leg["side"] == "long")
    short_leg = next(leg for leg in route["legs"] if leg["side"] == "short")
    for leg, rate in ((long_leg, long_rate), (short_leg, short_rate)):
        leg["funding_rate"] = rate
        leg["normalized_next_funding_rate"] = rate
        leg["hourly_funding_rate"] = rate / float(leg.get("funding_interval_hours") or 1.0)
    return route


def test_paperbot_v2_entry_uses_new_runtime_not_legacy_open(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, _bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)

    assert opened == [capture_id]
    assert store.funding_paper_open_positions() == []
    capture_rows = store.funding_capture_position_rows(states={"OPEN"})
    assert len(capture_rows) == 1
    assert capture_rows[0]["position_id"] == capture_id
    assert capture_rows[0]["quantity"] == pytest.approx(4.99)
    orders = store.funding_paper_order_rows(capture_id)
    assert [row["order_intent"] for row in orders] == ["ENTRY", "ENTRY"]
    assert {row["state"] for row in orders} == {"FILLED"}
    ledger = store.paper_event_ledger_rows(capture_id)
    event_types = [row["event_type"] for row in ledger]
    assert event_types.count("order_fee") == 2
    assert event_types.count("collateral_reserve") == 2


def test_paperbot_v2_accepts_one_settlement_alignment_mismatch(tmp_path) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    far = now + timedelta(hours=4)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now, short_next_lead_seconds=4 * 3600)
    route["legs"][0]["funding_rate"] = -0.014
    route["legs"][0]["normalized_next_funding_rate"] = -0.014
    route["legs"][0]["hourly_funding_rate"] = -0.014
    capture_id = capture_position_id_for_route(route)
    _seed_v2_entry_observations(
        bot,
        route,
        now,
        long_next_at=first,
        short_next_at=far,
        gross_funding_pnl=6.3,
    )

    opened = bot.process_entry_candidates([route], recheck_before_open=False)

    assert opened == [capture_id]
    position = store.funding_capture_position_by_id(capture_id)
    plan = position["config"]["funding_route_plan"]
    assert plan["opportunity_shape"] == "ONE_SETTLEMENT"
    assert [event["venue"] for event in plan["included_settlement_events"]] == ["binance"]
    assert [event["venue"] for event in plan["excluded_settlement_events"]] == ["bybit"]
    assert len(store.funding_capture_cycles_for_position(capture_id)) == 1


def test_paperbot_v2_actually_calls_shared_planner(tmp_path) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    class SpyPlanner:
        def __init__(self) -> None:
            self.delegate = FundingSettlementPlanner(
                evaluation_mode=EvaluationMode.VERIFIED_PAPER
            )
            self.calls: list[dict] = []

        def plan(self, **kwargs):
            self.calls.append(kwargs)
            return self.delegate.plan(**kwargs)

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    settlement_at = now + timedelta(seconds=30)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now)
    spy = SpyPlanner()
    bot.synchronized_runtime.planner = spy
    _seed_v2_entry_observations(bot, route, now, long_next_at=settlement_at)

    opened = bot.process_entry_candidates([route], recheck_before_open=False)

    assert opened == [capture_position_id_for_route(route)]
    assert spy.calls
    assert spy.calls[0]["long_market"]["venue"] == "binance"
    assert store.funding_capture_position_by_id(opened[0])["state"] == "OPEN"


def test_shadow_and_paperbot_route_plan_parity(tmp_path) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now, short_next_lead_seconds=50)
    paper_plan = bot.synchronized_runtime._plan_dict_for_route(route, now)
    long_leg = next(leg for leg in route["legs"] if leg["side"] == "long")
    short_leg = next(leg for leg in route["legs"] if leg["side"] == "short")
    shadow_plan = build_settlement_capture_opportunity(
        long_market=long_leg,
        short_market=short_leg,
        now=now,
        target_notional=500.0,
    )

    assert paper_plan["planner"]["selected_plan"] == shadow_plan["planner"]["selected_plan"]
    assert paper_plan["opportunity_shape"] == shadow_plan["opportunity_shape"]
    assert paper_plan["conservative_net_usd"] == pytest.approx(shadow_plan["conservative_net_usd"])
    assert [
        event["event_id"] for event in paper_plan["included_settlement_events"]
    ] == [event["event_id"] for event in shadow_plan["included_settlement_events"]]


def test_paperbot_v2_multi_event_survives_first_event_and_closes_after_last(tmp_path) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=20)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now, short_next_lead_seconds=50)
    route["legs"][0]["funding_settlement_events"] = [
        {"scheduled_at": first.isoformat(), "funding_rate": -0.008},
    ]
    route["legs"][1]["funding_settlement_events"] = [
        {"scheduled_at": second.isoformat(), "funding_rate": 0.010},
    ]
    capture_id = capture_position_id_for_route(route)
    _seed_v2_entry_observations(
        bot,
        route,
        now,
        long_next_at=first,
        short_next_at=second,
        gross_funding_pnl=8.1,
    )

    opened = bot.process_entry_candidates([route], recheck_before_open=False)

    assert opened == [capture_id]
    plan = store.funding_capture_position_by_id(capture_id)["config"]["funding_route_plan"]
    assert plan["opportunity_shape"] == "MULTIPLE_SETTLEMENTS"
    assert len(plan["included_settlement_events"]) == 2
    cycles = store.funding_capture_cycles_for_position(capture_id)
    assert [cycle["scheduled_funding_at"] for cycle in cycles] == [
        first.isoformat(),
        second.isoformat(),
    ]
    _install_targeted_refresh_clients(bot, route["route_key"], route)

    bot.clock.advance(31)
    bot.hot_routes[route["route_key"]] = _fresh_route_for_open_position(route, bot.clock.now())
    assert bot.process_open_positions() == ["settlement_crossed"]
    cycles = store.funding_capture_cycles_for_position(capture_id)
    assert [(cycle["cycle_number"], cycle["state"]) for cycle in cycles] == [
        (1, "SETTLEMENT_CROSSED"),
        (2, "OPEN"),
    ]

    bot.clock.advance(19)
    bot.hot_routes[route["route_key"]] = _fresh_route_for_open_position(route, bot.clock.now())
    assert bot.process_open_positions() == ["settlement_crossed"]
    cycles = store.funding_capture_cycles_for_position(capture_id)
    assert [(cycle["cycle_number"], cycle["state"]) for cycle in cycles] == [
        (1, "SETTLEMENT_CROSSED"),
        (2, "SETTLEMENT_CROSSED"),
    ]

    bot.clock.advance(5)
    bot.hot_routes[route["route_key"]] = _fresh_route_for_open_position(route, bot.clock.now())
    assert bot.process_open_positions() == ["closed"]
    assert store.funding_capture_position_by_id(capture_id)["state"] == "CLOSED_PENDING_RECONCILIATION"
    assert (
        store.funding_capture_position_by_id(capture_id)["config"]["payment_reconciliation_source"]
        == "SIMULATED"
    )


def test_paperbot_v2_settlement_crossing_creates_pending_reconciliation_only(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)

    outcomes = bot.process_open_positions()

    assert outcomes == ["settlement_crossed"]
    capture = store.funding_capture_position_rows(states={"SETTLEMENT_CROSSED"})[0]
    assert capture["settlements_captured_count"] == 1
    recon = store.funding_settlement_reconciliation_rows(capture_id)
    assert len(recon) == 2
    assert {row["status"] for row in recon} == {"PENDING"}
    assert all(row["funding_pnl"] is None for row in recon)
    assert "funding" not in {row["event_type"] for row in store.paper_event_ledger_rows(capture_id)}


def test_paperbot_v2_closes_after_planned_event_window_without_reconciled_prior_cycle(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    outcomes: list[str] = []
    all_outcomes: list[str] = []
    for offset in range(5, 31):
        target = settlement_at + timedelta(seconds=offset)
        bot.clock.advance((target - bot.clock.now()).total_seconds())
        current = bot.clock.now()
        next_route = _v2_runtime_route(
            current,
            lead_seconds=30,
            next_lead_seconds=(next_settlement - current).total_seconds(),
        )
        next_route["route_key"] = route["route_key"]
        _boost_next_cycle_funding(next_route)
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()
        all_outcomes.extend(outcomes)
        if outcomes:
            break

    assert all_outcomes == ["closed"]
    closed = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]
    assert closed["position_id"] == capture_id
    with store.connect() as conn:
        cycles = conn.execute(
            "SELECT cycle_number, state FROM funding_capture_cycles WHERE position_id = ? ORDER BY cycle_number",
            (capture_id,),
        ).fetchall()
    assert [(row[0], row[1]) for row in cycles] == [(1, "SETTLEMENT_CROSSED")]
    assert all(row["status"] == "PENDING" for row in store.funding_settlement_reconciliation_rows(capture_id))


def test_paperbot_v2_closes_when_next_timestamps_mismatch_without_phantom_funding(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]

    outcomes: list[str] = []
    for offset in range(5, 22):
        target = settlement_at + timedelta(seconds=offset)
        bot.clock.advance((target - bot.clock.now()).total_seconds())
        current = bot.clock.now()
        mismatch_route = _v2_runtime_route(
            current,
            lead_seconds=30,
            next_lead_seconds=3600,
            short_next_lead_seconds=3900,
        )
        mismatch_route["route_key"] = route["route_key"]
        bot.hot_routes[route["route_key"]] = mismatch_route
        outcomes = bot.process_open_positions()
        if outcomes:
            break

    assert outcomes == ["closed"]
    closed = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]
    assert closed["paper_net_pnl_estimated"] is not None
    assert len(store.funding_paper_order_rows(capture_id)) == 4
    ledger_types = [row["event_type"] for row in store.paper_event_ledger_rows(capture_id)]
    assert ledger_types.count("order_fee") == 4
    assert ledger_types.count("price_pnl") == 2
    assert "funding" not in ledger_types


def test_paperbot_v2_closes_different_intervals_after_planned_event_window(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    all_outcomes: list[str] = []
    for offset in range(5, 31):
        target = settlement_at + timedelta(seconds=offset)
        bot.clock.advance((target - bot.clock.now()).total_seconds())
        current = bot.clock.now()
        next_route = _v2_runtime_route(
            current,
            lead_seconds=30,
            next_lead_seconds=(next_settlement - current).total_seconds(),
        )
        next_route["route_key"] = route["route_key"]
        next_route["legs"][0]["funding_interval_hours"] = 1.0
        next_route["legs"][1]["funding_interval_hours"] = 4.0
        _boost_next_cycle_funding(next_route)
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()
        all_outcomes.extend(outcomes)
        if outcomes:
            break

    assert all_outcomes == ["closed"]
    closed = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]
    assert closed["position_id"] == capture_id


def test_paperbot_v2_rejects_hold_when_current_executable_pnl_is_too_negative(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    for idx in range(8):
        _seed_reconciled_hold_history_cycle(
            store,
            position_id=f"good-history-{idx}",
            created_at=f"2026-07-27T0{idx}:00:30+00:00",
            scheduled_at=f"2026-07-27T0{idx + 1}:00:00+00:00",
            predicted=10.0,
            realized=10.0,
        )
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    outcomes: list[str] = []
    all_outcomes: list[str] = []
    for offset in range(5, 31):
        target = settlement_at + timedelta(seconds=offset)
        bot.clock.advance((target - bot.clock.now()).total_seconds())
        current = bot.clock.now()
        next_route = _v2_runtime_route(
            current,
            lead_seconds=30,
            next_lead_seconds=(next_settlement - current).total_seconds(),
        )
        next_route["route_key"] = route["route_key"]
        next_route["legs"][0]["close_vwap"] = 80.0
        next_route["legs"][0]["bids"] = [[80.0, 20.0]]
        next_route["legs"][0]["best_bid"] = 80.0
        next_route["legs"][1]["close_vwap"] = 120.0
        next_route["legs"][1]["asks"] = [[120.0, 20.0]]
        next_route["legs"][1]["best_ask"] = 120.0
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()
        all_outcomes.extend(outcomes)

    assert any(item in {"closed", "emergency_unwind"} for item in all_outcomes)
    closed = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]
    assert closed["position_id"] == capture_id
    assert closed["paper_net_pnl_estimated"] < 0


def test_v2_entry_rejects_missing_normalized_next_funding_rate(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now))
    route = _v2_runtime_route(now)
    route["legs"][0].pop("normalized_next_funding_rate")
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - index * 2), settlement_at)
        for index in range(9)
    ]

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    assert result["opened"] is False
    assert result["reason"] in {"required_route_data_missing", "route_plan_blocked"}
    assert (
        "long_normalized_next_funding_rate_missing" in result.get("missing", [])
        or "long_next_rate_missing" in result.get("blockers", [])
    )
    assert store.funding_capture_position_by_id(capture_id)["state"] == "DISCOVERED"


def test_hold_proceeds_while_prior_reconciliation_pending(tmp_path) -> None:
    """Hold can proceed while prior reconciliation is PENDING (not yet confirmed)."""
    from smart_money_radar.paper_bot.position import close_decision
    from smart_money_radar.paper_bot.settlement import build_settlement_crossing_rows
    from smart_money_radar.funding.trader import PaperBotConfig
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    # Create a position with a PENDING reconciliation row
    position_id = store.upsert_funding_capture_position({
        "position_id": "fc-recon-hold-1",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 0.01,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": "2026-07-19T12:00:00+00:00",
    })
    cycle_id = store.upsert_funding_capture_cycle({
        "position_id": position_id,
        "cycle_number": 1,
        "scheduled_funding_at": "2026-07-19T13:00:00+00:00",
        "state": "SETTLEMENT_CROSSED",
    })
    rows = build_settlement_crossing_rows(
        position_id=position_id,
        cycle_id=cycle_id,
        long_venue="binance",
        long_symbol="BTCUSDT",
        short_venue="bybit",
        short_symbol="BTCUSDT",
        scheduled_funding_at="2026-07-19T13:00:00+00:00",
        quantity=0.01,
    )
    for row in rows:
        store.upsert_funding_settlement_reconciliation(row)
    recon_rows = store.funding_settlement_reconciliation_rows(position_id)
    assert all(r["status"] == "PENDING" for r in recon_rows)
    # Hold decision should not be blocked by pending reconciliation
    config = PaperBotConfig().validated()
    now = datetime(2026, 7, 19, 13, 0, 35, tzinfo=UTC)
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_settlement_at": "2026-07-19T13:00:00+00:00",
        "short_settlement_at": "2026-07-19T13:00:00+00:00",
        "max_settlement_at": "2026-07-19T13:00:00+00:00",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 1.0,
        "expected_live_net": 5.0,
        "expected_live_gross": 6.0,
        "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T14:00:00+00:00",
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T14:00:00+00:00",
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004},
        ],
        "entry_evidence": {},
        "notes": {"accrued_settlement_count": 1, "accrued_funding_pnl": 1.5},
    }
    # The hold decision is independent of reconciliation status
    from smart_money_radar.paper_bot.position import position_hold_decision
    route = {
        "status": "paper_candidate",
        "risk_flags": [],
        "observed_at": "2026-07-19T13:00:34+00:00",
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T14:00:00+00:00",
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T14:00:00+00:00",
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004},
        ],
        "evidence": {
            "selected_strategy": {
                "selection_model": "opportunity_engine_v1",
                "strategy_name": "synchronized_funding_capture",
                "eligible": True,
                "expected_net_pnl": 5.0,
                "funding_pnl_component": 5.0,
                "edge_type": "funding_led",
            },
        },
    }
    hold = position_hold_decision(position, route, now, config)
    assert hold["hold"], f"Expected hold but got reasons: {hold['reasons']}"


def test_different_timestamps_are_not_alignment_blockers() -> None:
    """Different timestamps are diagnostics; timing windows still gate legacy entry."""
    from smart_money_radar.paper_bot.position import route_entry_decision
    from smart_money_radar.funding.trader import PaperBotConfig
    config = PaperBotConfig().validated()
    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
    # Equal timestamps: eligible
    route_equal = {
        "status": "paper_candidate",
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "target_notional": 500.0,
        "risk_flags": [],
        "observed_at": now.isoformat(),
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=30)).isoformat(),
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001, "mark_price": 100000.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=30)).isoformat(),
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004, "mark_price": 100000.0},
        ],
        "evidence": {
            "selected_strategy": {
                "selection_model": "opportunity_engine_v1",
                "strategy_name": "synchronized_funding_capture",
                "eligible": True,
                "expected_net_pnl": 5.0,
                "funding_pnl_component": 5.0,
                "edge_type": "funding_led",
            },
        },
    }
    accounts = {"binance": {"available_balance": 10000.0}, "bybit": {"available_balance": 10000.0}}
    result_equal = route_entry_decision(route_equal, accounts, now, config)
    assert "settlement_alignment_mismatch" not in result_equal["reasons"]

    # Same interval but different timestamps: alignment is not the rejection reason.
    route_diff = {
        **route_equal,
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=30)).isoformat(),
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001, "mark_price": 100000.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=30 + 3600)).isoformat(),
             "funding_rate": 0.004, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.004, "mark_price": 100000.0},
        ],
    }
    result_diff = route_entry_decision(route_diff, accounts, now, config)
    assert "settlement_alignment_mismatch" not in result_diff["reasons"]
    assert "short_settlement_outside_final_entry_window" in result_diff["reasons"]


def test_different_nominal_intervals_equal_next_timestamp_passes() -> None:
    """Different nominal intervals (1h vs 4h) but equal next timestamp passes."""
    from smart_money_radar.paper_bot.position import position_hold_decision
    from smart_money_radar.funding.trader import PaperBotConfig
    config = PaperBotConfig().validated()
    now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    position = {
        "funding_paper_position_id": 1,
        "route_key": "BTC:binance:bybit",
        "opened_at": "2026-07-19T12:00:00+00:00",
        "notes": {},
    }
    route = {
        "status": "paper_candidate",
        "risk_flags": [],
        "observed_at": "2026-07-19T12:29:59+00:00",
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001, "funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-19T13:00:00+00:00",
             "funding_interval_hours": 4.0,
             "hourly_funding_rate": 0.002, "funding_rate": 0.008},
        ],
        "evidence": {
            "selected_strategy": {
                "selection_model": "opportunity_engine_v1",
                "strategy_name": "synchronized_funding_capture",
                "eligible": True,
                "expected_net_pnl": 5.0,
                "funding_pnl_component": 5.0,
                "edge_type": "funding_led",
            },
        },
    }
    result = position_hold_decision(position, route, now, config)
    assert result["hold"], f"Expected hold but got reasons: {result['reasons']}"


def test_1h_and_4h_aligned_next_settlements_can_hold() -> None:
    """1h and 4h venues with aligned next settlements can hold."""
    from smart_money_radar.paper_bot.cycle_manager import next_cycle_schedule_decision
    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
    next_settlement = "2026-07-19T13:00:00+00:00"
    result = next_cycle_schedule_decision(
        long_next_funding_at=next_settlement,
        short_next_funding_at=next_settlement,
        now=now,
    )
    assert result["hold_schedule"]
    assert result["settlement_skew_seconds"] == 0.0


def test_gt_4h_next_settlement_closes() -> None:
    """Next settlement > 4h away => close (too far)."""
    from smart_money_radar.paper_bot.cycle_manager import next_cycle_schedule_decision
    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
    far_future = "2026-07-19T17:00:01+00:00"  # > 14400s
    result = next_cycle_schedule_decision(
        long_next_funding_at=far_future,
        short_next_funding_at=far_future,
        now=now,
        max_wait_seconds=14_400.0,
    )
    assert not result["hold_schedule"]
    assert "next_settlement_too_far" in result["reasons"]


def test_open_fees_sunk_in_hold_economics() -> None:
    """Opening fees are sunk — never charged again in incremental hold."""
    economics = hold_economics(
        next_conservative_funding_gross=10.0,
        current_close_fees=2.0,
        reference_notional=500.0,
        wait_seconds=3600,
        entry_basis_reserve_bps=25.0,
        adverse_basis_change_30s_bps=[0.0] * 10,
    )
    # incremental_hold_net_pnl should NOT subtract original open fees
    # Only incremental costs (additional fee reserve + basis/legging/time/liquidity reserves)
    assert economics["incremental_hold_net_pnl"] > 0
    # Verify: funding=10, close_fee_stress=2.2, additional_fee_reserve=0.2
    # duration_floor=25, basis_reserve=25, scaled_observed=5 (default)
    # legging=15, time=10, liquidity=10 => total_reserve_bps=60
    # reserve_usd=500*60/10000=3.0, incremental_cost=0.2+3.0=3.2
    # incremental_net=10-3.2=6.8
    assert economics["incremental_hold_net_pnl"] == pytest.approx(6.8)


def test_hold_history_insufficient_haircut_no_veto() -> None:
    reliability = evaluate_hold_history_reliability([])

    assert reliability.status == "INSUFFICIENT"
    assert not reliability.gate_passed
    assert reliability.history_multiplier == pytest.approx(0.75)
    assert reliability.adjusted_funding(10.0) == pytest.approx(7.5)
    assert reliability.max_extra_cycles_when_insufficient == 1


def test_hold_history_positive_rate_fail() -> None:
    cycles = [
        {"predicted_gross": 10.0, "realized_gross": value}
        for value in [1.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0]
    ]

    reliability = evaluate_hold_history_reliability(cycles)

    assert reliability.status == "FAILED"
    assert not reliability.gate_passed
    assert "hold_history_positive_realization_rate_failed" in reliability.reasons


def test_hold_history_p25_fail() -> None:
    cycles = [
        {"predicted_gross": 10.0, "realized_gross": value}
        for value in [4.0, 4.0, 4.0, 6.0, 7.0, 8.0, 8.0, 8.0]
    ]

    reliability = evaluate_hold_history_reliability(cycles)

    assert reliability.status == "FAILED"
    assert reliability.p25_realization_ratio == pytest.approx(0.4)
    assert "hold_history_p25_realization_ratio_failed" in reliability.reasons


def test_good_hold_history_applies_p25_haircut() -> None:
    cycles = [{"predicted_gross": 10.0, "realized_gross": 8.0} for _ in range(8)]

    reliability = evaluate_hold_history_reliability(cycles)

    assert reliability.status == "PASSED"
    assert reliability.gate_passed
    assert reliability.history_multiplier == pytest.approx(0.8)
    assert reliability.adjusted_funding(10.0) == pytest.approx(8.0)


def test_good_hold_history_cannot_override_negative_current_funding() -> None:
    cycles = [{"predicted_gross": 10.0, "realized_gross": 10.0} for _ in range(8)]

    reliability = evaluate_hold_history_reliability(cycles)

    assert reliability.status == "PASSED"
    assert reliability.adjusted_funding(-5.0) == pytest.approx(-5.0)


def _seed_reconciled_hold_history_cycle(
    store: SQLiteStore,
    *,
    position_id: str,
    canonical_asset: str = "BTC",
    long_venue: str = "binance",
    short_venue: str = "bybit",
    collateral_asset: str = "USDT",
    state: str = "RECONCILED",
    reconciliation_status: str = "RATE_AND_MARK_RECONCILED",
    predicted: float = 10.0,
    realized: float = 8.0,
    created_at: str = "2026-07-28T16:00:30+00:00",
    scheduled_at: str = "2026-07-28T17:00:00+00:00",
) -> None:
    store.upsert_funding_capture_position({
        "position_id": position_id,
        "canonical_asset": canonical_asset,
        "long_venue": long_venue,
        "long_symbol": f"{canonical_asset}USDT",
        "short_venue": short_venue,
        "short_symbol": f"{canonical_asset}USDT",
        "quantity": 5.0,
        "target_notional": 500.0,
        "state": "RECONCILED",
        "opened_at": "2026-07-28T15:59:30+00:00",
        "closed_at": scheduled_at,
        "config": {
            "route_key": f"{canonical_asset}:{long_venue}:{short_venue}",
            "entry_legs": [
                {"side": "long", "venue": long_venue, "collateral_asset": collateral_asset},
                {"side": "short", "venue": short_venue, "collateral_asset": collateral_asset},
            ],
        },
    })
    store.upsert_funding_capture_cycle({
        "position_id": position_id,
        "cycle_number": 2,
        "scheduled_funding_at": scheduled_at,
        "conservative_funding_gross": predicted,
        "incremental_hold_net_pnl": predicted - 1.0,
        "decision": "HOLD",
        "state": state,
        "reconciliation_status": reconciliation_status,
        "reconciled_funding_pnl": realized,
        "created_at": created_at,
    })


def test_hold_history_query_uses_local_reconciled_same_scope_only(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_reconciled_hold_history_cycle(store, position_id="match")
    _seed_reconciled_hold_history_cycle(store, position_id="reverse", long_venue="bybit", short_venue="binance")
    _seed_reconciled_hold_history_cycle(store, position_id="other-collateral", collateral_asset="USDC")
    _seed_reconciled_hold_history_cycle(
        store,
        position_id="unreconciled",
        state="UNRECONCILED",
        reconciliation_status="UNRECONCILED",
    )

    rows = store.reconciled_funding_capture_hold_cycles(
        canonical_asset="BTC",
        long_venue="binance",
        short_venue="bybit",
        collateral_asset="USDT",
        wait_bucket="<=1h",
        since="2026-06-28T00:00:00+00:00",
        limit=20,
    )

    assert [row["position_id"] for row in rows] == ["match"]
    assert rows[0]["predicted_gross"] == pytest.approx(10.0)
    assert rows[0]["realized_gross"] == pytest.approx(8.0)


def test_risk_helper_called_by_paperbot_runtime(tmp_path, monkeypatch) -> None:
    """Direct v2 risk helper is called by real PaperBot process_open_positions."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    import smart_money_radar.paper_bot.runtime_v2 as runtime_module
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        telegram_enabled=False,
        venue_starting_balance=10_000.0,
    ).validated()
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    bot = PaperBot(store, config, clock=FakeClock(now))
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    store.upsert_funding_capture_position({
        "position_id": "fc-risk-runtime",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 0.01,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now.isoformat(),
        "paper_open_fees": 1.0,
        "config": {
            "route_key": "BTC:binance:bybit",
            "entry_legs": [
                {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
                 "next_funding_at": "2026-07-28T17:00:00+00:00",
                 "funding_rate": 0.001, "funding_interval_hours": 1.0,
                 "hourly_funding_rate": 0.001, "vwap": 100000.0},
                {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
                 "next_funding_at": "2026-07-28T17:00:00+00:00",
                 "funding_rate": 0.004, "funding_interval_hours": 1.0,
                 "hourly_funding_rate": 0.004, "vwap": 100000.0},
            ],
        },
    })
    store.upsert_funding_capture_cycle({
        "position_id": "fc-risk-runtime",
        "cycle_number": 1,
        "scheduled_funding_at": "2026-07-28T17:00:00+00:00",
        "state": "OPEN",
    })
    # Track that hard_risk_triggered is called by the synchronized runtime, not a trader legacy adapter.
    call_log = []
    original_hard_risk = runtime_module.hard_risk_triggered
    def tracking_hard_risk(*args, **kwargs):
        call_log.append(("hard_risk_triggered", kwargs))
        return original_hard_risk(*args, **kwargs)
    monkeypatch.setattr(runtime_module, "hard_risk_triggered", tracking_hard_risk)
    # Also need a live route for the position
    bot.hot_routes["BTC:binance:bybit"] = {
        "status": "paper_candidate",
            "route_key": "BTC:binance:bybit",
            "observed_at": now.isoformat(),
            "risk_flags": [],
            "legs": [
                {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
                 "next_funding_at": "2026-07-28T17:00:00+00:00",
                 "mark_price": 100000.0, "index_price": 100000.0,
                 "best_ask": 100001.0, "best_bid": 99999.0,
                 "bids": [[99999.0, 1.0]], "asks": [[100001.0, 1.0]],
                 "fee_rate": 0.0005,
                 "response_received_at": (now - timedelta(milliseconds=500)).isoformat(),
                 "orderbook_response_received_at": (now - timedelta(milliseconds=500)).isoformat(),
                 "funding_rate": 0.001, "normalized_next_funding_rate": 0.001,
                 "funding_interval_hours": 1.0,
                 "hourly_funding_rate": 0.001},
                {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
                 "next_funding_at": "2026-07-28T17:00:00+00:00",
                 "mark_price": 100000.0, "index_price": 100000.0,
                 "best_ask": 100001.0, "best_bid": 99999.0,
                 "bids": [[99999.0, 1.0]], "asks": [[100001.0, 1.0]],
                 "fee_rate": 0.0005,
                 "response_received_at": now.isoformat(),
                 "orderbook_response_received_at": now.isoformat(),
                 "funding_rate": 0.004, "normalized_next_funding_rate": 0.004,
                 "funding_interval_hours": 1.0,
                 "hourly_funding_rate": 0.004},
            ],
        "evidence": {
            "selected_strategy": {
                "selection_model": "opportunity_engine_v1",
                "strategy_name": "synchronized_funding_capture",
                "eligible": True,
                "expected_net_pnl": 5.0,
                "funding_pnl_component": 5.0,
                "edge_type": "funding_led",
            },
        },
    }
    _install_targeted_refresh_clients(
        bot,
        "BTC:binance:bybit",
        bot.hot_routes["BTC:binance:bybit"],
    )
    outcomes = bot.process_open_positions()
    # Verify hard_risk_triggered was called
    assert len(call_log) > 0, "hard_risk_triggered was not called by PaperBot runtime"
    assert call_log[0][1]["snapshot_age_seconds"] == pytest.approx(0.0)


def test_dynamic_basis_stop_boundary_39_99_40_00() -> None:
    """Basis stop: 39.99 bps deterioration with 40 bps budget => no exit; 40.00 => exit."""
    from smart_money_radar.paper_bot.risk import dynamic_basis_stop_decision
    # 39.99 < 40.0 budget => no exit
    result_below = dynamic_basis_stop_decision(
        entry_spread=0.0,
        current_exit_spread=39.99 / 10_000.0 * 100_000.0,
        reference_price=100_000.0,
        active_cycle_conservative_funding_edge_bps=100.0,  # budget = clamp(25,50,50) = 50
    )
    # budget is 50 bps (0.5 * 100 = 50, clamped to [25,50])
    # deterioration = 39.99 bps < 50 bps => no exit
    assert not result_below["immediate_hard_exit"]

    # Now test at exact boundary with budget=40
    result_at = dynamic_basis_stop_decision(
        entry_spread=0.0,
        current_exit_spread=40.0 / 10_000.0 * 100_000.0,
        reference_price=100_000.0,
        active_cycle_conservative_funding_edge_bps=80.0,  # budget = clamp(25,50,40) = 40
    )
    # deterioration = 40.0 bps >= 40.0 budget => exit
    assert result_at["immediate_hard_exit"]
    assert result_at["active_risk_budget_bps"] == pytest.approx(40.0)


def test_10pct_common_move_alerts_but_does_not_close_if_safe() -> None:
    """10% common move alerts but does not close if otherwise safe."""
    from smart_money_radar.paper_bot.risk import common_price_move_telemetry
    from smart_money_radar.paper_bot.position import price_stop_loss_triggered
    from smart_money_radar.funding.trader import PaperBotConfig
    telemetry = common_price_move_telemetry(
        {
            "price_move_tracking": True,
            "long_move_fraction": 0.10,
            "short_move_fraction": 0.098,
        },
        alert_fraction=0.05,
        critical_fraction=0.10,
    )
    assert telemetry["level"] == "critical"
    assert telemetry["requires_fresh_risk_recalculation"]
    # price_stop_loss_triggered always returns False (common move is telemetry only)
    config = PaperBotConfig().validated()
    triggered, reason = price_stop_loss_triggered(
        {"price_move_tracking": True, "long_move_fraction": 0.10, "short_move_fraction": 0.098},
        config,
    )
    assert not triggered


def test_hard_stale_after_retries_emergency_unwind() -> None:
    """Hard stale after retries => EMERGENCY_UNWIND with risk_data_hard_stale."""
    from smart_money_radar.paper_bot.risk import stale_data_decision
    # Healthy
    healthy = stale_data_decision(snapshot_age_seconds=1.5)
    assert healthy["status"] == "healthy"
    assert healthy["entry_allowed"]
    assert not healthy["emergency_unwind"]
    # Degraded with retries
    degraded = stale_data_decision(snapshot_age_seconds=3.0, stale_retries=0)
    assert degraded["status"] == "degraded"
    assert not degraded["entry_allowed"]
    assert degraded["retry_needed"]
    # Still degraded after max retries
    degraded_max = stale_data_decision(snapshot_age_seconds=4.0, stale_retries=3)
    assert degraded_max["status"] == "degraded"
    assert not degraded_max["retry_needed"]
    # Hard stale => emergency unwind
    hard_stale = stale_data_decision(snapshot_age_seconds=5.1)
    assert hard_stale["status"] == "hard_stale"
    assert hard_stale["emergency_unwind"]
    assert hard_stale["close_reason"] == "risk_data_hard_stale"
    assert hard_stale["penalty_bps"] == 100.0


def test_lightweight_discovery_finds_route_between_full_scans() -> None:
    """Lightweight discovery is scheduled between full scans."""
    from smart_money_radar.paper_bot.risk import lightweight_discovery_due, focused_recheck_capacity_check
    # Discovery is due after 30s
    assert lightweight_discovery_due(
        last_discovery_monotonic=0.0,
        now_monotonic=31.0,
    )
    # Not due before 30s
    assert not lightweight_discovery_due(
        last_discovery_monotonic=100.0,
        now_monotonic=120.0,
    )
    # Capacity check: within limits
    capacity_ok = focused_recheck_capacity_check(watch_route_count=10)
    assert capacity_ok["sufficient"]
    assert not capacity_ok["entry_forbidden"]
    # Capacity check: over limits
    capacity_over = focused_recheck_capacity_check(watch_route_count=25, max_focused_routes=20)
    assert not capacity_over["sufficient"]
    assert capacity_over["reason"] == "focused_recheck_capacity_insufficient"
    assert capacity_over["entry_forbidden"]


class _LightweightDiscoveryClient:
    def __init__(
        self,
        venue: str,
        *,
        asset: str = "ABC",
        funding_rate: float,
        next_funding_at: datetime,
        interval_hours: float = 1.0,
        mark_price: float = 100.0,
        include_fee: bool = True,
        include_volume: bool = True,
    ) -> None:
        self.venue = venue
        self.asset = asset
        self.symbol = f"{asset}USDT"
        self.funding_rate = funding_rate
        self.next_funding_at = next_funding_at
        self.interval_hours = interval_hours
        self.mark_price = mark_price
        self.include_fee = include_fee
        self.include_volume = include_volume
        self.catalog_calls = 0
        self.orderbook_calls = 0

    def catalog_and_markets(self, observed_at: str):
        self.catalog_calls += 1
        instrument = {
            "venue": self.venue,
            "environment": "mainnet",
            **_trusted_paper_market_fields(self.venue, observed_at),
            "symbol": self.symbol,
            "canonical_asset": self.asset,
            "base_asset": self.asset,
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "contract_type": "linear_perpetual",
            "contract_kind": "linear_perpetual",
            "supports_discrete_funding": True,
            "contract_multiplier": 0.01,
            "status": "active",
            "position_inclusion_rule": "perp_position_at_settlement",
            "entry_safety_buffer_seconds": 20,
            "exit_safety_buffer_seconds": 20,
            "timing_policy_source": f"adapter_{self.venue}_test",
            "observed_at": observed_at,
        }
        market = {
            "venue": self.venue,
            "environment": "mainnet",
            **_trusted_paper_market_fields(self.venue, observed_at),
            "symbol": self.symbol,
            "canonical_asset": self.asset,
            "funding_rate": self.funding_rate,
            "normalized_next_funding_rate": self.funding_rate,
            "funding_rate_semantics": "next_settlement",
            "funding_rate_unit": "fraction_of_notional_per_settlement",
            "funding_sign_convention": "positive_long_pays",
            "funding_interval_hours": self.interval_hours,
            "hourly_funding_rate": self.funding_rate / self.interval_hours,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": self.next_funding_at.isoformat(),
            "mark_price": self.mark_price,
            "index_price": self.mark_price,
            "open_interest_usd": 10_000_000.0,
            "volume_24h_usd": 30_000_000.0 if self.include_volume else None,
            "quantity_step": 0.01,
            "min_notional_usd": 5.0,
            "contract_kind": "linear_perpetual",
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "collateral_asset": "USDT",
            "quote_asset": "USDT",
            "position_inclusion_rule": "perp_position_at_settlement",
            "entry_safety_buffer_seconds": 20,
            "exit_safety_buffer_seconds": 20,
            "timing_policy_source": f"adapter_{self.venue}_test",
            "observed_at": observed_at,
            "response_received_at": observed_at,
            "source_event_at": observed_at,
        }
        if self.include_fee:
            market["taker_fee_rate"] = 0.0005
        return [instrument], [market], []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100):
        self.orderbook_calls += 1
        raise AssertionError("lightweight discovery must not fetch orderbooks")

    def funding_history(self, symbol: str, start_time_ms: int, interval_hours: float, observed_at: str):
        raise AssertionError("lightweight discovery must not fetch history")


def _lightweight_bot(
    tmp_path,
    now: datetime,
    clients: list[_LightweightDiscoveryClient],
    *,
    foreground_budget_seconds: float = 8.0,
):
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        telegram_enabled=False,
        arm_window_seconds=120,
        scan_interval_seconds=300,
        lightweight_foreground_budget_seconds=foreground_budget_seconds,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now, monotonic_start=100.0))
    bot.build_venue_clients = lambda: clients  # type: ignore[method-assign]
    return bot


def test_lightweight_discovery_adds_watch_route_without_orderbook(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    long_client = _LightweightDiscoveryClient(
        "binance",
        funding_rate=-0.004,
        next_funding_at=settlement,
        interval_hours=1.0,
    )
    short_client = _LightweightDiscoveryClient(
        "bybit",
        funding_rate=0.004,
        next_funding_at=settlement,
        interval_hours=4.0,
    )
    bot = _lightweight_bot(tmp_path, now, [long_client, short_client])

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["status"] == "success"
    assert summary["markets_checked"] == 2
    assert summary["routes_structurally_matched"] == 2
    assert summary["watch_routes_added"] == 1
    assert len(bot.hot_routes) == 1
    route = next(iter(bot.hot_routes.values()))
    assert route["status"] == "watch"
    assert route["status"] != "paper_candidate"
    assert route["evidence"]["selected_strategy"]["selection_model"] == "lightweight_discovery_v1"
    assert route["evidence"]["pnl_components"]["spread_convergence_component"] == 0.0
    assert {leg["funding_interval_hours"] for leg in route["legs"]} == {1.0, 4.0}
    assert long_client.orderbook_calls == 0
    assert short_client.orderbook_calls == 0


def test_lightweight_discovery_keeps_late_venue_and_uses_it_next_pass(tmp_path) -> None:
    class DelayedClient(_LightweightDiscoveryClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def catalog_and_markets(self, observed_at: str):
            self.started.set()
            if not self.release.wait(2.0):
                raise AssertionError("test did not release delayed venue")
            result = super().catalog_and_markets(observed_at)
            self.finished.set()
            return result

    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=300)
    fast = _LightweightDiscoveryClient(
        "binance",
        funding_rate=-0.004,
        next_funding_at=settlement,
    )
    slow = DelayedClient(
        "bybit",
        funding_rate=0.004,
        next_funding_at=settlement,
    )
    bot = _lightweight_bot(
        tmp_path,
        now,
        [fast, slow],
        foreground_budget_seconds=0.05,
    )

    first_markets, first_warnings = bot._fetch_lightweight_market_snapshots(
        [fast, slow],
        now.isoformat(),
    )

    assert slow.started.wait(1.0)
    assert {market["venue"] for market in first_markets} == {"binance"}
    assert bot._last_lightweight_venue_health["pending_venues"] == ["bybit"]
    assert any("bybit lightweight catalog still loading" in row for row in first_warnings)
    assert slow.catalog_calls == 0

    slow.release.set()
    assert slow.finished.wait(1.0)
    second_markets, _second_warnings = bot._fetch_lightweight_market_snapshots(
        [fast, slow],
        now.isoformat(),
    )
    routes, summary = bot._build_lightweight_watch_routes(
        second_markets,
        {"binance": fast, "bybit": slow},
        now,
    )

    assert {market["venue"] for market in second_markets} == {"binance", "bybit"}
    assert bot._last_lightweight_venue_health["ready_count"] == 2
    assert all(
        venue in bot._last_lightweight_venue_health["ready_venues"]
        for venue in bot._last_lightweight_venue_health["pending_venues"]
    )
    assert slow.catalog_calls == 1
    assert summary["routes_detected"] == 1
    assert len(routes) == 1
    bot.shutdown_foreground_executors()


def test_lightweight_discovery_records_early_route_without_hot_loop(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(minutes=30)
    clients = [
        _LightweightDiscoveryClient(
            "binance",
            funding_rate=-0.004,
            next_funding_at=settlement,
        ),
        _LightweightDiscoveryClient(
            "bybit",
            funding_rate=0.004,
            next_funding_at=settlement,
        ),
    ]
    bot = _lightweight_bot(tmp_path, now, clients)

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["routes_detected"] == 1
    assert summary["early_route_count"] == 1
    assert summary["focused_selected_count"] <= bot.config.max_focused_routes
    assert len(bot.discovered_routes) == 1
    assert len(bot.hot_routes) <= bot.config.max_focused_routes
    route = next(iter(bot.discovered_routes.values()))
    assert route["discovery_stage"] == "early"
    assert route["focused_state"] in {"selected", "deferred", "not_yet_eligible"}
    bot.shutdown_foreground_executors()


def test_lightweight_discovery_tracks_different_settlement_times(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    clients = [
        _LightweightDiscoveryClient(
            "binance",
            asset="NEAR",
            funding_rate=-0.004,
            next_funding_at=now + timedelta(minutes=5),
        ),
        _LightweightDiscoveryClient(
            "bybit",
            asset="NEAR",
            funding_rate=0.004,
            next_funding_at=now + timedelta(minutes=5),
        ),
        _LightweightDiscoveryClient(
            "okx",
            asset="LATER",
            funding_rate=-0.004,
            next_funding_at=now + timedelta(minutes=30),
        ),
        _LightweightDiscoveryClient(
            "hyperliquid",
            asset="LATER",
            funding_rate=0.004,
            next_funding_at=now + timedelta(minutes=30),
        ),
    ]
    bot = _lightweight_bot(tmp_path, now, clients)

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["routes_detected"] == 2, summary
    assert summary["watch_stage_route_count"] == 1
    assert summary["early_route_count"] == 1
    assert summary["research_only_routes"] == 0
    assert summary["rejection_reasons"]["raw_expected_funding_not_positive"] == 2
    assert {
        route["canonical_asset"]: route["discovery_stage"]
        for route in bot.discovered_routes.values()
    } == {"NEAR": "watch", "LATER": "early"}
    assert len(bot.hot_routes) <= bot.config.max_focused_routes
    assert {
        route["canonical_asset"]: route["focused_state"]
        for route in bot.discovered_routes.values()
    }.keys() == {"NEAR", "LATER"}
    bot.shutdown_foreground_executors()


def test_lightweight_discovery_fee_gap_becomes_risk_flagged_watch(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient(
                "hyperliquid",
                funding_rate=-0.004,
                next_funding_at=settlement,
                include_fee=False,
            ),
            _LightweightDiscoveryClient(
                "bybit",
                funding_rate=0.004,
                next_funding_at=settlement,
            ),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 1
    assert summary["research_only_routes"] == 0
    assert bot.hot_routes
    route = next(iter(bot.discovered_routes.values()))
    assert route["status"] == "watch"
    assert route["evidence"]["funding_cashflow_status"] == "ESTIMATED_ONLY"
    assert "long_fee_fallback_used" in route["evidence"]["risk_flags"]
    assert "long_account_fee_unknown" in route["evidence"]["risk_flags"]


def test_lightweight_discovery_zero_or_negative_gross_not_watch(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient("binance", funding_rate=0.0, next_funding_at=settlement),
            _LightweightDiscoveryClient("bybit", funding_rate=0.0, next_funding_at=settlement),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 0
    assert not bot.hot_routes
    assert summary["rejection_reasons"]["raw_expected_funding_not_positive"] == 2


def test_lightweight_discovery_does_not_call_legacy_strategy_builder(
    tmp_path,
    monkeypatch,
) -> None:
    import smart_money_radar.funding.economics as economics

    def fail(*args, **kwargs):
        raise AssertionError("legacy build_strategy_evaluation must not be used")

    monkeypatch.setattr(economics, "build_strategy_evaluation", fail)
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient("binance", funding_rate=-0.004, next_funding_at=settlement),
            _LightweightDiscoveryClient("bybit", funding_rate=0.004, next_funding_at=settlement),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 1


def test_real_public_adapters_flow_from_lightweight_watch_to_focused_paper_open(
    tmp_path,
    monkeypatch,
) -> None:
    import smart_money_radar.funding.trader as trader_module
    from smart_money_radar.funding.adapters.binance import BinanceFundingClient
    from smart_money_radar.funding.adapters.bybit import BybitFundingClient
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime.now(UTC).replace(microsecond=0)
    settlement = now + timedelta(seconds=55)
    settlement_ms = int(settlement.timestamp() * 1000)

    class RuntimeBinanceHttp:
        def get_json(self, url: str):
            if url.endswith("/exchangeInfo"):
                return {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                            "filters": [
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            if url.endswith("/premiumIndex"):
                return [
                    {
                        "symbol": "BTCUSDT",
                        "lastFundingRate": "0.010",
                        "nextFundingTime": str(settlement_ms),
                        "markPrice": "100",
                        "indexPrice": "100",
                    }
                ]
            if "/premiumIndex?" in url:
                return {
                    "symbol": "BTCUSDT",
                    "lastFundingRate": "0.010",
                    "nextFundingTime": str(settlement_ms),
                    "markPrice": "100",
                    "indexPrice": "100",
                }
            if url.endswith("/fundingInfo"):
                return [
                    {
                        "symbol": "BTCUSDT",
                        "fundingIntervalHours": 1,
                        "adjustedFundingRateCap": "0.05",
                        "adjustedFundingRateFloor": "-0.05",
                    }
                ]
            if "/depth?" in url:
                return {
                    "bids": [["99.95", "100"]],
                    "asks": [["100.05", "100"]],
                }
            raise AssertionError(url)

    class RuntimeBybitHttp:
        def get_json(self, url: str):
            if "instruments-info" in url:
                return {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "contractType": "LinearPerpetual",
                                "status": "Trading",
                                "baseCoin": "BTC",
                                "quoteCoin": "USDT",
                                "settleCoin": "USDT",
                                "fundingInterval": 60,
                                "isPreListing": False,
                                "lotSizeFilter": {
                                    "qtyStep": "0.001",
                                    "minOrderQty": "0.001",
                                    "minNotionalValue": "5",
                                },
                            }
                        ],
                        "nextPageCursor": "",
                    },
                }
            if "/tickers" in url:
                return {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "fundingRate": "-0.010",
                                "nextFundingTime": str(settlement_ms),
                                "fundingIntervalHour": "1",
                                "markPrice": "100",
                                "indexPrice": "100",
                                "openInterestValue": "1000000",
                                "turnover24h": "5000000",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                }
            if "/orderbook" in url:
                return {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "b": [["99.95", "100"]],
                        "a": [["100.05", "100"]],
                    },
                }
            raise AssertionError(url)

    clients = {
        "binance": BinanceFundingClient(http=RuntimeBinanceHttp()),
        "bybit": BybitFundingClient(http=RuntimeBybitHttp()),
    }

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=True,
        hot_route_recheck_workers=1,
        arm_window_seconds=120,
        lightweight_foreground_budget_seconds=0.1,
        lightweight_route_horizon_seconds=600,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
        max_open_positions_total=1,
        max_open_positions_per_venue=1,
        max_gross_exposure_usd=1_500.0,
        status_report_interval_seconds=0,
        export_dir=tmp_path / "exports",
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now, monotonic_start=100.0))
    bot.build_venue_clients = lambda: list(clients.values())  # type: ignore[method-assign]
    monkeypatch.setattr(
        trader_module,
        "funding_client_for_venue",
        lambda venue, **_kwargs: clients.get(str(venue).lower()),
    )
    monkeypatch.setattr(
        trader_module,
        "utc_now_iso",
        lambda: bot.clock.now().astimezone(UTC).isoformat(),
    )

    try:
        summary = bot._run_lightweight_discovery()
        assert summary is not None
        assert summary["watch_routes_added"] == 1, summary
        assert len(bot.hot_routes) == 1
        route = next(iter(bot.hot_routes.values()))
        assert route["status"] == "watch"
        assert route["long_venue"] == "bybit"
        assert route["short_venue"] == "binance"

        result = {"opened_count": 0}
        for _ in range(45):
            result = bot.run_hot_iteration()
            if result["opened_count"]:
                break
            bot.clock.advance(1.0)

        assert result["opened_count"] == 1, result
        open_positions = store.funding_capture_position_rows(states={"OPEN"})
        assert len(open_positions) == 1
        position = open_positions[0]
        assert position["long_venue"] == "bybit"
        assert position["short_venue"] == "binance"
        orders = store.funding_paper_order_rows(position["position_id"])
        entry_orders = [row for row in orders if row["order_intent"] == "ENTRY"]
        assert len(entry_orders) == 2
        assert {row["state"] for row in entry_orders} == {"FILLED"}
        with store.connect() as connection:
            observation_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM funding_capture_observations
                WHERE position_id = ?
                """,
                (position["position_id"],),
            ).fetchone()[0]
        assert observation_count >= 8
    finally:
        bot.shutdown_foreground_executors()


def test_post_settlement_probe_consecutive_agreement() -> None:
    """Post-settlement probes: two consecutive fresh agreements => hold next cycle."""
    from smart_money_radar.paper_bot.cycle_manager import post_settlement_probe_decision
    settlement = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 28, 16, 0, 10, tzinfo=UTC)  # T+10
    probes = [
        {
            "observed_at": "2026-07-28T16:00:06+00:00",
            "long_next_funding_at": "2026-07-28T17:00:00+00:00",
            "short_next_funding_at": "2026-07-28T17:00:00+00:00",
            "fresh": True,
        },
        {
            "observed_at": "2026-07-28T16:00:07+00:00",
            "long_next_funding_at": "2026-07-28T17:00:00+00:00",
            "short_next_funding_at": "2026-07-28T17:00:00+00:00",
            "fresh": True,
        },
    ]
    result = post_settlement_probe_decision(
        probes=probes,
        now=now,
        settlement_at=settlement,
    )
    assert result["decision"] == "hold_next_cycle"
    assert result["consecutive_agreements"] >= 2


def test_post_settlement_probe_no_agreement_by_t15_closes_at_t20() -> None:
    """No consecutive agreement by T+15 => close at T+20."""
    from smart_money_radar.paper_bot.cycle_manager import post_settlement_probe_decision
    settlement = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 28, 16, 0, 16, tzinfo=UTC)  # T+16 (> T+15)
    probes = [
        {
            "observed_at": "2026-07-28T16:00:06+00:00",
            "long_next_funding_at": "2026-07-28T17:00:00+00:00",
            "short_next_funding_at": "2026-07-28T18:00:00+00:00",  # mismatch
            "fresh": True,
        },
    ]
    result = post_settlement_probe_decision(
        probes=probes,
        now=now,
        settlement_at=settlement,
    )
    assert result["decision"] == "close_at_t20"
    assert result["close_at_t20"]


def test_next_cycle_observation_eligibility() -> None:
    """Next-cycle observations: 15+ valid, 20s span, age<=2s, all positive, latest>=0.8*median."""
    from smart_money_radar.paper_bot.cycle_manager import next_cycle_observation_decision
    now = datetime(2026, 7, 28, 16, 0, 30, tzinfo=UTC)
    observations = [
        {
            "gross_funding_pnl": 5.0 + i * 0.1,
            "observed_at": (now - timedelta(seconds=30 - i * 2)).isoformat(),
            "cross_venue_skew_seconds": 0.5,
        }
        for i in range(15)
    ]
    result = next_cycle_observation_decision(
        observations=observations,
        now=now,
    )
    assert result["eligible"], f"Expected eligible but got: {result['reasons']}"
    assert result["observation_count"] == 15
    assert result["observation_span_seconds"] >= 20.0
    assert result["conservative_funding_gross"] == pytest.approx(0.9 * 5.0)


# ---------------------------------------------------------------------------
# PASS 1: end-to-end runtime defect-fix tests
# ---------------------------------------------------------------------------


def _open_position_for_close_tests(tmp_path, now: datetime):
    """Create an OPEN position directly in storage for close-path tests."""
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    position_id = "fc-close-test"
    store.upsert_funding_capture_position({
        "position_id": position_id,
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 5.0,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now.isoformat(),
        "paper_open_fees": 0.50,
        "config": {
            "route_key": "BTC:binance:bybit",
            "entry_legs": [
                {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
                 "entry_fill_price": 100.02, "vwap": 100.02, "fee_rate": 0.0005},
                {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
                 "entry_fill_price": 99.98, "vwap": 99.98, "fee_rate": 0.0005},
            ],
        },
    })
    store.upsert_funding_capture_cycle({
        "position_id": position_id,
        "cycle_number": 1,
        "scheduled_funding_at": (now + timedelta(seconds=30)).isoformat(),
        "state": "OPEN",
    })
    # Manually reserve collateral to simulate a real open
    from smart_money_radar.paper_bot.accounting import collateral_reserve_event_key, make_ledger_entry
    for venue in ("binance", "bybit"):
        amount = 500.0 + 500.0 * 0.25  # isolated + reserve at leverage=1
        store.upsert_paper_event_ledger(make_ledger_entry(
            collateral_reserve_event_key(position_id, venue),
            position_id=position_id, venue=venue,
            event_type="collateral_reserve", cash_delta=0.0,
            payload={"amount": amount},
        ))
        store.update_funding_paper_account_reserved(venue, amount)
    return store, position_id


def test_hard_risk_executes_exit_orders_and_releases_collateral(tmp_path) -> None:
    """Hard-risk close: 2 exit orders, collateral release, price PnL, CLOSED_PENDING_RECONCILIATION."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    config = PaperBotConfig(telegram_enabled=False, venue_starting_balance=10_000.0).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    # Route with executable books and a hard-risk trigger (mark/index divergence > 100bps)
    bot.hot_routes["BTC:binance:bybit"] = {
        "status": "paper_candidate",
        "route_key": "BTC:binance:bybit",
        "observed_at": now.isoformat(),
        "risk_flags": [],
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=3600)).isoformat(),
             "mark_price": 100.0, "index_price": 99.0,
             "best_bid": 99.5, "best_ask": 100.5,
             "close_vwap": 99.5, "bids": [[99.5, 20.0]],
             "fee_rate": 0.0005, "funding_rate": 0.001,
             "normalized_next_funding_rate": 0.001,
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=3600)).isoformat(),
             "mark_price": 100.0, "index_price": 99.0,
             "best_bid": 99.5, "best_ask": 100.5,
             "close_vwap": 100.5, "asks": [[100.5, 20.0]],
             "fee_rate": 0.0005, "funding_rate": 0.001,
             "normalized_next_funding_rate": 0.001,
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
        ],
        "evidence": {"selected_strategy": {
            "selection_model": "opportunity_engine_v1",
            "strategy_name": "synchronized_funding_capture",
            "eligible": True, "expected_net_pnl": 5.0,
        }},
    }
    _install_targeted_refresh_clients(
        bot,
        "BTC:binance:bybit",
        bot.hot_routes["BTC:binance:bybit"],
    )
    outcomes = bot.process_open_positions()
    assert "emergency_unwind" in outcomes
    # Position must be CLOSED_PENDING_RECONCILIATION, not EMERGENCY_UNWIND
    positions = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})
    assert len(positions) == 1
    assert positions[0]["position_id"] == position_id
    # Must NOT appear as open
    assert all(p["position_id"] != position_id for p in store.funding_capture_open_positions())
    # Exit orders exist
    orders = store.funding_paper_order_rows(position_id)
    exit_orders = [o for o in orders if o["order_intent"] == "EXIT"]
    assert len(exit_orders) == 2
    assert {o["leg_side"] for o in exit_orders} == {"long", "short"}
    # Ledger: price_pnl, order_fee x2, collateral_release x2
    ledger = store.paper_event_ledger_rows(position_id)
    event_types = [row["event_type"] for row in ledger]
    assert "price_pnl" in event_types
    assert event_types.count("order_fee") >= 2
    assert event_types.count("collateral_release") == 2
    # Collateral actually released on accounts
    accounts = {r["venue"]: r for r in store.funding_paper_account_rows()}
    for venue in ("binance", "bybit"):
        reserved = accounts[venue]["reserved_margin"]
        assert reserved == 0.0, f"{venue} reserved_margin should be 0 after release, got {reserved}"


def test_hard_stale_no_fresh_book_uses_emergency_fallback(tmp_path) -> None:
    """Hard stale targeted refresh uses last valid executable route and closes."""
    from smart_money_radar.funding.trader import CaptureRouteRefreshResult, PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    config = PaperBotConfig(telegram_enabled=False, venue_starting_balance=10_000.0).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    last_valid_route = _v2_runtime_route(
        now - timedelta(seconds=6),
        next_lead_seconds=3606,
        route_key="BTC:binance:bybit",
    )
    position = store.funding_capture_position_by_id(position_id)
    position_config = dict(position["config"])
    position_config["data_quality"] = {
        "state": "DEGRADED",
        "first_degraded_at": (now - timedelta(seconds=6)).isoformat(),
        "refresh_attempt_count": 0,
    }
    position_config["last_valid_executable_route"] = last_valid_route
    position_config["last_valid_executable_snapshot"] = {
        "quality": "EXECUTABLE_FULL_DEPTH",
        "observed_at": (now - timedelta(seconds=6)).isoformat(),
        "paper_net_if_exit_now": -1.0,
    }
    store.update_funding_capture_position_config(
        position_id,
        position_config,
        now - timedelta(seconds=6),
    )
    refresh_calls = 0

    def unavailable_refresh(position, now):
        nonlocal refresh_calls
        refresh_calls += 1
        return CaptureRouteRefreshResult(
            quality="UNAVAILABLE",
            route=None,
            snapshot_id=f"failed-refresh-{refresh_calls}",
            reason="test_no_fresh_route",
        )

    bot.refresh_open_capture_route = unavailable_refresh
    outcomes = bot.process_open_positions()
    assert "emergency_unwind" in outcomes
    assert refresh_calls == 1
    positions = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})
    assert len(positions) == 1
    ledger = store.paper_event_ledger_rows(position_id)
    emergency_events = [r for r in ledger if r["event_type"] == "emergency_unwind_cost"]
    assert len(emergency_events) >= 1
    payload = emergency_events[0]["payload"]
    assert payload.get("reason") == "targeted_refresh_hard_stale"
    assert payload.get("pricing_quality") == "executable_book"
    updated = store.funding_capture_position_by_id(position_id)
    assert updated["config"]["data_quality"]["refresh_attempt_count"] == 1


def test_partial_entry_unwinds_and_releases_collateral(tmp_path, monkeypatch) -> None:
    """Partial fill (long 100%, short 90%) never opens, creates unwind, final FAILED."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    route = _v2_runtime_route(now)
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False, focused_recheck_enabled=False,
        venue_starting_balance=10_000.0, target_notional_per_leg=500.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    # Make short bids thin so short fill is only ~90%
    for leg in route["legs"]:
        if leg["side"] == "short":
            leg["bids"] = [[99.98, 1.1]]  # visible 1.1 * 0.4 haircut = 0.44 fillable
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    opened = bot.process_entry_candidates([route], recheck_before_open=False)
    assert opened == []
    # Position must NOT be OPEN
    open_positions = store.funding_capture_position_rows(states={"OPEN"})
    assert len(open_positions) == 0
    # Position must be FAILED
    failed = store.funding_capture_position_rows(states={"FAILED"})
    assert len(failed) == 1
    assert failed[0]["position_id"] == capture_id
    # Unwind orders exist
    orders = store.funding_paper_order_rows(capture_id)
    unwind_orders = [o for o in orders if o["order_intent"] == "UNWIND"]
    assert len(unwind_orders) >= 1
    # Ledger has fees, venue-level price_pnl, and zero-cash emergency diagnostics.
    ledger = store.paper_event_ledger_rows(capture_id)
    event_types = [r["event_type"] for r in ledger]
    assert "order_fee" in event_types
    assert event_types.count("price_pnl") == 2
    assert "emergency_unwind_cost" in event_types
    emergency_rows = [row for row in ledger if row["event_type"] == "emergency_unwind_cost"]
    assert sum(float(row["cash_delta"]) for row in emergency_rows) == pytest.approx(0.0)
    assert failed[0]["paper_emergency_unwind_cost"] == pytest.approx(0.0)


def test_missing_entry_book_rejects(tmp_path, monkeypatch) -> None:
    """Missing asks/bids rejects with executable_orderbook_missing."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    import smart_money_radar.funding.trader as trader_module
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    route = _v2_runtime_route(now)
    # Remove asks from long leg
    for leg in route["legs"]:
        if leg["side"] == "long":
            del leg["asks"]
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False, focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    opened = bot.process_entry_candidates([route], recheck_before_open=False)
    assert opened == []
    open_positions = store.funding_capture_position_rows(states={"OPEN"})
    assert len(open_positions) == 0


def test_quantity_step_lcm_common_quantity() -> None:
    """LCM example: long step 0.01, short step 0.025, q_raw 4.999 => common_step 0.05, q 4.95."""
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2
    from smart_money_radar.funding.trader import PaperBotConfig
    config = PaperBotConfig(target_notional_per_leg=500.0).validated()
    runtime = SynchronizedFundingRuntimeV2(
        store=None, config=config, clock=None, observations_by_route={},
    )
    result = runtime._compute_common_quantity(
        long_price=100.0,
        short_price=100.02,
        long_step=0.01,
        short_step=0.025,
        long_min_qty=0.01,
        short_min_qty=0.01,
        long_min_notional=10.0,
        short_min_notional=10.0,
    )
    # q_raw = min(500/100, 500/100.02) = min(5.0, 4.99900019996) = 4.99900019996
    # LCM(1000000, 2500000) = 5000000 => common_step = 0.05
    # q = floor(4.99900019996 / 0.05) * 0.05 = floor(99.98) * 0.05 = 99 * 0.05 = 4.95
    assert result["quantity"] == pytest.approx(4.95)
    assert result["reason"] is None


def test_insufficient_balance_rejects_entry(tmp_path, monkeypatch) -> None:
    """Insufficient balance rejects with no entry orders."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    # Only 100 USD per venue — not enough for 500 + 125 reserve + fee
    store.ensure_funding_paper_accounts(["binance", "bybit"], 100.0)
    route = _v2_runtime_route(now)
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False, focused_recheck_enabled=False,
        venue_starting_balance=100.0, target_notional_per_leg=500.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    opened = bot.process_entry_candidates([route], recheck_before_open=False)
    assert opened == []
    capture_id = capture_position_id_for_route(route)
    orders = store.funding_paper_order_rows(capture_id)
    entry_orders = [o for o in orders if o["order_intent"] == "ENTRY"]
    assert len(entry_orders) == 0


def test_max_position_limit_rejects_second_entry(tmp_path, monkeypatch) -> None:
    """Max position limit (1) rejects second synchronized entry."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit", "okx"], 10_000.0)
    config = PaperBotConfig(
        telegram_enabled=False, focused_recheck_enabled=False,
        venue_starting_balance=10_000.0, target_notional_per_leg=500.0,
        max_open_positions_total=1,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    # Open first position
    route1 = _v2_runtime_route(now, route_key="BTC:binance:bybit")
    settlement_at = now + timedelta(seconds=30)
    bot.v2_observations_by_route[route_entry_key(route1)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    opened1 = bot.process_entry_candidates([route1], recheck_before_open=False)
    assert len(opened1) == 1
    # Second route on different venues
    route2 = _v2_runtime_route(
        now, route_key="BTC:binance:okx",
        next_lead_seconds=30, short_next_lead_seconds=30,
    )
    for leg in route2["legs"]:
        if leg["side"] == "short":
            leg["venue"] = "okx"
            leg["symbol"] = "BTCUSDT"
    route2["short_venue"] = "okx"
    bot.v2_observations_by_route[route_entry_key(route2)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    opened2 = bot.process_entry_candidates([route2], recheck_before_open=False)
    assert len(opened2) == 0


def test_normal_close_rejects_without_orderbook(tmp_path) -> None:
    """Normal close_position rejects with executable_orderbook_missing when no bids/asks."""
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2
    from smart_money_radar.funding.trader import PaperBotConfig
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    config = PaperBotConfig(telegram_enabled=False).validated()
    runtime = SynchronizedFundingRuntimeV2(
        store=store, config=config, clock=FakeClock(now), observations_by_route={},
    )
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    # Route without bids/asks
    route_no_book = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "mark_price": 100.0, "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "mark_price": 100.0, "fee_rate": 0.0005},
        ],
    }
    result = runtime.close_position(position, route_no_book, now, reason="test_close")
    assert result["decision"] == "rejected"
    assert result["reason"] == "executable_orderbook_missing"


def test_collateral_reserved_on_entry_and_released_on_close(tmp_path, monkeypatch) -> None:
    """Collateral is reserved on entry and released on close."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    route = _v2_runtime_route(now)
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False, focused_recheck_enabled=False,
        venue_starting_balance=10_000.0, target_notional_per_leg=500.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    # Before entry: reserved = 0
    accounts_before = {r["venue"]: r for r in store.funding_paper_account_rows()}
    assert accounts_before["binance"]["reserved_margin"] == 0.0
    assert accounts_before["bybit"]["reserved_margin"] == 0.0
    opened = bot.process_entry_candidates([route], recheck_before_open=False)
    assert len(opened) == 1
    # After entry: reserved > 0
    accounts_after = {r["venue"]: r for r in store.funding_paper_account_rows()}
    assert accounts_after["binance"]["reserved_margin"] > 0.0
    assert accounts_after["bybit"]["reserved_margin"] > 0.0
    # Ledger has collateral_reserve events
    ledger = store.paper_event_ledger_rows(capture_id)
    reserve_events = [r for r in ledger if r["event_type"] == "collateral_reserve"]
    assert len(reserve_events) == 2  # one per venue


def test_partial_unwind_penalty_is_embedded_in_fill_price_once(tmp_path, monkeypatch) -> None:
    """Partial entry 100 bps unwind affects price PnL once and has zero separate emergency cash cost."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    route = _v2_runtime_route(now)
    for leg in route["legs"]:
        leg["open_vwap"] = 100.0
        if leg["side"] == "long":
            leg["asks"] = [[100.0, 20.0]]
        else:
            leg["bids"] = [[100.0, 11.25]]
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - i * 2), settlement_at)
        for i in range(9)
    ]
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))

    assert bot.process_entry_candidates([route], recheck_before_open=False) == []

    failed = store.funding_capture_position_rows(states={"FAILED"})[0]
    assert failed["paper_emergency_unwind_cost"] == pytest.approx(0.0)
    unwind_orders = [order for order in store.funding_paper_order_rows(capture_id) if order["order_intent"] == "UNWIND"]
    assert {order["leg_side"] for order in unwind_orders} == {"long", "short"}
    long_unwind = next(order for order in unwind_orders if order["leg_side"] == "long")
    short_unwind = next(order for order in unwind_orders if order["leg_side"] == "short")
    assert long_unwind["filled_quantity"] == pytest.approx(5.0)
    assert long_unwind["average_fill_price"] == pytest.approx(99.0)
    assert short_unwind["filled_quantity"] == pytest.approx(4.5)
    assert short_unwind["average_fill_price"] == pytest.approx(101.0)
    price_rows = [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "price_pnl"]
    assert {row["venue"] for row in price_rows} == {"binance", "bybit"}
    assert sum(float(row["cash_delta"]) for row in price_rows) == pytest.approx(-9.5)
    emergency_rows = [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "emergency_unwind_cost"]
    assert sum(float(row["cash_delta"]) for row in emergency_rows) == pytest.approx(0.0)


def test_emergency_300_bps_fallback_penalty_once(tmp_path) -> None:
    """Emergency fallback mark pricing embeds 300 bps in fills and records no separate cash cost."""
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    config = dict(position["config"] or {})
    config["entry_legs"] = [
        {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0},
        {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0},
    ]
    store.update_funding_capture_position_config(position_id, config)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route_no_book = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "mark_price": 100.0, "fee_rate": 0.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "mark_price": 100.0, "fee_rate": 0.0},
        ],
    }

    close = runtime.close_position(position, route_no_book, now, reason="hard_stale", emergency=True)

    assert close["decision"] == "closed"
    assert close["pricing_quality"] == "fallback_mark_300bps"
    assert close["paper_long_price_pnl"] == pytest.approx(-15.0)
    assert close["paper_short_price_pnl"] == pytest.approx(-15.0)
    assert close["paper_price_pnl"] == pytest.approx(-30.0)
    assert close["paper_emergency_unwind_cost"] == pytest.approx(0.0)
    emergency_rows = [row for row in store.paper_event_ledger_rows(position_id) if row["event_type"] == "emergency_unwind_cost"]
    assert sum(float(row["cash_delta"]) for row in emergency_rows) == pytest.approx(0.0)


def test_residual_unwind_creates_explicit_orders_and_closes_exposure(tmp_path) -> None:
    """Partial close IOC must create RESIDUAL_UNWIND orders before final CLOSED_PENDING_RECONCILIATION."""
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "bids": [[99.0, 5.0]], "fee_rate": 0.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "asks": [[101.0, 5.0]], "fee_rate": 0.0},
        ],
    }

    close = runtime.close_position(position, route, now, reason="residual_test")

    assert close["decision"] == "closed"
    assert close["long_closed_quantity"] == pytest.approx(5.0)
    assert close["short_closed_quantity"] == pytest.approx(5.0)
    assert close["paper_emergency_unwind_cost"] == pytest.approx(0.0)
    orders = store.funding_paper_order_rows(position_id)
    residual_orders = [order for order in orders if order["order_intent"] == "RESIDUAL_UNWIND"]
    assert len(residual_orders) == 2
    assert {order["state"] for order in residual_orders} == {"FILLED"}
    assert store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]["position_id"] == position_id


def test_position_cannot_close_with_unpriced_residual(tmp_path) -> None:
    """If residual cannot be priced, exposure stays actionable in EMERGENCY_UNWIND."""
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "bids": [[0.0, 5.0]], "fee_rate": 0.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "asks": [[101.0, 20.0]], "fee_rate": 0.0},
        ],
    }

    close = runtime.close_position(position, route, now, reason="bad_residual")

    assert close["decision"] == "failed"
    assert close["state"] == "EMERGENCY_UNWIND"
    assert store.funding_capture_position_by_id(position_id)["state"] == "EMERGENCY_UNWIND"
    assert store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"}) == []


def test_collateral_release_uses_original_reserve_amount(tmp_path) -> None:
    """Release must exactly match the reserve event payload, not current route prices."""
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "bids": [[200.0, 20.0]], "fee_rate": 0.0},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "asks": [[200.0, 20.0]], "fee_rate": 0.0},
        ],
    }

    assert runtime.close_position(position, route, now, reason="release_test")["decision"] == "closed"

    ledger = store.paper_event_ledger_rows(position_id)
    for venue in ("binance", "bybit"):
        reserve = next(row for row in ledger if row["event_type"] == "collateral_reserve" and row["venue"] == venue)
        release = next(row for row in ledger if row["event_type"] == "collateral_release" and row["venue"] == venue)
        assert release["payload"]["amount"] == pytest.approx(reserve["payload"]["amount"])
    accounts = {row["venue"]: row for row in store.funding_paper_account_rows()}
    assert accounts["binance"]["reserved_margin"] == pytest.approx(0.0)
    assert accounts["bybit"]["reserved_margin"] == pytest.approx(0.0)


def test_venue_cash_matches_venue_ledger_delta_after_close(tmp_path) -> None:
    """For every venue, cash balance delta equals idempotent cash ledger delta."""
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "bids": [[101.0, 20.0]], "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "asks": [[99.0, 20.0]], "fee_rate": 0.0005},
        ],
    }

    close = runtime.close_position(position, route, now, reason="cash_invariant")

    assert close["decision"] == "closed"
    accounts = {row["venue"]: row for row in store.funding_paper_account_rows()}
    ledger = store.paper_event_ledger_rows(position_id)
    for venue in ("binance", "bybit"):
        ledger_delta = sum(float(row["cash_delta"]) for row in ledger if row["venue"] == venue)
        account_delta = float(accounts[venue]["cash_balance"]) - float(accounts[venue]["starting_balance"])
        assert account_delta == pytest.approx(ledger_delta, abs=1e-8)
    price_rows = [row for row in ledger if row["event_type"] == "price_pnl"]
    assert {row["venue"] for row in price_rows} == {"binance", "bybit"}
    assert sum(float(row["cash_delta"]) for row in price_rows) == pytest.approx(close["paper_price_pnl"])


def test_account_consistency_repairs_reserve_ledger_without_margin_delta(tmp_path) -> None:
    from smart_money_radar.paper_bot.accounting import collateral_reserve_event_key, make_ledger_entry

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance"], 10_000.0)
    store.upsert_paper_event_ledger(
        make_ledger_entry(
            collateral_reserve_event_key("pos-reserve", "binance"),
            position_id="pos-reserve",
            venue="binance",
            event_type="collateral_reserve",
            cash_delta=0.0,
            payload={"amount": 625.0},
        )
    )

    assert not store.paper_account_consistency_report()["ok"]
    first = store.repair_paper_account_consistency()
    second = store.repair_paper_account_consistency()

    assert first["repaired"]
    assert second["ok"]
    assert store.funding_paper_account_rows()[0]["reserved_margin"] == pytest.approx(625.0)


def test_account_consistency_repairs_margin_delta_without_ledger(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance"], 10_000.0)
    store.update_funding_paper_account_reserved("binance", 625.0)

    assert not store.paper_account_consistency_report()["ok"]
    first = store.repair_paper_account_consistency()
    second = store.repair_paper_account_consistency()

    assert first["repaired"]
    assert second["ok"]
    assert store.funding_paper_account_rows()[0]["reserved_margin"] == pytest.approx(0.0)


def test_current_executable_pnl_requires_full_ioc_depth(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, _position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "bids": [[99.0, 5.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "asks": [[101.0, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": (now - timedelta(milliseconds=500)).isoformat()},
        ],
    }

    pnl = runtime.record_current_executable_pnl(position, route, now)

    assert pnl["decision"] == "skipped"
    assert pnl["quality"] == "INVALID_INCOMPLETE_DEPTH"
    assert pnl["long_fill_ratio"] == pytest.approx(0.4)
    refreshed = store.funding_capture_position_by_id(position["position_id"])
    assert refreshed["paper_net_pnl_estimated"] is None


def test_mark_cannot_be_normal_current_executable_close(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, _position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "mark_price": 100.0, "index_price": 100.0, "fee_rate": 0.0005,
             "orderbook_response_received_at": now.isoformat()},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "mark_price": 100.0, "index_price": 100.0, "fee_rate": 0.0005,
             "orderbook_response_received_at": now.isoformat()},
        ],
    }

    pnl = runtime.record_current_executable_pnl(position, route, now)

    assert pnl["decision"] == "skipped"
    assert pnl["quality"] == "INVALID_MISSING_BOOK"
    assert pnl["paper_net_if_exit_now"] is None if "paper_net_if_exit_now" in pnl else True


def test_missing_route_is_degraded_and_not_age_zero(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, _position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_rows(states={"OPEN"})[0]
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )

    pnl = runtime.record_current_executable_pnl(position, None, now)
    risk = runtime.poll_synchronized_position_risk(position, None, pnl, None, now)

    assert pnl["quality"] == "INVALID_MISSING_BOOK"
    assert math.isinf(float(pnl["snapshot_age_seconds"]))
    assert risk["close_reason"] == "risk_data_hard_stale"


def test_direct_basis_risk_denominator_uses_asset_price(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    store.upsert_funding_capture_cycle({
        "cycle_id": f"{position_id}:2",
        "position_id": position_id,
        "cycle_number": 2,
        "scheduled_funding_at": (now + timedelta(seconds=3600)).isoformat(),
        "conservative_funding_edge_bps": 100.0,
        "state": "OPEN",
    })
    position = store.funding_capture_position_by_id(position_id)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "bids": [[100.0, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "asks": [[101.0, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
        ],
    }

    pnl = runtime.record_current_executable_pnl(position, route, now)
    risk = runtime.poll_synchronized_position_risk(position, route, pnl, None, now)

    assert risk["close_reason"] == "basis_deterioration"
    assert risk["dynamic_basis"]["basis_deterioration_bps"] == pytest.approx(104.0)


def test_direct_margin_safety_changes_with_upnl(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_by_id(position_id)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    safe_route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "bids": [[99.98, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "asks": [[100.02, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
        ],
    }
    adverse_route = {
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "bids": [[99.98, 20.0]], "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "asks": [[100.02, 20.0]], "mark_price": 300.0, "index_price": 300.0,
             "fee_rate": 0.0005, "orderbook_response_received_at": now.isoformat()},
        ],
    }

    safe_pnl = runtime.record_current_executable_pnl(position, safe_route, now)
    safe_risk = runtime.poll_synchronized_position_risk(position, safe_route, safe_pnl, None, now)
    adverse_pnl = runtime.record_current_executable_pnl(position, adverse_route, now)
    adverse_risk = runtime.poll_synchronized_position_risk(position, adverse_route, adverse_pnl, None, now)

    safe_state = store.funding_capture_position_by_id(position_id)["config"]["risk_state"]
    assert safe_risk is None or safe_risk.get("close_reason") != "risk_hard_exit:margin_safety"
    assert adverse_risk["close_reason"] in {
        "risk_hard_exit:margin_safety",
        "risk_hard_exit:liquidation_distance",
    }
    assert adverse_risk["risk_gates"]["margin_safety_ratio"] < safe_state.get("last_margin_safety_ratio", math.inf)


# ---------------------------------------------------------------------------
# Corrective integrity pass regressions
# ---------------------------------------------------------------------------


def test_open_v2_position_performs_targeted_refresh(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    _store, bot, route, _capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    clients = _install_targeted_refresh_clients(bot, route["route_key"], route)
    bot.hot_routes.clear()
    bot.clock.advance(1.0)

    assert bot.process_open_positions() == []

    called = {client.venue: client for client in clients}
    assert called["binance"].market_snapshot_calls == 1
    assert called["binance"].orderbook_calls == 1
    assert called["bybit"].market_snapshot_calls == 1
    assert called["bybit"].orderbook_calls == 1
    assert called["okx"].market_snapshot_calls == 0
    assert called["okx"].orderbook_calls == 0


def test_db_or_old_hot_route_is_not_treated_as_fresh(tmp_path, monkeypatch) -> None:
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    bot.hot_routes[route["route_key"]] = route
    before_estimate = store.funding_capture_position_by_id(capture_id)["paper_net_pnl_estimated"]
    bot.build_venue_clients = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda *args, **kwargs: None)

    def db_route_must_not_be_used(route_key):
        raise AssertionError("DB route must not be treated as fresh open-position data")

    monkeypatch.setattr(store, "latest_funding_route_by_key", db_route_must_not_be_used)

    assert bot.process_open_positions() == []
    position = store.funding_capture_position_by_id(capture_id)
    assert position["paper_net_pnl_estimated"] == pytest.approx(before_estimate)
    assert position["config"]["data_quality"]["state"] == "DEGRADED"


def test_targeted_refresh_failure_records_degraded_metadata(tmp_path, monkeypatch) -> None:
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, _route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    bot.hot_routes.clear()
    bot.build_venue_clients = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda *args, **kwargs: None)

    assert bot.process_open_positions() == []
    quality = store.funding_capture_position_by_id(capture_id)["config"]["data_quality"]
    assert quality["state"] == "DEGRADED"
    assert quality["first_degraded_at"] == now.isoformat()
    assert quality["last_refresh_attempt_at"] == now.isoformat()
    assert quality["refresh_attempt_count"] == 1
    assert quality["last_refresh_quality"] == "UNAVAILABLE"


def test_next_sleep_honors_30_second_light_discovery(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient("venue_a", funding_rate=-0.004, next_funding_at=settlement),
            _LightweightDiscoveryClient("venue_b", funding_rate=0.004, next_funding_at=settlement),
        ],
    )
    bot._last_lightweight_discovery_monotonic = bot.clock.monotonic()
    bot.last_full_scan_monotonic = bot.clock.monotonic()

    assert bot.next_sleep_seconds({}) <= 30.0
    assert bot.next_sleep_seconds({}) == pytest.approx(30.0)


def test_next_sleep_uses_adaptive_light_discovery_cadence(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient("venue_a", funding_rate=-0.004, next_funding_at=settlement),
            _LightweightDiscoveryClient("venue_b", funding_rate=0.004, next_funding_at=settlement),
        ],
    )
    bot._last_lightweight_discovery_monotonic = bot.clock.monotonic()
    bot.last_full_scan_monotonic = bot.clock.monotonic()
    bot._last_lightweight_nearest_settlement_seconds = 90.0

    assert bot.next_sleep_seconds({}) == pytest.approx(5.0)


def test_background_full_scan_does_not_suppress_light_discovery(tmp_path) -> None:
    from concurrent.futures import Future

    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    clients = [
        _LightweightDiscoveryClient("binance", funding_rate=-0.004, next_funding_at=settlement),
        _LightweightDiscoveryClient("bybit", funding_rate=0.004, next_funding_at=settlement),
    ]
    bot = _lightweight_bot(tmp_path, now, clients)
    bot._last_lightweight_discovery_monotonic = bot.clock.monotonic() - 31.0
    bot.last_full_scan_monotonic = bot.clock.monotonic()
    pending = Future()
    bot.background_full_scan_future = pending

    result = bot.run_iteration()

    pending.cancel()
    assert result["background_full_scan_running"]
    assert result["lightweight_discovery"]["watch_routes_added"] == 1
    assert clients[0].catalog_calls == 1
    assert clients[1].catalog_calls == 1


def test_background_full_scan_does_not_delay_open_poll(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future

    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    bot = _lightweight_bot(tmp_path, now, [])
    pending = Future()
    bot.background_full_scan_future = pending
    calls: list[str] = []
    monkeypatch.setattr(bot, "has_open_exposure", lambda: True)
    monkeypatch.setattr(
        bot,
        "run_open_position_iteration",
        lambda: calls.append("open") or {"mode": "open_positions", "open_position_count": 1},
    )

    result = bot.run_iteration()

    pending.cancel()
    assert calls == ["open"]
    assert result["mode"] == "open_positions"
    assert result["background_full_scan_running"]


def test_background_full_scan_does_not_delay_critical_hot_recheck(tmp_path, monkeypatch) -> None:
    from concurrent.futures import Future

    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    bot = _lightweight_bot(tmp_path, now, [])
    pending = Future()
    bot.background_full_scan_future = pending
    calls: list[str] = []
    monkeypatch.setattr(bot, "has_open_exposure", lambda: False)
    monkeypatch.setattr(bot, "critical_entry_recheck_active", lambda: True)
    monkeypatch.setattr(
        bot,
        "run_hot_iteration",
        lambda: calls.append("hot") or {"mode": "hot_routes", "urgent_route_count": 1},
    )

    result = bot.run_iteration()

    pending.cancel()
    assert calls == ["hot"]
    assert result["mode"] == "critical_hot_routes"
    assert result["background_full_scan_running"]


def test_background_cache_read_does_not_duplicate_foreground_requests(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    clients = [
        _LightweightDiscoveryClient("venue_a", funding_rate=-0.004, next_funding_at=settlement),
        _LightweightDiscoveryClient("venue_b", funding_rate=0.004, next_funding_at=settlement),
    ]
    bot = _lightweight_bot(tmp_path, now, clients)
    foreground_markets, _foreground_warnings = bot._fetch_lightweight_market_snapshots(
        clients,
        now.isoformat(),
    )
    assert len(foreground_markets) == 2
    bot._foreground_venues.add("venue_a")

    markets, warnings = bot._fetch_lightweight_market_snapshots(
        clients,
        now.isoformat(),
        work_mode="background_full_scan",
    )

    assert {market["venue"] for market in markets} == {"venue_a", "venue_b"}
    assert bot.background_full_scan_skip_reasons == {}
    assert warnings == []
    assert clients[0].catalog_calls == 1
    assert clients[1].catalog_calls == 1
    bot.shutdown_foreground_executors()


def test_old_settlement_observations_do_not_qualify_new_settlement(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2, capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    settlement_a = now + timedelta(seconds=30)
    route_a = _v2_runtime_route(now)
    capture_a = capture_position_id_for_route(route_a)
    runtime._ensure_discovered_or_armed(route_a, capture_a, settlement_a, 30.0, now)
    for index in range(9):
        runtime._store_observation(
            route_a,
            capture_a,
            settlement_a,
            _valid_v2_observation(now - timedelta(seconds=20 - index * 2), settlement_a),
            phase="entry",
        )
    route_b = _v2_runtime_route(now + timedelta(hours=1))
    settlement_b = now + timedelta(hours=1, seconds=30)
    capture_b = capture_position_id_for_route(route_b)
    runtime._ensure_discovered_or_armed(
        route_b,
        capture_b,
        settlement_b,
        30.0,
        now + timedelta(hours=1),
    )
    runtime._store_observation(
        route_b,
        capture_b,
        settlement_b,
        _valid_v2_observation(now + timedelta(hours=1), settlement_b),
        phase="entry",
    )

    valid_b = runtime._valid_observations(
        route_b,
        now + timedelta(hours=1),
        phase="entry",
        cycle_id=f"{capture_b}:1",
    )
    underwriting = entry_underwriting(valid_b, now=now + timedelta(hours=1))

    assert len(valid_b) == 1
    assert not underwriting["eligible"]
    assert "insufficient_observations_1<10" in underwriting["reasons"]
    assert "observation_span_0.0s_below_20.0s" in underwriting["reasons"]


def test_entry_observation_must_match_current_timestamps(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2, capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = _v2_runtime_route(now)
    settlement = now + timedelta(seconds=30)
    capture_id = capture_position_id_for_route(route)
    runtime._ensure_discovered_or_armed(route, capture_id, settlement, 30.0, now)
    mismatched_observation = _valid_v2_observation(now, settlement + timedelta(seconds=2))
    runtime._store_observation(route, capture_id, settlement, mismatched_observation, phase="entry")

    assert runtime._valid_observations(route, now, phase="entry", cycle_id=f"{capture_id}:1") == []


def _partial_attempt_route(now: datetime) -> dict:
    route = _v2_runtime_route(now)
    for leg in route["legs"]:
        if leg["side"] == "short":
            leg["bids"] = [[99.98, 1.1]]
    return route


def _attempt_test_bot(tmp_path, now: datetime):
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
    ).validated()
    return store, PaperBot(store, config, clock=FakeClock(now))


def _seed_attempt_observations(bot, route: dict, now: datetime) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import route_next_settlement

    settlement = route_next_settlement(route)
    bot.v2_observations_by_route[route_entry_key(route)] = [
        _valid_v2_observation(now - timedelta(seconds=20 - index * 2), settlement)
        for index in range(9)
    ]


def _seed_entry_submitted_attempt(
    store: SQLiteStore,
    now: datetime,
    *,
    reserve: bool = False,
    order_sides: tuple[str, ...] = (),
) -> tuple[str, str, dict]:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2, capture_position_id_for_route

    route = _v2_runtime_route(now)
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False, target_notional_per_leg=500.0).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route_plan = runtime._plan_dict_for_route(route, now)
    runtime._ensure_discovered_or_armed(route, capture_id, settlement_at, 30.0, now)
    runtime._mark_armed(route, capture_id, settlement_at, now, route_plan, {"passed": True})
    attempt_id = "entry-recovery-attempt"
    runtime._mark_entry_attempt_submitted(
        capture_id=capture_id,
        route=route,
        attempt_id=attempt_id,
        settlement_at=settlement_at,
        decision_at=now,
    )
    store.update_funding_capture_position_state(capture_id, "ENTRY_SUBMITTED", now)
    if reserve:
        runtime._reserve_collateral(capture_id, route, attempt_id=attempt_id)
    for side in order_sides:
        leg = next(leg for leg in route["legs"] if leg["side"] == side)
        order_id = f"{attempt_id}:entry:{side}"
        store.upsert_funding_paper_order(
            {
                "paper_order_id": order_id,
                "position_id": capture_id,
                "cycle_id": f"{capture_id}:1",
                "leg_side": side,
                "order_intent": "ENTRY",
                "venue": leg["venue"],
                "symbol": leg["symbol"],
                "decision_at": now.isoformat(),
                "submitted_at": now.isoformat(),
                "acknowledged_at": now.isoformat(),
                "filled_at": (now + timedelta(seconds=1)).isoformat(),
                "filled_quantity": 4.99,
                "average_fill_price": 100.0,
                "fee": 0.25,
                "state": "FILLED",
                "payload": {"attempt_id": attempt_id},
            }
        )
    return capture_id, attempt_id, route


def test_entry_submitted_recovery_aborts_without_reserve_or_orders(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    capture_id, _attempt_id, _route = _seed_entry_submitted_attempt(store, now)

    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now + timedelta(seconds=5)))

    position = store.funding_capture_position_by_id(capture_id)
    assert bot.last_runtime_recovery["entry_submitted"]["recovered_aborted"] == 1
    assert position["state"] == "FAILED"
    assert position["config"]["entry_attempt_state"] == "RECOVERED_ABORTED"
    assert store.funding_paper_order_rows(capture_id) == []


def test_begin_entry_attempt_rolls_back_config_and_state_on_fault(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2, capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    route = _v2_runtime_route(now)
    capture_id = capture_position_id_for_route(route)
    settlement_at = now + timedelta(seconds=30)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route_plan = runtime._plan_dict_for_route(route, now)
    runtime._ensure_discovered_or_armed(route, capture_id, settlement_at, 30.0, now)
    runtime._mark_armed(route, capture_id, settlement_at, now, route_plan, {"passed": True})

    with pytest.raises(RuntimeError, match="fault_after_config"):
        store.begin_funding_entry_attempt(
            position_id=capture_id,
            route_key=route["route_key"],
            route_entry_key=route_entry_key(route),
            attempt_id="faulted-attempt",
            settlement_at=settlement_at,
            submitted_at=now,
            fault_after="config",
        )

    position = store.funding_capture_position_by_id(capture_id)
    assert position["state"] == "ARMED"
    assert position["config"].get("entry_attempt_submitted") is not True
    assert position["config"].get("entry_attempts") in (None, [])
    assert runtime._opportunity_guard(capture_id)["allowed"] is True


def test_legacy_armed_submitted_marker_recovers_aborted_idempotently(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    capture_id, attempt_id, _route = _seed_entry_submitted_attempt(store, now)
    store.update_funding_capture_position_state(capture_id, "ARMED", now)

    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now + timedelta(seconds=5)))
    second = bot.synchronized_runtime.recover_stale_entry_submissions(now + timedelta(seconds=6))

    position = store.funding_capture_position_by_id(capture_id)
    assert bot.last_runtime_recovery["entry_submitted"]["recovered_aborted"] == 1
    assert second["processed"] == 0
    assert position["state"] == "FAILED"
    assert position["config"]["entry_attempt_id"] == attempt_id
    assert position["config"]["entry_attempt_state"] == "RECOVERED_ABORTED"
    assert all(
        not (
            row["config"].get("entry_attempt_submitted")
            and row["state"] not in {
                "ENTRY_SUBMITTED",
                "OPEN",
                "FAILED",
                "REJECTED_AFTER_SUBMISSION",
                "CLOSED_PENDING_RECONCILIATION",
                "CLOSED_REQUIRES_REVIEW",
                "RECONCILED",
                "UNRECONCILED",
            }
        )
        for row in store.funding_capture_position_rows()
    )


def test_entry_submitted_recovery_releases_reserve_without_orders(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    capture_id, _attempt_id, _route = _seed_entry_submitted_attempt(store, now, reserve=True)

    PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now + timedelta(seconds=5)))

    assert store.funding_capture_position_by_id(capture_id)["state"] == "FAILED"
    assert [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "collateral_release"]
    assert all(row["reserved_margin"] == pytest.approx(0.0) for row in store.funding_paper_account_rows())


def test_entry_submitted_recovery_unwinds_one_leg_exposure(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    capture_id, _attempt_id, _route = _seed_entry_submitted_attempt(
        store,
        now,
        reserve=True,
        order_sides=("long",),
    )

    PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now + timedelta(seconds=5)))

    assert store.funding_capture_position_by_id(capture_id)["state"] == "FAILED"
    orders = store.funding_paper_order_rows(capture_id)
    assert {order["order_intent"] for order in orders} >= {"ENTRY", "UNWIND"}
    assert all(row["reserved_margin"] == pytest.approx(0.0) for row in store.funding_paper_account_rows())


def test_entry_submitted_recovery_completes_open_after_both_legs_filled(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    capture_id, _attempt_id, _route = _seed_entry_submitted_attempt(
        store,
        now,
        reserve=True,
        order_sides=("long", "short"),
    )

    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now + timedelta(seconds=5)))
    second = bot.synchronized_runtime.recover_stale_entry_submissions(now + timedelta(seconds=6))

    position = store.funding_capture_position_by_id(capture_id)
    assert bot.last_runtime_recovery["entry_submitted"]["recovered_open"] == 1
    assert second["processed"] == 0
    assert position["state"] == "OPEN"
    assert [row["event_type"] for row in store.paper_event_ledger_rows(capture_id)].count("order_fee") == 2


def test_failed_attempt_cannot_resurrect(tmp_path, monkeypatch) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _attempt_test_bot(tmp_path, now)
    route = _partial_attempt_route(now)
    capture_id = capture_position_id_for_route(route)
    _seed_attempt_observations(bot, route, now)
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))

    assert bot.process_entry_candidates([route], recheck_before_open=False) == []
    assert store.funding_capture_position_by_id(capture_id)["state"] == "FAILED"
    original_orders = store.funding_paper_order_rows(capture_id)

    bot.clock.advance(1.0)
    _seed_attempt_observations(bot, route, bot.clock.now())
    assert bot.process_entry_candidates([route], recheck_before_open=False) == []

    position = store.funding_capture_position_by_id(capture_id)
    assert position["state"] == "FAILED"
    assert position["config"]["entry_attempt_state"] == "REJECTED_AFTER_SUBMISSION"
    assert len(position["config"]["entry_attempts"]) == 1
    assert store.funding_paper_order_rows(capture_id) == original_orders


def test_failed_attempt_retry_next_settlement_uses_new_order_and_fee_ids(tmp_path, monkeypatch) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _attempt_test_bot(tmp_path, now)
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))

    first_route = _partial_attempt_route(now)
    first_capture = capture_position_id_for_route(first_route)
    _seed_attempt_observations(bot, first_route, now)
    assert bot.process_entry_candidates([first_route], recheck_before_open=False) == []
    first_order_ids = {row["paper_order_id"] for row in store.funding_paper_order_rows(first_capture)}
    first_fee_keys = {
        row["event_key"]
        for row in store.paper_event_ledger_rows(first_capture)
        if row["event_type"] == "order_fee"
    }

    bot.clock.advance(3600.0)
    second_now = bot.clock.now()
    second_route = _partial_attempt_route(second_now)
    second_capture = capture_position_id_for_route(second_route)
    _seed_attempt_observations(bot, second_route, second_now)
    assert bot.process_entry_candidates([second_route], recheck_before_open=False) == []
    second_order_ids = {row["paper_order_id"] for row in store.funding_paper_order_rows(second_capture)}
    second_fee_keys = {
        row["event_key"]
        for row in store.paper_event_ledger_rows(second_capture)
        if row["event_type"] == "order_fee"
    }

    assert first_capture != second_capture
    assert first_order_ids.isdisjoint(second_order_ids)
    assert first_fee_keys.isdisjoint(second_fee_keys)
    assert len(first_fee_keys) == 4
    assert len(second_fee_keys) == 4


def test_one_opportunity_allows_only_one_submitted_attempt(tmp_path, monkeypatch) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _attempt_test_bot(tmp_path, now)
    route = _partial_attempt_route(now)
    capture_id = capture_position_id_for_route(route)
    _seed_attempt_observations(bot, route, now)
    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))

    bot.process_entry_candidates([route], recheck_before_open=False)
    first_position = store.funding_capture_position_by_id(capture_id)
    first_attempt_id = first_position["config"]["entry_attempt_id"]

    bot.clock.advance(1.0)
    bot.process_entry_candidates([route], recheck_before_open=False)
    second_position = store.funding_capture_position_by_id(capture_id)

    assert second_position["config"]["entry_attempt_id"] == first_attempt_id
    assert [a["attempt_id"] for a in second_position["config"]["entry_attempts"]] == [first_attempt_id]
    assert len([o for o in store.funding_paper_order_rows(capture_id) if o["order_intent"] == "ENTRY"]) == 2


def _seed_history_instrument(store: SQLiteStore, observed_at: datetime) -> None:
    store.upsert_funding_instruments([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "canonical_asset": "BTC",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "contract_type": "linear_perpetual",
            "contract_multiplier": 1.0,
            "status": "active",
            "observed_at": observed_at.isoformat(),
            "raw": {},
        }
    ])


def test_history_row_without_semantics_remains_unclear(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    scheduled = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    _seed_history_instrument(store, scheduled)
    store.upsert_funding_history([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_at": scheduled.isoformat(),
            "funding_rate": 0.001,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": 0.001 / 8.0,
            "observed_at": scheduled.isoformat(),
            "raw": {},
        }
    ])
    row = store.funding_history_rate_near("binance", "BTCUSDT", scheduled.isoformat())
    raw = json.loads(row["raw_json"])

    assert raw["rate_semantics"] == "unclear"
    assert raw["rate_semantics_source"] == "unknown"


def test_legacy_default_row_cannot_reconcile(tmp_path) -> None:
    from smart_money_radar.paper_bot.settlement import StoredFundingSettlementDataProvider

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    scheduled = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    _seed_history_instrument(store, scheduled)
    store.upsert_funding_history([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_at": scheduled.isoformat(),
            "funding_rate": 0.001,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": 0.001 / 8.0,
            "observed_at": scheduled.isoformat(),
            "raw": {
                "rate_semantics": "realized_settlement",
                "rate_semantics_source": "legacy_default",
            },
        }
    ])
    provider = StoredFundingSettlementDataProvider(store)

    assert provider.get_public_funding_event("binance", "BTCUSDT", scheduled, 120.0) is None


def test_entry_does_not_call_long_history(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    route = _v2_runtime_route(now)
    store.upsert_funding_instruments([
        {
            "venue": leg["venue"],
            "symbol": leg["symbol"],
            "canonical_asset": "BTC",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "contract_type": "linear_perpetual",
            "contract_multiplier": 1.0,
            "status": "active",
            "observed_at": now.isoformat(),
            "raw": {},
        }
        for leg in route["legs"]
    ])
    clients = _install_targeted_refresh_clients(type("RouteHolder", (), {"hot_routes": {route["route_key"]: route}})(), route["route_key"], route)
    by_venue = {client.venue: client for client in clients}
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda venue, *a, **k: by_venue.get(str(venue).lower()))
    monkeypatch.setattr(store, "funding_history_rows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("history disabled")))
    monkeypatch.setattr(store, "funding_orderbook_sequences", lambda *a, **k: (_ for _ in ()).throw(AssertionError("orderbook history disabled")))
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now))

    refreshed = bot.direct_focused_recheck_route(route)

    assert refreshed is not None
    assert by_venue["binance"].orderbook_calls == 1
    assert by_venue["bybit"].orderbook_calls == 1


def test_empty_history_and_negative_old_history_produce_identical_entry_decision(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
    import smart_money_radar.funding.trader as trader_module

    def run_case(db_name: str, with_negative_history: bool) -> tuple[list[str], float, int]:
        now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
        store = SQLiteStore(tmp_path / db_name)
        store.init_db()
        store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
        if with_negative_history:
            _seed_history_instrument(store, now - timedelta(days=90))
            store.upsert_funding_history([
                {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "funding_at": (now - timedelta(days=90)).isoformat(),
                    "funding_rate": -0.50,
                    "funding_interval_hours": 8.0,
                    "hourly_funding_rate": -0.50 / 8.0,
                    "observed_at": (now - timedelta(days=90)).isoformat(),
                    "raw": {"rate_semantics": "predicted_next"},
                }
            ])
        bot = PaperBot(
            store,
            PaperBotConfig(
                telegram_enabled=False,
                focused_recheck_enabled=False,
                venue_starting_balance=10_000.0,
                target_notional_per_leg=500.0,
            ).validated(),
            clock=FakeClock(now),
        )
        route = _v2_runtime_route(now)
        _seed_attempt_observations(bot, route, now)
        opened = bot.process_entry_candidates([route], recheck_before_open=False)
        capture_id = capture_position_id_for_route(route)
        position = store.funding_capture_position_by_id(capture_id)
        return opened, float(position["quantity"]), len(store.funding_paper_order_rows(capture_id))

    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    empty = run_case("empty.sqlite", False)
    negative = run_case("negative.sqlite", True)

    assert empty == negative
    assert empty[1] == pytest.approx(4.99)
    assert empty[2] == 2


def _fresh_probe_route(now: datetime) -> dict:
    route = _v2_runtime_route(now, next_lead_seconds=3600)
    route["evidence"]["targeted_refresh"] = {
        "quality": "FRESH",
        "snapshot_id": "probe-refresh-1",
    }
    return route


def test_schedule_probe_with_stale_response_does_not_count(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 10, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_by_id(position_id)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = _fresh_probe_route(now)
    for leg in route["legs"]:
        leg["response_received_at"] = (now - timedelta(seconds=2.001)).isoformat()
        leg["orderbook_response_received_at"] = (now - timedelta(seconds=2.001)).isoformat()

    probe_state = runtime._record_schedule_probe(position, route, now)

    assert probe_state["probes"][-1]["fresh"] is False
    assert probe_state["probes"][-1]["invalid_reason"] == "schedule_probe_not_fresh_targeted_snapshot"


def test_schedule_probe_with_1001ms_skew_does_not_count(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 10, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    position = store.funding_capture_position_by_id(position_id)
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    route = _fresh_probe_route(now)
    route["legs"][0]["response_received_at"] = now.isoformat()
    route["legs"][0]["orderbook_response_received_at"] = now.isoformat()
    route["legs"][1]["response_received_at"] = (now - timedelta(milliseconds=1001)).isoformat()
    route["legs"][1]["orderbook_response_received_at"] = (now - timedelta(milliseconds=1001)).isoformat()

    probe_state = runtime._record_schedule_probe(position, route, now)

    assert probe_state["probes"][-1]["fresh"] is False
    assert probe_state["probes"][-1]["cross_venue_skew_seconds"] == pytest.approx(1.001)


def test_hold_legging_reserve_uses_observed_returns(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2

    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    runtime = SynchronizedFundingRuntimeV2(
        store=store,
        config=PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now),
        observations_by_route={},
    )
    observations = []
    long_mark = 100.0
    for index, return_bps in enumerate([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]):
        if index > 0:
            long_mark *= 1.0 + return_bps / 10_000.0
        observations.append(
            {
                "observed_at": (now + timedelta(seconds=index)).isoformat(),
                "long_mark": long_mark,
                "short_mark": 100.0,
            }
        )

    p95 = runtime._p95_abs_mark_return_1s_from_observations(observations)
    hold = hold_economics(
        next_conservative_funding_gross=10.0,
        current_close_fees=0.50,
        reference_notional=500.0,
        wait_seconds=3600.0,
        entry_basis_reserve_bps=30.0,
        adverse_basis_change_30s_bps=[0.0] * 10,
        p95_abs_mark_return_1s_bps=p95,
    )

    assert p95 == pytest.approx(10.0)
    assert hold["hold_legging_reserve_bps"] == pytest.approx(20.0)


def test_generic_funding_rate_cannot_become_normalized_next_rate_in_core() -> None:
    from smart_money_radar.funding.economics import route_leg

    market = {
        "venue": "binance",
        "symbol": "BTCUSDT",
        "funding_rate": 0.123,
        "funding_interval_hours": 1.0,
        "hourly_funding_rate": 0.123,
    }
    book = {"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]], "best_bid": 99.0, "best_ask": 101.0}
    leg = route_leg("long", market, book, {"vwap": 101.0, "filled_size": 1.0, "filled_notional": 101.0}, {"vwap": 99.0}, 0.0, 100.0)

    assert leg["funding_rate"] == pytest.approx(0.123)
    assert leg["normalized_next_funding_rate"] is None
    capability = capability_from_market(leg)
    assert "normalized_next_funding_rate_missing" in synchronized_capability_rejection(capability)


def test_unknown_adapter_contract_is_research_only() -> None:
    capability = capability_from_market(
        {
            "venue": "unknownx",
            "funding_rate": 0.001,
            "normalized_next_funding_rate": 0.001,
            "next_funding_at": "2026-07-28T16:00:00+00:00",
            "mark_price": 100.0,
            "index_price": 100.0,
            "taker_fee_rate": 0.0005,
        }
    )
    reasons = synchronized_capability_rejection(capability)

    assert not synchronized_paper_eligible(capability)
    assert "contract_kind_not_linear_perpetual" in reasons
    assert "funding_semantics_not_next_settlement" in reasons
    assert "funding_rate_unit_not_fraction_per_settlement" in reasons


def test_adapter_capability_inventory_reports_paper_research_and_deactivated() -> None:
    paper_market = {
        "venue": "binance",
        "environment": "mainnet",
        **_trusted_paper_market_fields("binance", "2026-07-28T15:59:59+00:00"),
        "contract_status": "active",
        "contract_kind": "linear_perpetual",
        "supports_perpetuals": True,
        "is_linear_contract": True,
        "supports_discrete_funding": True,
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "normalized_next_funding_rate": 0.001,
        "funding_interval_hours": 8.0,
        "published_funding_interval_hours": 8.0,
        "next_funding_at": "2026-07-28T16:00:00+00:00",
        "mark_price": 100.0,
        "index_price": 100.0,
        "orderbook_response_received_at": "2026-07-28T15:59:59+00:00",
        "orderbook_depth_available": True,
        "volume_24h_usd": 1_000_000.0,
        "open_interest_usd": 1_000_000.0,
        "taker_fee_rate": 0.0005,
        "quantity_step": 0.01,
        "min_notional_usd": 5.0,
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": "adapter_binance_test",
    }
    rows = venue_inventory_rows(
        registered_venues=["binance", "unknownx", "bingx"],
        active_venues=["binance", "unknownx", "bingx"],
        sample_markets=[paper_market, {"venue": "unknownx", "funding_rate": 0.001}],
    )
    by_venue = {row["venue"]: row for row in rows}

    assert by_venue["binance"]["status"] == "PAPER_ELIGIBLE"
    assert by_venue["binance"]["reason"] == "all_synchronized_funding_gates_passed"
    assert by_venue["unknownx"]["status"] == "RESEARCH_ONLY"
    assert "contract_kind_not_linear_perpetual" in by_venue["unknownx"]["reason"]
    assert by_venue["bingx"] == {"venue": "bingx", "status": "DEACTIVATED", "reason": "user_deactivated"}


def test_v2_equity_counts_open_v2_position_and_formula(tmp_path) -> None:
    from smart_money_radar.storage import utc_now_iso

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    now_iso = utc_now_iso()
    store.upsert_funding_capture_position({
        "position_id": "fc-equity",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 5.0,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now_iso,
        "config": {
            "route_key": "BTC:binance:bybit",
            "data_quality": {"state": "HEALTHY"},
            "last_valid_executable_snapshot": {
                "observed_at": now_iso,
                "paper_price_pnl": 12.0,
                "paper_close_fees": 1.5,
                "paper_confirmed_funding_pnl": 3.0,
            },
        },
    })
    store.update_funding_paper_account_reserved("binance", 100.0)
    store.update_funding_paper_account_reserved("bybit", 50.0)

    snapshot = store.record_funding_paper_equity_snapshot()

    assert snapshot["legacy_open_position_count"] == 0
    assert snapshot["v2_open_position_count"] == 1
    assert snapshot["total_open_position_count"] == 1
    assert snapshot["total_cash"] == pytest.approx(20_000.0)
    assert snapshot["total_reserved_margin"] == pytest.approx(150.0)
    assert snapshot["available_cash"] == pytest.approx(19_850.0)
    assert snapshot["v2_unrealized_price_pnl"] == pytest.approx(12.0)
    assert snapshot["v2_estimated_close_fees"] == pytest.approx(1.5)
    assert snapshot["v2_net_liquidation_pnl"] == pytest.approx(10.5)
    assert snapshot["total_equity"] == pytest.approx(20_010.5)
    assert snapshot["equity_quality"] == "FRESH"


def test_v2_equity_does_not_double_count_confirmed_funding(tmp_path) -> None:
    from smart_money_radar.storage import utc_now_iso

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    store.update_funding_paper_account_cash("binance", 3.0)
    now_iso = utc_now_iso()
    store.upsert_funding_capture_position({
        "position_id": "fc-equity-funding",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 5.0,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": now_iso,
        "config": {
            "route_key": "BTC:binance:bybit",
            "data_quality": {"state": "HEALTHY"},
            "last_valid_executable_snapshot": {
                "observed_at": now_iso,
                "paper_price_pnl": 0.0,
                "paper_close_fees": 0.0,
                "paper_confirmed_funding_pnl": 3.0,
            },
        },
    })

    snapshot = store.record_funding_paper_equity_snapshot()

    assert snapshot["total_cash"] == pytest.approx(20_003.0)
    assert snapshot["v2_confirmed_funding_pnl"] == pytest.approx(3.0)
    assert snapshot["v2_net_liquidation_pnl"] == pytest.approx(0.0)
    assert snapshot["total_equity"] == pytest.approx(20_003.0)


def test_v2_lifecycle_passes_with_legacy_entry_functions_monkeypatched(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    clients = _install_targeted_refresh_clients(bot, route["route_key"], route)

    bot.clock.advance(1.0)
    outcomes = bot.process_open_positions()

    assert opened == [capture_id]
    assert outcomes == []
    assert store.funding_capture_position_by_id(capture_id)["state"] == "OPEN"
    assert {client.venue: client.market_snapshot_calls for client in clients} == {
        "binance": 1,
        "bybit": 1,
        "okx": 0,
    }


def test_numeric_fee_without_provenance_blocks_armed(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now))
    route = _v2_runtime_route(now)
    for key in ("fee_source", "fee_evidence", "fee_observed_at", "fee_reviewed_at"):
        route["legs"][0].pop(key, None)
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    capture_id = capture_position_id_for_route(route)
    assert result["opened"] is False
    assert result["reason"] == "route_plan_blocked"
    assert any("fee" in blocker for blocker in result["blockers"])
    assert store.funding_capture_position_by_id(capture_id)["state"] == "DISCOVERED"


def _fee_status_market(now: datetime, venue: str = "binance") -> dict:
    return {
        "venue": venue,
        "environment": "mainnet",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "taker_fee_rate": 0.0005,
        "fee_source": "configured_trusted_fee",
        "fee_evidence": _trusted_fee_evidence(venue, now.isoformat()),
    }


def test_trusted_looking_fee_source_without_evidence_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)
    market.pop("fee_evidence")
    market["fee_source"] = "venue_public_tier"

    status = fee_evidence_status(market, "taker", now=now)

    assert not status["verified"]
    assert status["blocker"] == "taker_fee_evidence_missing"


def test_fee_evidence_venue_mismatch_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)
    market["fee_evidence"]["venue"] = "bybit"

    status = fee_evidence_status(market, "taker", now=now)

    assert not status["verified"]
    assert status["blocker"] == "taker_fee_venue_mismatch"


def test_fee_evidence_role_mismatch_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)
    market["fee_evidence"]["liquidity_role"] = "maker"

    status = fee_evidence_status(market, "taker", now=now)

    assert not status["verified"]
    assert status["blocker"] == "taker_fee_role_mismatch"


def test_fee_evidence_product_environment_mismatch_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    env_market = _fee_status_market(now)
    env_market["fee_evidence"]["environment"] = "testnet"
    product_market = _fee_status_market(now)
    product_market["fee_evidence"]["product_type"] = "spot"

    env_status = fee_evidence_status(env_market, "taker", now=now)
    product_status = fee_evidence_status(product_market, "taker", now=now)

    assert env_status["blocker"] == "taker_fee_environment_mismatch"
    assert product_status["blocker"] == "taker_fee_product_mismatch"


def test_fee_evidence_stale_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    stale = now - timedelta(days=31)
    market = _fee_status_market(now)
    market["fee_evidence"]["observed_at"] = stale.isoformat()
    market["fee_evidence"]["reviewed_at"] = stale.isoformat()

    status = fee_evidence_status(market, "taker", now=now)

    assert not status["verified"]
    assert status["blocker"] == "taker_fee_evidence_stale"


def test_out_of_range_fee_blocks_without_clamp() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)
    market["taker_fee_rate"] = 0.5

    status = fee_evidence_status(market, "taker", now=now)

    assert not status["verified"]
    assert status["rate"] == pytest.approx(0.5)
    assert status["blocker"] == "taker_fee_rate_out_of_range"


def _clear_fee_override_env(monkeypatch) -> None:
    for name in (
        "FUNDING_BINANCE_TAKER_FEE",
        "FUNDING_BINANCE_MAKER_FEE",
        "FUNDING_BYBIT_TAKER_FEE",
        "FUNDING_FEE_OVERRIDES_JSON",
    ):
        monkeypatch.delenv(name, raising=False)


def test_direct_fee_override_out_of_range_is_invalid_not_clamped(monkeypatch) -> None:
    _clear_fee_override_env(monkeypatch)
    monkeypatch.setenv("FUNDING_BINANCE_TAKER_FEE", "0.5")
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)

    status = fee_evidence_status(market, "taker", now=now)

    assert parsed_fee("0.5") is None
    assert fee_override("binance", "taker") is None
    assert fee_rate_value(market, "taker") is None
    assert math.isnan(funding_fee_rate(market, "taker"))
    assert not status["verified"]
    assert status["trust_status"] == "INVALID"
    assert status["blocker"] == "taker_fee_override_invalid"
    assert status["rate"] != pytest.approx(0.02)


@pytest.mark.parametrize("raw", ["NaN", "inf", "-inf"])
def test_direct_fee_override_non_finite_is_invalid(monkeypatch, raw: str) -> None:
    _clear_fee_override_env(monkeypatch)
    monkeypatch.setenv("FUNDING_BINANCE_TAKER_FEE", raw)
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)

    status = fee_evidence_status(market, "taker", now=now)

    assert fee_rate_value(market, "taker") is None
    assert math.isnan(funding_fee_rate(market, "taker"))
    assert status["blocker"] == "taker_fee_override_invalid"


def test_json_fee_override_out_of_range_is_invalid_not_clamped(monkeypatch) -> None:
    _clear_fee_override_env(monkeypatch)
    monkeypatch.setenv("FUNDING_FEE_OVERRIDES_JSON", json.dumps({"binance": {"taker": 0.5}}))
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)

    status = fee_evidence_status(market, "taker", now=now)

    assert fee_override("binance", "taker") is None
    assert fee_rate_value(market, "taker") is None
    assert math.isnan(funding_fee_rate(market, "taker"))
    assert status["blocker"] == "taker_fee_override_invalid"
    assert status["rate"] != pytest.approx(0.02)


def test_malformed_json_fee_override_is_blocker_not_default(monkeypatch) -> None:
    _clear_fee_override_env(monkeypatch)
    monkeypatch.setenv("FUNDING_FEE_OVERRIDES_JSON", "{bad")
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)

    status = fee_evidence_status(market, "taker", now=now)

    assert fee_rate_value(market, "taker") is None
    assert math.isnan(funding_fee_rate(market, "taker"))
    assert status["blocker"] == "funding_fee_overrides_json_malformed"


def test_valid_fee_override_still_requires_versioned_evidence(monkeypatch) -> None:
    _clear_fee_override_env(monkeypatch)
    monkeypatch.setenv("FUNDING_BINANCE_TAKER_FEE", "0.0004")
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)
    market.pop("fee_evidence")

    status = fee_evidence_status(market, "taker", now=now)

    assert fee_override("binance", "taker") == pytest.approx(0.0004)
    assert fee_rate_value(market, "taker") == pytest.approx(0.0004)
    assert funding_fee_rate(market, "taker") == pytest.approx(0.0004)
    assert status["rate"] == pytest.approx(0.0004)
    assert not status["verified"]
    assert status["blocker"] == "taker_fee_evidence_missing"


def test_absent_fee_override_uses_verified_market_fee(monkeypatch) -> None:
    _clear_fee_override_env(monkeypatch)
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    market = _fee_status_market(now)

    status = fee_evidence_status(market, "taker", now=now)

    assert fee_override("binance", "taker") is None
    assert fee_rate_value(market, "taker") == pytest.approx(0.0005)
    assert funding_fee_rate(market, "taker") == pytest.approx(0.0005)
    assert status["verified"]


def test_full_explicit_trusted_fee_fixture_passes() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    status = fee_evidence_status(_fee_status_market(now), "taker", now=now)

    assert status["verified"]
    assert status["blocker"] is None


def test_missing_endpoint_identity_blocks_armed(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=FakeClock(now))
    route = _v2_runtime_route(now)
    route["legs"][1]["environment_verified"] = None
    route["legs"][1]["endpoint_identity_provenance"] = "unknown"
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    capture_id = capture_position_id_for_route(route)
    assert result["opened"] is False
    assert result["reason"] == "route_plan_blocked"
    assert any("endpoint" in blocker or "environment" in blocker for blocker in result["blockers"])
    assert store.funding_capture_position_by_id(capture_id)["state"] == "DISCOVERED"


def test_verified_runtime_rejects_experimental_simulation_ready_route(tmp_path) -> None:
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now)
    route["evidence"]["paper_mode"] = "EXPERIMENTAL_SIMULATION"
    route["evidence"]["experimental_simulation_ready"] = True
    route["evidence"]["verified_paper_ready"] = False
    route["evidence"]["readiness_policy"] = {
        "experimental_simulation_ready": True,
        "verified_paper_ready": False,
    }
    for key in ("fee_source", "fee_evidence", "fee_observed_at", "fee_reviewed_at"):
        route["legs"][0].pop(key, None)
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    capture_id = capture_position_id_for_route(route)
    assert result["opened"] is False
    assert result["reason"] == "route_plan_blocked"
    assert "verified_paper_ready_false" in result["blockers"]
    assert result["verified_readiness"]["evaluation_mode"] == "VERIFIED_PAPER"
    assert result["verified_readiness"]["experimental_simulation_ready"] is True
    assert store.funding_capture_position_by_id(capture_id)["state"] == "DISCOVERED"


def test_verified_runtime_rejects_experimental_paper_mode(tmp_path) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now)
    route["evidence"]["paper_mode"] = "EXPERIMENTAL_SIMULATION"
    for leg in route["legs"]:
        leg.pop("quantity_step", None)
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    assert result["opened"] is False
    assert result["reason"] == "route_plan_blocked"
    assert "verified_paper_ready_false" in result["blockers"]
    assert any("quantity_step" in blocker for blocker in result["blockers"])
    assert result["verified_readiness"]["evaluation_mode"] == "VERIFIED_PAPER"


def test_verified_runtime_rejects_stale_fake_verified_discovery_json(tmp_path) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot = _paper_bot_for_route(tmp_path, now)
    route = _v2_runtime_route(now)
    route["evidence"]["verified_paper_ready"] = True
    route["evidence"]["synchronized_capability_passed"] = True
    route["evidence"]["readiness_policy"] = {
        "verified_paper_ready": True,
        "verified_paper_blockers": [],
    }
    for key in ("fee_source", "fee_evidence", "fee_observed_at", "fee_reviewed_at"):
        route["legs"][1].pop(key, None)
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    assert result["opened"] is False
    assert result["reason"] == "route_plan_blocked"
    assert "verified_paper_ready_false" in result["blockers"]
    assert result["verified_readiness"]["verified_paper_ready"] is False
    assert any("fee" in blocker for blocker in result["blockers"])


def test_verified_runtime_binance_okx_route_still_opens(tmp_path) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "okx"], 10_000.0)
    bot = PaperBot(
        store,
        PaperBotConfig(
            telegram_enabled=False,
            focused_recheck_enabled=False,
            venue_starting_balance=10_000.0,
            target_notional_per_leg=500.0,
        ).validated(),
        clock=FakeClock(now),
    )
    route = _v2_runtime_route(now, route_key="BTC:binance:okx")
    route["short_venue"] = "okx"
    route["short_symbol"] = "BTC-USDT-SWAP"
    short_leg = route["legs"][1]
    short_leg.update(
        {
            "venue": "okx",
            "symbol": "BTC-USDT-SWAP",
            **_trusted_paper_market_fields("okx", short_leg["response_received_at"]),
            "fee_rate": 0.0005,
            "taker_fee_rate": 0.0005,
            "collateral_asset": "USDT",
            "quote_asset": "USDT",
        }
    )
    _seed_attempt_observations(bot, route, now)

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    capture_id = capture_position_id_for_route(route)
    assert result["opened"] is True
    assert result["position_id"] == capture_id
    assert store.funding_capture_position_by_id(capture_id)["state"] == "OPEN"


def test_cross_stable_requires_fresh_snapshot_and_can_open_with_peg_guard(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    provider = StaticStablecoinPriceProvider(
        {
            "USDT": [
                StablecoinPrice("USDT", 1.0000, "source-a", now.isoformat(), now.isoformat()),
                StablecoinPrice("USDT", 1.0001, "source-b", now.isoformat(), now.isoformat()),
            ],
            "USDC": [
                StablecoinPrice("USDC", 1.0000, "source-a", now.isoformat(), now.isoformat()),
                StablecoinPrice("USDC", 1.0001, "source-b", now.isoformat(), now.isoformat()),
            ],
        }
    )
    stale_route = _v2_runtime_route(now)
    stale_route["legs"][1]["collateral_asset"] = "USDC"
    stale_route["legs"][1]["quote_asset"] = "USDC"
    stale_snapshot = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDC",
        provider=provider,
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    stale_snapshot["observed_at"] = (now - timedelta(seconds=10)).isoformat()
    stale_snapshot["expires_at"] = (now + timedelta(seconds=30)).isoformat()
    stale_route["legs"][0]["stablecoin_route_evaluation"] = stale_snapshot
    stale_plan = build_settlement_capture_opportunity(
        long_market=stale_route["legs"][0],
        short_market=stale_route["legs"][1],
        now=now,
        target_notional=500.0,
    )
    assert "stablecoin_snapshot_stale" in stale_plan["blockers"]

    fresh_route = _v2_runtime_route(now)
    fresh_route["legs"][1]["collateral_asset"] = "USDC"
    fresh_route["legs"][1]["quote_asset"] = "USDC"
    snapshot = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDC",
        provider=provider,
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    assert snapshot["status"] == "PASS"
    fresh_route["legs"][0]["stablecoin_route_evaluation"] = snapshot
    fresh_route["legs"][1]["stablecoin_route_evaluation"] = snapshot
    store, bot = _paper_bot_for_route(tmp_path, now)
    _seed_attempt_observations(bot, fresh_route, now)
    import smart_money_radar.funding.trader as trader_module

    monkeypatch.setattr(trader_module, "build_position_from_route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no legacy")))
    opened = bot.process_entry_candidates([fresh_route], recheck_before_open=False)
    assert opened


def _stablecoin_provider_for(now: datetime, *, disagreement: bool = False) -> StaticStablecoinPriceProvider:
    if disagreement:
        usdt_prices = [
            StablecoinPrice("USDT", 1.0000, "source-a", now.isoformat(), now.isoformat()),
            StablecoinPrice("USDT", 1.0100, "source-b", now.isoformat(), now.isoformat()),
        ]
    else:
        usdt_prices = [
            StablecoinPrice("USDT", 1.0000, "source-a", now.isoformat(), now.isoformat()),
            StablecoinPrice("USDT", 1.0001, "source-b", now.isoformat(), now.isoformat()),
        ]
    return StaticStablecoinPriceProvider(
        {
            "USDT": usdt_prices,
            "USDC": [
                StablecoinPrice("USDC", 1.0000, "source-a", now.isoformat(), now.isoformat()),
                StablecoinPrice("USDC", 1.0001, "source-b", now.isoformat(), now.isoformat()),
            ],
        }
    )


def _cross_stable_plan_with_snapshot(now: datetime, snapshot: dict) -> dict:
    route = _v2_runtime_route(now)
    route["legs"][1]["collateral_asset"] = "USDC"
    route["legs"][1]["quote_asset"] = "USDC"
    route["legs"][0]["stablecoin_route_evaluation"] = snapshot
    route["legs"][1]["stablecoin_route_evaluation"] = snapshot
    return build_settlement_capture_opportunity(
        long_market=route["legs"][0],
        short_market=route["legs"][1],
        now=now,
        target_notional=500.0,
    )


def test_stablecoin_wrong_pair_snapshot_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    snapshot = evaluate_stablecoin_route(
        long_collateral="USDC",
        short_collateral="USDT",
        provider=_stablecoin_provider_for(now),
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    plan = _cross_stable_plan_with_snapshot(now, snapshot)

    assert "stablecoin_snapshot_pair_mismatch" in plan["blockers"]


def test_stablecoin_expired_snapshot_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    snapshot = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDC",
        provider=_stablecoin_provider_for(now),
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    snapshot["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    plan = _cross_stable_plan_with_snapshot(now, snapshot)

    assert "stablecoin_snapshot_expired" in plan["blockers"]


def test_stablecoin_missing_identity_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    snapshot = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDC",
        provider=_stablecoin_provider_for(now),
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    snapshot.pop("source_identity", None)
    plan = _cross_stable_plan_with_snapshot(now, snapshot)

    assert "stablecoin_snapshot_source_missing" in plan["blockers"]


def test_stablecoin_source_disagreement_blocks() -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    snapshot = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDC",
        provider=_stablecoin_provider_for(now, disagreement=True),
        observed_at=now.isoformat(),
        reference_notional=500.0,
        funding_net_before_stablecoin_reserve=8.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )
    plan = _cross_stable_plan_with_snapshot(now, snapshot)

    assert "stablecoin_cross_source_disagreement" in snapshot["blockers"]
    assert "stablecoin_snapshot_not_pass" in plan["blockers"]


def test_focused_recheck_does_not_run_venue_wide_hot_path(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import FundingDataError, PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    import smart_money_radar.funding.trader as trader_module

    class VenueWideOnlyClient:
        thread_safe = True

        def __init__(self, venue: str) -> None:
            self.venue = venue
            self.catalog_calls = 0

        def catalog_and_markets(self, observed_at: str):
            self.catalog_calls += 1
            raise FundingDataError("venue-wide call must not run in focused hot path")

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    bot = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False, focused_recheck_enabled=True).validated(),
        clock=FakeClock(now),
    )
    route = _v2_runtime_route(now)
    clients = {
        "binance": VenueWideOnlyClient("binance"),
        "bybit": VenueWideOnlyClient("bybit"),
    }
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda venue, **_kwargs: clients[str(venue)])

    assert bot.focused_recheck_route(route) is None
    assert {venue: client.catalog_calls for venue, client in clients.items()} == {
        "binance": 0,
        "bybit": 0,
    }


def test_boundary_is_recorded_before_hard_stale_exit_and_reconciles_after_close(tmp_path, monkeypatch) -> None:
    from smart_money_radar.paper_bot.settlement import FakeFundingSettlementDataProvider
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    bot.hot_routes.clear()
    bot.build_venue_clients = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda *args, **kwargs: None)
    position = store.funding_capture_position_by_id(capture_id)
    config = dict(position["config"])
    config["last_valid_executable_route"] = _fresh_route_for_open_position(route, now)
    config["data_quality"] = {
        "state": "DEGRADED",
        "first_degraded_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
    }
    store.update_funding_capture_position_config(capture_id, config, now)
    bot.clock.advance(31)

    outcomes = bot.process_open_positions()

    assert outcomes == ["settlement_crossed", "emergency_unwind"]
    rows = store.funding_settlement_reconciliation_rows(capture_id)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"PENDING"}
    assert {row["evidence"]["lifecycle_state"] for row in rows} == {"BOUNDARY_CROSSED"}
    assert {row["evidence"]["venue_event_state"] for row in rows} == {"VENUE_EVENT_PENDING"}
    assert store.funding_capture_position_by_id(capture_id)["state"] == "CLOSED_PENDING_RECONCILIATION"

    settlement_at = now + timedelta(seconds=30)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": -0.008},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": 0.010},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
        ],
    )
    bot.synchronized_runtime.settlement_data_provider = provider
    first = bot.synchronized_runtime.process_pending_reconciliations(settlement_at + timedelta(seconds=60))
    second = bot.synchronized_runtime.process_pending_reconciliations(settlement_at + timedelta(seconds=60))
    funding_rows = [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "funding"]

    assert first["reconciled"] == 2
    assert second["processed"] == 0
    assert len(funding_rows) == 2
    assert store.funding_capture_position_by_id(capture_id)["state"] == "RECONCILED"


def _boundary_rows_from_cycle_plan(position: dict, cycle: dict, crossed_at: datetime) -> list[dict]:
    rows: list[dict] = []
    scheduled = cycle["scheduled_funding_at"]
    for event in (cycle.get("active_plan") or {}).get("included_settlement_events") or []:
        leg_id = str(event.get("leg_id") or "")
        side = "long" if ":long" in leg_id else "short"
        rows.append(
            {
                "position_id": position["position_id"],
                "cycle_id": cycle["cycle_id"],
                "venue": event["venue"],
                "symbol": event["symbol"],
                "side": side,
                "scheduled_funding_at": scheduled,
                "status": "PENDING",
                "confirmed_funding_rate": None,
                "settlement_mark_price": None,
                "funding_pnl": None,
                "rate_status": None,
                "mark_status": None,
                "evidence": {
                    "lifecycle_state": "BOUNDARY_CROSSED",
                    "boundary_crossed_at": crossed_at.isoformat(),
                    "planned_event": event,
                },
            }
        )
    return rows


@pytest.mark.parametrize(
    "fault_after",
    [
        "first_obligation",
        "all_obligations",
        "cycle_state",
        "before_position_counter",
        "position_counter",
    ],
)
def test_boundary_atomic_fault_retries_to_single_capture(tmp_path, monkeypatch, fault_after) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    position = store.funding_capture_position_by_id(capture_id)
    cycle = store.funding_capture_cycles_for_position(capture_id)[0]
    crossed_at = now + timedelta(seconds=31)
    rows = _boundary_rows_from_cycle_plan(position, cycle, crossed_at)

    with pytest.raises(RuntimeError):
        store.apply_settlement_boundary(
            position_id=capture_id,
            cycle_id=cycle["cycle_id"],
            expected_obligation_count=2,
            reconciliation_rows=rows,
            boundary_evidence={"fault_after": fault_after},
            position_config_update={"boundary_crossed_at": crossed_at.isoformat()},
            now=crossed_at,
            fault_after=fault_after,
        )

    assert store.funding_settlement_reconciliation_rows(capture_id) == []
    assert store.funding_capture_cycles_for_position(capture_id)[0]["state"] == "OPEN"
    assert store.funding_capture_position_by_id(capture_id)["settlements_captured_count"] == 0

    restarted = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(crossed_at),
    )
    _install_targeted_refresh_clients(restarted, route["route_key"], route)
    restarted.hot_routes[route["route_key"]] = _fresh_route_for_open_position(route, crossed_at)
    assert restarted.process_open_positions() == ["settlement_crossed"]
    assert len(store.funding_settlement_reconciliation_rows(capture_id)) == 2
    assert store.funding_capture_position_by_id(capture_id)["settlements_captured_count"] == 1
    assert restarted.process_open_positions() == []
    assert len(store.funding_settlement_reconciliation_rows(capture_id)) == 2
    assert store.funding_capture_position_by_id(capture_id)["settlements_captured_count"] == 1


def test_boundary_recovery_repairs_legacy_obligations_plus_uncrossed_cycle(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, _bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    position = store.funding_capture_position_by_id(capture_id)
    cycle = store.funding_capture_cycles_for_position(capture_id)[0]
    crossed_at = now + timedelta(seconds=31)
    for row in _boundary_rows_from_cycle_plan(position, cycle, crossed_at):
        store.upsert_funding_settlement_reconciliation(row)

    restarted = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(crossed_at),
    )

    assert restarted.last_runtime_recovery["boundary"]["cycles_completed_from_obligations"] == 1
    assert store.funding_capture_cycles_for_position(capture_id)[0]["state"] == "SETTLEMENT_CROSSED"
    assert store.funding_capture_position_by_id(capture_id)["settlements_captured_count"] == 1


def test_zero_obligation_crossed_legacy_cycle_fails_closed(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, _bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    store.update_funding_capture_cycle_state(
        f"{capture_id}:1",
        "SETTLEMENT_CROSSED",
        now + timedelta(seconds=31),
    )

    restarted = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(now + timedelta(seconds=31)),
    )

    assert restarted.last_runtime_recovery["boundary"]["zero_obligation_mismatches"] == 1
    assert store.funding_capture_cycles_for_position(capture_id)[0]["state"] == "SETTLEMENT_PLAN_MISMATCH"
    assert store.funding_capture_position_by_id(capture_id)["settlements_captured_count"] == 0
    assert store.funding_capture_position_by_id(capture_id)["state"] == "SETTLEMENT_PLAN_MISMATCH"
    assert any(row["position_id"] == capture_id for row in store.funding_capture_open_positions())

    _install_targeted_refresh_clients(restarted, route["route_key"], route)
    assert restarted.process_open_positions() == ["closed_requires_review"]
    assert store.funding_capture_position_by_id(capture_id)["state"] == "CLOSED_REQUIRES_REVIEW"
    assert store.funding_settlement_reconciliation_rows(capture_id) == []
    assert all(row["reserved_margin"] == pytest.approx(0.0) for row in store.funding_paper_account_rows())


def test_active_cycle_event_mismatch_does_not_cross_or_create_obligations(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    cycle = store.funding_capture_cycles_for_position(capture_id)[0]
    plan = dict(cycle["active_plan"])
    events = [dict(event) for event in plan["included_settlement_events"]]
    events[0]["scheduled_at"] = (now + timedelta(seconds=33)).isoformat()
    plan["included_settlement_events"] = events
    plan["expected_event_count"] = 2
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE funding_capture_cycles
            SET active_plan_json = ?
            WHERE cycle_id = ?
            """,
            (json.dumps(plan, sort_keys=True), cycle["cycle_id"]),
        )
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)

    outcomes = bot.process_open_positions()

    assert outcomes == ["settlement_plan_mismatch", "closed_requires_review"]
    assert store.funding_settlement_reconciliation_rows(capture_id) == []
    updated_cycle = store.funding_capture_cycles_for_position(capture_id)[0]
    assert updated_cycle["state"] == "SETTLEMENT_PLAN_MISMATCH"
    assert updated_cycle["boundary_evidence"]["blocker"] == "settlement_plan_event_mismatch"
    position = store.funding_capture_position_by_id(capture_id)
    assert position["settlements_captured_count"] == 0
    assert position["state"] == "CLOSED_REQUIRES_REVIEW"
    assert position["config"]["close_requires_review_reason"] == "settlement_plan_event_mismatch"
    assert "funding" not in {row["event_type"] for row in store.paper_event_ledger_rows(capture_id)}
    assert all(row["reserved_margin"] == pytest.approx(0.0) for row in store.funding_paper_account_rows())
    assert all(row["position_id"] != capture_id for row in store.funding_capture_open_positions())


def test_settlement_plan_mismatch_close_failure_remains_retryable(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import CaptureRouteRefreshResult
    from smart_money_radar.funding.presentation import filter_deactivated_funding_paper_payload

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    cycle = store.funding_capture_cycles_for_position(capture_id)[0]
    plan = dict(cycle["active_plan"])
    events = [dict(event) for event in plan["included_settlement_events"]]
    events[0]["scheduled_at"] = (now + timedelta(seconds=33)).isoformat()
    plan["included_settlement_events"] = events
    plan["expected_event_count"] = 2
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE funding_capture_cycles
            SET active_plan_json = ?
            WHERE cycle_id = ?
            """,
            (json.dumps(plan, sort_keys=True), cycle["cycle_id"]),
        )
    bot.clock.advance(31)
    bad_route = _fresh_route_for_open_position(route, bot.clock.now())
    for leg in bad_route["legs"]:
        if leg["side"] == "long":
            leg["bids"] = []
        if leg["side"] == "short":
            leg["asks"] = []

    bot.refresh_open_capture_route = lambda position, now: CaptureRouteRefreshResult(  # type: ignore[method-assign]
        quality="FRESH",
        route=bad_route,
        snapshot_id="bad-close-book",
        reason=None,
    )
    first = bot.process_open_positions()

    assert first == ["settlement_plan_mismatch", "close_failed"]
    position = store.funding_capture_position_by_id(capture_id)
    assert position["state"] == "SETTLEMENT_PLAN_MISMATCH"
    assert any(row["position_id"] == capture_id for row in store.funding_capture_open_positions())
    assert store.funding_settlement_reconciliation_rows(capture_id) == []
    snapshot = store.record_funding_paper_equity_snapshot()
    assert snapshot["legacy_open_position_count"] == 0
    assert snapshot["v2_open_position_count"] == 1
    assert snapshot["total_open_position_count"] == 1
    assert snapshot["open_position_count"] == 1
    with store.connect() as connection:
        persisted = connection.execute(
            """
            SELECT open_position_count, payload_json
            FROM funding_paper_equity_snapshots
            ORDER BY funding_paper_equity_snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()
    assert persisted["open_position_count"] == 1
    persisted_payload = json.loads(persisted["payload_json"])
    assert persisted_payload["v2_open_position_count"] == 1
    assert persisted_payload["total_open_position_count"] == 1
    dashboard = filter_deactivated_funding_paper_payload(
        store.funding_paper_dashboard(refresh_estimates=False)
    )
    assert dashboard["summary"]["v2_open_position_count"] == 1
    assert dashboard["summary"]["open_position_count"] == 1
    assert dashboard["v2_open_positions"][0]["position_id"] == capture_id

    original_process_open_positions = bot.process_open_positions
    open_poll_calls: list[str] = []
    bot.process_open_positions = lambda: open_poll_calls.append("open") or []  # type: ignore[method-assign]
    bot.process_entry_candidates = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("new entry must not run while settlement mismatch remains open")
    )
    blocked = bot.run_iteration()
    assert open_poll_calls == ["open"]
    assert blocked["mode"] == "open_positions"
    assert blocked["open_position_count"] == 1
    assert store.funding_capture_position_by_id(capture_id)["state"] == "SETTLEMENT_PLAN_MISMATCH"
    bot.process_open_positions = original_process_open_positions  # type: ignore[method-assign]

    good_route = _fresh_route_for_open_position(route, bot.clock.now())
    bot.refresh_open_capture_route = lambda position, now: CaptureRouteRefreshResult(  # type: ignore[method-assign]
        quality="FRESH",
        route=good_route,
        snapshot_id="good-close-book",
        reason=None,
    )
    second = bot.process_open_positions()

    assert second == ["closed_requires_review"]
    assert store.funding_capture_position_by_id(capture_id)["state"] == "CLOSED_REQUIRES_REVIEW"
    assert all(row["reserved_margin"] == pytest.approx(0.0) for row in store.funding_paper_account_rows())
    assert "funding" not in {row["event_type"] for row in store.paper_event_ledger_rows(capture_id)}


def test_restart_between_boundary_and_reconciliation_preserves_obligation(tmp_path, monkeypatch) -> None:
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    from smart_money_radar.paper_bot.settlement import FakeFundingSettlementDataProvider

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]
    assert len(store.funding_settlement_reconciliation_rows(capture_id)) == 2

    settlement_at = now + timedelta(seconds=30)
    restarted = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False).validated(),
        clock=FakeClock(settlement_at + timedelta(seconds=60)),
        settlement_data_provider=FakeFundingSettlementDataProvider(
            public_events=[
                {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": -0.008},
                {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": 0.010},
            ],
            mark_snapshots=[
                {"venue": "binance", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
                {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
            ],
        ),
    )

    result = restarted.synchronized_runtime.process_pending_reconciliations(
        settlement_at + timedelta(seconds=60)
    )
    rows = store.funding_settlement_reconciliation_rows(capture_id)
    assert result["reconciled"] == 2
    assert {row["status"] for row in rows} == {"RATE_AND_MARK_RECONCILED"}


def test_two_cycle_paper_mvp_script_reaches_reconciled_state() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_synchronized_funding_two_cycle_paper_mvp.py"),
        ],
        cwd=repo_root,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    events = {line["event"]: line for line in lines}

    assert events["hold"]["active_plan_generation"] == 2
    assert events["cycle_1_boundary"]["obligation_count"] == 2
    assert events["cycle_2_boundary"]["obligation_count"] == 2
    final = events["reconcile"]
    assert final["final_state"] == "RECONCILED"
    assert final["settlements_captured_count"] == 2
    assert final["cycle_1_obligation_count"] == 2
    assert final["cycle_2_obligation_count"] == 2
    assert final["total_funding_ledger_count"] == 4
    assert final["ledger_account_consistency"]["ok"] is True
    assert final["repeat_reconciliation"]["processed"] == 0


def test_risk_close_before_boundary_creates_no_settlement_accrual(tmp_path, monkeypatch) -> None:
    import smart_money_radar.funding.trader as trader_module

    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    bot.hot_routes.clear()
    bot.build_venue_clients = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(trader_module, "funding_client_for_venue", lambda *args, **kwargs: None)
    position = store.funding_capture_position_by_id(capture_id)
    config = dict(position["config"])
    config["last_valid_executable_route"] = _fresh_route_for_open_position(route, now)
    config["data_quality"] = {
        "state": "DEGRADED",
        "first_degraded_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
    }
    store.update_funding_capture_position_config(capture_id, config, now)
    bot.clock.advance(7)

    assert bot.process_open_positions() == ["emergency_unwind"]
    assert store.funding_settlement_reconciliation_rows(capture_id) == []
    assert [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "funding"] == []


def test_post_settlement_replan_records_new_generation_and_blocks_stale_timestamp(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    assert opened == [capture_id]
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    _install_fresh_hot_route(bot, route)
    assert bot.process_open_positions() == ["settlement_crossed"]

    bot.clock.advance(31)
    current = bot.clock.now()
    next_route = _v2_runtime_route(
        current,
        next_lead_seconds=3600,
        short_next_lead_seconds=3600,
        route_key=route["route_key"],
    )
    next_route["legs"][1]["normalized_next_funding_rate"] = 0.004
    bot.hot_routes[route["route_key"]] = next_route
    decision = bot.synchronized_runtime.next_cycle_hold_or_close_decision(
        store.funding_capture_position_by_id(capture_id),
        next_route,
        current,
    )
    replans = store.funding_capture_position_by_id(capture_id)["config"]["post_settlement_replans"]
    assert replans[-1]["plan_generation"] == 2
    assert replans[-1]["new_scheduled_funding_at"] != settlement_at.isoformat()
    assert decision["reason"] in {
        "hold_economics_failed",
        "next_cycle_observations_failed",
        "next_settlement_schedule_mismatch",
        "next_settlement_schedule_not_confirmed",
    }

    stale_route = _v2_runtime_route(
        current,
        next_lead_seconds=-1,
        short_next_lead_seconds=-1,
        route_key=route["route_key"],
    )
    stale_decision = bot.synchronized_runtime.next_cycle_hold_or_close_decision(
        store.funding_capture_position_by_id(capture_id),
        stale_route,
        current,
    )
    assert stale_decision["decision"] == "close"
    assert stale_decision["reason"] == "next_cycle_stale_or_past_settlement"
