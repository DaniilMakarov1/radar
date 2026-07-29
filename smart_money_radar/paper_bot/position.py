"""Position lifecycle: entry, hold, settlement, close, PnL, spread monitoring."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.paper_bot.helpers import (
    leg_by_side,
    optional_float,
    parse_iso,
    route_data_age_seconds,
    route_entry_key,
    route_settlement_leads,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    STRATEGY_NAME as SYNCHRONIZED_STRATEGY_NAME,
    STRATEGY_VERSION as SYNCHRONIZED_STRATEGY_VERSION,
    settlement_skew_seconds,
    synchronized_strategy_candidate,
)
from smart_money_radar.storage import SQLiteStore

STRATEGY_ALIASES = {
    "synchronized": SYNCHRONIZED_STRATEGY_NAME,
    "synchronized_funding": SYNCHRONIZED_STRATEGY_NAME,
    "synchronized_funding_capture": SYNCHRONIZED_STRATEGY_NAME,
    "funding": "funding_only",
    "funding_carry": "funding_only",
    "funnel_only": "funding_only",
    "carry": "funding_only",
    "spread": "spread_only",
    "spread_capture": "spread_only",
    "spread_monitor": "spread_only",
    "basis": "spread_only",
    "basis_capture": "spread_only",
    "combined_carry_spread": "combined",
    "combined_funding_spread": "combined",
    "any": "opportunistic_any",
    "any_edge": "opportunistic_any",
    "best_edge": "opportunistic_any",
    "mixed": "opportunistic_any",
    "opportunistic": "opportunistic_any",
    "opportunistic_total_edge": "opportunistic_any",
}
ALLOWED_STRATEGIES = (
    SYNCHRONIZED_STRATEGY_NAME,
    "funding_only",
    "spread_only",
    "combined",
    "opportunistic_any",
)


def normalize_strategy_name(raw: Any) -> str:
    key = str(raw or "").strip().lower().replace("-", "_")
    return STRATEGY_ALIASES.get(key, key)


def route_entry_decision(
    route: dict[str, Any],
    accounts: dict[str, dict[str, Any]],
    now: datetime,
    config: PaperBotConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    reasons: list[str] = []
    leads: dict[str, float | None] = {"long": None, "short": None}
    if route.get("status") != "paper_candidate":
        reasons.append("route_not_candidate")
    evidence = route.get("evidence") or {}
    selected_strategy = selected_route_strategy(route, config.strategy_set)
    if selected_strategy is None:
        reasons.append("no_allowed_strategy_candidate")
    live_net = strategy_expected_net(route, selected_strategy)
    if not long_leg or not short_leg:
        reasons.append("missing_route_legs")
    else:
        settlements: dict[str, datetime] = {}
        for side, leg in (("long", long_leg), ("short", short_leg)):
            settlement_at = parse_iso(leg.get("next_funding_at"))
            if settlement_at is None:
                reasons.append(f"{side}_settlement_missing")
                continue
            settlements[side] = settlement_at
            lead = (settlement_at - now).total_seconds()
            leads[side] = lead
            if lead < 0:
                reasons.append(f"{side}_settlement_already_passed")
            elif lead < config.entry_min_lead_seconds:
                reasons.append(f"{side}_settlement_inside_final_deadline")
            elif lead > config.entry_max_lead_seconds:
                reasons.append(f"{side}_settlement_outside_final_entry_window")
        if long_leg and short_leg:
            for leg in (long_leg, short_leg):
                venue = str(leg.get("venue") or "")
                notional = float(leg.get("notional") or route.get("target_notional") or 0.0)
                required = notional * (1.0 + config.collateral_reserve_fraction)
                available = float(
                    (accounts.get(venue) or {}).get("available_balance") or 0.0
                )
                if available < required:
                    reasons.append(f"{venue}_insufficient_paper_balance")
    entry_window_ready = all(
        lead is not None and 0 <= lead <= config.entry_max_lead_seconds
        for lead in leads.values()
    )
    snapshot_age = route_data_age_seconds(route, now)
    if entry_window_ready and (
        snapshot_age is None or snapshot_age > config.max_entry_snapshot_age_seconds
    ):
        reasons.append("entry_snapshot_stale")
    required_live_net = required_live_net_profit(route, config)
    if live_net <= 0:
        if "live_net_not_positive" not in reasons:
            reasons.append("live_net_not_positive")
    elif live_net < required_live_net:
        reasons.append("live_net_below_required_profit")
    armed = bool(
        not reasons
        and long_leg
        and short_leg
        and all(
            lead is not None and 0 <= lead <= config.arm_window_seconds
            for lead in leads.values()
        )
        and live_net >= required_live_net
    )
    return {
        "eligible": not reasons,
        "armed": armed,
        "reasons": reasons,
        "lead_seconds": leads,
        "entry_min_lead_seconds": config.entry_min_lead_seconds,
        "entry_max_lead_seconds": config.entry_max_lead_seconds,
        "arm_window_seconds": config.arm_window_seconds,
        "live_net": live_net,
        "required_live_net": required_live_net,
        "selected_strategy": selected_strategy,
        "strategy_name": (selected_strategy or {}).get("strategy_name"),
        "snapshot_age_seconds": snapshot_age,
        "max_entry_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
        "settlement_alignment_tolerance_seconds": (
            config.settlement_alignment_tolerance_seconds
        ),
    }


def required_live_net_profit(
    route: dict[str, Any],
    config: PaperBotConfig,
) -> float:
    evidence = route.get("evidence") or {}
    threshold = optional_float(evidence.get("actionable_profit_threshold"))
    selected = selected_route_strategy(route, config.strategy_set)
    if selected is not None:
        threshold = optional_float(selected.get("actionable_profit_threshold")) or threshold
    return max(float(config.min_live_net_profit), threshold or 0.0)


def route_monitor_decision(
    route: dict[str, Any],
    now: datetime,
    config: PaperBotConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    leads: dict[str, float | None] = {"long": None, "short": None}
    reasons: list[str] = []
    for side in ("long", "short"):
        leg = leg_by_side(legs, side)
        settlement = parse_iso((leg or {}).get("next_funding_at"))
        if settlement is None:
            reasons.append(f"{side}_settlement_missing")
            continue
        lead = (settlement - now).total_seconds()
        leads[side] = lead
        if lead < 0:
            reasons.append(f"{side}_settlement_already_passed")
        elif lead > config.arm_window_seconds:
            reasons.append(f"{side}_settlement_outside_arm_window")
    selected_strategy = selected_route_strategy(route, config.strategy_set)
    if selected_strategy is None:
        reasons.append("no_allowed_strategy_candidate")
    live_net = strategy_expected_net(route, selected_strategy)
    required_live_net = required_live_net_profit(route, config)
    if live_net <= 0:
        reasons.append("live_net_not_positive")
    elif live_net < required_live_net:
        reasons.append("live_net_below_required_profit")
    hot = bool(
        not reasons
        and route.get("status") in {"paper_candidate", "watch"}
        and all(
            lead is not None and 0 <= lead <= config.arm_window_seconds
            for lead in leads.values()
        )
    )
    urgent = bool(
        hot
        and all(
            lead is not None and 0 <= lead <= config.entry_max_lead_seconds
            for lead in leads.values()
        )
    )
    return {
        "hot": hot,
        "urgent": urgent,
        "reasons": reasons,
        "lead_seconds": leads,
        "live_net": live_net,
        "required_live_net": required_live_net,
        "selected_strategy": selected_strategy,
        "strategy_name": (selected_strategy or {}).get("strategy_name"),
    }


def route_discovery_stage(
    route: dict[str, Any],
    now: datetime,
    config: PaperBotConfig,
) -> dict[str, Any]:
    leads: dict[str, float | None] = {"long": None, "short": None}
    for side in ("long", "short"):
        leg = leg_by_side(route.get("legs") or [], side)
        settlement = parse_iso((leg or {}).get("next_funding_at"))
        if settlement is not None:
            leads[side] = (settlement - now).total_seconds()
    present_leads = [lead for lead in leads.values() if lead is not None]
    if len(present_leads) != 2:
        stage = "unavailable"
    elif any(lead < 0 for lead in present_leads):
        stage = "expired"
    elif status_publishable_candidate(route, config):
        stage = "qualified"
    elif all(lead <= config.entry_max_lead_seconds for lead in present_leads):
        stage = "urgent"
    elif all(lead <= config.arm_window_seconds for lead in present_leads):
        stage = "monitor"
    elif all(
        lead <= config.lightweight_watch_window_seconds
        for lead in present_leads
    ):
        stage = "watch"
    elif all(
        lead <= config.lightweight_route_horizon_seconds
        for lead in present_leads
    ):
        stage = "early"
    else:
        stage = "outside_horizon"
    return {
        "stage": stage,
        "lead_seconds": leads,
        "nearest_lead_seconds": min(present_leads, default=None),
        "furthest_lead_seconds": max(present_leads, default=None),
    }


def build_position_from_route(
    route: dict[str, Any],
    decision: dict[str, Any],
    config: PaperBotConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    evidence = route.get("evidence") or {}
    selected_strategy = selected_route_strategy(route, config.strategy_set) or {}
    strategy_name = str(
        selected_strategy.get("strategy_name") or SYNCHRONIZED_STRATEGY_NAME
    )
    strategy_components = {
        "strategy_name": strategy_name,
        "strategy_version": selected_strategy.get("strategy_version"),
        "primary_edge": selected_strategy.get("primary_edge"),
        "selection_model": selected_strategy.get("selection_model"),
        "edge_type": selected_strategy.get("edge_type"),
        "edge_label": selected_strategy.get("edge_label"),
        "edge_quality": selected_strategy.get("edge_quality"),
        "thesis": selected_strategy.get("thesis"),
        "funding_pnl_component": selected_strategy.get("funding_pnl_component"),
        "spread_pnl_component": selected_strategy.get("spread_pnl_component"),
        "signed_spread_pnl_component": selected_strategy.get(
            "signed_spread_pnl_component"
        ),
        "expected_spread_convergence_pnl": selected_strategy.get(
            "expected_spread_convergence_pnl"
        ),
        "expected_net_pnl": selected_strategy.get("expected_net_pnl"),
        "basis_stress_net_pnl": selected_strategy.get("basis_stress_net_pnl"),
        "risk_adjusted_net_pnl": selected_strategy.get(
            "risk_adjusted_net_pnl"
        ),
        "coverage_ratio": selected_strategy.get("coverage_ratio"),
        "warnings": selected_strategy.get("warnings") or [],
    }
    long_notional = float(long_leg.get("notional") or route.get("target_notional") or 0.0)
    short_notional = float(short_leg.get("notional") or route.get("target_notional") or 0.0)
    long_settlement = str(long_leg.get("next_funding_at") or "")
    short_settlement = str(short_leg.get("next_funding_at") or "")
    max_settlement = max(
        parse_iso(long_settlement) or datetime.min.replace(tzinfo=UTC),
        parse_iso(short_settlement) or datetime.min.replace(tzinfo=UTC),
    ).isoformat()
    return {
        "entry_key": route_entry_key(route),
        "route_key": route["route_key"],
        "open_funding_scan_id": route.get("funding_scan_id"),
        "open_funding_route_id": route.get("funding_route_id"),
        "canonical_asset": route["canonical_asset"],
        "long_venue": route["long_venue"],
        "long_symbol": route["long_symbol"],
        "short_venue": route["short_venue"],
        "short_symbol": route["short_symbol"],
        "base_quantity": min(
            float(long_leg.get("base_quantity") or 0.0),
            float(short_leg.get("base_quantity") or 0.0),
        ),
        "target_notional": float(route.get("target_notional") or 0.0),
        "long_notional": long_notional,
        "short_notional": short_notional,
        "long_reserved_margin": long_notional * (1.0 + config.collateral_reserve_fraction),
        "short_reserved_margin": short_notional * (1.0 + config.collateral_reserve_fraction),
        "long_settlement_at": long_settlement,
        "short_settlement_at": short_settlement,
        "max_settlement_at": max_settlement,
        "expected_live_gross": float(selected_strategy.get("gross_edge_pnl") or evidence.get("current_nowcast_gross") or 0.0),
        "expected_live_net": float(selected_strategy.get("expected_net_pnl") or evidence.get("current_nowcast_net") or 0.0),
        "expected_execution_cost": float(evidence.get("execution_cost") or 0.0),
        "entry_legs": legs,
        "entry_evidence": evidence,
        "entry_cross_spread": entry_cross_spread(long_leg, short_leg),
        "entry_basis_bps": float(evidence.get("signed_entry_basis") or 0.0) * 10_000.0,
        "notes": {
            "decision": decision,
            "paper_model": SYNCHRONIZED_STRATEGY_VERSION,
            "strategy": strategy_components,
            "strategy_name": strategy_name,
        },
    }


def normalize_strategy_set(strategies: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    raw_values = tuple(strategies or ())
    if not raw_values:
        raw_values = (SYNCHRONIZED_STRATEGY_NAME,)
    normalized: list[str] = []
    for raw in raw_values:
        key = normalize_strategy_name(raw)
        if key in ALLOWED_STRATEGIES and key not in normalized:
            normalized.append(key)
    if not normalized:
        allowed = ", ".join(ALLOWED_STRATEGIES)
        raise ValueError(
            f"No valid funding paper strategies in {raw_values!r}. Allowed: {allowed}"
        )
    return tuple(normalized)


def selected_route_strategy(
    route: dict[str, Any],
    allowed_strategies: tuple[str, ...] | list[str] | None,
) -> dict[str, Any] | None:
    allowed = set(normalize_strategy_set(allowed_strategies))
    evidence = route.get("evidence") or {}
    selected = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    )
    if (
        selected
        and str((selected or {}).get("selection_model") or "")
        == "opportunity_engine_v1"
    ):
        selected_name = normalize_strategy_name(
            (selected or {}).get("strategy_name")
            or (selected or {}).get("strategy_class")
        )
        if selected_name in allowed:
            candidate = dict(selected)
            expected_net = optional_float(candidate.get("expected_net_pnl"))
            candidate_eligible = (
                bool(candidate["eligible"])
                if "eligible" in candidate
                else route.get("status") == "paper_candidate"
                and expected_net is not None
                and expected_net > 0
            )
            if candidate_eligible:
                candidate["strategy_name"] = selected_name
                candidate["strategy_class"] = selected_name
                candidate["eligible"] = True
                return candidate
    candidates = []
    for row in evidence.get("strategy_candidates") or []:
        name = normalize_strategy_name(
            row.get("strategy_name") or row.get("strategy_class")
        )
        if name not in allowed or not bool(row.get("eligible")):
            continue
        candidate = dict(row)
        candidate["strategy_name"] = name
        candidate["strategy_class"] = name
        candidates.append(candidate)
    if not candidates:
        selected_name = normalize_strategy_name(
            (selected or {}).get("strategy_name")
            or (selected or {}).get("strategy_class")
        )
        if selected and selected_name in allowed:
            candidate = dict(selected)
            expected_net = optional_float(candidate.get("expected_net_pnl"))
            candidate_eligible = (
                bool(candidate["eligible"])
                if "eligible" in candidate
                else route.get("status") == "paper_candidate"
                and expected_net is not None
                and expected_net > 0
            )
            if candidate_eligible:
                candidate["strategy_name"] = selected_name
                candidate["strategy_class"] = selected_name
                candidate["eligible"] = True
                candidates.append(candidate)
    if not candidates:
        if SYNCHRONIZED_STRATEGY_NAME in allowed:
            synchronized = synchronized_strategy_from_route(route)
            if synchronized is not None:
                candidates.append(synchronized)
    if not candidates:
        legacy_net = optional_float(evidence.get("current_nowcast_net"))
        if (
            legacy_net is not None
            and legacy_net > 0
            and route.get("status") == "paper_candidate"
            and "funding_only" in allowed
        ):
            threshold = optional_float(evidence.get("actionable_profit_threshold")) or 0.0
            return {
                "strategy_name": "funding_only",
                "strategy_class": "funding_only",
                "primary_edge": "funding_carry",
                "eligible": True,
                "expected_net_pnl": legacy_net,
                "gross_edge_pnl": evidence.get("current_nowcast_gross"),
                "funding_pnl_component": evidence.get("current_nowcast_gross"),
                "spread_pnl_component": 0.0,
                "execution_cost": evidence.get("execution_cost"),
                "actionable_profit_threshold": threshold,
                "reasons": [],
                "legacy_strategy_fallback": True,
            }
        return None
    strict_candidates = [
        row for row in candidates if row.get("strategy_name") != "opportunistic_any"
    ]
    if strict_candidates:
        candidates = strict_candidates
    priority = {
        SYNCHRONIZED_STRATEGY_NAME: 5,
        "combined": 4,
        "spread_only": 3,
        "funding_only": 2,
        "opportunistic_any": 1,
    }
    selected = max(
        candidates,
        key=lambda row: (
            float(row.get("expected_net_pnl") or 0.0),
            priority.get(str(row.get("strategy_name") or row.get("strategy_class")), 0),
        ),
    )
    selected.setdefault("strategy_name", selected.get("strategy_class") or "funding_only")
    selected.setdefault("strategy_class", selected.get("strategy_name"))
    return selected


def synchronized_strategy_from_route(route: dict[str, Any]) -> dict[str, Any] | None:
    evidence = route.get("evidence") or {}
    selected = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    ) or {}
    funding_component = optional_float(selected.get("funding_pnl_component"))
    if funding_component is None:
        funding_component = optional_float(evidence.get("current_nowcast_gross"))
    if funding_component is None:
        return None
    row = {
        "funding_notional": evidence.get("funding_notional")
        or route.get("target_notional")
        or route.get("long_notional")
        or 0.0,
        "execution_cost": evidence.get("execution_cost")
        or selected.get("execution_cost")
        or route.get("total_fees")
        or 0.0,
        "basis_stress_loss": evidence.get("basis_stress_loss")
        or selected.get("basis_stress_loss")
        or 0.0,
    }
    threshold = (
        optional_float(selected.get("actionable_profit_threshold"))
        or optional_float(evidence.get("actionable_profit_threshold"))
        or 0.0
    )
    candidate = synchronized_strategy_candidate(
        row,
        current_funding_gross=funding_component,
        actionable_profit_threshold=threshold,
        blocking_risk_flags=list(evidence.get("blocking_risk_flags") or []),
        decision_mode=str(evidence.get("decision_mode") or "settlement_capture"),
    )
    if route.get("status") != "paper_candidate":
        candidate["eligible"] = False
        candidate["reasons"] = list(candidate.get("reasons") or []) + [
            "route_not_paper_candidate",
        ]
    return candidate if candidate.get("eligible") else None


def strategy_expected_net(
    route: dict[str, Any],
    selected_strategy: dict[str, Any] | None,
) -> float:
    if selected_strategy is not None:
        value = optional_float(selected_strategy.get("expected_net_pnl"))
        if value is not None:
            return value
    evidence = route.get("evidence") or {}
    return float(evidence.get("current_nowcast_net") or 0.0)


def close_decision(
    position: dict[str, Any],
    now: datetime,
    store: SQLiteStore,
    config: PaperBotConfig,
) -> dict[str, Any]:
    max_settlement = parse_iso(position.get("max_settlement_at"))
    if max_settlement is None:
        opened = parse_iso(position.get("opened_at"))
        lag = config.max_settlement_publication_lag_seconds
        if opened is not None and (now - opened).total_seconds() > max(60, lag):
            return {
                "status": "close",
                "close": build_close_payload(
                    position, {}, None,
                    use_entry_estimate_for_missing=True,
                    close_reason="settlement_publication_timeout",
                    hold_decision={"hold": False, "close_reason": "settlement_publication_timeout", "reasons": ["max_settlement_at_missing_timeout"]},
                ),
            }
        return {"status": "settlement_pending", "reason": "missing_max_settlement_at"}
    close_route = store.latest_funding_route_by_key(str(position["route_key"]))
    hold = position_hold_decision(position, close_route, now, config)

    notes = position.get("notes") or {}
    accrued_count = int(notes.get("accrued_settlement_count") or 0)
    opened_at = parse_iso(position.get("opened_at"))

    if accrued_count >= config.max_settlements_per_position:
        close = build_close_payload(
            position,
            {"long": None, "short": None},
            close_route,
            use_entry_estimate_for_missing=False,
            close_reason="max_settlements_reached",
            hold_decision={
                "hold": False,
                "close_reason": "max_settlements_reached",
                "current_settlement_funding_included": False,
                "reasons": [
                    f"accrued_settlements={accrued_count}",
                    f"max_settlements={config.max_settlements_per_position}",
                ],
            },
            include_current_settlement_funding=False,
        )
        return {"status": "close", "close": close}

    if opened_at is not None:
        position_age = (now - opened_at).total_seconds()
        if position_age >= config.max_position_age_seconds:
            include_current = now >= max_settlement + timedelta(
                seconds=config.settlement_grace_seconds
            )
            settlement = (
                settlement_rates_for_position(position, store)
                if include_current
                else {"long": None, "short": None}
            )
            missing = [side for side, row in settlement.items() if row is None]
            close = build_close_payload(
                position,
                settlement,
                close_route,
                use_entry_estimate_for_missing=bool(missing) if include_current else False,
                close_reason="max_position_age_reached",
                hold_decision={
                    "hold": False,
                    "close_reason": "max_position_age_reached",
                    "current_settlement_funding_included": include_current,
                    "reasons": [
                        f"position_age_seconds={position_age:.0f}",
                        f"max_position_age_seconds={config.max_position_age_seconds}",
                    ],
                },
                include_current_settlement_funding=include_current,
            )
            return {"status": "close", "close": close}

    if now < max_settlement + timedelta(seconds=config.settlement_grace_seconds):
        pre_settlement_exit_reasons = {
            "live_net_not_positive",
            "funding_rate_inverted",
            "no_allowed_strategy_candidate",
        }
        unverifiable_reasons = {
            "route_snapshot_stale",
            "route_snapshot_age_missing",
            "latest_route_missing",
            "missing_route_legs",
        }
        if (
            not hold["hold"]
            and pre_settlement_exit_reasons.intersection(hold["reasons"])
            and not unverifiable_reasons.intersection(hold["reasons"])
        ):
            close = build_close_payload(
                position,
                {"long": None, "short": None},
                close_route,
                use_entry_estimate_for_missing=False,
                close_reason=str(hold["close_reason"]),
                hold_decision={
                    **hold,
                    "pre_settlement_close": True,
                    "current_settlement_funding_included": False,
                },
                include_current_settlement_funding=False,
            )
            return {"status": "close", "close": close}
        return {"status": "wait", "reason": "settlement_not_reached"}

    settlement = settlement_rates_for_position(position, store)
    missing = [side for side, row in settlement.items() if row is None]

    # --- T+20 normal exit gate ---
    # Normal close forbidden until T+20 seconds after settlement.
    # Hard-risk exits are always allowed.
    hard_risk_close_reasons = {
        "risk_hard_exit",
        "risk_data_hard_stale",
        "basis_deterioration",
        "liquidation_distance",
        "margin_safety",
        "mark_index_divergence",
        "quantity_mismatch",
        "emergency_unwind",
    }
    t20_deadline = max_settlement + timedelta(
        seconds=config.no_normal_exit_before_settlement_plus_seconds
    )
    is_hard_risk = (
        not hold["hold"]
        and str(hold.get("close_reason") or "") in hard_risk_close_reasons
    )
    if now < t20_deadline and not hold["hold"] and not is_hard_risk:
        # Normal close before T+20: suppress, force hold
        accrual = build_settlement_accrual_payload(
            position,
            settlement,
            close_route or {},
            use_entry_estimate_for_missing=bool(missing),
            hold_decision={
                **hold,
                "hold": True,
                "close_reason": "",
                "t20_suppressed": True,
                "reasons": list(hold.get("reasons") or [])
                + ["normal_exit_before_t20_suppressed"],
            },
        )
        return {"status": "hold", "accrual": accrual}

    if hold["hold"]:
        accrual = build_settlement_accrual_payload(
            position,
            settlement,
            close_route or {},
            use_entry_estimate_for_missing=bool(missing),
            hold_decision=hold,
        )
        return {"status": "hold", "accrual": accrual}
    close = build_close_payload(
        position,
        settlement,
        close_route,
        use_entry_estimate_for_missing=bool(missing),
        close_reason=str(hold["close_reason"]),
        hold_decision=hold,
    )
    return {"status": "close", "close": close}


def position_hold_decision(
    position: dict[str, Any],
    route: dict[str, Any] | None,
    now: datetime,
    config: PaperBotConfig,
) -> dict[str, Any]:
    if not route:
        return {
            "hold": False,
            "close_reason": "arbitrage_window_unverifiable_route_missing",
            "reasons": ["latest_route_missing"],
        }
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    reasons: list[str] = []
    route_age = route_data_age_seconds(route, now)
    if route_age is None:
        reasons.append("route_snapshot_age_missing")
    elif route_age > config.max_entry_snapshot_age_seconds:
        reasons.append("route_snapshot_stale")
    if not long_leg or not short_leg:
        reasons.append("missing_route_legs")
    data_quality_flags = {
        "unit_identity_mismatch",
        "basis_divergence",
    }
    route_flags = set(str(flag) for flag in route.get("risk_flags") or [])
    evidence = route.get("evidence") or {}
    route_flags.update(
        str(flag) for flag in evidence.get("blocking_risk_flags") or []
    )
    if route_flags.intersection(data_quality_flags):
        reasons.append("data_quality_issue")
    next_settlements: dict[str, str] = {}
    for side, leg in (("long", long_leg), ("short", short_leg)):
        if not leg:
            continue
        settlement = parse_iso(leg.get("next_funding_at"))
        if settlement is None:
            reasons.append(f"{side}_next_settlement_missing")
            continue
        if settlement <= now:
            reasons.append(f"{side}_next_settlement_not_future")
            continue
        next_settlements[side] = settlement.isoformat()
    selected_strategy = selected_route_strategy(route, config.strategy_set)
    if selected_strategy is None:
        reasons.append("no_allowed_strategy_candidate")
    live_net = strategy_expected_net(route, selected_strategy)
    if optional_float(live_net) is None:
        reasons.append("live_net_missing")
    elif live_net <= max(0.0, float(config.min_live_net_profit)):
        reasons.append("live_net_not_positive")
    if long_leg and short_leg:
        long_hourly = optional_float(long_leg.get("hourly_funding_rate"))
        short_hourly = optional_float(short_leg.get("hourly_funding_rate"))
        strategy_name = (selected_strategy or {}).get("strategy_name")
        edge_type = (selected_strategy or {}).get("edge_type")
        funding_component = optional_float(
            (selected_strategy or {}).get("funding_pnl_component")
        )
        funding_sensitive = edge_type in {"funding_led", "mixed_edge"} or (
            strategy_name in {"funding_only", "combined"}
        ) or (
            strategy_name == "opportunistic_any"
            and (funding_component or 0.0) > 0.0
        )
        if (
            long_hourly is not None
            and short_hourly is not None
            and short_hourly < long_hourly
            and funding_sensitive
        ):
            reasons.append("funding_rate_inverted")
        # funding_interval_hours equality is NOT a schedule gate.
        # Only exact next-settlement timestamp alignment (<=1s) matters.
    close_reason = close_reason_from_hold_reasons(reasons)
    return {
        "hold": not reasons,
        "close_reason": close_reason,
        "reasons": reasons,
        "live_net": live_net,
        "selected_strategy": selected_strategy,
        "strategy_name": (selected_strategy or {}).get("strategy_name"),
        "route_snapshot_age_seconds": route_age,
        "max_route_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
        "next_settlements": next_settlements,
        "route_status": route.get("status"),
    }


def close_reason_from_hold_reasons(reasons: list[str]) -> str:
    if "live_net_not_positive" in reasons:
        return "arbitrage_window_closed_live_net_non_positive"
    if "funding_interval_mismatch" in reasons:
        return "funding_interval_mismatch_close"
    if "data_quality_issue" in reasons:
        return "arbitrage_window_data_quality_issue"
    if (
        "route_snapshot_stale" in reasons
        or "route_snapshot_age_missing" in reasons
    ):
        return "arbitrage_window_unverifiable_route_stale"
    if "latest_route_missing" in reasons:
        return "arbitrage_window_unverifiable_route_missing"
    if "no_allowed_strategy_candidate" in reasons:
        return "arbitrage_window_closed_no_allowed_strategy"
    if any("settlement" in reason for reason in reasons):
        return "arbitrage_window_unverifiable_next_settlement_missing"
    if "live_net_missing" in reasons:
        return "arbitrage_window_unverifiable_live_net_missing"
    if "funding_rate_inverted" in reasons:
        return "arbitrage_window_funding_rate_inverted"
    return "arbitrage_window_unverifiable"


def build_settlement_accrual_payload(
    position: dict[str, Any],
    settlement: dict[str, dict[str, Any] | None],
    continuation_route: dict[str, Any],
    *,
    use_entry_estimate_for_missing: bool,
    hold_decision: dict[str, Any],
) -> dict[str, Any]:
    current_long = current_position_leg(position, "long")
    current_short = current_position_leg(position, "short")
    long_rate = settlement_rate_or_entry(settlement.get("long"), current_long)
    short_rate = settlement_rate_or_entry(settlement.get("short"), current_short)
    long_notional = float(position.get("long_notional") or 0.0)
    short_notional = float(position.get("short_notional") or 0.0)
    long_funding_pnl = funding_leg_pnl("long", long_notional, long_rate)
    short_funding_pnl = funding_leg_pnl("short", short_notional, short_rate)
    next_legs = continuation_route.get("legs") or []
    next_long = leg_by_side(next_legs, "long") or {}
    next_short = leg_by_side(next_legs, "short") or {}
    next_long_settlement = str(next_long.get("next_funding_at") or "")
    next_short_settlement = str(next_short.get("next_funding_at") or "")
    next_max_settlement = max(
        parse_iso(next_long_settlement) or datetime.min.replace(tzinfo=UTC),
        parse_iso(next_short_settlement) or datetime.min.replace(tzinfo=UTC),
    ).isoformat()
    funding_pnl_delta = long_funding_pnl + short_funding_pnl
    evidence = continuation_route.get("evidence") or {}
    selected_strategy = (
        evidence.get("selected_strategy")
        or evidence.get("strategy_classification")
        or {}
    )
    next_expected_live_gross = selected_strategy.get("gross_edge_pnl")
    if next_expected_live_gross is None:
        next_expected_live_gross = evidence.get("current_nowcast_gross")
    next_expected_live_net = selected_strategy.get("expected_net_pnl")
    if next_expected_live_net is None:
        next_expected_live_net = evidence.get("current_nowcast_net")
    settlement_payload_value = {
        "long": settlement_payload(settlement.get("long"), current_long, long_rate),
        "short": settlement_payload(settlement.get("short"), current_short, short_rate),
        "funding_pnl_delta": funding_pnl_delta,
        "history_missing_fallback": use_entry_estimate_for_missing,
        "continued_live_net": next_expected_live_net,
        "continued_strategy": selected_strategy,
    }
    return {
        "settlement_key": ":".join(
            [
                str(position.get("funding_paper_position_id") or ""),
                str(position.get("max_settlement_at") or ""),
            ]
        ),
        "long_cash_delta": long_funding_pnl,
        "short_cash_delta": short_funding_pnl,
        "funding_pnl_delta": funding_pnl_delta,
        "history_missing_fallback": use_entry_estimate_for_missing,
        "settlement": settlement_payload_value,
        "hold_decision": hold_decision,
        "next_funding_scan_id": continuation_route.get("funding_scan_id"),
        "next_funding_route_id": continuation_route.get("funding_route_id"),
        "next_long_settlement_at": next_long_settlement,
        "next_short_settlement_at": next_short_settlement,
        "next_max_settlement_at": next_max_settlement,
        "next_entry_legs": next_legs,
        "next_entry_evidence": evidence,
        "next_expected_live_gross": next_expected_live_gross,
        "next_expected_live_net": next_expected_live_net,
    }


def settlement_rates_for_position(
    position: dict[str, Any],
    store: SQLiteStore,
) -> dict[str, dict[str, Any] | None]:
    return {
        "long": store.funding_history_rate_near(
            str(position["long_venue"]),
            str(position["long_symbol"]),
            str(position.get("long_settlement_at") or ""),
        ),
        "short": store.funding_history_rate_near(
            str(position["short_venue"]),
            str(position["short_symbol"]),
            str(position.get("short_settlement_at") or ""),
        ),
    }


def build_close_payload(
    position: dict[str, Any],
    settlement: dict[str, dict[str, Any] | None],
    close_route: dict[str, Any] | None,
    *,
    use_entry_estimate_for_missing: bool,
    close_reason: str,
    hold_decision: dict[str, Any] | None = None,
    include_current_settlement_funding: bool = True,
    v2_no_phantom_funding: bool = False,
) -> dict[str, Any]:
    entry_long = current_position_leg(position, "long")
    entry_short = current_position_leg(position, "short")
    if include_current_settlement_funding:
        if v2_no_phantom_funding:
            long_rate = (
                float(settlement.get("long", {}).get("funding_rate") or 0.0)
                if settlement.get("long") is not None
                else None
            )
            short_rate = (
                float(settlement.get("short", {}).get("funding_rate") or 0.0)
                if settlement.get("short") is not None
                else None
            )
        else:
            long_rate = settlement_rate_or_entry(settlement.get("long"), entry_long)
            short_rate = settlement_rate_or_entry(settlement.get("short"), entry_short)
        settlement_source = None
    else:
        long_rate = 0.0
        short_rate = 0.0
        settlement_source = "pre_settlement_no_funding"
    long_notional = float(position.get("long_notional") or 0.0)
    short_notional = float(position.get("short_notional") or 0.0)
    v2_missing_long = long_rate is None
    v2_missing_short = short_rate is None
    long_funding_pnl = (
        funding_leg_pnl("long", long_notional, long_rate)
        if long_rate is not None
        else None
    )
    short_funding_pnl = (
        funding_leg_pnl("short", short_notional, short_rate)
        if short_rate is not None
        else None
    )
    accrued_funding_pnl = float(
        (position.get("notes") or {}).get("accrued_funding_pnl") or 0.0
    )
    if long_funding_pnl is not None and short_funding_pnl is not None:
        current_funding_pnl = long_funding_pnl + short_funding_pnl
    else:
        current_funding_pnl = None
    if current_funding_pnl is not None:
        actual_funding_pnl = accrued_funding_pnl + current_funding_pnl
    else:
        actual_funding_pnl = accrued_funding_pnl if accrued_funding_pnl else None
    actual_execution_cost = float(position.get("expected_execution_cost") or 0.0)
    spread_snap = compute_spread_snapshot(position, close_route)
    basis_pnl = float(spread_snap.get("unrealized_basis_pnl") or 0.0)
    if actual_funding_pnl is not None:
        actual_net_pnl = actual_funding_pnl + basis_pnl - actual_execution_cost
    else:
        actual_net_pnl = None
    long_basis_pnl = float(spread_snap.get("long_basis_pnl") or 0.0)
    short_basis_pnl = float(spread_snap.get("short_basis_pnl") or 0.0)
    long_cash_delta = (
        (long_funding_pnl or 0.0) + long_basis_pnl - actual_execution_cost / 2.0
    )
    short_cash_delta = (
        (short_funding_pnl or 0.0) + short_basis_pnl - actual_execution_cost / 2.0
    )
    close_evidence = (close_route or {}).get("evidence") or {}
    strategy = dict((position.get("notes") or {}).get("strategy") or {})
    strategy_name = str(strategy.get("strategy_name") or position.get("strategy_name") or "funding_only")
    return {
        "close_funding_scan_id": (close_route or {}).get("funding_scan_id"),
        "close_funding_route_id": (close_route or {}).get("funding_route_id"),
        "actual_funding_pnl": actual_funding_pnl,
        "actual_basis_pnl": basis_pnl,
        "actual_execution_cost": actual_execution_cost,
        "actual_net_pnl": actual_net_pnl,
        "strategy_name": strategy_name,
        "pnl_decomposition": {
            "funding_pnl": actual_funding_pnl,
            "basis_pnl": basis_pnl,
            "execution_cost": actual_execution_cost,
            "net_pnl": actual_net_pnl,
        },
        "long_cash_delta": long_cash_delta,
        "short_cash_delta": short_cash_delta,
        "close_reason": close_reason,
        "hold_decision": hold_decision or {},
        "close_legs": (close_route or {}).get("legs") or [],
        "close_evidence": close_evidence,
        "spread_snapshot": spread_snap,
        "settlement": {
            "long": settlement_payload(
                settlement.get("long"),
                entry_long,
                long_rate if long_rate is not None else 0.0,
                source=(
                    settlement_source
                    or ("v2_missing_public_history" if v2_missing_long else None)
                ),
            ),
            "short": settlement_payload(
                settlement.get("short"),
                entry_short,
                short_rate if short_rate is not None else 0.0,
                source=(
                    settlement_source
                    or ("v2_missing_public_history" if v2_missing_short else None)
                ),
            ),
            "current_funding_pnl": current_funding_pnl,
            "accrued_funding_pnl": accrued_funding_pnl,
            "entry_expected_live_net": position.get("expected_live_net"),
            "entry_expected_live_gross": position.get("expected_live_gross"),
            "history_missing_fallback": (
                False if v2_no_phantom_funding else use_entry_estimate_for_missing
            ),
            "v2_no_phantom_funding": v2_no_phantom_funding,
            "funding_pnl_null": current_funding_pnl is None,
            "current_settlement_funding_included": include_current_settlement_funding,
        },
        "notes": {
            "paper_model": "funding_paper_trader_v2",
            "execution_cost_source": "entry_route_expected_execution_cost",
            "basis_pnl_included": spread_snap.get("spread_tracking", False),
            "strategy": strategy,
            "strategy_name": strategy_name,
        },
    }


def current_position_leg(position: dict[str, Any], side: str) -> dict[str, Any]:
    leg = dict(leg_by_side(position.get("entry_legs") or [], side) or {})
    leg.setdefault("side", side)
    leg.setdefault("venue", position.get(f"{side}_venue"))
    leg.setdefault("symbol", position.get(f"{side}_symbol"))
    leg.setdefault("notional", position.get(f"{side}_notional"))
    leg.setdefault("base_quantity", position.get("base_quantity"))
    leg["next_funding_at"] = position.get(f"{side}_settlement_at") or leg.get(
        "next_funding_at"
    )
    return leg


def funding_leg_pnl(side: str, notional: float, funding_rate: float) -> float:
    if str(side).lower() == "long":
        return -float(notional) * float(funding_rate)
    return float(notional) * float(funding_rate)


def settlement_rate_or_entry(
    settlement_row: dict[str, Any] | None,
    entry_leg: dict[str, Any],
) -> float:
    if settlement_row is not None:
        return float(settlement_row.get("funding_rate") or 0.0)
    interval_rate = optional_float(entry_leg.get("funding_rate"))
    interval = max(1.0, float(entry_leg.get("funding_interval_hours") or 1.0))
    if str(entry_leg.get("funding_rate_kind") or "") == "published_current_hourly":
        return float(interval_rate or 0.0) * interval
    if interval_rate is not None:
        return interval_rate
    hourly = float(entry_leg.get("hourly_funding_rate") or 0.0)
    return hourly * interval


def settlement_payload(
    settlement_row: dict[str, Any] | None,
    entry_leg: dict[str, Any],
    funding_rate: float,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    return {
        "venue": (settlement_row or {}).get("venue") or entry_leg.get("venue"),
        "symbol": (settlement_row or {}).get("symbol") or entry_leg.get("symbol"),
        "settlement_at": (settlement_row or {}).get("funding_at")
        or entry_leg.get("next_funding_at"),
        "funding_rate": funding_rate,
        "source": source
        or ("history" if settlement_row is not None else "entry_estimate_fallback"),
        "history_row": settlement_row,
    }


def compute_spread_snapshot(
    position: dict[str, Any],
    route: dict[str, Any] | None,
) -> dict[str, Any]:
    entry_spread = optional_float(position.get("entry_cross_spread"))
    if route is None or entry_spread is None:
        return {
            "spread_tracking": False,
            "reason": "missing_route_or_entry_spread",
        }
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    current_long = leg_vwap(long_leg)
    current_short = leg_vwap(short_leg)
    if current_long is None or current_short is None:
        return {
            "spread_tracking": False,
            "reason": "missing_current_prices",
        }
    current_spread = current_long - current_short
    quantity = float(position.get("base_quantity") or 0.0)
    unrealized_basis_pnl = (current_spread - entry_spread) * quantity
    long_basis_pnl, short_basis_pnl = spread_leg_pnls(
        position,
        current_long,
        current_short,
        unrealized_basis_pnl,
    )
    notional = float(position.get("target_notional") or 0.0)
    reference = (current_long + current_short) / 2.0
    current_basis_bps = (
        (current_short - current_long) / reference * 10_000.0
        if reference > 0
        else 0.0
    )
    entry_basis_bps = float(position.get("entry_basis_bps") or 0.0)
    accrued_funding = float(
        (position.get("notes") or {}).get("accrued_funding_pnl") or 0.0
    )
    expected_exec_cost = float(position.get("expected_execution_cost") or 0.0)
    total_unrealized = accrued_funding + unrealized_basis_pnl - expected_exec_cost
    return {
        "spread_tracking": True,
        "entry_cross_spread": entry_spread,
        "current_cross_spread": current_spread,
        "current_long_price": current_long,
        "current_short_price": current_short,
        "unrealized_basis_pnl": unrealized_basis_pnl,
        "long_basis_pnl": long_basis_pnl,
        "short_basis_pnl": short_basis_pnl,
        "current_basis_bps": current_basis_bps,
        "entry_basis_bps": entry_basis_bps,
        "accrued_funding_pnl": accrued_funding,
        "total_unrealized_pnl": total_unrealized,
        "notional": notional,
    }


def spread_leg_pnls(
    position: dict[str, Any],
    current_long: float,
    current_short: float,
    total_basis_pnl: float,
) -> tuple[float, float]:
    quantity = float(position.get("base_quantity") or 0.0)
    entry_long = leg_vwap(current_position_leg(position, "long"))
    entry_short = leg_vwap(current_position_leg(position, "short"))
    if quantity > 0 and entry_long is not None and entry_short is not None:
        return (
            (current_long - entry_long) * quantity,
            (entry_short - current_short) * quantity,
        )
    # Older paper rows may only have the cross-spread, not both entry prices.
    # Preserve total PnL and split it evenly so account equity still matches
    # the trade-level PnL.
    return total_basis_pnl / 2.0, total_basis_pnl / 2.0


def compute_price_move_snapshot(
    position: dict[str, Any],
    route: dict[str, Any] | None,
) -> dict[str, Any]:
    """Measure current leg price moves from the paper entry snapshot."""
    if route is None:
        return {"price_move_tracking": False, "reason": "missing_route"}
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    if not long_leg or not short_leg:
        return {"price_move_tracking": False, "reason": "missing_route_legs"}

    entry_long = leg_vwap(current_position_leg(position, "long"))
    entry_short = leg_vwap(current_position_leg(position, "short"))
    current_long = leg_vwap(long_leg)
    current_short = leg_vwap(short_leg)
    entry_prices = {"long": entry_long, "short": entry_short}
    current_prices = {"long": current_long, "short": current_short}
    missing = [
        side
        for side, value in (
            ("entry_long", entry_long),
            ("entry_short", entry_short),
            ("current_long", current_long),
            ("current_short", current_short),
        )
        if value is None or value <= 0
    ]
    if missing:
        return {
            "price_move_tracking": False,
            "reason": "missing_or_invalid_price",
            "missing": missing,
            "entry_prices": entry_prices,
            "current_prices": current_prices,
        }

    assert entry_long is not None
    assert entry_short is not None
    assert current_long is not None
    assert current_short is not None
    long_move = (current_long - entry_long) / entry_long
    short_move = (current_short - entry_short) / entry_short
    entry_mid = (entry_long + entry_short) / 2.0
    current_mid = (current_long + current_short) / 2.0
    mid_move = (current_mid - entry_mid) / entry_mid if entry_mid > 0 else 0.0
    max_side = "long" if abs(long_move) >= abs(short_move) else "short"
    max_abs_move = max(abs(long_move), abs(short_move))
    return {
        "price_move_tracking": True,
        "entry_prices": entry_prices,
        "current_prices": current_prices,
        "long_move_fraction": long_move,
        "short_move_fraction": short_move,
        "mid_move_fraction": mid_move,
        "max_abs_move_fraction": max_abs_move,
        "max_move_side": max_side,
        "entry_mid_price": entry_mid,
        "current_mid_price": current_mid,
    }


def price_stop_loss_triggered(
    snapshot: dict[str, Any],
    config: PaperBotConfig,
) -> tuple[bool, str]:
    return False, "common_price_move_is_telemetry_not_stop"


def spread_stop_loss_triggered(
    snapshot: dict[str, Any],
    config: PaperBotConfig,
) -> tuple[bool, str]:
    if not snapshot.get("spread_tracking"):
        return False, ""
    notional = float(snapshot.get("notional") or 0.0)
    if notional <= 0:
        return False, ""
    unrealized = float(snapshot.get("unrealized_basis_pnl") or 0.0)
    basis_loss_bps = abs(min(0.0, unrealized)) / notional * 10_000.0
    if basis_loss_bps >= config.basis_stop_loss_bps:
        return True, (
            f"basis_stop_loss: unrealized_basis_pnl={unrealized:.2f} "
            f"({basis_loss_bps:.0f} bps >= {config.basis_stop_loss_bps:.0f} bps)"
        )
    return False, ""


def final_recheck_freeze_window_active(
    route: dict[str, Any],
    now: datetime,
    config: PaperBotConfig,
) -> bool:
    if config.final_recheck_freeze_seconds <= 0:
        return False
    leads = route_settlement_leads(route, now)
    return all(
        lead is not None and 0 <= lead < config.final_recheck_freeze_seconds
        for lead in leads.values()
    )


def final_recheck_fallback_route(
    route: dict[str, Any],
    now: datetime,
    config: PaperBotConfig,
    reason: str,
) -> dict[str, Any] | None:
    if not final_recheck_freeze_window_active(route, now, config):
        return None
    leads = route_settlement_leads(route, now)
    age = route_data_age_seconds(route, now)
    if age is None or age > config.max_entry_snapshot_age_seconds:
        return None
    fallback = dict(route)
    evidence = dict(fallback.get("evidence") or {})
    previous_recheck = dict(evidence.get("focused_recheck") or {})
    evidence["focused_recheck"] = {
        **previous_recheck,
        "mode": "final_freeze_last_success_v1",
        "reason": reason,
        "snapshot_age_seconds": age,
        "max_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
        "freeze_seconds": config.final_recheck_freeze_seconds,
        "lead_seconds": leads,
        "used_at": now.isoformat(),
    }
    evidence["entry_snapshot_source"] = "last_successful_focused_recheck"
    evidence["entry_snapshot_frozen"] = True
    fallback["evidence"] = evidence
    return fallback


def entry_cross_spread(
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
) -> float | None:
    long_price = leg_vwap(long_leg)
    short_price = leg_vwap(short_leg)
    if long_price is None or short_price is None:
        return None
    return long_price - short_price


def leg_vwap(leg: dict[str, Any]) -> float | None:
    evidence = leg.get("evidence") or leg
    for key in ("vwap", "mark_price", "mid_price", "price"):
        value = optional_float(evidence.get(key))
        if value is not None and value > 0:
            return value
    return None


def status_publishable_candidate(
    route: dict[str, Any],
    config: PaperBotConfig,
) -> bool:
    if route.get("status") != "paper_candidate":
        return False
    selected = selected_route_strategy(route, config.strategy_set)
    live_net = (
        optional_float(selected.get("expected_net_pnl"))
        if selected is not None
        else None
    )
    if live_net is None or live_net <= 0:
        return False
    return live_net >= required_live_net_profit(route, config)
