from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def common_price_move_telemetry(
    snapshot: dict[str, Any],
    *,
    alert_fraction: float = 0.05,
    critical_fraction: float = 0.10,
    max_leg_move_divergence_fraction: float = 0.01,
) -> dict[str, Any]:
    if not snapshot.get("price_move_tracking"):
        return {"level": "none", "reason": snapshot.get("reason") or "not_tracked"}
    long_move = float(snapshot.get("long_move_fraction") or 0.0)
    short_move = float(snapshot.get("short_move_fraction") or 0.0)
    common_move = abs(long_move - short_move) <= float(max_leg_move_divergence_fraction)
    max_abs = max(abs(long_move), abs(short_move))
    level = "none"
    if common_move and max_abs >= float(critical_fraction):
        level = "critical"
    elif common_move and max_abs >= float(alert_fraction):
        level = "warning"
    return {
        "level": level,
        "common_move": common_move,
        "long_move_fraction": long_move,
        "short_move_fraction": short_move,
        "max_abs_move_fraction": max_abs,
        "requires_fresh_risk_recalculation": level == "critical",
    }


def synthetic_liquidation_prices(
    *,
    quantity: float,
    long_entry_price: float,
    short_entry_price: float,
    long_isolated_collateral: float,
    short_isolated_collateral: float,
    long_mmr: float = 0.02,
    short_mmr: float = 0.02,
) -> dict[str, float]:
    q = max(float(quantity), 1e-12)
    long_liq = max(
        0.0,
        (q * float(long_entry_price) - float(long_isolated_collateral))
        / (q * (1.0 - float(long_mmr))),
    )
    short_liq = (
        float(short_isolated_collateral) + q * float(short_entry_price)
    ) / (q * (1.0 + float(short_mmr)))
    return {"long_liquidation_price": long_liq, "short_liquidation_price": short_liq}


def liquidation_distance(price: float, liquidation_price: float, side: str) -> float:
    current = max(float(price), 1e-12)
    if str(side).lower() == "long":
        return (current - float(liquidation_price)) / current
    return (float(liquidation_price) - current) / current


def hard_risk_triggered(
    *,
    quantity_mismatch_fraction: float = 0.0,
    mark_index_divergence_bps: float = 0.0,
    liquidation_distance_fraction: float = 1.0,
    margin_safety_ratio: float = 99.0,
    basis_deterioration_bps: float = 0.0,
    active_risk_budget_bps: float = 50.0,
    snapshot_age_seconds: float = 0.0,
) -> tuple[bool, str]:
    if float(quantity_mismatch_fraction) > 0.001:
        return True, "quantity_mismatch"
    if float(mark_index_divergence_bps) > 100.0:
        return True, "mark_index_divergence"
    if float(liquidation_distance_fraction) <= 0.20:
        return True, "liquidation_distance"
    if float(margin_safety_ratio) <= 3.0:
        return True, "margin_safety"
    if float(basis_deterioration_bps) >= float(active_risk_budget_bps):
        return True, "basis_deterioration"
    if float(snapshot_age_seconds) > 5.0:
        return True, "risk_data_hard_stale"
    return False, ""


# ---------------------------------------------------------------------------
# Dynamic basis risk
# ---------------------------------------------------------------------------

def dynamic_basis_risk_budget_bps(
    active_cycle_conservative_funding_edge_bps: float,
) -> float:
    """Active risk budget = clamp(25, 50, 0.5 * conservative funding edge bps)."""
    return max(25.0, min(50.0, 0.5 * float(active_cycle_conservative_funding_edge_bps)))


def basis_deterioration_bps(
    *,
    entry_spread: float,
    current_exit_spread: float,
    reference_price: float,
) -> float:
    """Deterioration = max(0, current_exit_spread - entry_spread) / ref * 10000."""
    if float(reference_price) <= 0:
        return 0.0
    return max(0.0, float(current_exit_spread) - float(entry_spread)) / float(reference_price) * 10_000.0


def dynamic_basis_stop_decision(
    *,
    entry_spread: float,
    current_exit_spread: float,
    reference_price: float,
    active_cycle_conservative_funding_edge_bps: float,
) -> dict[str, Any]:
    budget = dynamic_basis_risk_budget_bps(active_cycle_conservative_funding_edge_bps)
    deterioration = basis_deterioration_bps(
        entry_spread=entry_spread,
        current_exit_spread=current_exit_spread,
        reference_price=reference_price,
    )
    return {
        "active_risk_budget_bps": budget,
        "basis_deterioration_bps": deterioration,
        "immediate_hard_exit": deterioration >= budget,
        "reason": "basis_deterioration" if deterioration >= budget else "",
    }


# ---------------------------------------------------------------------------
# Stale data handling
# ---------------------------------------------------------------------------

def stale_data_decision(
    *,
    snapshot_age_seconds: float,
    stale_retries: int = 0,
    max_retries: int = 3,
    retry_interval_seconds: float = 0.5,
    healthy_threshold_seconds: float = 2.0,
    hard_stale_threshold_seconds: float = 5.0,
) -> dict[str, Any]:
    age = float(snapshot_age_seconds)
    if age <= healthy_threshold_seconds:
        return {
            "status": "healthy",
            "entry_allowed": True,
            "retry_needed": False,
            "emergency_unwind": False,
        }
    if age <= hard_stale_threshold_seconds:
        return {
            "status": "degraded",
            "entry_allowed": False,
            "retry_needed": stale_retries < max_retries,
            "retry_after_seconds": retry_interval_seconds,
            "emergency_unwind": False,
        }
    return {
        "status": "hard_stale",
        "entry_allowed": False,
        "retry_needed": False,
        "emergency_unwind": True,
        "close_reason": "risk_data_hard_stale",
        "penalty_bps": 100.0,
    }


# ---------------------------------------------------------------------------
# Risk polling integration helpers
# ---------------------------------------------------------------------------

def risk_poll_interval_seconds(
    *,
    now: datetime,
    settlement_at: datetime | None = None,
    position_opened_at: datetime | None = None,
    normal_interval: float = 2.0,
    hot_interval: float = 1.0,
) -> float:
    """Poll every 1s near settlement or T..T+30, else 2s."""
    if settlement_at is not None:
        seconds_to = (settlement_at - now).total_seconds()
        if -30 <= seconds_to <= 35:
            return hot_interval
    return normal_interval


def entry_risk_gates(
    *,
    liquidation_distance_fraction: float,
    margin_safety_ratio: float,
    mark_index_divergence_bps: float,
) -> dict[str, Any]:
    passed = (
        float(liquidation_distance_fraction) >= 0.50
        and float(margin_safety_ratio) >= 10.0
        and float(mark_index_divergence_bps) <= 50.0
    )
    return {
        "passed": passed,
        "liquidation_distance_fraction": float(liquidation_distance_fraction),
        "margin_safety_ratio": float(margin_safety_ratio),
        "mark_index_divergence_bps": float(mark_index_divergence_bps),
    }


def risk_warnings(
    *,
    liquidation_distance_fraction: float,
    margin_safety_ratio: float,
) -> list[str]:
    warnings: list[str] = []
    if float(liquidation_distance_fraction) < 0.30:
        warnings.append("liquidation_distance_low")
    if float(margin_safety_ratio) < 5.0:
        warnings.append("margin_safety_ratio_low")
    return warnings


# ---------------------------------------------------------------------------
# Lightweight discovery helpers
# ---------------------------------------------------------------------------

def lightweight_discovery_due(
    *,
    last_discovery_monotonic: float,
    now_monotonic: float,
    interval_seconds: float = 30.0,
) -> bool:
    return (now_monotonic - last_discovery_monotonic) >= float(interval_seconds)


def focused_recheck_capacity_check(
    *,
    watch_route_count: int,
    max_focused_routes: int = 20,
    target_cadence_seconds: float = 1.0,
) -> dict[str, Any]:
    if watch_route_count > max_focused_routes:
        return {
            "sufficient": False,
            "reason": "focused_recheck_capacity_insufficient",
            "entry_forbidden": True,
        }
    return {
        "sufficient": True,
        "watch_route_count": watch_route_count,
        "target_cadence_seconds": target_cadence_seconds,
        "entry_forbidden": False,
    }
