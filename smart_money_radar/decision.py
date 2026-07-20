from __future__ import annotations

from typing import Any


MIN_DATASET_SNAPSHOTS = 100_000
MIN_INDEPENDENT_EVENTS = 100
MIN_MATURE_WALLET_OPPORTUNITIES = 1_000
MIN_WALLET_DENOMINATOR_COUNT = 50
MIN_IDENTITY_COVERAGE = 0.95
MIN_SHADOW_SIGNALS = 100
MIN_SHADOW_MARKS = 1_000
MIN_SHADOW_DAYS = 60.0


def build_capital_readiness(
    research: dict[str, Any],
    identity: dict[str, Any] | None = None,
    shadow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = identity or {}
    shadow = shadow or {}
    universe = research.get("universe") or {}
    outcomes = research.get("outcomes") or {}
    wallets = research.get("wallets") or {}
    model_runs = research.get("model_runs") or []

    snapshot_count = int(universe.get("snapshot_count") or 0)
    event_count = int(outcomes.get("independent_positive_event_count") or 0)
    matured_opportunities = int(wallets.get("matured_opportunity_count") or 0)
    denominator_wallets = int(wallets.get("wallet_count") or 0)
    identity_coverage = float(identity.get("coverage_ratio") or 0.0)
    shadow_signal_count = int(shadow.get("signal_count") or 0)
    shadow_mark_count = int(shadow.get("mark_count") or 0)
    shadow_days = float(shadow.get("history_days") or 0.0)

    model_ready = any(
        row.get("status") == "completed_ready"
        and bool((row.get("metrics") or {}).get("ready_for_research_claims"))
        for row in model_runs
    )
    gates = {
        "point_in_time_dataset": snapshot_count >= MIN_DATASET_SNAPSHOTS,
        "independent_listing_events": event_count >= MIN_INDEPENDENT_EVENTS,
        "wallet_denominator": matured_opportunities
        >= MIN_MATURE_WALLET_OPPORTUNITIES
        and denominator_wallets >= MIN_WALLET_DENOMINATOR_COUNT,
        "identity_coverage": identity_coverage >= MIN_IDENTITY_COVERAGE,
        "temporal_model": model_ready,
        "shadow_validation": shadow_signal_count >= MIN_SHADOW_SIGNALS
        and shadow_mark_count >= MIN_SHADOW_MARKS
        and shadow_days >= MIN_SHADOW_DAYS,
    }
    dataset_ready = bool(
        gates["point_in_time_dataset"]
        and gates["independent_listing_events"]
        and gates["wallet_denominator"]
        and gates["identity_coverage"]
    )
    trade_eligible = bool(dataset_ready and gates["temporal_model"] and gates["shadow_validation"])
    if trade_eligible:
        capital_state = "trade_eligible"
    elif dataset_ready and gates["temporal_model"]:
        capital_state = "shadow_validation"
    else:
        capital_state = "research_only"

    reasons = []
    labels = {
        "point_in_time_dataset": "Point-in-time universe is below 100,000 snapshots.",
        "independent_listing_events": "Fewer than 100 independent listing events are mature.",
        "wallet_denominator": "The full wallet opportunity denominator is incomplete.",
        "identity_coverage": "Identity coverage is below 95%; unknown wallets cannot count as independent.",
        "temporal_model": "No temporal model has passed the out-of-sample research gate.",
        "shadow_validation": "The 60-day shadow portfolio gate has not passed.",
    }
    for name, passed in gates.items():
        if not passed:
            reasons.append(labels[name])

    return {
        "capital_state": capital_state,
        "trade_eligible": trade_eligible,
        "automatic_execution_enabled": False,
        "gates": gates,
        "reasons": reasons,
        "metrics": {
            "snapshot_count": snapshot_count,
            "independent_event_count": event_count,
            "matured_wallet_opportunities": matured_opportunities,
            "wallet_denominator_count": denominator_wallets,
            "identity_coverage": identity_coverage,
            "shadow_signal_count": shadow_signal_count,
            "shadow_mark_count": shadow_mark_count,
            "shadow_history_days": shadow_days,
        },
    }


def build_signal_decision(
    signal_status: str,
    signal_type: str,
    global_readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    readiness = global_readiness or build_capital_readiness({})
    local_research_candidate = signal_status == "candidate"
    trade_eligible = bool(local_research_candidate and readiness.get("trade_eligible"))
    if trade_eligible:
        decision_state = "trade_eligible"
    elif local_research_candidate:
        decision_state = "research_candidate"
    else:
        decision_state = "watchlist"
    return {
        "decision_state": decision_state,
        "capital_state": readiness.get("capital_state", "research_only"),
        "trade_eligible": trade_eligible,
        "automatic_execution_enabled": False,
        "signal_scope": signal_type,
        "expected_value_available": False,
        "recommended_position_usd": 0.0,
        "gates": readiness.get("gates", {}),
        "blocking_reasons": readiness.get("reasons", []),
    }
