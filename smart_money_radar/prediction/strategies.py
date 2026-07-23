from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any


STRATEGY_BUCKETS = {
    "binary_complement": "binary_complement",
    "complete_set": "complete_set_discount",
    "complete_set_no": "complete_set_no_discount",
    "negative_risk_conversion": "negative_risk_conversion",
    "threshold_ladder": "threshold_ladder",
    "logical_implication": "logical_implication",
    "cross_venue_complement": "cross_venue_equivalence",
}

STRATEGY_NAMES = {
    "binary_complement": "Binary complement",
    "complete_set_discount": "Complete-set discount",
    "complete_set_no_discount": "Complete-set NO discount",
    "negative_risk_conversion": "Negative risk",
    "threshold_ladder": "Threshold ladder",
    "logical_implication": "Logical implication",
    "cross_venue_equivalence": "Cross-venue equivalence",
    "unclassified": "Unclassified",
}


def prediction_strategy_bucket(route_type: Any) -> str:
    return STRATEGY_BUCKETS.get(str(route_type or ""), "unclassified")


def prediction_strategy_name(strategy_bucket: Any) -> str:
    return STRATEGY_NAMES.get(str(strategy_bucket or ""), str(strategy_bucket or "Unclassified"))


def annotate_prediction_candidate_lifecycle(
    current_candidates: list[dict[str, Any]],
    previous_candidates: list[dict[str, Any]],
    *,
    disappeared_limit: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    previous_by_key = {
        str(row.get("route_key")): row
        for row in previous_candidates
        if row.get("route_key")
    }
    current_keys = {
        str(row.get("route_key"))
        for row in current_candidates
        if row.get("route_key")
    }
    lifecycle_counts: Counter[str] = Counter()
    annotated = []
    for row in current_candidates:
        item = dict(row)
        item["strategy_bucket"] = prediction_strategy_bucket(item.get("route_type"))
        item["strategy_name"] = prediction_strategy_name(item["strategy_bucket"])
        previous = previous_by_key.get(str(item.get("route_key")))
        if previous is None:
            item["lifecycle_status"] = "new"
            item["candidate_score_delta"] = None
            item["expected_net_profit_delta"] = None
            item["previous_candidate_status"] = None
        else:
            score_delta = delta(item.get("candidate_score"), previous.get("candidate_score"))
            profit_delta = delta(
                item.get("expected_net_profit"),
                previous.get("expected_net_profit"),
            )
            item["candidate_score_delta"] = score_delta
            item["expected_net_profit_delta"] = profit_delta
            item["previous_candidate_status"] = previous.get("candidate_status")
            if improved(score_delta, profit_delta):
                item["lifecycle_status"] = "improved"
            elif weakened(score_delta, profit_delta):
                item["lifecycle_status"] = "weakened"
            else:
                item["lifecycle_status"] = "persisting"
        lifecycle_counts[item["lifecycle_status"]] += 1
        annotated.append(item)

    disappeared = [
        {
            "route_key": row.get("route_key"),
            "title": row.get("title"),
            "route_type": row.get("route_type"),
            "strategy_bucket": prediction_strategy_bucket(row.get("route_type")),
            "strategy_name": prediction_strategy_name(
                prediction_strategy_bucket(row.get("route_type"))
            ),
            "candidate_status": row.get("candidate_status"),
            "candidate_score": row.get("candidate_score"),
            "expected_net_profit": row.get("expected_net_profit"),
            "screen_reason": row.get("screen_reason"),
        }
        for row in previous_candidates
        if row.get("route_key") and str(row.get("route_key")) not in current_keys
    ]
    disappeared.sort(
        key=lambda row: numeric(row.get("candidate_score")) or -math.inf,
        reverse=True,
    )
    lifecycle_counts["disappeared"] = len(disappeared)
    summary = {
        "current_count": len(current_candidates),
        "previous_count": len(previous_candidates),
        "counts": dict(lifecycle_counts),
        "recently_disappeared": disappeared[: max(0, int(disappeared_limit))],
        "storage_policy": "latest_scan_only",
    }
    return annotated, summary


def build_prediction_strategy_counts(
    status_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in status_rows:
        bucket = prediction_strategy_bucket(row.get("route_type"))
        grouped[bucket][str(row.get("candidate_status") or "unknown")] += int(
            row.get("candidate_count") or 0
        )
    output = []
    for bucket, counts in grouped.items():
        total = sum(counts.values())
        output.append(
            {
                "strategy_bucket": bucket,
                "strategy_name": prediction_strategy_name(bucket),
                "candidate_count": total,
                "paper_candidate": counts.get("paper_candidate", 0),
                "contract_review": counts.get("contract_review", 0),
                "near_miss": counts.get("near_miss", 0),
                "incomplete_book": counts.get("incomplete_book", 0),
            }
        )
    output.sort(key=lambda row: row["candidate_count"], reverse=True)
    return output


def build_prediction_opportunity_dashboard(
    candidates: list[dict[str, Any]],
    strategy_counts: list[dict[str, Any]],
    screen_reasons: list[dict[str, Any]],
    *,
    alert_limit: int = 8,
) -> dict[str, Any]:
    alerts = prediction_candidate_alerts(candidates, limit=alert_limit)
    return {
        "strategy_counts": strategy_counts,
        "screen_reasons": screen_reasons,
        "alerts": alerts,
        "new_opportunities": sum(
            1 for row in candidates if row.get("lifecycle_status") == "new"
        ),
        "improving_opportunities": sum(
            1 for row in candidates if row.get("lifecycle_status") == "improved"
        ),
        "close_to_paper": sum(
            1
            for row in candidates
            if row.get("candidate_status") in {"paper_candidate", "near_miss"}
        ),
        "contract_review_positive": sum(
            1
            for row in candidates
            if row.get("candidate_status") == "contract_review"
            and (numeric(row.get("expected_net_profit")) or 0.0) > 0
        ),
        "missing_depth": sum(
            1 for row in candidates if row.get("candidate_status") == "incomplete_book"
        ),
        "storage_policy": "latest_scan_only",
    }


def prediction_candidate_alerts(
    candidates: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    alerts = []
    seen: set[tuple[str, str]] = set()
    for row in candidates:
        route_key = str(row.get("route_key") or "")
        status = str(row.get("candidate_status") or "")
        lifecycle = str(row.get("lifecycle_status") or "")
        alert_type = None
        level = "info"
        reason = row.get("screen_reason") or row.get("blocking_reason") or ""
        expected_net_profit = numeric(row.get("expected_net_profit")) or 0.0
        if status == "paper_candidate":
            alert_type = "paper_ready"
            level = "good"
            reason = "Passed paper filters."
        elif status == "contract_review" and expected_net_profit > 0:
            alert_type = "contract_review_positive"
            level = "warn"
        elif status == "near_miss":
            alert_type = "near_miss"
            level = "info"
        elif lifecycle == "improved":
            alert_type = "improving"
            level = "info"
        elif lifecycle == "new":
            alert_type = "new_candidate"
            level = "info"
        if not alert_type:
            continue
        identity = (route_key, alert_type)
        if identity in seen:
            continue
        seen.add(identity)
        alerts.append(
            {
                "alert_type": alert_type,
                "level": level,
                "title": row.get("title"),
                "route_key": row.get("route_key"),
                "strategy_bucket": row.get("strategy_bucket"),
                "strategy_name": row.get("strategy_name"),
                "candidate_status": status,
                "lifecycle_status": lifecycle,
                "candidate_score": row.get("candidate_score"),
                "expected_net_profit": row.get("expected_net_profit"),
                "net_edge_per_share": row.get("net_edge_per_share"),
                "reason": reason,
            }
        )
    alerts.sort(
        key=lambda row: (
            {"good": 3, "warn": 2, "info": 1}.get(str(row.get("level")), 0),
            numeric(row.get("candidate_score")) or -math.inf,
            numeric(row.get("expected_net_profit")) or -math.inf,
        ),
        reverse=True,
    )
    return alerts[: max(0, int(limit))]


def numeric(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def delta(current: Any, previous: Any) -> float | None:
    current_number = numeric(current)
    previous_number = numeric(previous)
    if current_number is None or previous_number is None:
        return None
    return current_number - previous_number


def improved(score_delta: float | None, profit_delta: float | None) -> bool:
    return bool(
        (score_delta is not None and score_delta >= 1.0)
        or (profit_delta is not None and profit_delta >= 0.25)
    )


def weakened(score_delta: float | None, profit_delta: float | None) -> bool:
    return bool(
        (score_delta is not None and score_delta <= -1.0)
        or (profit_delta is not None and profit_delta <= -0.25)
    )
