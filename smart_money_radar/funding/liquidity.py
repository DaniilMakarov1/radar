from __future__ import annotations

import math
from typing import Any

from smart_money_radar.funding.normalization import parse_timestamp


def build_route_liquidity_profile(
    long_book: dict[str, Any],
    short_book: dict[str, Any],
    target_notional: float,
    maker_timeout_seconds: int,
    maker_prior_probability: float,
    minimum_maker_observations: int,
) -> dict[str, Any]:
    """Describe execution quality from the stored sequence of L2 snapshots."""
    long_history = list(long_book.get("_history") or [])
    short_history = list(short_book.get("_history") or [])
    legs = {
        "long_open": analyze_depth_sequence(
            long_history, long_book, "asks", target_notional
        ),
        "long_close": analyze_depth_sequence(
            long_history, long_book, "bids", target_notional
        ),
        "short_open": analyze_depth_sequence(
            short_history, short_book, "bids", target_notional
        ),
        "short_close": analyze_depth_sequence(
            short_history, short_book, "asks", target_notional
        ),
    }
    maker_long = estimate_maker_fill(
        long_history,
        long_book,
        "buy",
        maker_timeout_seconds,
        maker_prior_probability,
        minimum_maker_observations,
    )
    maker_short = estimate_maker_fill(
        short_history,
        short_book,
        "sell",
        maker_timeout_seconds,
        maker_prior_probability,
        minimum_maker_observations,
    )
    refill_events = sum(int(row["refill_event_count"]) for row in legs.values())
    refill_successes = sum(
        int(row["refill_success_count"]) for row in legs.values()
    )
    snapshot_counts = [int(row["snapshot_count"]) for row in legs.values()]
    persistence_scores = [float(row["persistence_score"]) for row in legs.values()]
    executable_fractions = [
        float(row["executable_fraction"]) for row in legs.values()
    ]
    return {
        "model_version": "l2_sequence_v1",
        "target_notional": target_notional,
        "minimum_snapshot_count": min(snapshot_counts, default=0),
        "route_persistence_score": min(persistence_scores, default=0.0),
        "route_executable_fraction": min(executable_fractions, default=0.0),
        "refill_event_count": refill_events,
        "refill_success_count": refill_successes,
        "refill_probability": (
            refill_successes / refill_events if refill_events else None
        ),
        "legs": legs,
        "maker_entry": {
            "long": maker_long,
            "short": maker_short,
            "joint_fill_probability": (
                float(maker_long["fill_probability"])
                * float(maker_short["fill_probability"])
            ),
            "data_ready": bool(maker_long["data_ready"] and maker_short["data_ready"]),
            "probability_source": (
                "orderbook_price_through_proxy"
                if maker_long["data_ready"] and maker_short["data_ready"]
                else "prior_fallback"
            ),
        },
    }


def analyze_depth_sequence(
    history: list[dict[str, Any]],
    current_book: dict[str, Any],
    levels_key: str,
    target_notional: float,
    near_touch_bps: float = 25.0,
    refill_window_seconds: int = 300,
) -> dict[str, Any]:
    snapshots = ordered_snapshots(history, current_book)
    rows: list[dict[str, Any]] = []
    action = "sell" if levels_key == "bids" else "buy"
    for snapshot in snapshots:
        levels = normalized_levels(snapshot.get(levels_key) or [])
        if not levels:
            continue
        fill = fill_notional(levels, target_notional)
        mid = positive_float(snapshot.get("mid_price"))
        slippage_bps = 0.0
        if mid and fill["vwap"] > 0:
            raw_slippage = (
                fill["vwap"] / mid - 1.0
                if action == "buy"
                else 1.0 - fill["vwap"] / mid
            )
            slippage_bps = max(0.0, raw_slippage * 10_000.0)
        rows.append(
            {
                "observed_at": snapshot.get("observed_at"),
                "timestamp": parse_timestamp(snapshot.get("observed_at")),
                "capacity": displayed_capacity(levels),
                "near_touch_depth": near_touch_depth(
                    levels, levels_key, near_touch_bps
                ),
                "fill_complete": (
                    target_notional > 0
                    and fill["filled_notional"] >= target_notional * 0.999
                ),
                "slippage_bps": slippage_bps,
                "book_walk_bps": fill["book_walk_bps"],
            }
        )
    capacities = [float(row["capacity"]) for row in rows]
    near_depths = [float(row["near_touch_depth"]) for row in rows]
    slippages = [
        float(row["slippage_bps"]) for row in rows if bool(row["fill_complete"])
    ]
    executable_fraction = (
        sum(bool(row["fill_complete"]) for row in rows) / len(rows)
        if rows
        else 0.0
    )
    median_near_depth = quantile(near_depths, 0.5)
    near_depth_iqr = quantile(near_depths, 0.75) - quantile(near_depths, 0.25)
    depth_stability = (
        max(0.0, min(1.0, 1.0 - near_depth_iqr / median_near_depth))
        if median_near_depth > 0
        else 0.0
    )
    sample_score = min(1.0, len(rows) / 20.0)
    persistence_score = 100.0 * (
        0.55 * executable_fraction + 0.30 * depth_stability + 0.15 * sample_score
    )
    refill = refill_statistics(rows, refill_window_seconds)
    timestamps = [row["timestamp"] for row in rows if row["timestamp"] is not None]
    span_hours = (
        max(0.0, (timestamps[-1] - timestamps[0]).total_seconds() / 3_600.0)
        if len(timestamps) >= 2
        else 0.0
    )
    return {
        "side": levels_key,
        "snapshot_count": len(rows),
        "span_hours": span_hours,
        "executable_fraction": executable_fraction,
        "persistence_score": persistence_score,
        "capacity_q25": quantile(capacities, 0.25),
        "capacity_median": quantile(capacities, 0.5),
        "near_touch_bps": near_touch_bps,
        "near_touch_depth_q25": quantile(near_depths, 0.25),
        "near_touch_depth_median": median_near_depth,
        "depth_stability": depth_stability,
        "slippage_bps_median": quantile(slippages, 0.5),
        "slippage_bps_q75": quantile(slippages, 0.75),
        **refill,
    }


def estimate_maker_fill(
    history: list[dict[str, Any]],
    current_book: dict[str, Any],
    action: str,
    timeout_seconds: int,
    prior_probability: float,
    minimum_observations: int,
) -> dict[str, Any]:
    """Conservative fill proxy: the opposite best price crosses our quote."""
    snapshots = ordered_snapshots(history, current_book)
    successes = 0
    trials = 0
    adverse_selection_bps: list[float] = []
    for index, anchor in enumerate(snapshots[:-1]):
        anchor_time = parse_timestamp(anchor.get("observed_at"))
        quote = maker_quote(anchor, action)
        if anchor_time is None or quote <= 0:
            continue
        futures: list[dict[str, Any]] = []
        for future in snapshots[index + 1 :]:
            future_time = parse_timestamp(future.get("observed_at"))
            if future_time is None:
                continue
            elapsed = (future_time - anchor_time).total_seconds()
            if elapsed <= 0:
                continue
            if elapsed > timeout_seconds:
                break
            futures.append(future)
        if not futures:
            continue
        trials += 1
        crossing = next(
            (
                future
                for future in futures
                if (
                    best_price(future.get("asks") or []) <= quote
                    if action == "buy"
                    else best_price(future.get("bids") or []) >= quote
                )
            ),
            None,
        )
        success = crossing is not None
        successes += int(success)
        if crossing is not None:
            future_mid = positive_float(crossing.get("mid_price"))
            if future_mid is not None:
                adverse_move = (
                    quote / future_mid - 1.0
                    if action == "buy"
                    else future_mid / quote - 1.0
                )
                adverse_selection_bps.append(max(0.0, adverse_move * 10_000.0))
    prior = max(0.0, min(1.0, prior_probability))
    prior_strength = 4.0
    posterior = (successes + prior * prior_strength) / (trials + prior_strength)
    lower_bound = wilson_lower_bound(successes, trials)
    data_ready = trials >= minimum_observations
    probability = lower_bound if data_ready else prior
    levels_key = "bids" if action == "buy" else "asks"
    current_levels = normalized_levels(current_book.get(levels_key) or [])
    return {
        "action": action,
        "timeout_seconds": timeout_seconds,
        "trial_count": trials,
        "success_count": successes,
        "empirical_probability": successes / trials if trials else None,
        "posterior_probability": posterior,
        "conservative_probability": lower_bound if trials else None,
        "adverse_selection_bps_median": (
            quantile(adverse_selection_bps, 0.5) if adverse_selection_bps else None
        ),
        "adverse_selection_bps_q75": (
            quantile(adverse_selection_bps, 0.75) if adverse_selection_bps else None
        ),
        "fill_probability": probability,
        "data_ready": data_ready,
        "probability_source": (
            "orderbook_price_through_proxy" if data_ready else "prior_fallback"
        ),
        "queue_ahead_notional": (
            current_levels[0][0] * current_levels[0][1] if current_levels else 0.0
        ),
        "proxy_limitations": (
            "L2 snapshots cannot distinguish trades from cancellations and do not "
            "reconstruct queue position; price-through is a conservative fill proxy."
        ),
    }


def refill_statistics(
    rows: list[dict[str, Any]],
    refill_window_seconds: int,
) -> dict[str, Any]:
    event_count = 0
    success_count = 0
    refill_seconds: list[float] = []
    for index in range(1, len(rows)):
        prior_depth = float(rows[index - 1]["near_touch_depth"])
        current_depth = float(rows[index]["near_touch_depth"])
        shock_time = rows[index]["timestamp"]
        if prior_depth <= 0 or current_depth >= prior_depth * 0.70 or shock_time is None:
            continue
        event_count += 1
        for future in rows[index + 1 :]:
            future_time = future["timestamp"]
            if future_time is None:
                continue
            elapsed = (future_time - shock_time).total_seconds()
            if elapsed <= 0:
                continue
            if elapsed > refill_window_seconds:
                break
            if float(future["near_touch_depth"]) >= prior_depth * 0.90:
                success_count += 1
                refill_seconds.append(elapsed)
                break
    return {
        "refill_window_seconds": refill_window_seconds,
        "refill_event_count": event_count,
        "refill_success_count": success_count,
        "refill_probability": success_count / event_count if event_count else None,
        "median_refill_seconds": quantile(refill_seconds, 0.5) if refill_seconds else None,
    }


def ordered_snapshots(
    history: list[dict[str, Any]],
    current_book: dict[str, Any],
) -> list[dict[str, Any]]:
    by_timestamp: dict[str, dict[str, Any]] = {}
    for snapshot in [*history, current_book]:
        observed_at = str(snapshot.get("observed_at") or "")
        timestamp = parse_timestamp(observed_at)
        if timestamp is not None:
            by_timestamp[timestamp.isoformat()] = snapshot
    return [
        row
        for _, row in sorted(
            by_timestamp.items(),
            key=lambda item: item[0],
        )
    ]


def near_touch_depth(
    levels: list[list[float]],
    levels_key: str,
    near_touch_bps: float,
) -> float:
    if not levels:
        return 0.0
    best = levels[0][0]
    if levels_key == "bids":
        boundary = best * (1.0 - near_touch_bps / 10_000.0)
        eligible = (level for level in levels if level[0] >= boundary)
    else:
        boundary = best * (1.0 + near_touch_bps / 10_000.0)
        eligible = (level for level in levels if level[0] <= boundary)
    return sum(price * size for price, size in eligible)


def fill_notional(levels: list[list[float]], target_notional: float) -> dict[str, float]:
    remaining = max(0.0, float(target_notional))
    filled_notional = 0.0
    filled_size = 0.0
    best = 0.0
    worst = 0.0
    for price, size in normalized_levels(levels):
        if remaining <= 1e-9:
            break
        available = price * size
        taken = min(remaining, available)
        if taken <= 0:
            continue
        if best == 0.0:
            best = price
        worst = price
        filled_notional += taken
        filled_size += taken / price
        remaining -= taken
    return {
        "filled_notional": filled_notional,
        "vwap": filled_notional / filled_size if filled_size else 0.0,
        "book_walk_bps": (
            abs(worst / best - 1.0) * 10_000.0 if best > 0 and worst > 0 else 0.0
        ),
    }


def normalized_levels(levels: list[list[float]]) -> list[list[float]]:
    normalized: list[list[float]] = []
    for row in levels:
        try:
            price = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            normalized.append([price, size])
    return normalized


def displayed_capacity(levels: list[list[float]]) -> float:
    return sum(price * size for price, size in normalized_levels(levels))


def best_price(levels: list[list[float]]) -> float:
    normalized = normalized_levels(levels)
    return normalized[0][0] if normalized else 0.0


def maker_quote(snapshot: dict[str, Any], action: str) -> float:
    levels_key = "bids" if action == "buy" else "asks"
    return best_price(snapshot.get(levels_key) or [])


def positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def quantile(values: list[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return 0.0
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


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
    margin = z_score * math.sqrt(
        probability * (1.0 - probability) / observations
        + z_squared / (4.0 * observations * observations)
    )
    return max(0.0, (center - margin) / denominator)
