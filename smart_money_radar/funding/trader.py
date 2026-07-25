from __future__ import annotations

import csv
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any

from smart_money_radar.bots.base import BaseBot
from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.funding.adapters import (
    AevoFundingClient,
    ApexFundingClient,
    AsterFundingClient,
    BackpackFundingClient,
    BinanceFundingClient,
    BingXFundingClient,
    BitMartFundingClient,
    BitgetFundingClient,
    BybitFundingClient,
    CoinExFundingClient,
    DeribitFundingClient,
    DriftFundingClient,
    DydxFundingClient,
    EdgexFundingClient,
    EtherealFundingClient,
    ExtendedFundingClient,
    FundingVenueClient,
    GateFundingClient,
    GrvtFundingClient,
    HTXFundingClient,
    HyperliquidFundingClient,
    KrakenFundingClient,
    KuCoinFundingClient,
    LighterFundingClient,
    MEXCFundingClient,
    OKXFundingClient,
    PacificaFundingClient,
    ParadexFundingClient,
    PhemexFundingClient,
    ReyaFundingClient,
    RiseXFundingClient,
    VariationalFundingClient,
    VertexFundingClient,
    WOOXFundingClient,
)
from smart_money_radar.funding.adapters.base import FundingDataError, FundingHttpClient
from smart_money_radar.funding.economics import evaluate_perp_route
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import (
    normalize_orderbook_canonical_units,
    normalize_stored_orderbook_units,
)
from smart_money_radar.funding.presentation import (
    filter_deactivated_funding_paper_payload,
)
from smart_money_radar.funding.retention import apply_funding_retention_plan
from smart_money_radar.funding.service import run_funding_scan
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore, utc_now_iso


FUNDING_HISTORY_RETENTION_PER_MARKET = 24
FUNDING_PAPER_WATCH_SCAN_RETENTION = 1


@dataclass(frozen=True)
class FundingPaperTraderConfig:
    venue_starting_balance: float = 1_000.0
    target_notional_per_leg: float = 500.0
    entry_window_seconds: int = 180
    entry_min_lead_seconds: int = 0
    entry_max_lead_seconds: int = 15
    arm_window_seconds: int = 900
    final_recheck_freeze_seconds: int = 15
    max_entry_snapshot_age_seconds: int = 30
    settlement_grace_seconds: int = 90
    max_settlement_publication_lag_seconds: int = 300
    collateral_reserve_fraction: float = 0.10
    min_live_net_profit: float = 0.0
    scan_interval_seconds: int = 300
    monitor_interval_seconds: int = 120
    hot_interval_seconds: int = 10
    hot_route_recheck_workers: int = 6
    status_report_interval_seconds: int = 1_800
    status_report_max_routes: int = 5
    retention_interval_seconds: int = 300
    iterations: int | None = None
    export_dir: Path = PROJECT_ROOT / "exports" / "funding_paper"
    telegram_enabled: bool = True
    focused_recheck_enabled: bool = True
    venue_set: tuple[str, ...] | None = None
    spread_arb_enabled: bool = False
    basis_stop_loss_bps: float = 200.0
    spread_monitoring_enabled: bool = True

    def validated(self) -> "FundingPaperTraderConfig":
        return FundingPaperTraderConfig(
            venue_starting_balance=max(100.0, float(self.venue_starting_balance)),
            target_notional_per_leg=max(50.0, float(self.target_notional_per_leg)),
            entry_window_seconds=max(15, min(int(self.entry_window_seconds), 900)),
            entry_min_lead_seconds=max(
                0,
                min(int(self.entry_min_lead_seconds), 300),
            ),
            entry_max_lead_seconds=max(
                1,
                min(int(self.entry_max_lead_seconds), 900),
            ),
            arm_window_seconds=max(
                int(self.entry_window_seconds),
                min(int(self.arm_window_seconds), 7_200),
            ),
            final_recheck_freeze_seconds=max(
                0,
                min(int(self.final_recheck_freeze_seconds), 60),
            ),
            max_entry_snapshot_age_seconds=max(
                5,
                min(int(self.max_entry_snapshot_age_seconds), 300),
            ),
            settlement_grace_seconds=max(
                15,
                min(int(self.settlement_grace_seconds), 1_800),
            ),
            max_settlement_publication_lag_seconds=max(
                60,
                min(int(self.max_settlement_publication_lag_seconds), 7_200),
            ),
            collateral_reserve_fraction=max(
                0.0,
                min(float(self.collateral_reserve_fraction), 1.0),
            ),
            min_live_net_profit=max(0.0, float(self.min_live_net_profit)),
            scan_interval_seconds=max(10, int(self.scan_interval_seconds)),
            monitor_interval_seconds=max(10, int(self.monitor_interval_seconds)),
            hot_interval_seconds=max(5, int(self.hot_interval_seconds)),
            hot_route_recheck_workers=max(
                1,
                min(int(self.hot_route_recheck_workers), 16),
            ),
            status_report_interval_seconds=max(
                0,
                min(int(self.status_report_interval_seconds), 86_400),
            ),
            status_report_max_routes=max(
                1,
                min(int(self.status_report_max_routes), 20),
            ),
            retention_interval_seconds=max(
                30,
                min(int(self.retention_interval_seconds), 3_600),
            ),
            iterations=(
                None
                if self.iterations is None
                else max(1, int(self.iterations))
            ),
            export_dir=Path(self.export_dir),
            telegram_enabled=bool(self.telegram_enabled),
            focused_recheck_enabled=bool(self.focused_recheck_enabled),
            venue_set=(
                tuple(str(v) for v in self.venue_set)
                if self.venue_set
                else None
            ),
            spread_arb_enabled=bool(self.spread_arb_enabled),
            basis_stop_loss_bps=max(0.0, float(self.basis_stop_loss_bps)),
            spread_monitoring_enabled=bool(self.spread_monitoring_enabled),
        ).normalized_entry_leads()

    def normalized_entry_leads(self) -> "FundingPaperTraderConfig":
        minimum = max(0, int(self.entry_min_lead_seconds))
        maximum = max(minimum, int(self.entry_max_lead_seconds))
        maximum = min(maximum, int(self.entry_window_seconds))
        minimum = min(minimum, maximum)
        return FundingPaperTraderConfig(
            **{
                **asdict(self),
                "entry_min_lead_seconds": minimum,
                "entry_max_lead_seconds": maximum,
            }
        )


class FundingPaperTrader(BaseBot):
    def __init__(
        self,
        store: SQLiteStore,
        config: FundingPaperTraderConfig | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        cfg = (config or FundingPaperTraderConfig()).validated()
        super().__init__(
            iterations=cfg.iterations,
            telegram_enabled=cfg.telegram_enabled,
            notifier=notifier or TelegramNotifier(),
        )
        self.store = store
        self.config = cfg
        self.armed_routes: set[str] = set()
        self.hot_routes: dict[str, dict[str, Any]] = {}
        self.last_full_scan_monotonic = 0.0
        self.last_status_report_monotonic = 0.0
        self.last_retention_monotonic = 0.0
        self.pending_notified_positions: set[int] = set()

    def run_loop(self) -> None:
        completed = 0
        previous_signal_handlers = self.install_signal_handlers()
        try:
            self.record_event(
                "trader_started",
                "Funding Paper Trader started",
                {"config": serializable_config(self.config)},
                notify=True,
            )
            while (
                not self.stop_requested
                and (self.config.iterations is None or completed < self.config.iterations)
            ):
                try:
                    result = self.run_iteration()
                except Exception as exc:
                    self.notify_shutdown(
                        "trader_crashed",
                        (
                            "Funding Paper Trader CRASHED\n"
                            f"Error: {type(exc).__name__}: {exc}\n"
                            "Paper trading loop is not running."
                        ),
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "last_iteration": self.last_iteration_result,
                        },
                        severity="error",
                    )
                    raise
                self.last_iteration_result = result
                completed += 1
                if self.config.iterations is not None and completed >= self.config.iterations:
                    break
                self.sleep_interruptibly(self.next_sleep_seconds(result))
            if self.stop_requested:
                self.notify_shutdown(
                    "trader_stopped",
                    (
                        "Funding Paper Trader STOPPED\n"
                        f"Reason: {self.stop_reason or 'stop requested'}\n"
                        "Paper trading loop is not running."
                    ),
                    {
                        "reason": self.stop_reason,
                        "completed_iterations": completed,
                        "last_iteration": self.last_iteration_result,
                    },
                    severity="warning",
                )
        except KeyboardInterrupt:
            self.request_stop("KeyboardInterrupt")
            self.notify_shutdown(
                "trader_interrupted",
                (
                    "Funding Paper Trader INTERRUPTED\n"
                    "Reason: KeyboardInterrupt\n"
                    "Paper trading loop is not running."
                ),
                {
                    "reason": "KeyboardInterrupt",
                    "completed_iterations": completed,
                    "last_iteration": self.last_iteration_result,
                },
                severity="warning",
            )
            raise
        finally:
            self.restore_signal_handlers(previous_signal_handlers)

    def notify_shutdown(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        *,
        severity: str,
    ) -> None:
        if self.shutdown_notified:
            return
        self.shutdown_notified = True
        self.record_event(
            event_type,
            message,
            payload,
            notify=True,
            severity=severity,
        )

    def run_iteration(self) -> dict[str, Any]:
        if self.should_run_hot_iteration():
            return self.run_hot_iteration()
        return self.run_full_iteration()

    def should_run_hot_iteration(self) -> bool:
        if self.store.funding_paper_open_positions():
            return True
        if not self.hot_routes or self.last_full_scan_monotonic <= 0:
            return False
        now = datetime.now(UTC)
        if any(
            route_monitor_decision(route, now, self.config)["urgent"]
            for route in self.hot_routes.values()
        ):
            return True
        elapsed = time.monotonic() - self.last_full_scan_monotonic
        return elapsed < self.config.scan_interval_seconds

    def run_full_iteration(self) -> dict[str, Any]:
        self.store.init_db()
        scan_result = run_funding_scan(
            self.store,
            config=self.scan_config(),
            venue_clients=self.build_venue_clients(),
            scan_mode="watch",
        )
        self.last_full_scan_monotonic = time.monotonic()
        funding = self.store.funding_dashboard(
            horizon_mode="next_settlement",
            include_watch_scans=True,
        )
        routes = list(funding.get("routes") or [])
        watch_routes = list(funding.get("watch_routes") or [])
        venues = venues_from_funding_payload(funding) or venues_from_routes(
            [*routes, *watch_routes]
        )
        self.store.ensure_funding_paper_accounts(
            venues,
            self.config.venue_starting_balance,
        )
        self.update_hot_routes([*routes, *watch_routes])
        close_events = self.process_open_positions()
        entry_events = self.process_entry_candidates(routes)
        repriced_count = self.refresh_and_publish_repriced_pnl()
        snapshot = self.store.record_funding_paper_equity_snapshot()
        export_funding_paper_csv(self.store, self.config.export_dir)
        self.apply_watch_scan_retention(minimum_keep_latest_scans=max(1, len(routes) + 1))
        result = {
            "mode": "full_market",
            "funding_scan_id": scan_result["funding_scan_id"],
            "candidate_count": len(routes),
            "watch_count": len(watch_routes),
            "opened_count": len(entry_events),
            "closed_count": sum(1 for event in close_events if event == "closed"),
            "repriced_count": repriced_count,
            "pending_count": sum(
                1 for event in close_events if event == "settlement_pending"
            ),
            "held_count": sum(1 for event in close_events if event == "held"),
            "open_position_count": snapshot["open_position_count"],
            "hot_route_count": count_hot_routes([*routes, *watch_routes], self.config),
            "urgent_route_count": count_urgent_routes([*routes, *watch_routes], self.config),
            "universe_route_count": scan_result.get("universe_route_count"),
            "execution_shortlist_count": scan_result.get("execution_shortlist_count"),
            "route_count": scan_result.get("route_count"),
            "equity": snapshot,
        }
        if should_record_routine_scan(result):
            self.record_event(
                "scan",
                (
                    "Funding paper scan "
                    f"{scan_result['funding_scan_id']}: candidates={len(routes)}, "
                    f"opened={result['opened_count']}, closed={result['closed_count']}"
                ),
                result,
                notify=False,
            )
        self.maybe_record_status_report(result, routes, watch_routes)
        return result

    def run_hot_iteration(self) -> dict[str, Any]:
        self.store.init_db()
        rechecked_routes = self.refresh_hot_routes()
        routes = [
            row for row in rechecked_routes if row.get("status") == "paper_candidate"
        ]
        watch_routes = [
            row for row in rechecked_routes if row.get("status") != "paper_candidate"
        ]
        venues = venues_from_routes([*routes, *watch_routes])
        self.store.ensure_funding_paper_accounts(
            venues,
            self.config.venue_starting_balance,
        )
        close_events = self.process_open_positions()
        entry_events = self.process_entry_candidates(
            routes,
            recheck_before_open=False,
        )
        repriced_count = self.refresh_and_publish_repriced_pnl()
        snapshot = self.store.record_funding_paper_equity_snapshot()
        export_funding_paper_csv(self.store, self.config.export_dir)
        self.apply_watch_scan_retention(
            minimum_keep_latest_scans=max(1, len(rechecked_routes) + 1)
        )
        scan_ids = [
            int(row.get("funding_scan_id") or 0)
            for row in rechecked_routes
            if row.get("funding_scan_id")
        ]
        result = {
            "mode": "hot_routes",
            "funding_scan_id": max(scan_ids, default=None),
            "candidate_count": len(routes),
            "watch_count": len(watch_routes),
            "opened_count": len(entry_events),
            "closed_count": sum(1 for event in close_events if event == "closed"),
            "repriced_count": repriced_count,
            "pending_count": sum(
                1 for event in close_events if event == "settlement_pending"
            ),
            "held_count": sum(1 for event in close_events if event == "held"),
            "open_position_count": snapshot["open_position_count"],
            "hot_route_count": len(self.hot_routes),
            "urgent_route_count": count_urgent_routes(
                list(self.hot_routes.values()),
                self.config,
            ),
            "equity": snapshot,
        }
        if should_record_routine_scan(result):
            self.record_event(
                "hot_scan",
                (
                    "Funding paper hot scan: "
                    f"tracked={len(self.hot_routes)}, candidates={len(routes)}, "
                    f"opened={result['opened_count']}, closed={result['closed_count']}"
                ),
                result,
                notify=False,
            )
        self.maybe_record_status_report(result, routes, watch_routes)
        return result

    def apply_watch_scan_retention(
        self,
        *,
        minimum_keep_latest_scans: int = FUNDING_PAPER_WATCH_SCAN_RETENTION,
    ) -> None:
        now_monotonic = time.monotonic()
        if (
            self.last_retention_monotonic > 0
            and now_monotonic - self.last_retention_monotonic
            < self.config.retention_interval_seconds
        ):
            return
        keep_latest_scans = max(
            FUNDING_PAPER_WATCH_SCAN_RETENTION,
            int(minimum_keep_latest_scans),
        )
        try:
            apply_funding_retention_plan(
                self.store,
                keep_latest_scans=keep_latest_scans,
                keep_latest_history_per_market=FUNDING_HISTORY_RETENTION_PER_MARKET,
            )
            self.last_retention_monotonic = time.monotonic()
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            if not is_retention_skip_error(exc):
                raise
            self.last_retention_monotonic = time.monotonic()
            try:
                self.record_event(
                    "retention_skipped",
                    (
                        "Funding Paper Trader retention skipped\n"
                        f"Reason: {type(exc).__name__}: {exc}; "
                        "trading loop continues."
                    ),
                    {"error": str(exc), "keep_latest_scans": keep_latest_scans},
                    notify=False,
                    severity="warning",
                )
            except sqlite3.DatabaseError:
                pass

    def maybe_record_status_report(
        self,
        result: dict[str, Any],
        routes: list[dict[str, Any]],
        watch_routes: list[dict[str, Any]],
    ) -> None:
        interval = int(self.config.status_report_interval_seconds)
        if interval <= 0:
            return
        now_monotonic = time.monotonic()
        if (
            self.last_status_report_monotonic > 0
            and now_monotonic - self.last_status_report_monotonic < interval
        ):
            return
        self.last_status_report_monotonic = now_monotonic
        dashboard = filter_deactivated_funding_paper_payload(
            self.store.funding_paper_dashboard(refresh_estimates=False)
        )
        publishable_routes = [
            route for route in routes if status_publishable_candidate(route, self.config)
        ]
        payload = {
            "result": result,
            "summary": dashboard.get("summary") or {},
            "candidate_routes": [
                route_summary(route)
                for route in ranked_status_routes(publishable_routes)[
                    : self.config.status_report_max_routes
                ]
            ],
            "internal_watch_count": len(watch_routes),
        }
        self.record_event(
            "status_report",
            status_report_message(
                result,
                routes,
                watch_routes,
                dashboard.get("summary") or {},
                self.config,
            ),
            payload,
            funding_scan_id=result.get("funding_scan_id"),
            notify=True,
        )

    def scan_config(self) -> FundingScanConfig:
        return FundingScanConfig(
            target_notional=self.config.target_notional_per_leg,
            horizon_mode="next_settlement",
            horizon_hours=None,
            near_miss_full_depth_routes=0,
            history_refresh_hours=0.25,
            max_live_history_markets=24,
            orderbook_cache_ttl_seconds=0,
            market_snapshot_cache_ttl_seconds=0,
        ).validated()

    def build_venue_clients(self) -> list[FundingVenueClient] | None:
        if not self.config.venue_set:
            return None
        from smart_money_radar.funding.service import active_default_funding_clients

        allowed = set(self.config.venue_set)
        return [
            client
            for client in active_default_funding_clients()
            if client.venue in allowed
        ]

    def focused_scan_config(self) -> FundingScanConfig:
        return FundingScanConfig(
            target_notional=self.config.target_notional_per_leg,
            horizon_mode="next_settlement",
            horizon_hours=None,
            near_miss_full_depth_routes=0,
            history_refresh_hours=48.0,
            max_live_history_markets=0,
            orderbook_cache_ttl_seconds=0,
            market_snapshot_cache_ttl_seconds=0,
        ).validated()

    def update_hot_routes(self, routes: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        current_keys: set[str] = set()
        for route in routes:
            route_key = str(route.get("route_key") or "")
            if not route_key:
                continue
            if route_monitor_decision(route, now, self.config)["hot"]:
                self.hot_routes[route_key] = route
                current_keys.add(route_key)
        for route_key, route in list(self.hot_routes.items()):
            if route_key in current_keys:
                continue
            if not route_monitor_decision(route, now, self.config)["hot"]:
                self.hot_routes.pop(route_key, None)

    def refresh_hot_routes(self) -> list[dict[str, Any]]:
        route_items = list(self.hot_routes.items())
        if not route_items:
            return []
        refreshed_by_key: dict[str, dict[str, Any]] = {}

        def handle_result(
            route_key: str,
            route: dict[str, Any],
            fresh: dict[str, Any] | None,
        ) -> None:
            if not fresh:
                self.hot_routes.pop(route_key, None)
                return
            decision = route_monitor_decision(fresh, datetime.now(UTC), self.config)
            if decision["hot"]:
                self.hot_routes[route_key] = fresh
                refreshed_by_key[route_key] = fresh
            else:
                self.hot_routes.pop(route_key, None)
                self.record_event(
                    "disarmed",
                    disarmed_message(fresh, decision),
                    {"route": route_summary(fresh), "decision": decision},
                    funding_scan_id=fresh.get("funding_scan_id"),
                    funding_route_id=fresh.get("funding_route_id"),
                    route_key=fresh.get("route_key"),
                    notify=False,
                    severity="warning",
                )

        workers = min(self.config.hot_route_recheck_workers, len(route_items))
        if workers <= 1:
            for route_key, route in route_items:
                handle_result(route_key, route, self.focused_recheck_route(route))
            return [
                refreshed_by_key[route_key]
                for route_key, _route in route_items
                if route_key in refreshed_by_key
            ]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.focused_recheck_route, route): (route_key, route)
                for route_key, route in route_items
            }
            for future in as_completed(futures):
                route_key, route = futures[future]
                try:
                    fresh = future.result()
                except Exception as exc:
                    self.record_event(
                        "focused_recheck_failed",
                        (
                            "Funding Paper Trader focused recheck failed\n"
                            f"{route.get('canonical_asset')}: "
                            f"LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
                            f"Error: {type(exc).__name__}: {exc}"
                        ),
                        {"route": route_summary(route), "error": str(exc)},
                        route_key=route.get("route_key"),
                        notify=False,
                        severity="warning",
                    )
                    fresh = None
                handle_result(route_key, route, fresh)
        return [
            refreshed_by_key[route_key]
            for route_key, _route in route_items
            if route_key in refreshed_by_key
        ]

    def focused_recheck_route(self, route: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.focused_recheck_enabled:
            return self.store.latest_funding_route_by_key(str(route.get("route_key") or ""))
        now = datetime.now(UTC)
        if final_recheck_freeze_window_active(route, now, self.config):
            frozen = final_recheck_fallback_route(
                route,
                now,
                self.config,
                "final_recheck_freeze_window",
            )
            if frozen is not None:
                return frozen
            self.record_event(
                "focused_recheck_skipped",
                (
                    "Funding Paper Trader skipped final recheck\n"
                    f"{route.get('canonical_asset')}: "
                    f"LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
                    "Reason: no fresh snapshot available inside final freeze window."
                ),
                {
                    "route": route_summary(route),
                    "reason": "fresh_snapshot_missing_inside_final_freeze_window",
                    "snapshot_age_seconds": route_data_age_seconds(route, now),
                    "max_snapshot_age_seconds": self.config.max_entry_snapshot_age_seconds,
                    "freeze_seconds": self.config.final_recheck_freeze_seconds,
                    "lead_seconds": route_settlement_leads(route, now),
                },
                route_key=route.get("route_key"),
                notify=False,
                severity="warning",
            )
            return None
        try:
            fresh = self.direct_focused_recheck_route(route)
            if fresh is not None:
                return fresh
        except Exception as exc:
            fallback = final_recheck_fallback_route(
                route,
                datetime.now(UTC),
                self.config,
                "focused_recheck_failed_inside_freeze_window",
            )
            if fallback is not None:
                self.record_event(
                    "focused_recheck_fallback",
                    (
                        "Funding Paper Trader used last successful focused snapshot\n"
                        f"{route.get('canonical_asset')}: "
                        f"LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
                        f"Reason: {type(exc).__name__}: {exc}"
                    ),
                    {
                        "route": route_summary(fallback),
                        "error": str(exc),
                        "fallback": (fallback.get("evidence") or {}).get(
                            "focused_recheck"
                        ),
                    },
                    route_key=route.get("route_key"),
                    notify=False,
                    severity="warning",
                )
                return fallback
            self.record_event(
                "focused_recheck_failed",
                (
                    "Funding Paper Trader focused recheck failed\n"
                    f"{route.get('canonical_asset')}: "
                    f"LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
                    f"Error: {type(exc).__name__}: {exc}"
                ),
                {"route": route_summary(route), "error": str(exc)},
                route_key=route.get("route_key"),
                notify=False,
                severity="warning",
            )
            # Fall through to scan-based fallback instead of returning None.
        clients = funding_clients_for_route(route, fast=True)
        if len(clients) < 2:
            return None
        try:
            result = run_funding_scan(
                self.store,
                config=self.focused_scan_config(),
                venue_clients=clients,
                scan_mode="watch",
                hydrate_missing_history=False,
            )
        except Exception as exc:
            self.record_event(
                "focused_recheck_failed",
                (
                    "Funding Paper Trader focused recheck failed\n"
                    f"{route.get('canonical_asset')}: "
                    f"LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
                    f"Error: {type(exc).__name__}: {exc}"
                ),
                {"route": route_summary(route), "error": str(exc)},
                route_key=route.get("route_key"),
                notify=False,
                severity="warning",
            )
            return None
        return self.store.funding_route_by_scan_and_key(
            int(result["funding_scan_id"]),
            str(route.get("route_key") or ""),
        )

    def direct_focused_recheck_route(
        self,
        route: dict[str, Any],
    ) -> dict[str, Any] | None:
        route_key = str(route.get("route_key") or "")
        if not route_key:
            return None
        settings = self.focused_scan_config()
        scan_id = self.store.start_funding_scan(
            {
                **asdict(settings),
                "scan_mode": "watch",
                "focused_route_key": route_key,
                "focused_route_mode": "direct_symbol_recheck_v1",
            }
        )
        observed_at = utc_now_iso()
        started = time.perf_counter()
        warnings: list[str] = []
        counts = {
            "instrument_count": 0,
            "market_snapshot_count": 0,
            "orderbook_count": 0,
            "history_row_count": 0,
            "route_count": 0,
            "paper_candidate_count": 0,
            "paper_execution_count": 0,
        }
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                market_futures = {
                    executor.submit(
                        self.fresh_market_for_route_leg,
                        route,
                        side,
                        observed_at,
                    ): side
                    for side in ("long", "short")
                }
                fresh_markets = {
                    side: future.result()
                    for future, side in market_futures.items()
                }
            long_market, long_client = fresh_markets["long"]
            short_market, short_client = fresh_markets["short"]
            markets = [long_market, short_market]
            counts["market_snapshot_count"] = self.store.insert_funding_market_snapshots(
                scan_id,
                markets,
                include_raw_json=settings.store_diagnostic_raw_json,
            )
            books = self.fetch_direct_orderbooks(
                [
                    (long_market, long_client),
                    (short_market, short_client),
                ],
                observed_at,
            )
            counts["orderbook_count"] = self.store.insert_funding_orderbooks(
                scan_id,
                list(books.values()),
                include_raw_json=settings.store_diagnostic_raw_json,
            )
            observed_datetime = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            )
            if observed_datetime.tzinfo is None:
                observed_datetime = observed_datetime.replace(tzinfo=UTC)
            history_start = observed_datetime.astimezone(UTC) - timedelta(
                days=settings.history_days
            )
            history_keys = [
                (str(long_market["venue"]), str(long_market["symbol"])),
                (str(short_market["venue"]), str(short_market["symbol"])),
            ]
            history = {
                key: self.store.funding_history_rows(
                    key[0],
                    key[1],
                    since=history_start.isoformat(),
                )
                for key in history_keys
            }
            book_sequences = self.store.funding_orderbook_sequences(
                history_keys,
                limit_per_market=settings.liquidity_sequence_limit,
                before_at=observed_at,
            )
            for key, book in books.items():
                market = long_market if key[0] == long_market["venue"] else short_market
                book["_history"] = normalize_stored_orderbook_units(
                    book_sequences.get(key, []),
                    float(market.get("canonical_unit_multiplier") or 1.0),
                    book.get("mid_price"),
                )
            fresh_route = evaluate_perp_route(
                long_market,
                short_market,
                books[(str(long_market["venue"]), str(long_market["symbol"]))],
                books[(str(short_market["venue"]), str(short_market["symbol"]))],
                history.get((str(long_market["venue"]), str(long_market["symbol"])), []),
                history.get((str(short_market["venue"]), str(short_market["symbol"])), []),
                observed_at,
                settings,
            )
            latency = time.perf_counter() - started
            fresh_route.setdefault("evidence", {})["focused_recheck"] = {
                "mode": "direct_symbol_recheck_v1",
                "latency_seconds": latency,
                "observed_at": observed_at,
            }
            fresh_route["funding_scan_id"] = scan_id
            stored_routes = self.store.insert_funding_routes(scan_id, [fresh_route])
            counts["route_count"] = len(stored_routes)
            counts["paper_candidate_count"] = sum(
                row["status"] == "paper_candidate" for row in stored_routes
            )
            self.store.insert_funding_scan_warnings(scan_id, warnings)
            self.store.finish_funding_scan(scan_id, "success", **counts)
            return stored_routes[0] if stored_routes else None
        except Exception as exc:
            warnings.append(f"direct focused recheck failed: {exc}")
            self.store.insert_funding_scan_warnings(scan_id, warnings)
            self.store.finish_funding_scan(
                scan_id,
                "failed",
                error=str(exc),
                **counts,
            )
            raise

    def fresh_market_for_route_leg(
        self,
        route: dict[str, Any],
        side: str,
        observed_at: str,
    ) -> tuple[dict[str, Any], FundingVenueClient]:
        leg = leg_by_side(route.get("legs") or [], side) or {}
        venue = str(leg.get("venue") or route.get(f"{side}_venue") or "")
        symbol = str(leg.get("symbol") or route.get(f"{side}_symbol") or "")
        asset = str(route.get("canonical_asset") or "")
        if not venue or not symbol or not asset:
            raise FundingDataError(f"Missing {side} route leg for focused recheck")
        client = funding_client_for_venue(venue, fast=True)
        if client is None:
            raise FundingDataError(f"No funding client for {venue}")
        previous = self.store.latest_funding_market_with_instrument(venue, symbol) or {}
        snapshot_method = getattr(client, "market_snapshot", None)
        if callable(snapshot_method):
            market = snapshot_method(symbol, asset, observed_at, previous)
        else:
            _instruments, markets, _warnings = client.catalog_and_markets(observed_at)
            market = next(
                (
                    row
                    for row in markets
                    if str(row.get("symbol") or "") == symbol
                    and str(row.get("venue") or "") == venue
                ),
                None,
            )
            if market is None:
                raise FundingDataError(f"{venue} {symbol} market unavailable")
        market["canonical_unit_multiplier"] = previous.get(
            "canonical_unit_multiplier",
            market.get("canonical_unit_multiplier", 1.0),
        )
        market["contract_multiplier"] = previous.get(
            "contract_multiplier",
            market.get("contract_multiplier", 1.0),
        )
        return market, client

    def fetch_direct_orderbooks(
        self,
        markets_and_clients: list[tuple[dict[str, Any], FundingVenueClient]],
        observed_at: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        books: dict[tuple[str, str], dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(markets_and_clients))) as executor:
            futures = {
                executor.submit(
                    client.orderbook,
                    str(market["symbol"]),
                    observed_at,
                    100,
                ): (market, client)
                for market, client in markets_and_clients
            }
            for future in as_completed(futures):
                market, _client = futures[future]
                key = (str(market["venue"]), str(market["symbol"]))
                books[key] = normalize_orderbook_canonical_units(
                    future.result(),
                    float(market.get("canonical_unit_multiplier") or 1.0),
                )
        return books

    def next_sleep_seconds(self, result: dict[str, Any]) -> int:
        if int(result.get("open_position_count") or 0) > 0:
            return self.config.hot_interval_seconds
        if int(result.get("pending_count") or 0) > 0:
            return self.config.hot_interval_seconds
        if int(result.get("urgent_route_count") or 0) > 0:
            return self.config.hot_interval_seconds
        if int(result.get("hot_route_count") or 0) > 0:
            return self.config.monitor_interval_seconds
        return self.config.scan_interval_seconds

    def process_entry_candidates(
        self,
        routes: list[dict[str, Any]],
        *,
        recheck_before_open: bool = True,
    ) -> list[int]:
        opened: list[int] = []
        now = datetime.now(UTC)
        accounts = {
            row["venue"]: row for row in self.store.funding_paper_account_rows()
        }
        open_route_keys = {
            str(position.get("route_key") or "")
            for position in self.store.funding_paper_open_positions()
        }
        for route in routes:
            route_key = str(route.get("route_key") or "")
            if route_key and route_key in open_route_keys:
                continue
            decision = route_entry_decision(route, accounts, now, self.config)
            if decision["armed"] and route["route_key"] not in self.armed_routes:
                self.armed_routes.add(route["route_key"])
                self.record_event(
                    "armed",
                    armed_message(route, decision),
                    {"route": route_summary(route), "decision": decision},
                    funding_scan_id=route.get("funding_scan_id"),
                    funding_route_id=route.get("funding_route_id"),
                    route_key=route.get("route_key"),
                    notify=True,
                )
            if not decision["eligible"]:
                continue
            active_route = route
            if recheck_before_open and self.config.focused_recheck_enabled:
                rechecked = self.focused_recheck_route(route)
                if not rechecked:
                    self.record_event(
                        "open_skipped",
                        (
                            "<b>Funding Paper Trader SKIP OPEN</b>\n\n"
                            f"<b>{tg(route.get('canonical_asset'))}</b>\n"
                            "Focused recheck did not return the route."
                        ),
                        {"route": route_summary(route), "reason": "focused_route_missing"},
                        funding_scan_id=route.get("funding_scan_id"),
                        funding_route_id=route.get("funding_route_id"),
                        route_key=route.get("route_key"),
                        notify=True,
                        severity="warning",
                    )
                    continue
                active_route = rechecked
                now = datetime.now(UTC)
                decision = route_entry_decision(active_route, accounts, now, self.config)
                if not decision["eligible"]:
                    self.record_event(
                        "open_skipped",
                        skipped_open_message(active_route, decision),
                        {"route": route_summary(active_route), "decision": decision},
                        funding_scan_id=active_route.get("funding_scan_id"),
                        funding_route_id=active_route.get("funding_route_id"),
                        route_key=active_route.get("route_key"),
                        notify=True,
                        severity="warning",
                    )
                    continue
            entry_key = route_entry_key(active_route)
            if self.store.funding_paper_position_by_entry_key(entry_key):
                continue
            position = build_position_from_route(active_route, decision, self.config)
            position_id = self.store.open_funding_paper_position(position)
            opened.append(position_id)
            if route_key:
                open_route_keys.add(route_key)
            self.record_event(
                "open",
                open_message(active_route, decision, position),
                {
                    "route": route_summary(active_route),
                    "decision": decision,
                    "position": position,
                },
                funding_paper_position_id=position_id,
                funding_scan_id=active_route.get("funding_scan_id"),
                funding_route_id=active_route.get("funding_route_id"),
                route_key=active_route.get("route_key"),
                notify=True,
            )
            accounts = {
                row["venue"]: row for row in self.store.funding_paper_account_rows()
            }
        return opened

    def process_open_positions(self) -> list[str]:
        outcomes: list[str] = []
        now = datetime.now(UTC)
        for position in self.store.funding_paper_open_positions():
            self.refresh_open_position_route(position)
            now = datetime.now(UTC)
            position_id = int(position["funding_paper_position_id"])
            if self.config.spread_monitoring_enabled:
                route_key = str(position.get("route_key") or "")
                live_route = self.hot_routes.get(route_key) or (
                    self.store.latest_funding_route_by_key(route_key)
                    if route_key
                    else None
                )
                spread_snap = compute_spread_snapshot(position, live_route)
                triggered, reason = spread_stop_loss_triggered(
                    spread_snap, self.config
                )
                if triggered:
                    close_payload = build_close_payload(
                        position,
                        settlement_rates_for_position(position, self.store),
                        live_route,
                        use_entry_estimate_for_missing=True,
                        close_reason=f"spread_stop_loss:{reason}",
                        hold_decision={
                            "hold": False,
                            "close_reason": "spread_stop_loss",
                            "reasons": [reason],
                        },
                    )
                    close_payload["spread_snapshot"] = spread_snap
                    self.store.close_funding_paper_position(
                        position_id, close_payload
                    )
                    self.record_event(
                        "close",
                        (
                            f"SPREAD STOP-LOSS {position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            f"Reason: {reason}\n"
                            f"Basis PnL: ${spread_snap.get('unrealized_basis_pnl', 0):.2f}\n"
                            f"Total PnL: ${spread_snap.get('total_unrealized_pnl', 0):.2f}"
                        ),
                        {
                            "position": position_summary(position),
                            "close": close_payload,
                            "spread_snapshot": spread_snap,
                        },
                        funding_paper_position_id=position_id,
                        route_key=position.get("route_key"),
                        notify=True,
                        severity="warning",
                    )
                    outcomes.append("closed")
                    continue
            result = close_decision(position, now, self.store, self.config)
            if result["status"] == "wait":
                continue
            if result["status"] == "settlement_pending":
                self.store.set_funding_paper_position_status(
                    position_id,
                    "settlement_pending",
                )
                if position_id not in self.pending_notified_positions:
                    self.pending_notified_positions.add(position_id)
                    self.record_event(
                        "settlement_pending",
                        pending_message(position, result),
                        {"position": position_summary(position), "decision": result},
                        funding_paper_position_id=position_id,
                        route_key=position.get("route_key"),
                        notify=True,
                    )
                outcomes.append("settlement_pending")
                continue
            if result["status"] == "hold":
                accrued = self.store.accrue_funding_paper_settlement(
                    position_id,
                    result["accrual"],
                )
                self.pending_notified_positions.discard(position_id)
                if accrued:
                    self.record_event(
                        "hold",
                        hold_message(position, result["accrual"]),
                        {
                            "position": position_summary(position),
                            "accrual": result["accrual"],
                        },
                        funding_paper_position_id=position_id,
                        funding_scan_id=result["accrual"].get(
                            "next_funding_scan_id"
                        ),
                        funding_route_id=result["accrual"].get(
                            "next_funding_route_id"
                        ),
                        route_key=position.get("route_key"),
                        notify=True,
                    )
                outcomes.append("held")
                continue
            if result["status"] == "close":
                self.store.close_funding_paper_position(position_id, result["close"])
                self.record_event(
                    "close",
                    close_message(position, result["close"]),
                    {
                        "position": position_summary(position),
                        "close": result["close"],
                    },
                    funding_paper_position_id=position_id,
                    funding_scan_id=result["close"].get("close_funding_scan_id"),
                    funding_route_id=result["close"].get("close_funding_route_id"),
                    route_key=position.get("route_key"),
                    notify=True,
                )
                outcomes.append("closed")
        return outcomes

    def refresh_open_position_route(self, position: dict[str, Any]) -> None:
        if not self.config.focused_recheck_enabled:
            return
        route_key = str(position.get("route_key") or "")
        if not route_key:
            return
        seed = self.hot_routes.get(route_key) or self.store.latest_funding_route_by_key(
            route_key
        )
        if not seed:
            return
        fresh = self.focused_recheck_route(seed)
        if fresh:
            self.hot_routes[route_key] = fresh

    def refresh_and_publish_repriced_pnl(self) -> int:
        repriced_count = self.store.refresh_estimated_funding_paper_positions()
        self.publish_repriced_pnl_events()
        return repriced_count

    def publish_repriced_pnl_events(self) -> int:
        published = 0
        min_created_at = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        for event in self.store.funding_paper_pending_reprice_events(
            min_created_at=min_created_at,
        ):
            event_id = int(event["event_id"])
            if not self.config.telegram_enabled:
                self.store.update_funding_paper_event_telegram_status(
                    event_id,
                    "disabled",
                )
                continue
            result = self.notifier.send(
                reprice_message(event["position"], event.get("payload") or {})
            )
            self.store.update_funding_paper_event_telegram_status(
                event_id,
                result.status,
                result.error,
            )
            if result.status == "sent":
                published += 1
        return published

    def record_event(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        *,
        funding_paper_position_id: int | None = None,
        funding_scan_id: int | None = None,
        funding_route_id: int | None = None,
        route_key: str | None = None,
        notify: bool,
        severity: str = "info",
    ) -> int:
        telegram_status = "disabled" if not self.config.telegram_enabled else "queued"
        event_id = self.store.insert_funding_paper_event(
            {
                "event_type": event_type,
                "severity": severity,
                "funding_paper_position_id": funding_paper_position_id,
                "funding_scan_id": funding_scan_id,
                "funding_route_id": funding_route_id,
                "route_key": route_key,
                "message": message,
                "payload": payload,
                "telegram_status": telegram_status if notify else "not_sent",
            }
        )
        if notify and self.config.telegram_enabled:
            result = self.notifier.send(message)
            self.store.update_funding_paper_event_telegram_status(
                event_id,
                result.status,
                result.error,
            )
        return event_id


def should_record_routine_scan(result: dict[str, Any]) -> bool:
    return any(
        int(result.get(key) or 0) > 0
        for key in (
            "candidate_count",
            "watch_count",
            "opened_count",
            "closed_count",
            "pending_count",
            "hot_route_count",
            "urgent_route_count",
        )
    )


def is_retention_skip_error(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        return "database is locked" in message or "database is busy" in message
    if isinstance(exc, sqlite3.IntegrityError):
        return "foreign key" in message
    return False


def route_entry_decision(
    route: dict[str, Any],
    accounts: dict[str, dict[str, Any]],
    now: datetime,
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    reasons: list[str] = []
    leads: dict[str, float | None] = {"long": None, "short": None}
    if route.get("status") != "paper_candidate":
        reasons.append("route_not_candidate")
    evidence = route.get("evidence") or {}
    live_net = float(evidence.get("current_nowcast_net") or 0.0)
    if not long_leg or not short_leg:
        reasons.append("missing_route_legs")
    else:
        for side, leg in (("long", long_leg), ("short", short_leg)):
            settlement_at = parse_iso(leg.get("next_funding_at"))
            if settlement_at is None:
                reasons.append(f"{side}_settlement_missing")
                continue
            lead = (settlement_at - now).total_seconds()
            leads[side] = lead
            if lead < 0:
                reasons.append(f"{side}_settlement_already_passed")
            elif lead < config.entry_min_lead_seconds:
                reasons.append(f"{side}_settlement_inside_final_deadline")
            elif lead > config.entry_max_lead_seconds:
                reasons.append(f"{side}_settlement_outside_final_entry_window")
        if long_leg and short_leg:
            for leg in (long_leg, short_leg):
                venue = str(leg.get("venue") or "")
                notional = float(leg.get("notional") or route.get("target_notional") or 0.0)
                required = notional * (1.0 + config.collateral_reserve_fraction)
                available = float(
                    (accounts.get(venue) or {}).get("available_balance") or 0.0
                )
                if available < required:
                    reasons.append(f"{venue}_insufficient_paper_balance")
    entry_window_ready = all(
        lead is not None and 0 <= lead <= config.entry_max_lead_seconds
        for lead in leads.values()
    )
    snapshot_age = route_data_age_seconds(route, now)
    if entry_window_ready and (
        snapshot_age is None or snapshot_age > config.max_entry_snapshot_age_seconds
    ):
        reasons.append("entry_snapshot_stale")
    required_live_net = required_live_net_profit(route, config)
    armed = bool(
        long_leg
        and short_leg
        and all(
            lead is not None and 0 <= lead <= config.arm_window_seconds
            for lead in leads.values()
        )
        and live_net >= required_live_net
    )
    if live_net <= 0:
        if "live_net_not_positive" not in reasons:
            reasons.append("live_net_not_positive")
    elif live_net < required_live_net:
        reasons.append("live_net_below_required_profit")
    return {
        "eligible": not reasons,
        "armed": armed,
        "reasons": reasons,
        "lead_seconds": leads,
        "entry_window_seconds": config.entry_window_seconds,
        "entry_min_lead_seconds": config.entry_min_lead_seconds,
        "entry_max_lead_seconds": config.entry_max_lead_seconds,
        "arm_window_seconds": config.arm_window_seconds,
        "live_net": live_net,
        "required_live_net": required_live_net,
        "snapshot_age_seconds": snapshot_age,
        "max_entry_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
    }


def required_live_net_profit(
    route: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> float:
    evidence = route.get("evidence") or {}
    threshold = optional_float(evidence.get("actionable_profit_threshold"))
    return max(float(config.min_live_net_profit), threshold or 0.0)


def route_monitor_decision(
    route: dict[str, Any],
    now: datetime,
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    leads: dict[str, float | None] = {"long": None, "short": None}
    reasons: list[str] = []
    for side in ("long", "short"):
        leg = leg_by_side(legs, side)
        settlement = parse_iso((leg or {}).get("next_funding_at"))
        if settlement is None:
            reasons.append(f"{side}_settlement_missing")
            continue
        lead = (settlement - now).total_seconds()
        leads[side] = lead
        if lead < 0:
            reasons.append(f"{side}_settlement_already_passed")
        elif lead > config.arm_window_seconds:
            reasons.append(f"{side}_settlement_outside_arm_window")
    live_net = float((route.get("evidence") or {}).get("current_nowcast_net") or 0.0)
    required_live_net = required_live_net_profit(route, config)
    if live_net <= 0:
        reasons.append("live_net_not_positive")
    elif live_net < required_live_net:
        reasons.append("live_net_below_required_profit")
    hot = bool(
        not reasons
        and route.get("status") in {"paper_candidate", "watch"}
        and all(
            lead is not None and 0 <= lead <= config.arm_window_seconds
            for lead in leads.values()
        )
    )
    urgent = bool(
        hot
        and all(
            lead is not None and 0 <= lead <= config.entry_window_seconds
            for lead in leads.values()
        )
    )
    return {
        "hot": hot,
        "urgent": urgent,
        "reasons": reasons,
        "lead_seconds": leads,
        "live_net": live_net,
        "required_live_net": required_live_net,
    }


def build_position_from_route(
    route: dict[str, Any],
    decision: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    evidence = route.get("evidence") or {}
    long_notional = float(long_leg.get("notional") or route.get("target_notional") or 0.0)
    short_notional = float(short_leg.get("notional") or route.get("target_notional") or 0.0)
    long_settlement = str(long_leg.get("next_funding_at") or "")
    short_settlement = str(short_leg.get("next_funding_at") or "")
    max_settlement = max(
        parse_iso(long_settlement) or datetime.min.replace(tzinfo=UTC),
        parse_iso(short_settlement) or datetime.min.replace(tzinfo=UTC),
    ).isoformat()
    return {
        "entry_key": route_entry_key(route),
        "route_key": route["route_key"],
        "open_funding_scan_id": route.get("funding_scan_id"),
        "open_funding_route_id": route.get("funding_route_id"),
        "canonical_asset": route["canonical_asset"],
        "long_venue": route["long_venue"],
        "long_symbol": route["long_symbol"],
        "short_venue": route["short_venue"],
        "short_symbol": route["short_symbol"],
        "base_quantity": min(
            float(long_leg.get("base_quantity") or 0.0),
            float(short_leg.get("base_quantity") or 0.0),
        ),
        "target_notional": float(route.get("target_notional") or 0.0),
        "long_notional": long_notional,
        "short_notional": short_notional,
        "long_reserved_margin": long_notional * (1.0 + config.collateral_reserve_fraction),
        "short_reserved_margin": short_notional * (1.0 + config.collateral_reserve_fraction),
        "long_settlement_at": long_settlement,
        "short_settlement_at": short_settlement,
        "max_settlement_at": max_settlement,
        "expected_live_gross": float(evidence.get("current_nowcast_gross") or 0.0),
        "expected_live_net": float(evidence.get("current_nowcast_net") or 0.0),
        "expected_execution_cost": float(evidence.get("execution_cost") or 0.0),
        "entry_legs": legs,
        "entry_evidence": evidence,
        "entry_cross_spread": entry_cross_spread(long_leg, short_leg),
        "entry_basis_bps": float(evidence.get("signed_entry_basis") or 0.0) * 10_000.0,
        "notes": {"decision": decision, "paper_model": "funding_paper_trader_v2"},
    }


def close_decision(
    position: dict[str, Any],
    now: datetime,
    store: SQLiteStore,
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    max_settlement = parse_iso(position.get("max_settlement_at"))
    if max_settlement is None:
        opened = parse_iso(position.get("opened_at"))
        lag = config.max_settlement_publication_lag_seconds
        if opened is not None and (now - opened).total_seconds() > max(60, lag):
            return {
                "status": "close",
                "close": build_close_payload(
                    position, {}, None,
                    use_entry_estimate_for_missing=True,
                    close_reason="settlement_publication_timeout",
                    hold_decision={"hold": False, "close_reason": "settlement_publication_timeout", "reasons": ["max_settlement_at_missing_timeout"]},
                ),
            }
        return {"status": "settlement_pending", "reason": "missing_max_settlement_at"}
    if now < max_settlement + timedelta(seconds=config.settlement_grace_seconds):
        return {"status": "wait", "reason": "settlement_not_reached"}

    settlement = settlement_rates_for_position(position, store)
    missing = [side for side, row in settlement.items() if row is None]
    close_route = store.latest_funding_route_by_key(str(position["route_key"]))
    hold = position_hold_decision(position, close_route, now, config)
    if hold["hold"]:
        accrual = build_settlement_accrual_payload(
            position,
            settlement,
            close_route or {},
            use_entry_estimate_for_missing=bool(missing),
            hold_decision=hold,
        )
        return {"status": "hold", "accrual": accrual}
    close = build_close_payload(
        position,
        settlement,
        close_route,
        use_entry_estimate_for_missing=bool(missing),
        close_reason=str(hold["close_reason"]),
        hold_decision=hold,
    )
    return {"status": "close", "close": close}


def position_hold_decision(
    position: dict[str, Any],
    route: dict[str, Any] | None,
    now: datetime,
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    if not route:
        return {
            "hold": False,
            "close_reason": "arbitrage_window_unverifiable_route_missing",
            "reasons": ["latest_route_missing"],
        }
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long")
    short_leg = leg_by_side(legs, "short")
    reasons: list[str] = []
    route_age = route_data_age_seconds(route, now)
    if route_age is None:
        reasons.append("route_snapshot_age_missing")
    elif route_age > config.max_entry_snapshot_age_seconds:
        reasons.append("route_snapshot_stale")
    if not long_leg or not short_leg:
        reasons.append("missing_route_legs")
    data_quality_flags = {
        "unit_identity_mismatch",
        "basis_divergence",
    }
    route_flags = set(str(flag) for flag in route.get("risk_flags") or [])
    evidence = route.get("evidence") or {}
    route_flags.update(
        str(flag) for flag in evidence.get("blocking_risk_flags") or []
    )
    if route_flags.intersection(data_quality_flags):
        reasons.append("data_quality_issue")
    next_settlements: dict[str, str] = {}
    for side, leg in (("long", long_leg), ("short", short_leg)):
        if not leg:
            continue
        settlement = parse_iso(leg.get("next_funding_at"))
        if settlement is None:
            reasons.append(f"{side}_next_settlement_missing")
            continue
        if settlement <= now:
            reasons.append(f"{side}_next_settlement_not_future")
            continue
        next_settlements[side] = settlement.isoformat()
    live_net = optional_float(evidence.get("current_nowcast_net"))
    if live_net is None:
        reasons.append("live_net_missing")
    elif live_net <= max(0.0, float(config.min_live_net_profit)):
        reasons.append("live_net_not_positive")
    if long_leg and short_leg:
        long_hourly = optional_float(long_leg.get("hourly_funding_rate"))
        short_hourly = optional_float(short_leg.get("hourly_funding_rate"))
        if (
            long_hourly is not None
            and short_hourly is not None
            and short_hourly < long_hourly
        ):
            reasons.append("funding_rate_inverted")
    close_reason = close_reason_from_hold_reasons(reasons)
    return {
        "hold": not reasons,
        "close_reason": close_reason,
        "reasons": reasons,
        "live_net": live_net,
        "route_snapshot_age_seconds": route_age,
        "max_route_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
        "next_settlements": next_settlements,
        "route_status": route.get("status"),
    }


def close_reason_from_hold_reasons(reasons: list[str]) -> str:
    if "live_net_not_positive" in reasons:
        return "arbitrage_window_closed_live_net_non_positive"
    if "data_quality_issue" in reasons:
        return "arbitrage_window_data_quality_issue"
    if (
        "route_snapshot_stale" in reasons
        or "route_snapshot_age_missing" in reasons
    ):
        return "arbitrage_window_unverifiable_route_stale"
    if "latest_route_missing" in reasons:
        return "arbitrage_window_unverifiable_route_missing"
    if any("settlement" in reason for reason in reasons):
        return "arbitrage_window_unverifiable_next_settlement_missing"
    if "live_net_missing" in reasons:
        return "arbitrage_window_unverifiable_live_net_missing"
    if "funding_rate_inverted" in reasons:
        return "arbitrage_window_funding_rate_inverted"
    return "arbitrage_window_unverifiable"


def build_settlement_accrual_payload(
    position: dict[str, Any],
    settlement: dict[str, dict[str, Any] | None],
    continuation_route: dict[str, Any],
    *,
    use_entry_estimate_for_missing: bool,
    hold_decision: dict[str, Any],
) -> dict[str, Any]:
    current_long = current_position_leg(position, "long")
    current_short = current_position_leg(position, "short")
    long_rate = settlement_rate_or_entry(settlement.get("long"), current_long)
    short_rate = settlement_rate_or_entry(settlement.get("short"), current_short)
    long_notional = float(position.get("long_notional") or 0.0)
    short_notional = float(position.get("short_notional") or 0.0)
    long_funding_pnl = funding_leg_pnl("long", long_notional, long_rate)
    short_funding_pnl = funding_leg_pnl("short", short_notional, short_rate)
    next_legs = continuation_route.get("legs") or []
    next_long = leg_by_side(next_legs, "long") or {}
    next_short = leg_by_side(next_legs, "short") or {}
    next_long_settlement = str(next_long.get("next_funding_at") or "")
    next_short_settlement = str(next_short.get("next_funding_at") or "")
    next_max_settlement = max(
        parse_iso(next_long_settlement) or datetime.min.replace(tzinfo=UTC),
        parse_iso(next_short_settlement) or datetime.min.replace(tzinfo=UTC),
    ).isoformat()
    funding_pnl_delta = long_funding_pnl + short_funding_pnl
    evidence = continuation_route.get("evidence") or {}
    settlement_payload_value = {
        "long": settlement_payload(settlement.get("long"), current_long, long_rate),
        "short": settlement_payload(settlement.get("short"), current_short, short_rate),
        "funding_pnl_delta": funding_pnl_delta,
        "history_missing_fallback": use_entry_estimate_for_missing,
        "continued_live_net": evidence.get("current_nowcast_net"),
    }
    return {
        "settlement_key": ":".join(
            [
                str(position.get("funding_paper_position_id") or ""),
                str(position.get("max_settlement_at") or ""),
            ]
        ),
        "long_cash_delta": long_funding_pnl,
        "short_cash_delta": short_funding_pnl,
        "funding_pnl_delta": funding_pnl_delta,
        "history_missing_fallback": use_entry_estimate_for_missing,
        "settlement": settlement_payload_value,
        "hold_decision": hold_decision,
        "next_funding_scan_id": continuation_route.get("funding_scan_id"),
        "next_funding_route_id": continuation_route.get("funding_route_id"),
        "next_long_settlement_at": next_long_settlement,
        "next_short_settlement_at": next_short_settlement,
        "next_max_settlement_at": next_max_settlement,
        "next_entry_legs": next_legs,
        "next_entry_evidence": evidence,
        "next_expected_live_gross": evidence.get("current_nowcast_gross"),
        "next_expected_live_net": evidence.get("current_nowcast_net"),
    }


def settlement_rates_for_position(
    position: dict[str, Any],
    store: SQLiteStore,
) -> dict[str, dict[str, Any] | None]:
    return {
        "long": store.funding_history_rate_near(
            str(position["long_venue"]),
            str(position["long_symbol"]),
            str(position.get("long_settlement_at") or ""),
        ),
        "short": store.funding_history_rate_near(
            str(position["short_venue"]),
            str(position["short_symbol"]),
            str(position.get("short_settlement_at") or ""),
        ),
    }


def build_close_payload(
    position: dict[str, Any],
    settlement: dict[str, dict[str, Any] | None],
    close_route: dict[str, Any] | None,
    *,
    use_entry_estimate_for_missing: bool,
    close_reason: str,
    hold_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry_long = current_position_leg(position, "long")
    entry_short = current_position_leg(position, "short")
    long_rate = settlement_rate_or_entry(settlement.get("long"), entry_long)
    short_rate = settlement_rate_or_entry(settlement.get("short"), entry_short)
    long_notional = float(position.get("long_notional") or 0.0)
    short_notional = float(position.get("short_notional") or 0.0)
    long_funding_pnl = funding_leg_pnl("long", long_notional, long_rate)
    short_funding_pnl = funding_leg_pnl("short", short_notional, short_rate)
    accrued_funding_pnl = float(
        (position.get("notes") or {}).get("accrued_funding_pnl") or 0.0
    )
    current_funding_pnl = long_funding_pnl + short_funding_pnl
    actual_funding_pnl = accrued_funding_pnl + current_funding_pnl
    actual_execution_cost = float(position.get("expected_execution_cost") or 0.0)
    spread_snap = compute_spread_snapshot(position, close_route)
    basis_pnl = float(spread_snap.get("unrealized_basis_pnl") or 0.0)
    actual_net_pnl = actual_funding_pnl + basis_pnl - actual_execution_cost
    long_cash_delta = long_funding_pnl - actual_execution_cost / 2.0
    short_cash_delta = short_funding_pnl - actual_execution_cost / 2.0
    close_evidence = (close_route or {}).get("evidence") or {}
    return {
        "close_funding_scan_id": (close_route or {}).get("funding_scan_id"),
        "close_funding_route_id": (close_route or {}).get("funding_route_id"),
        "actual_funding_pnl": actual_funding_pnl,
        "actual_basis_pnl": basis_pnl,
        "actual_execution_cost": actual_execution_cost,
        "actual_net_pnl": actual_net_pnl,
        "long_cash_delta": long_cash_delta,
        "short_cash_delta": short_cash_delta,
        "close_reason": close_reason,
        "hold_decision": hold_decision or {},
        "close_legs": (close_route or {}).get("legs") or [],
        "close_evidence": close_evidence,
        "spread_snapshot": spread_snap,
        "settlement": {
            "long": settlement_payload(settlement.get("long"), entry_long, long_rate),
            "short": settlement_payload(settlement.get("short"), entry_short, short_rate),
            "current_funding_pnl": current_funding_pnl,
            "accrued_funding_pnl": accrued_funding_pnl,
            "entry_expected_live_net": position.get("expected_live_net"),
            "entry_expected_live_gross": position.get("expected_live_gross"),
            "history_missing_fallback": use_entry_estimate_for_missing,
        },
        "notes": {
            "paper_model": "funding_paper_trader_v2",
            "execution_cost_source": "entry_route_expected_execution_cost",
            "basis_pnl_included": spread_snap.get("spread_tracking", False),
        },
    }


def current_position_leg(position: dict[str, Any], side: str) -> dict[str, Any]:
    leg = dict(leg_by_side(position.get("entry_legs") or [], side) or {})
    leg.setdefault("side", side)
    leg.setdefault("venue", position.get(f"{side}_venue"))
    leg.setdefault("symbol", position.get(f"{side}_symbol"))
    leg.setdefault("notional", position.get(f"{side}_notional"))
    leg.setdefault("base_quantity", position.get("base_quantity"))
    leg["next_funding_at"] = position.get(f"{side}_settlement_at") or leg.get(
        "next_funding_at"
    )
    return leg


def funding_leg_pnl(side: str, notional: float, funding_rate: float) -> float:
    if str(side).lower() == "long":
        return -float(notional) * float(funding_rate)
    return float(notional) * float(funding_rate)


def settlement_rate_or_entry(
    settlement_row: dict[str, Any] | None,
    entry_leg: dict[str, Any],
) -> float:
    if settlement_row is not None:
        return float(settlement_row.get("funding_rate") or 0.0)
    hourly = float(entry_leg.get("funding_rate") or 0.0)
    interval = max(1.0, float(entry_leg.get("funding_interval_hours") or 1.0))
    return hourly * interval


def settlement_payload(
    settlement_row: dict[str, Any] | None,
    entry_leg: dict[str, Any],
    funding_rate: float,
) -> dict[str, Any]:
    return {
        "venue": (settlement_row or {}).get("venue") or entry_leg.get("venue"),
        "symbol": (settlement_row or {}).get("symbol") or entry_leg.get("symbol"),
        "settlement_at": (settlement_row or {}).get("funding_at")
        or entry_leg.get("next_funding_at"),
        "funding_rate": funding_rate,
        "source": "history" if settlement_row is not None else "entry_estimate_fallback",
        "history_row": settlement_row,
    }


def leg_by_side(legs: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    return next((leg for leg in legs if str(leg.get("side")) == side), None)


def route_entry_key(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    return ":".join(
        [
            str(route.get("route_key") or ""),
            str(long_leg.get("next_funding_at") or ""),
            str(short_leg.get("next_funding_at") or ""),
        ]
    )


def parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def route_settlement_leads(
    route: dict[str, Any],
    now: datetime,
) -> dict[str, float | None]:
    leads: dict[str, float | None] = {"long": None, "short": None}
    for side in ("long", "short"):
        leg = leg_by_side(route.get("legs") or [], side)
        settlement = parse_iso((leg or {}).get("next_funding_at"))
        if settlement is not None:
            leads[side] = (settlement - now).total_seconds()
    return leads


def route_data_age_seconds(route: dict[str, Any], now: datetime) -> float | None:
    evidence = route.get("evidence") or {}
    focused = evidence.get("focused_recheck") or {}
    observed = parse_iso(focused.get("observed_at")) or parse_iso(route.get("observed_at"))
    if observed is None:
        return None
    return max(0.0, (now - observed).total_seconds())


# ---------------------------------------------------------------------------
# Spread / basis risk tracking
# ---------------------------------------------------------------------------


def entry_cross_spread(
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
) -> float | None:
    long_price = leg_vwap(long_leg)
    short_price = leg_vwap(short_leg)
    if long_price is None or short_price is None:
        return None
    return long_price - short_price


def leg_vwap(leg: dict[str, Any]) -> float | None:
    evidence = leg.get("evidence") or leg
    for key in ("vwap", "mark_price", "mid_price", "price"):
        value = optional_float(evidence.get(key))
        if value is not None and value > 0:
            return value
    return None


def compute_spread_snapshot(
    position: dict[str, Any],
    route: dict[str, Any] | None,
) -> dict[str, Any]:
    entry_spread = optional_float(position.get("entry_cross_spread"))
    if route is None or entry_spread is None:
        return {
            "spread_tracking": False,
            "reason": "missing_route_or_entry_spread",
        }
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    current_long = leg_vwap(long_leg)
    current_short = leg_vwap(short_leg)
    if current_long is None or current_short is None:
        return {
            "spread_tracking": False,
            "reason": "missing_current_prices",
        }
    current_spread = current_long - current_short
    quantity = float(position.get("base_quantity") or 0.0)
    unrealized_basis_pnl = (current_spread - entry_spread) * quantity
    notional = float(position.get("target_notional") or 0.0)
    reference = (current_long + current_short) / 2.0
    current_basis_bps = (
        (current_short - current_long) / reference * 10_000.0
        if reference > 0
        else 0.0
    )
    entry_basis_bps = float(position.get("entry_basis_bps") or 0.0)
    accrued_funding = float(
        (position.get("notes") or {}).get("accrued_funding_pnl") or 0.0
    )
    expected_exec_cost = float(position.get("expected_execution_cost") or 0.0)
    total_unrealized = accrued_funding + unrealized_basis_pnl - expected_exec_cost
    return {
        "spread_tracking": True,
        "entry_cross_spread": entry_spread,
        "current_cross_spread": current_spread,
        "current_long_price": current_long,
        "current_short_price": current_short,
        "unrealized_basis_pnl": unrealized_basis_pnl,
        "current_basis_bps": current_basis_bps,
        "entry_basis_bps": entry_basis_bps,
        "accrued_funding_pnl": accrued_funding,
        "total_unrealized_pnl": total_unrealized,
        "notional": notional,
    }


def spread_stop_loss_triggered(
    snapshot: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> tuple[bool, str]:
    if not snapshot.get("spread_tracking"):
        return False, ""
    notional = float(snapshot.get("notional") or 0.0)
    if notional <= 0:
        return False, ""
    unrealized = float(snapshot.get("unrealized_basis_pnl") or 0.0)
    basis_loss_bps = abs(min(0.0, unrealized)) / notional * 10_000.0
    if basis_loss_bps >= config.basis_stop_loss_bps:
        return True, (
            f"basis_stop_loss: unrealized_basis_pnl={unrealized:.2f} "
            f"({basis_loss_bps:.0f} bps >= {config.basis_stop_loss_bps:.0f} bps)"
        )
    return False, ""


def final_recheck_freeze_window_active(
    route: dict[str, Any],
    now: datetime,
    config: FundingPaperTraderConfig,
) -> bool:
    if config.final_recheck_freeze_seconds <= 0:
        return False
    leads = route_settlement_leads(route, now)
    return all(
        lead is not None and 0 <= lead < config.final_recheck_freeze_seconds
        for lead in leads.values()
    )


def final_recheck_fallback_route(
    route: dict[str, Any],
    now: datetime,
    config: FundingPaperTraderConfig,
    reason: str,
) -> dict[str, Any] | None:
    if not final_recheck_freeze_window_active(route, now, config):
        return None
    leads = route_settlement_leads(route, now)
    age = route_data_age_seconds(route, now)
    if age is None or age > config.max_entry_snapshot_age_seconds:
        return None
    fallback = dict(route)
    evidence = dict(fallback.get("evidence") or {})
    previous_recheck = dict(evidence.get("focused_recheck") or {})
    evidence["focused_recheck"] = {
        **previous_recheck,
        "mode": "final_freeze_last_success_v1",
        "reason": reason,
        "snapshot_age_seconds": age,
        "max_snapshot_age_seconds": config.max_entry_snapshot_age_seconds,
        "freeze_seconds": config.final_recheck_freeze_seconds,
        "lead_seconds": leads,
        "used_at": now.isoformat(),
    }
    evidence["entry_snapshot_source"] = "last_successful_focused_recheck"
    evidence["entry_snapshot_frozen"] = True
    fallback["evidence"] = evidence
    return fallback


def venues_from_routes(routes: list[dict[str, Any]]) -> list[str]:
    venues: set[str] = set()
    for route in routes:
        for key in ("long_venue", "short_venue"):
            value = route.get(key)
            if value:
                venues.add(str(value))
    return sorted(venues)


def venues_from_funding_payload(funding: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(row.get("venue"))
            for row in funding.get("venues") or []
            if row.get("venue")
        }
    )


def funding_clients_for_route(
    route: dict[str, Any],
    *,
    fast: bool = False,
) -> list[FundingVenueClient]:
    venues = {
        str(route.get("long_venue") or ""),
        str(route.get("short_venue") or ""),
    }
    clients = []
    for venue in sorted(venue for venue in venues if venue):
        client = funding_client_for_venue(venue, fast=fast)
        if client is not None:
            clients.append(client)
    return clients


def funding_client_for_venue(
    venue: str,
    *,
    fast: bool = False,
) -> FundingVenueClient | None:
    if venue.lower() in DEACTIVATED_FUNDING_VENUES:
        return None
    factories: dict[str, type[FundingVenueClient]] = {
        "aevo": AevoFundingClient,
        "apex": ApexFundingClient,
        "aster": AsterFundingClient,
        "backpack": BackpackFundingClient,
        "binance": BinanceFundingClient,
        "bingx": BingXFundingClient,
        "bitmart": BitMartFundingClient,
        "bitget": BitgetFundingClient,
        "bybit": BybitFundingClient,
        "coinex": CoinExFundingClient,
        "deribit": DeribitFundingClient,
        "drift": DriftFundingClient,
        "dydx": DydxFundingClient,
        "edgex": EdgexFundingClient,
        "ethereal": EtherealFundingClient,
        "extended": ExtendedFundingClient,
        "gate": GateFundingClient,
        "grvt": GrvtFundingClient,
        "htx": HTXFundingClient,
        "hyperliquid": HyperliquidFundingClient,
        "kraken": KrakenFundingClient,
        "kucoin": KuCoinFundingClient,
        "lighter": LighterFundingClient,
        "mexc": MEXCFundingClient,
        "okx": OKXFundingClient,
        "pacifica": PacificaFundingClient,
        "paradex": ParadexFundingClient,
        "phemex": PhemexFundingClient,
        "reya": ReyaFundingClient,
        "risex": RiseXFundingClient,
        "variational": VariationalFundingClient,
        "vertex_base": VertexFundingClient,
        "woox": WOOXFundingClient,
    }
    factory = factories.get(str(venue))
    if factory is None:
        return None
    if not fast:
        return factory()
    try:
        return factory(
            http=FundingHttpClient(
                timeout_seconds=3,
                max_retries=0,
                min_delay_seconds=0.0,
            )
        )
    except TypeError:
        return factory()


def count_hot_routes(
    routes: list[dict[str, Any]],
    config: FundingPaperTraderConfig,
) -> int:
    now = datetime.now(UTC)
    return sum(
        1 for route in routes if route_monitor_decision(route, now, config)["hot"]
    )


def count_urgent_routes(
    routes: list[dict[str, Any]],
    config: FundingPaperTraderConfig,
) -> int:
    now = datetime.now(UTC)
    return sum(
        1 for route in routes if route_monitor_decision(route, now, config)["urgent"]
    )


def route_summary(route: dict[str, Any]) -> dict[str, Any]:
    evidence = route.get("evidence") or {}
    return {
        "funding_scan_id": route.get("funding_scan_id"),
        "funding_route_id": route.get("funding_route_id"),
        "route_key": route.get("route_key"),
        "canonical_asset": route.get("canonical_asset"),
        "long": f"{route.get('long_venue')} {route.get('long_symbol')}",
        "short": f"{route.get('short_venue')} {route.get('short_symbol')}",
        "target_notional": route.get("target_notional"),
        "market_capacity": route.get("market_capacity"),
        "current_nowcast_net": evidence.get("current_nowcast_net"),
        "current_nowcast_gross": evidence.get("current_nowcast_gross"),
        "actionable_profit_threshold": evidence.get("actionable_profit_threshold"),
        "execution_cost": evidence.get("execution_cost"),
        "risk_flags": route.get("risk_flags") or [],
    }


def ranked_status_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(routes, key=status_route_sort_key, reverse=True)


def status_route_sort_key(route: dict[str, Any]) -> tuple[float, float]:
    evidence = route.get("evidence") or {}
    live_net = optional_float(evidence.get("current_nowcast_net")) or 0.0
    threshold = optional_float(evidence.get("actionable_profit_threshold")) or 0.0
    return live_net, live_net - threshold


def status_publishable_candidate(
    route: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> bool:
    if route.get("status") != "paper_candidate":
        return False
    evidence = route.get("evidence") or {}
    live_net = optional_float(evidence.get("current_nowcast_net"))
    if live_net is None or live_net <= 0:
        return False
    return live_net >= required_live_net_profit(route, config)


def status_report_message(
    result: dict[str, Any],
    routes: list[dict[str, Any]],
    watch_routes: list[dict[str, Any]],
    summary: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> str:
    candidates = ranked_status_routes(
        [
            route
            for route in routes
            if status_publishable_candidate(route, config)
        ]
    )
    watched = ranked_status_routes(watch_routes)
    max_routes = int(config.status_report_max_routes)
    universe_count = int(result.get("universe_route_count") or 0)
    route_count = int(result.get("route_count") or 0)
    full_depth_count = int(result.get("execution_shortlist_count") or route_count)
    lines = [
        "<b>Funding Paper Trader STATUS</b>",
        "",
        (
            f"<b>Mode:</b> <code>{tg(result.get('mode'))}</code> | "
            f"<b>Scan:</b> <code>{tg(result.get('funding_scan_id') or '-')}</code>"
        ),
        (
            f"<b>Candidates: {len(candidates)}</b> | "
            f"Watch/internal: {len(watch_routes)} | Monitor: {result.get('hot_route_count') or 0} | "
            f"Urgent: {result.get('urgent_route_count') or 0}"
        ),
        (
            f"<b>Open:</b> {int(summary.get('open_position_count') or 0)} | "
            f"Closed: {int(summary.get('closed_trade_count') or 0)} | "
            f"Realized PnL: <b>${float(summary.get('realized_pnl') or 0):.2f}</b>"
        ),
    ]
    if universe_count or full_depth_count:
        lines.extend(
            [
                "",
                (
                    f"<b>Market scan</b>\n"
                    f"Universe screened: <b>{universe_count:,}</b>\n"
                    f"Full-depth modeled: <b>{full_depth_count:,}</b>"
                ),
            ]
        )
    if candidates:
        lines.extend(["", "<b>Candidates</b>"])
        lines.extend(
            status_route_line(route, index)
            for index, route in enumerate(candidates[:max_routes], start=1)
        )
        hidden = len(candidates) - max_routes
        if hidden > 0:
            lines.append(f"<i>...and {hidden} more</i>")
    else:
        lines.extend(["", "<b>Candidates:</b> нет"])
        if watched:
            lines.append(
                "<i>Watch routes are hidden from status: they are monitored "
                "internally but are not trade candidates.</i>"
            )
    return "\n".join(lines)


def status_route_line(route: dict[str, Any], index: int) -> str:
    evidence = route.get("evidence") or {}
    legs = route.get("legs") or []
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    now = datetime.now(UTC)
    long_lead = lead_seconds(long_leg.get("next_funding_at"), now)
    short_lead = lead_seconds(short_leg.get("next_funding_at"), now)
    live_net = float(evidence.get("current_nowcast_net") or 0.0)
    threshold = float(evidence.get("actionable_profit_threshold") or 0.0)
    long_interval = format_interval_hours(long_leg.get("funding_interval_hours"))
    short_interval = format_interval_hours(short_leg.get("funding_interval_hours"))
    long_hourly = optional_float(long_leg.get("hourly_funding_rate"))
    short_hourly = optional_float(short_leg.get("hourly_funding_rate"))
    long_hourly_label = f"{long_hourly * 100:.4f}%/h" if long_hourly is not None else ""
    short_hourly_label = f"{short_hourly * 100:.4f}%/h" if short_hourly is not None else ""
    long_interval_rate = optional_float(long_leg.get("funding_rate"))
    short_interval_rate = optional_float(short_leg.get("funding_rate"))
    long_iv = max(1.0, float(long_leg.get("funding_interval_hours") or 1.0))
    short_iv = max(1.0, float(short_leg.get("funding_interval_hours") or 1.0))
    long_rate_display = format_rate(long_interval_rate * long_iv if long_interval_rate is not None else None)
    short_rate_display = format_rate(short_interval_rate * short_iv if short_interval_rate is not None else None)
    return (
        f"\n<b>{index}. {tg(route.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(route.get('long_venue'))} {tg(route.get('long_symbol'))}</code> "
        f"{long_rate_display}/{long_interval}"
        f"{f' ({long_hourly_label})' if long_hourly_label else ''}\n"
        f"SHORT <code>{tg(route.get('short_venue'))} {tg(route.get('short_symbol'))}</code> "
        f"{short_rate_display}/{short_interval}"
        f"{f' ({short_hourly_label})' if short_hourly_label else ''}\n"
        f"Next net PnL after costs: <b>{format_signed_money(live_net)}</b> | "
        f"Min: ${threshold:.2f}\n"
        f"Settlement: L {format_seconds(long_lead)} | S {format_seconds(short_lead)}"
    )


def lead_seconds(value: Any, now: datetime) -> float | None:
    settlement = parse_iso(value)
    if settlement is None:
        return None
    return (settlement - now).total_seconds()


def position_summary(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "funding_paper_position_id": position.get("funding_paper_position_id"),
        "entry_key": position.get("entry_key"),
        "route_key": position.get("route_key"),
        "canonical_asset": position.get("canonical_asset"),
        "long": f"{position.get('long_venue')} {position.get('long_symbol')}",
        "short": f"{position.get('short_venue')} {position.get('short_symbol')}",
        "expected_live_net": position.get("expected_live_net"),
        "max_settlement_at": position.get("max_settlement_at"),
    }


def funding_rate_lines(route: dict[str, Any]) -> str:
    legs = route.get("legs") or []
    lines: list[str] = []
    for side in ("long", "short"):
        leg = leg_by_side(legs, side) or {}
        venue = leg.get("venue") or route.get(f"{side}_venue") or ""
        rate = optional_float(leg.get("funding_rate"))
        interval = optional_float(leg.get("funding_interval_hours"))
        hourly = optional_float(leg.get("hourly_funding_rate"))
        if rate is None:
            continue
        interval_label = format_interval_hours(interval) if interval else "?"
        interval_rate = rate * max(1.0, interval or 1.0)
        hourly_label = f"{hourly * 100:.4f}%/h" if hourly is not None else "?"
        lines.append(
            f"{side.upper()} <code>{tg(venue)}</code>: "
            f"{format_rate(interval_rate)}/{interval_label} ({hourly_label})"
        )
    return "\n".join(lines)


def armed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    leads = decision.get("lead_seconds") or {}
    rates = funding_rate_lines(route)
    return (
        "<b>Funding Paper Trader ARMED</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"{rates}\n"
        f"Live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}"
    )


def open_message(
    route: dict[str, Any],
    decision: dict[str, Any],
    position: dict[str, Any],
) -> str:
    leads = decision.get("lead_seconds") or {}
    rates = funding_rate_lines(route)
    return (
        "<b>Funding Paper Trader OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"{rates}\n"
        f"Size: <b>${float(position.get('target_notional') or 0):.0f}</b> per leg\n"
        f"Expected live net: <b>${float(position.get('expected_live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}"
    )


def skipped_open_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "<b>Funding Paper Trader SKIP OPEN</b>\n\n"
        f"<b>{tg(route['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(route['long_venue'])}</code> / "
        f"SHORT <code>{tg(route['short_venue'])}</code>\n\n"
        f"Fresh live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"Reasons: {tg(', '.join(decision.get('reasons') or []) or 'recheck failed')}"
    )


def disarmed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "<b>Funding Paper Trader DISARMED</b>\n\n"
        f"<b>{tg(route.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(route.get('long_venue'))}</code> / "
        f"SHORT <code>{tg(route.get('short_venue'))}</code>\n\n"
        f"Fresh live net: <b>${float(decision.get('live_net') or 0):.2f}</b>\n"
        f"Need: ${float(decision.get('required_live_net') or 0):.2f}\n"
        f"Reasons: {tg(', '.join(decision.get('reasons') or []) or 'not hot')}"
    )


def pending_message(position: dict[str, Any], result: dict[str, Any]) -> str:
    return (
        "<b>Funding Paper Trader SETTLEMENT PENDING</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        f"Причина: {tg(result.get('reason'))}\n"
        "Жду публикацию funding history."
    )


def hold_message(position: dict[str, Any], accrual: dict[str, Any]) -> str:
    settlement = accrual.get("settlement") or {}
    quality = (
        "Начисление предварительное: одна или обе funding history еще не "
        "опубликованы, использована ставка на входе."
        if accrual.get("history_missing_fallback")
        else "Начисление финальное: использована опубликованная funding history."
    )
    hold_decision = accrual.get("hold_decision") or {}
    return (
        "<b>Funding Paper Trader HOLD</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        "Funding settlement начислен, позиция остается открытой: "
        "арбитражное окно все еще положительное.\n"
        f"Settlement PnL: <b>{format_money(accrual.get('funding_pnl_delta'))}</b>\n"
        f"Current live net: {format_money(hold_decision.get('live_net'))}\n"
        f"Next settlement: {tg(accrual.get('next_max_settlement_at'))}\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_message(position: dict[str, Any], close: dict[str, Any]) -> str:
    settlement = close.get("settlement") or {}
    quality = (
        "Качество PnL: предварительный расчет. Одна или обе биржи еще не "
        "опубликовали funding history, поэтому пока использованы ставки на входе. "
        "После публикации бот пересчитает PnL."
        if settlement.get("history_missing_fallback")
        else "Качество PnL: финальный расчет по опубликованной funding history."
    )
    window = close_window_message(close)
    details = close_decision_details_message(position, close)
    return (
        "<b>Funding Paper Trader CLOSE</b>\n\n"
        f"<b>{tg(position['canonical_asset'])}</b>\n"
        f"LONG <code>{tg(position['long_venue'])}</code> / "
        f"SHORT <code>{tg(position['short_venue'])}</code>\n\n"
        f"Причина закрытия: {tg(close_reason_message(close.get('close_reason')))}\n"
        f"{tg(window)}\n"
        f"{details}\n\n"
        f"Funding PnL: <b>${float(close.get('actual_funding_pnl') or 0):.2f}</b>\n"
        f"Basis PnL: <b>${float(close.get('actual_basis_pnl') or 0):.2f}</b>\n"
        f"Execution cost: ${float(close.get('actual_execution_cost') or 0):.2f}\n"
        f"Net PnL: <b>${float(close.get('actual_net_pnl') or 0):.2f}</b>\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_decision_details_message(
    position: dict[str, Any],
    close: dict[str, Any],
) -> str:
    entry_legs = position.get("entry_legs") or []
    close_legs = close.get("close_legs") or []
    hold_decision = close.get("hold_decision") or {}
    close_evidence = close.get("close_evidence") or {}
    live_net = optional_float(hold_decision.get("live_net"))
    if live_net is None:
        live_net = optional_float(close_evidence.get("current_nowcast_net"))
    required = optional_float(close_evidence.get("actionable_profit_threshold"))
    route_age = optional_float(hold_decision.get("route_snapshot_age_seconds"))
    reasons = hold_reason_labels(hold_decision.get("reasons") or [])
    lines = ["", "<b>Детали решения</b>"]
    if live_net is not None:
        need_text = (
            f"; нужно >= {format_money(required)}"
            if required is not None
            else ""
        )
        lines.append(f"Fresh live net: <b>{format_signed_money(live_net)}</b>{need_text}.")
    if reasons:
        lines.append(f"Триггеры: {tg('; '.join(reasons))}.")
    if route_age is not None:
        lines.append(f"Возраст fresh snapshot: {route_age:.0f}s.")
    if entry_legs:
        lines.append(
            "На входе: "
            + " | ".join(
                funding_leg_compact_line(leg)
                for leg in sorted(entry_legs, key=leg_side_sort)
            )
            + "."
        )
    if close_legs:
        lines.append(
            "После settlement: "
            + " | ".join(
                funding_leg_compact_line(leg)
                for leg in sorted(close_legs, key=leg_side_sort)
            )
            + "."
        )
        mismatch = funding_settlement_mismatch_line(close_legs)
        if mismatch:
            lines.append(mismatch)
    return "\n".join(lines)


def leg_side_sort(leg: dict[str, Any]) -> int:
    return 0 if str(leg.get("side") or "").lower() == "long" else 1


def funding_leg_compact_line(leg: dict[str, Any]) -> str:
    side = str(leg.get("side") or "").upper()
    venue = leg.get("venue")
    symbol = leg.get("symbol")
    interval = format_interval_hours(leg.get("funding_interval_hours"))
    hourly = optional_float(leg.get("funding_rate"))
    iv = max(1.0, float(leg.get("funding_interval_hours") or 1.0))
    interval_rate = hourly * iv if hourly is not None else None
    return (
        f"{tg(side)} <code>{tg(venue)} {tg(symbol)}</code> "
        f"{format_rate(interval_rate)}/{interval}, "
        f"next {tg(format_datetime_utc(leg.get('next_funding_at')))}"
    )


def funding_settlement_mismatch_line(legs: list[dict[str, Any]]) -> str:
    long_leg = leg_by_side(legs, "long") or {}
    short_leg = leg_by_side(legs, "short") or {}
    long_next = parse_iso(long_leg.get("next_funding_at"))
    short_next = parse_iso(short_leg.get("next_funding_at"))
    long_interval = optional_float(long_leg.get("funding_interval_hours"))
    short_interval = optional_float(short_leg.get("funding_interval_hours"))
    notes: list[str] = []
    if long_next and short_next and abs((long_next - short_next).total_seconds()) > 60:
        notes.append(
            "следующие funding settlement не совпадают: "
            f"long {tg(format_datetime_utc(long_next.isoformat()))}, "
            f"short {tg(format_datetime_utc(short_next.isoformat()))}"
        )
    if (
        long_interval is not None
        and short_interval is not None
        and abs(long_interval - short_interval) > 1e-9
    ):
        notes.append(
            "интервалы funding разные: "
            f"long {format_interval_hours(long_interval)}, "
            f"short {format_interval_hours(short_interval)}"
        )
    if not notes:
        return ""
    return (
        "Важно: "
        + "; ".join(notes)
        + ". Следующее удержание уже считается новой проверкой окна, "
        "а не автоматическим продолжением старой сделки."
    )


def hold_reason_labels(reasons: list[Any]) -> list[str]:
    labels = {
        "live_net_not_positive": "fresh live net стал <= 0",
        "live_net_missing": "не удалось проверить fresh live net",
        "data_quality_issue": "свежие данные маршрута несовместимы",
        "latest_route_missing": "нет свежего route snapshot",
        "route_snapshot_stale": "fresh snapshot устарел",
        "route_snapshot_age_missing": "непонятен возраст fresh snapshot",
        "missing_route_legs": "не хватает одной из ног маршрута",
        "long_next_settlement_missing": "не найден следующий settlement long-ноги",
        "short_next_settlement_missing": "не найден следующий settlement short-ноги",
        "long_next_settlement_not_future": "следующий settlement long-ноги уже прошел",
        "short_next_settlement_not_future": "следующий settlement short-ноги уже прошел",
        "funding_rate_inverted": "funding rate инвертировался: short-нога стала дешевле long-ноги",
    }
    return [labels.get(str(reason), str(reason)) for reason in reasons]


def reprice_message(position: dict[str, Any], payload: dict[str, Any]) -> str:
    settlement = position.get("settlement") or {}
    quality = (
        "Качество PnL: частичный пересчет. Одна из бирж все еще не "
        "опубликовала funding history, поэтому оставшаяся нога рассчитана по "
        "ставке на входе."
        if payload.get("history_missing_fallback")
        else "Качество PnL: финальный пересчет по опубликованной funding history."
    )
    return (
        "<b>Funding Paper Trader PnL REPRICED</b>\n\n"
        f"<b>{tg(position.get('canonical_asset'))}</b>\n"
        f"LONG <code>{tg(position.get('long_venue'))}</code> / "
        f"SHORT <code>{tg(position.get('short_venue'))}</code>\n\n"
        "Биржи опубликовали funding history, бот пересчитал результат.\n"
        f"Funding PnL: <b>{format_signed_money(position.get('actual_funding_pnl'))}</b>\n"
        f"Basis PnL: <b>{format_signed_money(position.get('actual_basis_pnl'))}</b>\n"
        f"Execution cost: {format_money(position.get('actual_execution_cost'))}\n"
        f"Net PnL: <b>{format_signed_money(position.get('actual_net_pnl'))}</b>\n"
        f"{tg(quality)}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}"
    )


def close_reason_message(value: Any) -> str:
    return {
        "arbitrage_window_closed_live_net_non_positive": (
            "арбитражное окно закрылось, live net стал неположительным"
        ),
        "arbitrage_window_data_quality_issue": (
            "свежие данные маршрута выглядят несовместимыми"
        ),
        "arbitrage_window_unverifiable_route_missing": (
            "не удалось получить свежий route snapshot"
        ),
        "arbitrage_window_unverifiable_route_stale": (
            "route snapshot устарел, продолжение окна не подтверждено"
        ),
        "arbitrage_window_unverifiable_next_settlement_missing": (
            "не удалось определить следующий funding settlement"
        ),
        "arbitrage_window_unverifiable_live_net_missing": (
            "не удалось проверить live net"
        ),
        "arbitrage_window_funding_rate_inverted": (
            "funding rate инвертировался: арбитражное окно закрылось"
        ),
        "arbitrage_window_unverifiable": (
            "продолжение окна не подтверждено"
        ),
    }.get(str(value or ""), str(value or "продолжение окна не подтверждено"))


def close_window_message(close: dict[str, Any]) -> str:
    evidence = close.get("close_evidence") or {}
    live_net = optional_float(evidence.get("current_nowcast_net"))
    if live_net is None:
        if close.get("close_funding_route_id"):
            return "Состояние окна при закрытии: close-scan был, но live net не рассчитан."
        return "Состояние окна при закрытии: свежий route snapshot не найден."
    if live_net > 0:
        return (
            "Состояние окна при закрытии: окно еще выглядело положительным, "
            f"live net {format_money(live_net)}."
        )
    if live_net < 0:
        return (
            "Состояние окна при закрытии: окно уже не выглядело положительным, "
            f"live net {format_money(live_net)}."
        )
    return "Состояние окна при закрытии: live net около $0.00."


def format_money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def format_signed_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if amount > 0 else ""
    return f"{sign}${amount:.2f}" if amount >= 0 else f"-${abs(amount):.2f}"


def tg(value: Any) -> str:
    if value is None:
        return "-"
    return html_escape(str(value), quote=False)


def format_seconds(value: Any) -> str:
    if value is None:
        return "-"
    seconds = max(0, int(float(value)))
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s"


def format_datetime_utc(value: Any) -> str:
    timestamp = parse_iso(value)
    if timestamp is None:
        return "-"
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_interval_hours(value: Any) -> str:
    hours = optional_float(value)
    if hours is None:
        return "-"
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours))}h"
    return f"{hours:.2f}h"


def format_rate(value: Any) -> str:
    try:
        return f"{float(value) * 100:.4f}%"
    except (TypeError, ValueError):
        return "-"


def serializable_config(config: FundingPaperTraderConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["export_dir"] = str(config.export_dir)
    return payload


def export_funding_paper_csv(
    store: SQLiteStore,
    export_dir: Path,
) -> dict[str, Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    dashboard = filter_deactivated_funding_paper_payload(
        store.funding_paper_dashboard(refresh_estimates=False)
    )
    outputs = {
        "accounts": export_dir / "paper_accounts.csv",
        "open_positions": export_dir / "paper_open_positions.csv",
        "closed_positions": export_dir / "paper_closed_positions.csv",
        "events": export_dir / "paper_events.csv",
        "equity": export_dir / "paper_equity_curve.csv",
    }
    write_csv(outputs["accounts"], dashboard["accounts"])
    write_csv(outputs["open_positions"], flatten_positions(dashboard["open_positions"]))
    write_csv(outputs["closed_positions"], flatten_positions(dashboard["closed_positions"]))
    write_csv(outputs["events"], flatten_events(dashboard["events"]))
    write_csv(outputs["equity"], dashboard["equity"])
    return outputs


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: csv_value(row.get(key))
                    for key in fieldnames
                }
            )


def flatten_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for row in rows:
        flattened.append(
            {
                "position_id": row.get("funding_paper_position_id"),
                "entry_key": row.get("entry_key"),
                "route_key": row.get("route_key"),
                "status": row.get("status"),
                "opened_at": row.get("opened_at"),
                "closed_at": row.get("closed_at"),
                "asset": row.get("canonical_asset"),
                "long_venue": row.get("long_venue"),
                "long_symbol": row.get("long_symbol"),
                "short_venue": row.get("short_venue"),
                "short_symbol": row.get("short_symbol"),
                "target_notional": row.get("target_notional"),
                "expected_live_net": row.get("expected_live_net"),
                "actual_funding_pnl": row.get("actual_funding_pnl"),
                "actual_basis_pnl": row.get("actual_basis_pnl"),
                "actual_execution_cost": row.get("actual_execution_cost"),
                "actual_net_pnl": row.get("actual_net_pnl"),
                "entry_cross_spread": row.get("entry_cross_spread"),
                "entry_basis_bps": row.get("entry_basis_bps"),
                "long_settlement_at": row.get("long_settlement_at"),
                "short_settlement_at": row.get("short_settlement_at"),
                "close_reason": row.get("close_reason"),
            }
        )
    return flattened


def flatten_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": row.get("funding_paper_event_id"),
            "created_at": row.get("created_at"),
            "event_type": row.get("event_type"),
            "severity": row.get("severity"),
            "position_id": row.get("funding_paper_position_id"),
            "route_key": row.get("route_key"),
            "message": row.get("message"),
            "telegram_status": row.get("telegram_status"),
            "telegram_error": row.get("telegram_error"),
        }
        for row in rows
    ]


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return str(value)
    return value
