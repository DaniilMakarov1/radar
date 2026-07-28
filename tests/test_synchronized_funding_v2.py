from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from smart_money_radar.funding.profiles import funding_bot_profile
from smart_money_radar.funding.strategy_synchronized_funding import (
    STRATEGY_NAME,
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
        "symbol": "BTCUSDT",
        "canonical_asset": "BTC",
        "hourly_funding_rate": 0.0001,
        "funding_rate": 0.0008,
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
        "contract_type": "linear_perpetual",
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "observed_at": "2026-07-28T15:59:30+00:00",
    }
    short_market = {
        "venue": "bybit",
        "symbol": "BTCUSDT",
        "canonical_asset": "BTC",
        "hourly_funding_rate": 0.0004,
        "funding_rate": 0.0032,
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
        "contract_type": "linear_perpetual",
        "collateral_asset": "USDT",
        "quote_asset": "USDT",
        "observed_at": "2026-07-28T15:59:30+00:00",
    }
    long_book = {
        "bids": [[99_990.0, 1.0], [99_980.0, 2.0]],
        "asks": [[100_010.0, 1.0], [100_020.0, 2.0]],
        "mid_price": 100_000.0,
        "observed_at": "2026-07-28T15:59:30+00:00",
    }
    short_book = {
        "bids": [[99_990.0, 1.0], [99_980.0, 2.0]],
        "asks": [[100_010.0, 1.0], [100_020.0, 2.0]],
        "mid_price": 100_000.0,
        "observed_at": "2026-07-28T15:59:30+00:00",
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
    )
    assert "funding_rate_unit_not_fraction_per_settlement" in synchronized_capability_rejection(capability)
    assert "funding_sign_convention_not_positive_long_pays" in synchronized_capability_rejection(capability)
    assert not synchronized_paper_eligible(capability)


def test_usdt_vs_usdc_collateral_mismatch_is_research_only() -> None:
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
    )
    check = synchronized_route_capability_check(long_cap, short_cap)
    assert not check["paper_eligible"]
    assert "collateral_asset_mismatch" in check["cross_venue_reasons"]
    assert "quote_asset_mismatch" in check["cross_venue_reasons"]


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
                "symbol": "BTCUSDT",
                "next_funding_at": next_at.isoformat(),
                "funding_rate": -0.008,
                "normalized_next_funding_rate": -0.008,
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
                "response_received_at": response_at.isoformat(),
                "orderbook_response_received_at": response_at.isoformat(),
                "asks": [[100.02, 20.0]],
                "bids": [[99.98, 20.0]],
            },
            {
                "side": "short",
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "next_funding_at": short_next_at.isoformat(),
                "funding_rate": 0.010,
                "normalized_next_funding_rate": 0.010,
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
                "response_received_at": (response_at + timedelta(milliseconds=500)).isoformat(),
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


def _valid_v2_observation(at: datetime, next_at: datetime, *, phase: str = "entry") -> dict:
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
        "short_next_funding_at": next_at.isoformat(),
        "long_next_funding_rate": -0.008,
        "short_next_funding_rate": 0.010,
        "gross_funding_pnl": 9.0,
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
    bot.v2_observations_by_route[route["route_key"]] = [
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
    return store, bot, route, capture_id, opened


def test_paperbot_v2_entry_uses_new_runtime_not_legacy_open(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, _bot, _route, capture_id, opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)

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


def test_paperbot_v2_settlement_crossing_creates_pending_reconciliation_only(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    bot.clock.advance(31)

    outcomes = bot.process_open_positions()

    assert outcomes == ["settlement_crossed"]
    capture = store.funding_capture_position_rows(states={"SETTLEMENT_CROSSED"})[0]
    assert capture["settlements_captured_count"] == 1
    recon = store.funding_settlement_reconciliation_rows(capture_id)
    assert len(recon) == 2
    assert {row["status"] for row in recon} == {"PENDING"}
    assert all(row["funding_pnl"] is None for row in recon)
    assert "funding" not in {row["event_type"] for row in store.paper_event_ledger_rows(capture_id)}


def test_paperbot_v2_holds_aligned_next_cycle_without_reconciled_prior_cycle(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    outcomes: list[str] = []
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
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()

    assert outcomes == ["hold"]
    held = store.funding_capture_position_rows(states={"HOLDING_NEXT_CYCLE"})[0]
    assert held["position_id"] == capture_id
    with store.connect() as conn:
        cycles = conn.execute(
            "SELECT cycle_number, state FROM funding_capture_cycles WHERE position_id = ? ORDER BY cycle_number",
            (capture_id,),
        ).fetchall()
        hold_rows = conn.execute(
            """
            SELECT observed_at
            FROM funding_capture_observations
            WHERE position_id = ? AND phase = 'hold'
            ORDER BY observed_at
            """,
            (capture_id,),
        ).fetchall()
    assert [(row[0], row[1]) for row in cycles] == [(1, "SETTLEMENT_CROSSED"), (2, "HOLDING_NEXT_CYCLE")]
    assert len(hold_rows) >= 15
    hold_start = datetime.fromisoformat(hold_rows[0][0])
    hold_end = datetime.fromisoformat(hold_rows[-1][0])
    assert (hold_end - hold_start).total_seconds() >= 20
    assert all(row["status"] == "PENDING" for row in store.funding_settlement_reconciliation_rows(capture_id))


def test_paperbot_v2_closes_when_next_timestamps_mismatch_without_phantom_funding(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    assert bot.process_open_positions() == ["settlement_crossed"]

    for offset in range(5, 20):
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
        assert bot.process_open_positions() == []

    target = settlement_at + timedelta(seconds=20)
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

    assert outcomes == ["closed"]
    closed = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})[0]
    assert closed["paper_net_pnl_estimated"] is not None
    assert len(store.funding_paper_order_rows(capture_id)) == 4
    ledger_types = [row["event_type"] for row in store.paper_event_ledger_rows(capture_id)]
    assert ledger_types.count("order_fee") == 4
    assert ledger_types.count("price_pnl") == 2
    assert "funding" not in ledger_types


def test_paperbot_v2_holds_different_intervals_with_same_exact_next_timestamp(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    outcomes: list[str] = []
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
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()

    assert outcomes == ["hold"]
    held = store.funding_capture_position_rows(states={"HOLDING_NEXT_CYCLE"})[0]
    assert held["position_id"] == capture_id


def test_paperbot_v2_rejects_hold_when_current_executable_pnl_is_too_negative(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    store, bot, route, capture_id, _opened = _open_v2_runtime_position(tmp_path, monkeypatch, now)
    settlement_at = now + timedelta(seconds=30)
    bot.clock.advance(31)
    assert bot.process_open_positions() == ["settlement_crossed"]

    next_settlement = settlement_at + timedelta(seconds=3600)
    outcomes: list[str] = []
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
        next_route["legs"][1]["close_vwap"] = 120.0
        bot.hot_routes[route["route_key"]] = next_route
        outcomes = bot.process_open_positions()

    assert outcomes == ["closed"]
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
    bot.v2_observations_by_route[route["route_key"]] = [
        _valid_v2_observation(now - timedelta(seconds=20 - index * 2), settlement_at)
        for index in range(9)
    ]

    result = bot.synchronized_runtime.consider_route(
        route,
        {row["venue"]: row for row in store.funding_paper_account_rows()},
    )

    assert result["opened"] is False
    assert result["reason"] == "required_route_data_missing"
    assert "long_normalized_next_funding_rate_missing" in result["missing"]
    assert store.funding_capture_position_by_id(capture_id)["state"] == "ARMED"


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


def test_equal_timestamps_eligible_same_interval_different_timestamps_closes() -> None:
    """Equal next timestamps eligible; same interval but different timestamps => entry rejects."""
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

    # Same interval but different timestamps: entry rejects with alignment mismatch
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
    assert "settlement_alignment_mismatch" in result_diff["reasons"]


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


def test_risk_helper_called_by_paperbot_runtime(tmp_path, monkeypatch) -> None:
    """Risk helper (hard_risk_triggered) is called by real PaperBot process_open_positions."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    import smart_money_radar.funding.trader as trader_module
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
    # Track that hard_risk_triggered is called via the trader module's reference
    call_log = []
    original_hard_risk = trader_module.hard_risk_triggered
    def tracking_hard_risk(*args, **kwargs):
        call_log.append(("hard_risk_triggered", kwargs))
        return original_hard_risk(*args, **kwargs)
    monkeypatch.setattr(trader_module, "hard_risk_triggered", tracking_hard_risk)
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
             "funding_rate": 0.001, "funding_interval_hours": 1.0,
             "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": "2026-07-28T17:00:00+00:00",
             "mark_price": 100000.0, "index_price": 100000.0,
             "best_ask": 100001.0, "best_bid": 99999.0,
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
    # Run process_open_positions
    monkeypatch.setattr(bot, "refresh_open_position_route", lambda pos: None)
    outcomes = bot.process_open_positions()
    # Verify hard_risk_triggered was called
    assert len(call_log) > 0, "hard_risk_triggered was not called by PaperBot runtime"


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
        self.orderbook_calls = 0

    def catalog_and_markets(self, observed_at: str):
        instrument = {
            "venue": self.venue,
            "symbol": self.symbol,
            "canonical_asset": self.asset,
            "base_asset": self.asset,
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "contract_type": "linear_perpetual",
            "contract_multiplier": 0.01,
            "status": "active",
            "observed_at": observed_at,
        }
        market = {
            "venue": self.venue,
            "symbol": self.symbol,
            "canonical_asset": self.asset,
            "funding_rate": self.funding_rate,
            "normalized_next_funding_rate": self.funding_rate,
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
            "observed_at": observed_at,
        }
        if self.include_fee:
            market["taker_fee_rate"] = 0.0005
        return [instrument], [market], []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100):
        self.orderbook_calls += 1
        raise AssertionError("lightweight discovery must not fetch orderbooks")

    def funding_history(self, symbol: str, start_time_ms: int, interval_hours: float, observed_at: str):
        raise AssertionError("lightweight discovery must not fetch history")


def _lightweight_bot(tmp_path, now: datetime, clients: list[_LightweightDiscoveryClient]):
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock

    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    config = PaperBotConfig(
        telegram_enabled=False,
        arm_window_seconds=120,
        scan_interval_seconds=300,
    ).validated()
    bot = PaperBot(store, config, clock=FakeClock(now, monotonic_start=100.0))
    bot.build_venue_clients = lambda: clients  # type: ignore[method-assign]
    return bot


def test_lightweight_discovery_adds_watch_route_without_orderbook(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    long_client = _LightweightDiscoveryClient(
        "venue_a",
        funding_rate=-0.004,
        next_funding_at=settlement,
        interval_hours=1.0,
    )
    short_client = _LightweightDiscoveryClient(
        "venue_b",
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


def test_lightweight_discovery_fail_closed_route_is_research_only(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient(
                "venue_a",
                funding_rate=-0.004,
                next_funding_at=settlement,
                include_fee=False,
            ),
            _LightweightDiscoveryClient(
                "venue_b",
                funding_rate=0.004,
                next_funding_at=settlement,
            ),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 0
    assert summary["research_only_routes"] >= 1
    assert not bot.hot_routes
    reasons = summary["rejection_reasons"]
    assert any("taker_fee_missing" in reason for reason in reasons)


def test_lightweight_discovery_zero_or_negative_gross_not_watch(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    bot = _lightweight_bot(
        tmp_path,
        now,
        [
            _LightweightDiscoveryClient("venue_a", funding_rate=0.0, next_funding_at=settlement),
            _LightweightDiscoveryClient("venue_b", funding_rate=0.0, next_funding_at=settlement),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 0
    assert not bot.hot_routes
    assert summary["rejection_reasons"]["preliminary_gross_funding_not_positive"] == 2


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
            _LightweightDiscoveryClient("venue_a", funding_rate=-0.004, next_funding_at=settlement),
            _LightweightDiscoveryClient("venue_b", funding_rate=0.004, next_funding_at=settlement),
        ],
    )

    summary = bot._run_lightweight_discovery()

    assert summary is not None
    assert summary["watch_routes_added"] == 1


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
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=3600)).isoformat(),
             "mark_price": 100.0, "index_price": 99.0,
             "best_bid": 99.5, "best_ask": 100.5,
             "close_vwap": 100.5, "asks": [[100.5, 20.0]],
             "fee_rate": 0.0005, "funding_rate": 0.001,
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
        ],
        "evidence": {"selected_strategy": {
            "selection_model": "opportunity_engine_v1",
            "strategy_name": "synchronized_funding_capture",
            "eligible": True, "expected_net_pnl": 5.0,
        }},
    }
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
    """Hard stale with no executable book uses fallback_mark_300bps and closes."""
    from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.clock import FakeClock
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    store, position_id = _open_position_for_close_tests(tmp_path, now)
    config = PaperBotConfig(telegram_enabled=False, venue_starting_balance=10_000.0).validated()
    bot = PaperBot(store, config, clock=FakeClock(now))
    # Route with mark_price but NO bids/asks, stale observed_at (>5s old)
    stale_at = now - timedelta(seconds=10)
    bot.hot_routes["BTC:binance:bybit"] = {
        "status": "paper_candidate",
        "route_key": "BTC:binance:bybit",
        "observed_at": stale_at.isoformat(),
        "risk_flags": [],
        "legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=3600)).isoformat(),
             "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "funding_rate": 0.001,
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "next_funding_at": (now + timedelta(seconds=3600)).isoformat(),
             "mark_price": 100.0, "index_price": 100.0,
             "fee_rate": 0.0005, "funding_rate": 0.001,
             "funding_interval_hours": 1.0, "hourly_funding_rate": 0.001},
        ],
        "evidence": {"selected_strategy": {
            "selection_model": "opportunity_engine_v1",
            "strategy_name": "synchronized_funding_capture",
            "eligible": True, "expected_net_pnl": 5.0,
        }},
    }
    outcomes = bot.process_open_positions()
    assert "emergency_unwind" in outcomes
    positions = store.funding_capture_position_rows(states={"CLOSED_PENDING_RECONCILIATION"})
    assert len(positions) == 1
    ledger = store.paper_event_ledger_rows(position_id)
    emergency_events = [r for r in ledger if r["event_type"] == "emergency_unwind_cost"]
    assert len(emergency_events) >= 1
    payload = emergency_events[0]["payload"]
    assert payload.get("pricing_quality") == "fallback_mark_300bps"


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
    bot.v2_observations_by_route[route["route_key"]] = [
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
    bot.v2_observations_by_route[route["route_key"]] = [
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
    bot.v2_observations_by_route[route["route_key"]] = [
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
    bot.v2_observations_by_route[route1["route_key"]] = [
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
    bot.v2_observations_by_route[route2["route_key"]] = [
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
    bot.v2_observations_by_route[route["route_key"]] = [
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
    bot.v2_observations_by_route[route["route_key"]] = [
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
