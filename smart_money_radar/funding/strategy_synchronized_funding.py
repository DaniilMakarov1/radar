from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

STRATEGY_NAME = "synchronized_funding_capture"
STRATEGY_VERSION = "synchronized_funding_capture_v2"


def clamp(minimum: float, maximum: float, value: float) -> float:
    return max(float(minimum), min(float(maximum), float(value)))


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def funding_leg_pnl(side: str, quantity: float, mark_price: float, funding_rate: float) -> float:
    notional = float(quantity) * float(mark_price)
    if str(side).lower() == "long":
        return -notional * float(funding_rate)
    return notional * float(funding_rate)


def gross_funding_pnl(
    *,
    quantity: float,
    long_mark: float,
    short_mark: float,
    long_funding_rate: float,
    short_funding_rate: float,
) -> float:
    return funding_leg_pnl("long", quantity, long_mark, long_funding_rate) + funding_leg_pnl(
        "short",
        quantity,
        short_mark,
        short_funding_rate,
    )


def settlement_skew_seconds(long_next: Any, short_next: Any) -> float | None:
    long_time = parse_time(long_next)
    short_time = parse_time(short_next)
    if long_time is None or short_time is None:
        return None
    return abs((long_time.astimezone(UTC) - short_time.astimezone(UTC)).total_seconds())


def settlement_alignment_passed(
    long_next: Any,
    short_next: Any,
    *,
    tolerance_seconds: float = 1.0,
) -> bool:
    skew = settlement_skew_seconds(long_next, short_next)
    return skew is not None and skew <= float(tolerance_seconds)


def entry_window_passed(
    lead_seconds: float,
    *,
    minimum: float = 25.0,
    maximum: float = 35.0,
) -> bool:
    return float(minimum) <= float(lead_seconds) <= float(maximum)


def initial_entry_economics(
    *,
    conservative_funding_gross: float,
    baseline_round_trip_book_cost: float,
    total_round_trip_fee_estimate: float,
    entry_basis_reserve_usd: float,
    entry_legging_reserve_usd: float,
    reference_notional: float,
) -> dict[str, Any]:
    modeled_cost = (
        max(0.0, float(baseline_round_trip_book_cost))
        + max(0.0, float(total_round_trip_fee_estimate))
        + max(0.0, float(entry_basis_reserve_usd))
        + max(0.0, float(entry_legging_reserve_usd))
    )
    funding = float(conservative_funding_gross)
    reference = max(0.0, float(reference_notional))
    expected_net = funding - modeled_cost
    coverage = funding / modeled_cost if modeled_cost > 0 else math.inf if funding > 0 else 0.0
    gross_threshold = max(2.50, reference * 0.005)
    net_threshold = max(1.00, reference * 0.002)
    return {
        "strategy_name": STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "conservative_funding_gross": funding,
        "baseline_round_trip_book_cost": max(0.0, float(baseline_round_trip_book_cost)),
        "total_round_trip_fee_estimate": max(0.0, float(total_round_trip_fee_estimate)),
        "entry_basis_reserve_usd": max(0.0, float(entry_basis_reserve_usd)),
        "entry_legging_reserve_usd": max(0.0, float(entry_legging_reserve_usd)),
        "initial_total_modeled_cost": modeled_cost,
        "initial_expected_net_pnl": expected_net,
        "conservative_funding_edge_bps": funding / reference * 10_000.0 if reference > 0 else 0.0,
        "initial_expected_net_bps": expected_net / reference * 10_000.0 if reference > 0 else 0.0,
        "initial_cost_coverage_ratio": coverage,
        "minimum_conservative_funding_gross": gross_threshold,
        "minimum_initial_expected_net_pnl": net_threshold,
        "eligible": (
            funding >= gross_threshold
            and expected_net >= net_threshold
            and coverage >= 1.50
        ),
        "expected_spread_convergence_pnl": 0.0,
    }


def percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def basis_duration_floor_bps(wait_seconds: float) -> float:
    wait_hours = max(0.0, float(wait_seconds)) / 3600.0
    return min(100.0, max(25.0, 25.0 * math.ceil(max(wait_hours, 1e-12))))


def hold_economics(
    *,
    next_conservative_funding_gross: float,
    current_close_fees: float,
    reference_notional: float,
    wait_seconds: float,
    entry_basis_reserve_bps: float,
    adverse_basis_change_30s_bps: list[float] | None = None,
    p95_abs_mark_return_1s_bps: float | None = None,
    close_depth_multiple: float = 10.0,
) -> dict[str, Any]:
    reference = max(0.0, float(reference_notional))
    current_close_fee_stress = max(0.0, float(current_close_fees)) * 1.10
    additional_fee_reserve = max(0.0, current_close_fee_stress - max(0.0, float(current_close_fees)))
    observed_changes = [max(0.0, float(value)) for value in adverse_basis_change_30s_bps or []]
    p95_adverse = percentile_95(observed_changes) if len(observed_changes) >= 10 else 5.0
    scaled_observed = p95_adverse * math.sqrt(max(0.0, float(wait_seconds)) / 30.0)
    basis_reserve_bps = clamp(
        25.0,
        200.0,
        max(
            float(entry_basis_reserve_bps),
            basis_duration_floor_bps(wait_seconds),
            scaled_observed,
        ),
    )
    legging_reserve_bps = (
        clamp(10.0, 30.0, 2.0 * float(p95_abs_mark_return_1s_bps))
        if p95_abs_mark_return_1s_bps is not None
        else 15.0
    )
    wait_hours = max(0.0, float(wait_seconds)) / 3600.0
    time_reserve_bps = min(40.0, 10.0 * math.ceil(max(wait_hours, 1e-12)))
    liquidity_reserve_bps = 10.0 if float(close_depth_multiple) >= 10.0 else 20.0
    total_reserve_bps = (
        basis_reserve_bps
        + legging_reserve_bps
        + time_reserve_bps
        + liquidity_reserve_bps
    )
    reserve_usd = reference * total_reserve_bps / 10_000.0
    incremental_cost = additional_fee_reserve + reserve_usd
    funding = float(next_conservative_funding_gross)
    incremental_net = funding - incremental_cost
    coverage = funding / incremental_cost if incremental_cost > 0 else math.inf if funding > 0 else 0.0
    return {
        "current_close_fee_stress": current_close_fee_stress,
        "additional_fee_reserve": additional_fee_reserve,
        "p95_adverse_basis_change_30s_bps": p95_adverse,
        "duration_basis_floor_bps": basis_duration_floor_bps(wait_seconds),
        "scaled_observed_basis_bps": scaled_observed,
        "hold_basis_reserve_bps": basis_reserve_bps,
        "hold_legging_reserve_bps": legging_reserve_bps,
        "hold_time_reserve_bps": time_reserve_bps,
        "hold_liquidity_reserve_bps": liquidity_reserve_bps,
        "hold_total_reserve_bps": total_reserve_bps,
        "hold_total_reserve_usd": reserve_usd,
        "incremental_hold_cost": incremental_cost,
        "incremental_hold_net_pnl": incremental_net,
        "incremental_hold_edge_bps": incremental_net / reference * 10_000.0 if reference > 0 else 0.0,
        "hold_cost_coverage_ratio": coverage,
    }


def summarize_funding_observations(
    observations: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    gross_values = [float(row["gross_funding_pnl"]) for row in observations]
    observed_times = [
        parse_time(row.get("observed_at"))
        for row in observations
        if parse_time(row.get("observed_at")) is not None
    ]
    latest = max(observed_times) if observed_times else None
    earliest = min(observed_times) if observed_times else None
    latest_age = (
        (now.astimezone(UTC) - latest.astimezone(UTC)).total_seconds()
        if latest is not None
        else None
    )
    return {
        "observation_count": len(observations),
        "observation_span_seconds": (
            (latest - earliest).total_seconds()
            if latest is not None and earliest is not None
            else 0.0
        ),
        "latest_observation_age_seconds": latest_age,
        "median_gross_funding": median(gross_values) if gross_values else 0.0,
        "minimum_gross_funding": min(gross_values) if gross_values else 0.0,
        "latest_gross_funding": gross_values[-1] if gross_values else 0.0,
        "all_positive": all(value > 0 for value in gross_values),
        "latest_vs_median_ok": (
            gross_values[-1] >= 0.80 * median(gross_values)
            if gross_values
            else False
        ),
        "conservative_funding_gross": 0.90 * min(gross_values) if gross_values else 0.0,
    }


def synchronized_strategy_candidate(
    row: dict[str, Any],
    *,
    current_funding_gross: float,
    actionable_profit_threshold: float,
    blocking_risk_flags: list[str],
    decision_mode: str,
) -> dict[str, Any]:
    funding_notional = float(row.get("funding_notional") or row.get("notional") or 0.0)
    execution_cost = float(row.get("execution_cost") or 0.0)
    basis_stress_loss = max(0.0, float(row.get("basis_stress_loss") or 0.0))
    conservative_funding = float(current_funding_gross)
    expected_net = conservative_funding - execution_cost - basis_stress_loss
    threshold = max(float(actionable_profit_threshold or 0.0), 1.0, funding_notional * 0.002)
    gross_threshold = max(2.50, funding_notional * 0.005)
    coverage_denominator = execution_cost + basis_stress_loss
    coverage = (
        conservative_funding / coverage_denominator
        if coverage_denominator > 0
        else math.inf if conservative_funding > 0 else 0.0
    )
    blockers = [
        str(flag)
        for flag in dict.fromkeys(blocking_risk_flags)
        if str(flag) not in {"basis_not_covered_by_funding", "basis_not_covered_by_live_funding"}
    ]
    reasons = list(blockers)
    if str(decision_mode) != "settlement_capture":
        reasons.append("not_next_settlement_capture")
    if conservative_funding < gross_threshold:
        reasons.append("conservative_funding_below_minimum")
    if expected_net < threshold:
        reasons.append("initial_expected_net_below_minimum")
    if coverage < 1.50:
        reasons.append("initial_cost_coverage_below_minimum")
    return {
        "selection_model": STRATEGY_VERSION,
        "strategy_name": STRATEGY_NAME,
        "strategy_class": STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "primary_edge": "synchronized_funding",
        "edge_type": "funding_led",
        "edge_label": "Synchronized funding capture",
        "edge_quality": "clean" if not reasons else "blocked",
        "eligible": not reasons,
        "expected_net_pnl": expected_net,
        "gross_edge_pnl": conservative_funding,
        "funding_pnl_component": conservative_funding,
        "spread_pnl_component": 0.0,
        "signed_spread_pnl_component": 0.0,
        "expected_spread_convergence_pnl": 0.0,
        "execution_cost": execution_cost,
        "basis_stress_loss": basis_stress_loss,
        "actionable_profit_threshold": threshold,
        "minimum_conservative_funding_gross": gross_threshold,
        "coverage_ratio": coverage,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": [],
        "thesis": (
            "Funding-only synchronized settlement capture: expected spread "
            "convergence is zero; executable spread and basis are modeled as cost/risk."
        ),
    }
