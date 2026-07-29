from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.fees import fee_rate_value
from smart_money_radar.funding.settlement_contracts import (
    FundingAccrualModel,
    FundingSettlementContract,
    settlement_contract_blockers,
    settlement_contract_from_market,
)

NORMALIZATION_EVIDENCE_VERSION = "sync-market-normalization-2026-07-29"
FEE_EVIDENCE_VERSION = "sync-fee-contract-2026-07-29"
FEE_CONTRACT_REVIEWED_AT = "2026-07-29T00:00:00+00:00"


@dataclass(frozen=True)
class SynchronizedRateContract:
    allowed_rate_kinds: tuple[str, ...]
    source_identifier: str
    source_kind: str = "venue_adapter_verified_contract"


@dataclass(frozen=True)
class VerifiedFeeContract:
    taker_fee_rate: float | None
    source_identifier: str
    source_kind: str = "official_public_fee_endpoint"
    trust_status: str = "OFFICIAL"


SYNCHRONIZED_RATE_CONTRACTS: dict[str, SynchronizedRateContract] = {
    "binance": SynchronizedRateContract(
        allowed_rate_kinds=("published_next_estimate",),
        source_identifier="premiumIndex.lastFundingRate + fundingInfo.fundingIntervalHours",
    ),
    "bybit": SynchronizedRateContract(
        allowed_rate_kinds=("published_next_estimate",),
        source_identifier="v5/market/tickers.fundingRate + instruments-info.fundingInterval",
    ),
    "okx": SynchronizedRateContract(
        allowed_rate_kinds=("published_current_estimate",),
        source_identifier="public/funding-rate.fundingRate + prevFundingTime/fundingTime",
    ),
    "lighter": SynchronizedRateContract(
        allowed_rate_kinds=("published_8h_equivalent_normalized_hourly",),
        source_identifier="funding-rates.rate divided by published 8h period",
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
    row["entry_safety_buffer_seconds"] = contract.assessment_jitter_before_seconds
    row["exit_safety_buffer_seconds"] = contract.assessment_jitter_after_seconds
    if contract.next_settlement_source:
        row["timing_policy_source"] = contract.next_settlement_source
    if contract.funding_notional_price_source:
        row["funding_notional_price_source"] = contract.funding_notional_price_source
    _normalize_contract_identity(row)
    _normalize_next_rate(row, contract, observed)
    _attach_fee_evidence(row, observed)
    return row


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
    if kind not in set(rate_contract.allowed_rate_kinds):
        return
    rate = _optional_float(row.get("funding_rate"))
    interval_hours = _optional_float(row.get("funding_interval_hours"))
    if rate is None or interval_hours is None or interval_hours <= 0:
        return
    if not row.get("next_funding_at"):
        return
    row["funding_rate_semantics"] = "next_settlement"
    row["funding_rate_unit"] = "fraction_of_notional_per_settlement"
    row["funding_sign_convention"] = "positive_long_pays"
    row.setdefault("raw_funding_rate", row.get("funding_rate"))
    row.setdefault("raw_funding_rate_unit", "fraction_of_notional_per_settlement")
    row["normalized_next_funding_rate"] = rate
    row["normalization_evidence"] = {
        "evidence_version": NORMALIZATION_EVIDENCE_VERSION,
        "venue": venue,
        "environment": row.get("environment"),
        "product_type": row.get("product_type") or row.get("market_type") or "linear_perpetual",
        "source_kind": rate_contract.source_kind,
        "source_identifier": rate_contract.source_identifier,
        "funding_rate_kind": kind,
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "rate_per_settlement_derivation": contract.rate_per_settlement_derivation,
        "settlement_contract_evidence_checked_at": contract.evidence_checked_at,
        "observed_at": observed,
        "reviewed_at": FEE_CONTRACT_REVIEWED_AT,
    }


def _attach_fee_evidence(row: dict[str, Any], observed: str) -> None:
    venue = str(row.get("venue") or "").strip().lower()
    contract = VERIFIED_TAKER_FEE_CONTRACTS.get(venue)
    if contract is None:
        return
    if contract.taker_fee_rate is not None:
        row["taker_fee_rate"] = contract.taker_fee_rate
    rate = fee_rate_value(row, "taker")
    if rate is None:
        return
    row["fee_source"] = contract.source_kind
    row["fee_observed_at"] = observed
    row["fee_reviewed_at"] = FEE_CONTRACT_REVIEWED_AT
    row["fee_evidence"] = {
        "source_kind": contract.source_kind,
        "source_identifier": contract.source_identifier,
        "trust_status": contract.trust_status,
        "venue": venue,
        "liquidity_role": "taker",
        "observed_at": observed,
        "reviewed_at": FEE_CONTRACT_REVIEWED_AT,
        "environment": str(row.get("environment") or "mainnet").strip().lower(),
        "market_type": row.get("market_type") or row.get("product_type") or "linear_perpetual",
        "product_type": row.get("product_type") or row.get("market_type") or "linear_perpetual",
        "applicability": "taker",
        "evidence_version": FEE_EVIDENCE_VERSION,
    }


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


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
