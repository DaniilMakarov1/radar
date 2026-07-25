from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Any

from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import funding_hourly_buckets, parse_timestamp


FAST_WINDOW_HOURS = 24
RECENT_WINDOW_HOURS = 72
STABILITY_WINDOW_HOURS = 14 * 24
LIVE_WINDOW_HOURS = 30 * 24
OUTCOME_WEIGHT_HALF_LIFE_HOURS = 72
DEFAULT_NOWCAST_WEIGHT = 0.45
REGIME_FILTER_WEIGHT = 0.05
WINDOW_MINIMUM_POINTS = {
    "realized_24h": 18,
    "recent_72h": 54,
    "stability_14d": 252,
    "live_30d": 540,
}


def paired_settlement_schedule(
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    observed_at: str,
    config: FundingScanConfig,
) -> dict[str, Any]:
    start = parse_timestamp(observed_at)
    if start is None:
        return empty_schedule(config.horizon_mode)
    long_next = normalized_next_settlement(long_market, start)
    short_next = normalized_next_settlement(short_market, start)
    if config.horizon_mode == "next_settlement":
        if long_next is None or short_next is None:
            return empty_schedule(config.horizon_mode)
        # The position must be revalidated at the first cash-flow event. Waiting
        # for the later venue silently assumes an unauthorised extra holding period.
        end = min(long_next, short_next)
    else:
        horizon_hours = max(0.0, float(config.horizon_hours or 0.0))
        end = start + timedelta(hours=horizon_hours)

    events: list[tuple[datetime, str, float]] = []
    events.extend(settlement_events(long_market, long_next, end, "long"))
    events.extend(settlement_events(short_market, short_next, end, "short"))
    events.sort(key=lambda item: (item[0], item[1]))

    long_coverage = 0.0
    short_coverage = 0.0
    paired_coverage = 0.0
    rows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(events):
        event_at = events[cursor][0]
        settled_sides: list[str] = []
        long_event_interval = 0.0
        short_event_interval = 0.0
        while cursor < len(events) and events[cursor][0] == event_at:
            _, side, interval_hours = events[cursor]
            if side == "long":
                long_coverage += interval_hours
                long_event_interval += interval_hours
            else:
                short_coverage += interval_hours
                short_event_interval += interval_hours
            settled_sides.append(side)
            cursor += 1
        new_paired_coverage = min(long_coverage, short_coverage)
        added_coverage = max(0.0, new_paired_coverage - paired_coverage)
        paired_coverage = new_paired_coverage
        rows.append(
            {
                "settlement_at": event_at.isoformat(),
                "hours_from_start": max(
                    0.0,
                    (event_at - start).total_seconds() / 3600.0,
                ),
                "paired_coverage_hours": added_coverage,
                "cumulative_paired_coverage_hours": paired_coverage,
                "long_settlement_interval_hours": long_event_interval,
                "short_settlement_interval_hours": short_event_interval,
                "settled_sides": sorted(set(settled_sides)),
            }
        )

    horizon_hours = max(0.0, (end - start).total_seconds() / 3600.0)
    authorization_times = [value for value in (long_next, short_next) if value]
    return {
        "horizon_mode": config.horizon_mode,
        "horizon_hours": horizon_hours,
        "horizon_label": horizon_label(config.horizon_mode, horizon_hours),
        "research_only": config.research_only,
        "authorization_valid_until": (
            min(authorization_times).isoformat() if authorization_times else None
        ),
        "long_next_settlement_at": long_next.isoformat() if long_next else None,
        "short_next_settlement_at": short_next.isoformat() if short_next else None,
        "paired_coverage_hours": paired_coverage,
        "settlement_event_count": len(rows),
        "long_settlement_count": sum(
            float(row["long_settlement_interval_hours"]) > 0 for row in rows
        ),
        "short_settlement_count": sum(
            float(row["short_settlement_interval_hours"]) > 0 for row in rows
        ),
        "settlements": rows,
    }


def build_funding_forecast(
    long_history: list[dict[str, Any]],
    short_history: list[dict[str, Any]],
    current_hourly_spread: float,
    schedule: dict[str, Any],
    config: FundingScanConfig,
    *,
    long_market: dict[str, Any] | None = None,
    short_market: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    series = aligned_spread_series(long_history, short_history)
    # Realized windows end at the last fully settled common hour. Anchoring them
    # to wall-clock time falsely removes valid observations late in an 8h cycle.
    window_end = series[-1][0] if series else parse_timestamp(observed_at)
    fast_series = trailing_series(series, FAST_WINDOW_HOURS, window_end)
    recent_series = trailing_series(series, RECENT_WINDOW_HOURS, window_end)
    stability_series = trailing_series(series, STABILITY_WINDOW_HOURS, window_end)
    live_series = trailing_series(series, LIVE_WINDOW_HOURS, window_end)
    fast_stats = window_stats(fast_series)
    recent_stats = window_stats(recent_series)
    stability_stats = window_stats(stability_series)
    live_stats = window_stats(live_series)
    risk_stats = window_stats(series)
    window_readiness = {
        "realized_24h": fast_stats["point_count"]
        >= WINDOW_MINIMUM_POINTS["realized_24h"],
        "recent_72h": recent_stats["point_count"]
        >= WINDOW_MINIMUM_POINTS["recent_72h"],
        "stability_14d": stability_stats["point_count"]
        >= WINDOW_MINIMUM_POINTS["stability_14d"],
        "live_30d": live_stats["point_count"]
        >= WINDOW_MINIMUM_POINTS["live_30d"],
    }
    required_window_readiness = {
        "realized_24h": window_readiness["realized_24h"],
    }

    component_weights = forecast_component_weights(
        long_market,
        short_market,
        observed_at,
    )
    pair_component_values = {
        "next_estimate": current_hourly_spread,
        "realized_24h": fast_stats["mean"],
        "realized_72h": recent_stats["mean"],
        "persistence_14d": stability_stats["median"],
        "regime_30d": live_stats["median"],
    }
    weighted_signal = blended_metric(
        weighted_component_rows(
            pair_component_values,
            component_weights,
            window_readiness,
        )
    )

    long_leg_signal = funding_leg_signal(
        long_history,
        long_market,
        window_readiness,
        observed_at,
    )
    short_leg_signal = funding_leg_signal(
        short_history,
        short_market,
        window_readiness,
        observed_at,
    )
    if long_market is not None and short_market is not None:
        weighted_signal = short_leg_signal["hourly_rate"] - long_leg_signal["hourly_rate"]
        pair_component_values = {
            name: float(short_leg_signal["components"][name])
            - float(long_leg_signal["components"][name])
            for name in pair_component_values
        }
    directional_confidence = blended_metric(
        (
            (1.0 if current_hourly_spread > 0 else 0.0, component_weights["next_estimate"], True),
            (
                fast_stats["positive_fraction"],
                component_weights["realized_24h"],
                window_readiness["realized_24h"],
            ),
            (
                recent_stats["positive_fraction"],
                component_weights["realized_72h"],
                window_readiness["recent_72h"],
            ),
            (
                stability_stats["positive_fraction"],
                component_weights["persistence_14d"],
                window_readiness["stability_14d"],
            ),
        )
    )
    live_z_score = robust_z_score(
        current_hourly_spread,
        [value for _, value in live_series],
    )
    live_percentile = empirical_percentile(
        current_hourly_spread,
        [value for _, value in live_series],
    )
    long_z_score = robust_z_score(
        current_hourly_spread,
        [value for _, value in series],
    )
    current_percentile = empirical_percentile(
        current_hourly_spread,
        [value for _, value in series],
    )
    recent_to_live_volatility_ratio = safe_ratio(
        recent_stats["stdev"],
        live_stats["stdev"],
    )
    live_to_long_volatility_ratio = safe_ratio(
        live_stats["stdev"],
        risk_stats["stdev"],
    )
    live_anomaly_penalty = anomaly_risk_penalty(
        live_z_score,
        recent_to_live_volatility_ratio,
    )
    long_anomaly_penalty = anomaly_risk_penalty(
        long_z_score,
        live_to_long_volatility_ratio,
    )
    anomaly_penalty = min(live_anomaly_penalty, long_anomaly_penalty)
    anchor = 0.0
    if (
        current_hourly_spread > 0
        and weighted_signal > 0
        and fast_stats["mean"] > 0
        and window_readiness["realized_24h"]
    ):
        anchor = weighted_signal * directional_confidence * anomaly_penalty

    horizon_hours = float(schedule.get("horizon_hours") or 0.0)
    window_hours = max(1, int(round(horizon_hours)))
    selected_horizon_hours = max(
        1,
        int(math.ceil(float(schedule.get("horizon_hours") or horizon_hours or 1.0))),
    )
    regime = regime_diagnostics(
        series,
        forecast_horizons=(4, 8, 24, selected_horizon_hours),
    )
    half_life, decay_points, half_life_censored = estimate_half_life(live_series)
    samples = settlement_forward_samples(
        long_history,
        short_history,
        window_hours,
        current_hourly_spread,
        observed_at=observed_at,
    )
    sample_weights = [float(sample["recency_weight"]) for sample in samples]
    schedule_directional_confidence = (
        weighted_mean(
            [
                1.0 if float(sample["actual_cumulative_rate"]) > 0 else 0.0
                for sample in samples
            ],
            sample_weights,
        )
        if samples
        else 0.0
    )
    schedule_rows = list(schedule.get("settlements", []))
    settlement_schedule_asymmetric = (
        str(schedule.get("horizon_mode") or "") == "next_settlement"
        and any(
            (float(row.get("long_settlement_interval_hours") or 0.0) > 0)
            != (float(row.get("short_settlement_interval_hours") or 0.0) > 0)
            for row in schedule_rows
        )
    )
    schedule_nowcast_rate = 0.0
    if long_market is not None and short_market is not None:
        schedule_nowcast_rate = sum(
            float(short_market.get("hourly_funding_rate") or 0.0)
            * float(row.get("short_settlement_interval_hours") or 0.0)
            - float(long_market.get("hourly_funding_rate") or 0.0)
            * float(row.get("long_settlement_interval_hours") or 0.0)
            for row in schedule_rows
        )

    raw_settlement_rows: list[dict[str, Any]] = []
    raw_settlement_rate = 0.0
    long_settlement_index = 0
    short_settlement_index = 0
    for row in schedule_rows:
        long_interval = float(row.get("long_settlement_interval_hours") or 0.0)
        short_interval = float(row.get("short_settlement_interval_hours") or 0.0)
        if long_interval <= 0 and short_interval <= 0:
            paired_interval = float(row.get("paired_coverage_hours") or 0.0)
            event_raw_rate = weighted_signal * paired_interval
        else:
            long_hourly_rate = (
                float(long_leg_signal["hourly_rate"])
                if long_interval > 0 and long_settlement_index == 0
                else float(long_leg_signal["future_hourly_rate"])
            )
            short_hourly_rate = (
                float(short_leg_signal["hourly_rate"])
                if short_interval > 0 and short_settlement_index == 0
                else float(short_leg_signal["future_hourly_rate"])
            )
            event_raw_rate = (
                short_hourly_rate * short_interval
                - long_hourly_rate * long_interval
            )
        if long_interval > 0:
            long_settlement_index += 1
        if short_interval > 0:
            short_settlement_index += 1
        raw_settlement_rate += event_raw_rate
        raw_settlement_rows.append(
            {
                **row,
                "raw_event_rate": event_raw_rate,
            }
        )

    forecast_directional_confidence = directional_confidence
    forecast_anomaly_penalty = anomaly_penalty
    schedule_z_score = None
    schedule_anomaly_penalty = 1.0
    if (
        settlement_schedule_asymmetric
        and long_market is not None
        and short_market is not None
    ):
        schedule_z_score = robust_z_score(
            schedule_nowcast_rate,
            [float(sample["actual_cumulative_rate"]) for sample in samples],
        )
        schedule_anomaly_penalty = anomaly_risk_penalty(schedule_z_score, None)
        forecast_directional_confidence = schedule_directional_confidence
        forecast_anomaly_penalty = schedule_anomaly_penalty
        interval_exposure = sum(
            max(
                float(row.get("long_settlement_interval_hours") or 0.0),
                float(row.get("short_settlement_interval_hours") or 0.0),
            )
            for row in schedule_rows
        )
        if (
            schedule_nowcast_rate > 0
            and raw_settlement_rate > 0
            and schedule_directional_confidence > 0
            and window_readiness["realized_24h"]
        ):
            anchor = raw_settlement_rate / max(interval_exposure, 1.0)

    settlement_rows: list[dict[str, Any]] = []
    decay_cumulative_rate = 0.0
    for row in raw_settlement_rows:
        event_hour = float(row.get("hours_from_start") or 0.0)
        long_interval = float(row.get("long_settlement_interval_hours") or 0.0)
        short_interval = float(row.get("short_settlement_interval_hours") or 0.0)
        event_raw_rate = float(row["raw_event_rate"])
        decay = math.pow(0.5, event_hour / max(half_life, 1.0))
        positive_scale = (
            forecast_directional_confidence
            * forecast_anomaly_penalty
            * float(regime["age_penalty"])
            * decay
            if anchor > 0
            else 0.0
        )
        forecast_event_rate = (
            event_raw_rate * positive_scale
            if event_raw_rate > 0
            else event_raw_rate
        )
        decay_cumulative_rate += forecast_event_rate
        settlement_rows.append(
            {
                **row,
                "raw_event_rate": event_raw_rate,
                "forecast_event_rate": forecast_event_rate,
                "forecast_hourly_spread": (
                    forecast_event_rate / max(long_interval, short_interval, 1.0)
                ),
                "forecast_cumulative_rate": decay_cumulative_rate,
            }
        )
    scenario_outcomes = [
        {
            "gross_rate": (
                decay_cumulative_rate
                * bounded(sample["carry_ratio"], -2.0, 3.0)
                if decay_cumulative_rate > 0
                else decay_cumulative_rate
            ),
            "weight": sample["recency_weight"],
        }
        for sample in samples
    ]

    if scenario_outcomes:
        scenario_median = weighted_quantile(
            [row["gross_rate"] for row in scenario_outcomes],
            [row["weight"] for row in scenario_outcomes],
            0.5,
        )
        if scenario_median > 0 and decay_cumulative_rate >= 0:
            positive_scale = min(1.0, decay_cumulative_rate / scenario_median)
            for outcome in scenario_outcomes:
                if outcome["gross_rate"] > 0:
                    outcome["gross_rate"] *= positive_scale

    scenario_rates = [row["gross_rate"] for row in scenario_outcomes]
    scenario_weights = [row["weight"] for row in scenario_outcomes]
    gross_rate_median = weighted_quantile(
        scenario_rates,
        scenario_weights,
        0.5,
    )
    gross_rate_q25 = (
        weighted_quantile(
            scenario_rates,
            scenario_weights,
            config.conservative_quantile,
        )
        if scenario_rates
        else 0.0
    )
    gross_positive_probability = (
        weighted_mean(
            [1.0 if value > 0 else 0.0 for value in scenario_rates],
            scenario_weights,
        )
        if scenario_rates
        else 0.0
    )
    return {
        "model_version": "funding_forecast_v10_settlement_aware",
        "sample_count": len(scenario_rates),
        "effective_sample_count": effective_sample_size(scenario_weights),
        "gross_rate_median": gross_rate_median,
        "gross_rate_q25": gross_rate_q25,
        "gross_rate_mean": weighted_mean(scenario_rates, scenario_weights),
        "gross_positive_probability": gross_positive_probability,
        "raw_settlement_rate": raw_settlement_rate,
        "decay_cumulative_rate": decay_cumulative_rate,
        "decay_half_life_hours": half_life,
        "decay_half_life_censored": half_life_censored,
        "decay_to_quarter_hours": half_life * 2.0,
        "decay_points": decay_points,
        "anchor_hourly_spread": anchor,
        "current_hourly_spread": current_hourly_spread,
        "historical_median_hourly_spread": weighted_signal,
        "weighted_expected_hourly_spread": weighted_signal,
        "positive_spread_fraction": forecast_directional_confidence,
        "pair_positive_spread_fraction": directional_confidence,
        "schedule_positive_carry_fraction": schedule_directional_confidence,
        "settlement_schedule_asymmetric": settlement_schedule_asymmetric,
        "schedule_nowcast_rate": schedule_nowcast_rate,
        "component_weights": component_weights,
        "component_values": pair_component_values,
        "regime_30d_is_risk_filter": True,
        "window_readiness": window_readiness,
        "required_window_readiness": required_window_readiness,
        "realized_window_end_at": window_end.isoformat() if window_end else None,
        "leg_signals": {
            "long": long_leg_signal,
            "short": short_leg_signal,
        },
        "lookback_windows": {
            "realized_24h": fast_stats,
            "recent_72h": recent_stats,
            "stability_14d": stability_stats,
            "live_30d": live_stats,
            "risk_90d": risk_stats,
        },
        "live_window_hours": LIVE_WINDOW_HOURS,
        "outcome_weight_half_life_hours": OUTCOME_WEIGHT_HALF_LIFE_HOURS,
        "live_window_z_score": live_z_score,
        "live_window_percentile": live_percentile,
        "long_window_z_score": long_z_score,
        "long_window_percentile": current_percentile,
        "recent_to_live_volatility_ratio": recent_to_live_volatility_ratio,
        "live_to_long_volatility_ratio": live_to_long_volatility_ratio,
        "live_window_anomaly_penalty": live_anomaly_penalty,
        "long_window_anomaly_penalty": long_anomaly_penalty,
        "anomaly_risk_penalty": forecast_anomaly_penalty,
        "pair_anomaly_risk_penalty": anomaly_penalty,
        "schedule_z_score": schedule_z_score,
        "schedule_anomaly_penalty": schedule_anomaly_penalty,
        "regime_age_hours": regime["age_hours"],
        "regime_age_percentile": regime["age_percentile"],
        "regime_age_penalty": regime["age_penalty"],
        "positive_run_count": regime["positive_run_count"],
        "completed_positive_run_count": regime["completed_positive_run_count"],
        "duration_sample_count": regime["duration_sample_count"],
        "estimated_remaining_hours_q25": regime["remaining_hours_q25"],
        "estimated_remaining_hours_median": regime["remaining_hours_median"],
        "estimated_remaining_hours_q75": regime["remaining_hours_q75"],
        "survival_probabilities": regime["survival_probabilities"],
        "selected_horizon_survival_probability": regime[
            "survival_probabilities"
        ].get(str(selected_horizon_hours)),
        "settlements": settlement_rows,
        "_scenario_outcomes": scenario_outcomes,
        "_historical_outcomes": [
            {
                "gross_rate": sample["actual_cumulative_rate"],
                "weight": sample["recency_weight"],
            }
            for sample in samples
        ],
    }


def aligned_spread_series(
    long_history: list[dict[str, Any]],
    short_history: list[dict[str, Any]],
) -> list[tuple[datetime, float]]:
    long_buckets = funding_hourly_buckets(long_history)
    short_buckets = funding_hourly_buckets(short_history)
    common_hours = sorted(set(long_buckets).intersection(short_buckets))
    return [
        (hour, short_buckets[hour] - long_buckets[hour])
        for hour in common_hours
    ]


def trailing_series(
    series: list[tuple[datetime, float]],
    hours: int,
    end_at: datetime | None = None,
) -> list[tuple[datetime, float]]:
    if not series:
        return []
    anchor = (end_at or series[-1][0]).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    cutoff = anchor - timedelta(hours=max(1, hours) - 1)
    return [row for row in series if cutoff <= row[0] <= anchor]


def window_stats(series: list[tuple[datetime, float]]) -> dict[str, float | int]:
    values = [value for _, value in series]
    flips = 0
    for left, right in zip(values, values[1:]):
        if (left > 0 >= right) or (left <= 0 < right):
            flips += 1
    span_hours = (
        max(1.0, (series[-1][0] - series[0][0]).total_seconds() / 3600.0 + 1.0)
        if series
        else 0.0
    )
    return {
        "point_count": len(values),
        "median": statistics.median(values) if values else 0.0,
        "mean": statistics.fmean(values) if values else 0.0,
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "positive_fraction": (
            sum(value > 0 for value in values) / len(values) if values else 0.0
        ),
        "sign_flip_count": flips,
        "sign_flips_per_day": flips / max(span_hours / 24.0, 1.0),
    }


def blended_metric(
    components: tuple[tuple[float | int, float, bool], ...],
) -> float:
    included = [
        (float(value), float(weight))
        for value, weight, available in components
        if available and weight > 0
    ]
    # Weights are allocations, not coefficients to renormalize into a moving
    # average. The remaining 5% belongs to the 30d risk filter, and unavailable
    # optional history should conservatively shrink the signal instead of
    # increasing the weight of the nowcast.
    return sum(value * weight for value, weight in included)


def weighted_component_rows(
    values: dict[str, float | int],
    weights: dict[str, float | bool | None],
    readiness: dict[str, bool],
) -> tuple[tuple[float | int, float, bool], ...]:
    return (
        (values["next_estimate"], float(weights["next_estimate"] or 0.0), True),
        (
            values["realized_24h"],
            float(weights["realized_24h"] or 0.0),
            readiness["realized_24h"],
        ),
        (
            values["realized_72h"],
            float(weights["realized_72h"] or 0.0),
            readiness["recent_72h"],
        ),
        (
            values["persistence_14d"],
            float(weights["persistence_14d"] or 0.0),
            readiness["stability_14d"],
        ),
    )


def funding_leg_signal(
    history: list[dict[str, Any]],
    market: dict[str, Any] | None,
    readiness: dict[str, bool],
    observed_at: str | None,
) -> dict[str, Any]:
    buckets = funding_hourly_buckets(history)
    series = sorted(buckets.items())
    window_end = series[-1][0] if series else parse_timestamp(observed_at)
    fast = window_stats(trailing_series(series, FAST_WINDOW_HOURS, window_end))
    recent = window_stats(trailing_series(series, RECENT_WINDOW_HOURS, window_end))
    stability = window_stats(trailing_series(series, STABILITY_WINDOW_HOURS, window_end))
    live = window_stats(trailing_series(series, LIVE_WINDOW_HOURS, window_end))
    components = {
        "next_estimate": float((market or {}).get("hourly_funding_rate") or 0.0),
        "realized_24h": float(fast["mean"]),
        "realized_72h": float(recent["mean"]),
        "persistence_14d": float(stability["median"]),
        "regime_30d": float(live["median"]),
    }
    weights = funding_market_component_weights(market, observed_at)
    future_weights = {
        **weights,
        "next_estimate": 0.0,
    }
    return {
        "hourly_rate": blended_metric(
            weighted_component_rows(components, weights, readiness)
        ),
        "future_hourly_rate": blended_metric(
            weighted_component_rows(components, future_weights, readiness)
        ),
        "components": components,
        "weights": weights,
    }


def forecast_component_weights(
    long_market: dict[str, Any] | None,
    short_market: dict[str, Any] | None,
    observed_at: str | None,
) -> dict[str, float | bool | None]:
    long_weights = funding_market_component_weights(long_market, observed_at)
    short_weights = funding_market_component_weights(short_market, observed_at)
    long_progress = long_weights["interval_progress"]
    short_progress = short_weights["interval_progress"]
    if long_market is None and short_market is None:
        interval_progress = None
        nowcast_weight = DEFAULT_NOWCAST_WEIGHT
    elif long_progress is None or short_progress is None:
        interval_progress = None
        nowcast_weight = 0.15
    else:
        # A spread nowcast is only as mature as its less mature leg.
        interval_progress = min(long_progress, short_progress)
        nowcast_weight = min(
            float(long_weights["next_estimate"]),
            float(short_weights["next_estimate"]),
        )
    source_trusted = bool(
        long_weights["source_trusted"] and short_weights["source_trusted"]
    )
    return component_weight_mix(nowcast_weight) | {
        "next_estimate": nowcast_weight,
        "interval_progress": interval_progress,
        "long_interval_progress": long_progress,
        "short_interval_progress": short_progress,
        "source_trusted": source_trusted,
    }


def funding_market_component_weights(
    market: dict[str, Any] | None,
    observed_at: str | None,
) -> dict[str, float | bool | None]:
    if market is None:
        return component_weight_mix(DEFAULT_NOWCAST_WEIGHT) | {
            "interval_progress": None,
            "source_trusted": True,
        }
    progress = funding_interval_progress(market, observed_at)
    nowcast_weight = dynamic_nowcast_weight(progress) if progress is not None else 0.15
    source_trusted = funding_nowcast_source_trusted(market)
    if not source_trusted:
        nowcast_weight = min(nowcast_weight, 0.15)
    return component_weight_mix(nowcast_weight) | {
        "interval_progress": progress,
        "source_trusted": source_trusted,
    }


def component_weight_mix(
    nowcast_weight: float,
) -> dict[str, float]:
    normalized_nowcast = bounded(nowcast_weight, 0.0, 1.0 - REGIME_FILTER_WEIGHT)
    historical_pool = 1.0 - REGIME_FILTER_WEIGHT - normalized_nowcast
    return {
        "next_estimate": normalized_nowcast,
        "realized_24h": historical_pool * 0.50,
        "realized_72h": historical_pool * 0.30,
        "persistence_14d": historical_pool * 0.20,
        "regime_30d": REGIME_FILTER_WEIGHT,
    }


def funding_interval_progress(
    market: dict[str, Any] | None,
    observed_at: str | None,
) -> float | None:
    if not market:
        return None
    observed = parse_timestamp(observed_at or market.get("observed_at"))
    if observed is None:
        return None
    next_settlement = normalized_next_settlement(market, observed)
    try:
        interval_hours = float(market.get("funding_interval_hours") or 0.0)
    except (TypeError, ValueError):
        return None
    if next_settlement is None or interval_hours <= 0:
        return None
    remaining_hours = max(
        0.0,
        (next_settlement - observed).total_seconds() / 3600.0,
    )
    return bounded(1.0 - remaining_hours / interval_hours, 0.0, 1.0)


def dynamic_nowcast_weight(interval_progress: float) -> float:
    progress = bounded(interval_progress, 0.0, 1.0)
    if progress <= 0.25:
        return 0.15 + 0.10 * progress / 0.25
    if progress <= 0.75:
        return 0.25 + 0.20 * (progress - 0.25) / 0.50
    return 0.45 + 0.15 * (progress - 0.75) / 0.25


def funding_nowcast_source_trusted(market: dict[str, Any]) -> bool:
    source = str(market.get("funding_rate_kind") or "")
    return source in {
        "published_next_estimate",
        "published_current_estimate",
        "published_current_hourly",
        "published_predicted_next",
        "published_next_hour",
        "published_next_hour_prediction",
        "published_8h_estimate",
        "published_8h_equivalent_normalized_hourly",
        "published_continuous_hourly_equivalent",
        "test",
    }


def robust_z_score(value: float, history: list[float]) -> float | None:
    if len(history) < 24:
        return None
    center = statistics.median(history)
    mad = statistics.median(abs(item - center) for item in history)
    scale = 1.4826 * mad
    if scale <= 1e-12:
        scale = max(statistics.pstdev(history), abs(center) * 0.25, 1e-8)
    return (value - center) / scale


def empirical_percentile(value: float, history: list[float]) -> float | None:
    if not history:
        return None
    return sum(item <= value for item in history) / len(history)


def safe_ratio(numerator: float | int, denominator: float | int) -> float | None:
    return float(numerator) / float(denominator) if float(denominator) > 1e-12 else None


def anomaly_risk_penalty(
    z_score: float | None,
    volatility_ratio: float | None,
) -> float:
    z_penalty = 1.0
    if z_score is not None and abs(z_score) > 2.0:
        z_penalty = max(0.5, math.exp(-0.18 * (abs(z_score) - 2.0)))
    volatility_penalty = 1.0
    if volatility_ratio is not None and volatility_ratio > 1.5:
        volatility_penalty = max(0.6, 1.5 / volatility_ratio)
    return min(z_penalty, volatility_penalty)


def forward_samples(
    series: list[tuple[datetime, float]],
    horizon_hours: int,
    current_hourly_spread: float,
) -> list[dict[str, float]]:
    if horizon_hours <= 0 or len(series) <= horizon_hours:
        return []
    step = max(1, horizon_hours)
    current = max(current_hourly_spread, 1e-12)
    lower = current * 0.2
    upper = current * 5.0

    def collect(conditioned: bool) -> list[dict[str, float]]:
        output = []
        for index in range(0, len(series) - horizon_hours, step):
            entry_at, entry = series[index]
            if entry <= 0:
                continue
            if conditioned and not lower <= entry <= upper:
                continue
            window = series[index + 1 : index + horizon_hours + 1]
            if not window or window[-1][0] != entry_at + timedelta(hours=horizon_hours):
                continue
            if any(
                right[0] - left[0] != timedelta(hours=1)
                for left, right in zip(series[index : index + horizon_hours], window)
            ):
                continue
            actual = sum(value for _, value in window)
            outcome_age_hours = max(
                0.0,
                (series[-1][0] - window[-1][0]).total_seconds() / 3600.0,
            )
            output.append(
                {
                    "entry_hourly_spread": entry,
                    "actual_cumulative_rate": actual,
                    "carry_ratio": actual / (entry * horizon_hours),
                    "age_hours": outcome_age_hours,
                    "recency_weight": 0.1
                    + 0.9
                    * math.pow(
                        0.5,
                        outcome_age_hours / OUTCOME_WEIGHT_HALF_LIFE_HOURS,
                    ),
                }
            )
        return output

    conditioned = collect(True)
    return conditioned if len(conditioned) >= 20 else collect(False)


def settlement_forward_samples(
    long_history: list[dict[str, Any]],
    short_history: list[dict[str, Any]],
    horizon_hours: int,
    current_hourly_spread: float,
    *,
    observed_at: str | None = None,
) -> list[dict[str, float]]:
    """Build non-overlapping outcomes from funding events known after entry.

    Hourly-expanded funding is useful for regime statistics, but using a final
    settled rate as the entry signal leaks information from the end of its
    interval. This walk-forward uses only the last settlement known at entry and
    sums actual cash-flow events that occur after entry.
    """
    if horizon_hours <= 0:
        return []
    long_events = historical_funding_events(long_history)
    short_events = historical_funding_events(short_history)
    if len(long_events) < 2 or len(short_events) < 2:
        return []
    long_times = [row[0] for row in long_events]
    short_times = [row[0] for row in short_events]
    start = max(long_times[0], short_times[0])
    latest_complete = min(long_times[-1], short_times[-1])
    requested_end = parse_timestamp(observed_at)
    if requested_end is not None:
        latest_complete = min(latest_complete, requested_end)
    # The 90-day dataset is structural risk context only. Forward outcomes used
    # in the live PnL forecast are limited to the 30-day live regime and then
    # strongly recency-weighted toward the latest 72 hours.
    start = max(start, latest_complete - timedelta(hours=LIVE_WINDOW_HOURS))
    last_entry = latest_complete - timedelta(hours=horizon_hours)
    if last_entry <= start:
        return []

    stride = timedelta(hours=max(1, horizon_hours))
    current = max(current_hourly_spread, 1e-12)
    lower = current * 0.2
    upper = current * 5.0

    def collect(conditioned: bool) -> list[dict[str, float]]:
        output: list[dict[str, float]] = []
        entry_at = start
        while entry_at <= last_entry:
            end_at = entry_at + timedelta(hours=horizon_hours)
            long_known_index = bisect_right(long_times, entry_at) - 1
            short_known_index = bisect_right(short_times, entry_at) - 1
            if long_known_index < 0 or short_known_index < 0:
                entry_at += stride
                continue
            future_long = long_events[
                long_known_index + 1 : bisect_right(long_times, end_at)
            ]
            future_short = short_events[
                short_known_index + 1 : bisect_right(short_times, end_at)
            ]
            if not future_long and not future_short:
                entry_at += stride
                continue

            long_known_hourly = long_events[long_known_index][2]
            short_known_hourly = short_events[short_known_index][2]
            expected_rate = (
                short_known_hourly * sum(row[3] for row in future_short)
                - long_known_hourly * sum(row[3] for row in future_long)
            )
            expected_hourly = expected_rate / max(horizon_hours, 1)
            if expected_rate <= 0 or (
                conditioned and not lower <= expected_hourly <= upper
            ):
                entry_at += stride
                continue

            actual_rate = sum(row[1] for row in future_short) - sum(
                row[1] for row in future_long
            )
            age_hours = max(
                0.0,
                (latest_complete - end_at).total_seconds() / 3600.0,
            )
            output.append(
                {
                    "entry_hourly_spread": expected_hourly,
                    "expected_cumulative_rate": expected_rate,
                    "actual_cumulative_rate": actual_rate,
                    "carry_ratio": actual_rate / expected_rate,
                    "age_hours": age_hours,
                    "recency_weight": 0.1
                    + 0.9
                    * math.pow(
                        0.5,
                        age_hours / OUTCOME_WEIGHT_HALF_LIFE_HOURS,
                    ),
                }
            )
            entry_at += stride
        return output

    conditioned = collect(True)
    return conditioned if len(conditioned) >= 20 else collect(False)


def historical_funding_events(
    rows: list[dict[str, Any]],
) -> list[tuple[datetime, float, float, float]]:
    events: dict[datetime, tuple[datetime, float, float, float]] = {}
    for row in rows:
        settled_at = parse_timestamp(row.get("funding_at"))
        if settled_at is None:
            continue
        try:
            interval = max(0.25, float(row.get("funding_interval_hours") or 1.0))
            rate = float(row.get("funding_rate"))
            hourly = float(row.get("hourly_funding_rate"))
        except (TypeError, ValueError):
            continue
        events[settled_at] = (settled_at, rate, hourly, interval)
    return [events[key] for key in sorted(events)]


def estimate_half_life(
    series: list[tuple[datetime, float]],
) -> tuple[float, list[dict[str, float]], bool]:
    points: list[dict[str, float]] = []
    estimates: list[float] = []
    for lag in (4, 8, 12, 24):
        ratios = []
        for index in range(0, len(series) - lag, 8):
            entry_at, entry = series[index]
            if entry <= 0:
                continue
            terminal_at, terminal = series[index + lag]
            if terminal_at - entry_at != timedelta(hours=lag):
                continue
            ratios.append(terminal / entry)
        if not ratios:
            continue
        median_ratio = bounded(statistics.median(ratios), -2.0, 3.0)
        points.append({"lag_hours": float(lag), "median_terminal_ratio": median_ratio})
        if 0.0 < median_ratio < 1.0:
            estimates.append(-lag * math.log(2.0) / math.log(median_ratio))
        elif median_ratio <= 0.0:
            estimates.append(float(lag))
    if not estimates:
        return 24.0, points, True
    return bounded(statistics.median(estimates), 1.0, 24.0), points, False


def regime_diagnostics(
    series: list[tuple[datetime, float]],
    forecast_horizons: tuple[int, ...] = (4, 8, 24),
) -> dict[str, Any]:
    completed_runs: list[int] = []
    run = 0
    previous_at: datetime | None = None
    for observed_at, value in series:
        continuous = previous_at is None or observed_at - previous_at == timedelta(hours=1)
        if value > 0 and continuous:
            run += 1
        elif value > 0:
            if run:
                completed_runs.append(run)
            run = 1
        elif run:
            completed_runs.append(run)
            run = 0
        previous_at = observed_at
    age_hours = run if series and series[-1][1] > 0 else 0
    minimum_run = max(1, age_hours)
    comparable_runs = [value for value in completed_runs if value >= minimum_run]
    remaining = [max(0, value - age_hours) for value in comparable_runs]
    survival_probabilities = {
        str(max(1, int(hours))): (
            sum(value >= age_hours + max(1, int(hours)) for value in comparable_runs)
            / len(comparable_runs)
            if comparable_runs
            else None
        )
        for hours in sorted(set(forecast_horizons))
    }
    percentile = (
        sum(value <= age_hours for value in completed_runs) / len(completed_runs)
        if completed_runs and age_hours > 0
        else 0.0
    )
    evidence_weight = min(1.0, len(completed_runs) / 20.0)
    penalty = (
        max(0.5, 1.0 - 0.5 * percentile * evidence_weight)
        if age_hours > 0
        else 1.0
    )
    return {
        "age_hours": age_hours,
        "age_percentile": percentile,
        "age_penalty": penalty,
        "positive_run_count": len(completed_runs) + (1 if age_hours > 0 else 0),
        "completed_positive_run_count": len(completed_runs),
        "duration_sample_count": len(comparable_runs),
        "remaining_hours_q25": quantile(remaining, 0.25) if remaining else None,
        "remaining_hours_median": quantile(remaining, 0.5) if remaining else None,
        "remaining_hours_q75": quantile(remaining, 0.75) if remaining else None,
        "survival_probabilities": survival_probabilities,
    }


def normalized_next_settlement(
    market: dict[str, Any],
    start: datetime,
) -> datetime | None:
    next_funding = parse_timestamp(market.get("next_funding_at"))
    try:
        interval_hours = max(1.0, float(market.get("funding_interval_hours") or 0))
    except (TypeError, ValueError):
        return None
    if next_funding is None:
        return None
    interval = timedelta(hours=interval_hours)
    if next_funding <= start:
        lag = start - next_funding
        if lag > min(interval / 4, timedelta(minutes=5)):
            return None
        next_funding += interval
    return next_funding


def settlement_events(
    market: dict[str, Any],
    next_funding: datetime | None,
    end: datetime,
    side: str,
) -> list[tuple[datetime, str, float]]:
    if next_funding is None:
        return []
    try:
        interval_hours = max(1.0, float(market.get("funding_interval_hours") or 0))
    except (TypeError, ValueError):
        return []
    interval = timedelta(hours=interval_hours)
    output = []
    cursor = next_funding
    while cursor <= end and len(output) < 1_000:
        output.append((cursor, side, interval_hours))
        cursor += interval
    return output


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = bounded(probability, 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def weighted_quantile(
    values: list[float],
    weights: list[float],
    probability: float,
) -> float:
    rows = sorted(
        (
            (float(value), max(0.0, float(weight)))
            for value, weight in zip(values, weights)
        ),
        key=lambda row: row[0],
    )
    total_weight = sum(weight for _, weight in rows)
    if not rows or total_weight <= 0:
        return 0.0
    threshold = bounded(probability, 0.0, 1.0) * total_weight
    cumulative = 0.0
    for value, weight in rows:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return rows[-1][0]


def weighted_mean(values: list[float], weights: list[float]) -> float:
    total_weight = sum(max(0.0, float(weight)) for weight in weights)
    if not values or total_weight <= 0:
        return 0.0
    return sum(
        float(value) * max(0.0, float(weight))
        for value, weight in zip(values, weights)
    ) / total_weight


def effective_sample_size(weights: list[float]) -> float:
    positive = [max(0.0, float(weight)) for weight in weights]
    weight_sum = sum(positive)
    squared_sum = sum(weight * weight for weight in positive)
    return weight_sum * weight_sum / squared_sum if squared_sum > 0 else 0.0


def bounded(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(float(value), maximum))


def horizon_label(mode: str, hours: float) -> str:
    if mode == "next_settlement":
        return "Next settlement"
    if mode == "research":
        return "3 days · Research"
    return f"{hours:g}h"


def empty_schedule(mode: str) -> dict[str, Any]:
    return {
        "horizon_mode": mode,
        "horizon_hours": 0.0,
        "horizon_label": horizon_label(mode, 0.0),
        "research_only": mode == "research",
        "authorization_valid_until": None,
        "long_next_settlement_at": None,
        "short_next_settlement_at": None,
        "paired_coverage_hours": 0.0,
        "settlement_event_count": 0,
        "long_settlement_count": 0,
        "short_settlement_count": 0,
        "settlements": [],
    }
