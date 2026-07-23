from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


PAPER_MODEL_VERSION = "funding_paper_v7_live_settlement_decision"


def simulate_paper_revalidations(
    previous_routes: list[dict[str, Any]],
    current_routes: list[dict[str, Any]],
    latency_ms: int,
) -> list[dict[str, Any]]:
    current_by_key = {row["route_key"]: row for row in current_routes}
    output = []
    for previous in previous_routes:
        current = current_by_key.get(previous["route_key"])
        requested = float(previous.get("target_notional") or 0)
        # Never extrapolate a route beyond the notional actually repriced through
        # the current books. Market capacity alone is not an execution price.
        available = float(current.get("target_notional") or 0) if current else 0.0
        filled = min(requested, available)
        fill_ratio = filled / requested if requested > 0 else 0.0
        current_target = available
        current_net = decision_net_profit(current) if current else 0.0
        previous_net = decision_net_profit(previous)
        repriced_net = current_net * filled / current_target if current_target > 0 else None
        actual_latency_ms = snapshot_latency_ms(previous, current, latency_ms)
        crossed_settlements = settlements_crossed(previous, current)
        reauthorization_required = bool(crossed_settlements)
        if not current:
            status = "route_disappeared"
        elif reauthorization_required:
            status = (
                "settlement_reauthorized"
                if fill_ratio >= 0.99
                and current.get("status") == "paper_candidate"
                and current_net > 0
                else "settlement_exit_required"
            )
        elif fill_ratio < 0.99:
            status = "partial_revalidation"
        elif current.get("status") == "paper_candidate" and current_net > 0:
            status = "revalidated_profitable"
        else:
            status = "revalidated_not_profitable"
        output.append(
            {
                "funding_route_id": (
                    current.get("funding_route_id") if current else None
                )
                or previous["funding_route_id"],
                "model_version": PAPER_MODEL_VERSION,
                "status": status,
                "requested_notional": requested,
                "filled_notional": filled,
                "fill_ratio": fill_ratio,
                "latency_ms": actual_latency_ms,
                "expected_net_profit": previous_net,
                "repriced_net_profit": repriced_net,
                "result": {
                    "route_key": previous["route_key"],
                    "previous_observed_at": previous.get("observed_at"),
                    "current_observed_at": current.get("observed_at") if current else None,
                    "current_status": current.get("status") if current else None,
                    "decision_mode": decision_mode(current),
                    "configured_minimum_latency_ms": latency_ms,
                    "settlements_crossed": crossed_settlements,
                    "reauthorization_required": reauthorization_required,
                    "authorization_valid_until": authorization_valid_until(current),
                },
            }
        )
    return output


def decision_mode(route: dict[str, Any] | None) -> str:
    evidence = (route or {}).get("evidence") or {}
    return str(evidence.get("decision_mode") or "persistent_carry")


def decision_net_profit(route: dict[str, Any]) -> float:
    evidence = route.get("evidence") or {}
    if decision_mode(route) == "settlement_capture":
        return float(evidence.get("current_nowcast_net") or 0.0)
    return float(route.get("expected_net_profit") or 0.0)


def snapshot_latency_ms(
    previous: dict[str, Any],
    current: dict[str, Any] | None,
    fallback: int,
) -> int:
    if not current:
        return max(0, int(fallback))
    try:
        start = parse_iso(previous.get("observed_at"))
        end = parse_iso(current.get("observed_at"))
    except (TypeError, ValueError):
        return max(0, int(fallback))
    elapsed = int((end - start).total_seconds() * 1_000)
    return elapsed if elapsed > 0 else max(0, int(fallback))


def parse_iso(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def settlements_crossed(
    previous: dict[str, Any],
    current: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not current:
        return []
    try:
        current_at = parse_iso(current.get("observed_at"))
    except (TypeError, ValueError):
        return []
    crossed = []
    for leg in previous.get("legs") or []:
        try:
            settlement_at = parse_iso(leg.get("next_funding_at"))
        except (TypeError, ValueError):
            continue
        if settlement_at <= current_at:
            crossed.append(
                {
                    "venue": str(leg.get("venue") or ""),
                    "symbol": str(leg.get("symbol") or ""),
                    "settlement_at": settlement_at.isoformat(),
                }
            )
    return crossed


def authorization_valid_until(route: dict[str, Any] | None) -> str | None:
    if not route:
        return None
    evidence = route.get("evidence") or {}
    horizon = evidence.get("horizon") or {}
    value = horizon.get("authorization_valid_until")
    return str(value) if value else None
