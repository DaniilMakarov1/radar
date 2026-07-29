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

from smart_money_radar.funding.trader import PaperBot, PaperBotConfig
from smart_money_radar.paper_bot.clock import FakeClock
from smart_money_radar.paper_bot.helpers import route_entry_key
from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
from smart_money_radar.paper_bot.settlement import FakeFundingSettlementDataProvider
from smart_money_radar.storage import SQLiteStore


def trusted_fee_evidence(venue: str, observed_at: str) -> dict[str, Any]:
    return {
        "source_kind": "configured_trusted_fee",
        "source_identifier": f"fixture:{venue}:fees:v1",
        "trust_status": "CONFIGURED_TRUSTED",
        "venue": venue,
        "liquidity_role": "taker",
        "observed_at": observed_at,
        "reviewed_at": observed_at,
        "environment": "mainnet",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "applicability": "taker",
        "evidence_version": "fixture-fee-evidence-v1",
    }


def trusted_fields(venue: str, observed_at: str) -> dict[str, Any]:
    return {
        "environment_verified": True,
        "endpoint_base_url": f"https://api.{venue}.fixture",
        "endpoint_identity_provenance": f"fixture:{venue}:endpoint:v1",
        "endpoint_client_version": "fixture-client-v1",
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
        "settlement_verification_level": "fixture_contract",
        "fee_source": "configured_trusted_fee",
        "fee_evidence": trusted_fee_evidence(venue, observed_at),
        "fee_observed_at": observed_at,
        "fee_reviewed_at": observed_at,
    }


def route(now: datetime, *, lead_seconds: float, route_key: str = "BTC:binance:bybit") -> dict[str, Any]:
    next_at = now + timedelta(seconds=lead_seconds)
    observed = now.astimezone(UTC).isoformat()
    return {
        "status": "watch",
        "route_key": route_key,
        "canonical_asset": "BTC",
        "target_notional": 500.0,
        "observed_at": observed,
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
                **trusted_fields("binance", observed),
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
                "taker_fee_rate": 0.0005,
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
                "timing_policy_source": "fixture_binance_hourly",
                "response_received_at": observed,
                "source_event_at": observed,
                "orderbook_response_received_at": observed,
                "asks": [[100.02, 20.0]],
                "bids": [[99.98, 20.0]],
            },
            {
                "side": "short",
                "venue": "bybit",
                "environment": "mainnet",
                **trusted_fields("bybit", observed),
                "canonical_asset": "BTC",
                "symbol": "BTCUSDT",
                "next_funding_at": next_at.isoformat(),
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
                "taker_fee_rate": 0.0005,
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
                "timing_policy_source": "fixture_bybit_hourly",
                "response_received_at": observed,
                "source_event_at": observed,
                "orderbook_response_received_at": observed,
                "asks": [[100.02, 20.0]],
                "bids": [[99.98, 20.0]],
            },
        ],
        "evidence": {"scenario": "deterministic_synchronized_funding_paper_mvp"},
    }


def observation(at: datetime, settlement_at: datetime, *, phase: str = "entry") -> dict[str, Any]:
    return {
        "phase": phase,
        "observed_at": at.isoformat(),
        "long_response_received_at": at.isoformat(),
        "short_response_received_at": (at + timedelta(milliseconds=500)).isoformat(),
        "long_age_seconds": 0.5,
        "short_age_seconds": 0.5,
        "cross_venue_skew_seconds": 0.5,
        "long_mark": 100.0,
        "short_mark": 100.0,
        "long_index": 100.0,
        "short_index": 100.0,
        "long_next_funding_at": settlement_at.isoformat(),
        "short_next_funding_at": settlement_at.isoformat(),
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


class SnapshotClient:
    thread_safe = True

    def __init__(self, venue: str, route_provider) -> None:
        self.venue = venue
        self._route_provider = route_provider

    def _leg(self) -> dict[str, Any]:
        for leg in self._route_provider().get("legs") or []:
            if str(leg.get("venue") or "").lower() == self.venue:
                return dict(leg)
        raise RuntimeError(f"missing leg for {self.venue}")

    def market_snapshot(self, symbol: str, asset: str, observed_at: str, previous: dict[str, Any]) -> dict[str, Any]:
        leg = self._leg()
        leg.update(
            {
                "venue": self.venue,
                "symbol": symbol,
                "canonical_asset": asset,
                "response_received_at": observed_at,
                "source_event_at": observed_at,
                "orderbook_response_received_at": observed_at,
            }
        )
        return leg

    def orderbook(self, symbol: str, observed_at: str, limit: int = 100) -> dict[str, Any]:
        leg = self._leg()
        return {
            "venue": self.venue,
            "symbol": symbol,
            "observed_at": observed_at,
            "bids": leg["bids"],
            "asks": leg["asks"],
            "best_bid": leg["best_bid"],
            "best_ask": leg["best_ask"],
            "mid_price": leg["mark_price"],
            "response_received_at": observed_at,
            "orderbook_event_time": observed_at,
        }


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, sort_keys=True))


def main() -> None:
    start = datetime(2026, 7, 28, 15, 59, 30, tzinfo=UTC)
    db_path = Path(tempfile.gettempdir()) / "radar_synchronized_funding_mvp.sqlite"
    if db_path.exists():
        db_path.unlink()
    store = SQLiteStore(db_path)
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    clock = FakeClock(start)
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
    current_route = route(start, lead_seconds=30)
    route_holder = {"route": current_route}
    bot.build_venue_clients = lambda: [
        SnapshotClient("binance", lambda: route_holder["route"]),
        SnapshotClient("bybit", lambda: route_holder["route"]),
    ]
    settlement_at = start + timedelta(seconds=30)
    bot.v2_observations_by_route[route_entry_key(current_route)] = [
        observation(start - timedelta(seconds=22.0 - index * (21.0 / 9.0)), settlement_at)
        for index in range(10)
    ]
    capture_id = capture_position_id_for_route(current_route)

    opened = bot.process_entry_candidates([current_route], recheck_before_open=False)
    assert opened == [capture_id], opened
    position = store.funding_capture_position_by_id(capture_id)
    assert position and position["state"] == "OPEN"
    emit(
        "armed",
        candidate_id=capture_id,
        gate_passed=bool((position["config"] or {}).get("entry_gate_result", {}).get("passed")),
        plan_generation=(position["config"] or {}).get("plan_generation"),
    )
    emit("open", candidate_id=capture_id, state=position["state"], quantity=position["quantity"])

    clock.advance(31)
    route_holder["route"] = route(clock.now(), lead_seconds=3600, route_key=current_route["route_key"])
    boundary_outcomes = bot.process_open_positions()
    assert boundary_outcomes == ["settlement_crossed"], boundary_outcomes
    rows = store.funding_settlement_reconciliation_rows(capture_id)
    assert len(rows) == 2
    assert {row["evidence"]["lifecycle_state"] for row in rows} == {"BOUNDARY_CROSSED"}
    assert not [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "funding"]
    emit("boundary", position_id=capture_id, outcomes=boundary_outcomes, obligations=len(rows))

    for offset in (5, 6, 31):
        target = settlement_at + timedelta(seconds=offset)
        clock.advance((target - clock.now()).total_seconds())
        route_holder["route"] = route(clock.now(), lead_seconds=3600, route_key=current_route["route_key"])
        outcomes = bot.process_open_positions()
        if outcomes:
            emit("post_boundary_iteration", offset_seconds=offset, outcomes=outcomes)

    position = store.funding_capture_position_by_id(capture_id)
    replans = (position["config"] or {}).get("post_settlement_replans") or []
    assert replans and replans[-1]["plan_generation"] == 2
    assert position["state"] == "CLOSED_PENDING_RECONCILIATION"
    emit(
        "replan_close",
        position_id=capture_id,
        plan_generation=replans[-1]["plan_generation"],
        next_settlement=replans[-1]["new_scheduled_funding_at"],
        state=position["state"],
    )

    bot.synchronized_runtime.settlement_data_provider = FakeFundingSettlementDataProvider(
        public_events=[
            {"venue": "binance", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": -0.008},
            {"venue": "bybit", "symbol": "BTCUSDT", "scheduled_at": settlement_at.isoformat(), "funding_rate": 0.010},
        ],
        mark_snapshots=[
            {"venue": "binance", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
            {"venue": "bybit", "symbol": "BTCUSDT", "observed_at": settlement_at.isoformat(), "mark_price": 100.0},
        ],
    )
    reconciliation = bot.synchronized_runtime.process_pending_reconciliations(clock.now())
    assert reconciliation["reconciled"] == 2, reconciliation
    repeat = bot.synchronized_runtime.process_pending_reconciliations(clock.now())
    assert repeat["processed"] == 0, repeat
    funding_rows = [row for row in store.paper_event_ledger_rows(capture_id) if row["event_type"] == "funding"]
    assert len(funding_rows) == 2
    final_position = store.funding_capture_position_by_id(capture_id)
    assert final_position["state"] == "RECONCILED"
    emit(
        "reconcile",
        position_id=capture_id,
        reconciled=reconciliation["reconciled"],
        funding_ledger_entries=len(funding_rows),
        final_state=final_position["state"],
        db_path=str(db_path),
    )


if __name__ == "__main__":
    main()
