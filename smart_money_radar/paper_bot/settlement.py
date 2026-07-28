from __future__ import annotations

from typing import Any


RECONCILIATION_STATES = (
    "PENDING",
    "PUBLIC_RATE_CONFIRMED",
    "RATE_AND_MARK_RECONCILED",
    "UNRECONCILED",
)


def settlement_reconciliation_key(position_id: int, venue: str, scheduled_funding_at: str) -> tuple[int, str, str]:
    return (int(position_id), str(venue), str(scheduled_funding_at))


def funding_reconciliation_pnl(
    *,
    side: str,
    quantity: float,
    settlement_mark_price: float,
    confirmed_funding_rate: float,
) -> float:
    notional = float(quantity) * float(settlement_mark_price)
    if str(side).lower() == "long":
        return -notional * float(confirmed_funding_rate)
    return notional * float(confirmed_funding_rate)


def cycle_reconciled(leg_rows: list[dict[str, Any]]) -> bool:
    return bool(leg_rows) and all(
        str(row.get("reconciliation_status")) == "RATE_AND_MARK_RECONCILED"
        for row in leg_rows
    )
