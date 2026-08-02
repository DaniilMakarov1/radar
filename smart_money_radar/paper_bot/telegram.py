"""Telegram message formatting for Paper Bot."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from smart_money_radar.paper_bot.helpers import (
    format_datetime_utc,
    format_interval_hours,
    format_money,
    format_rate,
    format_seconds,
    format_signed_money,
    leg_by_side,
    optional_float,
    parse_iso,
    ranked_status_routes,
    tg,
)
from smart_money_radar.paper_bot.position import (
    close_reason_from_hold_reasons,
    position_hold_decision,
    required_live_net_profit,
    route_monitor_decision,
    selected_route_strategy,
    status_publishable_candidate,
)

TELEGRAM_SEND_MESSAGE_LIMIT_CHARS = 4096
TELEGRAM_STATUS_MESSAGE_SOFT_LIMIT_CHARS = 3900


def status_report_message(
    result: dict[str, Any],
    routes: list[dict[str, Any]],
    watch_routes: list[dict[str, Any]],
    summary: dict[str, Any],
    config: PaperBotConfig,
) -> str:
    candidates = ranked_status_routes(
        [
            route
            for route in routes
            if status_publishable_candidate(route, config)
        ]
    )
    watched = ranked_status_routes(watch_routes)
    max_routes = int(config.status_report_max_routes)
    venue_health = result.get("venue_health") or {}
    requested_venues = int(venue_health.get("requested_count") or 0)
    ready_venues = int(venue_health.get("ready_count") or 0)
    pending_venues = int(venue_health.get("pending_count") or 0)
    unavailable_venues = int(venue_health.get("unavailable_count") or 0)
    detected_count = int(
        result.get("detected_route_count")
        if result.get("detected_route_count") is not None
        else len(candidates) + len(watched)
    )
    early_count = int(result.get("early_route_count") or 0)
    watch_count = int(result.get("watch_stage_route_count") or 0)
    monitor_count = int(result.get("hot_route_count") or 0)
    urgent_count = int(result.get("urgent_route_count") or 0)
    qualified_count = len(candidates)
    universe_count = int(result.get("universe_route_count") or 0)
    route_count = int(result.get("route_count") or 0)
    full_depth_count = int(result.get("execution_shortlist_count") or route_count)
    lines = [
        "<b>Paper Bot STATUS</b>",
        "",
        (
            f"<b>Mode:</b> <code>{tg(result.get('mode'))}</code> | "
            f"{status_scan_label(result)}"
        ),
        (
            f"<b>Routes detected: {detected_count}</b> | "
            f"Qualified: <b>{qualified_count}</b>"
        ),
        (
            f"Early: {early_count} | Watch: {watch_count} | "
            f"Monitor: {monitor_count} | Urgent: {urgent_count}"
        ),
        (
            f"<b>Open:</b> {int(summary.get('open_position_count') or 0)} | "
            f"Closed: {int(summary.get('closed_trade_count') or 0)} | "
            f"Realized PnL: <b>${float(summary.get('realized_pnl') or 0):.2f}</b>"
        ),
    ]
    if requested_venues:
        lines.extend(
            [
                "",
                (
                    f"<b>Venue coverage:</b> {ready_venues}/{requested_venues} ready | "
                    f"{pending_venues} loading | {unavailable_venues} unavailable"
                ),
                (
                    f"<b>Markets received:</b> "
                    f"{int(result.get('markets_checked') or 0):,}"
                ),
            ]
        )
    if universe_count or full_depth_count:
        lines.extend(
            [
                "",
                (
                    f"<b>Market scan</b>\n"
                    f"Universe screened: <b>{universe_count:,}</b>\n"
                    f"Full-depth modeled: <b>{full_depth_count:,}</b>"
                ),
            ]
        )
        filter_lines = compact_filter_lines(result)
        if filter_lines:
            lines.extend(filter_lines)
    funnel_lines = compact_funnel_lines(result)
    if funnel_lines:
        lines.extend(["", "<b>Discovery funnel</b>", *funnel_lines])
    if candidates:
        lines.extend(["", "<b>Qualified candidates</b>"])
        append_bounded_status_routes(
            lines,
            candidates,
            max_routes=max_routes,
            omitted_label="more",
        )
    else:
        lines.extend(["", "<b>Qualified candidates:</b> нет"])
    if watched:
        lines.extend(["", "<b>Top detected routes</b>"])
        append_bounded_status_routes(
            lines,
            watched,
            max_routes=max_routes,
            omitted_label="more detected routes",
        )
    return "\n".join(lines)


def append_bounded_status_routes(
    lines: list[str],
    routes: list[dict[str, Any]],
    *,
    max_routes: int,
    omitted_label: str,
) -> None:
    shown = 0
    limit = TELEGRAM_STATUS_MESSAGE_SOFT_LIMIT_CHARS
    for index, route in enumerate(routes[:max_routes], start=1):
        block = status_route_line(route, index, include_fee_evidence=False)
        hidden_after_this = len(routes) - index
        candidate_lines = [block]
        if hidden_after_this > 0:
            candidate_lines.append(
                f"<i>...and {hidden_after_this} {tg(omitted_label)}</i>"
            )
        if joined_line_length([*lines, *candidate_lines]) > limit:
            break
        lines.append(block)
        shown = index

    hidden = len(routes) - shown
    if hidden > 0:
        hidden_line = f"<i>...and {hidden} {tg(omitted_label)}</i>"
        if joined_line_length([*lines, hidden_line]) <= TELEGRAM_SEND_MESSAGE_LIMIT_CHARS:
            lines.append(hidden_line)


def joined_line_length(lines: list[str]) -> int:
    return len("\n".join(lines))


def status_scan_label(result: dict[str, Any]) -> str:
    scan_id = result.get("funding_scan_id")
    if result.get("mode") == "background_full_market":
        return f"<b>Background scan:</b> <code>{tg(scan_id or '-')}</code>"
    if scan_id:
        return f"<b>DB scan:</b> <code>{tg(scan_id)}</code>"
    return "<b>DB scan:</b> <code>-</code>"


def compact_filter_lines(result: dict[str, Any]) -> list[str]:
    raw_screen_reasons = result.get("screen_reasons") or []
    if isinstance(raw_screen_reasons, dict):
        screen_reasons = [
            {"execution_screen_reason": reason, "route_count": count}
            for reason, count in sorted(
                raw_screen_reasons.items(),
                key=lambda item: int(item[1] or 0),
                reverse=True,
            )[:4]
        ]
    else:
        screen_reasons = list(raw_screen_reasons)[:4]
    blocker_summary = list(result.get("blocker_summary") or [])[:4]
    lines: list[str] = []
    if screen_reasons:
        lines.append("<b>Pre-depth rejects</b>")
        for row in screen_reasons:
            reason = screen_reason_label(row.get("execution_screen_reason"))
            count = int(row.get("route_count") or 0)
            detail = (result.get("rejection_details") or {}).get(
                str(row.get("execution_screen_reason") or "")
            ) or {}
            stage = detail.get("stage")
            denominator = int(detail.get("denominator") or 0)
            suffix = f" / {denominator:,} @ {tg(stage)}" if stage and denominator else ""
            lines.append(f"• {tg(reason)}: <b>{count:,}</b>{suffix}")
    if blocker_summary:
        lines.append("<b>Full-model blockers</b>")
        for row in blocker_summary:
            reason = blocker_label(row.get("risk_flag"))
            count = int(row.get("route_count") or 0)
            lines.append(f"• {tg(reason)}: <b>{count:,}</b>")
    return lines


def compact_funnel_lines(result: dict[str, Any]) -> list[str]:
    funnel = result.get("funnel")
    if not isinstance(funnel, dict) or not funnel:
        return []
    ordered = [
        "markets_received",
        "structurally_rejected_markets",
        "structurally_usable_markets",
        "exact_next_rate_markets",
        "estimated_rate_markets",
        "markets_with_usable_rate_estimates",
        "within_horizon",
        "multi_venue_assets",
        "directed_pairs",
        "hard_blocked_pairs",
        "risk_flagged_pairs",
        "economically_observable",
        "watch",
        "focus_eligible",
        "focused_selected",
        "focused_deferred",
        "experimental_simulation_ready",
        "verified_paper_ready",
        "opened_verified",
        "reconciled",
        "unreconciled",
        "routes_with_any_risk_flag",
        "total_risk_flag_occurrences",
        "directed_pairs_soft_flagged",
        "unique_route_variants",
        "unique_route_families",
        "unique_route_variants_soft_flagged",
        "unique_route_families_soft_flagged",
    ]
    lines: list[str] = []
    for key in ordered:
        row = funnel.get(key)
        if not isinstance(row, dict):
            continue
        count = int(row.get("count") or 0)
        denominator = int(row.get("denominator") or 0)
        label = key.replace("_", " ")
        if denominator:
            lines.append(f"• {tg(label)}: <b>{count:,}</b> / {denominator:,}")
        else:
            lines.append(f"• {tg(label)}: <b>{count:,}</b>")
    examples = list(result.get("blocker_examples") or [])[:3]
    if examples:
        lines.append("<b>Examples</b>")
        for example in examples:
            blockers = ", ".join(str(item) for item in (example.get("blockers") or [])[:3])
            lines.append(
                "• "
                f"{tg(example.get('asset'))}: "
                f"{tg(example.get('long_venue'))}->{tg(example.get('short_venue'))} "
                f"{tg(blockers)}"
            )
    return lines


def screen_reason_label(value: Any) -> str:
    labels = {
        "best_case_opportunity_below_unavoidable_cost": "total opportunity <= cost",
        "spread_opportunity_below_actionable_threshold": "total net < $1 в spread-led precheck",
        "best_case_carry_below_unavoidable_cost": "total net <= 0 без полезного spread-edge",
        "funding_schedule_unavailable": "нет подтвержденного settlement",
        "unit_identity_mismatch": "несовместимые contract units",
        "non_positive_settlement_carry": "нет положительного funding или spread edge",
        "combined_opportunity_full_depth": "combined отправлен в full-depth",
        "spread_opportunity_full_depth": "spread отправлен в full-depth",
        "full_execution_required": "funding отправлен в full-depth",
        "pinned_revalidation": "повторная проверка watch-route",
    }
    key = str(value or "unknown")
    return labels.get(key, key.replace("_", " "))


def blocker_label(value: Any) -> str:
    labels = {
        "live_net_pnl_not_positive": "total opportunity net <= 0",
        "live_net_pnl_below_actionable_threshold": "total opportunity net < минимум",
        "insufficient_basis_history": "мало L2/basis history для wide spread",
        "basis_not_covered_by_live_funding": "basis stress не покрыт",
        "invalid_orderbook": "некорректный стакан",
        "unit_identity_mismatch": "несовместимые contract units",
        "funding_schedule_unavailable": "нет settlement",
        "stale_funding_nowcast": "устаревший nowcast",
    }
    key = str(value or "unknown")
    return labels.get(key, key.replace("_", " "))


def leg_display_rate(leg: dict[str, Any]) -> tuple[float | None, float | None]:
    published_rate = optional_float(leg.get("published_funding_rate"))
    published_interval = optional_float(leg.get("published_funding_interval_hours"))
    if published_rate is not None and published_interval is not None:
        return published_rate, published_interval
    return (
        optional_float(leg.get("funding_rate")),
        optional_float(leg.get("funding_interval_hours")),
    )


def fee_evidence_status_line(route: dict[str, Any]) -> str:
    evidence = route.get("evidence") or {}
    readiness = evidence.get("capability_check") or {}
    economics = readiness.get("economics") or {}
    modeled = economics.get("modeled_fee_rates") or {}
    parts: list[str] = []
    for side, label in (("long", "L"), ("short", "S")):
        fee = modeled.get(side) or {}
        status = fee.get("evidence_status") or {}
        if not isinstance(status, dict) or not status:
            continue
        stamp = (
            status.get("account_fee_observed_at")
            or status.get("fee_source_observed_at")
            or status.get("fee_schedule_reviewed_at")
            or status.get("observed_at")
        )
        parts.append(
            " ".join(
                item
                for item in (
                    f"{label}:{status.get('fee_evidence_kind') or 'UNKNOWN'}",
                    f"src={short_fee_source(status.get('source_identifier'))}",
                    f"ts={short_timestamp(stamp)}",
                    f"age={short_age(status.get('age_seconds'))}",
                    f"exp={short_timestamp(status.get('expires_at'))}",
                    f"verified={'Y' if status.get('verified') else 'N'}",
                    f"fallback={'Y' if status.get('fallback_required') else 'N'}",
                    f"reserve={'Y' if status.get('uncertainty_reserve_required') else 'N'}",
                )
                if item
            )
        )
    return f"Fees: {tg(' | '.join(parts))}\n" if parts else ""


def short_fee_source(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if len(text) <= 34:
        return text
    return f"{text[:16]}...{text[-15:]}"


def short_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    if text.endswith("+00:00"):
        text = f"{text[:-6]}Z"
    return text.replace("T", " ")


def short_age(value: Any) -> str:
    age = optional_float(value)
    if age is None:
        return "none"
    if abs(age) >= 86_400:
        return f"{age / 86_400:.1f}d"
    if abs(age) >= 3_600:
        return f"{age / 3_600:.1f}h"
    return f"{age:.0f}s"


def status_route_line(
    route: dict[str, Any],
    index: int,
    *,
    include_fee_evidence: bool = True,
) -> str:
    evidence = route.get("evidence") or {}
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    now = datetime.now(UTC)
    long_lead = lead_seconds(long_leg.get("next_funding_at"), now)
    short_lead = lead_seconds(short_leg.get("next_funding_at"), now)
    threshold = float(evidence.get("actionable_profit_threshold") or 0.0)
    long_display_rate, long_display_interval = leg_display_rate(long_leg)
    short_display_rate, short_display_interval = leg_display_rate(short_leg)
    long_interval = format_interval_hours(long_display_interval)
    short_interval = format_interval_hours(short_display_interval)
    long_hourly = optional_float(long_leg.get("hourly_funding_rate"))
    short_hourly = optional_float(short_leg.get("hourly_funding_rate"))
    long_hourly_label = f"{long_hourly * 100:.4f}%/h" if long_hourly is not None else ""
    short_hourly_label = f"{short_hourly * 100:.4f}%/h" if short_hourly is not None else ""
    long_rate_display = format_rate(long_display_rate)
    short_rate_display = format_rate(short_display_rate)
    strategy = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    )
    if not strategy:
        strategy = selected_route_strategy(
            route,
            ("synchronized_funding_capture", "funding_only", "spread_only", "combined"),
        )
    edge_name = opportunity_label(strategy)
    strategy_net = float(
        (strategy or {}).get("expected_net_pnl")
        if (strategy or {}).get("expected_net_pnl") is not None
        else evidence.get("current_nowcast_net")
        or 0.0
    )
    funding_component = optional_float((strategy or {}).get("funding_pnl_component"))
    spread_component = optional_float((strategy or {}).get("spread_pnl_component"))
    lightweight = (
        str((strategy or {}).get("selection_model") or "")
        == "lightweight_discovery_v1"
        or "lightweight_only_requires_focused_underwriting"
        in (route.get("risk_flags") or [])
    )
    stage = str(
        route.get("discovery_stage")
        or ((evidence.get("lightweight_discovery") or {}).get("stage"))
        or "qualified"
    )
    readiness = str(evidence.get("readiness_level") or route.get("readiness_level") or "").replace("_", " ")
    paper_mode = str(evidence.get("paper_mode") or route.get("paper_mode") or "").upper()
    funding_status = str(evidence.get("funding_cashflow_status") or "").upper()
    execution_status = str(evidence.get("execution_status") or "").upper()
    semantics_status = str(evidence.get("settlement_semantics_status") or "").upper()
    route_risks = list(dict.fromkeys(
        str(flag)
        for flag in [
            *(route.get("risk_flags") or []),
            *(evidence.get("risk_flags") or []),
            *(evidence.get("capability_rejections") or []),
        ]
        if flag
    ))
    hard_blockers = list(dict.fromkeys(
        str(blocker)
        for blocker in [
            *(evidence.get("hard_blockers") or []),
            *((evidence.get("capability_check") or {}).get("hard_blockers") or []),
        ]
        if blocker
    ))
    label_map = {
        "estimated_rate_used": "WATCH — ESTIMATED RATE",
        "rate_estimate_not_exact_next": "WATCH — ESTIMATED RATE",
        "exact_next_rate_unavailable": "WATCH — ESTIMATED RATE",
        "synthetic_fill": "SYNTHETIC FILL",
        "synthetic_fill_model_required": "SYNTHETIC FILL",
        "position_inclusion_rule_unverified": "UNVERIFIED INCLUSION",
        "position_inclusion_rule_missing": "UNVERIFIED INCLUSION",
        "fee_fallback_used": "FEE FALLBACK",
        "fee_unverified": "FEE FALLBACK",
        "collateral_cross_major_stable": "COLLATERAL RISK",
        "collateral_other_dollar_stable": "COLLATERAL RISK",
        "collateral_usdt0_risk": "COLLATERAL RISK",
    }
    badges: list[str] = []
    if paper_mode in {"EXPERIMENTAL_PAPER"}:
        badges.append("EXPERIMENTAL PAPER")
    elif paper_mode in {"EXPERIMENTAL_SIMULATION", "EXPERIMENTAL"}:
        badges.append("SIMULATION READY")
    elif paper_mode in {"VERIFIED_PAPER", "VERIFIED"}:
        badges.append("VERIFIED PAPER")
    if funding_status in {"ESTIMATED_ONLY", "UNRECONCILED", "PENDING"}:
        badges.append("UNRECONCILED" if funding_status != "ESTIMATED_ONLY" else "ESTIMATED RATE")
    for flag in route_risks:
        normalized_flag = flag.removeprefix("long_").removeprefix("short_")
        label = label_map.get(normalized_flag)
        if label:
            badges.append(label)
    badges = list(dict.fromkeys(badges))[:5]
    confidence = (
        f"Rate {float(evidence.get('rate_confidence') or 0.0):.2f} | "
        f"Exec {float(evidence.get('execution_confidence') or 0.0):.2f} | "
        f"Settle {float(evidence.get('settlement_confidence') or 0.0):.2f}"
        if evidence.get("rate_confidence") is not None
        else ""
    )
    risk_line = ""
    blocker_line = ""
    if hard_blockers:
        top_blockers = ", ".join(tg(flag) for flag in hard_blockers[:3])
        rest = len(hard_blockers) - 3
        blocker_line = (
            f"\nBlockers: {top_blockers}{f' +{rest}' if rest > 0 else ''}"
        )
    if route_risks:
        top_risks = ", ".join(tg(flag) for flag in route_risks[:3])
        rest = len(route_risks) - 3
        risk_line = f"\nRisks: {top_risks}{f' +{rest}' if rest > 0 else ''}"
    readiness_line = (
        f"Readiness: <code>{tg(readiness)}</code> | {tg(confidence)}\n"
        if readiness
        else ""
    )
    badges_line = f"{tg(' | '.join(badges))}\n" if badges else ""
    fee_line = fee_evidence_status_line(route) if include_fee_evidence else ""
    if lightweight:
        pnl_line = (
            "Preliminary funding before focused costs: "
            "raw / conservative net "
            f"<b>{format_signed_money(evidence.get('raw_expected_net') or strategy_net)}</b> / "
            f"<b>{format_signed_money(evidence.get('conservative_expected_net') or strategy_net)}</b>\n"
            "<i>Focused orderbooks, costs and entry observations: pending</i>"
        )
    else:
        pnl_line = (
            f"Net PnL after costs: <b>{format_signed_money(strategy_net)}</b> | "
            f"Min: ${threshold:.2f}"
        )
    return (
        f"\n<b>{index}. {tg(route.get('canonical_asset'))}</b>\n"
        f"Stage: <code>{tg(stage)}</code> | Edge: <code>{tg(edge_name)}</code>\n"
        f"{readiness_line}"
        f"{badges_line}"
        f"{fee_line}"
        f"LONG <code>{tg(route.get('long_venue'))} {tg(route.get('long_symbol'))}</code> "
        f"{long_rate_display}/{long_interval}"
        f"{f' ({long_hourly_label})' if long_hourly_label else ''}\n"
        f"SHORT <code>{tg(route.get('short_venue'))} {tg(route.get('short_symbol'))}</code> "
        f"{short_rate_display}/{short_interval}"
        f"{f' ({short_hourly_label})' if short_hourly_label else ''}\n"
        f"{pnl_line}\n"
        f"Funding {format_signed_money(funding_component)} | "
        f"Spread {format_signed_money(spread_component)}\n"
        f"Settlement: L {format_seconds(long_lead)} | S {format_seconds(short_lead)}"
        f"{f' | Semantics {tg(semantics_status)}' if semantics_status else ''}"
        f"{f' | {tg(execution_status)}' if execution_status else ''}"
        f"{blocker_line}"
        f"{risk_line}"
    )


def lead_seconds(value: Any, now: datetime) -> float | None:
    settlement = parse_iso(value)
    if settlement is None:
        return None
    return (settlement - now).total_seconds()


def strategy_label(value: Any) -> str:
    labels = {
        "funding_only": "Funding only",
        "spread_only": "Spread only",
        "combined": "Combined",
        "opportunistic_any": "Opportunistic any",
    }
    key = str(value or "funding_only").strip().lower()
    return labels.get(key, key or "Funding only")


def opportunity_label(strategy: dict[str, Any] | None) -> str:
    strategy = strategy or {}
    label = str(strategy.get("edge_label") or strategy_label(strategy.get("strategy_name")))
    quality = str(strategy.get("edge_quality") or "").strip().lower()
    if quality and quality != "clean":
        return f"{label} ({quality})"
    return label


def position_summary(position: dict[str, Any]) -> dict[str, Any]:
    strategy = (position.get("notes") or {}).get("strategy") or {}
    return {
        "funding_paper_position_id": position.get("funding_paper_position_id"),
        "entry_key": position.get("entry_key"),
        "route_key": position.get("route_key"),
        "canonical_asset": position.get("canonical_asset"),
        "long": f"{position.get('long_venue')} {position.get('long_symbol')}",
        "short": f"{position.get('short_venue')} {position.get('short_symbol')}",
        "expected_live_net": position.get("expected_live_net"),
        "strategy_name": strategy.get("strategy_name"),
        "max_settlement_at": position.get("max_settlement_at"),
    }


def funding_rate_lines(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    lines: list[str] = []
    for side in ("long", "short"):
        leg = leg_by_side(legs, side) or {}
        venue = leg.get("venue") or route.get(f"{side}_venue") or ""
        rate, interval = leg_display_rate(leg)
        hourly = optional_float(leg.get("hourly_funding_rate"))
        if rate is None:
            continue
        interval_label = format_interval_hours(interval) if interval else "?"
        hourly_label = f"{hourly * 100:.4f}%/h" if hourly is not None else "?"
        lines.append(
            f"{side.upper()} <code>{tg(venue)}</code>: "
            f"{format_rate(rate)}/{interval_label} ({hourly_label})"
        )
    return "\n".join(lines)


def armed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    leads = decision.get("lead_seconds") or {}
    rates = funding_rate_lines(route)
    strategy = decision.get("selected_strategy") or {}
    return (
        "<b>Paper Bot ARMED</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        f"{rates}\n"
        f"Live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Funding: {format_signed_money(strategy.get('funding_pnl_component'))} | "
        f"Spread: {format_signed_money(strategy.get('spread_pnl_component'))}\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}"
    )


def open_message(
    route: dict[str, Any],
    decision: dict[str, Any],
    position: dict[str, Any],
) -> str:
    leads = decision.get("lead_seconds") or {}
    rates = funding_rate_lines(route)
    strategy = decision.get("selected_strategy") or (position.get("notes") or {}).get("strategy") or {}
    return (
        "<b>Paper Bot OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        f"{rates}\n"
        f"Size: <b>${float(position.get('target_notional') or 0):.0f}</b> per leg\n"
        f"Expected live net: <b>${float(position.get('expected_live_net') or 0):.2f}</b>\n"
        f"Funding: {format_signed_money(strategy.get('funding_pnl_component'))} | "
        f"Spread: {format_signed_money(strategy.get('spread_pnl_component'))}\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}"
    )


def skipped_open_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    strategy = decision.get("selected_strategy") or {}
    return (
        "<b>Paper Bot SKIP OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        f"Fresh live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"Reasons: {tg(', '.join(decision.get('reasons') or []) or 'recheck failed')}"
    )


def disarmed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    strategy = decision.get("selected_strategy") or {}
    return (
        "<b>Paper Bot DISARMED</b>\n\n"
        f"<b>{tg(route.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(route.get('long_venue'))}</code> / "
        f"SHORT <code>{tg(route.get('short_venue'))}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        f"Fresh live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"Reasons: {tg(', '.join(decision.get('reasons') or []) or 'not hot')}"
    )


def pending_message(position: dict[str, Any], result: dict[str, Any]) -> str:
    return (
        "<b>Paper Bot SETTLEMENT PENDING</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        f"Причина: {tg(result.get('reason'))}\n"
        "Жду публикацию funding history."
    )


def hold_message(position: dict[str, Any], accrual: dict[str, Any]) -> str:
    settlement = accrual.get("settlement") or {}
    strategy = (position.get("notes") or {}).get("strategy") or (
        settlement.get("continued_strategy") or {}
    )
    quality = (
        "Начисление предварительное: одна или обе funding history еще не "
        "опубликованы, использована ставка на входе."
        if accrual.get("history_missing_fallback")
        else "Начисление финальное: использована опубликованная funding history."
    )
    hold_decision = accrual.get("hold_decision") or {}
    return (
        "<b>Paper Bot HOLD</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        "Funding settlement начислен, позиция остается открытой: "
        "арбитражное окно все еще положительное.\n"
        f"Settlement PnL: <b>{format_money(accrual.get('funding_pnl_delta'))}</b>\n"
        f"Current live net: {format_money(hold_decision.get('live_net'))}\n"
        f"Next settlement: {tg(accrual.get('next_max_settlement_at'))}\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_message(position: dict[str, Any], close: dict[str, Any]) -> str:
    settlement = close.get("settlement") or {}
    strategy = (position.get("notes") or {}).get("strategy") or {}
    quality = (
        "Качество PnL: предварительный расчет. Одна или обе биржи еще не "
        "опубликовали funding history, поэтому пока использованы ставки на входе. "
        "После публикации бот пересчитает PnL."
        if settlement.get("history_missing_fallback")
        else "Качество PnL: финальный расчет по опубликованной funding history."
    )
    window = close_window_message(close)
    details = close_decision_details_message(position, close)
    return (
        "<b>Paper Bot CLOSE</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        f"Edge: <code>{tg(opportunity_label(strategy))}</code>\n"
        f"Причина закрытия: {tg(close_reason_message(close.get('close_reason')))}\n"
        f"{tg(window)}\n"
        f"{details}\n\n"
        f"Funding PnL: <b>${float(close.get('actual_funding_pnl') or 0):.2f}</b>\n"
        f"Basis PnL: <b>${float(close.get('actual_basis_pnl') or 0):.2f}</b>\n"
        f"Execution cost: ${float(close.get('actual_execution_cost') or 0):.2f}\n"
        f"Net PnL: <b>${float(close.get('actual_net_pnl') or 0):.2f}</b>\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_decision_details_message(
    position: dict[str, Any],
    close: dict[str, Any],
) -> str:
    entry_legs = position.get("entry_legs") or []
    close_legs = close.get("close_legs") or []
    hold_decision = close.get("hold_decision") or {}
    close_evidence = close.get("close_evidence") or {}
    live_net = optional_float(hold_decision.get("live_net"))
    if live_net is None:
        live_net = optional_float(close_evidence.get("current_nowcast_net"))
    required = optional_float(close_evidence.get("actionable_profit_threshold"))
    route_age = optional_float(hold_decision.get("route_snapshot_age_seconds"))
    reasons = hold_reason_labels(hold_decision.get("reasons") or [])
    lines = ["", "<b>Детали решения</b>"]
    if live_net is not None:
        need_text = (
            f"; нужно >= {format_money(required)}"
            if required is not None
            else ""
        )
        lines.append(f"Fresh live net: <b>{format_signed_money(live_net)}</b>{need_text}.")
    if reasons:
        lines.append(f"Триггеры: {tg('; '.join(reasons))}.")
    if route_age is not None:
        lines.append(f"Возраст fresh snapshot: {route_age:.0f}s.")
    if entry_legs:
        lines.append(
            "На входе: "
            + " | ".join(
                funding_leg_compact_line(leg)
                for leg in sorted(entry_legs, key=leg_side_sort)
            )
            + "."
        )
    if close_legs:
        lines.append(
            "После settlement: "
            + " | ".join(
                funding_leg_compact_line(leg)
                for leg in sorted(close_legs, key=leg_side_sort)
            )
            + "."
        )
        mismatch = funding_settlement_mismatch_line(close_legs)
        if mismatch:
            lines.append(mismatch)
    return "\n".join(lines)


def leg_side_sort(leg: dict[str, Any]) -> int:
    return 0 if str(leg.get("side") or "").lower() == "long" else 1


def funding_leg_compact_line(leg: dict[str, Any]) -> str:
    side = str(leg.get("side") or "").upper()
    venue = leg.get("venue")
    symbol = leg.get("symbol")
    interval_rate, display_interval = leg_display_rate(leg)
    interval = format_interval_hours(display_interval)
    return (
        f"{tg(side)} <code>{tg(venue)} {tg(symbol)}</code> "
        f"{format_rate(interval_rate)}/{interval}, "
        f"next {tg(format_datetime_utc(leg.get('next_funding_at')))}"
    )


def funding_settlement_mismatch_line(legs: list[dict[str, Any]]) -> str:
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_next = parse_iso(long_leg.get("next_funding_at"))
    short_next = parse_iso(short_leg.get("next_funding_at"))
    long_interval = optional_float(long_leg.get("funding_interval_hours"))
    short_interval = optional_float(short_leg.get("funding_interval_hours"))
    notes: list[str] = []
    if long_next and short_next and abs((long_next - short_next).total_seconds()) > 60:
        notes.append(
            "следующие funding settlement не совпадают: "
            f"long {tg(format_datetime_utc(long_next.isoformat()))}, "
            f"short {tg(format_datetime_utc(short_next.isoformat()))}"
        )
    if (
        long_interval is not None
        and short_interval is not None
        and abs(long_interval - short_interval) > 1e-9
    ):
        notes.append(
            "интервалы funding разные: "
            f"long {format_interval_hours(long_interval)}, "
            f"short {format_interval_hours(short_interval)}"
        )
    if not notes:
        return ""
    return (
        "Важно: "
        + "; ".join(notes)
        + ". Следующее удержание уже считается новой проверкой окна, "
        "а не автоматическим продолжением старой сделки."
    )


def hold_reason_labels(reasons: list[Any]) -> list[str]:
    labels = {
        "live_net_not_positive": "fresh live net стал <= 0",
        "live_net_missing": "не удалось проверить fresh live net",
        "data_quality_issue": "свежие данные маршрута несовместимы",
        "latest_route_missing": "нет свежего route snapshot",
        "route_snapshot_stale": "fresh snapshot устарел",
        "route_snapshot_age_missing": "непонятен возраст fresh snapshot",
        "missing_route_legs": "не хватает одной из ног маршрута",
        "long_next_settlement_missing": "не найден следующий settlement long-ноги",
        "short_next_settlement_missing": "не найден следующий settlement short-ноги",
        "long_next_settlement_not_future": "следующий settlement long-ноги уже прошел",
        "short_next_settlement_not_future": "следующий settlement short-ноги уже прошел",
        "funding_rate_inverted": "funding rate инвертировался: short-нога стала дешевле long-ноги",
    }
    return [labels.get(str(reason), str(reason)) for reason in reasons]


def reprice_message(position: dict[str, Any], payload: dict[str, Any]) -> str:
    settlement = position.get("settlement") or {}
    quality = (
        "Качество PnL: частичный пересчет. Одна из бирж все еще не "
        "опубликовала funding history, поэтому оставшаяся нога рассчитана по "
        "ставке на входе."
        if payload.get("history_missing_fallback")
        else "Качество PnL: финальный пересчет по опубликованной funding history."
    )
    return (
        "<b>Paper Bot PnL REPRICED</b>\n\n"
        f"<b>{tg(position.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(position.get('long_venue'))}</code> / "
        f"SHORT <code>{tg(position.get('short_venue'))}</code>\n\n"
        "Биржи опубликовали funding history, бот пересчитал результат.\n"
        f"Funding PnL: <b>{format_signed_money(position.get('actual_funding_pnl'))}</b>\n"
        f"Basis PnL: <b>{format_signed_money(position.get('actual_basis_pnl'))}</b>\n"
        f"Execution cost: {format_money(position.get('actual_execution_cost'))}\n"
        f"Net PnL: <b>{format_signed_money(position.get('actual_net_pnl'))}</b>\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_reason_message(value: Any) -> str:
    raw = str(value or "")
    if raw.startswith("price_stop_loss"):
        return "цена ушла от входа сильнее stop-loss порога; обе ноги закрыты одновременно"
    if raw.startswith("spread_stop_loss"):
        return "basis/spread ушел против позиции сильнее stop-loss порога"
    return {
        "arbitrage_window_closed_live_net_non_positive": (
            "арбитражное окно закрылось, live net стал неположительным"
        ),
        "arbitrage_window_data_quality_issue": (
            "свежие данные маршрута выглядят несовместимыми"
        ),
        "arbitrage_window_unverifiable_route_missing": (
            "не удалось получить свежий route snapshot"
        ),
        "arbitrage_window_unverifiable_route_stale": (
            "route snapshot устарел, продолжение окна не подтверждено"
        ),
        "arbitrage_window_unverifiable_next_settlement_missing": (
            "не удалось определить следующий funding settlement"
        ),
        "arbitrage_window_unverifiable_live_net_missing": (
            "не удалось проверить live net"
        ),
        "arbitrage_window_funding_rate_inverted": (
            "funding rate инвертировался: арбитражное окно закрылось"
        ),
        "arbitrage_window_unverifiable": (
            "продолжение окна не подтверждено"
        ),
    }.get(raw, raw or "продолжение окна не подтверждено")


def close_window_message(close: dict[str, Any]) -> str:
    evidence = close.get("close_evidence") or {}
    live_net = optional_float(evidence.get("current_nowcast_net"))
    if live_net is None:
        if close.get("close_funding_route_id"):
            return "Состояние окна при закрытии: close-scan был, но live net не рассчитан."
        return "Состояние окна при закрытии: свежий route snapshot не найден."
    if live_net > 0:
        return (
            "Состояние окна при закрытии: окно еще выглядело положительным, "
            f"live net {format_money(live_net)}."
        )
    if live_net < 0:
        return (
            "Состояние окна при закрытии: окно уже не выглядело положительным, "
            f"live net {format_money(live_net)}."
        )
    return "Состояние окна при закрытии: live net около $0.00."
