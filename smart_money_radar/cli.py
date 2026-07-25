from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from smart_money_radar.analytics import (
    AnalyticsError,
    initialize_analytics,
    prune_analytics_executions,
    run_saved_query,
    run_sql,
)
from smart_money_radar.config import DEFAULT_DB_PATH, api_key_status
from smart_money_radar.funding.adapters import FundingDataError
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.retention import (
    apply_funding_retention_plan,
    build_funding_retention_plan,
)
from smart_money_radar.funding.service import (
    backfill_funding_history,
    run_funding_scan,
    watch_funding_markets,
)
from smart_money_radar.funding.trader import (
    FundingPaperTrader,
    FundingPaperTraderConfig,
    export_funding_paper_csv,
)
from smart_money_radar.dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DEFAULT_DASHBOARD_PORT,
    run_dashboard,
)
from smart_money_radar.notifications import (
    TelegramNotifier,
    telegram_update_chat_ids,
)
from smart_money_radar.prediction.clients import PredictionDataError
from smart_money_radar.prediction.bot import (
    PredictionBotConfig,
    PredictionRadarBot,
)
from smart_money_radar.prediction.mappings import (
    PredictionMappingError,
    load_prediction_verified_mapping_file,
)
from smart_money_radar.prediction.service import (
    PredictionScanConfig,
    run_prediction_scan,
    watch_prediction_markets,
)
from smart_money_radar.storage import SQLiteStore


ANALYTICS_EXECUTION_RETENTION = 5


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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

        if args.command == "analytics-init":
            catalog = initialize_analytics(store)
            print("Initialized local Radar Analytics")
            print(f"  engine: {catalog['engine']}")
            print(f"  views: {len(catalog['views'])}")
            print(f"  saved queries: {len(catalog['queries'])}")
            return 0

        if args.command == "analytics-list":
            catalog = initialize_analytics(store)
            print("Radar Analytics views")
            for view in catalog["views"]:
                print(f"  {view['name']}")
            print("\nSaved queries")
            for query in catalog["queries"]:
                print(
                    f"  {query['query_slug']} | {query['title']} | "
                    f"{query['source']}"
                )
            return 0

        if args.command == "analytics-run":
            try:
                result = run_saved_query(store, args.slug, limit=args.limit)
            finally:
                prune_analytics_executions(
                    store,
                    keep_latest=ANALYTICS_EXECUTION_RETENTION,
                )
            print_analytics_result(result, as_json=args.json)
            return 0

        if args.command == "analytics-query":
            if args.sql_file:
                sql = Path(args.sql_file).read_text(encoding="utf-8")
            else:
                sql = args.sql
            try:
                result = run_sql(store, sql, limit=args.limit)
            finally:
                prune_analytics_executions(
                    store,
                    keep_latest=ANALYTICS_EXECUTION_RETENTION,
                )
            print_analytics_result(result, as_json=args.json)
            return 0

        if args.command == "dune-health":
            statuses = api_key_status()
            print("Dune/API health:")
            print(f"  DUNE_API_KEY: {'present' if statuses['DUNE_API_KEY'] else 'missing'}")
            if not statuses["DUNE_API_KEY"]:
                print("  Next: add DUNE_API_KEY to .env before executing Dune SQL.")
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

        if args.command == "prediction-scan":
            store.init_db()
            result = run_prediction_scan(
                store,
                config=prediction_scan_config(args),
            )
            print("Prediction Radar scan")
            print(f"  scan: {result['prediction_scan_id']}")
            print(
                "  events: "
                f"Polymarket={result['polymarket_event_count']} "
                f"Kalshi={result['kalshi_event_count']} "
                f"Hyperliquid={result.get('hyperliquid_event_count', 0)}"
            )
            print(f"  markets: {result['market_count']}")
            print(f"  orderbooks: {result['orderbook_count']}")
            print(f"  routes evaluated: {result['route_count']}")
            print(f"  executable routes: {result['executable_route_count']}")
            print(f"  paper executions: {result['paper_execution_count']}")
            print(f"  wallet scores: {result['wallet_score_count']}")
            print(f"  event-token links: {result['token_link_count']}")
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}")
            return 0

        if args.command == "prediction-watch":
            store.init_db()
            watch_prediction_markets(
                store,
                interval_seconds=args.interval_seconds,
                iterations=args.iterations,
                config=prediction_scan_config(args),
            )
            return 0

        if args.command == "prediction-bot":
            store.init_db()
            bot = PredictionRadarBot(
                store,
                config=prediction_bot_config(args),
                notifier=TelegramNotifier(
                    token_env_var="PREDICTION_TELEGRAM_BOT_TOKEN",
                    chat_id_env_var="PREDICTION_TELEGRAM_CHAT_ID",
                ),
            )
            bot.run_loop()
            return 0

        if args.command == "prediction-report":
            store.init_db()
            report = store.prediction_dashboard(route_limit=args.limit)
            scan = report["latest_scan"]
            print("Prediction Radar report")
            print(f"  latest scan: {scan.get('prediction_scan_id', '-')}")
            print(f"  status: {scan.get('status', 'not_started')}")
            print(f"  freshness: {scan.get('freshness_status', 'unknown')}")
            print(
                "  venue events: "
                f"Polymarket={int(scan.get('polymarket_event_count') or 0)} "
                f"Kalshi={int(scan.get('kalshi_event_count') or 0)} "
                f"Hyperliquid={int(scan.get('hyperliquid_event_count') or 0)}"
            )
            print(f"  markets: {scan.get('market_count', 0)}")
            print(f"  paper/executable routes: {len(report['routes'])}")
            candidate_summary = report.get("candidate_summary") or {}
            candidate_count = int(candidate_summary.get("candidate_count") or 0)
            print(
                "  route candidates: "
                f"{candidate_count if candidate_count else 'нет кандидатов'}"
            )
            print(f"  executable routes: {scan.get('executable_route_count', 0)}")
            wallet_summary = report.get("wallet_summary") or {}
            print(
                "  wallet cache: "
                f"{wallet_summary.get('cache_status', 'unknown')}"
            )
            print(
                "  paper PnL: "
                f"${float(report['paper_summary'].get('simulated_net_profit') or 0):.2f}"
            )
            alerts = (report.get("opportunity_dashboard") or {}).get("alerts") or []
            for alert in alerts[:5]:
                print(
                    f"  alert: {alert['alert_type']} | "
                    f"{alert.get('strategy_name') or alert.get('strategy_bucket')} | "
                    f"score={float(alert.get('candidate_score') or 0):.2f} | "
                    f"{alert.get('title')}"
                )
            for route in report["routes"]:
                print(
                    f"  {route['route_type']} | {route['venue_scope']} | "
                    f"net=${float(route.get('expected_net_profit') or 0):.2f} | "
                    f"size={float(route.get('optimal_size') or 0):.2f} | "
                    f"{route['title']}"
                )
            near_zero = report.get("near_zero_routes") or []
            if near_zero:
                print("  near-zero watch, not candidates:")
                for route in near_zero[: args.limit]:
                    print(
                        f"    {route['route_type']} | {route['venue_scope']} | "
                        f"net=${float(route.get('expected_net_profit') or 0):.4f} | "
                        f"missing_edge={float(route.get('missing_net_edge_per_share') or 0) * 100:.3f}% | "
                        f"size={float(route.get('optimal_size') or 0):.2f} | "
                        f"{route['title']}"
                    )
            return 0

        if args.command == "prediction-backlog-report":
            store.init_db()
            report = store.prediction_dashboard(route_limit=args.limit)
            summary = report.get("backlog_summary") or {}
            print("Prediction Radar backlog")
            print(
                "  candidates: "
                f"{int(summary.get('candidate_backlog_count') or 0)} "
                f"(active={int(summary.get('active_candidate_count') or 0)})"
            )
            print(f"  trades: {int(summary.get('trade_backlog_count') or 0)}")
            print(f"  policy: {summary.get('storage_policy')}")
            candidates = report.get("candidate_backlog") or []
            if candidates:
                print("\nCandidates:")
                for row in candidates[: args.limit]:
                    status = "active" if row.get("active") else "inactive"
                    print(
                        f"  {status} | seen={row.get('seen_count')} | "
                        f"{row.get('route_type')} | {row.get('venue_scope')} | "
                        f"best=${float(row.get('best_expected_net_profit') or 0):.2f} | "
                        f"last=${float(row.get('expected_net_profit') or 0):.2f} | "
                        f"{row.get('title')}"
                    )
            trades = report.get("trade_backlog") or []
            if trades:
                print("\nTrades:")
                for row in trades[: args.limit]:
                    print(
                        f"  {row.get('source_type')} | {row.get('status')} | "
                        f"{row.get('route_type')} | "
                        f"sim=${float(row.get('simulated_net_profit') or 0):.2f} | "
                        f"{row.get('title')}"
                    )
            return 0

        if args.command == "prediction-mappings-import":
            store.init_db()
            try:
                mappings = load_prediction_verified_mapping_file(Path(args.input))
            except PredictionMappingError as exc:
                print(f"Prediction mapping import failed: {exc}", file=sys.stderr)
                return 2
            if args.dry_run:
                print("Prediction trusted mapping import dry run")
                print(f"  valid mappings: {len(mappings)}")
                return 0
            inserted = store.upsert_prediction_verified_contract_mappings(mappings)
            print("Prediction trusted mappings imported")
            print(f"  mappings: {inserted}")
            return 0

        if args.command == "prediction-mappings-report":
            store.init_db()
            mappings = store.prediction_verified_contract_mappings()
            print("Prediction trusted mappings")
            if not mappings:
                print("  нет trusted mappings")
                return 0
            for row in mappings[: max(0, int(args.limit))]:
                print(
                    f"  {row['venue_a']}:{row['market_id_a']} <=> "
                    f"{row['venue_b']}:{row['market_id_b']} | "
                    f"confidence={float(row.get('confidence_score') or 0):.3f} | "
                    f"status={row.get('status')}"
                )
            return 0

        if args.command == "risex-status":
            from smart_money_radar.risex.points import (
                RiseXPointsClient,
                fetch_leaderboard_snapshot,
                fetch_points_epoch,
            )
            from smart_money_radar.risex.farming import (
                RiseXFarmingConfig,
                estimate_farming_economics,
            )

            client = RiseXPointsClient()
            epoch = fetch_points_epoch(client)
            print("RiseX Status")
            print(f"  epoch: {epoch.get('epoch_id', '-')} ({epoch.get('description', '-')})")
            dist_secs = epoch.get("seconds_until_distribution", 0)
            print(f"  distribution in: {dist_secs / 3600:.1f}h")
            print()

            lb = fetch_leaderboard_snapshot(client, timeframe=args.timeframe, limit=args.limit)
            print(f"Volume leaderboard ({args.timeframe})")
            print(f"  total notional: ${lb['total_notional_volume']:,.0f}")
            print(f"  top notional: ${lb['top_notional_volume']:,.0f}")
            print(f"  median notional: ${lb['median_notional_volume']:,.0f}")
            for e in lb["entries"][: args.limit]:
                print(
                    f"  #{e['rank']:>3} | notional=${e['notional_volume']:>14,.0f} | "
                    f"share={e['notional_share_pct']:.2f}% | trades={e['trades']:>6}"
                )
            print()

            econ = estimate_farming_economics(RiseXFarmingConfig(
                target_notional_usd=10_000,
                cycles_per_day=12,
            ))
            print("Farming estimate ($10K notional, 12 cycles/day)")
            print(f"  weekly volume: ${econ['volume']['weekly_usd']:,.0f}")
            print(f"  weekly fees: ${econ['costs']['weekly_fees_usd']:,.2f}")
            print(f"  weekly leaderboard reward: ${econ['revenue']['weekly_leaderboard_usd']:,.2f}")
            print(f"  weekly funding income: ${econ['revenue']['weekly_funding_usd']:,.2f}")
            print(f"  weekly net P&L: ${econ['net_pnl']['weekly_usd']:,.2f}")
            print(f"  volume share: {econ['revenue']['volume_share_pct']:.3f}%")
            print(f"  est. weekly points: {econ['revenue']['weekly_points_est']:.0f}")
            return 0

        if args.command == "risex-leaderboard":
            from smart_money_radar.risex.points import (
                RiseXPointsClient,
                fetch_leaderboard_snapshot,
            )

            client = RiseXPointsClient()
            lb = fetch_leaderboard_snapshot(client, timeframe=args.timeframe, limit=args.limit)
            print(f"RiseX Volume Leaderboard ({args.timeframe})")
            print(f"  total notional: ${lb['total_notional_volume']:,.0f}")
            print(f"  entries: {lb['entry_count']}")
            print()
            for e in lb["entries"]:
                print(
                    f"  #{e['rank']:>3} | {e['address'][:10]}... | "
                    f"notional=${e['notional_volume']:>14,.0f} | "
                    f"referral=${e['referral_volume']:>14,.0f} | "
                    f"combined=${e['combined_volume']:>14,.0f} | "
                    f"trades={e['trades']:>6} | "
                    f"share={e['notional_share_pct']:.2f}%"
                )
            return 0

        if args.command == "risex-farming-estimate":
            from smart_money_radar.risex.farming import (
                RiseXFarmingConfig,
                estimate_farming_economics,
                paper_farming_cycle,
            )

            config = RiseXFarmingConfig(
                target_notional_usd=args.notional,
                cycles_per_day=args.cycles,
                hedge_venue=args.hedge_venue,
            )
            econ = estimate_farming_economics(config)
            print("RiseX Volume Farming Estimate")
            print(f"  notional: ${econ['config']['target_notional_usd']:,.0f}")
            print(f"  cycles/day: {econ['config']['cycles_per_day']}")
            print(f"  hedge venue: {econ['config']['hedge_venue']}")
            print(f"  RiseX fee: {econ['config']['risex_fee_rate_bps']:.1f} bps")
            print(f"  hedge fee: {econ['config']['hedge_fee_rate_bps']:.1f} bps")
            print(f"  point boost: {econ['config']['point_boost_pct']:.0f}%")
            print()
            print("Volume")
            print(f"  daily: ${econ['volume']['daily_usd']:,.0f}")
            print(f"  weekly: ${econ['volume']['weekly_usd']:,.0f}")
            print(f"  monthly: ${econ['volume']['monthly_usd']:,.0f}")
            print()
            print("Costs")
            print(f"  daily fees: ${econ['costs']['daily_fees_usd']:,.2f}")
            print(f"  weekly fees: ${econ['costs']['weekly_fees_usd']:,.2f}")
            print(f"  monthly fees: ${econ['costs']['monthly_fees_usd']:,.2f}")
            print()
            print("Revenue")
            print(f"  weekly leaderboard: ${econ['revenue']['weekly_leaderboard_usd']:,.2f}")
            print(f"  weekly funding: ${econ['revenue']['weekly_funding_usd']:,.2f}")
            print(f"  monthly funding: ${econ['revenue']['monthly_funding_usd']:,.2f}")
            print(f"  weekly points (est): {econ['revenue']['weekly_points_est']:.0f}")
            print(f"  volume share: {econ['revenue']['volume_share_pct']:.3f}%")
            print()
            print("Net P&L")
            print(f"  weekly: ${econ['net_pnl']['weekly_usd']:,.2f}")
            print(f"  monthly: ${econ['net_pnl']['monthly_usd']:,.2f}")
            print()
            print("Breakeven")
            print(f"  min volume share: {econ['breakeven']['min_volume_share_pct']:.3f}%")
            print(f"  min weekly volume: ${econ['breakeven']['min_weekly_volume_usd']:,.0f}")
            print()

            cycle = paper_farming_cycle(config, cycle_number=1)
            print("Paper cycle #1")
            print(f"  notional: ${cycle['notional_usd']:,.0f}")
            print(f"  RiseX fee: ${cycle['risex_fee_usd']:,.4f}")
            print(f"  hedge fee: ${cycle['hedge_fee_usd']:,.4f}")
            print(f"  funding earned: ${cycle['funding_earned_usd']:,.4f}")
            print(f"  net P&L: ${cycle['net_pnl_usd']:,.4f}")
            print(f"  volume generated: ${cycle['volume_generated_usd']:,.0f}")
            return 0

        if args.command == "risex-bot":
            from smart_money_radar.risex.bot import RiseXBot, RiseXBotConfig

            store.init_db()
            bot_config = RiseXBotConfig(
                venue_starting_balance=args.balance,
                target_notional_per_leg=args.notional,
                scan_interval_seconds=args.scan_interval,
                status_report_interval_seconds=args.report_interval,
                iterations=args.iterations,
                telegram_enabled=not args.no_telegram,
                hedge_venues=tuple(args.hedge_venues),
                funding_carry_enabled=not args.no_funding_carry,
                spread_arb_enabled=not args.no_spread_arb,
            ).validated()
            bot = RiseXBot(
                store,
                config=bot_config,
                notifier=TelegramNotifier(
                    token_env_var="RISEX_TELEGRAM_BOT_TOKEN",
                    chat_id_env_var="RISEX_TELEGRAM_CHAT_ID",
                ),
            )
            bot.run_loop()
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
            report = store.funding_dashboard()
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
                ).as_dict()
                print("Funding retention dry run")
                rows = result["rows_by_table"]
            print(f"  total scans: {result['total_scan_count']}")
            print(f"  protected scans: {result['protected_scan_count']}")
            print(f"  delete scans: {result['delete_scan_count']}")
            for table, count in rows.items():
                print(f"  {table}: {count}")
            if not args.apply:
                print("  apply: rerun with --apply to delete these rows")
            else:
                print("  note: run sqlite VACUUM separately to return disk space to the OS")
            return 0

        if args.command == "funding-paper-trader":
            store.init_db()
            trader = FundingPaperTrader(
                store,
                config=funding_paper_trader_config(args),
                notifier=TelegramNotifier(
                    token_env_var="FUNDING_TELEGRAM_BOT_TOKEN",
                    chat_id_env_var="FUNDING_TELEGRAM_CHAT_ID",
                ),
            )
            trader.run_loop()
            return 0

        if args.command == "funding-paper-report":
            store.init_db()
            report = store.funding_paper_dashboard()
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
            chats = telegram_update_chat_ids()
            if not chats:
                print(
                    "No Telegram chats found yet. Send any message to the bot, "
                    "then run this command again."
                )
                return 1
            print("Telegram chats found:")
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
            result = TelegramNotifier().send("Smart Money Radar Telegram test: ok")
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
    except AnalyticsError as exc:
        print(f"Analytics error: {exc}", file=sys.stderr)
        return 1
    except PredictionDataError as exc:
        print(f"Prediction market data error: {exc}", file=sys.stderr)
        return 1
    except FundingDataError as exc:
        print(f"Funding market data error: {exc}", file=sys.stderr)
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

    subparsers.add_parser(
        "analytics-init",
        help="Initialize local Radar Analytics views and built-in queries.",
    )

    subparsers.add_parser(
        "analytics-list",
        help="List local Radar Analytics datasets and saved queries.",
    )

    analytics_run = subparsers.add_parser(
        "analytics-run",
        help="Run a saved local analytics query without Dune.",
    )
    analytics_run.add_argument("--slug", required=True)
    analytics_run.add_argument("--limit", type=int, default=100)
    analytics_run.add_argument("--json", action="store_true")

    analytics_query = subparsers.add_parser(
        "analytics-query",
        help="Run a read-only local SQL analytics query without Dune.",
    )
    sql_source = analytics_query.add_mutually_exclusive_group(required=True)
    sql_source.add_argument("--sql")
    sql_source.add_argument("--sql-file")
    analytics_query.add_argument("--limit", type=int, default=100)
    analytics_query.add_argument("--json", action="store_true")

    subparsers.add_parser(
        "dune-health",
        help="Check whether Dune API configuration is present.",
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

    prediction_scan = subparsers.add_parser(
        "prediction-scan",
        help="Collect prediction-market books, scan routes, and paper execute.",
    )
    add_prediction_scan_arguments(prediction_scan)

    prediction_watch = subparsers.add_parser(
        "prediction-watch",
        help="Continuously refresh the Prediction Radar paper pipeline.",
    )
    add_prediction_scan_arguments(prediction_watch)
    prediction_watch.add_argument("--interval-seconds", type=int, default=300)
    prediction_watch.add_argument("--iterations", type=int)

    prediction_bot = subparsers.add_parser(
        "prediction-bot",
        help="Run Prediction Radar every 5 minutes and send Telegram status reports.",
    )
    prediction_bot.add_argument("--scan-interval-seconds", type=int, default=300)
    prediction_bot.add_argument(
        "--status-report-interval-seconds",
        type=int,
        default=3_600,
        help="Telegram status report interval; use 0 to disable.",
    )
    prediction_bot.add_argument(
        "--status-report-max-routes",
        type=int,
        default=5,
        help="Maximum prediction routes included in each status report.",
    )
    prediction_bot.add_argument("--events-per-venue", type=int, default=75)
    prediction_bot.add_argument("--max-markets-per-venue", type=int, default=1_500)
    prediction_bot.add_argument("--kalshi-market-pages", type=int, default=10)
    prediction_bot.add_argument("--http-timeout-seconds", type=int, default=8)
    prediction_bot.add_argument("--http-max-retries", type=int, default=1)
    prediction_bot.add_argument("--paper-size", type=float, default=100.0)
    prediction_bot.add_argument("--paper-latency-ms", type=int, default=750)
    prediction_bot.add_argument("--paper-depth-haircut", type=float, default=0.8)
    prediction_bot.add_argument("--skip-hyperliquid", action="store_true")
    prediction_bot.add_argument("--iterations", type=int)
    prediction_bot.add_argument("--no-telegram", action="store_true")
    prediction_bot.add_argument(
        "--lifecycle-telegram",
        action="store_true",
        help="Also send normal Prediction Radar start/stop Telegram notifications.",
    )

    prediction_report = subparsers.add_parser(
        "prediction-report",
        help="Print the latest research-only prediction-market paper candidates.",
    )
    prediction_report.add_argument("--limit", type=int, default=20)

    prediction_backlog_report = subparsers.add_parser(
        "prediction-backlog-report",
        help="Print bounded Prediction Radar candidate/trade backlog.",
    )
    prediction_backlog_report.add_argument("--limit", type=int, default=20)

    prediction_mappings_import = subparsers.add_parser(
        "prediction-mappings-import",
        help="Import manually verified Polymarket/Kalshi equivalent contracts.",
    )
    prediction_mappings_import.add_argument("--input", required=True)
    prediction_mappings_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the number of mappings without writing to SQLite.",
    )

    prediction_mappings_report = subparsers.add_parser(
        "prediction-mappings-report",
        help="Print trusted prediction cross-venue contract mappings.",
    )
    prediction_mappings_report.add_argument("--limit", type=int, default=50)

    risex_status = subparsers.add_parser(
        "risex-status",
        help="Show RiseX points epoch, leaderboard snapshot, and farming economics.",
    )
    risex_status.add_argument("--timeframe", default="7d", choices=("24h", "7d", "30d", "all"))
    risex_status.add_argument("--limit", type=int, default=10)

    risex_leaderboard = subparsers.add_parser(
        "risex-leaderboard",
        help="Print RiseX volume leaderboard.",
    )
    risex_leaderboard.add_argument("--timeframe", default="7d", choices=("24h", "7d", "30d", "all"))
    risex_leaderboard.add_argument("--limit", type=int, default=20)

    risex_farming = subparsers.add_parser(
        "risex-farming-estimate",
        help="Estimate RiseX volume farming economics.",
    )
    risex_farming.add_argument("--notional", type=float, default=10_000)
    risex_farming.add_argument("--cycles", type=int, default=12)
    risex_farming.add_argument("--hedge-venue", default="binance")

    risex_bot = subparsers.add_parser(
        "risex-bot",
        help="Run RiseX paper trading bot with funding carry and spread arb (scanner pipeline, no artificial BPS filters).",
    )
    risex_bot.add_argument("--balance", type=float, default=2_000.0, help="Starting balance per venue (default 2000)")
    risex_bot.add_argument("--notional", type=float, default=500.0)
    risex_bot.add_argument("--scan-interval", type=int, default=180)
    risex_bot.add_argument("--report-interval", type=int, default=900)
    risex_bot.add_argument("--iterations", type=int)
    risex_bot.add_argument("--no-telegram", action="store_true")
    risex_bot.add_argument(
        "--hedge-venues",
        nargs="+",
        default=["hyperliquid", "dydx", "lighter", "variational"],
        choices=("hyperliquid", "dydx", "lighter", "variational"),
    )
    risex_bot.add_argument("--no-funding-carry", action="store_true")
    risex_bot.add_argument("--no-spread-arb", action="store_true")

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
            "kraken", "kucoin", "lighter", "mexc", "okx", "paradex",
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
    funding_paper_trader.add_argument("--entry-window-seconds", type=int, default=180)
    funding_paper_trader.add_argument("--entry-min-lead-seconds", type=int, default=0)
    funding_paper_trader.add_argument("--entry-max-lead-seconds", type=int, default=15)
    funding_paper_trader.add_argument("--arm-window-seconds", type=int, default=900)
    funding_paper_trader.add_argument(
        "--final-recheck-freeze-seconds",
        type=int,
        default=15,
        help="Skip fresh API rechecks inside this final pre-settlement window.",
    )
    funding_paper_trader.add_argument(
        "--max-entry-snapshot-age-seconds",
        type=int,
        default=30,
        help="Maximum age of the last successful focused snapshot usable for paper entry.",
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
        type=int,
        default=120,
    )
    funding_paper_trader.add_argument("--hot-interval-seconds", type=int, default=10)
    funding_paper_trader.add_argument(
        "--hot-route-recheck-workers",
        type=int,
        default=6,
        help="Maximum focused route rechecks to run in parallel during hot monitoring.",
    )
    funding_paper_trader.add_argument(
        "--status-report-interval-seconds",
        type=int,
        default=1_800,
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
    funding_paper_trader.add_argument("--spread-arb", action="store_true")
    funding_paper_trader.add_argument(
        "--basis-stop-loss-bps",
        type=float,
        default=200.0,
        help="Close position when unrealized basis loss exceeds this many bps.",
    )
    funding_paper_trader.add_argument("--no-spread-monitoring", action="store_true")

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

    subparsers.add_parser(
        "telegram-chat-id",
        help="Print Telegram chat IDs from bot getUpdates.",
    )

    subparsers.add_parser(
        "telegram-test",
        help="Send a small Telegram test message using .env settings.",
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


def add_prediction_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--events-per-venue", type=int, default=75)
    parser.add_argument("--max-markets-per-venue", type=int, default=1_500)
    parser.add_argument("--kalshi-market-pages", type=int, default=10)
    parser.add_argument("--http-timeout-seconds", type=int, default=8)
    parser.add_argument("--http-max-retries", type=int, default=1)
    parser.add_argument("--wallet-limit", type=int, default=10)
    parser.add_argument("--wallet-position-limit", type=int, default=60)
    parser.add_argument("--wallet-refresh-hours", type=float, default=24.0)
    parser.add_argument("--paper-size", type=float, default=100.0)
    parser.add_argument("--paper-latency-ms", type=int, default=750)
    parser.add_argument("--paper-depth-haircut", type=float, default=0.8)
    parser.add_argument("--retention-scans", type=int, default=1)
    parser.add_argument("--dashboard-freshness-minutes", type=float, default=15.0)
    parser.add_argument("--skip-wallets", action="store_true")
    parser.add_argument("--skip-hyperliquid", action="store_true")


def prediction_scan_config(args: argparse.Namespace) -> PredictionScanConfig:
    return PredictionScanConfig(
        events_per_venue=args.events_per_venue,
        max_markets_per_venue=args.max_markets_per_venue,
        kalshi_market_pages=args.kalshi_market_pages,
        http_timeout_seconds=args.http_timeout_seconds,
        http_max_retries=args.http_max_retries,
        wallet_limit=args.wallet_limit,
        wallet_position_limit=args.wallet_position_limit,
        wallet_refresh_hours=args.wallet_refresh_hours,
        paper_size=args.paper_size,
        paper_latency_ms=args.paper_latency_ms,
        paper_depth_haircut=args.paper_depth_haircut,
        retention_scans=args.retention_scans,
        dashboard_freshness_minutes=args.dashboard_freshness_minutes,
        collect_wallets=not args.skip_wallets,
        collect_hyperliquid=not args.skip_hyperliquid,
    )


def prediction_bot_config(args: argparse.Namespace) -> PredictionBotConfig:
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


def funding_paper_trader_config(args: argparse.Namespace) -> FundingPaperTraderConfig:
    venue_set_raw = getattr(args, "venue_set", None)
    venue_set = (
        tuple(v.strip() for v in str(venue_set_raw).split(",") if v.strip())
        if venue_set_raw
        else None
    )
    return FundingPaperTraderConfig(
        venue_starting_balance=args.venue_starting_balance,
        target_notional_per_leg=args.target_notional,
        entry_window_seconds=args.entry_window_seconds,
        entry_min_lead_seconds=args.entry_min_lead_seconds,
        entry_max_lead_seconds=args.entry_max_lead_seconds,
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
        spread_monitoring_enabled=not getattr(args, "no_spread_monitoring", False),
    ).validated()


def print_analytics_result(result: dict[str, object], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    rows = result["rows"]
    columns = result["columns"]
    assert isinstance(rows, list)
    assert isinstance(columns, list)
    print(
        "Analytics execution "
        f"{result['analytics_execution_id']} | "
        f"engine={result['engine']} | rows={result['row_count']} | "
        f"limit={result['limit']}"
    )
    if not rows:
        print("No rows.")
        return

    visible_columns = [str(column) for column in columns[:8]]
    widths = {
        column: min(
            36,
            max(
                len(column),
                *[
                    len(format_analytics_cell(row.get(column)))
                    for row in rows[:20]
                    if isinstance(row, dict)
                ],
            ),
        )
        for column in visible_columns
    }
    header = " | ".join(column.ljust(widths[column]) for column in visible_columns)
    print(header)
    print("-+-".join("-" * widths[column] for column in visible_columns))
    for row in rows:
        assert isinstance(row, dict)
        print(
            " | ".join(
                format_analytics_cell(row.get(column))[: widths[column]].ljust(
                    widths[column]
                )
                for column in visible_columns
            )
        )
    if len(columns) > len(visible_columns):
        hidden = ", ".join(str(column) for column in columns[len(visible_columns) :])
        print(f"... hidden columns: {hidden}")


def format_analytics_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


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
    print(f"  Dune executions: {summary['dune_execution_count']}")
    print(f"  Pre-listing wallet buys: {summary['pre_listing_wallet_buy_count']}")
    print(f"  Wallet holding metrics: {summary['wallet_holding_metric_count']}")
    print(f"  Wallet scores: {summary['wallet_score_count']}")
    print(f"  DUNE_API_KEY: {'present' if api_status['DUNE_API_KEY'] else 'missing'}")
    print("")
    print("Next checks")
    if not api_status["DUNE_API_KEY"]:
        print("  1. Add DUNE_API_KEY to .env to run Dune queries.")
    print("  2. Run: python3 -m smart_money_radar.cli funding-scan --target-notional 10000")
    print("  3. Run: python3 -m smart_money_radar.cli prediction-scan")


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
