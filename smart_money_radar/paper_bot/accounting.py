from __future__ import annotations

from typing import Any


def leg_price_pnl(side: str, quantity: float, entry_price: float, exit_price: float) -> float:
    q = float(quantity)
    if str(side).lower() == "long":
        return q * (float(exit_price) - float(entry_price))
    return q * (float(entry_price) - float(exit_price))


def taker_fee(quantity: float, price: float, fee_rate: float) -> float:
    return abs(float(quantity) * float(price) * float(fee_rate))


def executable_paper_pnl(
    *,
    quantity: float,
    long_entry_price: float,
    long_exit_price: float,
    short_entry_price: float,
    short_exit_price: float,
    long_taker_fee: float,
    short_taker_fee: float,
    confirmed_funding_pnl: float = 0.0,
    paper_open_fees: float = 0.0,
    emergency_unwind_costs_already_incurred: float = 0.0,
) -> dict[str, Any]:
    long_price = leg_price_pnl("long", quantity, long_entry_price, long_exit_price)
    short_price = leg_price_pnl("short", quantity, short_entry_price, short_exit_price)
    close_fees = taker_fee(quantity, long_exit_price, long_taker_fee) + taker_fee(
        quantity,
        short_exit_price,
        short_taker_fee,
    )
    price_pnl = long_price + short_price
    net_if_exit = (
        price_pnl
        + float(confirmed_funding_pnl)
        - float(paper_open_fees)
        - close_fees
        - float(emergency_unwind_costs_already_incurred)
    )
    return {
        "paper_long_price_pnl": long_price,
        "paper_short_price_pnl": short_price,
        "paper_price_pnl": price_pnl,
        "paper_close_fees": close_fees,
        "paper_confirmed_funding_pnl": float(confirmed_funding_pnl),
        "paper_open_fees": float(paper_open_fees),
        "paper_emergency_unwind_cost": float(emergency_unwind_costs_already_incurred),
        "paper_net_if_exit_now": net_if_exit,
    }


# ---------------------------------------------------------------------------
# Idempotent paper event ledger
# ---------------------------------------------------------------------------

def order_fee_event_key(order_id: str) -> str:
    return f"order_fee:{order_id}"


def price_pnl_event_key(
    position_id: str,
    venue: str | None = None,
    *,
    attempt_id: str | None = None,
) -> str:
    identity = str(attempt_id or position_id)
    if venue:
        return f"price_pnl:{identity}:{venue}:close"
    return f"price_pnl:{identity}:close"


def collateral_reserve_event_key(
    position_id: str,
    venue: str,
    *,
    attempt_id: str | None = None,
) -> str:
    identity = str(attempt_id or position_id)
    return f"collateral_reserve:{identity}:{venue}"


def collateral_release_event_key(
    position_id: str,
    venue: str,
    *,
    attempt_id: str | None = None,
) -> str:
    identity = str(attempt_id or position_id)
    return f"collateral_release:{identity}:{venue}"


def funding_event_key(
    position_id: str,
    venue: str,
    scheduled_funding_at: str,
    *,
    attempt_id: str | None = None,
) -> str:
    identity = str(attempt_id or position_id)
    return f"funding:{identity}:{venue}:{scheduled_funding_at}"


def make_ledger_entry(
    event_key: str,
    *,
    position_id: str | None = None,
    cycle_id: str | None = None,
    venue: str | None = None,
    event_type: str,
    cash_delta: float = 0.0,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_key": event_key,
        "position_id": position_id,
        "cycle_id": cycle_id,
        "venue": venue,
        "event_type": event_type,
        "cash_delta": float(cash_delta),
        "payload": payload or {},
    }
