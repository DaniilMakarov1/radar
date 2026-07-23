from __future__ import annotations

import sqlite3
import threading
from typing import Any

from smart_money_radar.analytics import (
    initialize_analytics,
    prune_analytics_executions,
)
from smart_money_radar.local_radar import (
    LOCAL_DEX_SOURCE,
    LOCAL_LIVE_SOURCE,
    LOCAL_MARKET_SOURCE,
    run_base_local_live_scan,
)
from smart_money_radar.storage import SQLiteStore


DUNE_ANALYTICS_EXECUTION_RETENTION = 5
DUNE_LOCAL_SNAPSHOT_RETENTION = 1


def build_dune_overview(store: SQLiteStore) -> dict[str, object]:
    prune_analytics_executions(
        store,
        keep_latest=DUNE_ANALYTICS_EXECUTION_RETENTION,
    )
    catalog = initialize_analytics(store)
    with store.connect() as connection:
        local_sources = [
            local_snapshot_summary(
                connection,
                table_name="local_dex_trades",
                source=LOCAL_DEX_SOURCE,
                label="Local dex_trades",
            ),
            local_snapshot_summary(
                connection,
                table_name="local_dex_rollups",
                source=LOCAL_DEX_SOURCE,
                label="Local dex rollups",
            ),
            local_snapshot_summary(
                connection,
                table_name="radar_observations",
                source=LOCAL_DEX_SOURCE,
                label="Swap-backed observations",
            ),
            local_snapshot_summary(
                connection,
                table_name="radar_observations",
                source=LOCAL_LIVE_SOURCE,
                label="Transfer proxy observations",
            ),
            local_snapshot_summary(
                connection,
                table_name="token_market_snapshots",
                source=LOCAL_MARKET_SOURCE,
                label="DexScreener market snapshots",
            ),
        ]
        latest_executions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    analytics_execution_id,
                    query_slug,
                    engine,
                    status,
                    started_at,
                    finished_at,
                    elapsed_ms,
                    row_count,
                    limit_rows,
                    error
                FROM analytics_executions
                ORDER BY started_at DESC, analytics_execution_id DESC
                LIMIT ?
                """,
                (DUNE_ANALYTICS_EXECUTION_RETENTION,),
            )
        ]
    return {
        "status": "ready",
        "catalog": catalog,
        "local_sources": local_sources,
        "latest_executions": latest_executions,
        "latest_job": store.latest_app_job() or {},
        "cursor": store.local_ingestion_cursor(
            LOCAL_DEX_SOURCE,
            "base",
            "candidate_pair_swaps",
        )
        or {},
        "retention": {
            "raw_logs_stored": False,
            "local_snapshot_retention": DUNE_LOCAL_SNAPSHOT_RETENTION,
            "analytics_execution_retention": DUNE_ANALYTICS_EXECUTION_RETENTION,
            "query_result_rows_stored": False,
            "kept_history": "Only paper trade results are intended for long-term training history.",
        },
        "default_sql": """
SELECT
    canonical_asset,
    long_venue,
    short_venue,
    status,
    expected_net_profit,
    net_roc_annualized,
    observed_at
FROM analytics_funding_routes
ORDER BY expected_net_profit DESC
""".strip(),
        "scan_defaults": {
            "window_hours": 24,
            "max_wallets": 50,
            "max_tokens": 25,
            "minimum_wallets": 1,
            "max_hypersync_pages": 1,
            "max_pairs_per_token": 5,
            "min_pair_liquidity_usd": 1_000,
            "use_cursor": False,
            "generate_signals": True,
            "allow_transfer_fallback": True,
        },
    }


def local_snapshot_summary(
    connection: sqlite3.Connection,
    table_name: str,
    source: str,
    label: str,
) -> dict[str, object]:
    if table_name not in {
        "local_dex_trades",
        "local_dex_rollups",
        "radar_observations",
        "token_market_snapshots",
    }:
        raise ValueError(f"Unsupported local source table: {table_name}")
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            COUNT(DISTINCT observed_at) AS snapshot_count,
            MAX(observed_at) AS latest_observed_at
        FROM {table_name}
        WHERE source = ?
        """,
        (source,),
    ).fetchone()
    return {
        "label": label,
        "table": table_name,
        "source": source,
        "row_count": int(row["row_count"] or 0),
        "snapshot_count": int(row["snapshot_count"] or 0),
        "latest_observed_at": row["latest_observed_at"],
    }


def dune_local_scan_config(payload: dict[str, object]) -> dict[str, Any]:
    return {
        "window_hours": dashboard_int(payload, "window_hours", 24, 1, 168),
        "max_wallets": dashboard_int(payload, "max_wallets", 50, 1, 500),
        "max_tokens": dashboard_int(payload, "max_tokens", 25, 1, 250),
        "min_wallets": dashboard_int(payload, "minimum_wallets", 1, 1, 20),
        "max_hypersync_pages": dashboard_int(payload, "max_hypersync_pages", 1, 1, 10),
        "max_pairs_per_token": dashboard_int(payload, "max_pairs_per_token", 5, 1, 20),
        "min_pair_liquidity_usd": dashboard_float(
            payload,
            "min_pair_liquidity_usd",
            1_000.0,
            0.0,
            10_000_000.0,
        ),
        "use_cursor": dashboard_bool(payload, "use_cursor", False),
        "generate_signals": dashboard_bool(payload, "generate_signals", True),
        "allow_transfer_fallback": dashboard_bool(
            payload,
            "allow_transfer_fallback",
            True,
        ),
    }


def dashboard_int(
    payload: dict[str, object],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = payload.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def dashboard_float(
    payload: dict[str, object],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = payload.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def dashboard_bool(
    payload: dict[str, object],
    name: str,
    default: bool,
) -> bool:
    raw = payload.get(name, default)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        return bool(raw)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def run_dune_local_scan_job(
    store: SQLiteStore,
    job_id: int,
    active_job: dict[str, int | None],
    job_lock: threading.Lock,
    config: dict[str, Any],
) -> None:
    try:
        store.update_app_job(
            job_id,
            status="running",
            progress=0.15,
            message="Scanning local Base transfers, candidate pools, and DEX swaps",
        )
        result = run_base_local_live_scan(store=store, **config)
        store.update_app_job(
            job_id,
            status="success",
            progress=1.0,
            message="Local Dune scan completed",
            result={
                **result,
                "retention": {
                    "raw_logs_stored": False,
                    "local_snapshot_retention": DUNE_LOCAL_SNAPSHOT_RETENTION,
                    "analytics_execution_retention": DUNE_ANALYTICS_EXECUTION_RETENTION,
                },
            },
        )
    except Exception as exc:
        store.update_app_job(
            job_id,
            status="failed",
            progress=1.0,
            message="Local Dune scan failed",
            error=str(exc),
        )
    finally:
        with job_lock:
            active_job["job_id"] = None
