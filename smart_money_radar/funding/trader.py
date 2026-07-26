from __future__ import annotations

import csv
import signal
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from types import FrameType
from typing import Any

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
    filter_deactivated_funding_dashboard_payload,
    filter_deactivated_funding_paper_payload,
)
from smart_money_radar.funding.retention import apply_funding_retention_plan
from smart_money_radar.funding.service import run_funding_scan
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore, utc_now_iso

from smart_money_radar.paper_bot.helpers import (
    csv_value,
    format_datetime_utc,
    format_interval_hours,
    format_money,
    format_rate,
    format_seconds,
    format_signed_money,
    is_retention_skip_error,
    leg_by_side,
    optional_float,
    parse_iso,
    ranked_status_routes,
    route_data_age_seconds,
    route_entry_key,
    route_settlement_leads,
    should_record_routine_scan,
    status_route_sort_key,
    tg,
)
from smart_money_radar.paper_bot.position import (
    build_close_payload,
    build_position_from_route,
    build_settlement_accrual_payload,
    close_decision,
    close_reason_from_hold_reasons,
    compute_spread_snapshot,
    current_position_leg,
    entry_cross_spread,
    final_recheck_fallback_route,
    final_recheck_freeze_window_active,
    funding_leg_pnl,
    leg_vwap,
    position_hold_decision,
    required_live_net_profit,
    route_entry_decision,
    route_monitor_decision,
    settlement_rate_or_entry,
    settlement_rates_for_position,
    settlement_payload,
    spread_stop_loss_triggered,
    status_publishable_candidate,
)
from smart_money_radar.paper_bot.telegram import (
    armed_message,
    close_decision_details_message,
    close_message,
    close_reason_message,
    close_window_message,
    disarmed_message,
    funding_leg_compact_line,
    funding_rate_lines,
    funding_settlement_mismatch_line,
    hold_message,
    hold_reason_labels,
    lead_seconds,
    open_message,
    pending_message,
    position_summary,
    reprice_message,
    skipped_open_message,
    status_report_message,
    status_route_line,
)

FUNDING_HISTORY_RETENTION_PER_MARKET = 24
FUNDING_PAPER_WATCH_SCAN_RETENTION = 1

@dataclass(frozen=True)
class PaperBotConfig:
    profile_name: str = "default"
    strategy_set: tuple[str, ...] = ("funding_carry",)
    venue_starting_balance: float = 1_000.0
    target_notional_per_leg: float = 500.0
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
    hot_interval_seconds: int = 6
    hot_route_recheck_workers: int = 6
    status_report_interval_seconds: int = 3_600
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

    def validated(self) -> "PaperBotConfig":
        return PaperBotConfig(
            profile_name=str(self.profile_name or "default"),
            strategy_set=tuple(
                str(strategy)
                for strategy in (self.strategy_set or ("funding_carry",))
                if str(strategy).strip()
            ) or ("funding_carry",),
            venue_starting_balance=max(100.0, float(self.venue_starting_balance)),
            target_notional_per_leg=max(50.0, float(self.target_notional_per_leg)),
            entry_min_lead_seconds=max(
                0,
                min(int(self.entry_min_lead_seconds), 300),
            ),
            entry_max_lead_seconds=max(
                1,
                min(int(self.entry_max_lead_seconds), 900),
            ),
            arm_window_seconds=max(
                60,
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

    def normalized_entry_leads(self) -> "PaperBotConfig":
        minimum = max(0, int(self.entry_min_lead_seconds))
        maximum = max(minimum, int(self.entry_max_lead_seconds))
        minimum = min(minimum, maximum)
        return PaperBotConfig(
            **{
                **asdict(self),
                "entry_min_lead_seconds": minimum,
                "entry_max_lead_seconds": maximum,
            }
        )

class PaperBot:
    """Paper trading bot for funding carry arbitrage.

    Lifecycle: run_loop → scan → evaluate → entry → monitor → settlement → exit.
    Supports inheritance for venue-specific bots.
    """

    def __init__(
        self,
        store: SQLiteStore,
        config: PaperBotConfig | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        cfg = (config or PaperBotConfig()).validated()
        self.iterations = cfg.iterations
        self.telegram_enabled = cfg.telegram_enabled
        self.notifier = notifier or TelegramNotifier()
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.shutdown_notified = False
        self.last_iteration_result: dict[str, Any] | None = None
        self.store = store
        self.config = cfg
        self.armed_routes: set[str] = set()
        self.skipped_notified_routes: set[str] = set()
        self.hot_routes: dict[str, dict[str, Any]] = {}
        self.last_full_scan_monotonic = 0.0
        self.last_status_report_monotonic = 0.0
        self.last_retention_monotonic = 0.0
        self.pending_notified_positions: set[int] = set()

    # ------------------------------------------------------------------
    # Lifecycle: signal handling, sleep, notifications (folded from BaseBot)
    # ------------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        self.stop_reason = self.stop_reason or reason

    def install_signal_handlers(self) -> dict[int, Any]:
        handlers: dict[int, Any] = {}
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                handlers[int(stop_signal)] = signal.getsignal(stop_signal)
                signal.signal(stop_signal, self._handle_stop_signal)
            except (ValueError, OSError):
                continue
        return handlers

    def restore_signal_handlers(self, handlers: dict[int, Any]) -> None:
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    def _handle_stop_signal(self, signum: int, frame: FrameType | None) -> None:
        reason = signal.Signals(signum).name
        if self.stop_requested:
            raise KeyboardInterrupt
        self.request_stop(reason)

    def sleep_interruptibly(self, seconds: int) -> None:
        deadline = time.monotonic() + max(0, int(seconds))
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def notify(self, text: str) -> None:
        print(text, flush=True)
        if not self.telegram_enabled:
            return
        result = self.notifier.send(text)
        if result.status != "sent":
            print(f"[telegram {result.status}] {result.error or ''}", flush=True)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        completed = 0
        previous_signal_handlers = self.install_signal_handlers()
        try:
            self.record_event(
                "trader_started",
                "Paper Bot started",
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
                            "Paper Bot CRASHED\n"
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
                        "Paper Bot STOPPED\n"
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
                    "Paper Bot INTERRUPTED\n"
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
        self.skipped_notified_routes.clear()
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
        funding = filter_deactivated_funding_dashboard_payload(funding)
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
                        "Paper Bot retention skipped\n"
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
                            "Paper Bot focused recheck failed\n"
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
                    "Paper Bot skipped final recheck\n"
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
                        "Paper Bot used last successful focused snapshot\n"
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
                    "Paper Bot focused recheck failed\n"
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
                    "Paper Bot focused recheck failed\n"
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
                    skip_key = f"{route_key}:focused_route_missing"
                    if skip_key not in self.skipped_notified_routes:
                        self.skipped_notified_routes.add(skip_key)
                        self.record_event(
                            "open_skipped",
                            (
                                "<b>Paper Bot SKIP OPEN</b>\n\n"
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
                    skip_key = f"{route_key}:not_eligible_after_recheck"
                    if skip_key not in self.skipped_notified_routes:
                        self.skipped_notified_routes.add(skip_key)
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

# ---------------------------------------------------------------------------
# Spread / basis risk tracking
# ---------------------------------------------------------------------------

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
    config: PaperBotConfig,
) -> int:
    now = datetime.now(UTC)
    return sum(
        1 for route in routes if route_monitor_decision(route, now, config)["hot"]
    )

def count_urgent_routes(
    routes: list[dict[str, Any]],
    config: PaperBotConfig,
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

def serializable_config(config: PaperBotConfig) -> dict[str, Any]:
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

# Backward-compatible aliases
FundingPaperTrader = PaperBot
FundingPaperTraderConfig = PaperBotConfig
