"""Shared funding-bot entry / exit decision logic.

Extracted from ``funding/trader.py`` so that every funding bot
(FundingPaperTrader, RiseXBot, future venue bots) uses the same
entry-timing gates, arm-window logic and monitoring decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FundingEntryConfig:
    """Entry-timing parameters shared by all funding bots."""

    entry_window_seconds: int = 180
    entry_min_lead_seconds: int = 0
    entry_max_lead_seconds: int = 15
    arm_window_seconds: int = 900
    final_recheck_freeze_seconds: int = 15
    max_entry_snapshot_age_seconds: int = 30
    settlement_grace_seconds: int = 90
    max_settlement_publication_lag_seconds: int = 300
    collateral_reserve_fraction: float = 0.10
    min_live_net_profit: float = 0.0

    def validated(self) -> "FundingEntryConfig":
        minimum = max(0, int(self.entry_min_lead_seconds))
        maximum = max(minimum, int(self.entry_max_lead_seconds))
        maximum = min(maximum, int(self.entry_window_seconds))
        minimum = min(minimum, maximum)
        return FundingEntryConfig(
            entry_window_seconds=max(15, min(int(self.entry_window_seconds), 900)),
            entry_min_lead_seconds=minimum,
            entry_max_lead_seconds=maximum,
            arm_window_seconds=max(
                int(self.entry_window_seconds),
                min(int(self.arm_window_seconds), 7_200),
            ),
            final_recheck_freeze_seconds=max(
                0,
                min(int(self.final_recheck_freeze_seconds), 60),
            ),
            max_entry_snapshot_age_seconds=max(
                5,
                min(int(self.max_entry_snapshot_age_seconds), 300),
            ),
            settlement_grace_seconds=max(
                15,
                min(int(self.settlement_grace_seconds), 1_800),
            ),
            max_settlement_publication_lag_seconds=max(
                60,
                min(int(self.max_settlement_publication_lag_seconds), 7_200),
            ),
            collateral_reserve_fraction=max(
                0.0,
                min(float(self.collateral_reserve_fraction), 1.0),
            ),
            min_live_net_profit=max(0.0, float(self.min_live_net_profit)),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def leg_by_side(legs: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    return next((leg for leg in legs if str(leg.get("side")) == side), None)


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


def route_entry_key(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    return ":".join(
        [
            str(route.get("route_key") or ""),
            str(long_leg.get("next_funding_at") or ""),
            str(short_leg.get("next_funding_at") or ""),
        ]
    )


def required_live_net_profit(
    route: dict[str, Any],
    config: FundingEntryConfig,
) -> float:
    evidence = route.get("evidence") or {}
    threshold = optional_float(evidence.get("actionable_profit_threshold"))
    return max(float(config.min_live_net_profit), threshold or 0.0)


# ---------------------------------------------------------------------------
# Entry decision
# ---------------------------------------------------------------------------

def route_entry_decision(
    route: dict[str, Any],
    accounts: dict[str, dict[str, Any]],
    now: datetime,
    config: FundingEntryConfig,
) -> dict[str, Any]:
    """Decide whether a route is eligible for entry RIGHT NOW.

    Returns ``{"eligible": bool, "armed": bool, "reasons": [...], ...}``.
    """
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    reasons: list[str] = []
    leads: dict[str, float | None] = {"long": None, "short": None}

    if route.get("status") != "paper_candidate":
        reasons.append("route_not_candidate")

    evidence = route.get("evidence") or {}
    live_net = float(evidence.get("current_nowcast_net") or 0.0)

    if not long_leg or not short_leg:
        reasons.append("missing_route_legs")
    else:
        for side, leg in (("long", long_leg), ("short", short_leg)):
            settlement_at = parse_iso(leg.get("next_funding_at"))
            if settlement_at is None:
                reasons.append(f"{side}_settlement_missing")
                continue
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
                notional = float(
                    leg.get("notional") or route.get("target_notional") or 0.0
                )
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

    req_live_net = required_live_net_profit(route, config)
    armed = bool(
        long_leg
        and short_leg
        and all(
            lead is not None and 0 <= lead <= config.arm_window_seconds
            for lead in leads.values()
        )
        and live_net >= req_live_net
    )

    if live_net <= 0:
        if "live_net_not_positive" not in reasons:
            reasons.append("live_net_not_positive")
    elif live_net < req_live_net:
        reasons.append("live_net_below_required_profit")

    return {
        "eligible": not reasons,
        "armed": armed,
        "reasons": reasons,
        "lead_seconds": leads,
        "entry_window_seconds": config.entry_window_seconds,
        "entry_min_lead_seconds": config.entry_min_lead_seconds,
        "entry_max_lead_seconds": config.entry_max_lead_seconds,
        "arm_window_seconds": config.arm_window_seconds,
        "live_net": live_net,
        "required_live_net": req_live_net,
        "snapshot_age_seconds": snapshot_age,
        "max_entry_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
    }


# ---------------------------------------------------------------------------
# Monitor decision
# ---------------------------------------------------------------------------

def route_monitor_decision(
    route: dict[str, Any],
    now: datetime,
    config: FundingEntryConfig,
) -> dict[str, Any]:
    """Decide whether a route should be actively monitored (hot / urgent)."""
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

    live_net = float((route.get("evidence") or {}).get("current_nowcast_net") or 0.0)
    req_live_net = required_live_net_profit(route, config)
    if live_net <= 0:
        reasons.append("live_net_not_positive")
    elif live_net < req_live_net:
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
            lead is not None and 0 <= lead <= config.entry_window_seconds
            for lead in leads.values()
        )
    )

    return {
        "hot": hot,
        "urgent": urgent,
        "reasons": reasons,
        "lead_seconds": leads,
        "live_net": live_net,
        "required_live_net": req_live_net,
    }
