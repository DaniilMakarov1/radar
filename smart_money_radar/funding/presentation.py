from __future__ import annotations

import json
from typing import Any

from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES


def filter_deactivated_funding_paper_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    blocked = {venue.lower() for venue in DEACTIVATED_FUNDING_VENUES}
    filtered = dict(payload)
    accounts = [
        row
        for row in payload.get("accounts", [])
        if str(row.get("venue") or "").lower() not in blocked
    ]
    open_positions = [
        row
        for row in payload.get("open_positions", [])
        if not funding_position_uses_deactivated_venue(row, blocked)
    ]
    closed_positions = [
        row
        for row in payload.get("closed_positions", [])
        if not funding_position_uses_deactivated_venue(row, blocked)
    ]
    events = [
        row
        for row in payload.get("events", [])
        if not funding_row_mentions_deactivated_venue(row, blocked)
    ]
    trade_events = [
        row
        for row in payload.get("trade_events", [])
        if not funding_row_mentions_deactivated_venue(row, blocked)
    ]
    system_events = [
        row
        for row in payload.get("system_events", [])
        if not funding_row_mentions_deactivated_venue(row, blocked)
    ]
    filtered["accounts"] = accounts
    filtered["open_positions"] = open_positions
    filtered["closed_positions"] = closed_positions
    filtered["events"] = events
    filtered["trade_events"] = trade_events
    filtered["system_events"] = system_events
    filtered["summary"] = filtered_funding_paper_summary(
        payload.get("summary", {}),
        accounts,
        open_positions,
        closed_positions,
    )
    return filtered


def filter_deactivated_funding_paper_export_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocked = {venue.lower() for venue in DEACTIVATED_FUNDING_VENUES}
    return [
        row
        for row in rows
        if str(row.get("Long") or "").lower() not in blocked
        and str(row.get("Short") or "").lower() not in blocked
        and not funding_row_mentions_deactivated_venue(row, blocked)
    ]


def funding_position_uses_deactivated_venue(
    row: dict[str, Any],
    blocked: set[str],
) -> bool:
    return (
        str(row.get("long_venue") or "").lower() in blocked
        or str(row.get("short_venue") or "").lower() in blocked
    )


def funding_row_mentions_deactivated_venue(
    row: dict[str, Any],
    blocked: set[str],
) -> bool:
    text = json.dumps(row, ensure_ascii=False).lower()
    return any(venue in text for venue in blocked)


def filtered_funding_paper_summary(
    original: dict[str, Any],
    accounts: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    starting = sum(float(row.get("starting_balance") or 0.0) for row in accounts)
    total_cash = sum(float(row.get("cash_balance") or 0.0) for row in accounts)
    reserved = sum(float(row.get("reserved_margin") or 0.0) for row in accounts)
    realized = sum(float(row.get("realized_pnl") or 0.0) for row in accounts)
    net_pnls = [
        float(row.get("actual_net_pnl"))
        for row in closed_positions
        if row.get("actual_net_pnl") is not None
    ]
    summary = dict(original)
    summary.update(
        {
            "starting_capital": starting,
            "total_cash": total_cash,
            "reserved_margin": reserved,
            "available_cash": total_cash - reserved,
            "realized_pnl": realized,
            "return_pct": (total_cash - starting) / starting if starting > 0 else 0.0,
            "open_position_count": len(open_positions),
            "closed_trade_count": len(closed_positions),
            "win_rate": (
                sum(1 for value in net_pnls if value > 0) / len(net_pnls)
                if net_pnls
                else None
            ),
            "average_net_pnl": sum(net_pnls) / len(net_pnls) if net_pnls else None,
            "worst_net_pnl": min(net_pnls) if net_pnls else None,
            "best_net_pnl": max(net_pnls) if net_pnls else None,
        }
    )
    return summary
