from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.strategy_synchronized_funding import (
    STRATEGY_NAME,
    STRATEGY_VERSION,
    entry_underwriting,
    gross_funding_pnl,
    hold_economics,
    initial_entry_economics,
    parse_time,
    settlement_alignment_passed,
    summarize_funding_observations,
    validate_focused_observation,
)
from smart_money_radar.paper_bot.accounting import executable_paper_pnl
from smart_money_radar.paper_bot.accounting import (
    collateral_reserve_event_key,
    collateral_release_event_key,
    make_ledger_entry,
    order_fee_event_key,
    price_pnl_event_key,
)
from smart_money_radar.paper_bot.cycle_manager import (
    next_cycle_observation_decision,
    next_cycle_schedule_decision,
)
from smart_money_radar.paper_bot.execution import (
    EXECUTION_HAIRCUT_FRACTION,
    entry_fill_state,
    residual_adverse_penalty,
    simulate_marketable_ioc,
    t20_deadline_passed,
)
from smart_money_radar.paper_bot.helpers import leg_by_side, optional_float, route_entry_key
from smart_money_radar.paper_bot.risk import entry_risk_gates, hard_risk_triggered
from smart_money_radar.paper_bot.settlement import build_settlement_crossing_rows
from smart_money_radar.storage import SQLiteStore

OBSERVATION_MIN_COUNT = 10
OBSERVATION_MIN_SPAN_SECONDS = 20.0
POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS = 20.0
POST_SETTLEMENT_HOLD_DECISION_SECONDS = 30.0


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
    if not settlement_alignment_passed(long_next, short_next):
        return None
    return max(long_next.astimezone(UTC), short_next.astimezone(UTC))


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
    return float(
        optional_float(leg.get("normalized_next_funding_rate"))
        if optional_float(leg.get("normalized_next_funding_rate")) is not None
        else optional_float(leg.get("funding_rate")) or 0.0
    )


def _synthetic_levels(price: float, quantity: float) -> list[list[float]]:
    if price <= 0 or quantity <= 0:
        return []
    return [[price, quantity / EXECUTION_HAIRCUT_FRACTION]]


def _observation_bucket(route_key: str, phase: str, cycle_id: str) -> str:
    if phase == "entry":
        return route_key
    return f"{route_key}:{phase}:{cycle_id}"


def build_focused_observation(
    route: dict[str, Any],
    *,
    now: datetime,
    phase: str = "entry",
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
    capabilities_passed = bool(evidence.get("synchronized_capability_passed")) and not capability_reasons
    long_open = optional_float(long_leg.get("open_vwap")) or optional_float(long_leg.get("vwap"))
    short_open = optional_float(short_leg.get("open_vwap")) or optional_float(short_leg.get("vwap"))
    long_close = optional_float(long_leg.get("close_vwap")) or optional_float(long_leg.get("best_bid"))
    short_close = optional_float(short_leg.get("close_vwap")) or optional_float(short_leg.get("best_ask"))
    raw = {
        "phase": phase,
        "observed_at": now.astimezone(UTC).isoformat(),
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
        "long_book_executable": long_open is not None and long_close is not None,
        "short_book_executable": short_open is not None and short_close is not None,
        "capabilities_passed": capabilities_passed,
    }
    validation = validate_focused_observation(raw, now=now)
    raw["snapshot_valid"] = bool(validation["valid"])
    raw["invalid_reason"] = ",".join(validation["reasons"]) if validation["reasons"] else None
    return raw


class SynchronizedFundingRuntimeV2:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: Any,
        clock: Any,
        observations_by_route: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.observations_by_route = observations_by_route

    def _record_ledger_entry(self, row: dict[str, Any]) -> str | None:
        event_key = self.store.upsert_paper_event_ledger(row)
        if event_key is not None and row.get("venue") and float(row.get("cash_delta") or 0.0) != 0.0:
            self.store.update_funding_paper_account_cash(
                str(row["venue"]),
                float(row.get("cash_delta") or 0.0),
            )
        return event_key

    def consider_route(
        self,
        route: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        now = self.clock.now()
        route_key = str(route.get("route_key") or "")
        if not route_key:
            return {"opened": False, "reason": "route_key_missing"}
        settlement_at = route_next_settlement(route)
        if settlement_at is None:
            return {"opened": False, "reason": "settlement_alignment_missing"}
        lead = (settlement_at - now.astimezone(UTC)).total_seconds()
        capture_id = capture_position_id_for_route(route)
        existing_open = self.store.funding_capture_open_position_by_route_key(route_key)
        if existing_open is not None:
            return {"opened": False, "reason": "route_already_open", "position_id": existing_open["position_id"]}
        self._ensure_discovered_or_armed(route, capture_id, settlement_at, lead, now)
        observation = build_focused_observation(route, now=now)
        self._store_observation(route, capture_id, settlement_at, observation, phase="entry")
        observations = self._valid_observations(route_key, now, phase="entry", cycle_id=f"{capture_id}:1")
        underwriting = entry_underwriting(observations, now=now)
        if not underwriting["eligible"]:
            return {
                "opened": False,
                "reason": "focused_underwriting_not_ready",
                "underwriting": underwriting,
            }
        if not (float(self.config.entry_min_lead_seconds) <= lead <= float(self.config.entry_max_lead_seconds)):
            return {"opened": False, "reason": "outside_entry_window", "lead_seconds": lead}
        economics = self._initial_economics(route, observations, underwriting)
        if not economics["eligible"]:
            return {"opened": False, "reason": "initial_economics_failed", "economics": economics}
        risk_gate = self._entry_risk_gate(route)
        if not risk_gate["passed"]:
            return {"opened": False, "reason": "entry_risk_gate_failed", "risk_gate": risk_gate}
        account_check = self._enforce_account_limits(route, accounts)
        if not account_check["passed"]:
            return {"opened": False, "reason": account_check["reason"], "account_check": account_check}
        execution = self._execute_entry(route, capture_id, settlement_at, now)
        if execution["state"] != "OPEN":
            return {"opened": False, "reason": "entry_execution_failed", "execution": execution}
        self._mark_open(route, capture_id, settlement_at, economics, execution, now)
        return {
            "opened": True,
            "position_id": capture_id,
            "cycle_id": f"{capture_id}:1",
            "economics": economics,
            "execution": execution,
            "underwriting": underwriting,
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
        state = "ARMED" if lead <= float(self.config.arm_window_seconds) else "DISCOVERED"
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
                "state": state,
                "opened_at": now.isoformat(),
                "config": {
                    "route_key": route_key,
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": legs,
                },
            }
        )
        if state == "ARMED":
            self.store.upsert_funding_capture_cycle(
                {
                    "cycle_id": f"{capture_id}:1",
                    "position_id": capture_id,
                    "cycle_number": 1,
                    "scheduled_funding_at": settlement_at.isoformat(),
                    "state": "ARMED",
                    "decision": "ARMED",
                    "decision_reason": "focused_observations_collecting",
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
        bucket = _observation_bucket(route_key, observation_phase, resolved_cycle_id)
        observations = self.observations_by_route.setdefault(bucket, [])
        observations.append(observation)
        if len(observations) > 120:
            del observations[:-120]
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
        route_key: str,
        now: datetime,
        *,
        phase: str,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        bucket = _observation_bucket(route_key, phase, cycle_id)
        valid = []
        for row in self.observations_by_route.get(bucket, []):
            if str(row.get("phase") or phase) != phase:
                continue
            if validate_focused_observation(row, now=now)["valid"]:
                valid.append(row)
        return valid

    def _initial_economics(
        self,
        route: dict[str, Any],
        observations: list[dict[str, Any]],
        underwriting: dict[str, Any],
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
            q * long_open * float(long_leg.get("fee_rate") or 0.0)
            + q * long_close * float(long_leg.get("fee_rate") or 0.0)
            + q * short_open * float(short_leg.get("fee_rate") or 0.0)
            + q * short_close * float(short_leg.get("fee_rate") or 0.0)
        )
        reference = min(q * long_open, q * short_open)
        basis_reserve_usd = reference * 30.0 / 10_000.0
        legging_reserve_usd = reference * 15.0 / 10_000.0
        return initial_entry_economics(
            conservative_funding_gross=float(underwriting["conservative_funding_gross"]),
            baseline_round_trip_book_cost=baseline_book_cost,
            total_round_trip_fee_estimate=fees,
            entry_basis_reserve_usd=basis_reserve_usd,
            entry_legging_reserve_usd=legging_reserve_usd,
            reference_notional=reference,
        )

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
            fee_rate = float(leg.get("fee_rate") or 0.0)
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

    def _reserve_collateral(self, capture_id: str, route: dict[str, Any]) -> None:
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
            event_key = collateral_reserve_event_key(capture_id, venue)
            reserved = self._record_ledger_entry(
                make_ledger_entry(
                    event_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_reserve",
                    cash_delta=0.0,
                    payload={"amount": amount},
                )
            )
            if reserved is not None:
                self.store.update_funding_paper_account_reserved(venue, amount)

    def _release_collateral(self, capture_id: str, route: dict[str, Any]) -> None:
        legs = route.get("legs") or []
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        reserve_fraction = float(getattr(self.config, "collateral_reserve_fraction", 0.25))
        quantity = float(self._target_quantity(route) or 0.0)
        for leg in legs:
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            reserve_key = collateral_reserve_event_key(capture_id, venue)
            release_key = collateral_release_event_key(capture_id, venue)
            existing_reserve = self.store.paper_event_ledger_rows(capture_id)
            if not any(r["event_key"] == reserve_key for r in existing_reserve):
                continue
            already_released = any(r["event_key"] == release_key for r in existing_reserve)
            if already_released:
                continue
            price = float(leg.get("open_vwap") or leg.get("vwap") or leg.get("mark_price") or 0.0)
            leg_notional = quantity * price
            if leg_notional <= 0:
                payload_amount = optional_float(
                    next(
                        (
                            row.get("payload", {}).get("amount")
                            for row in existing_reserve
                            if row["event_key"] == reserve_key
                        ),
                        None,
                    )
                )
                leg_notional = float(payload_amount or 0.0) / (1.0 / leverage + reserve_fraction)
            amount = leg_notional / leverage + leg_notional * reserve_fraction
            released = self._record_ledger_entry(
                make_ledger_entry(
                    release_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_release",
                    cash_delta=0.0,
                    payload={"amount": amount},
                )
            )
            if released is not None:
                self.store.update_funding_paper_account_reserved(venue, -amount)

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
        self.store.update_funding_capture_position_state(capture_id, "ENTRY_SUBMITTED", decision_at)
        self._reserve_collateral(capture_id, route)
        submitted_at = decision_at + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        deadline_ok = t20_deadline_passed(filled_at, settlement_at, self.config.entry_fill_deadline_lead_seconds)
        long_fill = simulate_marketable_ioc(long_levels, "buy", q, EXECUTION_HAIRCUT_FRACTION)
        short_fill = simulate_marketable_ioc(short_levels, "sell", q, EXECUTION_HAIRCUT_FRACTION)
        long_fee = long_fill["notional"] * float(long_leg.get("fee_rate") or 0.0)
        short_fee = short_fill["notional"] * float(short_leg.get("fee_rate") or 0.0)
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
            order_id = f"{capture_id}:entry:{side}"
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
                    "payload": fill,
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
                    payload={"order_id": order_id},
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
            )
            return {
                "state": "FAILED",
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
            self._release_collateral(capture_id, route)
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
        long_unwind_fee = long_unwind_qty * long_exit_price * float(long_leg.get("fee_rate") or 0.0)
        short_unwind_fee = short_unwind_qty * short_exit_price * float(short_leg.get("fee_rate") or 0.0)
        emergency_cost = (
            long_unwind_qty * long_entry_price * 100.0 / 10_000.0
            + short_unwind_qty * short_entry_price * 100.0 / 10_000.0
        )
        self.store.update_funding_capture_position_state(capture_id, "EMERGENCY_UNWIND", now)
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        for side, leg, qty, price, fee in (
            ("long", long_leg, long_unwind_qty, long_exit_price, long_unwind_fee),
            ("short", short_leg, short_unwind_qty, short_exit_price, short_unwind_fee),
        ):
            if qty <= 0:
                continue
            order_id = f"{capture_id}:unwind:{side}"
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
                    "payload": {"adverse_penalty_bps": 100.0},
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
                    payload={"order_id": order_id, "intent": "unwind"},
                )
            )
        if total_price_pnl != 0.0:
            self._record_ledger_entry(
                make_ledger_entry(
                    price_pnl_event_key(capture_id),
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=None,
                    event_type="price_pnl",
                    cash_delta=total_price_pnl,
                    payload={"reason": "partial_entry_unwind", "price_pnl": total_price_pnl},
                )
            )
        if emergency_cost > 0:
            self._record_ledger_entry(
                make_ledger_entry(
                    f"emergency_unwind:{capture_id}:partial_entry",
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=None,
                    event_type="emergency_unwind_cost",
                    cash_delta=-emergency_cost,
                    payload={"reason": "partial_entry_unwind"},
                )
            )
        self._release_collateral(capture_id, route)
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
            "unwind_fees": long_unwind_fee + short_unwind_fee,
            "emergency_cost": emergency_cost,
        }

    def _mark_open(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        economics: dict[str, Any],
        execution: dict[str, Any],
        now: datetime,
    ) -> None:
        legs = route.get("legs") or []
        long_leg = dict(leg_by_side(legs, "long") or {})
        short_leg = dict(leg_by_side(legs, "short") or {})
        long_leg["entry_fill_price"] = execution["long_entry_price"]
        short_leg["entry_fill_price"] = execution["short_entry_price"]
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
                    "route_key": route.get("route_key"),
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": [long_leg, short_leg],
                    "entry_economics": economics,
                    "entry_execution": execution,
                },
            }
        )
        self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{capture_id}:1",
                "position_id": capture_id,
                "cycle_number": 1,
                "scheduled_funding_at": settlement_at.isoformat(),
                "long_next_funding_rate_at_decision": _leg_rate(long_leg),
                "short_next_funding_rate_at_decision": _leg_rate(short_leg),
                "conservative_funding_gross": economics.get("conservative_funding_gross"),
                "conservative_funding_edge_bps": economics.get("conservative_funding_edge_bps"),
                "state": "OPEN",
                "decision": "OPEN",
                "decision_reason": "two_entry_legs_filled_before_t20",
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
        observation = build_focused_observation(route, now=now, phase="hold")
        self._store_observation(
            route,
            position_id,
            scheduled,
            observation,
            phase="hold",
            cycle_id=cycle_id,
        )
        return observation

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
        if seconds_after < POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS:
            return {
                "decision": "wait",
                "reason": "normal_exit_forbidden_before_t_plus_20",
                "seconds_after_settlement": seconds_after,
            }
        if route is None:
            return {"decision": "close", "reason": "post_settlement_route_missing"}

        self.collect_hold_observation(position, route, now)
        route_key = str((position.get("config") or {}).get("route_key") or route.get("route_key") or "")
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        long_leg = leg_by_side(route.get("legs") or [], "long") or {}
        short_leg = leg_by_side(route.get("legs") or [], "short") or {}
        schedule = next_cycle_schedule_decision(
            long_next_funding_at=long_leg.get("next_funding_at"),
            short_next_funding_at=short_leg.get("next_funding_at"),
            now=now,
            min_wait_seconds=float(getattr(self.config, "min_next_settlement_wait_seconds", 300)),
            max_wait_seconds=float(getattr(self.config, "max_next_settlement_wait_seconds", 14_400)),
        )
        if not schedule["hold_schedule"]:
            return {"decision": "close", "reason": "next_cycle_schedule_failed", "schedule": schedule}
        if seconds_after < POST_SETTLEMENT_HOLD_DECISION_SECONDS:
            return {
                "decision": "wait",
                "reason": "collecting_next_cycle_observations",
                "seconds_after_settlement": seconds_after,
                "schedule": schedule,
            }

        observations = self._valid_observations(route_key, now, phase="hold", cycle_id=cycle_id)
        observation_decision = next_cycle_observation_decision(observations=observations, now=now)
        if not observation_decision["eligible"]:
            return {
                "decision": "close",
                "reason": "next_cycle_observations_failed",
                "observation_decision": observation_decision,
                "schedule": schedule,
            }

        quantity = float(position.get("quantity") or 0.0)
        close_prices = self._current_close_prices(route)
        current_close_fees = self._close_fee_estimate(route, quantity, close_prices)
        reference_notional = min(
            quantity * max(0.0, float(close_prices.get("long_exit_price") or 0.0)),
            quantity * max(0.0, float(close_prices.get("short_exit_price") or 0.0)),
        )
        entry_economics = (position.get("config") or {}).get("entry_economics") or {}
        hold = hold_economics(
            next_conservative_funding_gross=float(observation_decision["conservative_funding_gross"]),
            current_close_fees=current_close_fees,
            reference_notional=reference_notional,
            wait_seconds=float(schedule.get("seconds_to_next_cycle") or 0.0),
            entry_basis_reserve_bps=float(entry_economics.get("entry_basis_reserve_bps") or 30.0),
        )
        projected_total = float(position.get("paper_net_pnl_estimated") or 0.0) + float(
            hold["incremental_hold_net_pnl"]
        )
        opened_at = parse_time(position.get("opened_at")) or now
        next_cycle_time = parse_time(schedule.get("next_cycle_settlement_at"))
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
        if float(observation_decision["conservative_funding_gross"]) < max(2.50, reference * 0.005):
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
                "schedule": schedule,
                "observation_decision": observation_decision,
                "hold_economics": hold,
                "projected_total_after_next_cycle": projected_total,
                "projected_position_age_at_next_exit": projected_age,
            }

        next_cycle_number = int(position.get("current_cycle_number") or 1) + 1
        next_cycle_id = self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{position_id}:{next_cycle_number}",
                "position_id": position_id,
                "cycle_number": next_cycle_number,
                "scheduled_funding_at": str(schedule["next_cycle_settlement_at"]),
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
                "paper_net_if_exit_at_decision": position.get("paper_net_pnl_estimated"),
                "decision": "HOLD",
                "decision_reason": "next_cycle_underwriting_passed",
                "state": "HOLDING_NEXT_CYCLE",
            }
        )
        self.store.update_funding_capture_position_state(
            position_id,
            "HOLDING_NEXT_CYCLE",
            now,
            paper_net_pnl_estimated=projected_total,
        )
        return {
            "decision": "hold",
            "reason": "next_cycle_underwriting_passed",
            "cycle_id": next_cycle_id,
            "cycle_number": next_cycle_number,
            "schedule": schedule,
            "observation_decision": observation_decision,
            "hold_economics": hold,
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
        penalty_bps = 0.0
        long_has_book = bool(long_route_leg.get("bids"))
        short_has_book = bool(short_route_leg.get("asks"))
        if emergency:
            self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)
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
            if not long_has_book or not short_has_book:
                long_mark = float(long_route_leg.get("mark_price") or long_entry_leg.get("entry_fill_price") or long_entry_leg.get("vwap") or 0.0)
                short_mark = float(short_route_leg.get("mark_price") or short_entry_leg.get("entry_fill_price") or short_entry_leg.get("vwap") or 0.0)
                if long_mark > 0 and short_mark > 0:
                    pricing_quality = "fallback_mark_300bps"
                    penalty_bps = 300.0
                else:
                    return {
                        "decision": "rejected",
                        "reason": "emergency_pricing_unavailable",
                        "state": "EMERGENCY_UNWIND",
                    }
        long_exit_price = float(
            optional_float(long_route_leg.get("close_vwap"))
            or optional_float(long_route_leg.get("best_bid"))
            or optional_float(long_route_leg.get("mark_price"))
            or 0.0
        )
        short_exit_price = float(
            optional_float(short_route_leg.get("close_vwap"))
            or optional_float(short_route_leg.get("best_ask"))
            or optional_float(short_route_leg.get("mark_price"))
            or 0.0
        )
        if emergency and penalty_bps > 0:
            long_exit_price = float(long_route_leg.get("mark_price") or long_entry_leg.get("entry_fill_price") or long_entry_leg.get("vwap") or 0.0)
            short_exit_price = float(short_route_leg.get("mark_price") or short_entry_leg.get("entry_fill_price") or short_entry_leg.get("vwap") or 0.0)
            long_exit_price = long_exit_price * (1.0 - penalty_bps / 10_000.0) if long_exit_price > 0 else 0.0
            short_exit_price = short_exit_price * (1.0 + penalty_bps / 10_000.0) if short_exit_price > 0 else 0.0
            long_levels = _synthetic_levels(long_exit_price, quantity) if long_exit_price > 0 else []
            short_levels = _synthetic_levels(short_exit_price, quantity) if short_exit_price > 0 else []
        else:
            long_levels = long_route_leg.get("bids") or []
            short_levels = short_route_leg.get("asks") or []
        long_exit = simulate_marketable_ioc(long_levels, "sell", quantity, EXECUTION_HAIRCUT_FRACTION)
        short_exit = simulate_marketable_ioc(short_levels, "buy", quantity, EXECUTION_HAIRCUT_FRACTION)
        long_fee_rate = float(long_route_leg.get("fee_rate") or long_entry_leg.get("fee_rate") or 0.0)
        short_fee_rate = float(short_route_leg.get("fee_rate") or short_entry_leg.get("fee_rate") or 0.0)
        long_fee = long_exit["notional"] * long_fee_rate
        short_fee = short_exit["notional"] * short_fee_rate
        residual_quantity = float(long_exit["unfilled_quantity"]) + float(short_exit["unfilled_quantity"])
        reference_price = (
            float(long_exit["average_fill_price"] or 0.0)
            or float(short_exit["average_fill_price"] or 0.0)
            or long_exit_price
            or short_exit_price
        )
        emergency_cost = (
            residual_adverse_penalty(residual_quantity, reference_price)
            if residual_quantity > 0
            else 0.0
        )
        if emergency and penalty_bps > 0:
            emergency_cost += (
                quantity * max(long_exit_price, 0.0) * penalty_bps / 10_000.0
                + quantity * max(short_exit_price, 0.0) * penalty_bps / 10_000.0
            )
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        for side, leg, fill, fee in (
            ("long", long_route_leg or long_entry_leg, long_exit, long_fee),
            ("short", short_route_leg or short_entry_leg, short_exit, short_fee),
        ):
            order_id = f"{position_id}:exit:{cycle_id}:{side}"
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
                    "payload": fill,
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
                    payload={"order_id": order_id, "reason": reason},
                )
            )
        pnl = executable_paper_pnl(
            quantity=quantity,
            long_entry_price=long_entry_price,
            long_exit_price=float(long_exit["average_fill_price"] or long_exit_price or 0.0),
            short_entry_price=short_entry_price,
            short_exit_price=float(short_exit["average_fill_price"] or short_exit_price or 0.0),
            long_taker_fee=long_fee_rate,
            short_taker_fee=short_fee_rate,
            confirmed_funding_pnl=0.0,
            paper_open_fees=float(position.get("paper_open_fees") or 0.0),
            emergency_unwind_costs_already_incurred=emergency_cost,
        )
        self._record_ledger_entry(
            make_ledger_entry(
                price_pnl_event_key(position_id),
                position_id=position_id,
                cycle_id=cycle_id,
                venue=None,
                event_type="price_pnl",
                cash_delta=float(pnl["paper_price_pnl"]),
                payload={"reason": reason, "pnl": pnl},
            )
        )
        if emergency_cost > 0:
            self._record_ledger_entry(
                make_ledger_entry(
                    f"emergency_unwind:{position_id}:close",
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=None,
                    event_type="emergency_unwind_cost",
                    cash_delta=-emergency_cost,
                    payload={"reason": reason, "residual_quantity": residual_quantity, "pricing_quality": pricing_quality},
                )
            )
        self._release_collateral(position_id, route or {"legs": entry_legs})
        final_state = "CLOSED_PENDING_RECONCILIATION"
        self.store.update_funding_capture_position_state(
            position_id,
            final_state,
            now,
            closed_at=now.isoformat(),
            paper_close_fees=long_fee + short_fee,
            paper_emergency_unwind_cost=emergency_cost,
            paper_net_pnl_estimated=pnl["paper_net_if_exit_now"],
        )
        return {
            "decision": "closed",
            "reason": reason,
            "state": final_state,
            "pricing_quality": pricing_quality,
            "long_exit": long_exit,
            "short_exit": short_exit,
            "paper_close_fees": long_fee + short_fee,
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
            quantity * float(prices.get("long_exit_price") or 0.0) * float(long_leg.get("fee_rate") or 0.0)
            + quantity * float(prices.get("short_exit_price") or 0.0) * float(short_leg.get("fee_rate") or 0.0)
        )

    def mark_settlement_crossed(self, position: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if str(position.get("state") or "") == "SETTLEMENT_CROSSED":
            return None
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at"))
        if scheduled is None or now.astimezone(UTC) < scheduled.astimezone(UTC):
            return None
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        rows = build_settlement_crossing_rows(
            position_id=position_id,
            cycle_id=cycle_id,
            long_venue=str(position.get("long_venue") or ""),
            long_symbol=str(position.get("long_symbol") or ""),
            short_venue=str(position.get("short_venue") or ""),
            short_symbol=str(position.get("short_symbol") or ""),
            scheduled_funding_at=scheduled.isoformat(),
            quantity=float(position.get("quantity") or 0.0),
        )
        for row in rows:
            self.store.upsert_funding_settlement_reconciliation(row)
        self.store.update_funding_capture_cycle_state(cycle_id, "SETTLEMENT_CROSSED", now)
        self.store.update_funding_capture_position_state(
            position_id,
            "SETTLEMENT_CROSSED",
            now,
            settlements_captured_count=int(position.get("settlements_captured_count") or 0) + 1,
        )
        return {"position_id": position_id, "cycle_id": cycle_id, "state": "SETTLEMENT_CROSSED"}
