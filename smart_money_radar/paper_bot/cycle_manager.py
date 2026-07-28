from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import floor
from typing import Any

from smart_money_radar.funding.strategy_synchronized_funding import (
    entry_underwriting,
    parse_time,
    settlement_alignment_passed,
    settlement_skew_seconds,
    validate_focused_observation,
)


CAPTURE_STATES = (
    "DISCOVERED",
    "REJECTED",
    "ARMED",
    "ENTRY_SUBMITTED",
    "LEG_1_FILLED",
    "PARTIALLY_HEDGED",
    "OPEN",
    "SETTLEMENT_CROSSED",
    "POST_SETTLEMENT_EVALUATION",
    "HOLDING_NEXT_CYCLE",
    "EXIT_SCHEDULED",
    "EXIT_SUBMITTED",
    "PARTIALLY_CLOSED",
    "EMERGENCY_UNWIND",
    "CLOSED_PENDING_RECONCILIATION",
    "RECONCILED",
    "UNRECONCILED",
    "FAILED",
)

PRE_ENTRY_STATES = {"DISCOVERED", "ARMED"}
LIVE_STATES = {"OPEN", "SETTLEMENT_CROSSED", "POST_SETTLEMENT_EVALUATION", "HOLDING_NEXT_CYCLE"}
TERMINAL_STATES = {"CLOSED_PENDING_RECONCILIATION", "RECONCILED", "FAILED"}


def wait_bucket_for_seconds(wait_seconds: float) -> str:
    wait = max(0.0, float(wait_seconds))
    if wait <= 3_600.0:
        return "<=1h"
    if wait <= 7_200.0:
        return ">1h<=2h"
    return ">2h<=4h"


def percentile_25(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, floor((len(ordered) - 1) * 0.25)))
    return ordered[index]


@dataclass(frozen=True)
class HoldHistoryReliability:
    status: str
    gate_passed: bool
    valid_cycle_count: int
    positive_realization_rate: float | None
    p25_realization_ratio: float | None
    history_multiplier: float
    max_extra_cycles_when_insufficient: int
    reasons: tuple[str, ...] = ()

    def adjusted_funding(self, next_conservative_funding_gross: float) -> float:
        return float(next_conservative_funding_gross) * float(self.history_multiplier)

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_status": self.status,
            "hold_history_gate_passed": self.gate_passed,
            "valid_cycle_count": self.valid_cycle_count,
            "positive_realization_rate": self.positive_realization_rate,
            "p25_realization_ratio": self.p25_realization_ratio,
            "history_multiplier": self.history_multiplier,
            "hold_history_max_extra_cycles_when_insufficient": self.max_extra_cycles_when_insufficient,
            "reasons": list(self.reasons),
        }


def evaluate_hold_history_reliability(
    cycles: list[dict[str, Any]],
    *,
    min_cycles_for_gate: int = 8,
    insufficient_multiplier: float = 0.75,
    min_positive_realization_rate: float = 0.70,
    min_p25_realization_ratio: float = 0.50,
    max_extra_cycles_when_insufficient: int = 1,
) -> HoldHistoryReliability:
    valid: list[tuple[float, float]] = []
    for cycle in cycles:
        predicted = cycle.get("predicted_gross")
        if predicted is None:
            predicted = cycle.get("conservative_funding_gross")
        realized = cycle.get("realized_gross")
        if realized is None:
            realized = cycle.get("reconciled_funding_pnl")
        try:
            predicted_value = float(predicted)
            realized_value = float(realized)
        except (TypeError, ValueError):
            continue
        if predicted_value <= 0:
            continue
        valid.append((predicted_value, realized_value))
    if len(valid) < int(min_cycles_for_gate):
        return HoldHistoryReliability(
            status="INSUFFICIENT",
            gate_passed=True,
            valid_cycle_count=len(valid),
            positive_realization_rate=None,
            p25_realization_ratio=None,
            history_multiplier=float(insufficient_multiplier),
            max_extra_cycles_when_insufficient=int(max_extra_cycles_when_insufficient),
            reasons=("insufficient_reconciled_hold_history",),
        )
    realized_values = [realized for _predicted, realized in valid]
    ratios = [realized / predicted for predicted, realized in valid]
    positive_rate = sum(1 for value in realized_values if value > 0) / len(realized_values)
    p25_ratio = percentile_25(ratios)
    reasons: list[str] = []
    if positive_rate < float(min_positive_realization_rate):
        reasons.append("hold_history_positive_realization_rate_failed")
    if p25_ratio < float(min_p25_realization_ratio):
        reasons.append("hold_history_p25_realization_ratio_failed")
    multiplier = min(1.0, max(0.50, p25_ratio))
    return HoldHistoryReliability(
        status="PASSED" if not reasons else "FAILED",
        gate_passed=not reasons,
        valid_cycle_count=len(valid),
        positive_realization_rate=positive_rate,
        p25_realization_ratio=p25_ratio,
        history_multiplier=multiplier,
        max_extra_cycles_when_insufficient=int(max_extra_cycles_when_insufficient),
        reasons=tuple(reasons),
    )


def next_cycle_schedule_decision(
    *,
    long_next_funding_at: Any,
    short_next_funding_at: Any,
    now: datetime,
    tolerance_seconds: float = 1.0,
    min_wait_seconds: float = 300.0,
    max_wait_seconds: float = 14_400.0,
) -> dict[str, Any]:
    long_time = parse_time(long_next_funding_at)
    short_time = parse_time(short_next_funding_at)
    reasons: list[str] = []
    if long_time is None:
        reasons.append("long_next_settlement_missing")
    if short_time is None:
        reasons.append("short_next_settlement_missing")
    if long_time is not None and long_time <= now:
        reasons.append("long_next_settlement_not_future")
    if short_time is not None and short_time <= now:
        reasons.append("short_next_settlement_not_future")
    skew = settlement_skew_seconds(long_time, short_time)
    if not settlement_alignment_passed(long_time, short_time, tolerance_seconds=tolerance_seconds):
        reasons.append("next_settlement_schedule_mismatch")
    next_cycle_at = max(time for time in (long_time, short_time) if time is not None) if long_time and short_time else None
    seconds_to_next = (next_cycle_at - now).total_seconds() if next_cycle_at is not None else None
    if seconds_to_next is not None and seconds_to_next < float(min_wait_seconds):
        reasons.append("next_settlement_too_soon")
    if seconds_to_next is not None and seconds_to_next > float(max_wait_seconds):
        reasons.append("next_settlement_too_far")
    return {
        "hold_schedule": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "next_cycle_settlement_at": next_cycle_at.isoformat() if next_cycle_at else None,
        "seconds_to_next_cycle": seconds_to_next,
        "settlement_skew_seconds": skew,
    }


def discovered_position_payload(
    route: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = next((leg for leg in legs if str(leg.get("side") or "").lower() == "long"), {})
    short_leg = next((leg for leg in legs if str(leg.get("side") or "").lower() == "short"), {})
    return {
        "position_id": f"fc-{route.get('route_key', '')}-{int(now.timestamp())}",
        "canonical_asset": route.get("canonical_asset", ""),
        "long_venue": str(long_leg.get("venue") or route.get("long_venue") or ""),
        "long_symbol": str(long_leg.get("symbol") or route.get("long_symbol") or ""),
        "short_venue": str(short_leg.get("venue") or route.get("short_venue") or ""),
        "short_symbol": str(short_leg.get("symbol") or route.get("short_symbol") or ""),
        "quantity": 0.0,
        "target_notional": float(route.get("target_notional") or 0.0),
        "state": "DISCOVERED",
        "opened_at": now.isoformat(),
        "route_key": route.get("route_key", ""),
        "discovered_at": now.isoformat(),
    }


def arm_decision(
    position: dict[str, Any],
    route: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    now: datetime,
    entry_min_lead_seconds: float = 25.0,
    entry_max_lead_seconds: float = 35.0,
) -> dict[str, Any]:
    reasons: list[str] = []
    current_state = str(position.get("state") or "")
    if current_state not in PRE_ENTRY_STATES:
        reasons.append(f"position_not_pre_entry_state_{current_state}")
    legs = route.get("legs") or []
    long_leg = next((leg for leg in legs if str(leg.get("side") or "").lower() == "long"), {})
    short_leg = next((leg for leg in legs if str(leg.get("side") or "").lower() == "short"), {})
    long_settlement = parse_time(long_leg.get("next_funding_at"))
    short_settlement = parse_time(short_leg.get("next_funding_at"))
    if long_settlement is None or short_settlement is None:
        reasons.append("settlement_timestamp_missing")
    else:
        long_lead = (long_settlement - now).total_seconds()
        short_lead = (short_settlement - now).total_seconds()
        if not (entry_min_lead_seconds <= long_lead <= entry_max_lead_seconds):
            reasons.append(f"long_lead_{long_lead:.1f}s_outside_entry_window")
        if not (entry_min_lead_seconds <= short_lead <= entry_max_lead_seconds):
            reasons.append(f"short_lead_{short_lead:.1f}s_outside_entry_window")
        if not settlement_alignment_passed(long_settlement, short_settlement):
            reasons.append("settlement_alignment_mismatch")
    underwriting = entry_underwriting(observations, now=now)
    if not underwriting["eligible"]:
        reasons.extend(underwriting["reasons"])
    return {
        "armed": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "underwriting": underwriting,
        "new_state": "ARMED" if not reasons else current_state,
        "cycle_number": 1 if not reasons else None,
    }


def settlement_crossing_decision(
    position: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Detect whether settlement has been crossed for a position."""
    current_state = str(position.get("state") or "")
    if current_state != "OPEN":
        return {"crossed": False, "reason": f"state_not_open_{current_state}"}
    max_settlement = parse_time(position.get("max_settlement_at"))
    if max_settlement is None:
        return {"crossed": False, "reason": "max_settlement_at_missing"}
    if now <= max_settlement:
        return {"crossed": False, "reason": "settlement_not_yet_reached"}
    return {
        "crossed": True,
        "new_state": "SETTLEMENT_CROSSED",
        "settlement_at": max_settlement.isoformat(),
    }


def exit_simulation(
    *,
    long_book_asks: list[list[float]],
    short_book_bids: list[list[float]],
    quantity: float,
    depth_haircut_fraction: float = 0.40,
    adverse_bps: float = 100.0,
) -> dict[str, Any]:
    """Simulate reduce-only exit for both legs.

    Long exit = sell against asks. Short exit = buy against bids.
    If depth is insufficient, fill available part and record residual
    with adverse penalty.
    """
    from smart_money_radar.paper_bot.execution import (
        simulate_marketable_ioc,
        residual_adverse_penalty,
    )
    long_exit = simulate_marketable_ioc(
        long_book_asks, "sell", quantity, depth_haircut_fraction,
    )
    short_exit = simulate_marketable_ioc(
        short_book_bids, "buy", quantity, depth_haircut_fraction,
    )
    long_residual = long_exit["unfilled_quantity"]
    short_residual = short_exit["unfilled_quantity"]
    total_residual = long_residual + short_residual
    reference_price = (
        (long_exit["average_fill_price"] + short_exit["average_fill_price"]) / 2.0
        if long_exit["average_fill_price"] > 0 and short_exit["average_fill_price"] > 0
        else long_exit["average_fill_price"] or short_exit["average_fill_price"]
    )
    emergency_cost = (
        residual_adverse_penalty(total_residual, reference_price, adverse_bps)
        if total_residual > 0
        else 0.0
    )
    needs_emergency = total_residual > 0
    return {
        "long_exit": long_exit,
        "short_exit": short_exit,
        "long_residual_quantity": long_residual,
        "short_residual_quantity": short_residual,
        "total_residual_quantity": total_residual,
        "emergency_unwind_cost": emergency_cost,
        "needs_emergency_unwind": needs_emergency,
        "new_state": "EMERGENCY_UNWIND" if needs_emergency else "CLOSED_PENDING_RECONCILIATION",
    }


def exit_close_gate(
    *,
    long_filled_quantity: float,
    short_filled_quantity: float,
    target_quantity: float,
    residual_recorded: bool = False,
) -> dict[str, Any]:
    """EXIT_SUBMITTED -> CLOSED_PENDING_RECONCILIATION gate.

    Both legs must be closed or explicit residual unwind recorded.
    """
    long_closed = float(long_filled_quantity) >= float(target_quantity) * 0.999
    short_closed = float(short_filled_quantity) >= float(target_quantity) * 0.999
    both_closed = long_closed and short_closed
    if both_closed:
        return {"allowed": True, "new_state": "CLOSED_PENDING_RECONCILIATION"}
    if residual_recorded:
        return {"allowed": True, "new_state": "CLOSED_PENDING_RECONCILIATION"}
    return {
        "allowed": False,
        "new_state": "PARTIALLY_CLOSED",
        "reason": "both_legs_not_closed_and_no_residual_recorded",
    }


# ---------------------------------------------------------------------------
# Post-settlement schedule probes (T+5 through T+15)
# ---------------------------------------------------------------------------

def post_settlement_probe_decision(
    *,
    probes: list[dict[str, Any]],
    now: datetime,
    settlement_at: datetime,
    probe_start_seconds: float = 5.0,
    probe_end_seconds: float = 15.0,
    probe_interval_seconds: float = 1.0,
    tolerance_seconds: float = 1.0,
    close_at_t20: bool = False,
) -> dict[str, Any]:
    """Evaluate post-settlement probes for next schedule discovery.

    Require two consecutive fresh responses with same next timestamps.
    Exact next timestamp alignment <= 1s.
    Mismatch or missing timestamp by T+15 => close at T+20.
    """
    seconds_after = (now - settlement_at).total_seconds()
    reasons: list[str] = []
    if seconds_after < probe_start_seconds:
        return {"decision": "wait", "reason": "probe_window_not_started"}
    if not probes:
        if seconds_after >= probe_end_seconds:
            return {
                "decision": "close_at_t20",
                "reason": "no_probes_received_by_t15",
                "close_at_t20": True,
            }
        return {"decision": "wait", "reason": "no_probes_yet"}
    sorted_probes = sorted(
        probes,
        key=lambda p: str(p.get("observed_at") or ""),
    )
    consecutive_matches = 0
    agreed_long_next: str | None = None
    agreed_short_next: str | None = None
    for probe in sorted_probes:
        long_next = str(probe.get("long_next_funding_at") or "")
        short_next = str(probe.get("short_next_funding_at") or "")
        is_fresh = bool(probe.get("fresh"))
        if not is_fresh or not long_next or not short_next:
            consecutive_matches = 0
            agreed_long_next = None
            agreed_short_next = None
            continue
        long_time = parse_time(long_next)
        short_time = parse_time(short_next)
        if long_time is None or short_time is None:
            consecutive_matches = 0
            agreed_long_next = None
            agreed_short_next = None
            continue
        skew = settlement_skew_seconds(long_time, short_time)
        if skew is None or skew > tolerance_seconds:
            consecutive_matches = 0
            agreed_long_next = None
            agreed_short_next = None
            continue
        if (
            agreed_long_next == long_next
            and agreed_short_next == short_next
        ):
            consecutive_matches += 1
        else:
            consecutive_matches = 1
            agreed_long_next = long_next
            agreed_short_next = short_next
        if consecutive_matches >= 2:
            long_t = parse_time(long_next)
            short_t = parse_time(short_next)
            next_cycle_at = max(long_t, short_t) if long_t and short_t else None
            return {
                "decision": "hold_next_cycle",
                "long_next_funding_at": long_next,
                "short_next_funding_at": short_next,
                "settlement_skew_seconds": skew,
                "next_cycle_at": next_cycle_at.isoformat() if next_cycle_at else None,
                "consecutive_agreements": consecutive_matches,
            }
    if seconds_after >= probe_end_seconds:
        return {
            "decision": "close_at_t20",
            "reason": "no_consecutive_agreement_by_t15",
            "close_at_t20": True,
        }
    return {"decision": "wait", "reason": "awaiting_consecutive_agreement"}


# ---------------------------------------------------------------------------
# Next-cycle observations (through T+30)
# ---------------------------------------------------------------------------

def next_cycle_observation_decision(
    *,
    observations: list[dict[str, Any]],
    now: datetime,
    minimum_observations: int = 15,
    minimum_span_seconds: float = 20.0,
    max_latest_age_seconds: float = 2.0,
    max_cross_venue_skew_seconds: float = 1.0,
    latest_vs_median_fraction: float = 0.8,
    conservative_fraction: float = 0.9,
) -> dict[str, Any]:
    """Evaluate next-cycle observations for hold underwriting."""
    reasons: list[str] = []
    if len(observations) < minimum_observations:
        reasons.append(
            f"insufficient_observations_{len(observations)}<{minimum_observations}"
        )
    gross_values = [float(row.get("gross_funding_pnl") or 0.0) for row in observations]
    observed_times = [
        parse_time(row.get("observed_at"))
        for row in observations
        if parse_time(row.get("observed_at")) is not None
    ]
    latest = max(observed_times) if observed_times else None
    earliest = min(observed_times) if observed_times else None
    span = (
        (latest - earliest).total_seconds()
        if latest is not None and earliest is not None
        else 0.0
    )
    latest_age = (
        (now.astimezone(UTC) - latest.astimezone(UTC)).total_seconds()
        if latest is not None
        else None
    )
    if span < minimum_span_seconds:
        reasons.append(f"observation_span_{span:.1f}s_below_{minimum_span_seconds}s")
    if latest_age is None or latest_age > max_latest_age_seconds:
        reasons.append(
            f"latest_observation_age_exceeds_{max_latest_age_seconds}s"
            if latest_age is not None
            else "latest_observation_age_missing"
        )
    if gross_values and not all(value > 0 for value in gross_values):
        reasons.append("not_all_gross_funding_positive")
    from statistics import median
    median_gross = median(gross_values) if gross_values else 0.0
    latest_gross = gross_values[-1] if gross_values else 0.0
    if gross_values and latest_gross < latest_vs_median_fraction * median_gross:
        reasons.append(
            f"latest_gross_below_{latest_vs_median_fraction}*median"
        )
    conservative_funding = conservative_fraction * min(gross_values) if gross_values else 0.0
    cross_venue_skews = [
        float(row.get("cross_venue_skew_seconds") or 0.0)
        for row in observations
    ]
    max_skew = max(cross_venue_skews) if cross_venue_skews else 0.0
    if max_skew > max_cross_venue_skew_seconds:
        reasons.append(
            f"cross_venue_skew_{max_skew:.3f}s_exceeds_{max_cross_venue_skew_seconds}s"
        )
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "observation_count": len(observations),
        "observation_span_seconds": span,
        "latest_observation_age_seconds": latest_age,
        "conservative_funding_gross": conservative_funding,
        "median_gross_funding": median_gross,
        "latest_gross_funding": latest_gross,
        "minimum_gross_funding": min(gross_values) if gross_values else 0.0,
        "all_positive": all(value > 0 for value in gross_values) if gross_values else False,
        "max_cross_venue_skew_seconds": max_skew,
    }
