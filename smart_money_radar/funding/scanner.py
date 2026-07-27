from __future__ import annotations

import math
from itertools import combinations
from typing import Any

from smart_money_radar.funding.economics import (
    evaluate_perp_route,
    market_fee_rate,
)
from smart_money_radar.funding.forecast import paired_settlement_schedule
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import canonical_asset_symbol


def rank_perp_pairs(
    markets: list[dict[str, Any]],
    maximum: int | None,
    pinned_routes: list[dict[str, Any]] | None = None,
    config: FundingScanConfig | None = None,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        asset = canonical_asset_symbol(market.get("canonical_asset"))
        if asset:
            by_asset.setdefault(asset, []).append(market)
    candidates: list[dict[str, Any]] = []
    for asset, rows in by_asset.items():
        venue_rows = {str(row["venue"]): row for row in rows}
        for first, second in combinations(venue_rows.values(), 2):
            candidates.append(
                best_pair_orientation(
                    asset,
                    first,
                    second,
                    config,
                    observed_at,
                )
            )
    candidates.sort(
        key=lambda row: (
            bool(row.get("quick_schedule_ready", True)),
            float(
                row.get(
                    "quick_opportunity_best_case_net_rate",
                    row.get("quick_net_rate", row["current_hourly_spread"]),
                )
            ),
            float(row.get("quick_positive_edge_rate") or 0.0),
            float(row.get("quick_gross_rate") or row["current_hourly_spread"]),
            quick_liquidity(row),
        ),
        reverse=True,
    )
    selected = (
        list(candidates)
        if maximum is None or maximum <= 0
        else diversified_preselection(candidates, maximum)
    )
    selected_keys = {candidate_key(row) for row in selected}

    for previous in pinned_routes or []:
        asset = str(previous.get("canonical_asset") or "")
        venue_rows = {
            str(row["venue"]): row for row in by_asset.get(asset, [])
        }
        long_market = venue_rows.get(str(previous.get("long_venue") or ""))
        short_market = venue_rows.get(str(previous.get("short_venue") or ""))
        if not long_market or not short_market:
            continue
        pinned = pair_candidate(
            asset,
            long_market,
            short_market,
            preserve_order=True,
        )
        if pinned is None:
            continue
        pinned.update(quick_route_economics(pinned, config, observed_at))
        key = candidate_key(pinned)
        if key in selected_keys:
            for row in selected:
                if candidate_key(row) == key:
                    row["is_pinned_revalidation"] = True
                    break
        else:
            pinned["is_pinned_revalidation"] = True
            selected.append(pinned)
            selected_keys.add(key)
    return selected


def execution_shortlist(
    candidates: list[dict[str, Any]],
    near_miss_limit: int | None = None,
    config: FundingScanConfig | None = None,
) -> list[dict[str, Any]]:
    """Keep strict executable routes plus adaptive positive-gross near misses.

    ``None`` means adaptive near-miss selection.
    ``0`` disables the near-miss band for speed-focused experiments.
    """
    settings = (config or FundingScanConfig()).validated()
    selected: list[dict[str, Any]] = []
    strict_candidates: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    selected_markets: set[tuple[str, str]] = set()
    for candidate in candidates:
        candidate.update(quick_opportunity_metrics(candidate, settings))
        has_positive_edge = bool(candidate.get("quick_has_positive_edge"))
        spread_reject_reason = spread_dominant_strict_reject_reason(
            candidate,
            settings,
        )
        if bool(candidate.get("quick_identity_mismatch")):
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = "unit_identity_mismatch"
        elif not bool(candidate.get("quick_schedule_ready")):
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = "funding_schedule_unavailable"
        elif not has_positive_edge:
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = "non_positive_settlement_carry"
        elif float(candidate.get("quick_opportunity_best_case_net_rate") or 0.0) <= 0:
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = best_case_reject_reason(candidate)
            near_misses.append(candidate)
        elif spread_reject_reason is not None:
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = spread_reject_reason
            near_misses.append(candidate)
        elif candidate.get("is_pinned_revalidation"):
            candidate["execution_eligible"] = True
            candidate["execution_screen_reason"] = "pinned_revalidation"
            selected.append(candidate)
            selected_markets.update(candidate_market_keys(candidate))
        else:
            candidate["execution_eligible"] = True
            candidate["execution_screen_reason"] = full_depth_reason(candidate)
            strict_candidates.append(candidate)

    for candidate in strict_candidates:
        if market_budget_allows(candidate, selected_markets, settings):
            selected.append(candidate)
            selected_markets.update(candidate_market_keys(candidate))
        else:
            candidate["execution_eligible"] = False
            candidate["execution_screen_reason"] = deep_sweep_reason(candidate)
    if near_miss_limit == 0:
        return selected
    near_misses.sort(
        key=lambda row: (
            bool(row.get("quick_extreme_gross")),
            float(row.get("quick_opportunity_score") or 0.0),
            float(row.get("quick_cost_coverage") or 0.0),
            float(row.get("quick_opportunity_best_case_net_rate") or 0.0),
            float(row.get("quick_positive_edge_rate") or 0.0),
            quick_liquidity(row),
        ),
        reverse=True,
    )
    selected_near_misses = adaptive_near_miss_selection(
        near_misses,
        selected,
        settings,
        near_miss_limit,
    )
    for candidate in selected_near_misses:
        candidate["execution_eligible"] = True
        candidate["execution_screen_reason"] = adaptive_selection_reason(candidate)
        selected.append(candidate)
    return selected


def adaptive_near_miss_selection(
    near_misses: list[dict[str, Any]],
    strict_selected: list[dict[str, Any]],
    config: FundingScanConfig,
    route_limit: int | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_route_count = 0
    selected_candidate_keys: set[tuple[str, str, str]] = set()
    selected_markets = {
        key
        for candidate in strict_selected
        for key in candidate_market_keys(candidate)
    }
    max_markets = config.max_full_depth_orderbook_markets
    stage_candidates = [
        (
            "dynamic_gross",
            [
                row
                for row in near_misses
                if bool(row.get("quick_dynamic_gross_emergency"))
            ],
        ),
        (
            "near_break_even",
            [
                row
                for row in near_misses
                if bool(row.get("quick_material_gross"))
                and float(row.get("quick_cost_coverage") or 0.0)
                >= config.adaptive_near_miss_near_break_even_coverage
            ],
        ),
        (
            "quality_score",
            [
                row
                for row in near_misses
                if bool(row.get("quick_material_gross"))
                and float(row.get("quick_opportunity_score") or 0.0)
                >= config.adaptive_near_miss_min_score
                and float(row.get("quick_cost_coverage") or 0.0)
                >= config.adaptive_near_miss_min_cost_coverage
            ],
        ),
        (
            "liquidity_urgency",
            [
                row
                for row in near_misses
                if bool(row.get("quick_material_gross"))
                and float(row.get("quick_liquidity_score") or 0.0)
                >= config.adaptive_near_miss_liquidity_score
                and float(row.get("quick_settlement_urgency_score") or 0.0)
                >= config.adaptive_near_miss_urgency_score
                and float(row.get("quick_cost_coverage") or 0.0)
                >= config.adaptive_near_miss_min_cost_coverage
            ],
        ),
        ("diversity_probe", diversity_probe_candidates(near_misses, config)),
    ]
    for stage, rows in stage_candidates:
        for candidate in sorted(rows, key=adaptive_stage_rank, reverse=True):
            key = selection_candidate_key(candidate)
            if key in selected_candidate_keys:
                continue
            if route_limit is not None and selected_route_count >= max(0, int(route_limit)):
                candidate["execution_screen_reason"] = deep_sweep_reason(candidate)
                continue
            candidate_keys = candidate_market_keys(candidate)
            incremental_markets = {
                key for key in candidate_keys if key not in selected_markets
            }
            if (
                max_markets is not None
                and len(selected_markets) + len(incremental_markets) > max_markets
            ):
                candidate["execution_screen_reason"] = deep_sweep_reason(candidate)
                continue
            candidate["quick_selection_stage"] = stage
            selected.append(candidate)
            selected_candidate_keys.add(key)
            selected_route_count += 1
            selected_markets.update(incremental_markets)
    for candidate in near_misses:
        if selection_candidate_key(candidate) not in selected_candidate_keys:
            candidate["execution_screen_reason"] = deep_sweep_reason(candidate)
    return selected


def market_budget_allows(
    candidate: dict[str, Any],
    selected_markets: set[tuple[str, str]],
    config: FundingScanConfig,
) -> bool:
    max_markets = config.max_full_depth_orderbook_markets
    if max_markets is None:
        return True
    candidate_keys = set(candidate_market_keys(candidate))
    incremental_markets = {
        key for key in candidate_keys if key not in selected_markets
    }
    return len(selected_markets) + len(incremental_markets) <= max_markets


def deep_sweep_reason(candidate: dict[str, Any]) -> str:
    return (
        "projected_interval_deep_sweep"
        if bool(candidate.get("quick_projection_used"))
        else "queued_for_deep_sweep"
    )


def best_case_reject_reason(candidate: dict[str, Any]) -> str:
    if bool(candidate.get("quick_has_positive_spread_edge")):
        return "best_case_opportunity_below_unavoidable_cost"
    return "best_case_carry_below_unavoidable_cost"


def spread_dominant_strict_reject_reason(
    candidate: dict[str, Any],
    config: FundingScanConfig,
) -> str | None:
    spread_rate = max(0.0, float(candidate.get("quick_signed_spread_rate") or 0.0))
    funding_rate = max(0.0, float(candidate.get("quick_gross_rate") or 0.0))
    if spread_rate <= 0.0 or spread_rate < funding_rate:
        return None
    if float(candidate.get("quick_best_case_net_profit") or 0.0) < float(
        candidate.get("quick_actionable_profit_threshold") or 0.0
    ):
        return "spread_opportunity_below_actionable_threshold"
    return None


def full_depth_reason(candidate: dict[str, Any]) -> str:
    if bool(candidate.get("quick_projection_used")):
        return "projected_interval_full_depth"
    has_funding = bool(candidate.get("quick_has_positive_funding_edge"))
    has_spread = bool(candidate.get("quick_has_positive_spread_edge"))
    if has_funding and has_spread:
        return "combined_opportunity_full_depth"
    if has_spread:
        return "spread_opportunity_full_depth"
    return "full_execution_required"


def adaptive_selection_reason(candidate: dict[str, Any]) -> str:
    if bool(candidate.get("quick_projection_used")):
        return "adaptive_projected_interval_full_depth"
    if bool(candidate.get("quick_has_positive_spread_edge")):
        return "adaptive_spread_opportunity_full_depth"
    stage = str(candidate.get("quick_selection_stage") or "quality_score")
    return {
        "dynamic_gross": "adaptive_dynamic_gross_full_depth",
        "near_break_even": "adaptive_near_break_even_full_depth",
        "quality_score": "adaptive_quality_score_full_depth",
        "liquidity_urgency": "adaptive_liquidity_urgency_full_depth",
        "diversity_probe": "adaptive_diversity_probe_full_depth",
    }.get(stage, "adaptive_quality_score_full_depth")


def adaptive_stage_rank(candidate: dict[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        float(candidate.get("quick_opportunity_score") or 0.0),
        float(candidate.get("quick_cost_coverage") or 0.0),
        float(candidate.get("quick_positive_edge_rate") or 0.0),
        quick_liquidity(candidate),
        float(candidate.get("quick_opportunity_best_case_net_rate") or 0.0),
    )


def diversity_probe_candidates(
    near_misses: list[dict[str, Any]],
    config: FundingScanConfig,
) -> list[dict[str, Any]]:
    by_asset: dict[str, dict[str, Any]] = {}
    by_venue_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in near_misses:
        if not bool(candidate.get("quick_material_gross")):
            continue
        if (
            float(candidate.get("quick_cost_coverage") or 0.0)
            < config.adaptive_near_miss_min_cost_coverage
        ):
            continue
        asset = str(candidate.get("canonical_asset") or "")
        if asset:
            current = by_asset.get(asset)
            if current is None or adaptive_stage_rank(candidate) > adaptive_stage_rank(current):
                by_asset[asset] = candidate
        venues = tuple(
            sorted(
                (
                    str(candidate.get("long_market", {}).get("venue") or ""),
                    str(candidate.get("short_market", {}).get("venue") or ""),
                )
            )
        )
        current_pair = by_venue_pair.get(venues)
        if current_pair is None or adaptive_stage_rank(candidate) > adaptive_stage_rank(current_pair):
            by_venue_pair[venues] = candidate
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in [*by_asset.values(), *by_venue_pair.values()]:
        unique[selection_candidate_key(candidate)] = candidate
    return list(unique.values())


def selection_candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    try:
        return candidate_key(candidate)
    except KeyError:
        long_market = candidate.get("long_market") or {}
        short_market = candidate.get("short_market") or {}
        return (
            str(candidate.get("canonical_asset") or id(candidate)),
            f"{long_market.get('venue', '')}:{long_market.get('symbol', '')}",
            f"{short_market.get('venue', '')}:{short_market.get('symbol', '')}",
        )


def quick_opportunity_metrics(
    candidate: dict[str, Any],
    config: FundingScanConfig,
) -> dict[str, Any]:
    funding_rate = float(candidate.get("quick_gross_rate") or 0.0)
    spread_rate = float(candidate.get("quick_signed_spread_rate") or 0.0)
    positive_edge_rate = max(0.0, funding_rate) + max(0.0, spread_rate)
    gross_rate = positive_edge_rate
    best_net_rate = float(
        candidate.get(
            "quick_opportunity_best_case_net_rate",
            candidate.get("quick_best_case_net_rate") or 0.0,
        )
    )
    best_cost_rate = float(candidate.get("quick_best_case_cost_rate") or 0.0)
    if best_cost_rate <= 0:
        best_cost_rate = max(
            0.0,
            float(candidate.get("quick_opportunity_gross_rate") or funding_rate)
            - best_net_rate,
        )
    cost_coverage = (
        math.inf
        if best_cost_rate <= 0.0 and gross_rate > 0.0
        else safe_divide(gross_rate, best_cost_rate)
    )
    target_notional = float(config.target_notional)
    estimated_capital_required = target_notional * (
        2.0 * config.margin_fraction_per_leg
        + config.collateral_reserve_fraction
    )
    actionable_profit_threshold = max(
        config.minimum_net_profit,
        estimated_capital_required * config.minimum_net_return_bps / 10_000.0,
    )
    quick_best_case_net_profit = target_notional * best_net_rate
    material_gross_rate = 0.0
    emergency_gross_rate = dynamic_emergency_gross_rate(
        best_cost_rate,
        config,
    )
    coverage_score = min(cost_coverage, 1.0)
    gross_score = min(
        safe_divide(gross_rate, max(emergency_gross_rate, 1e-9)),
        1.0,
    )
    liquidity_score = quick_liquidity_score(quick_liquidity(candidate))
    urgency_score = quick_settlement_urgency_score(candidate)
    opportunity_score = 100.0 * (
        0.45 * coverage_score
        + 0.25 * gross_score
        + 0.20 * liquidity_score
        + 0.10 * urgency_score
    )
    return {
        "quick_cost_coverage": cost_coverage,
        "quick_opportunity_gross_rate": float(
            candidate.get("quick_opportunity_gross_rate") or funding_rate
        ),
        "quick_opportunity_best_case_net_rate": best_net_rate,
        "quick_best_case_net_rate": best_net_rate,
        "quick_best_case_net_profit": quick_best_case_net_profit,
        "quick_actionable_profit_threshold": actionable_profit_threshold,
        "quick_actionable_net": (
            quick_best_case_net_profit >= actionable_profit_threshold
        ),
        "quick_opportunity_score": opportunity_score,
        "quick_liquidity_score": liquidity_score,
        "quick_settlement_urgency_score": urgency_score,
        "quick_material_gross_rate": material_gross_rate,
        "quick_material_gross_bps": material_gross_rate * 10_000.0,
        "quick_material_gross": gross_rate > 0.0,
        "quick_dynamic_emergency_gross_rate": emergency_gross_rate,
        "quick_dynamic_emergency_gross_bps": emergency_gross_rate * 10_000.0,
        "quick_dynamic_gross_emergency": (
            emergency_gross_rate > 0 and gross_rate >= emergency_gross_rate
        ),
        "quick_extreme_gross": (
            emergency_gross_rate > 0 and gross_rate >= emergency_gross_rate
        ),
        "quick_has_positive_funding_edge": funding_rate > 0.0,
        "quick_has_positive_spread_edge": spread_rate > 0.0,
        "quick_has_positive_edge": positive_edge_rate > 0.0,
        "quick_positive_edge_rate": positive_edge_rate,
    }


def dynamic_emergency_gross_rate(
    best_cost_rate: float,
    config: FundingScanConfig,
) -> float:
    return max(
        config.adaptive_near_miss_emergency_floor_bps / 10_000.0,
        max(0.0, best_cost_rate)
        * config.adaptive_near_miss_emergency_cost_multiplier,
    )


def candidate_market_keys(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for market in (candidate.get("long_market"), candidate.get("short_market")):
        if not market:
            continue
        venue = str(market.get("venue") or "")
        symbol = str(market.get("symbol") or "")
        if venue and symbol:
            keys.append((venue, symbol))
    return keys


def quick_liquidity_score(liquidity_usd: float) -> float:
    liquidity = max(0.0, float(liquidity_usd or 0.0))
    if liquidity <= 0:
        return 0.0
    return max(0.0, min(math.log10(liquidity / 100_000.0 + 1.0) / 3.0, 1.0))


def quick_settlement_urgency_score(candidate: dict[str, Any]) -> float:
    lead_seconds = candidate.get("quick_settlement_lead_seconds")
    if lead_seconds is None:
        return 0.0
    lead_hours = max(0.0, float(lead_seconds) / 3_600.0)
    return max(0.0, min(1.0 - lead_hours / 8.0, 1.0))


def safe_divide(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def funding_universe_row(
    candidate: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    long_market = candidate["long_market"]
    short_market = candidate["short_market"]
    return {
        "canonical_asset": candidate["canonical_asset"],
        "long_venue": long_market["venue"],
        "long_symbol": long_market["symbol"],
        "short_venue": short_market["venue"],
        "short_symbol": short_market["symbol"],
        "current_hourly_spread": candidate["current_hourly_spread"],
        "quick_gross_rate": candidate.get("quick_gross_rate", 0.0),
        "quick_taker_cost_rate": candidate.get(
            "quick_taker_unavoidable_cost_rate",
            candidate.get("quick_cost_rate", 0.0),
        ),
        "quick_maker_cost_rate": candidate.get(
            "quick_maker_unavoidable_cost_rate",
            candidate.get("quick_cost_rate", 0.0),
        ),
        "quick_best_case_net_rate": candidate.get(
            "quick_best_case_net_rate",
            candidate.get("quick_net_rate", 0.0),
        ),
        "quick_schedule_ready": candidate.get("quick_schedule_ready", False),
        "execution_eligible": candidate.get("execution_eligible", False),
        "execution_screen_reason": candidate.get(
            "execution_screen_reason",
            "not_screened",
        ),
        "observed_at": observed_at,
    }


def diversified_preselection(
    candidates: list[dict[str, Any]],
    maximum: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str, str]] = set()

    def add(row: dict[str, Any]) -> None:
        key = candidate_key(row)
        if key not in selected_keys and len(selected) < maximum:
            selected.append(row)
            selected_keys.add(key)

    # A genuinely positive quick route must not disappear behind merely larger
    # but fee-negative headline spreads.
    for row in candidates:
        if bool(row.get("quick_schedule_ready", True)) and float(
            row.get("quick_net_rate") or 0.0
        ) > 0:
            add(row)

    # Preserve at least one representative of each venue pair when capacity allows.
    venue_pair_leaders: dict[tuple[str, str], dict[str, Any]] = {}
    for row in candidates:
        pair = tuple(
            sorted(
                (
                    str(row["long_market"]["venue"]),
                    str(row["short_market"]["venue"]),
                )
            )
        )
        venue_pair_leaders.setdefault(pair, row)
    for row in venue_pair_leaders.values():
        add(row)

    for row in candidates:
        add(row)
    return selected


def quick_route_economics(
    candidate: dict[str, Any],
    config: FundingScanConfig | None,
    observed_at: str | None,
) -> dict[str, Any]:
    if config is None or observed_at is None:
        return {}
    long_market = candidate["long_market"]
    short_market = candidate["short_market"]
    if long_market.get("hourly_funding_rate") is None or short_market.get("hourly_funding_rate") is None:
        return {}
    schedule = paired_settlement_schedule(
        long_market,
        short_market,
        observed_at,
        config,
    )
    actual_gross_rate = 0.0
    for event in schedule.get("settlements", []):
        actual_gross_rate += (
            float(short_market["hourly_funding_rate"])
            * float(event.get("short_settlement_interval_hours") or 0.0)
            - float(long_market["hourly_funding_rate"])
            * float(event.get("long_settlement_interval_hours") or 0.0)
        )
    projection_hours = normalized_projection_hours(long_market, short_market, schedule)
    projected_gross_rate = (
        float(short_market["hourly_funding_rate"])
        - float(long_market["hourly_funding_rate"])
    ) * projection_hours
    projection_used = should_use_normalized_projection(
        schedule,
        long_market,
        short_market,
    )
    raw_gross_rate = (
        projected_gross_rate
        if projection_used
        else actual_gross_rate
    )
    taker_fee_rate = 2.0 * (
        market_fee_rate(long_market) + market_fee_rate(short_market)
    )
    maker_assisted_fee_rate = (
        market_fee_rate(long_market, "maker")
        + market_fee_rate(short_market, "maker")
        + market_fee_rate(long_market, "taker")
        + market_fee_rate(short_market, "taker")
    )
    # Signed executable basis is evaluated with full books. Charging the
    # absolute mark/index gap here would discard routes where funding covers an
    # adverse basis move or where convergence is favorable.
    basis_reserve_rate = config.basis_reserve_bps / 10_000.0
    operations_rate = config.operations_buffer_bps / 10_000.0
    taker_unavoidable_cost_rate = (
        taker_fee_rate + basis_reserve_rate + operations_rate
    )
    maker_unavoidable_cost_rate = (
        maker_assisted_fee_rate + basis_reserve_rate + operations_rate
    )
    best_case_cost_rate = min(
        taker_unavoidable_cost_rate,
        maker_unavoidable_cost_rate,
    )
    schedule_ready = int(schedule.get("settlement_event_count") or 0) > 0 or (
        projection_used and projection_hours > 0
    )
    settlement_lead_seconds = min(
        (
            float(row.get("hours_from_start") or 0.0) * 3_600.0
            for row in schedule.get("settlements", [])
        ),
        default=None,
    )
    identity_gap = quick_price_identity_gap(long_market, short_market)
    maximum_identity_gap = (
        config.maximum_research_basis_gap_bps / 10_000.0
        if config is not None
        else 2_000.0 / 10_000.0
    )
    identity_mismatch = (
        identity_gap is not None and identity_gap > maximum_identity_gap
    )
    return {
        "quick_gross_rate": raw_gross_rate,
        "quick_actual_cashflow_gross_rate": actual_gross_rate,
        "quick_projected_gross_rate": projected_gross_rate,
        "quick_projection_hours": projection_hours,
        "quick_projection_used": projection_used,
        "quick_cost_rate": taker_unavoidable_cost_rate,
        "quick_net_rate": raw_gross_rate
        - taker_unavoidable_cost_rate,
        "quick_taker_unavoidable_cost_rate": taker_unavoidable_cost_rate,
        "quick_maker_unavoidable_cost_rate": maker_unavoidable_cost_rate,
        "quick_best_case_cost_rate": best_case_cost_rate,
        "quick_funding_best_case_net_rate": raw_gross_rate - best_case_cost_rate,
        "quick_schedule_ready": schedule_ready,
        "quick_settlement_lead_seconds": settlement_lead_seconds,
        "quick_identity_gap": identity_gap,
        "quick_identity_mismatch": identity_mismatch,
        **quick_spread_opportunity_fields(
            long_market,
            short_market,
            raw_gross_rate,
            best_case_cost_rate,
        ),
    }


def quick_spread_opportunity_fields(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    funding_gross_rate: float,
    best_case_cost_rate: float,
) -> dict[str, float]:
    signed_spread_rate = quick_signed_spread_rate(long_market, short_market)
    opportunity_gross_rate = float(funding_gross_rate) + signed_spread_rate
    opportunity_best_case_net_rate = (
        opportunity_gross_rate - float(best_case_cost_rate)
    )
    return {
        "quick_signed_spread_rate": signed_spread_rate,
        "quick_opportunity_gross_rate": opportunity_gross_rate,
        "quick_opportunity_best_case_net_rate": opportunity_best_case_net_rate,
        # The existing DB/UI column stores the best quick net score. With the
        # opportunity engine enabled that score includes signed spread, while
        # quick_funding_best_case_net_rate keeps funding-only diagnostics.
        "quick_best_case_net_rate": opportunity_best_case_net_rate,
        "quick_positive_edge_rate": max(0.0, float(funding_gross_rate))
        + max(0.0, signed_spread_rate),
    }


def normalized_projection_hours(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    schedule: dict[str, Any],
) -> float:
    horizon_hours = float(schedule.get("horizon_hours") or 0.0)
    long_interval = float(long_market.get("funding_interval_hours") or 0.0)
    short_interval = float(short_market.get("funding_interval_hours") or 0.0)
    return max(horizon_hours, long_interval, short_interval, 1.0)


def should_use_normalized_projection(
    schedule: dict[str, Any],
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> bool:
    if schedule.get("horizon_mode") == "next_settlement":
        return False
    long_interval = float(long_market.get("funding_interval_hours") or 0.0)
    short_interval = float(short_market.get("funding_interval_hours") or 0.0)
    horizon_hours = float(schedule.get("horizon_hours") or 0.0)
    if max(long_interval, short_interval) <= horizon_hours:
        return False
    return (
        int(schedule.get("long_settlement_count") or 0) == 0
        or int(schedule.get("short_settlement_count") or 0) == 0
        or float(schedule.get("paired_coverage_hours") or 0.0) <= 0
    )


def quick_liquidity(candidate: dict[str, Any]) -> float:
    values = []
    for market in (candidate.get("long_market"), candidate.get("short_market")):
        if not market:
            values.append(0.0)
            continue
        try:
            values.append(float(market.get("volume_24h_usd") or 0.0))
        except (TypeError, ValueError):
            values.append(0.0)
    return min(values) if values else 0.0


def quick_price_identity_gap(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> float | None:
    long_price = quick_reference_price(long_market)
    short_price = quick_reference_price(short_market)
    if long_price <= 0 or short_price <= 0:
        return None
    reference = (long_price + short_price) / 2.0
    if reference <= 0:
        return None
    return abs(short_price - long_price) / reference


def quick_signed_spread_rate(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> float:
    long_price = quick_reference_price(long_market)
    short_price = quick_reference_price(short_market)
    if long_price <= 0 or short_price <= 0:
        return 0.0
    reference = (long_price + short_price) / 2.0
    if reference <= 0:
        return 0.0
    return (short_price - long_price) / reference


def quick_reference_price(market: dict[str, Any]) -> float:
    for name in ("mark_price", "index_price"):
        try:
            value = float(market.get(name) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def pair_candidate(
    asset: str,
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    preserve_order: bool = False,
) -> dict[str, Any] | None:
    first_rate = first.get("hourly_funding_rate")
    second_rate = second.get("hourly_funding_rate")
    if first_rate is None or second_rate is None:
        return None
    if preserve_order or float(first_rate) <= float(second_rate):
        long_market, short_market = first, second
    else:
        long_market, short_market = second, first
    return {
        "canonical_asset": asset,
        "long_market": long_market,
        "short_market": short_market,
        "current_hourly_spread": (
            float(second_rate) - float(first_rate)
        ),
    }


def best_pair_orientation(
    asset: str,
    first: dict[str, Any],
    second: dict[str, Any],
    config: FundingScanConfig | None,
    observed_at: str | None,
) -> dict[str, Any]:
    """Choose direction by scheduled cash flow, not only hourly-rate order.

    When venues settle at different times, the lower hourly funding leg is not
    necessarily the profitable long before the first revalidation checkpoint.
    """
    if config is None or observed_at is None:
        result = pair_candidate(asset, first, second)
        return result if result is not None else {}

    orientations = []
    for long_market, short_market in ((first, second), (second, first)):
        candidate = pair_candidate(
            asset,
            long_market,
            short_market,
            preserve_order=True,
        )
        if candidate is None:
            continue
        candidate.update(quick_route_economics(candidate, config, observed_at))
        orientations.append(candidate)
    if not orientations:
        return {}
    return max(
        orientations,
        key=lambda row: (
            bool(row.get("quick_schedule_ready")),
            float(row.get("quick_opportunity_best_case_net_rate") or 0.0),
            float(row.get("quick_positive_edge_rate") or 0.0),
            float(row.get("quick_gross_rate") or 0.0),
            float(row.get("current_hourly_spread") or 0.0),
        ),
    )


def candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(candidate["canonical_asset"]),
        str(candidate["long_market"]["venue"]),
        str(candidate["short_market"]["venue"]),
    )


def scan_ranked_pairs(
    candidates: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    history: dict[tuple[str, str], list[dict[str, Any]]],
    observed_at: str,
    config: FundingScanConfig,
) -> list[dict[str, Any]]:
    routes = []
    for candidate in candidates:
        long_market = candidate["long_market"]
        short_market = candidate["short_market"]
        long_key = (str(long_market["venue"]), str(long_market["symbol"]))
        short_key = (str(short_market["venue"]), str(short_market["symbol"]))
        long_book = books.get(long_key)
        short_book = books.get(short_key)
        if not long_book or not short_book:
            continue
        routes.append(
            evaluate_perp_route(
                long_market,
                short_market,
                long_book,
                short_book,
                history.get(long_key, []),
                history.get(short_key, []),
                observed_at,
                config,
            )
        )
    routes.sort(
        key=lambda row: (
            row["status"] == "paper_candidate",
            route_opportunity_sort_net(row),
            float(
                (row.get("evidence") or {}).get("conservative_net_profit") or 0
            ),
            float(
                (row.get("evidence") or {}).get("net_profit_probability") or 0
            ),
            float(row["expected_net_profit"]),
            float(row["current_hourly_spread"]),
        ),
        reverse=True,
    )
    return routes


def route_opportunity_sort_net(route: dict[str, Any]) -> float:
    evidence = route.get("evidence") or {}
    selected = evidence.get("selected_strategy") or {}
    for value in (
        selected.get("expected_net_pnl"),
        evidence.get("current_opportunity_net"),
        evidence.get("current_nowcast_net"),
        route.get("expected_net_profit"),
    ):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return 0.0
