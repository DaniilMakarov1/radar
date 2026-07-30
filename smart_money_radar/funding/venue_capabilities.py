from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from smart_money_radar.funding.adapter_contracts import USD_MAJOR_STABLE, collateral_family
from smart_money_radar.funding.fees import fee_evidence_status
from smart_money_radar.funding.settlement_contracts import (
    settlement_contract_blockers,
    settlement_contract_from_market,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES

PAPER_COLLATERAL_ASSETS = {"USDT", "USDC", "USD"}
DECLARED_NEXT_SETTLEMENT_VENUES = {
    "aevo": {"timing_policy_source": "adapter_aevo_next_hour"},
    "apex": {"timing_policy_source": "adapter_apex_hourly"},
    "aster": {"timing_policy_source": "adapter_aster_next_funding_time"},
    "backpack": {"timing_policy_source": "adapter_backpack_next_funding_timestamp"},
    "binance": {"timing_policy_source": "adapter_binance_next_funding_time"},
    "bitget": {"timing_policy_source": "adapter_bitget_next_funding_time"},
    "bybit": {"timing_policy_source": "adapter_bybit_next_funding_time"},
    "deribit": {"timing_policy_source": "adapter_deribit_next_hour_prediction"},
    "dydx": {"timing_policy_source": "adapter_dydx_next_funding_rate"},
    "edgex": {"timing_policy_source": "adapter_edgex_next_funding_time"},
    "ethereal": {"timing_policy_source": "adapter_ethereal_projected_hour"},
    "extended": {"timing_policy_source": "adapter_extended_current_hour"},
    "gate": {"timing_policy_source": "adapter_gate_next_funding_time"},
    "grvt": {"timing_policy_source": "adapter_grvt_current_interval"},
    "hyperliquid": {"timing_policy_source": "adapter_hyperliquid_predicted_or_next_hour"},
    "kraken": {"timing_policy_source": "adapter_kraken_next_hour_prediction"},
    "kucoin": {"timing_policy_source": "adapter_kucoin_next_funding_time"},
    "lighter": {"timing_policy_source": "adapter_lighter_current_hour"},
    "mexc": {"timing_policy_source": "adapter_mexc_next_settle_time"},
    "okx": {"timing_policy_source": "adapter_okx_next_funding_time"},
    "paradex": {"timing_policy_source": "adapter_paradex_projected_hour"},
    "risex": {"timing_policy_source": "adapter_risex_next_funding_time"},
}
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
    "unclear",
}
VALID_FUNDING_SIGN_CONVENTIONS = {
    "positive_long_pays",
    "positive_short_pays",
    "unclear",
}
VALID_CONTRACT_KINDS = {
    "linear_perpetual",
    "inverse_perpetual",
    "delivery",
    "pre_market",
    "unknown",
}


class ExecutionModel(str, Enum):
    CLOB = "CLOB"
    RFQ = "RFQ"
    POOL = "POOL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VenueFundingCapabilities:
    venue: str
    data_enabled: bool
    strategy_observation_enabled: bool
    shadow_candidate_enabled: bool
    paper_enabled: bool
    live_enabled: bool
    execution_model: ExecutionModel
    settlement_verification_level: str
    readiness_status: str = "OBSERVATION_ONLY"
    blockers: tuple[str, ...] = ()
    evidence_version: str = "funding-capabilities-2026-07-29"
    mandatory: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["execution_model"] = self.execution_model.value
        return payload


_APPROVED_SETTLEMENT_CAPTURE_VENUES = {
    "binance",
    "bybit",
    "okx",
    "hyperliquid",
    "lighter",
    "dydx",
}
_PAPER_ENABLED_SETTLEMENT_CAPTURE_VENUES = {
    "binance",
    "bybit",
    "okx",
}


_VENUE_FUNDING_CAPABILITIES: dict[str, VenueFundingCapabilities] = {
    venue: VenueFundingCapabilities(
        venue=venue,
        data_enabled=True,
        strategy_observation_enabled=True,
        shadow_candidate_enabled=True,
        paper_enabled=venue in _PAPER_ENABLED_SETTLEMENT_CAPTURE_VENUES,
        live_enabled=False,
        execution_model=ExecutionModel.CLOB,
        settlement_verification_level="PUBLIC_OBSERVED",
        readiness_status=(
            "VERIFIED"
            if venue in _PAPER_ENABLED_SETTLEMENT_CAPTURE_VENUES
            else "EXPERIMENTAL"
        ),
        blockers=(
            ("live_disabled",)
            if venue in _PAPER_ENABLED_SETTLEMENT_CAPTURE_VENUES
            else ("paper_execution_contract_incomplete", "live_disabled")
        ),
    )
    for venue in _APPROVED_SETTLEMENT_CAPTURE_VENUES
}
_VENUE_FUNDING_CAPABILITIES.update(
    {
        "extended": VenueFundingCapabilities(
            venue="extended",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="UNVERIFIED",
            readiness_status="OBSERVATION_ONLY",
            blockers=("funding_accrual_model_unknown", "reviewed_promotion_required"),
        ),
        "edgex": VenueFundingCapabilities(
            venue="edgex",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="UNVERIFIED",
            readiness_status="OBSERVATION_ONLY",
            blockers=("funding_accrual_model_unknown", "reviewed_promotion_required"),
        ),
        "ethereal": VenueFundingCapabilities(
            venue="ethereal",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="UNVERIFIED",
            readiness_status="OBSERVATION_ONLY",
            blockers=("funding_accrual_model_unknown", "reviewed_promotion_required"),
        ),
        "grvt": VenueFundingCapabilities(
            venue="grvt",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="UNVERIFIED",
            readiness_status="OBSERVATION_ONLY",
            blockers=("funding_accrual_model_unknown", "reviewed_promotion_required"),
        ),
        "risex": VenueFundingCapabilities(
            venue="risex",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=True,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="PUBLIC_OBSERVED",
            readiness_status="EXPERIMENTAL",
            blockers=("mainnet_canary_required", "reviewed_promotion_required"),
            mandatory=True,
        ),
        "pacifica": VenueFundingCapabilities(
            venue="pacifica",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=True,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="MAINNET_CANARY_REQUIRED",
            readiness_status="EXPERIMENTAL",
            blockers=("mainnet_canary_required", "reviewed_promotion_required"),
        ),
        "nado": VenueFundingCapabilities(
            venue="nado",
            data_enabled=True,
            strategy_observation_enabled=True,
            shadow_candidate_enabled=True,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="MAINNET_CANARY_REQUIRED",
            readiness_status="EXPERIMENTAL",
            blockers=("position_inclusion_rule_unverified", "reviewed_promotion_required"),
        ),
        "variational": VenueFundingCapabilities(
            venue="variational",
            data_enabled=True,
            strategy_observation_enabled=False,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.RFQ,
            settlement_verification_level="UNVERIFIED",
            readiness_status="QUARANTINED",
            blockers=(
                "rfq_execution_layer_missing",
                "funding_mechanics_unverified",
                "fee_model_missing",
            ),
        ),
        "paradex": VenueFundingCapabilities(
            venue="paradex",
            data_enabled=True,
            strategy_observation_enabled=False,
            shadow_candidate_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            execution_model=ExecutionModel.CLOB,
            settlement_verification_level="CONTINUOUS_PRO_RATA",
            readiness_status="STRUCTURALLY_INCOMPATIBLE",
            blockers=("funding_continuous_pro_rata",),
        ),
    }
)


def venue_funding_capabilities(venue: str) -> VenueFundingCapabilities:
    venue_key = str(venue or "").strip().lower()
    configured = _VENUE_FUNDING_CAPABILITIES.get(venue_key)
    if configured is not None:
        return configured
    return VenueFundingCapabilities(
        venue=venue_key or "unknown",
        data_enabled=True,
        strategy_observation_enabled=True,
        shadow_candidate_enabled=True,
        paper_enabled=False,
        live_enabled=False,
        execution_model=ExecutionModel.CLOB,
        settlement_verification_level="UNVERIFIED",
        readiness_status="OBSERVATION_ONLY",
        blockers=("verified_paper_contract_incomplete", "live_disabled"),
    )


def apply_venue_funding_capabilities(market: dict[str, Any]) -> dict[str, Any]:
    row = dict(market)
    capabilities = venue_funding_capabilities(str(row.get("venue") or ""))
    payload = capabilities.as_dict()
    row["data_enabled"] = payload["data_enabled"]
    row["strategy_observation_enabled"] = payload["strategy_observation_enabled"]
    row["shadow_candidate_enabled"] = payload["shadow_candidate_enabled"]
    row["paper_enabled"] = payload["paper_enabled"]
    row["live_enabled"] = payload["live_enabled"]
    row["execution_model"] = payload["execution_model"]
    row["settlement_verification_level"] = payload["settlement_verification_level"]
    row["readiness_status"] = payload["readiness_status"]
    row["evidence_version"] = payload["evidence_version"]
    row["venue_capability_blockers"] = list(payload["blockers"])
    row["mandatory_venue"] = payload["mandatory"]
    return row


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
    normalized_next_funding_rate_present: bool = False
    position_inclusion_rule: str | None = None
    position_inclusion_rule_verified: bool = False
    settlement_contract_blockers: tuple[str, ...] = ()
    entry_safety_buffer_seconds: float | None = None
    exit_safety_buffer_seconds: float | None = None
    timing_policy_source: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_declared_venue_capability_contract(market: dict[str, Any]) -> dict[str, Any]:
    """Attach non-authoritative inventory metadata; adapters own eligibility fields."""
    row = apply_venue_funding_capabilities(market)
    venue = str(row.get("venue") or "").lower()
    contract = DECLARED_NEXT_SETTLEMENT_VENUES.get(venue)
    if not contract:
        return row
    row.setdefault("declared_next_settlement_venue", True)
    row.setdefault("declared_timing_policy_source_hint", contract["timing_policy_source"])
    return row


def capability_from_market(market: dict[str, Any]) -> VenueCapability:
    venue = str(market.get("venue") or "").lower()
    settlement_contract = settlement_contract_from_market(market)
    settlement_blockers = tuple(
        settlement_contract_blockers(
            settlement_contract,
            next_settlement_at=market.get("next_funding_at"),
        )
    )
    funding_kind = str(market.get("funding_rate_kind") or "")
    collateral = normalized_asset(market.get("collateral_asset"))
    quote = normalized_asset(market.get("quote_asset"))
    semantics = str(market.get("funding_rate_semantics") or "unclear").strip().lower()
    if semantics not in {"next_settlement", "last_settlement", "continuous", "unclear"}:
        semantics = "unclear"
    contract_type = str(market.get("contract_type") or "").strip().lower()
    contract_kind = str(market.get("contract_kind") or "unknown").strip().lower()
    if contract_kind not in VALID_CONTRACT_KINDS:
        contract_kind = "unknown"
    funding_rate_unit = str(market.get("funding_rate_unit") or "unclear").strip().lower()
    if funding_rate_unit not in VALID_FUNDING_RATE_UNITS:
        funding_rate_unit = "unclear"
    funding_sign_convention = str(
        market.get("funding_sign_convention") or "unclear"
    ).strip().lower()
    if funding_sign_convention not in VALID_FUNDING_SIGN_CONVENTIONS:
        funding_sign_convention = "unclear"
    supports_perpetuals = bool(market.get("supports_perpetuals"))
    is_linear = bool(market.get("is_linear_contract"))
    if contract_kind == "linear_perpetual":
        supports_perpetuals = True
        is_linear = True
    return VenueCapability(
        venue=venue,
        supports_perpetuals=supports_perpetuals,
        is_linear_contract=is_linear,
        contract_kind=contract_kind,
        collateral_asset=collateral,
        quote_asset=quote,
        supports_discrete_funding=bool(market.get("supports_discrete_funding")),
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
        supports_orderbook_depth=bool(
            market.get("supports_orderbook_depth")
            or market.get("orderbook_depth_available")
        ),
        supports_24h_quote_volume=positive(market.get("volume_24h_usd")),
        supports_open_interest=positive(market.get("open_interest_usd")),
        supports_taker_fee=bool(fee_evidence_status(market, "taker").get("verified")),
        supports_quantity_step=positive(market.get("quantity_step")),
        supports_min_notional=positive(market.get("min_notional_usd")) or positive(
            market.get("min_notional")
        ),
        supports_funding_history=bool(market.get("funding_history_available", False)),
        supports_server_time=bool(market.get("server_time")),
        normalized_next_funding_rate_present=market.get(
            "normalized_next_funding_rate"
        ) is not None,
        position_inclusion_rule=settlement_contract.position_inclusion_rule,
        position_inclusion_rule_verified=(
            settlement_contract.position_inclusion_rule_verified
        ),
        settlement_contract_blockers=settlement_blockers,
        entry_safety_buffer_seconds=(
            float(market["entry_safety_buffer_seconds"])
            if positive(market.get("entry_safety_buffer_seconds"))
            else None
        ),
        exit_safety_buffer_seconds=(
            float(market["exit_safety_buffer_seconds"])
            if positive(market.get("exit_safety_buffer_seconds"))
            else None
        ),
        timing_policy_source=(
            str(market.get("timing_policy_source"))
            if market.get("timing_policy_source") not in (None, "")
            else None
        ),
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
    if not capability.normalized_next_funding_rate_present:
        reasons.append("normalized_next_funding_rate_missing")
    if capability.settlement_contract_blockers:
        reasons.extend(capability.settlement_contract_blockers)
    elif not capability.position_inclusion_rule:
        reasons.append("position_inclusion_rule_missing")
    elif not capability.position_inclusion_rule_verified:
        reasons.append("position_inclusion_rule_unverified")
    if capability.entry_safety_buffer_seconds is None:
        reasons.append("entry_safety_buffer_seconds_missing")
    if capability.exit_safety_buffer_seconds is None:
        reasons.append("exit_safety_buffer_seconds_missing")
    if capability.timing_policy_source is None:
        reasons.append("timing_policy_source_missing")
    required_flags = {
        "supports_next_funding_timestamp": capability.supports_next_funding_timestamp,
        "supports_mark_price": capability.supports_mark_price,
        "supports_index_price": capability.supports_index_price,
        "supports_orderbook_timestamp": capability.supports_orderbook_timestamp,
        "supports_orderbook_depth": capability.supports_orderbook_depth,
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
    collateral_family_compatible = (
        collateral_family(long_capability.collateral_asset) == USD_MAJOR_STABLE
        and collateral_family(short_capability.collateral_asset) == USD_MAJOR_STABLE
    )
    quote_family_compatible = (
        collateral_family(long_capability.quote_asset) == USD_MAJOR_STABLE
        and collateral_family(short_capability.quote_asset) == USD_MAJOR_STABLE
    )
    if (
        long_capability.collateral_asset != short_capability.collateral_asset
        and not collateral_family_compatible
    ):
        cross_venue_reasons.append("collateral_asset_mismatch")
    if long_capability.quote_asset != short_capability.quote_asset and not quote_family_compatible:
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
            rows.append({"venue": venue, "status": "UNAVAILABLE", "reason": "no_sample_market"})
            continue
        capability = capability_from_market(market)
        rejections = synchronized_capability_rejection(capability)
        if rejections:
            rows.append(
                {
                    "venue": venue,
                    "status": "RESEARCH_ONLY",
                    "reason": ",".join(rejections),
                    "capability": capability.as_dict(),
                }
            )
        else:
            rows.append(
                {
                    "venue": venue,
                    "status": "PAPER_ELIGIBLE",
                    "reason": "all_synchronized_funding_gates_passed",
                    "capability": capability.as_dict(),
                }
            )
    return rows
