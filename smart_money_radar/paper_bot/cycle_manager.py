from __future__ import annotations

from datetime import datetime
from typing import Any

from smart_money_radar.funding.strategy_synchronized_funding import (
    parse_time,
    settlement_alignment_passed,
    settlement_skew_seconds,
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
