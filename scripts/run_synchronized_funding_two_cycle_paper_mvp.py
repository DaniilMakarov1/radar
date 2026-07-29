from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_synchronized_funding_paper_mvp import (
    SnapshotClient,
    observation,
    route,
)
from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
from smart_money_radar.paper_bot.clock import FakeClock
from smart_money_radar.paper_bot.helpers import route_entry_key
from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
from smart_money_radar.paper_bot.settlement import FakeFundingSettlementDataProvider
from smart_money_radar.storage import SQLiteStore


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, sort_keys=True))


def set_fixture_rates(route_payload: dict[str, Any], *, long_rate: float, short_rate: float) -> dict[str, Any]:
    for leg in route_payload.get("legs") or []:
        rate = long_rate if str(leg.get("side") or "").lower() == "long" else short_rate
        leg["funding_rate"] = rate
        leg["normalized_next_funding_rate"] = rate
        leg["hourly_funding_rate"] = rate / float(leg.get("funding_interval_hours") or 1.0)
    return route_payload


def route_for_settlement(at: datetime, scheduled_at: datetime, route_key: str) -> dict[str, Any]:
    return set_fixture_rates(
        route(
            at,
            lead_seconds=max(1.0, (scheduled_at - at).total_seconds()),
            route_key=route_key,
        ),
        long_rate=-0.020,
        short_rate=0.022,
    )


def seed_reconciled_hold_history(store: SQLiteStore) -> None:
    for idx in range(8):
        created = datetime(2026, 7, 27, idx, 0, 30, tzinfo=UTC)
        scheduled = datetime(2026, 7, 27, idx + 1, 0, 0, tzinfo=UTC)
        position_id = f"fixture-good-hold-history-{idx}"
        store.upsert_funding_capture_position(
            {
                "position_id": position_id,
                "canonical_asset": "BTC",
                "long_venue": "binance",
                "long_symbol": "BTCUSDT",
                "short_venue": "bybit",
                "short_symbol": "BTCUSDT",
                "quantity": 5.0,
                "target_notional": 500.0,
                "state": "RECONCILED",
                "opened_at": (created - timedelta(seconds=60)).isoformat(),
                "closed_at": scheduled.isoformat(),
                "config": {
                    "route_key": "BTC:binance:bybit",
                    "entry_legs": [
                        {"side": "long", "venue": "binance", "collateral_asset": "USDT"},
                        {"side": "short", "venue": "bybit", "collateral_asset": "USDT"},
                    ],
                },
            }
        )
        store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{position_id}:2",
                "position_id": position_id,
                "cycle_number": 2,
                "scheduled_funding_at": scheduled.isoformat(),
                "conservative_funding_gross": 10.0,
                "incremental_hold_net_pnl": 9.0,
                "decision": "HOLD",
                "state": "RECONCILED",
                "reconciliation_status": "RATE_AND_MARK_RECONCILED",
                "reconciled_funding_pnl": 10.0,
                "created_at": created.isoformat(),
            }
        )
        for side, venue in (("long", "binance"), ("short", "bybit")):
            store.upsert_funding_settlement_reconciliation(
                {
                    "position_id": position_id,
                    "cycle_id": f"{position_id}:2",
                    "venue": venue,
                    "symbol": "BTCUSDT",
                    "side": side,
                    "scheduled_funding_at": scheduled.isoformat(),
                    "status": "HISTORY_FIXTURE",
                    "evidence": {
                        "fixture": "reconciled_hold_history",
                        "financial_effect_external_to_scenario": True,
                    },
                }
            )


def make_bot(
    store: SQLiteStore,
    clock: FakeClock,
    route_holder: dict[str, dict[str, Any]],
    *,
    max_settlements: int = 2,
) -> PaperBot:
    config = PaperBotConfig(
        telegram_enabled=False,
        focused_recheck_enabled=False,
        venue_starting_balance=10_000.0,
        target_notional_per_leg=500.0,
        max_settlements_per_position=max_settlements,
    ).validated()
    bot = PaperBot(store, config, clock=clock)
    bot.build_venue_clients = lambda: [
        SnapshotClient("binance", lambda: route_holder["route"]),
        SnapshotClient("bybit", lambda: route_holder["route"]),
    ]
    return bot


def obligation_count(store: SQLiteStore, position_id: str, cycle_id: str) -> int:
    return len(store.funding_settlement_reconciliation_rows(position_id, cycle_id=cycle_id))


def funding_ledger_count(store: SQLiteStore, position_id: str, cycle_id: str | None = None) -> int:
    rows = [
        row
        for row in store.paper_event_ledger_rows(position_id)
        if row["event_type"] == "funding"
    ]
    if cycle_id is not None:
        rows = [row for row in rows if row["cycle_id"] == cycle_id]
    return len(rows)


def account_deltas(store: SQLiteStore) -> dict[str, float]:
    return {
        row["venue"]: round(float(row["cash_balance"]) - float(row["starting_balance"]), 10)
        for row in store.funding_paper_account_rows()
    }


def cycle_line(store: SQLiteStore, position_id: str, cycle_id: str) -> dict[str, Any]:
    cycles = {
        str(cycle["cycle_id"]): cycle
        for cycle in store.funding_capture_cycles_for_position(position_id)
    }
    cycle = cycles[cycle_id]
    position = store.funding_capture_position_by_id(position_id) or {}
    return {
        "position_id": position_id,
        "cycle_id": cycle_id,
        "active_plan_generation": int(cycle.get("plan_generation") or 0),
        "scheduled_funding_timestamp": cycle["scheduled_funding_at"],
        "obligation_count": obligation_count(store, position_id, cycle_id),
        "settlements_captured_count": int(position.get("settlements_captured_count") or 0),
        "funding_ledger_count": funding_ledger_count(store, position_id, cycle_id),
        "per_venue_account_delta": account_deltas(store),
        "final_state": position.get("state"),
    }


def main() -> None:
    start = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    settlement_1 = start + timedelta(seconds=30)
    settlement_2 = settlement_1 + timedelta(seconds=3600)
    settlement_3 = settlement_2 + timedelta(seconds=3600)
    db_path = Path(tempfile.gettempdir()) / "radar_synchronized_funding_two_cycle_mvp.sqlite"
    if db_path.exists():
        db_path.unlink()

    store = SQLiteStore(db_path)
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    seed_reconciled_hold_history(store)

    route_key = "BTC:binance:bybit"
    initial_route = route(start, lead_seconds=30, route_key=route_key)
    route_holder = {"route": initial_route}
    clock = FakeClock(start)
    bot = make_bot(store, clock, route_holder)
    bot.v2_observations_by_route[route_entry_key(initial_route)] = [
        observation(start - timedelta(seconds=22.0 - index * (21.0 / 9.0)), settlement_1)
        for index in range(10)
    ]
    position_id = capture_position_id_for_route(initial_route)

    emit(
        "discover",
        position_id=position_id,
        cycle_id=None,
        active_plan_generation=None,
        scheduled_funding_timestamp=settlement_1.isoformat(),
        obligation_count=0,
        settlements_captured_count=0,
        funding_ledger_count=0,
        per_venue_account_delta=account_deltas(store),
        final_state="DISCOVERED",
    )
    opened = bot.process_entry_candidates([initial_route], recheck_before_open=False)
    assert opened == [position_id], opened
    position = store.funding_capture_position_by_id(position_id)
    assert position and position["state"] == "OPEN"
    cycle_1 = f"{position_id}:1"
    emit("open", **cycle_line(store, position_id, cycle_1))

    clock.advance((settlement_1 + timedelta(seconds=1) - clock.now()).total_seconds())
    route_holder["route"] = route_for_settlement(clock.now(), settlement_2, route_key)
    outcomes = bot.process_open_positions()
    assert outcomes == ["settlement_crossed"], outcomes
    assert obligation_count(store, position_id, cycle_1) == 2
    emit("cycle_1_boundary", **cycle_line(store, position_id, cycle_1))

    hold_outcomes: list[str] = []
    for offset in range(5, 32):
        target = settlement_1 + timedelta(seconds=offset)
        clock.advance((target - clock.now()).total_seconds())
        route_holder["route"] = route_for_settlement(clock.now(), settlement_2, route_key)
        outcomes = bot.process_open_positions()
        hold_outcomes.extend(outcomes)
        if "hold" in outcomes:
            break
    assert hold_outcomes[-1:] == ["hold"], hold_outcomes
    cycle_2 = f"{position_id}:2"
    position = store.funding_capture_position_by_id(position_id)
    assert position and position["state"] == "HOLDING_NEXT_CYCLE"
    assert str(position["current_cycle_id"]) == cycle_2
    assert int(position["current_cycle_number"]) == 2
    cycles = {
        str(cycle["cycle_id"]): cycle
        for cycle in store.funding_capture_cycles_for_position(position_id)
    }
    assert cycles[cycle_1]["active_plan"]["scheduled_funding_at"] == settlement_1.isoformat()
    assert cycles[cycle_2]["active_plan"]["scheduled_funding_at"] == settlement_2.isoformat()
    assert {
        event["scheduled_at"]
        for event in cycles[cycle_2]["active_plan"]["included_settlement_events"]
    } == {settlement_2.isoformat()}
    emit("hold", **cycle_line(store, position_id, cycle_2))

    restart_at = clock.now()
    clock = FakeClock(restart_at)
    route_holder["route"] = route_for_settlement(clock.now(), settlement_2, route_key)
    bot = make_bot(store, clock, route_holder)
    recovery = bot.last_runtime_recovery
    emit(
        "restart",
        **cycle_line(store, position_id, cycle_2),
        recovery=recovery,
    )

    clock.advance((settlement_2 + timedelta(seconds=1) - clock.now()).total_seconds())
    route_holder["route"] = route_for_settlement(clock.now(), settlement_3, route_key)
    outcomes = bot.process_open_positions()
    assert outcomes == ["settlement_crossed"], outcomes
    assert obligation_count(store, position_id, cycle_2) == 2
    assert (store.funding_capture_position_by_id(position_id) or {})["settlements_captured_count"] == 2
    emit("cycle_2_boundary", **cycle_line(store, position_id, cycle_2))

    clock.advance((settlement_2 + timedelta(seconds=31) - clock.now()).total_seconds())
    route_holder["route"] = route_for_settlement(clock.now(), settlement_3, route_key)
    outcomes = bot.process_open_positions()
    assert outcomes == ["closed"], outcomes
    position = store.funding_capture_position_by_id(position_id)
    assert position and position["state"] == "CLOSED_PENDING_RECONCILIATION"
    emit("close", **cycle_line(store, position_id, cycle_2))

    bot.synchronized_runtime.settlement_data_provider = FakeFundingSettlementDataProvider(
        public_events=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": settlement_1.isoformat(),
                "funding_rate": -0.008,
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": settlement_1.isoformat(),
                "funding_rate": 0.010,
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": settlement_2.isoformat(),
                "funding_rate": -0.020,
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": settlement_2.isoformat(),
                "funding_rate": 0.022,
            },
        ],
        mark_snapshots=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "observed_at": settlement_1.isoformat(),
                "mark_price": 100.0,
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "observed_at": settlement_1.isoformat(),
                "mark_price": 100.0,
            },
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "observed_at": settlement_2.isoformat(),
                "mark_price": 100.0,
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "observed_at": settlement_2.isoformat(),
                "mark_price": 100.0,
            },
        ],
    )
    reconciliation = bot.synchronized_runtime.process_pending_reconciliations(clock.now())
    repeat = bot.synchronized_runtime.process_pending_reconciliations(clock.now())
    assert reconciliation["reconciled"] == 4, reconciliation
    assert repeat["processed"] == 0, repeat
    assert funding_ledger_count(store, position_id, cycle_1) == 2
    assert funding_ledger_count(store, position_id, cycle_2) == 2
    final_position = store.funding_capture_position_by_id(position_id)
    consistency = store.paper_account_consistency_report()
    assert final_position and final_position["state"] == "RECONCILED"
    assert final_position["settlements_captured_count"] == 2
    assert consistency["ok"], consistency
    emit(
        "reconcile",
        **cycle_line(store, position_id, cycle_2),
        cycle_1_obligation_count=obligation_count(store, position_id, cycle_1),
        cycle_2_obligation_count=obligation_count(store, position_id, cycle_2),
        total_funding_ledger_count=funding_ledger_count(store, position_id),
        ledger_account_consistency=consistency,
        reconciliation=reconciliation,
        repeat_reconciliation=repeat,
        db_path=str(db_path),
    )


if __name__ == "__main__":
    main()
