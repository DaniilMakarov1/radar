from __future__ import annotations

import csv
import hashlib
import signal
import sqlite3
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from threading import Event
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
    NadoFundingClient,
    OKXFundingClient,
    PacificaFundingClient,
    ParadexFundingClient,
    PhemexFundingClient,
    ReyaFundingClient,
    RiseXFundingClient,
    VertexFundingClient,
    WOOXFundingClient,
)
from smart_money_radar.funding.adapters.base import FundingDataError, FundingHttpClient
from smart_money_radar.funding.economics import evaluate_perp_route, perp_route_key
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import (
    normalize_catalog_canonical_units,
    normalize_orderbook_canonical_units,
    normalize_stored_orderbook_units,
)
from smart_money_radar.funding.presentation import (
    filter_deactivated_funding_dashboard_payload,
    filter_deactivated_funding_paper_payload,
)
from smart_money_radar.funding.retention import apply_funding_retention_plan
from smart_money_radar.funding.service import (
    active_default_funding_clients,
    run_funding_scan,
)
from smart_money_radar.funding.shadow_monitor import (
    adaptive_broad_sweep_interval_seconds,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    build_settlement_capture_opportunity,
    gross_funding_pnl,
    settlement_skew_seconds,
)
from smart_money_radar.funding.venue_capabilities import (
    apply_declared_venue_capability_contract,
    capability_from_market,
    synchronized_route_capability_check,
)
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
from smart_money_radar.paper_bot.clock import SystemClock
from smart_money_radar.paper_bot.position import (
    build_close_payload,
    build_position_from_route,
    build_settlement_accrual_payload,
    close_decision,
    close_reason_from_hold_reasons,
    compute_price_move_snapshot,
    compute_spread_snapshot,
    current_position_leg,
    entry_cross_spread,
    final_recheck_fallback_route,
    final_recheck_freeze_window_active,
    funding_leg_pnl,
    leg_vwap,
    normalize_strategy_set,
    position_hold_decision,
    required_live_net_profit,
    route_entry_decision,
    route_monitor_decision,
    selected_route_strategy,
    settlement_rates_for_position,
    settlement_payload,
    price_stop_loss_triggered,
    spread_stop_loss_triggered,
    status_publishable_candidate,
)
from smart_money_radar.paper_bot.runtime_v2 import (
    SynchronizedFundingRuntimeV2,
    synchronized_runtime_enabled,
)
from smart_money_radar.paper_bot.settlement import StoredFundingSettlementDataProvider
from smart_money_radar.paper_bot.risk import (
    common_price_move_telemetry,
    dynamic_basis_risk_budget_bps,
    dynamic_basis_stop_decision,
    entry_risk_gates,
    focused_recheck_capacity_check,
    hard_risk_triggered,
    lightweight_discovery_due,
    risk_poll_interval_seconds,
    risk_warnings,
    stale_data_decision,
)
from smart_money_radar.paper_bot.cycle_manager import (
    next_cycle_observation_decision,
    post_settlement_probe_decision,
    settlement_crossing_decision,
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

FUNDING_HISTORY_MIN_ROWS_PER_MARKET = 24
FUNDING_PAPER_WATCH_SCAN_RETENTION = 1
OPEN_CAPTURE_REFRESH_TIMEOUT_SECONDS = 2.0
OPEN_CAPTURE_DEGRADED_HARD_STALE_SECONDS = 5.0


@dataclass(frozen=True)
class CaptureRouteRefreshResult:
    quality: str
    route: dict[str, Any] | None
    snapshot_id: str | None
    reason: str | None = None
    attempts: int = 1
    leg_results: dict[str, dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "snapshot_id": self.snapshot_id,
            "reason": self.reason,
            "attempts": self.attempts,
            "leg_results": self.leg_results or {},
        }

@dataclass(frozen=True)
class PaperBotConfig:
    profile_name: str = "default"
    strategy_set: tuple[str, ...] = (
        "synchronized_funding_capture",
    )
    venue_starting_balance: float = 1_000.0
    target_notional_per_leg: float = 500.0
    leverage: float = 1.0
    margin_mode: str = "isolated"
    auto_add_margin: bool = False
    max_open_positions_total: int = 1
    max_open_positions_per_venue: int = 1
    max_gross_exposure_usd: float = 1_000.0
    entry_target_lead_seconds: float = 30.0
    entry_min_lead_seconds: float = 25.0
    entry_max_lead_seconds: float = 35.0
    entry_fill_deadline_lead_seconds: float = 20.0
    settlement_alignment_tolerance_seconds: float = 1.0
    arm_window_seconds: int = 120
    final_recheck_freeze_seconds: int = 0
    max_entry_snapshot_age_seconds: float = 2.0
    max_cross_venue_snapshot_skew_seconds: float = 1.0
    settlement_grace_seconds: int = 30
    max_settlement_publication_lag_seconds: int = 300
    no_normal_exit_before_settlement_plus_seconds: int = 20
    post_settlement_schedule_probe_seconds: int = 5
    post_settlement_hold_decision_seconds: int = 30
    hold_enabled: bool = True
    entry_history_required: bool = False
    entry_history_mode: str = "disabled"
    hold_history_window_days: int = 30
    hold_history_max_cycles: int = 20
    hold_history_min_cycles_for_gate: int = 8
    hold_history_insufficient_multiplier: float = 0.75
    hold_history_min_positive_realization_rate: float = 0.70
    hold_history_min_p25_realization_ratio: float = 0.50
    hold_history_max_extra_cycles_when_insufficient: int = 1
    max_settlements_per_position: int = 4
    max_position_age_seconds: int = 14_700
    min_next_settlement_wait_seconds: int = 300
    max_next_settlement_wait_seconds: int = 14_400
    collateral_reserve_fraction: float = 0.25
    min_live_net_profit: float = 0.0
    scan_interval_seconds: int = 300
    monitor_interval_seconds: float = 2.0
    hot_interval_seconds: float = 1.0
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
    common_price_move_alert_fraction: float = 0.05
    common_price_move_critical_fraction: float = 0.10
    spread_monitoring_enabled: bool = True

    def validated(self) -> "PaperBotConfig":
        strategies = list(normalize_strategy_set(self.strategy_set))
        if self.spread_arb_enabled:
            for strategy in ("spread_only", "combined", "opportunistic_any"):
                if strategy not in strategies:
                    strategies.append(strategy)
        return PaperBotConfig(
            profile_name=str(self.profile_name or "default"),
            strategy_set=tuple(strategies),
            venue_starting_balance=max(100.0, float(self.venue_starting_balance)),
            target_notional_per_leg=max(50.0, float(self.target_notional_per_leg)),
            leverage=1.0,
            margin_mode="isolated",
            auto_add_margin=False,
            max_open_positions_total=max(1, int(self.max_open_positions_total)),
            max_open_positions_per_venue=max(1, int(self.max_open_positions_per_venue)),
            max_gross_exposure_usd=max(0.0, float(self.max_gross_exposure_usd)),
            entry_target_lead_seconds=max(1.0, float(self.entry_target_lead_seconds)),
            entry_min_lead_seconds=max(
                0.0,
                min(float(self.entry_min_lead_seconds), 300.0),
            ),
            entry_max_lead_seconds=max(
                1.0,
                min(float(self.entry_max_lead_seconds), 900.0),
            ),
            entry_fill_deadline_lead_seconds=max(
                0.0,
                min(float(self.entry_fill_deadline_lead_seconds), 300.0),
            ),
            settlement_alignment_tolerance_seconds=max(
                0.0,
                min(float(self.settlement_alignment_tolerance_seconds), 60.0),
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
                0.1,
                min(float(self.max_entry_snapshot_age_seconds), 300.0),
            ),
            max_cross_venue_snapshot_skew_seconds=max(
                0.1,
                min(float(self.max_cross_venue_snapshot_skew_seconds), 60.0),
            ),
            settlement_grace_seconds=max(
                0,
                min(int(self.settlement_grace_seconds), 1_800),
            ),
            max_settlement_publication_lag_seconds=max(
                60,
                min(int(self.max_settlement_publication_lag_seconds), 7_200),
            ),
            no_normal_exit_before_settlement_plus_seconds=max(
                0,
                min(int(self.no_normal_exit_before_settlement_plus_seconds), 300),
            ),
            post_settlement_schedule_probe_seconds=max(
                0,
                min(int(self.post_settlement_schedule_probe_seconds), 300),
            ),
            post_settlement_hold_decision_seconds=max(
                1,
                min(int(self.post_settlement_hold_decision_seconds), 900),
            ),
            hold_enabled=bool(self.hold_enabled),
            entry_history_required=False,
            entry_history_mode="disabled",
            hold_history_window_days=max(1, min(int(self.hold_history_window_days), 365)),
            hold_history_max_cycles=max(1, min(int(self.hold_history_max_cycles), 1_000)),
            hold_history_min_cycles_for_gate=max(
                1,
                min(int(self.hold_history_min_cycles_for_gate), 1_000),
            ),
            hold_history_insufficient_multiplier=max(
                0.0,
                min(float(self.hold_history_insufficient_multiplier), 1.0),
            ),
            hold_history_min_positive_realization_rate=max(
                0.0,
                min(float(self.hold_history_min_positive_realization_rate), 1.0),
            ),
            hold_history_min_p25_realization_ratio=max(
                0.0,
                min(float(self.hold_history_min_p25_realization_ratio), 10.0),
            ),
            hold_history_max_extra_cycles_when_insufficient=max(
                0,
                min(int(self.hold_history_max_extra_cycles_when_insufficient), 24),
            ),
            max_settlements_per_position=max(
                1,
                min(int(self.max_settlements_per_position), 24),
            ),
            max_position_age_seconds=max(
                60,
                min(int(self.max_position_age_seconds), 86_400),
            ),
            min_next_settlement_wait_seconds=max(
                1,
                min(int(self.min_next_settlement_wait_seconds), 86_400),
            ),
            max_next_settlement_wait_seconds=max(
                1,
                min(int(self.max_next_settlement_wait_seconds), 86_400),
            ),
            collateral_reserve_fraction=max(
                0.0,
                min(float(self.collateral_reserve_fraction), 1.0),
            ),
            min_live_net_profit=max(0.0, float(self.min_live_net_profit)),
            scan_interval_seconds=max(10, int(self.scan_interval_seconds)),
            monitor_interval_seconds=max(1.0, float(self.monitor_interval_seconds)),
            hot_interval_seconds=max(1.0, float(self.hot_interval_seconds)),
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
            common_price_move_alert_fraction=max(
                0.0,
                float(self.common_price_move_alert_fraction),
            ),
            common_price_move_critical_fraction=max(
                0.0,
                float(self.common_price_move_critical_fraction),
            ),
            spread_monitoring_enabled=bool(self.spread_monitoring_enabled),
        ).normalized_entry_leads()

    def normalized_entry_leads(self) -> "PaperBotConfig":
        minimum = max(0.0, float(self.entry_min_lead_seconds))
        maximum = max(minimum, float(self.entry_max_lead_seconds))
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
        clock: Any | None = None,
        settlement_data_provider: Any | None = None,
    ) -> None:
        cfg = (config or PaperBotConfig()).validated()
        self.iterations = cfg.iterations
        self.telegram_enabled = cfg.telegram_enabled
        self.notifier = notifier or TelegramNotifier(
            token_env_var="FUNDING_TELEGRAM_BOT_TOKEN",
            chat_id_env_var="FUNDING_TELEGRAM_CHAT_ID",
        )
        self.clock = clock or SystemClock()
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
        self.last_reconciliation_monotonic = 0.0
        self.pending_notified_positions: set[int] = set()
        self.price_move_alerted_positions: set[tuple[int, str]] = set()
        self.v2_observations_by_route: dict[str, list[dict[str, Any]]] = {}
        resolved_settlement_data_provider = (
            settlement_data_provider or StoredFundingSettlementDataProvider(self.store)
        )
        self.synchronized_runtime = SynchronizedFundingRuntimeV2(
            store=self.store,
            config=self.config,
            clock=self.clock,
            observations_by_route=self.v2_observations_by_route,
            settlement_data_provider=resolved_settlement_data_provider,
        )
        self.focused_executor: ThreadPoolExecutor | None = None
        self.lightweight_executor: ThreadPoolExecutor | None = None
        self.background_full_scan_executor: ThreadPoolExecutor | None = None
        self.background_full_scan_future: Future[dict[str, Any]] | None = None
        self.background_full_scan_started_monotonic = 0.0
        self.background_full_scan_cancel_event: Event | None = None
        self.pending_completed_background_full_scan: dict[str, Any] | None = None
        self.background_full_scan_skip_reasons: dict[str, str] = {}
        self._foreground_venues: set[str] = set()
        self._background_venues: set[str] = set()
        self._last_lightweight_nearest_settlement_seconds: float | None = None
        self._lightweight_pending_futures: dict[Future[Any], str] = {}

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

    def sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
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
            if synchronized_runtime_enabled(self.config) and self.store.funding_paper_open_positions():
                raise RuntimeError("legacy open positions require manual resolution")
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
            self.shutdown_background_full_scan()
            self.shutdown_foreground_executors()
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
        background_results: list[dict[str, Any]] = []
        completed = self.collect_background_full_scan_result()
        if completed is not None:
            background_results.append(completed)

        reconciliation = self._maybe_run_reconciliation()

        if self.has_open_exposure():
            self.request_background_full_scan_cancel("open_exposure_priority")
            result = self.run_open_position_iteration()
            if reconciliation is not None:
                result["reconciliation"] = reconciliation
            if background_results:
                result["background_full_scan_results"] = background_results
            if self.background_full_scan_running():
                result["background_full_scan_running"] = True
            return result

        if self.critical_entry_recheck_active():
            self.request_background_full_scan_cancel("critical_hot_route_priority")
            result = self.run_hot_iteration()
            result["mode"] = "critical_hot_routes"
            if reconciliation is not None:
                result["reconciliation"] = reconciliation
            if background_results:
                result["background_full_scan_results"] = background_results
            if self.background_full_scan_running():
                result["background_full_scan_running"] = True
            return result

        if self.hot_routes:
            result = self.run_hot_iteration()
        else:
            result = self.run_background_wait_iteration(
                background_running=self.background_full_scan_running()
            )
            discovery = self._run_lightweight_discovery()
            if discovery is not None:
                result["lightweight_discovery"] = discovery

        if reconciliation is not None:
            result["reconciliation"] = reconciliation

        completed = self.collect_background_full_scan_result()
        if completed is not None:
            background_results.append(completed)
        if background_results:
            result["background_full_scan_results"] = background_results

        if not self.has_open_exposure() and not self.hot_routes:
            background_started = self.maybe_start_background_full_scan()
            if background_started:
                result["background_full_scan_status"] = "started"

        if self.background_full_scan_running():
            result["background_full_scan_running"] = True
        return result

    def should_run_hot_iteration(self) -> bool:
        if self.store.funding_capture_open_positions():
            return True
        if self.store.funding_paper_open_positions():
            return True
        return bool(self.hot_routes)

    def has_open_exposure(self) -> bool:
        return bool(
            self.store.funding_capture_open_positions()
            or self.store.funding_paper_open_positions()
        )

    def full_scan_due(self) -> bool:
        if self.last_full_scan_monotonic <= 0:
            return True
        elapsed = self.clock.monotonic() - self.last_full_scan_monotonic
        return elapsed >= self.config.scan_interval_seconds

    def background_full_scan_running(self) -> bool:
        future = self.background_full_scan_future
        return future is not None and not future.done()

    def critical_entry_recheck_active(self) -> bool:
        if not self.hot_routes:
            return False
        now = self.clock.now()
        return any(
            any(
                lead is not None and 0 <= lead <= 120.0
                for lead in route_monitor_decision(route, now, self.config)[
                    "lead_seconds"
                ].values()
            )
            for route in self.hot_routes.values()
        )

    def maybe_start_background_full_scan(self) -> bool:
        if not self.full_scan_due() or self.background_full_scan_running():
            return False
        if self.has_open_exposure() or self.hot_routes:
            return False
        if self.background_full_scan_executor is None:
            self.background_full_scan_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="funding-full-scan",
            )
        cancel_event = Event()
        self.background_full_scan_cancel_event = cancel_event
        self.background_full_scan_started_monotonic = self.clock.monotonic()
        self.background_full_scan_future = self.background_full_scan_executor.submit(
            self.run_discovery_full_scan,
            cancel_event,
        )
        return True

    def request_background_full_scan_cancel(self, reason: str) -> None:
        event = self.background_full_scan_cancel_event
        if event is not None:
            event.set()

    def collect_background_full_scan_result(self) -> dict[str, Any] | None:
        if self.pending_completed_background_full_scan is not None:
            if self.has_open_exposure() or self.critical_entry_recheck_active():
                return {
                    "status": "deferred",
                    "reason": "protected_hot_or_open_path",
                    "mode": "background_full_market",
                }
            result = self.pending_completed_background_full_scan
            self.pending_completed_background_full_scan = None
            return self.merge_background_full_scan_result(result)

        future = self.background_full_scan_future
        if future is None or not future.done():
            return None
        self.background_full_scan_future = None
        self.background_full_scan_cancel_event = None
        try:
            result = future.result()
        except Exception as exc:
            self.last_full_scan_monotonic = self.clock.monotonic()
            payload = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.record_event(
                "background_full_scan_failed",
                (
                    "Funding background full scan failed\n"
                    f"Error: {type(exc).__name__}: {exc}"
                ),
                payload,
                notify=False,
                severity="warning",
            )
            return payload

        if self.has_open_exposure() or self.critical_entry_recheck_active():
            self.pending_completed_background_full_scan = result
            return {
                "status": "deferred",
                "reason": "protected_hot_or_open_path",
                "mode": "background_full_market",
            }
        return self.merge_background_full_scan_result(result)

    def merge_background_full_scan_result(self, result: dict[str, Any]) -> dict[str, Any]:
        self.last_full_scan_monotonic = float(
            result.get("completed_monotonic") or self.clock.monotonic()
        )
        routes = list(result.pop("_routes", []) or [])
        watch_routes = list(result.pop("_watch_routes", []) or [])
        venues = venues_from_routes([*routes, *watch_routes])
        self.store.ensure_funding_paper_accounts(
            venues,
            self.config.venue_starting_balance,
        )
        self.update_hot_routes([*routes, *watch_routes])
        self.apply_watch_scan_retention(
            minimum_keep_latest_scans=max(1, len(routes) + 1)
        )
        if should_record_routine_scan(result):
            self.record_event(
                "background_scan",
                (
                    "Funding background full scan "
                    f"{result.get('funding_scan_id')}: candidates={len(routes)}, "
                    f"watch={len(watch_routes)}"
                ),
                result,
                notify=False,
            )
        self.maybe_record_status_report(result, routes, watch_routes)
        return result

    def shutdown_background_full_scan(self) -> None:
        if self.background_full_scan_executor is None:
            return
        self.background_full_scan_executor.shutdown(wait=False, cancel_futures=True)
        self.background_full_scan_executor = None
        self.background_full_scan_future = None
        self.background_full_scan_cancel_event = None

    def shutdown_foreground_executors(self) -> None:
        for name in ("focused_executor", "lightweight_executor"):
            executor = getattr(self, name)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                setattr(self, name, None)

    def _ensure_focused_executor(self, *, workers: int | None = None) -> ThreadPoolExecutor:
        if self.focused_executor is None:
            self.focused_executor = ThreadPoolExecutor(
                max_workers=max(2, int(workers or self.config.hot_route_recheck_workers)),
                thread_name_prefix="funding-focused",
            )
        return self.focused_executor

    def _ensure_lightweight_executor(self, *, workers: int) -> ThreadPoolExecutor:
        if self.lightweight_executor is None:
            self.lightweight_executor = ThreadPoolExecutor(
                max_workers=max(1, int(workers)),
                thread_name_prefix="funding-lightweight",
            )
        return self.lightweight_executor

    def run_discovery_full_scan(
        self,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        started = self.clock.monotonic()
        self.background_full_scan_skip_reasons = {}
        clients = self.build_venue_clients() or active_default_funding_clients()
        clients = [
            funding_client_for_venue(str(getattr(client, "venue", "")).lower()) or client
            for client in clients
            if str(getattr(client, "venue", "")).lower() not in DEACTIVATED_FUNDING_VENUES
        ]
        clients = [client for client in clients if client is not None]
        now = self.clock.now().astimezone(UTC)
        observed_at = now.isoformat()
        if cancel_event is not None and cancel_event.is_set():
            markets: list[dict[str, Any]] = []
            warnings = ["background scan cancelled before catalog fetch"]
        else:
            markets, warnings = self._fetch_lightweight_market_snapshots(
                clients,
                observed_at,
                max_workers=2,
                cancel_event=cancel_event,
                work_mode="background_full_scan",
            )
        clients_by_venue = {
            str(getattr(client, "venue", "")).lower(): client
            for client in clients
        }
        watch_routes, summary = self._build_lightweight_watch_routes(
            markets,
            clients_by_venue,
            now,
        )
        routes: list[dict[str, Any]] = []
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        return {
            "mode": "background_full_market",
            "funding_scan_id": None,
            "candidate_count": len(routes),
            "watch_count": 0 if cancelled else len(watch_routes),
            "opened_count": 0,
            "closed_count": 0,
            "repriced_count": 0,
            "pending_count": 0,
            "held_count": 0,
            "open_position_count": 0,
            "hot_route_count": 0 if cancelled else count_hot_routes(watch_routes, self.config),
            "urgent_route_count": 0 if cancelled else count_urgent_routes(watch_routes, self.config),
            "universe_route_count": summary["routes_structurally_matched"],
            "execution_shortlist_count": 0,
            "route_count": len(watch_routes),
            "screen_reasons": summary.get("rejection_reasons") or {},
            "blocker_summary": [],
            "warnings": warnings,
            "background_skip_reasons": dict(self.background_full_scan_skip_reasons),
            "cancelled": cancelled,
            "started_monotonic": started,
            "completed_monotonic": self.clock.monotonic(),
            "_routes": routes,
            "_watch_routes": [] if cancelled else watch_routes,
        }

    def run_background_wait_iteration(
        self,
        *,
        background_running: bool,
    ) -> dict[str, Any]:
        self.store.init_db()
        if self.has_open_exposure():
            return self.run_open_position_iteration()
        repriced_count = self.refresh_and_publish_repriced_pnl()
        snapshot = self.store.record_funding_paper_equity_snapshot()
        return {
            "mode": "background_full_scan_wait",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": 0,
            "opened_count": 0,
            "closed_count": 0,
            "repriced_count": repriced_count,
            "pending_count": 0,
            "held_count": 0,
            "open_position_count": snapshot["open_position_count"],
            "hot_route_count": len(self.hot_routes),
            "urgent_route_count": count_urgent_routes(
                list(self.hot_routes.values()),
                self.config,
            ),
            "background_full_scan_running": background_running,
            "equity": snapshot,
        }

    def run_open_position_iteration(self) -> dict[str, Any]:
        self.store.init_db()
        close_events = self.process_open_positions()
        repriced_count = self.refresh_and_publish_repriced_pnl()
        snapshot = self.store.record_funding_paper_equity_snapshot()
        export_funding_paper_csv(self.store, self.config.export_dir)
        return {
            "mode": "open_positions",
            "funding_scan_id": None,
            "candidate_count": 0,
            "watch_count": 0,
            "opened_count": 0,
            "closed_count": sum(1 for event in close_events if event == "closed"),
            "repriced_count": repriced_count,
            "pending_count": sum(
                1 for event in close_events if event == "settlement_pending"
            ),
            "held_count": sum(1 for event in close_events if event == "held"),
            "open_position_count": len(self.store.funding_capture_open_positions())
            + snapshot["open_position_count"],
            "hot_route_count": len(self.hot_routes),
            "urgent_route_count": count_urgent_routes(
                list(self.hot_routes.values()),
                self.config,
            ),
            "equity": snapshot,
        }

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
        entry_events = self.process_entry_candidates([*routes, *watch_routes])
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
            "screen_reasons": (
                (funding.get("universe_summary") or {}).get("screen_reasons")
                or []
            ),
            "blocker_summary": funding.get("blocker_summary") or [],
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
            rechecked_routes,
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
                keep_latest_history_per_market=FUNDING_HISTORY_MIN_ROWS_PER_MARKET,
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
                existing = self.hot_routes.get(route_key)
                if existing is not None and route_is_older_than(route, existing):
                    current_keys.add(route_key)
                    continue
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

        executor = self._ensure_focused_executor(workers=workers)
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
        return None

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
            executor = self._ensure_focused_executor(workers=2)
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
            history_keys = [
                (str(long_market["venue"]), str(long_market["symbol"])),
                (str(short_market["venue"]), str(short_market["symbol"])),
            ]
            history = {key: [] for key in history_keys}
            book_sequences: dict[tuple[str, str], list[dict[str, Any]]] = {}
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
        previous = self.store.latest_funding_market_with_instrument(venue, symbol) or dict(leg)
        snapshot_method = getattr(client, "market_snapshot", None)
        request_started_at = self.clock.now().isoformat()
        if callable(snapshot_method):
            market = snapshot_method(symbol, asset, observed_at, previous)
        else:
            raise FundingDataError(f"{venue} focused_symbol_snapshot_not_supported")
        response_received_at = self.clock.now().isoformat()
        for field in (
            "canonical_asset",
            "base_asset",
            "quote_asset",
            "collateral_asset",
            "contract_type",
            "contract_kind",
            "contract_multiplier",
            "canonical_unit_multiplier",
            "quantity_step",
            "min_quantity",
            "min_notional",
            "min_notional_usd",
            "maker_fee_rate",
            "taker_fee_rate",
            "fee_rate",
            "funding_rate_semantics",
            "funding_rate_unit",
            "funding_sign_convention",
            "supports_discrete_funding",
            "supports_perpetuals",
            "is_linear_contract",
            "position_inclusion_rule",
            "entry_safety_buffer_seconds",
            "exit_safety_buffer_seconds",
            "timing_policy_source",
            "normalization_evidence",
            "fee_source",
            "fee_evidence",
            "fee_observed_at",
            "fee_reviewed_at",
            "environment_verified",
            "endpoint_base_url",
            "endpoint_identity_provenance",
            "endpoint_client_version",
            "endpoint_verified_at",
            "api_product_type",
            "market_type",
            "product_type",
            "data_enabled",
            "strategy_observation_enabled",
            "shadow_candidate_enabled",
            "paper_enabled",
            "live_enabled",
            "execution_model",
            "settlement_verification_level",
            "venue_capability_blockers",
            "stablecoin_route_evaluation",
            "stablecoin_risk",
        ):
            if market.get(field) in (None, "") and previous.get(field) not in (None, ""):
                market[field] = previous[field]
        market["canonical_unit_multiplier"] = previous.get(
            "canonical_unit_multiplier",
            market.get("canonical_unit_multiplier", 1.0),
        )
        market["contract_multiplier"] = previous.get(
            "contract_multiplier",
            market.get("contract_multiplier", 1.0),
        )
        market.setdefault("request_started_at", request_started_at)
        market.setdefault("response_received_at", response_received_at)
        market.setdefault("normalized_at", response_received_at)
        market = apply_declared_venue_capability_contract(market)
        market.setdefault("normalization_evidence", {"source": "adapter_market_snapshot"})
        return market, client

    def fetch_direct_orderbooks(
        self,
        markets_and_clients: list[tuple[dict[str, Any], FundingVenueClient]],
        observed_at: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        books: dict[tuple[str, str], dict[str, Any]] = {}
        executor = self._ensure_focused_executor(workers=max(2, len(markets_and_clients)))
        futures = {
            executor.submit(
                client.orderbook,
                str(market["symbol"]),
                observed_at,
                100,
            ): (market, client, self.clock.now().isoformat())
            for market, client in markets_and_clients
        }
        for future in as_completed(futures):
            market, _client, request_started_at = futures[future]
            key = (str(market["venue"]), str(market["symbol"]))
            book = normalize_orderbook_canonical_units(
                future.result(),
                float(market.get("canonical_unit_multiplier") or 1.0),
            )
            response_received_at = self.clock.now().isoformat()
            book.setdefault("request_started_at", request_started_at)
            book.setdefault("response_received_at", response_received_at)
            book.setdefault("orderbook_event_time", response_received_at)
            books[key] = book
        return books

    def next_sleep_seconds(self, result: dict[str, Any]) -> float:
        now_monotonic = self.clock.monotonic()
        deadlines: list[float] = []
        try:
            has_open_exposure = self.has_open_exposure()
        except sqlite3.Error:
            has_open_exposure = False
        has_open = int(result.get("open_position_count") or 0) > 0 or has_open_exposure
        has_hot = int(result.get("hot_route_count") or 0) > 0 or bool(self.hot_routes)
        has_urgent = int(result.get("urgent_route_count") or 0) > 0 or self.critical_entry_recheck_active()
        try:
            has_pending_reconciliation = bool(self.store.pending_reconciliation_rows())
        except sqlite3.Error:
            has_pending_reconciliation = False
        if has_open:
            deadlines.append(float(self.config.hot_interval_seconds))
        if int(result.get("pending_count") or 0) > 0 or has_pending_reconciliation:
            elapsed = (
                now_monotonic - self.last_reconciliation_monotonic
                if self.last_reconciliation_monotonic > 0
                else 10.0
            )
            deadlines.append(max(0.0, 10.0 - elapsed))
        if has_urgent:
            deadlines.append(float(self.config.hot_interval_seconds))
        if has_hot:
            deadlines.append(float(self.config.monitor_interval_seconds))
        if not has_open and not has_hot:
            last_light = float(getattr(self, "_last_lightweight_discovery_monotonic", 0.0) or 0.0)
            lightweight_interval = self.lightweight_discovery_interval_seconds(result)
            lightweight_due_in = (
                0.0
                if last_light <= 0
                else max(0.0, last_light + lightweight_interval - now_monotonic)
            )
            if 0.0 < lightweight_interval - lightweight_due_in <= 0.05:
                lightweight_due_in = lightweight_interval
            deadlines.append(lightweight_due_in)
            full_due_in = (
                0.0
                if self.last_full_scan_monotonic <= 0
                else max(
                    0.0,
                    self.last_full_scan_monotonic
                    + float(self.config.scan_interval_seconds)
                    - now_monotonic,
                )
            )
            if (
                0.0
                < float(self.config.scan_interval_seconds) - full_due_in
                <= 0.05
            ):
                full_due_in = float(self.config.scan_interval_seconds)
            deadlines.append(full_due_in)
        if result.get("background_full_scan_running") or self.background_full_scan_running():
            deadlines.append(float(self.config.hot_interval_seconds))
        return max(0.0, min(deadlines)) if deadlines else float(self.config.scan_interval_seconds)

    def lightweight_discovery_interval_seconds(
        self,
        result: dict[str, Any] | None = None,
    ) -> float:
        nearest = None
        if result is not None:
            discovery = result.get("lightweight_discovery") or {}
            nearest = discovery.get("nearest_settlement_seconds")
        if nearest is None:
            nearest = self._last_lightweight_nearest_settlement_seconds
        return adaptive_broad_sweep_interval_seconds(nearest)

    def process_entry_candidates(
        self,
        routes: list[dict[str, Any]],
        *,
        recheck_before_open: bool = True,
    ) -> list[str]:
        if synchronized_runtime_enabled(self.config):
            return self.process_synchronized_entry_candidates(
                routes,
                recheck_before_open=recheck_before_open,
            )
        return [str(row) for row in self.process_legacy_entry_candidates(
            routes,
            recheck_before_open=recheck_before_open,
        )]

    def process_synchronized_entry_candidates(
        self,
        routes: list[dict[str, Any]],
        *,
        recheck_before_open: bool = True,
    ) -> list[str]:
        opened: list[str] = []
        accounts = {
            row["venue"]: row for row in self.store.funding_paper_account_rows()
        }
        for route in routes:
            route_key = str(route.get("route_key") or "")
            active_route = route
            now = self.clock.now()
            route_plan = self.synchronized_runtime._plan_dict_for_route(route, now)
            monitor = route_monitor_decision(route, now, self.config)
            if route_plan is not None:
                included_events = [
                    event
                    for event in list(route_plan.get("included_settlement_events") or [])
                    if isinstance(event, dict)
                ]
                first_event_at = min(
                    (
                        parse_iso(event.get("scheduled_at"))
                        for event in included_events
                        if parse_iso(event.get("scheduled_at")) is not None
                    ),
                    default=None,
                )
                first_lead = (
                    (first_event_at - now).total_seconds()
                    if first_event_at is not None
                    else None
                )
                plan_blockers = list(route_plan.get("blockers") or [])
                event_window_hot = bool(
                    not plan_blockers
                    and route_plan.get("lifecycle_state") == "ENTRY_WINDOW_OPEN"
                    and route_plan.get("eligibility_status") == "SHADOW_CANDIDATE"
                    and optional_float(route_plan.get("conservative_net_usd")) is not None
                    and float(route_plan.get("conservative_net_usd") or 0.0) > 0.0
                    and first_lead is not None
                    and first_lead >= 0.0
                    and route.get("status") in {"paper_candidate", "watch"}
                )
                if event_window_hot:
                    monitor = {
                        **monitor,
                        "hot": True,
                        "urgent": bool(
                            first_lead is not None
                            and first_lead <= float(self.config.entry_max_lead_seconds)
                        ),
                        "leads": {**(monitor.get("leads") or {}), "route_plan": first_lead},
                        "reasons": [
                            reason
                            for reason in list(monitor.get("reasons") or [])
                            if reason
                            not in {
                                "long_settlement_outside_arm_window",
                                "short_settlement_outside_arm_window",
                            }
                        ],
                        "event_window_plan": {
                            "opportunity_shape": route_plan.get("opportunity_shape"),
                            "selected_plan": (route_plan.get("planner") or {}).get("selected_plan"),
                            "first_included_settlement_at": first_event_at.isoformat()
                            if first_event_at is not None
                            else None,
                        },
                    }
            if monitor["hot"] and route_key and route_key not in self.armed_routes:
                self.armed_routes.add(route_key)
                self.record_event(
                    "armed",
                    armed_message(route, {**monitor, "armed": True}),
                    {"route": route_summary(route), "decision": monitor},
                    funding_scan_id=route.get("funding_scan_id"),
                    funding_route_id=route.get("funding_route_id"),
                    route_key=route.get("route_key"),
                    notify=True,
                )
            if not monitor["hot"]:
                continue
            if recheck_before_open and self.config.focused_recheck_enabled:
                rechecked = self.focused_recheck_route(route)
                if not rechecked:
                    continue
                active_route = rechecked
            result = self.synchronized_runtime.consider_route(active_route, accounts)
            if not result.get("opened"):
                continue
            position_id = str(result["position_id"])
            opened.append(position_id)
            self.record_event(
                "open",
                (
                    "<b>Paper Bot V2 OPEN</b>\n\n"
                    f"<b>{tg(active_route.get('canonical_asset'))}</b>\n"
                    f"LONG {tg(active_route.get('long_venue'))} / "
                    f"SHORT {tg(active_route.get('short_venue'))}\n"
                    f"Expected net: <b>{format_signed_money((result.get('economics') or {}).get('initial_expected_net_pnl'))}</b>\n"
                    "Source: synchronized_funding_capture_v2, two simulated fills."
                ),
                {
                    "route": route_summary(active_route),
                    "v2_result": result,
                },
                funding_scan_id=active_route.get("funding_scan_id"),
                funding_route_id=active_route.get("funding_route_id"),
                route_key=active_route.get("route_key"),
                notify=True,
            )
            accounts = {
                row["venue"]: row for row in self.store.funding_paper_account_rows()
            }
        return opened

    def process_legacy_entry_candidates(
        self,
        routes: list[dict[str, Any]],
        *,
        recheck_before_open: bool = True,
    ) -> list[int]:
        opened: list[int] = []
        now = self.clock.now()
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
            self._create_v2_capture_position(position, position_id, now)
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

    def _create_v2_capture_position(
        self,
        position: dict[str, Any],
        funding_paper_position_id: int,
        now: datetime,
    ) -> str:
        """Create a v2 funding_capture_position alongside the legacy position."""
        from smart_money_radar.funding.strategy_synchronized_funding import (
            STRATEGY_NAME,
            STRATEGY_VERSION,
        )
        capture_position_id = f"fc-{position.get('route_key', '')}-{int(now.timestamp())}"
        self.store.upsert_funding_capture_position({
            "position_id": capture_position_id,
            "funding_paper_position_id": funding_paper_position_id,
            "strategy_name": STRATEGY_NAME,
            "strategy_version": STRATEGY_VERSION,
            "canonical_asset": position.get("canonical_asset", ""),
            "long_venue": position.get("long_venue", ""),
            "long_symbol": position.get("long_symbol", ""),
            "short_venue": position.get("short_venue", ""),
            "short_symbol": position.get("short_symbol", ""),
            "quantity": float(position.get("base_quantity") or 0.0),
            "target_notional": float(position.get("target_notional") or 0.0),
            "state": "OPEN",
            "opened_at": now.isoformat(),
            "paper_open_fees": float(position.get("expected_execution_cost") or 0.0),
        })
        return capture_position_id

    def _detect_settlement_crossing(
        self,
        position: dict[str, Any],
        now: datetime,
    ) -> None:
        """Detect settlement crossing and create reconciliation rows."""
        from smart_money_radar.paper_bot.settlement import build_settlement_crossing_rows
        max_settlement = parse_iso(position.get("max_settlement_at"))
        if max_settlement is None or now <= max_settlement:
            return
        position_id = int(position["funding_paper_position_id"])
        route_key = str(position.get("route_key") or "")
        capture_id = f"fc-{route_key}-{int(max_settlement.timestamp())}"
        cycle_id = f"{capture_id}:1"
        quantity = float(position.get("base_quantity") or 0.0)
        scheduled_at = max_settlement.isoformat()
        rows = build_settlement_crossing_rows(
            position_id=capture_id,
            cycle_id=cycle_id,
            long_venue=str(position.get("long_venue") or ""),
            long_symbol=str(position.get("long_symbol") or ""),
            short_venue=str(position.get("short_venue") or ""),
            short_symbol=str(position.get("short_symbol") or ""),
            scheduled_funding_at=scheduled_at,
            quantity=quantity,
        )
        for row in rows:
            self.store.upsert_funding_settlement_reconciliation(row)

    def _poll_position_risk(
        self,
        position: dict[str, Any],
        route: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Poll risk engine for an open position. Returns close payload or None.

        Integrates: stale data, dynamic basis, hard risk (liquidation/margin/mark-index).
        Called by process_open_positions on every iteration for every open position.
        """
        close_reason: str | None = None
        reasons: list[str] = []
        risk_details: dict[str, Any] = {}

        # --- Stale data check ---
        route_age = route_data_age_seconds(route, now) if route else None
        if route_age is not None:
            stale = stale_data_decision(snapshot_age_seconds=route_age)
            if stale["emergency_unwind"]:
                return {
                    "close_reason": "risk_data_hard_stale",
                    "reasons": [f"snapshot_age_{route_age:.1f}s>5s_after_retries"],
                    "stale_data": stale,
                }

        # --- Dynamic basis risk ---
        if route is not None:
            spread_snap = compute_spread_snapshot(position, route)
            if spread_snap.get("spread_tracking"):
                entry_spread = float(spread_snap.get("entry_cross_spread") or 0.0)
                current_spread = float(spread_snap.get("current_cross_spread") or 0.0)
                reference = float(spread_snap.get("notional") or 0.0)
                legs = route.get("legs") or []
                long_leg = leg_by_side(legs, "long") or {}
                short_leg = leg_by_side(legs, "short") or {}
                long_exit_buy = float(long_leg.get("best_ask") or long_leg.get("mark_price") or 0.0)
                short_exit_buy = float(short_leg.get("best_ask") or short_leg.get("mark_price") or 0.0)
                long_exit_sell = float(long_leg.get("best_bid") or long_leg.get("mark_price") or 0.0)
                short_exit_sell = float(short_leg.get("best_bid") or short_leg.get("mark_price") or 0.0)
                current_exit_spread = short_exit_buy - long_exit_sell if (short_exit_buy > 0 and long_exit_sell > 0) else current_spread
                notes = position.get("notes") or {}
                accrued_funding = float(notes.get("accrued_funding_pnl") or 0.0)
                conservative_edge = accrued_funding + float(spread_snap.get("unrealized_basis_pnl") or 0.0)
                ref_for_bps = reference if reference > 0 else 500.0
                conservative_edge_bps = conservative_edge / ref_for_bps * 10_000.0
                basis_decision = dynamic_basis_stop_decision(
                    entry_spread=entry_spread,
                    current_exit_spread=current_exit_spread,
                    reference_price=ref_for_bps,
                    active_cycle_conservative_funding_edge_bps=max(0.0, conservative_edge_bps),
                )
                risk_details["dynamic_basis"] = basis_decision
                if basis_decision["immediate_hard_exit"]:
                    return {
                        "close_reason": "basis_deterioration",
                        "reasons": [
                            f"deterioration_{basis_decision['basis_deterioration_bps']:.1f}bps>=budget_{basis_decision['active_risk_budget_bps']:.1f}bps"
                        ],
                        "dynamic_basis": basis_decision,
                    }

        # --- Hard risk: liquidation, margin, mark/index ---
        quantity = float(position.get("base_quantity") or 0.0)
        entry_legs = position.get("entry_legs") or []
        long_entry = leg_by_side(entry_legs, "long") or {}
        short_entry = leg_by_side(entry_legs, "short") or {}
        long_entry_price = float(
            long_entry.get("entry_fill_price")
            or long_entry.get("vwap")
            or long_entry.get("mark_price")
            or 0.0
        )
        short_entry_price = float(
            short_entry.get("entry_fill_price")
            or short_entry.get("vwap")
            or short_entry.get("mark_price")
            or 0.0
        )
        long_notional = float(position.get("long_notional") or 0.0)
        short_notional = float(position.get("short_notional") or 0.0)
        if quantity > 0 and long_entry_price > 0 and short_entry_price > 0:
            from smart_money_radar.paper_bot.risk import (
                liquidation_distance,
                synthetic_liquidation_prices,
            )
            liq_prices = synthetic_liquidation_prices(
                quantity=quantity,
                long_entry_price=long_entry_price,
                short_entry_price=short_entry_price,
                long_isolated_collateral=long_notional,
                short_isolated_collateral=short_notional,
                long_mmr=0.02,
                short_mmr=0.02,
            )
            current_long = float(
                (route.get("legs") or [{}])[0].get("mark_price")
                or long_entry_price
            ) if route else long_entry_price
            current_short = float(
                (route.get("legs") or [{}, {}])[1].get("mark_price")
                or short_entry_price
            ) if route else short_entry_price
            long_liq_dist = liquidation_distance(
                current_long, liq_prices["long_liquidation_price"], "long"
            )
            short_liq_dist = liquidation_distance(
                current_short, liq_prices["short_liquidation_price"], "short"
            )
            min_liq_dist = min(long_liq_dist, short_liq_dist)
            total_position_notional = max(1.0, long_notional + short_notional)
            total_maintenance_margin = total_position_notional * 0.02
            margin_safety = (long_notional + short_notional) / max(1e-12, total_maintenance_margin)
            mark_index_bps = 0.0
            if route:
                for leg in route.get("legs") or []:
                    mark = float(leg.get("mark_price") or 0.0)
                    index_p = float(leg.get("index_price") or 0.0)
                    if mark > 0 and index_p > 0:
                        leg_bps = abs(mark - index_p) / index_p * 10_000.0
                        mark_index_bps = max(mark_index_bps, leg_bps)
            triggered, trigger_reason = hard_risk_triggered(
                liquidation_distance_fraction=min_liq_dist,
                margin_safety_ratio=margin_safety,
                mark_index_divergence_bps=mark_index_bps,
                snapshot_age_seconds=route_age or 0.0,
            )
            risk_details["risk_gates"] = {
                "liquidation_distance": min_liq_dist,
                "margin_safety_ratio": margin_safety,
                "mark_index_divergence_bps": mark_index_bps,
            }
            if triggered:
                return {
                    "close_reason": f"risk_hard_exit:{trigger_reason}",
                    "reasons": [trigger_reason],
                    "risk_gates": risk_details["risk_gates"],
                }
            warnings_list = risk_warnings(
                liquidation_distance_fraction=min_liq_dist,
                margin_safety_ratio=margin_safety,
            )
            if warnings_list:
                risk_details["warnings"] = warnings_list

        return None

    def _maybe_run_reconciliation(self) -> dict[str, Any] | None:
        """Run reconciliation worker on a 10-second cadence."""
        now_monotonic = self.clock.monotonic()
        reconciliation_interval = 10.0
        if (
            self.last_reconciliation_monotonic > 0
            and now_monotonic - self.last_reconciliation_monotonic < reconciliation_interval
        ):
            return None
        self.last_reconciliation_monotonic = now_monotonic
        now = self.clock.now()
        return self.synchronized_runtime.process_pending_reconciliations(now)

    def _run_lightweight_discovery(self) -> dict[str, Any] | None:
        """Lightweight discovery: market snapshots + next funding only, no full orderbooks."""
        if (
            self.hot_routes
            or self.store.funding_capture_open_positions()
            or self.store.funding_paper_open_positions()
        ):
            return None
        now_monotonic = self.clock.monotonic()
        if not lightweight_discovery_due(
            last_discovery_monotonic=getattr(self, "_last_lightweight_discovery_monotonic", 0.0),
            now_monotonic=now_monotonic,
            interval_seconds=self.lightweight_discovery_interval_seconds(),
        ):
            return None
        self._last_lightweight_discovery_monotonic = now_monotonic
        capacity = focused_recheck_capacity_check(
            watch_route_count=len(self.hot_routes),
        )
        if not capacity["sufficient"]:
            return {
                "status": "skipped",
                "reason": capacity["reason"],
            }
        clients = self.build_venue_clients() or active_default_funding_clients()
        clients = [
            client
            for client in clients
            if str(getattr(client, "venue", "")).lower() not in DEACTIVATED_FUNDING_VENUES
        ]
        if len(clients) < 2:
            return {
                "status": "skipped",
                "reason": "insufficient_lightweight_venues",
                "markets_checked": 0,
                "routes_structurally_matched": 0,
                "watch_routes_added": 0,
                "research_only_routes": 0,
                "rejection_reasons": {"insufficient_lightweight_venues": 1},
            }

        now = self.clock.now().astimezone(UTC)
        observed_at = now.isoformat()
        markets, warnings = self._fetch_lightweight_market_snapshots(clients, observed_at)
        clients_by_venue = {
            str(getattr(client, "venue", "")).lower(): client
            for client in clients
        }
        routes, summary = self._build_lightweight_watch_routes(
            markets,
            clients_by_venue,
            now,
        )
        self._last_lightweight_nearest_settlement_seconds = summary.get(
            "nearest_settlement_seconds"
        )
        for route in routes:
            route_key = str(route.get("route_key") or "")
            if not route_key:
                continue
            existing = self.hot_routes.get(route_key)
            if existing is not None and route_is_older_than(route, existing):
                continue
            self.hot_routes[route_key] = route
        return {
            "status": "success",
            "watch_route_count": len(self.hot_routes),
            "warnings": warnings,
            **summary,
        }

    def _fetch_lightweight_market_snapshots(
        self,
        clients: list[FundingVenueClient],
        observed_at: str,
        *,
        max_workers: int | None = None,
        cancel_event: Event | None = None,
        work_mode: str = "foreground_lightweight",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        catalog_results: dict[
            str,
            tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str, str],
        ] = {}
        warnings: list[str] = []
        worker_count = max(1, min(int(max_workers or len(clients) or 1), len(clients) or 1))
        if work_mode == "background_full_scan":
            executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="funding-background-venue",
            )
        else:
            executor = self._ensure_lightweight_executor(workers=worker_count)
        futures = {}
        foreground_venues: set[str] = set()
        try:
            if work_mode != "background_full_scan":
                for pending_future, pending_venue in list(
                    self._lightweight_pending_futures.items()
                ):
                    if pending_future.done() or pending_future.cancelled():
                        self._lightweight_pending_futures.pop(pending_future, None)
                inflight_venues = set(self._lightweight_pending_futures.values())
            else:
                inflight_venues = set()
            for client in clients:
                if cancel_event is not None and cancel_event.is_set():
                    break
                venue = str(getattr(client, "venue", "")).lower()
                if venue in inflight_venues:
                    warnings.append(f"{venue} lightweight catalog skipped: request_in_flight")
                    continue
                if (
                    work_mode == "background_full_scan"
                    and not bool(getattr(client, "thread_safe", False))
                    and venue in self._foreground_venues
                ):
                    reason = "foreground_venue_work_active"
                    self.background_full_scan_skip_reasons[venue] = reason
                    warnings.append(f"{venue} background catalog skipped: {reason}")
                    continue
                if work_mode != "background_full_scan":
                    self._foreground_venues.add(venue)
                    foreground_venues.add(venue)
                else:
                    self._background_venues.add(venue)
                request_started_at = self.clock.now().isoformat()
                futures[
                    executor.submit(
                        self._cancelable_catalog_and_markets,
                        client,
                        observed_at,
                        cancel_event,
                    )
                ] = (
                    venue,
                    request_started_at,
                )
            if work_mode != "background_full_scan":
                for future, (venue, _request_started_at) in futures.items():
                    self._lightweight_pending_futures[future] = venue
            done, pending = wait(
                futures,
                timeout=2.0,
            )
            for future in pending:
                venue, _request_started_at = futures[future]
                future.cancel()
                warnings.append(f"{venue} lightweight catalog skipped: venue_deadline_2s")
            for future in done:
                venue, request_started_at = futures[future]
                if work_mode != "background_full_scan":
                    self._lightweight_pending_futures.pop(future, None)
                if cancel_event is not None and cancel_event.is_set():
                    future.cancel()
                    continue
                try:
                    instruments, markets, venue_warnings = future.result()
                except FundingDataError as exc:
                    warnings.append(f"{venue} lightweight catalog skipped: {exc}")
                    continue
                response_received_at = self.clock.now().isoformat()
                catalog_results[venue] = (
                    instruments,
                    markets,
                    venue_warnings,
                    request_started_at,
                    response_received_at,
                )
        finally:
            for venue in foreground_venues:
                self._foreground_venues.discard(venue)
            if work_mode == "background_full_scan":
                for client in clients:
                    self._background_venues.discard(str(getattr(client, "venue", "")).lower())
            if work_mode == "background_full_scan":
                executor.shutdown(wait=False, cancel_futures=True)

        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        request_times: dict[tuple[str, str], tuple[str, str]] = {}
        for venue, result in catalog_results.items():
            venue_instruments, venue_markets, venue_warnings, started_at, received_at = result
            venue_instruments, venue_markets = normalize_catalog_canonical_units(
                venue_instruments,
                venue_markets,
            )
            warnings.extend(venue_warnings)
            for row in venue_instruments:
                key = (str(row.get("venue") or venue).lower(), str(row.get("symbol") or ""))
                request_times[key] = (started_at, received_at)
            for row in venue_markets:
                key = (str(row.get("venue") or venue).lower(), str(row.get("symbol") or ""))
                request_times.setdefault(key, (started_at, received_at))
            instruments.extend(venue_instruments)
            markets.extend(venue_markets)

        instrument_by_key = {
            (str(row.get("venue") or "").lower(), str(row.get("symbol") or "")): row
            for row in instruments
        }
        enriched_markets: list[dict[str, Any]] = []
        for market in markets:
            key = (
                str(market.get("venue") or "").lower(),
                str(market.get("symbol") or ""),
            )
            instrument = instrument_by_key.get(key, {})
            row = {**instrument, **market}
            started_at, received_at = request_times.get(key, (observed_at, observed_at))
            row.setdefault("request_started_at", started_at)
            row.setdefault("response_received_at", received_at)
            row.setdefault("normalized_at", received_at)
            row = apply_declared_venue_capability_contract(row)
            row.setdefault(
                "normalization_evidence",
                {"source": "lightweight_market_snapshot"},
            )
            enriched_markets.append(row)
        return enriched_markets, warnings

    def _cancelable_catalog_and_markets(
        self,
        client: FundingVenueClient,
        observed_at: str,
        cancel_event: Event | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        if cancel_event is not None and cancel_event.is_set():
            return [], [], ["background scan cancelled before venue fetch"]
        return client.catalog_and_markets(observed_at)

    def _build_lightweight_watch_routes(
        self,
        markets: list[dict[str, Any]],
        clients_by_venue: dict[str, FundingVenueClient],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rejection_reasons: dict[str, int] = {}
        nearest_settlement_seconds: float | None = None

        def reject(reason: str) -> None:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        eligible_by_asset: dict[str, list[dict[str, Any]]] = {}
        markets_checked = 0
        research_only_routes = 0
        for market in markets:
            markets_checked += 1
            venue = str(market.get("venue") or "").lower()
            market["venue"] = venue
            settlement = parse_iso(market.get("next_funding_at"))
            if settlement is None:
                reject("next_funding_at_missing")
                continue
            seconds_to_settlement = (settlement - now).total_seconds()
            if seconds_to_settlement >= 0:
                nearest_settlement_seconds = (
                    seconds_to_settlement
                    if nearest_settlement_seconds is None
                    else min(nearest_settlement_seconds, seconds_to_settlement)
                )
            if seconds_to_settlement < 55 or seconds_to_settlement > 600:
                reject("outside_lightweight_settlement_window")
                continue
            asset = str(market.get("canonical_asset") or "").upper()
            if not asset:
                reject("canonical_asset_missing")
                continue
            if optional_float(market.get("mark_price")) is None:
                reject("mark_price_missing")
                continue
            if optional_float(market.get("index_price")) is None:
                reject("index_price_missing")
                continue
            if optional_float(market.get("normalized_next_funding_rate")) is None:
                reject("normalized_next_funding_rate_missing")
                continue
            if venue not in clients_by_venue:
                reject("venue_client_missing")
                continue
            eligible_by_asset.setdefault(asset, []).append(market)

        routes_by_key: dict[str, dict[str, Any]] = {}
        structurally_matched = 0
        for asset, rows in eligible_by_asset.items():
            if len({str(row.get("venue") or "") for row in rows}) < 2:
                continue
            for long_market in rows:
                for short_market in rows:
                    long_venue = str(long_market.get("venue") or "")
                    short_venue = str(short_market.get("venue") or "")
                    if not long_venue or not short_venue or long_venue == short_venue:
                        continue
                    structurally_matched += 1
                    skew = settlement_skew_seconds(
                        long_market.get("next_funding_at"),
                        short_market.get("next_funding_at"),
                    )
                    capability = self._lightweight_capability_check(
                        long_market,
                        short_market,
                        clients_by_venue,
                    )
                    if not capability["paper_eligible"]:
                        research_only_routes += 1
                        for reason in capability["all_reasons"]:
                            reject(reason)
                        continue
                    plan = build_settlement_capture_opportunity(
                        long_market=long_market,
                        short_market=short_market,
                        now=now,
                        target_notional=float(self.config.target_notional_per_leg),
                    )
                    plan_blockers = list(plan.get("blockers") or [])
                    if plan_blockers:
                        research_only_routes += 1
                        for reason in plan_blockers:
                            reject(str(reason))
                        continue
                    long_mark = float(optional_float(long_market.get("mark_price")) or 0.0)
                    short_mark = float(optional_float(short_market.get("mark_price")) or 0.0)
                    if long_mark <= 0 or short_mark <= 0:
                        reject("mark_price_missing")
                        continue
                    target_notional = float(self.config.target_notional_per_leg)
                    quantity = min(target_notional / long_mark, target_notional / short_mark)
                    gross = float(plan.get("conservative_funding_cashflow_usd") or 0.0)
                    if gross <= 0:
                        reject("preliminary_gross_funding_not_positive")
                        continue
                    route = self._lightweight_watch_route(
                        asset,
                        long_market,
                        short_market,
                        quantity,
                        gross,
                        capability,
                        now,
                        route_plan=plan,
                        settlement_skew_seconds_value=skew,
                    )
                    route_key = str(route["route_key"])
                    existing = routes_by_key.get(route_key)
                    if (
                        existing is None
                        or gross > float(
                            ((existing.get("evidence") or {}).get("current_nowcast_gross") or 0.0)
                        )
                    ):
                        routes_by_key[route_key] = route

        return list(routes_by_key.values()), {
            "markets_checked": markets_checked,
            "routes_structurally_matched": structurally_matched,
            "watch_routes_added": len(routes_by_key),
            "research_only_routes": research_only_routes,
            "nearest_settlement_seconds": nearest_settlement_seconds,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        }

    def _lightweight_capability_check(
        self,
        long_market: dict[str, Any],
        short_market: dict[str, Any],
        clients_by_venue: dict[str, FundingVenueClient],
    ) -> dict[str, Any]:
        check = synchronized_route_capability_check(
            capability_from_market(long_market),
            capability_from_market(short_market),
        )
        allowed_lightweight_missing = {
            "long_orderbook_timestamp_missing",
            "long_orderbook_depth_missing",
            "short_orderbook_timestamp_missing",
            "short_orderbook_depth_missing",
        }
        reasons = [
            reason
            for reason in check["all_reasons"]
            if reason not in allowed_lightweight_missing
        ]
        for side, market in (("long", long_market), ("short", short_market)):
            venue = str(market.get("venue") or "").lower()
            client = clients_by_venue.get(venue)
            if client is None or not callable(getattr(client, "orderbook", None)):
                reasons.append(f"{side}_orderbook_client_missing")
            if optional_float(market.get("normalized_next_funding_rate")) is None:
                reasons.append(f"{side}_normalized_next_funding_rate_missing")
        reasons = list(dict.fromkeys(reasons))
        return {
            **check,
            "paper_eligible": not reasons,
            "all_reasons": reasons,
            "lightweight_allowed_missing": sorted(allowed_lightweight_missing),
        }

    def _lightweight_watch_route(
        self,
        asset: str,
        long_market: dict[str, Any],
        short_market: dict[str, Any],
        quantity: float,
        preliminary_gross: float,
        capability: dict[str, Any],
        now: datetime,
        *,
        route_plan: dict[str, Any] | None = None,
        settlement_skew_seconds_value: float | None = None,
    ) -> dict[str, Any]:
        long_venue = str(long_market["venue"])
        short_venue = str(short_market["venue"])
        long_mark = float(long_market["mark_price"])
        short_mark = float(short_market["mark_price"])
        target_notional = float(self.config.target_notional_per_leg)
        selected_strategy = {
            "selection_model": "lightweight_discovery_v1",
            "strategy_name": "synchronized_funding_capture",
            "strategy_class": "synchronized_funding_capture",
            "strategy_version": "synchronized_funding_capture_v2",
            "primary_edge": "synchronized_funding",
            "edge_type": "funding_led",
            "edge_label": "Lightweight funding watch",
            "eligible": True,
            "expected_net_pnl": preliminary_gross,
            "gross_edge_pnl": preliminary_gross,
            "funding_pnl_component": preliminary_gross,
            "spread_pnl_component": 0.0,
            "signed_spread_pnl_component": 0.0,
            "expected_spread_convergence_pnl": 0.0,
            "execution_cost": 0.0,
            "basis_stress_loss": 0.0,
            "actionable_profit_threshold": 0.0,
            "coverage_ratio": None,
            "reasons": [],
            "warnings": ["lightweight_only_requires_focused_underwriting"],
            "thesis": (
                "Preliminary synchronized funding watch. This is not an "
                "entry candidate until focused orderbook observations pass."
            ),
        }
        route = {
            "route_key": perp_route_key(asset, long_venue, short_venue),
            "route_type": "perp_perp",
            "canonical_asset": asset,
            "venue_scope": "+".join(sorted((long_venue, short_venue))),
            "long_venue": long_venue,
            "long_symbol": long_market["symbol"],
            "short_venue": short_venue,
            "short_symbol": short_market["symbol"],
            "status": "watch",
            "target_notional": target_notional,
            "observed_at": now.astimezone(UTC).isoformat(),
            "next_funding_at": long_market.get("next_funding_at"),
            "legs": [
                self._lightweight_route_leg("long", long_market, quantity, long_mark),
                self._lightweight_route_leg("short", short_market, quantity, short_mark),
            ],
            "rationale": [
                "Lightweight discovery found a positive event-window funding plan.",
                "No orderbooks were fetched; focused underwriting remains mandatory before entry.",
            ],
            "risk_flags": ["lightweight_only_requires_focused_underwriting"],
            "evidence": {
                "decision_mode": "settlement_capture",
                "history_is_advisory": True,
                "lightweight_discovery": {
                    "observed_at": now.astimezone(UTC).isoformat(),
                    "preliminary_gross_funding": preliminary_gross,
                    "target_notional": target_notional,
                    "quantity": quantity,
                    "settlement_skew_seconds": settlement_skew_seconds_value,
                },
                "funding_route_plan": route_plan,
                "strategy_candidates": [selected_strategy],
                "selected_strategy": selected_strategy,
                "strategy_classification": selected_strategy,
                "pnl_components": {
                    "funding_pnl_component": preliminary_gross,
                    "spread_pnl_component": 0.0,
                    "signed_spread_pnl_component": 0.0,
                    "spread_convergence_component": 0.0,
                    "execution_cost": 0.0,
                    "funding_only_net_pnl": preliminary_gross,
                    "combined_net_pnl": preliminary_gross,
                    "opportunity_expected_net_pnl": preliminary_gross,
                    "positive_edge_pnl": preliminary_gross,
                    "drag_pnl": 0.0,
                },
                "current_nowcast_gross": preliminary_gross,
                "current_nowcast_net": preliminary_gross,
                "synchronized_capability_passed": capability["paper_eligible"],
                "capability_rejections": capability["all_reasons"],
                "capability_check": capability,
                "blocking_reasons": [],
                "blocking_risk_flags": [],
                "advisory_reasons": ["lightweight_only_requires_focused_underwriting"],
                "requested_target_notional": target_notional,
            },
        }
        return route

    def _lightweight_route_leg(
        self,
        side: str,
        market: dict[str, Any],
        quantity: float,
        mark_price: float,
    ) -> dict[str, Any]:
        fee_rate = optional_float(market.get("fee_rate"))
        if fee_rate is None:
            fee_rate = optional_float(market.get("taker_fee_rate"))
        min_notional = optional_float(market.get("min_notional"))
        if min_notional is None:
            min_notional = optional_float(market.get("min_notional_usd"))
        return {
            "side": side,
            "venue": market["venue"],
            "environment": market.get("environment"),
            "symbol": market["symbol"],
            "canonical_asset": market.get("canonical_asset"),
            "notional": quantity * mark_price,
            "base_quantity": quantity,
            "funding_rate": market.get("funding_rate"),
            "raw_funding_rate": market.get("raw_funding_rate", market.get("funding_rate")),
            "raw_funding_rate_unit": market.get("raw_funding_rate_unit"),
            "normalized_next_funding_rate": market.get(
                "normalized_next_funding_rate"
            ),
            "funding_rate_unit": market.get("funding_rate_unit"),
            "funding_sign_convention": market.get("funding_sign_convention"),
            "normalization_evidence": market.get("normalization_evidence"),
            "funding_interval_hours": market.get("funding_interval_hours"),
            "hourly_funding_rate": market.get("hourly_funding_rate"),
            "funding_rate_kind": market.get("funding_rate_kind"),
            "published_funding_rate": market.get("published_funding_rate", market.get("funding_rate")),
            "published_funding_interval_hours": market.get(
                "published_funding_interval_hours",
                market.get("funding_interval_hours"),
            ),
            "next_funding_at": market.get("next_funding_at"),
            "market_request_started_at": market.get("request_started_at"),
            "market_response_received_at": market.get("response_received_at"),
            "response_received_at": market.get("response_received_at"),
            "normalized_at": market.get("normalized_at"),
            "venue_server_time": market.get("venue_server_time"),
            "source_event_at": market.get("source_event_at"),
            "mark_price": mark_price,
            "index_price": market.get("index_price"),
            "quote_asset": market.get("quote_asset"),
            "collateral_asset": market.get("collateral_asset"),
            "contract_type": market.get("contract_type"),
            "contract_kind": market.get("contract_kind"),
            "contract_multiplier": market.get("contract_multiplier"),
            "canonical_unit_multiplier": market.get("canonical_unit_multiplier"),
            "volume_24h_usd": market.get("volume_24h_usd"),
            "open_interest_usd": market.get("open_interest_usd"),
            "fee_rate": fee_rate,
            "taker_fee_rate": fee_rate,
            "maker_fee_rate": market.get("maker_fee_rate"),
            "quantity_step": market.get("quantity_step"),
            "min_quantity": market.get("min_quantity"),
            "min_notional": min_notional,
            "min_notional_usd": min_notional,
            "position_inclusion_rule": market.get("position_inclusion_rule"),
            "entry_safety_buffer_seconds": market.get("entry_safety_buffer_seconds"),
            "exit_safety_buffer_seconds": market.get("exit_safety_buffer_seconds"),
            "timing_policy_source": market.get("timing_policy_source"),
        }

    def refresh_open_capture_route(
        self,
        position: dict[str, Any],
        now: datetime,
    ) -> CaptureRouteRefreshResult:
        position_id = str(position.get("position_id") or "")
        canonical_asset = str(position.get("canonical_asset") or "").upper()
        leg_specs = {
            "long": {
                "venue": str(position.get("long_venue") or "").lower(),
                "symbol": str(position.get("long_symbol") or ""),
            },
            "short": {
                "venue": str(position.get("short_venue") or "").lower(),
                "symbol": str(position.get("short_symbol") or ""),
            },
        }
        if (
            not position_id
            or not canonical_asset
            or not leg_specs["long"]["venue"]
            or not leg_specs["long"]["symbol"]
            or not leg_specs["short"]["venue"]
            or not leg_specs["short"]["symbol"]
        ):
            return CaptureRouteRefreshResult(
                quality="UNAVAILABLE",
                route=None,
                snapshot_id=None,
                reason="position_leg_identity_missing",
            )

        observed_at = now.astimezone(UTC).isoformat()
        snapshot_id = (
            "open-refresh:"
            f"{position_id}:"
            f"{hashlib.sha256(observed_at.encode('utf-8')).hexdigest()[:12]}"
        )
        executor = self._ensure_focused_executor(workers=2)
        for spec in leg_specs.values():
            self._foreground_venues.add(str(spec["venue"]))
        futures = {
            executor.submit(
                self._refresh_open_capture_leg,
                side,
                canonical_asset,
                spec["venue"],
                spec["symbol"],
                observed_at,
                position,
            ): side
            for side, spec in leg_specs.items()
        }
        done, pending = wait(futures, timeout=OPEN_CAPTURE_REFRESH_TIMEOUT_SECONDS)
        leg_results: dict[str, dict[str, Any]] = {}
        try:
            for future in pending:
                side = futures[future]
                future.cancel()
                leg_results[side] = {
                    "status": "timeout",
                    "reason": "venue_refresh_timeout_2s",
                }
            for future in done:
                side = futures[future]
                try:
                    leg_results[side] = future.result()
                except Exception as exc:
                    leg_results[side] = {
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
        finally:
            for spec in leg_specs.values():
                self._foreground_venues.discard(str(spec["venue"]))

        long_result = leg_results.get("long") or {}
        short_result = leg_results.get("short") or {}
        long_leg = dict(long_result.get("leg") or {})
        short_leg = dict(short_result.get("leg") or {})
        if not long_leg and not short_leg:
            return CaptureRouteRefreshResult(
                quality="UNAVAILABLE",
                route=None,
                snapshot_id=snapshot_id,
                reason="both_venue_refreshes_unavailable",
                leg_results=leg_results,
            )

        route = {
            "route_key": str((position.get("config") or {}).get("route_key") or ""),
            "route_type": "perp_perp",
            "canonical_asset": canonical_asset,
            "venue_scope": "+".join(sorted([leg_specs["long"]["venue"], leg_specs["short"]["venue"]])),
            "long_venue": leg_specs["long"]["venue"],
            "long_symbol": leg_specs["long"]["symbol"],
            "short_venue": leg_specs["short"]["venue"],
            "short_symbol": leg_specs["short"]["symbol"],
            "status": "watch",
            "target_notional": float(position.get("target_notional") or self.config.target_notional_per_leg),
            "observed_at": observed_at,
            "next_funding_at": long_leg.get("next_funding_at"),
            "legs": [long_leg, short_leg],
            "evidence": {
                "targeted_refresh": {
                    "mode": "open_capture_route_refresh_v1",
                    "snapshot_id": snapshot_id,
                    "quality": None,
                    "observed_at": observed_at,
                    "position_id": position_id,
                    "venues": [leg_specs["long"]["venue"], leg_specs["short"]["venue"]],
                }
            },
        }
        quality, reason = self._open_capture_route_quality(route, now, leg_results)
        route["evidence"]["targeted_refresh"]["quality"] = quality
        route["evidence"]["targeted_refresh"]["reason"] = reason
        route["evidence"]["targeted_refresh"]["leg_results"] = {
            side: {k: v for k, v in result.items() if k != "leg"}
            for side, result in leg_results.items()
        }
        return CaptureRouteRefreshResult(
            quality=quality,
            route=route if quality == "FRESH" else None,
            snapshot_id=snapshot_id,
            reason=reason,
            leg_results=leg_results,
        )

    def _refresh_open_capture_leg(
        self,
        side: str,
        canonical_asset: str,
        venue: str,
        symbol: str,
        observed_at: str,
        position: dict[str, Any],
    ) -> dict[str, Any]:
        client = self._client_for_open_refresh_venue(venue)
        if client is None:
            return {"status": "unavailable", "reason": "venue_client_missing"}
        entry_legs = (position.get("config") or {}).get("entry_legs") or []
        previous = leg_by_side(entry_legs, side) or {}
        snapshot_method = getattr(client, "market_snapshot", None)
        request_started_at = self.clock.now().isoformat()
        if callable(snapshot_method):
            market = snapshot_method(symbol, canonical_asset, observed_at, previous)
        else:
            return {
                "status": "unavailable",
                "reason": "focused_symbol_snapshot_not_supported",
            }
        market = dict(market)
        market.setdefault("venue", venue)
        market.setdefault("symbol", symbol)
        market.setdefault("canonical_asset", canonical_asset)
        market_response_at = self.clock.now().isoformat()
        for field in (
            "base_asset",
            "quote_asset",
            "collateral_asset",
            "contract_type",
            "contract_kind",
            "contract_multiplier",
            "canonical_unit_multiplier",
            "quantity_step",
            "min_quantity",
            "min_notional",
            "min_notional_usd",
            "maker_fee_rate",
            "taker_fee_rate",
            "fee_rate",
            "funding_rate_semantics",
            "funding_rate_unit",
            "funding_sign_convention",
            "supports_discrete_funding",
            "supports_perpetuals",
            "is_linear_contract",
            "position_inclusion_rule",
            "entry_safety_buffer_seconds",
            "exit_safety_buffer_seconds",
            "timing_policy_source",
            "normalization_evidence",
            "fee_source",
            "fee_evidence",
            "fee_observed_at",
            "fee_reviewed_at",
            "environment_verified",
            "endpoint_base_url",
            "endpoint_identity_provenance",
            "endpoint_client_version",
            "endpoint_verified_at",
            "api_product_type",
            "market_type",
            "product_type",
            "data_enabled",
            "strategy_observation_enabled",
            "shadow_candidate_enabled",
            "paper_enabled",
            "live_enabled",
            "execution_model",
            "settlement_verification_level",
            "venue_capability_blockers",
            "stablecoin_route_evaluation",
            "stablecoin_risk",
        ):
            if market.get(field) in (None, "") and previous.get(field) not in (None, ""):
                market[field] = previous[field]
        book = client.orderbook(symbol, observed_at, 100)
        book = normalize_orderbook_canonical_units(
            book,
            float(market.get("canonical_unit_multiplier") or 1.0),
        )
        orderbook_response_at = self.clock.now().isoformat()
        fee_rate = optional_float(market.get("fee_rate"))
        if fee_rate is None:
            fee_rate = optional_float(market.get("taker_fee_rate"))
        min_notional = optional_float(market.get("min_notional"))
        if min_notional is None:
            min_notional = optional_float(market.get("min_notional_usd"))
        leg = {
            "side": side,
            "venue": venue,
            "environment": market.get("environment"),
            "symbol": symbol,
            "funding_rate": market.get("funding_rate"),
            "raw_funding_rate": market.get("raw_funding_rate", market.get("funding_rate")),
            "raw_funding_rate_unit": market.get("raw_funding_rate_unit"),
            "normalized_next_funding_rate": market.get(
                "normalized_next_funding_rate"
            ),
            "funding_rate_unit": market.get("funding_rate_unit"),
            "funding_sign_convention": market.get("funding_sign_convention"),
            "normalization_evidence": market.get("normalization_evidence"),
            "funding_interval_hours": market.get("funding_interval_hours"),
            "hourly_funding_rate": market.get("hourly_funding_rate"),
            "funding_rate_kind": market.get("funding_rate_kind"),
            "next_funding_at": market.get("next_funding_at"),
            "market_request_started_at": request_started_at,
            "market_response_received_at": market.get("response_received_at") or market_response_at,
            "response_received_at": book.get("response_received_at") or orderbook_response_at,
            "normalized_at": market.get("normalized_at") or market_response_at,
            "venue_server_time": market.get("venue_server_time") or book.get("venue_server_time"),
            "source_event_at": market.get("source_event_at") or book.get("orderbook_event_time"),
            "orderbook_request_started_at": book.get("request_started_at") or request_started_at,
            "orderbook_response_received_at": book.get("response_received_at") or orderbook_response_at,
            "orderbook_event_time": book.get("orderbook_event_time") or orderbook_response_at,
            "mark_price": market.get("mark_price"),
            "index_price": market.get("index_price"),
            "best_bid": book.get("best_bid"),
            "best_ask": book.get("best_ask"),
            "bids": book.get("bids") or [],
            "asks": book.get("asks") or [],
            "fee_rate": fee_rate,
            "taker_fee_rate": fee_rate,
            "maker_fee_rate": market.get("maker_fee_rate"),
            "fee_source": market.get("fee_source"),
            "fee_evidence": market.get("fee_evidence"),
            "fee_observed_at": market.get("fee_observed_at"),
            "fee_reviewed_at": market.get("fee_reviewed_at"),
            "quantity_step": market.get("quantity_step"),
            "min_quantity": market.get("min_quantity"),
            "min_notional": min_notional,
            "min_notional_usd": min_notional,
            "quote_asset": market.get("quote_asset"),
            "collateral_asset": market.get("collateral_asset"),
            "environment_verified": market.get("environment_verified"),
            "endpoint_base_url": market.get("endpoint_base_url"),
            "endpoint_identity_provenance": market.get("endpoint_identity_provenance"),
            "endpoint_client_version": market.get("endpoint_client_version"),
            "endpoint_verified_at": market.get("endpoint_verified_at"),
            "api_product_type": market.get("api_product_type"),
            "market_type": market.get("market_type"),
            "product_type": market.get("product_type"),
            "data_enabled": market.get("data_enabled"),
            "strategy_observation_enabled": market.get("strategy_observation_enabled"),
            "shadow_candidate_enabled": market.get("shadow_candidate_enabled"),
            "paper_enabled": market.get("paper_enabled"),
            "live_enabled": market.get("live_enabled"),
            "execution_model": market.get("execution_model"),
            "settlement_verification_level": market.get("settlement_verification_level"),
            "venue_capability_blockers": market.get("venue_capability_blockers"),
            "stablecoin_route_evaluation": market.get("stablecoin_route_evaluation"),
            "stablecoin_risk": market.get("stablecoin_risk"),
            "contract_type": market.get("contract_type"),
            "contract_kind": market.get("contract_kind"),
            "contract_multiplier": market.get("contract_multiplier"),
            "canonical_unit_multiplier": market.get("canonical_unit_multiplier"),
            "contract_status": market.get("contract_status") or market.get("status"),
            "status": market.get("status") or market.get("contract_status"),
            "position_inclusion_rule": market.get("position_inclusion_rule"),
            "entry_safety_buffer_seconds": market.get("entry_safety_buffer_seconds"),
            "exit_safety_buffer_seconds": market.get("exit_safety_buffer_seconds"),
            "timing_policy_source": market.get("timing_policy_source"),
        }
        return {"status": "success", "leg": leg}

    def _client_for_open_refresh_venue(self, venue: str) -> FundingVenueClient | None:
        configured = self.build_venue_clients()
        for client in configured or []:
            if str(getattr(client, "venue", "")).lower() == str(venue).lower():
                if (
                    not bool(getattr(client, "thread_safe", False))
                    and str(venue).lower() in self._background_venues
                ):
                    break
                return client
        return funding_client_for_venue(
            str(venue).lower(),
            fast=True,
            timeout_seconds=OPEN_CAPTURE_REFRESH_TIMEOUT_SECONDS,
        )

    def _open_capture_route_quality(
        self,
        route: dict[str, Any],
        now: datetime,
        leg_results: dict[str, dict[str, Any]],
    ) -> tuple[str, str | None]:
        legs = route.get("legs") or []
        long_leg = leg_by_side(legs, "long") or {}
        short_leg = leg_by_side(legs, "short") or {}
        if not long_leg or not short_leg:
            return "PARTIAL", "one_or_more_legs_missing"
        missing: list[str] = []
        for side, leg in (("long", long_leg), ("short", short_leg)):
            if leg_results.get(side, {}).get("status") != "success":
                missing.append(f"{side}_refresh_failed")
            if optional_float(leg.get("mark_price")) is None:
                missing.append(f"{side}_mark_missing")
            if optional_float(leg.get("index_price")) is None:
                missing.append(f"{side}_index_missing")
            if optional_float(leg.get("normalized_next_funding_rate")) is None:
                missing.append(f"{side}_normalized_next_funding_rate_missing")
            if parse_iso(leg.get("next_funding_at")) is None:
                missing.append(f"{side}_next_funding_at_missing")
            if optional_float(leg.get("fee_rate")) is None and optional_float(leg.get("taker_fee_rate")) is None:
                missing.append(f"{side}_taker_fee_missing")
            if not (leg.get("status") or leg.get("contract_status")):
                missing.append(f"{side}_contract_status_missing")
            close_book = leg.get("bids") if side == "long" else leg.get("asks")
            if not close_book:
                missing.append(f"{side}_executable_close_book_missing")
        long_response = parse_iso(long_leg.get("response_received_at"))
        short_response = parse_iso(short_leg.get("response_received_at"))
        if long_response is None:
            missing.append("long_response_received_at_missing")
        if short_response is None:
            missing.append("short_response_received_at_missing")
        if missing:
            return "PARTIAL", ",".join(sorted(set(missing)))
        now_utc = now.astimezone(UTC)
        long_age = (now_utc - long_response).total_seconds()
        short_age = (now_utc - short_response).total_seconds()
        cross_skew = abs((long_response - short_response).total_seconds())
        if long_age > 2.0 or short_age > 2.0 or cross_skew > 1.0:
            return "STALE", "response_stale_or_skewed"
        return "FRESH", None

    def process_open_positions(self) -> list[str]:
        if synchronized_runtime_enabled(self.config):
            return self.process_synchronized_open_positions()
        return self.process_legacy_open_positions()

    def process_synchronized_open_positions(self) -> list[str]:
        outcomes: list[str] = []
        now = self.clock.now()
        for position in self.store.funding_capture_open_positions():
            position_id = str(position["position_id"])
            state = str(position.get("state") or "")
            if state in {
                "CLOSED_PENDING_RECONCILIATION",
                "RECONCILED",
                "UNRECONCILED",
                "FAILED",
            }:
                continue
            config_json = position.get("config") or {}
            route_key = str(config_json.get("route_key") or "")
            attempt_now = self.clock.now()
            refreshed = self.refresh_open_capture_route(position, attempt_now)
            if refreshed.quality != "FRESH":
                self.synchronized_runtime.mark_position_data_quality(
                    position,
                    state="DEGRADED",
                    now=attempt_now,
                    refresh_result={
                        **refreshed.as_dict(),
                        "retry_mode": "across_iterations",
                    },
                )
            else:
                now = attempt_now
            live_route = refreshed.route if refreshed and refreshed.quality == "FRESH" else None
            if live_route is not None:
                if route_key:
                    self.hot_routes[route_key] = live_route
                self.synchronized_runtime.mark_position_data_quality(
                    position,
                    state="HEALTHY",
                    now=now,
                    refresh_result=refreshed.as_dict(),
                    last_valid_route=live_route,
                )
                position = self.store.funding_capture_position_by_id(position_id) or position
            else:
                position = self.store.funding_capture_position_by_id(position_id) or position
                data_quality = (position.get("config") or {}).get("data_quality") or {}
                first_degraded = parse_iso(data_quality.get("first_degraded_at"))
                degraded_age = (
                    (self.clock.now().astimezone(UTC) - first_degraded).total_seconds()
                    if first_degraded is not None
                    else 0.0
                )
                if degraded_age > OPEN_CAPTURE_DEGRADED_HARD_STALE_SECONDS:
                    last_valid_route = (position.get("config") or {}).get(
                        "last_valid_executable_route"
                    )
                    if last_valid_route:
                        crossed = self.synchronized_runtime.mark_settlement_crossed(
                            position,
                            self.clock.now(),
                        )
                        if crossed is not None:
                            self.record_event(
                                "settlement_crossed",
                                (
                                    f"V2 SETTLEMENT CROSSED {position.get('canonical_asset')} "
                                    f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                                    "Funding reconciliation obligation was stored before hard-stale exit."
                                ),
                                {"position": position, "cycle": crossed},
                                route_key=route_key,
                                notify=True,
                            )
                            outcomes.append("settlement_crossed")
                            position = self.store.funding_capture_position_by_id(position_id) or position
                        close_payload = self.synchronized_runtime.close_position(
                            position,
                            last_valid_route,
                            self.clock.now(),
                            reason="targeted_refresh_hard_stale",
                            emergency=True,
                        )
                        if close_payload.get("decision") == "closed":
                            self.record_event(
                                "close",
                                (
                                    f"V2 HARD STALE EXIT {position.get('canonical_asset')} "
                                    f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                                    "Reason: targeted refresh unavailable >5s; used last valid executable snapshot."
                                ),
                                {
                                    "position": position,
                                    "refresh": refreshed.as_dict() if refreshed else None,
                                    "close": close_payload,
                                },
                                route_key=route_key,
                                notify=True,
                                severity="error",
                            )
                            outcomes.append("emergency_unwind")
                            continue
                        outcomes.append("close_failed")
                        continue
                self.synchronized_runtime.record_current_executable_pnl(
                    position,
                    None,
                    self.clock.now(),
                )
                continue
            current_pnl = self.synchronized_runtime.record_current_executable_pnl(
                position,
                live_route,
                now,
            )
            if current_pnl.get("decision") == "recorded":
                position = {
                    **position,
                    "paper_net_pnl_estimated": current_pnl.get("paper_net_if_exit_now"),
                }
            crossed = None
            if state in {
                "OPEN",
                "HOLDING_NEXT_CYCLE",
                "SETTLEMENT_CROSSED",
                "POST_SETTLEMENT_EVALUATION",
            }:
                crossed = self.synchronized_runtime.mark_settlement_crossed(position, now)
                if crossed is not None:
                    self.record_event(
                        "settlement_crossed",
                        (
                            f"V2 SETTLEMENT CROSSED {position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            "Funding reconciliation is pending public rate + mark."
                        ),
                        {"position": position, "cycle": crossed},
                        route_key=route_key,
                        notify=True,
                    )
                    outcomes.append("settlement_crossed")
                    position = self.store.funding_capture_position_by_id(position_id) or position
                    state = str(position.get("state") or state)
            risk_exit = self.synchronized_runtime.poll_synchronized_position_risk(
                position,
                live_route,
                current_pnl,
                None,
                now,
            )
            if risk_exit is not None:
                self.store.update_funding_capture_position_state(
                    position_id,
                    "EXIT_SUBMITTED",
                    now,
                )
                close_payload = self.synchronized_runtime.close_position(
                    position,
                    live_route,
                    now,
                    reason=str(risk_exit.get("close_reason") or "hard_risk"),
                    emergency=True,
                )
                if close_payload.get("decision") != "closed":
                    self.store.update_funding_capture_position_state(
                        position_id,
                        "EMERGENCY_UNWIND",
                        now,
                    )
                    self.record_event(
                        "close_failed",
                        (
                            f"V2 RISK EXIT FAILED {position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            f"Risk reason: {risk_exit['close_reason']}\n"
                            f"Close reason: {close_payload.get('reason')}\n"
                            "Position remains actionable for retry."
                        ),
                        {
                            "position": position,
                            "risk_engine": risk_exit,
                            "close": close_payload,
                        },
                        route_key=route_key,
                        notify=True,
                        severity="error",
                    )
                    outcomes.append("close_failed")
                    continue
                self.record_event(
                    "close",
                    (
                        f"V2 RISK EXIT {position.get('canonical_asset')} "
                        f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                        f"Reason: {risk_exit['close_reason']}\n"
                        f"Price PnL: {format_signed_money(close_payload.get('paper_price_pnl'))}\n"
                        f"Emergency cost: {format_signed_money(close_payload.get('paper_emergency_unwind_cost'))}\n"
                        f"State: {close_payload.get('state')}"
                    ),
                    {
                        "position": position,
                        "risk_engine": risk_exit,
                        "close": close_payload,
                    },
                    route_key=route_key,
                    notify=True,
                    severity="error",
                )
                outcomes.append("emergency_unwind")
                continue
            if crossed is not None:
                continue
            if state in {"SETTLEMENT_CROSSED", "POST_SETTLEMENT_EVALUATION"}:
                decision = self.synchronized_runtime.next_cycle_hold_or_close_decision(
                    position,
                    live_route,
                    now,
                )
                if decision["decision"] == "wait":
                    continue
                if decision["decision"] == "hold":
                    self.record_event(
                        "hold",
                        (
                            f"V2 HOLD NEXT CYCLE {position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            f"Reason: {decision['reason']}\n"
                            f"Incremental net: "
                            f"{format_signed_money((decision.get('hold_economics') or {}).get('incremental_hold_net_pnl'))}"
                        ),
                        {"position": position, "decision": decision},
                        route_key=route_key,
                        notify=True,
                    )
                    outcomes.append("hold")
                    continue
                close_payload = self.synchronized_runtime.close_position(
                    position,
                    live_route,
                    now,
                    reason=str(decision.get("reason") or "post_settlement_close"),
                )
                if close_payload.get("decision") != "closed":
                    self.record_event(
                        "close_skipped",
                        (
                            f"V2 CLOSE SKIPPED {position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            f"Reason: {close_payload.get('reason')}"
                        ),
                        {"position": position, "decision": decision, "close": close_payload},
                        route_key=route_key,
                        notify=True,
                        severity="warning",
                    )
                    outcomes.append("close_rejected")
                    continue
                self.record_event(
                    "close",
                    (
                        f"V2 CLOSE {position.get('canonical_asset')} "
                        f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                        f"Reason: {close_payload['reason']}\n"
                        f"Price PnL: {format_signed_money(close_payload.get('paper_price_pnl'))}\n"
                        f"Estimated net without unreconciled funding: "
                        f"{format_signed_money(close_payload.get('paper_net_if_exit_now'))}"
                    ),
                    {"position": position, "decision": decision, "close": close_payload},
                    route_key=route_key,
                    notify=True,
                    severity="warning",
                )
                outcomes.append("closed")
        return outcomes

    def _legacy_like_position_from_capture(self, position: dict[str, Any]) -> dict[str, Any]:
        config_json = position.get("config") or {}
        entry_legs = list(config_json.get("entry_legs") or [])
        return {
            "funding_paper_position_id": 0,
            "route_key": config_json.get("route_key"),
            "canonical_asset": position.get("canonical_asset"),
            "long_venue": position.get("long_venue"),
            "long_symbol": position.get("long_symbol"),
            "short_venue": position.get("short_venue"),
            "short_symbol": position.get("short_symbol"),
            "base_quantity": position.get("quantity"),
            "target_notional": position.get("target_notional"),
            "long_notional": position.get("target_notional"),
            "short_notional": position.get("target_notional"),
            "long_settlement_at": position.get("current_cycle_scheduled_funding_at"),
            "short_settlement_at": position.get("current_cycle_scheduled_funding_at"),
            "max_settlement_at": position.get("current_cycle_scheduled_funding_at"),
            "opened_at": position.get("opened_at"),
            "expected_execution_cost": position.get("paper_open_fees"),
            "entry_cross_spread": position.get("original_entry_spread"),
            "entry_legs": entry_legs,
            "notes": {
                "paper_model": "synchronized_funding_capture_v2",
                "accrued_funding_pnl": 0.0,
                "strategy": {"strategy_name": "synchronized_funding_capture"},
            },
        }

    def process_legacy_open_positions(self) -> list[str]:
        outcomes: list[str] = []
        now = self.clock.now()
        for position in self.store.funding_paper_open_positions():
            self.refresh_open_position_route(position)
            now = datetime.now(UTC)
            position_id = int(position["funding_paper_position_id"])
            route_key = str(position.get("route_key") or "")
            live_route = self.hot_routes.get(route_key) or (
                self.store.latest_funding_route_by_key(route_key)
                if route_key
                else None
            )
            price_snap = compute_price_move_snapshot(position, live_route)
            price_telemetry = common_price_move_telemetry(
                price_snap,
                alert_fraction=self.config.common_price_move_alert_fraction,
                critical_fraction=self.config.common_price_move_critical_fraction,
            )
            if price_telemetry["level"] in {"warning", "critical"}:
                alert_key = (position_id, str(price_telemetry["level"]))
                if alert_key not in self.price_move_alerted_positions:
                    self.price_move_alerted_positions.add(alert_key)
                    self.record_event(
                        "price_move_alert",
                        (
                            f"COMMON PRICE MOVE {str(price_telemetry['level']).upper()} "
                            f"{position.get('canonical_asset')} "
                            f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                            "Action: no automatic close; fresh risk recalculation required.\n"
                            f"Long move: {float(price_telemetry.get('long_move_fraction') or 0.0) * 100:.2f}%\n"
                            f"Short move: {float(price_telemetry.get('short_move_fraction') or 0.0) * 100:.2f}%"
                        ),
                        {
                            "position": position_summary(position),
                            "price_move_snapshot": price_snap,
                            "price_move_telemetry": price_telemetry,
                        },
                        funding_paper_position_id=position_id,
                        route_key=position.get("route_key"),
                        notify=True,
                        severity=(
                            "warning"
                            if price_telemetry["level"] == "warning"
                            else "error"
                        ),
                    )
            if self.config.spread_monitoring_enabled:
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
            self._detect_settlement_crossing(position, now)
            # --- Risk engine integration ---
            risk_exit = self._poll_position_risk(position, live_route, now)
            if risk_exit is not None:
                close_payload = build_close_payload(
                    position,
                    settlement_rates_for_position(position, self.store),
                    live_route,
                    use_entry_estimate_for_missing=True,
                    close_reason=risk_exit["close_reason"],
                    hold_decision={
                        "hold": False,
                        "close_reason": risk_exit["close_reason"],
                        "reasons": risk_exit.get("reasons", []),
                        "risk_engine": True,
                    },
                )
                close_payload["risk_engine"] = risk_exit
                self.store.close_funding_paper_position(position_id, close_payload)
                self.record_event(
                    "close",
                    (
                        f"RISK EXIT {position.get('canonical_asset')} "
                        f"{position.get('long_venue')}/{position.get('short_venue')}\n"
                        f"Reason: {risk_exit['close_reason']}"
                    ),
                    {
                        "position": position_summary(position),
                        "close": close_payload,
                        "risk_engine": risk_exit,
                    },
                    funding_paper_position_id=position_id,
                    route_key=position.get("route_key"),
                    notify=True,
                    severity="error",
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

def route_is_older_than(
    incoming: dict[str, Any],
    existing: dict[str, Any],
) -> bool:
    incoming_at = route_observed_datetime(incoming)
    existing_at = route_observed_datetime(existing)
    if incoming_at is None or existing_at is None:
        return False
    return incoming_at < existing_at

def route_observed_datetime(route: dict[str, Any]) -> datetime | None:
    observed_at = parse_iso(route.get("observed_at"))
    if observed_at is not None:
        return observed_at
    focused = (route.get("evidence") or {}).get("focused_recheck") or {}
    return parse_iso(focused.get("observed_at"))

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
    environment: str = "auto",
    fast: bool = False,
    timeout_seconds: float | None = None,
) -> FundingVenueClient | None:
    venue_key = str(venue).lower()
    environment_key = str(environment or "mainnet").strip().lower()
    if environment_key == "auto":
        environment_key = "testnet" if venue_key == "risex" else "mainnet"
    if environment_key not in {"mainnet", "testnet"}:
        return None
    if venue_key in DEACTIVATED_FUNDING_VENUES:
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
        "nado": NadoFundingClient,
        "okx": OKXFundingClient,
        "pacifica": PacificaFundingClient,
        "paradex": ParadexFundingClient,
        "phemex": PhemexFundingClient,
        "reya": ReyaFundingClient,
        "risex": RiseXFundingClient,
        "vertex_base": VertexFundingClient,
        "woox": WOOXFundingClient,
    }
    factory = factories.get(venue_key)
    if factory is None:
        return None
    if venue_key != "risex" and environment_key != "mainnet":
        return None
    if venue_key == "risex" and environment_key == "mainnet":
        return None
    if not fast:
        if venue_key == "risex":
            return factory(environment=environment_key)
        return factory()
    try:
        kwargs: dict[str, Any] = {
            "http": FundingHttpClient(
                timeout_seconds=float(timeout_seconds or 3),
                max_retries=0,
                min_delay_seconds=0.0,
            )
        }
        if venue_key == "risex":
            kwargs["environment"] = environment_key
        return factory(**kwargs)
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
    selected = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    ) or {}
    return {
        "funding_scan_id": route.get("funding_scan_id"),
        "funding_route_id": route.get("funding_route_id"),
        "route_key": route.get("route_key"),
        "canonical_asset": route.get("canonical_asset"),
        "long": f"{route.get('long_venue')} {route.get('long_symbol')}",
        "short": f"{route.get('short_venue')} {route.get('short_symbol')}",
        "target_notional": route.get("target_notional"),
        "market_capacity": route.get("market_capacity"),
        "strategy_name": selected.get("strategy_name") or selected.get("strategy_class"),
        "current_nowcast_net": selected.get("expected_net_pnl")
        if selected.get("expected_net_pnl") is not None
        else evidence.get("current_nowcast_net"),
        "current_nowcast_gross": evidence.get("current_nowcast_gross"),
        "funding_pnl_component": selected.get("funding_pnl_component"),
        "spread_pnl_component": selected.get("spread_pnl_component"),
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
