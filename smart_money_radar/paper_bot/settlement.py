from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


RECONCILIATION_STATES = (
    "PENDING",
    "PUBLIC_RATE_CONFIRMED",
    "RATE_AND_MARK_RECONCILED",
    "UNRECONCILED",
)

RECONCILIATION_TOLERANCE_SECONDS = 120.0


def settlement_reconciliation_key(position_id: Any, venue: str, scheduled_funding_at: str) -> tuple[Any, str, str]:
    return (position_id, str(venue), str(scheduled_funding_at))


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
        str(row.get("status") or row.get("reconciliation_status"))
        == "RATE_AND_MARK_RECONCILED"
        for row in leg_rows
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def settlement_schedule_matches(
    public_scheduled_at: Any,
    expected_scheduled_at: Any,
    *,
    tolerance_seconds: float = RECONCILIATION_TOLERANCE_SECONDS,
) -> bool:
    public_time = _parse_time(public_scheduled_at)
    expected_time = _parse_time(expected_scheduled_at)
    if public_time is None or expected_time is None:
        return False
    skew = abs((public_time.astimezone(UTC) - expected_time.astimezone(UTC)).total_seconds())
    return skew <= float(tolerance_seconds)


def reconcile_leg(
    *,
    public_rate: float | None,
    public_mark: float | None,
    side: str,
    quantity: float,
) -> dict[str, Any]:
    """Reconcile a single leg against public data.

    - rate + mark => RATE_AND_MARK_RECONCILED, funding_pnl computed
    - rate only   => PUBLIC_RATE_CONFIRMED, funding_pnl NULL, no balance change
    - timeout     => UNRECONCILED
    """
    if public_rate is None:
        return {
            "status": "UNRECONCILED",
            "rate_status": "MISSING",
            "mark_status": "MISSING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
        }
    if public_mark is None:
        return {
            "status": "PUBLIC_RATE_CONFIRMED",
            "rate_status": "CONFIRMED",
            "mark_status": "MISSING",
            "confirmed_funding_rate": float(public_rate),
            "settlement_mark_price": None,
            "funding_pnl": None,
        }
    pnl = funding_reconciliation_pnl(
        side=side,
        quantity=quantity,
        settlement_mark_price=float(public_mark),
        confirmed_funding_rate=float(public_rate),
    )
    return {
        "status": "RATE_AND_MARK_RECONCILED",
        "rate_status": "CONFIRMED",
        "mark_status": "CONFIRMED",
        "confirmed_funding_rate": float(public_rate),
        "settlement_mark_price": float(public_mark),
        "funding_pnl": pnl,
    }


def build_settlement_crossing_rows(
    *,
    position_id: str,
    cycle_id: str,
    long_venue: str,
    long_symbol: str,
    short_venue: str,
    short_symbol: str,
    scheduled_funding_at: str,
    quantity: float,
) -> list[dict[str, Any]]:
    """Create one PENDING reconciliation row per leg on settlement crossing."""
    return [
        {
            "position_id": position_id,
            "cycle_id": cycle_id,
            "venue": long_venue,
            "symbol": long_symbol,
            "side": "long",
            "scheduled_funding_at": scheduled_funding_at,
            "status": "PENDING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
            "rate_status": None,
            "mark_status": None,
            "evidence": {},
        },
        {
            "position_id": position_id,
            "cycle_id": cycle_id,
            "venue": short_venue,
            "symbol": short_symbol,
            "side": "short",
            "scheduled_funding_at": scheduled_funding_at,
            "status": "PENDING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
            "rate_status": None,
            "mark_status": None,
            "evidence": {},
        },
    ]
