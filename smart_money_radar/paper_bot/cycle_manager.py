from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.strategy_synchronized_funding import (
    entry_underwriting,
    parse_time,
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
    "EXITING",
    "CLOSED",
    "FAILED",
)

PRE_ENTRY_STATES = {"DISCOVERED", "ARMED"}
LIVE_STATES = {"OPEN", "EXITING"}
TERMINAL_STATES = {"CLOSED", "FAILED"}


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
        "new_state": "EXITING" if needs_emergency else "CLOSED",
    }


def exit_close_gate(
    *,
    long_filled_quantity: float,
    short_filled_quantity: float,
    target_quantity: float,
    residual_recorded: bool = False,
) -> dict[str, Any]:
    """EXITING -> CLOSED gate.

    Both legs must be closed or explicit residual unwind recorded.
    """
    long_closed = float(long_filled_quantity) >= float(target_quantity) * 0.999
    short_closed = float(short_filled_quantity) >= float(target_quantity) * 0.999
    both_closed = long_closed and short_closed
    if both_closed:
        return {"allowed": True, "new_state": "CLOSED"}
    if residual_recorded:
        return {"allowed": True, "new_state": "CLOSED"}
    return {
        "allowed": False,
        "new_state": "EXITING",
        "reason": "both_legs_not_closed_and_no_residual_recorded",
    }
