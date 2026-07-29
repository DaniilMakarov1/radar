from __future__ import annotations

import math
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from smart_money_radar.funding.settlement_contracts import (
    FundingSettlementContract,
    settlement_contract_blockers,
    settlement_contract_from_market,
)

STRATEGY_NAME = "synchronized_funding_capture"
STRATEGY_VERSION = "synchronized_funding_capture_v2"
INTERNAL_STRATEGY_NAME = "FUNDING_SETTLEMENT_CAPTURE"
ONE_SETTLEMENT = "ONE_SETTLEMENT"
MULTIPLE_SETTLEMENTS = "MULTIPLE_SETTLEMENTS"


def clamp(minimum: float, maximum: float, value: float) -> float:
    return max(float(minimum), min(float(maximum), float(value)))


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def funding_leg_pnl(side: str, quantity: float, mark_price: float, funding_rate: float) -> float:
    notional = float(quantity) * float(mark_price)
    if str(side).lower() == "long":
        return -notional * float(funding_rate)
    return notional * float(funding_rate)


@dataclass(frozen=True)
class EventWindowPlannerConfig:
    max_strategy_hold_seconds: float = 180.0
    max_gap_between_settlements_seconds: float = 180.0
    entry_safety_buffer_seconds: float = 30.0
    exit_safety_buffer_seconds: float = 5.0
    settlement_confirmation_timeout_seconds: float = 5.0
    max_clock_uncertainty_ms: float = 500.0
    conservative_positive_cashflow_fraction: float = 0.90
    conservative_negative_cashflow_multiplier: float = 1.0
    configured_min_net_bps: float = 0.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    basis_movement_reserve_bps: float = 0.0
    stablecoin_reserve_usd: float = 0.0
    execution_failure_reserve_bps: float = 0.0
    partial_fill_reserve_bps: float = 0.0
    timing_uncertainty_reserve_bps: float = 0.0
    operational_reserve_bps: float = 0.0


@dataclass(frozen=True)
class FundingSettlementEvent:
    event_id: str
    venue: str
    environment: str
    symbol: str
    canonical_underlying: str
    leg_id: str
    scheduled_at: str
    earliest_possible_assessment_at: str
    latest_possible_assessment_at: str
    settlement_interval_seconds: float | None
    displayed_rate_period_seconds: float | None
    raw_api_rate: float | None
    raw_rate_unit: str | None
    raw_rate_scale: str | None
    normalized_rate: float | None
    rate_per_next_settlement: float | None
    receiver_side: str
    payer_side: str
    expected_cashflow_usd: float
    conservative_cashflow_usd: float
    rate_status: str
    source_event_at: str | None
    response_received_at: str | None
    source_sequence: str | None
    confirmation_source: str | None
    settlement_semantics_status: str
    semantics_verification_level: str | None
    evidence_version: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FundingRoutePlan:
    strategy_name: str
    compatibility_alias: str
    strategy_version: str
    opportunity_shape: str
    leg_a: dict[str, Any]
    leg_b: dict[str, Any]
    orientation: dict[str, Any]
    planned_entry_at: str
    entry_window_opens_at: str
    entry_deadline_at: str
    planned_exit_at: str
    monitor_until: str
    included_settlement_events: list[FundingSettlementEvent]
    excluded_settlement_events: list[FundingSettlementEvent]
    ambiguous_settlement_events: list[FundingSettlementEvent]
    expected_funding_cashflow_usd: Decimal
    conservative_funding_cashflow_usd: Decimal
    expected_costs_usd: Decimal
    conservative_costs_usd: Decimal
    expected_net_usd: Decimal
    conservative_net_usd: Decimal
    conservative_net_bps: Decimal
    blockers: list[str]
    confidence: str
    eligibility_status: str
    lifecycle_state: str
    expires_at: str
    evidence_version: str
    planner: dict[str, Any]
    modeled_costs: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "external_strategy_name": self.compatibility_alias,
            "compatibility_alias": self.compatibility_alias,
            "strategy_version": self.strategy_version,
            "opportunity_shape": self.opportunity_shape,
            "leg_a": self.leg_a,
            "leg_b": self.leg_b,
            "orientation": self.orientation,
            "planned_entry_at": self.planned_entry_at,
            "entry_window_opens_at": self.entry_window_opens_at,
            "entry_deadline_at": self.entry_deadline_at,
            "planned_exit_at": self.planned_exit_at,
            "monitor_until": self.monitor_until,
            "included_settlement_events": [
                event.as_dict() for event in self.included_settlement_events
            ],
            "excluded_settlement_events": [
                event.as_dict() for event in self.excluded_settlement_events
            ],
            "ambiguous_settlement_events": [
                event.as_dict() for event in self.ambiguous_settlement_events
            ],
            "expected_funding_cashflow_usd": float(self.expected_funding_cashflow_usd),
            "conservative_funding_cashflow_usd": float(self.conservative_funding_cashflow_usd),
            "expected_costs_usd": float(self.expected_costs_usd),
            "conservative_costs_usd": float(self.conservative_costs_usd),
            "expected_net_usd": float(self.expected_net_usd),
            "conservative_net_usd": float(self.conservative_net_usd),
            "conservative_net_bps": float(self.conservative_net_bps),
            "blockers": self.blockers,
            "confidence": self.confidence,
            "eligibility_status": self.eligibility_status,
            "lifecycle_state": self.lifecycle_state,
            "expires_at": self.expires_at,
            "evidence_version": self.evidence_version,
            "modeled_costs": self.modeled_costs,
            "planner": self.planner,
        }


def _event_id(
    *,
    venue: str,
    environment: str,
    symbol: str,
    side: str,
    scheduled_at: str,
) -> str:
    raw = "|".join([venue, environment, symbol, side, scheduled_at])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _decimal(value: Any, default: str = "0") -> Decimal:
    parsed = _optional_decimal(value)
    return parsed if parsed is not None else Decimal(default)


def _event_sort_key(event: FundingSettlementEvent) -> tuple[datetime, str, str]:
    scheduled = parse_time(event.scheduled_at) or datetime.max.replace(tzinfo=UTC)
    return scheduled.astimezone(UTC), event.venue, event.event_id


def _market_notional(mark_price: float | None, target_notional: float) -> float:
    if mark_price is None or mark_price <= 0:
        return max(0.0, float(target_notional))
    return max(0.0, float(target_notional))


def funding_receiver_side(rate_per_next_settlement: float | None) -> str:
    rate = float(rate_per_next_settlement or 0.0)
    if rate > 0:
        return "short"
    if rate < 0:
        return "long"
    return "none"


def funding_payer_side(rate_per_next_settlement: float | None) -> str:
    receiver = funding_receiver_side(rate_per_next_settlement)
    if receiver == "long":
        return "short"
    if receiver == "short":
        return "long"
    return "none"


def position_funding_cashflow_usd(
    *,
    side: str,
    notional: float,
    rate_per_next_settlement: float | None,
) -> float:
    rate = float(rate_per_next_settlement or 0.0)
    if str(side).lower() == "long":
        return -float(notional) * rate
    return float(notional) * rate


def _conservative_cashflow(value: float, config: EventWindowPlannerConfig) -> float:
    if value > 0:
        return value * float(config.conservative_positive_cashflow_fraction)
    if value < 0:
        return value * float(config.conservative_negative_cashflow_multiplier)
    return 0.0


def funding_event_from_market(
    market: dict[str, Any],
    *,
    side: str,
    leg_id: str,
    target_notional: float,
    config: EventWindowPlannerConfig | None = None,
    contract: FundingSettlementContract | None = None,
) -> FundingSettlementEvent | None:
    planner_config = config or EventWindowPlannerConfig()
    scheduled = parse_time(market.get("next_funding_at"))
    if scheduled is None:
        return None
    resolved_contract = contract or settlement_contract_from_market(market)
    uncertainty = float(planner_config.max_clock_uncertainty_ms) / 1000.0
    jitter_before = (
        float(resolved_contract.assessment_jitter_before_seconds)
        if resolved_contract.assessment_jitter_before_seconds is not None
        else 0.0
    )
    jitter_after = (
        float(resolved_contract.assessment_jitter_after_seconds)
        if resolved_contract.assessment_jitter_after_seconds is not None
        else 0.0
    )
    earliest = scheduled - timedelta(seconds=jitter_before + uncertainty)
    latest = scheduled + timedelta(seconds=jitter_after + uncertainty)
    mark = _optional_float(market.get("mark_price"))
    notional = _market_notional(mark, target_notional)
    raw_rate = _optional_float(market.get("funding_rate"))
    normalized_rate = _optional_float(market.get("normalized_next_funding_rate"))
    rate = normalized_rate if normalized_rate is not None else raw_rate
    expected = position_funding_cashflow_usd(
        side=side,
        notional=notional,
        rate_per_next_settlement=rate,
    )
    scheduled_iso = scheduled.astimezone(UTC).isoformat()
    return FundingSettlementEvent(
        event_id=_event_id(
            venue=str(market.get("venue") or "").lower(),
            environment=str(market.get("environment") or "unknown").lower(),
            symbol=str(market.get("symbol") or ""),
            side=side,
            scheduled_at=scheduled_iso,
        ),
        venue=str(market.get("venue") or "").lower(),
        environment=str(market.get("environment") or "unknown").lower(),
        symbol=str(market.get("symbol") or ""),
        canonical_underlying=str(
            market.get("canonical_asset") or market.get("canonical_underlying") or ""
        ).upper(),
        leg_id=leg_id,
        scheduled_at=scheduled_iso,
        earliest_possible_assessment_at=earliest.astimezone(UTC).isoformat(),
        latest_possible_assessment_at=latest.astimezone(UTC).isoformat(),
        settlement_interval_seconds=resolved_contract.settlement_interval_seconds,
        displayed_rate_period_seconds=resolved_contract.displayed_rate_period_seconds,
        raw_api_rate=raw_rate,
        raw_rate_unit=market.get("raw_funding_rate_unit") or market.get("funding_rate_unit"),
        raw_rate_scale=str(market.get("raw_funding_rate_scale") or "fraction"),
        normalized_rate=normalized_rate,
        rate_per_next_settlement=rate,
        receiver_side=funding_receiver_side(rate),
        payer_side=funding_payer_side(rate),
        expected_cashflow_usd=expected,
        conservative_cashflow_usd=_conservative_cashflow(expected, planner_config),
        rate_status=str(market.get("rate_status") or "predicted"),
        source_event_at=market.get("source_event_at"),
        response_received_at=market.get("response_received_at"),
        source_sequence=(
            str(market.get("source_sequence"))
            if market.get("source_sequence") is not None
            else None
        ),
        confirmation_source=resolved_contract.settlement_confirmation_source,
        settlement_semantics_status=resolved_contract.verification_level,
        semantics_verification_level=resolved_contract.verification_level,
        evidence_version=resolved_contract.evidence_checked_at,
    )


def funding_events_from_market(
    market: dict[str, Any],
    *,
    side: str,
    leg_id: str,
    target_notional: float,
    config: EventWindowPlannerConfig | None = None,
    contract: FundingSettlementContract | None = None,
) -> list[FundingSettlementEvent]:
    raw_events = (
        market.get("funding_settlement_events")
        or market.get("settlement_events")
        or []
    )
    if not raw_events:
        event = funding_event_from_market(
            market,
            side=side,
            leg_id=leg_id,
            target_notional=target_notional,
            config=config,
            contract=contract,
        )
        return [event] if event is not None else []
    events: list[FundingSettlementEvent] = []
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            continue
        event_market = {**market}
        event_market["next_funding_at"] = (
            raw_event.get("scheduled_at")
            or raw_event.get("next_funding_at")
            or raw_event.get("funding_time")
            or market.get("next_funding_at")
        )
        event_market["funding_rate"] = raw_event.get(
            "funding_rate",
            raw_event.get("rate", market.get("funding_rate")),
        )
        event_market["normalized_next_funding_rate"] = raw_event.get(
            "normalized_next_funding_rate",
            raw_event.get("rate_per_next_settlement", event_market.get("funding_rate")),
        )
        event_market["raw_funding_rate"] = raw_event.get(
            "raw_api_rate",
            raw_event.get("raw_funding_rate", event_market.get("funding_rate")),
        )
        event_market["raw_funding_rate_unit"] = raw_event.get(
            "raw_rate_unit",
            event_market.get("raw_funding_rate_unit") or event_market.get("funding_rate_unit"),
        )
        event_market["raw_funding_rate_scale"] = raw_event.get(
            "raw_rate_scale",
            event_market.get("raw_funding_rate_scale"),
        )
        event_market["source_event_at"] = raw_event.get(
            "source_event_at",
            event_market.get("source_event_at"),
        )
        event_market["response_received_at"] = raw_event.get(
            "response_received_at",
            event_market.get("response_received_at"),
        )
        event_market["source_sequence"] = raw_event.get(
            "source_sequence",
            event_market.get("source_sequence"),
        )
        event = funding_event_from_market(
            event_market,
            side=side,
            leg_id=f"{leg_id}:event:{index + 1}",
            target_notional=target_notional,
            config=config,
            contract=contract,
        )
        if event is not None:
            events.append(event)
    return sorted(events, key=_event_sort_key)


def _event_time_range(event: FundingSettlementEvent) -> tuple[datetime, datetime]:
    earliest = parse_time(event.earliest_possible_assessment_at)
    latest = parse_time(event.latest_possible_assessment_at)
    if earliest is None or latest is None:
        scheduled = parse_time(event.scheduled_at) or datetime.now(UTC)
        return scheduled, scheduled
    return earliest.astimezone(UTC), latest.astimezone(UTC)


def classify_event_for_hold_window(
    event: FundingSettlementEvent,
    *,
    planned_entry_at: datetime,
    planned_exit_at: datetime,
    exit_safety_buffer_seconds: float,
) -> str:
    earliest, latest = _event_time_range(event)
    entry = planned_entry_at.astimezone(UTC)
    exit_time = planned_exit_at.astimezone(UTC)
    exit_safety = timedelta(seconds=max(0.0, float(exit_safety_buffer_seconds)))
    if latest < entry:
        return "excluded"
    if earliest >= exit_time + exit_safety:
        return "excluded"
    if earliest >= entry and latest <= exit_time:
        return "included"
    return "ambiguous"


def _fee_rate(market: dict[str, Any]) -> float:
    value = _optional_float(market.get("taker_fee_rate"))
    if value is None:
        value = _optional_float(market.get("fee_rate"))
    return float(value or 0.0)


def _planned_costs(
    *,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    target_notional: float,
    config: EventWindowPlannerConfig,
) -> dict[str, float]:
    reference = max(0.0, float(target_notional))
    entry_fees = reference * (_fee_rate(long_market) + _fee_rate(short_market))
    exit_fees = entry_fees
    entry_slippage = reference * max(0.0, float(config.entry_slippage_bps)) / 10_000.0
    exit_slippage = reference * max(0.0, float(config.exit_slippage_bps)) / 10_000.0
    basis_reserve = reference * max(0.0, float(config.basis_movement_reserve_bps)) / 10_000.0
    execution_failure = reference * max(0.0, float(config.execution_failure_reserve_bps)) / 10_000.0
    partial_fill = reference * max(0.0, float(config.partial_fill_reserve_bps)) / 10_000.0
    timing = reference * max(0.0, float(config.timing_uncertainty_reserve_bps)) / 10_000.0
    operational = reference * max(0.0, float(config.operational_reserve_bps)) / 10_000.0
    return {
        "entry_fees_usd": entry_fees,
        "exit_fees_usd": exit_fees,
        "entry_slippage_usd": entry_slippage,
        "exit_slippage_usd": exit_slippage,
        "basis_movement_reserve_usd": basis_reserve,
        "stablecoin_reserve_usd": max(0.0, float(config.stablecoin_reserve_usd)),
        "execution_failure_reserve_usd": execution_failure,
        "partial_fill_reserve_usd": partial_fill,
        "timing_uncertainty_reserve_usd": timing,
        "operational_reserve_usd": operational,
    }


def _build_hold_plan(
    *,
    plan_name: str,
    exit_after_event: FundingSettlementEvent,
    events: list[FundingSettlementEvent],
    planned_entry_at: datetime,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    target_notional: float,
    config: EventWindowPlannerConfig,
) -> dict[str, Any]:
    exit_after = parse_time(exit_after_event.scheduled_at) or planned_entry_at
    planned_exit_at = exit_after + timedelta(
        seconds=max(0.0, float(config.settlement_confirmation_timeout_seconds))
    )
    entry_window_opens_at = planned_entry_at - timedelta(seconds=5.0)
    entry_deadline_at = planned_entry_at + timedelta(seconds=10.0)
    included: list[FundingSettlementEvent] = []
    excluded: list[FundingSettlementEvent] = []
    ambiguous: list[FundingSettlementEvent] = []
    for event in events:
        classification = classify_event_for_hold_window(
            event,
            planned_entry_at=planned_entry_at,
            planned_exit_at=planned_exit_at,
            exit_safety_buffer_seconds=config.exit_safety_buffer_seconds,
        )
        if classification == "included":
            included.append(event)
        elif classification == "excluded":
            excluded.append(event)
        else:
            ambiguous.append(event)
    included_sorted = sorted(included, key=_event_sort_key)
    costs = _planned_costs(
        long_market=long_market,
        short_market=short_market,
        target_notional=target_notional,
        config=config,
    )
    expected_cashflow = sum(
        (_decimal(event.expected_cashflow_usd) for event in included),
        Decimal("0"),
    )
    conservative_cashflow = sum(
        (_decimal(event.conservative_cashflow_usd) for event in included),
        Decimal("0"),
    )
    total_cost = sum((_decimal(value) for value in costs.values()), Decimal("0"))
    expected_net = expected_cashflow - total_cost
    conservative_net = conservative_cashflow - total_cost
    reference = max(0.0, float(target_notional))
    reference_decimal = _decimal(reference)
    blockers: list[str] = []
    hold_seconds = (planned_exit_at - planned_entry_at).total_seconds()
    if hold_seconds > float(config.max_strategy_hold_seconds):
        blockers.append("max_strategy_hold_seconds_exceeded")
    if ambiguous:
        blockers.append("settlement_timing_ambiguous")
    if conservative_net <= 0:
        blockers.append("conservative_net_not_positive")
    min_net_bps = max(0.0, float(config.configured_min_net_bps))
    conservative_net_bps = (
        conservative_net / reference_decimal * Decimal("10000")
        if reference_decimal > 0
        else Decimal("0")
    )
    if conservative_net_bps < _decimal(min_net_bps):
        blockers.append("conservative_net_bps_below_minimum")
    if included_sorted:
        last_latest = max(
            _event_time_range(event)[1]
            for event in included_sorted
        )
    else:
        last_latest = planned_exit_at
    monitor_until = last_latest + timedelta(
        seconds=max(
            0.0,
            float(config.settlement_confirmation_timeout_seconds)
            + float(config.exit_safety_buffer_seconds),
        )
    )
    return {
        "plan_name": plan_name,
        "planned_entry_at": planned_entry_at.astimezone(UTC).isoformat(),
        "entry_window_opens_at": entry_window_opens_at.astimezone(UTC).isoformat(),
        "entry_deadline_at": entry_deadline_at.astimezone(UTC).isoformat(),
        "planned_exit_at": planned_exit_at.astimezone(UTC).isoformat(),
        "monitor_until": monitor_until.astimezone(UTC).isoformat(),
        "max_hold_seconds": float(config.max_strategy_hold_seconds),
        "planned_hold_seconds": hold_seconds,
        "opportunity_shape": (
            MULTIPLE_SETTLEMENTS if len(included_sorted) > 1 else ONE_SETTLEMENT
        ),
        "included_settlement_events": [event.as_dict() for event in included_sorted],
        "excluded_settlement_events": [
            event.as_dict() for event in sorted(excluded, key=_event_sort_key)
        ],
        "ambiguous_settlement_events": [
            event.as_dict() for event in sorted(ambiguous, key=_event_sort_key)
        ],
        "expected_funding_cashflow_usd": float(expected_cashflow),
        "conservative_funding_cashflow_usd": float(conservative_cashflow),
        "modeled_costs": costs,
        "expected_costs_usd": float(total_cost),
        "conservative_costs_usd": float(total_cost),
        "expected_net_usd": float(expected_net),
        "conservative_net_usd": float(conservative_net),
        "conservative_net_bps": float(conservative_net_bps),
        "blockers": list(dict.fromkeys(blockers)),
    }


def _freshness_blockers(
    market: dict[str, Any],
    *,
    now: datetime,
    max_response_age_seconds: float,
    max_source_age_seconds: float,
) -> list[str]:
    blockers: list[str] = []
    response_at = parse_time(market.get("response_received_at"))
    source_at = parse_time(market.get("source_event_at"))
    if response_at is None:
        blockers.append("response_timestamp_missing")
    else:
        age = (now.astimezone(UTC) - response_at.astimezone(UTC)).total_seconds()
        if age < -max_response_age_seconds or age > max_response_age_seconds:
            blockers.append("response_timestamp_stale")
    if source_at is None:
        blockers.append("source_event_timestamp_missing")
    else:
        age = (now.astimezone(UTC) - source_at.astimezone(UTC)).total_seconds()
        if age < -max_source_age_seconds or age > max_source_age_seconds:
            blockers.append("source_event_timestamp_stale")
    return blockers


def _lifecycle_state(
    *,
    now: datetime,
    entry_window_opens_at: str | None,
    entry_deadline_at: str | None,
) -> str:
    opens = parse_time(entry_window_opens_at)
    deadline = parse_time(entry_deadline_at)
    now_utc = now.astimezone(UTC)
    if opens is not None and now_utc < opens.astimezone(UTC):
        return "ENTRY_PENDING"
    if deadline is not None and now_utc > deadline.astimezone(UTC):
        return "ENTRY_WINDOW_MISSED"
    return "ENTRY_WINDOW_OPEN"


def _eligibility_status(blockers: list[str], *, lifecycle_state: str) -> str:
    if lifecycle_state == "ENTRY_WINDOW_MISSED":
        return "WATCH"
    if any("stale" in reason for reason in blockers):
        return "DATA_STALE"
    if any(
        token in reason
        for reason in blockers
        for token in ("environment", "capability", "contract", "semantics", "continuous")
    ):
        return "CAPABILITY_BLOCKED"
    if blockers:
        return "RESEARCH_ONLY"
    return "SHADOW_CANDIDATE"


class FundingSettlementPlanner:
    """Side-effect-free funding settlement capture planner used by shadow and PaperBot."""

    def __init__(
        self,
        config: EventWindowPlannerConfig | None = None,
        *,
        max_response_age_seconds: float = 5.0,
        max_source_age_seconds: float = 60.0,
    ) -> None:
        self.config = config or EventWindowPlannerConfig()
        self.max_response_age_seconds = max_response_age_seconds
        self.max_source_age_seconds = max_source_age_seconds

    def plan(
        self,
        *,
        long_market: dict[str, Any],
        short_market: dict[str, Any],
        now: datetime,
        target_notional: float,
    ) -> FundingRoutePlan:
        config = self.config
        long_venue = str(long_market.get("venue") or "").lower()
        short_venue = str(short_market.get("venue") or "").lower()
        long_contract = settlement_contract_from_market(long_market)
        short_contract = settlement_contract_from_market(short_market)
        blockers: list[str] = []
        if long_venue == "paradex" or short_venue == "paradex":
            blockers.append("funding_continuous_pro_rata")
        long_env = str(long_market.get("environment") or "").lower()
        short_env = str(short_market.get("environment") or "").lower()
        if long_env not in {"mainnet", "testnet"} or short_env not in {"mainnet", "testnet"}:
            blockers.append("environment_unverified")
        elif long_env != short_env:
            blockers.append("environment_mismatch")
        long_asset = str(long_market.get("canonical_asset") or long_market.get("canonical_underlying") or "").upper()
        short_asset = str(short_market.get("canonical_asset") or short_market.get("canonical_underlying") or "").upper()
        if not long_asset or long_asset != short_asset:
            blockers.append("canonical_underlying_mismatch")

        long_events = funding_events_from_market(
            long_market,
            side="long",
            leg_id=f"{long_venue}:{long_market.get('symbol')}:long",
            target_notional=target_notional,
            config=config,
            contract=long_contract,
        )
        short_events = funding_events_from_market(
            short_market,
            side="short",
            leg_id=f"{short_venue}:{short_market.get('symbol')}:short",
            target_notional=target_notional,
            config=config,
            contract=short_contract,
        )
        blockers.extend(
            f"long_{reason}"
            for reason in settlement_contract_blockers(
                long_contract,
                next_settlement_at=long_market.get("next_funding_at"),
            )
        )
        blockers.extend(
            f"short_{reason}"
            for reason in settlement_contract_blockers(
                short_contract,
                next_settlement_at=short_market.get("next_funding_at"),
            )
        )
        blockers.extend(
            f"long_{reason}"
            for reason in _freshness_blockers(
                long_market,
                now=now,
                max_response_age_seconds=self.max_response_age_seconds,
                max_source_age_seconds=self.max_source_age_seconds,
            )
        )
        blockers.extend(
            f"short_{reason}"
            for reason in _freshness_blockers(
                short_market,
                now=now,
                max_response_age_seconds=self.max_response_age_seconds,
                max_source_age_seconds=self.max_source_age_seconds,
            )
        )
        if not long_events:
            blockers.append("long_next_settlement_time_missing")
        if not short_events:
            blockers.append("short_next_settlement_time_missing")
        events = sorted([*long_events, *short_events], key=_event_sort_key)
        if not events:
            blockers = list(dict.fromkeys(blockers))
            return FundingRoutePlan(
                strategy_name=INTERNAL_STRATEGY_NAME,
                compatibility_alias=STRATEGY_NAME,
                strategy_version=STRATEGY_VERSION,
                opportunity_shape=ONE_SETTLEMENT,
                leg_a=long_market,
                leg_b=short_market,
                orientation={"long_venue": long_venue, "short_venue": short_venue},
                planned_entry_at="",
                entry_window_opens_at="",
                entry_deadline_at="",
                planned_exit_at="",
                monitor_until="",
                included_settlement_events=[],
                excluded_settlement_events=[],
                ambiguous_settlement_events=[],
                expected_funding_cashflow_usd=Decimal("0"),
                conservative_funding_cashflow_usd=Decimal("0"),
                expected_costs_usd=Decimal("0"),
                conservative_costs_usd=Decimal("0"),
                expected_net_usd=Decimal("0"),
                conservative_net_usd=Decimal("0"),
                conservative_net_bps=Decimal("0"),
                blockers=blockers,
                confidence="blocked",
                eligibility_status="CAPABILITY_BLOCKED",
                lifecycle_state="DISCOVERED",
                expires_at="",
                evidence_version="",
                planner={"plans": [], "config": asdict(config)},
                modeled_costs={},
            )

        first_event = events[0]
        first_time = parse_time(first_event.scheduled_at) or now.astimezone(UTC)
        planned_entry_at = first_time - timedelta(
            seconds=max(0.0, float(config.entry_safety_buffer_seconds))
        )
        plans: list[dict[str, Any]] = []
        first_event_time = parse_time(first_event.scheduled_at) or first_time
        for index, event in enumerate(events):
            event_time = parse_time(event.scheduled_at)
            if event_time is None:
                continue
            gap = abs((event_time.astimezone(UTC) - first_event_time.astimezone(UTC)).total_seconds())
            if gap > min(
                float(config.max_strategy_hold_seconds),
                float(config.max_gap_between_settlements_seconds),
            ):
                continue
            plan_name = {
                0: "exit_after_first_settlement",
                1: "exit_after_second_settlement",
                2: "exit_after_third_settlement",
            }.get(index, f"exit_after_{index + 1}_settlement")
            plans.append(
                _build_hold_plan(
                    plan_name=plan_name,
                    exit_after_event=event,
                    events=events,
                    planned_entry_at=planned_entry_at,
                    long_market=long_market,
                    short_market=short_market,
                    target_notional=target_notional,
                    config=config,
                )
            )
        if not plans:
            plans.append(
                _build_hold_plan(
                    plan_name="exit_after_first_settlement",
                    exit_after_event=first_event,
                    events=events,
                    planned_entry_at=planned_entry_at,
                    long_market=long_market,
                    short_market=short_market,
                    target_notional=target_notional,
                    config=config,
                )
            )
        feasible = [plan for plan in plans if not plan["blockers"]]
        selected = max(
            feasible or plans,
            key=lambda plan: (
                not plan["blockers"],
                float(plan["conservative_net_usd"]),
                float(plan["expected_net_usd"]),
            ),
        )
        blockers.extend(selected.get("blockers") or [])
        lifecycle_state = _lifecycle_state(
            now=now,
            entry_window_opens_at=selected.get("entry_window_opens_at"),
            entry_deadline_at=selected.get("entry_deadline_at"),
        )
        if lifecycle_state == "ENTRY_WINDOW_MISSED":
            blockers.append("entry_window_missed")
        blockers = list(dict.fromkeys(blockers))
        eligibility_status = _eligibility_status(blockers, lifecycle_state=lifecycle_state)
        confidence = "candidate" if eligibility_status == "SHADOW_CANDIDATE" else "research"
        included_events = [
            FundingSettlementEvent(**event)
            for event in selected.get("included_settlement_events", [])
        ]
        excluded_events = [
            FundingSettlementEvent(**event)
            for event in selected.get("excluded_settlement_events", [])
        ]
        ambiguous_events = [
            FundingSettlementEvent(**event)
            for event in selected.get("ambiguous_settlement_events", [])
        ]
        evidence_versions = [
            str(event.evidence_version)
            for event in included_events
            if event.evidence_version
        ]
        monitor_until = str(selected.get("monitor_until") or selected.get("planned_exit_at") or "")
        return FundingRoutePlan(
            strategy_name=INTERNAL_STRATEGY_NAME,
            compatibility_alias=STRATEGY_NAME,
            strategy_version=STRATEGY_VERSION,
            opportunity_shape=str(selected["opportunity_shape"]),
            leg_a=long_market,
            leg_b=short_market,
            orientation={
                "long_venue": long_venue,
                "long_symbol": long_market.get("symbol"),
                "short_venue": short_venue,
                "short_symbol": short_market.get("symbol"),
            },
            planned_entry_at=str(selected.get("planned_entry_at") or ""),
            entry_window_opens_at=str(selected.get("entry_window_opens_at") or ""),
            entry_deadline_at=str(selected.get("entry_deadline_at") or ""),
            planned_exit_at=str(selected.get("planned_exit_at") or ""),
            monitor_until=monitor_until,
            included_settlement_events=included_events,
            excluded_settlement_events=excluded_events,
            ambiguous_settlement_events=ambiguous_events,
            expected_funding_cashflow_usd=_decimal(selected.get("expected_funding_cashflow_usd")),
            conservative_funding_cashflow_usd=_decimal(selected.get("conservative_funding_cashflow_usd")),
            expected_costs_usd=_decimal(selected.get("expected_costs_usd")),
            conservative_costs_usd=_decimal(selected.get("conservative_costs_usd")),
            expected_net_usd=_decimal(selected.get("expected_net_usd")),
            conservative_net_usd=_decimal(selected.get("conservative_net_usd")),
            conservative_net_bps=_decimal(selected.get("conservative_net_bps")),
            blockers=blockers,
            confidence=confidence,
            eligibility_status=eligibility_status,
            lifecycle_state=lifecycle_state,
            expires_at=monitor_until,
            evidence_version="|".join(sorted(set(evidence_versions))),
            modeled_costs=dict(selected.get("modeled_costs") or {}),
            planner={
                "selected_plan": selected["plan_name"],
                "plans": plans,
                "config": asdict(config),
            },
        )


def build_settlement_capture_opportunity(
    *,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    now: datetime,
    target_notional: float,
    planner_config: EventWindowPlannerConfig | None = None,
    max_response_age_seconds: float = 5.0,
    max_source_age_seconds: float = 60.0,
) -> dict[str, Any]:
    planner = FundingSettlementPlanner(
        planner_config,
        max_response_age_seconds=max_response_age_seconds,
        max_source_age_seconds=max_source_age_seconds,
    )
    return planner.plan(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=target_notional,
    ).as_dict()


def gross_funding_pnl(
    *,
    quantity: float,
    long_mark: float,
    short_mark: float,
    long_funding_rate: float,
    short_funding_rate: float,
) -> float:
    return funding_leg_pnl("long", quantity, long_mark, long_funding_rate) + funding_leg_pnl(
        "short",
        quantity,
        short_mark,
        short_funding_rate,
    )


def settlement_skew_seconds(long_next: Any, short_next: Any) -> float | None:
    long_time = parse_time(long_next)
    short_time = parse_time(short_next)
    if long_time is None or short_time is None:
        return None
    return abs((long_time.astimezone(UTC) - short_time.astimezone(UTC)).total_seconds())


def settlement_alignment_passed(
    long_next: Any,
    short_next: Any,
    *,
    tolerance_seconds: float = 1.0,
) -> bool:
    skew = settlement_skew_seconds(long_next, short_next)
    return skew is not None and skew <= float(tolerance_seconds)


def entry_window_passed(
    lead_seconds: float,
    *,
    minimum: float = 25.0,
    maximum: float = 35.0,
) -> bool:
    return float(minimum) <= float(lead_seconds) <= float(maximum)


def initial_entry_economics(
    *,
    conservative_funding_gross: float,
    baseline_round_trip_book_cost: float,
    total_round_trip_fee_estimate: float,
    entry_basis_reserve_usd: float,
    entry_legging_reserve_usd: float,
    reference_notional: float,
) -> dict[str, Any]:
    modeled_cost = (
        max(0.0, float(baseline_round_trip_book_cost))
        + max(0.0, float(total_round_trip_fee_estimate))
        + max(0.0, float(entry_basis_reserve_usd))
        + max(0.0, float(entry_legging_reserve_usd))
    )
    funding = float(conservative_funding_gross)
    reference = max(0.0, float(reference_notional))
    expected_net = funding - modeled_cost
    coverage = funding / modeled_cost if modeled_cost > 0 else math.inf if funding > 0 else 0.0
    gross_threshold = max(2.50, reference * 0.005)
    net_threshold = max(1.00, reference * 0.002)
    return {
        "strategy_name": STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "conservative_funding_gross": funding,
        "baseline_round_trip_book_cost": max(0.0, float(baseline_round_trip_book_cost)),
        "total_round_trip_fee_estimate": max(0.0, float(total_round_trip_fee_estimate)),
        "entry_basis_reserve_usd": max(0.0, float(entry_basis_reserve_usd)),
        "entry_legging_reserve_usd": max(0.0, float(entry_legging_reserve_usd)),
        "initial_total_modeled_cost": modeled_cost,
        "initial_expected_net_pnl": expected_net,
        "conservative_funding_edge_bps": funding / reference * 10_000.0 if reference > 0 else 0.0,
        "initial_expected_net_bps": expected_net / reference * 10_000.0 if reference > 0 else 0.0,
        "initial_cost_coverage_ratio": coverage,
        "minimum_conservative_funding_gross": gross_threshold,
        "minimum_initial_expected_net_pnl": net_threshold,
        "eligible": (
            funding >= gross_threshold
            and expected_net >= net_threshold
            and coverage >= 1.50
        ),
        "expected_spread_convergence_pnl": 0.0,
    }


def percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def basis_duration_floor_bps(wait_seconds: float) -> float:
    wait_hours = max(0.0, float(wait_seconds)) / 3600.0
    return min(100.0, max(25.0, 25.0 * math.ceil(max(wait_hours, 1e-12))))


def hold_economics(
    *,
    next_conservative_funding_gross: float,
    current_close_fees: float,
    reference_notional: float,
    wait_seconds: float,
    entry_basis_reserve_bps: float,
    adverse_basis_change_30s_bps: list[float] | None = None,
    p95_abs_mark_return_1s_bps: float | None = None,
    close_depth_multiple: float = 10.0,
) -> dict[str, Any]:
    reference = max(0.0, float(reference_notional))
    current_close_fee_stress = max(0.0, float(current_close_fees)) * 1.10
    additional_fee_reserve = max(0.0, current_close_fee_stress - max(0.0, float(current_close_fees)))
    observed_changes = [max(0.0, float(value)) for value in adverse_basis_change_30s_bps or []]
    p95_adverse = percentile_95(observed_changes) if len(observed_changes) >= 10 else 5.0
    scaled_observed = p95_adverse * math.sqrt(max(0.0, float(wait_seconds)) / 30.0)
    basis_reserve_bps = clamp(
        25.0,
        200.0,
        max(
            float(entry_basis_reserve_bps),
            basis_duration_floor_bps(wait_seconds),
            scaled_observed,
        ),
    )
    legging_reserve_bps = (
        clamp(10.0, 30.0, 2.0 * float(p95_abs_mark_return_1s_bps))
        if p95_abs_mark_return_1s_bps is not None
        else 15.0
    )
    wait_hours = max(0.0, float(wait_seconds)) / 3600.0
    time_reserve_bps = min(40.0, 10.0 * math.ceil(max(wait_hours, 1e-12)))
    liquidity_reserve_bps = 10.0 if float(close_depth_multiple) >= 10.0 else 20.0
    total_reserve_bps = (
        basis_reserve_bps
        + legging_reserve_bps
        + time_reserve_bps
        + liquidity_reserve_bps
    )
    reserve_usd = reference * total_reserve_bps / 10_000.0
    incremental_cost = additional_fee_reserve + reserve_usd
    funding = float(next_conservative_funding_gross)
    incremental_net = funding - incremental_cost
    coverage = funding / incremental_cost if incremental_cost > 0 else math.inf if funding > 0 else 0.0
    return {
        "current_close_fee_stress": current_close_fee_stress,
        "additional_fee_reserve": additional_fee_reserve,
        "p95_adverse_basis_change_30s_bps": p95_adverse,
        "duration_basis_floor_bps": basis_duration_floor_bps(wait_seconds),
        "scaled_observed_basis_bps": scaled_observed,
        "hold_basis_reserve_bps": basis_reserve_bps,
        "hold_legging_reserve_bps": legging_reserve_bps,
        "hold_time_reserve_bps": time_reserve_bps,
        "hold_liquidity_reserve_bps": liquidity_reserve_bps,
        "hold_total_reserve_bps": total_reserve_bps,
        "hold_total_reserve_usd": reserve_usd,
        "incremental_hold_cost": incremental_cost,
        "incremental_hold_net_pnl": incremental_net,
        "incremental_hold_edge_bps": incremental_net / reference * 10_000.0 if reference > 0 else 0.0,
        "hold_cost_coverage_ratio": coverage,
    }


def summarize_funding_observations(
    observations: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    gross_values = [float(row["gross_funding_pnl"]) for row in observations]
    observed_times = [
        parse_time(row.get("observed_at"))
        for row in observations
        if parse_time(row.get("observed_at")) is not None
    ]
    latest = max(observed_times) if observed_times else None
    earliest = min(observed_times) if observed_times else None
    latest_age = (
        (now.astimezone(UTC) - latest.astimezone(UTC)).total_seconds()
        if latest is not None
        else None
    )
    return {
        "observation_count": len(observations),
        "observation_span_seconds": (
            (latest - earliest).total_seconds()
            if latest is not None and earliest is not None
            else 0.0
        ),
        "latest_observation_age_seconds": latest_age,
        "median_gross_funding": median(gross_values) if gross_values else 0.0,
        "minimum_gross_funding": min(gross_values) if gross_values else 0.0,
        "latest_gross_funding": gross_values[-1] if gross_values else 0.0,
        "all_positive": all(value > 0 for value in gross_values),
        "latest_vs_median_ok": (
            gross_values[-1] >= 0.80 * median(gross_values)
            if gross_values
            else False
        ),
        "conservative_funding_gross": 0.90 * min(gross_values) if gross_values else 0.0,
    }


def synchronized_strategy_candidate(
    row: dict[str, Any],
    *,
    current_funding_gross: float,
    actionable_profit_threshold: float,
    blocking_risk_flags: list[str],
    decision_mode: str,
) -> dict[str, Any]:
    funding_notional = float(row.get("funding_notional") or row.get("notional") or 0.0)
    execution_cost = float(row.get("execution_cost") or 0.0)
    basis_stress_loss = max(0.0, float(row.get("basis_stress_loss") or 0.0))
    conservative_funding = float(current_funding_gross)
    expected_net = conservative_funding - execution_cost - basis_stress_loss
    threshold = max(float(actionable_profit_threshold or 0.0), 1.0, funding_notional * 0.002)
    gross_threshold = max(2.50, funding_notional * 0.005)
    coverage_denominator = execution_cost + basis_stress_loss
    coverage = (
        conservative_funding / coverage_denominator
        if coverage_denominator > 0
        else math.inf if conservative_funding > 0 else 0.0
    )
    blockers = [
        str(flag)
        for flag in dict.fromkeys(blocking_risk_flags)
        if str(flag) not in {"basis_not_covered_by_funding", "basis_not_covered_by_live_funding"}
    ]
    reasons = list(blockers)
    if str(decision_mode) != "settlement_capture":
        reasons.append("not_next_settlement_capture")
    if conservative_funding < gross_threshold:
        reasons.append("conservative_funding_below_minimum")
    if expected_net < threshold:
        reasons.append("initial_expected_net_below_minimum")
    if coverage < 1.50:
        reasons.append("initial_cost_coverage_below_minimum")
    return {
        "selection_model": STRATEGY_VERSION,
        "strategy_name": STRATEGY_NAME,
        "strategy_class": STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "primary_edge": "synchronized_funding",
        "edge_type": "funding_led",
        "edge_label": "Synchronized funding capture",
        "edge_quality": "clean" if not reasons else "blocked",
        "eligible": not reasons,
        "expected_net_pnl": expected_net,
        "gross_edge_pnl": conservative_funding,
        "funding_pnl_component": conservative_funding,
        "spread_pnl_component": 0.0,
        "signed_spread_pnl_component": 0.0,
        "expected_spread_convergence_pnl": 0.0,
        "execution_cost": execution_cost,
        "basis_stress_loss": basis_stress_loss,
        "actionable_profit_threshold": threshold,
        "minimum_conservative_funding_gross": gross_threshold,
        "coverage_ratio": coverage,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": [],
        "thesis": (
            "Funding-only synchronized settlement capture: expected spread "
            "convergence is zero; executable spread and basis are modeled as cost/risk."
        ),
    }


def validate_focused_observation(
    observation: dict[str, Any],
    *,
    now: datetime,
    max_age_seconds: float = 2.0,
    max_response_skew_seconds: float = 1.0,
) -> dict[str, Any]:
    reasons: list[str] = []
    long_age = float(observation.get("long_age_seconds") or 0.0)
    short_age = float(observation.get("short_age_seconds") or 0.0)
    long_response_at = parse_time(observation.get("long_response_received_at"))
    short_response_at = parse_time(observation.get("short_response_received_at"))
    long_next_funding = parse_time(observation.get("long_next_funding_at"))
    short_next_funding = parse_time(observation.get("short_next_funding_at"))
    long_book_executable = bool(observation.get("long_book_executable"))
    short_book_executable = bool(observation.get("short_book_executable"))
    capabilities_passed = bool(observation.get("capabilities_passed"))
    if long_age > max_age_seconds:
        reasons.append(f"long_age_exceeds_{max_age_seconds}s")
    if short_age > max_age_seconds:
        reasons.append(f"short_age_exceeds_{max_age_seconds}s")
    if long_response_at is not None and short_response_at is not None:
        response_skew = abs(
            (long_response_at.astimezone(UTC) - short_response_at.astimezone(UTC)).total_seconds()
        )
        if response_skew > max_response_skew_seconds:
            reasons.append(
                f"response_skew_{response_skew:.3f}s_exceeds_{max_response_skew_seconds}s"
            )
    elif long_response_at is None or short_response_at is None:
        reasons.append("response_timestamp_missing")
    if long_next_funding is None or short_next_funding is None:
        reasons.append("funding_timestamp_missing")
    if not long_book_executable:
        reasons.append("long_book_not_executable")
    if not short_book_executable:
        reasons.append("short_book_not_executable")
    if not capabilities_passed:
        reasons.append("capabilities_not_passed")
    return {
        "valid": not reasons,
        "reasons": reasons,
        "long_age_seconds": long_age,
        "short_age_seconds": short_age,
        "gross_funding_pnl": float(observation.get("gross_funding_pnl") or 0.0),
        "observed_at": observation.get("observed_at"),
    }


def entry_underwriting(
    observations: list[dict[str, Any]],
    *,
    now: datetime,
    minimum_observations: int = 10,
    minimum_span_seconds: float = 20.0,
    max_latest_age_seconds: float = 2.0,
    conservative_fraction: float = 0.9,
    latest_vs_median_fraction: float = 0.8,
) -> dict[str, Any]:
    reasons: list[str] = []
    if len(observations) < minimum_observations:
        reasons.append(
            f"insufficient_observations_{len(observations)}<{minimum_observations}"
        )
    gross_values = [float(row.get("gross_funding_pnl") or 0.0) for row in observations]
    observed_times = [
        parse_time(row.get("observed_at"))
        for row in observations
        if parse_time(row.get("observed_at")) is not None
    ]
    latest = max(observed_times) if observed_times else None
    earliest = min(observed_times) if observed_times else None
    span = (
        (latest - earliest).total_seconds()
        if latest is not None and earliest is not None
        else 0.0
    )
    latest_age = (
        (now.astimezone(UTC) - latest.astimezone(UTC)).total_seconds()
        if latest is not None
        else None
    )
    if span < minimum_span_seconds:
        reasons.append(f"observation_span_{span:.1f}s_below_{minimum_span_seconds}s")
    if latest_age is None or latest_age > max_latest_age_seconds:
        reasons.append(
            f"latest_observation_age_{latest_age}s_exceeds_{max_latest_age_seconds}s"
            if latest_age is not None
            else "latest_observation_age_missing"
        )
    if gross_values and not all(value > 0 for value in gross_values):
        reasons.append("not_all_gross_funding_positive")
    median_gross = median(gross_values) if gross_values else 0.0
    latest_gross = gross_values[-1] if gross_values else 0.0
    if gross_values and latest_gross < latest_vs_median_fraction * median_gross:
        reasons.append(
            f"latest_gross_{latest_gross:.4f}_below_{latest_vs_median_fraction}*median_{median_gross:.4f}"
        )
    conservative_funding = conservative_fraction * min(gross_values) if gross_values else 0.0
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "observation_count": len(observations),
        "observation_span_seconds": span,
        "latest_observation_age_seconds": latest_age,
        "median_gross_funding": median_gross,
        "latest_gross_funding": latest_gross,
        "minimum_gross_funding": min(gross_values) if gross_values else 0.0,
        "all_positive": all(value > 0 for value in gross_values),
        "conservative_funding_gross": conservative_funding,
    }
