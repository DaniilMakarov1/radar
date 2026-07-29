from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES


PRIMARY_SHADOW_VENUES: tuple[str, ...] = (
    "risex",
    "hyperliquid",
    "paradex",
    "extended",
    "edgex",
    "ethereal",
    "grvt",
    "lighter",
    "dydx",
    "binance",
    "bybit",
    "okx",
)

USD_MAJOR_STABLE = "USD_MAJOR_STABLE"

ADAPTER_STATUSES: tuple[str, ...] = (
    "PAPER_ELIGIBLE",
    "SHADOW_ELIGIBLE",
    "RESEARCH_ONLY",
    "DEACTIVATED",
    "UNAVAILABLE",
)


@dataclass(frozen=True)
class FundingMarketRules:
    quantity_step: float | None = None
    min_quantity: float | None = None
    min_notional: float | None = None
    contract_multiplier: float | None = None
    canonical_quantity_conversion: str | None = None
    taker_fee: float | None = None
    volume_24h_usd: float | None = None
    open_interest_usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FundingAdapterContract:
    venue: str
    environment: str = "mainnet"
    contract_kind: str | None = None
    price_quote_currency: str | None = None
    settlement_collateral: str | None = None
    collateral_family: str | None = None
    funding_rate_semantics: str | None = None
    funding_rate_unit: str | None = None
    funding_sign_convention: str | None = None
    position_inclusion_rule: str | None = None
    entry_safety_buffer_seconds: float | None = None
    exit_safety_buffer_seconds: float | None = None
    timing_policy_source: str | None = None
    realized_history_semantics: str | None = None
    thread_safe: bool = False
    supports_bulk_funding_sweep: bool = False
    supports_public_shadow_mode: bool = False
    market_rules: FundingMarketRules = field(default_factory=FundingMarketRules)
    status: str = "RESEARCH_ONLY"
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["market_rules"] = self.market_rules.as_dict()
        return payload


def normalized_environment(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"mainnet", "testnet"} else "unknown"


def collateral_family(asset: Any) -> str:
    text = str(asset or "").strip().upper()
    if text in {"USD", "USDT", "USDC"}:
        return USD_MAJOR_STABLE
    if text == "USDE":
        return "USD_SYNTHETIC"
    if text in {"DAI", "USDT0"}:
        return "USD_OTHER_STABLE"
    if text.endswith("USDC") and text != "USDC":
        return "USD_BRIDGED_STABLE"
    return "UNKNOWN"


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def market_rules_from_market(market: dict[str, Any]) -> FundingMarketRules:
    return FundingMarketRules(
        quantity_step=_optional_float(market.get("quantity_step")),
        min_quantity=_optional_float(market.get("min_quantity")),
        min_notional=_optional_float(
            market.get("min_notional_usd", market.get("min_notional"))
        ),
        contract_multiplier=_optional_float(market.get("contract_multiplier")),
        canonical_quantity_conversion=(
            str(market.get("canonical_quantity_conversion"))
            if market.get("canonical_quantity_conversion") not in (None, "")
            else None
        ),
        taker_fee=_optional_float(market.get("taker_fee_rate", market.get("fee_rate"))),
        volume_24h_usd=_optional_float(market.get("volume_24h_usd")),
        open_interest_usd=_optional_float(market.get("open_interest_usd")),
    )


def funding_adapter_contract_from_market(
    market: dict[str, Any] | None,
    *,
    registered: bool = True,
    active: bool = True,
) -> FundingAdapterContract:
    row = dict(market or {})
    venue = str(row.get("venue") or "").strip().lower()
    environment = normalized_environment(row.get("environment"))
    if not venue:
        return FundingAdapterContract(
            venue="unknown",
            environment=environment,
            status="UNAVAILABLE",
            reasons=("venue_missing",),
        )
    if venue in DEACTIVATED_FUNDING_VENUES:
        return FundingAdapterContract(
            venue=venue,
            environment=environment,
            status="DEACTIVATED",
            reasons=("user_deactivated",),
        )
    if not registered or not active or not row:
        return FundingAdapterContract(
            venue=venue,
            environment=environment,
            status="UNAVAILABLE",
            reasons=("no_current_market_data",),
        )

    quote = str(row.get("price_quote_currency") or row.get("quote_asset") or "").upper()
    settlement_collateral = str(
        row.get("settlement_collateral") or row.get("collateral_asset") or ""
    ).upper()
    family = str(row.get("collateral_family") or "").upper()
    if not family:
        family = collateral_family(settlement_collateral)
    market_rules = market_rules_from_market(row)
    contract = FundingAdapterContract(
        venue=venue,
        environment=environment,
        contract_kind=str(row.get("contract_kind") or row.get("contract_type") or "").lower()
        or None,
        price_quote_currency=quote or None,
        settlement_collateral=settlement_collateral or None,
        collateral_family=family or None,
        funding_rate_semantics=(
            str(row.get("funding_rate_semantics") or "").strip().lower() or None
        ),
        funding_rate_unit=(
            str(row.get("funding_rate_unit") or "").strip().lower() or None
        ),
        funding_sign_convention=(
            str(row.get("funding_sign_convention") or "").strip().lower() or None
        ),
        position_inclusion_rule=(
            str(row.get("position_inclusion_rule"))
            if row.get("position_inclusion_rule") not in (None, "")
            else None
        ),
        entry_safety_buffer_seconds=_optional_float(
            row.get("entry_safety_buffer_seconds")
        ),
        exit_safety_buffer_seconds=_optional_float(
            row.get("exit_safety_buffer_seconds")
        ),
        timing_policy_source=(
            str(row.get("timing_policy_source"))
            if row.get("timing_policy_source") not in (None, "")
            else None
        ),
        realized_history_semantics=(
            str(row.get("realized_history_semantics"))
            if row.get("realized_history_semantics") not in (None, "")
            else None
        ),
        thread_safe=bool(row.get("thread_safe")),
        supports_bulk_funding_sweep=bool(row.get("supports_bulk_funding_sweep")),
        supports_public_shadow_mode=bool(
            row.get("supports_public_shadow_mode")
            or row.get("adapter_contract_status") == "SHADOW_ELIGIBLE"
        ),
        market_rules=market_rules,
    )
    reasons = adapter_contract_reasons(contract, row)
    status = adapter_contract_status(contract, reasons)
    return FundingAdapterContract(
        **{
            **contract.as_dict(),
            "market_rules": market_rules,
            "status": status,
            "reasons": tuple(reasons),
        }
    )


def adapter_contract_reasons(
    contract: FundingAdapterContract,
    market: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    required = {
        "environment": contract.environment,
        "contract_kind": contract.contract_kind,
        "price_quote_currency": contract.price_quote_currency,
        "settlement_collateral": contract.settlement_collateral,
        "collateral_family": contract.collateral_family,
        "funding_rate_semantics": contract.funding_rate_semantics,
        "funding_rate_unit": contract.funding_rate_unit,
        "funding_sign_convention": contract.funding_sign_convention,
        "timing_policy_source": contract.timing_policy_source,
    }
    for name, value in required.items():
        if value in (None, "", "unknown", "UNKNOWN"):
            reasons.append(f"{name}_missing")
    if contract.contract_kind != "linear_perpetual":
        reasons.append("contract_kind_not_linear_perpetual")
    if contract.funding_rate_semantics != "next_settlement":
        reasons.append("funding_semantics_not_next_settlement")
    if contract.funding_rate_unit != "fraction_of_notional_per_settlement":
        reasons.append("funding_rate_unit_not_fraction_per_settlement")
    if contract.funding_sign_convention != "positive_long_pays":
        reasons.append("funding_sign_convention_not_positive_long_pays")
    if market.get("normalized_next_funding_rate") is None:
        reasons.append("normalized_next_funding_rate_missing")
    if market.get("next_funding_at") in (None, ""):
        reasons.append("next_funding_at_missing")
    if _optional_float(market.get("mark_price")) is None:
        reasons.append("mark_price_missing")
    if _optional_float(market.get("index_price")) is None:
        reasons.append("index_price_missing")
    if _optional_float(market.get("volume_24h_usd")) is None:
        reasons.append("volume_24h_usd_missing")
    if _optional_float(market.get("open_interest_usd")) is None:
        reasons.append("open_interest_usd_missing")
    if contract.market_rules.taker_fee is None:
        reasons.append("taker_fee_missing")
    if contract.market_rules.quantity_step is None:
        reasons.append("quantity_step_missing")
    if (
        contract.market_rules.quantity_step is None
        and contract.market_rules.contract_multiplier is not None
    ):
        reasons.append("contract_multiplier_is_not_quantity_step")
    if contract.market_rules.min_notional is None:
        reasons.append("min_notional_missing")
    if not contract.supports_public_shadow_mode:
        reasons.append("public_shadow_mode_not_declared")
    return list(dict.fromkeys(reasons))


def adapter_contract_status(
    contract: FundingAdapterContract,
    reasons: list[str],
) -> str:
    paper_blockers = {
        "public_shadow_mode_not_declared",
        "realized_history_semantics_missing",
        "execution_contract_not_proven",
    }
    shadow_blockers = {
        "contract_kind_missing",
        "price_quote_currency_missing",
        "settlement_collateral_missing",
        "collateral_family_missing",
        "funding_rate_semantics_missing",
        "funding_rate_unit_missing",
        "funding_sign_convention_missing",
        "timing_policy_source_missing",
        "environment_missing",
        "contract_kind_not_linear_perpetual",
        "funding_semantics_not_next_settlement",
        "funding_rate_unit_not_fraction_per_settlement",
        "funding_sign_convention_not_positive_long_pays",
        "normalized_next_funding_rate_missing",
        "next_funding_at_missing",
        "mark_price_missing",
        "index_price_missing",
        "taker_fee_missing",
        "quantity_step_missing",
        "contract_multiplier_is_not_quantity_step",
        "min_notional_missing",
        "public_shadow_mode_not_declared",
    }
    if any(reason in shadow_blockers for reason in reasons):
        return "RESEARCH_ONLY"
    if any(reason in paper_blockers for reason in reasons):
        return "SHADOW_ELIGIBLE"
    if contract.realized_history_semantics not in {
        "adapter_explicit_realized_settlement",
        "account_ledger",
    }:
        return "SHADOW_ELIGIBLE"
    return "PAPER_ELIGIBLE"


def mandatory_shadow_inventory(
    markets: list[dict[str, Any]],
    *,
    registered_venues: list[str] | None = None,
) -> list[dict[str, Any]]:
    sample_by_venue: dict[str, dict[str, Any]] = {}
    for market in markets:
        venue = str(market.get("venue") or "").lower()
        if venue and venue not in sample_by_venue:
            sample_by_venue[venue] = market
    venue_names = list(dict.fromkeys([*(registered_venues or []), *PRIMARY_SHADOW_VENUES]))
    rows: list[dict[str, Any]] = []
    for venue in sorted({str(v).lower() for v in venue_names if v}):
        market = sample_by_venue.get(venue, {"venue": venue})
        contract = funding_adapter_contract_from_market(
            market,
            registered=venue in {str(v).lower() for v in venue_names},
            active=venue in sample_by_venue,
        )
        rows.append(contract.as_dict())
    return rows
