from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from smart_money_radar.funding.trader import (
    PaperBot,
    PaperBotConfig,
    build_close_payload,
    build_position_from_route,
    close_decision,
    close_message,
    compute_price_move_snapshot,
    compute_spread_snapshot,
    entry_cross_spread,
    final_recheck_fallback_route,
    funding_client_for_venue,
    funding_leg_pnl,
    leg_vwap,
    route_entry_decision,
    route_monitor_decision,
    selected_route_strategy,
    price_stop_loss_triggered,
    spread_stop_loss_triggered,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.notifications import NotificationResult
from smart_money_radar.paper_bot.helpers import format_seconds
from smart_money_radar.paper_bot.position import (
    position_hold_decision,
    settlement_rate_or_entry,
)
from smart_money_radar.paper_bot.telegram import (
    funding_rate_lines,
    status_route_line,
    status_report_message,
)
from smart_money_radar.paper_bot.risk import common_price_move_telemetry
from smart_money_radar.storage import SQLiteStore


def test_risky_venues_are_disabled_for_focused_rechecks() -> None:
    assert funding_client_for_venue("bitunix") is None
    assert funding_client_for_venue("blofin") is None


def test_format_seconds_shows_overdue_settlement() -> None:
    assert format_seconds(-75) == "просрочено 1m 15s"


def test_telegram_uses_paper_badge_for_estimate_paper() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    route = paper_route(now, long_lead=90, short_lead=90)
    route["evidence"]["paper_mode"] = "PAPER"
    route["evidence"]["experimental_paper_ready"] = True
    route["evidence"]["readiness_level"] = "experimental_paper_ready"

    line = status_route_line(route, 1)

    assert "PAPER" in line
    assert "EXPERIMENTAL PAPER" not in line
    assert "SIMULATION READY" not in line


def paper_route(
    now: datetime,
    long_lead: int,
    short_lead: int,
    *,
    live_net: float = 2.0,
    actionable_threshold: float | None = None,
) -> dict:
    long_settlement = (now + timedelta(seconds=long_lead)).isoformat()
    short_settlement = (now + timedelta(seconds=short_lead)).isoformat()
    execution_cost = 0.5
    current_gross = live_net + execution_cost
    route = {
        "funding_scan_id": 1,
        "funding_route_id": 2,
        "route_key": "route-1",
        "route_type": "cex_cex",
        "venue_scope": "cross_venue",
        "status": "paper_candidate",
        "confidence_score": 0.75,
        "canonical_asset": "BTC",
        "long_venue": "aster",
        "long_symbol": "BTCUSDT",
        "short_venue": "binance",
        "short_symbol": "BTCUSDT",
        "target_notional": 500.0,
        "market_capacity": 1_000.0,
        "capital_required": 1_100.0,
        "horizon_days": 1 / 24,
        "current_hourly_spread": 0.001,
        "current_gross_apr": 8.76,
        "projected_hourly_spread": 0.001,
        "projected_gross_apr": 8.76,
        "historical_median_hourly_spread": 0.0,
        "positive_spread_fraction": 1.0,
        "persistence_score": 50.0,
        "history_point_count": 0,
        "expected_gross_funding": current_gross,
        "expected_net_profit": live_net,
        "net_roc_annualized": 1.0,
        "total_fees": execution_cost,
        "slippage_cost": 0.0,
        "basis_gap": 0.0,
        "basis_reserve": 0.0,
        "operations_buffer": 0.0,
        "long_next_funding_at": long_settlement,
        "short_next_funding_at": short_settlement,
        "observed_at": now.isoformat(),
        "risk_flags": [],
        "rationale": [],
        "legs": [
            {
                "side": "long",
                "venue": "aster",
                "symbol": "BTCUSDT",
                "notional": 500.0,
                "base_quantity": 0.01,
                "funding_rate": -0.001,
                "next_funding_at": long_settlement,
            },
            {
                "side": "short",
                "venue": "binance",
                "symbol": "BTCUSDT",
                "notional": 500.0,
                "base_quantity": 0.01,
                "funding_rate": 0.001,
                "next_funding_at": short_settlement,
            },
        ],
        "evidence": {
            "decision_mode": "settlement_capture",
            "current_nowcast_gross": current_gross,
            "current_nowcast_net": live_net,
            "execution_cost": execution_cost,
        },
    }
    if actionable_threshold is not None:
        route["evidence"]["actionable_profit_threshold"] = actionable_threshold
    return route


def test_research_paper_mode_does_not_crash_strategy_selection() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    route = paper_route(now, long_lead=90, short_lead=90, live_net=2.0)
    route["status"] = "research_only"
    strategy = {
        "selection_model": "lightweight_discovery_v1",
        "strategy_name": "synchronized_funding_capture",
        "strategy_class": "synchronized_funding_capture",
        "eligible": False,
        "expected_net_pnl": 2.0,
        "funding_pnl_component": 2.5,
        "execution_cost": 0.5,
        "actionable_profit_threshold": 0.0,
        "reasons": ["research_only_hard_blocked"],
    }
    route["evidence"].update(
        {
            "paper_mode": "RESEARCH",
            "strategy_candidates": [strategy],
            "selected_strategy": strategy,
            "strategy_classification": strategy,
            "blocking_risk_flags": ["research_only_hard_blocked"],
        }
    )
    config = PaperBotConfig().validated()

    assert selected_route_strategy(route, config.strategy_set) is None
    monitor = route_monitor_decision(route, now, config)
    entry = route_entry_decision(route, accounts(), now, config)

    assert not monitor["hot"]
    assert "no_allowed_strategy_candidate" in monitor["reasons"]
    assert not entry["eligible"]
    assert "route_not_candidate" in entry["reasons"]
    assert "no_allowed_strategy_candidate" in entry["reasons"]


def accounts() -> dict:
    return {
        "aster": {"available_balance": 1_000.0},
        "binance": {"available_balance": 1_000.0},
    }


def strategy_route(
    now: datetime,
    strategy_name: str,
    *,
    expected_net: float,
    funding_component: float,
    spread_component: float,
    live_net: float | None = None,
) -> dict:
    route = paper_route(
        now,
        long_lead=45,
        short_lead=45,
        live_net=live_net if live_net is not None else expected_net,
    )
    strategy = {
        "strategy_name": strategy_name,
        "strategy_class": strategy_name,
        "primary_edge": {
            "funding_only": "funding_carry",
            "spread_only": "spread_convergence",
            "combined": "funding_plus_spread",
            "opportunistic_any": "opportunistic_total_edge",
        }[strategy_name],
        "eligible": True,
        "expected_net_pnl": expected_net,
        "gross_edge_pnl": funding_component + spread_component,
        "funding_pnl_component": funding_component,
        "spread_pnl_component": spread_component,
        "execution_cost": 0.5,
        "actionable_profit_threshold": 1.0,
        "reasons": [],
    }
    route["evidence"]["strategy_candidates"] = [strategy]
    route["evidence"]["selected_strategy"] = strategy
    route["evidence"]["strategy_classification"] = strategy
    return route


def ensure_btc_instruments(store: SQLiteStore, observed_at: str) -> None:
    store.upsert_funding_instruments(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "canonical_asset": "BTC",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": observed_at,
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "canonical_asset": "BTC",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": observed_at,
            },
        ]
    )


def test_entry_requires_both_legs_inside_final_entry_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig(
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    too_early = route_entry_decision(
        paper_route(now, long_lead=90, short_lead=90),
        accounts(),
        now,
        config,
    )
    assert not too_early["eligible"]
    assert "short_settlement_outside_final_entry_window" in too_early["reasons"]

    too_late = route_entry_decision(
        paper_route(now, long_lead=20, short_lead=20),
        accounts(),
        now,
        config,
    )
    assert not too_late["eligible"]
    assert "short_settlement_inside_final_deadline" in too_late["reasons"]

    eligible = route_entry_decision(
        paper_route(now, long_lead=45, short_lead=45),
        accounts(),
        now,
        config,
    )
    assert eligible["eligible"]


def test_default_entry_window_targets_t_minus_thirty_seconds() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig().validated()

    too_early = route_entry_decision(
        paper_route(now, long_lead=36, short_lead=36),
        accounts(),
        now,
        config,
    )
    assert not too_early["eligible"]
    assert "long_settlement_outside_final_entry_window" in too_early["reasons"]

    allowed_upper = route_entry_decision(
        paper_route(now, long_lead=35, short_lead=35),
        accounts(),
        now,
        config,
    )
    assert allowed_upper["eligible"]

    allowed_lower = route_entry_decision(
        paper_route(now, long_lead=25, short_lead=25),
        accounts(),
        now,
        config,
    )
    assert allowed_lower["eligible"]

    too_late = route_entry_decision(
        paper_route(now, long_lead=24, short_lead=24),
        accounts(),
        now,
        config,
    )
    assert not too_late["eligible"]
    assert "long_settlement_inside_final_deadline" in too_late["reasons"]


def test_lightweight_discovery_defaults_and_window_validation() -> None:
    default = PaperBotConfig().validated()
    constrained = PaperBotConfig(
        lightweight_foreground_budget_seconds=120.0,
        lightweight_cache_ttl_seconds=5.0,
        lightweight_route_horizon_seconds=900.0,
        lightweight_watch_window_seconds=1_200.0,
    ).validated()

    assert default.lightweight_foreground_budget_seconds == pytest.approx(8.0)
    assert default.lightweight_route_horizon_seconds == pytest.approx(3_600.0)
    assert default.estimate_paper_enabled is True
    assert constrained.lightweight_foreground_budget_seconds == pytest.approx(30.0)
    assert constrained.lightweight_cache_ttl_seconds == pytest.approx(30.0)
    assert constrained.lightweight_route_horizon_seconds == pytest.approx(900.0)
    assert constrained.lightweight_watch_window_seconds == pytest.approx(900.0)


def test_estimate_paper_entry_is_default_and_can_be_disabled() -> None:
    assert PaperBotConfig().validated().estimate_paper_enabled is True
    assert PaperBotConfig(estimate_paper_enabled=False).validated().estimate_paper_enabled is False
    assert PaperBotConfig(estimate_paper_enabled=True).validated().estimate_paper_enabled is True


def test_funding_paper_trader_cli_defaults_to_estimated_paper_and_can_opt_out() -> None:
    from smart_money_radar.cli import build_parser, funding_paper_trader_config

    parser = build_parser()
    default_args = parser.parse_args(["funding-paper-trader", "--iterations", "1", "--no-telegram"])
    verified_only_args = parser.parse_args(
        [
            "funding-paper-trader",
            "--iterations",
            "1",
            "--no-telegram",
            "--no-estimated-funding-paper-entry",
        ]
    )

    assert funding_paper_trader_config(default_args).estimate_paper_enabled is True
    assert funding_paper_trader_config(verified_only_args).estimate_paper_enabled is False


def test_selected_route_strategy_supports_all_strategy_classes() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    for name, funding_component, spread_component in (
        ("funding_only", 2.0, 0.0),
        ("spread_only", -0.25, 2.5),
        ("combined", 1.5, 1.25),
        ("opportunistic_any", 0.75, 0.25),
    ):
        route = strategy_route(
            now,
            name,
            expected_net=1.25,
            funding_component=funding_component,
            spread_component=spread_component,
        )

        selected = selected_route_strategy(
            route,
            ("funding_only", "spread_only", "combined", "opportunistic_any"),
        )

        assert selected is not None
        assert selected["strategy_name"] == name


def test_selected_route_strategy_normalizes_legacy_strategy_alias() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "funding_only",
        expected_net=1.25,
        funding_component=1.75,
        spread_component=0.0,
    )
    route["evidence"]["strategy_candidates"][0]["strategy_name"] = "funding_carry"
    route["evidence"]["strategy_candidates"][0]["strategy_class"] = "funding_carry"

    selected = selected_route_strategy(route, ("funding_only",))

    assert selected is not None
    assert selected["strategy_name"] == "funding_only"


def test_spread_only_entry_uses_strategy_net_not_funding_nowcast() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "spread_only",
        expected_net=1.75,
        funding_component=-0.40,
        spread_component=2.65,
        live_net=-0.40,
    )
    config = PaperBotConfig(
        strategy_set=("spread_only",),
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    decision = route_entry_decision(route, accounts(), now, config)

    assert decision["eligible"]
    assert decision["strategy_name"] == "spread_only"
    assert decision["live_net"] == 1.75


def test_strategy_set_can_reject_otherwise_positive_route() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "spread_only",
        expected_net=1.75,
        funding_component=-0.40,
        spread_component=2.65,
        live_net=-0.40,
    )
    config = PaperBotConfig(
        strategy_set=("funding_only",),
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    decision = route_entry_decision(route, accounts(), now, config)

    assert not decision["eligible"]
    assert "no_allowed_strategy_candidate" in decision["reasons"]


def test_opportunistic_any_entry_can_use_total_edge_with_warning() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "opportunistic_any",
        expected_net=1.50,
        funding_component=1.80,
        spread_component=0.0,
    )
    route["evidence"]["strategy_candidates"][0]["warnings"] = [
        "basis_stress_not_covered",
        "spread_component_absent",
    ]
    config = PaperBotConfig(
        strategy_set=("opportunistic_any",),
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    decision = route_entry_decision(route, accounts(), now, config)

    assert decision["eligible"]
    assert decision["strategy_name"] == "opportunistic_any"
    assert decision["selected_strategy"]["warnings"] == [
        "basis_stress_not_covered",
        "spread_component_absent",
    ]


def test_clean_strategy_has_priority_over_opportunistic_candidate() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "funding_only",
        expected_net=1.25,
        funding_component=1.75,
        spread_component=0.0,
    )
    opportunistic = dict(route["evidence"]["strategy_candidates"][0])
    opportunistic.update(
        {
            "strategy_name": "opportunistic_any",
            "strategy_class": "opportunistic_any",
            "primary_edge": "opportunistic_total_edge",
            "expected_net_pnl": 2.50,
            "warnings": ["basis_stress_not_covered"],
        }
    )
    route["evidence"]["strategy_candidates"].append(opportunistic)

    selected = selected_route_strategy(
        route,
        ("funding_only", "opportunistic_any"),
    )

    assert selected is not None
    assert selected["strategy_name"] == "funding_only"


def test_entry_armed_false_when_route_has_blocking_reason() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "funding_only",
        expected_net=1.25,
        funding_component=1.75,
        spread_component=0.0,
    )
    route["status"] = "watch"
    config = PaperBotConfig(
        strategy_set=("funding_only",),
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
        arm_window_seconds=900,
    ).validated()

    decision = route_entry_decision(route, accounts(), now, config)

    assert not decision["eligible"]
    assert not decision["armed"]
    assert "route_not_candidate" in decision["reasons"]


def test_missing_selected_strategy_eligible_requires_positive_candidate_status() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "funding_only",
        expected_net=1.25,
        funding_component=1.75,
        spread_component=0.0,
    )
    selected = dict(route["evidence"].pop("selected_strategy"))
    selected.pop("eligible", None)
    route["evidence"]["strategy_candidates"] = []
    route["evidence"]["strategy_classification"] = selected
    route["status"] = "watch"

    assert selected_route_strategy(route, ("funding_only",)) is None


def test_invalid_strategy_config_fails_closed() -> None:
    with pytest.raises(ValueError):
        PaperBotConfig(strategy_set=("funding_onlly",)).validated()


def test_default_paper_bot_strategy_set_is_funding_led() -> None:
    config = PaperBotConfig().validated()
    assert config.strategy_set == (
        "single_settlement_hedged_capture",
        "synchronized_funding_capture",
    )
    assert "spread_only" not in config.strategy_set
    assert "opportunistic_any" not in config.strategy_set


def test_build_position_records_selected_strategy_components() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = strategy_route(
        now,
        "combined",
        expected_net=2.25,
        funding_component=1.20,
        spread_component=1.55,
    )
    config = PaperBotConfig(
        strategy_set=("funding_only", "spread_only", "combined"),
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()
    decision = route_entry_decision(route, accounts(), now, config)

    position = build_position_from_route(route, decision, config)

    assert position["expected_live_net"] == 2.25
    assert position["notes"]["strategy"]["strategy_name"] == "combined"
    assert position["notes"]["strategy"]["funding_pnl_component"] == 1.20
    assert position["notes"]["strategy"]["spread_pnl_component"] == 1.55


def test_final_recheck_freeze_uses_recent_successful_snapshot() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now - timedelta(seconds=20), long_lead=28, short_lead=26)
    config = PaperBotConfig(
        final_recheck_freeze_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    fallback = final_recheck_fallback_route(
        route,
        now,
        config,
        "test_freeze",
    )

    assert fallback is not None
    evidence = fallback["evidence"]
    assert evidence["entry_snapshot_frozen"] is True
    assert evidence["focused_recheck"]["mode"] == "final_freeze_last_success_v1"
    assert evidence["focused_recheck"]["snapshot_age_seconds"] == 20


def test_final_recheck_freeze_rejects_stale_snapshot() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now - timedelta(seconds=90), long_lead=98, short_lead=96)
    config = PaperBotConfig(
        final_recheck_freeze_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    assert final_recheck_fallback_route(route, now, config, "test_freeze") is None


def test_final_recheck_freeze_does_not_start_before_fifteen_second_boundary() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now - timedelta(seconds=20), long_lead=36, short_lead=35)
    config = PaperBotConfig(
        final_recheck_freeze_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    assert final_recheck_fallback_route(route, now, config, "test_freeze") is None


def test_entry_rejects_stale_snapshot_inside_final_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig(
        entry_min_lead_seconds=0,
        entry_max_lead_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    decision = route_entry_decision(
        paper_route(now - timedelta(seconds=31), long_lead=44, short_lead=43),
        accounts(),
        now,
        config,
    )

    assert not decision["eligible"]
    assert "entry_snapshot_stale" in decision["reasons"]


def test_default_entry_snapshot_age_boundary_is_twenty_seconds() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig(
        entry_min_lead_seconds=0,
        entry_max_lead_seconds=60,
    ).validated()
    at_boundary = paper_route(now, long_lead=30, short_lead=30)
    at_boundary["observed_at"] = (now - timedelta(seconds=20)).isoformat()
    over_boundary = paper_route(now, long_lead=30, short_lead=30)
    over_boundary["observed_at"] = (
        now - timedelta(seconds=20.001)
    ).isoformat()

    accepted = route_entry_decision(
        at_boundary,
        accounts(),
        now,
        config,
    )
    rejected = route_entry_decision(
        over_boundary,
        accounts(),
        now,
        config,
    )

    assert config.max_entry_snapshot_age_seconds == 20.0
    assert "entry_snapshot_stale" not in accepted["reasons"]
    assert "entry_snapshot_stale" in rejected["reasons"]


def test_default_focused_recheck_route_timeout_is_twenty_seconds() -> None:
    config = PaperBotConfig().validated()

    assert config.focused_recheck_route_timeout_seconds == 20.0


def test_paper_bot_scan_configs_keep_near_miss_full_depth_enabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated())

    assert bot.scan_config().near_miss_full_depth_routes == 50
    assert bot.focused_scan_config().near_miss_full_depth_routes == 50


def test_entry_requires_route_actionable_profit_threshold() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig(
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    too_small = route_entry_decision(
            paper_route(
                now,
                long_lead=45,
                short_lead=45,
                live_net=0.32,
                actionable_threshold=5.0,
            ),
        accounts(),
        now,
        config,
    )
    assert not too_small["eligible"]
    assert "live_net_below_required_profit" in too_small["reasons"]
    assert too_small["required_live_net"] == 5.0

    enough = route_entry_decision(
            paper_route(
                now,
                long_lead=45,
                short_lead=45,
                live_net=5.25,
                actionable_threshold=5.0,
            ),
        accounts(),
        now,
        config,
    )
    assert enough["eligible"]


def test_funding_leg_pnl_signs_match_perp_cashflow() -> None:
    assert funding_leg_pnl("long", 500.0, 0.01) == -5.0
    assert funding_leg_pnl("long", 500.0, -0.01) == 5.0
    assert funding_leg_pnl("short", 500.0, 0.01) == 5.0
    assert funding_leg_pnl("short", 500.0, -0.01) == -5.0


def test_settlement_rate_or_entry_history_vs_fallback() -> None:
    history_row = {"funding_rate": 0.0004, "funding_interval_hours": 8.0}
    entry_leg = {
        "funding_rate": 0.0004,
        "hourly_funding_rate": 0.00005,
        "funding_interval_hours": 8.0,
    }
    assert settlement_rate_or_entry(history_row, entry_leg) == 0.0004
    assert settlement_rate_or_entry(None, entry_leg) == 0.0004


def test_settlement_rate_or_entry_variational_4h_interval() -> None:
    entry_leg = {
        "funding_rate": 0.0004,
        "hourly_funding_rate": 0.0001,
        "funding_interval_hours": 4.0,
    }
    assert settlement_rate_or_entry(None, entry_leg) == 0.0004


def test_settlement_rate_or_entry_hourly_only_fallback() -> None:
    entry_leg = {"hourly_funding_rate": 0.0001, "funding_interval_hours": 4.0}
    assert settlement_rate_or_entry(None, entry_leg) == 0.0004


def test_settlement_rate_or_entry_legacy_hourly_kind_fallback() -> None:
    entry_leg = {
        "funding_rate": 0.0001,
        "funding_interval_hours": 4.0,
        "funding_rate_kind": "published_current_hourly",
    }
    assert settlement_rate_or_entry(None, entry_leg) == 0.0004


def test_open_close_updates_virtual_balances(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    position_id = store.open_funding_paper_position(
        {
            "entry_key": "route-1:long:short",
            "route_key": "route-1",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": "2026-07-19T12:00:00+00:00",
            "short_settlement_at": "2026-07-19T12:00:00+00:00",
            "max_settlement_at": "2026-07-19T12:00:00+00:00",
            "expected_live_gross": 4.0,
            "expected_live_net": 2.0,
            "expected_execution_cost": 2.0,
            "entry_legs": [],
            "entry_evidence": {},
        }
    )

    after_open = {
        row["venue"]: row for row in store.funding_paper_account_rows()
    }
    assert after_open["aster"]["reserved_margin"] == 550.0
    assert after_open["binance"]["available_balance"] == 450.0

    store.close_funding_paper_position(
        position_id,
        {
            "actual_funding_pnl": 5.0,
            "actual_execution_cost": 2.0,
            "actual_net_pnl": 3.0,
            "long_cash_delta": 1.0,
            "short_cash_delta": 2.0,
            "close_reason": "test",
            "settlement": {},
        },
    )
    after_close = {
        row["venue"]: row for row in store.funding_paper_account_rows()
    }
    assert after_close["aster"]["reserved_margin"] == 0.0
    assert after_close["binance"]["reserved_margin"] == 0.0
    assert after_close["aster"]["cash_balance"] == 1_001.0
    assert after_close["binance"]["cash_balance"] == 1_002.0
    report_rows = store.funding_paper_trade_report_rows()
    assert len(report_rows) == 1
    assert report_rows[0]["Открыто"]
    assert report_rows[0]["Закрыто"]
    assert "T" not in report_rows[0]["Закрыто"]
    assert report_rows[0]["Актив"] == "BTC"
    assert report_rows[0]["Маршрут"] == "LONG aster / SHORT binance"
    assert report_rows[0]["Объем $"] == "500.00"
    assert report_rows[0]["Net PnL $"] == "3.00"
    assert "route_key" not in report_rows[0]
    assert "entry_key" not in report_rows[0]


def test_closed_position_reprices_when_funding_history_arrives(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    settlement_at = "2026-07-19T12:00:00+00:00"
    observed_at = "2026-07-19T12:01:00+00:00"
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    store.upsert_funding_instruments(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "canonical_asset": "BTC",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": observed_at,
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "canonical_asset": "BTC",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": observed_at,
            },
        ]
    )
    position_id = store.open_funding_paper_position(
        {
            "entry_key": "route-2:long:short",
            "route_key": "route-2",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": settlement_at,
            "short_settlement_at": settlement_at,
            "max_settlement_at": settlement_at,
            "expected_live_gross": 0.0,
            "expected_live_net": -2.0,
            "expected_execution_cost": 2.0,
            "actual_basis_pnl": 0.75,
            "entry_legs": [],
            "entry_evidence": {},
        }
    )
    store.close_funding_paper_position(
        position_id,
        {
            "actual_funding_pnl": 0.0,
            "actual_basis_pnl": 0.75,
            "actual_execution_cost": 2.0,
            "actual_net_pnl": -1.25,
            "long_cash_delta": -0.625,
            "short_cash_delta": -0.625,
            "close_reason": "settlement_capture_complete_estimated_missing_history",
            "close_evidence": {"current_nowcast_net": -0.4},
            "settlement": {
                "history_missing_fallback": True,
                "long": {
                    "venue": "aster",
                    "symbol": "BTCUSDT",
                    "settlement_at": settlement_at,
                    "funding_rate": 0.0,
                    "source": "entry_estimate_fallback",
                    "history_row": None,
                },
                "short": {
                    "venue": "binance",
                    "symbol": "BTCUSDT",
                    "settlement_at": settlement_at,
                    "funding_rate": 0.0,
                    "source": "entry_estimate_fallback",
                    "history_row": None,
                },
            },
        },
    )
    store.upsert_funding_history(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.001,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.001,
                "observed_at": observed_at,
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": 0.002,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.002,
                "observed_at": observed_at,
            },
        ]
    )

    report_rows = store.funding_paper_trade_report_rows()
    dashboard = store.funding_paper_dashboard()

    assert report_rows[0]["Funding PnL $"] == "1.50"
    assert report_rows[0]["Basis PnL $"] == "0.75"
    assert report_rows[0]["Net PnL $"] == "0.25"
    assert report_rows[0]["Причина закрытия"].startswith("Закрыто старой логикой")
    assert "уже не выглядело положительным" in report_rows[0][
        "Состояние окна при закрытии"
    ]
    assert report_rows[0]["Качество PnL"].startswith("Финальный PnL")
    assert dashboard["closed_positions"][0]["close_reason_label"].startswith(
        "Закрыто старой логикой"
    )
    assert "уже не выглядело положительным" in dashboard["closed_positions"][0][
        "close_window_label"
    ]
    assert dashboard["summary"]["realized_pnl"] == 0.25
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(),
        notifier=notifier,
    )

    assert trader.publish_repriced_pnl_events() == 1
    assert len(notifier.messages) == 1
    assert "Paper Bot PnL REPRICED" in notifier.messages[0]
    assert "Basis PnL: <b>+$0.75</b>" in notifier.messages[0]
    assert "Net PnL: <b>+$0.25</b>" in notifier.messages[0]
    events = store.funding_paper_dashboard()["events"]
    reprice_event = next(
        event for event in events if event["event_type"] == "pnl_repriced"
    )
    assert reprice_event["telegram_status"] == "sent"


def test_settlement_mandatory_exit_uses_funding_without_rollover_accrual(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    settlement_at = "2026-07-19T12:00:00+00:00"
    next_long = "2026-07-19T13:00:00+00:00"
    next_short = "2026-07-19T13:00:00+00:00"
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    ensure_btc_instruments(store, now.isoformat())
    entry_route = paper_route(
        datetime(2026, 7, 19, 11, 59, tzinfo=UTC),
        60,
        60,
        live_net=1.25,
    )
    store.open_funding_paper_position(
        {
            "entry_key": "route-1:2026-07-19T12:00:00+00:00",
            "route_key": "route-1",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": settlement_at,
            "short_settlement_at": settlement_at,
            "max_settlement_at": settlement_at,
            "expected_live_gross": 2.0,
            "expected_live_net": 1.25,
            "expected_execution_cost": 0.5,
            "entry_legs": entry_route["legs"],
            "entry_evidence": entry_route["evidence"],
        }
    )
    store.upsert_funding_history(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.001,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.001,
                "observed_at": now.isoformat(),
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": 0.002,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.002,
                "observed_at": now.isoformat(),
            },
        ]
    )
    scan_id = store.start_funding_scan({"scan_mode": "watch"})
    continuation = paper_route(now, 3_480, 3_480, live_net=2.5)
    continuation["legs"][0]["next_funding_at"] = next_long
    continuation["legs"][1]["next_funding_at"] = next_short
    continuation["long_next_funding_at"] = next_long
    continuation["short_next_funding_at"] = next_short
    store.insert_funding_routes(scan_id, [continuation])
    store.finish_funding_scan(scan_id, "success", route_count=1, paper_candidate_count=1)
    position = store.funding_paper_open_positions()[0]

    decision = close_decision(
        position,
        now,
        store,
        PaperBotConfig(settlement_grace_seconds=0).validated(),
    )

    assert decision["status"] == "close"
    close = decision["close"]
    assert close["close_reason"] == "mandatory_exit_after_first_settlement"
    assert close["actual_funding_pnl"] == 1.5
    assert close["settlement"]["current_settlement_funding_included"] is True
    assert "accrual" not in decision
    after = store.funding_paper_open_positions()[0]
    balances = {row["venue"]: row for row in store.funding_paper_account_rows()}
    assert after["status"] == "open"
    assert after["max_settlement_at"] == settlement_at
    assert after["actual_funding_pnl"] is None
    assert balances["aster"]["cash_balance"] == 1_000.0
    assert balances["binance"]["cash_balance"] == 1_000.0
    assert balances["aster"]["reserved_margin"] == 550.0
    assert balances["binance"]["reserved_margin"] == 550.0


def test_settlement_closes_when_window_is_not_positive(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    settlement_at = "2026-07-19T12:00:00+00:00"
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    ensure_btc_instruments(store, now.isoformat())
    entry_route = paper_route(
        datetime(2026, 7, 19, 11, 59, tzinfo=UTC),
        60,
        60,
        live_net=1.25,
    )
    store.open_funding_paper_position(
        {
            "entry_key": "route-1:2026-07-19T12:00:00+00:00",
            "route_key": "route-1",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": settlement_at,
            "short_settlement_at": settlement_at,
            "max_settlement_at": settlement_at,
            "expected_live_gross": 2.0,
            "expected_live_net": 1.25,
            "expected_execution_cost": 0.5,
            "entry_legs": entry_route["legs"],
            "entry_evidence": entry_route["evidence"],
        }
    )
    store.upsert_funding_history(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.001,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.001,
                "observed_at": now.isoformat(),
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": 0.002,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.002,
                "observed_at": now.isoformat(),
            },
        ]
    )
    scan_id = store.start_funding_scan({"scan_mode": "watch"})
    continuation = paper_route(now, 3_480, 3_480, live_net=-0.1)
    continuation["status"] = "watch"
    store.insert_funding_routes(scan_id, [continuation])
    store.finish_funding_scan(scan_id, "success", route_count=1)
    position = store.funding_paper_open_positions()[0]

    decision = close_decision(
        position,
        now,
        store,
        PaperBotConfig(settlement_grace_seconds=0).validated(),
    )

    assert decision["status"] == "close"
    assert (
        decision["close"]["close_reason"]
        == "arbitrage_window_closed_live_net_non_positive"
    )
    assert decision["close"]["actual_funding_pnl"] == 1.5
    assert decision["close"]["actual_net_pnl"] == 1.0


def test_pre_settlement_closes_when_fresh_window_turns_negative(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    settlement_at = "2026-07-19T12:10:00+00:00"
    entry_route = paper_route(
        datetime(2026, 7, 19, 11, 59, tzinfo=UTC),
        660,
        660,
        live_net=1.25,
    )
    position = {
        "entry_key": "route-1:2026-07-19T12:10:00+00:00",
        "route_key": "route-1",
        "canonical_asset": "BTC",
        "long_venue": "aster",
        "long_symbol": "BTCUSDT",
        "short_venue": "binance",
        "short_symbol": "BTCUSDT",
        "base_quantity": 0.01,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "long_reserved_margin": 550.0,
        "short_reserved_margin": 550.0,
        "long_settlement_at": settlement_at,
        "short_settlement_at": settlement_at,
        "max_settlement_at": settlement_at,
        "expected_live_gross": 2.0,
        "expected_live_net": 1.25,
        "expected_execution_cost": 0.5,
        "entry_legs": entry_route["legs"],
        "entry_evidence": entry_route["evidence"],
        "entry_cross_spread": 0.0,
        "notes": {"accrued_funding_pnl": 0.75},
    }
    scan_id = store.start_funding_scan({"scan_mode": "watch"})
    continuation = paper_route(now, 480, 480, live_net=-0.2)
    continuation["legs"][0]["vwap"] = 50_000.0
    continuation["legs"][1]["vwap"] = 50_000.0
    store.insert_funding_routes(scan_id, [continuation])
    store.finish_funding_scan(scan_id, "success", route_count=1)

    decision = close_decision(
        position,
        now,
        store,
        PaperBotConfig(settlement_grace_seconds=0).validated(),
    )

    assert decision["status"] == "close"
    close = decision["close"]
    assert close["close_reason"] == "arbitrage_window_closed_live_net_non_positive"
    assert close["hold_decision"]["pre_settlement_close"] is True
    assert close["actual_funding_pnl"] == 0.75
    assert close["settlement"]["current_funding_pnl"] == 0.0
    assert close["settlement"]["current_settlement_funding_included"] is False
    assert close["settlement"]["long"]["source"] == "pre_settlement_no_funding"


def test_close_message_explains_interval_and_settlement_mismatch(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    settlement_at = "2026-07-19T12:00:00+00:00"
    store.ensure_funding_paper_accounts(["bybit", "aster"], 1_000.0)
    store.upsert_funding_instruments(
        [
            {
                "venue": "bybit",
                "symbol": "TLMUSDT",
                "canonical_asset": "TLM",
                "base_asset": "TLM",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": now.isoformat(),
            },
            {
                "venue": "aster",
                "symbol": "TLMUSDT",
                "canonical_asset": "TLM",
                "base_asset": "TLM",
                "quote_asset": "USDT",
                "collateral_asset": "USDT",
                "contract_type": "perp",
                "status": "live",
                "observed_at": now.isoformat(),
            },
        ]
    )
    entry_route = paper_route(
        datetime(2026, 7, 19, 11, 59, tzinfo=UTC),
        60,
        60,
        live_net=2.25,
    )
    entry_route["long_venue"] = "bybit"
    entry_route["short_venue"] = "aster"
    entry_route["legs"][0].update(
        {
            "venue": "bybit",
            "symbol": "TLMUSDT",
            "funding_interval_hours": 8.0,
            "funding_rate": -0.011,
        }
    )
    entry_route["legs"][1].update(
        {
            "venue": "aster",
            "symbol": "TLMUSDT",
            "funding_interval_hours": 1.0,
            "funding_rate": -0.001,
        }
    )
    position_id = store.open_funding_paper_position(
        {
            "entry_key": "route-1:2026-07-19T12:00:00+00:00",
            "route_key": "route-1",
            "canonical_asset": "TLM",
            "long_venue": "bybit",
            "long_symbol": "TLMUSDT",
            "short_venue": "aster",
            "short_symbol": "TLMUSDT",
            "base_quantity": 1.0,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": settlement_at,
            "short_settlement_at": settlement_at,
            "max_settlement_at": settlement_at,
            "expected_live_gross": 4.0,
            "expected_live_net": 2.25,
            "expected_execution_cost": 1.0,
            "entry_legs": entry_route["legs"],
            "entry_evidence": entry_route["evidence"],
        }
    )
    store.upsert_funding_history(
        [
            {
                "venue": "bybit",
                "symbol": "TLMUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.011,
                "funding_interval_hours": 8.0,
                "hourly_funding_rate": -0.001375,
                "observed_at": now.isoformat(),
            },
            {
                "venue": "aster",
                "symbol": "TLMUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.001,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.001,
                "observed_at": now.isoformat(),
            },
        ]
    )
    scan_id = store.start_funding_scan({"scan_mode": "watch"})
    continuation = paper_route(now, 8 * 3_600, 3_600, live_net=-6.13)
    continuation["long_venue"] = "bybit"
    continuation["short_venue"] = "aster"
    continuation["legs"][0].update(
        {
            "venue": "bybit",
            "symbol": "TLMUSDT",
            "funding_interval_hours": 8.0,
            "funding_rate": -0.019,
        }
    )
    continuation["legs"][1].update(
        {
            "venue": "aster",
            "symbol": "TLMUSDT",
            "funding_interval_hours": 1.0,
            "funding_rate": -0.0014,
        }
    )
    store.insert_funding_routes(scan_id, [continuation])
    store.finish_funding_scan(scan_id, "success", route_count=1)
    position = store.funding_paper_open_positions()[0]

    decision = close_decision(
        position,
        now,
        store,
        PaperBotConfig(settlement_grace_seconds=0).validated(),
    )
    message = close_message(position, decision["close"])

    assert decision["status"] == "close"
    assert "Детали решения" in message
    assert "Fresh live net: <b>-$6.13</b>" in message
    assert "интервалы funding разные: long 8h, short 1h" in message
    assert "следующие funding settlement не совпадают" in message


def test_settlement_closes_when_continuation_route_is_stale(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    settlement_at = "2026-07-19T12:00:00+00:00"
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    ensure_btc_instruments(store, now.isoformat())
    entry_route = paper_route(
        datetime(2026, 7, 19, 11, 59, tzinfo=UTC),
        60,
        60,
        live_net=1.25,
    )
    store.open_funding_paper_position(
        {
            "entry_key": "route-1:2026-07-19T12:00:00+00:00",
            "route_key": "route-1",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": settlement_at,
            "short_settlement_at": settlement_at,
            "max_settlement_at": settlement_at,
            "expected_live_gross": 2.0,
            "expected_live_net": 1.25,
            "expected_execution_cost": 0.5,
            "entry_legs": entry_route["legs"],
            "entry_evidence": entry_route["evidence"],
        }
    )
    store.upsert_funding_history(
        [
            {
                "venue": "aster",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": -0.001,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": -0.001,
                "observed_at": now.isoformat(),
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "funding_at": settlement_at,
                "funding_rate": 0.002,
                "funding_interval_hours": 1.0,
                "hourly_funding_rate": 0.002,
                "observed_at": now.isoformat(),
            },
        ]
    )
    stale_observed_at = now - timedelta(seconds=120)
    scan_id = store.start_funding_scan({"scan_mode": "watch"})
    continuation = paper_route(stale_observed_at, 3_600, 3_600, live_net=10.0)
    store.insert_funding_routes(scan_id, [continuation])
    store.finish_funding_scan(scan_id, "success", route_count=1, paper_candidate_count=1)
    position = store.funding_paper_open_positions()[0]

    decision = close_decision(
        position,
        now,
        store,
        PaperBotConfig(
            settlement_grace_seconds=0,
            max_entry_snapshot_age_seconds=30,
        ).validated(),
    )

    assert decision["status"] == "close"
    assert (
        decision["close"]["close_reason"]
        == "arbitrage_window_unverifiable_route_stale"
    )


def test_retention_database_lock_does_not_crash_trading_loop(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(
        store,
        config=PaperBotConfig(retention_interval_seconds=30),
    )

    def locked_retention(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "smart_money_radar.funding.trader.apply_funding_retention_plan",
        locked_retention,
    )

    trader.apply_watch_scan_retention(minimum_keep_latest_scans=1)

    events = store.funding_paper_dashboard(refresh_estimates=False)["events"]
    assert events[0]["event_type"] == "retention_skipped"
    assert trader.last_retention_monotonic > 0


def test_retention_foreign_key_error_does_not_crash_trading_loop(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(
        store,
        config=PaperBotConfig(retention_interval_seconds=30),
    )

    def broken_retention(*args, **kwargs):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(
        "smart_money_radar.funding.trader.apply_funding_retention_plan",
        broken_retention,
    )

    trader.apply_watch_scan_retention(minimum_keep_latest_scans=1)

    events = store.funding_paper_dashboard(refresh_estimates=False)["events"]
    assert events[0]["event_type"] == "retention_skipped"
    assert events[0]["severity"] == "warning"
    assert trader.last_retention_monotonic > 0


def test_trader_notifies_on_graceful_stop(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(),
        notifier=notifier,
    )

    trader.request_stop("test stop")
    trader.run_loop()

    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert "trader_stopped" in event_types
    assert any("STOPPED" in message for message in notifier.messages)


def test_trader_notifies_on_crash(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = ExplodingTrader(
        store,
        config=PaperBotConfig(),
        notifier=notifier,
    )

    try:
        trader.run_loop()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("run_loop should raise RuntimeError")

    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert "trader_crashed" in event_types
    assert any("CRASHED" in message for message in notifier.messages)


def test_status_report_sends_once_per_interval(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(
            status_report_interval_seconds=1_800,
            status_report_max_routes=3,
        ),
        notifier=notifier,
    )
    now = datetime.now(UTC)
    result = {
        "mode": "full_market",
        "funding_scan_id": None,
        "candidate_count": 1,
        "watch_count": 0,
        "opened_count": 0,
        "closed_count": 0,
        "pending_count": 0,
        "hot_route_count": 1,
        "urgent_route_count": 1,
    }
    preliminary = paper_route(now, 45, 45)
    preliminary["status"] = "watch"
    trader.discovered_routes[preliminary["route_key"]] = preliminary
    candidate = paper_route(now, 45, 45)

    trader.maybe_record_status_report(result, [candidate], [])
    trader.maybe_record_status_report(result, [candidate], [])

    assert len(notifier.messages) == 1
    assert "Paper Bot STATUS" in notifier.messages[0]
    assert "DB scan" in notifier.messages[0]
    assert "Qualified: <b>1</b>" in notifier.messages[0]
    assert "Qualified candidates" in notifier.messages[0]
    assert notifier.messages[0].count("LONG <code>aster BTCUSDT</code>") == 1
    assert "Top detected routes" not in notifier.messages[0]
    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert event_types.count("status_report") == 1


def test_status_report_labels_background_scan_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(status_report_interval_seconds=1_800),
        notifier=notifier,
    )

    trader.maybe_record_status_report(
        {
            "mode": "background_full_market",
            "funding_scan_id": 99,
            "candidate_count": 0,
            "watch_count": 0,
            "opened_count": 0,
            "closed_count": 0,
            "pending_count": 0,
            "hot_route_count": 0,
            "urgent_route_count": 0,
        },
        [],
        [],
    )

    assert len(notifier.messages) == 1
    assert "Background scan" in notifier.messages[0]
    assert "Scan:" not in notifier.messages[0]


def test_funding_rate_display_does_not_multiply_interval_rate_twice() -> None:
    now = datetime.now(UTC)
    route = paper_route(now, 60, 60)
    route["long_venue"] = "risex"
    route["long_symbol"] = "BTC/USDC"
    long_leg = route["legs"][0]
    long_leg["funding_rate"] = 0.001
    long_leg["funding_interval_hours"] = 8.0
    long_leg["hourly_funding_rate"] = 0.001 / 8.0

    message = funding_rate_lines(route)

    assert "0.1000%/8h" in message
    assert "0.8000%/8h" not in message


def test_status_report_uses_published_risex_8h_display_with_hourly_cashflow() -> None:
    now = datetime.now(UTC)
    route = paper_route(now, 60, 60)
    route["long_venue"] = "risex"
    route["long_symbol"] = "BTC/USDC"
    long_leg = route["legs"][0]
    long_leg["venue"] = "risex"
    long_leg["symbol"] = "BTC/USDC"
    long_leg["funding_rate"] = -0.001
    long_leg["funding_interval_hours"] = 1.0
    long_leg["hourly_funding_rate"] = -0.001
    long_leg["published_funding_rate"] = -0.008
    long_leg["published_funding_interval_hours"] = 8.0

    message = status_report_message(
        {"mode": "full_market", "funding_scan_id": 123},
        [route],
        [],
        {"open_position_count": 0, "closed_trade_count": 0, "realized_pnl": 0},
        PaperBotConfig(status_report_interval_seconds=1_800),
    )

    assert "LONG <code>risex BTC/USDC</code> -0.8000%/8h (-0.1000%/h)" in message


def test_status_report_publishes_detected_route_as_preliminary(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(status_report_interval_seconds=1_800),
        notifier=notifier,
    )
    now = datetime.now(UTC)
    watch = strategy_route(
        now,
        "funding_only",
        expected_net=3.0,
        funding_component=3.0,
        spread_component=0.0,
    )
    watch["status"] = "watch"
    watch["discovery_stage"] = "monitor"
    watch["risk_flags"] = ["lightweight_only_requires_focused_underwriting"]
    watch["evidence"]["selected_strategy"]["selection_model"] = (
        "lightweight_discovery_v1"
    )
    watch["evidence"]["selected_strategy"]["funding_pnl_component"] = 3.0

    trader.maybe_record_status_report(
        {
            "mode": "full_market",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": 1,
            "opened_count": 0,
            "closed_count": 0,
            "pending_count": 0,
            "hot_route_count": 1,
            "urgent_route_count": 1,
            "markets_checked": 1_234,
            "venue_health": {
                "requested_count": 30,
                "ready_count": 27,
                "pending_count": 2,
                "unavailable_count": 1,
            },
        },
        [],
        [watch],
    )

    assert len(notifier.messages) == 1
    assert "Routes detected: 1" in notifier.messages[0]
    assert "Venue coverage:</b> 27/30 ready | 2 loading | 1 unavailable" in notifier.messages[0]
    assert "Markets received:</b> 1,234" in notifier.messages[0]
    assert "Top detected routes" in notifier.messages[0]
    assert "LONG <code>aster BTCUSDT</code>" in notifier.messages[0]
    assert "Preliminary funding before focused costs" in notifier.messages[0]
    assert "Focused orderbooks, costs and entry observations: pending" in notifier.messages[0]


def test_status_report_hides_negative_detected_routes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(status_report_interval_seconds=1_800),
        notifier=notifier,
    )
    now = datetime.now(UTC)
    watch = strategy_route(
        now,
        "funding_only",
        expected_net=-2.0,
        funding_component=-1.0,
        spread_component=0.0,
    )
    watch["status"] = "watch"
    watch["discovery_stage"] = "monitor"
    watch["risk_flags"] = ["lightweight_only_requires_focused_underwriting"]
    watch["evidence"]["current_nowcast_net"] = -2.0
    watch["evidence"]["selected_strategy"]["selection_model"] = (
        "lightweight_discovery_v1"
    )

    trader.maybe_record_status_report(
        {
            "mode": "hot_routes",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": 1,
            "opened_count": 0,
            "closed_count": 0,
            "pending_count": 0,
            "hot_route_count": 1,
            "urgent_route_count": 0,
            "detected_route_count": 1,
        },
        [],
        [watch],
    )

    assert len(notifier.messages) == 1
    assert "Routes detected: 1" in notifier.messages[0]
    assert "Top detected routes" not in notifier.messages[0]
    assert "Preliminary funding before focused costs" not in notifier.messages[0]


def test_status_report_distinguishes_active_paper_and_hidden_detected() -> None:
    message = status_report_message(
        {
            "mode": "hot_routes",
            "funding_scan_id": None,
            "detected_route_count": 5,
            "early_route_count": 1,
            "watch_stage_route_count": 2,
            "hot_route_count": 1,
            "urgent_route_count": 1,
            "active_paper_route_count": 3,
            "hidden_detected_route_count": 2,
        },
        [],
        [],
        {"open_position_count": 0, "closed_trade_count": 0, "realized_pnl": 0},
        PaperBotConfig(status_report_interval_seconds=1_800),
    )

    assert "Routes detected: 5" in message
    assert "Early: 1 | Watch: 2 | Monitor: 1 | Urgent: 1" in message
    assert "Paper-ready routes: 3 | Hidden/blocked: 2" in message


def test_status_report_bounds_many_verbose_detected_routes_for_telegram(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(
            status_report_interval_seconds=1_800,
            status_report_max_routes=5,
        ),
        notifier=notifier,
    )
    now = datetime.now(UTC)
    routes = []
    for index in range(20):
        route = strategy_route(
            now,
            "funding_only",
            expected_net=3.0,
            funding_component=3.0,
            spread_component=0.0,
        )
        route["route_key"] = f"verbose-watch-{index}"
        route["canonical_asset"] = f"ASSET{index}"
        route["status"] = "watch"
        route["discovery_stage"] = "early"
        route["risk_flags"] = [
            "lightweight_only_requires_focused_underwriting",
            "long_rate_estimate_not_exact_next",
            "long_exact_next_rate_unavailable",
            "long_volume_24h_missing",
            "short_open_interest_missing",
        ]
        route["evidence"]["selected_strategy"]["selection_model"] = (
            "lightweight_discovery_v1"
        )
        route["evidence"]["capability_check"] = {
            "economics": {
                "modeled_fee_rates": {
                    "long": {
                        "evidence_status": {
                            "fee_evidence_kind": "REVIEWED_STATIC_SCHEDULE",
                            "source_identifier": (
                                "https://example.com/very/long/fee/source/path/"
                                "with/query?symbol=BTCUSDT&venue=binance"
                            ),
                            "fee_schedule_reviewed_at": now.isoformat(),
                            "age_seconds": 172800,
                            "expires_at": (now + timedelta(days=28)).isoformat(),
                            "verified": True,
                            "fallback_required": False,
                            "uncertainty_reserve_required": False,
                        }
                    },
                    "short": {
                        "evidence_status": {
                            "fee_evidence_kind": "UNVERIFIED_MARKET_FIELD",
                            "source_identifier": "unknown",
                            "verified": False,
                            "fallback_required": True,
                            "uncertainty_reserve_required": True,
                        }
                    },
                }
            }
        }
        routes.append(route)

    trader.maybe_record_status_report(
        {
            "mode": "hot_routes",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": len(routes),
            "opened_count": 0,
            "closed_count": 0,
            "pending_count": 0,
            "hot_route_count": 0,
            "urgent_route_count": 0,
            "detected_route_count": len(routes),
            "venue_health": {
                "requested_count": 24,
                "ready_count": 24,
                "pending_count": 0,
                "unavailable_count": 0,
            },
        },
        [],
        routes,
    )

    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert len(message) <= 4096
    assert "Top detected routes" in message
    assert "...and" in message
    assert "more detected routes" in message
    assert "Fees:" not in message


def test_status_report_does_not_publish_negative_live_pnl_candidate(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(status_report_interval_seconds=1_800),
        notifier=notifier,
    )
    now = datetime.now(UTC)

    trader.maybe_record_status_report(
        {
            "mode": "full_market",
            "funding_scan_id": None,
            "candidate_count": 1,
            "watch_count": 0,
            "opened_count": 0,
            "closed_count": 0,
            "pending_count": 0,
            "hot_route_count": 1,
            "urgent_route_count": 1,
        },
        [paper_route(now, 45, 45, live_net=-0.25)],
        [],
    )

    assert len(notifier.messages) == 1
    assert "Qualified: <b>0</b>" in notifier.messages[0]
    assert "LONG <code>aster BTCUSDT</code>" not in notifier.messages[0]
    assert "-$0.25" not in notifier.messages[0]


def test_next_sleep_uses_three_speed_monitoring(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(
        store,
        config=PaperBotConfig(
            scan_interval_seconds=300,
            monitor_interval_seconds=120,
            hot_interval_seconds=6,
        ),
    )
    trader._last_lightweight_discovery_monotonic = trader.clock.monotonic()
    trader.last_full_scan_monotonic = trader.clock.monotonic()

    assert trader.next_sleep_seconds({"hot_route_count": 0}) <= 30
    assert trader.next_sleep_seconds({"hot_route_count": 0}) == pytest.approx(30, abs=0.01)
    assert trader.next_sleep_seconds({"hot_route_count": 1}) == 120
    assert (
        trader.next_sleep_seconds({"hot_route_count": 1, "urgent_route_count": 1})
        == 6
    )
    trader.last_reconciliation_monotonic = trader.clock.monotonic() - 4
    assert trader.next_sleep_seconds({"pending_count": 1}) == pytest.approx(6, abs=0.01)
    assert trader.next_sleep_seconds({"open_position_count": 1}) == 6


def test_monitor_route_is_urgent_only_inside_entry_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = PaperBotConfig(
        arm_window_seconds=900,
    ).validated()

    monitored = route_monitor_decision(paper_route(now, 600, 600), now, config)
    urgent = route_monitor_decision(paper_route(now, 30, 30), now, config)

    assert monitored["hot"]
    assert not monitored["urgent"]
    assert urgent["hot"]
    assert urgent["urgent"]


def test_urgent_route_keeps_focused_loop_after_base_interval(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(
        store,
        config=PaperBotConfig(
            scan_interval_seconds=300,
        ),
    )
    trader.hot_routes["route-1"] = paper_route(now, 30, 30)
    trader.last_full_scan_monotonic = time.monotonic() - 300

    assert trader.should_run_hot_iteration()


def test_non_urgent_hot_route_keeps_focused_loop_when_full_scan_due(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(
        store,
        config=PaperBotConfig(scan_interval_seconds=300),
    )
    trader.hot_routes["route-1"] = paper_route(now, 600, 600)
    trader.last_full_scan_monotonic = time.monotonic() - 301

    assert trader.full_scan_due()
    assert trader.should_run_hot_iteration()


def test_background_scan_does_not_overwrite_newer_hot_route(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = PaperBot(store, config=PaperBotConfig())
    older = paper_route(now - timedelta(seconds=20), 90, 90, live_net=2.0)
    newer = paper_route(now, 90, 90, live_net=2.25)
    trader.hot_routes["route-1"] = newer

    trader.update_hot_routes([older])

    assert trader.hot_routes["route-1"]["evidence"]["current_nowcast_net"] == 2.25


def test_background_full_scan_does_not_block_hot_iterations(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = SlowBackgroundScanTrader(
        store,
        config=PaperBotConfig(scan_interval_seconds=300),
        background_delay=0.35,
    )
    trader.last_full_scan_monotonic = time.monotonic() - 301

    first_started = time.perf_counter()
    first = trader.run_iteration()
    first_elapsed = time.perf_counter() - first_started

    assert first["mode"] == "background_full_scan_wait"
    assert first["background_full_scan_status"] == "started"
    assert trader.background_started.wait(0.2)
    assert first_elapsed < 0.15
    assert trader.hot_iterations == 0

    trader.hot_routes["route-1"] = paper_route(now, 90, 90)

    second_started = time.perf_counter()
    second = trader.run_iteration()
    second_elapsed = time.perf_counter() - second_started

    assert second["mode"] == "critical_hot_routes"
    assert second.get("background_full_scan_running") is True
    assert second_elapsed < 0.15
    assert trader.hot_iterations == 1
    assert trader.last_cancel_event is not None
    assert trader.last_cancel_event.is_set()

    assert trader.background_finished.wait(1.0)
    third = trader.run_iteration()
    assert third["mode"] == "critical_hot_routes"
    assert third["background_full_scan_results"][0]["status"] == "deferred"
    trader.shutdown_background_full_scan()


def test_urgent_hot_route_does_not_start_background_full_scan(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = SlowBackgroundScanTrader(
        store,
        config=PaperBotConfig(scan_interval_seconds=300),
        background_delay=0.1,
    )
    trader.hot_routes["route-1"] = paper_route(now, 30, 30)
    trader.last_full_scan_monotonic = time.monotonic() - 301

    result = trader.run_iteration()

    assert result["mode"] == "critical_hot_routes"
    assert "background_full_scan_status" not in result
    assert not trader.background_started.is_set()


def test_background_discovery_scan_uses_lightweight_clients_only(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "main.sqlite")
    store.init_db()
    trader = PaperBot(store, config=PaperBotConfig())
    now = datetime.now(UTC)
    settlement = now + timedelta(seconds=90)
    clients = [
        LightweightFundingClient("binance", -0.004, settlement),
        LightweightFundingClient("bybit", 0.004, settlement),
    ]

    def fake_run_funding_scan(scan_store, **kwargs):
        raise AssertionError("paper bot background scan must not call run_funding_scan")

    monkeypatch.setattr(
        "smart_money_radar.funding.trader.run_funding_scan",
        fake_run_funding_scan,
    )
    monkeypatch.setattr(
        "smart_money_radar.funding.trader.funding_client_for_venue",
        lambda *_args, **_kwargs: None,
    )
    trader.build_venue_clients = lambda: clients  # type: ignore[method-assign]

    foreground_markets, _warnings = trader._fetch_lightweight_market_snapshots(
        clients,
        now.isoformat(),
    )
    assert len(foreground_markets) == 2
    calls_before_background = [client.catalog_calls for client in clients]
    result = trader.run_discovery_full_scan()

    assert result["mode"] == "background_full_market"
    assert result["watch_count"] == 1
    assert result["funding_scan_id"] is None
    assert [client.catalog_calls for client in clients] == calls_before_background
    assert all(client.orderbook_calls == 0 for client in clients)
    assert all(client.history_calls == 0 for client in clients)
    trader.shutdown_foreground_executors()


def test_hot_route_rechecks_are_parallelized(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = CountingRecheckTrader(
        store,
        config=PaperBotConfig(hot_route_recheck_workers=2),
    )
    for index in range(4):
        route = paper_route(now, 90, 90)
        route["route_key"] = f"route-{index}"
        route["canonical_asset"] = f"ASSET{index}"
        trader.hot_routes[route["route_key"]] = route

    refreshed = trader.refresh_hot_routes()

    assert len(refreshed) == 4
    assert trader.max_active_rechecks == 2


def test_negative_hot_route_disarm_does_not_record_routine_event(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = CountingRecheckTrader(store, config=PaperBotConfig())
    route = paper_route(now, 90, 90, live_net=-2.0)
    trader.hot_routes[route["route_key"]] = route

    refreshed = trader.refresh_hot_routes()

    assert refreshed == []
    assert not trader.hot_routes
    events = store.funding_paper_dashboard(refresh_estimates=False)["events"]
    assert "disarmed" not in {event["event_type"] for event in events}


def test_open_position_keeps_focused_loop_without_hot_route(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    store.open_funding_paper_position(
        {
            "entry_key": "route-open",
            "route_key": "route-open",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": "2026-07-19T12:00:00+00:00",
            "short_settlement_at": "2026-07-19T12:00:00+00:00",
            "max_settlement_at": "2026-07-19T12:00:00+00:00",
            "expected_live_gross": 2.0,
            "expected_live_net": 1.25,
            "expected_execution_cost": 0.5,
            "entry_legs": [],
            "entry_evidence": {},
        }
    )
    trader = PaperBot(
        store,
        config=PaperBotConfig(scan_interval_seconds=300),
    )

    assert trader.should_run_hot_iteration()


def test_v2_open_position_forces_scheduler_priority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    now = datetime.now(UTC)
    store.upsert_funding_capture_position(
        {
            "position_id": "v2-open-1",
            "strategy_name": "synchronized_funding_capture",
            "strategy_version": "synchronized_funding_capture_v2",
            "canonical_asset": "BTC",
            "long_venue": "venue_a",
            "long_symbol": "BTCUSDT",
            "short_venue": "venue_b",
            "short_symbol": "BTCUSDT",
            "quantity": 1.0,
            "target_notional": 500.0,
            "state": "OPEN",
            "opened_at": now.isoformat(),
            "config": {"route_key": "v2-route-1", "entry_legs": []},
        }
    )
    trader = OpenPriorityTrader(store, config=PaperBotConfig(scan_interval_seconds=300))
    trader.background_full_scan_cancel_event = threading.Event()

    result = trader.run_iteration()

    assert result["mode"] == "open_positions"
    assert trader.open_position_calls == 1
    assert trader.background_full_scan_cancel_event.is_set()
    assert not trader.lightweight_called
    assert not trader.hot_iteration_called


def test_background_future_result_not_called_until_done(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = IdleSchedulerTrader(store, config=PaperBotConfig(scan_interval_seconds=300))
    trader.last_full_scan_monotonic = trader.clock.monotonic()
    trader.background_full_scan_future = NotDoneFuture()

    result = trader.run_iteration()

    assert result["mode"] == "background_full_scan_wait"
    assert result["background_full_scan_running"] is True
    assert trader.background_full_scan_future.result_called is False


def test_status_report_can_be_disabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = PaperBot(
        store,
        config=PaperBotConfig(status_report_interval_seconds=0),
        notifier=notifier,
    )

    trader.maybe_record_status_report(
        {"mode": "full_market", "funding_scan_id": 1},
        [paper_route(datetime.now(UTC), 60, 45)],
        [],
    )

    assert notifier.messages == []
    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert "status_report" not in event_types


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> NotificationResult:
        self.messages.append(text)
        return NotificationResult("sent")


class ExplodingTrader(PaperBot):
    def run_iteration(self) -> dict:
        raise RuntimeError("boom")


class CountingRecheckTrader(PaperBot):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.active_rechecks = 0
        self.max_active_rechecks = 0
        self.recheck_lock = threading.Lock()

    def focused_recheck_route(self, route: dict) -> dict | None:
        with self.recheck_lock:
            self.active_rechecks += 1
            self.max_active_rechecks = max(
                self.max_active_rechecks,
                self.active_rechecks,
            )
        try:
            time.sleep(0.05)
            return route
        finally:
            with self.recheck_lock:
                self.active_rechecks -= 1


class InstrumentedFocusedRecheckTrader(PaperBot):
    def __init__(
        self,
        *args,
        fail_market_route_key: str | None = None,
        fail_orderbook_route_key: str | None = None,
        slow_market_route_key: str | None = None,
        slow_orderbook_route_key: str | None = None,
        slow_delay_seconds: float = 0.25,
        trace_path: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fail_market_route_key = fail_market_route_key
        self.fail_orderbook_route_key = fail_orderbook_route_key
        self.slow_market_route_key = slow_market_route_key
        self.slow_orderbook_route_key = slow_orderbook_route_key
        self.slow_delay_seconds = slow_delay_seconds
        self.trace: list[dict] = []
        self.trace_path = trace_path
        self.trace_lock = threading.Lock()
        self.active_outer_rechecks = 0
        self.max_outer_rechecks = 0
        self.outer_start_barrier = None

    def _append_trace_locked(self, row: dict) -> None:
        self.trace.append(row)
        if self.trace_path:
            with open(self.trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _record_trace(self, event: str, **payload) -> None:
        row = {
            "event": event,
            "thread": threading.current_thread().name,
            **payload,
        }
        with self.trace_lock:
            self._append_trace_locked(row)

    def focused_recheck_route(self, route: dict) -> dict | None:
        route_key = str(route.get("route_key") or "")
        with self.trace_lock:
            self.active_outer_rechecks += 1
            self.max_outer_rechecks = max(
                self.max_outer_rechecks,
                self.active_outer_rechecks,
            )
            self._append_trace_locked(
                {
                    "event": "outer_start",
                    "route_key": route_key,
                    "thread": threading.current_thread().name,
                }
            )
            barrier = self.outer_start_barrier
        if barrier is not None:
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
        try:
            return super().focused_recheck_route(route)
        finally:
            with self.trace_lock:
                self.active_outer_rechecks -= 1
                self._append_trace_locked(
                    {
                        "event": "outer_finish",
                        "route_key": route_key,
                        "thread": threading.current_thread().name,
                    }
                )

    def fresh_market_for_route_leg(
        self,
        route: dict,
        side: str,
        observed_at: str,
    ):
        route_key = str(route.get("route_key") or "")
        self._record_trace("market_start", route_key=route_key, side=side)
        if route_key == self.slow_market_route_key:
            time.sleep(self.slow_delay_seconds)
        if route_key == self.fail_market_route_key and side == "long":
            raise RuntimeError("market snapshot failed")
        leg = next(
            leg
            for leg in route["legs"]
            if str(leg.get("side") or "") == side
        )
        market = _focused_probe_market(route, leg, observed_at)
        return market, _FocusedProbeClient(self, route_key, side, market)


class LegacySharedPoolFocusedRecheckTrader(InstrumentedFocusedRecheckTrader):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.outer_start_barrier = threading.Barrier(
            int(self.config.hot_route_recheck_workers)
        )

    def _ensure_focused_io_executor(self, *, workers: int | None = None):
        return self._ensure_focused_route_executor(
            workers=max(2, int(workers or self.config.hot_route_recheck_workers))
        )


class _FocusedProbeClient:
    thread_safe = True

    def __init__(
        self,
        owner: InstrumentedFocusedRecheckTrader,
        route_key: str,
        side: str,
        market: dict,
    ) -> None:
        self.owner = owner
        self.route_key = route_key
        self.side = side
        self.venue = str(market["venue"])
        self.market = market

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict:
        self.owner._record_trace(
            "orderbook_start",
            route_key=self.route_key,
            side=self.side,
        )
        if self.route_key == self.owner.slow_orderbook_route_key:
            time.sleep(self.owner.slow_delay_seconds)
        if self.route_key == self.owner.fail_orderbook_route_key and self.side == "long":
            raise RuntimeError("orderbook failed")
        price = float(self.market.get("mark_price") or 100.0)
        return {
            "venue": self.venue,
            "symbol": symbol,
            "observed_at": observed_at,
            "bids": [[price - 0.02, 100.0]],
            "asks": [[price + 0.02, 100.0]],
            "best_bid": price - 0.02,
            "best_ask": price + 0.02,
            "mid_price": price,
            "response_received_at": observed_at,
            "orderbook_event_time": observed_at,
        }


def _focused_probe_route(now: datetime, index: int) -> dict:
    route = paper_route(now, 90 + index, 90 + index, live_net=5.0)
    route["route_key"] = f"focused-route-{index}"
    route["canonical_asset"] = "BTC"
    route["long_venue"] = "binance"
    route["long_symbol"] = "BTCUSDT"
    route["short_venue"] = "bybit"
    route["short_symbol"] = "BTCUSDT"
    route["legs"][0].update(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_rate": -0.003,
            "normalized_next_funding_rate": -0.003,
            "hourly_funding_rate": -0.003,
            "funding_interval_hours": 1.0,
            "mark_price": 100.0,
            "index_price": 100.0,
        }
    )
    route["legs"][1].update(
        {
            "venue": "bybit",
            "symbol": "BTCUSDT",
            "funding_rate": 0.003,
            "normalized_next_funding_rate": 0.003,
            "hourly_funding_rate": 0.003,
            "funding_interval_hours": 1.0,
            "mark_price": 100.0,
            "index_price": 100.0,
        }
    )
    return route


def _focused_probe_market(route: dict, leg: dict, observed_at: str) -> dict:
    venue = str(leg["venue"])
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
        "symbol": leg["symbol"],
        "canonical_asset": route["canonical_asset"],
        "base_asset": route["canonical_asset"],
        "quote_asset": "USDT",
        "collateral_asset": "USDT",
        "price_quote_currency": "USDT",
        "settlement_collateral": "USDT",
        "funding_rate": leg["funding_rate"],
        "normalized_next_funding_rate": leg["normalized_next_funding_rate"],
        "funding_interval_hours": leg["funding_interval_hours"],
        "hourly_funding_rate": leg["hourly_funding_rate"],
        "funding_rate_kind": "published_next_estimate",
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "next_funding_at": leg["next_funding_at"],
        "mark_price": leg["mark_price"],
        "index_price": leg["index_price"],
        "open_interest_usd": 10_000_000.0,
        "volume_24h_usd": 30_000_000.0,
        "quantity_step": 0.01,
        "min_quantity": 0.01,
        "min_notional": 5.0,
        "min_notional_usd": 5.0,
        "maker_fee_rate": 0.0001,
        "taker_fee_rate": 0.0001,
        "fee_rate": 0.0001,
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
        "contract_type": "linear_perpetual",
        "contract_kind": "linear_perpetual",
        "contract_multiplier": 1.0,
        "canonical_unit_multiplier": 1.0,
        "supports_perpetuals": True,
        "is_linear_contract": True,
        "supports_discrete_funding": True,
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": f"adapter_{venue}_test",
        "source_event_at": observed_at,
        "response_received_at": observed_at,
        "observed_at": observed_at,
    }


def _focused_probe_child(
    db_path: str,
    result_path: str,
    trace_path: str,
    *,
    route_count: int,
    workers: int,
    route_timeout_seconds: float,
    force_legacy_shared_pool: bool = False,
    fail_market_route_key: str | None = None,
    fail_orderbook_route_key: str | None = None,
    slow_market_route_key: str | None = None,
    slow_orderbook_route_key: str | None = None,
    slow_delay_seconds: float = 0.25,
) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(db_path)
    store.init_db()
    trader_cls = (
        LegacySharedPoolFocusedRecheckTrader
        if force_legacy_shared_pool
        else InstrumentedFocusedRecheckTrader
    )
    config_kwargs = {
        "telegram_enabled": False,
        "hot_route_recheck_workers": workers,
    }
    if "focused_recheck_route_timeout_seconds" in PaperBotConfig.__dataclass_fields__:
        config_kwargs["focused_recheck_route_timeout_seconds"] = route_timeout_seconds
    trader = trader_cls(
        store,
        config=PaperBotConfig(**config_kwargs),
        fail_market_route_key=fail_market_route_key,
        fail_orderbook_route_key=fail_orderbook_route_key,
        slow_market_route_key=slow_market_route_key,
        slow_orderbook_route_key=slow_orderbook_route_key,
        slow_delay_seconds=slow_delay_seconds,
        trace_path=trace_path,
    )
    for index in range(route_count):
        route = _focused_probe_route(now, index)
        trader.hot_routes[route["route_key"]] = route
    started = time.perf_counter()
    refreshed = trader.refresh_hot_routes()
    elapsed = time.perf_counter() - started
    with store.connect() as connection:
        statuses = [
            dict(row)
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM funding_scans GROUP BY status"
            )
        ]
        running_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM funding_scans WHERE status = 'running'"
            ).fetchone()[0]
        )
    trader.shutdown_foreground_executors()
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "elapsed_seconds": elapsed,
                "refreshed_count": len(refreshed),
                "max_outer_rechecks": trader.max_outer_rechecks,
                "trace": trader.trace,
                "scan_statuses": statuses,
                "running_scan_count": running_count,
            },
            handle,
            sort_keys=True,
        )


def _run_focused_probe_child(tmp_path, **kwargs) -> dict:
    db_path = tmp_path / "probe.sqlite"
    result_path = tmp_path / "probe-result.json"
    trace_path = tmp_path / "probe-trace.jsonl"
    process_timeout_seconds = float(kwargs.pop("process_timeout_seconds", 3.0))
    ctx = multiprocessing.get_context("fork")
    process = ctx.Process(
        target=_focused_probe_child,
        kwargs={
            "db_path": str(db_path),
            "result_path": str(result_path),
            "trace_path": str(trace_path),
            **kwargs,
        },
    )
    process.start()
    process.join(process_timeout_seconds)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(2.0)
        if process.is_alive():
            process.kill()
            process.join(2.0)
    payload = {}
    if result_path.exists():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    if trace_path.exists():
        payload["trace"] = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if db_path.exists():
        for _attempt in range(5):
            try:
                with sqlite3.connect(db_path, timeout=2.0) as connection:
                    connection.row_factory = sqlite3.Row
                    payload["running_scan_count"] = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM funding_scans WHERE status = 'running'"
                        ).fetchone()[0]
                    )
                    payload["scan_statuses"] = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT status, COUNT(*) AS count FROM funding_scans GROUP BY status"
                        )
                    ]
                break
            except sqlite3.OperationalError:
                time.sleep(0.05)
    payload["timed_out"] = timed_out
    payload["exitcode"] = process.exitcode
    return payload


def test_focused_hot_recheck_6_routes_6_workers_completes_without_deadlock(tmp_path) -> None:
    result = _run_focused_probe_child(
        tmp_path,
        route_count=6,
        workers=6,
        route_timeout_seconds=1.0,
        process_timeout_seconds=10.0,
    )

    assert result["timed_out"] is False
    assert result["running_scan_count"] == 0
    assert result["max_outer_rechecks"] == 6
    market_threads = {
        row["thread"] for row in result["trace"] if row["event"] == "market_start"
    }
    orderbook_threads = {
        row["thread"] for row in result["trace"] if row["event"] == "orderbook_start"
    }
    assert len([row for row in result["trace"] if row["event"] == "market_start"]) == 12
    assert len([row for row in result["trace"] if row["event"] == "orderbook_start"]) == 12
    assert market_threads
    assert orderbook_threads
    assert all("funding-focused-io" in name for name in market_threads)
    assert all("funding-focused-io" in name for name in orderbook_threads)


def test_focused_hot_recheck_20_routes_6_workers_completes_bounded_batches(tmp_path) -> None:
    result = _run_focused_probe_child(
        tmp_path,
        route_count=20,
        workers=6,
        route_timeout_seconds=1.0,
        process_timeout_seconds=15.0,
    )

    assert result["timed_out"] is False
    assert result["running_scan_count"] == 0
    assert result["max_outer_rechecks"] <= 6
    assert result["scan_statuses"] == [{"status": "success", "count": 20}]
    assert len([row for row in result["trace"] if row["event"] == "market_start"]) == 40
    assert len([row for row in result["trace"] if row["event"] == "orderbook_start"]) == 40


def test_legacy_shared_pool_focused_recheck_design_exhausts_route_timeout(tmp_path) -> None:
    result = _run_focused_probe_child(
        tmp_path,
        route_count=6,
        workers=6,
        route_timeout_seconds=1.0,
        force_legacy_shared_pool=True,
        process_timeout_seconds=10.0,
    )

    trace = result["trace"]
    assert result["timed_out"] is False
    assert result["running_scan_count"] == 0
    scan_counts = {
        row["status"]: row["count"]
        for row in result["scan_statuses"]
    }
    assert sum(scan_counts.values()) == 6
    assert scan_counts.get("failed", 0) >= 5
    assert len([row for row in trace if row["event"] == "outer_start"]) == 6
    market_starts = [row for row in trace if row["event"] == "market_start"]
    orderbook_starts = [row for row in trace if row["event"] == "orderbook_start"]
    assert all("funding-focused-route" in row["thread"] for row in market_starts)
    assert all("funding-focused-route" in row["thread"] for row in orderbook_starts)


def test_market_snapshot_failure_does_not_block_other_focused_routes(tmp_path) -> None:
    result = _run_focused_probe_child(
        tmp_path,
        route_count=6,
        workers=6,
        route_timeout_seconds=1.0,
        fail_market_route_key="focused-route-0",
        process_timeout_seconds=10.0,
    )

    assert result["timed_out"] is False
    assert result["running_scan_count"] == 0
    assert sorted(result["scan_statuses"], key=lambda row: row["status"]) == [
        {"status": "failed", "count": 1},
        {"status": "success", "count": 5},
    ]


def test_orderbook_failure_does_not_block_other_focused_routes(tmp_path) -> None:
    result = _run_focused_probe_child(
        tmp_path,
        route_count=6,
        workers=6,
        route_timeout_seconds=1.0,
        fail_orderbook_route_key="focused-route-0",
        process_timeout_seconds=10.0,
    )

    assert result["timed_out"] is False
    assert result["running_scan_count"] == 0
    assert sorted(result["scan_statuses"], key=lambda row: row["status"]) == [
        {"status": "failed", "count": 1},
        {"status": "success", "count": 5},
    ]


def test_focused_route_timeout_is_terminal_and_does_not_reach_execution(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = InstrumentedFocusedRecheckTrader(
        store,
        config=PaperBotConfig(
            telegram_enabled=False,
            hot_route_recheck_workers=1,
            focused_recheck_route_timeout_seconds=0.2,
            export_dir=tmp_path / "exports-timeout",
        ),
        slow_market_route_key="focused-route-0",
        slow_delay_seconds=0.6,
    )
    route = _focused_probe_route(now, 0)
    trader.hot_routes[route["route_key"]] = route
    entry_routes_seen: list[dict] = []

    def fake_process_entry_candidates(routes, *args, **kwargs):
        entry_routes_seen.extend(routes)
        return []

    trader.process_entry_candidates = fake_process_entry_candidates  # type: ignore[method-assign]

    result = trader.run_hot_iteration()
    time.sleep(0.3)
    trader.shutdown_foreground_executors()

    assert result["opened_count"] == 0
    assert entry_routes_seen == []
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM funding_scans WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
        failed = connection.execute(
            "SELECT error FROM funding_scans WHERE status = 'failed'"
        ).fetchone()
    assert failed is not None
    assert "focused_recheck_timeout" in failed["error"]


def test_focused_executor_shutdown_and_next_iteration_continue_after_timeout(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = InstrumentedFocusedRecheckTrader(
        store,
        config=PaperBotConfig(
            telegram_enabled=False,
            hot_route_recheck_workers=1,
            focused_recheck_route_timeout_seconds=0.2,
            export_dir=tmp_path / "exports-retry",
        ),
        slow_market_route_key="focused-route-0",
        slow_delay_seconds=0.6,
    )
    first = _focused_probe_route(now, 0)
    trader.hot_routes[first["route_key"]] = first

    assert trader.refresh_hot_routes() == []
    shutdown_started = time.perf_counter()
    trader.shutdown_foreground_executors()
    assert time.perf_counter() - shutdown_started < 0.5

    trader.slow_market_route_key = None
    second = _focused_probe_route(now, 1)
    trader.hot_routes[second["route_key"]] = second
    refreshed = trader.refresh_hot_routes()
    trader.shutdown_foreground_executors()

    assert len(refreshed) == 1
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM funding_scans WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )


def test_orphaned_focused_running_scan_recovery_is_idempotent(tmp_path) -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    old_started = (now - timedelta(minutes=10)).isoformat()
    fresh_started = (now - timedelta(seconds=10)).isoformat()
    current_run_id = "current-run"
    current_pid = str(os.getpid())
    with store.connect() as connection:
        orphan_id = connection.execute(
            """
            INSERT INTO funding_scans (status, started_at, config_json)
            VALUES ('running', ?, ?)
            """,
            (
                old_started,
                json.dumps(
                    {
                        "scan_mode": "watch",
                        "focused_route_key": "orphan-route",
                    },
                    sort_keys=True,
                ),
            ),
        ).lastrowid
        current_id = connection.execute(
            """
            INSERT INTO funding_scans (status, started_at, config_json)
            VALUES ('running', ?, ?)
            """,
            (
                old_started,
                json.dumps(
                    {
                        "scan_mode": "watch",
                        "focused_route_key": "current-route",
                        "focused_run_id": current_run_id,
                        "focused_process_id": current_pid,
                    },
                    sort_keys=True,
                ),
            ),
        ).lastrowid
        fresh_id = connection.execute(
            """
            INSERT INTO funding_scans (status, started_at, config_json)
            VALUES ('running', ?, ?)
            """,
            (
                fresh_started,
                json.dumps(
                    {
                        "scan_mode": "watch",
                        "focused_route_key": "fresh-route",
                    },
                    sort_keys=True,
                ),
            ),
        ).lastrowid

    first = store.recover_orphaned_focused_running_scans(
        current_run_id=current_run_id,
        current_process_id=current_pid,
        older_than_seconds=60,
        now=now,
    )
    second = store.recover_orphaned_focused_running_scans(
        current_run_id=current_run_id,
        current_process_id=current_pid,
        older_than_seconds=60,
        now=now,
    )

    assert first["recovered_scan_ids"] == [orphan_id]
    assert first["recovered_count"] == 1
    assert second["recovered_count"] == 0
    with store.connect() as connection:
        statuses = {
            row["funding_scan_id"]: row["status"]
            for row in connection.execute(
                "SELECT funding_scan_id, status FROM funding_scans"
            )
        }
        warning = connection.execute(
            """
            SELECT warning
            FROM funding_scan_warnings
            WHERE funding_scan_id = ?
            """,
            (orphan_id,),
        ).fetchone()
    assert statuses[orphan_id] == "failed"
    assert statuses[current_id] == "running"
    assert statuses[fresh_id] == "running"
    assert warning["warning"] == "orphaned_by_process_restart"


class SlowBackgroundScanTrader(PaperBot):
    def __init__(self, *args, background_delay: float = 0.35, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.background_delay = background_delay
        self.background_started = threading.Event()
        self.background_finished = threading.Event()
        self.hot_iterations = 0
        self.last_cancel_event = None

    def run_hot_iteration(self) -> dict:
        self.hot_iterations += 1
        return {
            "mode": "hot_routes",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": len(self.hot_routes),
            "opened_count": 0,
            "closed_count": 0,
            "repriced_count": 0,
            "pending_count": 0,
            "held_count": 0,
            "open_position_count": 0,
            "hot_route_count": len(self.hot_routes),
            "urgent_route_count": 0,
        }

    def _run_lightweight_discovery(self) -> dict | None:
        return None

    def run_discovery_full_scan(self, cancel_event=None) -> dict:
        self.last_cancel_event = cancel_event
        self.background_started.set()
        try:
            time.sleep(self.background_delay)
            return {
                "mode": "background_full_market",
                "funding_scan_id": 99,
                "candidate_count": 0,
                "watch_count": 0,
                "opened_count": 0,
                "closed_count": 0,
                "repriced_count": 0,
                "pending_count": 0,
                "held_count": 0,
                "open_position_count": 0,
                "hot_route_count": 0,
                "urgent_route_count": 0,
                "completed_monotonic": time.monotonic(),
                "_routes": [],
                "_watch_routes": [],
            }
        finally:
            self.background_finished.set()


class OpenPriorityTrader(PaperBot):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.open_position_calls = 0
        self.lightweight_called = False
        self.hot_iteration_called = False

    def process_open_positions(self) -> list[str]:
        self.open_position_calls += 1
        return []

    def _run_lightweight_discovery(self) -> dict | None:
        self.lightweight_called = True
        raise AssertionError("lightweight discovery must not run with open exposure")

    def run_hot_iteration(self) -> dict:
        self.hot_iteration_called = True
        raise AssertionError("hot iteration must not run before open exposure")


class IdleSchedulerTrader(PaperBot):
    def _run_lightweight_discovery(self) -> dict | None:
        return None


class NotDoneFuture:
    result_called = False

    def done(self) -> bool:
        return False

    def result(self):
        self.result_called = True
        raise AssertionError("future.result() must not be called until done()")


class LightweightFundingClient:
    def __init__(
        self,
        venue: str,
        funding_rate: float,
        next_funding_at: datetime,
    ) -> None:
        self.venue = venue
        self.funding_rate = funding_rate
        self.next_funding_at = next_funding_at
        self.orderbook_calls = 0
        self.history_calls = 0
        self.catalog_calls = 0

    def catalog_and_markets(self, observed_at: str):
        self.catalog_calls += 1
        instrument = {
            "venue": self.venue,
            "environment": "mainnet",
            "environment_verified": True,
            "endpoint_base_url": f"https://api.{self.venue}.test",
            "endpoint_identity_provenance": f"test-fixture:{self.venue}:endpoint:v1",
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
            "symbol": "ABCUSDT",
            "canonical_asset": "ABC",
            "base_asset": "ABC",
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "price_quote_currency": "USDT",
            "settlement_collateral": "USDT",
            "environment": "mainnet",
            "environment_verified": True,
            "contract_type": "linear_perpetual",
            "contract_kind": "linear_perpetual",
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "contract_multiplier": 0.01,
            "status": "active",
            "observed_at": observed_at,
            "source_event_at": observed_at,
            "response_received_at": observed_at,
        }
        market = {
            "venue": self.venue,
            "environment": "mainnet",
            "environment_verified": True,
            "endpoint_base_url": f"https://api.{self.venue}.test",
            "endpoint_identity_provenance": f"test-fixture:{self.venue}:endpoint:v1",
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
            "symbol": "ABCUSDT",
            "canonical_asset": "ABC",
            "funding_rate": self.funding_rate,
            "normalized_next_funding_rate": self.funding_rate,
            "funding_rate_semantics": "next_settlement",
            "funding_rate_unit": "fraction_of_notional_per_settlement",
            "funding_sign_convention": "positive_long_pays",
            "funding_interval_hours": 1.0,
            "hourly_funding_rate": self.funding_rate,
            "funding_rate_kind": "published_next_estimate",
            "next_funding_at": self.next_funding_at.isoformat(),
            "mark_price": 100.0,
            "index_price": 100.0,
            "open_interest_usd": 10_000_000.0,
            "volume_24h_usd": 30_000_000.0,
            "quantity_step": 0.01,
            "min_notional_usd": 5.0,
            "taker_fee_rate": 0.0005,
            "fee_source": "configured_trusted_fee",
            "fee_evidence": {
                "source_kind": "configured_trusted_fee",
                "source_identifier": f"test-fixture:{self.venue}:fees:v1",
                "trust_status": "CONFIGURED_TRUSTED",
                "venue": self.venue,
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
            "contract_kind": "linear_perpetual",
            "supports_perpetuals": True,
            "is_linear_contract": True,
            "supports_discrete_funding": True,
            "collateral_asset": "USDT",
            "quote_asset": "USDT",
            "price_quote_currency": "USDT",
            "settlement_collateral": "USDT",
            "environment": "mainnet",
            "environment_verified": True,
            "position_inclusion_rule": "perp_position_at_settlement",
            "entry_safety_buffer_seconds": 20,
            "exit_safety_buffer_seconds": 20,
            "timing_policy_source": f"adapter_{self.venue}_test",
            "source_event_at": observed_at,
            "response_received_at": observed_at,
            "observed_at": observed_at,
        }
        return [instrument], [market], []

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100):
        self.orderbook_calls += 1
        raise AssertionError("background discovery must not fetch orderbooks")

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ):
        self.history_calls += 1
        raise AssertionError("background discovery must not fetch history")


def test_filtered_summary_preserves_original_pnl() -> None:
    """Summary keeps realized_pnl and trade stats from the unfiltered source.

    Deactivated venues are hidden from position/account lists, but the
    aggregate PnL must reflect ALL historical trades — deactivation stops
    future entries, it does not erase past results.
    """
    from smart_money_radar.funding.presentation import filtered_funding_paper_summary

    original = {
        "realized_pnl": 46.26,
        "closed_trade_count": 13,
        "win_rate": 1.0,
        "starting_capital": 27000,
        "total_cash": 27046.26,
    }
    accounts = [
        {"venue": "binance", "starting_balance": 1000, "cash_balance": 986.34,
         "reserved_margin": 0, "realized_pnl": -13.66},
        {"venue": "bybit", "starting_balance": 1000, "cash_balance": 1003.50,
         "reserved_margin": 0, "realized_pnl": 3.50},
    ]
    closed_positions = [
        {"actual_net_pnl": 2.25, "long_venue": "bybit", "short_venue": "aster"},
        {"actual_net_pnl": 1.43, "long_venue": "mexc", "short_venue": "bybit"},
        {"actual_net_pnl": 0.31, "long_venue": "aster", "short_venue": "kraken"},
    ]
    summary = filtered_funding_paper_summary(original, accounts, [], closed_positions)
    assert abs(summary["realized_pnl"] - 46.26) < 0.01
    assert summary["closed_trade_count"] == 13
    assert summary["win_rate"] == 1.0
    assert summary["open_position_count"] == 0


def test_filtered_summary_empty_positions_keeps_original() -> None:
    from smart_money_radar.funding.presentation import filtered_funding_paper_summary

    original = {
        "realized_pnl": 46.26,
        "closed_trade_count": 13,
        "win_rate": 1.0,
    }
    accounts = [
        {"venue": "binance", "starting_balance": 1000, "cash_balance": 986.34,
         "reserved_margin": 0, "realized_pnl": -13.66},
    ]
    summary = filtered_funding_paper_summary(original, accounts, [], [])
    assert summary["realized_pnl"] == 46.26
    assert summary["closed_trade_count"] == 13
    assert summary["open_position_count"] == 0


# ---------------------------------------------------------------------------
# Spread / basis risk tracking
# ---------------------------------------------------------------------------


def test_leg_vwap_prefers_evidence_vwap() -> None:
    leg = {"evidence": {"vwap": 100.0, "mark_price": 99.0}, "mid_price": 98.0}
    assert leg_vwap(leg) == 100.0


def test_leg_vwap_falls_back_to_leg_price() -> None:
    leg = {"mid_price": 42.0}
    assert leg_vwap(leg) == 42.0


def test_leg_vwap_returns_none_when_no_price() -> None:
    assert leg_vwap({}) is None
    assert leg_vwap({"evidence": {"vwap": 0}}) is None


def test_entry_cross_spread_computes_long_minus_short() -> None:
    long_leg = {"evidence": {"vwap": 100.5}}
    short_leg = {"evidence": {"vwap": 100.2}}
    assert entry_cross_spread(long_leg, short_leg) == 100.5 - 100.2


def test_entry_cross_spread_returns_none_when_price_missing() -> None:
    assert entry_cross_spread({}, {"evidence": {"vwap": 100.0}}) is None


def test_compute_spread_snapshot_tracks_basis_pnl() -> None:
    position = {
        "entry_cross_spread": 0.3,
        "base_quantity": 2.0,
        "target_notional": 500.0,
        "entry_basis_bps": -5.0,
        "expected_execution_cost": 1.0,
        "notes": {"accrued_funding_pnl": 0.5},
    }
    route = {
        "legs": [
            {"side": "long", "evidence": {"vwap": 101.0}},
            {"side": "short", "evidence": {"vwap": 100.5}},
        ],
    }
    snap = compute_spread_snapshot(position, route)
    assert snap["spread_tracking"] is True
    assert snap["current_cross_spread"] == 101.0 - 100.5
    assert abs(snap["unrealized_basis_pnl"] - (0.5 - 0.3) * 2.0) < 1e-9
    assert snap["entry_basis_bps"] == -5.0
    assert abs(snap["total_unrealized_pnl"] - (0.5 + 0.4 - 1.0)) < 1e-9


def test_compute_spread_snapshot_returns_false_without_route() -> None:
    position = {"entry_cross_spread": 0.3}
    snap = compute_spread_snapshot(position, None)
    assert snap["spread_tracking"] is False


def test_compute_spread_snapshot_returns_false_without_entry_spread() -> None:
    route = {"legs": [{"side": "long", "evidence": {"vwap": 100.0}}]}
    snap = compute_spread_snapshot({"entry_cross_spread": None}, route)
    assert snap["spread_tracking"] is False


def test_spread_stop_loss_triggers_above_threshold() -> None:
    config = PaperBotConfig(basis_stop_loss_bps=200.0).validated()
    snapshot = {
        "spread_tracking": True,
        "notional": 500.0,
        "unrealized_basis_pnl": -15.0,
    }
    triggered, reason = spread_stop_loss_triggered(snapshot, config)
    assert triggered
    assert "basis_stop_loss" in reason
    assert "300 bps" in reason


def test_spread_stop_loss_does_not_trigger_below_threshold() -> None:
    config = PaperBotConfig(basis_stop_loss_bps=200.0).validated()
    snapshot = {
        "spread_tracking": True,
        "notional": 500.0,
        "unrealized_basis_pnl": -5.0,
    }
    triggered, _ = spread_stop_loss_triggered(snapshot, config)
    assert not triggered


def test_spread_stop_loss_ignores_positive_pnl() -> None:
    config = PaperBotConfig(basis_stop_loss_bps=200.0).validated()
    snapshot = {
        "spread_tracking": True,
        "notional": 500.0,
        "unrealized_basis_pnl": 10.0,
    }
    triggered, _ = spread_stop_loss_triggered(snapshot, config)
    assert not triggered


def test_spread_stop_loss_ignores_untracked_snapshot() -> None:
    config = PaperBotConfig(basis_stop_loss_bps=200.0).validated()
    triggered, _ = spread_stop_loss_triggered({"spread_tracking": False}, config)
    assert not triggered


def test_price_move_snapshot_tracks_both_legs_from_entry() -> None:
    position = {
        "entry_legs": [
            {"side": "long", "evidence": {"vwap": 10.0}},
            {"side": "short", "evidence": {"vwap": 10.0}},
        ],
    }
    route = {
        "legs": [
            {"side": "long", "evidence": {"vwap": 11.0}},
            {"side": "short", "evidence": {"vwap": 10.5}},
        ],
    }

    snapshot = compute_price_move_snapshot(position, route)

    assert snapshot["price_move_tracking"] is True
    assert snapshot["long_move_fraction"] == pytest.approx(0.10)
    assert snapshot["short_move_fraction"] == pytest.approx(0.05)
    assert snapshot["max_abs_move_fraction"] == pytest.approx(0.10)
    assert snapshot["max_move_side"] == "long"


def test_common_price_move_critical_is_telemetry_not_stop() -> None:
    config = PaperBotConfig(
        common_price_move_alert_fraction=0.05,
        common_price_move_critical_fraction=0.10,
    ).validated()
    snapshot = {
        "price_move_tracking": True,
        "long_move_fraction": 0.10,
        "short_move_fraction": 0.095,
        "max_abs_move_fraction": 0.10,
        "max_move_side": "long",
    }

    telemetry = common_price_move_telemetry(
        snapshot,
        alert_fraction=config.common_price_move_alert_fraction,
        critical_fraction=config.common_price_move_critical_fraction,
    )
    triggered, reason = price_stop_loss_triggered(snapshot, config)

    assert telemetry["level"] == "critical"
    assert telemetry["requires_fresh_risk_recalculation"]
    assert not triggered
    assert reason == "common_price_move_is_telemetry_not_stop"


def test_common_price_move_below_alert_threshold_is_quiet() -> None:
    config = PaperBotConfig(
        common_price_move_alert_fraction=0.05,
        common_price_move_critical_fraction=0.10,
    ).validated()
    snapshot = {
        "price_move_tracking": True,
        "long_move_fraction": 0.049,
        "short_move_fraction": 0.045,
        "max_abs_move_fraction": 0.049,
        "max_move_side": "long",
    }

    telemetry = common_price_move_telemetry(
        snapshot,
        alert_fraction=config.common_price_move_alert_fraction,
        critical_fraction=config.common_price_move_critical_fraction,
    )
    triggered, _ = price_stop_loss_triggered(snapshot, config)

    assert telemetry["level"] == "none"
    assert not triggered


def test_common_price_move_ignores_untracked_snapshot() -> None:
    config = PaperBotConfig().validated()
    telemetry = common_price_move_telemetry({"price_move_tracking": False})
    triggered, _ = price_stop_loss_triggered({"price_move_tracking": False}, config)
    assert telemetry["level"] == "none"
    assert not triggered


def test_price_stop_loss_close_payload_has_both_legs_in_one_close() -> None:
    position = {
        "long_venue": "aster",
        "long_symbol": "ABCUSDT",
        "short_venue": "binance",
        "short_symbol": "ABCUSDT",
        "long_notional": 500.0,
        "short_notional": 500.0,
        "base_quantity": 50.0,
        "expected_execution_cost": 1.0,
        "entry_cross_spread": 0.0,
        "entry_legs": [
            {
                "side": "long",
                "venue": "aster",
                "symbol": "ABCUSDT",
                "notional": 500.0,
                "evidence": {"vwap": 10.0, "funding_rate": -0.001},
            },
            {
                "side": "short",
                "venue": "binance",
                "symbol": "ABCUSDT",
                "notional": 500.0,
                "evidence": {"vwap": 10.0, "funding_rate": 0.001},
            },
        ],
        "notes": {},
    }
    close_route = {
        "legs": [
            {
                "side": "long",
                "venue": "aster",
                "symbol": "ABCUSDT",
                "evidence": {"vwap": 11.0},
            },
            {
                "side": "short",
                "venue": "binance",
                "symbol": "ABCUSDT",
                "evidence": {"vwap": 11.0},
            },
        ]
    }

    payload = build_close_payload(
        position,
        {"long": None, "short": None},
        close_route,
        use_entry_estimate_for_missing=True,
        close_reason="price_stop_loss:test",
    )

    assert payload["close_reason"].startswith("price_stop_loss")
    assert len(payload["close_legs"]) == 2
    assert "long_cash_delta" in payload
    assert "short_cash_delta" in payload
    assert payload["notes"]["basis_pnl_included"] is True


def test_process_open_positions_common_price_move_alert_does_not_close(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    now = datetime.now(UTC)
    store.open_funding_paper_position(
        {
            "entry_key": "route-1:price-stop",
            "route_key": "route-1",
            "canonical_asset": "ABC",
            "long_venue": "aster",
            "long_symbol": "ABCUSDT",
            "short_venue": "binance",
            "short_symbol": "ABCUSDT",
            "base_quantity": 50.0,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": (now + timedelta(hours=1)).isoformat(),
            "short_settlement_at": (now + timedelta(hours=1)).isoformat(),
            "max_settlement_at": (now + timedelta(hours=1)).isoformat(),
            "expected_live_gross": 2.0,
            "expected_live_net": 1.0,
            "expected_execution_cost": 1.0,
            "entry_cross_spread": 0.0,
            "entry_legs": [
                {
                    "side": "long",
                    "venue": "aster",
                    "symbol": "ABCUSDT",
                    "notional": 500.0,
                    "evidence": {"vwap": 10.0, "funding_rate": 0.0},
                },
                {
                    "side": "short",
                    "venue": "binance",
                    "symbol": "ABCUSDT",
                    "notional": 500.0,
                    "evidence": {"vwap": 10.0, "funding_rate": 0.0},
                },
            ],
            "entry_evidence": {},
        }
    )
    live_route = paper_route(now, 3_600, 3_600, live_net=2.5)
    live_route["funding_scan_id"] = None
    live_route["funding_route_id"] = None
    live_route["route_key"] = "route-1"
    live_route["canonical_asset"] = "ABC"
    live_route["long_symbol"] = "ABCUSDT"
    live_route["short_symbol"] = "ABCUSDT"
    live_route["legs"][0]["symbol"] = "ABCUSDT"
    live_route["legs"][0]["vwap"] = 11.0
    live_route["legs"][1]["symbol"] = "ABCUSDT"
    live_route["legs"][1]["vwap"] = 11.0
    trader = PaperBot(
        store,
        config=PaperBotConfig(
            focused_recheck_enabled=False,
            telegram_enabled=False,
            strategy_set=("funding_only",),
        ),
        notifier=FakeNotifier(),
    )
    trader.hot_routes["route-1"] = live_route

    outcomes = trader.process_open_positions()

    assert outcomes == []
    assert len(store.funding_paper_open_positions()) == 1
    events = store.funding_paper_dashboard()["events"]
    assert any(row["event_type"] == "price_move_alert" for row in events)


def test_hold_decision_detects_funding_rate_inversion() -> None:
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    config = PaperBotConfig(
        max_entry_snapshot_age_seconds=300,
    ).validated()
    position = {
        "route_key": "route-1",
        "max_settlement_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 0.5,
        "notes": {},
    }
    route = paper_route(now, 3_600, 3_600, live_net=2.5)
    route["legs"][0]["hourly_funding_rate"] = 0.001
    route["legs"][1]["hourly_funding_rate"] = 0.0005

    decision = position_hold_decision(position, route, now, config)
    assert not decision["hold"]
    assert "funding_rate_inverted" in decision["reasons"]
    assert decision["close_reason"] == "arbitrage_window_funding_rate_inverted"


def test_boundary_decision_requires_exit_even_when_next_rate_order_is_normal() -> None:
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    config = PaperBotConfig(
        max_entry_snapshot_age_seconds=300,
    ).validated()
    position = {
        "route_key": "route-1",
        "max_settlement_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 0.5,
        "notes": {},
    }
    route = paper_route(now, 3_600, 3_600, live_net=2.5)
    route["legs"][0]["hourly_funding_rate"] = -0.001
    route["legs"][1]["hourly_funding_rate"] = 0.001

    decision = position_hold_decision(position, route, now, config)
    assert not decision["hold"]
    assert decision["close_reason"] == "mandatory_exit_after_first_settlement"
    assert "funding_rate_inverted" not in decision["reasons"]
    assert "mandatory_exit_after_first_settlement" in decision["reasons"]


def test_build_position_includes_spread_fields() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now, 60, 45, live_net=1.0)
    route["legs"][0]["evidence"] = {"vwap": 100.5}
    route["legs"][1]["evidence"] = {"vwap": 100.2}
    route["evidence"]["signed_entry_basis"] = -0.003
    config = PaperBotConfig().validated()
    decision = route_entry_decision(route, accounts(), now, config)

    position = build_position_from_route(route, decision, config)
    assert abs(position["entry_cross_spread"] - 0.3) < 1e-9
    assert abs(position["entry_basis_bps"] - (-30.0)) < 1e-9
    assert position["notes"]["paper_model"] == "synchronized_funding_capture_v2"


def test_build_close_payload_includes_basis_pnl() -> None:
    position = {
        "entry_cross_spread": 0.3,
        "entry_basis_bps": -30.0,
        "base_quantity": 2.0,
        "target_notional": 500.0,
        "long_notional": 500.0,
        "short_notional": 500.0,
        "expected_execution_cost": 1.0,
        "long_venue": "aster",
        "long_symbol": "BTCUSDT",
        "short_venue": "binance",
        "short_symbol": "BTCUSDT",
        "long_settlement_at": "2026-07-19T12:00:00+00:00",
        "short_settlement_at": "2026-07-19T12:00:00+00:00",
        "entry_legs": [
            {"side": "long", "venue": "aster", "symbol": "BTCUSDT",
             "funding_rate": -0.001, "notional": 500.0, "base_quantity": 2.0,
             "next_funding_at": "2026-07-19T12:00:00+00:00",
             "evidence": {"vwap": 100.5}},
            {"side": "short", "venue": "binance", "symbol": "BTCUSDT",
             "funding_rate": 0.001, "notional": 500.0, "base_quantity": 2.0,
             "next_funding_at": "2026-07-19T12:00:00+00:00",
             "evidence": {"vwap": 100.2}},
        ],
        "notes": {"accrued_funding_pnl": 0.0},
    }
    close_route = {
        "funding_scan_id": 10,
        "funding_route_id": 20,
        "legs": [
            {"side": "long", "evidence": {"vwap": 101.0}},
            {"side": "short", "evidence": {"vwap": 100.5}},
        ],
        "evidence": {},
    }
    close = build_close_payload(
        position,
        {"long": None, "short": None},
        close_route,
        use_entry_estimate_for_missing=True,
        close_reason="test",
    )
    assert "actual_basis_pnl" in close
    assert abs(close["actual_basis_pnl"] - 0.4) < 1e-9
    assert abs(close["actual_net_pnl"] - 0.4) < 1e-9
    assert abs(close["long_cash_delta"] - 1.0) < 1e-9
    assert abs(close["short_cash_delta"] - (-0.6)) < 1e-9
    assert (
        abs(
            close["long_cash_delta"]
            + close["short_cash_delta"]
            - close["actual_net_pnl"]
        )
        < 1e-9
    )
    assert close["spread_snapshot"]["spread_tracking"] is True
    assert abs(close["spread_snapshot"]["long_basis_pnl"] - 1.0) < 1e-9
    assert abs(close["spread_snapshot"]["short_basis_pnl"] - (-0.6)) < 1e-9
    assert close["notes"]["paper_model"] == "funding_paper_trader_v2"
    assert close["notes"]["basis_pnl_included"] is True


def test_funding_client_for_venue_covers_all_active_venues() -> None:
    active_venues = ["hyperliquid", "risex"]
    for venue in active_venues:
        client = funding_client_for_venue(venue)
        assert client is not None, f"Missing client for {venue}"
        assert client.venue == venue
        assert callable(getattr(client, "market_snapshot", None)), (
            f"Missing focused market_snapshot for {venue}"
        )


def test_funding_client_for_venue_rejects_deactivated() -> None:
    for venue in DEACTIVATED_FUNDING_VENUES:
        assert funding_client_for_venue(venue) is None


def test_funding_client_factory_returns_verified_identity_for_active_venues() -> None:
    for venue, environment in (
        ("hyperliquid", "mainnet"),
        ("risex", "mainnet"),
    ):
        client = funding_client_for_venue(venue)
        assert client is not None
        assert client.venue == venue
        assert client.endpoint_identity.environment == environment
        assert client.endpoint_identity.environment_verified is True


def test_position_spread_fields_round_trip_through_db(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["aster", "binance"], 1_000.0)
    position_id = store.open_funding_paper_position(
        {
            "entry_key": "spread-rt",
            "route_key": "spread-rt",
            "canonical_asset": "BTC",
            "long_venue": "aster",
            "long_symbol": "BTCUSDT",
            "short_venue": "binance",
            "short_symbol": "BTCUSDT",
            "base_quantity": 0.01,
            "target_notional": 500.0,
            "long_notional": 500.0,
            "short_notional": 500.0,
            "long_reserved_margin": 550.0,
            "short_reserved_margin": 550.0,
            "long_settlement_at": "2026-07-19T12:00:00+00:00",
            "short_settlement_at": "2026-07-19T12:00:00+00:00",
            "max_settlement_at": "2026-07-19T12:00:00+00:00",
            "expected_live_gross": 2.0,
            "expected_live_net": 1.0,
            "expected_execution_cost": 0.5,
            "entry_cross_spread": 0.42,
            "entry_basis_bps": -35.0,
            "entry_legs": [],
            "entry_evidence": {},
        }
    )
    positions = store.funding_paper_open_positions()
    assert len(positions) == 1
    assert positions[0]["entry_cross_spread"] == 0.42
    assert positions[0]["entry_basis_bps"] == -35.0

    store.close_funding_paper_position(
        position_id,
        {
            "actual_funding_pnl": 3.0,
            "actual_basis_pnl": -1.5,
            "actual_execution_cost": 0.5,
            "actual_net_pnl": 1.0,
            "long_cash_delta": 0.5,
            "short_cash_delta": 0.5,
            "close_reason": "test",
            "settlement": {},
        },
    )
    dashboard = store.funding_paper_dashboard(refresh_estimates=False)
    closed = dashboard["closed_positions"][0]
    assert closed["entry_cross_spread"] == 0.42
    assert closed["entry_basis_bps"] == -35.0
    assert closed["actual_basis_pnl"] == -1.5


# --- Startup script default tests ---

_STARTUP_SCRIPTS = ("start_funding_bot.sh", "run_funding_bot_launchd.sh")


def _read_startup_script(name: str) -> str:
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
    )
    with open(os.path.join(scripts_dir, name)) as fh:
        return fh.read()


@pytest.mark.parametrize("script_name", _STARTUP_SCRIPTS)
def test_startup_default_entry_snapshot_age_is_20(script_name: str) -> None:
    content = _read_startup_script(script_name)
    assert "FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-20" in content, (
        f"{script_name}: expected default snapshot age 20"
    )


@pytest.mark.parametrize("script_name", _STARTUP_SCRIPTS)
def test_startup_no_stale_snapshot_age_default_5(script_name: str) -> None:
    content = _read_startup_script(script_name)
    assert "FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-5" not in content, (
        f"{script_name}: still contains stale snapshot age default :-5"
    )


@pytest.mark.parametrize("script_name", _STARTUP_SCRIPTS)
def test_startup_default_focused_recheck_route_timeout_is_20(
    script_name: str,
) -> None:
    content = _read_startup_script(script_name)
    assert (
        "FUNDING_PAPER_FOCUSED_RECHECK_ROUTE_TIMEOUT_SECONDS:-20" in content
    ), f"{script_name}: expected default focused recheck route timeout 20"


@pytest.mark.parametrize("script_name", _STARTUP_SCRIPTS)
def test_startup_no_stale_focused_recheck_timeout_8(script_name: str) -> None:
    content = _read_startup_script(script_name)
    assert "FUNDING_PAPER_FOCUSED_RECHECK_ROUTE_TIMEOUT_SECONDS:-8" not in content, (
        f"{script_name}: still contains stale focused recheck route timeout :-8"
    )
