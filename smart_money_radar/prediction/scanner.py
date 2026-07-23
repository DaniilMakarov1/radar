from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from smart_money_radar.prediction.strategies import prediction_strategy_bucket


@dataclass(frozen=True)
class ScannerConfig:
    minimum_net_edge: float = 0.0025
    minimum_expected_profit: float = 0.25
    operations_buffer_rate: float = 0.001
    fixed_operations_cost: float = 0.0
    max_route_size: float = 10_000.0
    minimum_semantic_match: float = 0.92
    max_negative_risk_outcomes: int = 25


def scan_prediction_routes(
    events: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    books: list[dict[str, Any]],
    observed_at: str,
    config: ScannerConfig | None = None,
    trusted_contract_mappings: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    settings = config or ScannerConfig()
    book_map = {(book["venue"], book["market_id"]): book for book in books}
    event_map = {(event["venue"], event["event_id"]): event for event in events}
    constraints = infer_implication_constraints(markets, observed_at)
    matches = match_cross_venue_contracts(
        markets,
        observed_at,
        trusted_contract_mappings=trusted_contract_mappings,
    )

    routes: list[dict[str, Any]] = []
    routes.extend(scan_binary_complements(markets, book_map, observed_at, settings))
    routes.extend(
        scan_complete_sets(event_map, markets, book_map, observed_at, settings)
    )
    routes.extend(
        scan_negative_risk(event_map, markets, book_map, observed_at, settings)
    )
    routes.extend(
        scan_threshold_ladders(
            constraints,
            markets,
            book_map,
            observed_at,
            settings,
        )
    )
    routes.extend(
        scan_implications(
            constraints,
            markets,
            book_map,
            observed_at,
            settings,
        )
    )
    routes.extend(
        scan_cross_venue(
            matches,
            markets,
            book_map,
            observed_at,
            settings,
        )
    )
    routes.sort(
        key=lambda row: (
            row["status"] == "executable",
            row.get("expected_net_profit") or -math.inf,
        ),
        reverse=True,
    )
    return constraints, matches, routes


def build_prediction_route_candidates(
    routes: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates = []
    for route in routes:
        candidate_status = prediction_candidate_status(route)
        if not candidate_status:
            continue
        evidence = route.get("evidence") or {}
        blocking_reasons = list(evidence.get("blocking_reasons") or [])
        advisory_reasons = list(evidence.get("advisory_reasons") or [])
        blocking_reason = blocking_reasons[0] if blocking_reasons else None
        screen_reason = prediction_candidate_screen_reason(
            candidate_status,
            blocking_reason,
        )
        risk_flags = list(
            dict.fromkeys(
                [
                    *(route.get("risk_flags") or []),
                    *(evidence.get("blocking_risk_flags") or []),
                    *(evidence.get("advisory_risk_flags") or []),
                ]
            )
        )
        candidates.append(
            {
                "route_key": route["route_key"],
                "route_type": route["route_type"],
                "strategy_bucket": prediction_strategy_bucket(route["route_type"]),
                "venue_scope": route["venue_scope"],
                "event_id": route.get("event_id"),
                "title": route["title"],
                "candidate_status": candidate_status,
                "route_status": route["status"],
                "candidate_score": prediction_candidate_score(
                    route,
                    candidate_status,
                    len(risk_flags),
                ),
                "expected_net_profit": route.get("expected_net_profit"),
                "net_edge_per_share": route.get("net_edge_per_share"),
                "optimal_size": route.get("optimal_size") or 0.0,
                "max_executable_size": route.get("max_executable_size") or 0.0,
                "blocking_reason": blocking_reason,
                "screen_reason": screen_reason,
                "observed_at": route["observed_at"],
                "risk_flags": risk_flags,
                "evidence": {
                    "route_key": route["route_key"],
                    "route_status": route["status"],
                    "gross_edge_per_share": route.get("gross_edge_per_share"),
                    "expected_gross_profit": route.get("expected_gross_profit"),
                    "capital_required": route.get("capital_required"),
                    "total_fees": route.get("total_fees"),
                    "slippage_cost": route.get("slippage_cost"),
                    "operations_buffer": route.get("operations_buffer"),
                    "confidence_score": route.get("confidence_score"),
                    "semantic_match_score": route.get("semantic_match_score"),
                    "minimum_net_edge": evidence.get("minimum_net_edge"),
                    "minimum_expected_profit": evidence.get("minimum_expected_profit"),
                    "needed_net_edge_improvement": needed_improvement(
                        route.get("net_edge_per_share"),
                        evidence.get("minimum_net_edge"),
                    ),
                    "needed_expected_profit_improvement": needed_improvement(
                        route.get("expected_net_profit"),
                        evidence.get("minimum_expected_profit"),
                    ),
                    "blocking_reasons": blocking_reasons,
                    "advisory_reasons": advisory_reasons,
                    "rationale": route.get("rationale") or [],
                },
            }
        )
    candidates.sort(
        key=lambda row: (
            row["candidate_score"],
            row.get("expected_net_profit") or -math.inf,
            row.get("net_edge_per_share") or -math.inf,
        ),
        reverse=True,
    )
    if limit is None:
        return candidates
    return candidates[: max(0, int(limit))]


def prediction_candidate_status(route: dict[str, Any]) -> str | None:
    status = route.get("status")
    expected_net_profit = numeric(route.get("expected_net_profit"))
    net_edge = numeric(route.get("net_edge_per_share"))
    gross_edge = numeric(route.get("gross_edge_per_share"))
    has_positive_shape = any(
        value is not None and value > 0
        for value in (expected_net_profit, net_edge, gross_edge)
    )
    if status in {"executable", "paper_candidate"}:
        return "paper_candidate"
    if status == "contract_review" and has_positive_shape:
        return "contract_review"
    if status == "not_profitable" and net_edge is not None and net_edge >= -0.01:
        return "near_miss"
    if status == "incomplete_book":
        return "incomplete_book"
    return None


def prediction_candidate_screen_reason(
    candidate_status: str,
    blocking_reason: str | None,
) -> str:
    if blocking_reason:
        return blocking_reason
    return {
        "paper_candidate": "passed_paper_filters",
        "contract_review": "manual_contract_review_required",
        "near_miss": "below_profit_or_edge_gate",
        "incomplete_book": "incomplete_orderbook_depth",
    }.get(candidate_status, "not_selected")


def prediction_candidate_score(
    route: dict[str, Any],
    candidate_status: str,
    risk_flag_count: int,
) -> float:
    status_component = {
        "paper_candidate": 1.0,
        "contract_review": 0.75,
        "near_miss": 0.45,
        "incomplete_book": 0.25,
    }.get(candidate_status, 0.0)
    expected_net_profit = max(0.0, numeric(route.get("expected_net_profit")) or 0.0)
    net_edge = numeric(route.get("net_edge_per_share"))
    profit_component = math.tanh(expected_net_profit / 100.0)
    edge_component = 0.0
    if net_edge is not None:
        edge_component = max(0.0, min(1.0, (net_edge + 0.01) / 0.05))
    confidence_component = max(
        0.0,
        min(1.0, (numeric(route.get("confidence_score")) or 0.0) / 100.0),
    )
    risk_penalty = 0.04 * min(max(0, risk_flag_count), 6)
    score = (
        0.45 * status_component
        + 0.30 * profit_component
        + 0.15 * edge_component
        + 0.10 * confidence_component
        - risk_penalty
    )
    return round(max(0.0, min(100.0, score * 100.0)), 2)


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


def needed_improvement(current: Any, required: Any) -> float | None:
    current_number = numeric(current)
    required_number = numeric(required)
    if current_number is None or required_number is None:
        return None
    return max(0.0, required_number - current_number)


def scan_binary_complements(
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    routes = []
    for market in markets:
        if market.get("status") != "open" or not market.get("accepting_orders"):
            continue
        book = books.get((market["venue"], market["market_id"]))
        if not book:
            continue
        legs = [
            market_leg(market, book, "buy", "yes"),
            market_leg(market, book, "buy", "no"),
        ]
        routes.append(
            evaluate_route(
                route_type="binary_complement",
                title=market["question"],
                venue_scope=market["venue"],
                event_id=market["event_id"],
                legs=legs,
                observed_at=observed_at,
                config=config,
                payout_per_share=1.0,
                locked_until=market.get("expected_resolution_at")
                or market.get("closes_at"),
                confidence_score=99.0,
                rationale=["YES and NO form a complete binary payout of 1.00."],
                risk_flags=["multi_leg_execution_unvalidated"],
                critical_review=False,
                execution_atomic=False,
                source_url=market.get("event_source_url"),
            )
        )
    return routes


def scan_complete_sets(
    events: dict[tuple[str, str], dict[str, Any]],
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    grouped = group_markets(markets)
    routes = []
    for key, event_markets in grouped.items():
        event = events.get(key)
        if not event or not event.get("mutually_exclusive") or not event.get("exhaustive"):
            continue
        if any(
            market.get("status") != "open" or not market.get("accepting_orders")
            for market in event_markets
        ):
            continue
        unresolved = event_markets
        if len(unresolved) < 2:
            continue
        yes_legs = []
        no_legs = []
        yes_incomplete = False
        no_incomplete = False
        for market in unresolved:
            book = books.get((market["venue"], market["market_id"]))
            if not book or not book.get("yes_asks"):
                yes_incomplete = True
            else:
                yes_legs.append(market_leg(market, book, "buy", "yes"))
            if not book or not book.get("no_asks"):
                no_incomplete = True
            else:
                no_legs.append(market_leg(market, book, "buy", "no"))
        if not yes_incomplete:
            routes.append(
                evaluate_route(
                    route_type="complete_set",
                    title=event["title"],
                    venue_scope=event["venue"],
                    event_id=event["event_id"],
                    legs=yes_legs,
                    observed_at=observed_at,
                    config=config,
                    payout_per_share=1.0,
                    locked_until=event.get("expected_resolution_at")
                    or event.get("closes_at"),
                    confidence_score=98.0,
                    rationale=[
                        "The normalized event is mutually exclusive and exhaustive.",
                        f"The route buys all {len(yes_legs)} unresolved YES outcomes.",
                    ],
                    risk_flags=["multi_leg_execution_unvalidated"],
                    critical_review=False,
                    execution_atomic=False,
                    source_url=event.get("source_url"),
                )
            )
        if not no_incomplete:
            payout = max(0.0, float(len(no_legs) - 1))
            routes.append(
                evaluate_route(
                    route_type="complete_set_no",
                    title=f"{event['title']}: all NO basket",
                    venue_scope=event["venue"],
                    event_id=event["event_id"],
                    legs=no_legs,
                    observed_at=observed_at,
                    config=config,
                    payout_per_share=payout,
                    locked_until=event.get("expected_resolution_at")
                    or event.get("closes_at"),
                    confidence_score=98.0,
                    rationale=[
                        "The normalized event is mutually exclusive and exhaustive.",
                        (
                            f"The route buys all {len(no_legs)} unresolved NO outcomes; "
                            f"exactly {len(no_legs) - 1} NO legs pay if one outcome resolves YES."
                        ),
                    ],
                    risk_flags=["multi_leg_execution_unvalidated"],
                    critical_review=False,
                    execution_atomic=False,
                    source_url=event.get("source_url"),
                )
            )
    return routes


def scan_negative_risk(
    events: dict[tuple[str, str], dict[str, Any]],
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    grouped = group_markets(markets)
    routes = []
    for key, event_markets in grouped.items():
        event = events.get(key)
        if (
            not event
            or not event.get("neg_risk")
            or not event.get("mutually_exclusive")
            or not event.get("exhaustive")
            or event.get("augmented_neg_risk")
        ):
            continue
        if any(
            market.get("status") != "open" or not market.get("accepting_orders")
            for market in event_markets
        ):
            continue
        unresolved = event_markets
        if len(unresolved) < 2 or len(unresolved) > config.max_negative_risk_outcomes:
            continue
        for source_market in unresolved:
            source_book = books.get((source_market["venue"], source_market["market_id"]))
            if not source_book or not source_book.get("no_asks"):
                continue
            legs = [market_leg(source_market, source_book, "buy", "no")]
            complete = True
            for other in unresolved:
                if other["market_id"] == source_market["market_id"]:
                    continue
                other_book = books.get((other["venue"], other["market_id"]))
                if not other_book or not other_book.get("yes_bids"):
                    complete = False
                    break
                legs.append(market_leg(other, other_book, "sell", "yes"))
            if not complete:
                continue
            routes.append(
                evaluate_route(
                    route_type="negative_risk_conversion",
                    title=f"{event['title']}: NO {source_market.get('outcome_label') or source_market['question']}",
                    venue_scope=event["venue"],
                    event_id=event["event_id"],
                    legs=legs,
                    observed_at=observed_at,
                    config=config,
                    payout_per_share=0.0,
                    locked_until=None,
                    confidence_score=86.0,
                    rationale=[
                        "One NO converts atomically into one YES for every other outcome.",
                        "All resulting YES legs are valued against executable bids.",
                    ],
                    risk_flags=["multi_book_non_atomic_execution"],
                    critical_review=False,
                    execution_atomic=False,
                    source_url=event.get("source_url"),
                    extra_evidence={
                        "negative_risk_conversion": {
                            "source_market_id": source_market["market_id"],
                            "source_outcome": (
                                source_market.get("outcome_label")
                                or source_market["question"]
                            ),
                            "converted_yes_count": len(legs) - 1,
                            "event_exhaustive": bool(event.get("exhaustive")),
                            "event_mutually_exclusive": bool(
                                event.get("mutually_exclusive")
                            ),
                            "augmented_neg_risk": bool(
                                event.get("augmented_neg_risk")
                            ),
                        }
                    },
                )
            )
    return routes


def scan_implications(
    constraints: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    market_map = {(market["venue"], market["market_id"]): market for market in markets}
    routes = []
    for constraint in constraints:
        if constraint.get("relation_type") == "threshold_implication":
            continue
        key_a = (constraint["venue"], constraint["antecedent_market_id"])
        key_b = (constraint["venue"], constraint["consequent_market_id"])
        antecedent = market_map.get(key_a)
        consequent = market_map.get(key_b)
        book_a = books.get(key_a)
        book_b = books.get(key_b)
        if not antecedent or not consequent or not book_a or not book_b:
            continue
        legs = [
            market_leg(consequent, book_b, "buy", "yes"),
            market_leg(antecedent, book_a, "buy", "no"),
        ]
        routes.append(
            evaluate_route(
                route_type="logical_implication",
                title=f"{antecedent['question']} implies {consequent['question']}",
                venue_scope=constraint["venue"],
                event_id=antecedent["event_id"],
                legs=legs,
                observed_at=observed_at,
                config=config,
                payout_per_share=1.0,
                locked_until=max_timestamp(
                    antecedent.get("expected_resolution_at"),
                    consequent.get("expected_resolution_at"),
                ),
                confidence_score=constraint["confidence_score"] * 100,
                rationale=constraint["rationale"],
                risk_flags=[
                    "auto_parsed_contract_semantics",
                    "multi_leg_execution_unvalidated",
                ],
                critical_review=True,
                execution_atomic=False,
                source_url=antecedent.get("event_source_url"),
                blocking_reasons=[
                    "Automatically parsed implication constraints require manual contract review before paper eligibility."
                ],
                blocking_risk_flags=["auto_parsed_contract_semantics"],
            )
        )
    return routes


def scan_threshold_ladders(
    constraints: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    market_map = {(market["venue"], market["market_id"]): market for market in markets}
    routes = []
    for constraint in constraints:
        if constraint.get("relation_type") != "threshold_implication":
            continue
        evidence = constraint.get("evidence") or {}
        if not evidence.get("deterministic_threshold_ladder"):
            continue
        key_high = (constraint["venue"], constraint["antecedent_market_id"])
        key_low = (constraint["venue"], constraint["consequent_market_id"])
        higher = market_map.get(key_high)
        lower = market_map.get(key_low)
        higher_book = books.get(key_high)
        lower_book = books.get(key_low)
        if not higher or not lower or not higher_book or not lower_book:
            continue
        if (
            higher.get("status") != "open"
            or lower.get("status") != "open"
            or not higher.get("accepting_orders")
            or not lower.get("accepting_orders")
        ):
            continue
        legs = [
            market_leg(lower, lower_book, "buy", "yes"),
            market_leg(higher, higher_book, "buy", "no"),
        ]
        routes.append(
            evaluate_route(
                route_type="threshold_ladder",
                title=f"{higher['question']} implies {lower['question']}",
                venue_scope=constraint["venue"],
                event_id=f"{lower['event_id']}|{higher['event_id']}",
                legs=legs,
                observed_at=observed_at,
                config=config,
                payout_per_share=1.0,
                locked_until=max_timestamp(
                    higher.get("expected_resolution_at"),
                    lower.get("expected_resolution_at"),
                ),
                confidence_score=96.0,
                rationale=[
                    *list(constraint.get("rationale") or []),
                    (
                        "Buying YES on the lower threshold and NO on the higher "
                        "threshold has a conservative minimum payout of 1.00."
                    ),
                ],
                risk_flags=["multi_leg_execution_unvalidated"],
                critical_review=False,
                execution_atomic=False,
                source_url=lower.get("event_source_url"),
                advisory_reasons=[
                    (
                        "Deterministic parser matched the same venue, asset, date, "
                        "direction, and threshold family; review contract text before real execution."
                    )
                ],
                advisory_risk_flags=["auto_parsed_contract_semantics"],
            )
        )
    return routes


def scan_cross_venue(
    matches: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    market_map = {(market["venue"], market["market_id"]): market for market in markets}
    routes = []
    for match in matches:
        market_a = market_map.get((match["venue_a"], match["market_id_a"]))
        market_b = market_map.get((match["venue_b"], match["market_id_b"]))
        book_a = books.get((match["venue_a"], match["market_id_a"]))
        book_b = books.get((match["venue_b"], match["market_id_b"]))
        if not market_a or not market_b or not book_a or not book_b:
            continue
        critical = (
            match["match_score"] < config.minimum_semantic_match
            or not bool(
                (match.get("evidence") or {}).get("contract_terms_verified")
            )
        )
        venue_scope = "+".join(
            sorted({str(match["venue_a"]), str(match["venue_b"])})
        )
        risks = list(match["risk_flags"])
        for yes_market, yes_book, no_market, no_book in (
            (market_a, book_a, market_b, book_b),
            (market_b, book_b, market_a, book_a),
        ):
            routes.append(
                evaluate_route(
                    route_type="cross_venue_complement",
                    title=f"{yes_market['question']} / {no_market['question']}",
                    venue_scope=venue_scope,
                    event_id=f"{market_a['event_id']}|{market_b['event_id']}",
                    legs=[
                        market_leg(yes_market, yes_book, "buy", "yes"),
                        market_leg(no_market, no_book, "buy", "no"),
                    ],
                    observed_at=observed_at,
                    config=config,
                    payout_per_share=1.0,
                    locked_until=max_timestamp(
                        market_a.get("expected_resolution_at"),
                        market_b.get("expected_resolution_at"),
                    ),
                    confidence_score=match["match_score"] * 100,
                    semantic_match_score=match["match_score"],
                    rationale=match["rationale"],
                    risk_flags=risks,
                    critical_review=critical,
                    execution_atomic=False,
                    source_url=yes_market.get("event_source_url"),
                    blocking_reasons=(match.get("evidence") or {}).get(
                        "blocking_reasons",
                        [],
                    ),
                    blocking_risk_flags=(match.get("evidence") or {}).get(
                        "blocking_risk_flags",
                        [],
                    ),
                    advisory_reasons=(match.get("evidence") or {}).get(
                        "advisory_reasons",
                        [],
                    ),
                    advisory_risk_flags=(match.get("evidence") or {}).get(
                        "advisory_risk_flags",
                        [],
                    ),
                    extra_evidence={
                        "cross_venue_match": {
                            "status": match.get("status"),
                            "match_score": match.get("match_score"),
                            "trusted_mapping": bool(
                                (match.get("evidence") or {}).get("trusted_mapping")
                            ),
                            "mapping_id": (match.get("evidence") or {}).get(
                                "mapping_id"
                            ),
                            "contract_terms_verified": bool(
                                (match.get("evidence") or {}).get(
                                    "contract_terms_verified"
                                )
                            ),
                        }
                    },
                )
            )
    return routes


def evaluate_route(
    *,
    route_type: str,
    title: str,
    venue_scope: str,
    event_id: str | None,
    legs: list[dict[str, Any]],
    observed_at: str,
    config: ScannerConfig,
    payout_per_share: float,
    locked_until: str | None,
    confidence_score: float,
    rationale: list[str],
    risk_flags: list[str],
    critical_review: bool,
    execution_atomic: bool,
    source_url: str | None,
    semantic_match_score: float | None = None,
    blocking_reasons: list[str] | None = None,
    blocking_risk_flags: list[str] | None = None,
    advisory_reasons: list[str] | None = None,
    advisory_risk_flags: list[str] | None = None,
    extra_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocking_reasons = list(blocking_reasons or [])
    blocking_risk_flags = list(blocking_risk_flags or [])
    advisory_reasons = list(advisory_reasons or [])
    advisory_risk_flags = list(advisory_risk_flags or [])
    fee_model_verified = all(bool(leg.get("fee_verified")) for leg in legs)
    if not fee_model_verified:
        critical_review = True
        risk_flags = [*risk_flags, "fee_schedule_unverified"]
        blocking_risk_flags.append("fee_schedule_unverified")
        blocking_reasons.append(
            "At least one leg has no verified or conservative public fee schedule."
        )
    if any(public_fee_source(leg.get("fee_source")) for leg in legs):
        risk_flags = [*risk_flags, "public_fee_assumptions"]
        advisory_risk_flags.append("public_fee_assumptions")
        advisory_reasons.append(
            "One or more legs use public conservative fee assumptions; account fees must be checked before any real execution."
        )
    if critical_review and not blocking_reasons:
        risk_flags = [*risk_flags, "manual_contract_review_required"]
        blocking_risk_flags.append("manual_contract_review_required")
        blocking_reasons.append(
            "The route requires manual contract review before paper eligibility."
        )
    common_capacity = min((leg_capacity(leg) for leg in legs), default=0.0)
    common_capacity = min(common_capacity, config.max_route_size)
    minimum_size = max((leg.get("min_order_size", 1.0) for leg in legs), default=1.0)
    sizes = route_candidate_sizes(legs, minimum_size, common_capacity)
    evaluations = [
        evaluate_size(legs, size, payout_per_share, config)
        for size in sizes
    ]
    evaluations = [row for row in evaluations if row is not None]
    best = max(evaluations, key=lambda row: row["net_profit"], default=None)
    if best is None:
        best = empty_evaluation()
        status = "incomplete_book"
    elif critical_review:
        status = "contract_review"
    elif (
        best["net_edge_per_share"] >= config.minimum_net_edge
        and best["net_profit"] >= config.minimum_expected_profit
    ):
        status = "executable" if execution_atomic else "paper_candidate"
    else:
        status = "not_profitable"

    lock_days = capital_lock_days(observed_at, locked_until) if payout_per_share else 0.0
    annualized = None
    if (
        payout_per_share
        and lock_days is not None
        and lock_days > 0
        and (best["capital_required"] or 0) > 0
    ):
        annualized = (
            best["net_profit"] / best["capital_required"] * 365.0 / lock_days
        )
    route_key = stable_route_key(route_type, legs)
    return {
        "route_key": route_key,
        "route_type": route_type,
        "title": title,
        "venue_scope": venue_scope,
        "event_id": event_id,
        "status": status,
        "confidence_score": max(0.0, min(100.0, confidence_score)),
        "semantic_match_score": semantic_match_score,
        "guaranteed_payout_per_share": payout_per_share,
        "max_executable_size": common_capacity,
        "optimal_size": best["size"],
        "gross_edge_per_share": best["gross_edge_per_share"],
        "net_edge_per_share": best["net_edge_per_share"],
        "expected_gross_profit": best["gross_profit"],
        "expected_net_profit": best["net_profit"],
        "capital_required": best["capital_required"],
        "total_fees": best["fees"],
        "slippage_cost": best["slippage"],
        "operations_buffer": best["operations_buffer"],
        "capital_lock_days": lock_days,
        "annualized_return": annualized,
        "observed_at": observed_at,
        "source_url": source_url,
        "legs": best["legs"] or serializable_legs(legs),
        "rationale": rationale,
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "evidence": {
            "minimum_net_edge": config.minimum_net_edge,
            "minimum_expected_profit": config.minimum_expected_profit,
            "candidate_size_count": len(sizes),
            "fee_model": "venue fee rate times C*p*(1-p), with conservative Kalshi rounding buffer",
            "full_depth_vwap": True,
            "fee_model_verified": fee_model_verified,
            "execution_atomic": execution_atomic,
            "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
            "blocking_risk_flags": list(dict.fromkeys(blocking_risk_flags)),
            "advisory_reasons": list(dict.fromkeys(advisory_reasons)),
            "advisory_risk_flags": list(dict.fromkeys(advisory_risk_flags)),
            **(extra_evidence or {}),
        },
    }


def evaluate_size(
    legs: list[dict[str, Any]],
    size: float,
    payout_per_share: float,
    config: ScannerConfig,
) -> dict[str, Any] | None:
    if size <= 0:
        return None
    buy_notional = 0.0
    sell_notional = 0.0
    fees = 0.0
    slippage = 0.0
    evaluated_legs = []
    for leg in legs:
        walked = walk_levels(leg["levels"], size)
        if walked is None:
            return None
        fee = venue_fee(
            leg["venue"],
            walked["fills"],
            leg.get("fee_rate", 0.0),
        )
        top_price = leg["levels"][0][0]
        if leg["action"] == "buy":
            buy_notional += walked["notional"]
            slippage += max(0.0, walked["notional"] - top_price * size)
        else:
            sell_notional += walked["notional"]
            slippage += max(0.0, top_price * size - walked["notional"])
        fees += fee
        evaluated_legs.append(
            {
                **{key: value for key, value in leg.items() if key != "levels"},
                "levels": leg["levels"][:100],
                "top_price": top_price,
                "vwap": walked["vwap"],
                "notional": walked["notional"],
                "fee": fee,
                "filled_size": size,
            }
        )
    gross_profit = payout_per_share * size + sell_notional - buy_notional
    capital = buy_notional
    guaranteed_payout = max(0.0, payout_per_share) * size
    operations_buffer = (
        config.operations_buffer_rate * max(capital, guaranteed_payout, size)
        + config.fixed_operations_cost
    )
    net_profit = gross_profit - fees - operations_buffer
    return {
        "size": size,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "gross_edge_per_share": gross_profit / size,
        "net_edge_per_share": net_profit / size,
        "capital_required": capital,
        "fees": fees,
        "slippage": slippage,
        "operations_buffer": operations_buffer,
        "legs": evaluated_legs,
    }


def market_leg(
    market: dict[str, Any],
    book: dict[str, Any],
    action: str,
    outcome: str,
) -> dict[str, Any]:
    levels_key = f"{outcome}_{'asks' if action == 'buy' else 'bids'}"
    return {
        "venue": market["venue"],
        "market_id": market["market_id"],
        "event_id": market["event_id"],
        "question": market["question"],
        "outcome_label": market.get("outcome_label"),
        "action": action,
        "outcome": outcome,
        "liquidity_role": "taker",
        "levels": book.get(levels_key, []),
        "fee_rate": market.get("fee_rate", 0.0),
        "fee_exponent": market.get("fee_exponent", 1.0),
        "fee_taker_only": market.get("fee_taker_only", True),
        "fee_verified": bool(market.get("fee_verified")),
        "fee_source": market.get("fee_source") or (market.get("raw") or {}).get("fee_policy"),
        "tick_size": market.get("tick_size", 0.01),
        "min_order_size": market.get("min_order_size", 1.0),
    }


def public_fee_source(value: Any) -> bool:
    text = str(value or "").lower()
    return "public" in text or "conservative" in text


def infer_implication_constraints(
    markets: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    grouped: dict[
        tuple[str, str, str, str, str],
        list[tuple[dict[str, Any], float, dict[str, Any]]],
    ] = {}
    for market in markets:
        if market.get("status") != "open" or not market.get("accepting_orders"):
            continue
        terms = threshold_terms(market)
        if not terms or terms.get("direction") != "above":
            continue
        grouped.setdefault(
            (
                market["venue"],
                terms["asset"],
                terms["date_key"],
                terms["direction"],
                terms["condition_family"],
            ),
            [],
        ).append(
            (market, float(terms["threshold"]), terms)
        )
    constraints = []
    for (venue, asset, date_key, direction, condition_family), rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row[1])
        for index, (lower_market, lower_value, lower_terms) in enumerate(ordered[:-1]):
            for higher_market, higher_value, higher_terms in ordered[index + 1 :]:
                constraints.append(
                    {
                        "venue": venue,
                        "antecedent_market_id": higher_market["market_id"],
                        "consequent_market_id": lower_market["market_id"],
                        "relation_type": "threshold_implication",
                        "confidence_score": 0.96,
                        "source": "deterministic_threshold_ladder_v1",
                        "rationale": [
                            (
                                f"{asset} {direction} {higher_value:g} implies "
                                f"{asset} {direction} {lower_value:g} on {date_key}."
                            )
                        ],
                        "evidence": {
                            "asset": asset,
                            "date_key": date_key,
                            "direction": direction,
                            "condition_family": condition_family,
                            "higher_threshold": higher_value,
                            "lower_threshold": lower_value,
                            "higher_terms": higher_terms,
                            "lower_terms": lower_terms,
                            "deterministic_threshold_ladder": True,
                        },
                        "created_at": observed_at,
                    }
                )
    return constraints


def match_cross_venue_contracts(
    markets: list[dict[str, Any]],
    observed_at: str,
    trusted_contract_mappings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    market_map = {(row["venue"], row["market_id"]): row for row in markets}
    trusted_candidates = trusted_cross_venue_contract_matches(
        market_map,
        trusted_contract_mappings or [],
        observed_at,
    )
    trusted_pair_keys = {
        contract_pair_key(candidate)
        for candidate in trusted_candidates
    }
    polymarket = [row for row in markets if row["venue"] == "polymarket" and row["status"] == "open"]
    kalshi = [row for row in markets if row["venue"] == "kalshi" and row["status"] == "open"]
    candidates = []
    for market_a in polymarket:
        for market_b in kalshi:
            pair_key = frozenset(
                {
                    market_ref("polymarket", market_a["market_id"]),
                    market_ref("kalshi", market_b["market_id"]),
                }
            )
            if pair_key in trusted_pair_keys:
                continue
            event_score = text_similarity(
                canonical_event_text(market_a),
                canonical_event_text(market_b),
            )
            outcome_score = outcome_similarity(market_a, market_b)
            if event_score < 0.52 or outcome_score < 0.65:
                continue
            deadline_delta = timestamp_delta_hours(
                market_a.get("expected_resolution_at") or market_a.get("closes_at"),
                market_b.get("expected_resolution_at") or market_b.get("closes_at"),
            )
            deadline_score = 0.5 if deadline_delta is None else max(
                0.0, 1.0 - deadline_delta / (24.0 * 14.0)
            )
            rules_score = text_similarity(
                market_a.get("resolution_rules") or "",
                market_b.get("resolution_rules") or "",
            )
            match_score = (
                0.52 * event_score
                + 0.28 * outcome_score
                + 0.12 * deadline_score
                + 0.08 * rules_score
            )
            cancellation_match = cancellation_rules_match(
                market_a.get("cancellation_rules"),
                market_b.get("cancellation_rules"),
            )
            risk_flags = []
            source_score = text_similarity(
                market_a.get("resolution_source") or "",
                market_b.get("resolution_source") or "",
            )
            review = cross_venue_contract_review(
                market_a,
                market_b,
                deadline_delta,
                cancellation_match,
                rules_score,
                source_score,
            )
            risk_flags = review["risk_flags"]
            contract_terms_verified = bool(review["contract_terms_verified"])
            candidates.append(
                {
                    "venue_a": "polymarket",
                    "market_id_a": market_a["market_id"],
                    "venue_b": "kalshi",
                    "market_id_b": market_b["market_id"],
                    "match_score": match_score,
                    "status": (
                        "matched"
                        if match_score >= 0.92 and contract_terms_verified
                        else "review"
                    ),
                    "deadline_delta_hours": deadline_delta,
                    "cancellation_match": cancellation_match,
                    "rationale": [
                        f"Event title similarity: {event_score:.2f}.",
                        f"Outcome similarity: {outcome_score:.2f}.",
                        f"Resolution-rule similarity: {rules_score:.2f}.",
                        f"Resolution-source similarity: {source_score:.2f}.",
                    ],
                    "risk_flags": risk_flags,
                    "evidence": {
                        "event_score": event_score,
                        "outcome_score": outcome_score,
                        "deadline_score": deadline_score,
                        "rules_score": rules_score,
                        "source_score": source_score,
                        "contract_terms_verified": contract_terms_verified,
                        "blocking_reasons": review["blocking_reasons"],
                        "blocking_risk_flags": review["blocking_risk_flags"],
                        "advisory_reasons": review["advisory_reasons"],
                        "advisory_risk_flags": review["advisory_risk_flags"],
                        "contract_terms": review["contract_terms"],
                    },
                    "created_at": observed_at,
                }
            )
    candidates.sort(key=lambda row: row["match_score"], reverse=True)
    used_refs: set[str] = set()
    selected = []
    for candidate in trusted_candidates:
        refs = contract_match_refs(candidate)
        if any(ref in used_refs for ref in refs):
            continue
        used_refs.update(refs)
        selected.append(candidate)
    for candidate in candidates:
        if candidate["match_score"] < 0.76:
            break
        refs = contract_match_refs(candidate)
        if any(ref in used_refs for ref in refs):
            continue
        used_refs.update(refs)
        selected.append(candidate)
    return selected


def trusted_cross_venue_contract_matches(
    market_map: dict[tuple[str, str], dict[str, Any]],
    mappings: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    matches = []
    for mapping in mappings:
        if str(mapping.get("status") or "active") != "active":
            continue
        if str(mapping.get("relation_type") or "equivalent") != "equivalent":
            continue
        venue_a = str(mapping.get("venue_a") or "")
        venue_b = str(mapping.get("venue_b") or "")
        market_id_a = str(mapping.get("market_id_a") or "")
        market_id_b = str(mapping.get("market_id_b") or "")
        if not venue_a or not venue_b or not market_id_a or not market_id_b:
            continue
        if venue_a == venue_b:
            continue
        market_a = market_map.get((venue_a, market_id_a))
        market_b = market_map.get((venue_b, market_id_b))
        if not market_a or not market_b:
            continue
        if market_a.get("status") != "open" or market_b.get("status") != "open":
            continue
        confidence_value = numeric(mapping.get("confidence_score"))
        if confidence_value is None:
            confidence_value = 1.0
        confidence_score = max(0.0, min(1.0, confidence_value))
        deadline_delta = timestamp_delta_hours(
            market_a.get("expected_resolution_at") or market_a.get("closes_at"),
            market_b.get("expected_resolution_at") or market_b.get("closes_at"),
        )
        rationale = list(mapping.get("rationale") or [])
        if not rationale:
            rationale = [
                (
                    f"Trusted manual mapping: {venue_a}:{market_id_a} is equivalent "
                    f"to {venue_b}:{market_id_b}."
                )
            ]
        matches.append(
            {
                "venue_a": venue_a,
                "market_id_a": market_id_a,
                "venue_b": venue_b,
                "market_id_b": market_id_b,
                "match_score": confidence_score,
                "status": "matched",
                "deadline_delta_hours": deadline_delta,
                "cancellation_match": True,
                "rationale": rationale,
                "risk_flags": [],
                "evidence": {
                    "trusted_mapping": True,
                    "mapping_id": mapping.get("prediction_verified_contract_mapping_id")
                    or mapping.get("mapping_id"),
                    "relation_type": mapping.get("relation_type", "equivalent"),
                    "verified_by": mapping.get("verified_by"),
                    "verified_at": mapping.get("verified_at"),
                    "notes": mapping.get("notes"),
                    "contract_terms_verified": True,
                    "blocking_reasons": [],
                    "blocking_risk_flags": [],
                    "advisory_reasons": [],
                    "advisory_risk_flags": [],
                },
                "created_at": observed_at,
            }
        )
    matches.sort(key=lambda row: row["match_score"], reverse=True)
    return matches


def market_ref(venue: str, market_id: str) -> str:
    return f"{venue}:{market_id}"


def contract_match_refs(match: dict[str, Any]) -> set[str]:
    return {
        market_ref(str(match.get("venue_a") or ""), str(match.get("market_id_a") or "")),
        market_ref(str(match.get("venue_b") or ""), str(match.get("market_id_b") or "")),
    }


def contract_pair_key(match: dict[str, Any]) -> frozenset[str]:
    return frozenset(contract_match_refs(match))


def cross_venue_contract_review(
    left: dict[str, Any],
    right: dict[str, Any],
    deadline_delta_hours: float | None,
    cancellation_match: bool,
    rules_score: float,
    source_score: float,
) -> dict[str, Any]:
    terms_left = contract_terms(left)
    terms_right = contract_terms(right)
    blocking_reasons: list[str] = []
    advisory_reasons: list[str] = []
    blocking_risk_flags: list[str] = []
    advisory_risk_flags: list[str] = []

    def block(flag: str, reason: str) -> None:
        blocking_risk_flags.append(flag)
        blocking_reasons.append(reason)

    def advise(flag: str, reason: str) -> None:
        advisory_risk_flags.append(flag)
        advisory_reasons.append(reason)

    rules_present = bool(
        str(left.get("resolution_rules") or "").strip()
        and str(right.get("resolution_rules") or "").strip()
    )
    sources_present = bool(
        str(left.get("resolution_source") or "").strip()
        and str(right.get("resolution_source") or "").strip()
    )
    if not cancellation_match:
        block(
            "cancellation_rules_differ",
            "Cancellation, void, refund, or Other-bucket rules are missing or materially different.",
        )
    if deadline_delta_hours is None:
        block(
            "resolution_deadline_unverified",
            "Both contracts must expose comparable resolution deadlines.",
        )
    elif deadline_delta_hours > 72:
        block(
            "resolution_deadline_differs",
            f"Resolution deadlines differ by {deadline_delta_hours:.1f} hours.",
        )
    elif deadline_delta_hours > 24:
        advise(
            "resolution_deadline_offset",
            f"Resolution deadlines differ by {deadline_delta_hours:.1f} hours.",
        )
    if not rules_present or rules_score < 0.75:
        block(
            "resolution_rules_unverified",
            "Resolution rules are missing or too different for deterministic equivalence.",
        )
    if not sources_present or source_score < 0.75:
        block(
            "resolution_source_unverified",
            "Resolution sources are missing or too different for deterministic equivalence.",
        )

    if terms_left["market_kind"] != terms_right["market_kind"]:
        block(
            "contract_kind_differs",
            f"Contract kinds differ: {terms_left['market_kind']} vs {terms_right['market_kind']}.",
        )
    if (
        terms_left["date_scope"] != terms_right["date_scope"]
        and "single_calendar_date" in {terms_left["date_scope"], terms_right["date_scope"]}
    ):
        block(
            "event_date_semantics_differ",
            "One contract is tied to a specific calendar date while the other is not.",
        )
    years_left = set(terms_left["years"])
    years_right = set(terms_right["years"])
    if years_left and years_right and years_left.isdisjoint(years_right):
        block(
            "contract_year_differs",
            f"Contract years differ: {', '.join(sorted(years_left))} vs {', '.join(sorted(years_right))}.",
        )
    threshold_left = terms_left.get("threshold")
    threshold_right = terms_right.get("threshold")
    if threshold_left and threshold_right and threshold_left != threshold_right:
        block(
            "threshold_terms_differ",
            "Threshold contracts have different assets, dates, or threshold values.",
        )
    elif bool(threshold_left) != bool(threshold_right):
        block(
            "threshold_terms_unpaired",
            "Only one side parsed as a threshold contract.",
        )

    return {
        "contract_terms_verified": not blocking_risk_flags,
        "risk_flags": list(dict.fromkeys([*blocking_risk_flags, *advisory_risk_flags])),
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "blocking_risk_flags": list(dict.fromkeys(blocking_risk_flags)),
        "advisory_reasons": list(dict.fromkeys(advisory_reasons)),
        "advisory_risk_flags": list(dict.fromkeys(advisory_risk_flags)),
        "contract_terms": {
            "left": terms_left,
            "right": terms_right,
        },
    }


def contract_terms(market: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        str(market.get(key) or "")
        for key in (
            "event_title",
            "question",
            "outcome_label",
            "resolution_rules",
        )
    )
    lowered = text.lower()
    threshold = threshold_descriptor(market)
    explicit_iso_dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", lowered)
    month_dates = re.findall(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
        lowered,
    )
    single_calendar_date = bool(
        explicit_iso_dates
        or re.search(r"\bon\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b", lowered)
    )
    tournament = bool(
        re.search(r"\b(world cup|championship|champion|tournament winner|overall winner)\b", lowered)
    )
    single_match = bool(re.search(r"\b(match|game|round of|qualification round)\b", lowered))
    if threshold:
        market_kind = "threshold"
    elif tournament and not single_match and not single_calendar_date:
        market_kind = "tournament_winner"
    elif single_match:
        market_kind = "single_match"
    elif single_calendar_date:
        market_kind = "date_binary"
    else:
        market_kind = "binary"
    return {
        "market_kind": market_kind,
        "date_scope": "single_calendar_date" if single_calendar_date else "event_resolution",
        "explicit_dates": sorted(set([*explicit_iso_dates, *month_dates])),
        "years": sorted(set(re.findall(r"\b20\d{2}\b", lowered))),
        "threshold": threshold,
    }


def threshold_descriptor(market: dict[str, Any]) -> tuple[str, str, float] | None:
    terms = threshold_terms(market)
    if not terms:
        return None
    return terms["asset"], terms["date_key"], float(terms["threshold"])


def threshold_terms(market: dict[str, Any]) -> dict[str, Any] | None:
    text = f"{market.get('event_title', '')} {market.get('question', '')}".lower()
    aliases = {
        "bitcoin": ("bitcoin", "btc"),
        "ethereum": ("ethereum", "ether", "eth"),
        "solana": ("solana", "sol"),
    }
    asset = next(
        (name for name, words in aliases.items() if any(re.search(rf"\b{word}\b", text) for word in words)),
        None,
    )
    if not asset:
        return None
    if re.search(
        (
            r"\b(below|under|less than|lower than|at most|"
            r"dip|dips|dipped|drop|drops|dropped|fall|falls|fell|"
            r"crash|crashes|low|lower)\b"
        ),
        text,
    ):
        return None
    if not re.search(
        r"\b(above|over|exceed|exceeds|exceeding|higher than|at least|hit|hits|reach|reaches|touch|touches|breach|breaches)\b",
        text,
    ):
        return None
    amounts = re.findall(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*([km]?)\b", text)
    if not amounts:
        amounts = re.findall(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*([km])\b", text)
    if not amounts:
        return None
    raw_number, suffix = amounts[-1]
    value = float(raw_number.replace(",", ""))
    value *= {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
    close = str(market.get("closes_at") or market.get("expected_resolution_at") or "unknown")
    if close == "unknown" or len(close) < 10:
        return None
    if re.search(r"\b(hit|hits|reach|reaches|touch|touches|breach|breaches)\b", text):
        condition_family = "hit_by"
    else:
        condition_family = "above_at_resolution"
    return {
        "asset": asset,
        "date_key": close[:10],
        "threshold": value,
        "direction": "above",
        "condition_family": condition_family,
    }


def route_candidate_sizes(
    legs: list[dict[str, Any]],
    minimum_size: float,
    capacity: float,
) -> list[float]:
    if capacity + 1e-9 < minimum_size or capacity <= 0:
        return []
    candidates = {minimum_size, capacity}
    for leg in legs:
        cumulative = 0.0
        for _, size in leg["levels"]:
            cumulative += size
            if minimum_size <= cumulative <= capacity:
                candidates.add(cumulative)
    ordered = sorted(candidates)
    if len(ordered) <= 240:
        return ordered
    indices = {round(index * (len(ordered) - 1) / 239) for index in range(240)}
    return [ordered[index] for index in sorted(indices)]


def walk_levels(levels: list[list[float]], size: float) -> dict[str, Any] | None:
    remaining = size
    notional = 0.0
    fills = []
    for price, available in levels:
        fill = min(remaining, available)
        if fill <= 0:
            continue
        fills.append([price, fill])
        notional += price * fill
        remaining -= fill
        if remaining <= 1e-8:
            break
    if remaining > 1e-8:
        return None
    return {
        "notional": notional,
        "vwap": notional / size,
        "fills": fills,
    }


def venue_fee(venue: str, fills: list[list[float]], fee_rate: float) -> float:
    if venue == "hyperliquid_hip4":
        return round(
            max(0.0, sum(size * price * fee_rate for price, size in fills)),
            5,
        )
    fee = sum(size * fee_rate * price * (1.0 - price) for price, size in fills)
    if venue == "kalshi":
        trade_fee = math.ceil(max(0.0, fee) * 10_000 - 1e-12) / 10_000
        return trade_fee + 0.01
    return round(max(0.0, fee), 5)


def leg_capacity(leg: dict[str, Any]) -> float:
    return sum(size for _, size in leg.get("levels", []))


def group_markets(markets: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in markets:
        grouped.setdefault((market["venue"], market["event_id"]), []).append(market)
    return grouped


def stable_route_key(route_type: str, legs: list[dict[str, Any]]) -> str:
    identity = "|".join(
        f"{leg['venue']}:{leg['market_id']}:{leg['action']}:{leg['outcome']}"
        for leg in legs
    )
    digest = hashlib.sha256(f"{route_type}|{identity}".encode("utf-8")).hexdigest()[:20]
    return f"{route_type}:{digest}"


def serializable_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **{key: value for key, value in leg.items() if key != "levels"},
            "levels": leg.get("levels", [])[:100],
        }
        for leg in legs
    ]


def empty_evaluation() -> dict[str, Any]:
    return {
        "size": 0.0,
        "gross_profit": None,
        "net_profit": None,
        "gross_edge_per_share": None,
        "net_edge_per_share": None,
        "capital_required": None,
        "fees": 0.0,
        "slippage": 0.0,
        "operations_buffer": 0.0,
        "legs": [],
    }


def canonical_event_text(market: dict[str, Any]) -> str:
    label = str(market.get("outcome_label") or "")
    if label.lower() in {"yes", "no"}:
        return str(market.get("question") or market.get("event_title") or "")
    return f"{market.get('event_title', '')} {label}"


def canonical_tokens(value: str) -> set[str]:
    stopwords = {
        "will", "the", "a", "an", "in", "on", "of", "to", "be", "by",
        "yes", "no", "fifa", "mens", "men", "market", "2024", "2025",
        "2026", "2027", "2028",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower().replace("'", ""))
        if token not in stopwords
    }


def text_similarity(left: str, right: str) -> float:
    if normalized_text(left) and normalized_text(left) == normalized_text(right):
        return 1.0
    left_tokens = canonical_tokens(left)
    right_tokens = canonical_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(
        None,
        " ".join(sorted(left_tokens)),
        " ".join(sorted(right_tokens)),
    ).ratio()
    return max(jaccard, sequence * 0.9)


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def outcome_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    label_left = str(left.get("outcome_label") or "yes").lower().strip()
    label_right = str(right.get("outcome_label") or "yes").lower().strip()
    if label_left == label_right:
        return 1.0
    return text_similarity(label_left, label_right)


def cancellation_rules_match(left: Any, right: Any) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text and not right_text:
        return False
    if not left_text or not right_text:
        return False
    return text_similarity(left_text, right_text) >= 0.45


def timestamp_delta_hours(left: Any, right: Any) -> float | None:
    left_dt = parse_timestamp(left)
    right_dt = parse_timestamp(right)
    if not left_dt or not right_dt:
        return None
    return abs((left_dt - right_dt).total_seconds()) / 3600.0


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def capital_lock_days(observed_at: str, locked_until: str | None) -> float | None:
    start = parse_timestamp(observed_at)
    end = parse_timestamp(locked_until)
    if not start or not end:
        return None
    return max(0.0, (end - start).total_seconds() / 86_400.0)


def max_timestamp(left: Any, right: Any) -> str | None:
    left_dt = parse_timestamp(left)
    right_dt = parse_timestamp(right)
    if left_dt and right_dt:
        return max(left_dt, right_dt).isoformat()
    if left_dt:
        return left_dt.isoformat()
    if right_dt:
        return right_dt.isoformat()
    return None
