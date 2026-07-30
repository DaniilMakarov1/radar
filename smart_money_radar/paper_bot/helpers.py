"""Shared utility functions for Paper Bot modules."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from html import escape as html_escape
from typing import Any


def parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def leg_by_side(legs: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    return next((leg for leg in legs if str(leg.get("side")) == side), None)


def route_entry_key(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    def leg_identity(leg: dict[str, Any], side: str) -> list[str]:
        venue = leg.get("venue") or route.get(f"{side}_venue") or ""
        symbol = leg.get("symbol") or route.get(f"{side}_symbol") or ""
        quote = leg.get("quote_asset") or leg.get("settlement_asset") or ""
        collateral = leg.get("collateral_asset") or quote
        product_identity = (
            leg.get("product_id")
            or leg.get("instrument_id")
            or leg.get("market_id")
            or leg.get("api_symbol")
            or symbol
        )
        product_type = (
            leg.get("api_product_type")
            or leg.get("product_type")
            or leg.get("market_type")
            or leg.get("contract_kind")
            or leg.get("contract_type")
            or ""
        )
        return [
            str(venue).lower(),
            str(leg.get("environment") or "mainnet").lower(),
            str(symbol),
            str(product_identity),
            str(quote).upper(),
            str(collateral).upper(),
            str(leg.get("contract_type") or leg.get("contract_kind") or "").lower(),
            str(product_type).lower(),
            str(leg.get("contract_multiplier") or "1"),
            str(leg.get("canonical_unit_multiplier") or "1"),
            str(leg.get("next_funding_at") or ""),
        ]

    return ":".join(
        [
            str(route.get("canonical_asset") or ""),
            *leg_identity(long_leg, "long"),
            *leg_identity(short_leg, "short"),
        ]
    )


def format_money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def format_signed_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if amount > 0 else ""
    return f"{sign}${amount:.2f}" if amount >= 0 else f"-${abs(amount):.2f}"


def tg(value: Any) -> str:
    if value is None:
        return "-"
    return html_escape(str(value), quote=False)


def format_seconds(value: Any) -> str:
    if value is None:
        return "-"
    raw_seconds = float(value)
    overdue = raw_seconds < 0
    seconds = int(abs(raw_seconds))
    minutes, rest = divmod(seconds, 60)
    label = f"{minutes}m {rest}s"
    return f"просрочено {label}" if overdue else label


def format_datetime_utc(value: Any) -> str:
    timestamp = parse_iso(value)
    if timestamp is None:
        return "-"
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_interval_hours(value: Any) -> str:
    hours = optional_float(value)
    if hours is None:
        return "-"
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours))}h"
    return f"{hours:.2f}h"


def format_rate(value: Any) -> str:
    try:
        return f"{float(value) * 100:.4f}%"
    except (TypeError, ValueError):
        return "-"


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return str(value)
    return value


def should_record_routine_scan(result: dict[str, Any]) -> bool:
    return any(
        int(result.get(key) or 0) > 0
        for key in (
            "candidate_count",
            "watch_count",
            "opened_count",
            "closed_count",
            "pending_count",
            "hot_route_count",
            "urgent_route_count",
        )
    )


def is_retention_skip_error(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        return "database is locked" in message or "database is busy" in message
    if isinstance(exc, sqlite3.IntegrityError):
        return "foreign key" in message
    return False


def route_settlement_leads(
    route: dict[str, Any],
    now: datetime,
) -> dict[str, float | None]:
    leads: dict[str, float | None] = {"long": None, "short": None}
    for side in ("long", "short"):
        leg = leg_by_side(route.get("legs") or [], side)
        settlement = parse_iso((leg or {}).get("next_funding_at"))
        if settlement is not None:
            leads[side] = (settlement - now).total_seconds()
    return leads


def route_data_age_seconds(route: dict[str, Any], now: datetime) -> float | None:
    evidence = route.get("evidence") or {}
    focused = evidence.get("focused_recheck") or {}
    observed = parse_iso(focused.get("observed_at")) or parse_iso(route.get("observed_at"))
    if observed is None:
        return None
    return max(0.0, (now - observed).total_seconds())


def status_route_sort_key(route: dict[str, Any]) -> tuple[float, float]:
    evidence = route.get("evidence") or {}
    selected = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    ) or {}
    live_net = (
        optional_float(selected.get("expected_net_pnl"))
        if selected
        else None
    )
    if live_net is None:
        live_net = optional_float(evidence.get("current_nowcast_net")) or 0.0
    threshold = (
        optional_float(selected.get("actionable_profit_threshold"))
        if selected
        else None
    )
    if threshold is None:
        threshold = optional_float(evidence.get("actionable_profit_threshold")) or 0.0
    return live_net, live_net - threshold


def ranked_status_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(routes, key=status_route_sort_key, reverse=True)
