from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    precision_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.research_validity import (
    OUTCOME_METHODOLOGY_VERSION,
    parse_time,
)
from smart_money_radar.storage import SQLiteStore, normalize_chain_address


MODEL_VERSION = "token_listing_v1"
MIN_READY_SNAPSHOTS = 100_000
MIN_READY_POSITIVE_EVENTS = 100
MIN_VALIDATION_POSITIVE_EVENTS = 20
MIN_VALIDATION_SNAPSHOT_DATES = 12
BOOTSTRAP_ITERATIONS = 500
EMBARGO_DAYS = 90
EVM_RESEARCH_CHAINS = ("base", "bsc", "ethereum")

FEATURE_NAMES = [
    "log_volume_usd",
    "log_trader_count",
    "log_trade_count",
    "buyer_to_trader_ratio",
    "seller_to_trader_ratio",
    "net_flow_share",
    "buy_volume_share",
    "trades_per_trader",
    "pair_count",
    "dex_count",
    "token_age_weeks",
    "volume_growth_1w",
    "volume_growth_4w",
    "trader_growth_1w",
    "price_return_1w",
    "price_return_4w",
    "smart_wallet_count",
    "smart_wallet_net_buy_usd_log",
    "smart_wallet_flow_share",
    "smart_wallet_top_share",
]


def train_model_suite(
    store: SQLiteStore,
    chain_id: str = "base",
) -> dict[str, Any]:
    runs = []
    for target_label in (
        "listed_within_30d",
        "listed_within_60d",
        "listed_within_90d",
    ):
        for family in ("logistic_regression", "gradient_boosting"):
            runs.append(
                train_research_model(
                    store=store,
                    chain_id=chain_id,
                    model_family=family,
                    target_label=target_label,
                )
            )
    return {"chain_id": chain_id, "runs": runs}


def train_research_model(
    store: SQLiteStore,
    chain_id: str,
    model_family: str,
    target_label: str = "listed_within_90d",
) -> dict[str, Any]:
    chains = EVM_RESEARCH_CHAINS if chain_id == "evm" else (chain_id,)
    raw_rows = [
        row
        for row in store.research_training_rows(
            methodology_version=OUTCOME_METHODOLOGY_VERSION,
            chain_id=None if chain_id == "evm" else chain_id,
        )
        if row["chain_id"] in chains
    ]
    smart_features: dict[tuple[str, str, str], dict[str, float]] = {}
    smart_coverages = []
    for research_chain in chains:
        chain_features, chain_coverage = point_in_time_smart_wallet_features(
            store, research_chain
        )
        smart_features.update(chain_features)
        smart_coverages.append(chain_coverage)
    smart_coverage = min(smart_coverages, default=0.0)
    dataset = build_feature_rows(raw_rows, smart_features, target_label)
    positive_events = {
        (row["chain_id"], row["token_address"], row.get("listing_at"))
        for row in dataset
        if int(row["label"]) == 1
    }
    unique_dates = sorted({parse_time(row["snapshot_at"]) for row in dataset})
    model_name = f"{chain_id}_{target_label}_{model_family}"
    config = {
        "chain_id": chain_id,
        "chains": list(chains),
        "target_label": target_label,
        "embargo_days": EMBARGO_DAYS,
        "minimum_ready_snapshots": MIN_READY_SNAPSHOTS,
        "minimum_ready_positive_events": MIN_READY_POSITIVE_EVENTS,
        "minimum_validation_positive_events": MIN_VALIDATION_POSITIVE_EVENTS,
        "minimum_validation_snapshot_dates": MIN_VALIDATION_SNAPSHOT_DATES,
        "smart_wallet_flow_coverage": smart_coverage,
    }
    run_id = store.start_research_model_run(
        model_name=model_name,
        model_version=MODEL_VERSION,
        model_family=model_family,
        target_label=target_label,
        feature_names=FEATURE_NAMES,
        config=config,
    )
    leakage_checks = {
        "point_in_time_outcomes_only": True,
        "already_listed_tokens_excluded": True,
        "future_returns_not_used_as_features": True,
        "chronological_holdout": True,
        "snapshot_block_bootstrap": True,
        "embargo_days": EMBARGO_DAYS,
        "smart_wallet_hits_cut_off_before_snapshot": True,
        "smart_wallet_flow_universe_complete": smart_coverage >= 0.95,
    }
    dataset_gate_passed = bool(
        len(dataset) >= MIN_READY_SNAPSHOTS
        and len(positive_events) >= MIN_READY_POSITIVE_EVENTS
    )
    if not dataset_gate_passed:
        metrics = dataset_readiness_metrics(dataset, positive_events, smart_coverage)
        store.finish_research_model_run(
            run_id=run_id,
            status="dataset_gate_blocked",
            metrics=metrics,
            feature_importance={},
            leakage_checks=leakage_checks,
            train_bounds=(None, None),
            validation_bounds=(None, None),
            notes=(
                "No model was fitted. The point-in-time dataset has not reached "
                "100,000 snapshots and 100 independent listing events."
            ),
        )
        return {
            "run_id": run_id,
            "status": "dataset_gate_blocked",
            "metrics": metrics,
        }

    if len(unique_dates) < 12:
        metrics = dataset_readiness_metrics(dataset, positive_events, smart_coverage)
        store.finish_research_model_run(
            run_id=run_id,
            status="insufficient_temporal_span",
            metrics=metrics,
            feature_importance={},
            leakage_checks=leakage_checks,
            train_bounds=(None, None),
            validation_bounds=(None, None),
            notes="Dataset gate passed but fewer than 12 independent weekly dates exist.",
        )
        return {
            "run_id": run_id,
            "status": "insufficient_temporal_span",
            "metrics": metrics,
        }

    validation_start = unique_dates[max(1, int(len(unique_dates) * 0.8))]
    train_cutoff = validation_start - timedelta(days=EMBARGO_DAYS)
    train_rows = [row for row in dataset if parse_time(row["snapshot_at"]) < train_cutoff]
    validation_rows = [
        row for row in dataset if parse_time(row["snapshot_at"]) >= validation_start
    ]
    if not valid_binary_split(train_rows, validation_rows):
        metrics = dataset_readiness_metrics(dataset, positive_events, smart_coverage)
        metrics.update(
            {
                "train_row_count": len(train_rows),
                "validation_row_count": len(validation_rows),
            }
        )
        store.finish_research_model_run(
            run_id=run_id,
            status="insufficient_temporal_split",
            metrics=metrics,
            feature_importance={},
            leakage_checks=leakage_checks,
            train_bounds=time_bounds(train_rows),
            validation_bounds=time_bounds(validation_rows),
            notes="Temporal split lacks enough rows or both labels after the embargo.",
        )
        return {
            "run_id": run_id,
            "status": "insufficient_temporal_split",
            "metrics": metrics,
        }

    x_train = np.asarray([[row["features"][name] for name in FEATURE_NAMES] for row in train_rows])
    y_train = np.asarray([row["label"] for row in train_rows], dtype=int)
    x_validation = np.asarray(
        [[row["features"][name] for name in FEATURE_NAMES] for row in validation_rows]
    )
    y_validation = np.asarray([row["label"] for row in validation_rows], dtype=int)
    model = build_model(model_family)
    model.fit(x_train, y_train)
    probabilities = model.predict_proba(x_validation)[:, 1]
    metrics = evaluate_predictions(
        validation_rows,
        y_validation,
        probabilities,
        dataset,
        positive_events,
        smart_coverage,
    )
    feature_importance = model_feature_importance(
        model,
        model_family,
        x_validation,
        y_validation,
    )
    artifact_dir = PROJECT_ROOT / "data" / "models"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{model_name}_{run_id}.joblib"
    joblib.dump(
        {
            "model": model,
            "feature_names": FEATURE_NAMES,
            "target_label": target_label,
            "chain_id": chain_id,
            "model_version": MODEL_VERSION,
        },
        artifact_path,
    )
    predictions = prediction_rows(
        run_id=run_id,
        rows=validation_rows,
        probabilities=probabilities,
        target_label=target_label,
        feature_importance=feature_importance,
    )
    store.insert_research_model_predictions(run_id, predictions)
    status = "completed_ready" if metrics["ready_for_research_claims"] else "completed_research_only"
    store.finish_research_model_run(
        run_id=run_id,
        status=status,
        metrics=metrics,
        feature_importance=feature_importance,
        leakage_checks=leakage_checks,
        train_bounds=time_bounds(train_rows),
        validation_bounds=time_bounds(validation_rows),
        artifact_path=str(artifact_path),
        notes=(
            "Model is a deterministic tabular estimator. LLM output is not used in training or scoring."
        ),
    )
    return {
        "run_id": run_id,
        "status": status,
        "metrics": metrics,
        "artifact_path": str(artifact_path),
    }


def build_feature_rows(
    rows: list[dict[str, Any]],
    smart_features: dict[tuple[str, str, str], dict[str, float]],
    target_label: str,
) -> list[dict[str, Any]]:
    by_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get(target_label) is None:
            continue
        if not row.get("outcome_evidence", {}).get("eligible_for_listing_prediction", True):
            continue
        if not bool(row.get("is_tradeable")):
            continue
        token_address = normalize_chain_address(
            row["chain_id"], row["token_address"]
        )
        by_token[(row["chain_id"], token_address)].append(row)
    output = []
    for (chain_id, token_address), token_rows in by_token.items():
        token_rows.sort(key=lambda row: row["snapshot_at"])
        first_seen_at = parse_time(token_rows[0]["snapshot_at"])
        for index, row in enumerate(token_rows):
            previous_1 = token_rows[index - 1] if index >= 1 else None
            previous_4 = token_rows[index - 4] if index >= 4 else None
            volume = float(row.get("volume_usd") or 0)
            traders = float(row.get("trader_count") or 0)
            trades = float(row.get("trade_count") or 0)
            smart = smart_features.get(
                (chain_id, row["snapshot_at"], token_address), {}
            )
            features = {
                "log_volume_usd": math.log1p(volume),
                "log_trader_count": math.log1p(traders),
                "log_trade_count": math.log1p(trades),
                "buyer_to_trader_ratio": safe_ratio(row.get("buyer_count"), traders),
                "seller_to_trader_ratio": safe_ratio(row.get("seller_count"), traders),
                "net_flow_share": safe_ratio(row.get("net_flow_usd"), volume),
                "buy_volume_share": safe_ratio(row.get("gross_buy_usd"), volume),
                "trades_per_trader": safe_ratio(trades, traders),
                "pair_count": float(row.get("pair_count") or 0),
                "dex_count": float(row.get("dex_count") or 0),
                "token_age_weeks": max(
                    0.0,
                    (parse_time(row["snapshot_at"]) - first_seen_at).total_seconds()
                    / (7 * 86_400),
                ),
                "volume_growth_1w": relative_change(volume, previous_1, "volume_usd"),
                "volume_growth_4w": relative_change(volume, previous_4, "volume_usd"),
                "trader_growth_1w": relative_change(traders, previous_1, "trader_count"),
                "price_return_1w": price_change(row, previous_1),
                "price_return_4w": price_change(row, previous_4),
                "smart_wallet_count": float(smart.get("wallet_count", 0)),
                "smart_wallet_net_buy_usd_log": math.log1p(max(0.0, smart.get("net_buy_usd", 0))),
                "smart_wallet_flow_share": safe_ratio(smart.get("net_buy_usd", 0), volume),
                "smart_wallet_top_share": float(smart.get("top_wallet_share", 0)),
            }
            output.append(
                {
                    "chain_id": row["chain_id"],
                    "token_address": token_address,
                    "snapshot_at": row["snapshot_at"],
                    "label": int(bool(row[target_label])),
                    "listing_at": row.get("listing_at"),
                    "features": features,
                }
            )
    return output


def point_in_time_smart_wallet_features(
    store: SQLiteStore,
    chain_id: str,
) -> tuple[dict[tuple[str, str, str], dict[str, float]], float]:
    buy_rows = store.pre_listing_wallet_buy_rows(chain_id=chain_id)
    holding_rows = store.wallet_holding_metric_rows(chain_id=chain_id)
    holding_by_key = {
        (
            row["wallet_address"].lower(),
            row["token_address"].lower(),
            row["announced_at"],
        ): row
        for row in holding_rows
    }
    hits_by_wallet: dict[str, list[Any]] = defaultdict(list)
    for row in buy_rows:
        holding = holding_by_key.get(
            (
                row["wallet_address"].lower(),
                row["token_address"].lower(),
                row["announced_at"],
            )
        )
        pre_buy = float(holding.get("pre_buy_usd") or 0) if holding else float(row.get("gross_buy_usd") or 0)
        net_pre = float(holding.get("net_pre_usd") or 0) if holding else pre_buy
        pre_sell_ratio = float(holding.get("pre_sell_ratio") or 0) if holding else 0.0
        if pre_buy >= 1_000 and net_pre >= 500 and pre_sell_ratio < 0.35:
            hits_by_wallet[row["wallet_address"].lower()].append(parse_time(row["announced_at"]))
    for dates in hits_by_wallet.values():
        dates.sort()

    flows = store.wallet_token_weekly_flow_rows(chain_id)
    aggregate: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"wallet_flows": []}
    )
    qualified_flow_count = 0
    for row in flows:
        snapshot = parse_time(row["snapshot_at"])
        cutoff = snapshot - timedelta(days=30)
        wallet = row["wallet_address"].lower()
        prior_hits = sum(date <= cutoff for date in hits_by_wallet.get(wallet, []))
        if prior_hits < 2:
            continue
        net_buy = float(row.get("net_buy_usd") or 0)
        if net_buy <= 0:
            continue
        qualified_flow_count += 1
        key = (
            chain_id,
            row["snapshot_at"],
            normalize_chain_address(chain_id, row["token_address"]),
        )
        aggregate[key]["wallet_flows"].append((wallet, net_buy))
    output = {}
    for key, item in aggregate.items():
        flows_for_token = item["wallet_flows"]
        total = sum(value for _, value in flows_for_token)
        output[key] = {
            "wallet_count": float(len({wallet for wallet, _ in flows_for_token})),
            "net_buy_usd": total,
            "top_wallet_share": max((value for _, value in flows_for_token), default=0) / total if total > 0 else 0,
        }
    historical_wallets = len(hits_by_wallet)
    flow_wallets = len({row["wallet_address"].lower() for row in flows})
    coverage = flow_wallets / historical_wallets if historical_wallets else 0.0
    return output, min(1.0, coverage)


def build_model(model_family: str) -> Pipeline:
    if model_family == "logistic_regression":
        classifier: Any = LogisticRegression(
            max_iter=2_000,
            class_weight="balanced",
            C=0.5,
            random_state=42,
        )
        scaler: Any = StandardScaler()
    elif model_family == "gradient_boosting":
        classifier = HistGradientBoostingClassifier(
            max_iter=250,
            learning_rate=0.05,
            max_leaf_nodes=15,
            min_samples_leaf=30,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=42,
        )
        scaler = "passthrough"
    else:
        raise ValueError(f"Unsupported model family: {model_family}")
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scaler", scaler),
            ("classifier", classifier),
        ]
    )


def evaluate_predictions(
    validation_rows: list[dict[str, Any]],
    labels: np.ndarray,
    probabilities: np.ndarray,
    dataset: list[dict[str, Any]],
    positive_tokens: set[Any],
    smart_coverage: float,
) -> dict[str, Any]:
    predictions = (probabilities >= 0.5).astype(int)
    baseline = float(labels.mean())
    top5_precision, top5_count = precision_at_k_by_snapshot(validation_rows, labels, probabilities, 5)
    top5_lift = top5_precision / baseline if baseline > 0 else None
    lift_lower, lift_upper = snapshot_block_bootstrap_lift(
        validation_rows,
        labels,
        probabilities,
        k=5,
        iterations=BOOTSTRAP_ITERATIONS,
    )
    validation_positive_events = {
        (row["chain_id"], row["token_address"], row.get("listing_at"))
        for row in validation_rows
        if int(row["label"]) == 1
    }
    validation_date_count = len({row["snapshot_at"] for row in validation_rows})
    brier = float(brier_score_loss(labels, probabilities))
    baseline_brier = float(
        brier_score_loss(labels, np.full(len(labels), baseline, dtype=float))
    )
    metrics = dataset_readiness_metrics(dataset, positive_tokens, smart_coverage)
    metrics.update(
        {
            "validation_row_count": len(validation_rows),
            "validation_positive_count": int(labels.sum()),
            "validation_independent_positive_event_count": len(validation_positive_events),
            "validation_snapshot_date_count": validation_date_count,
            "validation_base_rate": baseline,
            "average_precision": float(average_precision_score(labels, probabilities)),
            "roc_auc": float(roc_auc_score(labels, probabilities)),
            "brier_score": brier,
            "baseline_brier_score": baseline_brier,
            "brier_improvement": baseline_brier - brier,
            "log_loss": float(log_loss(labels, probabilities, labels=[0, 1])),
            "precision_at_0_5": float(precision_score(labels, predictions, zero_division=0)),
            "precision_at_5_per_snapshot": top5_precision,
            "top5_prediction_count": top5_count,
            "top5_lift_over_base_rate": top5_lift,
            "top5_lift_ci_95_lower": lift_lower,
            "top5_lift_ci_95_upper": lift_upper,
            "bootstrap_unit": "snapshot_date",
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "minimum_validation_positive_events": MIN_VALIDATION_POSITIVE_EVENTS,
            "minimum_validation_snapshot_dates": MIN_VALIDATION_SNAPSHOT_DATES,
        }
    )
    metrics["ready_for_research_claims"] = bool(
        metrics["dataset_ready"]
        and len(validation_positive_events) >= MIN_VALIDATION_POSITIVE_EVENTS
        and validation_date_count >= MIN_VALIDATION_SNAPSHOT_DATES
        and lift_lower is not None
        and lift_lower > 1
        and metrics["average_precision"] > baseline
        and brier < baseline_brier
    )
    return metrics


def dataset_readiness_metrics(
    dataset: list[dict[str, Any]],
    positive_tokens: set[Any],
    smart_coverage: float,
) -> dict[str, Any]:
    positive_rows = sum(int(row["label"]) for row in dataset)
    return {
        "dataset_row_count": len(dataset),
        "positive_row_count": positive_rows,
        "independent_positive_token_count": len(positive_tokens),
        "smart_wallet_flow_coverage": smart_coverage,
        "minimum_ready_snapshots": MIN_READY_SNAPSHOTS,
        "minimum_ready_positive_events": MIN_READY_POSITIVE_EVENTS,
        "dataset_gate_passed": bool(
            len(dataset) >= MIN_READY_SNAPSHOTS
            and len(positive_tokens) >= MIN_READY_POSITIVE_EVENTS
        ),
        "dataset_ready": bool(
            len(dataset) >= MIN_READY_SNAPSHOTS
            and len(positive_tokens) >= MIN_READY_POSITIVE_EVENTS
            and smart_coverage >= 0.95
        ),
        "ready_for_research_claims": False,
    }


def model_feature_importance(
    model: Pipeline,
    family: str,
    x_validation: np.ndarray,
    y_validation: np.ndarray,
) -> dict[str, float]:
    if family == "logistic_regression":
        coefficients = model.named_steps["classifier"].coef_[0]
        return {
            name: float(coefficients[index])
            for index, name in enumerate(FEATURE_NAMES)
            if index < len(coefficients)
        }
    sample_size = min(5_000, len(x_validation))
    result = permutation_importance(
        model,
        x_validation[:sample_size],
        y_validation[:sample_size],
        scoring="average_precision",
        n_repeats=3,
        random_state=42,
    )
    return {
        name: float(result.importances_mean[index])
        for index, name in enumerate(FEATURE_NAMES)
    }


def prediction_rows(
    run_id: int,
    rows: list[dict[str, Any]],
    probabilities: np.ndarray,
    target_label: str,
    feature_importance: dict[str, float],
) -> list[dict[str, Any]]:
    by_snapshot: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_snapshot[row["snapshot_at"]].append(index)
    ranks = {}
    for indexes in by_snapshot.values():
        ordered = sorted(indexes, key=lambda index: probabilities[index], reverse=True)
        for rank, index in enumerate(ordered, start=1):
            ranks[index] = rank
    top_features = [
        name
        for name, _ in sorted(
            feature_importance.items(),
            key=lambda item: abs(item[1]),
            reverse=True,
        )[:5]
    ]
    return [
        {
            "chain_id": row["chain_id"],
            "token_address": row["token_address"],
            "snapshot_at": row["snapshot_at"],
            "target_label": target_label,
            "actual_label": int(row["label"]),
            "probability": float(probabilities[index]),
            "rank_at_snapshot": ranks[index],
            "split": "validation",
            "feature_values": row["features"],
            "explanation": {"global_top_features": top_features},
        }
        for index, row in enumerate(rows)
    ]


def precision_at_k_by_snapshot(
    rows: list[dict[str, Any]],
    labels: np.ndarray,
    probabilities: np.ndarray,
    k: int,
) -> tuple[float, int]:
    by_snapshot: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_snapshot[row["snapshot_at"]].append(index)
    selected = []
    for indexes in by_snapshot.values():
        selected.extend(sorted(indexes, key=lambda index: probabilities[index], reverse=True)[:k])
    if not selected:
        return 0.0, 0
    return float(labels[selected].mean()), len(selected)


def snapshot_block_bootstrap_lift(
    rows: list[dict[str, Any]],
    labels: np.ndarray,
    probabilities: np.ndarray,
    k: int,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> tuple[float | None, float | None]:
    by_snapshot: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_snapshot[row["snapshot_at"]].append(index)
    cohorts = []
    for indexes in by_snapshot.values():
        selected = sorted(
            indexes,
            key=lambda index: probabilities[index],
            reverse=True,
        )[:k]
        cohorts.append(
            (
                int(labels[indexes].sum()),
                len(indexes),
                int(labels[selected].sum()),
                len(selected),
            )
        )
    if len(cohorts) < 2:
        return None, None
    rng = np.random.default_rng(42)
    lifts = []
    for _ in range(max(1, iterations)):
        sampled = rng.integers(0, len(cohorts), size=len(cohorts))
        positive_count = sum(cohorts[index][0] for index in sampled)
        row_count = sum(cohorts[index][1] for index in sampled)
        top_hits = sum(cohorts[index][2] for index in sampled)
        top_count = sum(cohorts[index][3] for index in sampled)
        base_rate = positive_count / row_count if row_count else 0.0
        top_precision = top_hits / top_count if top_count else 0.0
        if base_rate > 0:
            lifts.append(top_precision / base_rate)
    if not lifts:
        return None, None
    return float(np.quantile(lifts, 0.025)), float(np.quantile(lifts, 0.975))


def valid_binary_split(
    train_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
) -> bool:
    return bool(
        len(train_rows) >= 300
        and len(validation_rows) >= 100
        and {row["label"] for row in train_rows} == {0, 1}
        and {row["label"] for row in validation_rows} == {0, 1}
    )


def time_bounds(rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    if not rows:
        return None, None
    dates = sorted(row["snapshot_at"] for row in rows)
    return dates[0], dates[-1]


def relative_change(current: float, previous: dict[str, Any] | None, key: str) -> float:
    if not previous:
        return math.nan
    prior = float(previous.get(key) or 0)
    if prior <= 0:
        return math.nan
    return max(-1.0, min(20.0, current / prior - 1))


def price_change(current: dict[str, Any], previous: dict[str, Any] | None) -> float:
    if not previous:
        return math.nan
    current_price = float(current.get("close_price_usd") or 0)
    previous_price = float(previous.get("close_price_usd") or 0)
    if current_price <= 0 or previous_price <= 0:
        return math.nan
    return max(-1.0, min(100.0, current_price / previous_price - 1))


def safe_ratio(numerator: Any, denominator: Any) -> float:
    try:
        numerator_value = float(numerator or 0)
        denominator_value = float(denominator or 0)
    except (TypeError, ValueError):
        return 0.0
    return numerator_value / denominator_value if denominator_value > 0 else 0.0
