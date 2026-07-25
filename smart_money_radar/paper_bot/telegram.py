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
    status_publishable_candidate,
)


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
    universe_count = int(result.get("universe_route_count") or 0)
    route_count = int(result.get("route_count") or 0)
    full_depth_count = int(result.get("execution_shortlist_count") or route_count)
    lines = [
        "<b>Paper Bot STATUS</b>",
        "",
        (
            f"<b>Mode:</b> <code>{tg(result.get('mode'))}</code> | "
            f"<b>Scan:</b> <code>{tg(result.get('funding_scan_id') or '-')}</code>"
        ),
        (
            f"<b>Candidates: {len(candidates)}</b> | "
            f"Watch/internal: {len(watch_routes)} | Monitor: {result.get('hot_route_count') or 0} | "
            f"Urgent: {result.get('urgent_route_count') or 0}"
        ),
        (
            f"<b>Open:</b> {int(summary.get('open_position_count') or 0)} | "
            f"Closed: {int(summary.get('closed_trade_count') or 0)} | "
            f"Realized PnL: <b>${float(summary.get('realized_pnl') or 0):.2f}</b>"
        ),
    ]
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
    if candidates:
        lines.extend(["", "<b>Candidates</b>"])
        lines.extend(
            status_route_line(route, index)
            for index, route in enumerate(candidates[:max_routes], start=1)
        )
        hidden = len(candidates) - max_routes
        if hidden > 0:
            lines.append(f"<i>...and {hidden} more</i>")
    else:
        lines.extend(["", "<b>Candidates:</b> нет"])
        if watched:
            lines.append(
                "<i>Watch routes are hidden from status: they are monitored "
                "internally but are not trade candidates.</i>"
            )
    return "\n".join(lines)


def status_route_line(route: dict[str, Any], index: int) -> str:
    evidence = route.get("evidence") or {}
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    now = datetime.now(UTC)
    long_lead = lead_seconds(long_leg.get("next_funding_at"), now)
    short_lead = lead_seconds(short_leg.get("next_funding_at"), now)
    live_net = float(evidence.get("current_nowcast_net") or 0.0)
    threshold = float(evidence.get("actionable_profit_threshold") or 0.0)
    long_interval = format_interval_hours(long_leg.get("funding_interval_hours"))
    short_interval = format_interval_hours(short_leg.get("funding_interval_hours"))
    long_hourly = optional_float(long_leg.get("hourly_funding_rate"))
    short_hourly = optional_float(short_leg.get("hourly_funding_rate"))
    long_hourly_label = f"{long_hourly * 100:.4f}%/h" if long_hourly is not None else ""
    short_hourly_label = f"{short_hourly * 100:.4f}%/h" if short_hourly is not None else ""
    long_interval_rate = optional_float(long_leg.get("funding_rate"))
    short_interval_rate = optional_float(short_leg.get("funding_rate"))
    long_iv = max(1.0, float(long_leg.get("funding_interval_hours") or 1.0))
    short_iv = max(1.0, float(short_leg.get("funding_interval_hours") or 1.0))
    long_rate_display = format_rate(long_interval_rate * long_iv if long_interval_rate is not None else None)
    short_rate_display = format_rate(short_interval_rate * short_iv if short_interval_rate is not None else None)
    return (
        f"\n<b>{index}. {tg(route.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(route.get('long_venue'))} {tg(route.get('long_symbol'))}</code> "
        f"{long_rate_display}/{long_interval}"
        f"{f' ({long_hourly_label})' if long_hourly_label else ''}\n"
        f"SHORT <code>{tg(route.get('short_venue'))} {tg(route.get('short_symbol'))}</code> "
        f"{short_rate_display}/{short_interval}"
        f"{f' ({short_hourly_label})' if short_hourly_label else ''}\n"
        f"Next net PnL after costs: <b>{format_signed_money(live_net)}</b> | "
        f"Min: ${threshold:.2f}\n"
        f"Settlement: L {format_seconds(long_lead)} | S {format_seconds(short_lead)}"
    )


def lead_seconds(value: Any, now: datetime) -> float | None:
    settlement = parse_iso(value)
    if settlement is None:
        return None
    return (settlement - now).total_seconds()


def position_summary(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "funding_paper_position_id": position.get("funding_paper_position_id"),
        "entry_key": position.get("entry_key"),
        "route_key": position.get("route_key"),
        "canonical_asset": position.get("canonical_asset"),
        "long": f"{position.get('long_venue')} {position.get('long_symbol')}",
        "short": f"{position.get('short_venue')} {position.get('short_symbol')}",
        "expected_live_net": position.get("expected_live_net"),
        "max_settlement_at": position.get("max_settlement_at"),
    }


def funding_rate_lines(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    lines: list[str] = []
    for side in ("long", "short"):
        leg = leg_by_side(legs, side) or {}
        venue = leg.get("venue") or route.get(f"{side}_venue") or ""
        rate = optional_float(leg.get("funding_rate"))
        interval = optional_float(leg.get("funding_interval_hours"))
        hourly = optional_float(leg.get("hourly_funding_rate"))
        if rate is None:
            continue
        interval_label = format_interval_hours(interval) if interval else "?"
        interval_rate = rate * max(1.0, interval or 1.0)
        hourly_label = f"{hourly * 100:.4f}%/h" if hourly is not None else "?"
        lines.append(
            f"{side.upper()} <code>{tg(venue)}</code>: "
            f"{format_rate(interval_rate)}/{interval_label} ({hourly_label})"
        )
    return "\n".join(lines)


def armed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    leads = decision.get("lead_seconds") or {}
    rates = funding_rate_lines(route)
    return (
        "<b>Paper Bot ARMED</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"{rates}\n"
        f"Live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
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
    return (
        "<b>Paper Bot OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"{rates}\n"
        f"Size: <b>${float(position.get('target_notional') or 0):.0f}</b> per leg\n"
        f"Expected live net: <b>${float(position.get('expected_live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}"
    )


def skipped_open_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "<b>Paper Bot SKIP OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"Fresh live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"Reasons: {tg(', '.join(decision.get('reasons') or []) or 'recheck failed')}"
    )


def disarmed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "<b>Paper Bot DISARMED</b>\n\n"
        f"<b>{tg(route.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(route.get('long_venue'))}</code> / "
        f"SHORT <code>{tg(route.get('short_venue'))}</code>\n\n"
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
    interval = format_interval_hours(leg.get("funding_interval_hours"))
    hourly = optional_float(leg.get("funding_rate"))
    iv = max(1.0, float(leg.get("funding_interval_hours") or 1.0))
    interval_rate = hourly * iv if hourly is not None else None
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
    }.get(str(value or ""), str(value or "продолжение окна не подтверждено"))


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
