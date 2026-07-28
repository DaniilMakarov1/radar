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
VALID_FUNDING_RATE_UNITS = {
    "fraction_of_notional_per_settlement",
    "fraction_of_notional_per_hour",
}
VALID_FUNDING_SIGN_CONVENTIONS = {
    "positive_long_pays",
    "positive_short_pays",
}
VALID_CONTRACT_KINDS = {
    "linear_perpetual",
    "inverse_perpetual",
    "quanto_perpetual",
}


@dataclass(frozen=True)
class VenueCapability:
    venue: str
    supports_perpetuals: bool = False
    is_linear_contract: bool = False
    contract_kind: str | None = None
    collateral_asset: str | None = None
    quote_asset: str | None = None
    supports_discrete_funding: bool = False
    funding_rate_semantics: str = "unclear"
    funding_rate_unit: str | None = None
    funding_sign_convention: str | None = None
    supports_next_funding_timestamp: bool = False
    supports_mark_price: bool = False
    supports_index_price: bool = False
    supports_orderbook_timestamp: bool = False
    supports_orderbook_depth: bool = False
    supports_24h_quote_volume: bool = False
    supports_open_interest: bool = False
    supports_taker_fee: bool = False
    supports_quantity_step: bool = False
    supports_min_notional: bool = False
    supports_risk_tiers: bool = False
    supports_funding_history: bool = False
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
    contract_type = str(market.get("contract_type") or "").strip().lower()
    contract_kind = str(market.get("contract_kind") or "").strip() or None
    funding_rate_unit = str(market.get("funding_rate_unit") or "").strip() or None
    funding_sign_convention = str(market.get("funding_sign_convention") or "").strip() or None
    if contract_kind is None and contract_type == "linear_perpetual":
        contract_kind = "linear_perpetual"
    if contract_kind is None and bool(market.get("is_linear_contract")):
        contract_kind = "linear_perpetual"
    if funding_rate_unit is None and semantics == "next_settlement":
        funding_rate_unit = "fraction_of_notional_per_settlement"
    if funding_sign_convention is None and semantics == "next_settlement":
        funding_sign_convention = "positive_long_pays"
    supports_perpetuals = (
        "perpetual" in contract_type
        or (contract_kind is not None and "perpetual" in contract_kind)
    )
    is_linear = (
        bool(market.get("is_linear_contract"))
        or contract_kind == "linear_perpetual"
        or (collateral is not None and collateral == quote)
    )
    return VenueCapability(
        venue=venue,
        supports_perpetuals=supports_perpetuals,
        is_linear_contract=is_linear,
        contract_kind=contract_kind,
        collateral_asset=collateral,
        quote_asset=quote,
        supports_discrete_funding=discrete,
        funding_rate_semantics=semantics,
        funding_rate_unit=funding_rate_unit,
        funding_sign_convention=funding_sign_convention,
        supports_next_funding_timestamp=bool(market.get("next_funding_at")),
        supports_mark_price=positive(market.get("mark_price")),
        supports_index_price=positive(market.get("index_price")),
        supports_orderbook_timestamp=bool(
            market.get("orderbook_response_received_at")
            or market.get("orderbook_event_time")
        ),
        supports_orderbook_depth=bool(market.get("orderbook_depth_available")),
        supports_24h_quote_volume=positive(market.get("volume_24h_usd")),
        supports_open_interest=positive(market.get("open_interest_usd")),
        supports_taker_fee=market.get("taker_fee_rate") is not None,
        supports_quantity_step=positive(market.get("quantity_step")) or positive(
            market.get("contract_multiplier")
        ),
        supports_min_notional=positive(market.get("min_notional_usd")),
        supports_funding_history=bool(market.get("funding_history_available", False)),
        supports_server_time=bool(market.get("server_time")),
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
    if capability.contract_kind != "linear_perpetual":
        reasons.append("contract_kind_not_linear_perpetual")
    if capability.collateral_asset not in PAPER_COLLATERAL_ASSETS:
        reasons.append("unsupported_collateral_asset")
    if not capability.supports_discrete_funding:
        reasons.append("continuous_or_unclear_funding")
    if capability.funding_rate_semantics != "next_settlement":
        reasons.append("funding_semantics_not_next_settlement")
    if capability.funding_rate_unit != "fraction_of_notional_per_settlement":
        reasons.append("funding_rate_unit_not_fraction_per_settlement")
    if capability.funding_sign_convention != "positive_long_pays":
        reasons.append("funding_sign_convention_not_positive_long_pays")
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


def synchronized_route_capability_check(
    long_capability: VenueCapability,
    short_capability: VenueCapability,
) -> dict[str, Any]:
    long_rejections = synchronized_capability_rejection(long_capability)
    short_rejections = synchronized_capability_rejection(short_capability)
    cross_venue_reasons: list[str] = []
    if long_capability.collateral_asset != short_capability.collateral_asset:
        cross_venue_reasons.append("collateral_asset_mismatch")
    if long_capability.quote_asset != short_capability.quote_asset:
        cross_venue_reasons.append("quote_asset_mismatch")
    if long_capability.collateral_asset not in PAPER_COLLATERAL_ASSETS:
        cross_venue_reasons.append("long_collateral_not_paper_eligible")
    if short_capability.collateral_asset not in PAPER_COLLATERAL_ASSETS:
        cross_venue_reasons.append("short_collateral_not_paper_eligible")
    all_reasons = list(dict.fromkeys(
        [f"long_{r}" for r in long_rejections]
        + [f"short_{r}" for r in short_rejections]
        + cross_venue_reasons
    ))
    return {
        "paper_eligible": not all_reasons,
        "long_rejections": long_rejections,
        "short_rejections": short_rejections,
        "cross_venue_reasons": cross_venue_reasons,
        "all_reasons": all_reasons,
    }


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
