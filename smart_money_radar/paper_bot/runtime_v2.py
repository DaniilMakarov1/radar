from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.fees import fee_evidence_status
from smart_money_radar.funding.strategy_synchronized_funding import (
    EventWindowPlannerConfig,
    FundingRoutePlan,
    FundingSettlementPlanner,
    STRATEGY_NAME,
    STRATEGY_VERSION,
    entry_underwriting,
    gross_funding_pnl,
    hold_economics,
    initial_entry_economics,
    parse_time,
    summarize_funding_observations,
    validate_focused_observation,
)
from smart_money_radar.paper_bot.accounting import executable_paper_pnl
from smart_money_radar.paper_bot.accounting import (
    collateral_reserve_event_key,
    collateral_release_event_key,
    funding_event_key,
    make_ledger_entry,
    order_fee_event_key,
    price_pnl_event_key,
)
from smart_money_radar.paper_bot.cycle_manager import (
    evaluate_hold_history_reliability,
    next_cycle_observation_decision,
    next_cycle_schedule_decision,
    post_settlement_probe_decision,
    wait_bucket_for_seconds,
)
from smart_money_radar.paper_bot.execution import (
    EXECUTION_HAIRCUT_FRACTION,
    entry_fill_state,
    simulate_marketable_ioc,
    t20_deadline_passed,
)
from smart_money_radar.paper_bot.helpers import leg_by_side, optional_float, route_entry_key
from smart_money_radar.paper_bot.risk import (
    dynamic_basis_stop_decision,
    entry_risk_gates,
    hard_risk_triggered,
)
from smart_money_radar.paper_bot.settlement import (
    REALIZED_FUNDING_RATE_SOURCES,
    funding_rate_semantics,
    funding_rate_semantics_source,
    reconcile_leg,
    realized_public_funding_rate,
)
from smart_money_radar.storage import SQLiteStore

OBSERVATION_MIN_COUNT = 10
OBSERVATION_MIN_SPAN_SECONDS = 20.0
ENTRY_OBSERVATION_TTL_SECONDS = 900.0
POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS = 20.0
POST_SETTLEMENT_HOLD_DECISION_SECONDS = 30.0
POST_SETTLEMENT_PROBE_START_SECONDS = 5.0
POST_SETTLEMENT_PROBE_END_SECONDS = 15.0
POST_SETTLEMENT_CLOSE_AT_T20_SECONDS = 20.0
POST_SETTLEMENT_EVALUATION_START_SECONDS = 5.0


def schedule_seconds_to_next(probe_decision: dict[str, Any], now: datetime) -> float | None:
    next_at = parse_time(probe_decision.get("next_cycle_at"))
    if next_at is None:
        return None
    return max(0.0, (next_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds())


def synchronized_runtime_enabled(config: Any) -> bool:
    return STRATEGY_NAME in set(str(item) for item in getattr(config, "strategy_set", ()))


def capture_position_id_for_route(route: dict[str, Any]) -> str:
    digest = hashlib.sha256(route_entry_key(route).encode("utf-8")).hexdigest()[:16]
    return f"fc-{digest}"


def route_next_settlement(route: dict[str, Any]) -> datetime | None:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_next = parse_time(long_leg.get("next_funding_at"))
    short_next = parse_time(short_leg.get("next_funding_at"))
    if long_next is None or short_next is None:
        return None
    return min(long_next.astimezone(UTC), short_next.astimezone(UTC))


def _leg_response_time(leg: dict[str, Any]) -> datetime | None:
    for key in (
        "orderbook_response_received_at",
        "response_received_at",
        "market_response_received_at",
    ):
        parsed = parse_time(leg.get(key))
        if parsed is not None:
            return parsed.astimezone(UTC)
    return None


def _leg_mark(leg: dict[str, Any]) -> float:
    return float(
        optional_float(leg.get("mark_price"))
        or optional_float(leg.get("vwap"))
        or optional_float(leg.get("open_vwap"))
        or 0.0
    )


def _leg_rate(leg: dict[str, Any]) -> float:
    return float(optional_float(leg.get("normalized_next_funding_rate")) or 0.0)


def _leg_fee_rate(leg: dict[str, Any]) -> float:
    return float(
        optional_float(leg.get("fee_rate"))
        if optional_float(leg.get("fee_rate")) is not None
        else optional_float(leg.get("taker_fee_rate")) or 0.0
    )


def _synthetic_levels(price: float, quantity: float) -> list[list[float]]:
    if price <= 0 or quantity <= 0:
        return []
    return [[price, quantity / EXECUTION_HAIRCUT_FRACTION]]


def _observation_bucket(route_key: str, phase: str, cycle_id: str) -> str:
    if phase == "entry":
        return route_key
    return f"{route_key}:{phase}:{cycle_id}"


def _observation_timestamp_match(
    observation: dict[str, Any],
    route: dict[str, Any],
) -> bool:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    observed_long = parse_time(observation.get("long_next_funding_at"))
    observed_short = parse_time(observation.get("short_next_funding_at"))
    current_long = parse_time(long_leg.get("next_funding_at"))
    current_short = parse_time(short_leg.get("next_funding_at"))
    if (
        observed_long is None
        or observed_short is None
        or current_long is None
        or current_short is None
    ):
        return False
    return (
        abs((observed_long - current_long).total_seconds()) <= 1.0
        and abs((observed_short - current_short).total_seconds()) <= 1.0
    )


def _position_attempt_id(position: dict[str, Any]) -> str | None:
    config = position.get("config") or {}
    attempt_id = config.get("entry_attempt_id") or config.get("attempt_id")
    return str(attempt_id) if attempt_id else None


def build_focused_observation(
    route: dict[str, Any],
    *,
    now: datetime,
    phase: str = "entry",
    max_age_seconds: float = 5.0,
    max_response_skew_seconds: float = 1.0,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_response = _leg_response_time(long_leg)
    short_response = _leg_response_time(short_leg)
    long_mark = _leg_mark(long_leg)
    short_mark = _leg_mark(short_leg)
    quantity = min(
        float(long_leg.get("base_quantity") or route.get("base_quantity") or 0.0),
        float(short_leg.get("base_quantity") or route.get("base_quantity") or 0.0),
    )
    if quantity <= 0 and long_mark > 0 and short_mark > 0:
        quantity = min(
            float(route.get("target_notional") or 500.0) / long_mark,
            float(route.get("target_notional") or 500.0) / short_mark,
        )
    gross = (
        gross_funding_pnl(
            quantity=quantity,
            long_mark=long_mark,
            short_mark=short_mark,
            long_funding_rate=_leg_rate(long_leg),
            short_funding_rate=_leg_rate(short_leg),
        )
        if quantity > 0 and long_mark > 0 and short_mark > 0
        else 0.0
    )
    long_age = (
        (now.astimezone(UTC) - long_response).total_seconds()
        if long_response is not None
        else math.inf
    )
    short_age = (
        (now.astimezone(UTC) - short_response).total_seconds()
        if short_response is not None
        else math.inf
    )
    cross_skew = (
        abs((long_response - short_response).total_seconds())
        if long_response is not None and short_response is not None
        else math.inf
    )
    evidence = route.get("evidence") or {}
    capability_reasons = list(evidence.get("capability_rejections") or [])
    targeted_refresh = evidence.get("targeted_refresh") or {}
    capabilities_passed = (
        bool(evidence.get("synchronized_capability_passed"))
        or (phase == "hold" and targeted_refresh.get("quality") == "FRESH")
    ) and not capability_reasons
    long_open = optional_float(long_leg.get("open_vwap")) or optional_float(long_leg.get("vwap"))
    short_open = optional_float(short_leg.get("open_vwap")) or optional_float(short_leg.get("vwap"))
    long_close = optional_float(long_leg.get("close_vwap")) or optional_float(long_leg.get("best_bid"))
    short_close = optional_float(short_leg.get("close_vwap")) or optional_float(short_leg.get("best_ask"))
    long_book_executable = long_close is not None if phase == "hold" else long_open is not None and long_close is not None
    short_book_executable = short_close is not None if phase == "hold" else short_open is not None and short_close is not None
    raw = {
        "phase": phase,
        "observed_at": now.astimezone(UTC).isoformat(),
        "route_snapshot_id": targeted_refresh.get("snapshot_id"),
        "route_snapshot_quality": targeted_refresh.get("quality"),
        "long_response_received_at": long_response.isoformat() if long_response else None,
        "short_response_received_at": short_response.isoformat() if short_response else None,
        "long_age_seconds": long_age,
        "short_age_seconds": short_age,
        "cross_venue_skew_seconds": cross_skew,
        "cross_venue_skew_ms": None if not math.isfinite(cross_skew) else cross_skew * 1000.0,
        "long_mark": long_mark,
        "short_mark": short_mark,
        "long_index": optional_float(long_leg.get("index_price")),
        "short_index": optional_float(short_leg.get("index_price")),
        "long_next_funding_at": long_leg.get("next_funding_at"),
        "short_next_funding_at": short_leg.get("next_funding_at"),
        "long_next_funding_rate": _leg_rate(long_leg),
        "short_next_funding_rate": _leg_rate(short_leg),
        "gross_funding_pnl": gross,
        "long_open_vwap": long_open,
        "short_open_vwap": short_open,
        "long_close_vwap": long_close,
        "short_close_vwap": short_close,
        "current_exit_spread": (
            float(short_close) - float(long_close)
            if long_close is not None and short_close is not None
            else None
        ),
        "paper_net_if_exit_now": None,
        "long_book_executable": long_book_executable,
        "short_book_executable": short_book_executable,
        "capabilities_passed": capabilities_passed,
    }
    validation = validate_focused_observation(
        raw,
        now=now,
        max_age_seconds=max_age_seconds,
        max_response_skew_seconds=max_response_skew_seconds,
    )
    raw["snapshot_valid"] = bool(validation["valid"])
    raw["invalid_reason"] = ",".join(validation["reasons"]) if validation["reasons"] else None
    return raw


RECONCILIATION_TIMEOUT_SECONDS = 600.0
RECONCILIATION_TOLERANCE_SECONDS = 120.0
RECONCILIATION_MARK_MAX_DISTANCE_SECONDS = 2.0


class SynchronizedFundingRuntimeV2:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: Any,
        clock: Any,
        observations_by_route: dict[str, list[dict[str, Any]]],
        settlement_data_provider: Any | None = None,
        planner: FundingSettlementPlanner | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.observations_by_route = observations_by_route
        self.settlement_data_provider = settlement_data_provider
        self.planner = planner or FundingSettlementPlanner(
            EventWindowPlannerConfig(
                max_strategy_hold_seconds=float(
                    getattr(config, "max_strategy_hold_seconds", 180.0)
                ),
                max_gap_between_settlements_seconds=float(
                    getattr(config, "max_gap_between_settlements_seconds", 180.0)
                ),
                entry_safety_buffer_seconds=float(
                    getattr(config, "entry_safety_buffer_seconds", 30.0)
                ),
                exit_safety_buffer_seconds=float(
                    getattr(config, "exit_safety_buffer_seconds", 5.0)
                ),
                settlement_confirmation_timeout_seconds=float(
                    getattr(config, "settlement_confirmation_timeout_seconds", 5.0)
                ),
                entry_slippage_bps=float(
                    getattr(config, "entry_slippage_bps", 1.0)
                ),
                exit_slippage_bps=float(
                    getattr(config, "exit_slippage_bps", 1.0)
                ),
                basis_movement_reserve_bps=float(
                    getattr(config, "basis_movement_reserve_bps", 10.0)
                ),
                execution_failure_reserve_bps=float(
                    getattr(config, "execution_failure_reserve_bps", 1.0)
                ),
                partial_fill_reserve_bps=float(
                    getattr(config, "partial_fill_reserve_bps", 1.0)
                ),
                timing_uncertainty_reserve_bps=float(
                    getattr(config, "timing_uncertainty_reserve_bps", 1.0)
                ),
                operational_reserve_bps=float(
                    getattr(config, "operational_reserve_bps", 1.0)
                ),
            )
        )

    def _cycle_events_for_scheduled_at(
        self,
        route_plan: dict[str, Any] | None,
        scheduled_at: datetime,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        scheduled_utc = scheduled_at.astimezone(UTC)
        for event in list((route_plan or {}).get("included_settlement_events") or []):
            if not isinstance(event, dict):
                continue
            event_time = parse_time(event.get("scheduled_at"))
            if event_time is None:
                continue
            if abs((event_time.astimezone(UTC) - scheduled_utc).total_seconds()) <= 1.0:
                events.append(dict(event))
        return events

    def _event_side(self, event: dict[str, Any], position_or_route: dict[str, Any]) -> str | None:
        leg_id = str(event.get("leg_id") or "").lower()
        if leg_id.endswith(":long") or ":long:" in leg_id:
            return "long"
        if leg_id.endswith(":short") or ":short:" in leg_id:
            return "short"
        venue = str(event.get("venue") or "").lower()
        if venue and venue == str(position_or_route.get("long_venue") or "").lower():
            return "long"
        if venue and venue == str(position_or_route.get("short_venue") or "").lower():
            return "short"
        return None

    def _cycle_plan_snapshot(
        self,
        *,
        position_id: str,
        cycle_id: str,
        cycle_number: int,
        plan_generation: int,
        scheduled_at: datetime,
        route: dict[str, Any],
        route_plan: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        events = self._cycle_events_for_scheduled_at(route_plan, scheduled_at)
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        expected_leg_identities: list[dict[str, Any]] = []
        for event in events:
            side = self._event_side(event, route)
            fallback_leg = long_leg if side == "long" else short_leg if side == "short" else {}
            expected_leg_identities.append(
                {
                    "event_id": event.get("event_id"),
                    "leg_id": event.get("leg_id"),
                    "side": side,
                    "venue": event.get("venue") or fallback_leg.get("venue"),
                    "symbol": event.get("symbol") or fallback_leg.get("symbol"),
                    "scheduled_at": event.get("scheduled_at") or scheduled_at.isoformat(),
                    "rate_per_next_settlement": event.get("rate_per_next_settlement"),
                }
            )
        scheduled_timestamps = sorted(
            {
                str(item.get("scheduled_at") or scheduled_at.astimezone(UTC).isoformat())
                for item in expected_leg_identities
            }
        )
        evidence_versions = [
            str(event.get("evidence_version"))
            for event in events
            if event.get("evidence_version")
        ]
        return {
            "schema_version": 1,
            "cycle_id": cycle_id,
            "position_id": position_id,
            "plan_generation": int(plan_generation),
            "cycle_number": int(cycle_number),
            "route_plan": route_plan or {},
            "included_settlement_events": events,
            "expected_event_count": len(events),
            "expected_venues": sorted(
                {
                    str(item.get("venue") or "")
                    for item in expected_leg_identities
                    if item.get("venue")
                }
            ),
            "expected_symbols": sorted(
                {
                    str(item.get("symbol") or "")
                    for item in expected_leg_identities
                    if item.get("symbol")
                }
            ),
            "expected_sides": sorted(
                {
                    str(item.get("side") or "")
                    for item in expected_leg_identities
                    if item.get("side")
                }
            ),
            "scheduled_funding_timestamps": scheduled_timestamps,
            "scheduled_funding_at": scheduled_at.astimezone(UTC).isoformat(),
            "expected_leg_identities": expected_leg_identities,
            "planned_exit_at": (route_plan or {}).get("planned_exit_at"),
            "monitor_until": (route_plan or {}).get("monitor_until"),
            "captured_at_plan_built_at": now.astimezone(UTC).isoformat(),
            "evidence_version": max(evidence_versions) if evidence_versions else "unknown",
        }

    def _active_cycle_plan_for_cycle(
        self,
        position: dict[str, Any],
        cycle: dict[str, Any],
    ) -> dict[str, Any]:
        plan = cycle.get("active_plan") if isinstance(cycle.get("active_plan"), dict) else {}
        if plan:
            return dict(plan)
        config = dict(position.get("config") or {})
        scheduled = parse_time(cycle.get("scheduled_funding_at"))
        if scheduled is None:
            return {}
        route_plan = dict(config.get("funding_route_plan") or {})
        events = self._cycle_events_for_scheduled_at(route_plan, scheduled)
        if not events:
            return {}
        return {
            "schema_version": 0,
            "cycle_id": cycle.get("cycle_id"),
            "position_id": position.get("position_id"),
            "plan_generation": int(config.get("active_plan_generation") or config.get("plan_generation") or cycle.get("cycle_number") or 0),
            "route_plan": route_plan,
            "included_settlement_events": events,
            "expected_event_count": len(events),
            "scheduled_funding_at": scheduled.astimezone(UTC).isoformat(),
            "scheduled_funding_timestamps": [scheduled.astimezone(UTC).isoformat()],
            "expected_leg_identities": [
                {
                    "event_id": event.get("event_id"),
                    "leg_id": event.get("leg_id"),
                    "side": self._event_side(event, position),
                    "venue": event.get("venue"),
                    "symbol": event.get("symbol"),
                    "scheduled_at": event.get("scheduled_at"),
                    "rate_per_next_settlement": event.get("rate_per_next_settlement"),
                }
                for event in events
            ],
            "planned_exit_at": config.get("planned_exit_at"),
            "monitor_until": config.get("monitor_until"),
            "evidence_version": "legacy_config_snapshot",
        }

    def _record_ledger_entry(self, row: dict[str, Any]) -> str | None:
        if float(row.get("cash_delta") or 0.0) != 0.0 and not row.get("venue"):
            raise ValueError(
                f"cash-affecting paper ledger entry requires venue: {row.get('event_key')}"
            )
        if float(row.get("cash_delta") or 0.0) != 0.0:
            result = self.store.apply_paper_cash_event(row)
            return str(row["event_key"]) if result.get("applied") else None
        return self.store.upsert_paper_event_ledger(row)

    def _record_price_pnl_entries(
        self,
        *,
        position_id: str,
        cycle_id: str,
        long_venue: str,
        short_venue: str,
        long_price_pnl: float,
        short_price_pnl: float,
        payload: dict[str, Any],
        attempt_id: str | None = None,
    ) -> None:
        for side, venue, value in (
            ("long", long_venue, float(long_price_pnl)),
            ("short", short_venue, float(short_price_pnl)),
        ):
            if abs(value) <= 1e-12:
                continue
            self._record_ledger_entry(
                make_ledger_entry(
                    price_pnl_event_key(position_id, venue, attempt_id=attempt_id),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=venue,
                    event_type="price_pnl",
                    cash_delta=value,
                    payload={
                        **payload,
                        "side": side,
                        "price_pnl": value,
                        "attempt_id": attempt_id,
                    },
                )
            )

    def mark_position_data_quality(
        self,
        position: dict[str, Any],
        *,
        state: str,
        now: datetime,
        refresh_result: dict[str, Any] | None = None,
        last_valid_route: dict[str, Any] | None = None,
        executable_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        current_position = (
            self.store.funding_capture_position_by_id(position_id)
            if self.store is not None
            else None
        )
        config = dict((current_position or position).get("config") or {})
        data_quality = dict(config.get("data_quality") or {})
        now_iso = now.astimezone(UTC).isoformat()
        data_quality["state"] = state
        if refresh_result is not None:
            data_quality["last_refresh_attempt_at"] = now_iso
            data_quality["refresh_attempt_count"] = int(
                data_quality.get("refresh_attempt_count") or 0
            ) + 1
        if state == "HEALTHY":
            data_quality.pop("first_degraded_at", None)
        else:
            data_quality.setdefault("first_degraded_at", now_iso)
        if refresh_result is not None:
            data_quality["last_refresh_quality"] = refresh_result.get("quality")
            data_quality["last_refresh_reason"] = refresh_result.get("reason")
            data_quality["last_refresh_snapshot_id"] = refresh_result.get("snapshot_id")
        config["data_quality"] = data_quality
        if last_valid_route is not None:
            config["last_valid_executable_route"] = last_valid_route
        if executable_snapshot is not None:
            config["last_valid_executable_snapshot"] = executable_snapshot
        position["config"] = config
        self.store.update_funding_capture_position_config(position_id, config, now)
        return config

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def confirmed_funding_pnl_for_position(self, position_id: str) -> float:
        """Sum of idempotent reconciled funding ledger entries for a position.

        Only includes RATE_AND_MARK_RECONCILED entries. Excludes PENDING,
        PUBLIC_RATE_CONFIRMED, expected/provisional funding.
        """
        total = 0.0
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("event_type") or "") != "funding":
                continue
            total += float(row.get("cash_delta") or 0.0)
        return total

    def recover_reconciled_funding_effects(self, now: datetime) -> dict[str, Any]:
        repaired = 0
        touched: set[tuple[str, str]] = set()
        for row in self.store.reconciliation_rows_by_status({"RATE_AND_MARK_RECONCILED"}):
            position_id = str(row["position_id"])
            cycle_id = str(row.get("cycle_id") or "")
            if cycle_id:
                touched.add((position_id, cycle_id))
            evidence = dict(row.get("evidence") or {})
            financial_effect = evidence.get("financial_effect") if isinstance(evidence.get("financial_effect"), dict) else {}
            event_key = str(financial_effect.get("event_key") or "")
            position = self.store.funding_capture_position_by_id(position_id) or {}
            attempt_id = _position_attempt_id(position)
            if not event_key:
                event_key = funding_event_key(
                    position_id,
                    str(row["venue"]),
                    str(row["scheduled_funding_at"]),
                    attempt_id=attempt_id,
                )
            ledger_rows = self.store.paper_event_ledger_rows(position_id)
            exists = any(str(item.get("event_key") or "") == event_key for item in ledger_rows)
            if not exists:
                funding_pnl = float(row.get("funding_pnl") or 0.0)
                self.store.apply_reconciled_funding_effect(
                    row,
                    make_ledger_entry(
                        event_key,
                        position_id=position_id,
                        cycle_id=row.get("cycle_id"),
                        venue=row.get("venue"),
                        event_type="funding",
                        cash_delta=funding_pnl,
                        payload={
                            "confirmed_rate": row.get("confirmed_funding_rate"),
                            "settlement_mark": row.get("settlement_mark_price"),
                            "side": row.get("side"),
                            "payment_reconciliation_state": "PAYMENT_RECONCILED",
                            "paper_simulated": True,
                            "not_venue_account_ledger": True,
                            "attempt_id": attempt_id,
                            "recovered_at": now.astimezone(UTC).isoformat(),
                        },
                    ),
                )
                repaired += 1
            validation = self._validate_reconciliation_financial_effect(row)
            if validation.get("diagnostic") == "RECONCILIATION_EFFECT_MISMATCH":
                self._mark_reconciliation_effect_mismatch(row, validation)
        account_report = self.store.paper_account_consistency_report()
        account_repaired = False
        if not account_report.get("ok"):
            account_report = self.store.repair_paper_account_consistency()
            account_repaired = True
        finalized_cycles = 0
        finalized_positions = 0
        for position_id, cycle_id in sorted(touched):
            before_cycle = next(
                (
                    item
                    for item in self.store.funding_capture_cycles_for_position(position_id)
                    if str(item.get("cycle_id") or "") == cycle_id
                ),
                {},
            )
            before_position = self.store.funding_capture_position_by_id(position_id) or {}
            self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
            self._maybe_finalize_position_after_reconciliation(position_id)
            after_cycle = next(
                (
                    item
                    for item in self.store.funding_capture_cycles_for_position(position_id)
                    if str(item.get("cycle_id") or "") == cycle_id
                ),
                {},
            )
            after_position = self.store.funding_capture_position_by_id(position_id) or {}
            if before_cycle.get("state") != after_cycle.get("state") and after_cycle.get("state") in {"RECONCILED", "UNRECONCILED"}:
                finalized_cycles += 1
            if before_position.get("state") != after_position.get("state") and after_position.get("state") in {"RECONCILED", "UNRECONCILED"}:
                finalized_positions += 1
        return {
            "reconciled_funding_effects_repaired": repaired,
            "account_repaired": account_repaired,
            "account_consistency": account_report,
            "finalized_cycles": finalized_cycles,
            "finalized_positions": finalized_positions,
        }

    def _reconciliation_financial_effect_ready(self, row: dict[str, Any]) -> bool:
        validation = self._validate_reconciliation_financial_effect(row)
        if validation.get("ready"):
            return True
        if validation.get("diagnostic") == "RECONCILIATION_EFFECT_MISMATCH":
            self._mark_reconciliation_effect_mismatch(row, validation)
        return False

    def _validate_reconciliation_financial_effect(self, row: dict[str, Any]) -> dict[str, Any]:
        if str(row.get("status") or "") != "RATE_AND_MARK_RECONCILED":
            return {"ready": False, "reason": "reconciliation_not_rate_and_mark"}
        evidence = dict(row.get("evidence") or {})
        financial_effect = evidence.get("financial_effect") if isinstance(evidence.get("financial_effect"), dict) else {}
        event_key = str(financial_effect.get("event_key") or "")
        position_id = str(row["position_id"])
        venue = str(row.get("venue") or "")
        cycle_id = str(row.get("cycle_id") or "")
        scheduled_at = str(row.get("scheduled_funding_at") or "")
        if not event_key:
            position = self.store.funding_capture_position_by_id(position_id) or {}
            event_key = funding_event_key(
                position_id,
                venue,
                scheduled_at,
                attempt_id=_position_attempt_id(position),
            )
        ledger_rows = self.store.paper_event_ledger_rows(position_id)
        ledger = next(
            (item for item in ledger_rows if str(item.get("event_key") or "") == event_key),
            None,
        )
        if ledger is None:
            return {"ready": False, "reason": "funding_ledger_missing", "event_key": event_key}
        mismatches: list[str] = []
        expected_pnl = optional_float(row.get("funding_pnl"))
        actual_delta = optional_float(ledger.get("cash_delta"))
        payload = ledger.get("payload") if isinstance(ledger.get("payload"), dict) else {}
        if str(ledger.get("event_type") or "") != "funding":
            mismatches.append("event_type")
        if str(ledger.get("position_id") or "") != position_id:
            mismatches.append("position_id")
        if str(ledger.get("cycle_id") or "") != cycle_id:
            mismatches.append("cycle_id")
        if str(ledger.get("venue") or "") != venue:
            mismatches.append("venue")
        if expected_pnl is None or actual_delta is None or not math.isclose(
            float(actual_delta),
            float(expected_pnl),
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            mismatches.append("cash_delta")
        payload_side = str(payload.get("side") or "")
        if payload_side and payload_side != str(row.get("side") or ""):
            mismatches.append("side")
        expected_suffix = f":{venue}:{scheduled_at}"
        if not event_key.endswith(expected_suffix):
            mismatches.append("event_key_scheduled_identity")
        financial_event_key = str(financial_effect.get("event_key") or "")
        if financial_event_key and financial_event_key != event_key:
            mismatches.append("financial_effect_event_key")
        financial_delta = optional_float(financial_effect.get("cash_delta"))
        if financial_delta is not None and expected_pnl is not None and not math.isclose(
            float(financial_delta),
            float(expected_pnl),
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            mismatches.append("financial_effect_cash_delta")
        if mismatches:
            return {
                "ready": False,
                "diagnostic": "RECONCILIATION_EFFECT_MISMATCH",
                "event_key": event_key,
                "mismatches": list(dict.fromkeys(mismatches)),
                "expected_cash_delta": expected_pnl,
                "actual_cash_delta": actual_delta,
                "position_id": position_id,
                "cycle_id": cycle_id,
                "venue": venue,
            }
        return {"ready": True, "event_key": event_key}

    def _mark_reconciliation_effect_mismatch(
        self,
        row: dict[str, Any],
        validation: dict[str, Any],
    ) -> None:
        position_id = str(row.get("position_id") or "")
        cycle_id = str(row.get("cycle_id") or "")
        evidence = dict(row.get("evidence") or {})
        evidence["financial_effect_mismatch"] = {
            key: value
            for key, value in validation.items()
            if key not in {"ready"}
        }
        evidence["requires_review"] = True
        self._update_reconciliation_row(
            row,
            status="RECONCILIATION_EFFECT_MISMATCH",
            evidence_update=evidence,
        )
        if cycle_id:
            self.store.update_funding_capture_cycle_state(
                cycle_id,
                "RECONCILIATION_EFFECT_MISMATCH",
                reconciliation_status="RECONCILIATION_EFFECT_MISMATCH",
            )
        position = self.store.funding_capture_position_by_id(position_id) or {}
        if str(position.get("state") or "") == "CLOSED_PENDING_RECONCILIATION":
            config = dict(position.get("config") or {})
            config["lifecycle_state"] = "CLOSED_REQUIRES_REVIEW"
            config["requires_review"] = True
            config["blocker"] = "reconciliation_effect_mismatch"
            self.store.update_funding_capture_position_state(
                position_id,
                "CLOSED_REQUIRES_REVIEW",
            )
            self.store.update_funding_capture_position_config(position_id, config)

    def process_pending_reconciliations(self, now: datetime) -> dict[str, Any]:
        """Process all PENDING reconciliation rows.

        For each PENDING row:
        - public event missing and age <= 600s: remain PENDING
        - public event missing and age > 600s: set UNRECONCILED
        - rate found but nearest mark missing: set PUBLIC_RATE_CONFIRMED, no cashflow
        - rate + mark found: set RATE_AND_MARK_RECONCILED, calculate funding, ledger entry

        Returns summary dict.
        """
        provider = self.settlement_data_provider
        if provider is None:
            return {"processed": 0, "reason": "no_settlement_data_provider"}

        self.recover_reconciled_funding_effects(now)
        pending_rows = self.store.pending_reconciliation_rows()
        if not pending_rows:
            return {"processed": 0}

        processed = 0
        timed_out = 0
        rate_confirmed = 0
        reconciled = 0
        now_utc = now.astimezone(UTC)

        for row in pending_rows:
            position_id = str(row["position_id"])
            venue = str(row["venue"])
            symbol = str(row["symbol"])
            side = str(row["side"])
            scheduled_at_str = str(row["scheduled_funding_at"])
            cycle_id = str(row.get("cycle_id") or "")
            quantity = self._position_quantity(position_id)

            scheduled_at = parse_time(scheduled_at_str)
            if scheduled_at is None:
                continue

            age_seconds = (now_utc - scheduled_at.astimezone(UTC)).total_seconds()

            public_rate = optional_float(row.get("confirmed_funding_rate"))
            public_event: dict[str, Any] | None = None
            if public_rate is not None:
                stored_event = (row.get("evidence") or {}).get("public_event") or {}
                semantics = funding_rate_semantics(stored_event)
                semantics_source = funding_rate_semantics_source(stored_event)
                if (semantics, semantics_source) not in REALIZED_FUNDING_RATE_SOURCES:
                    self._update_reconciliation_row(
                        row,
                        status="PENDING",
                        confirmed_funding_rate=public_rate,
                        rate_status="UNACCEPTED_SEMANTICS",
                        mark_status=row.get("mark_status"),
                        evidence_update={
                            "ignored_public_event": {
                                "rate_semantics": semantics,
                                "rate_semantics_source": semantics_source,
                                "source": "stored_confirmed_rate",
                            }
                        },
                    )
                    public_rate = None
            if public_rate is None:
                public_event = provider.get_public_funding_event(
                    venue=venue,
                    symbol=symbol,
                    scheduled_funding_at=scheduled_at.astimezone(UTC),
                    tolerance_seconds=RECONCILIATION_TOLERANCE_SECONDS,
                )
                if public_event is not None:
                    public_rate = realized_public_funding_rate(public_event)
                    if public_rate is None:
                        self._update_reconciliation_row(
                            row,
                            status="PENDING",
                            rate_status="UNACCEPTED_SEMANTICS"
                            if public_event.get("funding_rate") is not None
                            else "MISSING",
                            mark_status=row.get("mark_status"),
                            evidence_update={
                                "ignored_public_event": {
                                    "rate_semantics": public_event.get("rate_semantics")
                                    or public_event.get("funding_rate_semantics")
                                    or "unclear",
                                    "rate_semantics_source": public_event.get("rate_semantics_source")
                                    or public_event.get("funding_rate_semantics_source")
                                    or "unknown",
                                    "published_at": public_event.get("published_at"),
                                    "skew_seconds": public_event.get("skew_seconds"),
                                    "source": public_event.get("source"),
                                }
                            },
                        )

            if public_rate is None:
                if age_seconds > RECONCILIATION_TIMEOUT_SECONDS:
                    self._update_reconciliation_row(
                        row, status="UNRECONCILED",
                        rate_status="MISSING", mark_status="MISSING",
                    )
                    timed_out += 1
                    processed += 1
                    self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
                    self._maybe_finalize_position_after_reconciliation(position_id)
                continue

            mark_snapshot = provider.get_nearest_mark_snapshot(
                venue=venue,
                symbol=symbol,
                scheduled_funding_at=scheduled_at.astimezone(UTC),
                max_distance_seconds=RECONCILIATION_MARK_MAX_DISTANCE_SECONDS,
            )

            if mark_snapshot is None:
                if age_seconds > RECONCILIATION_TIMEOUT_SECONDS:
                    self._update_reconciliation_row(
                        row,
                        status="UNRECONCILED",
                        confirmed_funding_rate=public_rate,
                        rate_status="CONFIRMED",
                        mark_status="MISSING",
                    )
                    timed_out += 1
                    processed += 1
                    self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
                    self._maybe_finalize_position_after_reconciliation(position_id)
                    continue
                self._update_reconciliation_row(
                    row,
                    status="PUBLIC_RATE_CONFIRMED",
                    confirmed_funding_rate=public_rate,
                    rate_status="CONFIRMED",
                    mark_status="MISSING",
                    evidence_update={
                        "venue_event_state": "VENUE_EVENT_CONFIRMED",
                        "payment_reconciliation_state": "PAYMENT_RECONCILIATION_PENDING",
                        "public_event": {
                            "rate_semantics": public_event.get("rate_semantics")
                            if public_event
                            else "stored_confirmed_rate",
                            "rate_semantics_source": public_event.get("rate_semantics_source")
                            if public_event
                            else None,
                            "published_at": public_event.get("published_at")
                            if public_event
                            else None,
                            "skew_seconds": public_event.get("skew_seconds")
                            if public_event
                            else None,
                        }
                    },
                )
                rate_confirmed += 1
                continue

            mark_price = optional_float(mark_snapshot.get("mark_price"))
            if mark_price is None:
                continue
            leg_result = reconcile_leg(
                public_rate=public_rate,
                public_mark=mark_price,
                side=side,
                quantity=quantity,
            )

            reconciliation_evidence = {
                "venue_event_state": "VENUE_EVENT_CONFIRMED",
                "payment_reconciliation_state": "PAYMENT_RECONCILED",
                "payment_reconciled_at": now_utc.isoformat(),
                "paper_simulated": True,
                "not_venue_account_ledger": True,
                "public_event": {
                    "rate_semantics": public_event.get("rate_semantics")
                    if public_event
                    else "stored_confirmed_rate",
                    "rate_semantics_source": public_event.get("rate_semantics_source")
                    if public_event
                    else None,
                    "published_at": public_event.get("published_at")
                    if public_event
                    else None,
                    "skew_seconds": public_event.get("skew_seconds")
                    if public_event
                    else None,
                },
                "mark_snapshot": {
                    "observed_at": mark_snapshot.get("observed_at"),
                    "skew_seconds": mark_snapshot.get("skew_seconds"),
                    "source": mark_snapshot.get("source"),
                    "timestamp_source": mark_snapshot.get("timestamp_source"),
                    "reconciliation_quality": mark_snapshot.get("reconciliation_quality")
                    or "UNKNOWN",
                },
            }
            updated_row = dict(row)
            updated_row.update(
                {
                    "status": leg_result["status"],
                    "confirmed_funding_rate": leg_result.get("confirmed_funding_rate"),
                    "settlement_mark_price": leg_result.get("settlement_mark_price"),
                    "funding_pnl": leg_result.get("funding_pnl"),
                    "rate_status": leg_result.get("rate_status"),
                    "mark_status": leg_result.get("mark_status"),
                    "evidence": {
                        **dict(row.get("evidence") or {}),
                        **reconciliation_evidence,
                    },
                }
            )

            if leg_result["status"] == "RATE_AND_MARK_RECONCILED":
                funding_pnl = float(leg_result["funding_pnl"] or 0.0)
                position = self.store.funding_capture_position_by_id(position_id) or {}
                attempt_id = _position_attempt_id(position)
                event_key = funding_event_key(
                    position_id,
                    venue,
                    scheduled_at_str,
                    attempt_id=attempt_id,
                )
                self.store.apply_reconciled_funding_effect(
                    updated_row,
                    make_ledger_entry(
                        event_key,
                        position_id=position_id,
                        cycle_id=cycle_id,
                        venue=venue,
                        event_type="funding",
                        cash_delta=funding_pnl,
                        payload={
                            "confirmed_rate": public_rate,
                            "settlement_mark": mark_price,
                            "side": side,
                            "payment_reconciliation_state": "PAYMENT_RECONCILED",
                            "paper_simulated": True,
                            "not_venue_account_ledger": True,
                            "rate_semantics": public_event.get("rate_semantics")
                            if public_event
                            else "stored_confirmed_rate",
                            "rate_semantics_source": public_event.get("rate_semantics_source")
                            if public_event
                            else None,
                            "attempt_id": attempt_id,
                            "reconciliation_quality": mark_snapshot.get("reconciliation_quality")
                            or "UNKNOWN",
                        },
                    ),
                )
                reconciled += 1
            else:
                self._update_reconciliation_row(
                    row,
                    status=leg_result["status"],
                    confirmed_funding_rate=leg_result.get("confirmed_funding_rate"),
                    settlement_mark_price=leg_result.get("settlement_mark_price"),
                    funding_pnl=leg_result.get("funding_pnl"),
                    rate_status=leg_result.get("rate_status"),
                    mark_status=leg_result.get("mark_status"),
                    evidence_update=reconciliation_evidence,
                )
            processed += 1
            self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
            self._maybe_finalize_position_after_reconciliation(position_id)

        return {
            "processed": processed,
            "timed_out": timed_out,
            "rate_confirmed": rate_confirmed,
            "reconciled": reconciled,
        }

    def _position_quantity(self, position_id: str) -> float:
        position = self.store.funding_capture_position_by_id(position_id)
        if position is None:
            return 0.0
        return float(position.get("quantity") or 0.0)

    def _update_reconciliation_row(
        self,
        row: dict[str, Any],
        *,
        status: str,
        confirmed_funding_rate: float | None = None,
        settlement_mark_price: float | None = None,
        funding_pnl: float | None = None,
        rate_status: str | None = None,
        mark_status: str | None = None,
        evidence_update: dict[str, Any] | None = None,
    ) -> None:
        updated = dict(row)
        updated["status"] = status
        if confirmed_funding_rate is not None:
            updated["confirmed_funding_rate"] = confirmed_funding_rate
        if settlement_mark_price is not None:
            updated["settlement_mark_price"] = settlement_mark_price
        if funding_pnl is not None:
            updated["funding_pnl"] = funding_pnl
        if rate_status is not None:
            updated["rate_status"] = rate_status
        if mark_status is not None:
            updated["mark_status"] = mark_status
        if evidence_update:
            evidence = dict(updated.get("evidence") or {})
            evidence.update(evidence_update)
            updated["evidence"] = evidence
        self.store.upsert_funding_settlement_reconciliation(updated)

    def _maybe_finalize_cycle_after_reconciliation(
        self,
        position_id: str,
        cycle_id: str,
    ) -> None:
        if not cycle_id:
            return
        leg_rows = self.store.funding_settlement_reconciliation_rows(
            position_id, cycle_id=cycle_id,
        )
        if not leg_rows:
            return
        position = self.store.funding_capture_position_by_id(position_id) or {}
        cycle = next(
            (
                item
                for item in self.store.funding_capture_cycles_for_position(position_id)
                if str(item.get("cycle_id") or "") == str(cycle_id)
            ),
            {},
        )
        active_plan = self._active_cycle_plan_for_cycle(position, cycle) if cycle else {}
        expected_count = int(active_plan.get("expected_event_count") or 0)
        if expected_count > 0 and len(leg_rows) != expected_count:
            return
        all_resolved = all(
            str(r.get("status") or "") in {
                "RATE_AND_MARK_RECONCILED", "UNRECONCILED",
            }
            for r in leg_rows
        )
        if not all_resolved:
            return
        has_unreconciled = any(
            str(r.get("status") or "") == "UNRECONCILED" for r in leg_rows
        )
        if has_unreconciled:
            self.store.update_funding_capture_cycle_state(
                cycle_id, "UNRECONCILED", reconciliation_status="UNRECONCILED",
            )
            return
        all_reconciled = all(
            str(r.get("status") or "") == "RATE_AND_MARK_RECONCILED"
            for r in leg_rows
        )
        if all_reconciled:
            if not all(self._reconciliation_financial_effect_ready(r) for r in leg_rows):
                return
            if not self.store.paper_account_consistency_report().get("ok"):
                return
            total_funding = sum(float(r.get("funding_pnl") or 0.0) for r in leg_rows)
            self.store.update_funding_capture_cycle_state(
                cycle_id,
                "RECONCILED",
                reconciliation_status="RATE_AND_MARK_RECONCILED",
                reconciled_funding_pnl=total_funding,
            )

    def _maybe_finalize_position_after_reconciliation(
        self,
        position_id: str,
    ) -> None:
        position = self.store.funding_capture_position_by_id(position_id)
        if position is None:
            return
        state = str(position.get("state") or "")
        if state not in ("CLOSED_PENDING_RECONCILIATION",):
            return
        cycles = self.store.funding_capture_cycles_for_position(position_id)
        if not cycles:
            return
        captured_cycles = [
            c for c in cycles
            if str(c.get("state") or "") in {
                "RECONCILED", "UNRECONCILED", "SETTLEMENT_CROSSED",
            }
        ]
        if not captured_cycles:
            return
        all_settled = all(
            str(c.get("state") or "") in {"RECONCILED", "UNRECONCILED"}
            for c in captured_cycles
        )
        if not all_settled:
            return
        has_unreconciled = any(
            str(c.get("state") or "") == "UNRECONCILED" for c in captured_cycles
        )
        if has_unreconciled:
            self.store.update_funding_capture_position_state(
                position_id, "UNRECONCILED",
            )
            return
        total_reconciled_funding = sum(
            float(c.get("reconciled_funding_pnl") or 0.0) for c in captured_cycles
        )
        price_pnl = 0.0
        total_fees = float(position.get("paper_open_fees") or 0.0) + float(position.get("paper_close_fees") or 0.0)
        emergency_cost = float(position.get("paper_emergency_unwind_cost") or 0.0)
        for ledger_row in self.store.paper_event_ledger_rows(position_id):
            if str(ledger_row.get("event_type") or "") == "price_pnl":
                price_pnl += float(ledger_row.get("cash_delta") or 0.0)
        reconciled_net = price_pnl + total_reconciled_funding - total_fees - emergency_cost
        self.store.update_funding_capture_position_state(
            position_id, "RECONCILED",
            paper_net_pnl_reconciled=reconciled_net,
        )

    def record_current_executable_pnl(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Store executable PnL for an open synchronized position.

        This is the runtime poll value used by risk/hold logic. It includes
        confirmed reconciled funding only and never includes expected funding
        or spread-convergence assumptions.
        """
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        snapshot = self._current_executable_pnl_snapshot(position, route, now)
        observed_at = now.astimezone(UTC).isoformat()
        self.store.upsert_funding_capture_observation(
            {
                "observation_id": f"{cycle_id}:current_pnl:{observed_at}",
                "position_id": position_id,
                "cycle_id": cycle_id,
                "phase": "current_pnl",
                "observed_at": observed_at,
                "long_response_received_at": snapshot.get("long_response_received_at"),
                "short_response_received_at": snapshot.get("short_response_received_at"),
                "cross_venue_skew_ms": snapshot.get("cross_venue_skew_ms"),
                "long_mark": snapshot.get("long_mark"),
                "short_mark": snapshot.get("short_mark"),
                "long_index": snapshot.get("long_index"),
                "short_index": snapshot.get("short_index"),
                "long_next_funding_at": snapshot.get("long_next_funding_at"),
                "short_next_funding_at": snapshot.get("short_next_funding_at"),
                "long_next_funding_rate": snapshot.get("long_next_funding_rate"),
                "short_next_funding_rate": snapshot.get("short_next_funding_rate"),
                "gross_funding_pnl": None,
                "long_close_vwap": snapshot.get("long_exit_price"),
                "short_close_vwap": snapshot.get("short_exit_price"),
                "current_exit_spread": snapshot.get("current_exit_spread"),
                "paper_net_if_exit_now": snapshot.get("paper_net_if_exit_now"),
                "snapshot_valid": snapshot.get("quality") == "EXECUTABLE_FULL_DEPTH",
                "invalid_reason": None
                if snapshot.get("quality") == "EXECUTABLE_FULL_DEPTH"
                else snapshot.get("quality"),
            }
        )
        if snapshot.get("quality") != "EXECUTABLE_FULL_DEPTH":
            self.mark_position_data_quality(
                position,
                state="DEGRADED",
                now=now,
                executable_snapshot=snapshot,
            )
            return {"decision": "skipped", **snapshot}
        snapshot_for_config = {
            **snapshot,
            "observed_at": observed_at,
            "position_id": position_id,
            "cycle_id": cycle_id,
        }
        self.mark_position_data_quality(
            position,
            state="HEALTHY",
            now=now,
            last_valid_route=route,
            executable_snapshot=snapshot_for_config,
        )
        self.store.update_funding_capture_position_state(
            position_id,
            str(position.get("state") or "OPEN"),
            now,
            paper_net_pnl_estimated=snapshot["paper_net_if_exit_now"],
        )
        return {"decision": "recorded", **snapshot}

    def _current_executable_pnl_snapshot(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        if route is None:
            return {
                "quality": "INVALID_MISSING_BOOK",
                "reason": "route_missing",
                "snapshot_age_seconds": math.inf,
                "risk_state": "DEGRADED",
            }
        quantity = float(position.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "quality": "INVALID_MISSING_BOOK",
                "reason": "quantity_missing",
                "snapshot_age_seconds": math.inf,
                "risk_state": "DEGRADED",
            }

        legs = route.get("legs") or []
        long_route_leg = leg_by_side(legs, "long") or {}
        short_route_leg = leg_by_side(legs, "short") or {}
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        long_entry_price = float(
            optional_float(long_entry_leg.get("entry_fill_price"))
            or optional_float(long_entry_leg.get("vwap"))
            or 0.0
        )
        short_entry_price = float(
            optional_float(short_entry_leg.get("entry_fill_price"))
            or optional_float(short_entry_leg.get("vwap"))
            or 0.0
        )
        long_response = _leg_response_time(long_route_leg)
        short_response = _leg_response_time(short_route_leg)
        now_utc = now.astimezone(UTC)
        long_age = (
            (now_utc - long_response).total_seconds()
            if long_response is not None
            else math.inf
        )
        short_age = (
            (now_utc - short_response).total_seconds()
            if short_response is not None
            else math.inf
        )
        cross_skew = (
            abs((long_response - short_response).total_seconds())
            if long_response is not None and short_response is not None
            else math.inf
        )
        base = {
            "long_response_received_at": long_response.isoformat() if long_response else None,
            "short_response_received_at": short_response.isoformat() if short_response else None,
            "long_age_seconds": long_age,
            "short_age_seconds": short_age,
            "snapshot_age_seconds": max(long_age, short_age),
            "cross_venue_skew_seconds": cross_skew,
            "cross_venue_skew_ms": None if not math.isfinite(cross_skew) else cross_skew * 1000.0,
            "long_mark": optional_float(long_route_leg.get("mark_price")),
            "short_mark": optional_float(short_route_leg.get("mark_price")),
            "long_index": optional_float(long_route_leg.get("index_price")),
            "short_index": optional_float(short_route_leg.get("index_price")),
            "long_next_funding_at": long_route_leg.get("next_funding_at"),
            "short_next_funding_at": short_route_leg.get("next_funding_at"),
            "long_next_funding_rate": optional_float(long_route_leg.get("normalized_next_funding_rate")),
            "short_next_funding_rate": optional_float(short_route_leg.get("normalized_next_funding_rate")),
        }
        if long_age > 2.0 or short_age > 2.0 or cross_skew > 1.0:
            return {
                **base,
                "quality": "INVALID_STALE",
                "reason": "current_snapshot_stale_or_skewed",
                "risk_state": "DEGRADED",
                "long_entry_price": long_entry_price,
                "short_entry_price": short_entry_price,
            }
        if long_entry_price <= 0 or short_entry_price <= 0:
            return {
                **base,
                "quality": "INVALID_MISSING_BOOK",
                "reason": "entry_fill_price_missing",
                "risk_state": "DEGRADED",
            }
        long_fee_source = optional_float(long_route_leg.get("fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_route_leg.get("taker_fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_entry_leg.get("fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_entry_leg.get("taker_fee_rate"))
        short_fee_source = optional_float(short_route_leg.get("fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_route_leg.get("taker_fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_entry_leg.get("fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_entry_leg.get("taker_fee_rate"))
        if long_fee_source is None or short_fee_source is None:
            return {
                **base,
                "quality": "INVALID_MISSING_FEE",
                "reason": "fee_rate_missing",
                "risk_state": "DEGRADED",
            }
        long_levels = long_route_leg.get("bids") or []
        short_levels = short_route_leg.get("asks") or []
        if not long_levels or not short_levels:
            return {
                **base,
                "quality": "INVALID_MISSING_BOOK",
                "reason": "executable_orderbook_missing",
                "risk_state": "DEGRADED",
            }
        long_close = simulate_marketable_ioc(long_levels, "sell", quantity, EXECUTION_HAIRCUT_FRACTION)
        short_close = simulate_marketable_ioc(short_levels, "buy", quantity, EXECUTION_HAIRCUT_FRACTION)
        long_ratio = float(long_close["filled_quantity"] or 0.0) / quantity
        short_ratio = float(short_close["filled_quantity"] or 0.0) / quantity
        if long_ratio < 0.999 or short_ratio < 0.999:
            return {
                **base,
                "quality": "INVALID_INCOMPLETE_DEPTH",
                "reason": "full_ioc_depth_unavailable",
                "risk_state": "DEGRADED",
                "long_fill_ratio": long_ratio,
                "short_fill_ratio": short_ratio,
            }
        long_exit_price = float(long_close["average_fill_price"] or 0.0)
        short_exit_price = float(short_close["average_fill_price"] or 0.0)
        pnl = executable_paper_pnl(
            quantity=quantity,
            long_entry_price=long_entry_price,
            long_exit_price=long_exit_price,
            short_entry_price=short_entry_price,
            short_exit_price=short_exit_price,
            long_taker_fee=float(long_fee_source),
            short_taker_fee=float(short_fee_source),
            confirmed_funding_pnl=self.confirmed_funding_pnl_for_position(str(position["position_id"])),
            paper_open_fees=float(position.get("paper_open_fees") or 0.0),
            emergency_unwind_costs_already_incurred=float(
                position.get("paper_emergency_unwind_cost") or 0.0
            ),
        )
        return {
            **base,
            **pnl,
            "quality": "EXECUTABLE_FULL_DEPTH",
            "reason": None,
            "risk_state": "HEALTHY",
            "long_entry_price": long_entry_price,
            "short_entry_price": short_entry_price,
            "long_exit_price": long_exit_price,
            "short_exit_price": short_exit_price,
            "current_exit_spread": short_exit_price - long_exit_price,
            "long_fill_ratio": long_ratio,
            "short_fill_ratio": short_ratio,
            "long_fee_rate": float(long_fee_source),
            "short_fee_rate": float(short_fee_source),
        }

    def _ledger_cash_sum(
        self,
        position_id: str,
        *,
        venue: str,
        event_type: str | None = None,
    ) -> float:
        total = 0.0
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("venue") or "") != str(venue):
                continue
            if event_type is not None and str(row.get("event_type") or "") != event_type:
                continue
            total += float(row.get("cash_delta") or 0.0)
        return total

    def _reserved_collateral_amount(self, position_id: str, venue: str) -> float:
        reserve_key = collateral_reserve_event_key(position_id, venue)
        amount = 0.0
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("venue") or "") != str(venue):
                continue
            if str(row.get("event_type") or "") != "collateral_reserve":
                continue
            if (
                str(row.get("event_key") or "") != reserve_key
                and not str(row.get("event_key") or "").startswith("collateral_reserve:")
            ):
                continue
            amount = float(optional_float((row.get("payload") or {}).get("amount")) or amount)
        return amount

    def _leg_margin_snapshot(
        self,
        *,
        position_id: str,
        side: str,
        venue: str,
        quantity: float,
        entry_price: float,
        mark: float,
        fee_rate_source_leg: dict[str, Any],
    ) -> dict[str, Any]:
        reserved_collateral = self._reserved_collateral_amount(position_id, venue)
        confirmed_funding = self._ledger_cash_sum(position_id, venue=venue, event_type="funding")
        allocated_fees = -self._ledger_cash_sum(position_id, venue=venue, event_type="order_fee")
        if side == "long":
            upnl = quantity * (mark - entry_price)
        else:
            upnl = quantity * (entry_price - mark)
        mmr_value = optional_float(fee_rate_source_leg.get("maintenance_margin_rate"))
        mmr_source = "venue"
        if mmr_value is None:
            mmr_value = float(getattr(self.config, "paper_mmr", 0.02) or 0.02)
            mmr_source = "paper_fallback"
        mmr = float(mmr_value)
        maintenance_margin = abs(quantity * mark) * mmr
        leg_equity = reserved_collateral + upnl + confirmed_funding - allocated_fees
        margin_safety = leg_equity / maintenance_margin if maintenance_margin > 0 else 0.0
        q = max(quantity, 1e-12)
        if side == "long":
            liquidation_price = (
                q * entry_price - reserved_collateral - confirmed_funding + allocated_fees
            ) / max(q * (1.0 - mmr), 1e-12)
            liquidation_price = max(0.0, liquidation_price)
            liquidation_distance = max(0.0, (mark - liquidation_price) / max(mark, 1e-12))
        else:
            liquidation_price = (
                reserved_collateral + confirmed_funding - allocated_fees + q * entry_price
            ) / max(q * (1.0 + mmr), 1e-12)
            liquidation_distance = max(0.0, (liquidation_price - mark) / max(mark, 1e-12))
        return {
            "venue": venue,
            "side": side,
            "reserved_collateral": reserved_collateral,
            "confirmed_funding": confirmed_funding,
            "allocated_fees": allocated_fees,
            "upnl": upnl,
            "leg_equity": leg_equity,
            "maintenance_margin": maintenance_margin,
            "maintenance_margin_rate": mmr,
            "mmr_source": mmr_source,
            "margin_safety_ratio": margin_safety,
            "liquidation_price": liquidation_price,
            "liquidation_distance_fraction": liquidation_distance,
        }

    def poll_synchronized_position_risk(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        current_executable_pnl: dict[str, Any],
        active_cycle: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        position_id = str(position["position_id"])
        quality = str(current_executable_pnl.get("quality") or "")
        snapshot_age_value = current_executable_pnl.get("snapshot_age_seconds")
        snapshot_age = float(snapshot_age_value) if snapshot_age_value is not None else math.inf
        if quality != "EXECUTABLE_FULL_DEPTH":
            if snapshot_age > 5.0 or not math.isfinite(snapshot_age):
                return {
                    "close_reason": "risk_data_hard_stale",
                    "reasons": [str(current_executable_pnl.get("reason") or quality)],
                    "current_executable_pnl": current_executable_pnl,
                }
            return None

        quantity = float(position.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "close_reason": "quantity_missing",
                "reasons": ["quantity_missing"],
                "current_executable_pnl": current_executable_pnl,
            }
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        legs = route.get("legs") if route else []
        long_route_leg = leg_by_side(legs or [], "long") or {}
        short_route_leg = leg_by_side(legs or [], "short") or {}
        long_entry_price = float(
            optional_float(long_entry_leg.get("entry_fill_price"))
            or optional_float(long_entry_leg.get("vwap"))
            or 0.0
        )
        short_entry_price = float(
            optional_float(short_entry_leg.get("entry_fill_price"))
            or optional_float(short_entry_leg.get("vwap"))
            or 0.0
        )
        long_mark = optional_float(long_route_leg.get("mark_price"))
        short_mark = optional_float(short_route_leg.get("mark_price"))
        long_index = optional_float(long_route_leg.get("index_price"))
        short_index = optional_float(short_route_leg.get("index_price"))
        if (
            long_entry_price <= 0
            or short_entry_price <= 0
            or long_mark is None
            or short_mark is None
            or long_mark <= 0
            or short_mark <= 0
        ):
            return {
                "close_reason": "risk_mark_or_entry_missing",
                "reasons": ["risk_mark_or_entry_missing"],
                "current_executable_pnl": current_executable_pnl,
            }

        reference_price = (float(long_mark) + float(short_mark)) / 2.0
        entry_spread = short_entry_price - long_entry_price
        current_exit_spread = float(current_executable_pnl["short_exit_price"]) - float(
            current_executable_pnl["long_exit_price"]
        )
        edge_bps = optional_float(position.get("current_cycle_conservative_funding_edge_bps"))
        if edge_bps is None and active_cycle is not None:
            edge_bps = optional_float(active_cycle.get("conservative_funding_edge_bps"))
        if edge_bps is None:
            conservative_gross = optional_float(position.get("current_cycle_conservative_funding_gross"))
            reference_notional_for_edge = min(quantity * float(long_mark), quantity * float(short_mark))
            edge_bps = (
                conservative_gross / reference_notional_for_edge * 10_000.0
                if conservative_gross is not None and reference_notional_for_edge > 0
                else 0.0
            )
        basis_decision = dynamic_basis_stop_decision(
            entry_spread=entry_spread,
            current_exit_spread=current_exit_spread,
            reference_price=reference_price,
            active_cycle_conservative_funding_edge_bps=max(0.0, float(edge_bps or 0.0)),
        )
        if basis_decision["immediate_hard_exit"]:
            return {
                "close_reason": "basis_deterioration",
                "reasons": [
                    f"deterioration_{basis_decision['basis_deterioration_bps']:.1f}bps>=budget_{basis_decision['active_risk_budget_bps']:.1f}bps"
                ],
                "dynamic_basis": basis_decision,
                "current_executable_pnl": current_executable_pnl,
            }

        long_margin = self._leg_margin_snapshot(
            position_id=position_id,
            side="long",
            venue=str(position.get("long_venue") or long_entry_leg.get("venue") or ""),
            quantity=quantity,
            entry_price=long_entry_price,
            mark=float(long_mark),
            fee_rate_source_leg=long_route_leg,
        )
        short_margin = self._leg_margin_snapshot(
            position_id=position_id,
            side="short",
            venue=str(position.get("short_venue") or short_entry_leg.get("venue") or ""),
            quantity=quantity,
            entry_price=short_entry_price,
            mark=float(short_mark),
            fee_rate_source_leg=short_route_leg,
        )
        min_liquidation_distance = min(
            float(long_margin["liquidation_distance_fraction"]),
            float(short_margin["liquidation_distance_fraction"]),
        )
        min_margin_safety = min(
            float(long_margin["margin_safety_ratio"]),
            float(short_margin["margin_safety_ratio"]),
        )
        mark_index_bps = 0.0
        for mark, index in ((long_mark, long_index), (short_mark, short_index)):
            if mark is None or index is None or index <= 0:
                return {
                    "close_reason": "risk_mark_index_missing",
                    "reasons": ["risk_mark_index_missing"],
                    "current_executable_pnl": current_executable_pnl,
                }
            mark_index_bps = max(mark_index_bps, abs(float(mark) - float(index)) / float(index) * 10_000.0)

        triggered, trigger_reason = hard_risk_triggered(
            liquidation_distance_fraction=min_liquidation_distance,
            margin_safety_ratio=min_margin_safety,
            mark_index_divergence_bps=mark_index_bps,
            snapshot_age_seconds=snapshot_age,
        )
        risk_gates = {
            "liquidation_distance": min_liquidation_distance,
            "margin_safety_ratio": min_margin_safety,
            "mark_index_divergence_bps": mark_index_bps,
            "long_margin": long_margin,
            "short_margin": short_margin,
        }
        if triggered:
            return {
                "close_reason": f"risk_hard_exit:{trigger_reason}",
                "reasons": [trigger_reason],
                "risk_gates": risk_gates,
                "current_executable_pnl": current_executable_pnl,
            }

        reference_notional = min(quantity * float(long_mark), quantity * float(short_mark))
        active_budget_usd = reference_notional * float(basis_decision["active_risk_budget_bps"]) / 10_000.0
        executable_breach = float(current_executable_pnl["paper_net_if_exit_now"]) <= -active_budget_usd
        config = dict(position.get("config") or {})
        risk_state = dict(config.get("risk_state") or {})
        previous_breach = bool(risk_state.get("executable_pnl_breach"))
        risk_state["executable_pnl_breach"] = executable_breach
        risk_state["last_checked_at"] = now.astimezone(UTC).isoformat()
        risk_state["last_paper_net_if_exit_now"] = current_executable_pnl["paper_net_if_exit_now"]
        risk_state["active_risk_budget_usd"] = active_budget_usd
        risk_state["last_margin_safety_ratio"] = min_margin_safety
        risk_state["last_liquidation_distance_fraction"] = min_liquidation_distance
        risk_state["last_mark_index_divergence_bps"] = mark_index_bps
        config["risk_state"] = risk_state
        self.store.update_funding_capture_position_config(position_id, config)
        if executable_breach and previous_breach:
            return {
                "close_reason": "executable_pnl_breach",
                "reasons": [
                    f"paper_net_if_exit_now_{current_executable_pnl['paper_net_if_exit_now']:.2f}<=-budget_{active_budget_usd:.2f}"
                ],
                "risk_gates": risk_gates,
                "dynamic_basis": basis_decision,
                "current_executable_pnl": current_executable_pnl,
            }

        return None

    def _route_plan(
        self,
        route: dict[str, Any],
        now: datetime,
    ) -> FundingRoutePlan | None:
        legs = route.get("legs") or []
        long_leg = dict(leg_by_side(legs, "long") or {})
        short_leg = dict(leg_by_side(legs, "short") or {})
        if not long_leg or not short_leg:
            return None
        canonical_asset = route.get("canonical_asset")
        if canonical_asset:
            long_leg.setdefault("canonical_asset", canonical_asset)
            short_leg.setdefault("canonical_asset", canonical_asset)
        target_notional = float(
            route.get("target_notional")
            or getattr(self.config, "target_notional_per_leg", 0.0)
            or 0.0
        )
        return self.planner.plan(
            long_market=long_leg,
            short_market=short_leg,
            now=now,
            target_notional=target_notional,
        )

    def _plan_dict_for_route(
        self,
        route: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        plan = self._route_plan(route, now)
        return plan.as_dict() if plan is not None else None

    def _first_included_settlement_at(self, plan: dict[str, Any] | None) -> datetime | None:
        events = (plan or {}).get("included_settlement_events") or []
        times = [
            parse_time(event.get("scheduled_at"))
            for event in events
            if isinstance(event, dict)
        ]
        times = [time.astimezone(UTC) for time in times if time is not None]
        return min(times) if times else None

    def _last_included_settlement_at(self, plan: dict[str, Any] | None) -> datetime | None:
        events = (plan or {}).get("included_settlement_events") or []
        times = [
            parse_time(event.get("scheduled_at"))
            for event in events
            if isinstance(event, dict)
        ]
        times = [time.astimezone(UTC) for time in times if time is not None]
        return max(times) if times else None

    def _route_plan_blocking_reasons(self, plan: dict[str, Any] | None) -> list[str]:
        if plan is None:
            return ["route_plan_missing"]
        reasons = list(plan.get("blockers") or [])
        status = str(plan.get("eligibility_status") or "")
        if status in {"DATA_STALE", "CAPABILITY_BLOCKED"} and status not in reasons:
            reasons.append(status.lower())
        return list(dict.fromkeys(str(reason) for reason in reasons if reason))

    def _paper_capability_blocking_reasons(
        self,
        plan: dict[str, Any] | None,
    ) -> list[str]:
        reasons: list[str] = []
        for label in ("leg_a", "leg_b"):
            leg = (plan or {}).get(label)
            if not isinstance(leg, dict):
                continue
            if leg.get("paper_enabled") is not True:
                venue = str(leg.get("venue") or label)
                reasons.append(f"{venue}_paper_disabled")
            if leg.get("live_enabled") is True:
                reasons.append("unexpected_live_enabled_in_paper_runtime")
        return list(dict.fromkeys(reasons))

    def consider_route(
        self,
        route: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        now = self.clock.now()
        route_key = str(route.get("route_key") or "")
        if not route_key:
            return {"opened": False, "reason": "route_key_missing"}
        route_plan = self._plan_dict_for_route(route, now)
        plan_blockers = self._route_plan_blocking_reasons(route_plan)
        paper_blockers = self._paper_capability_blocking_reasons(route_plan)
        plan_blockers = list(dict.fromkeys([*plan_blockers, *paper_blockers]))
        settlement_at = self._first_included_settlement_at(route_plan) or route_next_settlement(route)
        if settlement_at is None:
            return {
                "opened": False,
                "reason": "route_plan_settlement_missing",
                "route_plan": route_plan,
            }
        lead = (settlement_at - now.astimezone(UTC)).total_seconds()
        capture_id = capture_position_id_for_route(route)
        existing_open = self.store.funding_capture_open_position_by_route_key(route_key)
        if existing_open is not None:
            return {"opened": False, "reason": "route_already_open", "position_id": existing_open["position_id"]}
        guard = self._opportunity_guard(capture_id)
        if not guard["allowed"]:
            return {"opened": False, **guard}
        self._ensure_discovered_or_armed(route, capture_id, settlement_at, lead, now)
        if route_plan is None:
            return {"opened": False, "reason": "route_plan_missing"}
        lifecycle_state = str(route_plan.get("lifecycle_state") or "")
        if lifecycle_state == "ENTRY_WINDOW_MISSED":
            self.store.update_funding_capture_position_state(
                capture_id,
                "ENTRY_WINDOW_MISSED",
                now,
            )
            return {
                "opened": False,
                "reason": "entry_window_missed",
                "route_plan": route_plan,
            }
        if plan_blockers:
            return {
                "opened": False,
                "reason": "route_plan_blocked",
                "route_plan": route_plan,
                "blockers": plan_blockers,
            }
        missing_data = self._missing_required_route_data(route)
        if missing_data:
            return {"opened": False, "reason": "required_route_data_missing", "missing": missing_data}
        observation = build_focused_observation(
            route,
            now=now,
            max_age_seconds=float(self.config.max_entry_snapshot_age_seconds),
            max_response_skew_seconds=float(
                self.config.max_cross_venue_snapshot_skew_seconds
            ),
        )
        route_plan_gross = optional_float(route_plan.get("conservative_funding_cashflow_usd"))
        if route_plan_gross is not None:
            observation["gross_funding_pnl"] = float(route_plan_gross)
            observation["route_plan_conservative_funding_cashflow_usd"] = float(route_plan_gross)
            observation["route_plan_expected_funding_cashflow_usd"] = route_plan.get(
                "expected_funding_cashflow_usd"
            )
            validation = validate_focused_observation(
                observation,
                now=now,
                max_age_seconds=float(self.config.max_entry_snapshot_age_seconds),
                max_response_skew_seconds=float(
                    self.config.max_cross_venue_snapshot_skew_seconds
                ),
            )
            observation["snapshot_valid"] = bool(validation["valid"])
            observation["invalid_reason"] = (
                ",".join(validation["reasons"]) if validation["reasons"] else None
            )
        self._store_observation(route, capture_id, settlement_at, observation, phase="entry")
        observations = self._valid_observations(route, now, phase="entry", cycle_id=f"{capture_id}:1")
        if lifecycle_state == "ENTRY_PENDING":
            return {
                "opened": False,
                "reason": "entry_pending",
                "route_plan": route_plan,
                "valid_observation_count": len(observations),
            }
        underwriting = entry_underwriting(
            observations,
            now=now,
            max_latest_age_seconds=float(
                self.config.max_entry_snapshot_age_seconds
            ),
        )
        if not underwriting["eligible"]:
            return {
                "opened": False,
                "reason": "focused_underwriting_not_ready",
                "underwriting": underwriting,
                "route_plan": route_plan,
            }
        if not (float(self.config.entry_min_lead_seconds) <= lead <= float(self.config.entry_max_lead_seconds)):
            return {
                "opened": False,
                "reason": "outside_entry_window",
                "lead_seconds": lead,
                "route_plan": route_plan,
            }
        economics = self._initial_economics(route, observations, underwriting, route_plan)
        if not economics["eligible"]:
            return {
                "opened": False,
                "reason": "initial_economics_failed",
                "economics": economics,
                "route_plan": route_plan,
            }
        risk_gate = self._entry_risk_gate(route)
        if not risk_gate["passed"]:
            return {"opened": False, "reason": "entry_risk_gate_failed", "risk_gate": risk_gate}
        account_check = self._enforce_account_limits(route, accounts)
        if not account_check["passed"]:
            return {"opened": False, "reason": account_check["reason"], "account_check": account_check}
        gate_result = self._entry_gate_result(
            route=route,
            route_plan=route_plan,
            underwriting=underwriting,
            economics=economics,
            risk_gate=risk_gate,
            account_check=account_check,
            now=now,
        )
        self._mark_armed(route, capture_id, settlement_at, now, route_plan, gate_result)
        execution = self._execute_entry(route, capture_id, settlement_at, now)
        if execution["state"] != "OPEN":
            if execution.get("attempt_id"):
                self.close_entry_observation_bucket(route, capture_id=capture_id)
            return {"opened": False, "reason": "entry_execution_failed", "execution": execution}
        self._mark_open(route, capture_id, settlement_at, economics, execution, now, route_plan)
        self.close_entry_observation_bucket(route, capture_id=capture_id)
        return {
            "opened": True,
            "position_id": capture_id,
            "cycle_id": f"{capture_id}:1",
            "economics": economics,
            "execution": execution,
            "underwriting": underwriting,
            "route_plan": route_plan,
        }

    def _opportunity_guard(self, capture_id: str) -> dict[str, Any]:
        existing = self.store.funding_capture_position_by_id(capture_id)
        if existing is None:
            return {"allowed": True}
        state = str(existing.get("state") or "")
        config = existing.get("config") or {}
        if state in {"OPEN", "HOLDING_NEXT_CYCLE", "SETTLEMENT_CROSSED", "POST_SETTLEMENT_EVALUATION"}:
            return {"allowed": False, "reason": "opportunity_already_open", "position_id": capture_id}
        if config.get("entry_attempt_submitted"):
            return {
                "allowed": False,
                "reason": "opportunity_attempt_already_submitted",
                "position_id": capture_id,
                "attempt_id": config.get("entry_attempt_id"),
                "state": state,
            }
        if state in {
            "FAILED",
            "REJECTED_AFTER_SUBMISSION",
            "CLOSED_PENDING_RECONCILIATION",
            "CLOSED_REQUIRES_REVIEW",
            "RECONCILED",
            "UNRECONCILED",
        }:
            return {
                "allowed": False,
                "reason": "opportunity_terminal",
                "position_id": capture_id,
                "state": state,
            }
        return {"allowed": True}

    def _new_attempt_id(self, capture_id: str, decision_at: datetime) -> str:
        stamp = decision_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(f"{capture_id}:{stamp}".encode("utf-8")).hexdigest()[:12]
        return f"{capture_id}:attempt:{stamp}:{digest}"

    def _mark_entry_attempt_submitted(
        self,
        *,
        capture_id: str,
        route: dict[str, Any],
        attempt_id: str,
        settlement_at: datetime,
        decision_at: datetime,
    ) -> None:
        self.store.begin_funding_entry_attempt(
            position_id=capture_id,
            route_key=str(route.get("route_key") or ""),
            route_entry_key=route_entry_key(route),
            attempt_id=attempt_id,
            settlement_at=settlement_at,
            submitted_at=decision_at,
        )

    def _mark_entry_attempt_terminal(
        self,
        *,
        capture_id: str,
        attempt_id: str,
        state: str,
        now: datetime,
        reason: str | None = None,
    ) -> None:
        position = self.store.funding_capture_position_by_id(capture_id)
        if position is None:
            return
        config = dict(position.get("config") or {})
        config["entry_attempt_state"] = state
        if reason:
            config["entry_attempt_terminal_reason"] = reason
        attempts = []
        for attempt in list(config.get("entry_attempts") or []):
            item = dict(attempt)
            if item.get("attempt_id") == attempt_id:
                item["state"] = state
                item["terminal_at"] = now.astimezone(UTC).isoformat()
                if reason:
                    item["reason"] = reason
            attempts.append(item)
        config["entry_attempts"] = attempts
        self.store.update_funding_capture_position_config(capture_id, config, now)

    def recover_runtime_state(self, now: datetime) -> dict[str, Any]:
        boundary = self.store.repair_funding_capture_boundary_consistency(now=now)
        entry = self.recover_stale_entry_submissions(now)
        reconciliation = self.recover_reconciled_funding_effects(now)
        accounts = self.store.repair_paper_account_consistency()
        return {
            "boundary": boundary,
            "entry_submitted": entry,
            "reconciliation": reconciliation,
            "accounts": accounts,
        }

    def recover_stale_entry_submissions(self, now: datetime) -> dict[str, Any]:
        recovered = 0
        completed_open = 0
        aborted = 0
        unwound = 0
        terminal_states = {
            "FAILED",
            "REJECTED_AFTER_SUBMISSION",
            "CLOSED_PENDING_RECONCILIATION",
            "CLOSED_REQUIRES_REVIEW",
            "RECONCILED",
            "UNRECONCILED",
        }
        live_states = {
            "OPEN",
            "HOLDING_NEXT_CYCLE",
            "SETTLEMENT_CROSSED",
            "POST_SETTLEMENT_EVALUATION",
            "EXIT_SCHEDULED",
            "EXIT_SUBMITTED",
            "PARTIALLY_CLOSED",
            "EMERGENCY_UNWIND",
            "SETTLEMENT_PLAN_MISMATCH",
        }
        candidates: list[dict[str, Any]] = []
        for position in self.store.funding_capture_position_rows():
            state = str(position.get("state") or "")
            config = dict(position.get("config") or {})
            if state == "ENTRY_SUBMITTED":
                candidates.append(position)
                continue
            if not config.get("entry_attempt_submitted"):
                continue
            if state in terminal_states or state in live_states:
                continue
            candidates.append(position)
        for position in candidates:
            position_id = str(position["position_id"])
            config = dict(position.get("config") or {})
            attempt_id = _position_attempt_id(position)
            if not attempt_id:
                attempt_id = str(config.get("entry_attempt_id") or position_id)
            cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
            entry_legs = list(config.get("entry_legs") or [])
            ledger_rows = self.store.paper_event_ledger_rows(position_id)
            reserve_rows = [
                row
                for row in ledger_rows
                if str(row.get("event_type") or "") == "collateral_reserve"
                and str(row.get("event_key") or "").startswith(f"collateral_reserve:{attempt_id}:")
            ]
            orders = [
                row
                for row in self.store.funding_paper_order_rows(position_id)
                if str(row.get("order_intent") or "") == "ENTRY"
                and str(row.get("paper_order_id") or "").startswith(f"{attempt_id}:entry:")
            ]
            route_legs = [dict(leg) for leg in entry_legs if isinstance(leg, dict)]
            for reserve in reserve_rows:
                if any(str(leg.get("venue") or "") == str(reserve.get("venue") or "") for leg in route_legs):
                    continue
                side = "long" if str(reserve.get("venue") or "") == str(position.get("long_venue") or "") else "short"
                route_legs.append(
                    {
                        "side": side,
                        "venue": reserve.get("venue"),
                        "symbol": position.get(f"{side}_symbol"),
                        "fee_rate": 0.0,
                    }
                )
            route = {
                "route_key": config.get("route_key"),
                "canonical_asset": position.get("canonical_asset"),
                "long_venue": position.get("long_venue"),
                "short_venue": position.get("short_venue"),
                "long_symbol": position.get("long_symbol"),
                "short_symbol": position.get("short_symbol"),
                "target_notional": position.get("target_notional"),
                "legs": route_legs,
            }

            if not reserve_rows and not orders:
                self._mark_entry_attempt_terminal(
                    capture_id=position_id,
                    attempt_id=attempt_id,
                    state="RECOVERED_ABORTED",
                    now=now,
                    reason="entry_submitted_no_reserve_no_orders",
                )
                self.store.update_funding_capture_position_state(
                    position_id,
                    "FAILED",
                    now,
                    closed_at=now.astimezone(UTC).isoformat(),
                )
                aborted += 1
                recovered += 1
                continue

            if reserve_rows and not orders:
                self._release_collateral(position_id, route, attempt_id=attempt_id)
                self._mark_entry_attempt_terminal(
                    capture_id=position_id,
                    attempt_id=attempt_id,
                    state="RECOVERED_ABORTED",
                    now=now,
                    reason="entry_submitted_reserve_without_orders",
                )
                self.store.update_funding_capture_position_state(
                    position_id,
                    "FAILED",
                    now,
                    closed_at=now.astimezone(UTC).isoformat(),
                )
                aborted += 1
                recovered += 1
                continue

            by_side = {str(order.get("leg_side") or ""): order for order in orders}
            long_order = by_side.get("long")
            short_order = by_side.get("short")
            long_qty = float((long_order or {}).get("filled_quantity") or 0.0)
            short_qty = float((short_order or {}).get("filled_quantity") or 0.0)
            both_full = (
                long_order is not None
                and short_order is not None
                and str(long_order.get("state") or "") == "FILLED"
                and str(short_order.get("state") or "") == "FILLED"
                and long_qty > 0
                and short_qty > 0
                and abs(long_qty - short_qty) <= max(long_qty, short_qty) * 0.001
            )
            if not both_full or not reserve_rows:
                for order in orders:
                    self._record_ledger_entry(
                        make_ledger_entry(
                            order_fee_event_key(str(order["paper_order_id"])),
                            position_id=position_id,
                            cycle_id=cycle_id,
                            venue=order.get("venue"),
                            event_type="order_fee",
                            cash_delta=-float(order.get("fee") or 0.0),
                            payload={"order_id": order["paper_order_id"], "attempt_id": attempt_id},
                        )
                    )
                long_fill = {
                    "filled_quantity": long_qty,
                    "average_fill_price": (long_order or {}).get("average_fill_price"),
                }
                short_fill = {
                    "filled_quantity": short_qty,
                    "average_fill_price": (short_order or {}).get("average_fill_price"),
                }
                self._unwind_partial_entry(
                    route=route,
                    capture_id=position_id,
                    cycle_id=cycle_id,
                    long_fill=long_fill,
                    short_fill=short_fill,
                    long_fee=float((long_order or {}).get("fee") or 0.0),
                    short_fee=float((short_order or {}).get("fee") or 0.0),
                    decision_at=now,
                    now=now,
                    attempt_id=attempt_id,
                )
                self._mark_entry_attempt_terminal(
                    capture_id=position_id,
                    attempt_id=attempt_id,
                    state="REJECTED_AFTER_SUBMISSION",
                    now=now,
                    reason="entry_submitted_partial_or_unreserved_exposure_unwound",
                )
                unwound += 1
                recovered += 1
                continue

            for order in (long_order, short_order):
                self._record_ledger_entry(
                    make_ledger_entry(
                        order_fee_event_key(str(order["paper_order_id"])),
                        position_id=position_id,
                        cycle_id=cycle_id,
                        venue=order.get("venue"),
                        event_type="order_fee",
                        cash_delta=-float(order.get("fee") or 0.0),
                        payload={"order_id": order["paper_order_id"], "attempt_id": attempt_id},
                    )
                )
            settlement_at = parse_time(config.get("entry_attempt_submitted_at")) or now
            attempts = list(config.get("entry_attempts") or [])
            for attempt in attempts:
                if attempt.get("attempt_id") == attempt_id:
                    settlement_at = parse_time(attempt.get("settlement_at")) or settlement_at
                    break
            execution = {
                "state": "FILLED",
                "attempt_id": attempt_id,
                "quantity": min(long_qty, short_qty),
                "target_quantity": min(long_qty, short_qty),
                "long_entry_price": float(long_order.get("average_fill_price") or 0.0),
                "short_entry_price": float(short_order.get("average_fill_price") or 0.0),
                "paper_open_fees": float(long_order.get("fee") or 0.0) + float(short_order.get("fee") or 0.0),
                "filled_at": (long_order.get("filled_at") or short_order.get("filled_at") or now.isoformat()),
                "deadline_ok": True,
                "fill_state": "FILLED",
                "long_fill_ratio": 1.0,
                "short_fill_ratio": 1.0,
                "quantity_mismatch_fraction": 0.0,
            }
            self._mark_open(
                route,
                position_id,
                settlement_at,
                dict(config.get("entry_economics") or {}),
                execution,
                now,
                config.get("funding_route_plan"),
            )
            self._mark_entry_attempt_terminal(
                capture_id=position_id,
                attempt_id=attempt_id,
                state="OPEN",
                now=now,
                reason="entry_submitted_recovered_open",
            )
            completed_open += 1
            recovered += 1
        account_report = self.store.repair_paper_account_consistency()
        return {
            "processed": recovered,
            "recovered_aborted": aborted,
            "recovered_unwound": unwound,
            "recovered_open": completed_open,
            "account_consistency": account_report,
        }

    def _ensure_discovered_or_armed(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        lead: float,
        now: datetime,
    ) -> None:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        route_key = str(route.get("route_key") or "")
        self.store.upsert_funding_capture_position(
            {
                "position_id": capture_id,
                "strategy_name": STRATEGY_NAME,
                "strategy_version": STRATEGY_VERSION,
                "canonical_asset": route.get("canonical_asset", ""),
                "long_venue": long_leg.get("venue") or route.get("long_venue") or "",
                "long_symbol": long_leg.get("symbol") or route.get("long_symbol") or "",
                "short_venue": short_leg.get("venue") or route.get("short_venue") or "",
                "short_symbol": short_leg.get("symbol") or route.get("short_symbol") or "",
                "quantity": 0.0,
                "target_notional": float(route.get("target_notional") or self.config.target_notional_per_leg),
                "state": "DISCOVERED",
                "opened_at": now.isoformat(),
                "config": {
                    "route_key": route_key,
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": legs,
                    "candidate_state": "DISCOVERED",
                    "lead_seconds": lead,
                },
            }
        )
        self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{capture_id}:1",
                "position_id": capture_id,
                "cycle_number": 1,
                "scheduled_funding_at": settlement_at.isoformat(),
                "state": "DISCOVERED",
                "decision": "DISCOVERED",
                "decision_reason": "entry_gates_collecting",
            }
        )

    def _entry_gate_result(
        self,
        *,
        route: dict[str, Any],
        route_plan: dict[str, Any],
        underwriting: dict[str, Any],
        economics: dict[str, Any],
        risk_gate: dict[str, Any],
        account_check: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        return {
            "passed": True,
            "observed_at": now.astimezone(UTC).isoformat(),
            "passed_gates": [
                "normalized_instrument_identity",
                "endpoint_identity",
                "environment_identity",
                "venue_capability",
                "paper_execution_support",
                "funding_semantics",
                "settlement_timing",
                "funding_freshness",
                "book_freshness",
                "executable_depth",
                "fee_provenance",
                "stablecoin_conversion",
                "slippage",
                "net_edge",
                "risk_limits",
                "account_limits",
                "focused_underwriting",
            ],
            "blocked_gates": [],
            "reason_codes": [],
            "fee_evidence": {
                "long": fee_evidence_status(long_leg, "taker", now=now),
                "short": fee_evidence_status(short_leg, "taker", now=now),
            },
            "endpoint_identity": {
                "long": {
                    "venue": long_leg.get("venue"),
                    "environment": long_leg.get("environment"),
                    "base_url": long_leg.get("endpoint_base_url"),
                    "provenance": long_leg.get("endpoint_identity_provenance"),
                },
                "short": {
                    "venue": short_leg.get("venue"),
                    "environment": short_leg.get("environment"),
                    "base_url": short_leg.get("endpoint_base_url"),
                    "provenance": short_leg.get("endpoint_identity_provenance"),
                },
            },
            "route_plan": {
                "eligibility_status": route_plan.get("eligibility_status"),
                "lifecycle_state": route_plan.get("lifecycle_state"),
                "conservative_net_usd": route_plan.get("conservative_net_usd"),
                "cost_estimates": route_plan.get("cost_estimates"),
            },
            "underwriting": underwriting,
            "economics": economics,
            "risk_gate": risk_gate,
            "account_check": account_check,
        }

    def _mark_armed(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        now: datetime,
        route_plan: dict[str, Any],
        gate_result: dict[str, Any],
    ) -> None:
        cycle_identity = f"{STRATEGY_VERSION}:{capture_id}:1:{settlement_at.astimezone(UTC).isoformat()}"
        position = self.store.funding_capture_position_by_id(capture_id)
        config = dict((position or {}).get("config") or {})
        config.update(
            {
                "candidate_state": "ARMED",
                "armed_at": now.astimezone(UTC).isoformat(),
                "entry_gate_result": gate_result,
                "plan_generation": 1,
                "active_plan_generation": 1,
                "cycle_identity_key": cycle_identity,
                "funding_route_plan": route_plan,
                "included_settlement_events": route_plan.get("included_settlement_events") or [],
                "planned_exit_at": route_plan.get("planned_exit_at"),
                "monitor_until": route_plan.get("monitor_until"),
                "entry_legs": route.get("legs") or config.get("entry_legs") or [],
            }
        )
        self.store.update_funding_capture_position_config(capture_id, config, now)
        self.store.update_funding_capture_position_state(capture_id, "ARMED", now)
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        cycle_id = f"{capture_id}:1"
        active_plan = self._cycle_plan_snapshot(
            position_id=capture_id,
            cycle_id=cycle_id,
            cycle_number=1,
            plan_generation=1,
            scheduled_at=settlement_at,
            route=route,
            route_plan=route_plan,
            now=now,
        )
        self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": cycle_id,
                "position_id": capture_id,
                "cycle_number": 1,
                "plan_generation": 1,
                "scheduled_funding_at": settlement_at.astimezone(UTC).isoformat(),
                "long_next_funding_rate_at_decision": _leg_rate(long_leg),
                "short_next_funding_rate_at_decision": _leg_rate(short_leg),
                "conservative_funding_gross": route_plan.get("conservative_funding_cashflow_usd"),
                "conservative_funding_edge_bps": route_plan.get("conservative_net_bps"),
                "decision": "ARMED",
                "decision_reason": "all_entry_gates_passed",
                "state": "ARMED",
                "active_plan": active_plan,
            }
        )

    def _store_observation(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        observation: dict[str, Any],
        *,
        phase: str,
        cycle_id: str | None = None,
    ) -> None:
        route_key = str(route.get("route_key") or "")
        resolved_cycle_id = cycle_id or f"{capture_id}:1"
        observation_phase = phase or observation.get("phase") or "entry"
        bucket_key = route_entry_key(route) if observation_phase == "entry" else route_key
        bucket = _observation_bucket(bucket_key, observation_phase, resolved_cycle_id)
        observations = self.observations_by_route.setdefault(bucket, [])
        observations.append(observation)
        self._cleanup_observation_bucket(
            bucket,
            now=parse_time(observation.get("observed_at")) or self.clock.now(),
            settlement_at=settlement_at,
            phase=observation_phase,
        )
        observations = self.observations_by_route.setdefault(bucket, [])
        self.store.upsert_funding_capture_observation(
            {
                **observation,
                "position_id": capture_id,
                "cycle_id": resolved_cycle_id,
                "phase": observation_phase,
                "observation_id": f"{resolved_cycle_id}:{observation_phase}:{len(observations):04d}:{observation['observed_at']}",
            }
        )

    def _valid_observations(
        self,
        route: dict[str, Any],
        now: datetime,
        *,
        phase: str,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        bucket_key = route_entry_key(route) if phase == "entry" else str(route.get("route_key") or "")
        bucket = _observation_bucket(bucket_key, phase, cycle_id)
        valid = []
        for row in self.observations_by_route.get(bucket, []):
            if str(row.get("phase") or phase) != phase:
                continue
            if not _observation_timestamp_match(row, route):
                continue
            max_age_seconds = (
                float(self.config.max_entry_snapshot_age_seconds)
                if phase == "entry"
                else 2.0
            )
            if validate_focused_observation(
                row,
                now=now,
                max_age_seconds=max_age_seconds,
                max_response_skew_seconds=float(
                    self.config.max_cross_venue_snapshot_skew_seconds
                ),
            )["valid"]:
                valid.append(row)
        return valid

    def _cleanup_observation_bucket(
        self,
        bucket: str,
        *,
        now: datetime,
        settlement_at: datetime,
        phase: str,
    ) -> None:
        observations = self.observations_by_route.get(bucket)
        if not observations:
            return
        now_utc = now.astimezone(UTC)
        settlement_utc = settlement_at.astimezone(UTC)
        cleaned: list[dict[str, Any]] = []
        for row in observations:
            observed_at = parse_time(row.get("observed_at"))
            if observed_at is None:
                continue
            age = (now_utc - observed_at.astimezone(UTC)).total_seconds()
            if age > ENTRY_OBSERVATION_TTL_SECONDS:
                continue
            if phase == "entry" and observed_at.astimezone(UTC) > settlement_utc:
                continue
            cleaned.append(row)
        if len(cleaned) > 120:
            cleaned = cleaned[-120:]
        self.observations_by_route[bucket] = cleaned

    def close_entry_observation_bucket(
        self,
        route: dict[str, Any],
        *,
        capture_id: str,
    ) -> None:
        bucket = _observation_bucket(route_entry_key(route), "entry", f"{capture_id}:1")
        self.observations_by_route.pop(bucket, None)

    def _initial_economics(
        self,
        route: dict[str, Any],
        observations: list[dict[str, Any]],
        underwriting: dict[str, Any],
        route_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest = observations[-1]
        q = self._target_quantity(route)
        long_open = float(latest.get("long_open_vwap") or 0.0)
        short_open = float(latest.get("short_open_vwap") or 0.0)
        long_close = float(latest.get("long_close_vwap") or long_open)
        short_close = float(latest.get("short_close_vwap") or short_open)
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        price_pnl_if_flat_now = q * (long_close - long_open) + q * (short_open - short_close)
        baseline_book_cost = max(0.0, -price_pnl_if_flat_now)
        fees = (
            q * long_open * _leg_fee_rate(long_leg)
            + q * long_close * _leg_fee_rate(long_leg)
            + q * short_open * _leg_fee_rate(short_leg)
            + q * short_close * _leg_fee_rate(short_leg)
        )
        reference = min(q * long_open, q * short_open)
        sorted_obs = sorted(observations, key=lambda o: str(o.get("observed_at") or ""))
        adverse_exit_spread_changes_bps: list[float] = []
        for previous, current in zip(sorted_obs, sorted_obs[1:]):
            previous_spread = optional_float(previous.get("current_exit_spread"))
            current_spread = optional_float(current.get("current_exit_spread"))
            if previous_spread is None or current_spread is None:
                continue
            adverse_change = max(0.0, current_spread - previous_spread)
            adverse_exit_spread_changes_bps.append(
                adverse_change / max(long_open, short_open, 1e-12) * 10_000.0
            )
        if len(adverse_exit_spread_changes_bps) >= 10:
            from smart_money_radar.funding.strategy_synchronized_funding import percentile_95
            basis_reserve_bps = max(
                15.0,
                min(50.0, 3.0 * percentile_95(adverse_exit_spread_changes_bps)),
            )
        else:
            basis_reserve_bps = 30.0
        mark_returns: list[float] = []
        for previous, current in zip(sorted_obs, sorted_obs[1:]):
            step_returns: list[float] = []
            for key in ("long_mark", "short_mark"):
                previous_mark = optional_float(previous.get(key))
                current_mark = optional_float(current.get(key))
                if previous_mark is not None and previous_mark > 0 and current_mark is not None:
                    step_returns.append(abs(current_mark - previous_mark) / previous_mark * 10_000.0)
            if step_returns:
                mark_returns.append(max(step_returns))
        if len(mark_returns) >= 10:
            from smart_money_radar.funding.strategy_synchronized_funding import percentile_95
            legging_reserve_bps = min(30.0, max(10.0, 2.0 * percentile_95(mark_returns)))
        else:
            legging_reserve_bps = 15.0
        basis_reserve_usd = reference * basis_reserve_bps / 10_000.0
        legging_reserve_usd = reference * legging_reserve_bps / 10_000.0
        route_plan_conservative = optional_float(
            (route_plan or {}).get("conservative_funding_cashflow_usd")
        )
        conservative_funding_gross = (
            float(route_plan_conservative)
            if route_plan_conservative is not None
            else float(underwriting["conservative_funding_gross"])
        )
        result = initial_entry_economics(
            conservative_funding_gross=conservative_funding_gross,
            baseline_round_trip_book_cost=baseline_book_cost,
            total_round_trip_fee_estimate=fees,
            entry_basis_reserve_usd=basis_reserve_usd,
            entry_legging_reserve_usd=legging_reserve_usd,
            reference_notional=reference,
        )
        result["entry_basis_reserve_bps"] = basis_reserve_bps
        result["entry_legging_reserve_bps"] = legging_reserve_bps
        if route_plan is not None:
            result["route_plan"] = route_plan
            result["route_plan_expected_funding_cashflow_usd"] = route_plan.get(
                "expected_funding_cashflow_usd"
            )
            result["route_plan_conservative_funding_cashflow_usd"] = route_plan.get(
                "conservative_funding_cashflow_usd"
            )
        return result

    def _compute_common_quantity(
        self,
        long_price: float,
        short_price: float,
        long_step: float,
        short_step: float,
        long_min_qty: float,
        short_min_qty: float,
        long_min_notional: float,
        short_min_notional: float,
    ) -> dict[str, Any]:
        if long_price <= 0 or short_price <= 0:
            return {"quantity": 0.0, "reason": "zero_price"}
        target = float(self.config.target_notional_per_leg)
        q_raw = min(target / long_price, target / short_price)
        if long_step > 0 and short_step > 0:
            scale = 10**8
            long_units = max(1, round(long_step * scale))
            short_units = max(1, round(short_step * scale))
            common_units = (long_units * short_units) // math.gcd(long_units, short_units)
            common_step = common_units / scale
        else:
            return {"quantity": 0.0, "reason": "missing_quantity_step"}
        if common_step <= 0:
            return {"quantity": 0.0, "reason": "invalid_common_step"}
        q = math.floor(q_raw / common_step) * common_step
        long_notional = q * long_price
        short_notional = q * short_price
        if q < max(long_min_qty, short_min_qty):
            return {"quantity": 0.0, "reason": "below_min_quantity"}
        if long_notional < long_min_notional:
            return {"quantity": 0.0, "reason": "below_long_min_notional"}
        if short_notional < short_min_notional:
            return {"quantity": 0.0, "reason": "below_short_min_notional"}
        if long_notional < 450 or short_notional < 450:
            return {"quantity": 0.0, "reason": "below_notional_floor_450"}
        if long_notional > 500 or short_notional > 500:
            return {"quantity": 0.0, "reason": "above_notional_cap_500"}
        return {"quantity": q, "reason": None}

    def _target_quantity(self, route: dict[str, Any]) -> float:
        result = self._compute_target_quantity(route)
        return result["quantity"]

    def _compute_target_quantity(self, route: dict[str, Any]) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_price = float(long_leg.get("open_vwap") or long_leg.get("vwap") or long_leg.get("mark_price") or 0.0)
        short_price = float(short_leg.get("open_vwap") or short_leg.get("vwap") or short_leg.get("mark_price") or 0.0)
        for side, leg in (("long", long_leg), ("short", short_leg)):
            if optional_float(leg.get("quantity_step")) is None:
                return {"quantity": 0.0, "reason": f"{side}_quantity_step_missing"}
            if optional_float(leg.get("min_quantity")) is None:
                return {"quantity": 0.0, "reason": f"{side}_min_quantity_missing"}
            if optional_float(leg.get("min_notional")) is None:
                return {"quantity": 0.0, "reason": f"{side}_min_notional_missing"}
        long_step = float(long_leg.get("quantity_step") or 0.0)
        short_step = float(short_leg.get("quantity_step") or 0.0)
        long_min_qty = float(long_leg.get("min_quantity") or 0.0)
        short_min_qty = float(short_leg.get("min_quantity") or 0.0)
        long_min_notional = float(long_leg.get("min_notional") or 0.0)
        short_min_notional = float(short_leg.get("min_notional") or 0.0)
        return self._compute_common_quantity(
            long_price=long_price,
            short_price=short_price,
            long_step=long_step,
            short_step=short_step,
            long_min_qty=long_min_qty,
            short_min_qty=short_min_qty,
            long_min_notional=long_min_notional,
            short_min_notional=short_min_notional,
        )

    def _enforce_account_limits(
        self,
        route: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_venue = str(long_leg.get("venue") or "")
        short_venue = str(short_leg.get("venue") or "")
        quantity_result = self._compute_target_quantity(route)
        quantity = float(quantity_result.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "passed": False,
                "reason": str(quantity_result.get("reason") or "quantity_invalid"),
            }
        max_total = int(getattr(self.config, "max_open_positions_total", 1))
        max_per_venue = int(getattr(self.config, "max_open_positions_per_venue", 1))
        max_exposure = float(getattr(self.config, "max_gross_exposure_usd", 1_000.0))
        open_positions = self.store.funding_capture_open_positions()
        active = [
            p for p in open_positions
            if str(p.get("state") or "") not in {
                "CLOSED_PENDING_RECONCILIATION", "RECONCILED",
                "UNRECONCILED", "FAILED",
            }
        ]
        if len(active) >= max_total:
            return {"passed": False, "reason": "max_open_positions_total"}
        venue_counts: dict[str, int] = {}
        current_exposure = 0.0
        for p in active:
            for v in (str(p.get("long_venue") or ""), str(p.get("short_venue") or "")):
                if v:
                    venue_counts[v] = venue_counts.get(v, 0) + 1
            current_exposure += float(p.get("target_notional") or 0.0) * 2.0
        for v in (long_venue, short_venue):
            if v and venue_counts.get(v, 0) >= max_per_venue:
                return {"passed": False, "reason": "max_open_positions_per_venue"}
        long_price = float(long_leg.get("open_vwap") or long_leg.get("vwap") or long_leg.get("mark_price") or 0.0)
        short_price = float(short_leg.get("open_vwap") or short_leg.get("vwap") or short_leg.get("mark_price") or 0.0)
        long_notional = quantity * long_price
        short_notional = quantity * short_price
        if current_exposure + long_notional + short_notional > max_exposure:
            return {"passed": False, "reason": "max_gross_exposure_usd"}
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        reserve_fraction = float(getattr(self.config, "collateral_reserve_fraction", 0.25))
        for side, leg, leg_notional in (
            ("long", long_leg, long_notional),
            ("short", short_leg, short_notional),
        ):
            venue = str(leg.get("venue") or "")
            if leg_notional <= 0:
                return {"passed": False, "reason": f"{side}_price_missing"}
            isolated_collateral = leg_notional / leverage
            collateral_reserve = leg_notional * reserve_fraction
            fee_rate = _leg_fee_rate(leg)
            estimated_open_fee = leg_notional * fee_rate
            required_cash = isolated_collateral + collateral_reserve + estimated_open_fee
            account = accounts.get(venue) or {}
            available = float(account.get("available_balance") or 0.0)
            if available < required_cash:
                return {
                    "passed": False,
                    "reason": "insufficient_paper_balance",
                    "venue": venue,
                    "required": required_cash,
                    "available": available,
                }
        return {"passed": True, "reason": None}

    def _reserve_collateral(
        self,
        capture_id: str,
        route: dict[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> None:
        legs = route.get("legs") or []
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        reserve_fraction = float(getattr(self.config, "collateral_reserve_fraction", 0.25))
        quantity = float(self._target_quantity(route) or 0.0)
        for leg in legs:
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            price = float(leg.get("open_vwap") or leg.get("vwap") or leg.get("mark_price") or 0.0)
            leg_notional = quantity * price
            if leg_notional <= 0:
                continue
            amount = leg_notional / leverage + leg_notional * reserve_fraction
            event_key = collateral_reserve_event_key(capture_id, venue, attempt_id=attempt_id)
            self.store.apply_paper_reserve_event(
                make_ledger_entry(
                    event_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_reserve",
                    cash_delta=0.0,
                    payload={"amount": amount, "attempt_id": attempt_id},
                ),
                reserve_delta=amount,
            )

    def _release_collateral(
        self,
        capture_id: str,
        route: dict[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> None:
        legs = route.get("legs") or []
        for leg in legs:
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            reserve_key = collateral_reserve_event_key(capture_id, venue, attempt_id=attempt_id)
            release_key = collateral_release_event_key(capture_id, venue, attempt_id=attempt_id)
            existing_reserve = self.store.paper_event_ledger_rows(capture_id)
            reserve_row = next(
                (row for row in existing_reserve if row["event_key"] == reserve_key),
                None,
            )
            if reserve_row is None and attempt_id is not None:
                legacy_key = collateral_reserve_event_key(capture_id, venue)
                reserve_row = next(
                    (row for row in existing_reserve if row["event_key"] == legacy_key),
                    None,
                )
            if reserve_row is None:
                continue
            already_released = any(r["event_key"] == release_key for r in existing_reserve)
            if already_released:
                continue
            amount = float(optional_float((reserve_row.get("payload") or {}).get("amount")) or 0.0)
            if amount <= 0:
                continue
            self.store.apply_paper_reserve_event(
                make_ledger_entry(
                    release_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_release",
                    cash_delta=0.0,
                    payload={"amount": amount, "attempt_id": attempt_id},
                ),
                reserve_delta=-amount,
            )

    def _entry_risk_gate(self, route: dict[str, Any]) -> dict[str, Any]:
        legs = route.get("legs") or []
        max_divergence = 0.0
        q = self._target_quantity(route)
        if q <= 0:
            return entry_risk_gates(
                liquidation_distance_fraction=0.0,
                margin_safety_ratio=0.0,
                mark_index_divergence_bps=math.inf,
            )
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        min_liquidation_distance = math.inf
        min_margin_safety = math.inf
        for leg in legs:
            mark = float(leg.get("mark_price") or 0.0)
            index = float(leg.get("index_price") or 0.0)
            entry_price = float(leg.get("open_vwap") or leg.get("vwap") or mark or 0.0)
            if mark <= 0 or index <= 0 or entry_price <= 0:
                return entry_risk_gates(
                    liquidation_distance_fraction=0.0,
                    margin_safety_ratio=0.0,
                    mark_index_divergence_bps=math.inf,
                )
            max_divergence = max(max_divergence, abs(mark - index) / index * 10_000.0)
            notional = q * entry_price
            isolated_collateral = notional / leverage
            mmr = float(leg.get("maintenance_margin_rate") or getattr(self.config, "paper_mmr", 0.02) or 0.02)
            maintenance_margin = notional * mmr
            margin_safety = isolated_collateral / maintenance_margin if maintenance_margin > 0 else 0.0
            min_margin_safety = min(min_margin_safety, margin_safety)
            liquidation_buffer_usd = max(0.0, isolated_collateral - maintenance_margin)
            liquidation_move = liquidation_buffer_usd / max(q, 1e-12)
            if str(leg.get("side") or "").lower() == "long":
                liquidation_price = max(0.0, entry_price - liquidation_move)
                liquidation_distance = max(0.0, (mark - liquidation_price) / mark)
            else:
                liquidation_price = entry_price + liquidation_move
                liquidation_distance = max(0.0, (liquidation_price - mark) / mark)
            min_liquidation_distance = min(min_liquidation_distance, liquidation_distance)
        return entry_risk_gates(
            liquidation_distance_fraction=(
                min_liquidation_distance if math.isfinite(min_liquidation_distance) else 0.0
            ),
            margin_safety_ratio=(min_margin_safety if math.isfinite(min_margin_safety) else 0.0),
            mark_index_divergence_bps=max_divergence,
        )

    def _execute_entry(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        decision_at: datetime,
    ) -> dict[str, Any]:
        q = self._target_quantity(route)
        if q <= 0:
            return {"state": "FAILED", "reason": "zero_quantity", "quantity": 0.0, "target_quantity": 0.0,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_open = float(long_leg.get("open_vwap") or long_leg.get("vwap") or 0.0)
        short_open = float(short_leg.get("open_vwap") or short_leg.get("vwap") or 0.0)
        long_levels = long_leg.get("asks")
        short_levels = short_leg.get("bids")
        if not long_levels:
            return {"state": "FAILED", "reason": "executable_orderbook_missing",
                    "missing_side": "long_asks", "quantity": 0.0, "target_quantity": q,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        if not short_levels:
            return {"state": "FAILED", "reason": "executable_orderbook_missing",
                    "missing_side": "short_bids", "quantity": 0.0, "target_quantity": q,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        attempt_id = self._new_attempt_id(capture_id, decision_at)
        self._mark_entry_attempt_submitted(
            capture_id=capture_id,
            route=route,
            attempt_id=attempt_id,
            settlement_at=settlement_at,
            decision_at=decision_at,
        )
        self._reserve_collateral(capture_id, route, attempt_id=attempt_id)
        submitted_at = decision_at + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        deadline_ok = t20_deadline_passed(filled_at, settlement_at, self.config.entry_fill_deadline_lead_seconds)
        long_fill = simulate_marketable_ioc(long_levels, "buy", q, EXECUTION_HAIRCUT_FRACTION)
        short_fill = simulate_marketable_ioc(short_levels, "sell", q, EXECUTION_HAIRCUT_FRACTION)
        long_fee = long_fill["notional"] * _leg_fee_rate(long_leg)
        short_fee = short_fill["notional"] * _leg_fee_rate(short_leg)
        fill_state = entry_fill_state(
            long_filled_quantity=long_fill["filled_quantity"],
            short_filled_quantity=short_fill["filled_quantity"],
            target_quantity=q,
        )
        state = fill_state["state"] if deadline_ok else "FAILED"
        if not deadline_ok and (long_fill["filled_quantity"] > 0 or short_fill["filled_quantity"] > 0):
            state = "PARTIALLY_HEDGED"
        cycle_id = f"{capture_id}:1"
        for side, leg, fill, fee in (
            ("long", long_leg, long_fill, long_fee),
            ("short", short_leg, short_fill, short_fee),
        ):
            order_id = f"{attempt_id}:entry:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": capture_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "ENTRY",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": decision_at.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": fill["filled_quantity"],
                    "average_fill_price": fill["average_fill_price"],
                    "fee": fee,
                    "state": "FILLED" if fill["unfilled_quantity"] <= 1e-12 else "PARTIALLY_FILLED",
                    "payload": {**fill, "attempt_id": attempt_id},
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id, "attempt_id": attempt_id},
                )
            )
        if state in ("PARTIALLY_HEDGED", "FAILED") and (long_fill["filled_quantity"] > 0 or short_fill["filled_quantity"] > 0):
            self.store.update_funding_capture_position_state(capture_id, "PARTIALLY_HEDGED", filled_at)
            unwind = self._unwind_partial_entry(
                route=route,
                capture_id=capture_id,
                cycle_id=cycle_id,
                long_fill=long_fill,
                short_fill=short_fill,
                long_fee=long_fee,
                short_fee=short_fee,
                decision_at=decision_at,
                now=filled_at,
                attempt_id=attempt_id,
            )
            self._mark_entry_attempt_terminal(
                capture_id=capture_id,
                attempt_id=attempt_id,
                state="REJECTED_AFTER_SUBMISSION",
                now=filled_at,
                reason="partial_or_late_fill_unwound",
            )
            return {
                "state": "FAILED",
                "attempt_id": attempt_id,
                "quantity": 0.0,
                "target_quantity": q,
                "long_entry_price": long_fill["average_fill_price"],
                "short_entry_price": short_fill["average_fill_price"],
                "paper_open_fees": long_fee + short_fee,
                "filled_at": filled_at.isoformat(),
                "deadline_ok": deadline_ok,
                "unwind": unwind,
                **fill_state,
            }
        if state == "FAILED":
            self._release_collateral(capture_id, route, attempt_id=attempt_id)
            self._mark_entry_attempt_terminal(
                capture_id=capture_id,
                attempt_id=attempt_id,
                state="FAILED",
                now=filled_at,
                reason="entry_fill_failed",
            )
            self.store.update_funding_capture_position_state(
                capture_id,
                "FAILED",
                filled_at,
                closed_at=filled_at.isoformat(),
                paper_close_fees=0.0,
                paper_emergency_unwind_cost=0.0,
                paper_net_pnl_estimated=-(long_fee + short_fee),
            )
        return {
            "state": state,
            "attempt_id": attempt_id,
            "quantity": min(long_fill["filled_quantity"], short_fill["filled_quantity"]),
            "target_quantity": q,
            "long_entry_price": long_fill["average_fill_price"],
            "short_entry_price": short_fill["average_fill_price"],
            "paper_open_fees": long_fee + short_fee,
            "filled_at": filled_at.isoformat(),
            "deadline_ok": deadline_ok,
            **fill_state,
        }

    def _unwind_partial_entry(
        self,
        *,
        route: dict[str, Any],
        capture_id: str,
        cycle_id: str,
        long_fill: dict[str, Any],
        short_fill: dict[str, Any],
        long_fee: float,
        short_fee: float,
        decision_at: datetime,
        now: datetime,
        attempt_id: str,
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_unwind_qty = float(long_fill["filled_quantity"])
        short_unwind_qty = float(short_fill["filled_quantity"])
        long_entry_price = float(long_fill["average_fill_price"] or 0.0)
        short_entry_price = float(short_fill["average_fill_price"] or 0.0)
        long_exit_price = long_entry_price * (1.0 - 100.0 / 10_000.0) if long_entry_price > 0 else 0.0
        short_exit_price = short_entry_price * (1.0 + 100.0 / 10_000.0) if short_entry_price > 0 else 0.0
        long_price_pnl = long_unwind_qty * (long_exit_price - long_entry_price) if long_entry_price > 0 else 0.0
        short_price_pnl = short_unwind_qty * (short_entry_price - short_exit_price) if short_entry_price > 0 else 0.0
        total_price_pnl = long_price_pnl + short_price_pnl
        long_unwind_fee = long_unwind_qty * long_exit_price * _leg_fee_rate(long_leg)
        short_unwind_fee = short_unwind_qty * short_exit_price * _leg_fee_rate(short_leg)
        long_adverse_impact_usd = max(0.0, long_unwind_qty * (long_entry_price - long_exit_price))
        short_adverse_impact_usd = max(0.0, short_unwind_qty * (short_exit_price - short_entry_price))
        adverse_impact_usd = long_adverse_impact_usd + short_adverse_impact_usd
        emergency_cost = 0.0
        self.store.update_funding_capture_position_state(capture_id, "EMERGENCY_UNWIND", now)
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        for side, leg, qty, price, fee in (
            ("long", long_leg, long_unwind_qty, long_exit_price, long_unwind_fee),
            ("short", short_leg, short_unwind_qty, short_exit_price, short_unwind_fee),
        ):
            if qty <= 0:
                continue
            order_id = f"{attempt_id}:unwind:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": capture_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "UNWIND",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": qty,
                    "average_fill_price": price,
                    "fee": fee,
                    "state": "FILLED",
                    "payload": {
                        "attempt_id": attempt_id,
                        "adverse_penalty_bps": 100.0,
                        "adverse_price_impact_bps": 100.0,
                        "adverse_price_impact_usd": (
                            long_adverse_impact_usd if side == "long" else short_adverse_impact_usd
                        ),
                        "pricing_quality": "partial_entry_unwind_100bps",
                    },
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id, "intent": "unwind", "attempt_id": attempt_id},
                )
            )
        self._record_price_pnl_entries(
            position_id=capture_id,
            cycle_id=cycle_id,
            long_venue=str(long_leg.get("venue") or ""),
            short_venue=str(short_leg.get("venue") or ""),
            long_price_pnl=long_price_pnl,
            short_price_pnl=short_price_pnl,
            attempt_id=attempt_id,
            payload={
                "reason": "partial_entry_unwind",
                "pricing_quality": "partial_entry_unwind_100bps",
                "adverse_price_impact_bps": 100.0,
                "adverse_price_impact_usd": adverse_impact_usd,
            },
        )
        self._record_ledger_entry(
            make_ledger_entry(
                f"emergency_unwind:{attempt_id}:partial_entry:diagnostic",
                position_id=capture_id,
                cycle_id=cycle_id,
                venue=str(long_leg.get("venue") or short_leg.get("venue") or ""),
                event_type="emergency_unwind_cost",
                cash_delta=0.0,
                payload={
                    "reason": "partial_entry_unwind",
                    "pricing_quality": "partial_entry_unwind_100bps",
                    "adverse_price_impact_bps": 100.0,
                    "adverse_price_impact_usd": adverse_impact_usd,
                    "cash_cost_policy": "penalty_in_fill_price_once",
                    "attempt_id": attempt_id,
                },
            )
        )
        self._release_collateral(capture_id, route, attempt_id=attempt_id)
        self.store.update_funding_capture_position_state(
            capture_id,
            "FAILED",
            now,
            closed_at=now.isoformat(),
            paper_close_fees=long_unwind_fee + short_unwind_fee,
            paper_emergency_unwind_cost=emergency_cost,
            paper_net_pnl_estimated=total_price_pnl - long_fee - short_fee - long_unwind_fee - short_unwind_fee - emergency_cost,
        )
        return {
            "long_unwind_quantity": long_unwind_qty,
            "short_unwind_quantity": short_unwind_qty,
            "long_exit_price": long_exit_price,
            "short_exit_price": short_exit_price,
            "price_pnl": total_price_pnl,
            "long_price_pnl": long_price_pnl,
            "short_price_pnl": short_price_pnl,
            "unwind_fees": long_unwind_fee + short_unwind_fee,
            "emergency_cost": emergency_cost,
            "adverse_price_impact_bps": 100.0,
            "adverse_price_impact_usd": adverse_impact_usd,
        }

    def _mark_open(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        economics: dict[str, Any],
        execution: dict[str, Any],
        now: datetime,
        route_plan: dict[str, Any] | None = None,
    ) -> None:
        legs = route.get("legs") or []
        long_leg = dict(leg_by_side(legs, "long") or {})
        short_leg = dict(leg_by_side(legs, "short") or {})
        long_leg["entry_fill_price"] = execution["long_entry_price"]
        short_leg["entry_fill_price"] = execution["short_entry_price"]
        attempt_id = str(execution.get("attempt_id") or "")
        existing = self.store.funding_capture_position_by_id(capture_id) or {}
        existing_config = dict(existing.get("config") or {})
        self.store.upsert_funding_capture_position(
            {
                "position_id": capture_id,
                "strategy_name": STRATEGY_NAME,
                "strategy_version": STRATEGY_VERSION,
                "canonical_asset": route.get("canonical_asset", ""),
                "long_venue": long_leg.get("venue") or route.get("long_venue") or "",
                "long_symbol": long_leg.get("symbol") or route.get("long_symbol") or "",
                "short_venue": short_leg.get("venue") or route.get("short_venue") or "",
                "short_symbol": short_leg.get("symbol") or route.get("short_symbol") or "",
                "quantity": float(execution["quantity"]),
                "target_notional": float(route.get("target_notional") or self.config.target_notional_per_leg),
                "state": "OPEN",
                "opened_at": now.isoformat(),
                "original_entry_spread": float(execution["short_entry_price"]) - float(execution["long_entry_price"]),
                "paper_open_fees": float(execution["paper_open_fees"]),
                "paper_net_pnl_estimated": economics.get("initial_expected_net_pnl"),
                "config": {
                    **existing_config,
                    "opportunity_id": capture_id,
                    "attempt_id": attempt_id,
                    "entry_attempt_id": attempt_id,
                    "entry_attempt_submitted": True,
                    "entry_attempt_state": "OPEN",
                    "plan_generation": 1,
                    "active_plan_generation": 1,
                    "route_key": route.get("route_key"),
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": [long_leg, short_leg],
                    "funding_route_plan": route_plan,
                    "included_settlement_events": (route_plan or {}).get(
                        "included_settlement_events",
                        [],
                    ),
                    "excluded_settlement_events": (route_plan or {}).get(
                        "excluded_settlement_events",
                        [],
                    ),
                    "ambiguous_settlement_events": (route_plan or {}).get(
                        "ambiguous_settlement_events",
                        [],
                    ),
                    "planned_exit_at": (route_plan or {}).get("planned_exit_at"),
                    "monitor_until": (route_plan or {}).get("monitor_until"),
                    "payment_reconciliation_source": "SIMULATED",
                    "entry_economics": economics,
                    "entry_execution": execution,
                },
            }
        )
        events = list((route_plan or {}).get("included_settlement_events") or [])
        cycle_groups: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            scheduled = str(event.get("scheduled_at") or settlement_at.isoformat())
            cycle_groups.setdefault(scheduled, []).append(event)
        if not cycle_groups:
            cycle_groups = {settlement_at.isoformat(): []}
        scheduled_keys = sorted(
            cycle_groups,
            key=lambda value: parse_time(value) or datetime.max.replace(tzinfo=UTC),
        )
        for cycle_number, scheduled in enumerate(scheduled_keys, start=1):
            cycle_events = cycle_groups[scheduled]
            scheduled_time = parse_time(scheduled) or settlement_at.astimezone(UTC)
            cycle_id = f"{capture_id}:{cycle_number}"
            active_plan = self._cycle_plan_snapshot(
                position_id=capture_id,
                cycle_id=cycle_id,
                cycle_number=cycle_number,
                plan_generation=cycle_number,
                scheduled_at=scheduled_time,
                route=route,
                route_plan=route_plan,
                now=now,
            )
            long_rate = _leg_rate(long_leg)
            short_rate = _leg_rate(short_leg)
            conservative_gross = float(economics.get("conservative_funding_gross") or 0.0)
            if cycle_events:
                conservative_gross = sum(
                    float(event.get("conservative_cashflow_usd") or 0.0)
                    for event in cycle_events
                )
                for event in cycle_events:
                    event_side = str(event.get("leg_id") or "")
                    if event_side.endswith(":long") or ":long:" in event_side:
                        long_rate = float(event.get("rate_per_next_settlement") or long_rate)
                    elif event_side.endswith(":short") or ":short:" in event_side:
                        short_rate = float(event.get("rate_per_next_settlement") or short_rate)
            self.store.upsert_funding_capture_cycle(
                {
                    "cycle_id": cycle_id,
                    "position_id": capture_id,
                    "cycle_number": cycle_number,
                    "plan_generation": cycle_number,
                    "scheduled_funding_at": scheduled,
                    "long_next_funding_rate_at_decision": long_rate,
                    "short_next_funding_rate_at_decision": short_rate,
                    "conservative_funding_gross": conservative_gross,
                    "conservative_funding_edge_bps": economics.get("conservative_funding_edge_bps"),
                    "state": "OPEN",
                    "decision": "OPEN",
                    "decision_reason": "event_window_entry_legs_filled_before_t20",
                    "active_plan": active_plan,
                }
            )

    def collect_hold_observation(
        self,
        position: dict[str, Any],
        route: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at")) or now
        observation = build_focused_observation(
            route,
            now=now,
            phase="hold",
            max_age_seconds=2.0,
            max_response_skew_seconds=float(
                self.config.max_cross_venue_snapshot_skew_seconds
            ),
        )
        self._store_observation(
            route,
            position_id,
            scheduled,
            observation,
            phase="hold",
            cycle_id=cycle_id,
        )
        return observation

    def _probe_state(self, position: dict[str, Any]) -> dict[str, Any]:
        config = dict(position.get("config") or {})
        return dict(config.get("post_settlement_probes") or {})

    def _missing_required_route_data(self, route: dict[str, Any]) -> list[str]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        missing: list[str] = []
        for side, leg in (("long", long_leg), ("short", short_leg)):
            if optional_float(leg.get("mark_price")) is None:
                missing.append(f"{side}_mark_price_missing")
            if optional_float(leg.get("index_price")) is None:
                missing.append(f"{side}_index_price_missing")
            if optional_float(leg.get("fee_rate")) is None and optional_float(leg.get("taker_fee_rate")) is None:
                missing.append(f"{side}_fee_rate_missing")
            fee_status = fee_evidence_status(leg, "taker")
            if not bool(fee_status.get("verified")):
                missing.append(
                    f"{side}_{fee_status.get('blocker') or 'fee_provenance_unverified'}"
                )
            if optional_float(leg.get("normalized_next_funding_rate")) is None:
                missing.append(f"{side}_normalized_next_funding_rate_missing")
        return missing

    def _save_probe_state(
        self,
        position: dict[str, Any],
        probe_state: dict[str, Any],
    ) -> None:
        position_id = str(position["position_id"])
        config = dict(position.get("config") or {})
        config["post_settlement_probes"] = probe_state
        position["config"] = config
        self.store.update_funding_capture_position_config(position_id, config)

    def _record_schedule_probe(
        self,
        position: dict[str, Any],
        route: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_response = _leg_response_time(long_leg)
        short_response = _leg_response_time(short_leg)
        now_utc = now.astimezone(UTC)
        long_age = (
            (now_utc - long_response).total_seconds()
            if long_response is not None
            else math.inf
        )
        short_age = (
            (now_utc - short_response).total_seconds()
            if short_response is not None
            else math.inf
        )
        cross_skew = (
            abs((long_response - short_response).total_seconds())
            if long_response is not None and short_response is not None
            else math.inf
        )
        long_next = parse_time(long_leg.get("next_funding_at"))
        short_next = parse_time(short_leg.get("next_funding_at"))
        next_funding_skew_seconds = (
            abs((long_next.astimezone(UTC) - short_next.astimezone(UTC)).total_seconds())
            if long_next is not None and short_next is not None
            else None
        )
        targeted = (route.get("evidence") or {}).get("targeted_refresh") or {}
        snapshot_id = targeted.get("snapshot_id")
        fresh = (
            long_age <= 2.0
            and short_age <= 2.0
            and cross_skew <= 1.0
            and optional_float(long_leg.get("normalized_next_funding_rate")) is not None
            and optional_float(short_leg.get("normalized_next_funding_rate")) is not None
            and targeted.get("quality") == "FRESH"
            and bool(snapshot_id)
        )
        probe = {
            "observed_at": now.astimezone(UTC).isoformat(),
            "long_next_funding_at": long_leg.get("next_funding_at"),
            "short_next_funding_at": short_leg.get("next_funding_at"),
            "next_funding_skew_seconds": next_funding_skew_seconds,
            "fresh": fresh,
            "long_response_age_seconds": long_age,
            "short_response_age_seconds": short_age,
            "cross_venue_skew_seconds": cross_skew,
            "route_snapshot_id": snapshot_id,
            "invalid_reason": None
            if fresh
            else "schedule_probe_not_fresh_targeted_snapshot",
        }
        probe_state = self._probe_state(position)
        probes = list(probe_state.get("probes") or [])
        probes.append(probe)
        if len(probes) > 30:
            probes = probes[-30:]
        probe_state["probes"] = probes
        probe_state["latest_probe_at"] = now.astimezone(UTC).isoformat()
        if not probe_state.get("first_probe_at"):
            probe_state["first_probe_at"] = now.astimezone(UTC).isoformat()
        self._save_probe_state(position, probe_state)
        return probe_state

    def _route_collateral_asset(self, position: dict[str, Any], route: dict[str, Any] | None) -> str:
        candidates: list[str] = []
        for leg in (route or {}).get("legs") or []:
            if leg.get("collateral_asset"):
                candidates.append(str(leg.get("collateral_asset")).upper())
        config = position.get("config") or {}
        if config.get("collateral_asset"):
            candidates.append(str(config.get("collateral_asset")).upper())
        for leg in config.get("entry_legs") or []:
            if leg.get("collateral_asset"):
                candidates.append(str(leg.get("collateral_asset")).upper())
        unique = {value for value in candidates if value}
        if len(unique) == 1:
            return next(iter(unique))
        return "UNKNOWN"

    def _hold_history_reliability(
        self,
        *,
        position: dict[str, Any],
        route: dict[str, Any],
        now: datetime,
        wait_seconds: float,
    ) -> dict[str, Any]:
        since = (
            now.astimezone(UTC)
            - timedelta(days=int(getattr(self.config, "hold_history_window_days", 30)))
        ).isoformat()
        collateral = self._route_collateral_asset(position, route)
        history_rows = self.store.reconciled_funding_capture_hold_cycles(
            canonical_asset=str(position.get("canonical_asset") or route.get("canonical_asset") or ""),
            long_venue=str(position.get("long_venue") or route.get("long_venue") or ""),
            short_venue=str(position.get("short_venue") or route.get("short_venue") or ""),
            collateral_asset=collateral,
            wait_bucket=wait_bucket_for_seconds(wait_seconds),
            since=since,
            exclude_position_id=str(position.get("position_id") or ""),
            limit=int(getattr(self.config, "hold_history_max_cycles", 20)),
        )
        reliability = evaluate_hold_history_reliability(
            history_rows,
            min_cycles_for_gate=int(getattr(self.config, "hold_history_min_cycles_for_gate", 8)),
            insufficient_multiplier=float(getattr(self.config, "hold_history_insufficient_multiplier", 0.75)),
            min_positive_realization_rate=float(
                getattr(self.config, "hold_history_min_positive_realization_rate", 0.70)
            ),
            min_p25_realization_ratio=float(
                getattr(self.config, "hold_history_min_p25_realization_ratio", 0.50)
            ),
            max_extra_cycles_when_insufficient=int(
                getattr(self.config, "hold_history_max_extra_cycles_when_insufficient", 1)
            ),
        )
        return {
            **reliability.as_dict(),
            "collateral_asset": collateral,
            "wait_bucket": wait_bucket_for_seconds(wait_seconds),
        }

    def _adverse_basis_changes_from_observations(
        self,
        observations: list[dict[str, Any]],
        *,
        window_seconds: float = 30.0,
    ) -> list[float]:
        dated: list[tuple[datetime, float, float]] = []
        for row in observations:
            observed_at = parse_time(row.get("observed_at"))
            spread = optional_float(row.get("current_exit_spread"))
            long_mark = optional_float(row.get("long_mark"))
            short_mark = optional_float(row.get("short_mark"))
            if (
                observed_at is None
                or spread is None
                or long_mark is None
                or short_mark is None
                or long_mark <= 0
                or short_mark <= 0
            ):
                continue
            reference = (float(long_mark) + float(short_mark)) / 2.0
            dated.append((observed_at.astimezone(UTC), float(spread), reference))
        dated.sort(key=lambda item: item[0])
        changes: list[float] = []
        for idx, (current_time, current_spread, reference) in enumerate(dated):
            previous_candidates = [
                item
                for item in dated[:idx]
                if (current_time - item[0]).total_seconds() >= window_seconds
            ]
            if not previous_candidates:
                continue
            previous_time, previous_spread, _previous_reference = previous_candidates[-1]
            if (current_time - previous_time).total_seconds() > window_seconds * 2:
                continue
            changes.append(max(0.0, (current_spread - previous_spread) / reference * 10_000.0))
        return changes

    def _p95_abs_mark_return_1s_from_observations(
        self,
        observations: list[dict[str, Any]],
    ) -> float | None:
        dated: list[tuple[datetime, float, float]] = []
        for row in observations:
            observed_at = parse_time(row.get("observed_at"))
            long_mark = optional_float(row.get("long_mark"))
            short_mark = optional_float(row.get("short_mark"))
            if (
                observed_at is None
                or long_mark is None
                or short_mark is None
                or long_mark <= 0
                or short_mark <= 0
            ):
                continue
            dated.append((observed_at.astimezone(UTC), float(long_mark), float(short_mark)))
        dated.sort(key=lambda item: item[0])
        values: list[float] = []
        for idx, (current_time, current_long, current_short) in enumerate(dated):
            previous_candidates = [
                item
                for item in dated[:idx]
                if (current_time - item[0]).total_seconds() >= 1.0
            ]
            if not previous_candidates:
                continue
            previous_time, previous_long, previous_short = previous_candidates[-1]
            if (current_time - previous_time).total_seconds() > 2.0:
                continue
            long_return = abs(current_long / previous_long - 1.0) * 10_000.0
            short_return = abs(current_short / previous_short - 1.0) * 10_000.0
            values.append(max(long_return, short_return))
        if len(values) < 10:
            return None
        from smart_money_radar.funding.strategy_synchronized_funding import percentile_95
        return percentile_95(values)

    def next_cycle_hold_or_close_decision(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at"))
        if scheduled is None:
            return {"decision": "close", "reason": "current_cycle_settlement_missing"}
        seconds_after = (now.astimezone(UTC) - scheduled.astimezone(UTC)).total_seconds()
        position_id = str(position["position_id"])
        state = str(position.get("state") or "")

        if seconds_after < POST_SETTLEMENT_EVALUATION_START_SECONDS:
            return {
                "decision": "wait",
                "reason": "post_settlement_evaluation_not_started",
                "seconds_after_settlement": seconds_after,
            }

        if state == "SETTLEMENT_CROSSED":
            self.store.update_funding_capture_position_state(
                position_id, "POST_SETTLEMENT_EVALUATION", now,
            )
            position = {**position, "state": "POST_SETTLEMENT_EVALUATION"}

        config = dict(position.get("config") or {})
        planned_exit = parse_time(config.get("planned_exit_at"))
        route_plan = config.get("funding_route_plan") or {}
        included_events = [
            event
            for event in list(route_plan.get("included_settlement_events") or [])
            if isinstance(event, dict)
        ]
        if included_events and planned_exit is not None:
            cycles = self.store.funding_capture_cycles_for_position(position_id)
            unfinished_cycles = [
                cycle
                for cycle in cycles
                if str(cycle.get("state") or "") not in {
                    "SETTLEMENT_CROSSED",
                    "PUBLIC_RATE_CONFIRMED",
                    "RATE_AND_MARK_RECONCILED",
                    "RECONCILED",
                    "UNRECONCILED",
                }
                and (
                    parse_time(cycle.get("scheduled_funding_at")) is None
                    or parse_time(cycle.get("scheduled_funding_at")).astimezone(UTC)
                    > scheduled.astimezone(UTC)
                )
            ]
            if unfinished_cycles:
                return {
                    "decision": "wait",
                    "reason": "included_settlement_events_pending",
                    "unfinished_cycle_count": len(unfinished_cycles),
                }

        if route is None:
            if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {"decision": "close", "reason": "post_settlement_route_missing"}
            return {
                "decision": "wait",
                "reason": "post_settlement_route_missing_waiting",
                "seconds_after_settlement": seconds_after,
            }

        missing_data = self._missing_required_route_data(route)
        if missing_data:
            if seconds_after < POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {
                    "decision": "wait",
                    "reason": "required_route_data_missing_waiting_until_t20",
                    "missing": missing_data,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "close",
                "reason": "required_route_data_missing",
                "missing": missing_data,
            }

        next_plan = self._plan_dict_for_route(route, now)
        next_plan_blockers = self._route_plan_blocking_reasons(next_plan)
        next_plan_blockers.extend(self._paper_capability_blocking_reasons(next_plan))
        next_plan_blockers = list(dict.fromkeys(next_plan_blockers))
        next_settlement_at = self._first_included_settlement_at(next_plan) or route_next_settlement(route)
        next_cycle_number = int(position.get("current_cycle_number") or 1) + 1
        replan_record = {
            "plan_generation": next_cycle_number,
            "previous_cycle_id": str(position.get("current_cycle_id") or f"{position_id}:1"),
            "observed_at": now.astimezone(UTC).isoformat(),
            "previous_scheduled_funding_at": scheduled.astimezone(UTC).isoformat(),
            "new_scheduled_funding_at": (
                next_settlement_at.astimezone(UTC).isoformat()
                if next_settlement_at is not None
                else None
            ),
            "blockers": next_plan_blockers,
            "route_plan": next_plan,
        }
        replans = list(config.get("post_settlement_replans") or [])
        replans = [
            item
            for item in replans
            if int(item.get("plan_generation") or 0) != next_cycle_number
        ]
        replans.append(replan_record)
        config["post_settlement_replans"] = replans[-10:]
        config["proposed_plan_generation"] = next_cycle_number
        config["proposed_post_settlement_route_plan"] = next_plan
        position["config"] = config
        self.store.update_funding_capture_position_config(position_id, config, now)
        if next_settlement_at is None:
            if seconds_after < POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {
                    "decision": "wait",
                    "reason": "next_cycle_plan_settlement_missing_waiting_until_t20",
                    "route_plan": next_plan,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "close",
                "reason": "next_cycle_plan_settlement_missing",
                "route_plan": next_plan,
            }
        if (
            next_settlement_at.astimezone(UTC) <= scheduled.astimezone(UTC)
            or next_settlement_at.astimezone(UTC) <= now.astimezone(UTC)
        ):
            return {
                "decision": "close",
                "reason": "next_cycle_stale_or_past_settlement",
                "route_plan": next_plan,
                "new_scheduled_funding_at": next_settlement_at.astimezone(UTC).isoformat(),
                "previous_scheduled_funding_at": scheduled.astimezone(UTC).isoformat(),
            }
        if next_plan_blockers:
            if seconds_after < POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {
                    "decision": "wait",
                    "reason": "next_cycle_plan_blocked_waiting_until_t20",
                    "blockers": next_plan_blockers,
                    "route_plan": next_plan,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "close",
                "reason": "next_cycle_plan_blocked",
                "blockers": next_plan_blockers,
                "route_plan": next_plan,
            }

        probe_state = self._probe_state(position)
        if seconds_after <= POST_SETTLEMENT_PROBE_END_SECONDS:
            probe_state = self._record_schedule_probe(position, route, now)
        self.collect_hold_observation(position, route, now)

        probes = list(probe_state.get("probes") or [])
        probe_decision = post_settlement_probe_decision(
            probes=probes,
            now=now.astimezone(UTC),
            settlement_at=scheduled.astimezone(UTC),
        )

        if probe_decision.get("decision") == "close_at_t20":
            if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                probe_state["mismatch_reason"] = probe_decision.get("reason", "schedule_mismatch")
                self._save_probe_state(position, probe_state)
                return {
                    "decision": "close",
                    "reason": "next_settlement_schedule_mismatch",
                    "probe_decision": probe_decision,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "wait",
                "reason": "awaiting_t20_close_window",
                "probe_decision": probe_decision,
                "seconds_after_settlement": seconds_after,
            }

        schedule: dict[str, Any] | None = None
        if probe_decision.get("decision") == "hold_next_cycle":
            schedule = next_cycle_schedule_decision(
                long_next_funding_at=probe_decision.get("long_next_funding_at"),
                short_next_funding_at=probe_decision.get("short_next_funding_at"),
                now=now,
                min_wait_seconds=float(getattr(self.config, "min_next_settlement_wait_seconds", 300)),
                max_wait_seconds=float(getattr(self.config, "max_next_settlement_wait_seconds", 14_400)),
            )
            if not schedule["hold_schedule"]:
                if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                    return {
                        "decision": "close",
                        "reason": "next_cycle_schedule_failed",
                        "schedule": schedule,
                        "probe_decision": probe_decision,
                    }
                return {
                    "decision": "wait",
                    "reason": "next_cycle_schedule_failed_waiting_until_t20",
                    "schedule": schedule,
                    "probe_decision": probe_decision,
                }
            probe_decision = {
                **probe_decision,
                "seconds_to_next_cycle": schedule.get("seconds_to_next_cycle"),
            }
            if probe_state.get("aligned_next_settlement_at") != probe_decision.get("next_cycle_at"):
                probe_state["aligned_next_settlement_at"] = probe_decision.get("next_cycle_at")
                probe_state["consecutive_agreements"] = probe_decision.get("consecutive_agreements")
                self._save_probe_state(position, probe_state)

        if seconds_after < POST_SETTLEMENT_HOLD_DECISION_SECONDS:
            return {
                "decision": "wait",
                "reason": "collecting_next_cycle_observations",
                "seconds_after_settlement": seconds_after,
                "probe_decision": probe_decision,
            }

        if probe_decision.get("decision") not in ("hold_next_cycle",):
            return {
                "decision": "close",
                "reason": "next_settlement_schedule_not_confirmed",
                "probe_decision": probe_decision,
                "seconds_after_settlement": seconds_after,
            }

        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        long_leg = leg_by_side(route.get("legs") or [], "long") or {}
        short_leg = leg_by_side(route.get("legs") or [], "short") or {}

        observations = self._valid_observations(route, now, phase="hold", cycle_id=cycle_id)
        observation_decision = next_cycle_observation_decision(observations=observations, now=now)
        plan_conservative_gross = optional_float(
            (next_plan or {}).get("conservative_funding_cashflow_usd")
        )
        if plan_conservative_gross is not None:
            observation_decision = {
                **observation_decision,
                "conservative_funding_gross": plan_conservative_gross,
                "route_plan_conservative_funding_cashflow_usd": plan_conservative_gross,
            }
        if not observation_decision["eligible"]:
            return {
                "decision": "close",
                "reason": "next_cycle_observations_failed",
                "observation_decision": observation_decision,
            }

        quantity = float(position.get("quantity") or 0.0)
        current_pnl_snapshot = self.record_current_executable_pnl(position, route, now)
        if current_pnl_snapshot.get("quality") != "EXECUTABLE_FULL_DEPTH":
            return {
                "decision": "close",
                "reason": "current_executable_pnl_unavailable",
                "current_executable_pnl": current_pnl_snapshot,
            }
        current_close_fees = float(current_pnl_snapshot.get("paper_close_fees") or 0.0)
        reference_notional = min(
            quantity * max(0.0, float(current_pnl_snapshot.get("long_exit_price") or 0.0)),
            quantity * max(0.0, float(current_pnl_snapshot.get("short_exit_price") or 0.0)),
        )
        entry_economics = (position.get("config") or {}).get("entry_economics") or {}
        entry_basis_reserve_bps = float(entry_economics.get("entry_basis_reserve_bps") or 0.0)
        if entry_basis_reserve_bps <= 0:
            entry_basis_reserve_bps = 30.0
        adverse_basis_changes = self._adverse_basis_changes_from_observations(observations)
        p95_mark_return = self._p95_abs_mark_return_1s_from_observations(observations)
        wait_seconds = float(
            probe_decision.get("seconds_to_next_cycle")
            or schedule_seconds_to_next(probe_decision, now)
            or 0.0
        )
        unadjusted_next_conservative = float(observation_decision["conservative_funding_gross"])
        hold_history = self._hold_history_reliability(
            position=position,
            route=route,
            now=now,
            wait_seconds=wait_seconds,
        )
        history_adjusted_next_funding = unadjusted_next_conservative * float(
            hold_history["history_multiplier"]
        )
        hold = hold_economics(
            next_conservative_funding_gross=history_adjusted_next_funding,
            current_close_fees=current_close_fees,
            reference_notional=reference_notional,
            wait_seconds=wait_seconds,
            entry_basis_reserve_bps=entry_basis_reserve_bps,
            adverse_basis_change_30s_bps=adverse_basis_changes if len(adverse_basis_changes) >= 10 else None,
            p95_abs_mark_return_1s_bps=p95_mark_return,
        )
        hold["unadjusted_next_conservative_funding_gross"] = unadjusted_next_conservative
        hold["history_adjusted_next_funding"] = history_adjusted_next_funding
        hold["hold_history"] = hold_history
        current_executable_pnl = float(current_pnl_snapshot["paper_net_if_exit_now"])
        projected_total = current_executable_pnl + float(
            hold["incremental_hold_net_pnl"]
        )
        opened_at = parse_time(position.get("opened_at")) or now
        next_cycle_time = parse_time(probe_decision.get("next_cycle_at"))
        projected_age = (
            (next_cycle_time.astimezone(UTC) - opened_at.astimezone(UTC)).total_seconds()
            + POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS
            if next_cycle_time is not None
            else math.inf
        )
        settlements_count = int(position.get("settlements_captured_count") or 0)
        reference = max(0.0, reference_notional)
        reasons: list[str] = []
        if not bool(getattr(self.config, "hold_enabled", True)):
            reasons.append("hold_disabled")
        if not bool(hold_history.get("hold_history_gate_passed")):
            reasons.extend(list(hold_history.get("reasons") or []))
        if (
            hold_history.get("history_status") == "INSUFFICIENT"
            and max(0, settlements_count - 1)
            >= int(hold_history.get("hold_history_max_extra_cycles_when_insufficient") or 0)
        ):
            reasons.append("hold_history_insufficient_extra_cycle_limit")
        if history_adjusted_next_funding < max(2.50, reference * 0.005):
            reasons.append("next_conservative_funding_below_minimum")
        if float(hold["incremental_hold_net_pnl"]) < max(1.00, reference * 0.002):
            reasons.append("incremental_hold_net_below_minimum")
        if float(hold["hold_cost_coverage_ratio"]) < 1.50:
            reasons.append("hold_coverage_below_minimum")
        if projected_total < 0:
            reasons.append("projected_total_after_next_cycle_negative")
        if settlements_count >= int(getattr(self.config, "max_settlements_per_position", 4)):
            reasons.append("max_settlements_reached")
        if projected_age > float(getattr(self.config, "max_position_age_seconds", 14_700)):
            reasons.append("max_position_age_exceeded")
        if reasons:
            return {
                "decision": "close",
                "reason": "hold_economics_failed",
                "reasons": reasons,
                "observation_decision": observation_decision,
                "hold_economics": hold,
                "current_executable_pnl": current_pnl_snapshot,
                "projected_total_after_next_cycle": projected_total,
                "projected_position_age_at_next_exit": projected_age,
            }

        next_cycle_id = f"{position_id}:{next_cycle_number}"
        active_config = dict(config)
        active_config.update(
            {
                "plan_generation": next_cycle_number,
                "active_plan_generation": next_cycle_number,
                "active_cycle_id": next_cycle_id,
                "active_cycle_number": next_cycle_number,
                "active_cycle_scheduled_funding_at": next_settlement_at.astimezone(UTC).isoformat(),
                "funding_route_plan": next_plan,
                "included_settlement_events": next_plan.get("included_settlement_events") or [],
                "excluded_settlement_events": next_plan.get("excluded_settlement_events") or [],
                "ambiguous_settlement_events": next_plan.get("ambiguous_settlement_events") or [],
                "planned_exit_at": next_plan.get("planned_exit_at"),
                "monitor_until": next_plan.get("monitor_until"),
                "last_post_settlement_route_plan": next_plan,
            }
        )
        active_plan = self._cycle_plan_snapshot(
            position_id=position_id,
            cycle_id=next_cycle_id,
            cycle_number=next_cycle_number,
            plan_generation=next_cycle_number,
            scheduled_at=next_settlement_at,
            route=route,
            route_plan=next_plan,
            now=now,
        )
        next_cycle_id = self.store.activate_funding_capture_hold_cycle(
            cycle_row={
                "cycle_id": next_cycle_id,
                "position_id": position_id,
                "cycle_number": next_cycle_number,
                "plan_generation": next_cycle_number,
                "scheduled_funding_at": next_settlement_at.astimezone(UTC).isoformat(),
                "long_next_funding_rate_at_decision": _leg_rate(long_leg),
                "short_next_funding_rate_at_decision": _leg_rate(short_leg),
                "conservative_funding_gross": observation_decision["conservative_funding_gross"],
                "conservative_funding_edge_bps": (
                    float(observation_decision["conservative_funding_gross"]) / reference * 10_000.0
                    if reference > 0
                    else 0.0
                ),
                "hold_basis_reserve_bps": hold["hold_basis_reserve_bps"],
                "hold_legging_reserve_bps": hold["hold_legging_reserve_bps"],
                "hold_time_reserve_bps": hold["hold_time_reserve_bps"],
                "hold_liquidity_reserve_bps": hold["hold_liquidity_reserve_bps"],
                "incremental_hold_cost": hold["incremental_hold_cost"],
                "incremental_hold_net_pnl": hold["incremental_hold_net_pnl"],
                "hold_cost_coverage_ratio": hold["hold_cost_coverage_ratio"],
                "paper_net_if_exit_at_decision": current_executable_pnl,
                "decision": "HOLD",
                "decision_reason": "next_cycle_underwriting_passed",
                "state": "HOLDING_NEXT_CYCLE",
                "active_plan": active_plan,
            },
            active_config=active_config,
            now=now,
            paper_net_pnl_estimated=projected_total,
        )
        return {
            "decision": "hold",
            "reason": "next_cycle_underwriting_passed",
            "cycle_id": next_cycle_id,
            "cycle_number": next_cycle_number,
            "route_plan": next_plan,
            "observation_decision": observation_decision,
            "hold_economics": hold,
            "current_executable_pnl": current_pnl_snapshot,
            "projected_total_after_next_cycle": projected_total,
        }

    def close_position(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
        *,
        reason: str,
        emergency: bool = False,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        attempt_id = _position_attempt_id(position)
        quantity = float(position.get("quantity") or 0.0)
        route_legs = route.get("legs") if route else None
        long_route_leg = leg_by_side(route_legs or [], "long") or {}
        short_route_leg = leg_by_side(route_legs or [], "short") or {}
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        long_entry_price = float(
            long_entry_leg.get("entry_fill_price") or long_entry_leg.get("vwap") or 0.0
        )
        short_entry_price = float(
            short_entry_leg.get("entry_fill_price") or short_entry_leg.get("vwap") or 0.0
        )
        pricing_quality = "executable_book"
        long_has_book = bool(long_route_leg.get("bids"))
        short_has_book = bool(short_route_leg.get("asks"))
        if quantity <= 0:
            return {"decision": "rejected", "reason": "zero_open_quantity", "state": str(position.get("state") or "")}
        if not emergency:
            if not long_has_book or not short_has_book:
                return {
                    "decision": "rejected",
                    "reason": "executable_orderbook_missing",
                    "state": str(position.get("state") or ""),
                    "missing_long_bids": not long_has_book,
                    "missing_short_asks": not short_has_book,
                }
        else:
            self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)

        def fallback_reference(leg: dict[str, Any], entry_leg: dict[str, Any]) -> tuple[float, str]:
            mark = optional_float(leg.get("mark_price")) or optional_float(entry_leg.get("mark_price"))
            if mark is not None and mark > 0:
                return float(mark), "fallback_mark_300bps"
            entry_price = optional_float(entry_leg.get("entry_fill_price")) or optional_float(entry_leg.get("vwap"))
            if entry_price is not None and entry_price > 0:
                return float(entry_price), "fallback_entry_300bps"
            return 0.0, "emergency_pricing_unavailable"

        long_levels = list(long_route_leg.get("bids") or [])
        short_levels = list(short_route_leg.get("asks") or [])
        if emergency and not long_levels:
            reference, quality = fallback_reference(long_route_leg, long_entry_leg)
            if reference <= 0:
                return {"decision": "rejected", "reason": quality, "state": "EMERGENCY_UNWIND"}
            pricing_quality = quality
            long_levels = _synthetic_levels(reference * (1.0 - 300.0 / 10_000.0), quantity)
        if emergency and not short_levels:
            reference, quality = fallback_reference(short_route_leg, short_entry_leg)
            if reference <= 0:
                return {"decision": "rejected", "reason": quality, "state": "EMERGENCY_UNWIND"}
            pricing_quality = "fallback_entry_300bps" if quality == "fallback_entry_300bps" else pricing_quality
            if pricing_quality == "executable_book":
                pricing_quality = quality
            short_levels = _synthetic_levels(reference * (1.0 + 300.0 / 10_000.0), quantity)

        self.store.update_funding_capture_position_state(position_id, "EXIT_SUBMITTED", now)
        long_exit = simulate_marketable_ioc(long_levels, "sell", quantity, EXECUTION_HAIRCUT_FRACTION)
        short_exit = simulate_marketable_ioc(short_levels, "buy", quantity, EXECUTION_HAIRCUT_FRACTION)
        long_fee_rate = _leg_fee_rate(long_route_leg) or _leg_fee_rate(long_entry_leg)
        short_fee_rate = _leg_fee_rate(short_route_leg) or _leg_fee_rate(short_entry_leg)
        long_fee = long_exit["notional"] * long_fee_rate
        short_fee = short_exit["notional"] * short_fee_rate
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        close_fees_by_side: dict[str, float] = {"long": long_fee, "short": short_fee}
        close_notional_by_side: dict[str, float] = {
            "long": float(long_exit["notional"] or 0.0),
            "short": float(short_exit["notional"] or 0.0),
        }
        closed_quantity_by_side: dict[str, float] = {
            "long": float(long_exit["filled_quantity"] or 0.0),
            "short": float(short_exit["filled_quantity"] or 0.0),
        }
        for side, leg, fill, fee in (
            ("long", long_route_leg or long_entry_leg, long_exit, long_fee),
            ("short", short_route_leg or short_entry_leg, short_exit, short_fee),
        ):
            order_id = f"{attempt_id or position_id}:exit:{cycle_id}:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "EXIT",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": fill["filled_quantity"],
                    "average_fill_price": fill["average_fill_price"],
                    "fee": fee,
                    "state": "FILLED" if fill["unfilled_quantity"] <= 1e-12 else "PARTIALLY_FILLED",
                    "payload": {**fill, "attempt_id": attempt_id},
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id, "reason": reason, "attempt_id": attempt_id},
                )
            )

        residual_diagnostics: list[dict[str, Any]] = []

        def residual_price(
            *,
            side: str,
            leg: dict[str, Any],
            entry_leg: dict[str, Any],
            levels: list[list[float]],
            residual_qty: float,
        ) -> tuple[float, dict[str, Any]]:
            if side == "long":
                valid_prices = [float(level[0]) for level in levels if len(level) >= 2 and float(level[1]) > 0]
                if valid_prices:
                    reference = min(valid_prices)
                    price = reference * (1.0 - 100.0 / 10_000.0)
                    quality = "residual_book_100bps"
                    penalty = 100.0
                    impact = max(0.0, residual_qty * (reference - price))
                    return price, {
                        "pricing_quality": quality,
                        "adverse_price_impact_bps": penalty,
                        "adverse_price_impact_usd": impact,
                        "reference_price": reference,
                    }
                reference, quality = fallback_reference(leg, entry_leg)
                if reference <= 0:
                    return 0.0, {"pricing_quality": quality}
                price = reference * (1.0 - 300.0 / 10_000.0)
                return price, {
                    "pricing_quality": quality,
                    "adverse_price_impact_bps": 300.0,
                    "adverse_price_impact_usd": max(0.0, residual_qty * (reference - price)),
                    "reference_price": reference,
                }
            valid_prices = [float(level[0]) for level in levels if len(level) >= 2 and float(level[1]) > 0]
            if valid_prices:
                reference = max(valid_prices)
                price = reference * (1.0 + 100.0 / 10_000.0)
                return price, {
                    "pricing_quality": "residual_book_100bps",
                    "adverse_price_impact_bps": 100.0,
                    "adverse_price_impact_usd": max(0.0, residual_qty * (price - reference)),
                    "reference_price": reference,
                }
            reference, quality = fallback_reference(leg, entry_leg)
            if reference <= 0:
                return 0.0, {"pricing_quality": quality}
            price = reference * (1.0 + 300.0 / 10_000.0)
            return price, {
                "pricing_quality": quality,
                "adverse_price_impact_bps": 300.0,
                "adverse_price_impact_usd": max(0.0, residual_qty * (price - reference)),
                "reference_price": reference,
            }

        residual_specs = (
            ("long", long_route_leg or long_entry_leg, long_entry_leg, long_levels, long_exit, long_fee_rate),
            ("short", short_route_leg or short_entry_leg, short_entry_leg, short_levels, short_exit, short_fee_rate),
        )
        if any(float(fill["unfilled_quantity"] or 0.0) > 1e-12 for _, _, _, _, fill, _ in residual_specs):
            self.store.update_funding_capture_position_state(position_id, "PARTIALLY_CLOSED", now)
        for side, leg, entry_leg, levels, fill, fee_rate in residual_specs:
            residual_qty = float(fill["unfilled_quantity"] or 0.0)
            if residual_qty <= 1e-12:
                continue
            price, diagnostic = residual_price(
                side=side,
                leg=leg,
                entry_leg=entry_leg,
                levels=levels,
                residual_qty=residual_qty,
            )
            if price <= 0:
                self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)
                return {
                    "decision": "failed",
                    "reason": "residual_pricing_unavailable",
                    "state": "EMERGENCY_UNWIND",
                    "residual_side": side,
                    "pricing_quality": diagnostic.get("pricing_quality"),
                }
            residual_order_id = f"{attempt_id or position_id}:residual:{cycle_id}:{side}"
            residual_notional = residual_qty * price
            residual_fee = residual_notional * fee_rate
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": residual_order_id,
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "RESIDUAL_UNWIND",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": residual_qty,
                    "average_fill_price": price,
                    "fee": residual_fee,
                    "state": "FILLED",
                    "payload": {
                        **diagnostic,
                        "reason": reason,
                        "residual_quantity": residual_qty,
                        "cash_cost_policy": "penalty_in_fill_price_once",
                        "attempt_id": attempt_id,
                    },
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(residual_order_id),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-residual_fee,
                    payload={
                        "order_id": residual_order_id,
                        "reason": reason,
                        "intent": "residual_unwind",
                        "attempt_id": attempt_id,
                    },
                )
            )
            close_fees_by_side[side] += residual_fee
            close_notional_by_side[side] += residual_notional
            closed_quantity_by_side[side] += residual_qty
            residual_diagnostics.append({"side": side, **diagnostic, "residual_quantity": residual_qty})

        if (
            abs(closed_quantity_by_side["long"] - quantity) > 1e-12
            or abs(closed_quantity_by_side["short"] - quantity) > 1e-12
        ):
            self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)
            return {
                "decision": "failed",
                "reason": "residual_exposure_remaining",
                "state": "EMERGENCY_UNWIND",
                "long_closed_quantity": closed_quantity_by_side["long"],
                "short_closed_quantity": closed_quantity_by_side["short"],
                "target_quantity": quantity,
            }

        long_exit_avg = close_notional_by_side["long"] / closed_quantity_by_side["long"]
        short_exit_avg = close_notional_by_side["short"] / closed_quantity_by_side["short"]
        long_price_pnl = quantity * (long_exit_avg - long_entry_price)
        short_price_pnl = quantity * (short_entry_price - short_exit_avg)
        price_pnl = long_price_pnl + short_price_pnl
        confirmed_funding_pnl = self.confirmed_funding_pnl_for_position(position_id)
        open_fees = float(position.get("paper_open_fees") or 0.0)
        close_fees = close_fees_by_side["long"] + close_fees_by_side["short"]
        emergency_cost = float(position.get("paper_emergency_unwind_cost") or 0.0)
        paper_net_if_exit_now = (
            price_pnl
            + confirmed_funding_pnl
            - open_fees
            - close_fees
            - emergency_cost
        )
        pnl = {
            "paper_long_price_pnl": long_price_pnl,
            "paper_short_price_pnl": short_price_pnl,
            "paper_price_pnl": price_pnl,
            "paper_close_fees": close_fees,
            "paper_confirmed_funding_pnl": confirmed_funding_pnl,
            "paper_open_fees": open_fees,
            "paper_emergency_unwind_cost": emergency_cost,
            "paper_net_if_exit_now": paper_net_if_exit_now,
        }
        self._record_price_pnl_entries(
            position_id=position_id,
            cycle_id=cycle_id,
            long_venue=str((long_route_leg or long_entry_leg).get("venue") or ""),
            short_venue=str((short_route_leg or short_entry_leg).get("venue") or ""),
            long_price_pnl=long_price_pnl,
            short_price_pnl=short_price_pnl,
            attempt_id=attempt_id,
            payload={
                "reason": reason,
                "pricing_quality": pricing_quality,
                "residual_diagnostics": residual_diagnostics,
                "pnl": pnl,
            },
        )
        if emergency or residual_diagnostics:
            self._record_ledger_entry(
                make_ledger_entry(
                    f"emergency_unwind:{attempt_id or position_id}:close:diagnostic",
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=str((long_route_leg or long_entry_leg).get("venue") or ""),
                    event_type="emergency_unwind_cost",
                    cash_delta=0.0,
                    payload={
                        "reason": reason,
                        "pricing_quality": pricing_quality,
                        "residual_diagnostics": residual_diagnostics,
                        "cash_cost_policy": "penalty_in_fill_price_once",
                        "attempt_id": attempt_id,
                    },
                )
            )
        self._release_collateral(position_id, route or {"legs": entry_legs}, attempt_id=attempt_id)
        mismatch_close = (
            str(reason) == "settlement_plan_event_mismatch"
            or str(position.get("state") or "") == "SETTLEMENT_PLAN_MISMATCH"
            or str(position.get("current_cycle_state") or "") == "SETTLEMENT_PLAN_MISMATCH"
            or str((position.get("config") or {}).get("blocker") or "") == "settlement_plan_event_mismatch"
        )
        final_state = "CLOSED_REQUIRES_REVIEW" if mismatch_close else "CLOSED_PENDING_RECONCILIATION"
        self.store.update_funding_capture_position_state(
            position_id,
            final_state,
            now,
            closed_at=now.isoformat(),
            paper_close_fees=close_fees,
            paper_emergency_unwind_cost=emergency_cost,
            paper_net_pnl_estimated=pnl["paper_net_if_exit_now"],
        )
        if mismatch_close:
            latest = self.store.funding_capture_position_by_id(position_id) or position
            review_config = dict(latest.get("config") or position.get("config") or {})
            review_config["lifecycle_state"] = "CLOSED_REQUIRES_REVIEW"
            review_config["close_requires_review_reason"] = "settlement_plan_event_mismatch"
            review_config["blocker"] = "settlement_plan_event_mismatch"
            review_config["requires_review"] = True
            self.store.update_funding_capture_position_config(position_id, review_config, now)
        if attempt_id:
            self._mark_entry_attempt_terminal(
                capture_id=position_id,
                attempt_id=attempt_id,
                state="CLOSED",
                now=now,
                reason=reason,
            )
        return {
            "decision": "closed",
            "reason": reason,
            "state": final_state,
            "pricing_quality": pricing_quality,
            "long_exit": long_exit,
            "short_exit": short_exit,
            "long_closed_quantity": closed_quantity_by_side["long"],
            "short_closed_quantity": closed_quantity_by_side["short"],
            "long_exit_average_price": long_exit_avg,
            "short_exit_average_price": short_exit_avg,
            "residual_diagnostics": residual_diagnostics,
            "paper_close_fees": close_fees,
            "paper_emergency_unwind_cost": emergency_cost,
            **pnl,
        }

    def _current_close_prices(self, route: dict[str, Any]) -> dict[str, float]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        return {
            "long_exit_price": float(
                optional_float(long_leg.get("close_vwap"))
                or optional_float(long_leg.get("best_bid"))
                or optional_float(long_leg.get("mark_price"))
                or 0.0
            ),
            "short_exit_price": float(
                optional_float(short_leg.get("close_vwap"))
                or optional_float(short_leg.get("best_ask"))
                or optional_float(short_leg.get("mark_price"))
                or 0.0
            ),
        }

    def _close_fee_estimate(
        self,
        route: dict[str, Any],
        quantity: float,
        prices: dict[str, float],
    ) -> float:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        return (
            quantity * float(prices.get("long_exit_price") or 0.0) * _leg_fee_rate(long_leg)
            + quantity * float(prices.get("short_exit_price") or 0.0) * _leg_fee_rate(short_leg)
        )

    def mark_settlement_crossed(self, position: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        position_id = str(position["position_id"])
        config = dict(position.get("config") or {})
        cycles = self.store.funding_capture_cycles_for_position(position_id)
        crossed_cycles: list[dict[str, Any]] = []

        for cycle in cycles:
            cycle_state = str(cycle.get("state") or "")
            if cycle_state in {
                "SETTLEMENT_CROSSED",
                "PUBLIC_RATE_CONFIRMED",
                "RATE_AND_MARK_RECONCILED",
                "RECONCILED",
                "UNRECONCILED",
                "SETTLEMENT_PLAN_MISMATCH",
            }:
                continue
            scheduled = parse_time(cycle.get("scheduled_funding_at"))
            if scheduled is None or now.astimezone(UTC) < scheduled.astimezone(UTC):
                continue
            cycle_id = str(cycle.get("cycle_id") or f"{position_id}:{cycle.get('cycle_number')}")
            active_plan = self._active_cycle_plan_for_cycle(position, cycle)
            expected_events = [
                event
                for event in list(active_plan.get("included_settlement_events") or [])
                if isinstance(event, dict)
            ]
            expected_count = int(active_plan.get("expected_event_count") or len(expected_events))
            matched_events: list[dict[str, Any]] = []
            missing_events: list[dict[str, Any]] = []
            duplicate_events: list[dict[str, Any]] = []
            seen_keys: set[tuple[str, str, str, str]] = set()
            for event in expected_events:
                event_time = parse_time(event.get("scheduled_at"))
                side = self._event_side(event, position)
                venue = str(event.get("venue") or "")
                symbol = str(event.get("symbol") or "")
                key = (
                    str(side or ""),
                    venue,
                    symbol,
                    event_time.astimezone(UTC).isoformat() if event_time else "",
                )
                invalid = (
                    event_time is None
                    or side not in {"long", "short"}
                    or not venue
                    or not symbol
                    or abs((event_time.astimezone(UTC) - scheduled.astimezone(UTC)).total_seconds()) > 1.0
                )
                if key in seen_keys:
                    duplicate_events.append(event)
                    invalid = True
                seen_keys.add(key)
                if invalid:
                    missing_events.append(event)
                    continue
                matched_events.append(event)
            expected_sides = {self._event_side(event, position) for event in matched_events}
            standard_two_leg_mismatch = (
                expected_count == 2
                and (
                    len(matched_events) != 2
                    or expected_sides != {"long", "short"}
                )
            )
            if (
                expected_count <= 0
                or len(matched_events) != expected_count
                or duplicate_events
                or standard_two_leg_mismatch
            ):
                evidence = {
                    "cycle_id": cycle_id,
                    "active_plan_generation": active_plan.get("plan_generation"),
                    "expected_event_count": expected_count,
                    "expected_events": expected_events,
                    "matched_events": matched_events,
                    "missing_events": missing_events,
                    "duplicate_events": duplicate_events,
                    "actual_scheduled_timestamp": scheduled.astimezone(UTC).isoformat(),
                    "blocker": "settlement_plan_event_mismatch",
                    "state": "SETTLEMENT_PLAN_MISMATCH",
                }
                self.store.mark_settlement_plan_mismatch(
                    position_id=position_id,
                    cycle_id=cycle_id,
                    evidence=evidence,
                    now=now,
                )
                return {
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "state": "SETTLEMENT_PLAN_MISMATCH",
                    "blocker": "settlement_plan_event_mismatch",
                    "evidence": evidence,
                }
            rows = []
            for event in matched_events:
                side = self._event_side(event, position) or ""
                venue = str(event.get("venue") or "")
                symbol = str(event.get("symbol") or "")
                rows.append({
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "scheduled_funding_at": scheduled.astimezone(UTC).isoformat(),
                    "status": "PENDING",
                    "confirmed_funding_rate": None,
                    "settlement_mark_price": None,
                    "funding_pnl": None,
                    "rate_status": None,
                    "mark_status": None,
                    "evidence": {
                        "lifecycle_state": "BOUNDARY_CROSSED",
                        "boundary_crossed_at": now.astimezone(UTC).isoformat(),
                        "venue_event_state": "VENUE_EVENT_PENDING",
                        "payment_reconciliation_state": "PAYMENT_RECONCILIATION_PENDING",
                        "payment_reconciliation_source": "PAPER_RECONCILIATION_WORKER",
                        "paper_simulated": True,
                        "not_venue_account_ledger": True,
                        "active_plan_generation": active_plan.get("plan_generation"),
                        "cycle_id": cycle_id,
                        "planned_event": event,
                    },
                })
            boundary_evidence = {
                "cycle_id": cycle_id,
                "active_plan_generation": active_plan.get("plan_generation"),
                "expected_events": expected_events,
                "matched_events": matched_events,
                "actual_scheduled_timestamp": scheduled.astimezone(UTC).isoformat(),
                "obligation_count": len(rows),
                "boundary_crossed_at": now.astimezone(UTC).isoformat(),
            }
            config_update = {
                "lifecycle_state": "BOUNDARY_CROSSED",
                "venue_event_state": "VENUE_EVENT_PENDING",
                "payment_reconciliation_state": "PAYMENT_RECONCILIATION_PENDING",
                "boundary_crossed_at": now.astimezone(UTC).isoformat(),
                "active_cycle_id": cycle_id,
                "active_plan_generation": active_plan.get("plan_generation"),
            }
            self.store.apply_settlement_boundary(
                position_id=position_id,
                cycle_id=cycle_id,
                expected_obligation_count=len(rows),
                reconciliation_rows=rows,
                boundary_evidence=boundary_evidence,
                position_config_update=config_update,
                now=now,
            )
            crossed_cycles.append({**cycle, "cycle_id": cycle_id, "scheduled_funding_at": scheduled.isoformat()})

        if not crossed_cycles:
            return None
        current_position = self.store.funding_capture_position_by_id(position_id) or {}
        return {
            "position_id": position_id,
            "cycle_id": str(crossed_cycles[-1].get("cycle_id") or ""),
            "state": "SETTLEMENT_CROSSED",
            "lifecycle_state": "BOUNDARY_CROSSED",
            "venue_event_state": "VENUE_EVENT_PENDING",
            "payment_reconciliation_state": "PAYMENT_RECONCILIATION_PENDING",
            "crossed_cycles": crossed_cycles,
            "settlements_captured_count": current_position.get("settlements_captured_count"),
        }
