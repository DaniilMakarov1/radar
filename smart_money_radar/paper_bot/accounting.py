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
