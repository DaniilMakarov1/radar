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
    summary = dict(original)
    summary["open_position_count"] = len(open_positions)
    return summary
