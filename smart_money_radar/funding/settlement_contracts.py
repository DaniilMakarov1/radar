from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class FundingAccrualModel(str, Enum):
    POSITION_AT_EVENT_FULL = "POSITION_AT_EVENT_FULL"
    PERIODIC_INDEX_STEP = "PERIODIC_INDEX_STEP"
    CONTINUOUS_PRO_RATA = "CONTINUOUS_PRO_RATA"
    UNKNOWN = "UNKNOWN"


class FundingSemanticsVerificationLevel(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    PUBLIC_OBSERVED = "PUBLIC_OBSERVED"
    TESTNET_CANARY_OBSERVED = "TESTNET_CANARY_OBSERVED"
    TESTNET_SNAPSHOT_SUPPORTED = "TESTNET_SNAPSHOT_SUPPORTED"
    MAINNET_CANARY_REQUIRED = "MAINNET_CANARY_REQUIRED"
    VERIFIED = "VERIFIED"


ELIGIBLE_ACCRUAL_MODELS = {
    FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
    FundingAccrualModel.PERIODIC_INDEX_STEP.value,
}


SETTLEMENT_SEMANTICS_BLOCKERS = {
    "funding_accrual_model_unknown",
    "funding_continuous_pro_rata",
    "position_inclusion_rule_missing",
    "position_inclusion_rule_unverified",
    "next_settlement_time_missing",
    "settlement_interval_unknown",
    "displayed_rate_period_unknown",
    "rate_per_settlement_unknown",
    "settlement_timing_uncertainty_unknown",
    "settlement_confirmation_unavailable",
    "environment_unverified",
}


@dataclass(frozen=True)
class FundingSettlementContract:
    venue: str
    supported_environment: str
    base_url: str | None
    accrual_model: str
    position_inclusion_rule: str | None
    position_inclusion_rule_verified: bool
    settlement_interval_seconds: float | None
    settlement_interval_dynamic: bool
    displayed_rate_period_seconds: float | None
    next_settlement_source: str | None
    rate_per_settlement_derivation: str | None
    funding_notional_price_source: str | None
    assessment_jitter_before_seconds: float | None
    assessment_jitter_after_seconds: float | None
    settlement_confirmation_source: str | None
    realized_payment_source: str | None
    official_evidence_urls: tuple[str, ...]
    evidence_checked_at: str
    verification_level: str
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["official_evidence_urls"] = list(self.official_evidence_urls)
        return payload


def _checked_at() -> str:
    return "2026-07-29"


FUNDING_SETTLEMENT_CONTRACT_REGISTRY: dict[tuple[str, str], FundingSettlementContract] = {
    ("risex", "testnet"): FundingSettlementContract(
        venue="risex",
        supported_environment="testnet",
        base_url="https://api.testnet.rise.trade",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule="perp_position_at_settlement",
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=28_800.0,
        next_settlement_source="v1/markets.next_funding_time",
        rate_per_settlement_derivation="current_funding_rate from public market payload; semantics unverified",
        funding_notional_price_source="mark_or_index_public_market_payload",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=("https://www.rise.trade/en", "https://testnet.rise.trade/en/trade/BTC-PERP"),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes=(
            "RiseX is mandatory for monitoring and probe work. Candidate "
            "eligibility remains fail-closed until canary observations prove "
            "snapshot/full or periodic-step settlement mechanics."
        ),
    ),
    ("risex", "mainnet"): FundingSettlementContract(
        venue="risex",
        supported_environment="mainnet",
        base_url="https://api.rise.trade",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule="perp_position_at_settlement",
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=28_800.0,
        next_settlement_source="v1/markets.next_funding_time",
        rate_per_settlement_derivation="current_funding_rate from public market payload; semantics unverified",
        funding_notional_price_source="mark_or_index_public_market_payload",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=(
            "https://www.rise.trade/en",
            "https://developer.rise.trade/reference/general-information",
        ),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes=(
            "Mainnet public markets endpoint is declared for monitoring. Candidate "
            "eligibility remains fail-closed until mainnet canary observations prove "
            "snapshot/full or periodic-step settlement mechanics."
        ),
    ),
    ("pacifica", "mainnet"): FundingSettlementContract(
        venue="pacifica",
        supported_environment="mainnet",
        base_url="https://api.pacifica.fi/api/v1",
        accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule="open_position_during_hourly_funding_epoch",
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="derived_next_utc_hour_from_hourly_funding_docs",
        rate_per_settlement_derivation="info/prices.next_funding is next hourly epoch estimate",
        funding_notional_price_source="info/prices.mark_or_oracle",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source="public funding_rate/history and account funding/history",
        realized_payment_source="account funding/history",
        official_evidence_urls=(
            "https://docs.pacifica.fi/trading-on-pacifica/funding-rates",
            "https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-prices",
            "https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-historical-funding",
            "https://docs.pacifica.fi/api-documentation/api/rest-api/account/get-funding-history",
        ),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.MAINNET_CANARY_REQUIRED.value,
        notes=(
            "Official docs expose hourly predicted/current funding and history. "
            "Paper/shadow-candidate promotion remains blocked until reviewed mainnet canary."
        ),
    ),
    ("nado", "mainnet"): FundingSettlementContract(
        venue="nado",
        supported_environment="mainnet",
        base_url="https://gateway.prod.nado.xyz/v2",
        accrual_model=FundingAccrualModel.PERIODIC_INDEX_STEP.value,
        position_inclusion_rule="open_position_during_hourly_funding_step",
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=86_400.0,
        next_settlement_source="archive_v2_contracts.next_funding_rate_timestamp",
        rate_per_settlement_derivation="funding_rate_x18 24h predicted rate divided by 24",
        funding_notional_price_source="archive_v2_contracts.mark_price/index_price",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source="archive funding_rate_history hourly tick",
        realized_payment_source="archive interest_and_funding_payments",
        official_evidence_urls=(
            "https://docs.nado.xyz/core/funding-rates",
            "https://docs.nado.xyz/developer-resources/api/v2/contracts",
            "https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate",
            "https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate-history",
        ),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.MAINNET_CANARY_REQUIRED.value,
        notes=(
            "Official docs state hourly settlements and x18 24h/current funding. "
            "Position inclusion and exact capture eligibility remain unverified."
        ),
    ),
    ("paradex", "mainnet"): FundingSettlementContract(
        venue="paradex",
        supported_environment="mainnet",
        base_url="https://api.prod.paradex.trade",
        accrual_model=FundingAccrualModel.CONTINUOUS_PRO_RATA.value,
        position_inclusion_rule="continuous_time_weighted_position",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="projected hourly funding data",
        rate_per_settlement_derivation="continuous/pro-rata funding accrual, not full event snapshot capture",
        funding_notional_price_source="protocol_oracle_mark",
        assessment_jitter_before_seconds=0.0,
        assessment_jitter_after_seconds=0.0,
        settlement_confirmation_source="account funding payment history",
        realized_payment_source="account funding payment history",
        official_evidence_urls=("https://docs.paradex.trade/risk/funding-mechanism",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes="Excluded from short-hold settlement capture; keep adapter for general analytics.",
    ),
    ("hyperliquid", "mainnet"): FundingSettlementContract(
        venue="hyperliquid",
        supported_environment="mainnet",
        base_url="https://api.hyperliquid.xyz",
        accrual_model=FundingAccrualModel.PERIODIC_INDEX_STEP.value,
        position_inclusion_rule="position_during_hourly_funding_step",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="predictedFunding/fundingHistory time",
        rate_per_settlement_derivation="hourly funding rate",
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="funding history/account ledger",
        realized_payment_source="account ledger",
        official_evidence_urls=("https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes="Eligible for shadow settlement capture; paper/live execution remains separately gated.",
    ),
    ("extended", "mainnet"): FundingSettlementContract(
        venue="extended",
        supported_environment="mainnet",
        base_url="https://api.extended.exchange",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule=None,
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="current-hour funding payload",
        rate_per_settlement_derivation=None,
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=("https://docs.extended.exchange/extended-resources/trading/funding-payments",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
        notes="Public timestamps can be observed; snapshot inclusion rule is not verified.",
    ),
    ("edgex", "mainnet"): FundingSettlementContract(
        venue="edgex",
        supported_environment="mainnet",
        base_url="https://pro.edgex.exchange",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule=None,
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="next_funding_time public payload",
        rate_per_settlement_derivation=None,
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=("https://edgex-1.gitbook.io/edgex-documentation/trading/funding-fees",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
    ),
    ("ethereal", "mainnet"): FundingSettlementContract(
        venue="ethereal",
        supported_environment="mainnet",
        base_url="https://api.ethereal.trade",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule=None,
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="projected hourly funding payload",
        rate_per_settlement_derivation=None,
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=("https://docs.ethereal.trade/trading/perpetual-futures/funding-rates",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
    ),
    ("grvt", "mainnet"): FundingSettlementContract(
        venue="grvt",
        supported_environment="mainnet",
        base_url="https://api.grvt.io",
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule=None,
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="current interval funding payload",
        rate_per_settlement_derivation=None,
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=("https://api-docs.grvt.io/market_data_api/",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
    ),
    ("lighter", "mainnet"): FundingSettlementContract(
        venue="lighter",
        supported_environment="mainnet",
        base_url="https://mainnet.zklighter.elliot.ai",
        accrual_model=FundingAccrualModel.PERIODIC_INDEX_STEP.value,
        position_inclusion_rule="position_during_hourly_funding_step",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=28_800.0,
        next_settlement_source="current hourly funding estimate",
        rate_per_settlement_derivation="adapter converts 8h equivalent to hourly settlement rate",
        funding_notional_price_source="mark_price",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="funding history/account ledger",
        realized_payment_source="account ledger",
        official_evidence_urls=("https://docs.lighter.xyz/trading/funding", "https://apidocs.lighter.xyz/reference/funding-rates"),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
    ),
    ("dydx", "mainnet"): FundingSettlementContract(
        venue="dydx",
        supported_environment="mainnet",
        base_url="https://indexer.dydx.trade",
        accrual_model=FundingAccrualModel.PERIODIC_INDEX_STEP.value,
        position_inclusion_rule="position_during_hourly_funding_step",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=3600.0,
        settlement_interval_dynamic=False,
        displayed_rate_period_seconds=3600.0,
        next_settlement_source="perpetual market next funding",
        rate_per_settlement_derivation="hourly funding rate",
        funding_notional_price_source="oracle_price",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="subaccount transfers/fills ledger",
        realized_payment_source="account ledger",
        official_evidence_urls=("https://docs.dydx.xyz/",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
    ),
    ("binance", "mainnet"): FundingSettlementContract(
        venue="binance",
        supported_environment="mainnet",
        base_url="https://fapi.binance.com",
        accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule="perp_position_at_funding_time",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=None,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=None,
        next_settlement_source="premiumIndex.nextFundingTime",
        rate_per_settlement_derivation="lastFundingRate is the estimated next settlement rate",
        funding_notional_price_source="markPrice",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="income history FUNDING_FEE",
        realized_payment_source="account income history",
        official_evidence_urls=("https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes="Settlement interval is symbol/config dependent and must come from market data.",
    ),
    ("bybit", "mainnet"): FundingSettlementContract(
        venue="bybit",
        supported_environment="mainnet",
        base_url="https://api.bybit.com",
        accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule="perp_position_at_funding_timestamp",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=None,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=None,
        next_settlement_source="tickers.nextFundingTime",
        rate_per_settlement_derivation="fundingRate is the estimated next settlement rate",
        funding_notional_price_source="markPrice",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="transaction log funding",
        realized_payment_source="account transaction log",
        official_evidence_urls=("https://bybit-exchange.github.io/docs/v5/market/tickers",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes="Funding interval can vary by symbol; adapter market data must provide it.",
    ),
    ("okx", "mainnet"): FundingSettlementContract(
        venue="okx",
        supported_environment="mainnet",
        base_url="https://www.okx.com",
        accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule="swap_position_at_funding_time",
        position_inclusion_rule_verified=True,
        settlement_interval_seconds=None,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=None,
        next_settlement_source="public funding-rate.nextFundingTime",
        rate_per_settlement_derivation="fundingRate is the current next settlement rate",
        funding_notional_price_source="markPx",
        assessment_jitter_before_seconds=2.0,
        assessment_jitter_after_seconds=2.0,
        settlement_confirmation_source="bills funding fee",
        realized_payment_source="account bills",
        official_evidence_urls=("https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.PUBLIC_OBSERVED.value,
        notes="Funding interval can vary; adapter market data must provide the interval.",
    ),
}


def normalized_environment_or_none(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"mainnet", "testnet"} else None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _bool_from_market(value: Any, fallback: bool) -> bool:
    if value is None:
        return bool(fallback)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "verified"}:
        return True
    if text in {"0", "false", "no", "unverified"}:
        return False
    return bool(fallback)


def _contract_template(venue: str, environment: str | None) -> FundingSettlementContract:
    key = (venue, environment or "mainnet")
    if key in FUNDING_SETTLEMENT_CONTRACT_REGISTRY:
        return FUNDING_SETTLEMENT_CONTRACT_REGISTRY[key]
    fallback_key = (venue, "mainnet")
    if fallback_key in FUNDING_SETTLEMENT_CONTRACT_REGISTRY:
        return FUNDING_SETTLEMENT_CONTRACT_REGISTRY[fallback_key]
    return FundingSettlementContract(
        venue=venue or "unknown",
        supported_environment=environment or "unknown",
        base_url=None,
        accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule=None,
        position_inclusion_rule_verified=False,
        settlement_interval_seconds=None,
        settlement_interval_dynamic=True,
        displayed_rate_period_seconds=None,
        next_settlement_source=None,
        rate_per_settlement_derivation=None,
        funding_notional_price_source=None,
        assessment_jitter_before_seconds=None,
        assessment_jitter_after_seconds=None,
        settlement_confirmation_source=None,
        realized_payment_source=None,
        official_evidence_urls=(),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
        notes="No funding settlement contract registered.",
    )


def settlement_contract_from_market(market: dict[str, Any]) -> FundingSettlementContract:
    venue = str(market.get("venue") or "").strip().lower()
    environment = normalized_environment_or_none(market.get("environment"))
    exact_registry_match = (venue, environment or "mainnet") in FUNDING_SETTLEMENT_CONTRACT_REGISTRY
    template = _contract_template(venue, environment)
    interval_seconds = _optional_float(market.get("settlement_interval_seconds"))
    if interval_seconds is None:
        interval_hours = _optional_float(market.get("funding_interval_hours"))
        interval_seconds = interval_hours * 3600.0 if interval_hours is not None else None
    if interval_seconds is None:
        interval_seconds = template.settlement_interval_seconds
    displayed_seconds = _optional_float(market.get("displayed_rate_period_seconds"))
    if displayed_seconds is None:
        displayed_hours = _optional_float(market.get("published_funding_interval_hours"))
        displayed_seconds = displayed_hours * 3600.0 if displayed_hours is not None else None
    if displayed_seconds is None:
        displayed_seconds = template.displayed_rate_period_seconds or interval_seconds
    accrual_model = template.accrual_model or FundingAccrualModel.UNKNOWN.value
    verification_level = (
        template.verification_level
        or FundingSemanticsVerificationLevel.UNVERIFIED.value
    )
    official_urls = template.official_evidence_urls
    position_inclusion_rule = template.position_inclusion_rule
    position_inclusion_rule_verified = template.position_inclusion_rule_verified
    assessment_jitter_before_seconds = template.assessment_jitter_before_seconds
    assessment_jitter_after_seconds = template.assessment_jitter_after_seconds
    settlement_confirmation_source = template.settlement_confirmation_source
    realized_payment_source = template.realized_payment_source
    base_url = template.base_url
    if environment is not None and not exact_registry_match and environment != template.supported_environment:
        accrual_model = FundingAccrualModel.UNKNOWN.value
        verification_level = FundingSemanticsVerificationLevel.UNVERIFIED.value
        official_urls = ()
        position_inclusion_rule = None
        position_inclusion_rule_verified = False
        assessment_jitter_before_seconds = None
        assessment_jitter_after_seconds = None
        settlement_confirmation_source = None
        realized_payment_source = None
        base_url = None
        rate_derivation_fallback = None
    else:
        rate_derivation_fallback = template.rate_per_settlement_derivation
    return replace(
        template,
        venue=venue or template.venue,
        supported_environment=environment or template.supported_environment,
        base_url=base_url,
        accrual_model=accrual_model,
        position_inclusion_rule=position_inclusion_rule,
        position_inclusion_rule_verified=position_inclusion_rule_verified,
        settlement_interval_seconds=interval_seconds,
        settlement_interval_dynamic=template.settlement_interval_dynamic,
        displayed_rate_period_seconds=displayed_seconds,
        next_settlement_source=(
            str(market.get("next_settlement_source"))
            if market.get("next_settlement_source") not in (None, "")
            else template.next_settlement_source
        ),
        rate_per_settlement_derivation=rate_derivation_fallback,
        funding_notional_price_source=(
            str(market.get("funding_notional_price_source"))
            if market.get("funding_notional_price_source") not in (None, "")
            else template.funding_notional_price_source
        ),
        assessment_jitter_before_seconds=assessment_jitter_before_seconds,
        assessment_jitter_after_seconds=assessment_jitter_after_seconds,
        settlement_confirmation_source=settlement_confirmation_source,
        realized_payment_source=realized_payment_source,
        official_evidence_urls=official_urls,
        evidence_checked_at=template.evidence_checked_at,
        verification_level=verification_level,
        notes=str(template.notes or ""),
    )


def settlement_contract_blockers(
    contract: FundingSettlementContract,
    *,
    next_settlement_at: Any,
) -> list[str]:
    blockers: list[str] = []
    if normalized_environment_or_none(contract.supported_environment) is None:
        blockers.append("environment_unverified")
    if not next_settlement_at:
        blockers.append("next_settlement_time_missing")
    if contract.accrual_model == FundingAccrualModel.UNKNOWN.value:
        blockers.append("funding_accrual_model_unknown")
    if contract.accrual_model == FundingAccrualModel.CONTINUOUS_PRO_RATA.value:
        blockers.append("funding_continuous_pro_rata")
    if contract.accrual_model not in ELIGIBLE_ACCRUAL_MODELS:
        if "funding_accrual_model_unknown" not in blockers and "funding_continuous_pro_rata" not in blockers:
            blockers.append("funding_accrual_model_unknown")
    if not contract.position_inclusion_rule:
        blockers.append("position_inclusion_rule_missing")
    elif not contract.position_inclusion_rule_verified:
        blockers.append("position_inclusion_rule_unverified")
    if contract.settlement_interval_seconds is None:
        blockers.append("settlement_interval_unknown")
    if contract.displayed_rate_period_seconds is None:
        blockers.append("displayed_rate_period_unknown")
    if not contract.rate_per_settlement_derivation:
        blockers.append("rate_per_settlement_unknown")
    if (
        contract.assessment_jitter_before_seconds is None
        or contract.assessment_jitter_after_seconds is None
    ):
        blockers.append("settlement_timing_uncertainty_unknown")
    if not contract.settlement_confirmation_source:
        blockers.append("settlement_confirmation_unavailable")
    return list(dict.fromkeys(blockers))


def settlement_contract_strategy_eligible(
    contract: FundingSettlementContract,
    *,
    next_settlement_at: Any,
) -> bool:
    return not settlement_contract_blockers(
        contract,
        next_settlement_at=next_settlement_at,
    )


def settlement_contract_registry_rows() -> list[dict[str, Any]]:
    rows = [contract.as_dict() for contract in FUNDING_SETTLEMENT_CONTRACT_REGISTRY.values()]
    return sorted(rows, key=lambda row: (row["venue"], row["supported_environment"]))


def parse_contract_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
