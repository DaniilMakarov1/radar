"""End-to-end reconciliation worker tests (pass 006)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
from smart_money_radar.paper_bot.accounting import (
    collateral_reserve_event_key,
    funding_event_key,
    make_ledger_entry,
)
from smart_money_radar.paper_bot.clock import FakeClock
from smart_money_radar.paper_bot.runtime_v2 import SynchronizedFundingRuntimeV2
from smart_money_radar.paper_bot.settlement import (
    FakeFundingSettlementDataProvider,
    StoredFundingSettlementDataProvider,
    build_settlement_crossing_rows,
)
from smart_money_radar.storage import SQLiteStore


SCHEDULED = "2026-07-28T16:00:00+00:00"
NOW_BEFORE = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
NOW_AT_SETTLEMENT = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
NOW_AFTER = datetime(2026, 7, 28, 16, 1, 0, tzinfo=UTC)


def _seed_closed_pending_position(
    store, *, position_id="fc-recon-test", quantity=5.0,
    scheduled_funding_at=SCHEDULED, state="CLOSED_PENDING_RECONCILIATION",
):
    store.upsert_funding_capture_position({
        "position_id": position_id, "canonical_asset": "BTC",
        "long_venue": "binance", "long_symbol": "BTCUSDT",
        "short_venue": "bybit", "short_symbol": "BTCUSDT",
        "quantity": quantity, "target_notional": 500.0, "state": state,
        "opened_at": NOW_BEFORE.isoformat(), "closed_at": NOW_AFTER.isoformat(),
        "paper_open_fees": 0.50, "paper_close_fees": 0.50,
        "paper_emergency_unwind_cost": 0.0,
        "config": {"route_key": "BTC:binance:bybit", "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "fee_rate": 0.0005},
        ]},
    })
    store.upsert_funding_capture_cycle({
        "position_id": position_id, "cycle_number": 1,
        "scheduled_funding_at": scheduled_funding_at, "state": "SETTLEMENT_CROSSED",
    })
    for row in build_settlement_crossing_rows(
        position_id=position_id, cycle_id=f"{position_id}:1",
        long_venue="binance", long_symbol="BTCUSDT",
        short_venue="bybit", short_symbol="BTCUSDT",
        scheduled_funding_at=scheduled_funding_at, quantity=quantity,
    ):
        store.upsert_funding_settlement_reconciliation(row)
    store.upsert_paper_event_ledger(make_ledger_entry(
        f"price_pnl:{position_id}:close", position_id=position_id,
        cycle_id=f"{position_id}:1", event_type="price_pnl",
        cash_delta=-1.0, payload={"reason": "test_close"},
    ))
    return position_id


def _make_runtime(store, now, provider=None):
    config = PaperBotConfig(telegram_enabled=False).validated()
    return SynchronizedFundingRuntimeV2(
        store=store, config=config, clock=FakeClock(now),
        observations_by_route={}, settlement_data_provider=provider,
    )


def _seed_instrument(store, venue="binance", symbol="BTCUSDT", canonical_asset="BTC"):
    store.upsert_funding_instruments([
        {
            "venue": venue,
            "symbol": symbol,
            "canonical_asset": canonical_asset,
            "base_asset": canonical_asset,
            "quote_asset": "USDT",
            "collateral_asset": "USDT",
            "contract_type": "linear_perpetual",
            "contract_multiplier": 1.0,
            "status": "trading",
            "observed_at": NOW_BEFORE.isoformat(),
            "raw": {},
        }
    ])


def test_pending_no_public_event_no_funding_ledger(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(public_events=[], mark_snapshots=[])
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    runtime = _make_runtime(store, now, provider)
    result = runtime.process_pending_reconciliations(now)
    assert result["processed"] == 0
    assert result["reconciled"] == 0
    assert len(store.pending_reconciliation_rows()) == 2
    ledger = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger if r["event_type"] == "funding"]) == 0


def test_reconciliation_success_rate_and_mark(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store, quantity=5.0)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    runtime = _make_runtime(store, now, provider)
    result = runtime.process_pending_reconciliations(now)
    assert result["reconciled"] == 2
    assert result["processed"] == 2
    recon_rows = store.funding_settlement_reconciliation_rows("fc-recon-test")
    assert len(recon_rows) == 2
    for row in recon_rows:
        assert row["status"] == "RATE_AND_MARK_RECONCILED"
    long_row = next(r for r in recon_rows if r["side"] == "long")
    short_row = next(r for r in recon_rows if r["side"] == "short")
    assert long_row["funding_pnl"] == pytest.approx(-0.5)
    assert short_row["funding_pnl"] == pytest.approx(0.5)
    ledger = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger if r["event_type"] == "funding"]) == 2
    cycles = store.funding_capture_cycles_for_position("fc-recon-test")
    assert cycles[0]["state"] == "RECONCILED"
    assert cycles[0]["reconciled_funding_pnl"] == pytest.approx(0.0)
    position = store.funding_capture_position_by_id("fc-recon-test")
    assert position["state"] == "RECONCILED"
    assert position["paper_net_pnl_reconciled"] == pytest.approx(-2.0)


def test_duplicate_reconciliation_no_double_cash(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store, quantity=5.0)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    runtime = _make_runtime(store, now, provider)
    result1 = runtime.process_pending_reconciliations(now)
    assert result1["reconciled"] == 2
    ledger1 = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger1 if r["event_type"] == "funding"]) == 2
    result2 = runtime.process_pending_reconciliations(now)
    assert result2["processed"] == 0
    ledger2 = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger2 if r["event_type"] == "funding"]) == 2


def test_reconciliation_timeout_marks_unreconciled(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(public_events=[], mark_snapshots=[])
    now = NOW_AT_SETTLEMENT + timedelta(seconds=601)
    runtime = _make_runtime(store, now, provider)
    result = runtime.process_pending_reconciliations(now)
    assert result["timed_out"] == 2
    for row in store.funding_settlement_reconciliation_rows("fc-recon-test"):
        assert row["status"] == "UNRECONCILED"
    cycles = store.funding_capture_cycles_for_position("fc-recon-test")
    assert cycles[0]["state"] == "UNRECONCILED"
    position = store.funding_capture_position_by_id("fc-recon-test")
    assert position["state"] == "UNRECONCILED"
    assert position["paper_net_pnl_reconciled"] is None


def test_close_position_includes_confirmed_funding(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    pid = "fc-close-funding"
    store.upsert_funding_capture_position({
        "position_id": pid, "canonical_asset": "BTC",
        "long_venue": "binance", "long_symbol": "BTCUSDT",
        "short_venue": "bybit", "short_symbol": "BTCUSDT",
        "quantity": 5.0, "target_notional": 500.0, "state": "OPEN",
        "opened_at": (now - timedelta(seconds=60)).isoformat(), "paper_open_fees": 0.50,
        "config": {"route_key": "BTC:binance:bybit", "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0005},
        ]},
    })
    store.upsert_funding_capture_cycle({
        "position_id": pid, "cycle_number": 1,
        "scheduled_funding_at": now.isoformat(), "state": "OPEN",
    })
    for v in ("binance", "bybit"):
        store.upsert_paper_event_ledger(make_ledger_entry(
            collateral_reserve_event_key(pid, v), position_id=pid, venue=v,
            event_type="collateral_reserve", cash_delta=0.0, payload={"amount": 625.0},
        ))
        store.update_funding_paper_account_reserved(v, 625.0)
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "binance", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="binance", event_type="funding", cash_delta=-1.25,
    ))
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "bybit", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="bybit", event_type="funding", cash_delta=1.25,
    ))
    config = PaperBotConfig(telegram_enabled=False).validated()
    runtime = SynchronizedFundingRuntimeV2(
        store=store, config=config, clock=FakeClock(now), observations_by_route={},
    )
    position = store.funding_capture_position_by_id(pid)
    route = {"legs": [
        {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "mark_price": 100.0,
         "fee_rate": 0.0005, "bids": [[100.0, 10.0]], "close_vwap": 100.0, "best_bid": 100.0},
        {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "mark_price": 100.0,
         "fee_rate": 0.0005, "asks": [[100.0, 10.0]], "close_vwap": 100.0, "best_ask": 100.0},
    ]}
    result = runtime.close_position(position, route, now, reason="test_close")
    assert result["decision"] == "closed"
    assert result["paper_confirmed_funding_pnl"] == pytest.approx(0.0)


def test_close_position_includes_nonzero_confirmed_funding(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    now = datetime(2026, 7, 28, 16, 0, 0, tzinfo=UTC)
    pid = "fc-close-funding-nz"
    store.upsert_funding_capture_position({
        "position_id": pid, "canonical_asset": "BTC",
        "long_venue": "binance", "long_symbol": "BTCUSDT",
        "short_venue": "bybit", "short_symbol": "BTCUSDT",
        "quantity": 5.0, "target_notional": 500.0, "state": "OPEN",
        "opened_at": (now - timedelta(seconds=60)).isoformat(), "paper_open_fees": 0.50,
        "config": {"route_key": "BTC:binance:bybit", "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT",
             "entry_fill_price": 100.0, "vwap": 100.0, "fee_rate": 0.0005},
        ]},
    })
    store.upsert_funding_capture_cycle({
        "position_id": pid, "cycle_number": 1,
        "scheduled_funding_at": now.isoformat(), "state": "OPEN",
    })
    for v in ("binance", "bybit"):
        store.upsert_paper_event_ledger(make_ledger_entry(
            collateral_reserve_event_key(pid, v), position_id=pid, venue=v,
            event_type="collateral_reserve", cash_delta=0.0, payload={"amount": 625.0},
        ))
        store.update_funding_paper_account_reserved(v, 625.0)
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "binance", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="binance", event_type="funding", cash_delta=-0.5,
    ))
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "bybit", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="bybit", event_type="funding", cash_delta=1.0,
    ))
    config = PaperBotConfig(telegram_enabled=False).validated()
    runtime = SynchronizedFundingRuntimeV2(
        store=store, config=config, clock=FakeClock(now), observations_by_route={},
    )
    position = store.funding_capture_position_by_id(pid)
    route = {"legs": [
        {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "mark_price": 100.0,
         "fee_rate": 0.0005, "bids": [[100.0, 10.0]], "close_vwap": 100.0, "best_bid": 100.0},
        {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "mark_price": 100.0,
         "fee_rate": 0.0005, "asks": [[100.0, 10.0]], "close_vwap": 100.0, "best_ask": 100.0},
    ]}
    result = runtime.close_position(position, route, now, reason="test_close")
    assert result["decision"] == "closed"
    assert result["paper_confirmed_funding_pnl"] == pytest.approx(0.5)


def test_confirmed_funding_pnl_excludes_non_funding_events(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    pid = "fc-pnl-exclude"
    store.upsert_funding_capture_position({
        "position_id": pid, "canonical_asset": "BTC",
        "long_venue": "binance", "long_symbol": "BTCUSDT",
        "short_venue": "bybit", "short_symbol": "BTCUSDT",
        "quantity": 5.0, "target_notional": 500.0, "state": "OPEN",
        "opened_at": NOW_BEFORE.isoformat(), "paper_open_fees": 0.50,
        "config": {"route_key": "BTC:binance:bybit", "entry_legs": []},
    })
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "binance", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="binance", event_type="funding", cash_delta=-0.5,
    ))
    store.upsert_paper_event_ledger(make_ledger_entry(
        funding_event_key(pid, "bybit", SCHEDULED), position_id=pid,
        cycle_id=f"{pid}:1", venue="bybit", event_type="funding", cash_delta=0.75,
    ))
    store.upsert_paper_event_ledger(make_ledger_entry(
        f"order_fee:{pid}:entry:long", position_id=pid,
        cycle_id=f"{pid}:1", venue="binance", event_type="order_fee", cash_delta=-0.25,
    ))
    config = PaperBotConfig(telegram_enabled=False).validated()
    runtime = SynchronizedFundingRuntimeV2(
        store=store, config=config, clock=FakeClock(NOW_AT_SETTLEMENT),
        observations_by_route={},
    )
    assert runtime.confirmed_funding_pnl_for_position(pid) == pytest.approx(0.25)


def test_paperbot_calls_reconciliation_worker_on_cadence(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    clock = FakeClock(now, monotonic_start=100.0)
    config = PaperBotConfig(telegram_enabled=False, iterations=1).validated()
    bot = PaperBot(store, config, clock=clock, settlement_data_provider=provider)
    result1 = bot._maybe_run_reconciliation()
    assert result1 is not None
    assert result1["reconciled"] == 2
    result2 = bot._maybe_run_reconciliation()
    assert result2 is None
    clock.advance(11.0)
    result3 = bot._maybe_run_reconciliation()
    assert result3 is not None
    assert result3["processed"] == 0


def test_paperbot_run_iteration_includes_reconciliation(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    clock = FakeClock(now, monotonic_start=100.0)
    config = PaperBotConfig(
        telegram_enabled=False, iterations=1, focused_recheck_enabled=False,
    ).validated()
    bot = PaperBot(store, config, clock=clock, settlement_data_provider=provider)
    monkeypatch.setattr(bot, "collect_background_full_scan_result", lambda: None)
    monkeypatch.setattr(bot, "background_full_scan_running", lambda: False)
    monkeypatch.setattr(bot, "maybe_start_background_full_scan", lambda: False)
    monkeypatch.setattr(bot, "_run_lightweight_discovery", lambda: None)
    monkeypatch.setattr(bot, "run_full_iteration", lambda: {"mode": "fake_full_iteration"})
    monkeypatch.setattr(bot, "process_open_positions", lambda: [])
    monkeypatch.setattr(bot, "process_entry_candidates", lambda *args, **kwargs: [])
    result = bot.run_iteration()
    assert "reconciliation" in result
    assert result["reconciliation"]["reconciled"] == 2


def test_rate_confirmed_without_mark_no_cashflow(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    runtime = _make_runtime(store, now, provider)
    result = runtime.process_pending_reconciliations(now)
    assert result["rate_confirmed"] == 2
    assert result["reconciled"] == 0
    ledger = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger if r["event_type"] == "funding"]) == 0
    for row in store.funding_settlement_reconciliation_rows("fc-recon-test"):
        assert row["status"] == "PUBLIC_RATE_CONFIRMED"
        assert row["confirmed_funding_rate"] == pytest.approx(0.001)
        assert row["funding_pnl"] is None


def test_predicted_rate_near_settlement_is_ignored(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.001,
                "rate_semantics": "predicted_next",
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.001,
                "rate_semantics": "predicted_next",
            },
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    runtime = _make_runtime(store, NOW_AT_SETTLEMENT + timedelta(seconds=30), provider)

    result = runtime.process_pending_reconciliations(NOW_AT_SETTLEMENT + timedelta(seconds=30))

    assert result["processed"] == 0
    assert result["reconciled"] == 0
    assert len([r for r in store.paper_event_ledger_rows("fc-recon-test") if r["event_type"] == "funding"]) == 0
    for row in store.funding_settlement_reconciliation_rows("fc-recon-test"):
        assert row["status"] == "PENDING"
        assert row["rate_status"] == "UNACCEPTED_SEMANTICS"
        assert row["evidence"]["ignored_public_event"]["rate_semantics"] == "predicted_next"


def test_current_rate_is_not_realized_fallback(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.001,
                "rate_semantics": "current_estimate",
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.001,
                "rate_semantics": "current_estimate",
            },
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    runtime = _make_runtime(store, NOW_AT_SETTLEMENT + timedelta(seconds=30), provider)

    result = runtime.process_pending_reconciliations(NOW_AT_SETTLEMENT + timedelta(seconds=30))

    assert result["processed"] == 0
    assert result["reconciled"] == 0
    assert all(row["status"] == "PENDING" for row in store.funding_settlement_reconciliation_rows("fc-recon-test"))
    assert len([r for r in store.paper_event_ledger_rows("fc-recon-test") if r["event_type"] == "funding"]) == 0


def test_missing_rate_remains_pending_not_zero(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "rate_semantics": "realized_settlement",
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": None,
                "rate_semantics": "realized_settlement",
            },
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    runtime = _make_runtime(store, NOW_AT_SETTLEMENT + timedelta(seconds=30), provider)

    result = runtime.process_pending_reconciliations(NOW_AT_SETTLEMENT + timedelta(seconds=30))

    assert result["processed"] == 0
    assert result["reconciled"] == 0
    for row in store.funding_settlement_reconciliation_rows("fc-recon-test"):
        assert row["status"] == "PENDING"
        assert row["rate_status"] == "MISSING"
        assert row["confirmed_funding_rate"] is None


def test_realized_zero_rate_reconciles_as_zero(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store, quantity=5.0)
    provider = FakeFundingSettlementDataProvider(
        public_events=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.0,
                "rate_semantics": "realized_settlement",
            },
            {
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "scheduled_at": SCHEDULED,
                "funding_rate": 0.0,
                "rate_semantics": "realized_settlement",
            },
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    runtime = _make_runtime(store, NOW_AT_SETTLEMENT + timedelta(seconds=30), provider)

    result = runtime.process_pending_reconciliations(NOW_AT_SETTLEMENT + timedelta(seconds=30))

    assert result["reconciled"] == 2
    for row in store.funding_settlement_reconciliation_rows("fc-recon-test"):
        assert row["status"] == "RATE_AND_MARK_RECONCILED"
        assert row["confirmed_funding_rate"] == pytest.approx(0.0)
        assert row["funding_pnl"] == pytest.approx(0.0)
    funding_rows = [r for r in store.paper_event_ledger_rows("fc-recon-test") if r["event_type"] == "funding"]
    assert len(funding_rows) == 2
    assert sum(float(r["cash_delta"]) for r in funding_rows) == pytest.approx(0.0)


def test_public_rate_confirmed_retries_with_stored_rate_when_mark_arrives(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_closed_pending_position(store, quantity=5.0)
    first_provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": SCHEDULED, "funding_rate": 0.001},
        ],
        mark_snapshots=[],
    )
    now = NOW_AT_SETTLEMENT + timedelta(seconds=30)
    first_runtime = _make_runtime(store, now, first_provider)
    assert first_runtime.process_pending_reconciliations(now)["rate_confirmed"] == 2

    second_provider = FakeFundingSettlementDataProvider(
        public_events=[],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": SCHEDULED, "mark_price": 100.0},
        ],
    )
    second_runtime = _make_runtime(store, now + timedelta(seconds=20), second_provider)
    result = second_runtime.process_pending_reconciliations(now + timedelta(seconds=20))
    assert result["reconciled"] == 2
    ledger = store.paper_event_ledger_rows("fc-recon-test")
    assert len([r for r in ledger if r["event_type"] == "funding"]) == 2


def test_stored_provider_uses_funding_history_and_market_snapshots(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_instrument(store)
    store.upsert_funding_history([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_at": SCHEDULED,
            "funding_rate": 0.001,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": 0.001 / 8.0,
            "mark_price": 100.0,
            "observed_at": SCHEDULED,
            "raw": {},
        }
    ])
    scan_id = store.start_funding_scan({"scan_mode": "test"})
    store.insert_funding_market_snapshots(
        scan_id,
        [
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "canonical_asset": "BTC",
                "funding_rate": 0.001,
                "funding_interval_hours": 8.0,
                "hourly_funding_rate": 0.001 / 8.0,
                "funding_rate_kind": "next_settlement",
                "next_funding_at": SCHEDULED,
                "mark_price": 101.0,
                "index_price": 101.0,
                "open_interest_usd": 10_000_000.0,
                "volume_24h_usd": 50_000_000.0,
                "observed_at": SCHEDULED,
                "raw": {},
            }
        ],
    )
    provider = StoredFundingSettlementDataProvider(store)
    scheduled = datetime.fromisoformat(SCHEDULED)
    event = provider.get_public_funding_event("binance", "BTCUSDT", scheduled, 120.0)
    mark = provider.get_nearest_mark_snapshot("binance", "BTCUSDT", scheduled, 2.0)
    assert event is not None
    assert event["funding_rate"] == pytest.approx(0.001)
    assert mark is not None
    assert mark["mark_price"] == pytest.approx(101.0)
    assert mark["reconciliation_quality"] == "INGESTION_TIME_FALLBACK"


def test_stored_provider_ignores_predicted_history_rate(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_instrument(store)
    store.upsert_funding_history([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_at": SCHEDULED,
            "funding_rate": 0.001,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": 0.001 / 8.0,
            "mark_price": 100.0,
            "observed_at": SCHEDULED,
            "raw": {"rate_semantics": "predicted_next"},
        }
    ])
    provider = StoredFundingSettlementDataProvider(store)
    scheduled = datetime.fromisoformat(SCHEDULED)

    event = provider.get_public_funding_event("binance", "BTCUSDT", scheduled, 120.0)

    assert event is None


def test_stored_provider_falls_back_to_funding_history_mark(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    _seed_instrument(store)
    store.upsert_funding_history([
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "funding_at": SCHEDULED,
            "funding_rate": 0.001,
            "funding_interval_hours": 8.0,
            "hourly_funding_rate": 0.001 / 8.0,
            "mark_price": 100.0,
            "observed_at": SCHEDULED,
            "raw": {},
        }
    ])
    provider = StoredFundingSettlementDataProvider(store)
    scheduled = datetime.fromisoformat(SCHEDULED)
    mark = provider.get_nearest_mark_snapshot("binance", "BTCUSDT", scheduled, 2.0)
    assert mark is not None
    assert mark["mark_price"] == pytest.approx(100.0)
    assert mark["source"] == "funding_rate_history"


def test_open_position_poll_records_current_executable_pnl(tmp_path):
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    now = datetime(2026, 7, 28, 15, 55, 0, tzinfo=UTC)
    pid = "fc-current-pnl"
    route_key = "BTC:binance:bybit"
    store.upsert_funding_capture_position({
        "position_id": pid,
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "quantity": 5.0,
        "target_notional": 500.0,
        "state": "OPEN",
        "opened_at": (now - timedelta(seconds=30)).isoformat(),
        "paper_open_fees": 0.50,
        "original_entry_spread": 0.0,
        "config": {"route_key": route_key, "entry_legs": [
            {"side": "long", "venue": "binance", "symbol": "BTCUSDT", "entry_fill_price": 100.0, "fee_rate": 0.0005},
            {"side": "short", "venue": "bybit", "symbol": "BTCUSDT", "entry_fill_price": 100.0, "fee_rate": 0.0005},
        ]},
    })
    store.upsert_funding_capture_cycle({
        "position_id": pid,
        "cycle_number": 1,
        "scheduled_funding_at": (now + timedelta(minutes=5)).isoformat(),
        "state": "OPEN",
    })
    for venue in ("binance", "bybit"):
        amount = 625.0
        store.upsert_paper_event_ledger(make_ledger_entry(
            collateral_reserve_event_key(pid, venue),
            position_id=pid,
            venue=venue,
            event_type="collateral_reserve",
            cash_delta=0.0,
            payload={"amount": amount},
        ))
        store.update_funding_paper_account_reserved(venue, amount)
    clock = FakeClock(now, monotonic_start=100.0)
    bot = PaperBot(store, PaperBotConfig(telegram_enabled=False).validated(), clock=clock)
    bot.hot_routes[route_key] = {
        "route_key": route_key,
        "canonical_asset": "BTC",
        "observed_at": now.isoformat(),
        "legs": [
            {
                "side": "long",
                "venue": "binance",
                "symbol": "BTCUSDT",
                "mark_price": 100.0,
                "index_price": 100.0,
                "best_bid": 99.9,
                "best_ask": 100.1,
                "close_vwap": 99.9,
                "bids": [[99.9, 20.0]],
                "asks": [[100.1, 20.0]],
                "fee_rate": 0.0005,
                "response_received_at": (now - timedelta(milliseconds=500)).isoformat(),
                "orderbook_response_received_at": (now - timedelta(milliseconds=500)).isoformat(),
            },
            {
                "side": "short",
                "venue": "bybit",
                "symbol": "BTCUSDT",
                "mark_price": 100.0,
                "index_price": 100.0,
                "best_bid": 99.9,
                "best_ask": 100.1,
                "close_vwap": 100.1,
                "bids": [[99.9, 20.0]],
                "asks": [[100.1, 20.0]],
                "fee_rate": 0.0005,
                "response_received_at": now.isoformat(),
                "orderbook_response_received_at": now.isoformat(),
            },
        ],
    }
    outcomes = bot.process_synchronized_open_positions()
    assert outcomes == []
    position = store.funding_capture_position_by_id(pid)
    assert position["paper_net_pnl_estimated"] == pytest.approx(-2.0)
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT phase, paper_net_if_exit_now
            FROM funding_capture_observations
            WHERE position_id = ?
            """,
            (pid,),
        ).fetchone()
    assert row is not None
    assert row["phase"] == "current_pnl"
    assert row["paper_net_if_exit_now"] == pytest.approx(-2.0)


def test_no_hardcoded_confirmed_funding_zero_in_runtime():
    import inspect
    from smart_money_radar.paper_bot import runtime_v2
    source = inspect.getsource(runtime_v2)
    assert "confirmed_funding_pnl=0.0" not in source
