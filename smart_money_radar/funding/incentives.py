from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class IncentiveProgramMetadata:
    status: str = "UNKNOWN"
    source: str | None = None
    program_name: str | None = None
    season: str | None = None
    multiplier: float | None = None
    eligibility: str | None = None
    checked_at: str | None = None
    confidence: str = "LOW"
    notes: str | None = None
    incentive_program_status: str = "UNKNOWN"
    incentive_program_name: str | None = None
    season_or_epoch: str | None = None
    public_source: str | None = None
    api_trading_eligible: bool | None = None
    api_volume_multiplier: float | None = None
    known_exclusions: tuple[str, ...] = ()
    last_verified_at: str | None = None
    volume_generated_estimate: float | None = None
    estimated_fees: float | None = None
    points_observed: float | None = None
    points_per_1000_volume: float | None = None
    cost_per_point: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


UNKNOWN_INCENTIVES = IncentiveProgramMetadata()


def funding_net_excluding_points(opportunity: dict[str, Any]) -> float:
    for key in (
        "funding_net_excluding_points",
        "funding_net_after_stablecoin_reserve",
        "funding_net_before_stablecoin_reserve",
        "preliminary_gross_funding",
    ):
        try:
            value = float(opportunity.get(key))
        except (TypeError, ValueError):
            continue
        return value
    return 0.0


def opportunity_sort_key_with_points(opportunity: dict[str, Any]) -> tuple[bool, float, float]:
    trading_net = funding_net_excluding_points(opportunity)
    metadata = opportunity.get("points_metadata") or {}
    try:
        points_score = float(metadata.get("points_per_1000_volume") or 0.0)
    except (TypeError, ValueError):
        points_score = 0.0
    return (trading_net > 0.0, trading_net, points_score if trading_net > 0.0 else 0.0)


def rank_opportunities_with_points_tiebreaker(
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(opportunities, key=opportunity_sort_key_with_points, reverse=True)
