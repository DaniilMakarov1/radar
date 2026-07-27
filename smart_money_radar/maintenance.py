from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.storage import SQLiteStore


LEGACY_MODULE_TABLES = (
    "prediction_paper_executions",
    "prediction_orderbook_snapshots",
    "prediction_routes",
    "prediction_route_candidates",
    "prediction_candidate_backlog",
    "prediction_trade_backlog",
    "prediction_constraints",
    "prediction_contract_matches",
    "prediction_verified_contract_mappings",
    "prediction_event_token_links",
    "prediction_wallet_scores",
    "prediction_wallet_positions",
    "prediction_markets",
    "prediction_events",
    "prediction_scans",
    "signal_shadow_marks",
    "signals",
    "wallet_cluster_members",
    "wallet_clusters",
    "wallet_holding_metrics",
    "wallet_scores",
    "wallet_funding",
    "wallet_entities",
    "wallet_identity_edges",
    "wallet_identity_coverage",
    "wallet_token_opportunities",
    "wallet_token_weekly_flows",
    "wallet_research_scores",
    "pre_listing_wallet_buys",
    "research_event_coverage",
    "research_model_predictions",
    "research_token_outcomes",
    "research_token_universe",
    "backtest_evaluations",
    "backtest_token_candidates",
    "token_attention_snapshots",
    "token_social_snapshots",
    "token_onchain_snapshots",
    "token_risk_snapshots",
    "token_market_snapshots",
    "listing_events",
    "binance_announcements",
    "raw_binance_announcements",
)


@dataclass(frozen=True)
class FileCleanupResult:
    path: str
    deleted_files: int
    deleted_dirs: int
    deleted_bytes: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "deleted_files": self.deleted_files,
            "deleted_dirs": self.deleted_dirs,
            "deleted_bytes": self.deleted_bytes,
            "dry_run": self.dry_run,
        }


def legacy_table_counts(store: SQLiteStore) -> dict[str, int]:
    with store.connect() as connection:
        existing = existing_tables(connection, LEGACY_MODULE_TABLES)
        return {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in existing
        }


def delete_legacy_module_rows(
    store: SQLiteStore,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    before = legacy_table_counts(store)
    deleted = {table: 0 for table in before}
    if not apply:
        return {
            "dry_run": True,
            "rows_before": before,
            "deleted_rows": deleted,
        }

    with store.connect() as connection:
        connection.execute("PRAGMA defer_foreign_keys = ON")
        existing = existing_tables(connection, LEGACY_MODULE_TABLES)
        for table in existing:
            cursor = connection.execute(f'DELETE FROM "{table}"')
            deleted[table] = int(cursor.rowcount or 0)

    after = legacy_table_counts(store)
    return {
        "dry_run": False,
        "rows_before": before,
        "deleted_rows": deleted,
        "rows_after": after,
    }


def existing_tables(connection, tables: tuple[str, ...]) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    existing = {str(row["name"]) for row in rows}
    return tuple(table for table in tables if table in existing)


def prune_dune_page_cache(
    *,
    root: Path | None = None,
    apply: bool = False,
    max_age_days: int | None = None,
) -> FileCleanupResult:
    cache_root = root or PROJECT_ROOT / "exports" / "dune_page_cache"
    if not cache_root.exists():
        return FileCleanupResult(str(cache_root), 0, 0, 0, dry_run=not apply)

    cutoff = None
    if max_age_days is not None and max_age_days >= 0:
        import time

        cutoff = time.time() - int(max_age_days) * 86_400

    deleted_files = 0
    deleted_dirs = 0
    deleted_bytes = 0
    candidates: list[Path] = []
    for path in cache_root.rglob("*"):
        if not path.is_file():
            continue
        if cutoff is not None and path.stat().st_mtime >= cutoff:
            continue
        candidates.append(path)
        deleted_files += 1
        deleted_bytes += path.stat().st_size

    if apply:
        for path in candidates:
            path.unlink(missing_ok=True)
        for directory in sorted(
            [path for path in cache_root.rglob("*") if path.is_dir()],
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
                deleted_dirs += 1
            except OSError:
                pass
        if not any(cache_root.iterdir()):
            shutil.rmtree(cache_root, ignore_errors=True)
            deleted_dirs += 1

    return FileCleanupResult(
        str(cache_root),
        deleted_files,
        deleted_dirs,
        deleted_bytes,
        dry_run=not apply,
    )
