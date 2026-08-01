from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from smart_money_radar.funding.adapter_contracts import (
    USD_COMPARABLE_STABLE_FAMILIES,
    USD_MAJOR_STABLE,
    collateral_family,
)
from smart_money_radar.funding.fees import fee_evidence_status, fee_rate_value
from smart_money_radar.funding.models import DEFAULT_TAKER_FEE_RATES
from smart_money_radar.funding.settlement_contracts import (
    FundingAccrualModel,
    FundingSettlementContract,
    parse_contract_time,
    settlement_contract_from_market,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES


class EvaluationMode(str, Enum):
    DISCOVERY = "DISCOVERY"
    EXPERIMENTAL_SIMULATION = "EXPERIMENTAL_SIMULATION"
    EXPERIMENTAL_PAPER = "EXPERIMENTAL_PAPER"
    VERIFIED_PAPER = "VERIFIED_PAPER"


class ReadinessLevel(str, Enum):
    STRUCTURALLY_BLOCKED = "structurally_blocked"
    STRUCTURALLY_ELIGIBLE = "structurally_eligible"
    ECONOMICALLY_OBSERVABLE = "economically_observable"
    EXPERIMENTAL_SIMULATION_READY = "experimental_simulation_ready"
    EXPERIMENTAL_PAPER_READY = "experimental_paper_ready"
    VERIFIED_PAPER_READY = "verified_paper_ready"
    SETTLEMENT_VALIDATION_READY = "settlement_validation_ready"


class RateEstimateKind(str, Enum):
    PUBLISHED_NEXT = "PUBLISHED_NEXT"
    PUBLISHED_CURRENT_INTERVAL = "PUBLISHED_CURRENT_INTERVAL"
    PUBLISHED_FORECAST = "PUBLISHED_FORECAST"
    PUBLISHED_PREDICTED = "PUBLISHED_PREDICTED"
    DERIVED_CURRENT_INTERVAL = "DERIVED_CURRENT_INTERVAL"
    CURRENT_RATE_FALLBACK = "CURRENT_RATE_FALLBACK"
    LAST_SETTLEMENT_REFERENCE = "LAST_SETTLEMENT_REFERENCE"
    UNKNOWN = "UNKNOWN"


class ExecutionEvidenceLevel(str, Enum):
    REFERENCE_PRICE_ONLY = "REFERENCE_PRICE_ONLY"
    TOP_OF_BOOK = "TOP_OF_BOOK"
    PARTIAL_DEPTH = "PARTIAL_DEPTH"
    FULL_DEPTH = "FULL_DEPTH"
    VERIFIED_SIMULATED_IOC = "VERIFIED_SIMULATED_IOC"


RATE_KIND_MAP = {
    "published_next_estimate": RateEstimateKind.PUBLISHED_NEXT,
    "published_next_hour": RateEstimateKind.PUBLISHED_NEXT,
    "published_next_hour_estimate": RateEstimateKind.PUBLISHED_FORECAST,
    "published_predicted_next": RateEstimateKind.PUBLISHED_PREDICTED,
    "published_next_hour_prediction": RateEstimateKind.PUBLISHED_PREDICTED,
    "published_forecast": RateEstimateKind.PUBLISHED_FORECAST,
    "published_projected_1h": RateEstimateKind.PUBLISHED_FORECAST,
    "published_current_estimate": RateEstimateKind.PUBLISHED_CURRENT_INTERVAL,
    "published_current": RateEstimateKind.PUBLISHED_CURRENT_INTERVAL,
    "published_current_interval_rate": RateEstimateKind.PUBLISHED_CURRENT_INTERVAL,
    "published_current_hour_estimate": RateEstimateKind.PUBLISHED_CURRENT_INTERVAL,
    "published_8h_equivalent_normalized_hourly": RateEstimateKind.DERIVED_CURRENT_INTERVAL,
    "published_latest_24h_x18": RateEstimateKind.DERIVED_CURRENT_INTERVAL,
    "published_latest_hour_x18": RateEstimateKind.DERIVED_CURRENT_INTERVAL,
    "published_predicted_24h_hourly": RateEstimateKind.DERIVED_CURRENT_INTERVAL,
    "published_current_fallback": RateEstimateKind.CURRENT_RATE_FALLBACK,
    "published_last_settlement": RateEstimateKind.LAST_SETTLEMENT_REFERENCE,
    "last_settlement_reference": RateEstimateKind.LAST_SETTLEMENT_REFERENCE,
}

RATE_KIND_CONFIDENCE = {
    RateEstimateKind.PUBLISHED_NEXT: 0.92,
    RateEstimateKind.PUBLISHED_CURRENT_INTERVAL: 0.68,
    RateEstimateKind.PUBLISHED_FORECAST: 0.62,
    RateEstimateKind.PUBLISHED_PREDICTED: 0.60,
    RateEstimateKind.DERIVED_CURRENT_INTERVAL: 0.52,
    RateEstimateKind.CURRENT_RATE_FALLBACK: 0.45,
    RateEstimateKind.LAST_SETTLEMENT_REFERENCE: 0.25,
    RateEstimateKind.UNKNOWN: 0.0,
}

RATE_KIND_MIN_UNCERTAINTY_BPS = {
    RateEstimateKind.PUBLISHED_NEXT: 1.0,
    RateEstimateKind.PUBLISHED_CURRENT_INTERVAL: 5.0,
    RateEstimateKind.PUBLISHED_FORECAST: 7.5,
    RateEstimateKind.PUBLISHED_PREDICTED: 7.5,
    RateEstimateKind.DERIVED_CURRENT_INTERVAL: 10.0,
    RateEstimateKind.CURRENT_RATE_FALLBACK: 15.0,
    RateEstimateKind.LAST_SETTLEMENT_REFERENCE: 25.0,
    RateEstimateKind.UNKNOWN: 0.0,
}

EXACT_NEXT_RATE_KINDS = {RateEstimateKind.PUBLISHED_NEXT}

ESTIMATE_PAPER_ALLOWED_VERIFIED_BLOCKERS = frozenset({
    "long_exact_next_rate_missing",
    "short_exact_next_rate_missing",
    "rate_confidence_below_verified_threshold",
})


def estimated_paper_blockers(verified_blockers: list[str]) -> list[str]:
    """Return blockers that still apply to estimate-based paper entry.

    This deliberately does not change VERIFIED_PAPER. The estimate-based paper
    mode can use the latest typed funding estimate instead of an exact next
    settlement rate, so exact-next and verified-rate-confidence blockers are
    removed. Fee, sizing, paper_enabled, live-disabled, execution, settlement,
    accounting, collateral, environment, endpoint, and product blockers remain.
    """
    return [
        reason
        for reason in verified_blockers
        if reason not in ESTIMATE_PAPER_ALLOWED_VERIFIED_BLOCKERS
    ]

RATE_KIND_TO_SEMANTICS: dict[str, str] = {
    "published_next_estimate": "next_settlement",
    "published_next_hour": "next_settlement",
    "published_next_hour_estimate": "forecast_next_settlement",
    "published_predicted_next": "predicted_next_settlement",
    "published_next_hour_prediction": "predicted_next_settlement",
    "published_forecast": "forecast_next_settlement",
    "published_projected_1h": "forecast_next_settlement",
    "published_current_estimate": "current_interval_estimate",
    "published_current": "current_interval_estimate",
    "published_current_interval_rate": "current_interval_estimate",
    "published_current_hour_estimate": "current_interval_estimate",
    "published_8h_equivalent_normalized_hourly": "derived_current_interval_estimate",
    "published_latest_24h_x18": "derived_current_interval_estimate",
    "published_latest_hour_x18": "derived_current_interval_estimate",
    "published_predicted_24h_hourly": "derived_current_interval_estimate",
    "published_current_fallback": "current_interval_fallback",
    "published_last_settlement": "last_settlement_reference",
    "last_settlement_reference": "last_settlement_reference",
}

EXACT_NEXT_RATE_KIND_STRINGS: frozenset[str] = frozenset({
    "published_next_estimate",
    "published_next_hour",
})

INCOMPATIBLE_CONTRACT_KINDS = {
    "delivery",
    "future",
    "futures_delivery",
    "option",
    "spot",
    "pre_market",
}

USD_OTHER_STABLE_FAMILIES = USD_COMPARABLE_STABLE_FAMILIES - {USD_MAJOR_STABLE}


@dataclass(frozen=True)
class FundingRiskPolicy:
    rate_haircut_fraction: float = 0.20
    minimum_uncertainty_bps_by_kind: dict[RateEstimateKind, float] = field(
        default_factory=lambda: dict(RATE_KIND_MIN_UNCERTAINTY_BPS)
    )
    current_rate_confidence_multiplier: float = 0.85
    forecast_rate_confidence_multiplier: float = 0.80
    sign_flip_uncertainty_multiplier: float = 0.75
    global_conservative_taker_fee_rate: float = 0.0010
    fee_uncertainty_reserve_bps: float = 2.0
    account_fee_evidence_max_age_seconds: float = 24.0 * 60.0 * 60.0
    public_fee_endpoint_max_age_seconds: float = 7.0 * 24.0 * 60.0 * 60.0
    reviewed_static_fee_max_age_seconds: float = 30.0 * 24.0 * 60.0 * 60.0
    reference_price_slippage_bps: float = 15.0
    top_of_book_slippage_bps: float = 8.0
    partial_depth_slippage_bps: float = 4.0
    full_depth_slippage_bps: float = 2.0
    synthetic_execution_reserve_bps: float = 10.0
    timing_uncertainty_reserve_bps: float = 5.0
    collateral_major_stable_reserve_bps: float = 5.0
    collateral_other_stable_reserve_bps: float = 25.0
    settlement_uncertainty_multiplier: float = 0.75
    default_entry_safety_buffer_seconds: float = 30.0
    default_exit_safety_buffer_seconds: float = 5.0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["minimum_uncertainty_bps_by_kind"] = {
            kind.value if isinstance(kind, RateEstimateKind) else str(kind): value
            for kind, value in self.minimum_uncertainty_bps_by_kind.items()
        }
        return payload


DEFAULT_FUNDING_RISK_POLICY = FundingRiskPolicy()


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def positive_float(value: Any) -> float | None:
    parsed = optional_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _parse_snapshot_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _snapshot_reference_time(*markets: dict[str, Any]) -> datetime | None:
    timestamps: list[datetime] = []
    for market in markets:
        for field in (
            "orderbook_response_received_at",
            "response_received_at",
            "source_event_at",
            "observed_at",
            "fee_observed_at",
            "fee_reviewed_at",
        ):
            parsed = _parse_snapshot_time(market.get(field))
            if parsed is not None:
                timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def normalized_rate_kind(value: Any) -> RateEstimateKind:
    if isinstance(value, RateEstimateKind):
        return value
    text = str(value or "").strip()
    if not text:
        return RateEstimateKind.UNKNOWN
    upper = text.upper()
    if upper in RateEstimateKind.__members__:
        return RateEstimateKind[upper]
    try:
        return RateEstimateKind(upper)
    except ValueError:
        return RATE_KIND_MAP.get(text.lower(), RateEstimateKind.UNKNOWN)


def rate_estimate_from_market(
    market: dict[str, Any],
    *,
    policy: FundingRiskPolicy | None = None,
) -> dict[str, Any]:
    risk_policy = policy or DEFAULT_FUNDING_RISK_POLICY
    risk_flags: list[str] = []
    assumptions: list[str] = []
    source_kind = str(market.get("funding_rate_kind") or "").strip().lower()
    estimate_kind = normalized_rate_kind(
        market.get("rate_estimate_kind") or RATE_KIND_MAP.get(source_kind)
    )
    exact_next_rate_available = (
        estimate_kind in EXACT_NEXT_RATE_KINDS
        and optional_float(
            market.get("normalized_next_funding_rate")
            if market.get("normalized_next_funding_rate") is not None
            else market.get("rate_estimate_per_settlement")
        )
        is not None
    )
    rate = optional_float(market.get("rate_estimate_per_settlement"))
    rate_source_field = "rate_estimate_per_settlement"
    if rate is None:
        rate = optional_float(market.get("normalized_next_funding_rate"))
        rate_source_field = "normalized_next_funding_rate"
    if rate is None:
        rate = optional_float(market.get("normalized_rate_decimal"))
        rate_source_field = "normalized_rate_decimal"
    if rate is None and estimate_kind is not RateEstimateKind.UNKNOWN:
        rate = optional_float(market.get("funding_rate"))
        rate_source_field = "funding_rate"
        assumptions.append("typed funding_rate converted through rate estimate contract")

    interval_hours = positive_float(market.get("funding_interval_hours"))
    unit = str(market.get("funding_rate_unit") or "unclear").strip().lower()
    sign = str(market.get("funding_sign_convention") or "unclear").strip().lower()
    hard_blockers: list[str] = []
    if estimate_kind is RateEstimateKind.UNKNOWN:
        hard_blockers.append("rate_estimate_kind_unknown")
    if rate is None:
        hard_blockers.append("usable_funding_rate_estimate_missing")
    if sign != "positive_long_pays":
        hard_blockers.append("funding_sign_convention_unknown_or_unsupported")
    if unit not in {
        "fraction_of_notional_per_settlement",
        "fraction_of_notional_per_hour",
    }:
        hard_blockers.append("funding_rate_unit_or_scale_unknown")
    if interval_hours is None:
        hard_blockers.append("funding_interval_unknown")
    if hard_blockers:
        return {
            "rate_estimate_per_settlement": None,
            "rate_estimate_kind": RateEstimateKind.UNKNOWN.value,
            "rate_estimate_lower_bound": None,
            "rate_estimate_upper_bound": None,
            "rate_estimate_confidence": 0.0,
            "rate_estimate_source": source_kind or None,
            "rate_estimate_observed_at": observed_at_from_market(market),
            "exact_next_rate_available": False,
            "rate_estimate_assumptions": assumptions,
            "rate_estimate_risk_flags": risk_flags,
            "rate_estimate_hard_blockers": list(dict.fromkeys(hard_blockers)),
        }

    if unit == "fraction_of_notional_per_hour":
        rate = float(rate) * float(interval_hours)
        assumptions.append("hourly rate scaled to one settlement interval")

    confidence = RATE_KIND_CONFIDENCE[estimate_kind]
    if estimate_kind in {
        RateEstimateKind.PUBLISHED_CURRENT_INTERVAL,
        RateEstimateKind.CURRENT_RATE_FALLBACK,
        RateEstimateKind.DERIVED_CURRENT_INTERVAL,
    }:
        confidence *= risk_policy.current_rate_confidence_multiplier
        risk_flags.append("rate_estimate_not_exact_next")
    if estimate_kind in {
        RateEstimateKind.PUBLISHED_FORECAST,
        RateEstimateKind.PUBLISHED_PREDICTED,
    }:
        confidence *= risk_policy.forecast_rate_confidence_multiplier
        risk_flags.append("rate_forecast_or_predicted")
    if estimate_kind is RateEstimateKind.LAST_SETTLEMENT_REFERENCE:
        risk_flags.append("last_settlement_reference_not_next_cashflow")
        assumptions.append("last settlement rate shown for research only")
    if not exact_next_rate_available:
        risk_flags.append("exact_next_rate_unavailable")

    lower = optional_float(market.get("rate_estimate_lower_bound"))
    upper = optional_float(market.get("rate_estimate_upper_bound"))
    if lower is None or upper is None:
        minimum_bps = risk_policy.minimum_uncertainty_bps_by_kind.get(
            estimate_kind,
            RATE_KIND_MIN_UNCERTAINTY_BPS[estimate_kind],
        )
        uncertainty = max(abs(float(rate)) * risk_policy.rate_haircut_fraction, minimum_bps / 10_000.0)
        lower = float(rate) - uncertainty
        upper = float(rate) + uncertainty
    lower, upper = sorted((float(lower), float(upper)))
    if lower <= 0.0 <= upper:
        risk_flags.append("rate_sign_flip_possible")
        confidence *= risk_policy.sign_flip_uncertainty_multiplier
        confidence = min(confidence, 0.50)

    evidence = market.get("normalization_evidence")
    source = None
    if isinstance(evidence, dict):
        source = evidence.get("source_identifier") or evidence.get("source_kind")
    if not source:
        source = market.get("rate_estimate_source") or source_kind or rate_source_field

    return {
        "rate_estimate_per_settlement": float(rate),
        "rate_estimate_kind": estimate_kind.value,
        "rate_estimate_lower_bound": lower,
        "rate_estimate_upper_bound": upper,
        "rate_estimate_confidence": max(0.0, min(1.0, confidence)),
        "rate_estimate_source": str(source) if source not in (None, "") else None,
        "rate_estimate_observed_at": observed_at_from_market(market),
        "exact_next_rate_available": bool(exact_next_rate_available),
        "rate_estimate_assumptions": list(dict.fromkeys(assumptions)),
        "rate_estimate_risk_flags": list(dict.fromkeys(risk_flags)),
        "rate_estimate_hard_blockers": [],
        "rate_estimate_policy": {
            "rate_haircut_fraction": risk_policy.rate_haircut_fraction,
            "minimum_uncertainty_bps": risk_policy.minimum_uncertainty_bps_by_kind.get(
                estimate_kind,
                RATE_KIND_MIN_UNCERTAINTY_BPS[estimate_kind],
            ),
            "confidence_multiplier_applied": confidence,
        },
    }


def attach_rate_estimate(
    market: dict[str, Any],
    *,
    policy: FundingRiskPolicy | None = None,
) -> dict[str, Any]:
    row = dict(market)
    row.update(rate_estimate_from_market(row, policy=policy))
    return row


def observed_at_from_market(market: dict[str, Any]) -> str | None:
    for key in (
        "source_event_at",
        "response_received_at",
        "observed_at",
        "normalized_at",
    ):
        value = market.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def reference_price(market: dict[str, Any]) -> tuple[float | None, str | None]:
    for key, label in (
        ("mark_price", "mark"),
        ("index_price", "index"),
        ("oracle_price", "oracle"),
        ("reference_price", "reference"),
        ("mid_price", "mid"),
    ):
        value = positive_float(market.get(key))
        if value is not None:
            return value, label
    return None, None


def execution_evidence_level(
    market: dict[str, Any],
    *,
    has_orderbook_client: bool = True,
) -> ExecutionEvidenceLevel:
    bids = market.get("bids") or []
    asks = market.get("asks") or []
    if bids and asks:
        return ExecutionEvidenceLevel.FULL_DEPTH
    if market.get("verified_simulated_ioc"):
        return ExecutionEvidenceLevel.VERIFIED_SIMULATED_IOC
    if market.get("orderbook_depth_available") or market.get("supports_orderbook_depth"):
        return ExecutionEvidenceLevel.PARTIAL_DEPTH
    if positive_float(market.get("best_bid")) is not None and positive_float(market.get("best_ask")) is not None:
        return ExecutionEvidenceLevel.TOP_OF_BOOK
    return ExecutionEvidenceLevel.REFERENCE_PRICE_ONLY


def _execution_confidence(level: ExecutionEvidenceLevel) -> float:
    return {
        ExecutionEvidenceLevel.REFERENCE_PRICE_ONLY: 0.25,
        ExecutionEvidenceLevel.TOP_OF_BOOK: 0.45,
        ExecutionEvidenceLevel.PARTIAL_DEPTH: 0.65,
        ExecutionEvidenceLevel.FULL_DEPTH: 0.80,
        ExecutionEvidenceLevel.VERIFIED_SIMULATED_IOC: 0.95,
    }[level]


def _execution_slippage_bps(
    level: ExecutionEvidenceLevel,
    policy: FundingRiskPolicy,
) -> float:
    if level is ExecutionEvidenceLevel.REFERENCE_PRICE_ONLY:
        return policy.reference_price_slippage_bps + policy.synthetic_execution_reserve_bps
    if level is ExecutionEvidenceLevel.TOP_OF_BOOK:
        return policy.top_of_book_slippage_bps
    if level is ExecutionEvidenceLevel.PARTIAL_DEPTH:
        return policy.partial_depth_slippage_bps
    if level is ExecutionEvidenceLevel.FULL_DEPTH:
        return policy.full_depth_slippage_bps
    return policy.full_depth_slippage_bps


def modeled_fee_rate(
    market: dict[str, Any],
    *,
    policy: FundingRiskPolicy | None = None,
    now: Any = None,
) -> dict[str, Any]:
    risk_policy = policy or DEFAULT_FUNDING_RISK_POLICY
    venue = str(market.get("venue") or "").lower()
    numeric_rate = fee_rate_value(market, "taker")
    default_rate = DEFAULT_TAKER_FEE_RATES.get(venue)
    fee_status = fee_evidence_status(
        market,
        "taker",
        now=now,
        account_max_age_seconds=risk_policy.account_fee_evidence_max_age_seconds,
        public_endpoint_max_age_seconds=risk_policy.public_fee_endpoint_max_age_seconds,
        reviewed_static_max_age_seconds=risk_policy.reviewed_static_fee_max_age_seconds,
    )
    verified = bool(fee_status.get("verified")) and numeric_rate is not None
    flags: list[str] = []
    assumptions: list[str] = []
    if verified:
        base = float(numeric_rate or 0.0)
    else:
        candidates = [
            value
            for value in (
                numeric_rate,
                default_rate,
                risk_policy.global_conservative_taker_fee_rate,
            )
            if value is not None and math.isfinite(float(value))
        ]
        base = max(candidates) if candidates else risk_policy.global_conservative_taker_fee_rate
        if numeric_rate is None:
            flags.extend(["account_fee_unknown", "fee_fallback_used"])
            assumptions.append("taker fee missing; conservative global/venue fallback applied")
        flags.extend(["fee_unverified", "fee_fallback_used"])
        assumptions.append("taker fee evidence is not verified for paper-grade accounting")
        base += risk_policy.fee_uncertainty_reserve_bps / 10_000.0
    return {
        "fee_rate": max(0.0, float(base)),
        "verified": verified,
        "source_rate": numeric_rate,
        "venue_default_rate": default_rate,
        "global_fallback_rate": risk_policy.global_conservative_taker_fee_rate,
        "uncertainty_reserve_bps": 0.0 if verified else risk_policy.fee_uncertainty_reserve_bps,
        "risk_flags": list(dict.fromkeys(flags)),
        "assumptions": list(dict.fromkeys(assumptions)),
        "evidence_status": fee_status,
    }


def _settlement_confidence(contract: FundingSettlementContract) -> float:
    if contract.accrual_model == FundingAccrualModel.CONTINUOUS_PRO_RATA.value:
        return 0.0
    if contract.position_inclusion_rule_verified:
        return 0.85
    if contract.position_inclusion_rule:
        return 0.40
    return 0.18


def _accounting_confidence(contract: FundingSettlementContract) -> float:
    if contract.settlement_confirmation_source and contract.realized_payment_source:
        return 0.80
    if contract.settlement_confirmation_source:
        return 0.45
    return 0.20


def _market_contract_hard_blockers(
    market: dict[str, Any],
    contract: FundingSettlementContract,
) -> list[str]:
    venue = str(market.get("venue") or "").lower()
    hard: list[str] = []
    if venue in DEACTIVATED_FUNDING_VENUES:
        hard.append("venue_deactivated")
    if venue == "variational":
        hard.append("venue_quarantined_variational")
    if not str(market.get("canonical_asset") or market.get("canonical_underlying") or "").strip():
        hard.append("canonical_asset_missing")
    contract_kind = str(
        market.get("contract_kind") or market.get("contract_type") or ""
    ).strip().lower()
    if contract_kind in INCOMPATIBLE_CONTRACT_KINDS:
        hard.append("contract_kind_incompatible")
    if market.get("supports_perpetuals") is False:
        hard.append("not_perpetual")
    if (
        contract.accrual_model == FundingAccrualModel.CONTINUOUS_PRO_RATA.value
        or str(market.get("settlement_accrual_model") or "").upper()
        == FundingAccrualModel.CONTINUOUS_PRO_RATA.value
    ):
        hard.append("funding_continuous_pro_rata")
    if parse_contract_time(market.get("next_funding_at")) is None:
        hard.append("next_settlement_time_missing")
    if positive_float(market.get("funding_interval_hours")) is None and positive_float(
        market.get("settlement_interval_seconds")
    ) is None:
        hard.append("settlement_interval_unknown")
    if reference_price(market)[0] is None:
        hard.append("reference_price_missing")
    for key in ("funding_rate", "normalized_next_funding_rate", "rate_estimate_per_settlement"):
        value = market.get(key)
        if value in (None, ""):
            continue
        parsed = optional_float(value)
        if parsed is None:
            hard.append(f"{key}_numeric_invalid")
    return list(dict.fromkeys(hard))


def _market_soft_flags(
    market: dict[str, Any],
    contract: FundingSettlementContract,
    *,
    has_orderbook_client: bool,
    now: datetime | None = None,
) -> tuple[list[str], list[str], list[str]]:
    flags: list[str] = []
    assumptions: list[str] = []
    missing: list[str] = []
    rate = rate_estimate_from_market(market)
    flags.extend(rate.get("rate_estimate_risk_flags") or [])
    assumptions.extend(rate.get("rate_estimate_assumptions") or [])
    if not bool(rate.get("exact_next_rate_available")):
        flags.append("estimated_rate_used")
        missing.append("exact_next_funding_rate")
    if not contract.position_inclusion_rule:
        flags.append("position_inclusion_rule_missing")
        missing.append("position_inclusion_rule")
    elif not contract.position_inclusion_rule_verified:
        flags.append("position_inclusion_rule_unverified")
        missing.append("position_inclusion_canary")
    if not contract.settlement_confirmation_source:
        flags.append("settlement_confirmation_unavailable")
        missing.append("settlement_confirmation_source")
    if contract.assessment_jitter_before_seconds is None or contract.assessment_jitter_after_seconds is None:
        flags.append("assessment_jitter_unverified")
        missing.append("official_assessment_jitter")
    if positive_float(market.get("entry_safety_buffer_seconds")) is None:
        flags.append("entry_timing_buffer_defaulted")
        missing.append("entry_safety_buffer_seconds")
    if positive_float(market.get("exit_safety_buffer_seconds")) is None:
        flags.append("exit_timing_buffer_defaulted")
        missing.append("exit_safety_buffer_seconds")
    if not market.get("timing_policy_source"):
        flags.append("timing_policy_source_missing")
        missing.append("timing_policy_source")
    fee = modeled_fee_rate(market, now=now)
    flags.extend(fee["risk_flags"])
    assumptions.extend(fee["assumptions"])
    if not fee["verified"]:
        missing.append("verified_fee_evidence")
    if positive_float(market.get("quantity_step")) is None:
        flags.append("quantity_step_missing")
        assumptions.append("continuous theoretical sizing used in discovery")
        missing.append("quantity_step")
    if market.get("min_quantity_taker_applicable") is False:
        flags.append("taker_min_quantity_unverified")
        assumptions.append("public minimum quantity was not proven applicable to taker/IOC sizing")
        missing.append("taker_min_quantity")
    if positive_float(market.get("min_notional_usd")) is None and positive_float(market.get("min_notional")) is None:
        flags.append("min_notional_missing")
        assumptions.append("minimum notional not enforced until focused/verified stage")
        missing.append("min_notional")
    elif market.get("min_notional_taker_applicable") is False:
        flags.append("taker_min_notional_unverified")
        assumptions.append("public minimum notional was not proven applicable to taker/IOC sizing")
        missing.append("taker_min_notional")
    if positive_float(market.get("min_quantity")) is None:
        flags.append("min_quantity_missing")
        missing.append("min_quantity")
    if positive_float(market.get("open_interest_usd")) is None:
        flags.append("open_interest_missing")
        missing.append("open_interest")
    if positive_float(market.get("volume_24h_usd")) is None:
        flags.append("volume_24h_missing")
        missing.append("volume_24h")
    if not market.get("server_time") and not market.get("venue_server_time"):
        flags.append("server_time_missing")
        missing.append("server_time")
    if not market.get("orderbook_response_received_at") and not market.get("orderbook_event_time"):
        flags.append("orderbook_timestamp_missing")
        missing.append("orderbook_timestamp")
    level = execution_evidence_level(market, has_orderbook_client=has_orderbook_client)
    if level is ExecutionEvidenceLevel.REFERENCE_PRICE_ONLY:
        flags.append("synthetic_fill_model_required")
        assumptions.append("reference price plus conservative slippage reserve used for discovery")
        missing.append("orderbook_depth")
    elif level is not ExecutionEvidenceLevel.FULL_DEPTH:
        flags.append("execution_depth_incomplete")
        missing.append("full_orderbook_depth")
    if not has_orderbook_client:
        flags.append("orderbook_client_missing")
        missing.append("orderbook_client")
    ref_price, ref_kind = reference_price(market)
    if positive_float(market.get("mark_price")) is None and ref_price is not None:
        flags.append("mark_price_missing_reference_price_used")
        assumptions.append(f"{ref_kind} price used as discovery reference price")
        missing.append("mark_price")
    if market.get("paper_enabled") is False:
        flags.append("static_paper_enabled_false")
    if market.get("shadow_candidate_enabled") is False:
        flags.append("static_shadow_candidate_enabled_false")
    if market.get("environment_verified") is not True:
        flags.append("environment_unverified")
        missing.append("environment_identity")
    if not market.get("endpoint_base_url"):
        flags.append("endpoint_base_url_missing")
        missing.append("endpoint_base_url")
    provenance = str(market.get("endpoint_identity_provenance") or "").lower()
    if provenance in {"", "unknown", "unverified", "unverified_client_endpoint"}:
        flags.append("endpoint_identity_unverified")
        missing.append("endpoint_identity_evidence")
    if market.get("supports_discrete_funding") is False and contract.accrual_model in {
        FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        FundingAccrualModel.PERIODIC_INDEX_STEP.value,
    }:
        flags.append("adapter_discrete_funding_not_declared")
    return (
        list(dict.fromkeys(flags)),
        list(dict.fromkeys(assumptions)),
        list(dict.fromkeys(missing)),
    )


def evaluate_synchronized_route(
    *,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    target_notional: float,
    mode: EvaluationMode | str = EvaluationMode.DISCOVERY,
    clients_by_venue: dict[str, Any] | None = None,
    policy: FundingRiskPolicy | None = None,
) -> dict[str, Any]:
    risk_policy = policy or DEFAULT_FUNDING_RISK_POLICY
    evaluation_mode = (
        mode if isinstance(mode, EvaluationMode) else EvaluationMode(str(mode).upper())
    )
    long = attach_rate_estimate(long_market, policy=risk_policy)
    short = attach_rate_estimate(short_market, policy=risk_policy)
    reference_now = _snapshot_reference_time(long, short)
    long_contract = settlement_contract_from_market(long)
    short_contract = settlement_contract_from_market(short)
    clients = clients_by_venue or {}
    long_venue = str(long.get("venue") or "").lower()
    short_venue = str(short.get("venue") or "").lower()
    long_hard = _market_contract_hard_blockers(long, long_contract)
    short_hard = _market_contract_hard_blockers(short, short_contract)
    hard = [f"long_{reason}" for reason in long_hard] + [
        f"short_{reason}" for reason in short_hard
    ]
    if long_venue and short_venue and long_venue == short_venue:
        hard.append("same_venue_cross_venue_route")
    long_asset = str(long.get("canonical_asset") or long.get("canonical_underlying") or "").upper()
    short_asset = str(short.get("canonical_asset") or short.get("canonical_underlying") or "").upper()
    if not long_asset or not short_asset:
        hard.append("canonical_asset_missing")
    elif long_asset != short_asset:
        hard.append("canonical_asset_mismatch")
    long_env = str(long.get("environment") or "mainnet").lower()
    short_env = str(short.get("environment") or "mainnet").lower()
    if long_env in {"mainnet", "testnet"} and short_env in {"mainnet", "testnet"}:
        if long_env != short_env:
            hard.append("environment_mismatch")
    else:
        hard.append("environment_unknown")

    long_rate = rate_estimate_from_market(long, policy=risk_policy)
    short_rate = rate_estimate_from_market(short, policy=risk_policy)
    hard.extend(f"long_{reason}" for reason in long_rate.get("rate_estimate_hard_blockers") or [])
    hard.extend(f"short_{reason}" for reason in short_rate.get("rate_estimate_hard_blockers") or [])

    risk_flags: list[str] = []
    assumptions: list[str] = []
    missing: list[str] = []
    long_flags, long_assumptions, long_missing = _market_soft_flags(
        long,
        long_contract,
        has_orderbook_client=long_venue in clients,
        now=reference_now,
    )
    short_flags, short_assumptions, short_missing = _market_soft_flags(
        short,
        short_contract,
        has_orderbook_client=short_venue in clients,
        now=reference_now,
    )
    risk_flags.extend(f"long_{flag}" for flag in long_flags)
    risk_flags.extend(f"short_{flag}" for flag in short_flags)
    assumptions.extend(f"long: {item}" for item in long_assumptions)
    assumptions.extend(f"short: {item}" for item in short_assumptions)
    missing.extend(f"long_{item}" for item in long_missing)
    missing.extend(f"short_{item}" for item in short_missing)

    collateral_reserve_bps = 0.0
    long_collateral = str(long.get("collateral_asset") or long.get("quote_asset") or "").upper()
    short_collateral = str(short.get("collateral_asset") or short.get("quote_asset") or "").upper()
    long_family = collateral_family(long_collateral)
    short_family = collateral_family(short_collateral)
    if long_family == USD_MAJOR_STABLE and short_family == USD_MAJOR_STABLE:
        if long_collateral != short_collateral:
            risk_flags.append("collateral_cross_major_stable")
            assumptions.append("USDC/USDT collateral compared through USD numeraire")
            collateral_reserve_bps = max(
                collateral_reserve_bps,
                risk_policy.collateral_major_stable_reserve_bps,
            )
    elif long_family in USD_OTHER_STABLE_FAMILIES or short_family in USD_OTHER_STABLE_FAMILIES:
        risk_flags.append("collateral_other_dollar_stable")
        if "USDT0" in {long_collateral, short_collateral}:
            risk_flags.append("collateral_usdt0_risk")
        assumptions.append("other dollar-like collateral allowed only with reserve in discovery/experimental")
        missing.append("verified_stablecoin_accounting_contract")
        collateral_reserve_bps = max(
            collateral_reserve_bps,
            risk_policy.collateral_other_stable_reserve_bps,
        )
    else:
        hard.append("collateral_family_not_compatible")

    long_ref, long_ref_kind = reference_price(long)
    short_ref, short_ref_kind = reference_price(short)
    economics = _route_economics(
        long_market=long,
        short_market=short,
        long_rate=long_rate,
        short_rate=short_rate,
        long_reference_price=long_ref,
        short_reference_price=short_ref,
        target_notional=target_notional,
        policy=risk_policy,
        long_execution_level=execution_evidence_level(long, has_orderbook_client=long_venue in clients),
        short_execution_level=execution_evidence_level(short, has_orderbook_client=short_venue in clients),
        collateral_reserve_bps=collateral_reserve_bps,
        now=reference_now,
    )
    if economics.get("sizing_status") == "provisional":
        risk_flags.append("sizing_provisional")
        assumptions.append("continuous theoretical quantity used before exchange rounding")
    if economics.get("execution_status") == "SIMULATED_APPROXIMATE":
        risk_flags.append("synthetic_fill")

    rate_confidence_values = [
        float(long_rate.get("rate_estimate_confidence") or 0.0),
        float(short_rate.get("rate_estimate_confidence") or 0.0),
    ]
    execution_levels = [
        execution_evidence_level(long, has_orderbook_client=long_venue in clients),
        execution_evidence_level(short, has_orderbook_client=short_venue in clients),
    ]
    execution_confidence = min(_execution_confidence(level) for level in execution_levels)
    settlement_confidence = min(
        _settlement_confidence(long_contract),
        _settlement_confidence(short_contract),
    )
    accounting_confidence = min(
        _accounting_confidence(long_contract),
        _accounting_confidence(short_contract),
    )
    verified_blockers = _verified_blockers(
        risk_flags=list(dict.fromkeys(risk_flags)),
        missing=list(dict.fromkeys(missing)),
        economics=economics,
        long_market=long,
        short_market=short,
        long_rate=long_rate,
        short_rate=short_rate,
        execution_levels=execution_levels,
        settlement_confidence=settlement_confidence,
        accounting_confidence=accounting_confidence,
        now=reference_now,
    )
    hard = list(dict.fromkeys(str(reason) for reason in hard if reason))
    risk_flags = list(dict.fromkeys(str(flag) for flag in risk_flags if flag))
    assumptions = list(dict.fromkeys(str(item) for item in assumptions if item))
    missing = list(dict.fromkeys(str(item) for item in missing if item))

    economically_observable = not hard and economics.get("raw_expected_funding_usd") is not None
    experimental_simulation_ready = (
        economically_observable
        and float(economics.get("conservative_expected_net_usd") or 0.0) > 0.0
        and "funding_continuous_pro_rata" not in hard
        and "venue_quarantined_variational" not in hard
    )
    verified_ready = experimental_simulation_ready and not verified_blockers
    settlement_validation_ready = verified_ready and accounting_confidence >= 0.75
    if hard:
        readiness = ReadinessLevel.STRUCTURALLY_BLOCKED
    elif settlement_validation_ready:
        readiness = ReadinessLevel.SETTLEMENT_VALIDATION_READY
    elif verified_ready:
        readiness = ReadinessLevel.VERIFIED_PAPER_READY
    elif experimental_simulation_ready:
        readiness = ReadinessLevel.EXPERIMENTAL_SIMULATION_READY
    elif economically_observable:
        readiness = ReadinessLevel.ECONOMICALLY_OBSERVABLE
    else:
        readiness = ReadinessLevel.STRUCTURALLY_ELIGIBLE

    mode_blockers = hard
    if evaluation_mode is EvaluationMode.VERIFIED_PAPER:
        mode_blockers = list(dict.fromkeys([*hard, *verified_blockers]))

    return {
        "evaluation_mode": evaluation_mode.value,
        "hard_blockers": hard,
        "risk_flags": risk_flags,
        "assumptions": assumptions,
        "missing_capabilities": missing,
        "readiness_level": readiness.value,
        "rate_confidence": min(rate_confidence_values),
        "execution_confidence": execution_confidence,
        "settlement_confidence": settlement_confidence,
        "accounting_confidence": accounting_confidence,
        "rate_estimates": {
            "long": {key: value for key, value in long_rate.items() if key != "rate_estimate_policy"},
            "short": {key: value for key, value in short_rate.items() if key != "rate_estimate_policy"},
        },
        "execution_evidence_level": {
            "long": execution_levels[0].value,
            "short": execution_levels[1].value,
        },
        "economics": economics,
        "verified_paper_blockers": verified_blockers,
        "mode_blockers": mode_blockers,
        "structurally_eligible": not hard,
        "economically_observable": economically_observable,
        "experimental_simulation_ready": experimental_simulation_ready,
        "experimental_paper_ready": experimental_simulation_ready,
        "experimental_paper_ready_deprecated": True,
        "verified_paper_ready": verified_ready,
        "settlement_validation_ready": settlement_validation_ready,
        "paper_mode": (
            "VERIFIED_PAPER"
            if verified_ready
            else "EXPERIMENTAL_SIMULATION"
            if experimental_simulation_ready
            else None
        ),
        "funding_cashflow_status": "CONFIRMED_ONLY" if verified_ready else "ESTIMATED_ONLY",
        "execution_status": economics.get("execution_status"),
        "settlement_semantics_status": (
            "VERIFIED"
            if settlement_confidence >= 0.75
            else "PARTIALLY_EVIDENCED"
            if settlement_confidence >= 0.35
            else "UNKNOWN"
        ),
        "not_verified_alpha": not verified_ready,
        "policy": risk_policy.as_dict(),
    }


def _route_economics(
    *,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    long_rate: dict[str, Any],
    short_rate: dict[str, Any],
    long_reference_price: float | None,
    short_reference_price: float | None,
    target_notional: float,
    policy: FundingRiskPolicy,
    long_execution_level: ExecutionEvidenceLevel,
    short_execution_level: ExecutionEvidenceLevel,
    collateral_reserve_bps: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        long_reference_price is None
        or short_reference_price is None
        or long_rate.get("rate_estimate_per_settlement") is None
        or short_rate.get("rate_estimate_per_settlement") is None
    ):
        return {
            "raw_expected_funding_usd": None,
            "conservative_expected_funding_usd": None,
            "raw_expected_net_usd": None,
            "conservative_expected_net_usd": None,
            "sizing_status": "unavailable",
            "execution_status": "UNAVAILABLE",
        }
    target = max(0.0, float(target_notional))
    quantity = min(target / long_reference_price, target / short_reference_price)
    long_notional = quantity * long_reference_price
    short_notional = quantity * short_reference_price
    long_fee = modeled_fee_rate(long_market, policy=policy, now=now)
    short_fee = modeled_fee_rate(short_market, policy=policy, now=now)
    fee_uncertainty_reserve_bps = max(
        float(long_fee.get("uncertainty_reserve_bps") or 0.0),
        float(short_fee.get("uncertainty_reserve_bps") or 0.0),
    )
    raw_fee_rate = (
        (long_fee.get("source_rate") if long_fee.get("source_rate") is not None else long_fee["fee_rate"])
        + (short_fee.get("source_rate") if short_fee.get("source_rate") is not None else short_fee["fee_rate"])
    )
    estimated_round_trip_fees = target * (long_fee["fee_rate"] + short_fee["fee_rate"]) * 2.0
    raw_round_trip_fees = target * float(raw_fee_rate) * 2.0
    long_lower = float(long_rate["rate_estimate_lower_bound"])
    long_upper = float(long_rate["rate_estimate_upper_bound"])
    short_lower = float(short_rate["rate_estimate_lower_bound"])
    short_upper = float(short_rate["rate_estimate_upper_bound"])
    raw_long = -long_notional * float(long_rate["rate_estimate_per_settlement"])
    raw_short = short_notional * float(short_rate["rate_estimate_per_settlement"])
    conservative_long = -long_notional * long_upper
    conservative_short = short_notional * short_lower
    raw_funding = raw_long + raw_short
    conservative_funding = conservative_long + conservative_short
    slippage_bps = max(
        _execution_slippage_bps(long_execution_level, policy),
        _execution_slippage_bps(short_execution_level, policy),
    )
    slippage_reserve = target * slippage_bps / 10_000.0
    timing_reserve = target * policy.timing_uncertainty_reserve_bps / 10_000.0
    collateral_reserve = target * collateral_reserve_bps / 10_000.0
    conservative_costs = (
        estimated_round_trip_fees
        + slippage_reserve
        + timing_reserve
        + collateral_reserve
    )
    raw_costs = raw_round_trip_fees
    execution_status = (
        "SIMULATED_APPROXIMATE"
        if ExecutionEvidenceLevel.REFERENCE_PRICE_ONLY in {long_execution_level, short_execution_level}
        else "SIMULATED_WITH_DEPTH"
    )
    sizing_status = (
        "verified"
        if positive_float(long_market.get("quantity_step")) is not None
        and positive_float(short_market.get("quantity_step")) is not None
        and long_market.get("min_quantity_taker_applicable") is not False
        and short_market.get("min_quantity_taker_applicable") is not False
        and (
            positive_float(long_market.get("min_notional_usd")) is not None
            or positive_float(long_market.get("min_notional")) is not None
        )
        and (
            positive_float(short_market.get("min_notional_usd")) is not None
            or positive_float(short_market.get("min_notional")) is not None
        )
        and long_market.get("min_notional_taker_applicable") is not False
        and short_market.get("min_notional_taker_applicable") is not False
        else "provisional"
    )
    return {
        "quantity": quantity,
        "long_reference_price": long_reference_price,
        "short_reference_price": short_reference_price,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "raw_expected_funding_usd": raw_funding,
        "conservative_expected_funding_usd": conservative_funding,
        "raw_expected_net_usd": raw_funding - raw_costs,
        "conservative_expected_net_usd": conservative_funding - conservative_costs,
        "raw_expected_costs_usd": raw_costs,
        "conservative_expected_costs_usd": conservative_costs,
        "estimated_round_trip_fees_usd": estimated_round_trip_fees,
        "raw_round_trip_fees_usd": raw_round_trip_fees,
        "slippage_reserve_usd": slippage_reserve,
        "timing_uncertainty_reserve_usd": timing_reserve,
        "collateral_reserve_usd": collateral_reserve,
        "collateral_reserve_bps": collateral_reserve_bps,
        "execution_slippage_reserve_bps": slippage_bps,
        "sizing_status": sizing_status,
        "execution_status": execution_status,
        "modeled_fee_rates": {"long": long_fee, "short": short_fee},
        "uncertainty_reserves": {
            "slippage_reserve_usd": slippage_reserve,
            "timing_uncertainty_reserve_usd": timing_reserve,
            "collateral_reserve_usd": collateral_reserve,
            "fee_uncertainty_reserve_bps": fee_uncertainty_reserve_bps,
        },
    }


def _verified_blockers(
    *,
    risk_flags: list[str],
    missing: list[str],
    economics: dict[str, Any],
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    long_rate: dict[str, Any],
    short_rate: dict[str, Any],
    execution_levels: list[ExecutionEvidenceLevel],
    settlement_confidence: float,
    accounting_confidence: float,
    now: datetime | None = None,
) -> list[str]:
    blockers: list[str] = []
    if float(economics.get("conservative_expected_net_usd") or 0.0) <= 0.0:
        blockers.append("conservative_net_not_positive")
    if not bool(long_rate.get("exact_next_rate_available")):
        blockers.append("long_exact_next_rate_missing")
    if not bool(short_rate.get("exact_next_rate_available")):
        blockers.append("short_exact_next_rate_missing")
    if min(float(long_rate.get("rate_estimate_confidence") or 0.0), float(short_rate.get("rate_estimate_confidence") or 0.0)) < 0.80:
        blockers.append("rate_confidence_below_verified_threshold")
    if any(level not in {ExecutionEvidenceLevel.FULL_DEPTH, ExecutionEvidenceLevel.VERIFIED_SIMULATED_IOC} for level in execution_levels):
        blockers.append("verified_ioc_execution_evidence_missing")
    if settlement_confidence < 0.75:
        blockers.append("settlement_semantics_not_verified")
    if accounting_confidence < 0.75:
        blockers.append("settlement_accounting_not_verified")
    for side, market in (("long", long_market), ("short", short_market)):
        if market.get("paper_enabled") is not True:
            blockers.append(f"{side}_paper_enabled_false")
        if market.get("live_enabled") is True:
            blockers.append("unexpected_live_enabled_in_paper_runtime")
        if market.get("environment_verified") is not True:
            blockers.append(f"{side}_environment_unverified")
        environment = str(market.get("environment") or "").strip().lower()
        if environment not in {"mainnet", "testnet"}:
            blockers.append(f"{side}_environment_unknown")
        if not str(market.get("endpoint_base_url") or "").strip():
            blockers.append(f"{side}_endpoint_base_url_missing")
        provenance = str(market.get("endpoint_identity_provenance") or "").strip().lower()
        if provenance in {"", "unknown", "unverified", "unverified_client_endpoint"}:
            blockers.append(f"{side}_endpoint_identity_unverified")
        api_product = str(
            market.get("api_product_type")
            or market.get("product_type")
            or market.get("market_type")
            or market.get("contract_type")
            or ""
        ).strip().lower()
        if api_product in {"", "unknown"}:
            blockers.append(f"{side}_product_type_unverified")
        if positive_float(market.get("quantity_step")) is None:
            blockers.append(f"{side}_quantity_step_missing")
        if market.get("min_quantity_taker_applicable") is False:
            blockers.append(f"{side}_taker_min_quantity_missing")
        if positive_float(market.get("min_notional_usd")) is None and positive_float(market.get("min_notional")) is None:
            blockers.append(f"{side}_min_notional_missing")
        elif market.get("min_notional_taker_applicable") is False:
            blockers.append(f"{side}_taker_min_notional_missing")
        if not bool(modeled_fee_rate(market, now=now)["verified"]):
            blockers.append(f"{side}_fee_evidence_unverified")
    for flag in risk_flags:
        if flag.endswith("collateral_other_dollar_stable") or flag.endswith("collateral_usdt0_risk"):
            blockers.append("collateral_accounting_contract_unverified")
    return list(dict.fromkeys(blockers))
