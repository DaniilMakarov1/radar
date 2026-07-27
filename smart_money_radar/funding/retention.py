from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.storage import SQLiteStore

RETENTION_LOCK_RETRIES = 3
RETENTION_LOCK_RETRY_DELAY_SECONDS = 2.0


FUNDING_SCAN_CHILD_TABLES = (
    "funding_paper_executions",
    "funding_market_snapshots",
    "funding_orderbook_snapshots",
    "funding_route_universe",
    "funding_scan_warnings",
)
ROUTINE_PAPER_EVENT_TYPES = (
    "scan",
    "hot_scan",
    "background_scan",
    "status_report",
    "armed",
    "disarmed",
    "focused_recheck_failed",
    "focused_recheck_failed_inside_freeze_window",
    "focused_recheck_skipped",
    "retention_skipped",
)
DEFAULT_KEEP_LATEST_ROUTINE_PAPER_EVENTS = 200
DEFAULT_KEEP_LATEST_EQUITY_SNAPSHOTS = 300
DEFAULT_STALE_RUNNING_SCAN_SECONDS = 900


@dataclass(frozen=True)
class FundingRetentionPlan:
    keep_latest_scans: int
    total_scan_count: int
    protected_scan_count: int
    delete_scan_count: int
    delete_route_count: int
    keep_latest_history_per_market: int
    rows_by_table: dict[str, int]
    delete_scan_ids: list[int]
    protected_scan_ids: list[int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "keep_latest_scans": self.keep_latest_scans,
            "total_scan_count": self.total_scan_count,
            "protected_scan_count": self.protected_scan_count,
            "delete_scan_count": self.delete_scan_count,
            "delete_route_count": self.delete_route_count,
            "keep_latest_history_per_market": self.keep_latest_history_per_market,
            "rows_by_table": self.rows_by_table,
            "delete_scan_ids": self.delete_scan_ids,
            "protected_scan_ids": self.protected_scan_ids,
        }


def build_funding_retention_plan(
    store: SQLiteStore,
    keep_latest_scans: int = 5,
    keep_latest_history_per_market: int = 24,
    keep_latest_routine_paper_events: int = DEFAULT_KEEP_LATEST_ROUTINE_PAPER_EVENTS,
    keep_latest_equity_snapshots: int = DEFAULT_KEEP_LATEST_EQUITY_SNAPSHOTS,
    stale_running_scan_seconds: int = DEFAULT_STALE_RUNNING_SCAN_SECONDS,
) -> FundingRetentionPlan:
    retained = max(1, int(keep_latest_scans))
    retained_history = max(1, int(keep_latest_history_per_market))
    retained_routine_events = max(0, int(keep_latest_routine_paper_events))
    retained_equity = max(1, int(keep_latest_equity_snapshots))
    with store.connect() as connection:
        scan_ids = [
            int(row["funding_scan_id"])
            for row in connection.execute(
                """
                SELECT funding_scan_id
                FROM funding_scans
                ORDER BY funding_scan_id DESC
                """
            )
        ]
        latest_scan_ids = set(scan_ids[:retained])
        latest_non_focused_scan_ids = latest_non_focused_funding_scan_ids(
            connection,
            retained,
        )
        protected_route_ids = paper_linked_route_ids(connection)
        protected_scan_ids = (
            latest_scan_ids
            | latest_non_focused_scan_ids
            | running_funding_scan_ids(
                connection,
                stale_running_scan_seconds=stale_running_scan_seconds,
            )
            | paper_linked_scan_ids(connection)
            | route_scan_ids(connection, protected_route_ids)
        )
        delete_scan_ids = [
            scan_id for scan_id in scan_ids if scan_id not in protected_scan_ids
        ]
        delete_route_ids = routes_for_scans(
            connection,
            delete_scan_ids,
            exclude_route_ids=protected_route_ids,
        )
        rows_by_table = {
            table: count_rows_for_scans(connection, table, delete_scan_ids)
            for table in FUNDING_SCAN_CHILD_TABLES
        }
        rows_by_table["funding_routes"] = len(delete_route_ids)
        rows_by_table["funding_scans"] = len(delete_scan_ids)
        rows_by_table["funding_rate_history"] = count_old_funding_history_rows(
            connection,
            retained_history,
        )
        rows_by_table["funding_paper_events"] = count_old_routine_paper_events(
            connection,
            retained_routine_events,
        )
        rows_by_table["funding_paper_equity_snapshots"] = (
            count_old_funding_paper_equity_snapshots(connection, retained_equity)
        )
    return FundingRetentionPlan(
        keep_latest_scans=retained,
        total_scan_count=len(scan_ids),
        protected_scan_count=len(protected_scan_ids),
        delete_scan_count=len(delete_scan_ids),
        delete_route_count=len(delete_route_ids),
        keep_latest_history_per_market=retained_history,
        rows_by_table=rows_by_table,
        delete_scan_ids=delete_scan_ids,
        protected_scan_ids=sorted(protected_scan_ids, reverse=True),
    )


def apply_funding_retention_plan(
    store: SQLiteStore,
    keep_latest_scans: int = 5,
    keep_latest_history_per_market: int = 24,
    keep_latest_routine_paper_events: int = DEFAULT_KEEP_LATEST_ROUTINE_PAPER_EVENTS,
    keep_latest_equity_snapshots: int = DEFAULT_KEEP_LATEST_EQUITY_SNAPSHOTS,
    stale_running_scan_seconds: int = DEFAULT_STALE_RUNNING_SCAN_SECONDS,
) -> dict[str, Any]:
    stale_running_marked = mark_stale_running_funding_scans(
        store,
        stale_running_scan_seconds=stale_running_scan_seconds,
    )
    plan = build_funding_retention_plan(
        store,
        keep_latest_scans=keep_latest_scans,
        keep_latest_history_per_market=keep_latest_history_per_market,
        keep_latest_routine_paper_events=keep_latest_routine_paper_events,
        keep_latest_equity_snapshots=keep_latest_equity_snapshots,
        stale_running_scan_seconds=stale_running_scan_seconds,
    )
    deleted_rows = {key: 0 for key in plan.rows_by_table}
    if not plan.delete_scan_ids and all(
        count <= 0 for count in plan.rows_by_table.values()
    ):
        result = plan.as_dict()
        result["deleted_rows"] = deleted_rows
        result["stale_running_scans_marked"] = stale_running_marked
        return result

    last_error: Exception | None = None
    for attempt in range(RETENTION_LOCK_RETRIES):
        try:
            deleted_rows, delete_scan_ids = _apply_retention_deletes(
                store,
                plan,
                keep_latest_routine_paper_events,
                keep_latest_equity_snapshots,
            )
            break
        except sqlite3.IntegrityError as exc:
            last_error = exc
            if attempt < RETENTION_LOCK_RETRIES - 1:
                time.sleep(RETENTION_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc):
                raise
            last_error = exc
            if attempt < RETENTION_LOCK_RETRIES - 1:
                time.sleep(RETENTION_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    else:
        raise last_error  # type: ignore[misc]

    result = plan.as_dict()
    result["applied_delete_scan_ids"] = delete_scan_ids
    result["skipped_protected_scan_ids"] = [
        scan_id for scan_id in plan.delete_scan_ids if scan_id not in delete_scan_ids
    ]
    result["deleted_rows"] = deleted_rows
    result["stale_running_scans_marked"] = stale_running_marked
    return result


def _apply_retention_deletes(
    store: SQLiteStore,
    plan: FundingRetentionPlan,
    keep_latest_routine_paper_events: int,
    keep_latest_equity_snapshots: int,
) -> tuple[dict[str, int], list[int]]:
    deleted_rows = {key: 0 for key in plan.rows_by_table}
    with store.connect() as connection:
        connection.execute("PRAGMA defer_foreign_keys = ON")
        delete_scan_ids = retainable_scan_ids(connection, plan.delete_scan_ids)
        for scan_ids in chunks(delete_scan_ids, 200):
            delete_route_ids = routes_for_scans(
                connection,
                scan_ids,
                exclude_route_ids=paper_linked_route_ids(connection),
            )
            detach_paper_events_for_deleted_scan_data(
                connection,
                scan_ids,
                delete_route_ids,
            )
            deleted_rows["funding_paper_executions"] += delete_funding_paper_executions(
                connection,
                scan_ids,
                delete_route_ids,
            )
            for table in FUNDING_SCAN_CHILD_TABLES:
                deleted_rows[table] += delete_by_ids(
                    connection,
                    table,
                    "funding_scan_id",
                    scan_ids,
                )
            if delete_route_ids:
                deleted_rows["funding_routes"] += delete_funding_routes(
                    connection,
                    delete_route_ids,
                )
            deleted_rows["funding_scans"] += delete_by_ids(
                connection,
                "funding_scans",
                "funding_scan_id",
                scan_ids,
            )
        deleted_rows["funding_rate_history"] = prune_funding_rate_history(
            connection,
            plan.keep_latest_history_per_market,
        )
        deleted_rows["funding_paper_events"] = prune_routine_paper_events(
            connection,
            keep_latest_routine_paper_events,
        )
        deleted_rows["funding_paper_equity_snapshots"] = (
            prune_funding_paper_equity_snapshots(
                connection,
                keep_latest_equity_snapshots,
            )
        )
    return deleted_rows, delete_scan_ids


def retainable_scan_ids(
    connection: sqlite3.Connection,
    candidate_scan_ids: list[int],
) -> list[int]:
    """Return scan ids that are still safe to delete at apply time."""
    if not candidate_scan_ids:
        return []
    protected_route_ids = paper_linked_route_ids(connection)
    protected_scan_ids = (
        running_funding_scan_ids(connection)
        | paper_linked_scan_ids(connection)
        | route_scan_ids(connection, protected_route_ids)
    )
    return [
        int(scan_id)
        for scan_id in candidate_scan_ids
        if int(scan_id) not in protected_scan_ids
    ]


def paper_linked_scan_ids(connection: sqlite3.Connection) -> set[int]:
    scan_ids: set[int] = set()
    for row in connection.execute(
        """
        SELECT open_funding_scan_id, close_funding_scan_id
        FROM funding_paper_positions
        """
    ):
        for key in ("open_funding_scan_id", "close_funding_scan_id"):
            if row[key] is not None:
                scan_ids.add(int(row[key]))
    return scan_ids


def running_funding_scan_ids(
    connection: sqlite3.Connection,
    *,
    stale_running_scan_seconds: int = DEFAULT_STALE_RUNNING_SCAN_SECONDS,
) -> set[int]:
    cutoff = stale_running_scan_cutoff(stale_running_scan_seconds)
    rows = connection.execute(
        """
        SELECT funding_scan_id
        FROM funding_scans
        WHERE status = 'running'
          AND started_at >= ?
        """
        ,
        (cutoff,),
    ).fetchall()
    return {int(row["funding_scan_id"]) for row in rows}


def stale_running_scan_cutoff(stale_running_scan_seconds: int) -> str:
    seconds = max(60, int(stale_running_scan_seconds))
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def mark_stale_running_funding_scans(
    store: SQLiteStore,
    *,
    stale_running_scan_seconds: int = DEFAULT_STALE_RUNNING_SCAN_SECONDS,
) -> int:
    cutoff = stale_running_scan_cutoff(stale_running_scan_seconds)
    with store.connect() as connection:
        cursor = connection.execute(
            """
            UPDATE funding_scans
            SET status = 'failed',
                finished_at = COALESCE(finished_at, ?),
                error = COALESCE(error, 'stale_running_scan_timeout')
            WHERE status = 'running'
              AND started_at < ?
            """,
            (datetime.now(UTC).isoformat(timespec="seconds"), cutoff),
        )
        return int(cursor.rowcount or 0)


def latest_non_focused_funding_scan_ids(
    connection: sqlite3.Connection,
    keep_latest: int,
) -> set[int]:
    rows = connection.execute(
        """
        SELECT funding_scan_id
        FROM funding_scans
        WHERE json_extract(config_json, '$.focused_route_key') IS NULL
        ORDER BY funding_scan_id DESC
        LIMIT ?
        """,
        (max(1, int(keep_latest)),),
    ).fetchall()
    return {int(row["funding_scan_id"]) for row in rows}


def paper_linked_route_ids(connection: sqlite3.Connection) -> set[int]:
    route_ids: set[int] = set()
    for row in connection.execute(
        """
        SELECT open_funding_route_id, close_funding_route_id
        FROM funding_paper_positions
        """
    ):
        for key in ("open_funding_route_id", "close_funding_route_id"):
            if row[key] is not None:
                route_ids.add(int(row[key]))
    return route_ids


def count_old_routine_paper_events(
    connection: sqlite3.Connection,
    keep_latest: int,
) -> int:
    keep = max(0, int(keep_latest))
    placeholders = ",".join("?" for _ in ROUTINE_PAPER_EVENT_TYPES)
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT ROW_NUMBER() OVER (
                    ORDER BY funding_paper_event_id DESC
                ) AS row_number
                FROM funding_paper_events
                WHERE event_type IN ({placeholders})
            )
            WHERE row_number > ?
            """,
            (*ROUTINE_PAPER_EVENT_TYPES, keep),
        ).fetchone()[0]
    )


def prune_routine_paper_events(
    connection: sqlite3.Connection,
    keep_latest: int,
) -> int:
    keep = max(0, int(keep_latest))
    placeholders = ",".join("?" for _ in ROUTINE_PAPER_EVENT_TYPES)
    cursor = connection.execute(
        f"""
        DELETE FROM funding_paper_events
        WHERE funding_paper_event_id IN (
            SELECT funding_paper_event_id
            FROM (
                SELECT funding_paper_event_id,
                       ROW_NUMBER() OVER (
                           ORDER BY funding_paper_event_id DESC
                       ) AS row_number
                FROM funding_paper_events
                WHERE event_type IN ({placeholders})
            )
            WHERE row_number > ?
        )
        """,
        (*ROUTINE_PAPER_EVENT_TYPES, keep),
    )
    return int(cursor.rowcount or 0)


def count_old_funding_paper_equity_snapshots(
    connection: sqlite3.Connection,
    keep_latest: int,
) -> int:
    keep = max(1, int(keep_latest))
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT ROW_NUMBER() OVER (
                    ORDER BY funding_paper_equity_snapshot_id DESC
                ) AS row_number
                FROM funding_paper_equity_snapshots
            )
            WHERE row_number > ?
            """,
            (keep,),
        ).fetchone()[0]
    )


def prune_funding_paper_equity_snapshots(
    connection: sqlite3.Connection,
    keep_latest: int,
) -> int:
    keep = max(1, int(keep_latest))
    cursor = connection.execute(
        """
        DELETE FROM funding_paper_equity_snapshots
        WHERE funding_paper_equity_snapshot_id IN (
            SELECT funding_paper_equity_snapshot_id
            FROM (
                SELECT funding_paper_equity_snapshot_id,
                       ROW_NUMBER() OVER (
                           ORDER BY funding_paper_equity_snapshot_id DESC
                       ) AS row_number
                FROM funding_paper_equity_snapshots
            )
            WHERE row_number > ?
        )
        """,
        (keep,),
    )
    return int(cursor.rowcount or 0)


def route_scan_ids(
    connection: sqlite3.Connection,
    route_ids: set[int],
) -> set[int]:
    if not route_ids:
        return set()
    scan_ids: set[int] = set()
    for chunk in chunks(sorted(route_ids), 500):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT DISTINCT funding_scan_id
            FROM funding_routes
            WHERE funding_route_id IN ({placeholders})
              AND funding_scan_id IS NOT NULL
            """,
            chunk,
        ).fetchall()
        scan_ids.update(int(row["funding_scan_id"]) for row in rows)
    return scan_ids


def routes_for_scans(
    connection: sqlite3.Connection,
    scan_ids: list[int],
    exclude_route_ids: set[int],
) -> list[int]:
    if not scan_ids:
        return []
    placeholders = ",".join("?" for _ in scan_ids)
    rows = connection.execute(
        f"""
        SELECT funding_route_id
        FROM funding_routes
        WHERE funding_scan_id IN ({placeholders})
        """,
        scan_ids,
    ).fetchall()
    return [
        int(row["funding_route_id"])
        for row in rows
        if int(row["funding_route_id"]) not in exclude_route_ids
    ]


def delete_funding_paper_executions(
    connection: sqlite3.Connection,
    scan_ids: list[int],
    route_ids: list[int],
) -> int:
    deleted = 0
    if scan_ids:
        placeholders = ",".join("?" for _ in scan_ids)
        cursor = connection.execute(
            f"""
            DELETE FROM funding_paper_executions
            WHERE funding_scan_id IN ({placeholders})
            """,
            scan_ids,
        )
        deleted += int(cursor.rowcount if cursor.rowcount is not None else 0)
    if route_ids:
        placeholders = ",".join("?" for _ in route_ids)
        cursor = connection.execute(
            f"""
            DELETE FROM funding_paper_executions
            WHERE funding_route_id IN ({placeholders})
            """,
            route_ids,
        )
        deleted += int(cursor.rowcount if cursor.rowcount is not None else 0)
    return deleted


def delete_funding_routes(
    connection: sqlite3.Connection,
    route_ids: list[int],
) -> int:
    """Delete route diagnostics without breaking paper-trade FK links."""
    protected_route_ids = paper_linked_route_ids(connection)
    safe_route_ids = [
        int(route_id)
        for route_id in route_ids
        if int(route_id) not in protected_route_ids
    ]
    if not safe_route_ids:
        return 0
    detach_paper_events_for_deleted_scan_data(connection, [], safe_route_ids)
    delete_funding_paper_executions(connection, [], safe_route_ids)
    return delete_by_ids(
        connection,
        "funding_routes",
        "funding_route_id",
        safe_route_ids,
    )


def detach_paper_events_for_deleted_scan_data(
    connection: sqlite3.Connection,
    scan_ids: list[int],
    route_ids: list[int],
) -> None:
    if not scan_ids and not route_ids:
        return
    if scan_ids:
        for chunk in chunks(scan_ids, 500):
            scan_placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"""
                UPDATE funding_paper_events
                SET funding_scan_id = NULL
                WHERE funding_scan_id IN ({scan_placeholders})
                """,
                chunk,
            )
    if route_ids:
        for chunk in chunks(route_ids, 500):
            route_placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"""
                UPDATE funding_paper_events
                SET funding_route_id = NULL
                WHERE funding_route_id IN ({route_placeholders})
                """,
                chunk,
            )


def count_rows_for_scans(
    connection: sqlite3.Connection,
    table: str,
    scan_ids: list[int],
) -> int:
    if not scan_ids:
        return 0
    total = 0
    for chunk in chunks(scan_ids, 500):
        placeholders = ",".join("?" for _ in chunk)
        total += int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {table}
                WHERE funding_scan_id IN ({placeholders})
                """,
                chunk,
            ).fetchone()[0]
        )
    return total


def count_old_funding_history_rows(
    connection: sqlite3.Connection,
    keep_latest_per_market: int,
) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT ROW_NUMBER() OVER (
                    PARTITION BY venue, symbol
                    ORDER BY funding_at DESC
                ) AS row_number
                FROM funding_rate_history
            )
            WHERE row_number > ?
            """,
            (max(1, int(keep_latest_per_market)),),
        ).fetchone()[0]
    )


def prune_funding_rate_history(
    connection: sqlite3.Connection,
    keep_latest_per_market: int,
) -> int:
    keep = max(1, int(keep_latest_per_market))
    connection.execute("DROP TABLE IF EXISTS temp.funding_history_prune_keys")
    connection.execute(
        """
        CREATE TEMP TABLE funding_history_prune_keys (
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,
            funding_at TEXT NOT NULL,
            PRIMARY KEY (venue, symbol, funding_at)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO funding_history_prune_keys (venue, symbol, funding_at)
        SELECT venue, symbol, funding_at
        FROM (
            SELECT venue, symbol, funding_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY venue, symbol
                       ORDER BY funding_at DESC
                   ) AS row_number
            FROM funding_rate_history
        )
        WHERE row_number > ?
        """,
        (keep,),
    )
    deleted = int(
        connection.execute(
            """
            DELETE FROM funding_rate_history
            WHERE EXISTS (
                SELECT 1
                FROM funding_history_prune_keys old
                WHERE old.venue = funding_rate_history.venue
                  AND old.symbol = funding_rate_history.symbol
                  AND old.funding_at = funding_rate_history.funding_at
            )
            """
        ).rowcount
        or 0
    )
    refresh_funding_history_sync_state(connection)
    connection.execute("DROP TABLE IF EXISTS temp.funding_history_prune_keys")
    return deleted


def refresh_funding_history_sync_state(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        DELETE FROM funding_history_sync_state
        WHERE NOT EXISTS (
            SELECT 1
            FROM funding_rate_history history
            WHERE history.venue = funding_history_sync_state.venue
              AND history.symbol = funding_history_sync_state.symbol
        )
        """
    )
    connection.execute(
        """
        UPDATE funding_history_sync_state
        SET row_count = (
                SELECT COUNT(*)
                FROM funding_rate_history history
                WHERE history.venue = funding_history_sync_state.venue
                  AND history.symbol = funding_history_sync_state.symbol
            ),
            earliest_funding_at = (
                SELECT MIN(funding_at)
                FROM funding_rate_history history
                WHERE history.venue = funding_history_sync_state.venue
                  AND history.symbol = funding_history_sync_state.symbol
            ),
            latest_funding_at = (
                SELECT MAX(funding_at)
                FROM funding_rate_history history
                WHERE history.venue = funding_history_sync_state.venue
                  AND history.symbol = funding_history_sync_state.symbol
            )
        """
    )


def delete_by_ids(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ids: list[int],
) -> int:
    if not ids:
        return 0
    deleted = 0
    for chunk in chunks(ids, 500):
        placeholders = ",".join("?" for _ in chunk)
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
            chunk,
        )
        deleted += int(cursor.rowcount if cursor.rowcount is not None else 0)
    return deleted


def chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
