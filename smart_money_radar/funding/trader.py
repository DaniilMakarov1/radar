from __future__ import annotations

import csv
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.funding.adapters import (
    AsterFundingClient,
    BackpackFundingClient,
    BinanceFundingClient,
    BingXFundingClient,
    BitgetFundingClient,
    BybitFundingClient,
    DeribitFundingClient,
    DriftFundingClient,
    DydxFundingClient,
    EtherealFundingClient,
    ExtendedFundingClient,
    FundingVenueClient,
    GateFundingClient,
    HTXFundingClient,
    HyperliquidFundingClient,
    KrakenFundingClient,
    KuCoinFundingClient,
    LighterFundingClient,
    MEXCFundingClient,
    OKXFundingClient,
    ParadexFundingClient,
    VertexFundingClient,
)
from smart_money_radar.funding.adapters.base import FundingDataError, FundingHttpClient
from smart_money_radar.funding.economics import evaluate_perp_route
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import (
    normalize_orderbook_canonical_units,
    normalize_stored_orderbook_units,
)
from smart_money_radar.funding.service import run_funding_scan
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore, utc_now_iso


@dataclass(frozen=True)
class FundingPaperTraderConfig:
    venue_starting_balance: float = 1_000.0
    target_notional_per_leg: float = 500.0
    entry_window_seconds: int = 180
    entry_min_lead_seconds: int = 30
    entry_max_lead_seconds: int = 60
    arm_window_seconds: int = 900
    settlement_grace_seconds: int = 90
    max_settlement_publication_lag_seconds: int = 300
    collateral_reserve_fraction: float = 0.10
    min_live_net_profit: float = 0.0
    scan_interval_seconds: int = 60
    hot_interval_seconds: int = 10
    status_report_interval_seconds: int = 1_800
    status_report_max_routes: int = 5
    iterations: int | None = None
    export_dir: Path = PROJECT_ROOT / "exports" / "funding_paper"
    telegram_enabled: bool = True
    close_after_first_settlement_pair: bool = True
    focused_recheck_enabled: bool = True

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
                15,
                min(int(self.entry_max_lead_seconds), 900),
            ),
            arm_window_seconds=max(
                int(self.entry_window_seconds),
                min(int(self.arm_window_seconds), 7_200),
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
            hot_interval_seconds=max(5, int(self.hot_interval_seconds)),
            status_report_interval_seconds=max(
                0,
                min(int(self.status_report_interval_seconds), 86_400),
            ),
            status_report_max_routes=max(
                1,
                min(int(self.status_report_max_routes), 20),
            ),
            iterations=(
                None
                if self.iterations is None
                else max(1, int(self.iterations))
            ),
            export_dir=Path(self.export_dir),
            telegram_enabled=bool(self.telegram_enabled),
            close_after_first_settlement_pair=bool(
                self.close_after_first_settlement_pair
            ),
            focused_recheck_enabled=bool(self.focused_recheck_enabled),
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


class FundingPaperTrader:
    def __init__(
        self,
        store: SQLiteStore,
        config: FundingPaperTraderConfig | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.store = store
        self.config = (config or FundingPaperTraderConfig()).validated()
        self.notifier = notifier or TelegramNotifier()
        self.armed_routes: set[str] = set()
        self.hot_routes: dict[str, dict[str, Any]] = {}
        self.last_full_scan_monotonic = 0.0
        self.last_status_report_monotonic = 0.0
        self.pending_notified_positions: set[int] = set()
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.shutdown_notified = False
        self.last_iteration_result: dict[str, Any] | None = None

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
        if not self.hot_routes or self.last_full_scan_monotonic <= 0:
            return False
        elapsed = time.monotonic() - self.last_full_scan_monotonic
        return elapsed < self.config.scan_interval_seconds

    def run_full_iteration(self) -> dict[str, Any]:
        self.store.init_db()
        scan_result = run_funding_scan(
            self.store,
            config=self.scan_config(),
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
        snapshot = self.store.record_funding_paper_equity_snapshot()
        export_funding_paper_csv(self.store, self.config.export_dir)
        result = {
            "mode": "full_market",
            "funding_scan_id": scan_result["funding_scan_id"],
            "candidate_count": len(routes),
            "watch_count": len(watch_routes),
            "opened_count": len(entry_events),
            "closed_count": sum(1 for event in close_events if event == "closed"),
            "pending_count": sum(
                1 for event in close_events if event == "settlement_pending"
            ),
            "hot_route_count": count_hot_routes([*routes, *watch_routes], self.config),
            "equity": snapshot,
        }
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
        snapshot = self.store.record_funding_paper_equity_snapshot()
        export_funding_paper_csv(self.store, self.config.export_dir)
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
            "pending_count": sum(
                1 for event in close_events if event == "settlement_pending"
            ),
            "hot_route_count": len(self.hot_routes),
            "equity": snapshot,
        }
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
        dashboard = self.store.funding_paper_dashboard()
        payload = {
            "result": result,
            "summary": dashboard.get("summary") or {},
            "candidate_routes": [
                route_summary(route)
                for route in ranked_status_routes(routes)[
                    : self.config.status_report_max_routes
                ]
            ],
            "watch_routes": [
                route_summary(route)
                for route in ranked_status_routes(watch_routes)[
                    : self.config.status_report_max_routes
                ]
            ],
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
        refreshed: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for route_key, route in list(self.hot_routes.items()):
            fresh = self.focused_recheck_route(route)
            if not fresh:
                self.hot_routes.pop(route_key, None)
                continue
            decision = route_monitor_decision(fresh, now, self.config)
            if decision["hot"]:
                self.hot_routes[route_key] = fresh
                refreshed.append(fresh)
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
        return refreshed

    def focused_recheck_route(self, route: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.focused_recheck_enabled:
            return self.store.latest_funding_route_by_key(str(route.get("route_key") or ""))
        try:
            fresh = self.direct_focused_recheck_route(route)
            if fresh is not None:
                return fresh
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
        if int(result.get("hot_route_count") or 0) > 0:
            return self.config.hot_interval_seconds
        if int(result.get("pending_count") or 0) > 0:
            return self.config.hot_interval_seconds
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
        for route in routes:
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
                            "Funding Paper Trader SKIP OPEN\n"
                            f"{route.get('canonical_asset')}: focused recheck did not return the route."
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
            result = close_decision(position, now, self.store, self.config)
            if result["status"] == "wait":
                continue
            position_id = int(position["funding_paper_position_id"])
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
    return {
        "hot": hot,
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
        "notes": {"decision": decision, "paper_model": "funding_paper_trader_v1"},
    }


def close_decision(
    position: dict[str, Any],
    now: datetime,
    store: SQLiteStore,
    config: FundingPaperTraderConfig,
) -> dict[str, Any]:
    max_settlement = parse_iso(position.get("max_settlement_at"))
    if max_settlement is None:
        return {"status": "settlement_pending", "reason": "missing_max_settlement_at"}
    if now < max_settlement + timedelta(seconds=config.settlement_grace_seconds):
        return {"status": "wait", "reason": "settlement_not_reached"}

    settlement = settlement_rates_for_position(position, store)
    missing = [side for side, row in settlement.items() if row is None]
    publication_deadline = max_settlement + timedelta(
        seconds=config.max_settlement_publication_lag_seconds
    )
    if missing and now < publication_deadline:
        return {
            "status": "settlement_pending",
            "reason": "funding_history_not_published",
            "missing": missing,
            "publication_deadline": publication_deadline.isoformat(),
        }
    close_route = store.latest_funding_route_by_key(str(position["route_key"]))
    close = build_close_payload(
        position,
        settlement,
        close_route,
        use_entry_estimate_for_missing=bool(missing),
    )
    return {"status": "close", "close": close}


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
) -> dict[str, Any]:
    entry_long = leg_by_side(position.get("entry_legs") or [], "long") or {}
    entry_short = leg_by_side(position.get("entry_legs") or [], "short") or {}
    long_rate = settlement_rate_or_entry(settlement.get("long"), entry_long)
    short_rate = settlement_rate_or_entry(settlement.get("short"), entry_short)
    long_notional = float(position.get("long_notional") or 0.0)
    short_notional = float(position.get("short_notional") or 0.0)
    long_funding_pnl = funding_leg_pnl("long", long_notional, long_rate)
    short_funding_pnl = funding_leg_pnl("short", short_notional, short_rate)
    actual_funding_pnl = long_funding_pnl + short_funding_pnl
    actual_execution_cost = float(position.get("expected_execution_cost") or 0.0)
    actual_net_pnl = actual_funding_pnl - actual_execution_cost
    long_cash_delta = long_funding_pnl - actual_execution_cost / 2.0
    short_cash_delta = short_funding_pnl - actual_execution_cost / 2.0
    close_evidence = (close_route or {}).get("evidence") or {}
    return {
        "close_funding_scan_id": (close_route or {}).get("funding_scan_id"),
        "close_funding_route_id": (close_route or {}).get("funding_route_id"),
        "actual_funding_pnl": actual_funding_pnl,
        "actual_execution_cost": actual_execution_cost,
        "actual_net_pnl": actual_net_pnl,
        "long_cash_delta": long_cash_delta,
        "short_cash_delta": short_cash_delta,
        "close_reason": (
            "settlement_capture_complete_estimated_missing_history"
            if use_entry_estimate_for_missing
            else "settlement_capture_complete"
        ),
        "close_legs": (close_route or {}).get("legs") or [],
        "close_evidence": close_evidence,
        "settlement": {
            "long": settlement_payload(settlement.get("long"), entry_long, long_rate),
            "short": settlement_payload(settlement.get("short"), entry_short, short_rate),
            "entry_expected_live_net": position.get("expected_live_net"),
            "entry_expected_live_gross": position.get("expected_live_gross"),
            "history_missing_fallback": use_entry_estimate_for_missing,
        },
        "notes": {
            "paper_model": "funding_paper_trader_v1",
            "execution_cost_source": "entry_route_expected_execution_cost",
        },
    }


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
    return float(entry_leg.get("funding_rate") or 0.0)


def settlement_payload(
    settlement_row: dict[str, Any] | None,
    entry_leg: dict[str, Any],
    funding_rate: float,
) -> dict[str, Any]:
    return {
        "venue": entry_leg.get("venue"),
        "symbol": entry_leg.get("symbol"),
        "settlement_at": entry_leg.get("next_funding_at"),
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
    factories: dict[str, type[FundingVenueClient]] = {
        "aster": AsterFundingClient,
        "backpack": BackpackFundingClient,
        "binance": BinanceFundingClient,
        "bingx": BingXFundingClient,
        "bitget": BitgetFundingClient,
        "bybit": BybitFundingClient,
        "deribit": DeribitFundingClient,
        "drift": DriftFundingClient,
        "dydx": DydxFundingClient,
        "ethereal": EtherealFundingClient,
        "extended": ExtendedFundingClient,
        "gate": GateFundingClient,
        "htx": HTXFundingClient,
        "hyperliquid": HyperliquidFundingClient,
        "kraken": KrakenFundingClient,
        "kucoin": KuCoinFundingClient,
        "lighter": LighterFundingClient,
        "mexc": MEXCFundingClient,
        "okx": OKXFundingClient,
        "paradex": ParadexFundingClient,
        "vertex_base": VertexFundingClient,
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
    hot = 0
    for route in routes:
        legs = route.get("legs") or []
        leads = []
        for side in ("long", "short"):
            leg = leg_by_side(legs, side)
            settlement = parse_iso((leg or {}).get("next_funding_at"))
            if settlement is not None:
                leads.append((settlement - now).total_seconds())
        if len(leads) == 2 and all(0 <= lead <= config.arm_window_seconds for lead in leads):
            hot += 1
    return hot


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


def status_report_message(
    result: dict[str, Any],
    routes: list[dict[str, Any]],
    watch_routes: list[dict[str, Any]],
    summary: dict[str, Any],
    config: FundingPaperTraderConfig,
) -> str:
    candidates = ranked_status_routes(routes)
    watched = ranked_status_routes(watch_routes)
    max_routes = int(config.status_report_max_routes)
    lines = [
        "Funding Paper Trader STATUS",
        (
            f"Mode: {result.get('mode')} | scan: "
            f"{result.get('funding_scan_id') or '-'}"
        ),
        (
            f"Candidates: {len(routes)} | watch: {len(watch_routes)} | "
            f"hot: {result.get('hot_route_count') or 0}"
        ),
        (
            f"Open: {int(summary.get('open_position_count') or 0)} | "
            f"closed: {int(summary.get('closed_trade_count') or 0)} | "
            f"realized PnL: ${float(summary.get('realized_pnl') or 0):.2f}"
        ),
    ]
    if candidates:
        lines.append("Candidates:")
        lines.extend(
            status_route_line(route, index)
            for index, route in enumerate(candidates[:max_routes], start=1)
        )
        hidden = len(candidates) - max_routes
        if hidden > 0:
            lines.append(f"...and {hidden} more")
    else:
        lines.append("Candidates: нет")
        if watched:
            lines.append("Closest watch:")
            lines.extend(
                status_route_line(route, index)
                for index, route in enumerate(watched[:max_routes], start=1)
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
    return (
        f"{index}. {route.get('canonical_asset')}: "
        f"LONG {route.get('long_venue')} {format_rate(long_leg.get('funding_rate'))} / "
        f"SHORT {route.get('short_venue')} {format_rate(short_leg.get('funding_rate'))} | "
        f"net ${live_net:.2f} need ${threshold:.2f} | "
        f"settlement L {format_seconds(long_lead)}, S {format_seconds(short_lead)}"
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


def armed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    leads = decision.get("lead_seconds") or {}
    return (
        "Funding Paper Trader ARMED\n"
        f"{route['canonical_asset']}: LONG {route['long_venue']} / SHORT {route['short_venue']}\n"
        f"Live net: ${float(decision.get('live_net') or 0):.2f} "
        f"(need ${float(decision.get('required_live_net') or 0):.2f})\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}."
    )


def open_message(
    route: dict[str, Any],
    decision: dict[str, Any],
    position: dict[str, Any],
) -> str:
    leads = decision.get("lead_seconds") or {}
    return (
        "Funding Paper Trader OPEN\n"
        f"{route['canonical_asset']}: LONG {route['long_venue']} / SHORT {route['short_venue']}\n"
        f"Size: ${float(position.get('target_notional') or 0):.0f} per leg\n"
        f"Expected live net: ${float(position.get('expected_live_net') or 0):.2f} "
        f"(need ${float(decision.get('required_live_net') or 0):.2f})\n"
        f"До funding: long {format_seconds(leads.get('long'))}, "
        f"short {format_seconds(leads.get('short'))}."
    )


def skipped_open_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "Funding Paper Trader SKIP OPEN\n"
        f"{route['canonical_asset']}: LONG {route['long_venue']} / SHORT {route['short_venue']}\n"
        f"Fresh live net: ${float(decision.get('live_net') or 0):.2f} "
        f"(need ${float(decision.get('required_live_net') or 0):.2f})\n"
        f"Reasons: {', '.join(decision.get('reasons') or []) or 'recheck failed'}."
    )


def disarmed_message(route: dict[str, Any], decision: dict[str, Any]) -> str:
    return (
        "Funding Paper Trader DISARMED\n"
        f"{route.get('canonical_asset')}: LONG {route.get('long_venue')} / SHORT {route.get('short_venue')}\n"
        f"Fresh live net: ${float(decision.get('live_net') or 0):.2f} "
        f"(need ${float(decision.get('required_live_net') or 0):.2f})\n"
        f"Reasons: {', '.join(decision.get('reasons') or []) or 'not hot'}."
    )


def pending_message(position: dict[str, Any], result: dict[str, Any]) -> str:
    return (
        "Funding Paper Trader SETTLEMENT PENDING\n"
        f"{position['canonical_asset']}: LONG {position['long_venue']} / SHORT {position['short_venue']}\n"
        f"Причина: {result.get('reason')}. Жду публикацию funding history."
    )


def close_message(position: dict[str, Any], close: dict[str, Any]) -> str:
    settlement = close.get("settlement") or {}
    return (
        "Funding Paper Trader CLOSE\n"
        f"{position['canonical_asset']}: LONG {position['long_venue']} / SHORT {position['short_venue']}\n"
        f"Funding PnL: ${float(close.get('actual_funding_pnl') or 0):.2f}\n"
        f"Execution cost: ${float(close.get('actual_execution_cost') or 0):.2f}\n"
        f"Net PnL: ${float(close.get('actual_net_pnl') or 0):.2f}\n"
        f"Rates: long {format_rate((settlement.get('long') or {}).get('funding_rate'))}, "
        f"short {format_rate((settlement.get('short') or {}).get('funding_rate'))}."
    )


def format_seconds(value: Any) -> str:
    if value is None:
        return "-"
    seconds = max(0, int(float(value)))
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s"


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
    dashboard = store.funding_paper_dashboard()
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
                "actual_execution_cost": row.get("actual_execution_cost"),
                "actual_net_pnl": row.get("actual_net_pnl"),
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
