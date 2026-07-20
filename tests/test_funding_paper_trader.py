from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smart_money_radar.funding.trader import (
    FundingPaperTrader,
    FundingPaperTraderConfig,
    funding_leg_pnl,
    route_entry_decision,
)
from smart_money_radar.notifications import NotificationResult
from smart_money_radar.storage import SQLiteStore


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
        "status": "paper_candidate",
        "canonical_asset": "BTC",
        "long_venue": "aster",
        "long_symbol": "BTCUSDT",
        "short_venue": "binance",
        "short_symbol": "BTCUSDT",
        "target_notional": 500.0,
        "market_capacity": 1_000.0,
        "risk_flags": [],
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


def test_entry_requires_both_legs_inside_final_entry_window() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(entry_window_seconds=180).validated()

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


def test_entry_requires_route_actionable_profit_threshold() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    config = FundingPaperTraderConfig(entry_window_seconds=180).validated()

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
    }

    trader.maybe_record_status_report(result, [paper_route(now, 60, 45)], [])
    trader.maybe_record_status_report(result, [paper_route(now, 60, 45)], [])

    assert len(notifier.messages) == 1
    assert "Funding Paper Trader STATUS" in notifier.messages[0]
    assert "Candidates: 1" in notifier.messages[0]
    event_types = [row["event_type"] for row in store.funding_paper_dashboard()["events"]]
    assert event_types.count("status_report") == 1


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
