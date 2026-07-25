from __future__ import annotations

import threading
import time
import sqlite3
from datetime import UTC, datetime, timedelta

from smart_money_radar.funding.trader import (
    FundingPaperTrader,
    FundingPaperTraderConfig,
    build_close_payload,
    build_position_from_route,
    close_decision,
    close_message,
    compute_spread_snapshot,
    entry_cross_spread,
    final_recheck_fallback_route,
    funding_client_for_venue,
    funding_leg_pnl,
    leg_vwap,
    position_hold_decision,
    route_entry_decision,
    route_monitor_decision,
    settlement_rate_or_entry,
    spread_stop_loss_triggered,
)
from smart_money_radar.notifications import NotificationResult
from smart_money_radar.storage import SQLiteStore


def test_risky_venues_are_disabled_for_focused_rechecks() -> None:
    assert funding_client_for_venue("bitunix") is None
    assert funding_client_for_venue("blofin") is None


def paper_route(
    now: datetime,
    long_lead: int,
    short_lead: int,
    *,
    live_net: float = 0.5,
    actionable_threshold: float | None = None,
) -> dict:
    long_settlement = (now + timedelta(seconds=long_lead)).isoformat()
    short_settlement = (now + timedelta(seconds=short_lead)).isoformat()
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
        "expected_gross_funding": 1.0,
        "expected_net_profit": live_net,
        "net_roc_annualized": 1.0,
        "total_fees": 0.5,
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
            "current_nowcast_gross": 1.0,
            "current_nowcast_net": live_net,
            "execution_cost": 0.5,
        },
    }
    if actionable_threshold is not None:
        route["evidence"]["actionable_profit_threshold"] = actionable_threshold
    return route


def accounts() -> dict:
    return {
        "aster": {"available_balance": 1_000.0},
        "binance": {"available_balance": 1_000.0},
    }


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
    config = FundingPaperTraderConfig(
        entry_window_seconds=180,
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    too_early = route_entry_decision(
        paper_route(now, long_lead=60, short_lead=90),
        accounts(),
        now,
        config,
    )
    assert not too_early["eligible"]
    assert "short_settlement_outside_final_entry_window" in too_early["reasons"]

    too_late = route_entry_decision(
        paper_route(now, long_lead=60, short_lead=20),
        accounts(),
        now,
        config,
    )
    assert not too_late["eligible"]
    assert "short_settlement_inside_final_deadline" in too_late["reasons"]

    eligible = route_entry_decision(
        paper_route(now, long_lead=60, short_lead=45),
        accounts(),
        now,
        config,
    )
    assert eligible["eligible"]


def test_default_entry_window_targets_final_fifteen_seconds() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(entry_window_seconds=180).validated()

    too_early = route_entry_decision(
        paper_route(now, long_lead=17, short_lead=12),
        accounts(),
        now,
        config,
    )
    assert not too_early["eligible"]
    assert "long_settlement_outside_final_entry_window" in too_early["reasons"]

    eligible = route_entry_decision(
        paper_route(now, long_lead=14, short_lead=6),
        accounts(),
        now,
        config,
    )
    assert eligible["eligible"]


def test_final_recheck_freeze_uses_recent_successful_snapshot() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now - timedelta(seconds=20), long_lead=28, short_lead=26)
    config = FundingPaperTraderConfig(
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
    config = FundingPaperTraderConfig(
        final_recheck_freeze_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    assert final_recheck_fallback_route(route, now, config, "test_freeze") is None


def test_final_recheck_freeze_does_not_start_before_fifteen_second_boundary() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now - timedelta(seconds=20), long_lead=36, short_lead=35)
    config = FundingPaperTraderConfig(
        final_recheck_freeze_seconds=15,
        max_entry_snapshot_age_seconds=30,
    ).validated()

    assert final_recheck_fallback_route(route, now, config, "test_freeze") is None


def test_entry_rejects_stale_snapshot_inside_final_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(
        entry_window_seconds=180,
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


def test_entry_requires_route_actionable_profit_threshold() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(
        entry_window_seconds=180,
        entry_min_lead_seconds=30,
        entry_max_lead_seconds=60,
    ).validated()

    too_small = route_entry_decision(
            paper_route(
                now,
                long_lead=60,
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
                long_lead=60,
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
    entry_leg = {"funding_rate": 0.00005, "funding_interval_hours": 8.0}
    assert settlement_rate_or_entry(history_row, entry_leg) == 0.0004
    assert settlement_rate_or_entry(None, entry_leg) == 0.00005 * 8.0


def test_settlement_rate_or_entry_variational_4h_interval() -> None:
    entry_leg = {"funding_rate": 0.0001, "funding_interval_hours": 4.0}
    assert settlement_rate_or_entry(None, entry_leg) == 0.0001 * 4.0


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
            "entry_legs": [],
            "entry_evidence": {},
        }
    )
    store.close_funding_paper_position(
        position_id,
        {
            "actual_funding_pnl": 0.0,
            "actual_execution_cost": 2.0,
            "actual_net_pnl": -2.0,
            "long_cash_delta": -1.0,
            "short_cash_delta": -1.0,
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
    assert report_rows[0]["Net PnL $"] == "-0.50"
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
    assert dashboard["summary"]["realized_pnl"] == -0.5
    notifier = FakeNotifier()
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(),
        notifier=notifier,
    )

    assert trader.publish_repriced_pnl_events() == 1
    assert len(notifier.messages) == 1
    assert "Funding Paper Trader PnL REPRICED" in notifier.messages[0]
    assert "Net PnL: <b>-$0.50</b>" in notifier.messages[0]
    events = store.funding_paper_dashboard()["events"]
    reprice_event = next(
        event for event in events if event["event_type"] == "pnl_repriced"
    )
    assert reprice_event["telegram_status"] == "sent"


def test_settlement_hold_accrues_funding_and_rolls_position(tmp_path) -> None:
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
    position_id = store.open_funding_paper_position(
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
    continuation = paper_route(now, 3_480, 3_480, live_net=0.75)
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
        FundingPaperTraderConfig(settlement_grace_seconds=0).validated(),
    )

    assert decision["status"] == "hold"
    assert store.accrue_funding_paper_settlement(position_id, decision["accrual"])
    after = store.funding_paper_open_positions()[0]
    balances = {row["venue"]: row for row in store.funding_paper_account_rows()}
    assert after["status"] == "open"
    assert after["max_settlement_at"] == next_long
    assert after["actual_funding_pnl"] == 1.5
    assert balances["aster"]["cash_balance"] == 1_000.5
    assert balances["binance"]["cash_balance"] == 1_001.0
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
        FundingPaperTraderConfig(settlement_grace_seconds=0).validated(),
    )

    assert decision["status"] == "close"
    assert (
        decision["close"]["close_reason"]
        == "arbitrage_window_closed_live_net_non_positive"
    )
    assert decision["close"]["actual_funding_pnl"] == 1.5
    assert decision["close"]["actual_net_pnl"] == 1.0


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
        FundingPaperTraderConfig(settlement_grace_seconds=0).validated(),
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
        FundingPaperTraderConfig(
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
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(retention_interval_seconds=30),
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
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(retention_interval_seconds=30),
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
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(),
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
        config=FundingPaperTraderConfig(),
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
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(
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

    trader.maybe_record_status_report(result, [paper_route(now, 60, 45)], [])
    trader.maybe_record_status_report(result, [paper_route(now, 60, 45)], [])

    assert len(notifier.messages) == 1
    assert "Funding Paper Trader STATUS" in notifier.messages[0]
    assert "Candidates: 1" in notifier.messages[0]
    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert event_types.count("status_report") == 1


def test_status_report_does_not_publish_watch_routes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(status_report_interval_seconds=1_800),
        notifier=notifier,
    )
    now = datetime.now(UTC)
    watch = paper_route(now, 60, 45, live_net=3.0)
    watch["status"] = "watch"

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
        },
        [],
        [watch],
    )

    assert len(notifier.messages) == 1
    assert "Watch/internal: 1" in notifier.messages[0]
    assert "Closest watch" not in notifier.messages[0]
    assert "LONG <code>aster BTCUSDT</code>" not in notifier.messages[0]


def test_status_report_does_not_publish_negative_live_pnl_candidate(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(status_report_interval_seconds=1_800),
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
        [paper_route(now, 60, 45, live_net=-0.25)],
        [],
    )

    assert len(notifier.messages) == 1
    assert "Candidates: 0" in notifier.messages[0]
    assert "LONG <code>aster BTCUSDT</code>" not in notifier.messages[0]
    assert "-$0.25" not in notifier.messages[0]


def test_next_sleep_uses_three_speed_monitoring(tmp_path) -> None:
    trader = FundingPaperTrader(
        SQLiteStore(tmp_path / "radar.sqlite"),
        config=FundingPaperTraderConfig(
            scan_interval_seconds=300,
            monitor_interval_seconds=120,
            hot_interval_seconds=10,
        ),
    )

    assert trader.next_sleep_seconds({"hot_route_count": 0}) == 300
    assert trader.next_sleep_seconds({"hot_route_count": 1}) == 120
    assert (
        trader.next_sleep_seconds({"hot_route_count": 1, "urgent_route_count": 1})
        == 10
    )
    assert trader.next_sleep_seconds({"pending_count": 1}) == 10
    assert trader.next_sleep_seconds({"open_position_count": 1}) == 10


def test_monitor_route_is_urgent_only_inside_entry_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(
        entry_window_seconds=180,
        arm_window_seconds=900,
    ).validated()

    monitored = route_monitor_decision(paper_route(now, 600, 540), now, config)
    urgent = route_monitor_decision(paper_route(now, 120, 100), now, config)

    assert monitored["hot"]
    assert not monitored["urgent"]
    assert urgent["hot"]
    assert urgent["urgent"]


def test_urgent_route_keeps_focused_loop_after_base_interval(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(
            entry_window_seconds=180,
            scan_interval_seconds=300,
        ),
    )
    trader.hot_routes["route-1"] = paper_route(now, 120, 110)
    trader.last_full_scan_monotonic = time.monotonic() - 300

    assert trader.should_run_hot_iteration()


def test_hot_route_rechecks_are_parallelized(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    trader = CountingRecheckTrader(
        store,
        config=FundingPaperTraderConfig(hot_route_recheck_workers=2),
    )
    for index in range(4):
        route = paper_route(now, 600, 540)
        route["route_key"] = f"route-{index}"
        route["canonical_asset"] = f"ASSET{index}"
        trader.hot_routes[route["route_key"]] = route

    refreshed = trader.refresh_hot_routes()

    assert len(refreshed) == 4
    assert trader.max_active_rechecks == 2


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
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(scan_interval_seconds=300),
    )

    assert trader.should_run_hot_iteration()


def test_status_report_can_be_disabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    notifier = FakeNotifier()
    trader = FundingPaperTrader(
        store,
        config=FundingPaperTraderConfig(status_report_interval_seconds=0),
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


class ExplodingTrader(FundingPaperTrader):
    def run_iteration(self) -> dict:
        raise RuntimeError("boom")


class CountingRecheckTrader(FundingPaperTrader):
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
    config = FundingPaperTraderConfig(basis_stop_loss_bps=200.0).validated()
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
    config = FundingPaperTraderConfig(basis_stop_loss_bps=200.0).validated()
    snapshot = {
        "spread_tracking": True,
        "notional": 500.0,
        "unrealized_basis_pnl": -5.0,
    }
    triggered, _ = spread_stop_loss_triggered(snapshot, config)
    assert not triggered


def test_spread_stop_loss_ignores_positive_pnl() -> None:
    config = FundingPaperTraderConfig(basis_stop_loss_bps=200.0).validated()
    snapshot = {
        "spread_tracking": True,
        "notional": 500.0,
        "unrealized_basis_pnl": 10.0,
    }
    triggered, _ = spread_stop_loss_triggered(snapshot, config)
    assert not triggered


def test_spread_stop_loss_ignores_untracked_snapshot() -> None:
    config = FundingPaperTraderConfig(basis_stop_loss_bps=200.0).validated()
    triggered, _ = spread_stop_loss_triggered({"spread_tracking": False}, config)
    assert not triggered


def test_hold_decision_detects_funding_rate_inversion() -> None:
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    config = FundingPaperTraderConfig(
        max_entry_snapshot_age_seconds=300,
    ).validated()
    position = {
        "route_key": "route-1",
        "max_settlement_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 0.5,
        "notes": {},
    }
    route = paper_route(now, 3_600, 3_600, live_net=1.0)
    route["legs"][0]["hourly_funding_rate"] = 0.001
    route["legs"][1]["hourly_funding_rate"] = 0.0005

    decision = position_hold_decision(position, route, now, config)
    assert not decision["hold"]
    assert "funding_rate_inverted" in decision["reasons"]
    assert decision["close_reason"] == "arbitrage_window_funding_rate_inverted"


def test_hold_decision_allows_normal_rate_order() -> None:
    now = datetime(2026, 7, 19, 12, 2, tzinfo=UTC)
    config = FundingPaperTraderConfig(
        max_entry_snapshot_age_seconds=300,
    ).validated()
    position = {
        "route_key": "route-1",
        "max_settlement_at": "2026-07-19T12:00:00+00:00",
        "expected_execution_cost": 0.5,
        "notes": {},
    }
    route = paper_route(now, 3_600, 3_600, live_net=1.0)
    route["legs"][0]["hourly_funding_rate"] = -0.001
    route["legs"][1]["hourly_funding_rate"] = 0.001

    decision = position_hold_decision(position, route, now, config)
    assert decision["hold"]
    assert "funding_rate_inverted" not in decision["reasons"]


def test_build_position_includes_spread_fields() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    route = paper_route(now, 60, 45, live_net=1.0)
    route["legs"][0]["evidence"] = {"vwap": 100.5}
    route["legs"][1]["evidence"] = {"vwap": 100.2}
    route["evidence"]["signed_entry_basis"] = -0.003
    config = FundingPaperTraderConfig().validated()
    decision = route_entry_decision(route, accounts(), now, config)

    position = build_position_from_route(route, decision, config)
    assert abs(position["entry_cross_spread"] - 0.3) < 1e-9
    assert abs(position["entry_basis_bps"] - (-30.0)) < 1e-9
    assert position["notes"]["paper_model"] == "funding_paper_trader_v2"


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
             "next_funding_at": "2026-07-19T12:00:00+00:00"},
            {"side": "short", "venue": "binance", "symbol": "BTCUSDT",
             "funding_rate": 0.001, "notional": 500.0, "base_quantity": 2.0,
             "next_funding_at": "2026-07-19T12:00:00+00:00"},
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
    assert close["spread_snapshot"]["spread_tracking"] is True
    assert close["notes"]["paper_model"] == "funding_paper_trader_v2"
    assert close["notes"]["basis_pnl_included"] is True


def test_funding_client_for_venue_covers_all_active_venues() -> None:
    active_venues = [
        "aevo", "apex", "aster", "backpack", "binance", "bitmart",
        "bitget", "bybit", "coinex", "deribit", "drift", "dydx",
        "edgex", "ethereal", "extended", "gate", "grvt", "htx",
        "hyperliquid", "kraken", "kucoin", "lighter", "mexc", "okx",
        "pacifica", "paradex", "reya", "risex", "variational",
        "vertex_base", "woox",
    ]
    for venue in active_venues:
        client = funding_client_for_venue(venue)
        assert client is not None, f"Missing client for {venue}"
        assert client.venue == venue


def test_funding_client_for_venue_rejects_deactivated() -> None:
    for venue in ("bingx", "bitunix", "blofin", "phemex"):
        assert funding_client_for_venue(venue) is None


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
