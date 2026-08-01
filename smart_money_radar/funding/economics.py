from __future__ import annotations

import hashlib
import math
from typing import Any

from smart_money_radar.funding.forecast import (
    build_funding_forecast,
    effective_sample_size,
    paired_settlement_schedule,
    weighted_mean,
    weighted_quantile,
)
from smart_money_radar.funding.models import (
    FundingScanConfig,
)
from smart_money_radar.funding.fees import (
    funding_fee_rate,
    funding_fee_source,
    verified_vip1_fee_profile,
)
from smart_money_radar.funding.liquidity import build_route_liquidity_profile
from smart_money_radar.funding.normalization import (
    canonical_asset_symbol,
    funding_persistence,
    parse_timestamp,
)
from smart_money_radar.funding.readiness_policy import (
    EvaluationMode,
    evaluate_synchronized_route,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    synchronized_strategy_candidate,
)


MAX_ABSOLUTE_HOURLY_FUNDING_RATE = 0.02
MAX_ABSOLUTE_INTERVAL_FUNDING_RATE = 0.08
STRATEGY_ECONOMIC_BLOCKERS = {
    "basis_not_covered_by_funding",
    "basis_not_covered_by_live_funding",
    "conservative_net_after_costs_too_low",
    "forecast_median_net_negative",
    "funding_direction_unstable",
    "insufficient_forward_samples",
    "live_net_pnl_below_actionable_threshold",
    "live_net_pnl_not_positive",
    "profit_probability_too_low",
}


def evaluate_perp_route(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    long_book: dict[str, Any],
    short_book: dict[str, Any],
    long_history: list[dict[str, Any]],
    short_history: list[dict[str, Any]],
    observed_at: str,
    config: FundingScanConfig,
) -> dict[str, Any]:
    long_market = market_with_book_mark(long_market, long_book)
    short_market = market_with_book_mark(short_market, short_book)
    books_valid = valid_orderbook(long_book) and valid_orderbook(short_book)
    capacity = 0.0
    if books_valid:
        executable_quantity = min(
            displayed_side_quantity(long_book.get("asks", [])),
            displayed_side_quantity(short_book.get("bids", [])),
            displayed_side_quantity(long_book.get("bids", [])),
            displayed_side_quantity(short_book.get("asks", [])),
        ) * config.depth_haircut
        conservative_price = min(
            positive_float(long_book.get("mid_price")),
            positive_float(short_book.get("mid_price")),
        )
        capacity = executable_quantity * conservative_price
    maximum_notional = min(config.target_notional, capacity)
    current_hourly_spread = (
        float(short_market["hourly_funding_rate"])
        - float(long_market["hourly_funding_rate"])
    )
    persistence = funding_persistence(long_history, short_history)
    history_ready = persistence["history_point_count"] >= config.minimum_history_points
    history_coverage = float(persistence["history_coverage_fraction"])
    history_age_hours = funding_history_age_hours(persistence, observed_at)
    history_dense = history_coverage >= config.minimum_history_coverage
    history_age_limit_hours = min(
        config.maximum_history_age_hours,
        max(
            2.0,
            1.0
            + 1.5
            * max(
                float(long_market.get("funding_interval_hours") or 1.0),
                float(short_market.get("funding_interval_hours") or 1.0),
            ),
        ),
    )
    history_recent = (
        history_age_hours is not None
        and history_age_hours <= history_age_limit_hours
    )
    nowcast_age_seconds = max(
        market_snapshot_age_seconds(long_market, observed_at),
        market_snapshot_age_seconds(short_market, observed_at),
    )
    nowcast_recent = nowcast_age_seconds <= config.maximum_nowcast_age_seconds
    schedule = paired_settlement_schedule(
        long_market,
        short_market,
        observed_at,
        config,
    )
    horizon_hours = float(schedule["horizon_hours"])
    settlement_event_count = int(schedule.get("settlement_event_count") or 0)
    settlement_lead_seconds = min(
        (
            float(row.get("hours_from_start") or 0.0) * 3_600.0
            for row in schedule.get("settlements", [])
        ),
        default=None,
    )
    both_legs_settle = (
        int(schedule.get("long_settlement_count") or 0) > 0
        and int(schedule.get("short_settlement_count") or 0) > 0
    )
    interval_projection = normalized_interval_projection(
        long_market,
        short_market,
        schedule,
    )
    forecast = build_funding_forecast(
        long_history,
        short_history,
        current_hourly_spread,
        schedule,
        config,
        long_market=long_market,
        short_market=short_market,
        observed_at=observed_at,
    )
    median_spread = float(forecast["historical_median_hourly_spread"])
    positive_fraction = float(forecast["positive_spread_fraction"])
    projected_hourly_spread = (
        float(forecast["decay_cumulative_rate"]) / horizon_hours
        if horizon_hours > 0
        else 0.0
    )

    fee_long = market_fee_rate(long_market)
    fee_short = market_fee_rate(short_market)
    reference_prices_valid = valid_reference_prices(long_market) and valid_reference_prices(short_market)
    reference_basis_gap = relative_basis_gap(long_market, short_market)
    basis_history = build_basis_history_profile(
        long_book,
        short_book,
        float(schedule["horizon_hours"]),
    )
    basis_reserve_rate = config.basis_reserve_bps / 10_000.0
    schedule_ready = settlement_event_count > 0 or bool(
        interval_projection["projection_used"]
    )
    decision_mode = (
        "settlement_capture"
        if config.horizon_mode == "next_settlement"
        else "persistent_carry"
    )
    current_nowcast_settlement_rate = current_schedule_carry_rate(
        schedule,
        long_market,
        short_market,
    )
    sizing_rows = [
        add_live_settlement_economics(
            notional_economics(
                trial_notional,
                long_book,
                short_book,
                fee_long,
                fee_short,
                forecast,
                float(schedule["horizon_hours"]),
                basis_reserve_rate,
                config,
                basis_history,
            ),
            current_nowcast_settlement_rate,
        )
        for trial_notional in notional_trials(
            maximum_notional,
            config.minimum_market_capacity,
        )
    ]
    historical_passing_sizes = [
        row
        for row in sizing_rows
        if row["fill_complete"]
        and row["conservative_net_profit"] > 0
        and row["meets_basis_coverage_gate"]
        and row["net_profit_probability"] >= config.minimum_profit_probability
    ]
    live_passing_sizes = [
        row
        for row in sizing_rows
        if row["fill_complete"]
        and row["current_nowcast_net"] > 0
    ]
    selected_size = (
        select_live_sizing_row(sizing_rows, live_passing_sizes)
        if decision_mode == "settlement_capture"
        else select_sizing_row(sizing_rows, historical_passing_sizes)
    )
    notional = float(selected_size["notional"])
    long_open = selected_size["long_open"]
    short_open = selected_size["short_open"]
    long_close = selected_size["long_close"]
    short_close = selected_size["short_close"]
    long_open_notional = float(selected_size["long_open_notional"])
    short_open_notional = float(selected_size["short_open_notional"])
    leg_notional_imbalance = notional_imbalance_ratio(
        long_open_notional,
        short_open_notional,
    )
    fill_complete = bool(selected_size["fill_complete"])
    slippage_cost = float(selected_size["slippage_cost"])
    total_fees = float(selected_size["total_fees"])
    basis_reserve = float(selected_size["basis_reserve"])
    basis_model = dict(selected_size["basis_model"])
    signed_entry_basis = float(basis_model["signed_entry_basis"])
    basis_gap = abs(signed_entry_basis)
    large_basis = basis_gap > config.large_basis_threshold_bps / 10_000.0
    basis_stress_loss = float(selected_size["basis_stress_loss"])
    basis_stress_net_profit = float(selected_size["basis_stress_net_profit"])
    basis_coverage_ratio = float(selected_size["basis_coverage_ratio"])
    meets_basis_coverage_gate = bool(selected_size["meets_basis_coverage_gate"])
    operations_buffer = float(selected_size["operations_buffer"])
    max_book_walk_bps = float(selected_size["max_book_walk_bps"])
    expected_gross_funding = float(selected_size["expected_gross_funding"])
    expected_net_profit = float(selected_size["expected_net_profit"])
    conservative_net_profit = float(selected_size["conservative_net_profit"])
    net_profit_probability = float(selected_size["net_profit_probability"])
    empirical_net_profit_probability = float(
        selected_size["empirical_net_profit_probability"]
    )
    capital_required = float(selected_size["capital_required"])
    net_roc_annualized = float(selected_size["net_roc_annualized"])
    net_roc_horizon = float(selected_size["net_roc_horizon"])
    actionable_profit_threshold = float(
        selected_size["actionable_profit_threshold"]
    )
    current_nowcast_gross = float(selected_size["current_nowcast_gross"])
    current_nowcast_net = float(selected_size["current_nowcast_net"])
    current_opportunity_net = float(selected_size["current_opportunity_net"])
    current_opportunity_basis_stress_net = float(
        selected_size["current_opportunity_basis_stress_net"]
    )
    current_basis_stress_net_profit = float(
        selected_size["current_basis_stress_net_profit"]
    )
    meets_live_actionable_profit_gate = bool(
        selected_size["meets_live_actionable_profit_gate"]
    )
    meets_live_basis_coverage_gate = bool(
        selected_size["meets_live_basis_coverage_gate"]
    )
    meets_live_opportunity_basis_coverage_gate = bool(
        selected_size["meets_live_opportunity_basis_coverage_gate"]
    )
    liquidity_profile = build_route_liquidity_profile(
        long_book,
        short_book,
        notional,
        config.maker_timeout_seconds,
        config.maker_fill_probability,
        config.minimum_maker_observations,
    )
    maker_scenario = maker_entry_scenario(
        notional,
        long_market,
        short_market,
        long_book,
        short_book,
        forecast,
        basis_reserve_rate,
        config,
        liquidity_profile,
        current_nowcast_settlement_rate,
    )
    needed_improvement = route_needed_improvement(
        selected_size,
        maker_scenario,
        long_market,
        short_market,
        current_nowcast_gross,
        current_opportunity_net,
        current_opportunity_basis_stress_net,
        actionable_profit_threshold,
        basis_stress_loss,
        basis_gap,
    )
    current_gross_apr = current_hourly_spread * 24.0 * 365.0
    projected_gross_apr = projected_hourly_spread * 24.0 * 365.0
    asset = canonical_asset_symbol(long_market.get("canonical_asset"))
    long_venue = str(long_market["venue"])
    short_venue = str(short_market["venue"])
    fee_sources = {
        long_venue: funding_fee_source(long_market),
        short_venue: funding_fee_source(short_market),
    }
    risk_flags = [
        "non_atomic_two_venue_execution",
        "ticker_identity_unverified",
    ]
    if any(source != "account_override" for source in fee_sources.values()):
        risk_flags.append("public_fee_assumptions")
    if (
        market_funding_rate_at_cap_or_floor(long_market)
        or market_funding_rate_at_cap_or_floor(short_market)
    ):
        risk_flags.append("funding_rate_at_cap_or_floor")
    if max_book_walk_bps > 100.0:
        risk_flags.append("deep_book_walk_dependency")
    liquidity_history_ready = (
        int(liquidity_profile["minimum_snapshot_count"])
        >= config.minimum_liquidity_snapshots
    )
    if not liquidity_history_ready:
        risk_flags.append("insufficient_orderbook_sequence")
    elif float(liquidity_profile["route_executable_fraction"]) < 0.70:
        risk_flags.append("fragile_depth_persistence")
    refill_probability = liquidity_profile.get("refill_probability")
    if (
        int(liquidity_profile.get("refill_event_count") or 0) >= 3
        and refill_probability is not None
        and float(refill_probability) < 0.50
    ):
        risk_flags.append("weak_depth_refill")
    blocking_reasons: list[str] = []
    advisory_reasons: list[str] = []
    blocking_risk_flags: list[str] = []
    advisory_risk_flags: list[str] = []

    def block(flag: str, reason: str) -> None:
        risk_flags.append(flag)
        blocking_risk_flags.append(flag)
        blocking_reasons.append(reason)

    def advise(flag: str, reason: str) -> None:
        risk_flags.append(flag)
        advisory_risk_flags.append(flag)
        advisory_reasons.append(reason)

    def assess_history(flag: str, reason: str) -> None:
        if decision_mode == "settlement_capture":
            advise(flag, reason)
        else:
            block(flag, reason)

    funding_unit_outlier = market_funding_rate_unit_outlier(
        long_market
    ) or market_funding_rate_unit_outlier(short_market)
    unit_identity_mismatch = (
        leg_notional_imbalance is not None
        and leg_notional_imbalance >= 3.0
    )

    if not books_valid:
        block(
            "invalid_orderbook",
            "Стакан пуст, пересечен или не содержит корректный bid/ask.",
        )
    elif capacity < config.minimum_market_capacity or not fill_complete:
        block(
            "insufficient_two_sided_depth",
            "Недостаточная двухсторонняя глубина для входа и выхода.",
        )
    if not reference_prices_valid:
        block(
            "missing_reference_price",
            "Нет корректных mark/index цен для проверки basis.",
        )
    if not nowcast_recent:
        block(
            "stale_funding_nowcast",
            "Текущий funding nowcast одной из площадок устарел.",
        )
    if funding_unit_outlier:
        block(
            "funding_rate_unit_outlier",
            "Funding rate выходит за sanity cap; вероятна ошибка единиц API или ручной override биржи.",
        )
    if not schedule_ready:
        block(
            "funding_schedule_unavailable",
            "В выбранном горизонте нет подтвержденного funding cash flow.",
        )
    elif interval_projection["projection_used"]:
        advise(
            "normalized_interval_projection",
            "Funding сравнен как нормализованная проекция на общий interval; фактический cash flow внутри выбранного горизонта может быть неполным.",
        )
    elif (
        settlement_lead_seconds is not None
        and settlement_lead_seconds < config.minimum_settlement_lead_seconds
    ):
        block(
            "insufficient_settlement_lead_time",
            "До ближайшего settlement недостаточно времени для проверки и открытия обеих ног.",
        )
    if not history_ready:
        assess_history(
            "insufficient_funding_history",
            "Истории funding недостаточно: вероятность сохранения текущего spread оценена с низкой уверенностью.",
        )
    if not history_dense:
        advise(
            "sparse_funding_history",
            "История funding разрежена; прогноз схлопывания spread менее надёжен.",
        )
    if not history_recent:
        assess_history(
            "stale_funding_history",
            "Последний совместный funding interval устарел; исторический прогноз может не отражать текущий режим.",
        )
    window_readiness = forecast.get("required_window_readiness") or {}
    missing_windows = [
        name for name, ready in window_readiness.items() if not ready
    ]
    if missing_windows:
        assess_history(
            "insufficient_forecast_windows",
            "Недостаточно истории для прогнозных окон: "
            + ", ".join(missing_windows)
            + "; live opportunity остаётся видимой, но confidence снижен.",
        )
    optional_windows = forecast.get("window_readiness") or {}
    if not bool(optional_windows.get("recent_72h")):
        risk_flags.append("limited_72h_context")
    if not bool(optional_windows.get("stability_14d")):
        risk_flags.append("limited_14d_context")
    if not bool(optional_windows.get("live_30d")):
        risk_flags.append("limited_30d_regime_context")
    if positive_fraction < config.minimum_persistence:
        assess_history(
            "funding_direction_unstable",
            "Исторически направление funding spread часто менялось и может схлопнуться до settlement.",
        )
    if int(forecast["sample_count"]) < config.minimum_forward_samples:
        assess_history(
            "insufficient_forward_samples",
            "Недостаточно независимых исторических исходов для надёжной оценки вероятности прибыли.",
        )
    if (
        large_basis
        and int(basis_history.get("change_sample_count") or 0)
        < config.minimum_liquidity_snapshots
    ):
        assess_history(
            "insufficient_basis_history",
            "Для большого executable basis недостаточно совместной L2-истории: "
            "paper-тест может проверить окно, но для real trade это риск фантомного или быстро исчезающего spread.",
        )
    if large_basis:
        risk_flags.append("large_basis_requires_stop_loss")
    if large_basis:
        if (
            decision_mode == "settlement_capture"
            and not meets_live_basis_coverage_gate
        ):
            advise(
                "basis_not_covered_by_live_funding",
                f"Текущий funding-only net не покрывает basis stress "
                f"и полные расходы: funding stress PnL "
                f"${current_basis_stress_net_profit:,.2f}. "
                f"Это warning для stop-loss, не veto для live opportunity.",
            )
        elif decision_mode != "settlement_capture" and not meets_basis_coverage_gate:
            block(
                "basis_not_covered_by_funding",
                f"Q25 funding не покрывает basis stress, fees и depth: "
                f"stress PnL ${basis_stress_net_profit:,.2f}.",
            )
    if unit_identity_mismatch:
        block(
            "unit_identity_mismatch",
            "Ноги требуют резко разный USD-notional для одной и той же base quantity; вероятно, у площадок разные contract units или scaled ticker.",
        )
    elif basis_gap > config.maximum_research_basis_gap_bps / 10_000.0:
        block(
            "basis_divergence",
            "Executable basis выше 2 000 bps; вероятна ошибка identity или единиц контракта.",
        )
    if decision_mode == "settlement_capture" and current_nowcast_net < 0:
        block(
            "live_net_pnl_not_positive",
            f"Текущий funding-only net PnL "
            f"${current_nowcast_net:,.2f} отрицательный.",
        )
    elif (
        decision_mode == "settlement_capture"
        and current_nowcast_net < actionable_profit_threshold
    ):
        block(
            "live_net_pnl_below_actionable_threshold",
            f"Текущий funding-only net PnL "
            f"${current_nowcast_net:,.2f} ниже минимально "
            f"значимой прибыли ${actionable_profit_threshold:,.2f}.",
        )
    if expected_net_profit <= 0:
        assess_history(
            "forecast_median_net_negative",
            f"Историческая медиана net PnL ${expected_net_profit:,.2f}; funding spread может схлопнуться и превратить текущую прибыль в убыток.",
        )
    if conservative_net_profit <= 0:
        assess_history(
            "conservative_net_after_costs_too_low",
            "Исторический Q25 PnL отрицательный; downside-сценарий не покрывает fees, depth и risk reserves.",
        )
    elif conservative_net_profit < actionable_profit_threshold:
        advise(
            "actionable_profit_too_low",
            f"Q25 net PnL ${conservative_net_profit:,.2f} ниже минимально "
            f"значимой прибыли ${actionable_profit_threshold:,.2f}; это предупреждение об устойчивости spread.",
        )
    if net_profit_probability < config.minimum_profit_probability:
        assess_history(
            "profit_probability_too_low",
            f"Консервативная историческая вероятность прибыли {net_profit_probability * 100:.1f}% ниже ориентира {config.minimum_profit_probability * 100:.0f}%; текущий spread может быстро исчезнуть.",
        )
    if config.research_only:
        block(
            "research_only_horizon",
            "72-часовой горизонт предназначен только для Research.",
        )
    if float(forecast["regime_age_penalty"]) < 0.75:
        risk_flags.append("mature_funding_regime")
    if abs(float(forecast.get("live_window_z_score") or 0.0)) >= 3.0:
        risk_flags.append("spread_30d_tail_anomaly")
    if abs(float(forecast.get("long_window_z_score") or 0.0)) >= 3.0:
        risk_flags.append("spread_90d_tail_anomaly")
    if float(forecast.get("recent_to_live_volatility_ratio") or 0.0) >= 2.0:
        risk_flags.append("spread_30d_volatility_regime_shift")
    if float(forecast.get("live_to_long_volatility_ratio") or 0.0) >= 2.0:
        risk_flags.append("spread_90d_volatility_regime_shift")
    if int(forecast["duration_sample_count"]) < 5:
        risk_flags.append("limited_regime_duration_history")
    risk_flags.append("account_margin_unverified")
    long_capability_market = {
        **long_market,
        "bids": long_book.get("bids") or [],
        "asks": long_book.get("asks") or [],
        "best_bid": long_book.get("best_bid"),
        "best_ask": long_book.get("best_ask"),
        "orderbook_response_received_at": long_book.get("response_received_at")
        or long_book.get("observed_at"),
        "orderbook_event_time": long_book.get("orderbook_event_time")
        or long_book.get("observed_at"),
        "orderbook_depth_available": valid_orderbook(long_book),
    }
    short_capability_market = {
        **short_market,
        "bids": short_book.get("bids") or [],
        "asks": short_book.get("asks") or [],
        "best_bid": short_book.get("best_bid"),
        "best_ask": short_book.get("best_ask"),
        "orderbook_response_received_at": short_book.get("response_received_at")
        or short_book.get("observed_at"),
        "orderbook_event_time": short_book.get("orderbook_event_time")
        or short_book.get("observed_at"),
        "orderbook_depth_available": valid_orderbook(short_book),
    }
    capability_check = evaluate_synchronized_route(
        long_market=long_capability_market,
        short_market=short_capability_market,
        target_notional=config.target_notional,
        mode=EvaluationMode.DISCOVERY,
        clients_by_venue={
            str(long_venue).lower(): True,
            str(short_venue).lower(): True,
        },
    )
    if decision_mode == "settlement_capture":
        blocking_risk_flags.extend(capability_check["hard_blockers"])
        advisory_risk_flags.extend(capability_check["risk_flags"])
        risk_flags.extend(capability_check["risk_flags"])

    if decision_mode == "settlement_capture":
        synchronized_candidate = synchronized_strategy_candidate(
            {
                "funding_notional": selected_size.get("funding_notional", notional),
                "execution_cost": selected_size.get("execution_cost", total_fees + slippage_cost),
                "basis_stress_loss": basis_stress_loss,
            },
            current_funding_gross=current_nowcast_gross,
            actionable_profit_threshold=actionable_profit_threshold,
            blocking_risk_flags=blocking_risk_flags,
            decision_mode=decision_mode,
        )
        strategy_evaluation = {
            "strategy_candidates": [synchronized_candidate],
            "selected_strategy": synchronized_candidate if synchronized_candidate.get("eligible") else None,
            "pnl_components": {
                "funding_pnl_component": synchronized_candidate.get("funding_pnl_component", 0.0),
                "spread_pnl_component": 0.0,
                "signed_spread_pnl_component": 0.0,
                "spread_convergence_component": 0.0,
                "execution_cost": synchronized_candidate.get("execution_cost", 0.0),
                "funding_only_net_pnl": current_nowcast_net,
                "spread_total_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "combined_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "opportunistic_any_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "opportunity_expected_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "opportunity_risk_adjusted_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "opportunistic_risk_adjusted_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "basis_stress_loss": basis_stress_loss,
                "incremental_basis_stress_loss": basis_stress_loss,
                "funding_basis_stress_net_pnl": current_basis_stress_net_profit,
                "spread_basis_stress_net_pnl": synchronized_candidate.get("expected_net_pnl", 0.0),
                "positive_edge_pnl": synchronized_candidate.get("funding_pnl_component", 0.0),
                "drag_pnl": synchronized_candidate.get("execution_cost", 0.0) + basis_stress_loss,
                "coverage_ratio": synchronized_candidate.get("coverage_ratio", 0.0),
            },
        }
        selected_strategy = synchronized_candidate
        status = "research_only" if capability_check["hard_blockers"] else "watch"
    else:
        strategy_evaluation = build_strategy_evaluation(
            selected_size,
            current_nowcast_gross,
            current_nowcast_net,
            current_basis_stress_net_profit,
            actionable_profit_threshold,
            blocking_risk_flags,
            decision_mode,
        )
        selected_strategy = strategy_evaluation["selected_strategy"]
        legacy_paper_candidate = not blocking_reasons
        strategy_paper_candidate = bool(
            selected_strategy and selected_strategy.get("eligible")
        )
        status = "paper_candidate" if legacy_paper_candidate else "watch"
    current_depth_score = min(1.0, capacity / max(config.target_notional, 1.0))
    sequence_depth_score = (
        float(liquidity_profile["route_persistence_score"]) / 100.0
        if liquidity_history_ready
        else current_depth_score
    )
    depth_score = min(current_depth_score, sequence_depth_score)
    basis_score = max(
        0.0,
        1.0 - basis_gap / max(config.maximum_basis_gap_bps / 10_000.0, 1e-9),
    )
    economics_score = max(
        0.0,
        min(
            1.0,
            conservative_net_profit
            / max(2.0 * actionable_profit_threshold, 1e-9),
        ),
    )
    probability_score = max(0.0, min(1.0, net_profit_probability))
    confidence_score = 100.0 * (
        0.35 * float(persistence["persistence_score"]) / 100.0
        + 0.2 * depth_score
        + 0.15 * basis_score
        + 0.15 * economics_score
        + 0.15 * probability_score
    )
    route_key = perp_route_key(asset, long_venue, short_venue)
    remaining_hours = forecast.get("estimated_remaining_hours_median")
    survival_probability = forecast.get("selected_horizon_survival_probability")
    duration_rationale = (
        f"При текущем возрасте режима историческая медиана остатка "
        f"{float(remaining_hours):.1f} ч.; вероятность пережить весь горизонт "
        f"{float(survival_probability) * 100:.1f}%."
        if remaining_hours is not None and survival_probability is not None
        else "Истории завершённых сопоставимых funding-режимов пока мало для duration forecast."
    )
    decision_net_profit = (
        float(selected_strategy.get("expected_net_pnl"))
        if selected_strategy and selected_strategy.get("expected_net_pnl") is not None
        else current_nowcast_net
    )
    decision_edge_label = (
        str(selected_strategy.get("edge_label"))
        if selected_strategy and selected_strategy.get("edge_label")
        else "funding-only"
    )
    if current_hourly_spread >= 0:
        orientation_rationale = [
            f"Long {long_venue}: funding ниже на нормализованной почасовой базе.",
            f"Short {short_venue}: funding выше; gross spread {current_hourly_spread * 10_000:.3f} bps/ч.",
        ]
    else:
        orientation_rationale = [
            f"Long {long_venue}: направление выбрано по ближайшему settlement cash flow, а не по одной почасовой ставке.",
            f"Short {short_venue}: расписание дает live carry {current_nowcast_settlement_rate * 10_000:.3f} bps до обязательной revalidation.",
        ]
    rationale = [
        *orientation_rationale,
        (
            f"Нижняя 90%-граница P(net > 0) равна {net_profit_probability * 100:.1f}% "
            f"при empirical {empirical_net_profit_probability * 100:.1f}% "
            f"на {forecast['sample_count']} point-in-time settlement-исходах."
        ),
        (
            f"Sizing выбрал одинаковые {float(selected_size['target_quantity']):,.6f} "
            f"единиц базового актива: LONG ${long_open_notional:,.0f}, "
            f"SHORT ${short_open_notional:,.0f}; учтены все "
            f"уровни стаканов и book walk {max_book_walk_bps:.1f} bps."
        ),
        (
            f"Signed executable basis {signed_entry_basis * 10_000:.1f} bps "
            f"({basis_model['tier']}); неблагоприятное схождение включено в Q25 "
            f"вместе с funding cash flow. Basis stress loss ${basis_stress_loss:,.2f}, "
            f"coverage {basis_coverage_ratio:.2f}x."
        ),
        (
            f"Settlement-capture gate использует funding-only net "
            f"({decision_edge_label}); сейчас ${decision_net_profit:,.2f}. "
            f"Минимально значимая прибыль "
            f"${actionable_profit_threshold:,.2f} используется как warning, не veto. "
            f"Исторический Q25 ${conservative_net_profit:,.2f} тоже предупреждение, а не veto."
            if decision_mode == "settlement_capture"
            else f"Persistent-carry gate требует положительный Q25; "
            f"минимально значимая прибыль ${actionable_profit_threshold:,.2f} "
            f"используется как warning. Расчетный Q25 "
            f"${conservative_net_profit:,.2f} на полном collateral обеих ног."
        ),
        (
            f"Прогноз пересчитан по settlement с half-life "
            f"{forecast['decay_half_life_hours']:.1f} ч. и regime-age penalty "
            f"{forecast['regime_age_penalty']:.2f}."
        ),
        dynamic_weight_rationale(forecast),
        duration_rationale,
        liquidity_rationale(liquidity_profile),
    ]
    if selected_strategy:
        rationale.append(strategy_rationale(selected_strategy))
    return {
        "route_key": route_key,
        "route_type": "perp_perp",
        "canonical_asset": asset,
        "venue_scope": "+".join(sorted((long_venue, short_venue))),
        "long_venue": long_venue,
        "long_symbol": long_market["symbol"],
        "short_venue": short_venue,
        "short_symbol": short_market["symbol"],
        "status": status,
        "confidence_score": max(0.0, min(confidence_score, 100.0)),
        "target_notional": notional,
        "market_capacity": capacity,
        "capital_required": capital_required,
        "horizon_days": float(schedule["horizon_hours"]) / 24.0,
        "current_hourly_spread": current_hourly_spread,
        "current_gross_apr": current_gross_apr,
        "projected_hourly_spread": projected_hourly_spread,
        "projected_gross_apr": projected_gross_apr,
        "historical_median_hourly_spread": median_spread,
        "positive_spread_fraction": positive_fraction,
        "persistence_score": persistence["persistence_score"],
        "history_point_count": persistence["history_point_count"],
        "expected_gross_funding": expected_gross_funding,
        "expected_net_profit": expected_net_profit,
        "net_roc_annualized": net_roc_annualized,
        "total_fees": total_fees,
        "slippage_cost": slippage_cost,
        "basis_gap": basis_gap,
        "basis_reserve": basis_reserve,
        "operations_buffer": operations_buffer,
        "long_next_funding_at": schedule.get("long_next_settlement_at")
        or long_market.get("next_funding_at"),
        "short_next_funding_at": schedule.get("short_next_settlement_at")
        or short_market.get("next_funding_at"),
        "observed_at": observed_at,
        "legs": [
            route_leg(
                "long",
                long_market,
                long_book,
                long_open,
                long_close,
                fee_long,
                float(selected_size["long_open_notional"]),
                schedule.get("long_next_settlement_at"),
            ),
            route_leg(
                "short",
                short_market,
                short_book,
                short_open,
                short_close,
                fee_short,
                float(selected_size["short_open_notional"]),
                schedule.get("short_next_settlement_at"),
            ),
        ],
        "rationale": rationale,
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "evidence": {
            "decision_mode": decision_mode,
            "history_is_advisory": decision_mode == "settlement_capture",
            "strategy_candidates": strategy_evaluation["strategy_candidates"],
            "selected_strategy": selected_strategy,
            "strategy_classification": selected_strategy,
            "pnl_components": strategy_evaluation["pnl_components"],
            "readiness_level": capability_check.get("readiness_level"),
            "paper_mode": capability_check.get("paper_mode")
            or (
                "EXPERIMENTAL_SIMULATION"
                if (
                    capability_check.get("experimental_simulation_ready")
                    or capability_check.get("experimental_paper_ready")
                )
                else "RESEARCH"
            ),
            "funding_cashflow_status": capability_check.get("funding_cashflow_status"),
            "execution_status": capability_check.get("execution_status"),
            "settlement_semantics_status": capability_check.get("settlement_semantics_status"),
            "rate_confidence": capability_check.get("rate_confidence"),
            "execution_confidence": capability_check.get("execution_confidence"),
            "settlement_confidence": capability_check.get("settlement_confidence"),
            "accounting_confidence": capability_check.get("accounting_confidence"),
            "hard_blockers": capability_check.get("hard_blockers", []),
            "risk_flags": capability_check.get("risk_flags", []),
            "assumptions": capability_check.get("assumptions", []),
            "missing_capabilities": capability_check.get("missing_capabilities", []),
            "not_verified_alpha": capability_check.get("not_verified_alpha"),
            "raw_expected_funding": (
                capability_check.get("economics") or {}
            ).get("raw_expected_funding_usd"),
            "conservative_expected_funding": (
                capability_check.get("economics") or {}
            ).get("conservative_expected_funding_usd"),
            "raw_expected_net": (
                capability_check.get("economics") or {}
            ).get("raw_expected_net_usd"),
            "conservative_expected_net": (
                capability_check.get("economics") or {}
            ).get("conservative_expected_net_usd"),
            "uncertainty_reserves": (
                capability_check.get("economics") or {}
            ).get("uncertainty_reserves", {}),
            "synchronized_capability_passed": bool(
                capability_check.get("verified_paper_ready")
            ),
            "experimental_simulation_ready": bool(
                capability_check.get("experimental_simulation_ready")
                or capability_check.get("experimental_paper_ready")
            ),
            "experimental_paper_ready": bool(
                capability_check.get("experimental_simulation_ready")
                or capability_check.get("experimental_paper_ready")
            ),
            "experimental_paper_ready_deprecated": True,
            "verified_paper_ready": bool(
                capability_check.get("verified_paper_ready")
            ),
            "capability_rejections": capability_check.get("verified_paper_blockers", []),
            "capability_check": capability_check,
            "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
            "blocking_risk_flags": list(dict.fromkeys(blocking_risk_flags)),
            "advisory_reasons": list(dict.fromkeys(advisory_reasons)),
            "advisory_risk_flags": list(dict.fromkeys(advisory_risk_flags)),
            "persistence": persistence,
            "fill_complete": fill_complete,
            "requested_target_notional": config.target_notional,
            "maximum_executable_notional": maximum_notional,
            "sizing_trials": [
                {
                    "notional": row["notional"],
                    "target_quantity": row["target_quantity"],
                    "long_open_notional": row["long_open_notional"],
                    "short_open_notional": row["short_open_notional"],
                    "funding_notional": row["funding_notional"],
                    "signed_entry_basis": row["basis_model"]["signed_entry_basis"],
                    "basis_stress_loss": row["basis_stress_loss"],
                    "basis_stress_net_profit": row["basis_stress_net_profit"],
                    "basis_coverage_ratio": row["basis_coverage_ratio"],
                    "meets_basis_coverage_gate": row[
                        "meets_basis_coverage_gate"
                    ],
                    "expected_net_profit": row["expected_net_profit"],
                    "conservative_net_profit": row["conservative_net_profit"],
                    "net_profit_probability": row["net_profit_probability"],
                    "empirical_net_profit_probability": row[
                        "empirical_net_profit_probability"
                    ],
                    "net_roc_annualized": row["net_roc_annualized"],
                    "net_roc_horizon": row["net_roc_horizon"],
                    "actionable_profit_threshold": row[
                        "actionable_profit_threshold"
                    ],
                    "meets_actionable_profit_gate": row[
                        "meets_actionable_profit_gate"
                    ],
                    "current_nowcast_gross": row["current_nowcast_gross"],
                    "current_nowcast_net": row["current_nowcast_net"],
                    "current_signed_spread_pnl": row[
                        "current_signed_spread_pnl"
                    ],
                    "current_opportunity_gross": row[
                        "current_opportunity_gross"
                    ],
                    "current_opportunity_net": row[
                        "current_opportunity_net"
                    ],
                    "current_opportunity_basis_stress_net": row[
                        "current_opportunity_basis_stress_net"
                    ],
                    "current_basis_stress_net_profit": row[
                        "current_basis_stress_net_profit"
                    ],
                    "meets_live_actionable_profit_gate": row[
                        "meets_live_actionable_profit_gate"
                    ],
                    "meets_live_basis_coverage_gate": row[
                        "meets_live_basis_coverage_gate"
                    ],
                    "effective_sample_count": row["effective_sample_count"],
                    "slippage_cost": row["slippage_cost"],
                    "max_book_walk_bps": row["max_book_walk_bps"],
                    "fill_complete": row["fill_complete"],
                }
                for row in sizing_rows
            ],
            "fee_rates": {long_venue: fee_long, short_venue: fee_short},
            "fee_sources": fee_sources,
            "execution_scenarios": {
                "taker_taker": {
                    "candidate_eligible": True,
                    "execution_cost": selected_size["execution_cost"],
                    "break_even_gross_rate": selected_size[
                        "break_even_gross_rate"
                    ],
                    "break_even_settlements": selected_size[
                        "break_even_settlements"
                    ],
                    "break_even_hours": selected_size["break_even_hours"],
                },
                "maker_entry_taker_exit": maker_scenario,
            },
            "needed_improvement": needed_improvement,
            "liquidity_sequence": liquidity_profile,
            "long_basis": market_basis(long_market),
            "short_basis": market_basis(short_market),
            "reference_basis_gap": reference_basis_gap,
            "leg_notional_imbalance": leg_notional_imbalance,
            "basis_model": basis_model,
            "funding_notional": selected_size["funding_notional"],
            "current_nowcast_settlement_rate": current_nowcast_settlement_rate,
            "current_nowcast_gross": current_nowcast_gross,
            "current_nowcast_net": current_nowcast_net,
            "current_opportunity_gross": selected_size[
                "current_opportunity_gross"
            ],
            "current_opportunity_net": current_opportunity_net,
            "current_opportunity_basis_stress_net": (
                current_opportunity_basis_stress_net
            ),
            "current_basis_stress_net_profit": current_basis_stress_net_profit,
            "meets_live_actionable_profit_gate": meets_live_actionable_profit_gate,
            "meets_live_basis_coverage_gate": meets_live_basis_coverage_gate,
            "meets_live_opportunity_basis_coverage_gate": (
                meets_live_opportunity_basis_coverage_gate
            ),
            "target_quantity": selected_size["target_quantity"],
            "long_open_notional": long_open_notional,
            "short_open_notional": short_open_notional,
            "funding_q25_gross": selected_size["funding_q25_gross"],
            "basis_stress_loss": basis_stress_loss,
            "basis_stress_net_profit": basis_stress_net_profit,
            "basis_coverage_ratio": basis_coverage_ratio,
            "meets_basis_coverage_gate": meets_basis_coverage_gate,
            "reference_price_kinds": {
                long_venue: long_market.get("mark_price_kind", "published_mark"),
                short_venue: short_market.get("mark_price_kind", "published_mark"),
            },
            "settlement_event_count": settlement_event_count,
            "settlement_lead_seconds": settlement_lead_seconds,
            "minimum_settlement_lead_seconds": config.minimum_settlement_lead_seconds,
            "both_legs_settle": both_legs_settle,
            "normalized_interval_projection": interval_projection,
            "horizon": schedule,
            "forecast": {
                key: value
                for key, value in forecast.items()
                if not key.startswith("_")
            },
            "conservative_net_profit": conservative_net_profit,
            "execution_cost": selected_size["execution_cost"],
            "max_book_walk_bps": max_book_walk_bps,
            "break_even_gross_rate": selected_size["break_even_gross_rate"],
            "break_even_settlements": selected_size["break_even_settlements"],
            "break_even_hours": selected_size["break_even_hours"],
            "actionable_profit_threshold": actionable_profit_threshold,
            "conservative_net_roc_horizon": net_roc_horizon,
            "net_profit_probability": net_profit_probability,
            "empirical_net_profit_probability": empirical_net_profit_probability,
            "median_net_roc_annualized": selected_size["median_net_roc_annualized"],
            "median_net_roc_horizon": selected_size["median_net_roc_horizon"],
            "walk_forward": selected_size["walk_forward"],
            "historical_execution_is_proxy": True,
            "history_age_hours": history_age_hours,
            "history_age_limit_hours": history_age_limit_hours,
            "nowcast_age_seconds": nowcast_age_seconds,
            "calculation_mode": "conservative_public_data",
            "execution_mode": "paper_only",
        },
    }


def normalized_interval_projection(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    schedule: dict[str, Any],
) -> dict[str, Any]:
    horizon_hours = float(schedule.get("horizon_hours") or 0.0)
    long_interval_hours = float(long_market.get("funding_interval_hours") or 0.0)
    short_interval_hours = float(short_market.get("funding_interval_hours") or 0.0)
    projection_hours = max(
        horizon_hours,
        long_interval_hours,
        short_interval_hours,
        1.0,
    )
    projection_used = (
        schedule.get("horizon_mode") != "next_settlement"
        and max(long_interval_hours, short_interval_hours) > horizon_hours
        and (
            int(schedule.get("long_settlement_count") or 0) == 0
            or int(schedule.get("short_settlement_count") or 0) == 0
            or float(schedule.get("paired_coverage_hours") or 0.0) <= 0
        )
    )
    long_projected_rate = (
        float(long_market.get("hourly_funding_rate") or 0.0) * projection_hours
    )
    short_projected_rate = (
        float(short_market.get("hourly_funding_rate") or 0.0) * projection_hours
    )
    return {
        "projection_used": projection_used,
        "projection_hours": projection_hours,
        "long_interval_hours": long_interval_hours,
        "short_interval_hours": short_interval_hours,
        "long_projected_rate": long_projected_rate,
        "short_projected_rate": short_projected_rate,
        "projected_gross_rate": short_projected_rate - long_projected_rate,
        "actual_cashflow_event_count": int(
            schedule.get("settlement_event_count") or 0
        ),
        "actual_paired_coverage_hours": float(
            schedule.get("paired_coverage_hours") or 0.0
        ),
    }


def route_needed_improvement(
    selected_size: dict[str, Any],
    maker_scenario: dict[str, Any],
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    current_nowcast_gross: float,
    current_nowcast_net: float,
    current_basis_stress_net_profit: float,
    actionable_profit_threshold: float,
    basis_stress_loss: float,
    basis_gap: float,
) -> dict[str, Any]:
    execution_cost = float(selected_size.get("execution_cost") or 0.0)
    total_fees = float(selected_size.get("total_fees") or 0.0)
    slippage_cost = float(selected_size.get("slippage_cost") or 0.0)
    basis_reserve = float(selected_size.get("basis_reserve") or 0.0)
    operations_buffer = float(selected_size.get("operations_buffer") or 0.0)
    long_open_notional = float(selected_size.get("long_open_notional") or 0.0)
    short_open_notional = float(selected_size.get("short_open_notional") or 0.0)
    long_close_notional = float(
        (selected_size.get("long_close") or {}).get("filled_notional") or 0.0
    )
    short_close_notional = float(
        (selected_size.get("short_close") or {}).get("filled_notional") or 0.0
    )
    long_venue = str(long_market.get("venue") or "")
    short_venue = str(short_market.get("venue") or "")
    maker_net = finite_float_or_none(
        maker_scenario.get("current_net_profit_if_filled")
    )
    maker_cost = finite_float_or_none(
        maker_scenario.get("if_filled_execution_cost")
    )
    maker_action = {
        "type": "maker_entry_taker_exit",
        "label": "Maker entry, taker exit",
        "net_profit_if_filled": maker_net,
        "expected_attempt_pnl": finite_float_or_none(
            maker_scenario.get("current_expected_attempt_pnl")
        ),
        "execution_cost_if_filled": maker_cost,
        "cost_reduction": (
            execution_cost - maker_cost if maker_cost is not None else None
        ),
        "improvement_vs_current": (
            maker_net - current_nowcast_net if maker_net is not None else None
        ),
        "remaining_to_positive": (
            max(0.0, -maker_net) if maker_net is not None else None
        ),
        "remaining_to_actionable": (
            max(0.0, actionable_profit_threshold - maker_net)
            if maker_net is not None
            else None
        ),
        "data_ready": bool(maker_scenario.get("data_ready")),
        "source": "route_l2_maker_proxy",
    }

    vip_profiles = {
        long_venue: verified_vip1_fee_profile(long_venue),
        short_venue: verified_vip1_fee_profile(short_venue),
    }
    vip_total_fees = (
        vip_taker_fee_rate(long_venue, long_market)
        * (long_open_notional + long_close_notional)
        + vip_taker_fee_rate(short_venue, short_market)
        * (short_open_notional + short_close_notional)
    )
    vip_execution_cost = execution_cost - total_fees + vip_total_fees
    vip_net = current_nowcast_gross - vip_execution_cost
    vip_applied = {
        venue: profile
        for venue, profile in vip_profiles.items()
        if profile is not None
    }
    vip_action = {
        "type": "verified_vip1_taker_taker",
        "label": "VIP1 taker fees where verified",
        "net_profit": vip_net,
        "execution_cost": vip_execution_cost,
        "fee_cost": vip_total_fees,
        "cost_reduction": execution_cost - vip_execution_cost,
        "improvement_vs_current": vip_net - current_nowcast_net,
        "remaining_to_positive": max(0.0, -vip_net),
        "remaining_to_actionable": max(0.0, actionable_profit_threshold - vip_net),
        "applied_profiles": vip_applied,
        "missing_profiles": [
            venue for venue, profile in vip_profiles.items() if profile is None
        ],
        "source": "verified_public_vip1_profiles",
    }
    actions = []
    if (
        maker_action["improvement_vs_current"] is not None
        and float(maker_action["improvement_vs_current"]) > 0.0
    ):
        actions.append(maker_action)
    if vip_applied and vip_action["improvement_vs_current"] > 0:
        actions.append(vip_action)

    def action_net(action: dict[str, Any]) -> float:
        return float(
            action.get("net_profit_if_filled")
            if action.get("net_profit_if_filled") is not None
            else action.get("net_profit")
            if action.get("net_profit") is not None
            else -math.inf
        )

    best_action = max(actions, key=action_net, default=None)
    return {
        "model_version": "needed_improvement_v1",
        "current": {
            "live_net_profit": current_nowcast_net,
            "live_gross_funding": current_nowcast_gross,
            "execution_cost": execution_cost,
            "fee_cost": total_fees,
            "slippage_cost": slippage_cost,
            "basis_reserve": basis_reserve,
            "operations_buffer": operations_buffer,
            "basis_stress_loss": basis_stress_loss,
            "basis_gap_bps": basis_gap * 10_000.0,
            "live_basis_stress_net_profit": current_basis_stress_net_profit,
        },
        "needed_to_positive": max(0.0, -current_nowcast_net),
        "needed_to_actionable": max(
            0.0,
            actionable_profit_threshold - current_nowcast_net,
        ),
        "needed_basis_stress_reduction": max(
            0.0,
            -current_basis_stress_net_profit,
        ),
        "maker_entry": maker_action,
        "vip1": vip_action,
        "actions": actions,
        "best_action": best_action,
        "actionable_profit_threshold": actionable_profit_threshold,
    }


def vip_taker_fee_rate(venue: str, market: dict[str, Any]) -> float:
    profile = verified_vip1_fee_profile(venue)
    if profile is None:
        return market_fee_rate(market, "taker")
    return min(market_fee_rate(market, "taker"), float(profile["taker"]))


def finite_float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def notional_trials(maximum_notional: float, minimum_notional: float) -> list[float]:
    maximum = max(0.0, float(maximum_notional))
    minimum = max(0.0, float(minimum_notional))
    if maximum <= 0:
        return [0.0]
    if maximum < minimum:
        return [maximum]
    ladder = (
        100.0,
        250.0,
        500.0,
        1_000.0,
        2_500.0,
        5_000.0,
        10_000.0,
        25_000.0,
        50_000.0,
        100_000.0,
        250_000.0,
        500_000.0,
        1_000_000.0,
    )
    values = {
        value
        for value in ladder
        if minimum <= value < maximum
    }
    values.add(minimum)
    values.add(maximum)
    return sorted(values)


def select_sizing_row(
    rows: list[dict[str, Any]],
    passing_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if passing_rows:
        return max(
            passing_rows,
            key=lambda row: (
                float(row["conservative_net_profit"]),
                float(row["expected_net_profit"]),
                float(row["net_roc_horizon"]),
            ),
        )
    positive_rows = [row for row in rows if float(row["expected_net_profit"]) > 0]
    if positive_rows:
        return max(
            positive_rows,
            key=lambda row: (
                float(row["expected_net_profit"]),
                float(row["net_roc_horizon"]),
            ),
        )
    return max(
        rows,
        key=lambda row: (
            float(row["net_roc_horizon"]),
            float(row["notional"]),
        ),
    )


def add_live_settlement_economics(
    row: dict[str, Any],
    current_settlement_rate: float,
) -> dict[str, Any]:
    """Attach current, executable next-settlement economics to a sizing row."""
    current_gross = float(row["funding_notional"]) * float(current_settlement_rate)
    current_net = current_gross - float(row["execution_cost"])
    signed_spread = float(row["funding_notional"]) * float(
        (row.get("basis_model") or {}).get("signed_entry_basis") or 0.0
    )
    opportunity_gross = current_gross + signed_spread
    opportunity_net = opportunity_gross - float(row["execution_cost"])
    incremental_basis_stress_loss = max(
        0.0,
        float(row["basis_stress_loss"]) - max(0.0, -signed_spread),
    )
    opportunity_basis_stress_net = opportunity_net - incremental_basis_stress_loss
    current_basis_stress_net = current_net - float(row["basis_stress_loss"])
    threshold = float(row["actionable_profit_threshold"])
    large_basis = bool((row.get("basis_model") or {}).get("tier") != "standard")
    return {
        **row,
        "current_nowcast_gross": current_gross,
        "current_nowcast_net": current_net,
        "current_signed_spread_pnl": signed_spread,
        "current_opportunity_gross": opportunity_gross,
        "current_opportunity_net": opportunity_net,
        "current_opportunity_basis_stress_net": opportunity_basis_stress_net,
        "current_basis_stress_net_profit": current_basis_stress_net,
        "meets_live_actionable_profit_gate": current_net > 0.0,
        "meets_live_opportunity_profit_gate": opportunity_net > threshold,
        "meets_live_basis_coverage_gate": (
            not large_basis or current_basis_stress_net > 0.0
        ),
        "meets_live_opportunity_basis_coverage_gate": (
            not large_basis or opportunity_basis_stress_net > 0.0
        ),
    }


def build_strategy_evaluation(
    row: dict[str, Any],
    current_funding_gross: float,
    current_funding_net: float,
    current_basis_stress_net: float,
    actionable_profit_threshold: float,
    blocking_risk_flags: list[str],
    decision_mode: str,
) -> dict[str, Any]:
    funding_notional = float(row.get("funding_notional") or 0.0)
    execution_cost = float(row.get("execution_cost") or 0.0)
    basis_model = dict(row.get("basis_model") or {})
    signed_entry_basis = float(basis_model.get("signed_entry_basis") or 0.0)
    spread_convergence = funding_notional * signed_entry_basis
    if str(decision_mode) == "settlement_capture":
        spread_convergence = 0.0
    total_with_spread = current_funding_gross + spread_convergence - execution_cost
    threshold = max(0.0, float(actionable_profit_threshold or 0.0))
    operational_blockers = [
        flag
        for flag in dict.fromkeys(str(flag) for flag in blocking_risk_flags)
        if flag not in STRATEGY_ECONOMIC_BLOCKERS
    ]
    common_ok = not operational_blockers and str(decision_mode) == "settlement_capture"
    basis_stress_loss = float(row.get("basis_stress_loss") or 0.0)
    incremental_basis_stress_loss = max(
        0.0,
        basis_stress_loss - max(0.0, -spread_convergence),
    )
    risk_adjusted_net = total_with_spread - incremental_basis_stress_loss
    positive_edge = max(0.0, current_funding_gross) + max(0.0, spread_convergence)
    drag = execution_cost + max(0.0, -current_funding_gross) + max(
        0.0,
        -spread_convergence,
    )
    coverage_ratio = (
        positive_edge / drag
        if drag > 0
        else math.inf if positive_edge > 0 else 0.0
    )
    edge_type = classify_opportunity_edge(
        current_funding_gross,
        spread_convergence,
        total_with_spread,
    )
    edge_quality, quality_warnings = classify_opportunity_quality(
        total_with_spread,
        risk_adjusted_net,
        threshold,
        coverage_ratio,
        current_funding_gross,
        spread_convergence,
    )
    strategy_name = opportunity_strategy_name(edge_type)
    warnings = opportunity_warnings(
        current_funding_gross,
        spread_convergence,
        risk_adjusted_net,
        threshold,
        edge_quality,
        quality_warnings,
    )
    reasons = list(operational_blockers)
    if edge_type == "no_positive_edge":
        reasons.append("no_positive_edge_component")
    if total_with_spread < threshold:
        reasons.append("opportunity_net_below_required_profit")
    eligible = common_ok and not reasons

    opportunity = {
        "selection_model": "opportunity_engine_v1",
        "strategy_name": strategy_name,
        "strategy_class": strategy_name,
        "primary_edge": opportunity_primary_edge(edge_type),
        "edge_type": edge_type,
        "edge_label": opportunity_edge_label(edge_type),
        "edge_quality": edge_quality,
        "eligible": bool(eligible),
        "expected_net_pnl": total_with_spread,
        "gross_edge_pnl": current_funding_gross + spread_convergence,
        "funding_pnl_component": current_funding_gross,
        "spread_pnl_component": spread_convergence,
        "signed_spread_pnl_component": spread_convergence,
        "execution_cost": execution_cost,
        "actionable_profit_threshold": threshold,
        "basis_stress_loss": basis_stress_loss,
        "incremental_basis_stress_loss": incremental_basis_stress_loss,
        "basis_stress_net_pnl": risk_adjusted_net,
        "risk_adjusted_net_pnl": risk_adjusted_net,
        "positive_edge_pnl": positive_edge,
        "drag_pnl": drag,
        "coverage_ratio": coverage_ratio,
        "operational_blocking_risk_flags": operational_blockers,
        "hard_blocking_risk_flags": operational_blockers,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "thesis": opportunity_thesis(
            edge_type,
            edge_quality,
            current_funding_gross,
            spread_convergence,
            execution_cost,
            total_with_spread,
            risk_adjusted_net,
            coverage_ratio,
        ),
    }
    candidates = [opportunity]
    selected = select_strategy_candidate(candidates)
    return {
        "strategy_candidates": candidates,
        "selected_strategy": selected,
        "pnl_components": {
            "funding_pnl_component": current_funding_gross,
            "spread_pnl_component": spread_convergence,
            "signed_spread_pnl_component": spread_convergence,
            "spread_convergence_component": spread_convergence,
            "execution_cost": execution_cost,
            "funding_only_net_pnl": current_funding_net,
            "spread_total_net_pnl": total_with_spread,
            "combined_net_pnl": total_with_spread,
            "opportunistic_any_net_pnl": total_with_spread,
            "opportunity_expected_net_pnl": total_with_spread,
            "opportunity_risk_adjusted_net_pnl": risk_adjusted_net,
            "opportunistic_risk_adjusted_net_pnl": risk_adjusted_net,
            "basis_stress_loss": basis_stress_loss,
            "incremental_basis_stress_loss": incremental_basis_stress_loss,
            "funding_basis_stress_net_pnl": current_basis_stress_net,
            "spread_basis_stress_net_pnl": risk_adjusted_net,
            "positive_edge_pnl": positive_edge,
            "drag_pnl": drag,
            "coverage_ratio": coverage_ratio,
        },
    }


def classify_opportunity_edge(
    funding_component: float,
    spread_component: float,
    expected_net: float,
) -> str:
    if expected_net <= 0:
        return "no_positive_edge"
    gross_edge = abs(funding_component) + abs(spread_component)
    material = max(0.05, gross_edge * 0.10)
    funding_material = funding_component > material
    spread_material = spread_component > material
    if funding_material and spread_material:
        return "mixed_edge"
    if funding_component > 0 and funding_component >= spread_component:
        return "funding_led"
    if spread_component > 0:
        return "spread_led"
    return "no_positive_edge"


def classify_opportunity_quality(
    expected_net: float,
    risk_adjusted_net: float,
    threshold: float,
    coverage_ratio: float,
    funding_component: float,
    spread_component: float,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if expected_net < threshold:
        warnings.append("net_below_actionable_threshold")
    if risk_adjusted_net <= 0:
        warnings.append("basis_stress_not_covered")
    elif risk_adjusted_net < threshold:
        warnings.append("basis_stress_net_below_required_profit")
    if coverage_ratio < 1.25:
        warnings.append("thin_total_edge_coverage")
    if (funding_component < 0 or spread_component < 0) and coverage_ratio < 1.50:
        warnings.append("thin_component_coverage")
    if warnings:
        return "fragile", warnings
    return "clean", []


def opportunity_strategy_name(edge_type: str) -> str:
    return {
        "funding_led": "funding_only",
        "spread_led": "spread_only",
        "mixed_edge": "combined",
        "no_positive_edge": "opportunistic_any",
    }.get(edge_type, "opportunistic_any")


def opportunity_primary_edge(edge_type: str) -> str:
    return {
        "funding_led": "funding_carry",
        "spread_led": "spread_convergence",
        "mixed_edge": "funding_plus_spread",
        "no_positive_edge": "none",
    }.get(edge_type, "opportunistic_total_edge")


def opportunity_edge_label(edge_type: str) -> str:
    return {
        "funding_led": "Funding-led",
        "spread_led": "Spread-led",
        "mixed_edge": "Mixed edge",
        "no_positive_edge": "No positive edge",
    }.get(edge_type, str(edge_type or "Unknown"))


def opportunity_warnings(
    funding_component: float,
    spread_component: float,
    risk_adjusted_net: float,
    threshold: float,
    edge_quality: str,
    quality_warnings: list[str],
) -> list[str]:
    warnings = list(quality_warnings)
    if funding_component < 0:
        warnings.append("funding_drag")
    if spread_component < 0:
        warnings.append("spread_drag")
    if edge_quality == "fragile":
        warnings.append("fragile_positive_total_edge")
    if risk_adjusted_net <= 0 and "basis_stress_not_covered" not in warnings:
        warnings.append("basis_stress_not_covered")
    elif (
        0 < risk_adjusted_net < threshold
        and "basis_stress_net_below_required_profit" not in warnings
    ):
        warnings.append("basis_stress_net_below_required_profit")
    return warnings


def opportunity_thesis(
    edge_type: str,
    edge_quality: str,
    funding_component: float,
    spread_component: float,
    execution_cost: float,
    expected_net: float,
    risk_adjusted_net: float,
    coverage_ratio: float,
) -> str:
    label = opportunity_edge_label(edge_type)
    coverage = "inf" if math.isinf(coverage_ratio) else f"{coverage_ratio:.2f}x"
    return (
        f"{label} ({edge_quality}): funding ${funding_component:,.2f}, "
        f"spread ${spread_component:,.2f}, cost ${execution_cost:,.2f}, "
        f"net ${expected_net:,.2f}; stress net ${risk_adjusted_net:,.2f}, "
        f"coverage {coverage}."
    )


def select_strategy_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [row for row in candidates if row.get("eligible")]
    if not eligible:
        return None
    strict = [
        row
        for row in eligible
        if str(row.get("strategy_name")) != "opportunistic_any"
    ]
    if strict:
        eligible = strict
    priority = {
        "combined": 4,
        "spread_only": 3,
        "funding_only": 2,
        "opportunistic_any": 1,
    }
    return max(
        eligible,
        key=lambda row: (
            float(row.get("expected_net_pnl") or 0.0),
            priority.get(str(row.get("strategy_name")), 0),
        ),
    )


def strategy_rationale(strategy: dict[str, Any]) -> str:
    if strategy.get("selection_model") == "opportunity_engine_v1":
        thesis = str(strategy.get("thesis") or "").strip()
        if thesis:
            return f"Opportunity {thesis}"
    warning_text = ""
    warnings = [str(item) for item in strategy.get("warnings") or []]
    if warnings:
        warning_text = f" Warnings: {', '.join(warnings)}."
    return (
        f"Strategy {strategy.get('strategy_name')}: "
        f"funding ${float(strategy.get('funding_pnl_component') or 0.0):,.2f}, "
        f"spread ${float(strategy.get('spread_pnl_component') or 0.0):,.2f}, "
        f"cost ${float(strategy.get('execution_cost') or 0.0):,.2f}, "
        f"net ${float(strategy.get('expected_net_pnl') or 0.0):,.2f}."
        f"{warning_text}"
    )


def select_live_sizing_row(
    rows: list[dict[str, Any]],
    passing_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Size a settlement capture from live PnL, without a historical veto."""
    eligible = passing_rows or [row for row in rows if row["fill_complete"]] or rows
    return max(
        eligible,
        key=lambda row: (
            float(row["current_nowcast_net"]),
            float(row["current_basis_stress_net_profit"]),
            float(row["notional"]),
        ),
    )


def current_schedule_carry_rate(
    schedule: dict[str, Any],
    long_market: dict[str, Any],
    short_market: dict[str, Any],
) -> float:
    """Cash-flow rate implied by live nowcasts for the next revalidation point.

    Current exchange funding estimates are strongest for the nearest fee
    assessment. For fixed/research horizons, repeating that nowcast across
    later settlements would silently assume the spread survives unchanged.
    """
    long_hourly = float(long_market.get("hourly_funding_rate") or 0.0)
    short_hourly = float(short_market.get("hourly_funding_rate") or 0.0)
    settlements = list(schedule.get("settlements", []))
    if str(schedule.get("horizon_mode") or "") != "next_settlement":
        first_hour = min(
            (
                float(row.get("hours_from_start") or 0.0)
                for row in settlements
            ),
            default=None,
        )
        settlements = [
            row
            for row in settlements
            if first_hour is not None
            and abs(float(row.get("hours_from_start") or 0.0) - first_hour)
            <= 1e-9
        ]
    return sum(
        short_hourly * float(row.get("short_settlement_interval_hours") or 0.0)
        - long_hourly * float(row.get("long_settlement_interval_hours") or 0.0)
        for row in settlements
    )


def notional_economics(
    notional: float,
    long_book: dict[str, Any],
    short_book: dict[str, Any],
    fee_long: float,
    fee_short: float,
    forecast: dict[str, Any],
    horizon_hours: float,
    basis_reserve_rate: float,
    config: FundingScanConfig,
    basis_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    long_mid = positive_float(long_book.get("mid_price"))
    short_mid = positive_float(short_book.get("mid_price"))
    # The ladder is the minimum USD notional per leg. Equal base quantity can
    # make the more expensive venue slightly larger when basis is non-zero.
    sizing_price = min(long_mid, short_mid)
    target_quantity = notional / sizing_price if sizing_price > 0 else 0.0
    long_open = fill_quantity(long_book.get("asks", []), target_quantity)
    short_open = fill_quantity(short_book.get("bids", []), target_quantity)
    long_close = fill_quantity(long_book.get("bids", []), target_quantity)
    short_close = fill_quantity(short_book.get("asks", []), target_quantity)
    fills = [long_open, short_open, long_close, short_close]
    fill_complete = target_quantity > 0 and all(
        fill["filled_size"] >= target_quantity * 0.999 for fill in fills
    )
    slippage_cost = (
        directional_slippage(long_open, long_book.get("mid_price"), "buy")
        + directional_slippage(short_open, short_book.get("mid_price"), "sell")
        + directional_slippage(long_close, long_book.get("mid_price"), "sell")
        + directional_slippage(short_close, short_book.get("mid_price"), "buy")
    )
    max_book_walk_bps = max(
        (float(fill.get("book_walk_bps") or 0.0) for fill in fills),
        default=0.0,
    )
    long_open_notional = float(long_open["filled_notional"])
    short_open_notional = float(short_open["filled_notional"])
    long_close_notional = float(long_close["filled_notional"])
    short_close_notional = float(short_close["filled_notional"])
    funding_notional = (long_open_notional + short_open_notional) / 2.0
    total_fees = fee_long * (long_open_notional + long_close_notional) + fee_short * (
        short_open_notional + short_close_notional
    )
    basis_reserve = funding_notional * basis_reserve_rate
    operations_buffer = (
        funding_notional * config.operations_buffer_bps / 10_000.0
    )
    execution_cost = total_fees + slippage_cost + basis_reserve + operations_buffer
    scenario_outcomes = list(forecast.get("_scenario_outcomes") or [])
    funding_scenario_weights = [
        max(0.0, float(row.get("weight") or 0.0)) for row in scenario_outcomes
    ]
    basis_model = build_basis_scenarios(
        long_open,
        short_open,
        basis_history or {},
        config,
    )
    combined_outcomes = [
        {
            "net_profit": funding_notional
            * (float(funding_row.get("gross_rate") or 0.0) + float(basis_row["pnl_rate"]))
            - execution_cost,
            "weight": max(0.0, float(funding_row.get("weight") or 0.0))
            * float(basis_row["weight"]),
        }
        for funding_row in scenario_outcomes
        for basis_row in basis_model["scenarios"]
    ]
    scenario_net_profit = [float(row["net_profit"]) for row in combined_outcomes]
    scenario_weights = [float(row["weight"]) for row in combined_outcomes]
    expected_gross_funding = funding_notional * float(
        forecast.get("gross_rate_median") or 0.0
    )
    expected_net_profit = (
        weighted_quantile(scenario_net_profit, scenario_weights, 0.5)
        if scenario_net_profit
        else -execution_cost
    )
    conservative_net_profit = (
        weighted_quantile(
            scenario_net_profit,
            scenario_weights,
            config.conservative_quantile,
        )
        if scenario_net_profit
        else -execution_cost
    )
    empirical_net_profit_probability = (
        weighted_mean(
            [1.0 if value > 0 else 0.0 for value in scenario_net_profit],
            scenario_weights,
        )
        if scenario_net_profit
        else 0.0
    )
    # Basis states are stress branches, not independent observations. Confidence
    # remains bounded by the number of historical funding outcomes.
    effective_observations = effective_sample_size(funding_scenario_weights)
    net_profit_probability = (
        wilson_lower_bound(
            empirical_net_profit_probability * effective_observations,
            effective_observations,
        )
        if scenario_net_profit
        else 0.0
    )
    capital_required = config.margin_fraction_per_leg * (
        long_open_notional + short_open_notional
    ) + config.collateral_reserve_fraction * max(
        long_open_notional,
        short_open_notional,
    )
    horizon_days = max(float(horizon_hours) / 24.0, 1.0 / 24.0)
    conservative_return = (
        conservative_net_profit / capital_required if capital_required > 0 else 0.0
    )
    median_return = expected_net_profit / capital_required if capital_required > 0 else 0.0
    actionable_profit_threshold = max(
        config.minimum_net_profit,
        capital_required * config.minimum_net_return_bps / 10_000.0,
    )
    adverse_basis_rate = min(
        (
            float(row.get("pnl_rate") or 0.0)
            for row in basis_model.get("scenarios", [])
        ),
        default=0.0,
    )
    basis_stress_loss = funding_notional * max(0.0, -adverse_basis_rate)
    funding_q25_gross = funding_notional * float(
        forecast.get("gross_rate_q25") or 0.0
    )
    basis_stress_net_profit = (
        funding_q25_gross - basis_stress_loss - execution_cost
    )
    basis_coverage_denominator = basis_stress_loss + execution_cost
    basis_coverage_ratio = (
        max(0.0, funding_q25_gross) / basis_coverage_denominator
        if basis_coverage_denominator > 0
        else math.inf
    )
    large_basis = (
        float(basis_model.get("absolute_entry_basis") or 0.0)
        > config.large_basis_threshold_bps / 10_000.0
    )
    meets_basis_coverage_gate = not large_basis or basis_stress_net_profit > 0.0
    historical_outcomes = list(forecast.get("_historical_outcomes") or [])
    historical_net_profit = [
        funding_notional * float(row.get("gross_rate") or 0.0) - execution_cost
        for row in historical_outcomes
    ]
    historical_weights = [
        max(0.0, float(row.get("weight") or 0.0)) for row in historical_outcomes
    ]
    positive_event_rates = [
        float(row.get("forecast_event_rate") or 0.0)
        for row in forecast.get("settlements", [])
        if float(row.get("forecast_event_rate") or 0.0) > 0
    ]
    typical_event_rate = (
        weighted_quantile(
            positive_event_rates,
            [1.0] * len(positive_event_rates),
            0.5,
        )
        if positive_event_rates
        else 0.0
    )
    break_even_gross_rate = (
        execution_cost / funding_notional if funding_notional > 0 else math.inf
    )
    break_even_settlements = (
        math.ceil(break_even_gross_rate / typical_event_rate)
        if typical_event_rate > 0 and math.isfinite(break_even_gross_rate)
        else None
    )
    forecast_gross_rate = float(forecast.get("gross_rate_median") or 0.0)
    break_even_hours = (
        horizon_hours * break_even_gross_rate / forecast_gross_rate
        if forecast_gross_rate > 0 and horizon_hours > 0
        else None
    )
    return {
        "notional": notional,
        "target_quantity": target_quantity,
        "funding_notional": funding_notional,
        "long_open_notional": long_open_notional,
        "short_open_notional": short_open_notional,
        "long_open": long_open,
        "short_open": short_open,
        "long_close": long_close,
        "short_close": short_close,
        "fill_complete": fill_complete,
        "slippage_cost": slippage_cost,
        "max_book_walk_bps": max_book_walk_bps,
        "total_fees": total_fees,
        "basis_reserve": basis_reserve,
        "basis_model": basis_model,
        "operations_buffer": operations_buffer,
        "execution_cost": execution_cost,
        "break_even_gross_rate": break_even_gross_rate,
        "break_even_settlements": break_even_settlements,
        "break_even_hours": break_even_hours,
        "expected_gross_funding": expected_gross_funding,
        "expected_net_profit": expected_net_profit,
        "conservative_net_profit": conservative_net_profit,
        "net_profit_probability": net_profit_probability,
        "empirical_net_profit_probability": empirical_net_profit_probability,
        "effective_sample_count": effective_observations,
        "capital_required": capital_required,
        "net_roc_horizon": conservative_return,
        "median_net_roc_horizon": median_return,
        "actionable_profit_threshold": actionable_profit_threshold,
        "funding_q25_gross": funding_q25_gross,
        "basis_stress_loss": basis_stress_loss,
        "basis_stress_net_profit": basis_stress_net_profit,
        "basis_coverage_ratio": basis_coverage_ratio,
        "meets_basis_coverage_gate": meets_basis_coverage_gate,
        "meets_actionable_profit_gate": conservative_net_profit > 0.0,
        "net_roc_annualized": conservative_return * 365.0 / horizon_days,
        "median_net_roc_annualized": median_return * 365.0 / horizon_days,
        "walk_forward": {
            "sample_count": len(historical_net_profit),
            "effective_sample_count": effective_sample_size(historical_weights),
            "net_win_probability": (
                weighted_mean(
                    [1.0 if value > 0 else 0.0 for value in historical_net_profit],
                    historical_weights,
                )
                if historical_net_profit
                else 0.0
            ),
            "median_net_profit": (
                weighted_quantile(historical_net_profit, historical_weights, 0.5)
                if historical_net_profit
                else None
            ),
            "q25_net_profit": (
                weighted_quantile(
                    historical_net_profit,
                    historical_weights,
                    config.conservative_quantile,
                )
                if historical_net_profit
                else None
            ),
            "cost_model": "equal_quantity_four_side_signed_basis_v1",
        },
    }


def maker_entry_scenario(
    notional: float,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    long_book: dict[str, Any],
    short_book: dict[str, Any],
    forecast: dict[str, Any],
    basis_reserve_rate: float,
    config: FundingScanConfig,
    liquidity_profile: dict[str, Any],
    current_settlement_rate: float = 0.0,
) -> dict[str, Any]:
    """Indicative maker-entry economics, never an executable candidate gate.

    The probability comes from a conservative price-through proxy when enough L2
    snapshots exist. It remains a shadow scenario because L2 cannot reconstruct
    queue position or distinguish trades from cancellations.
    """
    maker_long = market_fee_rate(long_market, "maker")
    maker_short = market_fee_rate(short_market, "maker")
    taker_long = market_fee_rate(long_market, "taker")
    taker_short = market_fee_rate(short_market, "taker")
    long_close = fill_notional(long_book.get("bids", []), notional)
    short_close = fill_notional(short_book.get("asks", []), notional)
    close_slippage = (
        directional_slippage(long_close, long_book.get("mid_price"), "sell")
        + directional_slippage(short_close, short_book.get("mid_price"), "buy")
    )
    fees = notional * (maker_long + maker_short + taker_long + taker_short)
    basis_reserve = notional * basis_reserve_rate
    operations_buffer = notional * config.operations_buffer_bps / 10_000.0
    maker_profile = liquidity_profile.get("maker_entry") or {}
    long_fill = maker_profile.get("long") or {}
    short_fill = maker_profile.get("short") or {}
    adverse_selection_bps = max(
        0.0,
        float(long_fill.get("adverse_selection_bps_q75") or 0.0),
    ) + max(
        0.0,
        float(short_fill.get("adverse_selection_bps_q75") or 0.0),
    )
    adverse_selection_cost = notional * adverse_selection_bps / 10_000.0
    if_filled_cost = (
        fees
        + close_slippage
        + basis_reserve
        + operations_buffer
        + adverse_selection_cost
    )
    current_gross_if_filled = notional * float(current_settlement_rate)
    current_net_if_filled = current_gross_if_filled - if_filled_cost
    scenario_outcomes = list(forecast.get("_scenario_outcomes") or [])
    rates = [float(row.get("gross_rate") or 0.0) for row in scenario_outcomes]
    weights = [
        max(0.0, float(row.get("weight") or 0.0)) for row in scenario_outcomes
    ]
    pnl_if_filled = [notional * rate - if_filled_cost for rate in rates]
    median_if_filled = (
        weighted_quantile(pnl_if_filled, weights, 0.5) if pnl_if_filled else None
    )
    q25_if_filled = (
        weighted_quantile(pnl_if_filled, weights, config.conservative_quantile)
        if pnl_if_filled
        else None
    )
    long_fill_probability = float(
        long_fill.get("fill_probability", config.maker_fill_probability)
    )
    short_fill_probability = float(
        short_fill.get("fill_probability", config.maker_fill_probability)
    )
    joint_fill = long_fill_probability * short_fill_probability
    one_leg_fill = (
        long_fill_probability * (1.0 - short_fill_probability)
        + (1.0 - long_fill_probability) * short_fill_probability
    )
    no_fill = (1.0 - long_fill_probability) * (1.0 - short_fill_probability)
    mismatch_reserve = notional * (
        2.0 * max(taker_long, taker_short)
        + config.maker_timeout_penalty_bps / 10_000.0
    )
    expected_attempt_pnl = (
        joint_fill * float(median_if_filled)
        - one_leg_fill * mismatch_reserve
        if median_if_filled is not None
        else None
    )
    current_expected_attempt_pnl = (
        joint_fill * current_net_if_filled - one_leg_fill * mismatch_reserve
    )
    estimated_capital_required = notional * (
        2.0 * config.margin_fraction_per_leg
        + config.collateral_reserve_fraction
    )
    current_actionable_threshold = max(
        config.minimum_net_profit,
        estimated_capital_required * config.minimum_net_return_bps / 10_000.0,
    )
    setup_visible = (
        current_net_if_filled > 0.0
        or current_expected_attempt_pnl > 0.0
    )
    return {
        "candidate_eligible": False,
        "setup_visible": setup_visible,
        "reason": (
            "L2 price-through is a fill proxy; queue position and trade prints "
            "are not yet reconstructed"
        ),
        "probability_source": maker_profile.get(
            "probability_source", "prior_fallback"
        ),
        "data_ready": bool(maker_profile.get("data_ready")),
        "maker_fill_probability_per_leg": {
            str(long_market["venue"]): long_fill_probability,
            str(short_market["venue"]): short_fill_probability,
        },
        "joint_fill_probability": joint_fill,
        "one_leg_fill_probability": one_leg_fill,
        "no_fill_probability": no_fill,
        "fee_rates": {
            str(long_market["venue"]): {"maker": maker_long, "taker": taker_long},
            str(short_market["venue"]): {"maker": maker_short, "taker": taker_short},
        },
        "queue_ahead_notional": {
            str(long_market["venue"]): float(
                long_fill.get(
                    "queue_ahead_notional",
                    top_level_notional(long_book.get("bids", [])),
                )
            ),
            str(short_market["venue"]): float(
                short_fill.get(
                    "queue_ahead_notional",
                    top_level_notional(short_book.get("asks", [])),
                )
            ),
        },
        "fill_observations": {
            str(long_market["venue"]): {
                "trials": int(long_fill.get("trial_count") or 0),
                "successes": int(long_fill.get("success_count") or 0),
                "empirical_probability": long_fill.get("empirical_probability"),
                "conservative_probability": long_fill.get(
                    "conservative_probability"
                ),
                "adverse_selection_bps_q75": long_fill.get(
                    "adverse_selection_bps_q75"
                ),
            },
            str(short_market["venue"]): {
                "trials": int(short_fill.get("trial_count") or 0),
                "successes": int(short_fill.get("success_count") or 0),
                "empirical_probability": short_fill.get("empirical_probability"),
                "conservative_probability": short_fill.get(
                    "conservative_probability"
                ),
                "adverse_selection_bps_q75": short_fill.get(
                    "adverse_selection_bps_q75"
                ),
            },
        },
        "if_filled_execution_cost": if_filled_cost,
        "current_gross_if_filled": current_gross_if_filled,
        "current_net_profit_if_filled": current_net_if_filled,
        "current_expected_attempt_pnl": current_expected_attempt_pnl,
        "current_actionable_profit_threshold": current_actionable_threshold,
        "current_settlement_rate": current_settlement_rate,
        "adverse_selection_cost": adverse_selection_cost,
        "adverse_selection_bps": adverse_selection_bps,
        "median_net_profit_if_filled": median_if_filled,
        "q25_net_profit_if_filled": q25_if_filled,
        "one_leg_mismatch_reserve": mismatch_reserve,
        "expected_attempt_pnl": expected_attempt_pnl,
        "timeout_penalty_bps": config.maker_timeout_penalty_bps,
        "timeout_seconds": config.maker_timeout_seconds,
    }


def top_level_notional(levels: list[list[float]]) -> float:
    if not levels:
        return 0.0
    try:
        return max(0.0, float(levels[0][0]) * float(levels[0][1]))
    except (TypeError, ValueError, IndexError):
        return 0.0


def wilson_lower_bound(
    successes: float,
    observations: float,
    z_score: float = 1.645,
) -> float:
    if observations <= 0:
        return 0.0
    probability = max(0.0, min(1.0, successes / observations))
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / observations
    center = probability + z_squared / (2.0 * observations)
    margin = z_score * (
        (
            probability * (1.0 - probability) / observations
            + z_squared / (4.0 * observations * observations)
        )
        ** 0.5
    )
    return max(0.0, (center - margin) / denominator)


def displayed_side_capacity(levels: list[list[float]]) -> float:
    total = 0.0
    for raw_price, raw_size in levels:
        try:
            price = float(raw_price)
            size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            total += price * size
    return total


def displayed_side_quantity(levels: list[list[float]]) -> float:
    total = 0.0
    for raw_price, raw_size in levels:
        try:
            price = float(raw_price)
            size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            total += size
    return total


def valid_orderbook(book: dict[str, Any]) -> bool:
    try:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(
            book.get("best_bid")
            if book.get("best_bid") is not None
            else bids[0][0]
        )
        best_ask = float(
            book.get("best_ask")
            if book.get("best_ask") is not None
            else asks[0][0]
        )
        mid = float(book.get("mid_price"))
    except (IndexError, TypeError, ValueError):
        return False
    return best_bid > 0 and best_ask > best_bid and best_bid < mid < best_ask


def fill_notional(levels: list[list[float]], target_notional: float) -> dict[str, float]:
    remaining = max(0.0, target_notional)
    filled_notional = 0.0
    filled_size = 0.0
    levels_consumed = 0
    best_price = 0.0
    worst_price = 0.0
    for raw_price, raw_size in levels:
        if remaining <= 1e-9:
            break
        price = float(raw_price)
        available_size = float(raw_size)
        available_notional = price * available_size
        if price <= 0 or available_size <= 0:
            continue
        take_notional = min(remaining, available_notional)
        if take_notional <= 0:
            continue
        take_size = take_notional / price
        if levels_consumed == 0:
            best_price = price
        worst_price = price
        levels_consumed += 1
        filled_notional += take_notional
        filled_size += take_size
        remaining -= take_notional
    vwap = filled_notional / filled_size if filled_size > 0 else 0.0
    book_walk_bps = (
        abs(worst_price / best_price - 1.0) * 10_000.0
        if best_price > 0 and worst_price > 0
        else 0.0
    )
    return {
        "requested_notional": target_notional,
        "filled_notional": filled_notional,
        "filled_size": filled_size,
        "vwap": vwap,
        "best_price": best_price,
        "worst_price": worst_price,
        "levels_consumed": float(levels_consumed),
        "book_walk_bps": book_walk_bps,
    }


def fill_quantity(levels: list[list[float]], target_quantity: float) -> dict[str, float]:
    remaining = max(0.0, float(target_quantity))
    filled_notional = 0.0
    filled_size = 0.0
    levels_consumed = 0
    best_price = 0.0
    worst_price = 0.0
    for raw_price, raw_size in levels:
        if remaining <= 1e-12:
            break
        try:
            price = float(raw_price)
            available_size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if price <= 0 or available_size <= 0:
            continue
        take_size = min(remaining, available_size)
        if levels_consumed == 0:
            best_price = price
        worst_price = price
        levels_consumed += 1
        filled_size += take_size
        filled_notional += take_size * price
        remaining -= take_size
    vwap = filled_notional / filled_size if filled_size > 0 else 0.0
    book_walk_bps = (
        abs(worst_price / best_price - 1.0) * 10_000.0
        if best_price > 0 and worst_price > 0
        else 0.0
    )
    return {
        "requested_quantity": target_quantity,
        "requested_notional": target_quantity * best_price,
        "filled_notional": filled_notional,
        "filled_size": filled_size,
        "vwap": vwap,
        "best_price": best_price,
        "worst_price": worst_price,
        "levels_consumed": float(levels_consumed),
        "book_walk_bps": book_walk_bps,
    }


def build_basis_history_profile(
    long_book: dict[str, Any],
    short_book: dict[str, Any],
    horizon_hours: float,
) -> dict[str, Any]:
    def series(book: dict[str, Any]) -> dict[str, float]:
        output: dict[str, float] = {}
        for row in book.get("_history", []):
            observed = parse_timestamp(row.get("observed_at"))
            mid = positive_float(row.get("mid_price"))
            if observed is None or mid <= 0:
                continue
            minute = observed.replace(second=0, microsecond=0).isoformat()
            output[minute] = mid
        return output

    long_series = series(long_book)
    short_series = series(short_book)
    common = sorted(set(long_series).intersection(short_series))
    signed_basis: list[tuple[Any, float]] = []
    for timestamp in common:
        long_mid = long_series[timestamp]
        short_mid = short_series[timestamp]
        reference = (long_mid + short_mid) / 2.0
        if reference > 0:
            parsed = parse_timestamp(timestamp)
            if parsed is not None:
                signed_basis.append((parsed, (short_mid - long_mid) / reference))
    maximum_horizon = max(0.25, float(horizon_hours or 0.25))
    pnl_changes: list[float] = []
    for entry_index, (entry_at, entry_basis) in enumerate(signed_basis):
        for exit_at, exit_basis in signed_basis[entry_index + 1 :]:
            elapsed_hours = (exit_at - entry_at).total_seconds() / 3_600.0
            if elapsed_hours <= 0:
                continue
            if elapsed_hours > maximum_horizon:
                break
            pnl_changes.append(entry_basis - exit_basis)
    weights = [1.0] * len(pnl_changes)
    return {
        "snapshot_count": len(signed_basis),
        "change_sample_count": len(pnl_changes),
        "change_horizon_hours": maximum_horizon,
        "basis_pnl_q25": (
            weighted_quantile(pnl_changes, weights, 0.25) if pnl_changes else 0.0
        ),
        "basis_pnl_median": (
            weighted_quantile(pnl_changes, weights, 0.5) if pnl_changes else 0.0
        ),
        "basis_pnl_q75": (
            weighted_quantile(pnl_changes, weights, 0.75) if pnl_changes else 0.0
        ),
        "latest_signed_mid_basis": signed_basis[-1][1] if signed_basis else None,
    }


def build_basis_scenarios(
    long_open: dict[str, float],
    short_open: dict[str, float],
    history: dict[str, Any],
    config: FundingScanConfig,
) -> dict[str, Any]:
    long_price = positive_float(long_open.get("vwap"))
    short_price = positive_float(short_open.get("vwap"))
    reference = (long_price + short_price) / 2.0
    signed_entry_basis = (
        (short_price - long_price) / reference if reference > 0 else 0.0
    )
    historical_adverse = max(
        0.0,
        -float(history.get("basis_pnl_q25") or 0.0),
    )
    historical_favorable = max(
        0.0,
        float(history.get("basis_pnl_q75") or 0.0),
    )
    absolute_basis = abs(signed_entry_basis)
    structural_widening = (
        absolute_basis * config.basis_stress_fraction
        if absolute_basis > config.large_basis_threshold_bps / 10_000.0
        else 0.0
    )
    adverse_widening = max(
        historical_adverse,
        structural_widening,
    )
    convergence_pnl_rate = signed_entry_basis
    adverse_rate = min(0.0, convergence_pnl_rate, -adverse_widening)
    favorable_rate = max(0.0, convergence_pnl_rate, historical_favorable)
    if absolute_basis <= config.large_basis_threshold_bps / 10_000.0:
        tier = "standard"
    elif absolute_basis <= config.maximum_basis_gap_bps / 10_000.0:
        tier = "conditional"
    elif absolute_basis <= config.maximum_research_basis_gap_bps / 10_000.0:
        tier = "extreme_conditional"
    else:
        tier = "reject"
    return {
        "model_version": "signed_executable_basis_v2_stressed",
        "signed_entry_basis": signed_entry_basis,
        "absolute_entry_basis": absolute_basis,
        "convergence_pnl_rate": convergence_pnl_rate,
        "unfavorable_convergence_loss_rate": max(0.0, -convergence_pnl_rate),
        "historical_adverse_rate": historical_adverse,
        "historical_favorable_rate": historical_favorable,
        "structural_widening_rate": structural_widening,
        "history": history,
        "tier": tier,
        "scenarios": [
            {"name": "adverse", "pnl_rate": adverse_rate, "weight": 0.25},
            {"name": "unchanged", "pnl_rate": 0.0, "weight": 0.50},
            {"name": "convergence", "pnl_rate": favorable_rate, "weight": 0.25},
        ],
    }


def positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def notional_imbalance_ratio(
    first_notional: float,
    second_notional: float,
) -> float | None:
    first = positive_float(first_notional)
    second = positive_float(second_notional)
    if first <= 0 or second <= 0:
        return None
    return max(first, second) / min(first, second)


def directional_slippage(fill: dict[str, float], mid_price: Any, side: str) -> float:
    try:
        mid = float(mid_price)
    except (TypeError, ValueError):
        return 0.0
    if mid <= 0 or fill["vwap"] <= 0:
        return 0.0
    price_cost = fill["vwap"] - mid if side == "buy" else mid - fill["vwap"]
    return max(0.0, price_cost / mid * fill["filled_notional"])


def market_fee_rate(
    market: dict[str, Any],
    liquidity_role: str = "taker",
) -> float:
    return funding_fee_rate(market, liquidity_role)


def valid_reference_prices(market: dict[str, Any]) -> bool:
    try:
        return float(market.get("mark_price")) > 0 and float(market.get("index_price")) > 0
    except (TypeError, ValueError):
        return False


def market_with_book_mark(
    market: dict[str, Any],
    book: dict[str, Any],
) -> dict[str, Any]:
    mark_ready = False
    try:
        if float(market.get("mark_price")) > 0:
            mark_ready = True
    except (TypeError, ValueError):
        mark_ready = False
    index_ready = False
    try:
        if float(market.get("index_price")) > 0:
            index_ready = True
    except (TypeError, ValueError):
        index_ready = False
    needs_index_proxy = (
        str(market.get("index_price_kind") or "")
        == "orderbook_mid_proxy"
    )
    if mark_ready and (index_ready or not needs_index_proxy):
        return market
    try:
        mid_price = float(book.get("mid_price"))
    except (TypeError, ValueError):
        return market
    if mid_price <= 0:
        return market
    enriched = dict(market)
    if not mark_ready:
        enriched["mark_price"] = mid_price
        enriched["mark_price_kind"] = "orderbook_mid"
    if needs_index_proxy and not index_ready:
        enriched["index_price"] = mid_price
        enriched["index_price_kind"] = "orderbook_mid_proxy"
    return enriched


def market_basis(market: dict[str, Any]) -> float:
    mark = float(market.get("mark_price") or 0)
    index = float(market.get("index_price") or 0)
    return mark / index - 1.0 if mark > 0 and index > 0 else 0.0


def relative_basis_gap(long_market: dict[str, Any], short_market: dict[str, Any]) -> float:
    return abs(market_basis(long_market) - market_basis(short_market))


def funding_history_age_hours(
    persistence: dict[str, Any],
    observed_at: str,
) -> float | None:
    observed = parse_timestamp(observed_at)
    history_end = parse_timestamp(persistence.get("history_end_at"))
    if observed is None or history_end is None:
        return None
    return max(0.0, (observed - history_end).total_seconds() / 3600.0)


def market_snapshot_age_seconds(
    market: dict[str, Any],
    observed_at: str,
) -> float:
    scan_time = parse_timestamp(observed_at)
    market_time = parse_timestamp(market.get("observed_at"))
    if scan_time is None or market_time is None:
        return math.inf
    return max(0.0, (scan_time - market_time).total_seconds())


def perp_route_key(asset: str, long_venue: str, short_venue: str) -> str:
    return hashlib.sha256(
        f"perp_perp|{asset}|{long_venue}|{short_venue}".encode("utf-8")
    ).hexdigest()[:24]


def dynamic_weight_rationale(forecast: dict[str, Any]) -> str:
    weights = forecast.get("component_weights") or {}
    progress = weights.get("interval_progress")
    progress_label = (
        f"{float(progress) * 100:.0f}% интервала"
        if progress is not None
        else "зрелость интервала неизвестна"
    )
    return (
        "Dynamic nowcast: "
        f"next {float(weights.get('next_estimate') or 0.0) * 100:.0f}%, "
        f"24ч {float(weights.get('realized_24h') or 0.0) * 100:.0f}%, "
        f"72ч {float(weights.get('realized_72h') or 0.0) * 100:.0f}%, "
        f"14д {float(weights.get('persistence_14d') or 0.0) * 100:.0f}%, "
        f"30д risk {float(weights.get('regime_30d') or 0.0) * 100:.0f}% "
        f"({progress_label}); 90д только structural risk."
    )


def liquidity_rationale(profile: dict[str, Any]) -> str:
    refill_probability = profile.get("refill_probability")
    refill_label = (
        f"{float(refill_probability) * 100:.0f}%"
        if refill_probability is not None
        else "недостаточно shock-событий"
    )
    return (
        f"L2 sequence: минимум {int(profile.get('minimum_snapshot_count') or 0)} "
        f"снимков на ноге, исполнимость ${float(profile.get('target_notional') or 0):,.0f} "
        f"в {float(profile.get('route_executable_fraction') or 0) * 100:.0f}% наблюдений, "
        f"persistence {float(profile.get('route_persistence_score') or 0):.0f}/100, "
        f"refill {refill_label}."
    )
def route_leg(
    side: str,
    market: dict[str, Any],
    book: dict[str, Any],
    open_fill: dict[str, float],
    close_fill: dict[str, float],
    fee_rate: float,
    notional: float,
    next_settlement_at: Any | None = None,
) -> dict[str, Any]:
    has_book_depth = bool(book.get("bids")) and bool(book.get("asks"))
    return {
        "side": side,
        "venue": market["venue"],
        "symbol": market["symbol"],
        "notional": notional,
        "base_quantity": open_fill.get("filled_size", 0.0),
        "funding_rate": market["funding_rate"],
        "raw_funding_rate": market.get("raw_funding_rate", market.get("published_funding_rate", market["funding_rate"])),
        "raw_funding_rate_unit": market.get("raw_funding_rate_unit"),
        "normalized_next_funding_rate": market.get(
            "normalized_next_funding_rate"
        ),
        "rate_estimate_per_settlement": market.get("rate_estimate_per_settlement"),
        "rate_estimate_kind": market.get("rate_estimate_kind"),
        "rate_estimate_lower_bound": market.get("rate_estimate_lower_bound"),
        "rate_estimate_upper_bound": market.get("rate_estimate_upper_bound"),
        "rate_estimate_confidence": market.get("rate_estimate_confidence"),
        "rate_estimate_source": market.get("rate_estimate_source"),
        "rate_estimate_observed_at": market.get("rate_estimate_observed_at"),
        "exact_next_rate_available": market.get("exact_next_rate_available"),
        "rate_estimate_assumptions": market.get("rate_estimate_assumptions"),
        "rate_estimate_risk_flags": market.get("rate_estimate_risk_flags"),
        "funding_rate_unit": market.get("funding_rate_unit"),
        "funding_sign_convention": market.get("funding_sign_convention"),
        "normalization_evidence": market.get("normalization_evidence"),
        "funding_interval_hours": market["funding_interval_hours"],
        "hourly_funding_rate": market["hourly_funding_rate"],
        "funding_rate_kind": market.get("funding_rate_kind"),
        "funding_rate_cap": market.get("funding_rate_cap"),
        "funding_rate_floor": market.get("funding_rate_floor"),
        "funding_rate_cap_floor_state": market_funding_cap_floor_state(market),
        "published_funding_rate": market.get(
            "published_funding_rate",
            market["funding_rate"],
        ),
        "published_funding_interval_hours": market.get(
            "published_funding_interval_hours",
            market["funding_interval_hours"],
        ),
        "funding_display_note": market.get("funding_display_note"),
        "next_funding_at": next_settlement_at or market.get("next_funding_at"),
        "market_request_started_at": market.get("request_started_at"),
        "market_response_received_at": market.get("response_received_at"),
        "response_received_at": book.get("response_received_at")
        or market.get("response_received_at"),
        "normalized_at": market.get("normalized_at"),
        "venue_server_time": market.get("venue_server_time"),
        "source_event_at": market.get("source_event_at"),
        "source_freshness_basis": market.get("source_freshness_basis"),
        "orderbook_request_started_at": book.get("request_started_at"),
        "orderbook_response_received_at": book.get("response_received_at"),
        "orderbook_event_time": book.get("orderbook_event_time"),
        "orderbook_depth_available": bool(
            market.get("orderbook_depth_available")
            or market.get("supports_orderbook_depth")
            or has_book_depth
        ),
        "supports_orderbook_depth": bool(
            market.get("supports_orderbook_depth")
            or market.get("orderbook_depth_available")
            or has_book_depth
        ),
        "mark_price": market.get("mark_price"),
        "index_price": market.get("index_price"),
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "bids": book.get("bids") or [],
        "asks": book.get("asks") or [],
        "vwap": open_fill["vwap"],
        "open_vwap": open_fill["vwap"],
        "close_vwap": close_fill["vwap"],
        "open_book_walk_bps": open_fill.get("book_walk_bps", 0.0),
        "close_book_walk_bps": close_fill.get("book_walk_bps", 0.0),
        "open_levels_consumed": open_fill.get("levels_consumed", 0.0),
        "close_levels_consumed": close_fill.get("levels_consumed", 0.0),
        "filled_notional": open_fill["filled_notional"],
        "fee_rate": fee_rate,
        "taker_fee_rate": market.get("taker_fee_rate", fee_rate),
        "maker_fee_rate": market.get("maker_fee_rate"),
        "fee_source": market.get("fee_source"),
        "fee_evidence": market.get("fee_evidence"),
        "fee_observed_at": market.get("fee_observed_at"),
        "fee_reviewed_at": market.get("fee_reviewed_at"),
        "environment": market.get("environment"),
        "environment_verified": market.get("environment_verified"),
        "endpoint_base_url": market.get("endpoint_base_url"),
        "endpoint_identity_provenance": market.get("endpoint_identity_provenance"),
        "endpoint_client_version": market.get("endpoint_client_version"),
        "endpoint_verified_at": market.get("endpoint_verified_at"),
        "api_product_type": market.get("api_product_type"),
        "market_type": market.get("market_type"),
        "product_type": market.get("product_type"),
        "data_enabled": market.get("data_enabled"),
        "strategy_observation_enabled": market.get("strategy_observation_enabled"),
        "shadow_candidate_enabled": market.get("shadow_candidate_enabled"),
        "paper_enabled": market.get("paper_enabled"),
        "live_enabled": market.get("live_enabled"),
        "execution_model": market.get("execution_model"),
        "settlement_verification_level": market.get("settlement_verification_level"),
        "venue_capability_blockers": market.get("venue_capability_blockers"),
        "stablecoin_route_evaluation": market.get("stablecoin_route_evaluation"),
        "stablecoin_risk": market.get("stablecoin_risk"),
        "status": market.get("status"),
        "contract_status": market.get("contract_status"),
        "contract_kind": market.get("contract_kind"),
        "collateral_asset": market.get("collateral_asset"),
        "quote_asset": market.get("quote_asset"),
        "supports_perpetuals": market.get("supports_perpetuals"),
        "is_linear": market.get("is_linear"),
        "supports_discrete_funding": market.get("supports_discrete_funding"),
        "quantity_step": market.get("quantity_step"),
        "min_quantity": market.get("min_quantity"),
        "min_notional": market.get("min_notional"),
        "open_interest_usd": market.get("open_interest_usd"),
        "volume_24h_usd": market.get("volume_24h_usd"),
        "funding_rate_semantics": market.get("funding_rate_semantics"),
        "position_inclusion_rule": market.get("position_inclusion_rule"),
        "entry_safety_buffer_seconds": market.get("entry_safety_buffer_seconds"),
        "exit_safety_buffer_seconds": market.get("exit_safety_buffer_seconds"),
        "timing_policy_source": market.get("timing_policy_source"),
    }


def market_funding_cap_floor_state(market: dict[str, Any]) -> str | None:
    try:
        rate = float(market.get("funding_rate"))
    except (TypeError, ValueError):
        return None
    cap = finite_float_or_none(market.get("funding_rate_cap"))
    floor = finite_float_or_none(market.get("funding_rate_floor"))
    tolerance = max(1e-10, abs(rate) * 1e-6)
    if cap is not None and abs(rate - cap) <= tolerance:
        return "cap"
    if floor is not None and abs(rate - floor) <= tolerance:
        return "floor"
    return None


def market_funding_rate_at_cap_or_floor(market: dict[str, Any]) -> bool:
    return market_funding_cap_floor_state(market) is not None


def market_funding_rate_unit_outlier(market: dict[str, Any]) -> bool:
    interval_rate = finite_float_or_none(market.get("funding_rate"))
    hourly_rate = finite_float_or_none(market.get("hourly_funding_rate"))
    if (
        hourly_rate is not None
        and abs(hourly_rate) > MAX_ABSOLUTE_HOURLY_FUNDING_RATE
    ):
        return True
    return (
        interval_rate is not None
        and abs(interval_rate) > MAX_ABSOLUTE_INTERVAL_FUNDING_RATE
    )
