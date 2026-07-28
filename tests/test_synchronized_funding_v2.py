from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from smart_money_radar.funding.profiles import funding_bot_profile
from smart_money_radar.funding.strategy_synchronized_funding import (
    STRATEGY_NAME,
    basis_duration_floor_bps,
    entry_window_passed,
    gross_funding_pnl,
    hold_economics,
    initial_entry_economics,
    settlement_alignment_passed,
    synchronized_strategy_candidate,
)
from smart_money_radar.funding.venue_capabilities import (
    VenueCapability,
    capability_from_market,
    synchronized_capability_rejection,
    synchronized_paper_eligible,
    venue_inventory_rows,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.funding.service import active_default_funding_clients
from smart_money_radar.paper_bot.accounting import executable_paper_pnl
from smart_money_radar.paper_bot.execution import entry_fill_state
from smart_money_radar.paper_bot.risk import common_price_move_telemetry, hard_risk_triggered
from smart_money_radar.paper_bot.settlement import (
    cycle_reconciled,
    funding_reconciliation_pnl,
    settlement_reconciliation_key,
)
from smart_money_radar.storage import SQLiteStore


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

    assert rows[0]["status"] == "ACTIVE_RESEARCH_ONLY"
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
        collateral_asset="USDT",
        quote_asset="USDT",
        supports_quantity_step=True,
        supports_min_notional=True,
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
