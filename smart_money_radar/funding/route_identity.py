from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.economics import perp_route_key


def _clean_text(value: Any, *, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _upper(value: Any, *, default: str = "") -> str:
    return _clean_text(value, default=default).upper()


def _lower(value: Any, *, default: str = "") -> str:
    return _clean_text(value, default=default).lower()


def _finite_text(value: Any, *, default: str = "") -> str:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _clean_text(value, default=default)
    if not math.isfinite(parsed):
        return default
    return f"{parsed:.12g}"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp_score(value: Any) -> float:
    parsed = _parse_time(value)
    return parsed.timestamp() if parsed is not None else float("-inf")


def market_variant_identity_fields(market: dict[str, Any]) -> dict[str, str]:
    """Fields that distinguish economically different listed products."""
    product_identity = (
        market.get("product_id")
        or market.get("instrument_id")
        or market.get("market_id")
        or market.get("api_symbol")
        or market.get("symbol")
    )
    product_type = (
        market.get("api_product_type")
        or market.get("product_type")
        or market.get("market_type")
        or market.get("contract_kind")
        or market.get("contract_type")
    )
    quote = market.get("quote_asset") or market.get("settlement_asset")
    collateral = market.get("collateral_asset") or quote
    return {
        "venue": _lower(market.get("venue")),
        "environment": _lower(market.get("environment"), default="mainnet"),
        "symbol": _clean_text(market.get("symbol")),
        "product_identity": _clean_text(product_identity),
        "quote_asset": _upper(quote),
        "collateral_asset": _upper(collateral),
        "contract_type": _lower(market.get("contract_type") or market.get("contract_kind")),
        "product_type": _lower(product_type),
        "contract_multiplier": _finite_text(market.get("contract_multiplier"), default="1"),
        "canonical_unit_multiplier": _finite_text(
            market.get("canonical_unit_multiplier"),
            default="1",
        ),
    }


def route_family_key(asset: str, long_venue: str, short_venue: str) -> str:
    """Canonical asset plus directed venues; groups variants for display."""
    return perp_route_key(_upper(asset), _lower(long_venue), _lower(short_venue))


def route_variant_identity_fields(
    *,
    asset: str,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> dict[str, Any]:
    long = market_variant_identity_fields(long_market)
    short = market_variant_identity_fields(short_market)
    family = {
        "canonical_asset": _upper(asset),
        "long_venue": long["venue"],
        "short_venue": short["venue"],
    }
    return {
        **family,
        "route_family_key": route_family_key(
            family["canonical_asset"],
            family["long_venue"],
            family["short_venue"],
        ),
        "long": long,
        "short": short,
    }


def route_variant_key(
    *,
    asset: str,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> str:
    fields = route_variant_identity_fields(
        asset=asset,
        long_market=long_market,
        short_market=short_market,
    )
    parts = [
        "route_variant",
        fields["canonical_asset"],
        fields["long_venue"],
        fields["short_venue"],
    ]
    for side in ("long", "short"):
        side_fields = fields[side]
        parts.extend(
            [
                side_fields["environment"],
                side_fields["symbol"],
                side_fields["product_identity"],
                side_fields["quote_asset"],
                side_fields["collateral_asset"],
                side_fields["contract_type"],
                side_fields["product_type"],
                side_fields["contract_multiplier"],
                side_fields["canonical_unit_multiplier"],
            ]
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def route_identity_from_route(route: dict[str, Any]) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = next((leg for leg in legs if str(leg.get("side")) == "long"), {})
    short_leg = next((leg for leg in legs if str(leg.get("side")) == "short"), {})
    long_market = {
        **dict(long_leg),
        "venue": long_leg.get("venue") or route.get("long_venue"),
        "symbol": long_leg.get("symbol") or route.get("long_symbol"),
    }
    short_market = {
        **dict(short_leg),
        "venue": short_leg.get("venue") or route.get("short_venue"),
        "symbol": short_leg.get("symbol") or route.get("short_symbol"),
    }
    asset = route.get("canonical_asset") or long_leg.get("canonical_asset")
    fields = route_variant_identity_fields(
        asset=str(asset or ""),
        long_market=long_market,
        short_market=short_market,
    )
    fields["route_variant_key"] = route_variant_key(
        asset=str(asset or ""),
        long_market=long_market,
        short_market=short_market,
    )
    return fields


def enrich_route_identity(
    route: dict[str, Any],
    *,
    asset: str,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> dict[str, Any]:
    fields = route_variant_identity_fields(
        asset=asset,
        long_market=long_market,
        short_market=short_market,
    )
    variant_key = route_variant_key(
        asset=asset,
        long_market=long_market,
        short_market=short_market,
    )
    evidence = dict(route.get("evidence") or {})
    evidence["route_family_key"] = fields["route_family_key"]
    evidence["route_variant_key"] = variant_key
    evidence["route_identity"] = {**fields, "route_variant_key": variant_key}
    route = {
        **route,
        "route_key": variant_key,
        "route_family_key": fields["route_family_key"],
        "route_variant_key": variant_key,
        "legacy_route_key": fields["route_family_key"],
        "evidence": evidence,
    }
    return route


def _readiness(route: dict[str, Any]) -> dict[str, Any]:
    evidence = route.get("evidence") or {}
    policy = evidence.get("readiness_policy")
    if isinstance(policy, dict):
        return policy
    capability = evidence.get("capability_check")
    if isinstance(capability, dict):
        return capability
    return evidence


def _economics(route: dict[str, Any]) -> dict[str, Any]:
    readiness = _readiness(route)
    economics = readiness.get("economics")
    return economics if isinstance(economics, dict) else {}


def _float_from(*values: Any, default: float = 0.0) -> float:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return default


def _route_variant_key_for_tie_break(route: dict[str, Any]) -> str:
    key = route.get("route_variant_key") or (route.get("evidence") or {}).get("route_variant_key")
    if key:
        return str(key)
    return route_identity_from_route(route)["route_variant_key"]


def route_variant_rank_sort_key(route: dict[str, Any]) -> tuple[Any, ...]:
    readiness = _readiness(route)
    evidence = route.get("evidence") or {}
    economics = _economics(route)
    hard_blockers = list(
        readiness.get("hard_blockers")
        or evidence.get("hard_blockers")
        or []
    )
    verified = bool(
        readiness.get("verified_paper_ready")
        or evidence.get("verified_paper_ready")
    )
    experimental = bool(
        readiness.get("experimental_simulation_ready")
        or evidence.get("experimental_simulation_ready")
        or readiness.get("experimental_paper_ready")
        or evidence.get("experimental_paper_ready")
    )
    conservative_net = _float_from(
        economics.get("conservative_expected_net_usd"),
        evidence.get("conservative_expected_net"),
        evidence.get("current_nowcast_net"),
    )
    conservative_funding = _float_from(
        economics.get("conservative_expected_funding_usd"),
        evidence.get("conservative_expected_funding"),
        evidence.get("current_nowcast_gross"),
    )
    observed_at = (
        route.get("observed_at")
        or (evidence.get("lightweight_discovery") or {}).get("observed_at")
    )
    return (
        1 if hard_blockers else 0,
        0 if verified else 1,
        0 if experimental else 1,
        -conservative_net,
        -conservative_funding,
        -_float_from(readiness.get("rate_confidence"), evidence.get("rate_confidence")),
        -_float_from(readiness.get("execution_confidence"), evidence.get("execution_confidence")),
        -_float_from(readiness.get("settlement_confidence"), evidence.get("settlement_confidence")),
        -_timestamp_score(observed_at),
        _route_variant_key_for_tie_break(route),
    )


def best_route_variant(routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not routes:
        return None
    return sorted(routes, key=route_variant_rank_sort_key)[0]


@dataclass(frozen=True)
class FocusedSelectionConfig:
    max_focused_routes: int = 20
    max_experimental_focused_routes: int = 6
    max_focused_routes_per_venue: int = 8
    focused_selection_hysteresis: int = 2
    focused_selection_min_ttl_seconds: float = 20.0
    arm_window_seconds: float = 120.0
    entry_max_lead_seconds: float = 35.0


def _route_key(route: dict[str, Any]) -> str:
    return str(route.get("route_key") or route.get("route_variant_key") or "")


def _route_venues(route: dict[str, Any]) -> tuple[str, str]:
    return _lower(route.get("long_venue")), _lower(route.get("short_venue"))


def _lead_seconds(route: dict[str, Any], now: datetime) -> float | None:
    leads: list[float] = []
    for value in (
        route.get("next_funding_at"),
        route.get("long_next_funding_at"),
        route.get("short_next_funding_at"),
    ):
        parsed = _parse_time(value)
        if parsed is not None:
            leads.append((parsed - now.astimezone(UTC)).total_seconds())
    for leg in route.get("legs") or []:
        parsed = _parse_time(leg.get("next_funding_at"))
        if parsed is not None:
            leads.append((parsed - now.astimezone(UTC)).total_seconds())
    positive = [lead for lead in leads if lead >= 0]
    return min(positive) if positive else None


def _focus_state(route: dict[str, Any], now: datetime, config: FocusedSelectionConfig) -> dict[str, Any]:
    readiness = _readiness(route)
    evidence = route.get("evidence") or {}
    hard_blockers = list(readiness.get("hard_blockers") or evidence.get("hard_blockers") or [])
    verified = bool(readiness.get("verified_paper_ready") or evidence.get("verified_paper_ready"))
    experimental = bool(
        readiness.get("experimental_simulation_ready")
        or evidence.get("experimental_simulation_ready")
        or readiness.get("experimental_paper_ready")
        or evidence.get("experimental_paper_ready")
    )
    economically_observable = bool(
        readiness.get("economically_observable")
        or evidence.get("economically_observable")
        or _float_from(evidence.get("current_nowcast_gross")) > 0.0
    )
    lead = _lead_seconds(route, now)
    inside_arm = lead is not None and 0.0 <= lead <= float(config.arm_window_seconds)
    inside_entry = lead is not None and 0.0 <= lead <= float(config.entry_max_lead_seconds)
    eligible = bool(not hard_blockers and (verified or experimental or inside_arm or economically_observable))
    return {
        "eligible": eligible,
        "verified": verified,
        "experimental": experimental,
        "inside_arm_window": inside_arm,
        "inside_entry_window": inside_entry,
        "lead_seconds": lead,
        "hard_blockers": hard_blockers,
    }


def _venue_health_score(route: dict[str, Any], venue_health: dict[str, Any] | None) -> float:
    if not venue_health:
        return 0.5
    fresh = set(venue_health.get("fresh_venues") or [])
    ready = set(venue_health.get("ready_venues") or [])
    cached = set(venue_health.get("cached_venues") or [])
    pending = set(venue_health.get("pending_venues") or [])
    unavailable = set(venue_health.get("unavailable_venues") or []) | set(
        venue_health.get("stale_venues") or []
    )

    def one(venue: str) -> float:
        if venue in fresh:
            return 1.0
        if venue in ready:
            return 0.85
        if venue in cached:
            return 0.70
        if venue in pending:
            return 0.35
        if venue in unavailable:
            return 0.0
        return 0.5

    long_venue, short_venue = _route_venues(route)
    return min(one(long_venue), one(short_venue))


def focused_route_rank_sort_key(
    route: dict[str, Any],
    *,
    now: datetime,
    config: FocusedSelectionConfig,
    open_position_route_keys: set[str],
    submitted_route_keys: set[str],
    venue_health: dict[str, Any] | None,
) -> tuple[Any, ...]:
    state = _focus_state(route, now, config)
    readiness = _readiness(route)
    evidence = route.get("evidence") or {}
    economics = _economics(route)
    route_key = _route_key(route)
    open_position = route_key in open_position_route_keys
    submitted = route_key in submitted_route_keys
    lead = state["lead_seconds"]
    return (
        0 if open_position else 1,
        0 if submitted else 1,
        0 if state["verified"] else 1,
        0 if state["inside_arm_window"] else 1,
        float("inf") if lead is None else max(0.0, float(lead)),
        0 if state["experimental"] else 1,
        -_float_from(
            economics.get("conservative_expected_net_usd"),
            evidence.get("conservative_expected_net"),
            evidence.get("current_nowcast_net"),
        ),
        -_float_from(
            economics.get("conservative_expected_funding_usd"),
            evidence.get("conservative_expected_funding"),
            evidence.get("current_nowcast_gross"),
        ),
        -_float_from(readiness.get("rate_confidence"), evidence.get("rate_confidence")),
        -_float_from(readiness.get("execution_confidence"), evidence.get("execution_confidence")),
        -_float_from(readiness.get("settlement_confidence"), evidence.get("settlement_confidence")),
        -_venue_health_score(route, venue_health),
        _route_variant_key_for_tie_break(route),
    )


def _copy_with_focus(
    route: dict[str, Any],
    *,
    state: str,
    rank: int | None,
    reason: str | None = None,
    cutoff_rank: int | None = None,
    cutoff_score: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    copied = dict(route)
    evidence = dict(copied.get("evidence") or {})
    flags = list(dict.fromkeys([*(copied.get("risk_flags") or []), *(evidence.get("risk_flags") or [])]))
    advisory = list(evidence.get("advisory_reasons") or [])
    if state == "deferred":
        if "focused_capacity_deferred" not in flags:
            flags.append("focused_capacity_deferred")
        advisory.append("focused_capacity_deferred")
    copied["risk_flags"] = list(dict.fromkeys(flags))
    evidence["risk_flags"] = list(dict.fromkeys([*(evidence.get("risk_flags") or []), *flags]))
    evidence["advisory_reasons"] = list(dict.fromkeys(advisory))
    evidence["focused_selection"] = {
        "state": state,
        "rank": rank,
        "reason": reason,
        "cutoff_rank": cutoff_rank,
        "cutoff_score": list(cutoff_score) if cutoff_score is not None else None,
    }
    evidence["focused_state"] = state
    evidence["focus_rank"] = rank
    copied["focused_state"] = state
    copied["focus_rank"] = rank
    copied["evidence"] = evidence
    return copied


def select_focused_routes(
    routes: list[dict[str, Any]],
    *,
    now: datetime,
    config: FocusedSelectionConfig,
    previous_selected_at: dict[str, float] | None = None,
    now_monotonic: float = 0.0,
    open_position_route_keys: set[str] | None = None,
    submitted_route_keys: set[str] | None = None,
    venue_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    open_keys = set(open_position_route_keys or set())
    submitted_keys = set(submitted_route_keys or set())
    selected_at = previous_selected_at or {}
    eligible: list[dict[str, Any]] = []
    not_eligible: list[dict[str, Any]] = []
    for route in routes:
        if _focus_state(route, now, config)["eligible"] or _route_key(route) in open_keys:
            eligible.append(route)
        else:
            not_eligible.append(route)

    ranked = sorted(
        eligible,
        key=lambda route: focused_route_rank_sort_key(
            route,
            now=now,
            config=config,
            open_position_route_keys=open_keys,
            submitted_route_keys=submitted_keys,
            venue_health=venue_health,
        ),
    )
    rank_by_key = {_route_key(route): index + 1 for index, route in enumerate(ranked)}
    score_by_key = {
        _route_key(route): focused_route_rank_sort_key(
            route,
            now=now,
            config=config,
            open_position_route_keys=open_keys,
            submitted_route_keys=submitted_keys,
            venue_health=venue_health,
        )
        for route in ranked
    }
    protected: list[dict[str, Any]] = []
    normal: list[dict[str, Any]] = []
    for route in ranked:
        route_key = _route_key(route)
        state = _focus_state(route, now, config)
        if route_key in open_keys or route_key in submitted_keys or (
            state["verified"] and state["inside_entry_window"]
        ):
            protected.append(route)
        else:
            normal.append(route)

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    venue_counts: dict[str, int] = {}
    experimental_count = 0

    def can_add(route: dict[str, Any], *, protected_route: bool) -> bool:
        route_key = _route_key(route)
        if route_key in selected_keys:
            return False
        if route_key in open_keys:
            return True
        if len([r for r in selected if _route_key(r) not in open_keys]) >= int(config.max_focused_routes):
            return False
        state = _focus_state(route, now, config)
        if (
            state["experimental"]
            and not state["verified"]
            and route_key not in submitted_keys
            and experimental_count >= int(config.max_experimental_focused_routes)
        ):
            return False
        per_venue = int(config.max_focused_routes_per_venue)
        if per_venue > 0 and not protected_route:
            long_venue, short_venue = _route_venues(route)
            if venue_counts.get(long_venue, 0) >= per_venue:
                return False
            if venue_counts.get(short_venue, 0) >= per_venue:
                return False
        return True

    def add(route: dict[str, Any], *, protected_route: bool) -> bool:
        nonlocal experimental_count
        if not can_add(route, protected_route=protected_route):
            return False
        selected.append(route)
        selected_keys.add(_route_key(route))
        long_venue, short_venue = _route_venues(route)
        venue_counts[long_venue] = venue_counts.get(long_venue, 0) + 1
        venue_counts[short_venue] = venue_counts.get(short_venue, 0) + 1
        state = _focus_state(route, now, config)
        if state["experimental"] and not state["verified"]:
            experimental_count += 1
        return True

    for route in protected:
        add(route, protected_route=True)

    sticky: list[dict[str, Any]] = []
    hysteresis_cutoff = int(config.max_focused_routes) + int(config.focused_selection_hysteresis)
    for route in normal:
        route_key = _route_key(route)
        age = max(0.0, float(now_monotonic) - float(selected_at.get(route_key, -math.inf)))
        if (
            route_key in selected_at
            and age <= float(config.focused_selection_min_ttl_seconds)
            and rank_by_key.get(route_key, 10**9) <= hysteresis_cutoff
        ):
            sticky.append(route)
    for route in sticky:
        add(route, protected_route=False)
    for route in normal:
        add(route, protected_route=False)

    cutoff_rank = max(
        (rank_by_key.get(_route_key(route), 0) for route in selected if _route_key(route) not in open_keys),
        default=0,
    )
    cutoff_score = None
    cutoff_candidates = [
        score_by_key.get(_route_key(route))
        for route in selected
        if _route_key(route) not in open_keys
    ]
    if cutoff_candidates:
        cutoff_score = max(cutoff_candidates)

    annotated: list[dict[str, Any]] = []
    focused: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for route in ranked:
        route_key = _route_key(route)
        rank = rank_by_key.get(route_key)
        if route_key in selected_keys:
            annotated_route = _copy_with_focus(route, state="selected", rank=rank)
            focused.append(annotated_route)
        else:
            annotated_route = _copy_with_focus(
                route,
                state="deferred",
                rank=rank,
                reason="focused_capacity_deferred",
                cutoff_rank=cutoff_rank or None,
                cutoff_score=cutoff_score,
            )
            deferred.append(annotated_route)
        annotated.append(annotated_route)
    for route in not_eligible:
        annotated.append(
            _copy_with_focus(
                route,
                state="not_yet_eligible",
                rank=None,
                reason="outside_focus_window_or_not_economic",
            )
        )
    return {
        "routes": annotated,
        "focus_eligible_routes": ranked,
        "focused_routes": focused,
        "deferred_routes": deferred,
        "not_eligible_routes": not_eligible,
        "rank_by_route_key": rank_by_key,
        "selected_route_keys": [_route_key(route) for route in focused],
        "deferred_route_keys": [_route_key(route) for route in deferred],
        "cutoff_rank": cutoff_rank,
        "cutoff_score": list(cutoff_score) if cutoff_score is not None else None,
        "config": {
            "max_focused_routes": int(config.max_focused_routes),
            "max_experimental_focused_routes": int(config.max_experimental_focused_routes),
            "max_focused_routes_per_venue": int(config.max_focused_routes_per_venue),
            "focused_selection_hysteresis": int(config.focused_selection_hysteresis),
            "focused_selection_min_ttl_seconds": float(config.focused_selection_min_ttl_seconds),
        },
    }
