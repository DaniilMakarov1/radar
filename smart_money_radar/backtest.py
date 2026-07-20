from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import Any

from smart_money_radar.scoring.wallets import (
    MAX_MEANINGFUL_PRE_SELL_RATIO,
    MIN_MEANINGFUL_BUY_USD,
    MIN_MEANINGFUL_NET_USD,
    MODEL_VERSION,
    parse_time,
    score_wallet_rows,
    target_key,
)
from smart_money_radar.storage import SQLiteStore


BACKTEST_TYPE = "wallet_walk_forward"
MINIMUM_REQUIRED_LIFT = 1.5


def run_wallet_walk_forward(
    store: SQLiteStore,
    post_window_days: int = 30,
    minimum_interest_score: float = 52.0,
    maximum_noise_score: float = 65.0,
    minimum_history_target_count: int = 2,
) -> dict[str, Any]:
    rows = store.pre_listing_wallet_buy_rows()
    holding_rows = store.wallet_holding_metric_rows()
    config = {
        "post_window_days": post_window_days,
        "minimum_interest_score": minimum_interest_score,
        "maximum_noise_score": maximum_noise_score,
        "minimum_history_target_count": minimum_history_target_count,
        "minimum_meaningful_buy_usd": MIN_MEANINGFUL_BUY_USD,
        "minimum_meaningful_net_usd": MIN_MEANINGFUL_NET_USD,
        "maximum_meaningful_pre_sell_ratio": MAX_MEANINGFUL_PRE_SELL_RATIO,
    }
    run_id = store.start_backtest_run(
        model_version=MODEL_VERSION,
        backtest_type=BACKTEST_TYPE,
        config=config,
    )

    try:
        result = walk_forward_wallet_backtest(
            rows=rows,
            holding_rows=holding_rows,
            post_window_days=post_window_days,
            minimum_interest_score=minimum_interest_score,
            maximum_noise_score=maximum_noise_score,
            minimum_history_target_count=minimum_history_target_count,
        )
        store.insert_backtest_evaluations(run_id, result["evaluations"])
        store.finish_backtest_run(
            backtest_run_id=run_id,
            status="completed",
            metrics=result["metrics"],
            leakage_checks=result["leakage_checks"],
            notes=result["notes"],
        )
    except Exception as exc:
        store.finish_backtest_run(
            backtest_run_id=run_id,
            status="failed",
            metrics={},
            leakage_checks={},
            notes=str(exc),
        )
        raise

    return {"backtest_run_id": run_id, **result}


def walk_forward_wallet_backtest(
    rows: list[dict[str, Any]],
    holding_rows: list[dict[str, Any]],
    post_window_days: int = 30,
    minimum_interest_score: float = 52.0,
    maximum_noise_score: float = 65.0,
    minimum_history_target_count: int = 2,
) -> dict[str, Any]:
    holding_by_key = {
        (*target_key(row), row["wallet_address"].lower()): row
        for row in holding_rows
    }
    targets = sorted(
        {
            target_key(row)
            for row in rows
        },
        key=lambda item: item[3],
    )
    rows_by_target: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {
        key: [] for key in targets
    }
    for row in rows:
        rows_by_target[target_key(row)].append(row)

    evaluations = []
    for current_key in targets:
        current_at = parse_time(current_key[3])
        mature_history_keys = {
            key
            for key in targets
            if parse_time(key[3]) + timedelta(days=post_window_days) <= current_at
        }
        if not mature_history_keys:
            continue

        history_rows = [
            row
            for key in mature_history_keys
            for row in rows_by_target[key]
        ]
        history_holding_rows = [
            row
            for row in holding_rows
            if target_key(row) in mature_history_keys
        ]
        history_target_count = len(mature_history_keys)
        history_scores = score_wallet_rows(
            rows=history_rows,
            holding_rows=history_holding_rows,
            dataset_target_count=history_target_count,
        )
        predicted_wallets = {
            row["wallet_address"].lower()
            for row in history_scores
            if row["target_count"] >= minimum_history_target_count
            and row["interest_score"] >= minimum_interest_score
            and row["noise_score"] < maximum_noise_score
            and row["label"] in {"watch_candidate", "strong_candidate"}
        }
        eligible_wallets = {
            row["wallet_address"].lower()
            for row in history_rows
        }

        current_rows = rows_by_target[current_key]
        meaningful_current = {
            row["wallet_address"].lower()
            for row in current_rows
            if is_meaningful_pre_listing_entry(
                row,
                holding_by_key.get((*current_key, row["wallet_address"].lower())),
            )
        }
        hits = predicted_wallets & meaningful_current
        baseline_hits = eligible_wallets & meaningful_current
        precision = safe_divide(len(hits), len(predicted_wallets))
        baseline_rate = safe_divide(len(baseline_hits), len(eligible_wallets))
        lift = (
            precision / baseline_rate
            if precision is not None and baseline_rate not in {None, 0}
            else None
        )
        coverage = safe_divide(len(hits), len(meaningful_current))
        hit_leads = [
            (current_at - parse_time(row["first_buy_at"])).total_seconds() / 86400
            for row in current_rows
            if row["wallet_address"].lower() in hits
        ]

        evaluations.append(
            {
                "snapshot_at": current_at.isoformat(),
                "chain_id": current_key[0],
                "symbol": current_key[1],
                "token_address": current_key[2],
                "history_target_count": history_target_count,
                "eligible_wallet_count": len(eligible_wallets),
                "predicted_wallet_count": len(predicted_wallets),
                "hit_wallet_count": len(hits),
                "meaningful_buyer_count": len(meaningful_current),
                "precision": precision,
                "baseline_rate": baseline_rate,
                "lift": lift,
                "coverage": coverage,
                "median_hit_lead_days": median(hit_leads) if hit_leads else None,
                "evidence": {
                    "predicted_wallets": sorted(predicted_wallets)[:100],
                    "hit_wallets": sorted(hits)[:100],
                    "baseline_hit_count": len(baseline_hits),
                    "baseline_hit_wallets": sorted(baseline_hits)[:100],
                },
            }
        )

    total_predictions = sum(row["predicted_wallet_count"] for row in evaluations)
    total_hits = sum(row["hit_wallet_count"] for row in evaluations)
    total_eligible = sum(row["eligible_wallet_count"] for row in evaluations)
    total_baseline_hits = sum(
        int(row["evidence"]["baseline_hit_count"])
        for row in evaluations
    )
    micro_precision = safe_divide(total_hits, total_predictions)
    micro_baseline_rate = safe_divide(total_baseline_hits, total_eligible)
    micro_lift = (
        micro_precision / micro_baseline_rate
        if micro_precision is not None and micro_baseline_rate not in {None, 0}
        else None
    )
    repeatability_diagnostic_passed = (
        len(evaluations) >= 10
        and total_predictions >= 30
        and total_hits >= 10
        and micro_lift is not None
        and micro_lift >= MINIMUM_REQUIRED_LIFT
    )

    metrics = {
        "observed_target_count": len(targets),
        "evaluated_target_count": len(evaluations),
        "candidate_wallet_opportunities": total_predictions,
        "candidate_wallet_hits": total_hits,
        "micro_precision": micro_precision,
        "baseline_rate": micro_baseline_rate,
        "lift_over_baseline": micro_lift,
        "micro_precision_wilson_95": wilson_interval(total_hits, total_predictions),
        "baseline_rate_wilson_95": wilson_interval(total_baseline_hits, total_eligible),
        "repeatability_diagnostic_passed": repeatability_diagnostic_passed,
        "ready_for_wallet_quality_claims": False,
        "ready_for_token_prediction_claims": False,
        "minimum_required_evaluated_targets": 10,
        "minimum_required_candidate_opportunities": 30,
        "minimum_required_wallet_hits": 10,
        "minimum_required_lift": MINIMUM_REQUIRED_LIFT,
    }
    leakage_checks = {
        "future_target_rows_excluded_from_training": True,
        "post_listing_behavior_maturity_enforced": True,
        "current_target_post_behavior_excluded_from_hit_definition": True,
        "chronological_walk_forward_order": True,
        "negative_control_universe_present": False,
        "full_wallet_opportunity_denominator_present": False,
        "cluster_independence_adjusted": False,
        "confidence_interval_reported": True,
        "token_prediction_claim_allowed": False,
    }
    notes = (
        "This run evaluates wallet repeatability only. It cannot establish token-level "
        "listing alpha or wallet quality until a point-in-time negative token universe, "
        "the full wallet opportunity denominator, and identity-adjusted clusters are imported."
    )
    return {
        "metrics": metrics,
        "leakage_checks": leakage_checks,
        "evaluations": evaluations,
        "notes": notes,
    }


def is_meaningful_pre_listing_entry(
    buy_row: dict[str, Any],
    holding_row: dict[str, Any] | None,
) -> bool:
    gross_buy = float(buy_row["gross_buy_usd"] or 0)
    if holding_row:
        pre_buy = float(holding_row["pre_buy_usd"] or 0)
        pre_sell = float(holding_row["pre_sell_usd"] or 0)
        net_pre = float(holding_row["net_pre_usd"] or 0)
    else:
        pre_buy = gross_buy
        pre_sell = 0.0
        net_pre = gross_buy
    pre_sell_ratio = pre_sell / pre_buy if pre_buy > 0 else 0.0
    return (
        pre_buy >= MIN_MEANINGFUL_BUY_USD
        and net_pre >= MIN_MEANINGFUL_NET_USD
        and pre_sell_ratio < MAX_MEANINGFUL_PRE_SELL_RATIO
    )


def safe_divide(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> list[float] | None:
    if trials <= 0:
        return None
    rate = successes / trials
    denominator = 1 + z * z / trials
    centre = rate + z * z / (2 * trials)
    margin = z * ((rate * (1 - rate) / trials + z * z / (4 * trials * trials)) ** 0.5)
    return [
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    ]
