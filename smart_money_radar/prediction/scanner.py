from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    settings = config or ScannerConfig()
    book_map = {(book["venue"], book["market_id"]): book for book in books}
    event_map = {(event["venue"], event["event_id"]): event for event in events}
    constraints = infer_implication_constraints(markets, observed_at)
    matches = match_cross_venue_contracts(markets, observed_at)

    routes: list[dict[str, Any]] = []
    routes.extend(scan_binary_complements(markets, book_map, observed_at, settings))
    routes.extend(
        scan_complete_sets(event_map, markets, book_map, observed_at, settings)
    )
    routes.extend(
        scan_negative_risk(event_map, markets, book_map, observed_at, settings)
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
        legs = []
        incomplete = False
        for market in unresolved:
            book = books.get((market["venue"], market["market_id"]))
            if not book or not book.get("yes_asks"):
                incomplete = True
                break
            legs.append(market_leg(market, book, "buy", "yes"))
        if incomplete:
            continue
        routes.append(
            evaluate_route(
                route_type="complete_set",
                title=event["title"],
                venue_scope=event["venue"],
                event_id=event["event_id"],
                legs=legs,
                observed_at=observed_at,
                config=config,
                payout_per_share=1.0,
                locked_until=event.get("expected_resolution_at")
                or event.get("closes_at"),
                confidence_score=98.0,
                rationale=[
                    "The normalized event is mutually exclusive and exhaustive.",
                    f"The route buys all {len(legs)} unresolved YES outcomes.",
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
            or not match["cancellation_match"]
            or not bool(
                (match.get("evidence") or {}).get("contract_terms_verified")
            )
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
                    venue_scope="polymarket+kalshi",
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
) -> dict[str, Any]:
    fee_model_verified = all(bool(leg.get("fee_verified")) for leg in legs)
    if not fee_model_verified:
        critical_review = True
        risk_flags = [*risk_flags, "fee_schedule_unverified"]
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
    operations_buffer = (
        config.operations_buffer_rate * max(capital, size)
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
        "tick_size": market.get("tick_size", 0.01),
        "min_order_size": market.get("min_order_size", 1.0),
    }


def infer_implication_constraints(
    markets: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[dict[str, Any], float]]] = {}
    for market in markets:
        if market.get("status") != "open":
            continue
        descriptor = threshold_descriptor(market)
        if not descriptor:
            continue
        asset, date_key, threshold = descriptor
        grouped.setdefault((market["venue"], asset, date_key), []).append(
            (market, threshold)
        )
    constraints = []
    for (venue, asset, date_key), rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row[1])
        for index, (lower_market, lower_value) in enumerate(ordered[:-1]):
            for higher_market, higher_value in ordered[index + 1 :]:
                constraints.append(
                    {
                        "venue": venue,
                        "antecedent_market_id": higher_market["market_id"],
                        "consequent_market_id": lower_market["market_id"],
                        "relation_type": "threshold_implication",
                        "confidence_score": 0.75,
                        "source": "deterministic_threshold_parser",
                        "rationale": [
                            f"{asset} above {higher_value:g} implies {asset} above {lower_value:g} on {date_key}."
                        ],
                        "evidence": {
                            "asset": asset,
                            "date_key": date_key,
                            "higher_threshold": higher_value,
                            "lower_threshold": lower_value,
                        },
                        "created_at": observed_at,
                    }
                )
    return constraints


def match_cross_venue_contracts(
    markets: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    polymarket = [row for row in markets if row["venue"] == "polymarket" and row["status"] == "open"]
    kalshi = [row for row in markets if row["venue"] == "kalshi" and row["status"] == "open"]
    candidates = []
    for market_a in polymarket:
        for market_b in kalshi:
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
            rules_present = bool(
                str(market_a.get("resolution_rules") or "").strip()
                and str(market_b.get("resolution_rules") or "").strip()
            )
            sources_present = bool(
                str(market_a.get("resolution_source") or "").strip()
                and str(market_b.get("resolution_source") or "").strip()
            )
            if not cancellation_match:
                risk_flags.append("cancellation_rules_differ")
            if deadline_delta is None or deadline_delta > 72:
                risk_flags.append("resolution_deadline_differs")
            if not rules_present or rules_score < 0.75:
                risk_flags.append("resolution_rules_unverified")
            if not sources_present or source_score < 0.75:
                risk_flags.append("resolution_source_unverified")
            contract_terms_verified = bool(
                cancellation_match
                and rules_present
                and rules_score >= 0.75
                and sources_present
                and source_score >= 0.75
                and deadline_delta is not None
                and deadline_delta <= 24
            )
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
                    },
                    "created_at": observed_at,
                }
            )
    candidates.sort(key=lambda row: row["match_score"], reverse=True)
    selected = []
    used_a: set[str] = set()
    used_b: set[str] = set()
    for candidate in candidates:
        if candidate["match_score"] < 0.76:
            break
        if candidate["market_id_a"] in used_a or candidate["market_id_b"] in used_b:
            continue
        used_a.add(candidate["market_id_a"])
        used_b.add(candidate["market_id_b"])
        selected.append(candidate)
    return selected


def threshold_descriptor(market: dict[str, Any]) -> tuple[str, str, float] | None:
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
    if not asset or not re.search(r"\b(above|over|exceed|higher than|at least)\b", text):
        return None
    amounts = re.findall(r"\$([0-9]+(?:[,.][0-9]+)?)\s*([km]?)\b", text)
    if not amounts:
        amounts = re.findall(r"\b([0-9]+(?:[,.][0-9]+)?)\s*([km])\b", text)
    if not amounts:
        return None
    raw_number, suffix = amounts[-1]
    value = float(raw_number.replace(",", ""))
    value *= {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
    close = str(market.get("closes_at") or market.get("expected_resolution_at") or "unknown")
    return asset, close[:10], value


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
