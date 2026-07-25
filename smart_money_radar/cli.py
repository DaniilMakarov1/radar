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
from smart_money_radar.attention import enrich_attention_snapshots
from smart_money_radar.backtest import run_wallet_walk_forward
from smart_money_radar.backfill_plan import prepare_dune_backfill
from smart_money_radar.config import DEFAULT_DB_PATH, api_key_status
from smart_money_radar.contract_validation import (
    validate_base_contract_mappings,
    validate_evm_contract_mappings,
)
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
from smart_money_radar.dune_queries import (
    write_generated_contract_discovery_sql,
    write_generated_holding_sql,
    write_generated_pre_listing_threshold_probe_sql,
    write_generated_negative_control_sql,
    write_generated_sql,
)
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient, write_json
from smart_money_radar.ingestion.binance_announcements import (
    BinanceAnnouncementClient,
    BinanceAnnouncementCollector,
    BinanceAnnouncementError,
)
from smart_money_radar.notifications import (
    TelegramNotifier,
    telegram_update_chat_ids,
)
from smart_money_radar.live_radar import (
    enrich_onchain_snapshots,
    enrich_social_snapshots,
    recompute_live_signals,
    run_base_live_scan,
)
from smart_money_radar.local_radar import run_base_local_live_scan
from smart_money_radar.identity_graph import run_identity_graph_backfill
from smart_money_radar.ml import train_model_suite
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
from smart_money_radar.research_pipeline import (
    UNIVERSE_EXPORT_COLUMNS,
    run_cross_chain_listing_history_backfill,
    run_solana_listing_history_backfill,
    run_solana_universe_backfill,
    run_wallet_weekly_flow_backfill,
    run_weekly_universe_threshold_probe,
    run_weekly_universe_backfill,
)
from smart_money_radar.research_validity import run_research_validity
from smart_money_radar.reports import write_report
from smart_money_radar.scoring.wallets import MODEL_VERSION, score_wallet_rows
from smart_money_radar.storage import SQLiteStore
from smart_money_radar.wallet_intelligence import (
    rebuild_wallet_clusters,
    run_wallet_intelligence,
)
from smart_money_radar.wallet_research import (
    rescore_wallet_opportunities,
    run_wallet_opportunity_backfill,
)


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

        if args.command == "collect-binance":
            store.init_db()
            run_id = store.start_run("binance_announcements")
            client = (
                None
                if args.request_delay is None
                else BinanceAnnouncementClient(min_delay_seconds=args.request_delay)
            )
            collector = BinanceAnnouncementCollector(client=client)
            seen = 0
            written = 0
            try:
                for announcement in collector.collect(
                    start_page=args.start_page,
                    pages=args.pages,
                    page_size=args.page_size,
                    with_details=args.with_details or args.listing_details_only,
                    listing_details_only=args.listing_details_only,
                ):
                    seen += 1
                    store.upsert_binance_announcement(announcement)
                    written += 1
                store.finish_run(run_id, "success", seen, written)
            except Exception as exc:
                store.finish_run(run_id, "failed", seen, written, str(exc))
                raise
            print(
                "Collected Binance announcements: "
                f"seen={seen}, written={written}, db={store.db_path}"
            )
            if collector.detail_errors:
                print(
                    "Detail fetches skipped after retries: "
                    f"{len(collector.detail_errors)}"
                )
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

        if args.command in {"verify-base-contracts", "verify-evm-contracts"}:
            store.init_db()
            chain_id = "base" if args.command == "verify-base-contracts" else args.chain
            result = (
                validate_base_contract_mappings(store)
                if chain_id == "base"
                else validate_evm_contract_mappings(store, chain_id)
            )
            print(f"{chain_id} contract verification:")
            print(f"  checked: {result['checked']}")
            print(f"  verified: {result['verified']}")
            print(f"  mismatched: {result['mismatched']}")
            print(f"  failed: {result['failed']}")
            for row in result["results"]:
                observed = row.get("observed_symbol") or "-"
                print(
                    f"  {row['symbol']:<12} -> {observed:<12} | "
                    f"{row['validation_status']} | {row['contract_address']}"
                )
            return 0

        if args.command == "prepare-dune-backfill":
            store.init_db()
            result = prepare_dune_backfill(store)
            print("Dune V4 backfill package prepared without execution")
            print(f"  wallets: {result['wallet_count']}")
            print(f"  artifacts: {len(result['artifacts'])}")
            print(f"  manifest: {result['manifest_path']}")
            for artifact in result["artifacts"]:
                print(
                    f"  {'ok' if artifact['valid'] else 'invalid'} | "
                    f"{artifact['kind']} | {artifact['path']}"
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

        if args.command == "report":
            store.init_db()
            markdown_path, html_path = write_report(
                store,
                markdown_path=Path(args.output),
                html_path=Path(args.html_output),
            )
            print(f"Wrote report: {markdown_path}")
            print(f"Wrote HTML report: {html_path}")
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

        if args.command == "render-dune-base-buyers":
            store.init_db()
            targets = store.chain_backtest_targets("base")
            count = write_generated_sql(
                Path(args.output),
                targets,
                lookback_days=args.lookback_days,
                minimum_total_buy_usd=args.minimum_total_buy_usd,
                max_wallets_per_target=args.max_wallets_per_target,
            )
            print(f"Rendered Dune SQL for {count} Base target(s): {args.output}")
            if count == 0:
                print("No Base-ready targets yet. Add/verify Base contract mappings first.")
            return 0

        if args.command == "render-dune-base-holding":
            store.init_db()
            targets = store.chain_backtest_targets("base")
            count = write_generated_holding_sql(
                Path(args.output),
                targets,
                lookback_days=args.lookback_days,
                post_window_days=args.post_window_days,
                minimum_pre_buy_usd=args.minimum_pre_buy_usd,
                max_wallets_per_target=args.max_wallets_per_target,
            )
            print(f"Rendered Dune SQL for {count} Base holding target(s): {args.output}")
            if count == 0:
                print("No Base-ready targets yet. Add/verify Base contract mappings first.")
            return 0

        if args.command == "render-dune-base-contract-discovery":
            store.init_db()
            targets = store.backtest_targets()
            count = write_generated_contract_discovery_sql(
                output_path=Path(args.output),
                targets=targets,
                lookback_days=args.lookback_days,
            )
            print(
                "Rendered point-in-time Base contract-discovery SQL for "
                f"{count} Binance target(s): {args.output}"
            )
            return 0

        if args.command == "render-dune-base-threshold-probe":
            store.init_db()
            targets = store.chain_backtest_targets("base")
            count = write_generated_pre_listing_threshold_probe_sql(
                output_path=Path(args.output),
                targets=targets,
                lookback_days=args.lookback_days,
            )
            print(
                "Rendered Base historical-threshold probe for "
                f"{count} target(s): {args.output}"
            )
            return 0

        if args.command == "render-dune-negative-controls":
            store.init_db()
            backtest = store.latest_backtest("wallet_walk_forward")
            evaluations = backtest.get("evaluations", []) if backtest else []
            count = write_generated_negative_control_sql(
                output_path=Path(args.output),
                evaluations=evaluations,
                window_days=args.window_days,
                min_trade_usd=args.minimum_trade_usd,
            )
            print(
                "Rendered Base negative-control SQL for "
                f"{count} wallet-snapshot cohort rows: {args.output}"
            )
            return 0

        if args.command == "dune-health":
            statuses = api_key_status()
            print("Dune/API health:")
            print(f"  DUNE_API_KEY: {'present' if statuses['DUNE_API_KEY'] else 'missing'}")
            if not statuses["DUNE_API_KEY"]:
                print("  Next: add DUNE_API_KEY to .env before executing Dune SQL.")
            return 0

        if args.command == "dune-usage":
            usage = DuneClient().usage()
            print("Dune billing-period usage")
            for period in usage.get("billing_periods") or []:
                print(
                    f"  {period.get('start_date')}..{period.get('end_date')}: "
                    f"{period.get('credits_used', 0)} / "
                    f"{period.get('credits_included', 0)} credits"
                )
            return 0

        if args.command == "dune-export-plan":
            client = DuneClient()
            status = client.execution_status(args.execution_id)
            columns = (
                UNIVERSE_EXPORT_COLUMNS
                if args.columns_profile == "universe"
                else None
            )
            plan = client.export_cost_plan(status, columns=columns)
            plan["remaining_credits"] = client.remaining_credits()
            remaining = plan["remaining_credits"]
            estimated = float(plan["estimated_export_credits"])
            plan["affordable_with_10pct_reserve"] = bool(
                remaining is None or estimated * 1.1 <= remaining * 0.9
            )
            print("Dune export plan (no result rows downloaded)")
            for key, value in plan.items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "dune-execute-sql":
            store.init_db()
            sql_file = Path(args.sql_file)
            output = Path(args.output)
            sql = sql_file.read_text(encoding="utf-8")
            if args.dry_run:
                print(f"Dry run only. SQL file is ready: {sql_file}")
                print(f"Output would be written to: {output}")
                print(f"SQL length: {len(sql)} characters")
                return 0

            client = DuneClient()
            execution = client.execute_sql(sql, performance=args.performance)
            execution_id = execution["execution_id"]
            store.record_dune_execution(
                execution_id=execution_id,
                source="dune_sql",
                state=execution.get("state", "submitted"),
                sql_file=str(sql_file),
                output_file=str(output),
            )
            print(f"Submitted Dune execution: {execution_id}")

            if args.poll:
                try:
                    result = client.poll_results(
                        execution_id,
                        timeout_seconds=args.timeout_seconds,
                        poll_interval_seconds=args.poll_interval_seconds,
                    )
                    write_json(output, result)
                    row_count = (
                        result.get("result", {})
                        .get("metadata", {})
                        .get("row_count")
                    )
                    store.finish_dune_execution(
                        execution_id,
                        state=result.get("state", "QUERY_STATE_COMPLETED"),
                        row_count=row_count,
                        output_file=str(output),
                    )
                    print(f"Wrote Dune results: {output}")
                    print(f"Rows: {row_count}")
                except DuneAPIError as exc:
                    store.finish_dune_execution(
                        execution_id,
                        state="failed",
                        error=str(exc),
                        output_file=str(output),
                    )
                    raise
            return 0

        if args.command == "dune-fetch-results":
            store.init_db()
            client = DuneClient()
            result = client.execution_results_all(args.execution_id)
            output = Path(args.output)
            write_json(output, result)
            row_count = (
                result.get("result", {})
                .get("metadata", {})
                .get("fetched_row_count")
            )
            store.finish_dune_execution(
                args.execution_id,
                state=result.get("state", "QUERY_STATE_COMPLETED"),
                row_count=row_count,
                output_file=str(output),
            )
            print(f"Fetched Dune results: {output}")
            print(f"Rows: {row_count}")
            return 0

        if args.command == "import-base-buyers":
            store.init_db()
            data = json_load(Path(args.input))
            rows = data.get("result", {}).get("rows", [])
            execution_id = data.get("execution_id")
            count = store.import_pre_listing_wallet_buys(
                chain_id="base",
                execution_id=execution_id,
                rows=rows,
            )
            print(f"Imported {count} Base pre-listing wallet buy rows")
            return 0

        if args.command == "import-base-holding":
            store.init_db()
            data = json_load(Path(args.input))
            rows = data.get("result", {}).get("rows", [])
            execution_id = data.get("execution_id")
            count = store.import_wallet_holding_metrics(
                chain_id="base",
                execution_id=execution_id,
                rows=rows,
            )
            print(f"Imported {count} Base wallet holding metric rows")
            return 0

        if args.command == "import-base-contract-discovery":
            store.init_db()
            data = json_load(Path(args.input))
            rows = data.get("result", {}).get("rows", [])
            stats = store.import_base_contract_discovery_candidates(
                rows,
                minimum_notional_usd=args.minimum_notional_usd,
                minimum_notional_share=args.minimum_notional_share,
                minimum_trade_count=args.minimum_trade_count,
                approved_symbols=(
                    {
                        symbol.strip().upper()
                        for symbol in args.approved_symbols.split(",")
                        if symbol.strip()
                    }
                    if args.approved_symbols
                    else None
                ),
            )
            print("Imported Base pre-announcement contract-discovery candidates:")
            for key, value in stats.items():
                print(f"  {key}: {value}")
            print("Run verify-base-contracts before using new targets in Dune history.")
            return 0

        if args.command == "import-negative-controls":
            store.init_db()
            data = json_load(Path(args.input))
            rows = data.get("result", {}).get("rows", [])
            execution_id = data.get("execution_id")
            wallet_backtest = store.latest_backtest("wallet_walk_forward")
            evaluations = wallet_backtest.get("evaluations", []) if wallet_backtest else []
            expected_snapshots = sum(
                1 for row in evaluations if row.get("predicted_wallet_count", 0) > 0
            )
            run_id = store.start_backtest_run(
                model_version=MODEL_VERSION,
                backtest_type="token_candidate_negative_control",
                config={
                    "source_execution_id": execution_id,
                    "expected_snapshot_count": expected_snapshots,
                    "candidate_universe": "tokens_touched_by_prior_qualified_wallets",
                },
            )
            metrics = store.import_negative_control_candidates(
                backtest_run_id=run_id,
                execution_id=execution_id,
                chain_id="base",
                rows=rows,
                expected_snapshot_count=expected_snapshots,
            )
            checks = {
                "wallet_cohorts_built_from_prior_mature_targets": True,
                "trades_cut_off_at_snapshot": True,
                "negative_control_universe_present": metrics["negative_candidate_count"] > 0,
                "historical_liquidity_snapshots_present": False,
                "historical_risk_snapshots_present": False,
            }
            store.finish_backtest_run(
                backtest_run_id=run_id,
                status="completed",
                metrics=metrics,
                leakage_checks=checks,
                notes=(
                    "Negative controls cover tokens touched by qualified historical "
                    "wallets. Historical liquidity/risk enrichment is still required."
                ),
            )
            print(f"Imported {metrics['candidate_count']} negative-control candidates")
            print(f"  snapshots: {metrics['snapshot_count']}")
            print(f"  negatives: {metrics['negative_candidate_count']}")
            print(f"  hit rate @1: {format_optional_ratio(metrics['hit_rate_at_1'])}")
            print(f"  hit rate @5: {format_optional_ratio(metrics['hit_rate_at_5'])}")
            print(
                "  token prediction claims ready: "
                f"{metrics['ready_for_token_prediction_claims']}"
            )
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

        if args.command == "score-wallets":
            store.init_db()
            rows = store.pre_listing_wallet_buy_rows()
            holding_rows = store.wallet_holding_metric_rows()
            scores = score_wallet_rows(
                rows,
                holding_rows=holding_rows,
                dataset_target_count=store.observed_target_count(),
            )
            count = store.upsert_wallet_scores(scores)
            print(f"Scored {count} wallet(s) with {MODEL_VERSION}")
            return 0

        if args.command == "wallet-intelligence":
            store.init_db()
            result = run_wallet_intelligence(
                store=store,
                max_wallets=args.max_wallets,
                entity_chunk_size=args.entity_chunk_size,
                funding_chunk_size=args.funding_chunk_size,
            )
            print("Wallet intelligence completed")
            print(
                "  entity rows: "
                f"{result['entities']['imported_row_count']} / {result['entities']['wallet_count']} wallets"
            )
            print(
                "  excluded entities: "
                f"{result['entities']['entity_summary']['excluded_count']}"
            )
            print(
                "  funding rows: "
                f"{result['funding']['imported_row_count']} / {result['funding']['wallet_count']} wallets"
            )
            print(f"  clusters: {result['clusters']['cluster_count']}")
            print(f"  linked clusters: {result['clusters']['linked_cluster_count']}")
            return 0

        if args.command == "rebuild-wallet-clusters":
            store.init_db()
            result = rebuild_wallet_clusters(store=store, max_wallets=args.max_wallets)
            print("Wallet clusters rebuilt")
            for key, value in result.items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "enrich-social":
            store.init_db()
            count = enrich_social_snapshots(store, max_tokens=args.max_tokens)
            signals = recompute_live_signals(store)
            print(f"Stored {count} social snapshot(s)")
            print(f"Recomputed {len(signals)} signal(s)")
            return 0

        if args.command == "enrich-onchain":
            store.init_db()
            result = enrich_onchain_snapshots(store, max_tokens=args.max_tokens)
            signals = recompute_live_signals(store)
            print("Stored on-chain due-diligence snapshots")
            print(f"  targets: {result['target_count']}")
            print(f"  snapshots: {result['snapshot_count']}")
            for source, count in sorted(result["by_source"].items()):
                print(f"  {source}: {count}")
            for error in result["errors"]:
                print(f"  warning: {error}")
            print(f"Recomputed {len(signals)} signal(s)")
            return 0

        if args.command == "research-universe-backfill":
            store.init_db()
            result = run_weekly_universe_backfill(
                store=store,
                chain_id=args.chain,
                timeout_seconds=args.timeout_seconds,
                min_weekly_volume_usd=args.minimum_weekly_volume_usd,
                min_weekly_trades=args.minimum_weekly_trades,
                min_weekly_traders=args.minimum_weekly_traders,
                execution_id=args.execution_id,
            )
            print("Research weekly universe imported")
            print(f"  chain: {result['chain_id']}")
            print(f"  snapshots: {result['snapshot_count']}")
            print(f"  outcomes: {result['validity']['outcome_count']}")
            print(f"  zero-flow Binance events: {result['validity']['zero_flow_event_count']}")
            return 0

        if args.command == "research-universe-probe":
            store.init_db()
            result = run_weekly_universe_threshold_probe(
                store=store,
                chain_id=args.chain,
                timeout_seconds=args.timeout_seconds,
            )
            print("Research universe threshold probe")
            print(f"  chain: {result['chain_id']}")
            for key, value in result["thresholds"].items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "research-validity":
            store.init_db()
            result = run_research_validity(store, chain_id=args.chain)
            print("Research Validity V3 recomputed")
            for key, value in result.items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "wallet-opportunity-backfill":
            store.init_db()
            result = run_wallet_opportunity_backfill(
                store=store,
                chain_id=args.chain,
                max_wallets=args.max_wallets,
                timeout_seconds=args.timeout_seconds,
            )
            print("Wallet opportunity denominator imported")
            print(f"  wallets: {result['wallet_count']}")
            print(f"  opportunities: {result['opportunity_count']}")
            print(f"  Bayesian scores: {result['score_count']}")
            return 0

        if args.command == "wallet-opportunity-rescore":
            store.init_db()
            result = rescore_wallet_opportunities(store, chain_id=args.chain)
            print("Wallet opportunity scores recomputed")
            for key, value in result.items():
                print(f"  {key}: {value}")
            return 0

        if args.command == "wallet-weekly-flows":
            store.init_db()
            result = run_wallet_weekly_flow_backfill(
                store=store,
                chain_id=args.chain,
                max_wallets=args.max_wallets,
                timeout_seconds=args.timeout_seconds,
            )
            print("Point-in-time wallet weekly flows imported")
            print(f"  wallets: {result['wallet_count']}")
            print(f"  weekly flows: {result['weekly_flow_count']}")
            return 0

        if args.command == "identity-graph-v2":
            store.init_db()
            result = run_identity_graph_backfill(
                store=store,
                max_wallets=args.max_wallets,
                timeout_seconds=args.timeout_seconds,
            )
            print("Identity Graph V2 rebuilt")
            print(f"  wallets: {result['wallet_count']}")
            print(f"  edges: {result['identity_edge_count']}")
            print(f"  account owners: {result['account_owner_edge_count']}")
            print(f"  clusters: {result['clusters']['cluster_count']}")
            for warning in result["warnings"]:
                print(f"  warning: {warning}")
            return 0

        if args.command == "attention-gap":
            store.init_db()
            result = enrich_attention_snapshots(store, max_tokens=args.max_tokens)
            signals = recompute_live_signals(store)
            print("Attention Gap snapshots stored")
            print(f"  targets: {result['target_count']}")
            print(f"  snapshots: {result['snapshot_count']}")
            print(f"  signals recomputed: {len(signals)}")
            for error in result["errors"]:
                print(f"  warning: {error}")
            return 0

        if args.command == "cross-chain-history":
            store.init_db()
            result = run_cross_chain_listing_history_backfill(
                store=store,
                chain_id=args.chain,
                timeout_seconds=args.timeout_seconds,
                lookback_days=args.lookback_days,
            )
            print("Cross-chain Binance history imported")
            print(f"  chain: {result['chain_id']}")
            print(f"  targets: {result['target_count']}")
            print(f"  buyer rows: {result['row_count']}")
            return 0

        if args.command == "solana-history":
            store.init_db()
            listing_result = run_solana_listing_history_backfill(
                store=store,
                timeout_seconds=args.timeout_seconds,
                lookback_days=args.lookback_days,
            )
            print("Solana Binance history imported")
            print(f"  targets: {listing_result['target_count']}")
            print(f"  buyer rows: {listing_result['row_count']}")
            if args.with_universe:
                universe_result = run_solana_universe_backfill(
                    store=store,
                    timeout_seconds=args.timeout_seconds,
                    min_weekly_volume_usd=args.minimum_weekly_volume_usd,
                    min_weekly_trades=args.minimum_weekly_trades,
                    min_weekly_traders=args.minimum_weekly_traders,
                )
                print(f"  universe snapshots: {universe_result['snapshot_count']}")
            return 0

        if args.command == "train-research-models":
            store.init_db()
            result = train_model_suite(store, chain_id=args.chain)
            print("Research model suite finished")
            for run in result["runs"]:
                metrics = run.get("metrics", {})
                print(
                    f"  run={run['run_id']} status={run['status']} "
                    f"rows={metrics.get('dataset_row_count', 0)} "
                    f"events={metrics.get('independent_positive_token_count', 0)}"
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

        if args.command == "run-backtest":
            store.init_db()
            result = run_wallet_walk_forward(
                store,
                post_window_days=args.post_window_days,
                minimum_interest_score=args.minimum_interest_score,
                maximum_noise_score=args.maximum_noise_score,
                minimum_history_target_count=args.minimum_history_target_count,
            )
            metrics = result["metrics"]
            print(f"Completed wallet walk-forward backtest run {result['backtest_run_id']}")
            print(f"  observed targets: {metrics['observed_target_count']}")
            print(f"  evaluated targets: {metrics['evaluated_target_count']}")
            print(f"  candidate opportunities: {metrics['candidate_wallet_opportunities']}")
            print(f"  hits: {metrics['candidate_wallet_hits']}")
            print(f"  precision: {format_optional_ratio(metrics['micro_precision'])}")
            print(f"  baseline: {format_optional_ratio(metrics['baseline_rate'])}")
            print(f"  lift: {format_optional_number(metrics['lift_over_baseline'])}")
            print(
                "  wallet claims ready: "
                f"{metrics['ready_for_wallet_quality_claims']}"
            )
            print("  token prediction claims ready: False")
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

        if args.command == "live-scan":
            store.init_db()
            result = run_base_live_scan(
                store=store,
                window_hours=args.window_hours,
                min_trade_usd=args.minimum_trade_usd,
                max_wallets=args.max_wallets,
                max_tokens_to_enrich=args.max_tokens,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout_seconds,
            )
            if result.get("dry_run"):
                print("Live Radar SQL prepared")
                print(f"  tracked wallets: {result['tracked_wallet_count']}")
                print(
                    "  excluded training tokens: "
                    f"{result['excluded_training_token_count']}"
                )
                print(f"  SQL: {result['sql_path']}")
                return 0
            print(f"Live Radar status: {result['status']}")
            print(f"  tracked wallets: {result['tracked_wallet_count']}")
            print(f"  observations: {result.get('observation_count', 0)}")
            print(f"  market snapshots: {result.get('market_snapshot_count', 0)}")
            print(f"  risk snapshots: {result.get('risk_snapshot_count', 0)}")
            print(f"  signals: {result.get('signal_count', 0)}")
            print(f"  qualified signals: {result.get('qualified_signal_count', 0)}")
            for error in result.get("enrichment_errors", []):
                print(f"  enrichment warning: {error}")
            return 0

        if args.command == "local-live-scan":
            store.init_db()
            result = run_base_local_live_scan(
                store=store,
                window_hours=args.window_hours,
                max_wallets=args.max_wallets,
                max_tokens=args.max_tokens,
                min_wallets=args.minimum_wallets,
                max_hypersync_pages=args.max_hypersync_pages,
                max_pairs_per_token=args.max_pairs_per_token,
                min_pair_liquidity_usd=args.min_pair_liquidity_usd,
                use_cursor=args.use_cursor,
                generate_signals=not args.no_signals,
                allow_transfer_fallback=not args.no_transfer_fallback,
            )
            print(f"Local Live Radar status: {result['status']}")
            print(f"  tracked wallets: {result['tracked_wallet_count']}")
            print(f"  transfer events: {result['transfer_event_count']}")
            print(f"  swap events: {result.get('swap_event_count', 0)}")
            print(f"  local dex trades: {result.get('dex_trade_count', 0)}")
            print(f"  local dex rollups: {result.get('dex_rollup_count', 0)}")
            print(f"  pair candidates: {result.get('pair_candidate_count', 0)}")
            print(f"  pair token maps: {result.get('pair_token_count', 0)}")
            print(f"  token candidates: {result.get('token_candidate_count', 0)}")
            print(f"  observations: {result['observation_count']}")
            print(
                "  observations by source: "
                f"dex={result.get('dex_observation_count', 0)}, "
                f"transfer_proxy={result.get('fallback_observation_count', 0)}"
            )
            print(f"  market snapshots: {result.get('market_snapshot_count', 0)}")
            print(f"  priced tokens: {result.get('priced_token_count', 0)}")
            print(f"  signals: {result.get('signal_count', 0)}")
            print(f"  qualified signals: {result.get('qualified_signal_count', 0)}")
            print(f"  cursor block: {result.get('cursor_block') or '-'}")
            print(f"  raw logs stored: {result['raw_logs_stored']}")
            print(f"  pruned observations: {result['pruned_observation_count']}")
            print(f"  pruned market snapshots: {result['pruned_market_snapshot_count']}")
            print(f"  pruned local dex trades: {result.get('pruned_dex_trade_count', 0)}")
            print(f"  pruned local dex rollups: {result.get('pruned_dex_rollup_count', 0)}")
            print(f"  pruned local signals: {result.get('pruned_signal_count', 0)}")
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}")
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

        if args.command == "wallet-report":
            store.init_db()
            summary = store.wallet_score_summary(
                model_version=MODEL_VERSION,
                limit=args.limit,
            )
            print("Wallet score summary:")
            print(f"  model: {MODEL_VERSION}")
            print(f"  wallets: {summary['total_wallets']}")
            print("\nBy label:")
            for row in summary["by_label"]:
                print(f"  {row['label']}: {row['count']}")
            print("\nTop candidates:")
            for row in summary["top_candidates"]:
                flags = ",".join(row["flags"][:4])
                print(
                    f"  {row['wallet_address']} | "
                    f"label={row['label']} | "
                    f"interest={row['interest_score']:.1f} | "
                    f"noise={row['noise_score']:.1f} | "
                    f"confidence={row['confidence_score']:.1f} | "
                    f"targets={row['target_count']} | "
                    f"gross=${row['total_gross_buy_usd']:.2f} | "
                    f"trades={row['total_buy_trades']} | "
                    f"flags={flags}"
                )
            print("\nNoisiest wallets:")
            for row in summary["noisiest"]:
                flags = ",".join(row["flags"][:4])
                print(
                    f"  {row['wallet_address']} | "
                    f"label={row['label']} | "
                    f"noise={row['noise_score']:.1f} | "
                    f"interest={row['interest_score']:.1f} | "
                    f"trades={row['total_buy_trades']} | "
                    f"gross=${row['total_gross_buy_usd']:.2f} | "
                    f"flags={flags}"
                )
            return 0

        if args.command == "repeatability-report":
            store.init_db()
            summary = store.wallet_repeatability_summary(
                model_version=MODEL_VERSION,
                limit=args.limit,
            )
            print("Wallet repeatability summary:")
            print(f"  model: {MODEL_VERSION}")
            print(f"  repeated wallets: {summary['repeat_wallet_count']}")
            print("\nBy target count:")
            for row in summary["by_target_count"]:
                print(f"  target_count={row['target_count']}: {row['wallet_count']}")
            print("\nRepeated wallets:")
            for row in summary["repeated_wallets"]:
                flags = ",".join(row["flags"][:5])
                symbols = ",".join(row["evidence"].get("symbols", []))
                print(
                    f"  {row['wallet_address']} | "
                    f"label={row['label']} | "
                    f"symbols={symbols} | "
                    f"interest={row['interest_score']:.1f} | "
                    f"noise={row['noise_score']:.1f} | "
                    f"confidence={row['confidence_score']:.1f} | "
                    f"gross=${row['total_gross_buy_usd']:.2f} | "
                    f"flags={flags}"
                )
            return 0

        if args.command == "export-wallet-scores":
            store.init_db()
            output = Path(args.output)
            count = store.export_wallet_scores_csv(output, model_version=MODEL_VERSION)
            print(f"Exported {count} wallet score rows to {output}")
            return 0

        parser.print_help()
        return 2
    except BinanceAnnouncementError as exc:
        print(f"Binance collector error: {exc}", file=sys.stderr)
        return 1
    except DuneAPIError as exc:
        print(f"Dune error: {exc}", file=sys.stderr)
        return 1
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

    collect = subparsers.add_parser(
        "collect-binance",
        help="Collect Binance announcement catalog entries.",
    )
    collect.add_argument("--pages", type=int, default=1)
    collect.add_argument("--start-page", type=int, default=1)
    collect.add_argument("--page-size", type=int, default=20)
    collect.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help="Minimum delay between Binance requests in seconds.",
    )
    collect.add_argument(
        "--with-details",
        action="store_true",
        help="Fetch article details and extract contract links/body text.",
    )
    collect.add_argument(
        "--listing-details-only",
        action="store_true",
        help="Fetch details only for spot listing and launch/airdrop candidates.",
    )

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

    subparsers.add_parser(
        "verify-base-contracts",
        help="Verify Base token mappings with on-chain ERC-20 symbol calls.",
    )
    verify_evm = subparsers.add_parser(
        "verify-evm-contracts",
        help="Verify EVM token mappings with chain-specific ERC-20 symbol calls.",
    )
    verify_evm.add_argument(
        "--chain",
        choices=("base", "bsc", "ethereum"),
        required=True,
    )

    subparsers.add_parser(
        "prepare-dune-backfill",
        help="Render and validate the V4 SQL package without executing Dune.",
    )

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

    report = subparsers.add_parser(
        "report",
        help="Write Markdown and HTML project reports.",
    )
    report.add_argument("--output", default="reports/latest.md")
    report.add_argument("--html-output", default="reports/latest.html")

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

    render_dune = subparsers.add_parser(
        "render-dune-base-buyers",
        help="Render generated Dune SQL for Base pre-listing buyers.",
    )
    render_dune.add_argument(
        "--output",
        default="queries/generated/base_pre_listing_buyers.generated.sql",
    )
    render_dune.add_argument("--lookback-days", type=int, default=90)
    render_dune.add_argument("--minimum-total-buy-usd", type=float, default=1000.0)
    render_dune.add_argument("--max-wallets-per-target", type=int, default=500)

    render_holding = subparsers.add_parser(
        "render-dune-base-holding",
        help="Render generated Dune SQL for Base sell-side / holding behavior.",
    )
    render_holding.add_argument(
        "--output",
        default="queries/generated/base_holding_behavior.generated.sql",
    )
    render_holding.add_argument("--lookback-days", type=int, default=90)
    render_holding.add_argument("--post-window-days", type=int, default=30)
    render_holding.add_argument("--minimum-pre-buy-usd", type=float, default=1000.0)
    render_holding.add_argument("--max-wallets-per-target", type=int, default=500)

    render_contract_discovery = subparsers.add_parser(
        "render-dune-base-contract-discovery",
        help="Render pre-announcement Base contract discovery across Binance targets.",
    )
    render_contract_discovery.add_argument(
        "--output",
        default="queries/generated/base_contract_discovery.generated.sql",
    )
    render_contract_discovery.add_argument("--lookback-days", type=int, default=120)

    render_threshold_probe = subparsers.add_parser(
        "render-dune-base-threshold-probe",
        help="Render a one-row historical wallet-notional threshold probe for Base.",
    )
    render_threshold_probe.add_argument(
        "--output",
        default="queries/generated/base_pre_listing_threshold_probe.generated.sql",
    )
    render_threshold_probe.add_argument("--lookback-days", type=int, default=120)

    render_negative = subparsers.add_parser(
        "render-dune-negative-controls",
        help="Render point-in-time Base token negative-control SQL.",
    )
    render_negative.add_argument(
        "--output",
        default="queries/generated/base_negative_controls.generated.sql",
    )
    render_negative.add_argument("--window-days", type=int, default=14)
    render_negative.add_argument("--minimum-trade-usd", type=float, default=25.0)

    subparsers.add_parser(
        "dune-health",
        help="Check whether Dune API configuration is present.",
    )
    subparsers.add_parser(
        "dune-usage",
        help="Show current Dune billing-period credit usage.",
    )
    dune_export_plan = subparsers.add_parser(
        "dune-export-plan",
        help="Estimate Dune export cost from free execution status metadata.",
    )
    dune_export_plan.add_argument("--execution-id", required=True)
    dune_export_plan.add_argument(
        "--columns-profile",
        choices=("universe", "all"),
        default="universe",
    )

    dune_execute = subparsers.add_parser(
        "dune-execute-sql",
        help="Execute a SQL file through Dune's SQL API.",
    )
    dune_execute.add_argument("--sql-file", required=True)
    dune_execute.add_argument("--output", default="exports/dune_result.json")
    dune_execute.add_argument(
        "--performance",
        choices=("small", "medium", "large"),
        default="medium",
    )
    dune_execute.add_argument("--dry-run", action="store_true")
    dune_execute.add_argument("--poll", action="store_true")
    dune_execute.add_argument("--timeout-seconds", type=int, default=600)
    dune_execute.add_argument("--poll-interval-seconds", type=int, default=5)

    dune_fetch = subparsers.add_parser(
        "dune-fetch-results",
        help="Fetch all result pages for an existing Dune execution.",
    )
    dune_fetch.add_argument("--execution-id", required=True)
    dune_fetch.add_argument("--output", required=True)

    import_buyers = subparsers.add_parser(
        "import-base-buyers",
        help="Import Base pre-listing buyer rows from a Dune result JSON.",
    )
    import_buyers.add_argument("--input", default="exports/base_pre_listing_buyers.json")

    import_holding = subparsers.add_parser(
        "import-base-holding",
        help="Import Base wallet holding metric rows from a Dune result JSON.",
    )
    import_holding.add_argument("--input", default="exports/base_holding_behavior.json")

    import_contract_discovery = subparsers.add_parser(
        "import-base-contract-discovery",
        help="Import high-confidence Base contracts discovered only from pre-cutoff DEX data.",
    )
    import_contract_discovery.add_argument(
        "--input", default="exports/base_contract_discovery.json"
    )
    import_contract_discovery.add_argument("--minimum-notional-usd", type=float, default=100_000)
    import_contract_discovery.add_argument("--minimum-notional-share", type=float, default=0.9)
    import_contract_discovery.add_argument("--minimum-trade-count", type=int, default=500)
    import_contract_discovery.add_argument(
        "--approved-symbols",
        default="",
        help="Comma-separated manual approvals after name/provenance review.",
    )

    import_negative = subparsers.add_parser(
        "import-negative-controls",
        help="Import and score point-in-time Base negative-control candidates.",
    )
    import_negative.add_argument(
        "--input",
        default="exports/base_negative_controls.json",
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
        "score-wallets",
        help="Compute wallet diagnostics and provisional wallet scores.",
    )

    wallet_intelligence = subparsers.add_parser(
        "wallet-intelligence",
        help="Label candidate wallets, remove entities, and build wallet clusters.",
    )
    wallet_intelligence.add_argument("--max-wallets", type=int, default=1_500)
    wallet_intelligence.add_argument("--entity-chunk-size", type=int, default=500)
    wallet_intelligence.add_argument("--funding-chunk-size", type=int, default=400)

    rebuild_clusters = subparsers.add_parser(
        "rebuild-wallet-clusters",
        help="Rebuild conservative wallet clusters from existing research data.",
    )
    rebuild_clusters.add_argument("--max-wallets", type=int, default=1_500)

    enrich_social = subparsers.add_parser(
        "enrich-social",
        help="Collect GDELT social-silence proxy snapshots for live Base observations.",
    )
    enrich_social.add_argument("--max-tokens", type=int, default=3)

    enrich_onchain = subparsers.add_parser(
        "enrich-onchain",
        help="Verify live candidates with Blockscout, Moralis, and HyperSync.",
    )
    enrich_onchain.add_argument("--max-tokens", type=int, default=10)

    research_universe = subparsers.add_parser(
        "research-universe-backfill",
        help="Build a weekly point-in-time EVM token universe through Dune.",
    )
    research_universe.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )
    research_universe.add_argument("--minimum-weekly-volume-usd", type=float, default=200_000)
    research_universe.add_argument("--minimum-weekly-trades", type=int, default=20)
    research_universe.add_argument("--minimum-weekly-traders", type=int, default=10)
    research_universe.add_argument(
        "--execution-id",
        help="Reuse a completed Dune result and apply server-side result filters.",
    )
    research_universe.add_argument("--timeout-seconds", type=int, default=3_600)

    universe_probe = subparsers.add_parser(
        "research-universe-probe",
        help="Count weekly universe sizes at several quality thresholds.",
    )
    universe_probe.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )
    universe_probe.add_argument("--timeout-seconds", type=int, default=3_600)

    research_validity = subparsers.add_parser(
        "research-validity",
        help="Recompute point-in-time labels, returns, drawdowns, and event coverage.",
    )
    research_validity.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )

    wallet_opportunities = subparsers.add_parser(
        "wallet-opportunity-backfill",
        help="Collect the complete token opportunity denominator for selected wallets.",
    )
    wallet_opportunities.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )
    wallet_opportunities.add_argument("--max-wallets", type=int, default=50)
    wallet_opportunities.add_argument("--timeout-seconds", type=int, default=1_800)

    wallet_rescore = subparsers.add_parser(
        "wallet-opportunity-rescore",
        help="Recompute Bayesian wallet scores from imported opportunities.",
    )
    wallet_rescore.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )

    wallet_weekly = subparsers.add_parser(
        "wallet-weekly-flows",
        help="Collect weekly point-in-time flows for historical smart-wallet features.",
    )
    wallet_weekly.add_argument(
        "--chain", choices=("base", "bsc", "ethereum"), default="base"
    )
    wallet_weekly.add_argument("--max-wallets", type=int, default=200)
    wallet_weekly.add_argument("--timeout-seconds", type=int, default=3_600)

    identity_graph = subparsers.add_parser(
        "identity-graph-v2",
        help="Build two-hop funding, synchronized-flow, team, and account-owner edges.",
    )
    identity_graph.add_argument("--max-wallets", type=int, default=200)
    identity_graph.add_argument("--timeout-seconds", type=int, default=1_800)

    attention_gap = subparsers.add_parser(
        "attention-gap",
        help="Collect X-independent DexScreener, Farcaster, GitHub, news, and on-chain attention.",
    )
    attention_gap.add_argument("--max-tokens", type=int, default=3)

    cross_chain = subparsers.add_parser(
        "cross-chain-history",
        help="Backfill historical Binance pre-listing buyers on another EVM chain.",
    )
    cross_chain.add_argument("--chain", choices=("bsc", "ethereum"), required=True)
    cross_chain.add_argument("--lookback-days", type=int, default=365)
    cross_chain.add_argument("--timeout-seconds", type=int, default=3_600)

    solana_history = subparsers.add_parser(
        "solana-history",
        help="Run the separate Solana Binance-history and optional universe pipeline.",
    )
    solana_history.add_argument("--lookback-days", type=int, default=365)
    solana_history.add_argument("--timeout-seconds", type=int, default=3_600)
    solana_history.add_argument("--with-universe", action="store_true")
    solana_history.add_argument(
        "--minimum-weekly-volume-usd", type=float, default=5_000
    )
    solana_history.add_argument("--minimum-weekly-trades", type=int, default=5)
    solana_history.add_argument("--minimum-weekly-traders", type=int, default=3)

    train_models = subparsers.add_parser(
        "train-research-models",
        help="Train temporal logistic and gradient-boosting listing models.",
    )
    train_models.add_argument(
        "--chain", choices=("base", "bsc", "ethereum", "evm"), default="base"
    )

    subparsers.add_parser(
        "research-status",
        help="Print Research Validity, wallet denominator, attention, and ML readiness.",
    )

    run_backtest = subparsers.add_parser(
        "run-backtest",
        help="Run a strict chronological wallet walk-forward backtest.",
    )
    run_backtest.add_argument("--post-window-days", type=int, default=30)
    run_backtest.add_argument("--minimum-interest-score", type=float, default=52.0)
    run_backtest.add_argument("--maximum-noise-score", type=float, default=65.0)
    run_backtest.add_argument("--minimum-history-target-count", type=int, default=2)

    subparsers.add_parser(
        "backtest-report",
        help="Print the latest honest backtest result and readiness limits.",
    )

    live_scan = subparsers.add_parser(
        "live-scan",
        help="Run the near-live Base smart-wallet accumulation scan.",
    )
    live_scan.add_argument("--window-hours", type=int, default=336)
    live_scan.add_argument("--minimum-trade-usd", type=float, default=25.0)
    live_scan.add_argument("--max-wallets", type=int, default=200)
    live_scan.add_argument("--max-tokens", type=int, default=100)
    live_scan.add_argument("--timeout-seconds", type=int, default=600)
    live_scan.add_argument("--dry-run", action="store_true")

    local_live_scan = subparsers.add_parser(
        "local-live-scan",
        help="Run local Base wallet-transfer radar through HyperSync without Dune.",
    )
    local_live_scan.add_argument("--window-hours", type=int, default=24)
    local_live_scan.add_argument("--max-wallets", type=int, default=200)
    local_live_scan.add_argument("--max-tokens", type=int, default=100)
    local_live_scan.add_argument("--minimum-wallets", type=int, default=1)
    local_live_scan.add_argument("--max-hypersync-pages", type=int, default=3)
    local_live_scan.add_argument("--max-pairs-per-token", type=int, default=5)
    local_live_scan.add_argument("--min-pair-liquidity-usd", type=float, default=1_000.0)
    local_live_scan.add_argument(
        "--use-cursor",
        action="store_true",
        help="Start pool-swap ingestion after the stored local cursor.",
    )
    local_live_scan.add_argument(
        "--no-signals",
        action="store_true",
        help="Skip local signal generation for this scan.",
    )
    local_live_scan.add_argument(
        "--no-transfer-fallback",
        action="store_true",
        help="Only write observations backed by local swap events.",
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

    wallet_report = subparsers.add_parser(
        "wallet-report",
        help="Print wallet score diagnostics.",
    )
    wallet_report.add_argument("--limit", type=int, default=20)

    repeatability_report = subparsers.add_parser(
        "repeatability-report",
        help="Print wallets that appear across multiple Binance backtest targets.",
    )
    repeatability_report.add_argument("--limit", type=int, default=20)

    export_wallet_scores = subparsers.add_parser(
        "export-wallet-scores",
        help="Export wallet scores to CSV.",
    )
    export_wallet_scores.add_argument(
        "--output",
        default="exports/wallet_scores.csv",
    )

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
    if summary["pre_listing_wallet_buy_count"] > 0 and summary["wallet_holding_metric_count"] == 0:
        print("  2. Run: python3 -m smart_money_radar.cli render-dune-base-holding")
    elif summary["pre_listing_wallet_buy_count"] > 0 and summary["wallet_score_count"] == 0:
        print("  2. Run: python3 -m smart_money_radar.cli score-wallets")
    elif summary["wallet_score_count"] > 0:
        print("  2. Run: python3 -m smart_money_radar.cli wallet-report --limit 20")
    elif base_targets:
        print("  2. Run: python3 -m smart_money_radar.cli render-dune-base-buyers")
    else:
        print("  2. Add Base contract mappings before Base Dune backtest.")
    print("  3. Run: python3 -m smart_money_radar.cli report")


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
