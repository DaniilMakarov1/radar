from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


ORDER_STATES = (
    "SUBMITTED",
    "ACKNOWLEDGED",
    "FILLED",
    "PARTIALLY_FILLED",
    "FAILED",
)


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


def quantity_mismatch_fraction(long_quantity: float, short_quantity: float) -> float:
    denominator = max(abs(float(long_quantity)), abs(float(short_quantity)))
    if denominator <= 0:
        return 0.0
    return abs(float(long_quantity) - float(short_quantity)) / denominator


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
