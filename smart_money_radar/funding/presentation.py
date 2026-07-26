from __future__ import annotations

from collections import Counter
import json
from typing import Any

from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES


def filter_deactivated_funding_dashboard_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    blocked = {venue.lower() for venue in DEACTIVATED_FUNDING_VENUES}
    filtered = dict(payload)
    routes = [
        row
        for row in payload.get("routes", [])
        if not funding_route_uses_deactivated_venue(row, blocked)
    ]
    watch_routes = [
        row
        for row in payload.get("watch_routes", [])
        if not funding_route_uses_deactivated_venue(row, blocked)
    ]
    maker_routes = [
        row
        for row in payload.get("maker_routes", [])
        if not funding_route_uses_deactivated_venue(row, blocked)
    ]
    filtered["routes"] = routes
    filtered["watch_routes"] = watch_routes
    filtered["maker_routes"] = maker_routes
    filtered["venues"] = [
        row
        for row in payload.get("venues", [])
        if str(row.get("venue") or "").lower() not in blocked
    ]
    filtered["route_counts"] = filtered_route_counts(routes, watch_routes, maker_routes)
    filtered["visible_route_count"] = len(routes)
    filtered["internal_watch_route_count"] = len(watch_routes)
    filtered["internal_maker_setup_count"] = len(maker_routes)
    filtered["deactivated_venues_filtered"] = sorted(blocked)
    filtered["constraint_diagnostics"] = filter_constraint_diagnostics(
        payload.get("constraint_diagnostics") or {},
        blocked,
    )
    filtered["universe_summary"] = filter_universe_summary(
        payload.get("universe_summary") or {},
        blocked,
    )
    filtered["horizon_comparisons"] = filter_horizon_comparisons(
        payload.get("horizon_comparisons") or {},
        blocked,
    )
    filtered["instrument_collapse_audit"] = filter_instrument_collapse_audit(
        payload.get("instrument_collapse_audit") or {},
        blocked,
    )
    return filtered


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


def funding_route_uses_deactivated_venue(
    row: dict[str, Any],
    blocked: set[str],
) -> bool:
    if funding_position_uses_deactivated_venue(row, blocked):
        return True
    legs = row.get("legs") or []
    if isinstance(legs, list):
        return any(str((leg or {}).get("venue") or "").lower() in blocked for leg in legs)
    return funding_row_mentions_deactivated_venue(row, blocked)


def funding_row_mentions_deactivated_venue(
    row: dict[str, Any],
    blocked: set[str],
) -> bool:
    text = json.dumps(row, ensure_ascii=False).lower()
    return any(venue in text for venue in blocked)


def filtered_route_counts(
    routes: list[dict[str, Any]],
    watch_routes: list[dict[str, Any]],
    maker_routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    counts.update(str(row.get("status") or "paper_candidate") for row in routes)
    counts.update(str(row.get("status") or "watch") for row in watch_routes)
    if maker_routes and "watch" not in counts:
        counts["watch"] = len(maker_routes)
    return [
        {"status": status, "route_count": count}
        for status, count in sorted(counts.items())
    ]


def filter_constraint_diagnostics(
    diagnostics: dict[str, Any],
    blocked: set[str],
) -> dict[str, Any]:
    filtered = dict(diagnostics)
    filtered["near_misses"] = [
        row
        for row in diagnostics.get("near_misses", [])
        if not funding_route_uses_deactivated_venue(row, blocked)
    ]
    return filtered


def filter_universe_summary(
    summary: dict[str, Any],
    blocked: set[str],
) -> dict[str, Any]:
    filtered = dict(summary)
    filtered["top_routes"] = [
        row
        for row in summary.get("top_routes", [])
        if not funding_route_uses_deactivated_venue(row, blocked)
    ]
    return filtered


def filter_horizon_comparisons(
    comparisons: dict[str, Any],
    blocked: set[str],
) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for horizon, payload in comparisons.items():
        row = dict(payload or {})
        routes = row.get("routes") or {}
        if isinstance(routes, dict):
            row["routes"] = {
                route_key: route
                for route_key, route in routes.items()
                if not funding_route_uses_deactivated_venue(route, blocked)
            }
        filtered[horizon] = row
    return filtered


def filter_instrument_collapse_audit(
    audit: dict[str, Any],
    blocked: set[str],
) -> dict[str, Any]:
    filtered = dict(audit)
    filtered["venue_coverage"] = [
        row
        for row in audit.get("venue_coverage", [])
        if str(row.get("venue") or "").lower() not in blocked
    ]
    filtered["duplicate_groups"] = [
        row
        for row in audit.get("duplicate_groups", [])
        if str(row.get("venue") or "").lower() not in blocked
    ]
    filtered_assets: list[dict[str, Any]] = []
    for row in audit.get("top_assets_by_route_count", []):
        venues = [
            venue
            for venue in row.get("venues", [])
            if str(venue).lower() not in blocked
        ]
        if not venues:
            continue
        updated = dict(row)
        updated["venues"] = venues
        updated["venue_count"] = len(venues)
        updated["route_count"] = len(venues) * (len(venues) - 1) // 2
        filtered_assets.append(updated)
    filtered["top_assets_by_route_count"] = filtered_assets
    return filtered


def filtered_funding_paper_summary(
    original: dict[str, Any],
    accounts: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = dict(original)
    summary["open_position_count"] = len(open_positions)
    return summary
