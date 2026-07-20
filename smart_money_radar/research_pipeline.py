from __future__ import annotations

from pathlib import Path
from typing import Any

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.dune_queries import (
    write_solana_pre_listing_buyers_sql,
    write_weekly_solana_universe_sql,
    write_evm_pre_listing_buyers_sql,
    write_wallet_weekly_flows_sql,
    write_weekly_evm_universe_probe_sql,
    write_weekly_evm_universe_sql,
)
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient, write_json
from smart_money_radar.research_validity import run_research_validity
from smart_money_radar.storage import SQLiteStore, normalize_chain_address
from smart_money_radar.wallet_research import research_wallet_selection


UNIVERSE_SOURCE = "dune_weekly_universe_v4_dense_90d_followup"
SOLANA_UNIVERSE_SOURCE = "dune_weekly_universe_v2_solana"
WALLET_WEEKLY_FLOW_SOURCE = "dune_wallet_weekly_flows_v2"
UNIVERSE_EXPORT_COLUMNS = [
    "token_address",
    "token_symbol",
    "snapshot_at",
    "pair_count",
    "dex_count",
    "trader_count",
    "buyer_count",
    "seller_count",
    "trade_count",
    "gross_buy_usd",
    "gross_sell_usd",
    "volume_usd",
    "close_price_usd",
    "is_dense_zero",
]


def run_weekly_universe_threshold_probe(
    store: SQLiteStore,
    chain_id: str = "base",
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
) -> dict[str, Any]:
    sql_path = generated_query_path(f"{chain_id}_weekly_universe_probe_v2")
    result_path = export_path(f"{chain_id}_weekly_universe_probe_v2")
    write_weekly_evm_universe_probe_sql(sql_path, chain_id=chain_id)
    execution_id, payload = execute_dune_artifact(
        store=store,
        source=f"dune_weekly_universe_probe_v2_{chain_id}",
        sql_path=sql_path,
        result_path=result_path,
        client=client,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    return {
        "chain_id": chain_id,
        "execution_id": execution_id,
        "thresholds": rows[0] if rows else {},
        "sql_path": str(sql_path),
        "result_path": str(result_path),
    }


def run_weekly_universe_backfill(
    store: SQLiteStore,
    chain_id: str = "base",
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
    min_weekly_volume_usd: float = 200_000.0,
    min_weekly_trades: int = 20,
    min_weekly_traders: int = 10,
    execution_id: str | None = None,
) -> dict[str, Any]:
    sql_path = generated_query_path(f"{chain_id}_weekly_universe_v4")
    result_path = export_path(f"{chain_id}_weekly_universe_v4")
    write_weekly_evm_universe_sql(
        sql_path,
        chain_id=chain_id,
        min_weekly_volume_usd=min_weekly_volume_usd,
        min_weekly_trades=min_weekly_trades,
        min_weekly_traders=min_weekly_traders,
    )
    if execution_id:
        dune = client or DuneClient()
        status = dune.execution_status(execution_id)
        export_plan = dune.require_affordable_export(
            status,
            columns=UNIVERSE_EXPORT_COLUMNS,
        )
        payload = dune.execution_results_all_cached(
            execution_id,
            cache_root=PROJECT_ROOT / "exports" / "dune_page_cache",
            columns=UNIVERSE_EXPORT_COLUMNS,
        )
        payload.setdefault("result", {}).setdefault("metadata", {})[
            "export_plan"
        ] = export_plan
        write_json(result_path, payload)
        rows = payload.get("result", {}).get("rows", [])
        store.finish_dune_execution(
            execution_id,
            state=payload.get("state", "QUERY_STATE_COMPLETED"),
            row_count=len(rows),
            output_file=str(result_path),
        )
    else:
        execution_id, payload = execute_dune_artifact(
            store=store,
            source=f"{UNIVERSE_SOURCE}_{chain_id}",
            sql_path=sql_path,
            result_path=result_path,
            client=client,
            timeout_seconds=timeout_seconds,
            performance="medium",
            result_columns=UNIVERSE_EXPORT_COLUMNS,
        )
    rows = payload.get("result", {}).get("rows", [])
    imported = store.replace_research_universe_snapshots(
        chain_id=chain_id,
        source=UNIVERSE_SOURCE,
        rows=rows,
        execution_id=execution_id,
    )
    validity = run_research_validity(store, chain_id=chain_id)
    return {
        "chain_id": chain_id,
        "execution_id": execution_id,
        "snapshot_count": imported,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
        "validity": validity,
    }


def run_cross_chain_listing_history_backfill(
    store: SQLiteStore,
    chain_id: str,
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
    lookback_days: int = 365,
) -> dict[str, Any]:
    targets = deduplicate_targets(store.chain_backtest_targets(chain_id))
    sql_path = generated_query_path(f"{chain_id}_pre_listing_buyers_v2")
    result_path = export_path(f"{chain_id}_pre_listing_buyers_v2")
    write_evm_pre_listing_buyers_sql(
        sql_path,
        chain_id=chain_id,
        targets=targets,
        lookback_days=lookback_days,
        minimum_total_buy_usd=500,
        max_wallets_per_target=2_000,
    )
    execution_id, payload = execute_dune_artifact(
        store=store,
        source=f"dune_{chain_id}_pre_listing_buyers_v2",
        sql_path=sql_path,
        result_path=result_path,
        client=client,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    imported = store.import_pre_listing_wallet_buys(
        chain_id=chain_id,
        execution_id=execution_id,
        rows=rows,
    )
    return {
        "chain_id": chain_id,
        "target_count": len(targets),
        "row_count": imported,
        "execution_id": execution_id,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
    }


def run_solana_listing_history_backfill(
    store: SQLiteStore,
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
    lookback_days: int = 365,
) -> dict[str, Any]:
    chain_id = "solana"
    targets = deduplicate_targets(store.chain_backtest_targets(chain_id))
    sql_path = generated_query_path("solana_pre_listing_buyers_v2")
    result_path = export_path("solana_pre_listing_buyers_v2")
    write_solana_pre_listing_buyers_sql(
        sql_path,
        targets=targets,
        lookback_days=lookback_days,
        minimum_total_buy_usd=500,
        max_wallets_per_target=2_000,
    )
    execution_id, payload = execute_dune_artifact(
        store=store,
        source="dune_solana_pre_listing_buyers_v2",
        sql_path=sql_path,
        result_path=result_path,
        client=client,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    imported = store.import_pre_listing_wallet_buys(
        chain_id=chain_id,
        execution_id=execution_id,
        rows=rows,
    )
    return {
        "chain_id": chain_id,
        "target_count": len(targets),
        "row_count": imported,
        "execution_id": execution_id,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
    }


def run_solana_universe_backfill(
    store: SQLiteStore,
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
    min_weekly_volume_usd: float = 5_000.0,
    min_weekly_trades: int = 5,
    min_weekly_traders: int = 3,
) -> dict[str, Any]:
    chain_id = "solana"
    sql_path = generated_query_path("solana_weekly_universe_v2")
    result_path = export_path("solana_weekly_universe_v2")
    write_weekly_solana_universe_sql(
        sql_path,
        min_weekly_volume_usd=min_weekly_volume_usd,
        min_weekly_trades=min_weekly_trades,
        min_weekly_traders=min_weekly_traders,
    )
    execution_id, payload = execute_dune_artifact(
        store=store,
        source=SOLANA_UNIVERSE_SOURCE,
        sql_path=sql_path,
        result_path=result_path,
        client=client,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    imported = store.replace_research_universe_snapshots(
        chain_id=chain_id,
        source=SOLANA_UNIVERSE_SOURCE,
        rows=rows,
        execution_id=execution_id,
    )
    validity = run_research_validity(store, chain_id=chain_id)
    return {
        "chain_id": chain_id,
        "execution_id": execution_id,
        "snapshot_count": imported,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
        "validity": validity,
    }


def run_wallet_weekly_flow_backfill(
    store: SQLiteStore,
    chain_id: str = "base",
    max_wallets: int = 200,
    client: DuneClient | None = None,
    timeout_seconds: int = 3_600,
) -> dict[str, Any]:
    wallets = research_wallet_selection(store, chain_id, max_wallets)
    sql_path = generated_query_path(f"{chain_id}_wallet_weekly_flows_v2")
    result_path = export_path(f"{chain_id}_wallet_weekly_flows_v2")
    write_wallet_weekly_flows_sql(
        sql_path,
        chain_id=chain_id,
        wallets=wallets,
    )
    execution_id, payload = execute_dune_artifact(
        store=store,
        source=f"{WALLET_WEEKLY_FLOW_SOURCE}_{chain_id}",
        sql_path=sql_path,
        result_path=result_path,
        client=client,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    imported = store.replace_wallet_token_weekly_flows(
        chain_id=chain_id,
        source=WALLET_WEEKLY_FLOW_SOURCE,
        rows=rows,
        execution_id=execution_id,
    )
    return {
        "chain_id": chain_id,
        "wallet_count": len(wallets),
        "weekly_flow_count": imported,
        "execution_id": execution_id,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
    }


def execute_dune_artifact(
    store: SQLiteStore,
    source: str,
    sql_path: Path,
    result_path: Path,
    client: DuneClient | None,
    timeout_seconds: int,
    performance: str,
    result_columns: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    dune = client or DuneClient()
    dune.require_credit_reserve()
    execution = dune.execute_sql(
        sql_path.read_text(encoding="utf-8"),
        performance=performance,
    )
    execution_id = execution["execution_id"]
    store.record_dune_execution(
        execution_id=execution_id,
        source=source,
        state=execution.get("state", "submitted"),
        sql_file=str(sql_path),
        output_file=str(result_path),
    )
    try:
        status = dune.wait_for_execution(
            execution_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=20,
        )
        export_plan = dune.require_affordable_export(
            status,
            columns=result_columns,
        )
        payload = dune.execution_results_all_cached(
            execution_id,
            cache_root=PROJECT_ROOT / "exports" / "dune_page_cache",
            columns=result_columns,
        )
        payload.setdefault("result", {}).setdefault("metadata", {})[
            "export_plan"
        ] = export_plan
    except DuneAPIError as exc:
        store.finish_dune_execution(
            execution_id,
            state="failed",
            error=str(exc),
            output_file=str(result_path),
        )
        raise
    write_json(result_path, payload)
    rows = payload.get("result", {}).get("rows", [])
    store.finish_dune_execution(
        execution_id,
        state=payload.get("state", "QUERY_STATE_COMPLETED"),
        row_count=len(rows),
        output_file=str(result_path),
    )
    return execution_id, payload


def deduplicate_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets:
        key = (
            normalize_chain_address(
                target["chain_id"], target["contract_address"]
            ),
            target["announced_at"],
        )
        unique.setdefault(key, target)
    return sorted(unique.values(), key=lambda row: row["announced_at"])


def generated_query_path(stem: str) -> Path:
    path = PROJECT_ROOT / "queries" / "generated" / f"{stem}.generated.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_path(stem: str) -> Path:
    path = PROJECT_ROOT / "exports" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
