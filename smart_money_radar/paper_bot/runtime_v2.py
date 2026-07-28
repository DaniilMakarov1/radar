from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.strategy_synchronized_funding import (
    STRATEGY_NAME,
    STRATEGY_VERSION,
    entry_underwriting,
    gross_funding_pnl,
    hold_economics,
    initial_entry_economics,
    parse_time,
    settlement_alignment_passed,
    summarize_funding_observations,
    validate_focused_observation,
)
from smart_money_radar.paper_bot.accounting import executable_paper_pnl
from smart_money_radar.paper_bot.accounting import (
    collateral_reserve_event_key,
    collateral_release_event_key,
    funding_event_key,
    make_ledger_entry,
    order_fee_event_key,
    price_pnl_event_key,
)
from smart_money_radar.paper_bot.cycle_manager import (
    next_cycle_observation_decision,
    next_cycle_schedule_decision,
    post_settlement_probe_decision,
)
from smart_money_radar.paper_bot.execution import (
    EXECUTION_HAIRCUT_FRACTION,
    entry_fill_state,
    simulate_marketable_ioc,
    t20_deadline_passed,
)
from smart_money_radar.paper_bot.helpers import leg_by_side, optional_float, route_entry_key
from smart_money_radar.paper_bot.risk import (
    dynamic_basis_stop_decision,
    entry_risk_gates,
    hard_risk_triggered,
)
from smart_money_radar.paper_bot.settlement import (
    build_settlement_crossing_rows,
    reconcile_leg,
    realized_public_funding_rate,
)
from smart_money_radar.storage import SQLiteStore

OBSERVATION_MIN_COUNT = 10
OBSERVATION_MIN_SPAN_SECONDS = 20.0
POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS = 20.0
POST_SETTLEMENT_HOLD_DECISION_SECONDS = 30.0
POST_SETTLEMENT_PROBE_START_SECONDS = 5.0
POST_SETTLEMENT_PROBE_END_SECONDS = 15.0
POST_SETTLEMENT_CLOSE_AT_T20_SECONDS = 20.0
POST_SETTLEMENT_EVALUATION_START_SECONDS = 5.0


def schedule_seconds_to_next(probe_decision: dict[str, Any], now: datetime) -> float | None:
    next_at = parse_time(probe_decision.get("next_cycle_at"))
    if next_at is None:
        return None
    return max(0.0, (next_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds())


def synchronized_runtime_enabled(config: Any) -> bool:
    return STRATEGY_NAME in set(str(item) for item in getattr(config, "strategy_set", ()))


def capture_position_id_for_route(route: dict[str, Any]) -> str:
    digest = hashlib.sha256(route_entry_key(route).encode("utf-8")).hexdigest()[:16]
    return f"fc-{digest}"


def route_next_settlement(route: dict[str, Any]) -> datetime | None:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_next = parse_time(long_leg.get("next_funding_at"))
    short_next = parse_time(short_leg.get("next_funding_at"))
    if long_next is None or short_next is None:
        return None
    if not settlement_alignment_passed(long_next, short_next):
        return None
    return max(long_next.astimezone(UTC), short_next.astimezone(UTC))


def _leg_response_time(leg: dict[str, Any]) -> datetime | None:
    for key in (
        "orderbook_response_received_at",
        "response_received_at",
        "market_response_received_at",
    ):
        parsed = parse_time(leg.get(key))
        if parsed is not None:
            return parsed.astimezone(UTC)
    return None


def _leg_mark(leg: dict[str, Any]) -> float:
    return float(
        optional_float(leg.get("mark_price"))
        or optional_float(leg.get("vwap"))
        or optional_float(leg.get("open_vwap"))
        or 0.0
    )


def _leg_rate(leg: dict[str, Any]) -> float:
    return float(
        optional_float(leg.get("normalized_next_funding_rate"))
        if optional_float(leg.get("normalized_next_funding_rate")) is not None
        else optional_float(leg.get("funding_rate")) or 0.0
    )


def _leg_fee_rate(leg: dict[str, Any]) -> float:
    return float(
        optional_float(leg.get("fee_rate"))
        if optional_float(leg.get("fee_rate")) is not None
        else optional_float(leg.get("taker_fee_rate")) or 0.0
    )


def _synthetic_levels(price: float, quantity: float) -> list[list[float]]:
    if price <= 0 or quantity <= 0:
        return []
    return [[price, quantity / EXECUTION_HAIRCUT_FRACTION]]


def _observation_bucket(route_key: str, phase: str, cycle_id: str) -> str:
    if phase == "entry":
        return route_key
    return f"{route_key}:{phase}:{cycle_id}"


def build_focused_observation(
    route: dict[str, Any],
    *,
    now: datetime,
    phase: str = "entry",
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_response = _leg_response_time(long_leg)
    short_response = _leg_response_time(short_leg)
    long_mark = _leg_mark(long_leg)
    short_mark = _leg_mark(short_leg)
    quantity = min(
        float(long_leg.get("base_quantity") or route.get("base_quantity") or 0.0),
        float(short_leg.get("base_quantity") or route.get("base_quantity") or 0.0),
    )
    if quantity <= 0 and long_mark > 0 and short_mark > 0:
        quantity = min(
            float(route.get("target_notional") or 500.0) / long_mark,
            float(route.get("target_notional") or 500.0) / short_mark,
        )
    gross = (
        gross_funding_pnl(
            quantity=quantity,
            long_mark=long_mark,
            short_mark=short_mark,
            long_funding_rate=_leg_rate(long_leg),
            short_funding_rate=_leg_rate(short_leg),
        )
        if quantity > 0 and long_mark > 0 and short_mark > 0
        else 0.0
    )
    long_age = (
        (now.astimezone(UTC) - long_response).total_seconds()
        if long_response is not None
        else math.inf
    )
    short_age = (
        (now.astimezone(UTC) - short_response).total_seconds()
        if short_response is not None
        else math.inf
    )
    cross_skew = (
        abs((long_response - short_response).total_seconds())
        if long_response is not None and short_response is not None
        else math.inf
    )
    evidence = route.get("evidence") or {}
    capability_reasons = list(evidence.get("capability_rejections") or [])
    capabilities_passed = bool(evidence.get("synchronized_capability_passed")) and not capability_reasons
    long_open = optional_float(long_leg.get("open_vwap")) or optional_float(long_leg.get("vwap"))
    short_open = optional_float(short_leg.get("open_vwap")) or optional_float(short_leg.get("vwap"))
    long_close = optional_float(long_leg.get("close_vwap")) or optional_float(long_leg.get("best_bid"))
    short_close = optional_float(short_leg.get("close_vwap")) or optional_float(short_leg.get("best_ask"))
    raw = {
        "phase": phase,
        "observed_at": now.astimezone(UTC).isoformat(),
        "long_response_received_at": long_response.isoformat() if long_response else None,
        "short_response_received_at": short_response.isoformat() if short_response else None,
        "long_age_seconds": long_age,
        "short_age_seconds": short_age,
        "cross_venue_skew_seconds": cross_skew,
        "cross_venue_skew_ms": None if not math.isfinite(cross_skew) else cross_skew * 1000.0,
        "long_mark": long_mark,
        "short_mark": short_mark,
        "long_index": optional_float(long_leg.get("index_price")),
        "short_index": optional_float(short_leg.get("index_price")),
        "long_next_funding_at": long_leg.get("next_funding_at"),
        "short_next_funding_at": short_leg.get("next_funding_at"),
        "long_next_funding_rate": _leg_rate(long_leg),
        "short_next_funding_rate": _leg_rate(short_leg),
        "gross_funding_pnl": gross,
        "long_open_vwap": long_open,
        "short_open_vwap": short_open,
        "long_close_vwap": long_close,
        "short_close_vwap": short_close,
        "current_exit_spread": (
            float(short_close) - float(long_close)
            if long_close is not None and short_close is not None
            else None
        ),
        "paper_net_if_exit_now": None,
        "long_book_executable": long_open is not None and long_close is not None,
        "short_book_executable": short_open is not None and short_close is not None,
        "capabilities_passed": capabilities_passed,
    }
    validation = validate_focused_observation(raw, now=now)
    raw["snapshot_valid"] = bool(validation["valid"])
    raw["invalid_reason"] = ",".join(validation["reasons"]) if validation["reasons"] else None
    return raw


RECONCILIATION_TIMEOUT_SECONDS = 600.0
RECONCILIATION_TOLERANCE_SECONDS = 120.0
RECONCILIATION_MARK_MAX_DISTANCE_SECONDS = 2.0


class SynchronizedFundingRuntimeV2:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: Any,
        clock: Any,
        observations_by_route: dict[str, list[dict[str, Any]]],
        settlement_data_provider: Any | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.observations_by_route = observations_by_route
        self.settlement_data_provider = settlement_data_provider

    def _record_ledger_entry(self, row: dict[str, Any]) -> str | None:
        if float(row.get("cash_delta") or 0.0) != 0.0 and not row.get("venue"):
            raise ValueError(
                f"cash-affecting paper ledger entry requires venue: {row.get('event_key')}"
            )
        event_key = self.store.upsert_paper_event_ledger(row)
        if event_key is not None and row.get("venue") and float(row.get("cash_delta") or 0.0) != 0.0:
            self.store.update_funding_paper_account_cash(
                str(row["venue"]),
                float(row.get("cash_delta") or 0.0),
            )
        return event_key

    def _record_price_pnl_entries(
        self,
        *,
        position_id: str,
        cycle_id: str,
        long_venue: str,
        short_venue: str,
        long_price_pnl: float,
        short_price_pnl: float,
        payload: dict[str, Any],
    ) -> None:
        for side, venue, value in (
            ("long", long_venue, float(long_price_pnl)),
            ("short", short_venue, float(short_price_pnl)),
        ):
            if abs(value) <= 1e-12:
                continue
            self._record_ledger_entry(
                make_ledger_entry(
                    price_pnl_event_key(position_id, venue),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=venue,
                    event_type="price_pnl",
                    cash_delta=value,
                    payload={**payload, "side": side, "price_pnl": value},
                )
            )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def confirmed_funding_pnl_for_position(self, position_id: str) -> float:
        """Sum of idempotent reconciled funding ledger entries for a position.

        Only includes RATE_AND_MARK_RECONCILED entries. Excludes PENDING,
        PUBLIC_RATE_CONFIRMED, expected/provisional funding.
        """
        total = 0.0
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("event_type") or "") != "funding":
                continue
            total += float(row.get("cash_delta") or 0.0)
        return total

    def process_pending_reconciliations(self, now: datetime) -> dict[str, Any]:
        """Process all PENDING reconciliation rows.

        For each PENDING row:
        - public event missing and age <= 600s: remain PENDING
        - public event missing and age > 600s: set UNRECONCILED
        - rate found but nearest mark missing: set PUBLIC_RATE_CONFIRMED, no cashflow
        - rate + mark found: set RATE_AND_MARK_RECONCILED, calculate funding, ledger entry

        Returns summary dict.
        """
        provider = self.settlement_data_provider
        if provider is None:
            return {"processed": 0, "reason": "no_settlement_data_provider"}

        pending_rows = self.store.pending_reconciliation_rows()
        if not pending_rows:
            return {"processed": 0}

        processed = 0
        timed_out = 0
        rate_confirmed = 0
        reconciled = 0
        now_utc = now.astimezone(UTC)

        for row in pending_rows:
            position_id = str(row["position_id"])
            venue = str(row["venue"])
            symbol = str(row["symbol"])
            side = str(row["side"])
            scheduled_at_str = str(row["scheduled_funding_at"])
            cycle_id = str(row.get("cycle_id") or "")
            quantity = self._position_quantity(position_id)

            scheduled_at = parse_time(scheduled_at_str)
            if scheduled_at is None:
                continue

            age_seconds = (now_utc - scheduled_at.astimezone(UTC)).total_seconds()

            public_rate = optional_float(row.get("confirmed_funding_rate"))
            public_event: dict[str, Any] | None = None
            if public_rate is None:
                public_event = provider.get_public_funding_event(
                    venue=venue,
                    symbol=symbol,
                    scheduled_funding_at=scheduled_at.astimezone(UTC),
                    tolerance_seconds=RECONCILIATION_TOLERANCE_SECONDS,
                )
                if public_event is not None:
                    public_rate = realized_public_funding_rate(public_event)
                    if public_rate is None:
                        self._update_reconciliation_row(
                            row,
                            status="PENDING",
                            rate_status="UNACCEPTED_SEMANTICS"
                            if public_event.get("funding_rate") is not None
                            else "MISSING",
                            mark_status=row.get("mark_status"),
                            evidence_update={
                                "ignored_public_event": {
                                    "rate_semantics": public_event.get("rate_semantics")
                                    or public_event.get("funding_rate_semantics")
                                    or "unclear",
                                    "published_at": public_event.get("published_at"),
                                    "skew_seconds": public_event.get("skew_seconds"),
                                    "source": public_event.get("source"),
                                }
                            },
                        )

            if public_rate is None:
                if age_seconds > RECONCILIATION_TIMEOUT_SECONDS:
                    self._update_reconciliation_row(
                        row, status="UNRECONCILED",
                        rate_status="MISSING", mark_status="MISSING",
                    )
                    timed_out += 1
                    processed += 1
                    self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
                    self._maybe_finalize_position_after_reconciliation(position_id)
                continue

            mark_snapshot = provider.get_nearest_mark_snapshot(
                venue=venue,
                symbol=symbol,
                scheduled_funding_at=scheduled_at.astimezone(UTC),
                max_distance_seconds=RECONCILIATION_MARK_MAX_DISTANCE_SECONDS,
            )

            if mark_snapshot is None:
                if age_seconds > RECONCILIATION_TIMEOUT_SECONDS:
                    self._update_reconciliation_row(
                        row,
                        status="UNRECONCILED",
                        confirmed_funding_rate=public_rate,
                        rate_status="CONFIRMED",
                        mark_status="MISSING",
                    )
                    timed_out += 1
                    processed += 1
                    self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
                    self._maybe_finalize_position_after_reconciliation(position_id)
                    continue
                self._update_reconciliation_row(
                    row,
                    status="PUBLIC_RATE_CONFIRMED",
                    confirmed_funding_rate=public_rate,
                    rate_status="CONFIRMED",
                    mark_status="MISSING",
                    evidence_update={
                        "public_event": {
                            "rate_semantics": public_event.get("rate_semantics")
                            if public_event
                            else "stored_confirmed_rate",
                            "published_at": public_event.get("published_at")
                            if public_event
                            else None,
                            "skew_seconds": public_event.get("skew_seconds")
                            if public_event
                            else None,
                        }
                    },
                )
                rate_confirmed += 1
                continue

            mark_price = optional_float(mark_snapshot.get("mark_price"))
            if mark_price is None:
                continue
            leg_result = reconcile_leg(
                public_rate=public_rate,
                public_mark=mark_price,
                side=side,
                quantity=quantity,
            )

            self._update_reconciliation_row(
                row,
                status=leg_result["status"],
                confirmed_funding_rate=leg_result.get("confirmed_funding_rate"),
                settlement_mark_price=leg_result.get("settlement_mark_price"),
                funding_pnl=leg_result.get("funding_pnl"),
                rate_status=leg_result.get("rate_status"),
                mark_status=leg_result.get("mark_status"),
                evidence_update={
                    "public_event": {
                        "rate_semantics": public_event.get("rate_semantics")
                        if public_event
                        else "stored_confirmed_rate",
                        "published_at": public_event.get("published_at")
                        if public_event
                        else None,
                        "skew_seconds": public_event.get("skew_seconds")
                        if public_event
                        else None,
                    },
                    "mark_snapshot": {
                        "observed_at": mark_snapshot.get("observed_at"),
                        "skew_seconds": mark_snapshot.get("skew_seconds"),
                        "source": mark_snapshot.get("source"),
                        "timestamp_source": mark_snapshot.get("timestamp_source"),
                        "reconciliation_quality": mark_snapshot.get("reconciliation_quality")
                        or "UNKNOWN",
                    },
                },
            )

            if leg_result["status"] == "RATE_AND_MARK_RECONCILED":
                funding_pnl = float(leg_result["funding_pnl"] or 0.0)
                event_key = funding_event_key(position_id, venue, scheduled_at_str)
                self._record_ledger_entry(
                    make_ledger_entry(
                        event_key,
                        position_id=position_id,
                        cycle_id=cycle_id,
                        venue=venue,
                        event_type="funding",
                        cash_delta=funding_pnl,
                        payload={
                            "confirmed_rate": public_rate,
                            "settlement_mark": mark_price,
                            "side": side,
                            "rate_semantics": public_event.get("rate_semantics")
                            if public_event
                            else "stored_confirmed_rate",
                            "reconciliation_quality": mark_snapshot.get("reconciliation_quality")
                            or "UNKNOWN",
                        },
                    )
                )
                reconciled += 1
            processed += 1
            self._maybe_finalize_cycle_after_reconciliation(position_id, cycle_id)
            self._maybe_finalize_position_after_reconciliation(position_id)

        return {
            "processed": processed,
            "timed_out": timed_out,
            "rate_confirmed": rate_confirmed,
            "reconciled": reconciled,
        }

    def _position_quantity(self, position_id: str) -> float:
        position = self.store.funding_capture_position_by_id(position_id)
        if position is None:
            return 0.0
        return float(position.get("quantity") or 0.0)

    def _update_reconciliation_row(
        self,
        row: dict[str, Any],
        *,
        status: str,
        confirmed_funding_rate: float | None = None,
        settlement_mark_price: float | None = None,
        funding_pnl: float | None = None,
        rate_status: str | None = None,
        mark_status: str | None = None,
        evidence_update: dict[str, Any] | None = None,
    ) -> None:
        updated = dict(row)
        updated["status"] = status
        if confirmed_funding_rate is not None:
            updated["confirmed_funding_rate"] = confirmed_funding_rate
        if settlement_mark_price is not None:
            updated["settlement_mark_price"] = settlement_mark_price
        if funding_pnl is not None:
            updated["funding_pnl"] = funding_pnl
        if rate_status is not None:
            updated["rate_status"] = rate_status
        if mark_status is not None:
            updated["mark_status"] = mark_status
        if evidence_update:
            evidence = dict(updated.get("evidence") or {})
            evidence.update(evidence_update)
            updated["evidence"] = evidence
        self.store.upsert_funding_settlement_reconciliation(updated)

    def _maybe_finalize_cycle_after_reconciliation(
        self,
        position_id: str,
        cycle_id: str,
    ) -> None:
        if not cycle_id:
            return
        leg_rows = self.store.funding_settlement_reconciliation_rows(
            position_id, cycle_id=cycle_id,
        )
        if not leg_rows:
            return
        all_resolved = all(
            str(r.get("status") or "") in {
                "RATE_AND_MARK_RECONCILED", "PUBLIC_RATE_CONFIRMED", "UNRECONCILED",
            }
            for r in leg_rows
        )
        if not all_resolved:
            return
        has_unreconciled = any(
            str(r.get("status") or "") == "UNRECONCILED" for r in leg_rows
        )
        if has_unreconciled:
            self.store.update_funding_capture_cycle_state(
                cycle_id, "UNRECONCILED", reconciliation_status="UNRECONCILED",
            )
            return
        all_reconciled = all(
            str(r.get("status") or "") == "RATE_AND_MARK_RECONCILED"
            for r in leg_rows
        )
        if all_reconciled:
            total_funding = sum(float(r.get("funding_pnl") or 0.0) for r in leg_rows)
            self.store.update_funding_capture_cycle_state(
                cycle_id,
                "RECONCILED",
                reconciliation_status="RATE_AND_MARK_RECONCILED",
                reconciled_funding_pnl=total_funding,
            )

    def _maybe_finalize_position_after_reconciliation(
        self,
        position_id: str,
    ) -> None:
        position = self.store.funding_capture_position_by_id(position_id)
        if position is None:
            return
        state = str(position.get("state") or "")
        if state not in ("CLOSED_PENDING_RECONCILIATION",):
            return
        cycles = self.store.funding_capture_cycles_for_position(position_id)
        if not cycles:
            return
        captured_cycles = [
            c for c in cycles
            if str(c.get("state") or "") in {
                "RECONCILED", "UNRECONCILED", "SETTLEMENT_CROSSED",
            }
        ]
        if not captured_cycles:
            return
        all_settled = all(
            str(c.get("state") or "") in {"RECONCILED", "UNRECONCILED"}
            for c in captured_cycles
        )
        if not all_settled:
            return
        has_unreconciled = any(
            str(c.get("state") or "") == "UNRECONCILED" for c in captured_cycles
        )
        if has_unreconciled:
            self.store.update_funding_capture_position_state(
                position_id, "UNRECONCILED",
            )
            return
        total_reconciled_funding = sum(
            float(c.get("reconciled_funding_pnl") or 0.0) for c in captured_cycles
        )
        price_pnl = 0.0
        total_fees = float(position.get("paper_open_fees") or 0.0) + float(position.get("paper_close_fees") or 0.0)
        emergency_cost = float(position.get("paper_emergency_unwind_cost") or 0.0)
        for ledger_row in self.store.paper_event_ledger_rows(position_id):
            if str(ledger_row.get("event_type") or "") == "price_pnl":
                price_pnl += float(ledger_row.get("cash_delta") or 0.0)
        reconciled_net = price_pnl + total_reconciled_funding - total_fees - emergency_cost
        self.store.update_funding_capture_position_state(
            position_id, "RECONCILED",
            paper_net_pnl_reconciled=reconciled_net,
        )

    def record_current_executable_pnl(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Store executable PnL for an open synchronized position.

        This is the runtime poll value used by risk/hold logic. It includes
        confirmed reconciled funding only and never includes expected funding
        or spread-convergence assumptions.
        """
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        snapshot = self._current_executable_pnl_snapshot(position, route, now)
        observed_at = now.astimezone(UTC).isoformat()
        self.store.upsert_funding_capture_observation(
            {
                "observation_id": f"{cycle_id}:current_pnl:{observed_at}",
                "position_id": position_id,
                "cycle_id": cycle_id,
                "phase": "current_pnl",
                "observed_at": observed_at,
                "long_response_received_at": snapshot.get("long_response_received_at"),
                "short_response_received_at": snapshot.get("short_response_received_at"),
                "cross_venue_skew_ms": snapshot.get("cross_venue_skew_ms"),
                "long_mark": snapshot.get("long_mark"),
                "short_mark": snapshot.get("short_mark"),
                "long_index": snapshot.get("long_index"),
                "short_index": snapshot.get("short_index"),
                "long_next_funding_at": snapshot.get("long_next_funding_at"),
                "short_next_funding_at": snapshot.get("short_next_funding_at"),
                "long_next_funding_rate": snapshot.get("long_next_funding_rate"),
                "short_next_funding_rate": snapshot.get("short_next_funding_rate"),
                "gross_funding_pnl": None,
                "long_close_vwap": snapshot.get("long_exit_price"),
                "short_close_vwap": snapshot.get("short_exit_price"),
                "current_exit_spread": snapshot.get("current_exit_spread"),
                "paper_net_if_exit_now": snapshot.get("paper_net_if_exit_now"),
                "snapshot_valid": snapshot.get("quality") == "EXECUTABLE_FULL_DEPTH",
                "invalid_reason": None
                if snapshot.get("quality") == "EXECUTABLE_FULL_DEPTH"
                else snapshot.get("quality"),
            }
        )
        if snapshot.get("quality") != "EXECUTABLE_FULL_DEPTH":
            return {"decision": "skipped", **snapshot}
        self.store.update_funding_capture_position_state(
            position_id,
            str(position.get("state") or "OPEN"),
            now,
            paper_net_pnl_estimated=snapshot["paper_net_if_exit_now"],
        )
        return {"decision": "recorded", **snapshot}

    def _current_executable_pnl_snapshot(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        if route is None:
            return {
                "quality": "INVALID_MISSING_BOOK",
                "reason": "route_missing",
                "snapshot_age_seconds": math.inf,
                "risk_state": "DEGRADED",
            }
        quantity = float(position.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "quality": "INVALID_MISSING_BOOK",
                "reason": "quantity_missing",
                "snapshot_age_seconds": math.inf,
                "risk_state": "DEGRADED",
            }

        legs = route.get("legs") or []
        long_route_leg = leg_by_side(legs, "long") or {}
        short_route_leg = leg_by_side(legs, "short") or {}
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        long_entry_price = float(
            optional_float(long_entry_leg.get("entry_fill_price"))
            or optional_float(long_entry_leg.get("vwap"))
            or 0.0
        )
        short_entry_price = float(
            optional_float(short_entry_leg.get("entry_fill_price"))
            or optional_float(short_entry_leg.get("vwap"))
            or 0.0
        )
        long_response = _leg_response_time(long_route_leg)
        short_response = _leg_response_time(short_route_leg)
        now_utc = now.astimezone(UTC)
        long_age = (
            (now_utc - long_response).total_seconds()
            if long_response is not None
            else math.inf
        )
        short_age = (
            (now_utc - short_response).total_seconds()
            if short_response is not None
            else math.inf
        )
        cross_skew = (
            abs((long_response - short_response).total_seconds())
            if long_response is not None and short_response is not None
            else math.inf
        )
        base = {
            "long_response_received_at": long_response.isoformat() if long_response else None,
            "short_response_received_at": short_response.isoformat() if short_response else None,
            "long_age_seconds": long_age,
            "short_age_seconds": short_age,
            "snapshot_age_seconds": max(long_age, short_age),
            "cross_venue_skew_seconds": cross_skew,
            "cross_venue_skew_ms": None if not math.isfinite(cross_skew) else cross_skew * 1000.0,
            "long_mark": optional_float(long_route_leg.get("mark_price")),
            "short_mark": optional_float(short_route_leg.get("mark_price")),
            "long_index": optional_float(long_route_leg.get("index_price")),
            "short_index": optional_float(short_route_leg.get("index_price")),
            "long_next_funding_at": long_route_leg.get("next_funding_at"),
            "short_next_funding_at": short_route_leg.get("next_funding_at"),
            "long_next_funding_rate": optional_float(long_route_leg.get("normalized_next_funding_rate")),
            "short_next_funding_rate": optional_float(short_route_leg.get("normalized_next_funding_rate")),
        }
        if long_age > 2.0 or short_age > 2.0 or cross_skew > 1.0:
            return {
                **base,
                "quality": "INVALID_STALE",
                "reason": "current_snapshot_stale_or_skewed",
                "risk_state": "DEGRADED",
                "long_entry_price": long_entry_price,
                "short_entry_price": short_entry_price,
            }
        if long_entry_price <= 0 or short_entry_price <= 0:
            return {
                **base,
                "quality": "INVALID_MISSING_BOOK",
                "reason": "entry_fill_price_missing",
                "risk_state": "DEGRADED",
            }
        long_fee_source = optional_float(long_route_leg.get("fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_route_leg.get("taker_fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_entry_leg.get("fee_rate"))
        if long_fee_source is None:
            long_fee_source = optional_float(long_entry_leg.get("taker_fee_rate"))
        short_fee_source = optional_float(short_route_leg.get("fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_route_leg.get("taker_fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_entry_leg.get("fee_rate"))
        if short_fee_source is None:
            short_fee_source = optional_float(short_entry_leg.get("taker_fee_rate"))
        if long_fee_source is None or short_fee_source is None:
            return {
                **base,
                "quality": "INVALID_MISSING_FEE",
                "reason": "fee_rate_missing",
                "risk_state": "DEGRADED",
            }
        long_levels = long_route_leg.get("bids") or []
        short_levels = short_route_leg.get("asks") or []
        if not long_levels or not short_levels:
            return {
                **base,
                "quality": "INVALID_MISSING_BOOK",
                "reason": "executable_orderbook_missing",
                "risk_state": "DEGRADED",
            }
        long_close = simulate_marketable_ioc(long_levels, "sell", quantity, EXECUTION_HAIRCUT_FRACTION)
        short_close = simulate_marketable_ioc(short_levels, "buy", quantity, EXECUTION_HAIRCUT_FRACTION)
        long_ratio = float(long_close["filled_quantity"] or 0.0) / quantity
        short_ratio = float(short_close["filled_quantity"] or 0.0) / quantity
        if long_ratio < 0.999 or short_ratio < 0.999:
            return {
                **base,
                "quality": "INVALID_INCOMPLETE_DEPTH",
                "reason": "full_ioc_depth_unavailable",
                "risk_state": "DEGRADED",
                "long_fill_ratio": long_ratio,
                "short_fill_ratio": short_ratio,
            }
        long_exit_price = float(long_close["average_fill_price"] or 0.0)
        short_exit_price = float(short_close["average_fill_price"] or 0.0)
        pnl = executable_paper_pnl(
            quantity=quantity,
            long_entry_price=long_entry_price,
            long_exit_price=long_exit_price,
            short_entry_price=short_entry_price,
            short_exit_price=short_exit_price,
            long_taker_fee=float(long_fee_source),
            short_taker_fee=float(short_fee_source),
            confirmed_funding_pnl=self.confirmed_funding_pnl_for_position(str(position["position_id"])),
            paper_open_fees=float(position.get("paper_open_fees") or 0.0),
            emergency_unwind_costs_already_incurred=float(
                position.get("paper_emergency_unwind_cost") or 0.0
            ),
        )
        return {
            **base,
            **pnl,
            "quality": "EXECUTABLE_FULL_DEPTH",
            "reason": None,
            "risk_state": "HEALTHY",
            "long_entry_price": long_entry_price,
            "short_entry_price": short_entry_price,
            "long_exit_price": long_exit_price,
            "short_exit_price": short_exit_price,
            "current_exit_spread": short_exit_price - long_exit_price,
            "long_fill_ratio": long_ratio,
            "short_fill_ratio": short_ratio,
            "long_fee_rate": float(long_fee_source),
            "short_fee_rate": float(short_fee_source),
        }

    def _ledger_cash_sum(
        self,
        position_id: str,
        *,
        venue: str,
        event_type: str | None = None,
    ) -> float:
        total = 0.0
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("venue") or "") != str(venue):
                continue
            if event_type is not None and str(row.get("event_type") or "") != event_type:
                continue
            total += float(row.get("cash_delta") or 0.0)
        return total

    def _reserved_collateral_amount(self, position_id: str, venue: str) -> float:
        reserve_key = collateral_reserve_event_key(position_id, venue)
        for row in self.store.paper_event_ledger_rows(position_id):
            if str(row.get("event_key") or "") != reserve_key:
                continue
            return float(optional_float((row.get("payload") or {}).get("amount")) or 0.0)
        return 0.0

    def _leg_margin_snapshot(
        self,
        *,
        position_id: str,
        side: str,
        venue: str,
        quantity: float,
        entry_price: float,
        mark: float,
        fee_rate_source_leg: dict[str, Any],
    ) -> dict[str, Any]:
        reserved_collateral = self._reserved_collateral_amount(position_id, venue)
        confirmed_funding = self._ledger_cash_sum(position_id, venue=venue, event_type="funding")
        allocated_fees = -self._ledger_cash_sum(position_id, venue=venue, event_type="order_fee")
        if side == "long":
            upnl = quantity * (mark - entry_price)
        else:
            upnl = quantity * (entry_price - mark)
        mmr_value = optional_float(fee_rate_source_leg.get("maintenance_margin_rate"))
        mmr_source = "venue"
        if mmr_value is None:
            mmr_value = float(getattr(self.config, "paper_mmr", 0.02) or 0.02)
            mmr_source = "paper_fallback"
        mmr = float(mmr_value)
        maintenance_margin = abs(quantity * mark) * mmr
        leg_equity = reserved_collateral + upnl + confirmed_funding - allocated_fees
        margin_safety = leg_equity / maintenance_margin if maintenance_margin > 0 else 0.0
        q = max(quantity, 1e-12)
        if side == "long":
            liquidation_price = (
                q * entry_price - reserved_collateral - confirmed_funding + allocated_fees
            ) / max(q * (1.0 - mmr), 1e-12)
            liquidation_price = max(0.0, liquidation_price)
            liquidation_distance = max(0.0, (mark - liquidation_price) / max(mark, 1e-12))
        else:
            liquidation_price = (
                reserved_collateral + confirmed_funding - allocated_fees + q * entry_price
            ) / max(q * (1.0 + mmr), 1e-12)
            liquidation_distance = max(0.0, (liquidation_price - mark) / max(mark, 1e-12))
        return {
            "venue": venue,
            "side": side,
            "reserved_collateral": reserved_collateral,
            "confirmed_funding": confirmed_funding,
            "allocated_fees": allocated_fees,
            "upnl": upnl,
            "leg_equity": leg_equity,
            "maintenance_margin": maintenance_margin,
            "maintenance_margin_rate": mmr,
            "mmr_source": mmr_source,
            "margin_safety_ratio": margin_safety,
            "liquidation_price": liquidation_price,
            "liquidation_distance_fraction": liquidation_distance,
        }

    def poll_synchronized_position_risk(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        current_executable_pnl: dict[str, Any],
        active_cycle: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        position_id = str(position["position_id"])
        quality = str(current_executable_pnl.get("quality") or "")
        snapshot_age_value = current_executable_pnl.get("snapshot_age_seconds")
        snapshot_age = float(snapshot_age_value) if snapshot_age_value is not None else math.inf
        if quality != "EXECUTABLE_FULL_DEPTH":
            if snapshot_age > 5.0 or not math.isfinite(snapshot_age):
                return {
                    "close_reason": "risk_data_hard_stale",
                    "reasons": [str(current_executable_pnl.get("reason") or quality)],
                    "current_executable_pnl": current_executable_pnl,
                }
            return None

        quantity = float(position.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "close_reason": "quantity_missing",
                "reasons": ["quantity_missing"],
                "current_executable_pnl": current_executable_pnl,
            }
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        legs = route.get("legs") if route else []
        long_route_leg = leg_by_side(legs or [], "long") or {}
        short_route_leg = leg_by_side(legs or [], "short") or {}
        long_entry_price = float(
            optional_float(long_entry_leg.get("entry_fill_price"))
            or optional_float(long_entry_leg.get("vwap"))
            or 0.0
        )
        short_entry_price = float(
            optional_float(short_entry_leg.get("entry_fill_price"))
            or optional_float(short_entry_leg.get("vwap"))
            or 0.0
        )
        long_mark = optional_float(long_route_leg.get("mark_price"))
        short_mark = optional_float(short_route_leg.get("mark_price"))
        long_index = optional_float(long_route_leg.get("index_price"))
        short_index = optional_float(short_route_leg.get("index_price"))
        if (
            long_entry_price <= 0
            or short_entry_price <= 0
            or long_mark is None
            or short_mark is None
            or long_mark <= 0
            or short_mark <= 0
        ):
            return {
                "close_reason": "risk_mark_or_entry_missing",
                "reasons": ["risk_mark_or_entry_missing"],
                "current_executable_pnl": current_executable_pnl,
            }

        reference_price = (float(long_mark) + float(short_mark)) / 2.0
        entry_spread = short_entry_price - long_entry_price
        current_exit_spread = float(current_executable_pnl["short_exit_price"]) - float(
            current_executable_pnl["long_exit_price"]
        )
        edge_bps = optional_float(position.get("current_cycle_conservative_funding_edge_bps"))
        if edge_bps is None and active_cycle is not None:
            edge_bps = optional_float(active_cycle.get("conservative_funding_edge_bps"))
        if edge_bps is None:
            conservative_gross = optional_float(position.get("current_cycle_conservative_funding_gross"))
            reference_notional_for_edge = min(quantity * float(long_mark), quantity * float(short_mark))
            edge_bps = (
                conservative_gross / reference_notional_for_edge * 10_000.0
                if conservative_gross is not None and reference_notional_for_edge > 0
                else 0.0
            )
        basis_decision = dynamic_basis_stop_decision(
            entry_spread=entry_spread,
            current_exit_spread=current_exit_spread,
            reference_price=reference_price,
            active_cycle_conservative_funding_edge_bps=max(0.0, float(edge_bps or 0.0)),
        )
        if basis_decision["immediate_hard_exit"]:
            return {
                "close_reason": "basis_deterioration",
                "reasons": [
                    f"deterioration_{basis_decision['basis_deterioration_bps']:.1f}bps>=budget_{basis_decision['active_risk_budget_bps']:.1f}bps"
                ],
                "dynamic_basis": basis_decision,
                "current_executable_pnl": current_executable_pnl,
            }

        long_margin = self._leg_margin_snapshot(
            position_id=position_id,
            side="long",
            venue=str(position.get("long_venue") or long_entry_leg.get("venue") or ""),
            quantity=quantity,
            entry_price=long_entry_price,
            mark=float(long_mark),
            fee_rate_source_leg=long_route_leg,
        )
        short_margin = self._leg_margin_snapshot(
            position_id=position_id,
            side="short",
            venue=str(position.get("short_venue") or short_entry_leg.get("venue") or ""),
            quantity=quantity,
            entry_price=short_entry_price,
            mark=float(short_mark),
            fee_rate_source_leg=short_route_leg,
        )
        min_liquidation_distance = min(
            float(long_margin["liquidation_distance_fraction"]),
            float(short_margin["liquidation_distance_fraction"]),
        )
        min_margin_safety = min(
            float(long_margin["margin_safety_ratio"]),
            float(short_margin["margin_safety_ratio"]),
        )
        mark_index_bps = 0.0
        for mark, index in ((long_mark, long_index), (short_mark, short_index)):
            if mark is None or index is None or index <= 0:
                return {
                    "close_reason": "risk_mark_index_missing",
                    "reasons": ["risk_mark_index_missing"],
                    "current_executable_pnl": current_executable_pnl,
                }
            mark_index_bps = max(mark_index_bps, abs(float(mark) - float(index)) / float(index) * 10_000.0)

        triggered, trigger_reason = hard_risk_triggered(
            liquidation_distance_fraction=min_liquidation_distance,
            margin_safety_ratio=min_margin_safety,
            mark_index_divergence_bps=mark_index_bps,
            snapshot_age_seconds=snapshot_age,
        )
        risk_gates = {
            "liquidation_distance": min_liquidation_distance,
            "margin_safety_ratio": min_margin_safety,
            "mark_index_divergence_bps": mark_index_bps,
            "long_margin": long_margin,
            "short_margin": short_margin,
        }
        if triggered:
            return {
                "close_reason": f"risk_hard_exit:{trigger_reason}",
                "reasons": [trigger_reason],
                "risk_gates": risk_gates,
                "current_executable_pnl": current_executable_pnl,
            }

        reference_notional = min(quantity * float(long_mark), quantity * float(short_mark))
        active_budget_usd = reference_notional * float(basis_decision["active_risk_budget_bps"]) / 10_000.0
        executable_breach = float(current_executable_pnl["paper_net_if_exit_now"]) <= -active_budget_usd
        config = dict(position.get("config") or {})
        risk_state = dict(config.get("risk_state") or {})
        previous_breach = bool(risk_state.get("executable_pnl_breach"))
        risk_state["executable_pnl_breach"] = executable_breach
        risk_state["last_checked_at"] = now.astimezone(UTC).isoformat()
        risk_state["last_paper_net_if_exit_now"] = current_executable_pnl["paper_net_if_exit_now"]
        risk_state["active_risk_budget_usd"] = active_budget_usd
        risk_state["last_margin_safety_ratio"] = min_margin_safety
        risk_state["last_liquidation_distance_fraction"] = min_liquidation_distance
        risk_state["last_mark_index_divergence_bps"] = mark_index_bps
        config["risk_state"] = risk_state
        self.store.update_funding_capture_position_config(position_id, config)
        if executable_breach and previous_breach:
            return {
                "close_reason": "executable_pnl_breach",
                "reasons": [
                    f"paper_net_if_exit_now_{current_executable_pnl['paper_net_if_exit_now']:.2f}<=-budget_{active_budget_usd:.2f}"
                ],
                "risk_gates": risk_gates,
                "dynamic_basis": basis_decision,
                "current_executable_pnl": current_executable_pnl,
            }

        return None

    def consider_route(
        self,
        route: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        now = self.clock.now()
        route_key = str(route.get("route_key") or "")
        if not route_key:
            return {"opened": False, "reason": "route_key_missing"}
        settlement_at = route_next_settlement(route)
        if settlement_at is None:
            return {"opened": False, "reason": "settlement_alignment_missing"}
        lead = (settlement_at - now.astimezone(UTC)).total_seconds()
        capture_id = capture_position_id_for_route(route)
        existing_open = self.store.funding_capture_open_position_by_route_key(route_key)
        if existing_open is not None:
            return {"opened": False, "reason": "route_already_open", "position_id": existing_open["position_id"]}
        self._ensure_discovered_or_armed(route, capture_id, settlement_at, lead, now)
        missing_data = self._missing_required_route_data(route)
        if missing_data:
            return {"opened": False, "reason": "required_route_data_missing", "missing": missing_data}
        observation = build_focused_observation(route, now=now)
        self._store_observation(route, capture_id, settlement_at, observation, phase="entry")
        observations = self._valid_observations(route_key, now, phase="entry", cycle_id=f"{capture_id}:1")
        underwriting = entry_underwriting(observations, now=now)
        if not underwriting["eligible"]:
            return {
                "opened": False,
                "reason": "focused_underwriting_not_ready",
                "underwriting": underwriting,
            }
        if not (float(self.config.entry_min_lead_seconds) <= lead <= float(self.config.entry_max_lead_seconds)):
            return {"opened": False, "reason": "outside_entry_window", "lead_seconds": lead}
        economics = self._initial_economics(route, observations, underwriting)
        if not economics["eligible"]:
            return {"opened": False, "reason": "initial_economics_failed", "economics": economics}
        risk_gate = self._entry_risk_gate(route)
        if not risk_gate["passed"]:
            return {"opened": False, "reason": "entry_risk_gate_failed", "risk_gate": risk_gate}
        account_check = self._enforce_account_limits(route, accounts)
        if not account_check["passed"]:
            return {"opened": False, "reason": account_check["reason"], "account_check": account_check}
        execution = self._execute_entry(route, capture_id, settlement_at, now)
        if execution["state"] != "OPEN":
            return {"opened": False, "reason": "entry_execution_failed", "execution": execution}
        self._mark_open(route, capture_id, settlement_at, economics, execution, now)
        return {
            "opened": True,
            "position_id": capture_id,
            "cycle_id": f"{capture_id}:1",
            "economics": economics,
            "execution": execution,
            "underwriting": underwriting,
        }

    def _ensure_discovered_or_armed(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        lead: float,
        now: datetime,
    ) -> None:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        route_key = str(route.get("route_key") or "")
        state = "ARMED" if lead <= float(self.config.arm_window_seconds) else "DISCOVERED"
        self.store.upsert_funding_capture_position(
            {
                "position_id": capture_id,
                "strategy_name": STRATEGY_NAME,
                "strategy_version": STRATEGY_VERSION,
                "canonical_asset": route.get("canonical_asset", ""),
                "long_venue": long_leg.get("venue") or route.get("long_venue") or "",
                "long_symbol": long_leg.get("symbol") or route.get("long_symbol") or "",
                "short_venue": short_leg.get("venue") or route.get("short_venue") or "",
                "short_symbol": short_leg.get("symbol") or route.get("short_symbol") or "",
                "quantity": 0.0,
                "target_notional": float(route.get("target_notional") or self.config.target_notional_per_leg),
                "state": state,
                "opened_at": now.isoformat(),
                "config": {
                    "route_key": route_key,
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": legs,
                },
            }
        )
        if state == "ARMED":
            self.store.upsert_funding_capture_cycle(
                {
                    "cycle_id": f"{capture_id}:1",
                    "position_id": capture_id,
                    "cycle_number": 1,
                    "scheduled_funding_at": settlement_at.isoformat(),
                    "state": "ARMED",
                    "decision": "ARMED",
                    "decision_reason": "focused_observations_collecting",
                }
            )

    def _store_observation(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        observation: dict[str, Any],
        *,
        phase: str,
        cycle_id: str | None = None,
    ) -> None:
        route_key = str(route.get("route_key") or "")
        resolved_cycle_id = cycle_id or f"{capture_id}:1"
        observation_phase = phase or observation.get("phase") or "entry"
        bucket = _observation_bucket(route_key, observation_phase, resolved_cycle_id)
        observations = self.observations_by_route.setdefault(bucket, [])
        observations.append(observation)
        if len(observations) > 120:
            del observations[:-120]
        self.store.upsert_funding_capture_observation(
            {
                **observation,
                "position_id": capture_id,
                "cycle_id": resolved_cycle_id,
                "phase": observation_phase,
                "observation_id": f"{resolved_cycle_id}:{observation_phase}:{len(observations):04d}:{observation['observed_at']}",
            }
        )

    def _valid_observations(
        self,
        route_key: str,
        now: datetime,
        *,
        phase: str,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        bucket = _observation_bucket(route_key, phase, cycle_id)
        valid = []
        for row in self.observations_by_route.get(bucket, []):
            if str(row.get("phase") or phase) != phase:
                continue
            if validate_focused_observation(row, now=now)["valid"]:
                valid.append(row)
        return valid

    def _initial_economics(
        self,
        route: dict[str, Any],
        observations: list[dict[str, Any]],
        underwriting: dict[str, Any],
    ) -> dict[str, Any]:
        latest = observations[-1]
        q = self._target_quantity(route)
        long_open = float(latest.get("long_open_vwap") or 0.0)
        short_open = float(latest.get("short_open_vwap") or 0.0)
        long_close = float(latest.get("long_close_vwap") or long_open)
        short_close = float(latest.get("short_close_vwap") or short_open)
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        price_pnl_if_flat_now = q * (long_close - long_open) + q * (short_open - short_close)
        baseline_book_cost = max(0.0, -price_pnl_if_flat_now)
        fees = (
            q * long_open * _leg_fee_rate(long_leg)
            + q * long_close * _leg_fee_rate(long_leg)
            + q * short_open * _leg_fee_rate(short_leg)
            + q * short_close * _leg_fee_rate(short_leg)
        )
        reference = min(q * long_open, q * short_open)
        sorted_obs = sorted(observations, key=lambda o: str(o.get("observed_at") or ""))
        adverse_exit_spread_changes_bps: list[float] = []
        for previous, current in zip(sorted_obs, sorted_obs[1:]):
            previous_spread = optional_float(previous.get("current_exit_spread"))
            current_spread = optional_float(current.get("current_exit_spread"))
            if previous_spread is None or current_spread is None:
                continue
            adverse_change = max(0.0, current_spread - previous_spread)
            adverse_exit_spread_changes_bps.append(
                adverse_change / max(long_open, short_open, 1e-12) * 10_000.0
            )
        if len(adverse_exit_spread_changes_bps) >= 10:
            from smart_money_radar.funding.strategy_synchronized_funding import percentile_95
            basis_reserve_bps = max(
                15.0,
                min(50.0, 3.0 * percentile_95(adverse_exit_spread_changes_bps)),
            )
        else:
            basis_reserve_bps = 30.0
        mark_returns: list[float] = []
        for previous, current in zip(sorted_obs, sorted_obs[1:]):
            step_returns: list[float] = []
            for key in ("long_mark", "short_mark"):
                previous_mark = optional_float(previous.get(key))
                current_mark = optional_float(current.get(key))
                if previous_mark is not None and previous_mark > 0 and current_mark is not None:
                    step_returns.append(abs(current_mark - previous_mark) / previous_mark * 10_000.0)
            if step_returns:
                mark_returns.append(max(step_returns))
        if len(mark_returns) >= 10:
            from smart_money_radar.funding.strategy_synchronized_funding import percentile_95
            legging_reserve_bps = min(30.0, max(10.0, 2.0 * percentile_95(mark_returns)))
        else:
            legging_reserve_bps = 15.0
        basis_reserve_usd = reference * basis_reserve_bps / 10_000.0
        legging_reserve_usd = reference * legging_reserve_bps / 10_000.0
        result = initial_entry_economics(
            conservative_funding_gross=float(underwriting["conservative_funding_gross"]),
            baseline_round_trip_book_cost=baseline_book_cost,
            total_round_trip_fee_estimate=fees,
            entry_basis_reserve_usd=basis_reserve_usd,
            entry_legging_reserve_usd=legging_reserve_usd,
            reference_notional=reference,
        )
        result["entry_basis_reserve_bps"] = basis_reserve_bps
        result["entry_legging_reserve_bps"] = legging_reserve_bps
        return result

    def _compute_common_quantity(
        self,
        long_price: float,
        short_price: float,
        long_step: float,
        short_step: float,
        long_min_qty: float,
        short_min_qty: float,
        long_min_notional: float,
        short_min_notional: float,
    ) -> dict[str, Any]:
        if long_price <= 0 or short_price <= 0:
            return {"quantity": 0.0, "reason": "zero_price"}
        target = float(self.config.target_notional_per_leg)
        q_raw = min(target / long_price, target / short_price)
        if long_step > 0 and short_step > 0:
            scale = 10**8
            long_units = max(1, round(long_step * scale))
            short_units = max(1, round(short_step * scale))
            common_units = (long_units * short_units) // math.gcd(long_units, short_units)
            common_step = common_units / scale
        else:
            return {"quantity": 0.0, "reason": "missing_quantity_step"}
        if common_step <= 0:
            return {"quantity": 0.0, "reason": "invalid_common_step"}
        q = math.floor(q_raw / common_step) * common_step
        long_notional = q * long_price
        short_notional = q * short_price
        if q < max(long_min_qty, short_min_qty):
            return {"quantity": 0.0, "reason": "below_min_quantity"}
        if long_notional < long_min_notional:
            return {"quantity": 0.0, "reason": "below_long_min_notional"}
        if short_notional < short_min_notional:
            return {"quantity": 0.0, "reason": "below_short_min_notional"}
        if long_notional < 450 or short_notional < 450:
            return {"quantity": 0.0, "reason": "below_notional_floor_450"}
        if long_notional > 500 or short_notional > 500:
            return {"quantity": 0.0, "reason": "above_notional_cap_500"}
        return {"quantity": q, "reason": None}

    def _target_quantity(self, route: dict[str, Any]) -> float:
        result = self._compute_target_quantity(route)
        return result["quantity"]

    def _compute_target_quantity(self, route: dict[str, Any]) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_price = float(long_leg.get("open_vwap") or long_leg.get("vwap") or long_leg.get("mark_price") or 0.0)
        short_price = float(short_leg.get("open_vwap") or short_leg.get("vwap") or short_leg.get("mark_price") or 0.0)
        for side, leg in (("long", long_leg), ("short", short_leg)):
            if optional_float(leg.get("quantity_step")) is None:
                return {"quantity": 0.0, "reason": f"{side}_quantity_step_missing"}
            if optional_float(leg.get("min_quantity")) is None:
                return {"quantity": 0.0, "reason": f"{side}_min_quantity_missing"}
            if optional_float(leg.get("min_notional")) is None:
                return {"quantity": 0.0, "reason": f"{side}_min_notional_missing"}
        long_step = float(long_leg.get("quantity_step") or 0.0)
        short_step = float(short_leg.get("quantity_step") or 0.0)
        long_min_qty = float(long_leg.get("min_quantity") or 0.0)
        short_min_qty = float(short_leg.get("min_quantity") or 0.0)
        long_min_notional = float(long_leg.get("min_notional") or 0.0)
        short_min_notional = float(short_leg.get("min_notional") or 0.0)
        return self._compute_common_quantity(
            long_price=long_price,
            short_price=short_price,
            long_step=long_step,
            short_step=short_step,
            long_min_qty=long_min_qty,
            short_min_qty=short_min_qty,
            long_min_notional=long_min_notional,
            short_min_notional=short_min_notional,
        )

    def _enforce_account_limits(
        self,
        route: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_venue = str(long_leg.get("venue") or "")
        short_venue = str(short_leg.get("venue") or "")
        quantity_result = self._compute_target_quantity(route)
        quantity = float(quantity_result.get("quantity") or 0.0)
        if quantity <= 0:
            return {
                "passed": False,
                "reason": str(quantity_result.get("reason") or "quantity_invalid"),
            }
        max_total = int(getattr(self.config, "max_open_positions_total", 1))
        max_per_venue = int(getattr(self.config, "max_open_positions_per_venue", 1))
        max_exposure = float(getattr(self.config, "max_gross_exposure_usd", 1_000.0))
        open_positions = self.store.funding_capture_open_positions()
        active = [
            p for p in open_positions
            if str(p.get("state") or "") not in {
                "CLOSED_PENDING_RECONCILIATION", "RECONCILED",
                "UNRECONCILED", "FAILED",
            }
        ]
        if len(active) >= max_total:
            return {"passed": False, "reason": "max_open_positions_total"}
        venue_counts: dict[str, int] = {}
        current_exposure = 0.0
        for p in active:
            for v in (str(p.get("long_venue") or ""), str(p.get("short_venue") or "")):
                if v:
                    venue_counts[v] = venue_counts.get(v, 0) + 1
            current_exposure += float(p.get("target_notional") or 0.0) * 2.0
        for v in (long_venue, short_venue):
            if v and venue_counts.get(v, 0) >= max_per_venue:
                return {"passed": False, "reason": "max_open_positions_per_venue"}
        long_price = float(long_leg.get("open_vwap") or long_leg.get("vwap") or long_leg.get("mark_price") or 0.0)
        short_price = float(short_leg.get("open_vwap") or short_leg.get("vwap") or short_leg.get("mark_price") or 0.0)
        long_notional = quantity * long_price
        short_notional = quantity * short_price
        if current_exposure + long_notional + short_notional > max_exposure:
            return {"passed": False, "reason": "max_gross_exposure_usd"}
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        reserve_fraction = float(getattr(self.config, "collateral_reserve_fraction", 0.25))
        for side, leg, leg_notional in (
            ("long", long_leg, long_notional),
            ("short", short_leg, short_notional),
        ):
            venue = str(leg.get("venue") or "")
            if leg_notional <= 0:
                return {"passed": False, "reason": f"{side}_price_missing"}
            isolated_collateral = leg_notional / leverage
            collateral_reserve = leg_notional * reserve_fraction
            fee_rate = _leg_fee_rate(leg)
            estimated_open_fee = leg_notional * fee_rate
            required_cash = isolated_collateral + collateral_reserve + estimated_open_fee
            account = accounts.get(venue) or {}
            available = float(account.get("available_balance") or 0.0)
            if available < required_cash:
                return {
                    "passed": False,
                    "reason": "insufficient_paper_balance",
                    "venue": venue,
                    "required": required_cash,
                    "available": available,
                }
        return {"passed": True, "reason": None}

    def _reserve_collateral(self, capture_id: str, route: dict[str, Any]) -> None:
        legs = route.get("legs") or []
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        reserve_fraction = float(getattr(self.config, "collateral_reserve_fraction", 0.25))
        quantity = float(self._target_quantity(route) or 0.0)
        for leg in legs:
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            price = float(leg.get("open_vwap") or leg.get("vwap") or leg.get("mark_price") or 0.0)
            leg_notional = quantity * price
            if leg_notional <= 0:
                continue
            amount = leg_notional / leverage + leg_notional * reserve_fraction
            event_key = collateral_reserve_event_key(capture_id, venue)
            reserved = self._record_ledger_entry(
                make_ledger_entry(
                    event_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_reserve",
                    cash_delta=0.0,
                    payload={"amount": amount},
                )
            )
            if reserved is not None:
                self.store.update_funding_paper_account_reserved(venue, amount)

    def _release_collateral(self, capture_id: str, route: dict[str, Any]) -> None:
        legs = route.get("legs") or []
        for leg in legs:
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            reserve_key = collateral_reserve_event_key(capture_id, venue)
            release_key = collateral_release_event_key(capture_id, venue)
            existing_reserve = self.store.paper_event_ledger_rows(capture_id)
            reserve_row = next(
                (row for row in existing_reserve if row["event_key"] == reserve_key),
                None,
            )
            if reserve_row is None:
                continue
            already_released = any(r["event_key"] == release_key for r in existing_reserve)
            if already_released:
                continue
            amount = float(optional_float((reserve_row.get("payload") or {}).get("amount")) or 0.0)
            if amount <= 0:
                continue
            released = self._record_ledger_entry(
                make_ledger_entry(
                    release_key,
                    position_id=capture_id,
                    venue=venue,
                    event_type="collateral_release",
                    cash_delta=0.0,
                    payload={"amount": amount},
                )
            )
            if released is not None:
                self.store.update_funding_paper_account_reserved(venue, -amount)

    def _entry_risk_gate(self, route: dict[str, Any]) -> dict[str, Any]:
        legs = route.get("legs") or []
        max_divergence = 0.0
        q = self._target_quantity(route)
        if q <= 0:
            return entry_risk_gates(
                liquidation_distance_fraction=0.0,
                margin_safety_ratio=0.0,
                mark_index_divergence_bps=math.inf,
            )
        leverage = float(getattr(self.config, "leverage", 1.0) or 1.0)
        min_liquidation_distance = math.inf
        min_margin_safety = math.inf
        for leg in legs:
            mark = float(leg.get("mark_price") or 0.0)
            index = float(leg.get("index_price") or 0.0)
            entry_price = float(leg.get("open_vwap") or leg.get("vwap") or mark or 0.0)
            if mark <= 0 or index <= 0 or entry_price <= 0:
                return entry_risk_gates(
                    liquidation_distance_fraction=0.0,
                    margin_safety_ratio=0.0,
                    mark_index_divergence_bps=math.inf,
                )
            max_divergence = max(max_divergence, abs(mark - index) / index * 10_000.0)
            notional = q * entry_price
            isolated_collateral = notional / leverage
            mmr = float(leg.get("maintenance_margin_rate") or getattr(self.config, "paper_mmr", 0.02) or 0.02)
            maintenance_margin = notional * mmr
            margin_safety = isolated_collateral / maintenance_margin if maintenance_margin > 0 else 0.0
            min_margin_safety = min(min_margin_safety, margin_safety)
            liquidation_buffer_usd = max(0.0, isolated_collateral - maintenance_margin)
            liquidation_move = liquidation_buffer_usd / max(q, 1e-12)
            if str(leg.get("side") or "").lower() == "long":
                liquidation_price = max(0.0, entry_price - liquidation_move)
                liquidation_distance = max(0.0, (mark - liquidation_price) / mark)
            else:
                liquidation_price = entry_price + liquidation_move
                liquidation_distance = max(0.0, (liquidation_price - mark) / mark)
            min_liquidation_distance = min(min_liquidation_distance, liquidation_distance)
        return entry_risk_gates(
            liquidation_distance_fraction=(
                min_liquidation_distance if math.isfinite(min_liquidation_distance) else 0.0
            ),
            margin_safety_ratio=(min_margin_safety if math.isfinite(min_margin_safety) else 0.0),
            mark_index_divergence_bps=max_divergence,
        )

    def _execute_entry(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        decision_at: datetime,
    ) -> dict[str, Any]:
        q = self._target_quantity(route)
        if q <= 0:
            return {"state": "FAILED", "reason": "zero_quantity", "quantity": 0.0, "target_quantity": 0.0,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_open = float(long_leg.get("open_vwap") or long_leg.get("vwap") or 0.0)
        short_open = float(short_leg.get("open_vwap") or short_leg.get("vwap") or 0.0)
        long_levels = long_leg.get("asks")
        short_levels = short_leg.get("bids")
        if not long_levels:
            return {"state": "FAILED", "reason": "executable_orderbook_missing",
                    "missing_side": "long_asks", "quantity": 0.0, "target_quantity": q,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        if not short_levels:
            return {"state": "FAILED", "reason": "executable_orderbook_missing",
                    "missing_side": "short_bids", "quantity": 0.0, "target_quantity": q,
                    "long_entry_price": 0.0, "short_entry_price": 0.0, "paper_open_fees": 0.0,
                    "filled_at": decision_at.isoformat(), "deadline_ok": False,
                    "long_fill_ratio": 0.0, "short_fill_ratio": 0.0, "quantity_mismatch_fraction": 0.0}
        self.store.update_funding_capture_position_state(capture_id, "ENTRY_SUBMITTED", decision_at)
        self._reserve_collateral(capture_id, route)
        submitted_at = decision_at + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        deadline_ok = t20_deadline_passed(filled_at, settlement_at, self.config.entry_fill_deadline_lead_seconds)
        long_fill = simulate_marketable_ioc(long_levels, "buy", q, EXECUTION_HAIRCUT_FRACTION)
        short_fill = simulate_marketable_ioc(short_levels, "sell", q, EXECUTION_HAIRCUT_FRACTION)
        long_fee = long_fill["notional"] * _leg_fee_rate(long_leg)
        short_fee = short_fill["notional"] * _leg_fee_rate(short_leg)
        fill_state = entry_fill_state(
            long_filled_quantity=long_fill["filled_quantity"],
            short_filled_quantity=short_fill["filled_quantity"],
            target_quantity=q,
        )
        state = fill_state["state"] if deadline_ok else "FAILED"
        if not deadline_ok and (long_fill["filled_quantity"] > 0 or short_fill["filled_quantity"] > 0):
            state = "PARTIALLY_HEDGED"
        cycle_id = f"{capture_id}:1"
        for side, leg, fill, fee in (
            ("long", long_leg, long_fill, long_fee),
            ("short", short_leg, short_fill, short_fee),
        ):
            order_id = f"{capture_id}:entry:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": capture_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "ENTRY",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": decision_at.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": fill["filled_quantity"],
                    "average_fill_price": fill["average_fill_price"],
                    "fee": fee,
                    "state": "FILLED" if fill["unfilled_quantity"] <= 1e-12 else "PARTIALLY_FILLED",
                    "payload": fill,
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id},
                )
            )
        if state in ("PARTIALLY_HEDGED", "FAILED") and (long_fill["filled_quantity"] > 0 or short_fill["filled_quantity"] > 0):
            self.store.update_funding_capture_position_state(capture_id, "PARTIALLY_HEDGED", filled_at)
            unwind = self._unwind_partial_entry(
                route=route,
                capture_id=capture_id,
                cycle_id=cycle_id,
                long_fill=long_fill,
                short_fill=short_fill,
                long_fee=long_fee,
                short_fee=short_fee,
                decision_at=decision_at,
                now=filled_at,
            )
            return {
                "state": "FAILED",
                "quantity": 0.0,
                "target_quantity": q,
                "long_entry_price": long_fill["average_fill_price"],
                "short_entry_price": short_fill["average_fill_price"],
                "paper_open_fees": long_fee + short_fee,
                "filled_at": filled_at.isoformat(),
                "deadline_ok": deadline_ok,
                "unwind": unwind,
                **fill_state,
            }
        if state == "FAILED":
            self._release_collateral(capture_id, route)
            self.store.update_funding_capture_position_state(
                capture_id,
                "FAILED",
                filled_at,
                closed_at=filled_at.isoformat(),
                paper_close_fees=0.0,
                paper_emergency_unwind_cost=0.0,
                paper_net_pnl_estimated=-(long_fee + short_fee),
            )
        return {
            "state": state,
            "quantity": min(long_fill["filled_quantity"], short_fill["filled_quantity"]),
            "target_quantity": q,
            "long_entry_price": long_fill["average_fill_price"],
            "short_entry_price": short_fill["average_fill_price"],
            "paper_open_fees": long_fee + short_fee,
            "filled_at": filled_at.isoformat(),
            "deadline_ok": deadline_ok,
            **fill_state,
        }

    def _unwind_partial_entry(
        self,
        *,
        route: dict[str, Any],
        capture_id: str,
        cycle_id: str,
        long_fill: dict[str, Any],
        short_fill: dict[str, Any],
        long_fee: float,
        short_fee: float,
        decision_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        long_unwind_qty = float(long_fill["filled_quantity"])
        short_unwind_qty = float(short_fill["filled_quantity"])
        long_entry_price = float(long_fill["average_fill_price"] or 0.0)
        short_entry_price = float(short_fill["average_fill_price"] or 0.0)
        long_exit_price = long_entry_price * (1.0 - 100.0 / 10_000.0) if long_entry_price > 0 else 0.0
        short_exit_price = short_entry_price * (1.0 + 100.0 / 10_000.0) if short_entry_price > 0 else 0.0
        long_price_pnl = long_unwind_qty * (long_exit_price - long_entry_price) if long_entry_price > 0 else 0.0
        short_price_pnl = short_unwind_qty * (short_entry_price - short_exit_price) if short_entry_price > 0 else 0.0
        total_price_pnl = long_price_pnl + short_price_pnl
        long_unwind_fee = long_unwind_qty * long_exit_price * _leg_fee_rate(long_leg)
        short_unwind_fee = short_unwind_qty * short_exit_price * _leg_fee_rate(short_leg)
        long_adverse_impact_usd = max(0.0, long_unwind_qty * (long_entry_price - long_exit_price))
        short_adverse_impact_usd = max(0.0, short_unwind_qty * (short_exit_price - short_entry_price))
        adverse_impact_usd = long_adverse_impact_usd + short_adverse_impact_usd
        emergency_cost = 0.0
        self.store.update_funding_capture_position_state(capture_id, "EMERGENCY_UNWIND", now)
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        for side, leg, qty, price, fee in (
            ("long", long_leg, long_unwind_qty, long_exit_price, long_unwind_fee),
            ("short", short_leg, short_unwind_qty, short_exit_price, short_unwind_fee),
        ):
            if qty <= 0:
                continue
            order_id = f"{capture_id}:unwind:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": capture_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "UNWIND",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": qty,
                    "average_fill_price": price,
                    "fee": fee,
                    "state": "FILLED",
                    "payload": {
                        "adverse_penalty_bps": 100.0,
                        "adverse_price_impact_bps": 100.0,
                        "adverse_price_impact_usd": (
                            long_adverse_impact_usd if side == "long" else short_adverse_impact_usd
                        ),
                        "pricing_quality": "partial_entry_unwind_100bps",
                    },
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=capture_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id, "intent": "unwind"},
                )
            )
        self._record_price_pnl_entries(
            position_id=capture_id,
            cycle_id=cycle_id,
            long_venue=str(long_leg.get("venue") or ""),
            short_venue=str(short_leg.get("venue") or ""),
            long_price_pnl=long_price_pnl,
            short_price_pnl=short_price_pnl,
            payload={
                "reason": "partial_entry_unwind",
                "pricing_quality": "partial_entry_unwind_100bps",
                "adverse_price_impact_bps": 100.0,
                "adverse_price_impact_usd": adverse_impact_usd,
            },
        )
        self._record_ledger_entry(
            make_ledger_entry(
                f"emergency_unwind:{capture_id}:partial_entry:diagnostic",
                position_id=capture_id,
                cycle_id=cycle_id,
                venue=str(long_leg.get("venue") or short_leg.get("venue") or ""),
                event_type="emergency_unwind_cost",
                cash_delta=0.0,
                payload={
                    "reason": "partial_entry_unwind",
                    "pricing_quality": "partial_entry_unwind_100bps",
                    "adverse_price_impact_bps": 100.0,
                    "adverse_price_impact_usd": adverse_impact_usd,
                    "cash_cost_policy": "penalty_in_fill_price_once",
                },
            )
        )
        self._release_collateral(capture_id, route)
        self.store.update_funding_capture_position_state(
            capture_id,
            "FAILED",
            now,
            closed_at=now.isoformat(),
            paper_close_fees=long_unwind_fee + short_unwind_fee,
            paper_emergency_unwind_cost=emergency_cost,
            paper_net_pnl_estimated=total_price_pnl - long_fee - short_fee - long_unwind_fee - short_unwind_fee - emergency_cost,
        )
        return {
            "long_unwind_quantity": long_unwind_qty,
            "short_unwind_quantity": short_unwind_qty,
            "long_exit_price": long_exit_price,
            "short_exit_price": short_exit_price,
            "price_pnl": total_price_pnl,
            "long_price_pnl": long_price_pnl,
            "short_price_pnl": short_price_pnl,
            "unwind_fees": long_unwind_fee + short_unwind_fee,
            "emergency_cost": emergency_cost,
            "adverse_price_impact_bps": 100.0,
            "adverse_price_impact_usd": adverse_impact_usd,
        }

    def _mark_open(
        self,
        route: dict[str, Any],
        capture_id: str,
        settlement_at: datetime,
        economics: dict[str, Any],
        execution: dict[str, Any],
        now: datetime,
    ) -> None:
        legs = route.get("legs") or []
        long_leg = dict(leg_by_side(legs, "long") or {})
        short_leg = dict(leg_by_side(legs, "short") or {})
        long_leg["entry_fill_price"] = execution["long_entry_price"]
        short_leg["entry_fill_price"] = execution["short_entry_price"]
        self.store.upsert_funding_capture_position(
            {
                "position_id": capture_id,
                "strategy_name": STRATEGY_NAME,
                "strategy_version": STRATEGY_VERSION,
                "canonical_asset": route.get("canonical_asset", ""),
                "long_venue": long_leg.get("venue") or route.get("long_venue") or "",
                "long_symbol": long_leg.get("symbol") or route.get("long_symbol") or "",
                "short_venue": short_leg.get("venue") or route.get("short_venue") or "",
                "short_symbol": short_leg.get("symbol") or route.get("short_symbol") or "",
                "quantity": float(execution["quantity"]),
                "target_notional": float(route.get("target_notional") or self.config.target_notional_per_leg),
                "state": "OPEN",
                "opened_at": now.isoformat(),
                "original_entry_spread": float(execution["short_entry_price"]) - float(execution["long_entry_price"]),
                "paper_open_fees": float(execution["paper_open_fees"]),
                "paper_net_pnl_estimated": economics.get("initial_expected_net_pnl"),
                "config": {
                    "route_key": route.get("route_key"),
                    "route_entry_key": route_entry_key(route),
                    "entry_legs": [long_leg, short_leg],
                    "entry_economics": economics,
                    "entry_execution": execution,
                },
            }
        )
        self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{capture_id}:1",
                "position_id": capture_id,
                "cycle_number": 1,
                "scheduled_funding_at": settlement_at.isoformat(),
                "long_next_funding_rate_at_decision": _leg_rate(long_leg),
                "short_next_funding_rate_at_decision": _leg_rate(short_leg),
                "conservative_funding_gross": economics.get("conservative_funding_gross"),
                "conservative_funding_edge_bps": economics.get("conservative_funding_edge_bps"),
                "state": "OPEN",
                "decision": "OPEN",
                "decision_reason": "two_entry_legs_filled_before_t20",
            }
        )

    def collect_hold_observation(
        self,
        position: dict[str, Any],
        route: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at")) or now
        observation = build_focused_observation(route, now=now, phase="hold")
        self._store_observation(
            route,
            position_id,
            scheduled,
            observation,
            phase="hold",
            cycle_id=cycle_id,
        )
        return observation

    def _probe_state(self, position: dict[str, Any]) -> dict[str, Any]:
        config = dict(position.get("config") or {})
        return dict(config.get("post_settlement_probes") or {})

    def _missing_required_route_data(self, route: dict[str, Any]) -> list[str]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        missing: list[str] = []
        for side, leg in (("long", long_leg), ("short", short_leg)):
            if optional_float(leg.get("mark_price")) is None:
                missing.append(f"{side}_mark_price_missing")
            if optional_float(leg.get("index_price")) is None:
                missing.append(f"{side}_index_price_missing")
            if optional_float(leg.get("fee_rate")) is None and optional_float(leg.get("taker_fee_rate")) is None:
                missing.append(f"{side}_fee_rate_missing")
            if optional_float(leg.get("normalized_next_funding_rate")) is None:
                missing.append(f"{side}_normalized_next_funding_rate_missing")
        return missing

    def _save_probe_state(
        self,
        position: dict[str, Any],
        probe_state: dict[str, Any],
    ) -> None:
        position_id = str(position["position_id"])
        config = dict(position.get("config") or {})
        config["post_settlement_probes"] = probe_state
        position["config"] = config
        self.store.update_funding_capture_position_config(position_id, config)

    def _record_schedule_probe(
        self,
        position: dict[str, Any],
        route: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        probe = {
            "observed_at": now.astimezone(UTC).isoformat(),
            "long_next_funding_at": long_leg.get("next_funding_at"),
            "short_next_funding_at": short_leg.get("next_funding_at"),
            "fresh": True,
        }
        probe_state = self._probe_state(position)
        probes = list(probe_state.get("probes") or [])
        probes.append(probe)
        if len(probes) > 30:
            probes = probes[-30:]
        probe_state["probes"] = probes
        probe_state["latest_probe_at"] = now.astimezone(UTC).isoformat()
        if not probe_state.get("first_probe_at"):
            probe_state["first_probe_at"] = now.astimezone(UTC).isoformat()
        self._save_probe_state(position, probe_state)
        return probe_state

    def next_cycle_hold_or_close_decision(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at"))
        if scheduled is None:
            return {"decision": "close", "reason": "current_cycle_settlement_missing"}
        seconds_after = (now.astimezone(UTC) - scheduled.astimezone(UTC)).total_seconds()
        position_id = str(position["position_id"])
        state = str(position.get("state") or "")

        if seconds_after < POST_SETTLEMENT_EVALUATION_START_SECONDS:
            return {
                "decision": "wait",
                "reason": "post_settlement_evaluation_not_started",
                "seconds_after_settlement": seconds_after,
            }

        if state == "SETTLEMENT_CROSSED":
            self.store.update_funding_capture_position_state(
                position_id, "POST_SETTLEMENT_EVALUATION", now,
            )
            position = {**position, "state": "POST_SETTLEMENT_EVALUATION"}

        if route is None:
            if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {"decision": "close", "reason": "post_settlement_route_missing"}
            return {
                "decision": "wait",
                "reason": "post_settlement_route_missing_waiting",
                "seconds_after_settlement": seconds_after,
            }

        missing_data = self._missing_required_route_data(route)
        if missing_data:
            if seconds_after < POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                return {
                    "decision": "wait",
                    "reason": "required_route_data_missing_waiting_until_t20",
                    "missing": missing_data,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "close",
                "reason": "required_route_data_missing",
                "missing": missing_data,
            }

        probe_state = self._probe_state(position)
        if seconds_after <= POST_SETTLEMENT_PROBE_END_SECONDS:
            probe_state = self._record_schedule_probe(position, route, now)
        self.collect_hold_observation(position, route, now)

        probes = list(probe_state.get("probes") or [])
        probe_decision = post_settlement_probe_decision(
            probes=probes,
            now=now.astimezone(UTC),
            settlement_at=scheduled.astimezone(UTC),
        )

        if probe_decision.get("decision") == "close_at_t20":
            if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                probe_state["mismatch_reason"] = probe_decision.get("reason", "schedule_mismatch")
                self._save_probe_state(position, probe_state)
                return {
                    "decision": "close",
                    "reason": "next_settlement_schedule_mismatch",
                    "probe_decision": probe_decision,
                    "seconds_after_settlement": seconds_after,
                }
            return {
                "decision": "wait",
                "reason": "awaiting_t20_close_window",
                "probe_decision": probe_decision,
                "seconds_after_settlement": seconds_after,
            }

        schedule: dict[str, Any] | None = None
        if probe_decision.get("decision") == "hold_next_cycle":
            schedule = next_cycle_schedule_decision(
                long_next_funding_at=probe_decision.get("long_next_funding_at"),
                short_next_funding_at=probe_decision.get("short_next_funding_at"),
                now=now,
                min_wait_seconds=float(getattr(self.config, "min_next_settlement_wait_seconds", 300)),
                max_wait_seconds=float(getattr(self.config, "max_next_settlement_wait_seconds", 14_400)),
            )
            if not schedule["hold_schedule"]:
                if seconds_after >= POST_SETTLEMENT_CLOSE_AT_T20_SECONDS:
                    return {
                        "decision": "close",
                        "reason": "next_cycle_schedule_failed",
                        "schedule": schedule,
                        "probe_decision": probe_decision,
                    }
                return {
                    "decision": "wait",
                    "reason": "next_cycle_schedule_failed_waiting_until_t20",
                    "schedule": schedule,
                    "probe_decision": probe_decision,
                }
            probe_decision = {
                **probe_decision,
                "seconds_to_next_cycle": schedule.get("seconds_to_next_cycle"),
            }
            if probe_state.get("aligned_next_settlement_at") != probe_decision.get("next_cycle_at"):
                probe_state["aligned_next_settlement_at"] = probe_decision.get("next_cycle_at")
                probe_state["consecutive_agreements"] = probe_decision.get("consecutive_agreements")
                self._save_probe_state(position, probe_state)

        if seconds_after < POST_SETTLEMENT_HOLD_DECISION_SECONDS:
            return {
                "decision": "wait",
                "reason": "collecting_next_cycle_observations",
                "seconds_after_settlement": seconds_after,
                "probe_decision": probe_decision,
            }

        if probe_decision.get("decision") not in ("hold_next_cycle",):
            return {
                "decision": "close",
                "reason": "next_settlement_schedule_not_confirmed",
                "probe_decision": probe_decision,
                "seconds_after_settlement": seconds_after,
            }

        route_key = str((position.get("config") or {}).get("route_key") or route.get("route_key") or "")
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        long_leg = leg_by_side(route.get("legs") or [], "long") or {}
        short_leg = leg_by_side(route.get("legs") or [], "short") or {}

        observations = self._valid_observations(route_key, now, phase="hold", cycle_id=cycle_id)
        observation_decision = next_cycle_observation_decision(observations=observations, now=now)
        if not observation_decision["eligible"]:
            return {
                "decision": "close",
                "reason": "next_cycle_observations_failed",
                "observation_decision": observation_decision,
            }

        quantity = float(position.get("quantity") or 0.0)
        close_prices = self._current_close_prices(route)
        current_close_fees = self._close_fee_estimate(route, quantity, close_prices)
        reference_notional = min(
            quantity * max(0.0, float(close_prices.get("long_exit_price") or 0.0)),
            quantity * max(0.0, float(close_prices.get("short_exit_price") or 0.0)),
        )
        entry_economics = (position.get("config") or {}).get("entry_economics") or {}
        entry_basis_reserve_bps = float(entry_economics.get("entry_basis_reserve_bps") or 0.0)
        if entry_basis_reserve_bps <= 0:
            entry_basis_reserve_bps = 30.0
        entry_observations = self._valid_observations(
            route_key, now, phase="entry", cycle_id=f"{position_id}:1",
        )
        adverse_basis_changes = [
            abs(float(obs.get("current_exit_spread") or 0.0))
            for obs in entry_observations
            if obs.get("current_exit_spread") is not None
        ]
        p95_mark_return = None
        hold = hold_economics(
            next_conservative_funding_gross=float(observation_decision["conservative_funding_gross"]),
            current_close_fees=current_close_fees,
            reference_notional=reference_notional,
            wait_seconds=float(probe_decision.get("seconds_to_next_cycle") or schedule_seconds_to_next(probe_decision, now) or 0.0),
            entry_basis_reserve_bps=entry_basis_reserve_bps,
            adverse_basis_change_30s_bps=adverse_basis_changes if len(adverse_basis_changes) >= 10 else None,
            p95_abs_mark_return_1s_bps=p95_mark_return,
        )
        current_executable_pnl = float(position.get("paper_net_pnl_estimated") or 0.0)
        projected_total = current_executable_pnl + float(
            hold["incremental_hold_net_pnl"]
        )
        opened_at = parse_time(position.get("opened_at")) or now
        next_cycle_time = parse_time(probe_decision.get("next_cycle_at"))
        projected_age = (
            (next_cycle_time.astimezone(UTC) - opened_at.astimezone(UTC)).total_seconds()
            + POST_SETTLEMENT_NORMAL_EXIT_DELAY_SECONDS
            if next_cycle_time is not None
            else math.inf
        )
        settlements_count = int(position.get("settlements_captured_count") or 0)
        reference = max(0.0, reference_notional)
        reasons: list[str] = []
        if not bool(getattr(self.config, "hold_enabled", True)):
            reasons.append("hold_disabled")
        if float(observation_decision["conservative_funding_gross"]) < max(2.50, reference * 0.005):
            reasons.append("next_conservative_funding_below_minimum")
        if float(hold["incremental_hold_net_pnl"]) < max(1.00, reference * 0.002):
            reasons.append("incremental_hold_net_below_minimum")
        if float(hold["hold_cost_coverage_ratio"]) < 1.50:
            reasons.append("hold_coverage_below_minimum")
        if projected_total < 0:
            reasons.append("projected_total_after_next_cycle_negative")
        if settlements_count >= int(getattr(self.config, "max_settlements_per_position", 4)):
            reasons.append("max_settlements_reached")
        if projected_age > float(getattr(self.config, "max_position_age_seconds", 14_700)):
            reasons.append("max_position_age_exceeded")
        if reasons:
            return {
                "decision": "close",
                "reason": "hold_economics_failed",
                "reasons": reasons,
                "observation_decision": observation_decision,
                "hold_economics": hold,
                "projected_total_after_next_cycle": projected_total,
                "projected_position_age_at_next_exit": projected_age,
            }

        next_cycle_number = int(position.get("current_cycle_number") or 1) + 1
        next_cycle_id = self.store.upsert_funding_capture_cycle(
            {
                "cycle_id": f"{position_id}:{next_cycle_number}",
                "position_id": position_id,
                "cycle_number": next_cycle_number,
                "scheduled_funding_at": str(probe_decision.get("next_cycle_at")),
                "long_next_funding_rate_at_decision": _leg_rate(long_leg),
                "short_next_funding_rate_at_decision": _leg_rate(short_leg),
                "conservative_funding_gross": observation_decision["conservative_funding_gross"],
                "conservative_funding_edge_bps": (
                    float(observation_decision["conservative_funding_gross"]) / reference * 10_000.0
                    if reference > 0
                    else 0.0
                ),
                "hold_basis_reserve_bps": hold["hold_basis_reserve_bps"],
                "hold_legging_reserve_bps": hold["hold_legging_reserve_bps"],
                "hold_time_reserve_bps": hold["hold_time_reserve_bps"],
                "hold_liquidity_reserve_bps": hold["hold_liquidity_reserve_bps"],
                "incremental_hold_cost": hold["incremental_hold_cost"],
                "incremental_hold_net_pnl": hold["incremental_hold_net_pnl"],
                "hold_cost_coverage_ratio": hold["hold_cost_coverage_ratio"],
                "paper_net_if_exit_at_decision": current_executable_pnl,
                "decision": "HOLD",
                "decision_reason": "next_cycle_underwriting_passed",
                "state": "HOLDING_NEXT_CYCLE",
            }
        )
        self.store.update_funding_capture_position_state(
            position_id,
            "HOLDING_NEXT_CYCLE",
            now,
            paper_net_pnl_estimated=projected_total,
        )
        return {
            "decision": "hold",
            "reason": "next_cycle_underwriting_passed",
            "cycle_id": next_cycle_id,
            "cycle_number": next_cycle_number,
            "observation_decision": observation_decision,
            "hold_economics": hold,
            "projected_total_after_next_cycle": projected_total,
        }

    def close_position(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
        *,
        reason: str,
        emergency: bool = False,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        quantity = float(position.get("quantity") or 0.0)
        route_legs = route.get("legs") if route else None
        long_route_leg = leg_by_side(route_legs or [], "long") or {}
        short_route_leg = leg_by_side(route_legs or [], "short") or {}
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        long_entry_leg = leg_by_side(entry_legs, "long") or {}
        short_entry_leg = leg_by_side(entry_legs, "short") or {}
        long_entry_price = float(
            long_entry_leg.get("entry_fill_price") or long_entry_leg.get("vwap") or 0.0
        )
        short_entry_price = float(
            short_entry_leg.get("entry_fill_price") or short_entry_leg.get("vwap") or 0.0
        )
        pricing_quality = "executable_book"
        long_has_book = bool(long_route_leg.get("bids"))
        short_has_book = bool(short_route_leg.get("asks"))
        if quantity <= 0:
            return {"decision": "rejected", "reason": "zero_open_quantity", "state": str(position.get("state") or "")}
        if not emergency:
            if not long_has_book or not short_has_book:
                return {
                    "decision": "rejected",
                    "reason": "executable_orderbook_missing",
                    "state": str(position.get("state") or ""),
                    "missing_long_bids": not long_has_book,
                    "missing_short_asks": not short_has_book,
                }
        else:
            self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)

        def fallback_reference(leg: dict[str, Any], entry_leg: dict[str, Any]) -> tuple[float, str]:
            mark = optional_float(leg.get("mark_price")) or optional_float(entry_leg.get("mark_price"))
            if mark is not None and mark > 0:
                return float(mark), "fallback_mark_300bps"
            entry_price = optional_float(entry_leg.get("entry_fill_price")) or optional_float(entry_leg.get("vwap"))
            if entry_price is not None and entry_price > 0:
                return float(entry_price), "fallback_entry_300bps"
            return 0.0, "emergency_pricing_unavailable"

        long_levels = list(long_route_leg.get("bids") or [])
        short_levels = list(short_route_leg.get("asks") or [])
        if emergency and not long_levels:
            reference, quality = fallback_reference(long_route_leg, long_entry_leg)
            if reference <= 0:
                return {"decision": "rejected", "reason": quality, "state": "EMERGENCY_UNWIND"}
            pricing_quality = quality
            long_levels = _synthetic_levels(reference * (1.0 - 300.0 / 10_000.0), quantity)
        if emergency and not short_levels:
            reference, quality = fallback_reference(short_route_leg, short_entry_leg)
            if reference <= 0:
                return {"decision": "rejected", "reason": quality, "state": "EMERGENCY_UNWIND"}
            pricing_quality = "fallback_entry_300bps" if quality == "fallback_entry_300bps" else pricing_quality
            if pricing_quality == "executable_book":
                pricing_quality = quality
            short_levels = _synthetic_levels(reference * (1.0 + 300.0 / 10_000.0), quantity)

        self.store.update_funding_capture_position_state(position_id, "EXIT_SUBMITTED", now)
        long_exit = simulate_marketable_ioc(long_levels, "sell", quantity, EXECUTION_HAIRCUT_FRACTION)
        short_exit = simulate_marketable_ioc(short_levels, "buy", quantity, EXECUTION_HAIRCUT_FRACTION)
        long_fee_rate = _leg_fee_rate(long_route_leg) or _leg_fee_rate(long_entry_leg)
        short_fee_rate = _leg_fee_rate(short_route_leg) or _leg_fee_rate(short_entry_leg)
        long_fee = long_exit["notional"] * long_fee_rate
        short_fee = short_exit["notional"] * short_fee_rate
        submitted_at = now + timedelta(milliseconds=750)
        filled_at = submitted_at + timedelta(milliseconds=750)
        close_fees_by_side: dict[str, float] = {"long": long_fee, "short": short_fee}
        close_notional_by_side: dict[str, float] = {
            "long": float(long_exit["notional"] or 0.0),
            "short": float(short_exit["notional"] or 0.0),
        }
        closed_quantity_by_side: dict[str, float] = {
            "long": float(long_exit["filled_quantity"] or 0.0),
            "short": float(short_exit["filled_quantity"] or 0.0),
        }
        for side, leg, fill, fee in (
            ("long", long_route_leg or long_entry_leg, long_exit, long_fee),
            ("short", short_route_leg or short_entry_leg, short_exit, short_fee),
        ):
            order_id = f"{position_id}:exit:{cycle_id}:{side}"
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": order_id,
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "EXIT",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": fill["filled_quantity"],
                    "average_fill_price": fill["average_fill_price"],
                    "fee": fee,
                    "state": "FILLED" if fill["unfilled_quantity"] <= 1e-12 else "PARTIALLY_FILLED",
                    "payload": fill,
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(order_id),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-fee,
                    payload={"order_id": order_id, "reason": reason},
                )
            )

        residual_diagnostics: list[dict[str, Any]] = []

        def residual_price(
            *,
            side: str,
            leg: dict[str, Any],
            entry_leg: dict[str, Any],
            levels: list[list[float]],
            residual_qty: float,
        ) -> tuple[float, dict[str, Any]]:
            if side == "long":
                valid_prices = [float(level[0]) for level in levels if len(level) >= 2 and float(level[1]) > 0]
                if valid_prices:
                    reference = min(valid_prices)
                    price = reference * (1.0 - 100.0 / 10_000.0)
                    quality = "residual_book_100bps"
                    penalty = 100.0
                    impact = max(0.0, residual_qty * (reference - price))
                    return price, {
                        "pricing_quality": quality,
                        "adverse_price_impact_bps": penalty,
                        "adverse_price_impact_usd": impact,
                        "reference_price": reference,
                    }
                reference, quality = fallback_reference(leg, entry_leg)
                if reference <= 0:
                    return 0.0, {"pricing_quality": quality}
                price = reference * (1.0 - 300.0 / 10_000.0)
                return price, {
                    "pricing_quality": quality,
                    "adverse_price_impact_bps": 300.0,
                    "adverse_price_impact_usd": max(0.0, residual_qty * (reference - price)),
                    "reference_price": reference,
                }
            valid_prices = [float(level[0]) for level in levels if len(level) >= 2 and float(level[1]) > 0]
            if valid_prices:
                reference = max(valid_prices)
                price = reference * (1.0 + 100.0 / 10_000.0)
                return price, {
                    "pricing_quality": "residual_book_100bps",
                    "adverse_price_impact_bps": 100.0,
                    "adverse_price_impact_usd": max(0.0, residual_qty * (price - reference)),
                    "reference_price": reference,
                }
            reference, quality = fallback_reference(leg, entry_leg)
            if reference <= 0:
                return 0.0, {"pricing_quality": quality}
            price = reference * (1.0 + 300.0 / 10_000.0)
            return price, {
                "pricing_quality": quality,
                "adverse_price_impact_bps": 300.0,
                "adverse_price_impact_usd": max(0.0, residual_qty * (price - reference)),
                "reference_price": reference,
            }

        residual_specs = (
            ("long", long_route_leg or long_entry_leg, long_entry_leg, long_levels, long_exit, long_fee_rate),
            ("short", short_route_leg or short_entry_leg, short_entry_leg, short_levels, short_exit, short_fee_rate),
        )
        if any(float(fill["unfilled_quantity"] or 0.0) > 1e-12 for _, _, _, _, fill, _ in residual_specs):
            self.store.update_funding_capture_position_state(position_id, "PARTIALLY_CLOSED", now)
        for side, leg, entry_leg, levels, fill, fee_rate in residual_specs:
            residual_qty = float(fill["unfilled_quantity"] or 0.0)
            if residual_qty <= 1e-12:
                continue
            price, diagnostic = residual_price(
                side=side,
                leg=leg,
                entry_leg=entry_leg,
                levels=levels,
                residual_qty=residual_qty,
            )
            if price <= 0:
                self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)
                return {
                    "decision": "failed",
                    "reason": "residual_pricing_unavailable",
                    "state": "EMERGENCY_UNWIND",
                    "residual_side": side,
                    "pricing_quality": diagnostic.get("pricing_quality"),
                }
            residual_order_id = f"{position_id}:residual:{cycle_id}:{side}"
            residual_notional = residual_qty * price
            residual_fee = residual_notional * fee_rate
            self.store.upsert_funding_paper_order(
                {
                    "paper_order_id": residual_order_id,
                    "position_id": position_id,
                    "cycle_id": cycle_id,
                    "leg_side": side,
                    "order_intent": "RESIDUAL_UNWIND",
                    "venue": leg.get("venue") or "",
                    "symbol": leg.get("symbol") or "",
                    "decision_at": now.isoformat(),
                    "submitted_at": submitted_at.isoformat(),
                    "acknowledged_at": submitted_at.isoformat(),
                    "filled_at": filled_at.isoformat(),
                    "filled_quantity": residual_qty,
                    "average_fill_price": price,
                    "fee": residual_fee,
                    "state": "FILLED",
                    "payload": {
                        **diagnostic,
                        "reason": reason,
                        "residual_quantity": residual_qty,
                        "cash_cost_policy": "penalty_in_fill_price_once",
                    },
                }
            )
            self._record_ledger_entry(
                make_ledger_entry(
                    order_fee_event_key(residual_order_id),
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=leg.get("venue"),
                    event_type="order_fee",
                    cash_delta=-residual_fee,
                    payload={"order_id": residual_order_id, "reason": reason, "intent": "residual_unwind"},
                )
            )
            close_fees_by_side[side] += residual_fee
            close_notional_by_side[side] += residual_notional
            closed_quantity_by_side[side] += residual_qty
            residual_diagnostics.append({"side": side, **diagnostic, "residual_quantity": residual_qty})

        if (
            abs(closed_quantity_by_side["long"] - quantity) > 1e-12
            or abs(closed_quantity_by_side["short"] - quantity) > 1e-12
        ):
            self.store.update_funding_capture_position_state(position_id, "EMERGENCY_UNWIND", now)
            return {
                "decision": "failed",
                "reason": "residual_exposure_remaining",
                "state": "EMERGENCY_UNWIND",
                "long_closed_quantity": closed_quantity_by_side["long"],
                "short_closed_quantity": closed_quantity_by_side["short"],
                "target_quantity": quantity,
            }

        long_exit_avg = close_notional_by_side["long"] / closed_quantity_by_side["long"]
        short_exit_avg = close_notional_by_side["short"] / closed_quantity_by_side["short"]
        long_price_pnl = quantity * (long_exit_avg - long_entry_price)
        short_price_pnl = quantity * (short_entry_price - short_exit_avg)
        price_pnl = long_price_pnl + short_price_pnl
        confirmed_funding_pnl = self.confirmed_funding_pnl_for_position(position_id)
        open_fees = float(position.get("paper_open_fees") or 0.0)
        close_fees = close_fees_by_side["long"] + close_fees_by_side["short"]
        emergency_cost = float(position.get("paper_emergency_unwind_cost") or 0.0)
        paper_net_if_exit_now = (
            price_pnl
            + confirmed_funding_pnl
            - open_fees
            - close_fees
            - emergency_cost
        )
        pnl = {
            "paper_long_price_pnl": long_price_pnl,
            "paper_short_price_pnl": short_price_pnl,
            "paper_price_pnl": price_pnl,
            "paper_close_fees": close_fees,
            "paper_confirmed_funding_pnl": confirmed_funding_pnl,
            "paper_open_fees": open_fees,
            "paper_emergency_unwind_cost": emergency_cost,
            "paper_net_if_exit_now": paper_net_if_exit_now,
        }
        self._record_price_pnl_entries(
            position_id=position_id,
            cycle_id=cycle_id,
            long_venue=str((long_route_leg or long_entry_leg).get("venue") or ""),
            short_venue=str((short_route_leg or short_entry_leg).get("venue") or ""),
            long_price_pnl=long_price_pnl,
            short_price_pnl=short_price_pnl,
            payload={
                "reason": reason,
                "pricing_quality": pricing_quality,
                "residual_diagnostics": residual_diagnostics,
                "pnl": pnl,
            },
        )
        if emergency or residual_diagnostics:
            self._record_ledger_entry(
                make_ledger_entry(
                    f"emergency_unwind:{position_id}:close:diagnostic",
                    position_id=position_id,
                    cycle_id=cycle_id,
                    venue=str((long_route_leg or long_entry_leg).get("venue") or ""),
                    event_type="emergency_unwind_cost",
                    cash_delta=0.0,
                    payload={
                        "reason": reason,
                        "pricing_quality": pricing_quality,
                        "residual_diagnostics": residual_diagnostics,
                        "cash_cost_policy": "penalty_in_fill_price_once",
                    },
                )
            )
        self._release_collateral(position_id, route or {"legs": entry_legs})
        final_state = "CLOSED_PENDING_RECONCILIATION"
        self.store.update_funding_capture_position_state(
            position_id,
            final_state,
            now,
            closed_at=now.isoformat(),
            paper_close_fees=close_fees,
            paper_emergency_unwind_cost=emergency_cost,
            paper_net_pnl_estimated=pnl["paper_net_if_exit_now"],
        )
        return {
            "decision": "closed",
            "reason": reason,
            "state": final_state,
            "pricing_quality": pricing_quality,
            "long_exit": long_exit,
            "short_exit": short_exit,
            "long_closed_quantity": closed_quantity_by_side["long"],
            "short_closed_quantity": closed_quantity_by_side["short"],
            "long_exit_average_price": long_exit_avg,
            "short_exit_average_price": short_exit_avg,
            "residual_diagnostics": residual_diagnostics,
            "paper_close_fees": close_fees,
            "paper_emergency_unwind_cost": emergency_cost,
            **pnl,
        }

    def _current_close_prices(self, route: dict[str, Any]) -> dict[str, float]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        return {
            "long_exit_price": float(
                optional_float(long_leg.get("close_vwap"))
                or optional_float(long_leg.get("best_bid"))
                or optional_float(long_leg.get("mark_price"))
                or 0.0
            ),
            "short_exit_price": float(
                optional_float(short_leg.get("close_vwap"))
                or optional_float(short_leg.get("best_ask"))
                or optional_float(short_leg.get("mark_price"))
                or 0.0
            ),
        }

    def _close_fee_estimate(
        self,
        route: dict[str, Any],
        quantity: float,
        prices: dict[str, float],
    ) -> float:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        return (
            quantity * float(prices.get("long_exit_price") or 0.0) * _leg_fee_rate(long_leg)
            + quantity * float(prices.get("short_exit_price") or 0.0) * _leg_fee_rate(short_leg)
        )

    def mark_settlement_crossed(self, position: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if str(position.get("state") or "") == "SETTLEMENT_CROSSED":
            return None
        scheduled = parse_time(position.get("current_cycle_scheduled_funding_at"))
        if scheduled is None or now.astimezone(UTC) < scheduled.astimezone(UTC):
            return None
        position_id = str(position["position_id"])
        cycle_id = str(position.get("current_cycle_id") or f"{position_id}:1")
        rows = build_settlement_crossing_rows(
            position_id=position_id,
            cycle_id=cycle_id,
            long_venue=str(position.get("long_venue") or ""),
            long_symbol=str(position.get("long_symbol") or ""),
            short_venue=str(position.get("short_venue") or ""),
            short_symbol=str(position.get("short_symbol") or ""),
            scheduled_funding_at=scheduled.isoformat(),
            quantity=float(position.get("quantity") or 0.0),
        )
        for row in rows:
            self.store.upsert_funding_settlement_reconciliation(row)
        self.store.update_funding_capture_cycle_state(cycle_id, "SETTLEMENT_CROSSED", now)
        self.store.update_funding_capture_position_state(
            position_id,
            "SETTLEMENT_CROSSED",
            now,
            settlements_captured_count=int(position.get("settlements_captured_count") or 0) + 1,
        )
        return {"position_id": position_id, "cycle_id": cycle_id, "state": "SETTLEMENT_CROSSED"}
