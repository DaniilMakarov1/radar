from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.fees import (
    FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    fee_rate_value,
)
from smart_money_radar.funding.readiness_policy import (
    DEFAULT_FUNDING_RISK_POLICY,
    RATE_KIND_TO_SEMANTICS,
    normalized_rate_kind,
    attach_rate_estimate,
    optional_float as policy_optional_float,
)
from smart_money_radar.funding.settlement_contracts import (
    FUNDING_SETTLEMENT_CONTRACT_REGISTRY,
    FundingAccrualModel,
    FundingSettlementContract,
    settlement_contract_blockers,
    settlement_contract_from_market,
)

NORMALIZATION_EVIDENCE_VERSION = "sync-market-normalization-2026-07-29"
FEE_EVIDENCE_VERSION = "sync-fee-contract-2026-07-29"
FEE_CONTRACT_REVIEWED_AT = "2026-07-29T00:00:00+00:00"
_VERSIONED_FEE_SOURCE_KINDS = {
    "account_api",
    "account_fee_endpoint",
    "official_account_fee_endpoint",
    "public_fee_endpoint",
    "official_public_fee_endpoint",
    "configured_trusted_fee",
    "trusted_config",
    "versioned_trusted_config",
    "reviewed_static_schedule",
}
_VERSIONED_FEE_TRUST_STATUSES = {
    "ACCOUNT_VERIFIED",
    "CONFIGURED_TRUSTED",
    "OFFICIAL",
    "REVIEWED",
    "VERIFIED",
}


@dataclass(frozen=True)
class SynchronizedRateContract:
    observable_rate_kinds: tuple[str, ...]
    source_identifier: str
    source_kind: str = "venue_adapter_verified_contract"
    exact_next_rate_kinds: tuple[str, ...] = ()

    def registered_rate_kinds(self) -> frozenset[str]:
        return frozenset((*self.observable_rate_kinds, *self.exact_next_rate_kinds))


@dataclass(frozen=True)
class VerifiedFeeContract:
    taker_fee_rate: float | None
    source_identifier: str
    source_kind: str = "official_public_fee_endpoint"
    trust_status: str = "OFFICIAL"


SYNCHRONIZED_RATE_CONTRACTS: dict[str, SynchronizedRateContract] = {
    "binance": SynchronizedRateContract(
        observable_rate_kinds=(),
        source_identifier="premiumIndex.lastFundingRate + fundingInfo.fundingIntervalHours",
        exact_next_rate_kinds=("published_next_estimate",),
    ),
    "bybit": SynchronizedRateContract(
        observable_rate_kinds=(),
        source_identifier="v5/market/tickers.fundingRate + instruments-info.fundingInterval",
        exact_next_rate_kinds=("published_next_estimate",),
    ),
    "okx": SynchronizedRateContract(
        observable_rate_kinds=("published_current_estimate",),
        source_identifier="public/funding-rate.fundingRate + prevFundingTime/fundingTime",
    ),
    "lighter": SynchronizedRateContract(
        observable_rate_kinds=("published_8h_equivalent_normalized_hourly",),
        source_identifier="funding-rates.rate divided by published 8h period",
    ),
    "hyperliquid": SynchronizedRateContract(
        observable_rate_kinds=(
            "published_predicted_next",
            "published_current_fallback",
            "published_next_hour_prediction",
        ),
        source_identifier="predictedFundings.fundingRate or assetCtx.funding hourly fallback",
    ),
    "dydx": SynchronizedRateContract(
        observable_rate_kinds=("published_next_hour_estimate",),
        source_identifier=(
            "perpetualMarkets.nextFundingRate hourly funding estimate; "
            "exact next settlement contract unverified"
        ),
    ),
    "pacifica": SynchronizedRateContract(
        observable_rate_kinds=(
            "published_next_hour_estimate",
            "published_current_hour_estimate",
        ),
        source_identifier="info/prices next_funding or current funding hourly estimate",
    ),
    "nado": SynchronizedRateContract(
        observable_rate_kinds=(
            "published_latest_24h_x18",
            "published_predicted_24h_hourly",
        ),
        source_identifier="archive contracts funding_rate_x18 24h divided by 24",
    ),
    "risex": SynchronizedRateContract(
        observable_rate_kinds=("published_current_interval_rate",),
        source_identifier="markets.current_funding_rate per funding interval",
    ),
}


VERIFIED_TAKER_FEE_CONTRACTS: dict[str, VerifiedFeeContract] = {
    "binance": VerifiedFeeContract(
        taker_fee_rate=0.0005,
        source_identifier="https://www.binance.com/en/fee/futureFee",
    ),
    "bybit": VerifiedFeeContract(
        taker_fee_rate=0.00055,
        source_identifier="https://www.bybit.com/en/help-center/article/Trading-Fee-Structure",
    ),
    "okx": VerifiedFeeContract(
        taker_fee_rate=0.0005,
        source_identifier="https://www.okx.com/fees",
    ),
    "lighter": VerifiedFeeContract(
        taker_fee_rate=None,
        source_identifier="https://mainnet.zklighter.elliot.ai/api/v1/orderBooks",
    ),
}


def normalize_synchronized_capture_market(
    market: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Attach the synchronized-capture contract without inventing raw data.

    The only rate promotion performed here is venue-specific and versioned. If a
    venue or rate kind is not explicitly registered, the market remains
    fail-closed and must not become paper eligible from a generic funding_rate.
    """
    row = dict(market)
    venue = str(row.get("venue") or "").strip().lower()
    if not venue:
        return row
    row["venue"] = venue
    observed = _evidence_timestamp(row, observed_at)
    contract = settlement_contract_from_market(row)
    row["settlement_contract_evidence"] = _settlement_evidence(contract)
    row["settlement_contract_blockers"] = settlement_contract_blockers(
        contract,
        next_settlement_at=row.get("next_funding_at"),
    )
    row["position_inclusion_rule"] = contract.position_inclusion_rule
    row["position_inclusion_rule_verified"] = contract.position_inclusion_rule_verified
    row["settlement_accrual_model"] = contract.accrual_model
    row["settlement_verification_level"] = contract.verification_level
    row["supports_discrete_funding"] = (
        contract.accrual_model
        in {
            FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
            FundingAccrualModel.PERIODIC_INDEX_STEP.value,
        }
    )
    _merge_timing_policy(row, contract)
    if contract.funding_notional_price_source:
        row["funding_notional_price_source"] = contract.funding_notional_price_source
    _normalize_contract_identity(row)
    _normalize_next_rate(row, contract, observed)
    row.update(attach_rate_estimate(row, policy=DEFAULT_FUNDING_RISK_POLICY))
    _attach_fee_evidence(row, observed)
    return row


def _merge_timing_policy(row: dict[str, Any], contract: FundingSettlementContract) -> None:
    adapter_entry = _non_negative_float(row.get("entry_safety_buffer_seconds"))
    adapter_exit = _non_negative_float(row.get("exit_safety_buffer_seconds"))
    adapter_source = (
        str(row.get("timing_policy_source"))
        if row.get("timing_policy_source") not in (None, "")
        else None
    )
    registry_template = FUNDING_SETTLEMENT_CONTRACT_REGISTRY.get(
        (str(contract.venue).lower(), str(contract.supported_environment).lower())
    )
    registry_entry = (
        registry_template.assessment_jitter_before_seconds
        if registry_template is not None
        else None
    )
    registry_exit = (
        registry_template.assessment_jitter_after_seconds
        if registry_template is not None
        else None
    )
    if registry_entry is not None:
        row["entry_safety_buffer_seconds"] = registry_entry
        entry_source = "settlement_registry"
    elif adapter_entry is not None:
        row["entry_safety_buffer_seconds"] = adapter_entry
        entry_source = adapter_source or "venue_adapter"
    else:
        row["entry_safety_buffer_seconds"] = (
            DEFAULT_FUNDING_RISK_POLICY.default_entry_safety_buffer_seconds
        )
        entry_source = "conservative_default"
    if registry_exit is not None:
        row["exit_safety_buffer_seconds"] = registry_exit
        exit_source = "settlement_registry"
    elif adapter_exit is not None:
        row["exit_safety_buffer_seconds"] = adapter_exit
        exit_source = adapter_source or "venue_adapter"
    else:
        row["exit_safety_buffer_seconds"] = (
            DEFAULT_FUNDING_RISK_POLICY.default_exit_safety_buffer_seconds
        )
        exit_source = "conservative_default"
    if (
        "settlement_registry" in {entry_source, exit_source}
        and contract.next_settlement_source
    ):
        row["timing_policy_source"] = contract.next_settlement_source
    elif adapter_source:
        row["timing_policy_source"] = adapter_source
    elif contract.next_settlement_source:
        row["timing_policy_source"] = contract.next_settlement_source
    else:
        row["timing_policy_source"] = "conservative_default_hourly_schedule"
    row["timing_policy_provenance"] = {
        "entry_safety_buffer_seconds": row.get("entry_safety_buffer_seconds"),
        "entry_source": entry_source,
        "entry": entry_source,
        "exit_safety_buffer_seconds": row.get("exit_safety_buffer_seconds"),
        "exit_source": exit_source,
        "exit": exit_source,
        "timing_policy_source": row.get("timing_policy_source"),
    }


def _normalize_contract_identity(row: dict[str, Any]) -> None:
    if str(row.get("contract_type") or "").strip().lower() == "linear_perpetual":
        row.setdefault("contract_kind", "linear_perpetual")
    if str(row.get("contract_kind") or "").strip().lower() == "linear_perpetual":
        row["contract_kind"] = "linear_perpetual"
        row["supports_perpetuals"] = True
        row["is_linear_contract"] = True
        row.setdefault("api_product_type", "linear_perpetual")
        row.setdefault("market_type", "linear_perpetual")
        row.setdefault("product_type", "linear_perpetual")


def _normalize_next_rate(
    row: dict[str, Any],
    contract: FundingSettlementContract,
    observed: str,
) -> None:
    venue = str(row.get("venue") or "").strip().lower()
    rate_contract = SYNCHRONIZED_RATE_CONTRACTS.get(venue)
    if rate_contract is None:
        return
    kind = str(row.get("funding_rate_kind") or "").strip()
    if kind not in rate_contract.registered_rate_kinds():
        return
    semantics = RATE_KIND_TO_SEMANTICS.get(kind, "unknown")
    row["funding_rate_semantics"] = semantics
    row["funding_rate_unit"] = "fraction_of_notional_per_settlement"
    row["funding_sign_convention"] = "positive_long_pays"
    row.setdefault("raw_funding_rate", row.get("funding_rate"))
    row.setdefault("raw_funding_rate_unit", "fraction_of_notional_per_settlement")
    row["rate_estimate_kind"] = normalized_rate_kind(kind).value
    if row.get("source_event_at") in (None, ""):
        row.setdefault("source_freshness_basis", "response_time_current_snapshot_contract")
    rate = _optional_float(row.get("funding_rate"))
    interval_hours = _optional_float(row.get("funding_interval_hours"))
    if rate is None or interval_hours is None or interval_hours <= 0:
        return
    if not row.get("next_funding_at"):
        return
    row["rate_estimate_per_settlement"] = rate
    is_exact_next = kind in set(rate_contract.exact_next_rate_kinds)
    derivation = (
        contract.rate_per_settlement_derivation
        if is_exact_next
        else "observable rate estimate per settlement; not verified exact next cashflow"
    )
    row["normalization_evidence"] = {
        "evidence_version": NORMALIZATION_EVIDENCE_VERSION,
        "venue": venue,
        "environment": row.get("environment"),
        "product_type": row.get("product_type") or row.get("market_type") or "linear_perpetual",
        "source_kind": rate_contract.source_kind,
        "source_identifier": rate_contract.source_identifier,
        "funding_rate_kind": kind,
        "funding_rate_semantics": semantics,
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "rate_per_settlement_derivation": derivation,
        "settlement_contract_evidence_checked_at": contract.evidence_checked_at,
        "exact_next_rate_available": is_exact_next,
        "source_freshness_basis": row.get("source_freshness_basis"),
        "observed_at": observed,
        "reviewed_at": FEE_CONTRACT_REVIEWED_AT,
    }
    if is_exact_next:
        row["normalized_next_funding_rate"] = rate
    else:
        row.pop("normalized_next_funding_rate", None)


def _attach_fee_evidence(row: dict[str, Any], observed: str) -> None:
    venue = str(row.get("venue") or "").strip().lower()
    contract = VERIFIED_TAKER_FEE_CONTRACTS.get(venue)
    if contract is None:
        return
    row["market_observed_at"] = observed
    if _has_versioned_taker_fee_evidence(row):
        return
    if contract.taker_fee_rate is not None:
        row["taker_fee_rate"] = contract.taker_fee_rate
    rate = fee_rate_value(row, "taker")
    if rate is None:
        return
    row["fee_source"] = contract.source_kind
    row["market_observed_at"] = observed
    row["fee_observed_at"] = FEE_CONTRACT_REVIEWED_AT
    row["fee_reviewed_at"] = FEE_CONTRACT_REVIEWED_AT
    row["fee_schedule_reviewed_at"] = FEE_CONTRACT_REVIEWED_AT
    row["fee_evidence"] = {
        "fee_evidence_kind": FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
        "source_kind": contract.source_kind,
        "source_identifier": contract.source_identifier,
        "trust_status": contract.trust_status,
        "venue": venue,
        "liquidity_role": "taker",
        "market_observed_at": observed,
        "reviewed_at": FEE_CONTRACT_REVIEWED_AT,
        "fee_schedule_reviewed_at": FEE_CONTRACT_REVIEWED_AT,
        "environment": str(row.get("environment") or "mainnet").strip().lower(),
        "market_type": row.get("market_type") or row.get("product_type") or "linear_perpetual",
        "product_type": row.get("product_type") or row.get("market_type") or "linear_perpetual",
        "applicability": "taker",
        "evidence_version": FEE_EVIDENCE_VERSION,
    }


def _has_versioned_taker_fee_evidence(row: dict[str, Any]) -> bool:
    evidence = row.get("fee_evidence")
    if not isinstance(evidence, dict):
        return False
    taker = evidence.get("taker")
    if isinstance(taker, dict):
        evidence = taker
    generic = evidence.get("default") or evidence.get("generic")
    if isinstance(generic, dict):
        evidence = generic
    if not isinstance(evidence, dict):
        return False
    source_kind = str(evidence.get("source_kind") or "").strip().lower()
    trust_status = str(evidence.get("trust_status") or "").strip().upper()
    source_identifier = str(
        evidence.get("source_identifier")
        or evidence.get("source_url")
        or evidence.get("source")
        or ""
    ).strip()
    liquidity_role = str(evidence.get("liquidity_role") or evidence.get("applicability") or "taker").strip().lower()
    return (
        bool(evidence.get("evidence_version") or evidence.get("schema_version"))
        and source_kind in _VERSIONED_FEE_SOURCE_KINDS
        and trust_status in _VERSIONED_FEE_TRUST_STATUSES
        and bool(source_identifier)
        and liquidity_role in {"taker", "all"}
    )


def _settlement_evidence(contract: FundingSettlementContract) -> dict[str, Any]:
    return {
        "venue": contract.venue,
        "environment": contract.supported_environment,
        "accrual_model": contract.accrual_model,
        "position_inclusion_rule": contract.position_inclusion_rule,
        "position_inclusion_rule_verified": contract.position_inclusion_rule_verified,
        "settlement_interval_seconds": contract.settlement_interval_seconds,
        "settlement_interval_dynamic": contract.settlement_interval_dynamic,
        "displayed_rate_period_seconds": contract.displayed_rate_period_seconds,
        "next_settlement_source": contract.next_settlement_source,
        "rate_per_settlement_derivation": contract.rate_per_settlement_derivation,
        "assessment_jitter_before_seconds": contract.assessment_jitter_before_seconds,
        "assessment_jitter_after_seconds": contract.assessment_jitter_after_seconds,
        "settlement_confirmation_source": contract.settlement_confirmation_source,
        "realized_payment_source": contract.realized_payment_source,
        "official_evidence_urls": list(contract.official_evidence_urls),
        "evidence_checked_at": contract.evidence_checked_at,
        "verification_level": contract.verification_level,
    }


def _evidence_timestamp(row: dict[str, Any], observed_at: str | None) -> str:
    for key in ("response_received_at", "observed_at", "normalized_at"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    if observed_at:
        return str(observed_at)
    return datetime.now(UTC).isoformat()


def _non_negative_float(value: Any) -> float | None:
    parsed = policy_optional_float(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
