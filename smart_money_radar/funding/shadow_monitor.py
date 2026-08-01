from __future__ import annotations

import hashlib
import math
import time
from queue import Empty, Queue
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Thread
from typing import Any

from smart_money_radar.funding.adapter_contracts import (
    PRIMARY_SHADOW_VENUES,
    funding_adapter_contract_from_market,
    mandatory_shadow_inventory,
)
from smart_money_radar.funding.adapters import FundingDataError, FundingVenueClient
from smart_money_radar.funding.adapters.base import (
    apply_endpoint_identity,
    client_endpoint_identity,
)
from smart_money_radar.funding.incentives import UNKNOWN_INCENTIVES
from smart_money_radar.funding.normalization import normalize_catalog_canonical_units
from smart_money_radar.funding.readiness_policy import rate_estimate_from_market
from smart_money_radar.funding.settlement_contracts import (
    settlement_contract_from_market,
)
from smart_money_radar.funding.stablecoins import (
    StablecoinPriceProvider,
    evaluate_stablecoin_route,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    EventWindowPlannerConfig,
    build_settlement_capture_opportunity,
)
from smart_money_radar.funding.venue_capabilities import (
    apply_declared_venue_capability_contract,
)
from smart_money_radar.paper_bot.clock import SystemClock
from smart_money_radar.paper_bot.helpers import parse_iso
from smart_money_radar.storage import SQLiteStore


SHADOW_STATUSES = {
    "DISCOVERED",
    "WATCH",
    "SHADOW_CANDIDATE",
    "RESEARCH_ONLY",
    "EXPIRED",
    "DATA_STALE",
    "CAPABILITY_BLOCKED",
}


@dataclass(frozen=True)
class FundingShadowConfig:
    profile: str = "dex_shadow"
    environment: str = "mainnet"
    target_notional: float = 500.0
    catalog_refresh_seconds: float = 1_800.0
    venue_deadline_seconds: float = 2.0
    periodic_summary_seconds: float = 900.0
    unchanged_opportunity_cooldown_seconds: float = 900.0
    max_strategy_hold_seconds: float = 180.0
    max_gap_between_settlements_seconds: float = 60.0
    entry_safety_buffer_seconds: float = 30.0
    exit_safety_buffer_seconds: float = 5.0
    settlement_confirmation_timeout_seconds: float = 5.0
    max_clock_uncertainty_ms: float = 500.0
    max_response_age_seconds: float = 5.0
    max_source_age_seconds: float = 60.0
    configured_min_net_bps: float = 0.0
    max_individual_alerts_per_run: int = 20
    strict_required_venues: bool = False
    telegram_enabled: bool = True
    max_workers: int = 12
    broad_far_interval_seconds: float = 30.0
    broad_mid_interval_seconds: float = 10.0
    broad_near_interval_seconds: float = 5.0
    broad_mid_threshold_seconds: float = 600.0
    broad_near_threshold_seconds: float = 120.0
    focused_refresh_cadence_seconds: float = 1.0
    alert_claim_ttl_seconds: float = 300.0
    alert_retry_delay_seconds: float = 60.0

    def validated(self) -> "FundingShadowConfig":
        environment = str(self.environment or "").strip().lower()
        if environment not in {"mainnet", "testnet"}:
            environment = "unknown"
        return FundingShadowConfig(
            profile=str(self.profile or "dex_shadow"),
            environment=environment,
            target_notional=max(50.0, float(self.target_notional)),
            catalog_refresh_seconds=max(60.0, float(self.catalog_refresh_seconds)),
            venue_deadline_seconds=max(0.1, min(float(self.venue_deadline_seconds), 10.0)),
            periodic_summary_seconds=max(30.0, float(self.periodic_summary_seconds)),
            unchanged_opportunity_cooldown_seconds=max(
                60.0,
                float(self.unchanged_opportunity_cooldown_seconds),
            ),
            max_strategy_hold_seconds=max(1.0, float(self.max_strategy_hold_seconds)),
            max_gap_between_settlements_seconds=max(
                0.0,
                float(self.max_gap_between_settlements_seconds),
            ),
            entry_safety_buffer_seconds=max(0.0, float(self.entry_safety_buffer_seconds)),
            exit_safety_buffer_seconds=max(0.0, float(self.exit_safety_buffer_seconds)),
            settlement_confirmation_timeout_seconds=max(
                0.0,
                float(self.settlement_confirmation_timeout_seconds),
            ),
            max_clock_uncertainty_ms=max(0.0, float(self.max_clock_uncertainty_ms)),
            max_response_age_seconds=max(0.1, float(self.max_response_age_seconds)),
            max_source_age_seconds=max(0.1, float(self.max_source_age_seconds)),
            configured_min_net_bps=max(0.0, float(self.configured_min_net_bps)),
            max_individual_alerts_per_run=max(0, int(self.max_individual_alerts_per_run)),
            strict_required_venues=bool(self.strict_required_venues),
            telegram_enabled=bool(self.telegram_enabled),
            max_workers=max(1, min(int(self.max_workers), 32)),
            broad_far_interval_seconds=max(1.0, float(self.broad_far_interval_seconds)),
            broad_mid_interval_seconds=max(1.0, float(self.broad_mid_interval_seconds)),
            broad_near_interval_seconds=max(0.5, float(self.broad_near_interval_seconds)),
            broad_mid_threshold_seconds=max(1.0, float(self.broad_mid_threshold_seconds)),
            broad_near_threshold_seconds=max(1.0, float(self.broad_near_threshold_seconds)),
            focused_refresh_cadence_seconds=max(
                0.2,
                float(self.focused_refresh_cadence_seconds),
            ),
            alert_claim_ttl_seconds=max(1.0, float(self.alert_claim_ttl_seconds)),
            alert_retry_delay_seconds=max(1.0, float(self.alert_retry_delay_seconds)),
        )

    def planner_config(self, *, stablecoin_reserve_usd: float = 0.0) -> EventWindowPlannerConfig:
        return EventWindowPlannerConfig(
            max_strategy_hold_seconds=self.max_strategy_hold_seconds,
            max_gap_between_settlements_seconds=self.max_gap_between_settlements_seconds,
            entry_safety_buffer_seconds=self.entry_safety_buffer_seconds,
            exit_safety_buffer_seconds=self.exit_safety_buffer_seconds,
            settlement_confirmation_timeout_seconds=self.settlement_confirmation_timeout_seconds,
            max_clock_uncertainty_ms=self.max_clock_uncertainty_ms,
            configured_min_net_bps=self.configured_min_net_bps,
            stablecoin_reserve_usd=stablecoin_reserve_usd,
        )


class UnavailableFundingVenueClient:
    def __init__(self, venue: str, *, environment: str, reason: str) -> None:
        self.venue = str(venue).lower()
        self.environment = str(environment or "unknown").lower()
        self.reason = str(reason)

    def catalog_metadata(self, _observed_at: str) -> list[dict[str, Any]]:
        return []

    def funding_sweep(
        self,
        _observed_at: str,
        _catalog: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raise FundingDataError(self.reason)


class ShadowAlertDeduper:
    def __init__(self, cooldown_seconds: float = 900.0) -> None:
        self.cooldown_seconds = max(60.0, float(cooldown_seconds))
        self.last_sent_monotonic_by_key: dict[str, float] = {}

    def alert_key(self, opportunity: dict[str, Any]) -> str:
        economics = opportunity.get("stablecoin_risk") or {}
        try:
            bucket_value = float(
                economics.get("funding_net_after_stablecoin_reserve")
                or opportunity.get("preliminary_gross_funding")
                or 0.0
            )
        except (TypeError, ValueError):
            bucket_value = 0.0
        bucket = round(bucket_value, 1)
        blockers = ",".join(sorted(opportunity.get("blockers") or []))
        raw = "|".join(
            [
                str(opportunity.get("opportunity_key") or ""),
                str(opportunity.get("status") or ""),
                str(bucket),
                blockers,
                str(opportunity.get("settlement_at") or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def should_send(
        self,
        opportunity: dict[str, Any],
        *,
        now_monotonic: float,
    ) -> tuple[bool, str]:
        key = self.alert_key(opportunity)
        last = self.last_sent_monotonic_by_key.get(key)
        if last is not None and now_monotonic - last < self.cooldown_seconds:
            return False, key
        self.last_sent_monotonic_by_key[key] = now_monotonic
        return True, key


class FundingShadowMonitor:
    def __init__(
        self,
        store: SQLiteStore,
        clients: list[FundingVenueClient],
        *,
        config: FundingShadowConfig | None = None,
        stablecoin_price_provider: StablecoinPriceProvider | None = None,
        notifier: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.clients = list(clients)
        self.config = (config or FundingShadowConfig()).validated()
        self.stablecoin_price_provider = stablecoin_price_provider
        self.notifier = notifier
        self.clock = clock or SystemClock()
        self.catalog_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self.alert_deduper = ShadowAlertDeduper(
            self.config.unchanged_opportunity_cooldown_seconds
        )
        self.last_summary_monotonic = 0.0
        self._inflight_lock = Lock()
        self._inflight_keys: set[tuple[str, str, str, str]] = set()
        self.last_request_counts_by_venue: dict[str, int] = {}
        self.last_request_counts_by_endpoint_class: dict[str, int] = {}
        self.overlapping_call_prevention_count = 0
        self.last_focused_refresh_count = 0
        self._last_opportunities: list[dict[str, Any]] = []
        self._last_summary: dict[str, Any] = {}
        self._stablecoin_route_snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self.cumulative_broad_sweep_count = 0
        self.cumulative_focused_refresh_count = 0
        self.cumulative_request_counts_by_venue: dict[str, int] = {}
        self.cumulative_request_counts_by_endpoint_class: dict[str, int] = {}
        self.cumulative_skipped_overlap_count = 0
        self.cumulative_deadline_count = 0
        self.cumulative_stale_result_rejected_count = 0
        self.cumulative_route_refresh_count = 0
        self.cumulative_event_observation_count = 0
        self.cumulative_boundary_confirmation_count = 0

    def run(self, *, duration_seconds: float = 180.0) -> dict[str, Any]:
        self.store.init_db()
        started = self.clock.monotonic()
        iterations = 0
        next_broad_due = started
        next_focused_due = started + float(self.config.focused_refresh_cadence_seconds)
        self._notify(
            "<b>SHADOW FUNDING</b>\n\nSHADOW STARTED\nNO POSITION OPENED"
        )
        last_result: dict[str, Any] = {}
        try:
            while self.clock.monotonic() - started < max(1.0, float(duration_seconds)):
                now_monotonic = self.clock.monotonic()
                if now_monotonic >= next_broad_due:
                    result = self.run_once()
                    last_result = result
                    iterations += 1
                    interval = self._adaptive_broad_sweep_interval_seconds(
                        result.get("nearest_settlement_seconds")
                    )
                    next_broad_due = now_monotonic + interval
                    next_focused_due = min(
                        next_focused_due,
                        now_monotonic + float(self.config.focused_refresh_cadence_seconds),
                    )
                    continue
                if self._last_opportunities and now_monotonic >= next_focused_due:
                    result = self.run_focused_once()
                    if result:
                        last_result = {**last_result, **result}
                    iterations += 1
                    next_focused_due = (
                        now_monotonic + float(self.config.focused_refresh_cadence_seconds)
                    )
                    continue
                remaining = max(
                    0.0,
                    float(duration_seconds) - (self.clock.monotonic() - started),
                )
                if remaining <= 0:
                    break
                due_times = [next_broad_due]
                if self._last_opportunities:
                    due_times.append(next_focused_due)
                sleep_for = max(0.0, min(due_times) - self.clock.monotonic())
                self.clock.sleep(min(sleep_for, remaining))
        except Exception as exc:
            self._notify(
                "<b>SHADOW FUNDING</b>\n\n"
                "monitor crashed\n"
                f"Error: {type(exc).__name__}: {exc}\n"
                "NO POSITION OPENED"
            )
            raise
        finally:
            self._notify(
                "<b>SHADOW FUNDING</b>\n\nSHADOW STOPPED\nNO POSITION OPENED"
            )
        return {
            "iterations": iterations,
            "duration_seconds": duration_seconds,
            **last_result,
        }

    def run_focused_once(self) -> dict[str, Any]:
        observed = self.clock.now().astimezone(UTC)
        observed_at = observed.isoformat()
        focused_markets = self.focused_route_refresh(self._last_opportunities, observed_at)
        if not focused_markets:
            return self._scheduler_metrics()
        opportunities, summary = self.build_shadow_opportunities(
            focused_markets,
            observed,
            allow_stablecoin_provider=False,
        )
        self._last_opportunities = opportunities
        self._last_summary = {**self._last_summary, **summary}
        self.cumulative_route_refresh_count += len(opportunities)
        for opportunity in opportunities:
            self.store.upsert_funding_shadow_opportunity(opportunity)
            self._persist_settlement_events_and_observations(opportunity)
        return {
            "opportunities": len(opportunities),
            **summary,
            **self._scheduler_metrics(),
        }

    def run_once(self) -> dict[str, Any]:
        self.store.init_db()
        before_safety = self.store.funding_shadow_paper_safety_snapshot()
        observed = self.clock.now().astimezone(UTC)
        observed_at = observed.isoformat()
        markets, venue_health, warnings = self.broad_funding_sweep(observed_at)
        self.cumulative_broad_sweep_count += 1
        for health in venue_health:
            self.store.upsert_funding_shadow_venue_health(health)
        opportunities, summary = self.build_shadow_opportunities(
            markets,
            observed,
            allow_stablecoin_provider=True,
        )
        focused_markets = self.focused_route_refresh(opportunities, observed_at)
        if focused_markets:
            focused_opportunities, focused_summary = self.build_shadow_opportunities(
                focused_markets,
                observed,
                allow_stablecoin_provider=False,
            )
            if focused_opportunities:
                opportunities = focused_opportunities
                summary = {**summary, **focused_summary}
        self._last_opportunities = opportunities
        self._last_summary = summary
        self.cumulative_route_refresh_count += len(opportunities)
        alert_attempted = 0
        alert_sent = 0
        alert_failed = 0
        alert_disabled = 0
        for opportunity in opportunities:
            self.store.upsert_funding_shadow_opportunity(opportunity)
            self._persist_settlement_events_and_observations(opportunity)
            if (
                opportunity.get("status") == "SHADOW_CANDIDATE"
                and alert_attempted < self.config.max_individual_alerts_per_run
            ):
                should_send, alert_key = self.alert_deduper.should_send(
                    opportunity,
                    now_monotonic=self.clock.monotonic(),
                )
                if should_send:
                    message = shadow_opportunity_message(opportunity)
                    claim_id = self.store.claim_funding_shadow_alert(
                        {
                            "alert_key": alert_key,
                            "opportunity_key": opportunity["opportunity_key"],
                            "environment": opportunity["environment"],
                            "status": opportunity["status"],
                            "message": message,
                            "telegram_status": "queued",
                            "telegram_error": None,
                            "payload": {
                                "opportunity": opportunity,
                                "material_bucket": alert_key,
                            },
                        },
                        claim_ttl_seconds=self.config.alert_claim_ttl_seconds,
                        retry_delay_seconds=self.config.alert_retry_delay_seconds,
                    )
                    if claim_id is None:
                        continue
                    alert_attempted += 1
                    result = self._notify(message)
                    self.store.update_funding_shadow_alert_status(
                        alert_key,
                        result.get("status") or "unknown",
                        result.get("error"),
                        retry_delay_seconds=self.config.alert_retry_delay_seconds,
                    )
                    if result.get("status") == "sent":
                        alert_sent += 1
                    elif result.get("status") == "disabled":
                        alert_disabled += 1
                    else:
                        alert_failed += 1
        if self._summary_due():
            self._notify(shadow_summary_message(summary, opportunities, warnings))
        self.store.prune_funding_shadow_runtime_rows()
        after_safety = self.store.funding_shadow_paper_safety_snapshot()
        safety_deltas = funding_shadow_safety_deltas(before_safety, after_safety)
        safety_violation = any(value != 0 for value in safety_deltas.values())
        return {
            "status": "safety_violation" if safety_violation else "success",
            "warnings": warnings,
            "opportunities": len(opportunities),
            "telegram_individual_attempted": alert_attempted,
            "telegram_individual_sent": alert_sent,
            "telegram_individual_failed": alert_failed,
            "telegram_individual_disabled": alert_disabled,
            "paper_safety_deltas": safety_deltas,
            **self._scheduler_metrics(),
            **summary,
        }

    def _persist_settlement_events_and_observations(
        self,
        opportunity: dict[str, Any],
    ) -> None:
        for event in opportunity.get("included_settlement_events") or []:
            self.store.upsert_funding_shadow_settlement_event(
                opportunity["opportunity_key"],
                event,
                classification="included",
            )
            self.cumulative_event_observation_count += 1
        for event in opportunity.get("excluded_settlement_events") or []:
            self.store.upsert_funding_shadow_settlement_event(
                opportunity["opportunity_key"],
                event,
                classification="excluded",
            )
            self.cumulative_event_observation_count += 1
        for event in opportunity.get("ambiguous_settlement_events") or []:
            self.store.upsert_funding_shadow_settlement_event(
                opportunity["opportunity_key"],
                event,
                classification="ambiguous",
            )
            self.cumulative_event_observation_count += 1
        for observation in shadow_observations_for_opportunity(opportunity):
            self.store.insert_funding_shadow_observation(observation)

    def _adaptive_broad_sweep_interval_seconds(
        self,
        nearest_settlement_seconds: Any,
    ) -> float:
        return adaptive_broad_sweep_interval_seconds(
            nearest_settlement_seconds,
            near_threshold_seconds=self.config.broad_near_threshold_seconds,
            mid_threshold_seconds=self.config.broad_mid_threshold_seconds,
            near_interval_seconds=self.config.broad_near_interval_seconds,
            mid_interval_seconds=self.config.broad_mid_interval_seconds,
            far_interval_seconds=self.config.broad_far_interval_seconds,
        )

    def _scheduler_metrics(self) -> dict[str, Any]:
        return {
            "broad_sweep_count": self.cumulative_broad_sweep_count,
            "focused_refresh_count": self.cumulative_focused_refresh_count,
            "last_focused_refresh_count": self.last_focused_refresh_count,
            "request_counts_by_venue": dict(
                sorted(self.cumulative_request_counts_by_venue.items())
            ),
            "request_counts_by_endpoint_class": dict(
                sorted(self.cumulative_request_counts_by_endpoint_class.items())
            ),
            "last_request_counts_by_venue": dict(
                sorted(self.last_request_counts_by_venue.items())
            ),
            "last_request_counts_by_endpoint_class": dict(
                sorted(self.last_request_counts_by_endpoint_class.items())
            ),
            "skipped_overlap_count": self.cumulative_skipped_overlap_count,
            "deadline_count": self.cumulative_deadline_count,
            "stale_result_rejected_count": self.cumulative_stale_result_rejected_count,
            "route_refresh_count": self.cumulative_route_refresh_count,
            "event_observation_count": self.cumulative_event_observation_count,
            "boundary_confirmation_count": self.cumulative_boundary_confirmation_count,
            "overlapping_call_prevention_count": self.overlapping_call_prevention_count,
        }

    def broad_funding_sweep(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        clients = {
            str(getattr(client, "venue", "")).lower(): client
            for client in self.clients
            if getattr(client, "venue", None)
        }
        if not clients:
            return [], [], ["shadow sweep skipped: no venues configured"]
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        health_rows: list[dict[str, Any]] = []
        started_monotonic = self.clock.monotonic()
        self.last_request_counts_by_venue = {}
        self.last_request_counts_by_endpoint_class = {}
        result_queue: Queue[dict[str, Any]] = Queue()
        threads: dict[str, tuple[Thread, float, str, str]] = {}
        for venue, client in clients.items():
            if len(threads) >= self.config.max_workers:
                warnings.append("shadow sweep worker limit reached")
                break
            endpoint_class = "funding_sweep"
            request_key = self._request_key(client, endpoint_class, "*")
            if not self._begin_inflight_request(request_key):
                warnings.append(f"{venue} shadow sweep skipped: request already in-flight")
                continue
            request_started = time.monotonic()
            request_started_at = self.clock.now().astimezone(UTC).isoformat()
            self._record_request_count(venue, endpoint_class)
            thread = Thread(
                target=self._fetch_public_market_snapshot_worker,
                args=(
                    result_queue,
                    venue,
                    client,
                    observed_at,
                    request_started,
                    request_started_at,
                    endpoint_class,
                    request_key,
                ),
                name=f"funding-shadow-sweep-{venue}",
                daemon=True,
            )
            threads[venue] = (thread, request_started, request_started_at, endpoint_class)
            thread.start()

        deadline = time.monotonic() + float(self.config.venue_deadline_seconds)
        for thread, _request_started, _request_started_at, _endpoint_class in list(threads.values()):
            thread.join(max(0.0, deadline - time.monotonic()))

        completed: set[str] = set()
        while True:
            try:
                item = result_queue.get_nowait()
            except Empty:
                break
            venue = str(item["venue"])
            completed.add(venue)
            request_started = float(item["request_started"])
            latency_ms = (
                float(item.get("response_received_monotonic") or time.monotonic())
                - request_started
            ) * 1_000.0
            if item.get("error"):
                warnings.append(f"{venue} shadow sweep failed: {item['error']}")
                health_rows.append(
                    {
                        "venue": venue,
                        "environment": client_environment(
                            clients[venue],
                            self.config.environment,
                        ),
                        "status": mandatory_health_status(venue, "degraded"),
                        "latency_ms": latency_ms,
                        **worker_latency_fields(item, latency_ms),
                        "last_error": str(item["error"]),
                        "observed_at": observed_at,
                    }
                )
                continue
            markets.extend(list(item.get("markets") or []))
            warnings.extend(list(item.get("warnings") or []))
            health_rows.append(
                {
                    "venue": venue,
                    "environment": client_environment(
                        clients[venue],
                        self.config.environment,
                    ),
                    "status": "healthy",
                    "latency_ms": latency_ms,
                    **worker_latency_fields(item, latency_ms),
                    "last_error": None,
                    "observed_at": observed_at,
                }
            )
        for venue, (thread, request_started, request_started_at, endpoint_class) in (
            threads.items()
        ):
            if venue in completed:
                continue
            if thread.is_alive():
                latency_ms = (time.monotonic() - request_started) * 1_000.0
                self.cumulative_deadline_count += 1
                warnings.append(f"{venue} shadow sweep timed out")
                health_rows.append(
                    {
                        "venue": venue,
                        "environment": client_environment(
                            clients[venue],
                            self.config.environment,
                        ),
                        "status": mandatory_health_status(venue, "degraded"),
                        "latency_ms": latency_ms,
                        "request_started_at": request_started_at,
                        "response_received_at": None,
                        "parsing_completed_at": None,
                        "network_latency_ms": latency_ms,
                        "parsing_latency_ms": None,
                        "total_latency_ms": latency_ms,
                        "endpoint_class": endpoint_class,
                        "last_error": "venue_deadline_exceeded",
                        "observed_at": observed_at,
                    }
                )
        elapsed = self.clock.monotonic() - started_monotonic
        if elapsed > float(self.config.venue_deadline_seconds) + 0.25:
            warnings.append("shadow sweep exceeded venue deadline budget")
        return markets, health_rows, warnings

    def _request_key(
        self,
        client: FundingVenueClient,
        endpoint_class: str,
        symbol_scope: str,
    ) -> tuple[str, str, str, str]:
        venue = str(getattr(client, "venue", "") or "").lower()
        environment = client_environment(client, self.config.environment)
        return (
            venue,
            environment,
            str(endpoint_class or "unknown"),
            str(symbol_scope or "*"),
        )

    def _begin_inflight_request(
        self,
        key: tuple[str, str, str, str],
    ) -> bool:
        with self._inflight_lock:
            if key in self._inflight_keys:
                self.overlapping_call_prevention_count += 1
                self.cumulative_skipped_overlap_count += 1
                return False
            self._inflight_keys.add(key)
            return True

    def _clear_inflight_request(
        self,
        key: tuple[str, str, str, str],
    ) -> None:
        with self._inflight_lock:
            self._inflight_keys.discard(key)

    def _record_request_count(self, venue: str, endpoint_class: str) -> None:
        venue_key = str(venue or "unknown").lower()
        endpoint_key = str(endpoint_class or "unknown")
        self.last_request_counts_by_venue[venue_key] = (
            self.last_request_counts_by_venue.get(venue_key, 0) + 1
        )
        self.last_request_counts_by_endpoint_class[endpoint_key] = (
            self.last_request_counts_by_endpoint_class.get(endpoint_key, 0) + 1
        )
        self.cumulative_request_counts_by_venue[venue_key] = (
            self.cumulative_request_counts_by_venue.get(venue_key, 0) + 1
        )
        self.cumulative_request_counts_by_endpoint_class[endpoint_key] = (
            self.cumulative_request_counts_by_endpoint_class.get(endpoint_key, 0) + 1
        )

    def _fetch_public_market_snapshot_worker(
        self,
        result_queue: Queue[dict[str, Any]],
        venue: str,
        client: FundingVenueClient,
        observed_at: str,
        request_started: float,
        request_started_at: str,
        endpoint_class: str,
        request_key: tuple[str, str, str, str],
    ) -> None:
        try:
            markets, warnings = self._fetch_public_market_snapshot(
                venue,
                client,
                observed_at,
            )
            response_received_monotonic = time.monotonic()
            response_received_at = self.clock.now().astimezone(UTC).isoformat()
            result_queue.put(
                {
                    "venue": venue,
                    "request_started": request_started,
                    "request_started_at": request_started_at,
                    "response_received_monotonic": response_received_monotonic,
                    "response_received_at": response_received_at,
                    "parsing_completed_at": response_received_at,
                    "endpoint_class": endpoint_class,
                    "markets": markets,
                    "warnings": warnings,
                    "error": None,
                }
            )
        except Exception as exc:
            response_received_monotonic = time.monotonic()
            response_received_at = self.clock.now().astimezone(UTC).isoformat()
            result_queue.put(
                {
                    "venue": venue,
                    "request_started": request_started,
                    "request_started_at": request_started_at,
                    "response_received_monotonic": response_received_monotonic,
                    "response_received_at": response_received_at,
                    "parsing_completed_at": response_received_at,
                    "endpoint_class": endpoint_class,
                    "markets": [],
                    "warnings": [],
                    "error": str(exc),
                }
            )
        finally:
            self._clear_inflight_request(request_key)

    def _fetch_public_market_snapshot(
        self,
        venue: str,
        client: FundingVenueClient,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        cached_instruments = self._catalog_instruments(venue, client, observed_at)
        funding_sweep = getattr(client, "funding_sweep", None)
        if callable(funding_sweep):
            raw_markets, warnings = funding_sweep(observed_at, cached_instruments)
            markets = list(raw_markets)
        else:
            instruments, markets, warnings = client.catalog_and_markets(observed_at)
            cached_instruments, markets = normalize_catalog_canonical_units(
                instruments,
                markets,
            )
            self.catalog_cache[venue] = (
                cached_instruments,
                self.clock.monotonic(),
            )
        response_received_at = self.clock.now().astimezone(UTC).isoformat()
        instrument_by_key = {
            (str(row.get("venue") or venue).lower(), str(row.get("symbol") or "")): row
            for row in cached_instruments
        }
        enriched: list[dict[str, Any]] = []
        for market in markets:
            row = dict(market)
            key = (str(row.get("venue") or venue).lower(), str(row.get("symbol") or ""))
            instrument = instrument_by_key.get(key, {})
            row = {**instrument, **row}
            row["venue"] = str(row.get("venue") or venue).lower()
            if row.get("response_received_at") in (None, ""):
                row["response_received_at"] = response_received_at
            if row.get("observed_at") in (None, ""):
                row["observed_at"] = observed_at
            enriched.append(row)
        if hasattr(client, "endpoint_identity"):
            identity = client_endpoint_identity(client)
            enriched = apply_endpoint_identity(enriched, identity)
        enriched = [apply_declared_venue_capability_contract(row) for row in enriched]
        return enriched, list(warnings or [])

    def _catalog_instruments(
        self,
        venue: str,
        client: FundingVenueClient,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        cached = self.catalog_cache.get(venue)
        now_monotonic = self.clock.monotonic()
        if cached and now_monotonic - cached[1] < self.config.catalog_refresh_seconds:
            return cached[0]
        catalog_method = getattr(client, "catalog_metadata", None)
        if callable(catalog_method):
            instruments = list(catalog_method(observed_at))
            self.catalog_cache[venue] = (instruments, now_monotonic)
            return instruments
        return cached[0] if cached else []

    def focused_route_refresh(
        self,
        opportunities: list[dict[str, Any]],
        observed_at: str,
    ) -> list[dict[str, Any]]:
        clients = {
            str(getattr(client, "venue", "")).lower(): client
            for client in self.clients
            if getattr(client, "venue", None)
        }
        wanted: dict[str, set[str]] = {}
        for opportunity in opportunities:
            try:
                seconds = float(opportunity.get("seconds_until_settlement"))
            except (TypeError, ValueError):
                continue
            if seconds > 60.0:
                continue
            if opportunity.get("status") not in {
                "WATCH",
                "SHADOW_CANDIDATE",
                "RESEARCH_ONLY",
            }:
                continue
            for side in ("long", "short"):
                venue = str(opportunity.get(f"{side}_venue") or "").lower()
                symbol = str(opportunity.get(f"{side}_symbol") or "")
                if venue and symbol:
                    wanted.setdefault(venue, set()).add(symbol)
        self.last_focused_refresh_count = 0
        refreshed: list[dict[str, Any]] = []
        for venue, symbols in sorted(wanted.items()):
            client = clients.get(venue)
            if client is None:
                continue
            endpoint_class = "funding_sweep"
            request_key = self._request_key(client, endpoint_class, "*")
            if not self._begin_inflight_request(request_key):
                continue
            try:
                markets, _warnings = self._fetch_public_market_snapshot(
                    venue,
                    client,
                    observed_at,
                )
                self._record_request_count(venue, endpoint_class)
                self.last_focused_refresh_count += 1
                self.cumulative_focused_refresh_count += 1
                refreshed.extend(
                    market
                    for market in markets
                    if str(market.get("symbol") or "") in symbols
                )
            except Exception:
                continue
            finally:
                self._clear_inflight_request(request_key)
        return refreshed

    def build_shadow_opportunities(
        self,
        markets: list[dict[str, Any]],
        now: datetime,
        *,
        allow_stablecoin_provider: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rejection_reasons: dict[str, int] = {}

        def reject(reason: str) -> None:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        eligible_by_asset: dict[str, list[dict[str, Any]]] = {}
        inventory = mandatory_shadow_inventory(
            markets,
            registered_venues=[
                str(getattr(client, "venue", "")).lower()
                for client in self.clients
                if getattr(client, "venue", None)
            ],
        )
        for market in markets:
            env = str(market.get("environment") or "").lower()
            if env not in {"mainnet", "testnet"}:
                reject("environment_unverified")
                continue
            if env != self.config.environment:
                reject("environment_mismatch")
                continue
            asset = str(market.get("canonical_asset") or "").upper()
            if not asset:
                reject("canonical_asset_missing")
                continue
            eligible_by_asset.setdefault(asset, []).append(market)

        opportunities_by_key: dict[str, dict[str, Any]] = {}
        structurally_matched = 0
        nearest_settlement_seconds: float | None = None
        for asset, rows in eligible_by_asset.items():
            for long_market in rows:
                for short_market in rows:
                    if long_market is short_market:
                        continue
                    if str(long_market.get("venue")) == str(short_market.get("venue")):
                        continue
                    if str(long_market.get("venue")).lower() == "paradex" or str(
                        short_market.get("venue")
                    ).lower() == "paradex":
                        reject("funding_continuous_pro_rata")
                        continue
                    structurally_matched += 1
                    opportunity = self._shadow_opportunity_for_pair(
                        asset,
                        long_market,
                        short_market,
                        now,
                        allow_stablecoin_provider=allow_stablecoin_provider,
                    )
                    for reason in opportunity.get("blockers") or []:
                        reject(str(reason))
                    settlement_at = parse_iso(opportunity.get("settlement_at"))
                    if settlement_at is not None:
                        lead = max(0.0, (settlement_at - now).total_seconds())
                        nearest_settlement_seconds = (
                            lead
                            if nearest_settlement_seconds is None
                            else min(nearest_settlement_seconds, lead)
                        )
                    if opportunity["status"] in {
                        "SHADOW_CANDIDATE",
                        "WATCH",
                        "RESEARCH_ONLY",
                        "CAPABILITY_BLOCKED",
                        "DATA_STALE",
                        "EXPIRED",
                    }:
                        key = opportunity["opportunity_key"]
                        existing = opportunities_by_key.get(key)
                        if existing is None or opportunity_sort_value(opportunity) > opportunity_sort_value(existing):
                            opportunities_by_key[key] = opportunity

        opportunities = sorted(
            opportunities_by_key.values(),
            key=opportunity_sort_value,
            reverse=True,
        )
        return opportunities, {
            "venues_checked": len({str(row.get("venue")) for row in markets}),
            "markets_checked": len(markets),
            "inventory": inventory,
            "primary_inventory_venue_count": len(PRIMARY_SHADOW_VENUES),
            "routes_structurally_matched": structurally_matched,
            "aligned_routes": sum(
                1
                for row in opportunities
                if row.get("opportunity_shape") == "MULTIPLE_SETTLEMENTS"
            ),
            "shadow_candidates": sum(
                1 for row in opportunities if row["status"] == "SHADOW_CANDIDATE"
            ),
            "research_only_routes": sum(
                1 for row in opportunities if row["status"] == "RESEARCH_ONLY"
            ),
            "one_settlement_routes": sum(
                1 for row in opportunities if row.get("opportunity_shape") == "ONE_SETTLEMENT"
            ),
            "multiple_settlements_routes": sum(
                1
                for row in opportunities
                if row.get("opportunity_shape") == "MULTIPLE_SETTLEMENTS"
            ),
            "included_settlement_events": sum(
                len(row.get("included_settlement_events") or []) for row in opportunities
            ),
            "excluded_settlement_events": sum(
                len(row.get("excluded_settlement_events") or []) for row in opportunities
            ),
            "ambiguous_settlement_events": sum(
                len(row.get("ambiguous_settlement_events") or []) for row in opportunities
            ),
            "mandatory_unavailable": sum(
                1
                for row in inventory
                if row.get("venue") == "risex" and row.get("status") == "UNAVAILABLE"
            ),
            "nearest_settlement_seconds": nearest_settlement_seconds,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        }

    def _shadow_opportunity_for_pair(
        self,
        asset: str,
        long_market: dict[str, Any],
        short_market: dict[str, Any],
        now: datetime,
        *,
        allow_stablecoin_provider: bool = True,
    ) -> dict[str, Any]:
        long_market = apply_declared_venue_capability_contract(long_market)
        short_market = apply_declared_venue_capability_contract(short_market)
        observed_at = now.astimezone(UTC).isoformat()
        long_venue = str(long_market.get("venue") or "").lower()
        short_venue = str(short_market.get("venue") or "").lower()
        long_contract = funding_adapter_contract_from_market(long_market)
        short_contract = funding_adapter_contract_from_market(short_market)
        long_settlement_contract = settlement_contract_from_market(long_market)
        short_settlement_contract = settlement_contract_from_market(short_market)
        blockers: list[str] = []
        if long_contract.environment != short_contract.environment:
            blockers.append("environment_mismatch")
        long_mark = optional_float(long_market.get("mark_price"))
        short_mark = optional_float(short_market.get("mark_price"))
        if long_mark is None or short_mark is None:
            blockers.append("mark_price_missing")
        if optional_float(long_market.get("index_price")) is None or optional_float(
            short_market.get("index_price")
        ) is None:
            blockers.append("index_price_missing")
        long_rate_estimate = rate_estimate_from_market(long_market)
        short_rate_estimate = rate_estimate_from_market(short_market)
        long_rate = optional_float(long_rate_estimate.get("rate_estimate_per_settlement"))
        short_rate = optional_float(short_rate_estimate.get("rate_estimate_per_settlement"))
        if long_rate is None or short_rate is None:
            blockers.append("usable_funding_rate_estimate_missing")
        if not long_market.get("response_received_at") or not short_market.get(
            "response_received_at"
        ):
            blockers.append("response_timestamp_missing")
        if long_contract.status not in {"PAPER_ELIGIBLE", "SHADOW_ELIGIBLE"}:
            blockers.extend(f"long_{reason}" for reason in long_contract.reasons)
        if short_contract.status not in {"PAPER_ELIGIBLE", "SHADOW_ELIGIBLE"}:
            blockers.extend(f"short_{reason}" for reason in short_contract.reasons)

        long_collateral = str(
            long_contract.settlement_collateral
            or long_market.get("collateral_asset")
            or ""
        )
        short_collateral = str(
            short_contract.settlement_collateral
            or short_market.get("collateral_asset")
            or ""
        )
        stablecoin_key = (long_collateral.upper(), short_collateral.upper())
        if allow_stablecoin_provider:
            preliminary_stablecoin = evaluate_stablecoin_route(
                long_collateral=long_collateral,
                short_collateral=short_collateral,
                provider=self.stablecoin_price_provider,
                observed_at=observed_at,
                reference_notional=float(self.config.target_notional),
                funding_net_before_stablecoin_reserve=0.0,
            )
            self._stablecoin_route_snapshots[stablecoin_key] = dict(preliminary_stablecoin)
        else:
            cached_stablecoin = self._stablecoin_route_snapshots.get(stablecoin_key)
            if cached_stablecoin is not None:
                preliminary_stablecoin = dict(cached_stablecoin)
            else:
                preliminary_stablecoin = evaluate_stablecoin_route(
                    long_collateral=long_collateral,
                    short_collateral=short_collateral,
                    provider=None,
                    observed_at=observed_at,
                    reference_notional=float(self.config.target_notional),
                    funding_net_before_stablecoin_reserve=0.0,
                )
        long_market = {
            **long_market,
            "stablecoin_route_evaluation": preliminary_stablecoin,
            "stablecoin_risk": preliminary_stablecoin,
        }
        short_market = {
            **short_market,
            "stablecoin_route_evaluation": preliminary_stablecoin,
            "stablecoin_risk": preliminary_stablecoin,
        }
        stablecoin_reserve_usd = float(
            preliminary_stablecoin.get("stablecoin_reserve_usd") or 0.0
        )
        planner = build_settlement_capture_opportunity(
            long_market=long_market,
            short_market=short_market,
            now=now,
            target_notional=float(self.config.target_notional),
            planner_config=self.config.planner_config(
                stablecoin_reserve_usd=stablecoin_reserve_usd
            ),
            max_response_age_seconds=self.config.max_response_age_seconds,
            max_source_age_seconds=self.config.max_source_age_seconds,
        )
        preliminary_gross = float(planner.get("expected_funding_cashflow_usd") or 0.0)
        conservative_gross = float(
            planner.get("conservative_funding_cashflow_usd") or 0.0
        )
        funding_net_before_stablecoin = (
            float(planner.get("conservative_net_usd") or 0.0)
            + stablecoin_reserve_usd
        )
        stablecoin = {
            **preliminary_stablecoin,
            "funding_net_before_stablecoin_reserve": funding_net_before_stablecoin,
            "funding_net_after_stablecoin_reserve": float(planner.get("conservative_net_usd") or 0.0),
        }
        if stablecoin.get("status") == "RESEARCH_ONLY":
            blockers.extend(str(reason) for reason in stablecoin.get("blockers") or [])
        blockers.extend(str(reason) for reason in planner.get("blockers") or [])
        funding_net_after_stablecoin = float(
            stablecoin.get(
                "funding_net_after_stablecoin_reserve",
                planner.get("conservative_net_usd") or 0.0,
            )
            or 0.0
        )
        if funding_net_after_stablecoin <= 0:
            blockers.append("funding_net_after_stablecoin_reserve_not_positive")
        blockers = list(dict.fromkeys(blockers))
        status = shadow_status_from_blockers(blockers, funding_net_after_stablecoin)
        settlement_at = planner.get("expires_at")
        seconds_until_settlement = (
            (parse_iso(settlement_at) - now).total_seconds()
            if parse_iso(settlement_at) is not None
            else None
        )
        if (
            seconds_until_settlement is not None
            and seconds_until_settlement
            < -float(self.config.settlement_confirmation_timeout_seconds)
        ):
            status = "EXPIRED"
        skew = settlement_skew_for_display(
            long_market.get("next_funding_at"),
            short_market.get("next_funding_at"),
        )
        key = shadow_opportunity_key(
            environment=self.config.environment,
            asset=asset,
            long_venue=long_venue,
            short_venue=short_venue,
            long_collateral=stablecoin.get("stablecoin_pair", "").split("/", 1)[0],
            short_collateral=stablecoin.get("stablecoin_pair", "/").split("/", 1)[-1],
            settlement_at=settlement_at,
        )
        return {
            "opportunity_key": key,
            "environment": self.config.environment,
            "profile": self.config.profile,
            "canonical_asset": asset,
            "status": status,
            "long_venue": long_venue,
            "long_symbol": long_market.get("symbol"),
            "short_venue": short_venue,
            "short_symbol": short_market.get("symbol"),
            "settlement_at": settlement_at,
            "settlement_skew_seconds": skew,
            "seconds_until_settlement": seconds_until_settlement,
            "long_next_funding_rate": long_rate,
            "short_next_funding_rate": short_rate,
            "preliminary_gross_funding": preliminary_gross,
            "conservative_funding_cashflow_usd": conservative_gross,
            "expected_net_usd": planner.get("expected_net_usd"),
            "conservative_net_usd": planner.get("conservative_net_usd"),
            "conservative_net_bps": planner.get("conservative_net_bps"),
            "opportunity_shape": planner.get("opportunity_shape"),
            "planned_entry_at": planner.get("planned_entry_at"),
            "planned_exit_at": planner.get("planned_exit_at"),
            "included_settlement_events": planner.get("included_settlement_events") or [],
            "excluded_settlement_events": planner.get("excluded_settlement_events") or [],
            "ambiguous_settlement_events": planner.get("ambiguous_settlement_events") or [],
            "settlement_contracts": {
                "long": long_settlement_contract.as_dict(),
                "short": short_settlement_contract.as_dict(),
            },
            "planner": planner.get("planner") or {},
            "stablecoin_risk": stablecoin,
            "funding_net_excluding_points": stablecoin.get(
                "funding_net_after_stablecoin_reserve",
                funding_net_after_stablecoin,
            ),
            "capability_status": {
                "long": long_contract.as_dict(),
                "short": short_contract.as_dict(),
            },
            "blockers": blockers,
            "points_metadata": UNKNOWN_INCENTIVES.as_dict(),
            "observed_at": observed_at,
            "long_market": compact_market_shadow_payload(long_market),
            "short_market": compact_market_shadow_payload(short_market),
        }

    def _notify(self, message: str) -> dict[str, str | None]:
        print(message, flush=True)
        if not self.config.telegram_enabled or self.notifier is None:
            return {"status": "disabled", "error": None}
        result = self.notifier.send(message)
        return {"status": result.status, "error": result.error}

    def _summary_due(self) -> bool:
        now = self.clock.monotonic()
        if self.last_summary_monotonic <= 0:
            self.last_summary_monotonic = now
            return True
        if now - self.last_summary_monotonic >= self.config.periodic_summary_seconds:
            self.last_summary_monotonic = now
            return True
        return False


def adaptive_broad_sweep_interval_seconds(
    nearest_settlement_seconds: Any,
    *,
    near_threshold_seconds: float = 120.0,
    mid_threshold_seconds: float = 600.0,
    near_interval_seconds: float = 5.0,
    mid_interval_seconds: float = 10.0,
    far_interval_seconds: float = 30.0,
) -> float:
    if nearest_settlement_seconds is None:
        return float(far_interval_seconds)
    try:
        lead = float(nearest_settlement_seconds)
    except (TypeError, ValueError):
        return float(far_interval_seconds)
    if lead <= float(near_threshold_seconds):
        return float(near_interval_seconds)
    if lead <= float(mid_threshold_seconds):
        return float(mid_interval_seconds)
    return float(far_interval_seconds)


def worker_latency_fields(item: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    return {
        "request_started_at": item.get("request_started_at"),
        "response_received_at": item.get("response_received_at"),
        "parsing_completed_at": item.get("parsing_completed_at"),
        "network_latency_ms": latency_ms,
        "parsing_latency_ms": 0.0,
        "total_latency_ms": latency_ms,
        "endpoint_class": item.get("endpoint_class"),
    }


def client_environment(client: Any, requested_environment: str) -> str:
    del requested_environment
    if hasattr(client, "endpoint_identity"):
        return client_endpoint_identity(client).environment
    environment = str(getattr(client, "environment", "") or "").strip().lower()
    if environment in {"mainnet", "testnet"}:
        return environment
    return "unknown"


def mandatory_health_status(venue: str, fallback_status: str) -> str:
    if str(venue).lower() == "risex" and fallback_status != "healthy":
        return "MANDATORY_UNAVAILABLE"
    return fallback_status


def settlement_skew_for_display(first: Any, second: Any) -> float | None:
    first_time = parse_iso(first)
    second_time = parse_iso(second)
    if first_time is None or second_time is None:
        return None
    return abs((first_time.astimezone(UTC) - second_time.astimezone(UTC)).total_seconds())


def funding_shadow_safety_deltas(
    before: dict[str, float],
    after: dict[str, float],
) -> dict[str, float]:
    keys = sorted({*before, *after})
    return {
        key: float(after.get(key, 0.0)) - float(before.get(key, 0.0))
        for key in keys
    }


def optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def shadow_status_from_blockers(blockers: list[str], preliminary_gross: float) -> str:
    if not blockers and preliminary_gross > 0:
        return "SHADOW_CANDIDATE"
    if any("timestamp" in reason or "stale" in reason for reason in blockers):
        return "DATA_STALE"
    research_blockers = {
        "insufficient_stablecoin_price_sources",
        "stablecoin_price_provider_unavailable",
        "stablecoin_basis_above_30bps",
        "stablecoin_reserve_above_50bps",
        "stablecoin_family_not_compatible",
        "funding_net_after_stablecoin_reserve_not_positive",
        "funding_accrual_model_unknown",
        "funding_continuous_pro_rata",
        "position_inclusion_rule_unverified",
        "settlement_timing_uncertainty_unknown",
        "settlement_confirmation_unavailable",
        "rate_per_settlement_unknown",
        "displayed_rate_period_unknown",
        "settlement_interval_unknown",
        "shadow_candidate_disabled",
    }
    if any(
        reason in research_blockers
        or any(reason.endswith(f"_{blocker}") for blocker in research_blockers)
        for reason in blockers
    ):
        return "RESEARCH_ONLY"
    if blockers:
        return "CAPABILITY_BLOCKED"
    return "WATCH"


def opportunity_sort_value(opportunity: dict[str, Any]) -> float:
    stablecoin = opportunity.get("stablecoin_risk") or {}
    for key in ("funding_net_after_stablecoin_reserve", "preliminary_gross_funding"):
        try:
            return float(stablecoin.get(key, opportunity.get(key)))
        except (TypeError, ValueError):
            continue
    return 0.0


def shadow_opportunity_key(
    *,
    environment: str,
    asset: str,
    long_venue: str,
    short_venue: str,
    long_collateral: str,
    short_collateral: str,
    settlement_at: Any,
) -> str:
    raw = "|".join(
        [
            str(environment),
            str(asset),
            str(long_venue),
            str(short_venue),
            str(long_collateral),
            str(short_collateral),
            str(settlement_at or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def compact_market_shadow_payload(market: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "venue",
        "symbol",
        "canonical_asset",
        "environment",
        "normalized_next_funding_rate",
        "next_funding_at",
        "mark_price",
        "index_price",
        "volume_24h_usd",
        "open_interest_usd",
        "contract_status",
        "status",
        "response_received_at",
        "source_event_at",
        "collateral_asset",
        "quote_asset",
        "price_quote_currency",
        "settlement_collateral",
        "collateral_family",
        "funding_accrual_model",
        "position_inclusion_rule_verified",
        "settlement_interval_seconds",
        "displayed_rate_period_seconds",
        "settlement_semantics_status",
        "assessment_jitter_before_seconds",
        "assessment_jitter_after_seconds",
        "settlement_confirmation_source",
        "realized_payment_source",
        "stablecoin_route_evaluation",
        "stablecoin_risk",
    )
    return {field: market.get(field) for field in fields if field in market}


def shadow_observations_for_opportunity(
    opportunity: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for side in ("long", "short"):
        market = opportunity.get(f"{side}_market") or {}
        rows.append(
            {
                "opportunity_key": opportunity["opportunity_key"],
                "environment": opportunity["environment"],
                "venue": market.get("venue"),
                "symbol": market.get("symbol"),
                "canonical_asset": opportunity["canonical_asset"],
                "side": side,
                "observed_at": opportunity["observed_at"],
                "next_funding_at": market.get("next_funding_at"),
                "normalized_next_funding_rate": market.get(
                    "normalized_next_funding_rate"
                ),
                "mark_price": market.get("mark_price"),
                "index_price": market.get("index_price"),
                "volume_24h_usd": market.get("volume_24h_usd"),
                "open_interest_usd": market.get("open_interest_usd"),
                "response_received_at": market.get("response_received_at"),
                "payload": market,
            }
        )
    return rows


def shadow_opportunity_message(opportunity: dict[str, Any]) -> str:
    stablecoin = opportunity.get("stablecoin_risk") or {}
    blockers = opportunity.get("blockers") or []
    return (
        "<b>SHADOW FUNDING</b>\n\n"
        "SHADOW FUNDING WINDOW\n\n"
        f"Asset: {opportunity.get('canonical_asset')}\n"
        f"LONG venue / collateral: {opportunity.get('long_venue')} / "
        f"{stablecoin.get('stablecoin_pair', '/').split('/', 1)[0]}\n"
        f"SHORT venue / collateral: {opportunity.get('short_venue')} / "
        f"{stablecoin.get('stablecoin_pair', '/').split('/', 1)[-1]}\n\n"
        f"Environment: {opportunity.get('environment')}\n"
        f"Settlement time: {opportunity.get('settlement_at') or '-'}\n"
        f"Settlement skew: {opportunity.get('settlement_skew_seconds')}\n"
        f"Seconds until settlement: {opportunity.get('seconds_until_settlement')}\n\n"
        f"Long next funding: {opportunity.get('long_next_funding_rate')}\n"
        f"Short next funding: {opportunity.get('short_next_funding_rate')}\n"
        f"Preliminary gross funding: {opportunity.get('preliminary_gross_funding')}\n\n"
        f"Stablecoin pair: {stablecoin.get('stablecoin_pair')}\n"
        f"Stablecoin basis: {stablecoin.get('current_stablecoin_basis_bps')}\n"
        f"Stablecoin reserve: {stablecoin.get('stablecoin_reserve_bps')}\n"
        f"Funding net after stablecoin reserve: "
        f"{stablecoin.get('funding_net_after_stablecoin_reserve')}\n\n"
        f"Capability status: {opportunity.get('status')}\n"
        "Points programs: UNKNOWN\n"
        f"Status: {opportunity.get('status')}\n"
        f"Exact blockers: {', '.join(blockers) if blockers else '-'}\n\n"
        "NO POSITION OPENED"
    )


def shadow_summary_message(
    summary: dict[str, Any],
    opportunities: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    top = opportunities[:5]
    top_lines = [
        f"{row.get('canonical_asset')} {row.get('long_venue')}/{row.get('short_venue')} "
        f"{row.get('status')} net={opportunity_sort_value(row):.4f}"
        for row in top
    ]
    blockers = summary.get("rejection_reasons") or {}
    common_blockers = ", ".join(
        f"{key}:{value}" for key, value in list(blockers.items())[:8]
    ) or "-"
    return (
        "<b>SHADOW FUNDING</b>\n\n"
        "periodic summary\n"
        f"venues checked: {summary.get('venues_checked', 0)}\n"
        f"markets checked: {summary.get('markets_checked', 0)}\n"
        f"aligned routes: {summary.get('aligned_routes', 0)}\n"
        f"shadow candidates: {summary.get('shadow_candidates', 0)}\n"
        f"research-only routes: {summary.get('research_only_routes', 0)}\n"
        f"top 5 opportunities: {'; '.join(top_lines) or '-'}\n"
        f"exact common blockers: {common_blockers}\n"
        f"warnings: {', '.join(warnings[:5]) or '-'}\n\n"
        "NO POSITION OPENED"
    )
