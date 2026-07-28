from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


ORDER_STATES = (
    "SUBMITTED",
    "ACKNOWLEDGED",
    "FILLED",
    "PARTIALLY_FILLED",
    "FAILED",
)

EXECUTION_HAIRCUT_FRACTION = 0.40
DEFAULT_FALLBACK_LATENCY_MS = 750.0
LATENCY_CLAMP_MIN_MS = 750.0
LATENCY_CLAMP_MAX_MS = 3000.0
VENUE_BLOCK_P95_THRESHOLD_MS = 3000.0
RTT_HISTORY_SIZE = 20


@dataclass(frozen=True)
class PaperLegOrder:
    side: str
    venue: str
    symbol: str
    decision_at: datetime
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    filled_at: datetime | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    fee: float = 0.0
    state: str = "SUBMITTED"


@dataclass
class VenueLatencyTracker:
    """Tracks per-venue p95 RTT from the last N requests."""
    rtt_history: list[float] = field(default_factory=list)
    max_history: int = RTT_HISTORY_SIZE

    def record_rtt_ms(self, rtt_ms: float) -> None:
        self.rtt_history.append(float(rtt_ms))
        if len(self.rtt_history) > self.max_history:
            self.rtt_history = self.rtt_history[-self.max_history:]

    def p95_rtt_ms(self) -> float:
        if not self.rtt_history:
            return DEFAULT_FALLBACK_LATENCY_MS
        ordered = sorted(self.rtt_history)
        index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]

    def simulated_latency_ms(self) -> float:
        p95 = self.p95_rtt_ms()
        return max(LATENCY_CLAMP_MIN_MS, min(LATENCY_CLAMP_MAX_MS, p95))

    def is_blocked(self) -> bool:
        if not self.rtt_history:
            return False
        return self.p95_rtt_ms() > VENUE_BLOCK_P95_THRESHOLD_MS


def quantity_mismatch_fraction(long_quantity: float, short_quantity: float) -> float:
    denominator = max(abs(float(long_quantity)), abs(float(short_quantity)))
    if denominator <= 0:
        return 0.0
    return abs(float(long_quantity) - float(short_quantity)) / denominator


def simulate_marketable_ioc(
    levels: list[list[float]],
    side: str,
    target_quantity: float,
    depth_haircut_fraction: float = EXECUTION_HAIRCUT_FRACTION,
) -> dict[str, Any]:
    """Simulate a marketable IOC order against a price-level book.

    Each level is [price, visible_quantity]. The execution haircut reduces
    each level's executable quantity. Returns fill summary.
    """
    target = max(0.0, float(target_quantity))
    haircut = max(0.0, min(1.0, float(depth_haircut_fraction)))
    side_lower = str(side).lower()

    if side_lower == "buy":
        sorted_levels = sorted(levels, key=lambda lv: float(lv[0]))
    elif side_lower == "sell":
        sorted_levels = sorted(levels, key=lambda lv: float(lv[0]), reverse=True)
    else:
        raise ValueError(f"Unknown side: {side!r}; expected 'buy' or 'sell'")

    filled_quantity = 0.0
    notional = 0.0
    remaining = target

    for level in sorted_levels:
        if remaining <= 0:
            break
        price = float(level[0])
        visible_qty = float(level[1])
        executable_qty = visible_qty * haircut
        fill_qty = min(executable_qty, remaining)
        if fill_qty <= 0:
            continue
        filled_quantity += fill_qty
        notional += fill_qty * price
        remaining -= fill_qty

    average_fill_price = notional / filled_quantity if filled_quantity > 0 else 0.0
    fee = 0.0
    unfilled_quantity = max(0.0, remaining)

    return {
        "filled_quantity": filled_quantity,
        "average_fill_price": average_fill_price,
        "notional": notional,
        "fee": fee,
        "unfilled_quantity": unfilled_quantity,
    }


def entry_fill_state(
    *,
    long_filled_quantity: float,
    short_filled_quantity: float,
    target_quantity: float,
) -> dict[str, object]:
    target = max(0.0, float(target_quantity))
    long_ratio = float(long_filled_quantity) / target if target > 0 else 0.0
    short_ratio = float(short_filled_quantity) / target if target > 0 else 0.0
    mismatch = quantity_mismatch_fraction(long_filled_quantity, short_filled_quantity)
    if long_ratio >= 0.999 and short_ratio >= 0.999 and mismatch <= 0.001:
        state = "OPEN"
    elif long_ratio > 0 or short_ratio > 0:
        state = "PARTIALLY_HEDGED"
    else:
        state = "FAILED"
    return {
        "state": state,
        "long_fill_ratio": long_ratio,
        "short_fill_ratio": short_ratio,
        "quantity_mismatch_fraction": mismatch,
    }


def t20_deadline_passed(
    fill_time: datetime,
    settlement_at: datetime,
    deadline_lead_seconds: float = 20.0,
) -> bool:
    """Both legs must be filled no later than settlement_at - 20 seconds."""
    deadline = settlement_at - timedelta(seconds=float(deadline_lead_seconds))
    return fill_time <= deadline


def residual_adverse_penalty(
    residual_quantity: float,
    reference_price: float,
    adverse_bps: float = 100.0,
) -> float:
    """Penalty for residual quantity that could not be filled at market."""
    return abs(float(residual_quantity)) * float(reference_price) * float(adverse_bps) / 10_000.0
