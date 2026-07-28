from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES

PAPER_COLLATERAL_ASSETS = {"USDT", "USDC", "USD"}
NEXT_SETTLEMENT_RATE_KINDS = {
    "published_next_estimate",
    "published_current_estimate",
    "published_current",
    "published_current_interval_rate",
    "published_next_hour",
    "published_next_hour_prediction",
    "published_current_hour_estimate",
    "published_projected_1h",
}
CONTINUOUS_OR_UNCLEAR_RATE_KINDS = {
    "published_continuous_hourly_equivalent",
    "published_last_settlement",
    "published_latest_hour_x18",
    "published_velocity_hourly_side_cashflow",
}


@dataclass(frozen=True)
class VenueCapability:
    venue: str
    supports_perpetuals: bool = True
    is_linear_contract: bool = True
    collateral_asset: str | None = None
    quote_asset: str | None = None
    supports_discrete_funding: bool = True
    funding_rate_semantics: str = "next_settlement"
    supports_next_funding_timestamp: bool = True
    supports_mark_price: bool = True
    supports_index_price: bool = True
    supports_orderbook_timestamp: bool = True
    supports_orderbook_depth: bool = True
    supports_24h_quote_volume: bool = True
    supports_open_interest: bool = True
    supports_taker_fee: bool = True
    supports_quantity_step: bool = False
    supports_min_notional: bool = False
    supports_risk_tiers: bool = False
    supports_funding_history: bool = True
    supports_server_time: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capability_from_market(market: dict[str, Any]) -> VenueCapability:
    venue = str(market.get("venue") or "").lower()
    funding_kind = str(market.get("funding_rate_kind") or "")
    collateral = normalized_asset(market.get("collateral_asset"))
    quote = normalized_asset(market.get("quote_asset"))
    semantics = "next_settlement" if funding_kind in NEXT_SETTLEMENT_RATE_KINDS else "unclear"
    discrete = funding_kind not in CONTINUOUS_OR_UNCLEAR_RATE_KINDS
    return VenueCapability(
        venue=venue,
        collateral_asset=collateral,
        quote_asset=quote,
        supports_discrete_funding=discrete,
        funding_rate_semantics=semantics,
        supports_next_funding_timestamp=bool(market.get("next_funding_at")),
        supports_mark_price=positive(market.get("mark_price")),
        supports_index_price=positive(market.get("index_price")),
        supports_24h_quote_volume=positive(market.get("volume_24h_usd")),
        supports_open_interest=positive(market.get("open_interest_usd")),
        supports_taker_fee=market.get("taker_fee_rate") is not None,
        supports_quantity_step=positive(market.get("quantity_step")) or positive(
            market.get("contract_multiplier")
        ),
        supports_min_notional=positive(market.get("min_notional_usd")),
        reason=f"funding_rate_kind={funding_kind or 'missing'}",
    )


def normalized_asset(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def synchronized_capability_rejection(capability: VenueCapability) -> list[str]:
    reasons: list[str] = []
    if not capability.supports_perpetuals:
        reasons.append("not_perpetual")
    if not capability.is_linear_contract:
        reasons.append("not_linear_contract")
    if capability.collateral_asset not in PAPER_COLLATERAL_ASSETS:
        reasons.append("unsupported_collateral_asset")
    if not capability.supports_discrete_funding:
        reasons.append("continuous_or_unclear_funding")
    if capability.funding_rate_semantics != "next_settlement":
        reasons.append("funding_semantics_not_next_settlement")
    required_flags = {
        "supports_next_funding_timestamp": capability.supports_next_funding_timestamp,
        "supports_mark_price": capability.supports_mark_price,
        "supports_index_price": capability.supports_index_price,
        "supports_orderbook_timestamp": capability.supports_orderbook_timestamp,
        "supports_orderbook_depth": capability.supports_orderbook_depth,
        "supports_24h_quote_volume": capability.supports_24h_quote_volume,
        "supports_open_interest": capability.supports_open_interest,
        "supports_taker_fee": capability.supports_taker_fee,
        "supports_quantity_step": capability.supports_quantity_step,
        "supports_min_notional": capability.supports_min_notional,
    }
    for name, passed in required_flags.items():
        if not passed:
            reasons.append(name.removeprefix("supports_") + "_missing")
    return reasons


def synchronized_paper_eligible(capability: VenueCapability) -> bool:
    return not synchronized_capability_rejection(capability)


def venue_inventory_rows(
    *,
    registered_venues: list[str],
    active_venues: list[str],
    sample_markets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    sample_by_venue: dict[str, dict[str, Any]] = {}
    for market in sample_markets or []:
        sample_by_venue.setdefault(str(market.get("venue") or "").lower(), market)
    rows: list[dict[str, Any]] = []
    active = {venue.lower() for venue in active_venues}
    for venue in sorted({venue.lower() for venue in registered_venues}):
        if venue in DEACTIVATED_FUNDING_VENUES:
            rows.append({"venue": venue, "status": "DEACTIVATED", "reason": "user_deactivated"})
            continue
        if venue not in active:
            rows.append({"venue": venue, "status": "UNAVAILABLE", "reason": "no_current_market_data"})
            continue
        market = sample_by_venue.get(venue)
        if not market:
            rows.append({"venue": venue, "status": "REGISTERED", "reason": "no_sample_market"})
            continue
        capability = capability_from_market(market)
        rejections = synchronized_capability_rejection(capability)
        if rejections:
            rows.append(
                {
                    "venue": venue,
                    "status": "ACTIVE_RESEARCH_ONLY",
                    "reason": ",".join(rejections),
                    "capability": capability.as_dict(),
                }
            )
        else:
            rows.append(
                {
                    "venue": venue,
                    "status": "ACTIVE_PAPER_ELIGIBLE",
                    "reason": "all_synchronized_funding_gates_passed",
                    "capability": capability.as_dict(),
                }
            )
    return rows
