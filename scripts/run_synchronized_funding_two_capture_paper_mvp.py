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

from scripts.run_synchronized_funding_paper_mvp import (  # noqa: E402
    SnapshotClient,
    observation,
    route,
)
from smart_money_radar.funding.trader import PaperBot, PaperBotConfig  # noqa: E402
from smart_money_radar.paper_bot.clock import FakeClock  # noqa: E402
from smart_money_radar.paper_bot.helpers import route_entry_key  # noqa: E402
from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route  # noqa: E402
from smart_money_radar.storage import SQLiteStore  # noqa: E402


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, sort_keys=True))


def account_deltas(store: SQLiteStore) -> dict[str, float]:
    return {
        str(row["venue"]): round(
            float(row["cash_balance"]) - float(row["starting_balance"]),
            10,
        )
        for row in store.funding_paper_account_rows()
    }


def seed_entry_observations(
    bot: PaperBot,
    route_payload: dict[str, Any],
    *,
    settlement_at: datetime,
) -> None:
    bot.v2_observations_by_route[route_entry_key(route_payload)] = [
        observation(
            settlement_at - timedelta(seconds=22.0 - index * (21.0 / 9.0)),
            settlement_at,
        )
        for index in range(10)
    ]


def run_one_capture(
    *,
    bot: PaperBot,
    store: SQLiteStore,
    clock: FakeClock,
    route_holder: dict[str, dict[str, Any]],
    route_payload: dict[str, Any],
    settlement_at: datetime,
    label: str,
) -> str:
    route_holder["route"] = route_payload
    seed_entry_observations(bot, route_payload, settlement_at=settlement_at)
    capture_id = capture_position_id_for_route(route_payload)

    opened = bot.process_entry_candidates([route_payload], recheck_before_open=False)
    assert opened == [capture_id], opened
    opened_position = store.funding_capture_position_by_id(capture_id)
    assert opened_position and opened_position["state"] == "OPEN"
    emit(label + "_open", capture_id=capture_id, state=opened_position["state"])

    clock.advance((settlement_at + timedelta(seconds=1) - clock.now()).total_seconds())
    route_holder["route"] = route(
        clock.now(),
        lead_seconds=3600,
        route_key=route_payload["route_key"],
    )
    boundary_outcomes = bot.process_open_positions()
    assert boundary_outcomes == ["settlement_crossed"], boundary_outcomes
    crossed_position = store.funding_capture_position_by_id(capture_id)
    assert crossed_position and crossed_position["state"] == "OPEN"
    assert (crossed_position["config"] or {}).get("boundary_crossed_at")
    obligations = store.funding_settlement_reconciliation_rows(capture_id)
    assert len(obligations) == 2
    emit(label + "_boundary", capture_id=capture_id, obligations=len(obligations))

    clock.advance((settlement_at + timedelta(seconds=31) - clock.now()).total_seconds())
    route_holder["route"] = route(
        clock.now(),
        lead_seconds=3600,
        route_key=route_payload["route_key"],
    )
    close_outcomes = bot.process_open_positions()
    assert close_outcomes == ["closed"], close_outcomes
    closed_position = store.funding_capture_position_by_id(capture_id)
    assert closed_position and closed_position["state"] == "CLOSED"
    config = closed_position["config"] or {}
    assert config.get("mandatory_exit_after_first_boundary") is True
    assert config.get("exit_mode") == "MANDATORY"
    emit(
        label + "_closed",
        capture_id=capture_id,
        state=closed_position["state"],
        exit_mode=config.get("exit_mode"),
    )
    return capture_id


def main() -> None:
    first_entry_at = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    first_settlement = first_entry_at + timedelta(seconds=30)
    second_settlement = first_settlement + timedelta(seconds=3600)
    second_entry_at = second_settlement - timedelta(seconds=30)
    db_path = Path(tempfile.gettempdir()) / "radar_synchronized_funding_two_capture_mvp.sqlite"
    if db_path.exists():
        db_path.unlink()

    store = SQLiteStore(db_path)
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    clock = FakeClock(first_entry_at)
    route_holder: dict[str, dict[str, Any]] = {
        "route": route(first_entry_at, lead_seconds=30),
    }
    bot = PaperBot(
        store,
        PaperBotConfig(
            telegram_enabled=False,
            focused_recheck_enabled=False,
            venue_starting_balance=10_000.0,
            target_notional_per_leg=500.0,
        ).validated(),
        clock=clock,
    )
    bot.build_venue_clients = lambda: [
        SnapshotClient("binance", lambda: route_holder["route"]),
        SnapshotClient("bybit", lambda: route_holder["route"]),
    ]

    first_route = route(first_entry_at, lead_seconds=30)
    first_capture_id = run_one_capture(
        bot=bot,
        store=store,
        clock=clock,
        route_holder=route_holder,
        route_payload=first_route,
        settlement_at=first_settlement,
        label="capture_1",
    )

    clock.advance((second_entry_at - clock.now()).total_seconds())
    second_route = route(second_entry_at, lead_seconds=30)
    second_capture_id = run_one_capture(
        bot=bot,
        store=store,
        clock=clock,
        route_holder=route_holder,
        route_payload=second_route,
        settlement_at=second_settlement,
        label="capture_2",
    )

    assert first_capture_id != second_capture_id
    rows = store.funding_capture_position_rows()
    assert len(rows) == 2
    assert {str(row["state"]) for row in rows} == {"CLOSED"}
    emit(
        "complete",
        first_capture_id=first_capture_id,
        second_capture_id=second_capture_id,
        position_count=len(rows),
        account_delta=account_deltas(store),
        db_path=str(db_path),
    )


if __name__ == "__main__":
    main()
