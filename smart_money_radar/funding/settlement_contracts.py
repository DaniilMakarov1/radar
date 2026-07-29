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
        official_evidence_urls=("https://www.rise.trade/en",),
        evidence_checked_at=_checked_at(),
        verification_level=FundingSemanticsVerificationLevel.UNVERIFIED.value,
        notes="No official mainnet API endpoint is declared in this repository.",
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
    accrual_model = str(
        market.get("funding_accrual_model")
        or market.get("accrual_model")
        or template.accrual_model
        or FundingAccrualModel.UNKNOWN.value
    ).strip().upper()
    if accrual_model not in {item.value for item in FundingAccrualModel}:
        accrual_model = FundingAccrualModel.UNKNOWN.value
    verification_level = str(
        market.get("settlement_semantics_status")
        or market.get("verification_level")
        or template.verification_level
        or FundingSemanticsVerificationLevel.UNVERIFIED.value
    ).strip().upper()
    if verification_level not in {item.value for item in FundingSemanticsVerificationLevel}:
        verification_level = FundingSemanticsVerificationLevel.UNVERIFIED.value
    urls = market.get("official_evidence_urls")
    if isinstance(urls, str):
        official_urls = tuple(url.strip() for url in urls.split(",") if url.strip())
    elif isinstance(urls, (list, tuple)):
        official_urls = tuple(str(url) for url in urls if str(url).strip())
    else:
        official_urls = template.official_evidence_urls
    if environment is not None and not exact_registry_match and environment != template.supported_environment:
        accrual_model = FundingAccrualModel.UNKNOWN.value
        verification_level = FundingSemanticsVerificationLevel.UNVERIFIED.value
        forced_position_verified = False
        forced_jitter_before = None
        forced_jitter_after = None
        forced_confirmation_source = None
        forced_realized_source = None
        forced_rate_derivation = None
    else:
        forced_position_verified = None
        forced_jitter_before = "template"
        forced_jitter_after = "template"
        forced_confirmation_source = "template"
        forced_realized_source = "template"
        forced_rate_derivation = "template"
    return replace(
        template,
        venue=venue or template.venue,
        supported_environment=environment or template.supported_environment,
        base_url=(
            str(market.get("base_url"))
            if market.get("base_url") not in (None, "")
            else template.base_url
        ),
        accrual_model=accrual_model,
        position_inclusion_rule=(
            str(market.get("position_inclusion_rule"))
            if market.get("position_inclusion_rule") not in (None, "")
            else template.position_inclusion_rule
        ),
        position_inclusion_rule_verified=_bool_from_market(
            market.get("position_inclusion_rule_verified"),
            template.position_inclusion_rule_verified
            if forced_position_verified is None
            else forced_position_verified,
        ),
        settlement_interval_seconds=interval_seconds,
        settlement_interval_dynamic=bool(
            market.get(
                "settlement_interval_dynamic",
                template.settlement_interval_dynamic,
            )
        ),
        displayed_rate_period_seconds=displayed_seconds,
        next_settlement_source=(
            str(market.get("next_settlement_source"))
            if market.get("next_settlement_source") not in (None, "")
            else template.next_settlement_source
        ),
        rate_per_settlement_derivation=(
            str(market.get("rate_per_settlement_derivation"))
            if market.get("rate_per_settlement_derivation") not in (None, "")
            else (
                template.rate_per_settlement_derivation
                if forced_rate_derivation == "template"
                else forced_rate_derivation
            )
        ),
        funding_notional_price_source=(
            str(market.get("funding_notional_price_source"))
            if market.get("funding_notional_price_source") not in (None, "")
            else template.funding_notional_price_source
        ),
        assessment_jitter_before_seconds=_optional_non_negative_float(
            market.get("assessment_jitter_before_seconds")
        )
        if market.get("assessment_jitter_before_seconds") is not None
        else (
            template.assessment_jitter_before_seconds
            if forced_jitter_before == "template"
            else forced_jitter_before
        ),
        assessment_jitter_after_seconds=_optional_non_negative_float(
            market.get("assessment_jitter_after_seconds")
        )
        if market.get("assessment_jitter_after_seconds") is not None
        else (
            template.assessment_jitter_after_seconds
            if forced_jitter_after == "template"
            else forced_jitter_after
        ),
        settlement_confirmation_source=(
            str(market.get("settlement_confirmation_source"))
            if market.get("settlement_confirmation_source") not in (None, "")
            else (
                template.settlement_confirmation_source
                if forced_confirmation_source == "template"
                else forced_confirmation_source
            )
        ),
        realized_payment_source=(
            str(market.get("realized_payment_source"))
            if market.get("realized_payment_source") not in (None, "")
            else (
                template.realized_payment_source
                if forced_realized_source == "template"
                else forced_realized_source
            )
        ),
        official_evidence_urls=official_urls,
        evidence_checked_at=str(
            market.get("evidence_checked_at") or template.evidence_checked_at
        ),
        verification_level=verification_level,
        notes=str(market.get("semantics_notes") or template.notes or ""),
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
