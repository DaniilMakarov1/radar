from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html import escape as html_escape
from pathlib import Path
from types import FrameType
from typing import Any

from smart_money_radar.config import DEFAULT_DB_PATH
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.prediction.service import PredictionScanConfig, run_prediction_scan
from smart_money_radar.storage import SQLiteStore, utc_now_iso


PREDICTION_BOT_RETENTION_SCANS = 1
PREDICTION_BOT_DASHBOARD_FRESHNESS_MINUTES = 15.0

PredictionScanRunner = Callable[..., dict[str, Any]]
PredictionEventWriter = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class PredictionBotConfig:
    scan_interval_seconds: int = 300
    status_report_interval_seconds: int = 3_600
    status_report_max_routes: int = 5
    events_per_venue: int = 75
    max_markets_per_venue: int = 1_500
    kalshi_market_pages: int = 10
    http_timeout_seconds: int = 8
    http_max_retries: int = 1
    paper_size: float = 100.0
    paper_latency_ms: int = 750
    paper_depth_haircut: float = 0.8
    collect_hyperliquid: bool = True
    iterations: int | None = None
    telegram_enabled: bool = True
    lifecycle_telegram_enabled: bool = False

    def validated(self) -> "PredictionBotConfig":
        return PredictionBotConfig(
            scan_interval_seconds=max(30, int(self.scan_interval_seconds)),
            status_report_interval_seconds=max(
                0,
                min(int(self.status_report_interval_seconds), 86_400),
            ),
            status_report_max_routes=max(
                1,
                min(int(self.status_report_max_routes), 20),
            ),
            events_per_venue=max(1, min(int(self.events_per_venue), 100)),
            max_markets_per_venue=max(1, min(int(self.max_markets_per_venue), 2_000)),
            kalshi_market_pages=max(1, min(int(self.kalshi_market_pages), 20)),
            http_timeout_seconds=max(1, min(int(self.http_timeout_seconds), 60)),
            http_max_retries=max(0, min(int(self.http_max_retries), 5)),
            paper_size=max(1.0, float(self.paper_size)),
            paper_latency_ms=max(0, int(self.paper_latency_ms)),
            paper_depth_haircut=max(0.0, min(float(self.paper_depth_haircut), 1.0)),
            collect_hyperliquid=bool(self.collect_hyperliquid),
            iterations=(
                None
                if self.iterations is None
                else max(1, int(self.iterations))
            ),
            telegram_enabled=bool(self.telegram_enabled),
            lifecycle_telegram_enabled=bool(self.lifecycle_telegram_enabled),
        )


class PredictionRadarBot:
    def __init__(
        self,
        store: SQLiteStore,
        config: PredictionBotConfig | None = None,
        notifier: TelegramNotifier | None = None,
        scan_runner: PredictionScanRunner | None = None,
        event_writer: PredictionEventWriter | None = None,
    ) -> None:
        self.store = store
        self.config = (config or PredictionBotConfig()).validated()
        self.notifier = notifier or TelegramNotifier()
        self.scan_runner = scan_runner or run_prediction_scan
        self.event_writer = event_writer or write_prediction_event
        self.last_status_report_monotonic = 0.0
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.shutdown_notified = False
        self.last_iteration_result: dict[str, Any] | None = None

    def run_loop(self) -> None:
        completed = 0
        previous_signal_handlers = self.install_signal_handlers()
        try:
            self.record_event(
                "bot_started",
                prediction_started_message(self.config),
                {"config": serializable_config(self.config)},
                notify=self.config.lifecycle_telegram_enabled,
            )
            while (
                not self.stop_requested
                and (self.config.iterations is None or completed < self.config.iterations)
            ):
                iteration_started = time.monotonic()
                try:
                    result = self.run_iteration()
                except Exception as exc:
                    self.notify_shutdown(
                        "bot_crashed",
                        (
                            "<b>Prediction Radar Bot CRASHED</b>\n\n"
                            f"Error: <code>{tg(type(exc).__name__)}</code>: {tg(exc)}\n"
                            "Scan loop is not running."
                        ),
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "last_iteration": self.last_iteration_result,
                        },
                        severity="error",
                        notify=True,
                    )
                    raise
                self.last_iteration_result = result
                completed += 1
                if self.config.iterations is not None and completed >= self.config.iterations:
                    break
                elapsed = time.monotonic() - iteration_started
                self.sleep_interruptibly(
                    max(0, int(self.config.scan_interval_seconds - elapsed))
                )
            if self.stop_requested:
                self.notify_shutdown(
                    "bot_stopped",
                    (
                        "<b>Prediction Radar Bot STOPPED</b>\n\n"
                        f"Reason: {tg(self.stop_reason or 'stop requested')}\n"
                        "Scan loop is not running."
                    ),
                    {
                        "reason": self.stop_reason,
                        "completed_iterations": completed,
                        "last_iteration": self.last_iteration_result,
                    },
                    severity="warning",
                    notify=self.config.lifecycle_telegram_enabled,
                )
        except KeyboardInterrupt:
            self.request_stop("KeyboardInterrupt")
            self.notify_shutdown(
                "bot_interrupted",
                (
                    "<b>Prediction Radar Bot INTERRUPTED</b>\n\n"
                    "Reason: KeyboardInterrupt\n"
                    "Scan loop is not running."
                ),
                {
                    "reason": "KeyboardInterrupt",
                    "completed_iterations": completed,
                    "last_iteration": self.last_iteration_result,
                },
                severity="warning",
                notify=self.config.lifecycle_telegram_enabled,
            )
            raise
        finally:
            self.restore_signal_handlers(previous_signal_handlers)

    def run_iteration(self) -> dict[str, Any]:
        self.store.init_db()
        scan_result = self.scan_runner(self.store, config=self.scan_config())
        dashboard = self.store.prediction_dashboard(
            route_limit=self.config.status_report_max_routes
        )
        candidate_summary = dashboard.get("candidate_summary") or {}
        backlog_summary = dashboard.get("backlog_summary") or {}
        latest_scan = dashboard.get("latest_scan") or {}
        result = {
            "mode": "full_market",
            "prediction_scan_id": scan_result.get("prediction_scan_id")
            or latest_scan.get("prediction_scan_id"),
            "status": scan_result.get("status") or latest_scan.get("status"),
            "candidate_count": int(candidate_summary.get("candidate_count") or 0),
            "visible_route_count": len(dashboard.get("routes") or []),
            "visible_candidate_count": len(dashboard.get("route_candidates") or []),
            "near_zero_route_count": len(dashboard.get("near_zero_routes") or []),
            "candidate_backlog_count": int(
                backlog_summary.get("candidate_backlog_count") or 0
            ),
            "active_candidate_backlog_count": int(
                backlog_summary.get("active_candidate_count") or 0
            ),
            "trade_backlog_count": int(
                backlog_summary.get("trade_backlog_count") or 0
            ),
            "market_count": scan_result.get("market_count", latest_scan.get("market_count")),
            "orderbook_count": scan_result.get(
                "orderbook_count",
                latest_scan.get("orderbook_count"),
            ),
            "route_count": scan_result.get("route_count", latest_scan.get("route_count")),
            "executable_route_count": scan_result.get(
                "executable_route_count",
                latest_scan.get("executable_route_count"),
            ),
            "paper_execution_count": scan_result.get("paper_execution_count", 0),
            "paper_checked_route_count": scan_result.get(
                "paper_checked_route_count",
                0,
            ),
            "paper_simulation_count": scan_result.get("paper_simulation_count", 0),
            "paper_trade_count": scan_result.get("paper_trade_count", 0),
            "hyperliquid_event_count": scan_result.get("hyperliquid_event_count", 0),
            "hyperliquid_market_count": scan_result.get("hyperliquid_market_count", 0),
            "hyperliquid_orderbook_count": scan_result.get(
                "hyperliquid_orderbook_count",
                0,
            ),
            "wallet_score_count": scan_result.get("wallet_score_count", 0),
            "token_link_count": scan_result.get("token_link_count", 0),
            "warnings": list(scan_result.get("warnings") or []),
        }
        self.record_event(
            "scan",
            (
                "Prediction Radar scan "
                f"{result.get('prediction_scan_id')}: "
                f"candidates={result['candidate_count']}, "
                f"routes={result.get('route_count') or 0}, "
                f"orderbooks={result.get('orderbook_count') or 0}, "
                f"paper_trades={result.get('paper_trade_count') or 0}"
            ),
            result,
            notify=False,
        )
        self.maybe_send_status_report(result, dashboard)
        return result

    def scan_config(self) -> PredictionScanConfig:
        return PredictionScanConfig(
            events_per_venue=self.config.events_per_venue,
            max_markets_per_venue=self.config.max_markets_per_venue,
            kalshi_market_pages=self.config.kalshi_market_pages,
            http_timeout_seconds=self.config.http_timeout_seconds,
            http_max_retries=self.config.http_max_retries,
            wallet_limit=0,
            wallet_position_limit=0,
            wallet_refresh_hours=24.0,
            paper_size=self.config.paper_size,
            paper_latency_ms=self.config.paper_latency_ms,
            paper_depth_haircut=self.config.paper_depth_haircut,
            retention_scans=PREDICTION_BOT_RETENTION_SCANS,
            dashboard_freshness_minutes=PREDICTION_BOT_DASHBOARD_FRESHNESS_MINUTES,
            collect_wallets=False,
            collect_hyperliquid=self.config.collect_hyperliquid,
        )

    def maybe_send_status_report(
        self,
        result: dict[str, Any],
        dashboard: dict[str, Any],
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
        self.record_event(
            "status_report",
            prediction_status_message(result, dashboard, self.config),
            {
                "result": result,
                "latest_scan": dashboard.get("latest_scan") or {},
                "candidate_summary": dashboard.get("candidate_summary") or {},
                "top_candidates": [
                    prediction_route_summary(row)
                    for row in (dashboard.get("route_candidates") or [])
                ],
                "top_routes": [
                    prediction_route_summary(row)
                    for row in (dashboard.get("routes") or [])
                ],
                "top_near_zero_routes": [
                    prediction_route_summary(row)
                    for row in (dashboard.get("near_zero_routes") or [])
                ],
                "backlog_summary": dashboard.get("backlog_summary") or {},
            },
            notify=True,
        )

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        self.stop_reason = self.stop_reason or reason

    def install_signal_handlers(self) -> dict[int, Any]:
        handlers: dict[int, Any] = {}
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                handlers[int(stop_signal)] = signal.getsignal(stop_signal)
                signal.signal(stop_signal, self.handle_stop_signal)
            except (ValueError, OSError):
                continue
        return handlers

    def restore_signal_handlers(self, handlers: dict[int, Any]) -> None:
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    def handle_stop_signal(
        self,
        signum: int,
        frame: FrameType | None,
    ) -> None:
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

    def notify_shutdown(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        *,
        severity: str,
        notify: bool,
    ) -> None:
        if self.shutdown_notified:
            return
        self.shutdown_notified = True
        self.record_event(
            event_type,
            message,
            payload,
            notify=notify,
            severity=severity,
        )

    def record_event(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        *,
        notify: bool,
        severity: str = "info",
    ) -> None:
        log_payload = {
            "created_at": utc_now_iso(),
            "event_type": event_type,
            "severity": severity,
            "payload": payload,
        }
        self.event_writer(log_payload)
        if notify and self.config.telegram_enabled:
            result = self.notifier.send(message)
            notifier_payload = {
                "created_at": utc_now_iso(),
                "event_type": f"{event_type}_telegram",
                "severity": "info" if result.status == "sent" else "warning",
                "telegram_status": result.status,
                "telegram_error": result.error,
            }
            self.event_writer(notifier_payload)


def write_prediction_event(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def prediction_started_message(config: PredictionBotConfig) -> str:
    return (
        "<b>Prediction Radar Bot STARTED</b>\n\n"
        f"Scan interval: <b>{int(config.scan_interval_seconds) // 60} min</b>\n"
        f"Telegram status: <b>{int(config.status_report_interval_seconds) // 60} min</b>\n"
        "Storage: <code>latest prediction scan only</code>"
    )


def prediction_status_message(
    result: dict[str, Any],
    dashboard: dict[str, Any],
    config: PredictionBotConfig,
) -> str:
    latest_scan = dashboard.get("latest_scan") or {}
    candidate_summary = dashboard.get("candidate_summary") or {}
    backlog_summary = dashboard.get("backlog_summary") or {}
    candidates = list(dashboard.get("route_candidates") or [])
    routes = list(dashboard.get("routes") or [])
    candidate_count = int(candidate_summary.get("candidate_count") or 0)
    scan_id = result.get("prediction_scan_id") or latest_scan.get("prediction_scan_id")
    freshness = latest_scan.get("freshness_status") or "unknown"
    market_count = value_or(latest_scan.get("market_count"), result.get("market_count"), 0)
    orderbook_count = value_or(
        latest_scan.get("orderbook_count"),
        result.get("orderbook_count"),
        0,
    )
    route_count = value_or(latest_scan.get("route_count"), result.get("route_count"), 0)
    executable_count = value_or(
        latest_scan.get("executable_route_count"),
        result.get("executable_route_count"),
        0,
    )
    paper_checked_count = int(result.get("paper_checked_route_count") or 0)
    paper_simulation_count = int(result.get("paper_simulation_count") or 0)
    paper_trade_count = int(result.get("paper_trade_count") or 0)
    hyperliquid_market_count = int(result.get("hyperliquid_market_count") or 0)
    lines = [
        "<b>Prediction Radar STATUS</b>",
        "",
        (
            f"<b>Scan:</b> <code>{tg(scan_id or '-')}</code> | "
            f"<b>Freshness:</b> <code>{tg(freshness)}</code>"
        ),
        (
            f"<b>Candidates:</b> {candidate_count} | "
            f"Paper routes: {len(routes)} | Books: {int(orderbook_count or 0)}"
        ),
        (
            f"Markets: {int(market_count or 0)} | "
            f"Routes screened: {int(route_count or 0)} | "
            f"Executable: {int(executable_count or 0)}"
        ),
        (
            f"Paper recheck: {paper_checked_count} routes | "
            f"fills: {paper_trade_count}/{paper_simulation_count}"
        ),
        f"Hyperliquid HIP-4: {hyperliquid_market_count} markets",
        (
            f"Backlog: active "
            f"{int(backlog_summary.get('active_candidate_count') or 0)}/"
            f"{int(backlog_summary.get('candidate_backlog_count') or 0)} candidates | "
            f"trades {int(backlog_summary.get('trade_backlog_count') or 0)}"
        ),
    ]
    warnings = list(result.get("warnings") or [])
    if warnings:
        lines.extend(["", "<b>Warnings</b>"])
        lines.extend(f"- {tg(shorten(warning, 180))}" for warning in warnings[:3])
        if len(warnings) > 3:
            lines.append(f"<i>...and {len(warnings) - 3} more</i>")
    max_routes = int(config.status_report_max_routes)
    if candidates:
        lines.extend(["", "<b>Top candidates</b>"])
        lines.extend(
            prediction_status_route_line(row, index)
            for index, row in enumerate(candidates[:max_routes], start=1)
        )
        hidden = candidate_count - min(candidate_count, max_routes)
        if hidden > 0:
            lines.append(f"<i>...and {hidden} more</i>")
    elif routes:
        lines.extend(["", "<b>Top paper routes</b>"])
        lines.extend(
            prediction_status_route_line(row, index)
            for index, row in enumerate(routes[:max_routes], start=1)
        )
    else:
        lines.extend(["", "<b>Кандидатов нет.</b>"])
    return "\n".join(lines)


def prediction_status_route_line(row: dict[str, Any], index: int) -> str:
    source = prediction_source_anchor(row)
    status = row.get("candidate_status") or row.get("status") or row.get("route_status")
    return (
        f"\n<b>{index}. {tg(shorten(row.get('title') or '-', 120))}</b>\n"
        f"{tg(prediction_route_type_name(row.get('route_type')))} | "
        f"<code>{tg(prediction_venue_name(row.get('venue_scope')))}</code> | "
        f"{tg(status or '-')}\n"
        f"Net PnL: <b>{format_money(row.get('expected_net_profit'))}</b> | "
        f"Edge: {format_pct(row.get('net_edge_per_share'))} | "
        f"Size: {format_shares(row.get('optimal_size'))}\n"
        f"{source}"
    )


def prediction_status_near_zero_line(row: dict[str, Any], index: int) -> str:
    source = prediction_source_anchor(row)
    missing_edge = row.get("missing_net_edge_per_share")
    if missing_edge is None:
        try:
            missing_edge = max(0.0, -float(row.get("net_edge_per_share") or 0.0))
        except (TypeError, ValueError):
            missing_edge = None
    return (
        f"\n<b>{index}. {tg(shorten(row.get('title') or '-', 120))}</b>\n"
        f"{tg(prediction_route_type_name(row.get('route_type')))} | "
        f"<code>{tg(prediction_venue_name(row.get('venue_scope')))}</code> | "
        "watch only\n"
        f"Current PnL: <b>{format_money(row.get('expected_net_profit'))}</b> | "
        f"Missing edge: {format_pct(missing_edge)} | "
        f"Size: {format_shares(row.get('optimal_size'))}\n"
        f"{source}"
    )


def prediction_route_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_key": row.get("route_key"),
        "route_type": row.get("route_type"),
        "venue_scope": row.get("venue_scope"),
        "title": row.get("title"),
        "status": row.get("candidate_status") or row.get("status"),
        "expected_net_profit": row.get("expected_net_profit"),
        "net_edge_per_share": row.get("net_edge_per_share"),
        "missing_net_edge_per_share": row.get("missing_net_edge_per_share"),
        "optimal_size": row.get("optimal_size"),
        "source_url": row.get("source_url"),
    }


def prediction_source_anchor(row: dict[str, Any]) -> str:
    url = row.get("source_url")
    if not url:
        return "Source: -"
    label = f"Open {prediction_venue_name(row.get('venue_scope'))}"
    return f'Source: <a href="{tg_attr(url)}">{tg(label)}</a>'


def prediction_route_type_name(value: Any) -> str:
    return {
        "binary_complement": "Binary complement",
        "complete_set": "Complete set",
        "complete_set_no": "Complete-set NO",
        "negative_risk_conversion": "Negative risk",
        "logical_implication": "Implication",
        "cross_venue_complement": "Cross-venue",
        "threshold_ladder": "Threshold ladder",
    }.get(str(value or ""), str(value or "Route"))


def prediction_venue_name(value: Any) -> str:
    return {
        "hyperliquid_hip4": "Hyperliquid HIP-4",
        "hyperliquid_hip4+kalshi": "Hyperliquid HIP-4 + Kalshi",
        "hyperliquid_hip4+polymarket": "Hyperliquid HIP-4 + Polymarket",
        "hyperliquid_hip4+kalshi+polymarket": "Hyperliquid HIP-4 + Kalshi + Polymarket",
        "polymarket": "Polymarket",
        "kalshi": "Kalshi",
        "polymarket+kalshi": "Polymarket + Kalshi",
    }.get(str(value or ""), str(value or "-"))


def value_or(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def shorten(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def format_money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def format_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def format_shares(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.1f}"


def tg(value: Any) -> str:
    if value is None:
        return "-"
    return html_escape(str(value), quote=False)


def tg_attr(value: Any) -> str:
    if value is None:
        return ""
    return html_escape(str(value), quote=True)


def serializable_config(config: PredictionBotConfig) -> dict[str, Any]:
    return asdict(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m smart_money_radar.prediction.bot",
        description=(
            "Run Prediction Radar scans on a fixed cadence and send Telegram "
            "status reports for the prediction module only."
        ),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--scan-interval-seconds", type=int, default=300)
    parser.add_argument("--status-report-interval-seconds", type=int, default=3_600)
    parser.add_argument("--status-report-max-routes", type=int, default=5)
    parser.add_argument("--events-per-venue", type=int, default=24)
    parser.add_argument("--max-markets-per-venue", type=int, default=500)
    parser.add_argument("--kalshi-market-pages", type=int, default=5)
    parser.add_argument("--http-timeout-seconds", type=int, default=8)
    parser.add_argument("--http-max-retries", type=int, default=1)
    parser.add_argument("--paper-size", type=float, default=100.0)
    parser.add_argument("--paper-latency-ms", type=int, default=750)
    parser.add_argument("--paper-depth-haircut", type=float, default=0.8)
    parser.add_argument("--skip-hyperliquid", action="store_true")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument(
        "--lifecycle-telegram",
        action="store_true",
        help=(
            "Also send normal start/stop notifications. Crash notifications and "
            "hourly status reports are controlled by --no-telegram."
        ),
    )
    return parser


def config_from_args(args: argparse.Namespace) -> PredictionBotConfig:
    return PredictionBotConfig(
        scan_interval_seconds=args.scan_interval_seconds,
        status_report_interval_seconds=args.status_report_interval_seconds,
        status_report_max_routes=args.status_report_max_routes,
        events_per_venue=args.events_per_venue,
        max_markets_per_venue=args.max_markets_per_venue,
        kalshi_market_pages=args.kalshi_market_pages,
        http_timeout_seconds=args.http_timeout_seconds,
        http_max_retries=args.http_max_retries,
        paper_size=args.paper_size,
        paper_latency_ms=args.paper_latency_ms,
        paper_depth_haircut=args.paper_depth_haircut,
        collect_hyperliquid=not args.skip_hyperliquid,
        iterations=args.iterations,
        telegram_enabled=not args.no_telegram,
        lifecycle_telegram_enabled=args.lifecycle_telegram,
    ).validated()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteStore(Path(args.db))
    store.init_db()
    bot = PredictionRadarBot(
        store,
        config=config_from_args(args),
        notifier=TelegramNotifier(
            token_env_var="PREDICTION_TELEGRAM_BOT_TOKEN",
            chat_id_env_var="PREDICTION_TELEGRAM_CHAT_ID",
        ),
    )
    bot.run_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
