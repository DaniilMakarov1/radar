from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.prediction.scanner import ScannerConfig, evaluate_size, leg_capacity


PAPER_MODEL_VERSION = "prediction_paper_v2_future_snapshot"


@dataclass(frozen=True)
class PaperConfig:
    target_size: float = 100.0
    minimum_latency_ms: int = 750
    depth_haircut: float = 0.8
    operations_buffer_rate: float = 0.001


def simulate_paper_routes(
    routes: list[dict[str, Any]],
    current_books: list[dict[str, Any]],
    execution_observed_at: str,
    config: PaperConfig | None = None,
) -> list[dict[str, Any]]:
    settings = config or PaperConfig()
    book_map = {
        (str(book["venue"]), str(book["market_id"])): book
        for book in current_books
    }
    return [
        simulate_paper_route(
            route,
            book_map,
            execution_observed_at,
            settings,
        )
        for route in routes
        if route.get("status") == "paper_candidate"
        and route.get("prediction_route_id")
    ]


def simulate_paper_route(
    route: dict[str, Any],
    current_books: dict[tuple[str, str], dict[str, Any]],
    execution_observed_at: str,
    config: PaperConfig,
) -> dict[str, Any]:
    quote_observed_at = str(route.get("observed_at") or "")
    actual_latency_ms = elapsed_milliseconds(
        quote_observed_at,
        execution_observed_at,
    )
    requested = min(
        max(0.0, config.target_size),
        max(0.0, float(route.get("optimal_size") or 0)),
    )
    adjusted_legs = []
    missing_legs = []
    for leg in route.get("legs", []):
        book = current_books.get((str(leg["venue"]), str(leg["market_id"])))
        if not book or parse_time(str(book.get("observed_at") or "")) <= parse_time(
            quote_observed_at
        ):
            missing_legs.append(f"{leg['venue']}:{leg['market_id']}")
            continue
        adjusted = future_snapshot_leg(leg, book, config.depth_haircut)
        if not adjusted.get("levels"):
            missing_legs.append(f"{leg['venue']}:{leg['market_id']}")
            continue
        adjusted_legs.append(adjusted)

    minimum_size = max(
        (float(leg.get("min_order_size") or 1.0) for leg in adjusted_legs),
        default=1.0,
    )
    available = min((leg_capacity(leg) for leg in adjusted_legs), default=0.0)
    filled = min(requested, available)
    latency_valid = actual_latency_ms >= config.minimum_latency_ms
    all_legs_future = len(adjusted_legs) == len(route.get("legs", []))
    if (
        not latency_valid
        or not all_legs_future
        or filled + 1e-9 < minimum_size
        or not adjusted_legs
    ):
        evaluation = None
        status = "rejected"
        filled = 0.0
    else:
        evaluation = evaluate_size(
            adjusted_legs,
            filled,
            float(route.get("guaranteed_payout_per_share") or 0.0),
            ScannerConfig(operations_buffer_rate=config.operations_buffer_rate),
        )
        profitable = bool(evaluation and evaluation.get("net_profit", 0) > 0)
        complete = filled + 1e-9 >= requested
        status = (
            "filled_profitable"
            if complete and profitable
            else "filled_unprofitable"
            if complete
            else "partial_profitable"
            if profitable
            else "partial_unprofitable"
        )
    fill_ratio = filled / requested if requested > 0 else 0.0
    result = evaluation or {
        "net_profit": None,
        "fees": 0.0,
        "slippage": 0.0,
        "capital_required": None,
        "legs": [],
    }
    return {
        "prediction_route_id": route["prediction_route_id"],
        "model_version": PAPER_MODEL_VERSION,
        "status": status,
        "requested_size": requested,
        "filled_size": filled,
        "fill_ratio": fill_ratio,
        "latency_ms": actual_latency_ms,
        "depth_haircut": config.depth_haircut,
        "queue_ahead_size": 0.0,
        "expected_net_profit": route.get("expected_net_profit"),
        "simulated_net_profit": result["net_profit"],
        "simulated_fees": result["fees"],
        "simulated_slippage": result["slippage"],
        "capital_required": result["capital_required"],
        "legs": result["legs"],
        "result": {
            "profitable_after_latency": bool(
                result["net_profit"] is not None and result["net_profit"] > 0
            ),
            "quote_observed_at": quote_observed_at,
            "execution_observed_at": execution_observed_at,
            "actual_latency_ms": actual_latency_ms,
            "minimum_latency_ms": config.minimum_latency_ms,
            "latency_valid": latency_valid,
            "all_legs_from_future_snapshot": all_legs_future,
            "missing_legs": missing_legs,
            "deterministic": True,
            "queue_model": "taker_only_no_queue_claim",
            "execution_atomic": False,
            "research_only": True,
        },
    }


def future_snapshot_leg(
    leg: dict[str, Any],
    book: dict[str, Any],
    depth_haircut: float,
) -> dict[str, Any]:
    levels_key = f"{leg['outcome']}_{'asks' if leg['action'] == 'buy' else 'bids'}"
    haircut = max(0.0, min(1.0, depth_haircut))
    levels = [
        [float(price), float(size) * haircut]
        for price, size in book.get(levels_key, [])
        if float(size) * haircut > 0
    ]
    return {
        **leg,
        "levels": levels,
        "future_book_observed_at": book.get("observed_at"),
        "queue_ahead_size": 0.0,
    }


def elapsed_milliseconds(start: str, end: str) -> int:
    return max(0, round((parse_time(end) - parse_time(start)).total_seconds() * 1_000))


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
