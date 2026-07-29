from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from smart_money_radar.config import DEFAULT_DB_PATH, PROJECT_ROOT, api_key_status
from smart_money_radar.funding.adapters import FundingDataError
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.presentation import (
    filter_deactivated_funding_dashboard_payload,
    filter_deactivated_funding_paper_payload,
)
from smart_money_radar.funding.profiles import (
    funding_bot_profile,
    funding_bot_profile_names,
)
from smart_money_radar.funding.retention import (
    apply_funding_retention_plan,
    build_funding_retention_plan,
)
from smart_money_radar.funding.adapter_contracts import PRIMARY_SHADOW_VENUES
from smart_money_radar.funding.service import (
    active_default_funding_clients,
    backfill_funding_history,
    run_funding_scan,
    watch_funding_markets,
)
from smart_money_radar.funding.shadow_monitor import (
    FundingShadowConfig,
    FundingShadowMonitor,
    UnavailableFundingVenueClient,
)
from smart_money_radar.funding.stablecoins import PublicStablecoinPriceProvider
from smart_money_radar.funding.risex_probe import (
    RiseXProbeConfig,
    run_risex_funding_probe,
)
from smart_money_radar.funding.trader import (
    PaperBot,
    PaperBotConfig,
    export_funding_paper_csv,
    funding_client_for_venue,
)
from smart_money_radar.maintenance import (
    delete_legacy_module_rows,
    prune_dune_page_cache,
)
from smart_money_radar.dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DEFAULT_DASHBOARD_PORT,
    run_dashboard,
)
from smart_money_radar.notifications import (
    TelegramScope,
    TelegramNotifier,
    resolve_telegram_credentials,
    telegram_update_chat_ids,
)
from smart_money_radar.storage import SQLiteStore


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    db_explicit = any(item == "--db" or item.startswith("--db=") for item in raw_argv)
    if not db_explicit and args.command == "funding-shadow-monitor":
        args.db = str(PROJECT_ROOT / "data" / "radar-shadow.sqlite")
    if not db_explicit and args.command == "funding-risex-probe":
        args.db = str(PROJECT_ROOT / "data" / "risex-funding-probe-testnet.sqlite")
    store = SQLiteStore(Path(args.db))

    try:
        if args.command == "init-db":
            store.init_db()
            print(f"Initialized database: {store.db_path}")
            return 0

        if args.command == "show-binance":
            store.init_db()
            rows = store.list_binance_announcements(limit=args.limit)
            for row in rows:
                print(
                    f"{row['release_at']} | {row['category']:<16} | "
                    f"pairs={row['pair_count']} contracts={row['contract_link_count']} | "
                    f"{row['title']}"
                )
            return 0

        if args.command == "dashboard":
            run_dashboard(db_path=Path(args.db), host=args.host, port=args.port)
            return 0

        if args.command == "sync-registry":
            store.init_db()
            if args.rebuild:
                stats = store.rebuild_token_registry_from_announcements()
            else:
                stats = store.sync_token_registry_from_announcements()
            print("Synced token registry from Binance announcements:")
            for key, value in stats.items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "registry-report":
            store.init_db()
            summary = store.registry_summary()
            print("Registry summary:")
            for key in (
                "tokens",
                "listing_events",
                "backtest_targets",
                "token_contracts",
                "manual_review_events",
            ):
                print(f"  {key}: {summary[key]}")
            print("\nEvents by type:")
            for row in summary["events_by_type"]:
                print(f"  {row['event_type']}: {row['count']}")
            print("\nContracts by chain:")
            for row in summary["contracts_by_chain"]:
                print(f"  {row['chain_id']}: {row['count']}")
            print("\nRecent registry events:")
            for event in store.registry_events(limit=args.limit):
                target = "target" if event["is_backtest_target"] else "context"
                pairs = ",".join(event["trading_pairs"]) or "-"
                print(
                    f"  {event['announced_at']} | {event['event_type']:<16} | "
                    f"{event['symbol']:<12} | {target:<7} | pairs={pairs} | "
                    f"{event['mapping_status']}"
                )
            return 0

        if args.command == "export-backtest-targets":
            store.init_db()
            output = Path(args.output)
            count = store.export_backtest_targets_csv(output)
            print(f"Exported {count} backtest targets to {output}")
            return 0

        if args.command == "status":
            store.init_db()
            print_status(store)
            return 0

        if args.command == "buyers-report":
            store.init_db()
            summary = store.pre_listing_buy_summary(limit=args.limit)
            print("Pre-listing buyers summary:")
            print(f"  rows: {summary['total_rows']}")
            print(f"  wallets: {summary['total_wallets']}")
            print("\nBy symbol:")
            for row in summary["by_symbol"]:
                print(
                    f"  {row['symbol']} on {row['chain_id']}: "
                    f"wallets={row['wallet_count']}, "
                    f"gross_buy_usd={row['gross_buy_usd']:.2f}, "
                    f"window={row['earliest_buy_at']} -> {row['latest_buy_at']}"
                )
            print("\nTop wallets:")
            for row in summary["top_wallets"]:
                print(
                    f"  {row['wallet_address']} | "
                    f"symbols={row['symbol_count']} | "
                    f"trades={row['buy_trade_count']} | "
                    f"gross_buy_usd={row['gross_buy_usd']:.2f}"
                )
            return 0

        if args.command == "holding-report":
            store.init_db()
            summary = store.holding_behavior_summary(limit=args.limit)
            print("Holding behavior summary:")
            print(f"  rows: {summary['total_rows']}")
            print("\nBy label:")
            for row in summary["by_label"]:
                print(
                    f"  {row['holding_label']}: "
                    f"count={row['count']} | "
                    f"pre_buy=${row['pre_buy_usd']:.2f} | "
                    f"pre_sell=${row['pre_sell_usd']:.2f} | "
                    f"post_sell=${row['post_sell_usd']:.2f} | "
                    f"avg_pre_sell_ratio={row['avg_pre_sell_ratio']:.3f} | "
                    f"avg_post_sell_ratio={row['avg_post_sell_ratio']:.3f}"
                )
            print("\nTop accumulators:")
            for row in summary["top_holders"]:
                print(
                    f"  {row['wallet_address']} | "
                    f"{row['symbol']} | "
                    f"label={row['holding_label']} | "
                    f"net_pre=${row['net_pre_usd']:.2f} | "
                    f"pre_sell_ratio={row['pre_sell_ratio']:.3f} | "
                    f"post_sell_ratio={row['post_sell_ratio']:.3f}"
                )
            print("\nTop sellers/flippers:")
            for row in summary["top_sellers"]:
                print(
                    f"  {row['wallet_address']} | "
                    f"{row['symbol']} | "
                    f"label={row['holding_label']} | "
                    f"pre_buy=${row['pre_buy_usd']:.2f} | "
                    f"pre_sell_ratio={row['pre_sell_ratio']:.3f} | "
                    f"post_sell_ratio={row['post_sell_ratio']:.3f}"
                )
            return 0

        if args.command == "research-status":
            store.init_db()
            result = store.research_dashboard()
            print("Research platform status")
            for section in ("universe", "outcomes", "event_coverage", "wallets", "attention"):
                print(f"\n{section}:")
                for key, value in result[section].items():
                    print(f"  {key}: {value}")
            print(f"\nmodel_runs: {len(result['model_runs'])}")
            return 0

        if args.command == "backtest-report":
            store.init_db()
            result = store.latest_backtest("wallet_walk_forward")
            if not result:
                print("No backtest run exists yet.")
                return 0
            metrics = result["metrics"]
            print(
                f"Backtest run {result['backtest_run_id']} | "
                f"{result['backtest_type']} | {result['status']}"
            )
            print(f"  model: {result['model_version']}")
            print(f"  evaluated targets: {metrics.get('evaluated_target_count', 0)}")
            print(
                "  precision: "
                f"{format_optional_ratio(metrics.get('micro_precision'))}"
            )
            print(
                "  baseline: "
                f"{format_optional_ratio(metrics.get('baseline_rate'))}"
            )
            print(
                "  lift: "
                f"{format_optional_number(metrics.get('lift_over_baseline'))}"
            )
            print(f"  notes: {result.get('notes') or '-'}")
            return 0

        if args.command == "live-report":
            store.init_db()
            observations = store.latest_radar_observations(limit=args.limit)
            signals = store.dashboard_signals(limit=500)
            print("Live Radar report")
            print(f"  latest observations: {len(observations)}")
            print(
                "  qualified signals: "
                f"{sum(1 for row in signals if row['status'] == 'candidate')}"
            )
            print("\nTop observations:")
            for row in observations[: args.limit]:
                print(
                    f"  {row.get('market_token_symbol') or row.get('token_symbol') or '?'} | "
                    f"wallets={row['tracked_wallet_count']} | "
                    f"net=${row['net_buy_usd']:.2f} | "
                    f"liquidity=${float(row.get('liquidity_usd') or 0):.2f} | "
                    f"risk={row.get('risk_score')}"
                )
            return 0

        if args.command == "funding-scan":
            store.init_db()
            result = run_funding_scan(store, config=funding_scan_config(args))
            print("Funding Radar scan")
            print(f"  scan: {result['funding_scan_id']}")
            print(f"  instruments: {result['instrument_count']}")
            print(f"  overlapping assets checked: {result['overlapping_asset_count']}")
            print(f"  universe routes screened: {result['universe_route_count']}")
            print(f"  full-depth shortlist: {result['execution_shortlist_count']}")
            print(f"  orderbooks: {result['orderbook_count']}")
            print(f"  full-model routes: {result['route_count']}")
            print(f"  paper candidates: {result['paper_candidate_count']}")
            print(f"  paper revalidations: {result['paper_execution_count']}")
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}")
            return 0

        if args.command == "funding-watch":
            store.init_db()
            watch_funding_markets(
                store,
                interval_seconds=args.interval_seconds,
                iterations=args.iterations,
                config=funding_scan_config(args),
            )
            return 0

        if args.command == "funding-report":
            store.init_db()
            report = filter_deactivated_funding_dashboard_payload(
                store.funding_dashboard()
            )
            scan = report["latest_scan"]
            print("Funding Radar report")
            print(f"  latest scan: {scan.get('funding_scan_id', '-')}")
            print(f"  status: {scan.get('status', 'not_started')}")
            print(f"  paper candidates: {len(report['routes'])}")
            print(f"  watch routes: {len(report['watch_routes'])}")
            for route in report["routes"][: args.limit]:
                evidence = route.get("evidence") or {}
                horizon = evidence.get("horizon") or {}
                settlement_capture = (
                    evidence.get("decision_mode") == "settlement_capture"
                )
                primary_profit = (
                    float(evidence.get("current_nowcast_net") or 0)
                    if settlement_capture
                    else float(evidence.get("conservative_net_profit") or 0)
                )
                primary_label = "live net" if settlement_capture else "Q25 net"
                print(
                    f"  {route['canonical_asset']} | "
                    f"LONG {route['long_venue']} / SHORT {route['short_venue']} | "
                    f"{horizon.get('horizon_label', '-')} | "
                    f"{primary_label}=${primary_profit:.2f} | "
                    f"Q25 net=${float(evidence.get('conservative_net_profit') or 0):.2f} | "
                    f"P(net>0)={float(evidence.get('net_profit_probability') or 0) * 100:.1f}% | "
                    f"Q25 return={float(evidence.get('conservative_net_roc_horizon') or 0) * 100:.3f}% | "
                    f"capacity=${float(route['market_capacity']):.0f}"
                )
            return 0

        if args.command == "funding-history-backfill":
            result = backfill_funding_history(
                store,
                days=args.days,
                limit=args.limit,
                target_venues=set(args.venue or []) or None,
            )
            print("Funding history backfill")
            print(f"  overlapping assets: {result['overlapping_asset_count']}")
            print(f"  eligible markets: {result['eligible_market_count']}")
            print(f"  attempted: {result['attempted_market_count']}")
            print(f"  synced: {result['synced_market_count']}")
            print(f"  imported rows: {result['imported_row_count']}")
            print(f"  remaining: {result['remaining_market_count']}")
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}")
            return 0

        if args.command == "funding-prune":
            store.init_db()
            if args.apply:
                result = apply_funding_retention_plan(
                    store,
                    keep_latest_scans=args.keep_latest_scans,
                    keep_latest_history_per_market=(
                        args.keep_latest_history_per_market
                    ),
                    stale_running_scan_seconds=args.stale_running_scan_seconds,
                )
                print("Funding retention applied")
                rows = result["deleted_rows"]
            else:
                result = build_funding_retention_plan(
                    store,
                    keep_latest_scans=args.keep_latest_scans,
                    keep_latest_history_per_market=(
                        args.keep_latest_history_per_market
                    ),
                    stale_running_scan_seconds=args.stale_running_scan_seconds,
                ).as_dict()
                print("Funding retention dry run")
                rows = result["rows_by_table"]
            print(f"  total scans: {result['total_scan_count']}")
            print(f"  protected scans: {result['protected_scan_count']}")
            print(f"  delete scans: {result['delete_scan_count']}")
            if args.apply:
                print(
                    "  stale running scans marked failed: "
                    f"{result.get('stale_running_scans_marked', 0)}"
                )
            for table, count in rows.items():
                print(f"  {table}: {count}")
            if not args.apply:
                print("  apply: rerun with --apply to delete these rows")
            else:
                print("  note: run sqlite VACUUM separately to return disk space to the OS")
            return 0

        if args.command == "cleanup-legacy-modules":
            store.init_db()
            legacy_result = delete_legacy_module_rows(store, apply=args.apply)
            print(
                "Legacy module cleanup "
                + ("applied" if args.apply else "dry run")
            )
            rows = (
                legacy_result["deleted_rows"]
                if args.apply
                else legacy_result["rows_before"]
            )
            for table, count in rows.items():
                print(f"  {table}: {count}")
            if args.include_dune_cache:
                cache_result = prune_dune_page_cache(
                    apply=args.apply,
                    max_age_days=(
                        None
                        if args.all_dune_cache
                        else args.dune_cache_max_age_days
                    ),
                ).as_dict()
                print("Dune page cache cleanup")
                print(f"  path: {cache_result['path']}")
                print(f"  files: {cache_result['deleted_files']}")
                print(f"  dirs: {cache_result['deleted_dirs']}")
                print(
                    "  bytes: "
                    f"{cache_result['deleted_bytes']}"
                )
            if not args.apply:
                print("  apply: rerun with --apply to delete rows/files")
            else:
                print("  note: run sqlite-maintenance --vacuum to reclaim DB file space")
            return 0

        if args.command == "funding-paper-trader":
            store.init_db()
            trader = PaperBot(
                store,
                config=funding_paper_trader_config(args),
                notifier=TelegramNotifier(
                    token_env_var="FUNDING_TELEGRAM_BOT_TOKEN",
                    chat_id_env_var="FUNDING_TELEGRAM_CHAT_ID",
                ),
            )
            trader.run_loop()
            return 0

        if args.command == "funding-shadow-monitor":
            store.init_db()
            clients = shadow_monitor_clients(args)
            monitor = FundingShadowMonitor(
                store,
                clients,
                config=funding_shadow_config(args),
                stablecoin_price_provider=PublicStablecoinPriceProvider(
                    timeout_seconds=2.0,
                ),
                notifier=(
                    TelegramNotifier(scope=TelegramScope.SHADOW)
                    if not args.no_telegram
                    else None
                ),
            )
            result = monitor.run(duration_seconds=args.duration_seconds)
            print("Funding shadow monitor")
            print(f"  profile: {args.profile}")
            print(f"  environment: {args.environment}")
            print(f"  db: {store.db_path}")
            print(f"  duration_seconds: {args.duration_seconds}")
            print(f"  iterations: {result['iterations']}")
            print(f"  paper_safety_deltas: {result.get('paper_safety_deltas', {})}")
            print(f"  mandatory_unavailable: {result.get('mandatory_unavailable', 0)}")
            if result.get("paper_safety_deltas") and any(
                float(value) != 0.0
                for value in result.get("paper_safety_deltas", {}).values()
            ):
                return 1
            if args.strict_required_venues and result.get("mandatory_unavailable", 0):
                return 1
            return 0

        if args.command == "funding-risex-probe":
            result = run_risex_funding_probe(risex_probe_config(args))
            print("RiseX funding semantics probe")
            print(f"  mode: {args.mode}")
            print(f"  environment: {args.environment}")
            print(f"  db: {args.db}")
            print(f"  status: {result.get('status')}")
            print(f"  orders_enabled: {result.get('orders_enabled')}")
            if result.get("reason"):
                print(f"  reason: {result.get('reason')}")
            if result.get("error"):
                print(f"  error: {result.get('error')}")
            print(f"  probe_run_id: {result.get('probe_run_id')}")
            return 0 if str(result.get("status")) not in {"FAILED", "CANARY_BLOCKED"} else 1

        if args.command == "funding-paper-report":
            store.init_db()
            report = filter_deactivated_funding_paper_payload(
                store.funding_paper_dashboard()
            )
            summary = report["summary"]
            print("Funding Paper Trader report")
            print(f"  starting capital: ${summary['starting_capital']:.2f}")
            print(f"  total cash: ${summary['total_cash']:.2f}")
            print(f"  reserved margin: ${summary['reserved_margin']:.2f}")
            print(f"  realized PnL: ${summary['realized_pnl']:.2f}")
            print(f"  return: {summary['return_pct'] * 100:.3f}%")
            print(f"  open positions: {summary['open_position_count']}")
            print(f"  closed trades: {summary['closed_trade_count']}")
            if summary["win_rate"] is not None:
                print(f"  win rate: {summary['win_rate'] * 100:.1f}%")
            print("\nAccounts:")
            for row in report["accounts"]:
                print(
                    f"  {row['venue']}: cash=${float(row['cash_balance']):.2f}, "
                    f"reserved=${float(row['reserved_margin']):.2f}, "
                    f"available=${float(row['available_balance']):.2f}"
                )
            print("\nOpen positions:")
            for row in report["open_positions"][: args.limit]:
                print(
                    f"  #{row['funding_paper_position_id']} {row['canonical_asset']} | "
                    f"LONG {row['long_venue']} / SHORT {row['short_venue']} | "
                    f"expected=${float(row['expected_live_net'] or 0):.2f} | "
                    f"settlement={row.get('max_settlement_at')}"
                )
            return 0

        if args.command == "funding-paper-export":
            store.init_db()
            outputs = export_funding_paper_csv(store, Path(args.output_dir))
            print("Funding paper CSV exports")
            for name, path in outputs.items():
                print(f"  {name}: {path}")
            return 0

        if args.command == "telegram-chat-id":
            chats = telegram_update_chat_ids(scope=args.scope)
            if not chats:
                credentials = resolve_telegram_credentials(args.scope)
                if not credentials.token:
                    print(
                        "No Telegram token configured for scope "
                        f"{args.scope}: {credentials.token_env_var} missing."
                    )
                    return 1
                print(
                    "No Telegram chats found yet. Send any message to the bot, "
                    "then run this command again."
                )
                return 1
            print(f"Telegram chats found for scope {args.scope}:")
            for chat in chats:
                label = (
                    chat.get("username")
                    or chat.get("first_name")
                    or chat.get("title")
                    or "-"
                )
                print(
                    f"  chat_id={chat['chat_id']} "
                    f"type={chat.get('type')} label={label}"
                )
            return 0

        if args.command == "telegram-test":
            result = TelegramNotifier(scope=args.scope).send(
                f"Smart Money Radar Telegram test ({args.scope}): ok"
            )
            print(f"Telegram test: {result.status}")
            if result.error:
                print(f"  error: {result.error}")
            return 0 if result.status == "sent" else 1

        if args.command == "sqlite-maintenance":
            store.init_db()
            result = store.sqlite_maintenance(
                vacuum=args.vacuum,
                analyze=not args.no_analyze,
                optimize=not args.no_optimize,
                wal_checkpoint=not args.no_wal_checkpoint,
            )
            print("SQLite maintenance")
            print(f"  database: {result['database_path']}")
            print(f"  operations: {', '.join(result['operations']) or '-'}")
            print(f"  before: {int(result['before_bytes'])} bytes")
            print(f"  after: {int(result['after_bytes'])} bytes")
            print(f"  saved: {int(result['saved_bytes'])} bytes")
            if not args.vacuum:
                print("  note: add --vacuum to return free pages to the OS")
            return 0

        parser.print_help()
        return 2
    except FundingDataError as exc:
        print(f"Funding market data error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smr")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize local SQLite database.")

    show = subparsers.add_parser(
        "show-binance",
        help="Print collected Binance announcements.",
    )
    show.add_argument("--limit", type=int, default=20)

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Run the local Smart Money Radar dashboard.",
    )
    dashboard.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    dashboard.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)

    sync_registry = subparsers.add_parser(
        "sync-registry",
        help="Build token/listing registry from collected Binance announcements.",
    )
    sync_registry.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear and rebuild token/listing registry from announcements.",
    )

    registry_report = subparsers.add_parser(
        "registry-report",
        help="Print token/listing registry diagnostics.",
    )
    registry_report.add_argument("--limit", type=int, default=20)

    export_targets = subparsers.add_parser(
        "export-backtest-targets",
        help="Export Binance spot listing targets to CSV for research/backtests.",
    )
    export_targets.add_argument(
        "--output",
        default="exports/backtest_targets.csv",
        help="CSV output path.",
    )

    subparsers.add_parser(
        "status",
        help="Print concise project status and next blockers.",
    )

    buyers_report = subparsers.add_parser(
        "buyers-report",
        help="Print summarized pre-listing buyer results.",
    )
    buyers_report.add_argument("--limit", type=int, default=20)

    holding_report = subparsers.add_parser(
        "holding-report",
        help="Print summarized wallet sell-side / holding behavior.",
    )
    holding_report.add_argument("--limit", type=int, default=20)

    subparsers.add_parser(
        "research-status",
        help="Print Research Validity, wallet denominator, attention, and ML readiness.",
    )

    subparsers.add_parser(
        "backtest-report",
        help="Print the latest honest backtest result and readiness limits.",
    )

    live_report = subparsers.add_parser(
        "live-report",
        help="Print the latest Live Radar observations and qualified signals.",
    )
    live_report.add_argument("--limit", type=int, default=20)

    funding_scan = subparsers.add_parser(
        "funding-scan",
        help="Collect public perp venues and scan paper carry routes.",
    )
    add_funding_scan_arguments(funding_scan)

    funding_watch = subparsers.add_parser(
        "funding-watch",
        help="Continuously refresh the Funding Radar paper pipeline.",
    )
    add_funding_scan_arguments(funding_watch)
    funding_watch.add_argument("--interval-seconds", type=int, default=60)
    funding_watch.add_argument("--iterations", type=int)

    funding_report = subparsers.add_parser(
        "funding-report",
        help="Print executable-after-cost funding carry candidates.",
    )
    funding_report.add_argument("--limit", type=int, default=20)

    funding_backfill = subparsers.add_parser(
        "funding-history-backfill",
        help="Progressively collect 90-day funding history for overlapping venues.",
    )
    funding_backfill.add_argument("--days", type=int, default=90)
    funding_backfill.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Markets per resumable batch; use 0 for all remaining markets.",
    )
    funding_backfill.add_argument(
        "--venue",
        action="append",
        choices=(
            "aster", "backpack", "binance", "bitget", "bybit", "dydx",
            "deribit", "drift", "ethereal", "extended", "gate", "hyperliquid",
            "kraken", "kucoin", "lighter", "mexc", "okx", "paradex", "risex",
            "vertex_base",
        ),
        help="Backfill only this venue; repeat the option for multiple venues.",
    )

    funding_prune = subparsers.add_parser(
        "funding-prune",
        help="Prune old Funding Radar scan diagnostics while preserving paper ledger rows.",
    )
    funding_prune.add_argument(
        "--keep-latest-scans",
        type=int,
        default=20,
        help="Always keep this many latest funding scans plus paper-linked scans.",
    )
    funding_prune.add_argument(
        "--keep-latest-history-per-market",
        type=int,
        default=24,
        help="Keep only this many latest funding-rate rows per venue/symbol.",
    )
    funding_prune.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete rows. Omit for a dry-run plan.",
    )
    funding_prune.add_argument(
        "--stale-running-scan-seconds",
        type=int,
        default=900,
        help="Treat running funding scans older than this as stale failed scans.",
    )

    cleanup_legacy = subparsers.add_parser(
        "cleanup-legacy-modules",
        help="Delete old Prediction/Wallet/Binance research rows and optional Dune cache.",
    )
    cleanup_legacy.add_argument("--apply", action="store_true")
    cleanup_legacy.add_argument("--include-dune-cache", action="store_true")
    cleanup_legacy.add_argument("--all-dune-cache", action="store_true")
    cleanup_legacy.add_argument(
        "--dune-cache-max-age-days",
        type=int,
        default=7,
        help="Delete Dune page cache files older than this many days.",
    )

    funding_paper_trader = subparsers.add_parser(
        "funding-paper-trader",
        help="Run deterministic local funding paper trader loop.",
    )
    funding_paper_trader.add_argument(
        "--venue-starting-balance",
        type=float,
        default=1_000.0,
    )
    funding_paper_trader.add_argument("--target-notional", type=float, default=500.0)
    funding_paper_trader.add_argument("--entry-min-lead-seconds", type=float, default=25.0)
    funding_paper_trader.add_argument("--entry-max-lead-seconds", type=float, default=35.0)
    funding_paper_trader.add_argument("--arm-window-seconds", type=int, default=120)
    funding_paper_trader.add_argument(
        "--final-recheck-freeze-seconds",
        type=int,
        default=0,
        help="Legacy freeze window. Default v2 behavior requires fresh entry snapshots.",
    )
    funding_paper_trader.add_argument(
        "--max-entry-snapshot-age-seconds",
        type=float,
        default=2.0,
        help="Maximum fresh route snapshot age usable for paper entry.",
    )
    funding_paper_trader.add_argument(
        "--settlement-alignment-tolerance-seconds",
        type=float,
        default=1.0,
        help="Maximum allowed skew between the two next funding settlement timestamps.",
    )
    funding_paper_trader.add_argument(
        "--settlement-grace-seconds",
        type=int,
        default=90,
    )
    funding_paper_trader.add_argument(
        "--max-settlement-publication-lag-seconds",
        type=int,
        default=300,
    )
    funding_paper_trader.add_argument(
        "--min-live-net-profit",
        type=float,
        default=0.0,
    )
    funding_paper_trader.add_argument("--scan-interval-seconds", type=int, default=300)
    funding_paper_trader.add_argument(
        "--monitor-interval-seconds",
        type=float,
        default=2.0,
    )
    funding_paper_trader.add_argument("--hot-interval-seconds", type=float, default=1.0)
    funding_paper_trader.add_argument(
        "--hot-route-recheck-workers",
        type=int,
        default=6,
        help="Maximum focused route rechecks to run in parallel during hot monitoring.",
    )
    funding_paper_trader.add_argument(
        "--status-report-interval-seconds",
        type=int,
        default=3_600,
        help="Telegram status report interval; use 0 to disable.",
    )
    funding_paper_trader.add_argument(
        "--status-report-max-routes",
        type=int,
        default=5,
        help="Maximum candidate routes included in each status report.",
    )
    funding_paper_trader.add_argument("--iterations", type=int)
    funding_paper_trader.add_argument("--export-dir", default="exports/funding_paper")
    funding_paper_trader.add_argument("--no-telegram", action="store_true")
    funding_paper_trader.add_argument("--no-focused-recheck", action="store_true")
    funding_paper_trader.add_argument(
        "--venue-set",
        default=None,
        help="Comma-separated venue whitelist (e.g. binance,bybit,hyperliquid).",
    )
    funding_paper_trader.add_argument(
        "--profile",
        choices=funding_bot_profile_names(),
        default="default",
        help="Named paper bot profile. CLI venue-set overrides profile venues.",
    )
    funding_paper_trader.add_argument("--spread-arb", action="store_true")
    funding_paper_trader.add_argument(
        "--strategy-set",
        default=None,
        help=(
            "Comma-separated strategy list: "
            "synchronized_funding_capture by default. "
            "Legacy spread/funding strategy names are research-only."
        ),
    )
    funding_paper_trader.add_argument(
        "--basis-stop-loss-bps",
        type=float,
        default=200.0,
        help="Close position when unrealized basis loss exceeds this many bps.",
    )
    funding_paper_trader.add_argument(
        "--common-price-move-alert-pct",
        type=float,
        default=5.0,
        help="Warn when both legs share at least this common absolute price move.",
    )
    funding_paper_trader.add_argument(
        "--common-price-move-critical-pct",
        type=float,
        default=10.0,
        help="Critical telemetry threshold for common price move; not an automatic close.",
    )
    funding_paper_trader.add_argument("--no-spread-monitoring", action="store_true")

    funding_shadow_monitor = subparsers.add_parser(
        "funding-shadow-monitor",
        help="Run read-only synchronized funding shadow monitor.",
    )
    funding_shadow_monitor.add_argument(
        "--profile",
        choices=("dex_shadow",),
        default="dex_shadow",
    )
    funding_shadow_monitor.add_argument(
        "--environment",
        choices=("mainnet", "testnet"),
        default="mainnet",
    )
    funding_shadow_monitor.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path for shadow observations.",
    )
    funding_shadow_monitor.add_argument("--target-notional", type=float, default=500.0)
    funding_shadow_monitor.add_argument(
        "--duration-seconds",
        type=float,
        default=180.0,
        help="Bounded run duration. The command never starts a permanent service.",
    )
    funding_shadow_monitor.add_argument(
        "--max-strategy-hold-seconds",
        type=float,
        default=180.0,
    )
    funding_shadow_monitor.add_argument(
        "--max-gap-between-settlements-seconds",
        type=float,
        default=60.0,
    )
    funding_shadow_monitor.add_argument(
        "--entry-safety-buffer-seconds",
        type=float,
        default=30.0,
    )
    funding_shadow_monitor.add_argument(
        "--exit-safety-buffer-seconds",
        type=float,
        default=5.0,
    )
    funding_shadow_monitor.add_argument(
        "--settlement-confirmation-timeout-seconds",
        type=float,
        default=5.0,
    )
    funding_shadow_monitor.add_argument(
        "--max-clock-uncertainty-ms",
        type=float,
        default=500.0,
    )
    funding_shadow_monitor.add_argument(
        "--strict-required-venues",
        action="store_true",
    )
    funding_shadow_monitor.add_argument("--no-telegram", action="store_true")

    risex_probe = subparsers.add_parser(
        "funding-risex-probe",
        help="Run RiseX funding semantics probe in public or guarded testnet canary mode.",
    )
    risex_probe.add_argument("--environment", choices=("testnet",), default="testnet")
    risex_probe.add_argument(
        "--mode",
        choices=("public", "testnet-canary"),
        default="public",
    )
    risex_probe.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path for RiseX probe observations.",
    )
    risex_probe.add_argument("--max-notional-usd", type=float, default=10.0)
    risex_probe.add_argument("--entry-lead-seconds", type=float, default=30.0)
    risex_probe.add_argument("--max-wait-seconds", type=float, default=120.0)
    risex_probe.add_argument(
        "--confirmation-timeout-seconds",
        type=float,
        default=60.0,
    )
    risex_probe.add_argument("--no-telegram", action="store_true", default=True)
    risex_probe.add_argument("--confirm-testnet-canary", action="store_true")

    funding_paper_report = subparsers.add_parser(
        "funding-paper-report",
        help="Print local funding paper trader balances and open positions.",
    )
    funding_paper_report.add_argument("--limit", type=int, default=20)

    funding_paper_export = subparsers.add_parser(
        "funding-paper-export",
        help="Export funding paper trader tables to CSV.",
    )
    funding_paper_export.add_argument(
        "--output-dir",
        default="exports/funding_paper",
    )

    telegram_chat_id = subparsers.add_parser(
        "telegram-chat-id",
        help="Print Telegram chat IDs from bot getUpdates.",
    )
    telegram_chat_id.add_argument(
        "--scope",
        choices=tuple(scope.value for scope in TelegramScope),
        default=TelegramScope.DEFAULT.value,
    )

    telegram_test = subparsers.add_parser(
        "telegram-test",
        help="Send a small Telegram test message using .env settings.",
    )
    telegram_test.add_argument(
        "--scope",
        choices=tuple(scope.value for scope in TelegramScope),
        default=TelegramScope.DEFAULT.value,
    )

    sqlite_maintenance = subparsers.add_parser(
        "sqlite-maintenance",
        help="Run SQLite checkpoint/analyze/optimize and optional VACUUM.",
    )
    sqlite_maintenance.add_argument("--vacuum", action="store_true")
    sqlite_maintenance.add_argument("--no-analyze", action="store_true")
    sqlite_maintenance.add_argument("--no-optimize", action="store_true")
    sqlite_maintenance.add_argument("--no-wal-checkpoint", action="store_true")

    return parser


def add_funding_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-notional", type=float, default=10_000.0)
    parser.add_argument(
        "--horizon-mode",
        choices=("next_settlement", "fixed", "research"),
        default="fixed",
    )
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=0,
        help="Optional test/debug cap; 0 scans the complete route universe",
    )
    parser.add_argument(
        "--near-miss-routes",
        type=int,
        default=None,
        help=(
            "Extra fee-negative but positive-gross routes to inspect with full "
            "depth. Omit for all; use 0 to disable."
        ),
    )
    parser.add_argument("--history-days", type=int, default=90)
    parser.add_argument("--history-refresh-hours", type=float, default=48.0)
    parser.add_argument(
        "--max-live-history-markets",
        type=int,
        default=24,
        help=(
            "Maximum stale funding-history markets to hydrate during one live scan; "
            "0 disables live hydration. Use funding-history-backfill for full history."
        ),
    )
    parser.add_argument(
        "--max-full-depth-orderbook-markets",
        type=int,
        default=0,
        help=(
            "Optional emergency/debug budget for unique orderbook markets; "
            "0 scans without this budget."
        ),
    )
    parser.add_argument(
        "--orderbook-cache-ttl-seconds",
        type=int,
        default=0,
        help=(
            "Reuse a recently fetched orderbook for this many seconds; "
            "0 disables the cache."
        ),
    )
    parser.add_argument(
        "--market-snapshot-cache-ttl-seconds",
        type=int,
        default=0,
        help=(
            "Reuse recently fetched venue funding/mark snapshots for this many "
            "seconds; 0 disables the cache."
        ),
    )
    parser.add_argument(
        "--adaptive-near-miss-min-score",
        type=float,
        default=55.0,
        help="Minimum 0-100 quick score for near-miss full-depth selection.",
    )
    parser.add_argument(
        "--adaptive-near-miss-min-cost-coverage",
        type=float,
        default=0.35,
        help="Minimum gross/cost coverage for adaptive near-miss selection.",
    )
    parser.add_argument(
        "--adaptive-near-miss-emergency-floor-bps",
        type=float,
        default=2.0,
        help="Minimum floor for the dynamic gross emergency threshold, in bps.",
    )
    parser.add_argument(
        "--adaptive-near-miss-emergency-cost-multiplier",
        type=float,
        default=0.75,
        help="Dynamic gross emergency threshold as a fraction of estimated cost.",
    )
    parser.add_argument(
        "--adaptive-near-miss-near-break-even-coverage",
        type=float,
        default=0.65,
        help="Gross/cost coverage that admits a near-break-even route.",
    )
    parser.add_argument(
        "--adaptive-near-miss-liquidity-score",
        type=float,
        default=0.70,
        help="Liquidity score gate for the liquidity+urgency stage.",
    )
    parser.add_argument(
        "--adaptive-near-miss-urgency-score",
        type=float,
        default=0.35,
        help="Settlement urgency score gate for the liquidity+urgency stage.",
    )


def funding_scan_config(args: argparse.Namespace) -> FundingScanConfig:
    return FundingScanConfig(
        target_notional=args.target_notional,
        horizon_mode=args.horizon_mode,
        horizon_hours=args.horizon_hours,
        max_candidates=args.max_candidates,
        near_miss_full_depth_routes=args.near_miss_routes,
        history_days=args.history_days,
        history_refresh_hours=args.history_refresh_hours,
        max_live_history_markets=args.max_live_history_markets,
        max_full_depth_orderbook_markets=args.max_full_depth_orderbook_markets,
        orderbook_cache_ttl_seconds=args.orderbook_cache_ttl_seconds,
        market_snapshot_cache_ttl_seconds=args.market_snapshot_cache_ttl_seconds,
        adaptive_near_miss_min_score=args.adaptive_near_miss_min_score,
        adaptive_near_miss_min_cost_coverage=args.adaptive_near_miss_min_cost_coverage,
        adaptive_near_miss_emergency_floor_bps=args.adaptive_near_miss_emergency_floor_bps,
        adaptive_near_miss_emergency_cost_multiplier=args.adaptive_near_miss_emergency_cost_multiplier,
        adaptive_near_miss_near_break_even_coverage=args.adaptive_near_miss_near_break_even_coverage,
        adaptive_near_miss_liquidity_score=args.adaptive_near_miss_liquidity_score,
        adaptive_near_miss_urgency_score=args.adaptive_near_miss_urgency_score,
    ).validated()


def funding_paper_trader_config(args: argparse.Namespace) -> PaperBotConfig:
    profile = funding_bot_profile(getattr(args, "profile", "default"))
    missing_required_venues = [
        venue
        for venue in profile.required_venues
        if funding_client_for_venue(venue, fast=True) is None
    ]
    if missing_required_venues:
        raise ValueError(
            "Funding bot profile "
            f"{profile.name!r} requires missing verified venue adapter(s): "
            + ", ".join(missing_required_venues)
        )
    venue_set_raw = getattr(args, "venue_set", None)
    venue_set = (
        tuple(v.strip() for v in str(venue_set_raw).split(",") if v.strip())
        if venue_set_raw
        else profile.venue_set
    )
    strategy_set_raw = getattr(args, "strategy_set", None)
    strategy_set = (
        tuple(v.strip() for v in str(strategy_set_raw).split(",") if v.strip())
        if strategy_set_raw
        else profile.strategy_set
    )
    return PaperBotConfig(
        profile_name=profile.name,
        strategy_set=strategy_set,
        venue_starting_balance=args.venue_starting_balance,
        target_notional_per_leg=args.target_notional,
        entry_min_lead_seconds=args.entry_min_lead_seconds,
        entry_max_lead_seconds=args.entry_max_lead_seconds,
        settlement_alignment_tolerance_seconds=(
            args.settlement_alignment_tolerance_seconds
        ),
        arm_window_seconds=args.arm_window_seconds,
        final_recheck_freeze_seconds=args.final_recheck_freeze_seconds,
        max_entry_snapshot_age_seconds=args.max_entry_snapshot_age_seconds,
        settlement_grace_seconds=args.settlement_grace_seconds,
        max_settlement_publication_lag_seconds=(
            args.max_settlement_publication_lag_seconds
        ),
        scan_interval_seconds=args.scan_interval_seconds,
        monitor_interval_seconds=args.monitor_interval_seconds,
        hot_interval_seconds=args.hot_interval_seconds,
        hot_route_recheck_workers=args.hot_route_recheck_workers,
        status_report_interval_seconds=args.status_report_interval_seconds,
        status_report_max_routes=args.status_report_max_routes,
        min_live_net_profit=args.min_live_net_profit,
        iterations=args.iterations,
        export_dir=Path(args.export_dir),
        telegram_enabled=not args.no_telegram,
        focused_recheck_enabled=not args.no_focused_recheck,
        venue_set=venue_set,
        spread_arb_enabled=getattr(args, "spread_arb", False),
        basis_stop_loss_bps=getattr(args, "basis_stop_loss_bps", 200.0),
        common_price_move_alert_fraction=(
            float(getattr(args, "common_price_move_alert_pct", 5.0) or 0.0) / 100.0
        ),
        common_price_move_critical_fraction=(
            float(getattr(args, "common_price_move_critical_pct", 10.0) or 0.0) / 100.0
        ),
        spread_monitoring_enabled=not getattr(args, "no_spread_monitoring", False),
    ).validated()


def funding_shadow_config(args: argparse.Namespace) -> FundingShadowConfig:
    return FundingShadowConfig(
        profile=args.profile,
        environment=args.environment,
        target_notional=args.target_notional,
        max_strategy_hold_seconds=args.max_strategy_hold_seconds,
        max_gap_between_settlements_seconds=args.max_gap_between_settlements_seconds,
        entry_safety_buffer_seconds=args.entry_safety_buffer_seconds,
        exit_safety_buffer_seconds=args.exit_safety_buffer_seconds,
        settlement_confirmation_timeout_seconds=(
            args.settlement_confirmation_timeout_seconds
        ),
        max_clock_uncertainty_ms=args.max_clock_uncertainty_ms,
        strict_required_venues=args.strict_required_venues,
        telegram_enabled=not args.no_telegram,
    ).validated()


def risex_probe_config(args: argparse.Namespace) -> RiseXProbeConfig:
    return RiseXProbeConfig(
        environment=args.environment,
        mode=args.mode,
        db_path=Path(args.db),
        max_notional_usd=args.max_notional_usd,
        entry_lead_seconds=args.entry_lead_seconds,
        max_wait_seconds=args.max_wait_seconds,
        confirmation_timeout_seconds=args.confirmation_timeout_seconds,
        no_telegram=True,
        confirm_testnet_canary=args.confirm_testnet_canary,
    ).validated()


def shadow_monitor_clients(args: argparse.Namespace) -> list[Any]:
    if args.profile == "dex_shadow":
        clients: list[Any] = []
        for venue in PRIMARY_SHADOW_VENUES:
            client = funding_client_for_venue(
                venue,
                environment=args.environment,
                fast=True,
                timeout_seconds=2.0,
            )
            if client is None:
                clients.append(
                    UnavailableFundingVenueClient(
                        venue,
                        environment=args.environment,
                        reason=f"{venue} client unavailable for {args.environment}",
                    )
                )
            else:
                clients.append(client)
        return clients
    return active_default_funding_clients()


def print_status(store: SQLiteStore) -> None:
    summary = store.dashboard_summary()
    registry = store.registry_summary()
    base_targets = store.chain_backtest_targets("base")
    api_status = api_key_status()
    print("Smart Money Radar status")
    print(f"  DB: {summary['db_path']}")
    print(f"  Binance announcements: {summary['announcement_count']}")
    print(f"  Registry tokens: {registry['tokens']}")
    print(f"  Listing events: {registry['listing_events']}")
    print(f"  Spot backtest targets: {registry['backtest_targets']}")
    print(f"  Base-ready targets: {len(base_targets)}")
    print(f"  Pre-listing wallet buys: {summary['pre_listing_wallet_buy_count']}")
    print(f"  Wallet holding metrics: {summary['wallet_holding_metric_count']}")
    print(f"  Wallet scores: {summary['wallet_score_count']}")
    print("")
    print("Next checks")
    print("  1. Run: python3 -m smart_money_radar.cli funding-scan --target-notional 10000")
    print("  2. Run: python3 -m smart_money_radar.cli funding-paper-trader")


def json_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"File not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}")


def format_optional_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def format_optional_number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
