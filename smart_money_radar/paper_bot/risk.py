from __future__ import annotations

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
