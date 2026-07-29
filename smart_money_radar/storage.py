from __future__ import annotations

import json
import math
import sqlite3
import csv
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from smart_money_radar.config import (
    CHAINS,
    DEFAULT_DB_PATH,
    FUNDING_MINIMUM_ACTIONABLE_NOTIONAL,
    SCHEMA_PATH,
    ChainConfig,
)
from smart_money_radar.funding.normalization import CANONICAL_ASSET_UNIT_MULTIPLIERS


FUNDING_UNIT_MULTIPLIERS = CANONICAL_ASSET_UNIT_MULTIPLIERS


def funding_unit_multiplier(row: dict[str, Any]) -> float:
    values = (
        row.get("base_asset"),
        row.get("symbol"),
        row.get("canonical_asset"),
    )
    for value in values:
        cleaned = re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())
        for alias in sorted(FUNDING_UNIT_MULTIPLIERS, key=len, reverse=True):
            if cleaned.startswith(alias):
                return FUNDING_UNIT_MULTIPLIERS[alias]
    return 1.0


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _funding_wait_bucket(wait_seconds: float) -> str:
    wait = max(0.0, float(wait_seconds))
    if wait <= 3_600.0:
        return "<=1h"
    if wait <= 7_200.0:
        return ">1h<=2h"
    return ">2h<=4h"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(/bot)[^/\s]+|"
    r"((?:api[_-]?key|secret|token|authorization|signature|auth)[=:]\s*)[^\s,;]+|"
    r"(bearer\s+)[a-z0-9._~+/=-]+"
)


def redact_sensitive_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = SENSITIVE_TEXT_RE.sub(
        lambda match: (
            f"{match.group(1)}<redacted>"
            if match.group(1)
            else f"{match.group(2) or match.group(3)}<redacted>"
        ),
        text,
    )
    return text[:1_000]


FUNDING_SHADOW_ALERT_STATES = {
    "CLAIMED",
    "SENT",
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "RETRY_SCHEDULED",
}


def _stored_alert_state(row: sqlite3.Row) -> str:
    state = str(row["state"] or "").strip().upper() if "state" in row.keys() else ""
    if state in FUNDING_SHADOW_ALERT_STATES:
        return state
    telegram_status = str(row["telegram_status"] or "").strip().lower()
    if telegram_status == "sent":
        return "SENT"
    if telegram_status in {"disabled", "not_sent"}:
        return "FAILED_PERMANENT"
    if telegram_status in {"failed", "queued"}:
        return "FAILED_RETRYABLE"
    return "FAILED_RETRYABLE"


class SQLiteStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            self._ensure_funding_paper_position_columns(connection)
            self._ensure_funding_capture_cycle_columns(connection)
            self._ensure_funding_shadow_columns(connection)
            self.upsert_chains(connection, CHAINS)

    def _ensure_funding_paper_position_columns(
        self, connection: sqlite3.Connection
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(funding_paper_positions)"
            )
        }
        for name in ("entry_cross_spread", "entry_basis_bps", "actual_basis_pnl"):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE funding_paper_positions ADD COLUMN {name} REAL"
                )

    def _ensure_funding_capture_cycle_columns(
        self, connection: sqlite3.Connection
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(funding_capture_cycles)")
        }
        specs = {
            "plan_generation": "INTEGER NOT NULL DEFAULT 0",
            "active_plan_json": "TEXT NOT NULL DEFAULT '{}'",
            "boundary_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, spec in specs.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE funding_capture_cycles ADD COLUMN {name} {spec}"
                )

    def _ensure_funding_shadow_columns(self, connection: sqlite3.Connection) -> None:
        alert_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(funding_shadow_alerts)")
        }
        alert_specs = {
            "state": "TEXT NOT NULL DEFAULT 'CLAIMED'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "claimed_at": "TEXT",
            "claim_expires_at": "TEXT",
            "sent_at": "TEXT",
            "failed_at": "TEXT",
            "next_retry_at": "TEXT",
            "last_error_redacted": "TEXT",
        }
        for name, spec in alert_specs.items():
            if name not in alert_columns:
                connection.execute(
                    f"ALTER TABLE funding_shadow_alerts ADD COLUMN {name} {spec}"
                )

        health_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(funding_shadow_venue_health)")
        }
        health_specs = {
            "request_started_at": "TEXT",
            "response_received_at": "TEXT",
            "parsing_completed_at": "TEXT",
            "network_latency_ms": "REAL",
            "parsing_latency_ms": "REAL",
            "total_latency_ms": "REAL",
            "endpoint_class": "TEXT",
        }
        for name, spec in health_specs.items():
            if name not in health_columns:
                connection.execute(
                    f"ALTER TABLE funding_shadow_venue_health ADD COLUMN {name} {spec}"
                )

    def sqlite_maintenance(
        self,
        *,
        vacuum: bool = False,
        analyze: bool = True,
        optimize: bool = True,
        wal_checkpoint: bool = True,
    ) -> dict[str, Any]:
        before_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        operations: list[str] = []
        with self.connect() as connection:
            if wal_checkpoint:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                operations.append("wal_checkpoint")
            if analyze:
                connection.execute("ANALYZE")
                operations.append("analyze")
            if optimize:
                connection.execute("PRAGMA optimize")
                operations.append("optimize")
        if vacuum:
            connection = sqlite3.connect(self.db_path, timeout=60.0)
            try:
                connection.execute("PRAGMA busy_timeout = 60000")
                connection.execute("VACUUM")
                operations.append("vacuum")
            finally:
                connection.close()
        after_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "database_path": str(self.db_path),
            "operations": operations,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "saved_bytes": max(0, before_bytes - after_bytes),
        }

    def upsert_funding_capture_position(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        position_id = str(row["position_id"])
        config_json = json.dumps(row.get("config", row.get("config_json", {})), sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_capture_positions (
                    position_id,
                    funding_paper_position_id,
                    strategy_name,
                    strategy_version,
                    canonical_asset,
                    long_venue,
                    long_symbol,
                    short_venue,
                    short_symbol,
                    quantity,
                    target_notional,
                    state,
                    opened_at,
                    closed_at,
                    settlements_captured_count,
                    max_settlements,
                    original_entry_spread,
                    paper_open_fees,
                    paper_close_fees,
                    paper_emergency_unwind_cost,
                    paper_net_pnl_estimated,
                    paper_net_pnl_reconciled,
                    config_json,
                    config_hash,
                    code_commit,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    funding_paper_position_id = excluded.funding_paper_position_id,
                    strategy_name = excluded.strategy_name,
                    strategy_version = excluded.strategy_version,
                    canonical_asset = excluded.canonical_asset,
                    long_venue = excluded.long_venue,
                    long_symbol = excluded.long_symbol,
                    short_venue = excluded.short_venue,
                    short_symbol = excluded.short_symbol,
                    quantity = excluded.quantity,
                    target_notional = excluded.target_notional,
                    state = excluded.state,
                    opened_at = excluded.opened_at,
                    closed_at = excluded.closed_at,
                    settlements_captured_count = excluded.settlements_captured_count,
                    max_settlements = excluded.max_settlements,
                    original_entry_spread = excluded.original_entry_spread,
                    paper_open_fees = excluded.paper_open_fees,
                    paper_close_fees = excluded.paper_close_fees,
                    paper_emergency_unwind_cost = excluded.paper_emergency_unwind_cost,
                    paper_net_pnl_estimated = excluded.paper_net_pnl_estimated,
                    paper_net_pnl_reconciled = excluded.paper_net_pnl_reconciled,
                    config_json = excluded.config_json,
                    config_hash = excluded.config_hash,
                    code_commit = excluded.code_commit,
                    updated_at = excluded.updated_at
                """,
                (
                    position_id,
                    row.get("funding_paper_position_id"),
                    row.get("strategy_name", "synchronized_funding_capture"),
                    row.get("strategy_version", "synchronized_funding_capture_v2"),
                    row["canonical_asset"],
                    row["long_venue"],
                    row["long_symbol"],
                    row["short_venue"],
                    row["short_symbol"],
                    float(row["quantity"]),
                    float(row["target_notional"]),
                    row.get("state", "OPEN"),
                    row.get("opened_at", now),
                    row.get("closed_at"),
                    int(row.get("settlements_captured_count", 0)),
                    int(row.get("max_settlements", 4)),
                    row.get("original_entry_spread"),
                    float(row.get("paper_open_fees", 0.0)),
                    float(row.get("paper_close_fees", 0.0)),
                    float(row.get("paper_emergency_unwind_cost", 0.0)),
                    row.get("paper_net_pnl_estimated"),
                    row.get("paper_net_pnl_reconciled"),
                    config_json,
                    row.get("config_hash"),
                    row.get("code_commit"),
                    row.get("created_at", now),
                    now,
                ),
            )
        return position_id

    def upsert_funding_capture_cycle(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        cycle_id = str(
            row.get("cycle_id")
            or f"{row['position_id']}:{row['cycle_number']}"
        )
        active_plan_json = json.dumps(
            row.get("active_plan", row.get("active_plan_json", {})),
            sort_keys=True,
        )
        boundary_evidence_json = json.dumps(
            row.get("boundary_evidence", row.get("boundary_evidence_json", {})),
            sort_keys=True,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_capture_cycles (
                    cycle_id,
                    position_id,
                    cycle_number,
                    plan_generation,
                    scheduled_funding_at,
                    long_next_funding_rate_at_decision,
                    short_next_funding_rate_at_decision,
                    conservative_funding_gross,
                    conservative_funding_edge_bps,
                    hold_basis_reserve_bps,
                    hold_legging_reserve_bps,
                    hold_time_reserve_bps,
                    hold_liquidity_reserve_bps,
                    incremental_hold_cost,
                    incremental_hold_net_pnl,
                    hold_cost_coverage_ratio,
                    paper_net_if_exit_at_decision,
                    decision,
                    decision_reason,
                    state,
                    settlement_crossed_at,
                    reconciliation_status,
                    reconciled_funding_pnl,
                    active_plan_json,
                    boundary_evidence_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id, cycle_number) DO UPDATE SET
                    plan_generation = excluded.plan_generation,
                    scheduled_funding_at = excluded.scheduled_funding_at,
                    long_next_funding_rate_at_decision = excluded.long_next_funding_rate_at_decision,
                    short_next_funding_rate_at_decision = excluded.short_next_funding_rate_at_decision,
                    conservative_funding_gross = excluded.conservative_funding_gross,
                    conservative_funding_edge_bps = excluded.conservative_funding_edge_bps,
                    hold_basis_reserve_bps = excluded.hold_basis_reserve_bps,
                    hold_legging_reserve_bps = excluded.hold_legging_reserve_bps,
                    hold_time_reserve_bps = excluded.hold_time_reserve_bps,
                    hold_liquidity_reserve_bps = excluded.hold_liquidity_reserve_bps,
                    incremental_hold_cost = excluded.incremental_hold_cost,
                    incremental_hold_net_pnl = excluded.incremental_hold_net_pnl,
                    hold_cost_coverage_ratio = excluded.hold_cost_coverage_ratio,
                    paper_net_if_exit_at_decision = excluded.paper_net_if_exit_at_decision,
                    decision = excluded.decision,
                    decision_reason = excluded.decision_reason,
                    state = excluded.state,
                    settlement_crossed_at = excluded.settlement_crossed_at,
                    reconciliation_status = excluded.reconciliation_status,
                    reconciled_funding_pnl = excluded.reconciled_funding_pnl,
                    active_plan_json = CASE
                        WHEN excluded.active_plan_json != '{}' THEN excluded.active_plan_json
                        ELSE funding_capture_cycles.active_plan_json
                    END,
                    boundary_evidence_json = CASE
                        WHEN excluded.boundary_evidence_json != '{}' THEN excluded.boundary_evidence_json
                        ELSE funding_capture_cycles.boundary_evidence_json
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle_id,
                    row["position_id"],
                    int(row["cycle_number"]),
                    int(row.get("plan_generation") or row.get("cycle_number") or 0),
                    row["scheduled_funding_at"],
                    row.get("long_next_funding_rate_at_decision"),
                    row.get("short_next_funding_rate_at_decision"),
                    row.get("conservative_funding_gross"),
                    row.get("conservative_funding_edge_bps"),
                    row.get("hold_basis_reserve_bps"),
                    row.get("hold_legging_reserve_bps"),
                    row.get("hold_time_reserve_bps"),
                    row.get("hold_liquidity_reserve_bps"),
                    row.get("incremental_hold_cost"),
                    row.get("incremental_hold_net_pnl"),
                    row.get("hold_cost_coverage_ratio"),
                    row.get("paper_net_if_exit_at_decision"),
                    row.get("decision"),
                    row.get("decision_reason"),
                    row.get("state", "PENDING"),
                    row.get("settlement_crossed_at"),
                    row.get("reconciliation_status", "PENDING"),
                    row.get("reconciled_funding_pnl"),
                    active_plan_json,
                    boundary_evidence_json,
                    row.get("created_at", now),
                    now,
                ),
            )
        return cycle_id

    def begin_funding_entry_attempt(
        self,
        *,
        position_id: str,
        route_key: str | None,
        route_entry_key: str | None,
        attempt_id: str,
        settlement_at: datetime,
        submitted_at: datetime,
        fault_after: str | None = None,
    ) -> dict[str, Any]:
        """Atomically mark a v2 entry attempt as submitted.

        This intentionally keeps the config marker, audit record, and position
        lifecycle state in one SQLite transaction so recovery never sees
        ARMED + entry_attempt_submitted from a partial begin-entry write.
        """
        submitted_iso = submitted_at.astimezone(UTC).replace(microsecond=0).isoformat()
        settlement_iso = settlement_at.astimezone(UTC).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT config_json
                FROM funding_capture_positions
                WHERE position_id = ?
                """,
                (position_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"funding capture position not found: {position_id}")
            config = json.loads(row["config_json"] or "{}")
            attempts = [dict(item) for item in list(config.get("entry_attempts") or []) if isinstance(item, dict)]
            existing = next(
                (item for item in attempts if str(item.get("attempt_id") or "") == str(attempt_id)),
                None,
            )
            if existing is None:
                attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "opportunity_id": position_id,
                        "route_entry_key": route_entry_key,
                        "settlement_at": settlement_iso,
                        "submitted_at": submitted_at.astimezone(UTC).isoformat(),
                        "state": "ENTRY_SUBMITTED",
                    }
                )
            else:
                existing.update(
                    {
                        "opportunity_id": position_id,
                        "route_entry_key": existing.get("route_entry_key") or route_entry_key,
                        "settlement_at": existing.get("settlement_at") or settlement_iso,
                        "submitted_at": existing.get("submitted_at") or submitted_at.astimezone(UTC).isoformat(),
                        "state": "ENTRY_SUBMITTED",
                    }
                )
            config["opportunity_id"] = position_id
            if route_key:
                config["route_key"] = route_key
            config["entry_attempt_id"] = attempt_id
            config["attempt_id"] = attempt_id
            config["entry_attempt_submitted"] = True
            config["entry_attempt_submitted_at"] = submitted_at.astimezone(UTC).isoformat()
            config["entry_attempt_state"] = "ENTRY_SUBMITTED"
            config["entry_attempts"] = attempts
            connection.execute(
                """
                UPDATE funding_capture_positions
                SET config_json = ?,
                    updated_at = ?
                WHERE position_id = ?
                """,
                (json.dumps(config, sort_keys=True), submitted_iso, position_id),
            )
            if fault_after == "config":
                raise RuntimeError("fault_after_config")
            connection.execute(
                """
                UPDATE funding_capture_positions
                SET state = 'ENTRY_SUBMITTED',
                    updated_at = ?
                WHERE position_id = ?
                """,
                (submitted_iso, position_id),
            )
        return {"position_id": position_id, "attempt_id": attempt_id, "state": "ENTRY_SUBMITTED"}

    def upsert_funding_settlement_reconciliation(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        reconciliation_id = str(
            row.get("reconciliation_id")
            or ":".join(
                [
                    str(row["position_id"]),
                    str(row["venue"]),
                    str(row["scheduled_funding_at"]),
                ]
            )
        )
        evidence_json = json.dumps(row.get("evidence", row.get("evidence_json", {})), sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_settlement_reconciliations (
                    reconciliation_id,
                    position_id,
                    cycle_id,
                    venue,
                    symbol,
                    side,
                    scheduled_funding_at,
                    status,
                    confirmed_funding_rate,
                    settlement_mark_price,
                    funding_pnl,
                    rate_status,
                    mark_status,
                    evidence_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id, venue, scheduled_funding_at) DO UPDATE SET
                    cycle_id = excluded.cycle_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = excluded.status,
                    confirmed_funding_rate = excluded.confirmed_funding_rate,
                    settlement_mark_price = excluded.settlement_mark_price,
                    funding_pnl = excluded.funding_pnl,
                    rate_status = excluded.rate_status,
                    mark_status = excluded.mark_status,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    reconciliation_id,
                    row["position_id"],
                    row.get("cycle_id"),
                    row["venue"],
                    row["symbol"],
                    row["side"],
                    row["scheduled_funding_at"],
                    row.get("status", "PENDING"),
                    row.get("confirmed_funding_rate"),
                    row.get("settlement_mark_price"),
                    row.get("funding_pnl"),
                    row.get("rate_status"),
                    row.get("mark_status"),
                    evidence_json,
                    row.get("created_at", now),
                    now,
                ),
            )
        return reconciliation_id

    def funding_settlement_reconciliation_rows(
        self,
        position_id: str,
        *,
        cycle_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [position_id]
        cycle_filter = ""
        if cycle_id is not None:
            cycle_filter = "AND cycle_id = ?"
            parameters.append(cycle_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM funding_settlement_reconciliations
                WHERE position_id = ?
                {cycle_filter}
                ORDER BY scheduled_funding_at, venue
                """,
                parameters,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output

    def pending_reconciliation_rows(self) -> list[dict[str, Any]]:
        """Return unresolved reconciliation rows that can still progress."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM funding_settlement_reconciliations
                WHERE status IN ('PENDING', 'PUBLIC_RATE_CONFIRMED')
                ORDER BY scheduled_funding_at, venue
                """
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output

    def reconciliation_rows_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM funding_settlement_reconciliations
                WHERE status IN ({placeholders})
                ORDER BY scheduled_funding_at, venue
                """,
                sorted(statuses),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output

    def apply_settlement_boundary(
        self,
        *,
        position_id: str,
        cycle_id: str,
        expected_obligation_count: int,
        reconciliation_rows: list[dict[str, Any]],
        boundary_evidence: dict[str, Any],
        position_config_update: dict[str, Any],
        now: datetime,
        position_state: str = "SETTLEMENT_CROSSED",
        fault_after: str | None = None,
    ) -> dict[str, Any]:
        """Persist obligations, cycle boundary state, and position counter atomically."""
        if expected_obligation_count <= 0 or not reconciliation_rows:
            raise ValueError("settlement boundary requires non-empty expected obligations")
        now_iso = now.astimezone(UTC).replace(microsecond=0).isoformat()
        evidence_json = json.dumps(boundary_evidence, sort_keys=True)
        with self.connect() as connection:
            inserted = 0
            for index, row in enumerate(reconciliation_rows, start=1):
                reconciliation_id = str(
                    row.get("reconciliation_id")
                    or ":".join(
                        [
                            str(row["position_id"]),
                            str(row["venue"]),
                            str(row["scheduled_funding_at"]),
                        ]
                    )
                )
                row_evidence_json = json.dumps(
                    row.get("evidence", row.get("evidence_json", {})),
                    sort_keys=True,
                )
                cursor = connection.execute(
                    """
                    INSERT INTO funding_settlement_reconciliations (
                        reconciliation_id,
                        position_id,
                        cycle_id,
                        venue,
                        symbol,
                        side,
                        scheduled_funding_at,
                        status,
                        confirmed_funding_rate,
                        settlement_mark_price,
                        funding_pnl,
                        rate_status,
                        mark_status,
                        evidence_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(position_id, venue, scheduled_funding_at) DO UPDATE SET
                        cycle_id = excluded.cycle_id,
                        symbol = excluded.symbol,
                        side = excluded.side,
                        status = excluded.status,
                        confirmed_funding_rate = excluded.confirmed_funding_rate,
                        settlement_mark_price = excluded.settlement_mark_price,
                        funding_pnl = excluded.funding_pnl,
                        rate_status = excluded.rate_status,
                        mark_status = excluded.mark_status,
                        evidence_json = excluded.evidence_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        reconciliation_id,
                        row["position_id"],
                        row.get("cycle_id"),
                        row["venue"],
                        row["symbol"],
                        row["side"],
                        row["scheduled_funding_at"],
                        row.get("status", "PENDING"),
                        row.get("confirmed_funding_rate"),
                        row.get("settlement_mark_price"),
                        row.get("funding_pnl"),
                        row.get("rate_status"),
                        row.get("mark_status"),
                        row_evidence_json,
                        row.get("created_at", now_iso),
                        now_iso,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                if fault_after == "first_obligation" and index == 1:
                    raise RuntimeError("fault_after_first_obligation")
            if fault_after == "all_obligations":
                raise RuntimeError("fault_after_all_obligations")
            actual_obligations = connection.execute(
                """
                SELECT COUNT(*)
                FROM funding_settlement_reconciliations
                WHERE position_id = ? AND cycle_id = ?
                """,
                (position_id, cycle_id),
            ).fetchone()[0]
            if int(actual_obligations) != int(expected_obligation_count):
                raise ValueError(
                    f"settlement obligation count mismatch {actual_obligations}!={expected_obligation_count}"
                )
            connection.execute(
                """
                UPDATE funding_capture_cycles
                SET state = 'SETTLEMENT_CROSSED',
                    settlement_crossed_at = COALESCE(settlement_crossed_at, ?),
                    boundary_evidence_json = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                (now_iso, evidence_json, now_iso, cycle_id),
            )
            if fault_after == "cycle_state":
                raise RuntimeError("fault_after_cycle_state")
            if fault_after == "before_position_counter":
                raise RuntimeError("fault_after_before_position_counter")
            row = connection.execute(
                """
                SELECT config_json
                FROM funding_capture_positions
                WHERE position_id = ?
                """,
                (position_id,),
            ).fetchone()
            current_config = json.loads(row["config_json"] or "{}") if row else {}
            current_config.update(position_config_update)
            captured_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM funding_capture_cycles
                WHERE position_id = ?
                  AND (
                    settlement_crossed_at IS NOT NULL
                    OR state IN (
                        'SETTLEMENT_CROSSED',
                        'PUBLIC_RATE_CONFIRMED',
                        'RATE_AND_MARK_RECONCILED',
                        'RECONCILED',
                        'UNRECONCILED'
                    )
                  )
                """,
                (position_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE funding_capture_positions
                SET state = ?,
                    config_json = ?,
                    settlements_captured_count = MAX(settlements_captured_count, ?),
                    updated_at = ?
                WHERE position_id = ?
                """,
                (
                    position_state,
                    json.dumps(current_config, sort_keys=True),
                    int(captured_count),
                    now_iso,
                    position_id,
                ),
            )
            if fault_after == "position_counter":
                raise RuntimeError("fault_after_position_counter")
        return {
            "position_id": position_id,
            "cycle_id": cycle_id,
            "expected_obligation_count": int(expected_obligation_count),
            "actual_obligation_count": int(expected_obligation_count),
            "inserted_obligation_count": inserted,
            "settlements_captured_count": int(captured_count),
        }

    def activate_funding_capture_hold_cycle(
        self,
        *,
        cycle_row: dict[str, Any],
        active_config: dict[str, Any],
        now: datetime,
        paper_net_pnl_estimated: float | None = None,
    ) -> str:
        """Atomically create the next active HOLD cycle and switch position state."""
        now_iso = now.astimezone(UTC).replace(microsecond=0).isoformat()
        position_id = str(cycle_row["position_id"])
        cycle_id = str(
            cycle_row.get("cycle_id")
            or f"{position_id}:{int(cycle_row['cycle_number'])}"
        )
        active_plan_json = json.dumps(
            cycle_row.get("active_plan", cycle_row.get("active_plan_json", {})),
            sort_keys=True,
        )
        boundary_evidence_json = json.dumps(
            cycle_row.get("boundary_evidence", cycle_row.get("boundary_evidence_json", {})),
            sort_keys=True,
        )
        assignments = [
            "state = 'HOLDING_NEXT_CYCLE'",
            "config_json = ?",
            "updated_at = ?",
        ]
        parameters: list[Any] = [json.dumps(active_config, sort_keys=True), now_iso]
        if paper_net_pnl_estimated is not None:
            assignments.append("paper_net_pnl_estimated = ?")
            parameters.append(float(paper_net_pnl_estimated))
        parameters.append(position_id)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_capture_cycles (
                    cycle_id,
                    position_id,
                    cycle_number,
                    plan_generation,
                    scheduled_funding_at,
                    long_next_funding_rate_at_decision,
                    short_next_funding_rate_at_decision,
                    conservative_funding_gross,
                    conservative_funding_edge_bps,
                    hold_basis_reserve_bps,
                    hold_legging_reserve_bps,
                    hold_time_reserve_bps,
                    hold_liquidity_reserve_bps,
                    incremental_hold_cost,
                    incremental_hold_net_pnl,
                    hold_cost_coverage_ratio,
                    paper_net_if_exit_at_decision,
                    decision,
                    decision_reason,
                    state,
                    settlement_crossed_at,
                    reconciliation_status,
                    reconciled_funding_pnl,
                    active_plan_json,
                    boundary_evidence_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id, cycle_number) DO UPDATE SET
                    plan_generation = excluded.plan_generation,
                    scheduled_funding_at = excluded.scheduled_funding_at,
                    long_next_funding_rate_at_decision = excluded.long_next_funding_rate_at_decision,
                    short_next_funding_rate_at_decision = excluded.short_next_funding_rate_at_decision,
                    conservative_funding_gross = excluded.conservative_funding_gross,
                    conservative_funding_edge_bps = excluded.conservative_funding_edge_bps,
                    hold_basis_reserve_bps = excluded.hold_basis_reserve_bps,
                    hold_legging_reserve_bps = excluded.hold_legging_reserve_bps,
                    hold_time_reserve_bps = excluded.hold_time_reserve_bps,
                    hold_liquidity_reserve_bps = excluded.hold_liquidity_reserve_bps,
                    incremental_hold_cost = excluded.incremental_hold_cost,
                    incremental_hold_net_pnl = excluded.incremental_hold_net_pnl,
                    hold_cost_coverage_ratio = excluded.hold_cost_coverage_ratio,
                    paper_net_if_exit_at_decision = excluded.paper_net_if_exit_at_decision,
                    decision = excluded.decision,
                    decision_reason = excluded.decision_reason,
                    state = excluded.state,
                    settlement_crossed_at = excluded.settlement_crossed_at,
                    reconciliation_status = excluded.reconciliation_status,
                    reconciled_funding_pnl = excluded.reconciled_funding_pnl,
                    active_plan_json = CASE
                        WHEN excluded.active_plan_json != '{}' THEN excluded.active_plan_json
                        ELSE funding_capture_cycles.active_plan_json
                    END,
                    boundary_evidence_json = CASE
                        WHEN excluded.boundary_evidence_json != '{}' THEN excluded.boundary_evidence_json
                        ELSE funding_capture_cycles.boundary_evidence_json
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle_id,
                    position_id,
                    int(cycle_row["cycle_number"]),
                    int(cycle_row.get("plan_generation") or cycle_row.get("cycle_number") or 0),
                    cycle_row["scheduled_funding_at"],
                    cycle_row.get("long_next_funding_rate_at_decision"),
                    cycle_row.get("short_next_funding_rate_at_decision"),
                    cycle_row.get("conservative_funding_gross"),
                    cycle_row.get("conservative_funding_edge_bps"),
                    cycle_row.get("hold_basis_reserve_bps"),
                    cycle_row.get("hold_legging_reserve_bps"),
                    cycle_row.get("hold_time_reserve_bps"),
                    cycle_row.get("hold_liquidity_reserve_bps"),
                    cycle_row.get("incremental_hold_cost"),
                    cycle_row.get("incremental_hold_net_pnl"),
                    cycle_row.get("hold_cost_coverage_ratio"),
                    cycle_row.get("paper_net_if_exit_at_decision"),
                    cycle_row.get("decision", "HOLD"),
                    cycle_row.get("decision_reason", "next_cycle_underwriting_passed"),
                    cycle_row.get("state", "HOLDING_NEXT_CYCLE"),
                    cycle_row.get("settlement_crossed_at"),
                    cycle_row.get("reconciliation_status", "PENDING"),
                    cycle_row.get("reconciled_funding_pnl"),
                    active_plan_json,
                    boundary_evidence_json,
                    cycle_row.get("created_at", now_iso),
                    now_iso,
                ),
            )
            connection.execute(
                f"""
                UPDATE funding_capture_positions
                SET {", ".join(assignments)}
                WHERE position_id = ?
                """,
                parameters,
            )
        return cycle_id

    def mark_settlement_plan_mismatch(
        self,
        *,
        position_id: str,
        cycle_id: str,
        evidence: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        now_iso = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
        evidence_json = json.dumps(evidence, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_capture_cycles
                SET state = 'SETTLEMENT_PLAN_MISMATCH',
                    settlement_crossed_at = NULL,
                    boundary_evidence_json = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                (evidence_json, now_iso, cycle_id),
            )
            row = connection.execute(
                """
                SELECT config_json
                FROM funding_capture_positions
                WHERE position_id = ?
                """,
                (position_id,),
            ).fetchone()
            config = json.loads(row["config_json"] or "{}") if row else {}
            diagnostics = list(config.get("settlement_plan_mismatches") or [])
            diagnostics.append(evidence)
            config["settlement_plan_mismatches"] = diagnostics[-10:]
            config["lifecycle_state"] = "SETTLEMENT_PLAN_MISMATCH"
            config["blocker"] = "settlement_plan_event_mismatch"
            connection.execute(
                """
                UPDATE funding_capture_positions
                SET state = 'SETTLEMENT_PLAN_MISMATCH',
                    config_json = ?,
                    updated_at = ?
                WHERE position_id = ?
                """,
                (json.dumps(config, sort_keys=True), now_iso, position_id),
            )

    def repair_funding_capture_boundary_consistency(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Repair legacy boundary partial writes without creating synthetic funding."""
        now_iso = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
        repaired_counts = 0
        completed_cycles = 0
        mismatches = 0
        with self.connect() as connection:
            positions = connection.execute(
                "SELECT position_id, state, closed_at, config_json FROM funding_capture_positions"
            ).fetchall()
            for pos in positions:
                position_id = str(pos["position_id"])
                position_state = str(pos["state"] or "")
                position_closed = pos["closed_at"] is not None
                try:
                    position_config = json.loads(pos["config_json"] or "{}")
                except (TypeError, ValueError):
                    position_config = {}
                cycles = connection.execute(
                    """
                    SELECT *
                    FROM funding_capture_cycles
                    WHERE position_id = ?
                    ORDER BY cycle_number
                    """,
                    (position_id,),
                ).fetchall()
                for cycle in cycles:
                    cycle_id = str(cycle["cycle_id"])
                    state = str(cycle["state"] or "")
                    obligation_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM funding_settlement_reconciliations
                            WHERE position_id = ? AND cycle_id = ?
                            """,
                            (position_id, cycle_id),
                        ).fetchone()[0]
                    )
                    expected = 0
                    try:
                        plan = json.loads(cycle["active_plan_json"] or "{}")
                    except (TypeError, ValueError):
                        plan = {}
                    expected = int(plan.get("expected_event_count") or 0)
                    crossed_like = (
                        cycle["settlement_crossed_at"] is not None
                        or state
                        in {
                            "SETTLEMENT_CROSSED",
                            "PUBLIC_RATE_CONFIRMED",
                            "RATE_AND_MARK_RECONCILED",
                            "RECONCILED",
                            "UNRECONCILED",
                        }
                    )
                    if crossed_like and obligation_count == 0:
                        evidence = {
                            "repair": "zero_obligation_legacy_inconsistency",
                            "cycle_id": cycle_id,
                            "position_id": position_id,
                            "active_plan_generation": cycle["plan_generation"],
                            "expected_event_count": expected,
                            "blocker": "settlement_plan_event_mismatch",
                            "requires_review": True,
                        }
                        connection.execute(
                            """
                            UPDATE funding_capture_cycles
                            SET state = 'SETTLEMENT_PLAN_MISMATCH',
                                settlement_crossed_at = NULL,
                                boundary_evidence_json = ?,
                                updated_at = ?
                            WHERE cycle_id = ?
                            """,
                            (json.dumps(evidence, sort_keys=True), now_iso, cycle_id),
                        )
                        mismatches += 1
                        if (
                            not position_closed
                            and position_state not in {
                                "FAILED",
                                "REJECTED_AFTER_SUBMISSION",
                                "CLOSED_PENDING_RECONCILIATION",
                                "CLOSED_REQUIRES_REVIEW",
                                "RECONCILED",
                                "UNRECONCILED",
                            }
                        ):
                            diagnostics = list(position_config.get("settlement_plan_mismatches") or [])
                            diagnostics.append(evidence)
                            position_config["settlement_plan_mismatches"] = diagnostics[-10:]
                            position_config["lifecycle_state"] = "SETTLEMENT_PLAN_MISMATCH"
                            position_config["blocker"] = "settlement_plan_event_mismatch"
                            position_config["requires_review"] = True
                            connection.execute(
                                """
                                UPDATE funding_capture_positions
                                SET state = 'SETTLEMENT_PLAN_MISMATCH',
                                    config_json = ?,
                                    updated_at = ?
                                WHERE position_id = ?
                                """,
                                (
                                    json.dumps(position_config, sort_keys=True),
                                    now_iso,
                                    position_id,
                                ),
                            )
                            position_state = "SETTLEMENT_PLAN_MISMATCH"
                        continue
                    if (
                        obligation_count > 0
                        and not crossed_like
                        and expected > 0
                        and obligation_count == expected
                    ):
                        connection.execute(
                            """
                            UPDATE funding_capture_cycles
                            SET state = 'SETTLEMENT_CROSSED',
                                settlement_crossed_at = COALESCE(settlement_crossed_at, ?),
                                updated_at = ?
                            WHERE cycle_id = ?
                            """,
                            (now_iso, now_iso, cycle_id),
                        )
                        completed_cycles += 1
                captured_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM funding_capture_cycles
                        WHERE position_id = ?
                          AND (
                            settlement_crossed_at IS NOT NULL
                            OR state IN (
                                'SETTLEMENT_CROSSED',
                                'PUBLIC_RATE_CONFIRMED',
                                'RATE_AND_MARK_RECONCILED',
                                'RECONCILED',
                                'UNRECONCILED'
                            )
                          )
                        """,
                        (position_id,),
                    ).fetchone()[0]
                )
                current_count = int(
                    connection.execute(
                        """
                        SELECT settlements_captured_count
                        FROM funding_capture_positions
                        WHERE position_id = ?
                        """,
                        (position_id,),
                    ).fetchone()[0]
                )
                if captured_count != current_count:
                    connection.execute(
                        """
                        UPDATE funding_capture_positions
                        SET settlements_captured_count = ?,
                            updated_at = ?
                        WHERE position_id = ?
                        """,
                        (captured_count, now_iso, position_id),
                    )
                    repaired_counts += 1
                mismatch_cycle = connection.execute(
                    """
                    SELECT cycle_id, boundary_evidence_json
                    FROM funding_capture_cycles
                    WHERE position_id = ?
                      AND state = 'SETTLEMENT_PLAN_MISMATCH'
                    ORDER BY cycle_number
                    LIMIT 1
                    """,
                    (position_id,),
                ).fetchone()
                if (
                    mismatch_cycle is not None
                    and not position_closed
                    and position_state not in {
                        "SETTLEMENT_PLAN_MISMATCH",
                        "FAILED",
                        "REJECTED_AFTER_SUBMISSION",
                        "CLOSED_PENDING_RECONCILIATION",
                        "CLOSED_REQUIRES_REVIEW",
                        "RECONCILED",
                        "UNRECONCILED",
                    }
                ):
                    try:
                        evidence = json.loads(mismatch_cycle["boundary_evidence_json"] or "{}")
                    except (TypeError, ValueError):
                        evidence = {}
                    evidence.setdefault("cycle_id", mismatch_cycle["cycle_id"])
                    evidence.setdefault("position_id", position_id)
                    evidence.setdefault("blocker", "settlement_plan_event_mismatch")
                    diagnostics = list(position_config.get("settlement_plan_mismatches") or [])
                    diagnostics.append(evidence)
                    position_config["settlement_plan_mismatches"] = diagnostics[-10:]
                    position_config["lifecycle_state"] = "SETTLEMENT_PLAN_MISMATCH"
                    position_config["blocker"] = "settlement_plan_event_mismatch"
                    position_config["requires_review"] = True
                    connection.execute(
                        """
                        UPDATE funding_capture_positions
                        SET state = 'SETTLEMENT_PLAN_MISMATCH',
                            config_json = ?,
                            updated_at = ?
                        WHERE position_id = ?
                        """,
                        (json.dumps(position_config, sort_keys=True), now_iso, position_id),
                    )
                    mismatches += 1
        return {
            "position_counters_repaired": repaired_counts,
            "cycles_completed_from_obligations": completed_cycles,
            "zero_obligation_mismatches": mismatches,
        }

    def funding_capture_cycles_for_position(
        self,
        position_id: str,
    ) -> list[dict[str, Any]]:
        """Return all capture cycles for a position, ordered by cycle_number."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM funding_capture_cycles
                WHERE position_id = ?
                ORDER BY cycle_number
                """,
                (position_id,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["active_plan"] = json.loads(item.pop("active_plan_json") or "{}")
            item["boundary_evidence"] = json.loads(item.pop("boundary_evidence_json") or "{}")
            output.append(item)
        return output

    def reconciled_funding_capture_hold_cycles(
        self,
        *,
        canonical_asset: str,
        long_venue: str,
        short_venue: str,
        collateral_asset: str,
        wait_bucket: str,
        since: str,
        exclude_position_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return local reconciled hold-cycle history for reliability scoring.

        This query intentionally uses only our compact reconciled cycle records,
        not raw exchange funding history.
        """
        query = """
            SELECT c.*, p.canonical_asset, p.long_venue, p.short_venue,
                   p.config_json
            FROM funding_capture_cycles c
            JOIN funding_capture_positions p
              ON p.position_id = c.position_id
            WHERE p.canonical_asset = ?
              AND p.long_venue = ?
              AND p.short_venue = ?
              AND c.cycle_number > 1
              AND c.decision = 'HOLD'
              AND c.state = 'RECONCILED'
              AND c.reconciliation_status = 'RATE_AND_MARK_RECONCILED'
              AND c.conservative_funding_gross IS NOT NULL
              AND c.conservative_funding_gross > 0
              AND c.reconciled_funding_pnl IS NOT NULL
              AND c.created_at >= ?
        """
        params: list[Any] = [canonical_asset, long_venue, short_venue, since]
        if exclude_position_id is not None:
            query += " AND c.position_id != ?"
            params.append(exclude_position_id)
        query += " ORDER BY c.created_at DESC LIMIT ?"
        params.append(max(1, int(limit) * 5))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            config = json.loads(item.pop("config_json") or "{}")
            entry_legs = config.get("entry_legs") or []
            collateral_values = {
                str(leg.get("collateral_asset") or "").upper()
                for leg in entry_legs
                if leg.get("collateral_asset")
            }
            row_collateral = (
                str(config.get("collateral_asset") or "").upper()
                or (next(iter(collateral_values)) if len(collateral_values) == 1 else "")
                or "UNKNOWN"
            )
            if row_collateral != str(collateral_asset or "UNKNOWN").upper():
                continue
            scheduled_at = _parse_iso_datetime(item.get("scheduled_funding_at"))
            created_at = _parse_iso_datetime(item.get("created_at"))
            if scheduled_at is None or created_at is None:
                continue
            wait_seconds = (scheduled_at - created_at).total_seconds()
            if _funding_wait_bucket(wait_seconds) != wait_bucket:
                continue
            item["wait_seconds"] = wait_seconds
            item["wait_bucket"] = wait_bucket
            item["predicted_gross"] = item.get("conservative_funding_gross")
            item["realized_gross"] = item.get("reconciled_funding_pnl")
            output.append(item)
            if len(output) >= int(limit):
                break
        return output

    def funding_capture_position_by_id(
        self,
        position_id: str,
    ) -> dict[str, Any] | None:
        """Return a single capture position by ID with its latest cycle."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, c.cycle_id AS current_cycle_id,
                       c.cycle_number AS current_cycle_number,
                       c.scheduled_funding_at AS current_cycle_scheduled_funding_at,
                       c.state AS current_cycle_state,
                       c.conservative_funding_gross AS current_cycle_conservative_funding_gross,
                       c.conservative_funding_edge_bps AS current_cycle_conservative_funding_edge_bps
                FROM funding_capture_positions p
                LEFT JOIN funding_capture_cycles c
                  ON c.position_id = p.position_id
                 AND c.cycle_number = (
                    SELECT MAX(c2.cycle_number)
                    FROM funding_capture_cycles c2
                    WHERE c2.position_id = p.position_id
                 )
                WHERE p.position_id = ?
                """,
                (position_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json") or "{}")
        return item

    def funding_capture_position_rows(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        state_filter = ""
        if states:
            placeholders = ",".join("?" for _ in states)
            state_filter = f"WHERE p.state IN ({placeholders})"
            parameters.extend(sorted(states))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, c.cycle_id AS current_cycle_id,
                       c.cycle_number AS current_cycle_number,
                       c.scheduled_funding_at AS current_cycle_scheduled_funding_at,
                       c.state AS current_cycle_state,
                       c.conservative_funding_gross AS current_cycle_conservative_funding_gross,
                       c.conservative_funding_edge_bps AS current_cycle_conservative_funding_edge_bps
                FROM funding_capture_positions p
                LEFT JOIN funding_capture_cycles c
                  ON c.position_id = p.position_id
                 AND c.cycle_number = (
                    SELECT MAX(c2.cycle_number)
                    FROM funding_capture_cycles c2
                    WHERE c2.position_id = p.position_id
                 )
                {state_filter}
                ORDER BY p.opened_at DESC
                """,
                parameters,
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json") or "{}")
            output.append(item)
        return output

    def funding_capture_open_positions(self) -> list[dict[str, Any]]:
        return self.funding_capture_position_rows(
            states={
                "OPEN",
                "SETTLEMENT_CROSSED",
                "POST_SETTLEMENT_EVALUATION",
                "HOLDING_NEXT_CYCLE",
                "EXIT_SCHEDULED",
                "EXIT_SUBMITTED",
                "PARTIALLY_CLOSED",
                "EMERGENCY_UNWIND",
                "SETTLEMENT_PLAN_MISMATCH",
            }
        )

    def funding_capture_open_position_by_route_key(
        self,
        route_key: str,
    ) -> dict[str, Any] | None:
        for row in self.funding_capture_open_positions():
            if str((row.get("config") or {}).get("route_key") or "") == str(route_key):
                return row
        return None

    def update_funding_capture_position_state(
        self,
        position_id: str,
        state: str,
        now: datetime | None = None,
        *,
        settlements_captured_count: int | None = None,
        closed_at: str | None = None,
        paper_close_fees: float | None = None,
        paper_emergency_unwind_cost: float | None = None,
        paper_net_pnl_estimated: float | None = None,
        paper_net_pnl_reconciled: float | None = None,
    ) -> None:
        updated_at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[Any] = [state, updated_at]
        optional_fields = {
            "settlements_captured_count": settlements_captured_count,
            "closed_at": closed_at,
            "paper_close_fees": paper_close_fees,
            "paper_emergency_unwind_cost": paper_emergency_unwind_cost,
            "paper_net_pnl_estimated": paper_net_pnl_estimated,
            "paper_net_pnl_reconciled": paper_net_pnl_reconciled,
        }
        for column, value in optional_fields.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(position_id)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE funding_capture_positions
                SET {", ".join(assignments)}
                WHERE position_id = ?
                """,
                parameters,
            )

    def update_funding_capture_position_config(
        self,
        position_id: str,
        config: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        updated_at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
        config_json = json.dumps(config, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_capture_positions
                SET config_json = ?, updated_at = ?
                WHERE position_id = ?
                """,
                (config_json, updated_at, str(position_id)),
            )

    def update_funding_capture_cycle_state(
        self,
        cycle_id: str,
        state: str,
        now: datetime | None = None,
        *,
        settlement_crossed_at: str | None = None,
        reconciliation_status: str | None = None,
        reconciled_funding_pnl: float | None = None,
    ) -> None:
        updated_at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[Any] = [state, updated_at]
        optional_fields = {
            "settlement_crossed_at": settlement_crossed_at or (updated_at if state == "SETTLEMENT_CROSSED" else None),
            "reconciliation_status": reconciliation_status,
            "reconciled_funding_pnl": reconciled_funding_pnl,
        }
        for column, value in optional_fields.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(cycle_id)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE funding_capture_cycles
                SET {", ".join(assignments)}
                WHERE cycle_id = ?
                """,
                parameters,
            )

    def upsert_funding_paper_order(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        order_id = str(
            row.get("paper_order_id")
            or f"{row['position_id']}:{row.get('cycle_id', '')}:{row['leg_side']}:{row['order_intent']}"
        )
        payload_json = json.dumps(row.get("payload", row.get("payload_json", {})), sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_paper_orders (
                    paper_order_id, position_id, cycle_id, leg_side,
                    order_intent, venue, symbol, decision_at,
                    submitted_at, acknowledged_at, filled_at,
                    filled_quantity, average_fill_price, fee, state,
                    payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_order_id) DO UPDATE SET
                    submitted_at = excluded.submitted_at,
                    acknowledged_at = excluded.acknowledged_at,
                    filled_at = excluded.filled_at,
                    filled_quantity = excluded.filled_quantity,
                    average_fill_price = excluded.average_fill_price,
                    fee = excluded.fee,
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    row["position_id"],
                    row.get("cycle_id"),
                    row["leg_side"],
                    row["order_intent"],
                    row["venue"],
                    row["symbol"],
                    row["decision_at"],
                    row.get("submitted_at"),
                    row.get("acknowledged_at"),
                    row.get("filled_at"),
                    float(row.get("filled_quantity", 0.0)),
                    row.get("average_fill_price"),
                    float(row.get("fee", 0.0)),
                    row.get("state", "SUBMITTED"),
                    payload_json,
                    row.get("created_at", now),
                    now,
                ),
            )
        return order_id

    def funding_paper_order_rows(
        self,
        position_id: str,
        *,
        cycle_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [position_id]
        cycle_filter = ""
        if cycle_id is not None:
            cycle_filter = "AND cycle_id = ?"
            parameters.append(cycle_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM funding_paper_orders
                WHERE position_id = ?
                {cycle_filter}
                ORDER BY created_at
                """,
                parameters,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            output.append(item)
        return output

    def upsert_funding_capture_observation(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        observation_id = str(
            row.get("observation_id")
            or f"{row['position_id']}:{row.get('cycle_id', '')}:{row['observed_at']}"
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_capture_observations (
                    observation_id, position_id, cycle_id, phase,
                    observed_at, long_response_received_at,
                    short_response_received_at, cross_venue_skew_ms,
                    long_mark, short_mark, long_index, short_index,
                    long_next_funding_at, short_next_funding_at,
                    long_next_funding_rate, short_next_funding_rate,
                    gross_funding_pnl,
                    long_open_vwap, short_open_vwap,
                    long_close_vwap, short_close_vwap,
                    current_exit_spread, total_basis_deterioration_bps,
                    cycle_basis_deterioration_bps, paper_net_if_exit_now,
                    snapshot_valid, invalid_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    phase = excluded.phase,
                    long_mark = excluded.long_mark,
                    short_mark = excluded.short_mark,
                    gross_funding_pnl = excluded.gross_funding_pnl,
                    paper_net_if_exit_now = excluded.paper_net_if_exit_now,
                    snapshot_valid = excluded.snapshot_valid,
                    invalid_reason = excluded.invalid_reason
                """,
                (
                    observation_id,
                    row["position_id"],
                    row.get("cycle_id"),
                    row.get("phase", "entry"),
                    row["observed_at"],
                    row.get("long_response_received_at"),
                    row.get("short_response_received_at"),
                    row.get("cross_venue_skew_ms"),
                    row.get("long_mark"),
                    row.get("short_mark"),
                    row.get("long_index"),
                    row.get("short_index"),
                    row.get("long_next_funding_at"),
                    row.get("short_next_funding_at"),
                    row.get("long_next_funding_rate"),
                    row.get("short_next_funding_rate"),
                    row.get("gross_funding_pnl"),
                    row.get("long_open_vwap"),
                    row.get("short_open_vwap"),
                    row.get("long_close_vwap"),
                    row.get("short_close_vwap"),
                    row.get("current_exit_spread"),
                    row.get("total_basis_deterioration_bps"),
                    row.get("cycle_basis_deterioration_bps"),
                    row.get("paper_net_if_exit_now"),
                    int(bool(row.get("snapshot_valid", False))),
                    row.get("invalid_reason"),
                ),
            )
        return observation_id

    def upsert_paper_event_ledger(self, row: dict[str, Any]) -> str | None:
        """Idempotent insert. Returns event_key on first insert, None on duplicate."""
        now = utc_now_iso()
        event_key = str(row["event_key"])
        payload_json = json.dumps(row.get("payload", row.get("payload_json", {})), sort_keys=True)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_event_ledger (
                    event_key, position_id, cycle_id, venue,
                    event_type, cash_delta, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    row.get("position_id"),
                    row.get("cycle_id"),
                    row.get("venue"),
                    row["event_type"],
                    float(row.get("cash_delta", 0.0)),
                    payload_json,
                    row.get("created_at", now),
                ),
            )
            if cursor.rowcount == 0:
                return None
        return event_key

    def apply_paper_cash_event(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert a cash ledger row and apply its account effect atomically."""
        now = utc_now_iso()
        event_key = str(row["event_key"])
        venue = str(row.get("venue") or "")
        cash_delta = float(row.get("cash_delta", 0.0))
        if cash_delta != 0.0 and not venue:
            raise ValueError(f"cash-affecting paper ledger entry requires venue: {event_key}")
        payload_json = json.dumps(row.get("payload", row.get("payload_json", {})), sort_keys=True)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_event_ledger (
                    event_key, position_id, cycle_id, venue,
                    event_type, cash_delta, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    row.get("position_id"),
                    row.get("cycle_id"),
                    row.get("venue"),
                    row["event_type"],
                    cash_delta,
                    payload_json,
                    row.get("created_at", now),
                ),
            )
            applied = cursor.rowcount == 1
            if venue:
                connection.execute(
                    """
                    INSERT INTO funding_paper_accounts (
                        venue, starting_balance, cash_balance, reserved_margin,
                        realized_pnl, updated_at
                    )
                    VALUES (?, 0, 0, 0, 0, ?)
                    ON CONFLICT(venue) DO NOTHING
                    """,
                    (venue, now),
                )
            if applied and venue and cash_delta != 0.0:
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (cash_delta, cash_delta, now, venue),
                )
        return {"event_key": event_key, "applied": applied}

    def apply_paper_reserve_event(
        self,
        row: dict[str, Any],
        *,
        reserve_delta: float,
    ) -> dict[str, Any]:
        """Insert a collateral ledger row and apply reserved_margin atomically."""
        now = utc_now_iso()
        event_key = str(row["event_key"])
        venue = str(row.get("venue") or "")
        if not venue:
            raise ValueError(f"collateral paper ledger entry requires venue: {event_key}")
        payload_json = json.dumps(row.get("payload", row.get("payload_json", {})), sort_keys=True)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_event_ledger (
                    event_key, position_id, cycle_id, venue,
                    event_type, cash_delta, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    row.get("position_id"),
                    row.get("cycle_id"),
                    row.get("venue"),
                    row["event_type"],
                    float(row.get("cash_delta", 0.0)),
                    payload_json,
                    row.get("created_at", now),
                ),
            )
            applied = cursor.rowcount == 1
            connection.execute(
                """
                INSERT INTO funding_paper_accounts (
                    venue, starting_balance, cash_balance, reserved_margin,
                    realized_pnl, updated_at
                )
                VALUES (?, 0, 0, 0, 0, ?)
                ON CONFLICT(venue) DO NOTHING
                """,
                (venue, now),
            )
            if applied and float(reserve_delta) != 0.0:
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET reserved_margin = MAX(0, reserved_margin + ?),
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (float(reserve_delta), now, venue),
                )
        return {"event_key": event_key, "applied": applied}

    def apply_reconciled_funding_effect(
        self,
        reconciliation_row: dict[str, Any],
        ledger_row: dict[str, Any],
        *,
        fault_after: str | None = None,
    ) -> dict[str, Any]:
        """Store a reconciled funding leg, ledger row, and cash effect together."""
        now = utc_now_iso()
        event_key = str(ledger_row["event_key"])
        venue = str(ledger_row.get("venue") or "")
        cash_delta = float(ledger_row.get("cash_delta") or 0.0)
        if not venue:
            raise ValueError(f"funding reconciliation ledger entry requires venue: {event_key}")
        evidence = dict(reconciliation_row.get("evidence") or {})
        evidence["financial_effect"] = {
            "event_key": event_key,
            "event_type": ledger_row.get("event_type"),
            "venue": venue,
            "cash_delta": cash_delta,
            "applied": True,
            "applied_at": now,
        }
        reconciliation_id = str(
            reconciliation_row.get("reconciliation_id")
            or ":".join(
                [
                    str(reconciliation_row["position_id"]),
                    venue,
                    str(reconciliation_row["scheduled_funding_at"]),
                ]
            )
        )
        recon_evidence_json = json.dumps(evidence, sort_keys=True)
        ledger_payload_json = json.dumps(
            ledger_row.get("payload", ledger_row.get("payload_json", {})),
            sort_keys=True,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_settlement_reconciliations (
                    reconciliation_id,
                    position_id,
                    cycle_id,
                    venue,
                    symbol,
                    side,
                    scheduled_funding_at,
                    status,
                    confirmed_funding_rate,
                    settlement_mark_price,
                    funding_pnl,
                    rate_status,
                    mark_status,
                    evidence_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id, venue, scheduled_funding_at) DO UPDATE SET
                    cycle_id = excluded.cycle_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = excluded.status,
                    confirmed_funding_rate = excluded.confirmed_funding_rate,
                    settlement_mark_price = excluded.settlement_mark_price,
                    funding_pnl = excluded.funding_pnl,
                    rate_status = excluded.rate_status,
                    mark_status = excluded.mark_status,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    reconciliation_id,
                    reconciliation_row["position_id"],
                    reconciliation_row.get("cycle_id"),
                    venue,
                    reconciliation_row["symbol"],
                    reconciliation_row["side"],
                    reconciliation_row["scheduled_funding_at"],
                    reconciliation_row.get("status", "RATE_AND_MARK_RECONCILED"),
                    reconciliation_row.get("confirmed_funding_rate"),
                    reconciliation_row.get("settlement_mark_price"),
                    reconciliation_row.get("funding_pnl"),
                    reconciliation_row.get("rate_status"),
                    reconciliation_row.get("mark_status"),
                    recon_evidence_json,
                    reconciliation_row.get("created_at", now),
                    now,
                ),
            )
            if fault_after == "reconciliation_row":
                raise RuntimeError("fault_after_reconciliation_row")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_event_ledger (
                    event_key, position_id, cycle_id, venue,
                    event_type, cash_delta, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    ledger_row.get("position_id"),
                    ledger_row.get("cycle_id"),
                    venue,
                    ledger_row["event_type"],
                    cash_delta,
                    ledger_payload_json,
                    ledger_row.get("created_at", now),
                ),
            )
            ledger_inserted = cursor.rowcount == 1
            if fault_after == "ledger_insert":
                raise RuntimeError("fault_after_ledger_insert")
            connection.execute(
                """
                INSERT INTO funding_paper_accounts (
                    venue, starting_balance, cash_balance, reserved_margin,
                    realized_pnl, updated_at
                )
                VALUES (?, 0, 0, 0, 0, ?)
                ON CONFLICT(venue) DO NOTHING
                """,
                (venue, now),
            )
            if ledger_inserted and cash_delta != 0.0:
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (cash_delta, cash_delta, now, venue),
                )
            if fault_after == "cash_update":
                raise RuntimeError("fault_after_cash_update")
        return {"event_key": event_key, "applied": ledger_inserted}

    def paper_event_ledger_rows(
        self,
        position_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if position_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM paper_event_ledger
                    WHERE position_id = ?
                    ORDER BY created_at
                    """,
                    (position_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM paper_event_ledger ORDER BY created_at"
                ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            output.append(item)
        return output

    def upsert_chains(
        self,
        connection: sqlite3.Connection,
        chains: Iterable[ChainConfig],
    ) -> None:
        rows = [
            (
                chain.chain_id,
                chain.name,
                chain.ecosystem,
                chain.explorer_name,
                chain.explorer_url,
                chain.api_env_var,
                chain.live_priority,
                int(chain.research_enabled),
            )
            for chain in chains
        ]
        connection.executemany(
            """
            INSERT INTO chains (
                chain_id,
                name,
                ecosystem,
                explorer_name,
                explorer_url,
                api_env_var,
                live_priority,
                research_enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id) DO UPDATE SET
                name = excluded.name,
                ecosystem = excluded.ecosystem,
                explorer_name = excluded.explorer_name,
                explorer_url = excluded.explorer_url,
                api_env_var = excluded.api_env_var,
                live_priority = excluded.live_priority,
                research_enabled = excluded.research_enabled
            """,
            rows,
        )

    def start_run(self, source: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ingestion_runs (source, started_at, status)
                VALUES (?, ?, ?)
                """,
                (source, utc_now_iso(), "running"),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        records_seen: int,
        records_written: int,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE ingestion_runs
                SET finished_at = ?,
                    status = ?,
                    records_seen = ?,
                    records_written = ?,
                    error = ?
                WHERE run_id = ?
                """,
                (utc_now_iso(), status, records_seen, records_written, error, run_id),
            )

    def upsert_binance_announcement(self, announcement: dict[str, Any]) -> None:
        with self.connect() as connection:
            raw = announcement["raw"]
            normalized = announcement["normalized"]
            connection.execute(
                """
                INSERT INTO raw_binance_announcements (
                    article_id,
                    article_code,
                    catalog_id,
                    title,
                    release_ts_ms,
                    release_at,
                    source_url,
                    fetched_at,
                    list_payload_json,
                    detail_payload_json,
                    body_text,
                    body_links_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_code) DO UPDATE SET
                    title = excluded.title,
                    release_ts_ms = excluded.release_ts_ms,
                    release_at = excluded.release_at,
                    source_url = excluded.source_url,
                    fetched_at = excluded.fetched_at,
                    list_payload_json = excluded.list_payload_json,
                    detail_payload_json = excluded.detail_payload_json,
                    body_text = excluded.body_text,
                    body_links_json = excluded.body_links_json
                """,
                (
                    raw["article_id"],
                    raw["article_code"],
                    raw["catalog_id"],
                    raw["title"],
                    raw["release_ts_ms"],
                    raw["release_at"],
                    raw["source_url"],
                    raw["fetched_at"],
                    json.dumps(raw["list_payload"], sort_keys=True),
                    json.dumps(raw["detail_payload"], sort_keys=True)
                    if raw.get("detail_payload")
                    else None,
                    raw.get("body_text"),
                    json.dumps(raw.get("body_links", []), sort_keys=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO binance_announcements (
                    announcement_id,
                    article_code,
                    title,
                    release_at,
                    source_url,
                    category,
                    is_spot_listing,
                    is_futures_listing,
                    is_alpha_or_airdrop,
                    requires_manual_review,
                    extracted_symbols_json,
                    trading_pairs_json,
                    contract_links_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_code) DO UPDATE SET
                    title = excluded.title,
                    release_at = excluded.release_at,
                    source_url = excluded.source_url,
                    category = excluded.category,
                    is_spot_listing = excluded.is_spot_listing,
                    is_futures_listing = excluded.is_futures_listing,
                    is_alpha_or_airdrop = excluded.is_alpha_or_airdrop,
                    requires_manual_review = excluded.requires_manual_review,
                    extracted_symbols_json = excluded.extracted_symbols_json,
                    trading_pairs_json = excluded.trading_pairs_json,
                    contract_links_json = excluded.contract_links_json,
                    updated_at = excluded.updated_at
                """,
                (
                    raw["article_id"],
                    raw["article_code"],
                    raw["title"],
                    raw["release_at"],
                    raw["source_url"],
                    normalized["category"],
                    int(normalized["is_spot_listing"]),
                    int(normalized["is_futures_listing"]),
                    int(normalized["is_alpha_or_airdrop"]),
                    int(normalized["requires_manual_review"]),
                    json.dumps(normalized["extracted_symbols"], sort_keys=True),
                    json.dumps(normalized["trading_pairs"], sort_keys=True),
                    json.dumps(normalized["contract_links"], sort_keys=True),
                    utc_now_iso(),
                ),
            )

    def list_binance_announcements(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT
                        release_at,
                        category,
                        title,
                        json_array_length(trading_pairs_json) AS pair_count,
                        json_array_length(contract_links_json) AS contract_link_count,
                        source_url
                    FROM binance_announcements
                    ORDER BY release_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def sync_token_registry_from_announcements(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    announcement_id,
                    title,
                    release_at,
                    source_url,
                    category,
                    is_spot_listing,
                    extracted_symbols_json,
                    trading_pairs_json,
                    contract_links_json
                FROM binance_announcements
                ORDER BY release_at ASC
                """
            ).fetchall()

            stats = {
                "announcements_seen": 0,
                "announcements_used": 0,
                "tokens_upserted": 0,
                "listing_events_upserted": 0,
                "contracts_upserted": 0,
                "announcements_skipped": 0,
                "backtest_targets_selected": 0,
            }
            for row in rows:
                stats["announcements_seen"] += 1
                used = self._sync_registry_row(connection, row)
                if used:
                    stats["announcements_used"] += 1
                    stats["tokens_upserted"] += used["tokens"]
                    stats["listing_events_upserted"] += used["events"]
                    stats["contracts_upserted"] += used["contracts"]
                else:
                    stats["announcements_skipped"] += 1
            stats["backtest_targets_selected"] = self._select_earliest_backtest_targets(
                connection
            )
            return stats

    def rebuild_token_registry_from_announcements(self) -> dict[str, int]:
        with self.connect() as connection:
            connection.execute("DELETE FROM token_contracts")
            connection.execute("DELETE FROM listing_events")
            connection.execute("DELETE FROM tokens")
        return self.sync_token_registry_from_announcements()

    def _sync_registry_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, int] | None:
        category = row["category"]
        if category not in {"spot", "alpha_or_airdrop", "product_bundle"}:
            return None

        symbols = [
            normalize_symbol(symbol)
            for symbol in json.loads(row["extracted_symbols_json"])
            if normalize_symbol(symbol)
        ]
        symbols = sorted(set(symbols))
        if not symbols:
            return None

        pairs = json.loads(row["trading_pairs_json"])
        contract_links = [
            link
            for link in json.loads(row["contract_links_json"])
            if link.get("chain_id") and link.get("address")
        ]
        configured_chain_ids = {
            chain["chain_id"] for chain in self.dashboard_chains()
        }
        contract_links = [
            link for link in contract_links if link["chain_id"] in configured_chain_ids
        ]
        now = utc_now_iso()
        stats = {"tokens": 0, "events": 0, "contracts": 0}
        mapping_status = registry_mapping_status(symbols, contract_links)
        confidence = registry_confidence(category, symbols, contract_links)
        is_backtest_target = int(is_initial_binance_listing_title(row["title"]))

        for symbol in symbols:
            token_id = self._upsert_token(
                connection=connection,
                symbol=symbol,
                first_seen_at=row["release_at"],
                source=row["source_url"],
                now=now,
            )
            stats["tokens"] += 1
            self._upsert_listing_event(
                connection=connection,
                announcement_id=row["announcement_id"],
                token_id=token_id,
                event_type=category,
                announced_at=row["release_at"],
                source_url=row["source_url"],
                trading_pairs=pairs_for_symbol(symbol, pairs),
                mapping_status=mapping_status,
                confidence_score=confidence,
                is_backtest_target=is_backtest_target,
                requires_manual_review=1,
                now=now,
            )
            stats["events"] += 1

            if len(symbols) == 1:
                for link in contract_links:
                    self._upsert_token_contract(
                        connection=connection,
                        token_id=token_id,
                        chain_id=link["chain_id"],
                        contract_address=link["address"],
                        explorer_url=link["href"],
                        first_seen_at=row["release_at"],
                        confidence_score=0.9,
                        mapping_status="single_symbol_contract_link",
                        now=now,
                    )
                    stats["contracts"] += 1

        return stats

    def _select_earliest_backtest_targets(
        self,
        connection: sqlite3.Connection,
    ) -> int:
        positive_token_ids = [
            int(row["token_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT token_id
                FROM listing_events
                WHERE is_backtest_target = 1
                """
            ).fetchall()
        ]
        if not positive_token_ids:
            connection.execute("UPDATE listing_events SET is_backtest_target = 0")
            return 0
        placeholders = ",".join("?" for _ in positive_token_ids)
        candidates = connection.execute(
            f"""
            SELECT listing_event_id, token_id
            FROM listing_events
            WHERE token_id IN ({placeholders})
              AND is_backtest_target = 1
            ORDER BY token_id, announced_at, listing_event_id
            """,
            positive_token_ids,
        ).fetchall()
        selected_ids = []
        seen_token_ids: set[int] = set()
        for row in candidates:
            token_id = int(row["token_id"])
            if token_id in seen_token_ids:
                continue
            seen_token_ids.add(token_id)
            selected_ids.append(int(row["listing_event_id"]))

        connection.execute("UPDATE listing_events SET is_backtest_target = 0")
        connection.executemany(
            "UPDATE listing_events SET is_backtest_target = 1 WHERE listing_event_id = ?",
            [(listing_event_id,) for listing_event_id in selected_ids],
        )
        return len(selected_ids)

    def _upsert_token(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        first_seen_at: str,
        source: str,
        now: str,
    ) -> int:
        connection.execute(
            """
            INSERT INTO tokens (symbol, first_seen_at, first_source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                first_seen_at = CASE
                    WHEN tokens.first_seen_at IS NULL THEN excluded.first_seen_at
                    WHEN excluded.first_seen_at < tokens.first_seen_at THEN excluded.first_seen_at
                    ELSE tokens.first_seen_at
                END,
                first_source = CASE
                    WHEN tokens.first_seen_at IS NULL THEN excluded.first_source
                    WHEN excluded.first_seen_at < tokens.first_seen_at THEN excluded.first_source
                    ELSE tokens.first_source
                END,
                updated_at = excluded.updated_at
            """,
            (symbol, first_seen_at, source, now, now),
        )
        row = connection.execute(
            "SELECT token_id FROM tokens WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return int(row["token_id"])

    def _upsert_listing_event(
        self,
        connection: sqlite3.Connection,
        announcement_id: int,
        token_id: int,
        event_type: str,
        announced_at: str,
        source_url: str,
        trading_pairs: list[str],
        mapping_status: str,
        confidence_score: float,
        is_backtest_target: int,
        requires_manual_review: int,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO listing_events (
                announcement_id,
                token_id,
                exchange,
                event_type,
                announced_at,
                source_url,
                trading_pairs_json,
                mapping_status,
                confidence_score,
                is_backtest_target,
                requires_manual_review,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'binance', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(announcement_id, token_id, event_type) DO UPDATE SET
                announced_at = excluded.announced_at,
                source_url = excluded.source_url,
                trading_pairs_json = excluded.trading_pairs_json,
                mapping_status = excluded.mapping_status,
                confidence_score = excluded.confidence_score,
                is_backtest_target = excluded.is_backtest_target,
                requires_manual_review = excluded.requires_manual_review,
                updated_at = excluded.updated_at
            """,
            (
                announcement_id,
                token_id,
                event_type,
                announced_at,
                source_url,
                json.dumps(trading_pairs, sort_keys=True),
                mapping_status,
                confidence_score,
                is_backtest_target,
                requires_manual_review,
                now,
                now,
            ),
        )

    def _upsert_token_contract(
        self,
        connection: sqlite3.Connection,
        token_id: int,
        chain_id: str,
        contract_address: str,
        explorer_url: str,
        first_seen_at: str,
        confidence_score: float,
        mapping_status: str,
        now: str,
        source: str = "binance_announcement",
    ) -> None:
        connection.execute(
            """
            INSERT INTO token_contracts (
                token_id,
                chain_id,
                contract_address,
                explorer_url,
                source,
                confidence_score,
                mapping_status,
                first_seen_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id, chain_id, contract_address) DO UPDATE SET
                explorer_url = excluded.explorer_url,
                source = CASE
                    WHEN token_contracts.source = 'binance_announcement'
                    THEN token_contracts.source
                    ELSE excluded.source
                END,
                confidence_score = excluded.confidence_score,
                mapping_status = excluded.mapping_status,
                first_seen_at = excluded.first_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                token_id,
                chain_id,
                contract_address.lower()
                if contract_address.startswith("0x")
                else contract_address,
                explorer_url,
                source,
                confidence_score,
                mapping_status,
                first_seen_at,
                now,
                now,
            ),
        )

    def registry_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            counts = {
                "tokens": connection.execute("SELECT COUNT(*) FROM tokens").fetchone()[0],
                "listing_events": connection.execute(
                    "SELECT COUNT(*) FROM listing_events"
                ).fetchone()[0],
                "backtest_targets": connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM listing_events
                    WHERE is_backtest_target = 1
                    """
                ).fetchone()[0],
                "token_contracts": connection.execute(
                    "SELECT COUNT(*) FROM token_contracts"
                ).fetchone()[0],
                "manual_review_events": connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM listing_events
                    WHERE requires_manual_review = 1
                    """
                ).fetchone()[0],
            }
            events_by_type = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT event_type, COUNT(*) AS count
                    FROM listing_events
                    GROUP BY event_type
                    ORDER BY count DESC, event_type
                    """
                ).fetchall()
            ]
            contracts_by_chain = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT chain_id, COUNT(*) AS count
                    FROM token_contracts
                    GROUP BY chain_id
                    ORDER BY count DESC, chain_id
                    """
                ).fetchall()
            ]
        return {
            **counts,
            "events_by_type": events_by_type,
            "contracts_by_chain": contracts_by_chain,
        }

    def registry_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    le.announced_at,
                    le.event_type,
                    le.is_backtest_target,
                    le.mapping_status,
                    le.confidence_score,
                    t.symbol,
                    le.trading_pairs_json,
                    le.source_url
                FROM listing_events le
                JOIN tokens t ON t.token_id = le.token_id
                ORDER BY le.announced_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        events = []
        for row in rows:
            item = dict(row)
            item["trading_pairs"] = json.loads(item.pop("trading_pairs_json"))
            item["is_backtest_target"] = bool(item["is_backtest_target"])
            events.append(item)
        return events

    def backtest_targets(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT
                listing_event_id,
                symbol,
                name,
                exchange,
                event_type,
                announced_at,
                source_url,
                trading_pairs_json,
                mapping_status,
                confidence_score,
                requires_manual_review
            FROM backtest_targets
            ORDER BY announced_at DESC
        """
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)

        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        targets = []
        for row in rows:
            item = dict(row)
            item["trading_pairs"] = json.loads(item.pop("trading_pairs_json"))
            item["requires_manual_review"] = bool(item["requires_manual_review"])
            targets.append(item)
        return targets

    def chain_backtest_targets(self, chain_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    le.listing_event_id,
                    t.symbol,
                    t.name,
                    le.announced_at,
                    le.source_url,
                    le.mapping_status,
                    le.confidence_score,
                    tc.chain_id,
                    tc.contract_address,
                    tc.explorer_url,
                    tc.source AS contract_source,
                    tc.mapping_status AS contract_mapping_status
                FROM listing_events le
                JOIN tokens t ON t.token_id = le.token_id
                JOIN token_contracts tc ON tc.token_id = t.token_id
                WHERE le.is_backtest_target = 1
                  AND tc.chain_id = ?
                  AND (
                      (
                          tc.chain_id IN ('base', 'bsc', 'ethereum')
                          AND tc.mapping_status = 'rpc_symbol_verified'
                          AND tc.source IN (
                              'binance_announcement',
                              'dune_preannouncement_contract_discovery_reviewed'
                          )
                      )
                      OR (
                          tc.chain_id NOT IN ('base', 'bsc', 'ethereum')
                          AND tc.source = 'binance_announcement'
                      )
                  )
                ORDER BY le.announced_at DESC, t.symbol
                """,
                (chain_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def contract_mapping_rows(self, chain_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    tc.token_contract_id,
                    t.symbol,
                    tc.contract_address,
                    tc.explorer_url,
                    tc.source,
                    tc.mapping_status,
                    tc.confidence_score
                FROM token_contracts tc
                JOIN tokens t ON t.token_id = tc.token_id
                WHERE tc.chain_id = ?
                ORDER BY t.symbol, tc.contract_address
                """,
                (chain_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def base_contract_mapping_rows(self) -> list[dict[str, Any]]:
        return self.contract_mapping_rows("base")

    def update_contract_mapping_validation(
        self,
        token_contract_id: int,
        mapping_status: str,
        confidence_score: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE token_contracts
                SET mapping_status = ?, confidence_score = ?, updated_at = ?
                WHERE token_contract_id = ?
                """,
                (
                    mapping_status,
                    confidence_score,
                    utc_now_iso(),
                    token_contract_id,
                ),
            )

    def import_base_contract_discovery_candidates(
        self,
        rows: list[dict[str, Any]],
        minimum_notional_usd: float = 100_000,
        minimum_notional_share: float = 0.9,
        minimum_trade_count: int = 500,
        approved_symbols: set[str] | None = None,
    ) -> dict[str, int]:
        stats = {
            "rows_seen": len(rows),
            "eligible": 0,
            "inserted": 0,
            "skipped_existing_verified": 0,
            "skipped_low_confidence": 0,
            "skipped_not_reviewed": 0,
            "skipped_unknown_symbol": 0,
        }
        now = utc_now_iso()
        with self.connect() as connection:
            for row in rows:
                token_address = str(row.get("token_address") or "").lower()
                gross_notional = float(row.get("gross_notional_usd") or 0)
                notional_share = float(row.get("notional_share") or 0)
                candidate_rank = int(row.get("candidate_rank") or 0)
                trade_count = int(row.get("trade_count") or 0)
                if (
                    candidate_rank != 1
                    or gross_notional < minimum_notional_usd
                    or notional_share < minimum_notional_share
                    or trade_count < minimum_trade_count
                    or not token_address.startswith("0x")
                    or len(token_address) != 42
                ):
                    stats["skipped_low_confidence"] += 1
                    continue
                symbol = normalize_symbol(str(row.get("symbol") or ""))
                if approved_symbols is not None and symbol not in approved_symbols:
                    stats["skipped_not_reviewed"] += 1
                    continue
                token = connection.execute(
                    "SELECT token_id FROM tokens WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
                if not token:
                    stats["skipped_unknown_symbol"] += 1
                    continue
                stats["eligible"] += 1
                existing_verified = connection.execute(
                    """
                    SELECT 1
                    FROM token_contracts
                    WHERE token_id = ?
                      AND chain_id = 'base'
                      AND mapping_status = 'rpc_symbol_verified'
                    LIMIT 1
                    """,
                    (int(token["token_id"]),),
                ).fetchone()
                if existing_verified:
                    stats["skipped_existing_verified"] += 1
                    continue
                first_trade_at = normalize_dune_timestamp(row["first_trade_at"])
                self._upsert_token_contract(
                    connection=connection,
                    token_id=int(token["token_id"]),
                    chain_id="base",
                    contract_address=token_address,
                    explorer_url=f"https://basescan.org/token/{token_address}",
                    first_seen_at=first_trade_at,
                    confidence_score=min(0.95, 0.4 + notional_share * 0.55),
                    mapping_status="dune_preannouncement_discovered",
                    now=now,
                    source=(
                        "dune_preannouncement_contract_discovery_reviewed"
                        if approved_symbols is not None
                        else "dune_preannouncement_contract_discovery"
                    ),
                )
                stats["inserted"] += 1
        return stats

    def export_backtest_targets_csv(self, output_path: Path) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.backtest_targets()
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "listing_event_id",
                    "symbol",
                    "name",
                    "exchange",
                    "event_type",
                    "announced_at",
                    "trading_pairs",
                    "mapping_status",
                    "confidence_score",
                    "requires_manual_review",
                    "source_url",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        **row,
                        "trading_pairs": "|".join(row["trading_pairs"]),
                    }
                )
        return len(rows)

    def import_pre_listing_wallet_buys(
        self,
        chain_id: str,
        execution_id: str | None,
        rows: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        source = f"dune_{chain_id}_pre_listing_buyers"
        records = []
        for row in rows:
            records.append(
                (
                    chain_id,
                    row["symbol"],
                    normalize_chain_address(chain_id, row["token_address"]),
                    normalize_dune_timestamp(row["announced_at"]),
                    normalize_chain_address(chain_id, row["wallet_address"]),
                    normalize_dune_timestamp(row["first_buy_at"]),
                    normalize_dune_timestamp(row["last_buy_at"]),
                    int(row["buy_trade_count"]),
                    float(row["gross_buy_usd"]),
                    source,
                    execution_id,
                    now,
                    now,
                )
            )

        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM pre_listing_wallet_buys
                WHERE chain_id = ? AND source = ?
                """,
                (chain_id, source),
            )
            connection.executemany(
                """
                INSERT INTO pre_listing_wallet_buys (
                    chain_id,
                    symbol,
                    token_address,
                    announced_at,
                    wallet_address,
                    first_buy_at,
                    last_buy_at,
                    buy_trade_count,
                    gross_buy_usd,
                    source,
                    execution_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, symbol, token_address, announced_at, wallet_address)
                DO UPDATE SET
                    first_buy_at = excluded.first_buy_at,
                    last_buy_at = excluded.last_buy_at,
                    buy_trade_count = excluded.buy_trade_count,
                    gross_buy_usd = excluded.gross_buy_usd,
                    source = excluded.source,
                    execution_id = excluded.execution_id,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def import_wallet_holding_metrics(
        self,
        chain_id: str,
        execution_id: str | None,
        rows: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        source = f"dune_{chain_id}_holding_behavior"
        records = []
        for row in rows:
            pre_buy_usd = float(row["pre_buy_usd"] or 0)
            pre_sell_usd = float(row["pre_sell_usd"] or 0)
            post_sell_usd = float(row["post_sell_usd"] or 0)
            post_buy_usd = float(row["post_buy_usd"] or 0)
            pre_sell_ratio = safe_ratio(pre_sell_usd, pre_buy_usd)
            post_sell_ratio = safe_ratio(post_sell_usd, pre_buy_usd)
            records.append(
                (
                    chain_id,
                    row["symbol"],
                    row["token_address"].lower(),
                    normalize_dune_timestamp(row["announced_at"]),
                    row["wallet_address"].lower(),
                    pre_buy_usd,
                    pre_sell_usd,
                    post_buy_usd,
                    post_sell_usd,
                    int(row["pre_buy_trades"] or 0),
                    int(row["pre_sell_trades"] or 0),
                    int(row["post_buy_trades"] or 0),
                    int(row["post_sell_trades"] or 0),
                    normalize_optional_dune_timestamp(row.get("first_buy_at")),
                    normalize_optional_dune_timestamp(row.get("last_buy_at")),
                    normalize_optional_dune_timestamp(row.get("first_sell_at")),
                    normalize_optional_dune_timestamp(row.get("last_sell_at")),
                    float(row["net_pre_usd"] or pre_buy_usd - pre_sell_usd),
                    pre_sell_ratio,
                    post_sell_ratio,
                    classify_holding_behavior(
                        pre_buy_usd=pre_buy_usd,
                        pre_sell_usd=pre_sell_usd,
                        post_sell_usd=post_sell_usd,
                        pre_sell_ratio=pre_sell_ratio,
                        post_sell_ratio=post_sell_ratio,
                    ),
                    source,
                    execution_id,
                    now,
                    now,
                )
            )

        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM wallet_holding_metrics
                WHERE chain_id = ? AND source = ?
                """,
                (chain_id, source),
            )
            connection.executemany(
                """
                INSERT INTO wallet_holding_metrics (
                    chain_id,
                    symbol,
                    token_address,
                    announced_at,
                    wallet_address,
                    pre_buy_usd,
                    pre_sell_usd,
                    post_buy_usd,
                    post_sell_usd,
                    pre_buy_trades,
                    pre_sell_trades,
                    post_buy_trades,
                    post_sell_trades,
                    first_buy_at,
                    last_buy_at,
                    first_sell_at,
                    last_sell_at,
                    net_pre_usd,
                    pre_sell_ratio,
                    post_sell_ratio,
                    holding_label,
                    source,
                    execution_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, symbol, token_address, announced_at, wallet_address)
                DO UPDATE SET
                    pre_buy_usd = excluded.pre_buy_usd,
                    pre_sell_usd = excluded.pre_sell_usd,
                    post_buy_usd = excluded.post_buy_usd,
                    post_sell_usd = excluded.post_sell_usd,
                    pre_buy_trades = excluded.pre_buy_trades,
                    pre_sell_trades = excluded.pre_sell_trades,
                    post_buy_trades = excluded.post_buy_trades,
                    post_sell_trades = excluded.post_sell_trades,
                    first_buy_at = excluded.first_buy_at,
                    last_buy_at = excluded.last_buy_at,
                    first_sell_at = excluded.first_sell_at,
                    last_sell_at = excluded.last_sell_at,
                    net_pre_usd = excluded.net_pre_usd,
                    pre_sell_ratio = excluded.pre_sell_ratio,
                    post_sell_ratio = excluded.post_sell_ratio,
                    holding_label = excluded.holding_label,
                    source = excluded.source,
                    execution_id = excluded.execution_id,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def pre_listing_buy_summary(self, limit: int = 20) -> dict[str, Any]:
        with self.connect() as connection:
            total_rows = connection.execute(
                "SELECT COUNT(*) FROM pre_listing_wallet_buys"
            ).fetchone()[0]
            total_wallets = connection.execute(
                "SELECT COUNT(DISTINCT wallet_address) FROM pre_listing_wallet_buys"
            ).fetchone()[0]
            by_symbol = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        symbol,
                        chain_id,
                        COUNT(*) AS wallet_count,
                        SUM(gross_buy_usd) AS gross_buy_usd,
                        MIN(first_buy_at) AS earliest_buy_at,
                        MAX(last_buy_at) AS latest_buy_at
                    FROM pre_listing_wallet_buys
                    GROUP BY symbol, chain_id
                    ORDER BY gross_buy_usd DESC
                    """
                ).fetchall()
            ]
            top_wallets = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        wallet_address,
                        COUNT(DISTINCT symbol) AS symbol_count,
                        SUM(buy_trade_count) AS buy_trade_count,
                        SUM(gross_buy_usd) AS gross_buy_usd,
                        MIN(first_buy_at) AS earliest_buy_at,
                        MAX(last_buy_at) AS latest_buy_at
                    FROM pre_listing_wallet_buys
                    GROUP BY wallet_address
                    ORDER BY gross_buy_usd DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
        return {
            "total_rows": total_rows,
            "total_wallets": total_wallets,
            "by_symbol": by_symbol,
            "top_wallets": top_wallets,
        }

    def pre_listing_wallet_buy_rows(
        self,
        chain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE chain_id = ?" if chain_id else ""
        params: tuple[Any, ...] = (chain_id,) if chain_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    chain_id,
                    symbol,
                    token_address,
                    announced_at,
                    wallet_address,
                    first_buy_at,
                    last_buy_at,
                    buy_trade_count,
                    gross_buy_usd
                FROM pre_listing_wallet_buys
                {where}
                ORDER BY chain_id, wallet_address, symbol, announced_at
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def wallet_holding_metric_rows(
        self,
        chain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE chain_id = ?" if chain_id else ""
        params: tuple[Any, ...] = (chain_id,) if chain_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    chain_id,
                    symbol,
                    token_address,
                    announced_at,
                    wallet_address,
                    pre_buy_usd,
                    pre_sell_usd,
                    post_buy_usd,
                    post_sell_usd,
                    pre_buy_trades,
                    pre_sell_trades,
                    post_buy_trades,
                    post_sell_trades,
                    net_pre_usd,
                    pre_sell_ratio,
                    post_sell_ratio,
                    holding_label
                FROM wallet_holding_metrics
                {where}
                ORDER BY chain_id, wallet_address, symbol, announced_at
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def holding_behavior_summary(self, limit: int = 20) -> dict[str, Any]:
        with self.connect() as connection:
            total_rows = connection.execute(
                "SELECT COUNT(*) FROM wallet_holding_metrics"
            ).fetchone()[0]
            by_label = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        holding_label,
                        COUNT(*) AS count,
                        SUM(pre_buy_usd) AS pre_buy_usd,
                        SUM(pre_sell_usd) AS pre_sell_usd,
                        SUM(post_sell_usd) AS post_sell_usd,
                        AVG(pre_sell_ratio) AS avg_pre_sell_ratio,
                        AVG(post_sell_ratio) AS avg_post_sell_ratio
                    FROM wallet_holding_metrics
                    GROUP BY holding_label
                    ORDER BY count DESC, holding_label
                    """
                ).fetchall()
            ]
            top_holders = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_holding_metrics
                    WHERE holding_label IN ('strong_accumulator', 'accumulator')
                    ORDER BY net_pre_usd DESC, post_sell_ratio ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            top_sellers = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_holding_metrics
                    WHERE holding_label IN (
                        'pre_listing_flipper',
                        'partial_pre_listing_seller',
                        'post_listing_seller',
                        'partial_post_listing_seller'
                    )
                    ORDER BY
                        CASE holding_label
                            WHEN 'pre_listing_flipper' THEN pre_sell_ratio
                            ELSE post_sell_ratio
                        END DESC,
                        pre_buy_usd DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
        return {
            "total_rows": total_rows,
            "by_label": by_label,
            "top_holders": top_holders,
            "top_sellers": top_sellers,
        }

    def upsert_wallet_scores(self, scores: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        records = []
        for score in scores:
            records.append(
                (
                    score["wallet_address"].lower(),
                    score["chain_id"],
                    score["model_version"],
                    score["interest_score"],
                    score["noise_score"],
                    score["confidence_score"],
                    score["label"],
                    score["target_count"],
                    score["symbol_count"],
                    score["total_buy_trades"],
                    score["total_gross_buy_usd"],
                    score["avg_trade_usd"],
                    score["earliest_buy_at"],
                    score["latest_buy_at"],
                    score["max_first_lead_days"],
                    score["min_last_lead_hours"],
                    score["active_span_days"],
                    json.dumps(score["flags"], sort_keys=True),
                    json.dumps(score["evidence"], sort_keys=True),
                    now,
                    now,
                )
            )

        with self.connect() as connection:
            score_scopes = sorted(
                {
                    (record[1], record[2])
                    for record in records
                }
            )
            connection.executemany(
                """
                DELETE FROM wallet_scores
                WHERE chain_id = ? AND model_version = ?
                """,
                score_scopes,
            )
            connection.executemany(
                """
                INSERT INTO wallet_scores (
                    wallet_address,
                    chain_id,
                    model_version,
                    interest_score,
                    noise_score,
                    confidence_score,
                    label,
                    target_count,
                    symbol_count,
                    total_buy_trades,
                    total_gross_buy_usd,
                    avg_trade_usd,
                    earliest_buy_at,
                    latest_buy_at,
                    max_first_lead_days,
                    min_last_lead_hours,
                    active_span_days,
                    flags_json,
                    evidence_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, chain_id, model_version)
                DO UPDATE SET
                    interest_score = excluded.interest_score,
                    noise_score = excluded.noise_score,
                    confidence_score = excluded.confidence_score,
                    label = excluded.label,
                    target_count = excluded.target_count,
                    symbol_count = excluded.symbol_count,
                    total_buy_trades = excluded.total_buy_trades,
                    total_gross_buy_usd = excluded.total_gross_buy_usd,
                    avg_trade_usd = excluded.avg_trade_usd,
                    earliest_buy_at = excluded.earliest_buy_at,
                    latest_buy_at = excluded.latest_buy_at,
                    max_first_lead_days = excluded.max_first_lead_days,
                    min_last_lead_hours = excluded.min_last_lead_hours,
                    active_span_days = excluded.active_span_days,
                    flags_json = excluded.flags_json,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def wallet_score_summary(
        self,
        model_version: str = "wallet_score_v1_2",
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            total_wallets = connection.execute(
                """
                SELECT COUNT(*)
                FROM wallet_scores
                WHERE model_version = ?
                """,
                (model_version,),
            ).fetchone()[0]
            by_label = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT label, COUNT(*) AS count
                    FROM wallet_scores
                    WHERE model_version = ?
                    GROUP BY label
                    ORDER BY count DESC, label
                    """,
                    (model_version,),
                ).fetchall()
            ]
            top_candidates = [
                self._decode_wallet_score_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_scores
                    WHERE model_version = ?
                    ORDER BY
                        CASE label
                            WHEN 'strong_candidate' THEN 1
                            WHEN 'watch_candidate' THEN 2
                            WHEN 'weak_candidate' THEN 3
                            WHEN 'likely_noise' THEN 4
                            ELSE 5
                        END,
                        interest_score DESC,
                        noise_score ASC
                    LIMIT ?
                    """,
                    (model_version, limit),
                ).fetchall()
            ]
            noisiest = [
                self._decode_wallet_score_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_scores
                    WHERE model_version = ?
                    ORDER BY noise_score DESC, total_buy_trades DESC
                    LIMIT ?
                    """,
                    (model_version, limit),
                ).fetchall()
            ]
        return {
            "total_wallets": total_wallets,
            "by_label": by_label,
            "top_candidates": top_candidates,
            "noisiest": noisiest,
        }

    def wallet_repeatability_summary(
        self,
        model_version: str = "wallet_score_v1_2",
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            by_target_count = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT target_count, COUNT(*) AS wallet_count
                    FROM wallet_scores
                    WHERE model_version = ?
                    GROUP BY target_count
                    ORDER BY target_count DESC
                    """,
                    (model_version,),
                ).fetchall()
            ]
            repeated_wallets = [
                self._decode_wallet_score_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_scores
                    WHERE model_version = ?
                      AND target_count > 1
                    ORDER BY interest_score DESC, confidence_score DESC, noise_score ASC
                    LIMIT ?
                    """,
                    (model_version, limit),
                ).fetchall()
            ]

        return {
            "by_target_count": by_target_count,
            "repeated_wallets": repeated_wallets,
            "repeat_wallet_count": sum(
                row["wallet_count"]
                for row in by_target_count
                if int(row["target_count"]) > 1
            ),
        }

    def export_wallet_scores_csv(
        self,
        output_path: Path,
        model_version: str = "wallet_score_v1_2",
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            rows = [
                self._decode_wallet_score_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM wallet_scores
                    WHERE model_version = ?
                    ORDER BY
                        CASE label
                            WHEN 'strong_candidate' THEN 1
                            WHEN 'watch_candidate' THEN 2
                            WHEN 'weak_candidate' THEN 3
                            WHEN 'likely_noise' THEN 4
                            ELSE 5
                        END,
                        interest_score DESC,
                        noise_score ASC
                    """,
                    (model_version,),
                ).fetchall()
            ]

        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "wallet_address",
                    "chain_id",
                    "model_version",
                    "label",
                    "interest_score",
                    "noise_score",
                    "confidence_score",
                    "target_count",
                    "symbol_count",
                    "total_buy_trades",
                    "total_gross_buy_usd",
                    "avg_trade_usd",
                    "earliest_buy_at",
                    "latest_buy_at",
                    "max_first_lead_days",
                    "min_last_lead_hours",
                    "active_span_days",
                    "flags",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: row[key]
                        for key in writer.fieldnames
                        if key != "flags"
                    }
                    | {"flags": "|".join(row["flags"])}
                )
        return len(rows)

    def wallet_score_rows(
        self,
        model_version: str = "wallet_score_v1_2",
        limit: int = 500,
        labels: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                ws.*,
                COALESCE(we.entity_type, 'eoa') AS entity_type,
                we.entity_label,
                we.evidence_json AS entity_evidence_json,
                COALESCE(we.excluded_from_research, 0) AS excluded_from_research,
                wcm.cluster_id,
                wc.member_count AS cluster_member_count,
                wc.independence_score AS cluster_independence_score,
                wc.methods_json AS cluster_methods_json,
                wc.rationale_json AS cluster_rationale_json
            FROM wallet_scores ws
            LEFT JOIN wallet_entities we
              ON we.chain_id = ws.chain_id
             AND we.wallet_address = ws.wallet_address
            LEFT JOIN wallet_cluster_members wcm
              ON wcm.chain_id = ws.chain_id
             AND wcm.wallet_address = ws.wallet_address
             AND wcm.model_version = ws.model_version
            LEFT JOIN wallet_clusters wc
              ON wc.cluster_id = wcm.cluster_id
             AND wc.model_version = wcm.model_version
            WHERE ws.model_version = ?
              AND COALESCE(we.excluded_from_research, 0) = 0
        """
        params: list[Any] = [model_version]
        if labels:
            placeholders = ",".join("?" for _ in labels)
            sql += f" AND ws.label IN ({placeholders})"
            params.extend(labels)
        sql += " ORDER BY ws.interest_score DESC, ws.confidence_score DESC, ws.noise_score ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        output = []
        for row in rows:
            item = self._decode_wallet_score_row(row)
            item["cluster_methods"] = json.loads(
                item.pop("cluster_methods_json") or "[]"
            )
            item["cluster_rationale"] = json.loads(
                item.pop("cluster_rationale_json") or "[]"
            )
            item["entity_evidence"] = json.loads(
                item.pop("entity_evidence_json") or "{}"
            )
            item["excluded_from_research"] = bool(item["excluded_from_research"])
            output.append(item)
        return output

    def observed_target_count(self, chain_id: str | None = None) -> int:
        sql = """
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT chain_id, symbol, token_address, announced_at
                FROM pre_listing_wallet_buys
        """
        params: tuple[Any, ...] = ()
        if chain_id:
            sql += " WHERE chain_id = ?"
            params = (chain_id,)
        sql += ")"
        with self.connect() as connection:
            return int(connection.execute(sql, params).fetchone()[0])

    def start_backtest_run(
        self,
        model_version: str,
        backtest_type: str,
        config: dict[str, Any],
        as_of: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backtest_runs (
                    model_version,
                    backtest_type,
                    status,
                    started_at,
                    as_of,
                    config_json
                )
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (
                    model_version,
                    backtest_type,
                    now,
                    as_of or now,
                    json.dumps(config, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def finish_backtest_run(
        self,
        backtest_run_id: int,
        status: str,
        metrics: dict[str, Any],
        leakage_checks: dict[str, Any],
        notes: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE backtest_runs
                SET status = ?,
                    finished_at = ?,
                    metrics_json = ?,
                    leakage_checks_json = ?,
                    notes = ?
                WHERE backtest_run_id = ?
                """,
                (
                    status,
                    utc_now_iso(),
                    json.dumps(metrics, sort_keys=True),
                    json.dumps(leakage_checks, sort_keys=True),
                    notes,
                    backtest_run_id,
                ),
            )

    def insert_backtest_evaluations(
        self,
        backtest_run_id: int,
        evaluations: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                backtest_run_id,
                row["snapshot_at"],
                row["chain_id"],
                row["symbol"],
                row["token_address"].lower(),
                row["history_target_count"],
                row["eligible_wallet_count"],
                row["predicted_wallet_count"],
                row["hit_wallet_count"],
                row["meaningful_buyer_count"],
                row.get("precision"),
                row.get("baseline_rate"),
                row.get("lift"),
                row.get("coverage"),
                row.get("median_hit_lead_days"),
                json.dumps(row.get("evidence", {}), sort_keys=True),
            )
            for row in evaluations
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO backtest_evaluations (
                    backtest_run_id,
                    snapshot_at,
                    chain_id,
                    symbol,
                    token_address,
                    history_target_count,
                    eligible_wallet_count,
                    predicted_wallet_count,
                    hit_wallet_count,
                    meaningful_buyer_count,
                    precision,
                    baseline_rate,
                    lift,
                    coverage,
                    median_hit_lead_days,
                    evidence_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def latest_backtest(self, backtest_type: str | None = None) -> dict[str, Any] | None:
        where = "WHERE backtest_type = ?" if backtest_type else ""
        params: tuple[Any, ...] = (backtest_type,) if backtest_type else ()
        with self.connect() as connection:
            run = connection.execute(
                f"""
                SELECT *
                FROM backtest_runs
                {where}
                ORDER BY backtest_run_id DESC
                LIMIT 1
                """
                ,
                params,
            ).fetchone()
            if not run:
                return None
            evaluations = connection.execute(
                """
                SELECT *
                FROM backtest_evaluations
                WHERE backtest_run_id = ?
                ORDER BY snapshot_at ASC
                """,
                (run["backtest_run_id"],),
            ).fetchall()

        item = dict(run)
        item["config"] = json.loads(item.pop("config_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["leakage_checks"] = json.loads(item.pop("leakage_checks_json"))
        item["evaluations"] = []
        for row in evaluations:
            evaluation = dict(row)
            evaluation["evidence"] = json.loads(evaluation.pop("evidence_json"))
            item["evaluations"].append(evaluation)
        return item

    def import_negative_control_candidates(
        self,
        backtest_run_id: int,
        execution_id: str | None,
        chain_id: str,
        rows: list[dict[str, Any]],
        expected_snapshot_count: int | None = None,
    ) -> dict[str, Any]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for raw in rows:
            key = (
                normalize_dune_timestamp(raw["snapshot_at"]),
                str(raw["positive_symbol"]),
                str(raw["positive_token_address"]).lower(),
            )
            grouped.setdefault(key, []).append(raw)

        records = []
        positive_ranks = []
        for (snapshot_at, positive_symbol, positive_address), candidates in grouped.items():
            scored = []
            for raw in candidates:
                gross_buy = float(raw.get("gross_buy_usd") or 0)
                gross_sell = float(raw.get("gross_sell_usd") or 0)
                net_buy = float(raw.get("net_buy_usd") or 0)
                wallets = int(raw.get("tracked_wallet_count") or 0)
                sell_ratio = safe_ratio(gross_sell, gross_buy)
                score = (
                    wallets * 15.0
                    + math.log10(max(net_buy, 1.0)) * 10.0
                    - min(30.0, sell_ratio * 30.0)
                )
                scored.append((score, raw))
            scored.sort(key=lambda item: item[0], reverse=True)
            for rank, (score, raw) in enumerate(scored, start=1):
                is_positive = int(bool(int(raw.get("is_positive") or 0)))
                if is_positive:
                    positive_ranks.append(rank)
                records.append(
                    (
                        backtest_run_id,
                        execution_id,
                        snapshot_at,
                        chain_id,
                        positive_symbol,
                        positive_address,
                        raw.get("token_symbol"),
                        str(raw["token_address"]).lower(),
                        is_positive,
                        int(raw.get("cohort_wallet_count") or 0),
                        int(raw.get("tracked_wallet_count") or 0),
                        float(raw.get("gross_buy_usd") or 0),
                        float(raw.get("gross_sell_usd") or 0),
                        float(raw.get("net_buy_usd") or 0),
                        int(raw.get("buy_trade_count") or 0),
                        int(raw.get("sell_trade_count") or 0),
                        normalize_optional_dune_timestamp(raw.get("first_trade_at")),
                        normalize_optional_dune_timestamp(raw.get("last_trade_at")),
                        score,
                        rank,
                        json.dumps({}, sort_keys=True),
                    )
                )

        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO backtest_token_candidates (
                    backtest_run_id,
                    execution_id,
                    snapshot_at,
                    chain_id,
                    positive_symbol,
                    positive_token_address,
                    token_symbol,
                    token_address,
                    is_positive,
                    cohort_wallet_count,
                    tracked_wallet_count,
                    gross_buy_usd,
                    gross_sell_usd,
                    net_buy_usd,
                    buy_trade_count,
                    sell_trade_count,
                    first_trade_at,
                    last_trade_at,
                    candidate_score,
                    candidate_rank,
                    evidence_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )

        observed_snapshot_count = len(grouped)
        snapshot_count = max(observed_snapshot_count, int(expected_snapshot_count or 0))
        positive_found_count = len(positive_ranks)
        negative_candidate_count = sum(1 for record in records if not record[8])
        reciprocal_rank = (
            sum(1 / rank for rank in positive_ranks) / snapshot_count
            if snapshot_count
            else None
        )
        return {
            "snapshot_count": snapshot_count,
            "observed_snapshot_count": observed_snapshot_count,
            "candidate_count": len(records),
            "negative_candidate_count": negative_candidate_count,
            "positive_found_count": positive_found_count,
            "positive_hit_rate": safe_ratio(positive_found_count, snapshot_count)
            if snapshot_count
            else None,
            "hit_rate_at_1": safe_ratio(
                sum(1 for rank in positive_ranks if rank <= 1),
                snapshot_count,
            )
            if snapshot_count
            else None,
            "hit_rate_at_5": safe_ratio(
                sum(1 for rank in positive_ranks if rank <= 5),
                snapshot_count,
            )
            if snapshot_count
            else None,
            "mean_reciprocal_rank": reciprocal_rank,
            "diagnostic_gate_passed": (
                snapshot_count >= 10 and negative_candidate_count >= 100
            ),
            "ready_for_token_prediction_claims": False,
            "research_only": True,
            "minimum_required_snapshots": 10,
            "minimum_required_negative_candidates": 100,
        }

    def latest_negative_control_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        latest = self.latest_backtest("token_candidate_negative_control")
        if not latest:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM backtest_token_candidates
                WHERE backtest_run_id = ?
                ORDER BY snapshot_at DESC, candidate_rank ASC
                LIMIT ?
                """,
                (latest["backtest_run_id"], max(1, min(int(limit), 1000))),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def import_radar_observations(
        self,
        chain_id: str,
        execution_id: str | None,
        rows: list[dict[str, Any]],
        source: str = "dune_base_live_radar",
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in rows:
            observed_at = normalize_dune_timestamp(row.get("observed_at") or now)
            wallet_addresses = normalize_dune_array(
                row.get("accumulating_wallet_addresses")
            )
            wallet_net_buy_usd = normalize_dune_array(
                row.get("accumulating_wallet_net_buy_usd")
            )
            wallet_flows = [
                {
                    "wallet_address": str(address).lower(),
                    "net_buy_usd": optional_float(value),
                }
                for address, value in zip(wallet_addresses, wallet_net_buy_usd)
                if address
            ]
            evidence = {
                "average_wallet_confidence": float(
                    row.get("average_wallet_confidence") or 0
                ),
                "accumulating_wallet_addresses": [
                    str(address).lower() for address in wallet_addresses if address
                ],
                "accumulating_wallet_flows": wallet_flows,
            }
            evidence.update(row.get("evidence") or {})
            records.append(
                (
                    chain_id,
                    str(row["token_address"]).lower(),
                    row.get("token_symbol"),
                    observed_at,
                    int(row.get("window_hours") or 0),
                    int(row.get("tracked_wallet_count") or 0),
                    int(row.get("strong_wallet_count") or 0),
                    int(row.get("watch_wallet_count") or 0),
                    float(row.get("gross_buy_usd") or 0),
                    float(row.get("gross_sell_usd") or 0),
                    float(row.get("net_buy_usd") or 0),
                    int(row.get("buy_trade_count") or 0),
                    int(row.get("sell_trade_count") or 0),
                    normalize_optional_dune_timestamp(row.get("first_trade_at")),
                    normalize_optional_dune_timestamp(row.get("last_trade_at")),
                    float(row.get("weighted_wallet_score") or 0),
                    source,
                    execution_id,
                    json.dumps(evidence, sort_keys=True),
                    now,
                )
            )

        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO radar_observations (
                    chain_id,
                    token_address,
                    token_symbol,
                    observed_at,
                    window_hours,
                    tracked_wallet_count,
                    strong_wallet_count,
                    watch_wallet_count,
                    gross_buy_usd,
                    gross_sell_usd,
                    net_buy_usd,
                    buy_trade_count,
                    sell_trade_count,
                    first_trade_at,
                    last_trade_at,
                    weighted_wallet_score,
                    source,
                    execution_id,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, observed_at, window_hours)
                DO UPDATE SET
                    token_symbol = excluded.token_symbol,
                    tracked_wallet_count = excluded.tracked_wallet_count,
                    strong_wallet_count = excluded.strong_wallet_count,
                    watch_wallet_count = excluded.watch_wallet_count,
                    gross_buy_usd = excluded.gross_buy_usd,
                    gross_sell_usd = excluded.gross_sell_usd,
                    net_buy_usd = excluded.net_buy_usd,
                    buy_trade_count = excluded.buy_trade_count,
                    sell_trade_count = excluded.sell_trade_count,
                    first_trade_at = excluded.first_trade_at,
                    last_trade_at = excluded.last_trade_at,
                    weighted_wallet_score = excluded.weighted_wallet_score,
                    source = excluded.source,
                    execution_id = excluded.execution_id,
                    evidence_json = excluded.evidence_json
                """,
                records,
            )
        return len(records)

    def prune_radar_observations_by_source(
        self,
        source: str,
        keep_latest_snapshots: int = 1,
    ) -> int:
        retained = max(0, int(keep_latest_snapshots))
        with self.connect() as connection:
            if retained == 0:
                cursor = connection.execute(
                    "DELETE FROM radar_observations WHERE source = ?",
                    (source,),
                )
                return int(cursor.rowcount if cursor.rowcount is not None else 0)
            retained_times = [
                row["observed_at"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT observed_at
                    FROM radar_observations
                    WHERE source = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                    """,
                    (source, retained),
                )
            ]
            if not retained_times:
                return 0
            placeholders = ",".join("?" for _ in retained_times)
            cursor = connection.execute(
                f"""
                DELETE FROM radar_observations
                WHERE source = ?
                  AND observed_at NOT IN ({placeholders})
                """,
                [source, *retained_times],
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def upsert_token_market_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
                row["observed_at"],
                row["source"],
                row.get("token_name"),
                row.get("token_symbol"),
                row.get("pair_address"),
                row.get("dex_id"),
                row.get("price_usd"),
                row.get("liquidity_usd"),
                row.get("volume_24h_usd"),
                row.get("market_cap_usd"),
                row.get("fdv_usd"),
                row.get("pair_created_at"),
                row.get("website_url"),
                json.dumps(row.get("social_links", []), sort_keys=True),
                row.get("boosts_active"),
                json.dumps(row.get("raw", {}), sort_keys=True),
            )
            for row in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO token_market_snapshots (
                    chain_id,
                    token_address,
                    observed_at,
                    source,
                    token_name,
                    token_symbol,
                    pair_address,
                    dex_id,
                    price_usd,
                    liquidity_usd,
                    volume_24h_usd,
                    market_cap_usd,
                    fdv_usd,
                    pair_created_at,
                    website_url,
                    social_links_json,
                    boosts_active,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, observed_at, source)
                DO UPDATE SET
                    token_name = excluded.token_name,
                    token_symbol = excluded.token_symbol,
                    pair_address = excluded.pair_address,
                    dex_id = excluded.dex_id,
                    price_usd = excluded.price_usd,
                    liquidity_usd = excluded.liquidity_usd,
                    volume_24h_usd = excluded.volume_24h_usd,
                    market_cap_usd = excluded.market_cap_usd,
                    fdv_usd = excluded.fdv_usd,
                    pair_created_at = excluded.pair_created_at,
                    website_url = excluded.website_url,
                    social_links_json = excluded.social_links_json,
                    boosts_active = excluded.boosts_active,
                    raw_json = excluded.raw_json
                """,
                records,
            )
        return len(records)

    def prune_token_market_snapshots_by_source(
        self,
        source: str,
        keep_latest_snapshots: int = 1,
    ) -> int:
        retained = max(0, int(keep_latest_snapshots))
        with self.connect() as connection:
            if retained == 0:
                cursor = connection.execute(
                    "DELETE FROM token_market_snapshots WHERE source = ?",
                    (source,),
                )
                return int(cursor.rowcount if cursor.rowcount is not None else 0)
            retained_times = [
                row["observed_at"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT observed_at
                    FROM token_market_snapshots
                    WHERE source = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                    """,
                    (source, retained),
                )
            ]
            if not retained_times:
                return 0
            placeholders = ",".join("?" for _ in retained_times)
            cursor = connection.execute(
                f"""
                DELETE FROM token_market_snapshots
                WHERE source = ?
                  AND observed_at NOT IN ({placeholders})
                """,
                [source, *retained_times],
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def local_ingestion_cursor(
        self,
        source: str,
        chain_id: str,
        cursor_key: str = "default",
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM local_ingestion_cursors
                WHERE source = ? AND chain_id = ? AND cursor_key = ?
                """,
                (source, chain_id, cursor_key),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def upsert_local_ingestion_cursor(
        self,
        source: str,
        chain_id: str,
        cursor_key: str,
        block_number: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO local_ingestion_cursors (
                    source,
                    chain_id,
                    cursor_key,
                    block_number,
                    updated_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, chain_id, cursor_key)
                DO UPDATE SET
                    block_number = excluded.block_number,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    source,
                    chain_id,
                    cursor_key,
                    int(block_number),
                    utc_now_iso(),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def import_local_dex_trades(self, trades: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        records = [
            (
                row["chain_id"],
                row.get("project"),
                row.get("dex_id"),
                str(row["pair_address"]).lower(),
                str(row["token_address"]).lower(),
                row.get("token_symbol"),
                str(row["wallet_address"]).lower(),
                str(row["tx_hash"]).lower(),
                int(row.get("log_index") or 0),
                row.get("block_number"),
                normalize_optional_dune_timestamp(row.get("block_time")),
                row["side"],
                str(row.get("amount_raw") or "0"),
                row.get("amount_token"),
                row.get("amount_usd"),
                row.get("price_usd"),
                normalize_dune_timestamp(row.get("observed_at") or now),
                int(row.get("window_hours") or 0),
                row.get("source") or "local_hypersync_dex_trades",
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
            )
            for row in trades
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO local_dex_trades (
                    chain_id,
                    project,
                    dex_id,
                    pair_address,
                    token_address,
                    token_symbol,
                    wallet_address,
                    tx_hash,
                    log_index,
                    block_number,
                    block_time,
                    side,
                    amount_raw,
                    amount_token,
                    amount_usd,
                    price_usd,
                    observed_at,
                    window_hours,
                    source,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    chain_id,
                    tx_hash,
                    log_index,
                    token_address,
                    wallet_address,
                    source
                )
                DO UPDATE SET
                    project = excluded.project,
                    dex_id = excluded.dex_id,
                    pair_address = excluded.pair_address,
                    token_symbol = excluded.token_symbol,
                    block_number = excluded.block_number,
                    block_time = excluded.block_time,
                    side = excluded.side,
                    amount_raw = excluded.amount_raw,
                    amount_token = excluded.amount_token,
                    amount_usd = excluded.amount_usd,
                    price_usd = excluded.price_usd,
                    observed_at = excluded.observed_at,
                    window_hours = excluded.window_hours,
                    evidence_json = excluded.evidence_json
                """,
                records,
            )
        return len(records)

    def import_local_dex_rollups(self, rollups: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        records = [
            (
                row["chain_id"],
                str(row["token_address"]).lower(),
                row.get("token_symbol"),
                normalize_dune_timestamp(row.get("observed_at") or now),
                int(row.get("window_hours") or 0),
                int(row.get("tracked_wallet_count") or 0),
                int(row.get("strong_wallet_count") or 0),
                int(row.get("watch_wallet_count") or 0),
                float(row.get("gross_buy_usd") or 0),
                float(row.get("gross_sell_usd") or 0),
                float(row.get("net_buy_usd") or 0),
                int(row.get("buy_trade_count") or 0),
                int(row.get("sell_trade_count") or 0),
                normalize_optional_dune_timestamp(row.get("first_trade_at")),
                normalize_optional_dune_timestamp(row.get("last_trade_at")),
                float(row.get("weighted_wallet_score") or 0),
                row.get("source") or "local_hypersync_dex_trades",
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
            )
            for row in rollups
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO local_dex_rollups (
                    chain_id,
                    token_address,
                    token_symbol,
                    observed_at,
                    window_hours,
                    tracked_wallet_count,
                    strong_wallet_count,
                    watch_wallet_count,
                    gross_buy_usd,
                    gross_sell_usd,
                    net_buy_usd,
                    buy_trade_count,
                    sell_trade_count,
                    first_trade_at,
                    last_trade_at,
                    weighted_wallet_score,
                    source,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, observed_at, window_hours, source)
                DO UPDATE SET
                    token_symbol = excluded.token_symbol,
                    tracked_wallet_count = excluded.tracked_wallet_count,
                    strong_wallet_count = excluded.strong_wallet_count,
                    watch_wallet_count = excluded.watch_wallet_count,
                    gross_buy_usd = excluded.gross_buy_usd,
                    gross_sell_usd = excluded.gross_sell_usd,
                    net_buy_usd = excluded.net_buy_usd,
                    buy_trade_count = excluded.buy_trade_count,
                    sell_trade_count = excluded.sell_trade_count,
                    first_trade_at = excluded.first_trade_at,
                    last_trade_at = excluded.last_trade_at,
                    weighted_wallet_score = excluded.weighted_wallet_score,
                    evidence_json = excluded.evidence_json
                """,
                records,
            )
        return len(records)

    def prune_local_dex_trades_by_source(
        self,
        source: str,
        keep_latest_snapshots: int = 1,
    ) -> int:
        return self._prune_local_snapshot_table(
            table_name="local_dex_trades",
            source=source,
            keep_latest_snapshots=keep_latest_snapshots,
        )

    def prune_local_dex_rollups_by_source(
        self,
        source: str,
        keep_latest_snapshots: int = 1,
    ) -> int:
        return self._prune_local_snapshot_table(
            table_name="local_dex_rollups",
            source=source,
            keep_latest_snapshots=keep_latest_snapshots,
        )

    def _prune_local_snapshot_table(
        self,
        table_name: str,
        source: str,
        keep_latest_snapshots: int,
    ) -> int:
        if table_name not in {"local_dex_trades", "local_dex_rollups"}:
            raise ValueError(f"Unsupported local snapshot table: {table_name}")
        retained = max(0, int(keep_latest_snapshots))
        with self.connect() as connection:
            if retained == 0:
                cursor = connection.execute(
                    f"DELETE FROM {table_name} WHERE source = ?",
                    (source,),
                )
                return int(cursor.rowcount if cursor.rowcount is not None else 0)
            retained_times = [
                row["observed_at"]
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT observed_at
                    FROM {table_name}
                    WHERE source = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                    """,
                    (source, retained),
                )
            ]
            if not retained_times:
                return 0
            placeholders = ",".join("?" for _ in retained_times)
            cursor = connection.execute(
                f"""
                DELETE FROM {table_name}
                WHERE source = ?
                  AND observed_at NOT IN ({placeholders})
                """,
                [source, *retained_times],
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def prune_signals_by_evidence_source_mode(
        self,
        source_mode: str,
        keep_latest_detected: int = 1,
    ) -> int:
        retained = max(0, int(keep_latest_detected))
        pattern = f'%\"source_mode\": \"{source_mode}\"%'
        with self.connect() as connection:
            retained_times = (
                [
                    row["detected_at"]
                    for row in connection.execute(
                        """
                        SELECT DISTINCT detected_at
                        FROM signals
                        WHERE evidence_json LIKE ?
                        ORDER BY detected_at DESC
                        LIMIT ?
                        """,
                        (pattern, retained),
                    )
                ]
                if retained
                else []
            )
            parameters: list[Any] = [pattern]
            retained_clause = ""
            if retained_times:
                placeholders = ",".join("?" for _ in retained_times)
                retained_clause = f" AND detected_at NOT IN ({placeholders})"
                parameters.extend(retained_times)
            signal_ids = [
                row["signal_id"]
                for row in connection.execute(
                    f"""
                    SELECT signal_id
                    FROM signals
                    WHERE evidence_json LIKE ?
                    {retained_clause}
                    """,
                    parameters,
                )
            ]
            if not signal_ids:
                return 0
            placeholders = ",".join("?" for _ in signal_ids)
            connection.execute(
                f"DELETE FROM signal_shadow_marks WHERE signal_id IN ({placeholders})",
                signal_ids,
            )
            connection.execute(
                f"DELETE FROM signal_wallets WHERE signal_id IN ({placeholders})",
                signal_ids,
            )
            cursor = connection.execute(
                f"DELETE FROM signals WHERE signal_id IN ({placeholders})",
                signal_ids,
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def upsert_token_risk_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                row["chain_id"],
                row["token_address"].lower(),
                row["observed_at"],
                row["source"],
                row.get("risk_score"),
                optional_bool_int(row.get("is_honeypot")),
                optional_bool_int(row.get("is_open_source")),
                optional_bool_int(row.get("is_proxy")),
                optional_bool_int(row.get("is_mintable")),
                row.get("buy_tax"),
                row.get("sell_tax"),
                row.get("holder_count"),
                row.get("top_holder_ratio"),
                json.dumps(row.get("flags", []), sort_keys=True),
                json.dumps(row.get("raw", {}), sort_keys=True),
            )
            for row in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO token_risk_snapshots (
                    chain_id,
                    token_address,
                    observed_at,
                    source,
                    risk_score,
                    is_honeypot,
                    is_open_source,
                    is_proxy,
                    is_mintable,
                    buy_tax,
                    sell_tax,
                    holder_count,
                    top_holder_ratio,
                    flags_json,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, observed_at, source)
                DO UPDATE SET
                    risk_score = excluded.risk_score,
                    is_honeypot = excluded.is_honeypot,
                    is_open_source = excluded.is_open_source,
                    is_proxy = excluded.is_proxy,
                    is_mintable = excluded.is_mintable,
                    buy_tax = excluded.buy_tax,
                    sell_tax = excluded.sell_tax,
                    holder_count = excluded.holder_count,
                    top_holder_ratio = excluded.top_holder_ratio,
                    flags_json = excluded.flags_json,
                    raw_json = excluded.raw_json
                """,
                records,
            )
        return len(records)

    def upsert_token_onchain_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                row["chain_id"],
                row["token_address"].lower(),
                row["observed_at"],
                row["source"],
                optional_bool_int(row.get("contract_verified")),
                optional_bool_int(row.get("is_contract")),
                optional_bool_int(row.get("is_scam")),
                row.get("reputation"),
                row.get("proxy_type"),
                json.dumps(row.get("implementation_addresses", []), sort_keys=True),
                row.get("holder_count"),
                row.get("top10_holder_ratio"),
                row.get("top10_eoa_holder_ratio"),
                row.get("top10_contract_holder_ratio"),
                row.get("labeled_holder_ratio"),
                row.get("inbound_wallet_count"),
                row.get("outbound_wallet_count"),
                row.get("transfer_count"),
                row.get("last_activity_at"),
                json.dumps(row.get("flags", []), sort_keys=True),
                json.dumps(row.get("raw", {}), sort_keys=True),
            )
            for row in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO token_onchain_snapshots (
                    chain_id,
                    token_address,
                    observed_at,
                    source,
                    contract_verified,
                    is_contract,
                    is_scam,
                    reputation,
                    proxy_type,
                    implementation_addresses_json,
                    holder_count,
                    top10_holder_ratio,
                    top10_eoa_holder_ratio,
                    top10_contract_holder_ratio,
                    labeled_holder_ratio,
                    inbound_wallet_count,
                    outbound_wallet_count,
                    transfer_count,
                    last_activity_at,
                    flags_json,
                    raw_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(chain_id, token_address, observed_at, source)
                DO UPDATE SET
                    contract_verified = excluded.contract_verified,
                    is_contract = excluded.is_contract,
                    is_scam = excluded.is_scam,
                    reputation = excluded.reputation,
                    proxy_type = excluded.proxy_type,
                    implementation_addresses_json = excluded.implementation_addresses_json,
                    holder_count = excluded.holder_count,
                    top10_holder_ratio = excluded.top10_holder_ratio,
                    top10_eoa_holder_ratio = excluded.top10_eoa_holder_ratio,
                    top10_contract_holder_ratio = excluded.top10_contract_holder_ratio,
                    labeled_holder_ratio = excluded.labeled_holder_ratio,
                    inbound_wallet_count = excluded.inbound_wallet_count,
                    outbound_wallet_count = excluded.outbound_wallet_count,
                    transfer_count = excluded.transfer_count,
                    last_activity_at = excluded.last_activity_at,
                    flags_json = excluded.flags_json,
                    raw_json = excluded.raw_json
                """,
                records,
            )
        return len(records)

    def latest_radar_observations(
        self,
        limit: int = 100,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        source_filter = ""
        latest_source_filter = ""
        parameters: list[Any] = []
        if source:
            source_filter = "AND ro.source = ?"
            latest_source_filter = "WHERE source = ?"
            parameters.extend([source, source])
        parameters.append(max(1, min(int(limit), 1000)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    ro.*,
                    ms.token_name AS market_token_name,
                    ms.token_symbol AS market_token_symbol,
                    ms.pair_address,
                    ms.dex_id,
                    ms.price_usd,
                    ms.liquidity_usd,
                    ms.volume_24h_usd,
                    ms.market_cap_usd,
                    ms.fdv_usd,
                    ms.pair_created_at,
                    ms.website_url,
                    ms.social_links_json,
                    ms.boosts_active,
                    rs.risk_score,
                    rs.is_honeypot,
                    rs.is_open_source,
                    rs.is_proxy,
                    rs.is_mintable,
                    rs.buy_tax,
                    rs.sell_tax,
                    rs.holder_count,
                    rs.top_holder_ratio,
                    rs.flags_json AS risk_flags_json,
                    bs.contract_verified AS blockscout_contract_verified,
                    bs.is_contract AS blockscout_is_contract,
                    bs.is_scam AS blockscout_is_scam,
                    bs.reputation AS blockscout_reputation,
                    bs.proxy_type AS blockscout_proxy_type,
                    bs.implementation_addresses_json AS blockscout_implementations_json,
                    bs.holder_count AS blockscout_holder_count,
                    bs.flags_json AS blockscout_flags_json,
                    mh.top10_holder_ratio AS moralis_top10_holder_ratio,
                    mh.top10_eoa_holder_ratio AS moralis_top10_eoa_holder_ratio,
                    mh.top10_contract_holder_ratio AS moralis_top10_contract_holder_ratio,
                    mh.labeled_holder_ratio AS moralis_labeled_holder_ratio,
                    mh.flags_json AS moralis_flags_json,
                    hs.inbound_wallet_count AS hypersync_inbound_wallet_count,
                    hs.outbound_wallet_count AS hypersync_outbound_wallet_count,
                    hs.transfer_count AS hypersync_transfer_count,
                    hs.last_activity_at AS hypersync_last_activity_at,
                    hs.flags_json AS hypersync_flags_json,
                    ss.query_text AS social_query_text,
                    ss.mentions_24h,
                    ss.mentions_7d,
                    ss.social_silence_score,
                    ss.coverage_score AS social_coverage_score,
                    ss.x_available AS social_x_available,
                    ss.provider_counts_json AS social_provider_counts_json,
                    ss.flags_json AS social_flags_json,
                    ats.dexscreener_ads AS attention_dexscreener_ads,
                    ats.dexscreener_profile AS attention_dexscreener_profile,
                    ats.farcaster_mentions_24h AS attention_farcaster_mentions_24h,
                    ats.farcaster_mentions_7d AS attention_farcaster_mentions_7d,
                    ats.github_commits_30d AS attention_github_commits_30d,
                    ats.github_contributors_90d AS attention_github_contributors_90d,
                    ats.news_mentions_24h AS attention_news_mentions_24h,
                    ats.news_mentions_7d AS attention_news_mentions_7d,
                    ats.onchain_trader_growth_7d,
                    ats.onchain_volume_growth_7d,
                    ats.public_attention_score,
                    ats.onchain_attention_score,
                    ats.attention_gap_score,
                    ats.coverage_score AS attention_coverage_score,
                    ats.flags_json AS attention_flags_json
                FROM radar_observations ro
                LEFT JOIN token_market_snapshots ms
                  ON ms.token_market_snapshot_id = (
                      SELECT ms2.token_market_snapshot_id
                      FROM token_market_snapshots ms2
                      WHERE ms2.chain_id = ro.chain_id
                        AND ms2.token_address = ro.token_address
                      ORDER BY ms2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_risk_snapshots rs
                  ON rs.token_risk_snapshot_id = (
                      SELECT rs2.token_risk_snapshot_id
                      FROM token_risk_snapshots rs2
                      WHERE rs2.chain_id = ro.chain_id
                        AND rs2.token_address = ro.token_address
                      ORDER BY rs2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_onchain_snapshots bs
                  ON bs.token_onchain_snapshot_id = (
                      SELECT bs2.token_onchain_snapshot_id
                      FROM token_onchain_snapshots bs2
                      WHERE bs2.chain_id = ro.chain_id
                        AND bs2.token_address = ro.token_address
                        AND bs2.source = 'blockscout'
                      ORDER BY bs2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_onchain_snapshots mh
                  ON mh.token_onchain_snapshot_id = (
                      SELECT mh2.token_onchain_snapshot_id
                      FROM token_onchain_snapshots mh2
                      WHERE mh2.chain_id = ro.chain_id
                        AND mh2.token_address = ro.token_address
                        AND mh2.source = 'moralis'
                      ORDER BY mh2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_onchain_snapshots hs
                  ON hs.token_onchain_snapshot_id = (
                      SELECT hs2.token_onchain_snapshot_id
                      FROM token_onchain_snapshots hs2
                      WHERE hs2.chain_id = ro.chain_id
                        AND hs2.token_address = ro.token_address
                        AND hs2.source = 'hypersync'
                      ORDER BY hs2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_social_snapshots ss
                  ON ss.token_social_snapshot_id = (
                      SELECT ss2.token_social_snapshot_id
                      FROM token_social_snapshots ss2
                      WHERE ss2.chain_id = ro.chain_id
                        AND ss2.token_address = ro.token_address
                      ORDER BY ss2.observed_at DESC
                      LIMIT 1
                  )
                LEFT JOIN token_attention_snapshots ats
                  ON ats.token_attention_snapshot_id = (
                      SELECT ats2.token_attention_snapshot_id
                      FROM token_attention_snapshots ats2
                      WHERE ats2.chain_id = ro.chain_id
                        AND ats2.token_address = ro.token_address
                      ORDER BY ats2.observed_at DESC
                      LIMIT 1
                  )
                WHERE ro.observed_at = (
                    SELECT MAX(observed_at)
                    FROM radar_observations
                    {latest_source_filter}
                )
                {source_filter}
                ORDER BY ro.net_buy_usd DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["social_links"] = json.loads(item.pop("social_links_json") or "[]")
            item["risk_flags"] = json.loads(item.pop("risk_flags_json") or "[]")
            item["blockscout_implementations"] = json.loads(
                item.pop("blockscout_implementations_json") or "[]"
            )
            item["blockscout_flags"] = json.loads(
                item.pop("blockscout_flags_json") or "[]"
            )
            item["moralis_flags"] = json.loads(
                item.pop("moralis_flags_json") or "[]"
            )
            item["hypersync_flags"] = json.loads(
                item.pop("hypersync_flags_json") or "[]"
            )
            for field in (
                "blockscout_contract_verified",
                "blockscout_is_contract",
                "blockscout_is_scam",
            ):
                item[field] = (
                    None if item.get(field) is None else bool(item[field])
                )
            item["social_provider_counts"] = json.loads(
                item.pop("social_provider_counts_json") or "{}"
            )
            item["social_flags"] = json.loads(item.pop("social_flags_json") or "[]")
            item["attention_flags"] = json.loads(
                item.pop("attention_flags_json") or "[]"
            )
            item["social_x_available"] = bool(item.get("social_x_available"))
            output.append(item)
        return output

    def radar_observation_history(
        self,
        chain_id: str,
        token_address: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM radar_observations
                WHERE chain_id = ? AND token_address = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (chain_id, token_address.lower(), limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output

    def upsert_signals(self, signals: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        records = [
            (
                row["token_symbol"],
                row.get("token_name"),
                row["chain_id"],
                row.get("contract_address"),
                row["signal_type"],
                row["signal_level"],
                row["confidence_score"],
                row.get("status", "candidate"),
                row["detected_at"],
                row.get("strong_wallet_count", 0),
                row.get("cluster_count", 0),
                row.get("net_buy_usd"),
                row.get("liquidity_usd"),
                row.get("social_silence_score"),
                row.get("risk_score"),
                row.get("thesis"),
                json.dumps(row.get("risk_flags", []), sort_keys=True),
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
            )
            for row in signals
        ]
        with self.connect() as connection:
            signal_keys = list(
                {
                    (
                        row["chain_id"],
                        row.get("contract_address"),
                        row["detected_at"],
                    )
                    for row in signals
                }
            )
            connection.executemany(
                """
                DELETE FROM signals
                WHERE chain_id = ?
                  AND COALESCE(contract_address, '') = COALESCE(?, '')
                  AND detected_at = ?
                """,
                signal_keys,
            )
            connection.executemany(
                """
                INSERT INTO signals (
                    token_symbol,
                    token_name,
                    chain_id,
                    contract_address,
                    signal_type,
                    signal_level,
                    confidence_score,
                    status,
                    detected_at,
                    strong_wallet_count,
                    cluster_count,
                    net_buy_usd,
                    liquidity_usd,
                    social_silence_score,
                    risk_score,
                    thesis,
                    risk_flags_json,
                    evidence_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, contract_address, signal_type, detected_at)
                DO UPDATE SET
                    token_symbol = excluded.token_symbol,
                    token_name = excluded.token_name,
                    signal_level = excluded.signal_level,
                    confidence_score = excluded.confidence_score,
                    status = excluded.status,
                    strong_wallet_count = excluded.strong_wallet_count,
                    cluster_count = excluded.cluster_count,
                    net_buy_usd = excluded.net_buy_usd,
                    liquidity_usd = excluded.liquidity_usd,
                    social_silence_score = excluded.social_silence_score,
                    risk_score = excluded.risk_score,
                    thesis = excluded.thesis,
                    risk_flags_json = excluded.risk_flags_json,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def create_app_job(self, job_type: str, message: str | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_jobs (job_type, status, requested_at, message)
                VALUES (?, 'queued', ?, ?)
                """,
                (job_type, utc_now_iso(), message),
            )
            return int(cursor.lastrowid)

    def update_app_job(
        self,
        job_id: int,
        status: str,
        progress: float,
        message: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        started_at = now if status == "running" else None
        finished_at = now if status in {"success", "failed"} else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE app_jobs
                SET status = ?,
                    progress = ?,
                    message = COALESCE(?, message),
                    error = ?,
                    result_json = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = COALESCE(?, finished_at)
                WHERE job_id = ?
                """,
                (
                    status,
                    max(0.0, min(1.0, float(progress))),
                    message,
                    error,
                    json.dumps(result or {}, sort_keys=True),
                    started_at,
                    finished_at,
                    job_id,
                ),
            )

    def app_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM app_jobs
                WHERE job_id = ?
                """,
                (int(job_id),),
            ).fetchone()
        return self._app_job_from_row(row)

    def latest_app_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM app_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY job_id DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                row = connection.execute(
                    """
                    SELECT *
                    FROM app_jobs
                    ORDER BY job_id DESC
                    LIMIT 1
                    """
                ).fetchone()
        return self._app_job_from_row(row)

    def fail_running_app_jobs(self, error: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE app_jobs
                SET status = 'failed',
                    finished_at = ?,
                    progress = 1.0,
                    message = COALESCE(message, 'Interrupted job'),
                    error = ?
                WHERE status IN ('queued', 'running')
                """,
                (utc_now_iso(), error),
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def _app_job_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        return item

    def latest_app_job_by_request_time(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM app_jobs
                ORDER BY job_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._app_job_from_row(row)

    def dashboard_coverage(self) -> dict[str, Any]:
        with self.connect() as connection:
            by_chain = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        tc.chain_id,
                        COUNT(DISTINCT le.listing_event_id) AS mapped_targets,
                        COUNT(DISTINCT CASE
                            WHEN tc.first_seen_at <= le.announced_at
                            THEN le.listing_event_id END
                        ) AS point_in_time_mappings
                    FROM listing_events le
                    JOIN token_contracts tc ON tc.token_id = le.token_id
                    WHERE le.is_backtest_target = 1
                      AND (
                          tc.chain_id <> 'base'
                          OR tc.mapping_status = 'rpc_symbol_verified'
                      )
                    GROUP BY tc.chain_id
                    ORDER BY mapped_targets DESC, tc.chain_id
                    """
                ).fetchall()
            ]
            base_targets = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        t.symbol,
                        le.announced_at,
                        tc.contract_address,
                        tc.first_seen_at,
                        tc.source AS contract_source,
                        CASE WHEN tc.first_seen_at <= le.announced_at THEN 1 ELSE 0 END
                            AS point_in_time_mapping,
                        COUNT(DISTINCT pb.wallet_address) AS wallet_rows,
                        COUNT(DISTINCT CASE
                            WHEN hm.pre_buy_usd >= 100
                             AND hm.net_pre_usd >= 100
                             AND hm.pre_sell_ratio < 0.35
                            THEN hm.wallet_address END
                        ) AS meaningful_buyers
                    FROM listing_events le
                    JOIN tokens t ON t.token_id = le.token_id
                    JOIN token_contracts tc
                      ON tc.token_id = le.token_id
                     AND tc.chain_id = 'base'
                     AND tc.mapping_status = 'rpc_symbol_verified'
                    LEFT JOIN pre_listing_wallet_buys pb
                      ON pb.chain_id = tc.chain_id
                     AND pb.symbol = t.symbol
                     AND pb.token_address = tc.contract_address
                     AND pb.announced_at = le.announced_at
                    LEFT JOIN wallet_holding_metrics hm
                      ON hm.chain_id = pb.chain_id
                     AND hm.symbol = pb.symbol
                     AND hm.token_address = pb.token_address
                     AND hm.announced_at = pb.announced_at
                     AND hm.wallet_address = pb.wallet_address
                    WHERE le.is_backtest_target = 1
                    GROUP BY
                        t.symbol,
                        le.announced_at,
                        tc.contract_address,
                        tc.first_seen_at,
                        tc.source
                    ORDER BY le.announced_at DESC
                    """
                ).fetchall()
            ]
        return {"by_chain": by_chain, "base_targets": base_targets}

    def _decode_wallet_score_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["flags"] = json.loads(item.pop("flags_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def dashboard_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            announcement_count = connection.execute(
                "SELECT COUNT(*) FROM binance_announcements"
            ).fetchone()[0]
            spot_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM binance_announcements
                WHERE category = 'spot'
                """
            ).fetchone()[0]
            contract_link_count = connection.execute(
                """
                SELECT COALESCE(SUM(json_array_length(contract_links_json)), 0)
                FROM binance_announcements
                """
            ).fetchone()[0]
            signal_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM signals
                WHERE detected_at = (SELECT MAX(detected_at) FROM signals)
                """
            ).fetchone()[0]
            registry_token_count = connection.execute(
                "SELECT COUNT(*) FROM tokens"
            ).fetchone()[0]
            registry_event_count = connection.execute(
                "SELECT COUNT(*) FROM listing_events"
            ).fetchone()[0]
            base_ready_target_count = connection.execute(
                """
                SELECT COUNT(DISTINCT le.listing_event_id)
                FROM listing_events le
                JOIN token_contracts tc ON tc.token_id = le.token_id
                WHERE le.is_backtest_target = 1
                  AND tc.chain_id = 'base'
                  AND tc.mapping_status = 'rpc_symbol_verified'
                """
            ).fetchone()[0]
            pre_listing_wallet_buy_count = connection.execute(
                "SELECT COUNT(*) FROM pre_listing_wallet_buys"
            ).fetchone()[0]
            wallet_holding_metric_count = connection.execute(
                "SELECT COUNT(*) FROM wallet_holding_metrics"
            ).fetchone()[0]
            wallet_score_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM wallet_scores
                WHERE model_version = 'wallet_score_v1_2'
                """
            ).fetchone()[0]
            meaningful_repeat_wallet_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM wallet_scores
                WHERE model_version = 'wallet_score_v1_2'
                  AND target_count >= 2
                """
            ).fetchone()[0]
            radar_observation_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM radar_observations
                WHERE observed_at = (SELECT MAX(observed_at) FROM radar_observations)
                """
            ).fetchone()[0]
            backtest_run_count = connection.execute(
                "SELECT COUNT(*) FROM backtest_runs"
            ).fetchone()[0]
            qualified_signal_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM signals
                WHERE status = 'candidate'
                  AND detected_at = (SELECT MAX(detected_at) FROM signals)
                """
            ).fetchone()[0]
            excluded_wallet_count = connection.execute(
                "SELECT COUNT(*) FROM wallet_entities WHERE excluded_from_research = 1"
            ).fetchone()[0]
            clustered_wallet_count = connection.execute(
                "SELECT COUNT(*) FROM wallet_cluster_members WHERE model_version = 'wallet_score_v1_2'"
            ).fetchone()[0]
            wallet_cluster_count = connection.execute(
                "SELECT COUNT(*) FROM wallet_clusters WHERE model_version = 'wallet_score_v1_2'"
            ).fetchone()[0]
            social_snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM token_social_snapshots"
            ).fetchone()[0]
            onchain_snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM token_onchain_snapshots"
            ).fetchone()[0]
            latest_release = connection.execute(
                "SELECT MAX(release_at) FROM binance_announcements"
            ).fetchone()[0]
            latest_run = connection.execute(
                """
                SELECT status, finished_at, records_seen, records_written
                FROM ingestion_runs
                ORDER BY run_id DESC
                LIMIT 1
                """
            ).fetchone()

        return {
            "announcement_count": announcement_count,
            "spot_count": spot_count,
            "contract_link_count": contract_link_count,
            "signal_count": signal_count,
            "registry_token_count": registry_token_count,
            "registry_event_count": registry_event_count,
            "base_ready_target_count": base_ready_target_count,
            "pre_listing_wallet_buy_count": pre_listing_wallet_buy_count,
            "wallet_holding_metric_count": wallet_holding_metric_count,
            "wallet_score_count": wallet_score_count,
            "meaningful_repeat_wallet_count": meaningful_repeat_wallet_count,
            "radar_observation_count": radar_observation_count,
            "backtest_run_count": backtest_run_count,
            "qualified_signal_count": qualified_signal_count,
            "excluded_wallet_count": excluded_wallet_count,
            "clustered_wallet_count": clustered_wallet_count,
            "wallet_cluster_count": wallet_cluster_count,
            "social_snapshot_count": social_snapshot_count,
            "onchain_snapshot_count": onchain_snapshot_count,
            "latest_release_at": latest_release,
            "latest_run": dict(latest_run) if latest_run else None,
            "db_path": str(self.db_path),
            "generated_at": utc_now_iso(),
        }

    def announcement_category_counts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT category, COUNT(*) AS count
                FROM binance_announcements
                GROUP BY category
                ORDER BY count DESC, category
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_announcements(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    release_at,
                    category,
                    title,
                    source_url,
                    extracted_symbols_json,
                    trading_pairs_json,
                    contract_links_json,
                    requires_manual_review
                FROM binance_announcements
                ORDER BY release_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        announcements = []
        for row in rows:
            item = dict(row)
            item["extracted_symbols"] = json.loads(item.pop("extracted_symbols_json"))
            item["trading_pairs"] = json.loads(item.pop("trading_pairs_json"))
            item["contract_links"] = json.loads(item.pop("contract_links_json"))
            item["requires_manual_review"] = bool(item["requires_manual_review"])
            announcements.append(item)
        return announcements

    def dashboard_chains(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    chain_id,
                    name,
                    ecosystem,
                    explorer_name,
                    explorer_url,
                    api_env_var,
                    live_priority,
                    research_enabled
                FROM chains
                ORDER BY live_priority
                """
            ).fetchall()

        chains = []
        for row in rows:
            item = dict(row)
            item["research_enabled"] = bool(item["research_enabled"])
            chains.append(item)
        return chains

    def dashboard_ingestion_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    run_id,
                    source,
                    started_at,
                    finished_at,
                    status,
                    records_seen,
                    records_written,
                    error
                FROM ingestion_runs
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    signal_id,
                    token_symbol,
                    token_name,
                    chain_id,
                    contract_address,
                    signal_type,
                    signal_level,
                    confidence_score,
                    status,
                    detected_at,
                    strong_wallet_count,
                    cluster_count,
                    net_buy_usd,
                    liquidity_usd,
                    social_silence_score,
                    risk_score,
                    thesis,
                    risk_flags_json,
                    evidence_json
                FROM signals
                WHERE detected_at = (SELECT MAX(detected_at) FROM signals)
                ORDER BY
                    CASE status
                        WHEN 'candidate' THEN 0
                        WHEN 'needs_review' THEN 1
                        ELSE 2
                    END,
                    confidence_score DESC,
                    net_buy_usd DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        signals = []
        for row in rows:
            item = dict(row)
            item["risk_flags"] = json.loads(item.pop("risk_flags_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            signals.append(item)
        return signals

    def import_wallet_entities(
        self,
        chain_id: str,
        rows: list[dict[str, Any]],
        source: str = "dune_wallet_entities",
    ) -> int:
        checked_at = utc_now_iso()
        records = []
        for row in rows:
            wallet_address = str(row["wallet_address"]).lower()
            cex_name = row.get("cex_name")
            service_label = row.get("service_label")
            service_category = row.get("service_category")
            is_contract = bool(row.get("is_contract"))
            is_exchange = bool(cex_name)
            is_service = bool(service_label or service_category)
            entity_type = (
                "contract"
                if is_contract
                else "exchange"
                if is_exchange
                else "service"
                if is_service
                else "eoa"
            )
            entity_label = cex_name or service_label
            excluded = is_contract or is_exchange or is_service
            confidence = 1.0 if is_exchange else 0.95 if is_contract else 0.85 if is_service else 0.8
            evidence = {
                "cex_distinct_name": row.get("distinct_name"),
                "identity_label": row.get("identity_label"),
                "identity_category": row.get("identity_category"),
                "service_category": service_category,
                "trader_age": row.get("trader_age"),
                "average_trade_value": row.get("average_trade_value"),
                "trader_frequency": row.get("trader_frequency"),
                "dex_diversity": row.get("dex_diversity"),
                "aggregator_profile": row.get("aggregator_profile"),
                "signer_origin_eoa": bool(row.get("signer_origin_eoa", True)),
            }
            records.append(
                (
                    chain_id,
                    wallet_address,
                    entity_type,
                    entity_label,
                    int(is_contract),
                    int(is_exchange),
                    int(is_service),
                    int(excluded),
                    confidence,
                    source,
                    json.dumps(evidence, sort_keys=True),
                    checked_at,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO wallet_entities (
                    chain_id, wallet_address, entity_type, entity_label,
                    is_contract, is_exchange, is_service,
                    excluded_from_research, confidence_score, source,
                    evidence_json, checked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, wallet_address) DO UPDATE SET
                    entity_type = excluded.entity_type,
                    entity_label = excluded.entity_label,
                    is_contract = excluded.is_contract,
                    is_exchange = excluded.is_exchange,
                    is_service = excluded.is_service,
                    excluded_from_research = excluded.excluded_from_research,
                    confidence_score = excluded.confidence_score,
                    source = excluded.source,
                    evidence_json = excluded.evidence_json,
                    checked_at = excluded.checked_at
                """,
                records,
            )
        return len(records)

    def excluded_wallet_addresses(self, chain_id: str = "base") -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT wallet_address
                FROM wallet_entities
                WHERE chain_id = ? AND excluded_from_research = 1
                """,
                (chain_id,),
            ).fetchall()
        return {str(row["wallet_address"]).lower() for row in rows}

    def wallet_entity_summary(self, chain_id: str = "base") -> dict[str, Any]:
        with self.connect() as connection:
            by_type = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT entity_type, COUNT(*) AS count
                    FROM wallet_entities
                    WHERE chain_id = ?
                    GROUP BY entity_type
                    ORDER BY count DESC, entity_type
                    """,
                    (chain_id,),
                ).fetchall()
            ]
            excluded = connection.execute(
                """
                SELECT COUNT(*) FROM wallet_entities
                WHERE chain_id = ? AND excluded_from_research = 1
                """,
                (chain_id,),
            ).fetchone()[0]
        return {"by_type": by_type, "excluded_count": int(excluded)}

    def wallet_entity_rows(self, chain_id: str = "base") -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_entities WHERE chain_id = ?",
                (chain_id,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            item["excluded_from_research"] = bool(item["excluded_from_research"])
            output.append(item)
        return output

    def upsert_wallet_identity_coverage(
        self,
        chain_id: str,
        wallet_addresses: Iterable[str],
        coverage_type: str,
        status: str,
        source: str,
        evidence: dict[str, Any] | None = None,
    ) -> int:
        checked_at = utc_now_iso()
        addresses = sorted({str(address).lower() for address in wallet_addresses if address})
        records = [
            (
                chain_id,
                address,
                coverage_type,
                status,
                source,
                json.dumps(evidence or {}, sort_keys=True),
                checked_at,
            )
            for address in addresses
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO wallet_identity_coverage (
                    chain_id, wallet_address, coverage_type, status,
                    source, evidence_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, wallet_address, coverage_type, source)
                DO UPDATE SET
                    status = excluded.status,
                    evidence_json = excluded.evidence_json,
                    checked_at = excluded.checked_at
                """,
                records,
            )
        return len(records)

    def wallet_identity_coverage_rows(
        self,
        chain_id: str = "base",
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM wallet_identity_coverage
                WHERE chain_id = ?
                ORDER BY wallet_address, coverage_type, checked_at DESC
                """,
                (chain_id,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            output.append(item)
        return output

    def identity_coverage_summary(
        self,
        chain_id: str = "base",
        model_version: str = "wallet_score_v1_2",
    ) -> dict[str, Any]:
        required = {
            "entity_classification",
            "funding_hops_3",
            "shared_routes",
            "owner_resolution",
        }
        with self.connect() as connection:
            candidate_rows = connection.execute(
                """
                SELECT ws.wallet_address
                FROM wallet_scores ws
                LEFT JOIN wallet_entities we
                  ON we.chain_id = ws.chain_id
                 AND we.wallet_address = ws.wallet_address
                WHERE ws.chain_id = ?
                  AND ws.model_version = ?
                  AND ws.label IN ('strong_candidate', 'watch_candidate')
                  AND ws.target_count >= 2
                  AND COALESCE(we.excluded_from_research, 0) = 0
                """,
                (chain_id, model_version),
            ).fetchall()
        candidates = {str(row["wallet_address"]).lower() for row in candidate_rows}
        coverage_by_wallet: dict[str, set[str]] = {}
        for row in self.wallet_identity_coverage_rows(chain_id):
            if row["status"] != "complete":
                continue
            coverage_by_wallet.setdefault(row["wallet_address"], set()).add(
                row["coverage_type"]
            )
        supported = {
            address
            for address in candidates
            if required.issubset(coverage_by_wallet.get(address, set()))
        }
        return {
            "candidate_wallet_count": len(candidates),
            "supported_wallet_count": len(supported),
            "unknown_wallet_count": len(candidates - supported),
            "coverage_ratio": len(supported) / len(candidates) if candidates else 0.0,
            "required_coverage_types": sorted(required),
        }

    def import_wallet_funding(
        self,
        chain_id: str,
        rows: list[dict[str, Any]],
        source: str = "dune_base_first_funder",
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in rows:
            funder_cex = row.get("funder_cex")
            funder_category = row.get("funder_category")
            service = bool(funder_cex) or str(funder_category or "").lower() in {
                "bridge",
                "cex",
                "dex",
                "infrastructure",
                "market maker",
                "mev",
            }
            records.append(
                (
                    chain_id,
                    str(row["wallet_address"]).lower(),
                    str(row.get("funder_address") or "").lower() or None,
                    normalize_optional_dune_timestamp(row.get("funded_at")),
                    float(row.get("amount_eth") or 0),
                    row.get("funder_label"),
                    funder_category,
                    funder_cex,
                    int(service),
                    source,
                    json.dumps({"minimum_funding_eth": 0.0001}, sort_keys=True),
                    now,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO wallet_funding (
                    chain_id, wallet_address, funder_address, funded_at,
                    amount_native, funder_label, funder_category, funder_cex,
                    is_shared_service, source, evidence_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, wallet_address) DO UPDATE SET
                    funder_address = excluded.funder_address,
                    funded_at = excluded.funded_at,
                    amount_native = excluded.amount_native,
                    funder_label = excluded.funder_label,
                    funder_category = excluded.funder_category,
                    funder_cex = excluded.funder_cex,
                    is_shared_service = excluded.is_shared_service,
                    source = excluded.source,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def wallet_funding_rows(self, chain_id: str = "base") -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_funding WHERE chain_id = ?",
                (chain_id,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["is_shared_service"] = bool(item["is_shared_service"])
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def replace_wallet_clusters(
        self,
        clusters: list[dict[str, Any]],
        model_version: str,
        chain_id: str = "base",
    ) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM wallet_cluster_members WHERE chain_id = ? AND model_version = ?",
                (chain_id, model_version),
            )
            connection.execute(
                "DELETE FROM wallet_clusters WHERE chain_id = ? AND model_version = ?",
                (chain_id, model_version),
            )
            for cluster in clusters:
                connection.execute(
                    """
                    INSERT INTO wallet_clusters (
                        cluster_id, chain_id, model_version, member_count,
                        independence_score, methods_json, rationale_json,
                        evidence_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cluster["cluster_id"],
                        chain_id,
                        model_version,
                        len(cluster["members"]),
                        cluster["independence_score"],
                        json.dumps(cluster.get("methods", []), sort_keys=True),
                        json.dumps(cluster.get("rationale", []), sort_keys=True),
                        json.dumps(cluster.get("evidence", {}), sort_keys=True),
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO wallet_cluster_members (
                        cluster_id, model_version, chain_id, wallet_address,
                        role, link_confidence, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            cluster["cluster_id"],
                            model_version,
                            chain_id,
                            member["wallet_address"].lower(),
                            member.get("role", "member"),
                            member.get("link_confidence", 0),
                            json.dumps(member.get("evidence", {}), sort_keys=True),
                        )
                        for member in cluster["members"]
                    ],
                )
        return len(clusters)

    def dashboard_clusters(
        self,
        model_version: str = "wallet_score_v1_2",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM wallet_clusters
                WHERE model_version = ?
                ORDER BY member_count DESC, independence_score ASC, cluster_id
                LIMIT ?
                """,
                (model_version, max(1, min(int(limit), 200))),
            ).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                members = connection.execute(
                    """
                    SELECT wallet_address, role, link_confidence, evidence_json
                    FROM wallet_cluster_members
                    WHERE cluster_id = ? AND model_version = ?
                    ORDER BY link_confidence DESC, wallet_address
                    """,
                    (item["cluster_id"], model_version),
                ).fetchall()
                item["methods"] = json.loads(item.pop("methods_json"))
                item["rationale"] = json.loads(item.pop("rationale_json"))
                item["evidence"] = json.loads(item.pop("evidence_json"))
                item["members"] = [
                    {
                        **dict(member),
                        "evidence": json.loads(member["evidence_json"]),
                    }
                    for member in members
                ]
                for member in item["members"]:
                    member.pop("evidence_json", None)
                output.append(item)
        return output

    def wallet_context_for_addresses(
        self,
        addresses: list[str],
        model_version: str = "wallet_score_v1_2",
    ) -> list[dict[str, Any]]:
        unique = sorted({address.lower() for address in addresses if address})
        if not unique:
            return []
        placeholders = ",".join("?" for _ in unique)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    ws.wallet_address, ws.label, ws.interest_score,
                    ws.noise_score, ws.confidence_score, ws.target_count,
                    ws.evidence_json, we.entity_type, we.entity_label,
                    we.wallet_address AS entity_checked_address,
                    we.evidence_json AS entity_evidence_json, wcm.cluster_id,
                    wc.member_count AS cluster_member_count,
                    wc.independence_score AS cluster_independence_score,
                    wc.methods_json AS cluster_methods_json,
                    wc.evidence_json AS cluster_evidence_json,
                    wc.rationale_json AS cluster_rationale_json
                FROM wallet_scores ws
                LEFT JOIN wallet_entities we
                  ON we.chain_id = ws.chain_id
                 AND we.wallet_address = ws.wallet_address
                LEFT JOIN wallet_cluster_members wcm
                  ON wcm.chain_id = ws.chain_id
                 AND wcm.wallet_address = ws.wallet_address
                 AND wcm.model_version = ws.model_version
                LEFT JOIN wallet_clusters wc
                  ON wc.cluster_id = wcm.cluster_id
                 AND wc.model_version = wcm.model_version
                WHERE ws.model_version = ?
                  AND ws.wallet_address IN ({placeholders})
                  AND COALESCE(we.excluded_from_research, 0) = 0
                ORDER BY ws.interest_score DESC
                """,
                (model_version, *unique),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            evidence = json.loads(item.pop("evidence_json"))
            item["why_smart_money"] = evidence.get("why_smart_money", [])
            item["counter_evidence"] = evidence.get("counter_evidence", [])
            item["verdict"] = evidence.get("verdict")
            item["entity_evidence"] = json.loads(
                item.pop("entity_evidence_json") or "{}"
            )
            item["cluster_rationale"] = json.loads(
                item.pop("cluster_rationale_json") or "[]"
            )
            item["cluster_methods"] = json.loads(
                item.pop("cluster_methods_json") or "[]"
            )
            item["cluster_evidence"] = json.loads(
                item.pop("cluster_evidence_json") or "{}"
            )
            item["entity_checked"] = bool(item.pop("entity_checked_address"))
            output.append(item)
        return output

    def upsert_token_social_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                row["chain_id"],
                row["token_address"].lower(),
                row["observed_at"],
                row["source"],
                row["query_text"],
                row.get("mentions_24h"),
                row.get("mentions_7d"),
                row.get("social_silence_score"),
                row.get("coverage_score", 0),
                int(bool(row.get("x_available"))),
                json.dumps(row.get("provider_counts", {}), sort_keys=True),
                json.dumps(row.get("flags", []), sort_keys=True),
                json.dumps(row.get("raw", {}), sort_keys=True),
            )
            for row in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO token_social_snapshots (
                    chain_id, token_address, observed_at, source, query_text,
                    mentions_24h, mentions_7d, social_silence_score,
                    coverage_score, x_available, provider_counts_json,
                    flags_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, observed_at, source)
                DO UPDATE SET
                    query_text = excluded.query_text,
                    mentions_24h = excluded.mentions_24h,
                    mentions_7d = excluded.mentions_7d,
                    social_silence_score = excluded.social_silence_score,
                    coverage_score = excluded.coverage_score,
                    x_available = excluded.x_available,
                    provider_counts_json = excluded.provider_counts_json,
                    flags_json = excluded.flags_json,
                    raw_json = excluded.raw_json
                """,
                records,
            )
        return len(records)

    def replace_research_universe_snapshots(
        self,
        chain_id: str,
        source: str,
        rows: list[dict[str, Any]],
        execution_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in rows:
            token_address = normalize_chain_address(
                chain_id, str(row.get("token_address") or "")
            )
            snapshot_at = normalize_optional_dune_timestamp(row.get("snapshot_at"))
            if not token_address or not snapshot_at:
                continue
            volume_usd = float(row.get("volume_usd") or 0)
            gross_buy_usd = float(row.get("gross_buy_usd") or 0)
            gross_sell_usd = float(row.get("gross_sell_usd") or 0)
            net_flow_usd = row.get("net_flow_usd")
            if net_flow_usd is None:
                net_flow_usd = gross_buy_usd - gross_sell_usd
            trader_count = int(row.get("trader_count") or 0)
            trade_count = int(row.get("trade_count") or 0)
            quality_flags = list(row.get("quality_flags") or [])
            if bool(row.get("is_dense_zero")):
                quality_flags.append("explicit_zero_activity_followup")
            if volume_usd < 5_000:
                quality_flags.append("weekly_volume_below_5000")
            if trader_count < 5:
                quality_flags.append("fewer_than_5_traders")
            if trade_count < 10:
                quality_flags.append("fewer_than_10_trades")
            is_tradeable = row.get("is_tradeable")
            if is_tradeable is None:
                is_tradeable = (
                    volume_usd >= 5_000
                    and trader_count >= 5
                    and trade_count >= 10
                )
            records.append(
                (
                    chain_id,
                    token_address,
                    row.get("token_symbol"),
                    snapshot_at,
                    int(row.get("window_days") or 7),
                    normalize_optional_dune_timestamp(row.get("first_trade_at")),
                    normalize_optional_dune_timestamp(row.get("last_trade_at")),
                    int(row.get("pair_count") or 0),
                    int(row.get("dex_count") or 0),
                    trader_count,
                    int(row.get("buyer_count") or 0),
                    int(row.get("seller_count") or 0),
                    trade_count,
                    int(row.get("buy_trade_count") or 0),
                    int(row.get("sell_trade_count") or 0),
                    gross_buy_usd,
                    gross_sell_usd,
                    float(net_flow_usd),
                    volume_usd,
                    optional_float(row.get("close_price_usd")),
                    optional_float(row.get("vwap_price_usd")),
                    optional_float(row.get("liquidity_usd")),
                    optional_float(row.get("market_cap_usd")),
                    optional_float(row.get("fdv_usd")),
                    int(row["holder_count"]) if row.get("holder_count") is not None else None,
                    optional_bool_int(row.get("website_present")),
                    optional_bool_int(row.get("contract_verified")),
                    optional_float(row.get("risk_score")),
                    int(bool(is_tradeable)),
                    source,
                    execution_id,
                    json.dumps(sorted(set(quality_flags)), sort_keys=True),
                    json.dumps(
                        {
                            **(row.get("raw", {}) or {}),
                            "is_dense_zero": bool(row.get("is_dense_zero")),
                        },
                        sort_keys=True,
                    ),
                    now,
                    now,
                )
            )
        with self.connect() as connection:
            if source.startswith("dune_weekly_universe_v"):
                connection.execute(
                    "DELETE FROM research_universe_snapshots "
                    "WHERE chain_id = ? AND source LIKE 'dune_weekly_universe_v%'",
                    (chain_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM research_universe_snapshots WHERE chain_id = ? AND source = ?",
                    (chain_id, source),
                )
            connection.executemany(
                """
                INSERT INTO research_universe_snapshots (
                    chain_id, token_address, token_symbol, snapshot_at,
                    window_days, first_trade_at, last_trade_at, pair_count,
                    dex_count, trader_count, buyer_count, seller_count,
                    trade_count, buy_trade_count, sell_trade_count,
                    gross_buy_usd, gross_sell_usd, net_flow_usd, volume_usd,
                    close_price_usd, vwap_price_usd, liquidity_usd,
                    market_cap_usd, fdv_usd, holder_count, website_present,
                    contract_verified, risk_score, is_tradeable, source,
                    execution_id, quality_flags_json, raw_json, created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                records,
            )
        return len(records)

    def research_universe_rows(
        self,
        chain_id: str | None = None,
        tradeable_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if chain_id:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        if tradeable_only:
            clauses.append("is_tradeable = 1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM research_universe_snapshots
                {where}
                ORDER BY snapshot_at, chain_id, token_address
                """,
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["quality_flags"] = json.loads(item.pop("quality_flags_json"))
            item["raw"] = json.loads(item.pop("raw_json"))
            output.append(item)
        return output

    def replace_research_token_outcomes(
        self,
        methodology_version: str,
        rows: list[dict[str, Any]],
        chain_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        records = [
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
                row["snapshot_at"],
                methodology_version,
                row.get("listing_at"),
                optional_bool_int(row.get("listed_within_30d")),
                optional_bool_int(row.get("listed_within_60d")),
                optional_bool_int(row.get("listed_within_90d")),
                optional_float(row.get("return_7d")),
                optional_float(row.get("return_30d")),
                optional_float(row.get("return_60d")),
                optional_float(row.get("return_90d")),
                optional_float(row.get("max_favorable_excursion_30d")),
                optional_float(row.get("max_drawdown_30d")),
                optional_float(row.get("max_favorable_excursion_90d")),
                optional_float(row.get("max_drawdown_90d")),
                optional_bool_int(row.get("activity_collapse_30d")),
                optional_bool_int(row.get("liquidity_collapse_30d")),
                optional_bool_int(row.get("rug_proxy_30d")),
                row["labels_matured_through"],
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
                now,
            )
            for row in rows
        ]
        with self.connect() as connection:
            if chain_id:
                connection.execute(
                    """
                    DELETE FROM research_token_outcomes
                    WHERE chain_id = ?
                    """,
                    (chain_id,),
                )
            else:
                connection.execute("DELETE FROM research_token_outcomes")
            connection.executemany(
                """
                INSERT INTO research_token_outcomes (
                    chain_id, token_address, snapshot_at, methodology_version,
                    listing_at, listed_within_30d, listed_within_60d,
                    listed_within_90d, return_7d, return_30d, return_60d,
                    return_90d, max_favorable_excursion_30d,
                    max_drawdown_30d, max_favorable_excursion_90d,
                    max_drawdown_90d, activity_collapse_30d,
                    liquidity_collapse_30d, rug_proxy_30d,
                    labels_matured_through, evidence_json, created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                records,
            )
        return len(records)

    def replace_research_event_coverage(
        self,
        chain_id: str,
        rows: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        records = [
            (
                int(row["listing_event_id"]),
                chain_id,
                normalize_chain_address(chain_id, row["token_address"]),
                row["token_symbol"],
                row["announced_at"],
                int(bool(row.get("pre_announcement_flow_observed"))),
                int(bool(row.get("universe_snapshot_observed"))),
                row.get("first_pre_announcement_trade_at"),
                row.get("last_pre_announcement_trade_at"),
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
            )
            for row in rows
        ]
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM research_event_coverage WHERE chain_id = ?",
                (chain_id,),
            )
            connection.executemany(
                """
                INSERT INTO research_event_coverage (
                    listing_event_id, chain_id, token_address, token_symbol,
                    announced_at, pre_announcement_flow_observed,
                    universe_snapshot_observed,
                    first_pre_announcement_trade_at,
                    last_pre_announcement_trade_at, evidence_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def replace_wallet_token_opportunities(
        self,
        chain_id: str,
        source: str,
        rows: list[dict[str, Any]],
        execution_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in rows:
            first_buy_at = normalize_optional_dune_timestamp(row.get("first_buy_at"))
            token_address = normalize_chain_address(
                chain_id, str(row.get("token_address") or "")
            )
            wallet_address = normalize_chain_address(
                chain_id, str(row.get("wallet_address") or "")
            )
            if not first_buy_at or not token_address or not wallet_address:
                continue
            records.append(
                (
                    chain_id,
                    wallet_address,
                    token_address,
                    row.get("token_symbol"),
                    first_buy_at,
                    normalize_dune_timestamp(row.get("last_buy_at") or first_buy_at),
                    normalize_optional_dune_timestamp(row.get("first_sell_at")),
                    normalize_optional_dune_timestamp(row.get("last_sell_at")),
                    int(row.get("buy_trade_count") or 0),
                    int(row.get("sell_trade_count") or 0),
                    int(row.get("active_day_count") or 0),
                    float(row.get("gross_buy_usd") or 0),
                    float(row.get("gross_sell_usd") or 0),
                    float(row.get("net_cash_flow_usd") or 0),
                    optional_float(row.get("token_bought_amount")),
                    optional_float(row.get("token_sold_amount")),
                    optional_float(row.get("average_buy_price_usd")),
                    optional_float(row.get("average_sell_price_usd")),
                    optional_float(row.get("wallet_observed_buy_usd")),
                    optional_float(row.get("position_to_observed_flow")),
                    optional_float(row.get("turnover_ratio")),
                    row.get("listing_at"),
                    optional_bool_int(row.get("listed_within_30d")),
                    optional_bool_int(row.get("listed_within_60d")),
                    optional_bool_int(row.get("listed_within_90d")),
                    int(bool(row.get("outcome_matured"))),
                    optional_float(row.get("mark_price_usd")),
                    optional_float(row.get("estimated_pnl_usd")),
                    optional_float(row.get("estimated_return")),
                    optional_float(row.get("max_favorable_excursion_90d")),
                    optional_float(row.get("max_drawdown_90d")),
                    optional_float(row.get("holding_days")),
                    optional_float(row.get("exit_quality_score")),
                    source,
                    execution_id,
                    json.dumps(row.get("evidence", {}), sort_keys=True),
                    now,
                    now,
                )
            )
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM wallet_token_opportunities WHERE chain_id = ? AND source = ?",
                (chain_id, source),
            )
            connection.executemany(
                """
                INSERT INTO wallet_token_opportunities (
                    chain_id, wallet_address, token_address, token_symbol,
                    first_buy_at, last_buy_at, first_sell_at, last_sell_at,
                    buy_trade_count, sell_trade_count, active_day_count,
                    gross_buy_usd, gross_sell_usd, net_cash_flow_usd,
                    token_bought_amount, token_sold_amount,
                    average_buy_price_usd, average_sell_price_usd,
                    wallet_observed_buy_usd, position_to_observed_flow,
                    turnover_ratio, listing_at, listed_within_30d,
                    listed_within_60d, listed_within_90d, outcome_matured,
                    mark_price_usd, estimated_pnl_usd, estimated_return,
                    max_favorable_excursion_90d, max_drawdown_90d,
                    holding_days, exit_quality_score, source, execution_id,
                    evidence_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                """,
                records,
            )
        return len(records)

    def wallet_token_opportunity_rows(
        self,
        chain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE chain_id = ?" if chain_id else ""
        params: tuple[Any, ...] = (chain_id,) if chain_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM wallet_token_opportunities
                {where}
                ORDER BY wallet_address, first_buy_at, token_address
                """,
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def replace_wallet_token_weekly_flows(
        self,
        chain_id: str,
        source: str,
        rows: list[dict[str, Any]],
        execution_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in rows:
            wallet_address = normalize_chain_address(
                chain_id, str(row.get("wallet_address") or "")
            )
            token_address = normalize_chain_address(
                chain_id, str(row.get("token_address") or "")
            )
            snapshot_at = normalize_optional_dune_timestamp(row.get("snapshot_at"))
            if not wallet_address or not token_address or not snapshot_at:
                continue
            records.append(
                (
                    chain_id,
                    wallet_address,
                    token_address,
                    row.get("token_symbol"),
                    snapshot_at,
                    float(row.get("gross_buy_usd") or 0),
                    float(row.get("gross_sell_usd") or 0),
                    float(row.get("net_buy_usd") or 0),
                    int(row.get("buy_trade_count") or 0),
                    int(row.get("sell_trade_count") or 0),
                    normalize_optional_dune_timestamp(row.get("first_trade_at")),
                    normalize_optional_dune_timestamp(row.get("last_trade_at")),
                    source,
                    execution_id,
                    now,
                    now,
                )
            )
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM wallet_token_weekly_flows WHERE chain_id = ? AND source = ?",
                (chain_id, source),
            )
            connection.executemany(
                """
                INSERT INTO wallet_token_weekly_flows (
                    chain_id, wallet_address, token_address, token_symbol,
                    snapshot_at, gross_buy_usd, gross_sell_usd, net_buy_usd,
                    buy_trade_count, sell_trade_count, first_trade_at,
                    last_trade_at, source, execution_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def wallet_token_weekly_flow_rows(
        self,
        chain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE chain_id = ?" if chain_id else ""
        params: tuple[Any, ...] = (chain_id,) if chain_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM wallet_token_weekly_flows
                {where}
                ORDER BY snapshot_at, token_address, wallet_address
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_wallet_research_scores(
        self,
        scores: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        records = [
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["wallet_address"]),
                row["model_version"],
                int(row["opportunity_count"]),
                int(row["matured_opportunity_count"]),
                int(row["hit_count_30d"]),
                int(row["hit_count_60d"]),
                int(row["hit_count_90d"]),
                int(row["miss_count_90d"]),
                float(row["baseline_rate"]),
                float(row["posterior_hit_rate"]),
                float(row["posterior_lower_95"]),
                float(row["posterior_upper_95"]),
                optional_float(row.get("posterior_lift")),
                optional_float(row.get("estimated_pnl_usd")),
                optional_float(row.get("median_return")),
                optional_float(row.get("win_rate")),
                optional_float(row.get("median_max_drawdown")),
                optional_float(row.get("median_turnover")),
                optional_float(row.get("median_position_to_flow")),
                optional_float(row.get("median_exit_quality")),
                float(row["research_score"]),
                float(row["confidence_score"]),
                row["label"],
                json.dumps(row.get("rationale", []), sort_keys=True),
                json.dumps(row.get("evidence", {}), sort_keys=True),
                now,
                now,
            )
            for row in scores
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO wallet_research_scores (
                    chain_id, wallet_address, model_version, opportunity_count,
                    matured_opportunity_count, hit_count_30d, hit_count_60d,
                    hit_count_90d, miss_count_90d, baseline_rate,
                    posterior_hit_rate, posterior_lower_95,
                    posterior_upper_95, posterior_lift, estimated_pnl_usd,
                    median_return, win_rate, median_max_drawdown,
                    median_turnover, median_position_to_flow,
                    median_exit_quality, research_score, confidence_score,
                    label, rationale_json, evidence_json, created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(chain_id, wallet_address, model_version)
                DO UPDATE SET
                    opportunity_count = excluded.opportunity_count,
                    matured_opportunity_count = excluded.matured_opportunity_count,
                    hit_count_30d = excluded.hit_count_30d,
                    hit_count_60d = excluded.hit_count_60d,
                    hit_count_90d = excluded.hit_count_90d,
                    miss_count_90d = excluded.miss_count_90d,
                    baseline_rate = excluded.baseline_rate,
                    posterior_hit_rate = excluded.posterior_hit_rate,
                    posterior_lower_95 = excluded.posterior_lower_95,
                    posterior_upper_95 = excluded.posterior_upper_95,
                    posterior_lift = excluded.posterior_lift,
                    estimated_pnl_usd = excluded.estimated_pnl_usd,
                    median_return = excluded.median_return,
                    win_rate = excluded.win_rate,
                    median_max_drawdown = excluded.median_max_drawdown,
                    median_turnover = excluded.median_turnover,
                    median_position_to_flow = excluded.median_position_to_flow,
                    median_exit_quality = excluded.median_exit_quality,
                    research_score = excluded.research_score,
                    confidence_score = excluded.confidence_score,
                    label = excluded.label,
                    rationale_json = excluded.rationale_json,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def wallet_research_score_rows(
        self,
        model_version: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where = "WHERE model_version = ?" if model_version else ""
        params: list[Any] = [model_version] if model_version else []
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM wallet_research_scores
                {where}
                ORDER BY research_score DESC, posterior_lower_95 DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["rationale"] = json.loads(item.pop("rationale_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def replace_wallet_identity_edges(
        self,
        chain_id: str,
        source: str,
        edges: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        records = []
        for row in edges:
            first, second = sorted(
                (
                    str(row["wallet_address_a"]).lower(),
                    str(row["wallet_address_b"]).lower(),
                )
            )
            if first == second:
                continue
            records.append(
                (
                    chain_id,
                    first,
                    second,
                    row["edge_type"],
                    int(row.get("hop_count") or 1),
                    float(row["confidence"]),
                    row.get("first_observed_at"),
                    row.get("last_observed_at"),
                    source,
                    json.dumps(row.get("evidence", {}), sort_keys=True),
                    now,
                    now,
                )
            )
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM wallet_identity_edges WHERE chain_id = ? AND source = ?",
                (chain_id, source),
            )
            connection.executemany(
                """
                INSERT INTO wallet_identity_edges (
                    chain_id, wallet_address_a, wallet_address_b, edge_type,
                    hop_count, confidence, first_observed_at, last_observed_at,
                    source, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def wallet_identity_edge_rows(
        self,
        chain_id: str = "base",
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM wallet_identity_edges
                WHERE chain_id = ?
                ORDER BY confidence DESC, edge_type
                """,
                (chain_id,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def upsert_token_attention_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        records = [
            (
                row["chain_id"],
                row["token_address"].lower(),
                row["observed_at"],
                row["source"],
                row.get("dexscreener_boosts"),
                row.get("dexscreener_ads"),
                row.get("dexscreener_profile"),
                row.get("farcaster_mentions_24h"),
                row.get("farcaster_mentions_7d"),
                row.get("github_commits_30d"),
                row.get("github_contributors_90d"),
                row.get("news_mentions_24h"),
                row.get("news_mentions_7d"),
                optional_float(row.get("onchain_trader_growth_7d")),
                optional_float(row.get("onchain_volume_growth_7d")),
                optional_float(row.get("public_attention_score")),
                optional_float(row.get("onchain_attention_score")),
                optional_float(row.get("attention_gap_score")),
                float(row.get("coverage_score") or 0),
                json.dumps(row.get("flags", []), sort_keys=True),
                json.dumps(row.get("raw", {}), sort_keys=True),
                now,
            )
            for row in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO token_attention_snapshots (
                    chain_id, token_address, observed_at, source,
                    dexscreener_boosts, dexscreener_ads,
                    dexscreener_profile, farcaster_mentions_24h,
                    farcaster_mentions_7d, github_commits_30d,
                    github_contributors_90d, news_mentions_24h,
                    news_mentions_7d, onchain_trader_growth_7d,
                    onchain_volume_growth_7d, public_attention_score,
                    onchain_attention_score, attention_gap_score,
                    coverage_score, flags_json, raw_json, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                ON CONFLICT(chain_id, token_address, observed_at, source)
                DO UPDATE SET
                    dexscreener_boosts = excluded.dexscreener_boosts,
                    dexscreener_ads = excluded.dexscreener_ads,
                    dexscreener_profile = excluded.dexscreener_profile,
                    farcaster_mentions_24h = excluded.farcaster_mentions_24h,
                    farcaster_mentions_7d = excluded.farcaster_mentions_7d,
                    github_commits_30d = excluded.github_commits_30d,
                    github_contributors_90d = excluded.github_contributors_90d,
                    news_mentions_24h = excluded.news_mentions_24h,
                    news_mentions_7d = excluded.news_mentions_7d,
                    onchain_trader_growth_7d = excluded.onchain_trader_growth_7d,
                    onchain_volume_growth_7d = excluded.onchain_volume_growth_7d,
                    public_attention_score = excluded.public_attention_score,
                    onchain_attention_score = excluded.onchain_attention_score,
                    attention_gap_score = excluded.attention_gap_score,
                    coverage_score = excluded.coverage_score,
                    flags_json = excluded.flags_json,
                    raw_json = excluded.raw_json
                """,
                records,
            )
        return len(records)

    def start_research_model_run(
        self,
        model_name: str,
        model_version: str,
        model_family: str,
        target_label: str,
        feature_names: list[str],
        config: dict[str, Any],
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO research_model_runs (
                    model_name, model_version, model_family, target_label,
                    status, feature_names_json, config_json, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    model_name,
                    model_version,
                    model_family,
                    target_label,
                    json.dumps(feature_names, sort_keys=True),
                    json.dumps(config, sort_keys=True),
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def finish_research_model_run(
        self,
        run_id: int,
        status: str,
        metrics: dict[str, Any],
        feature_importance: dict[str, Any],
        leakage_checks: dict[str, Any],
        train_bounds: tuple[str | None, str | None],
        validation_bounds: tuple[str | None, str | None],
        artifact_path: str | None = None,
        notes: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE research_model_runs
                SET status = ?, train_start_at = ?, train_end_at = ?,
                    validation_start_at = ?, validation_end_at = ?,
                    metrics_json = ?, feature_importance_json = ?,
                    leakage_checks_json = ?, artifact_path = ?,
                    finished_at = ?, notes = ?
                WHERE research_model_run_id = ?
                """,
                (
                    status,
                    train_bounds[0],
                    train_bounds[1],
                    validation_bounds[0],
                    validation_bounds[1],
                    json.dumps(metrics, sort_keys=True),
                    json.dumps(feature_importance, sort_keys=True),
                    json.dumps(leakage_checks, sort_keys=True),
                    artifact_path,
                    utc_now_iso(),
                    notes,
                    run_id,
                ),
            )

    def insert_research_model_predictions(
        self,
        run_id: int,
        predictions: list[dict[str, Any]],
    ) -> int:
        records = [
            (
                run_id,
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
                row["snapshot_at"],
                row["target_label"],
                row.get("actual_label"),
                float(row["probability"]),
                row.get("rank_at_snapshot"),
                row["split"],
                json.dumps(row.get("feature_values", {}), sort_keys=True),
                json.dumps(row.get("explanation", {}), sort_keys=True),
            )
            for row in predictions
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO research_model_predictions (
                    research_model_run_id, chain_id, token_address,
                    snapshot_at, target_label, actual_label, probability,
                    rank_at_snapshot, split, feature_values_json,
                    explanation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def research_model_run_rows(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM research_model_runs
                ORDER BY research_model_run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            for source_key, target_key in (
                ("feature_names_json", "feature_names"),
                ("config_json", "config"),
                ("metrics_json", "metrics"),
                ("feature_importance_json", "feature_importance"),
                ("leakage_checks_json", "leakage_checks"),
            ):
                item[target_key] = json.loads(item.pop(source_key) or "{}")
            output.append(item)
        return output

    def research_training_rows(
        self,
        methodology_version: str,
        chain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        chain_filter = "AND u.chain_id = ?" if chain_id else ""
        params: tuple[Any, ...] = (
            (methodology_version, chain_id)
            if chain_id
            else (methodology_version,)
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    u.*,
                    o.listing_at,
                    o.listed_within_30d,
                    o.listed_within_60d,
                    o.listed_within_90d,
                    o.return_7d,
                    o.return_30d,
                    o.return_60d,
                    o.return_90d,
                    o.max_favorable_excursion_30d,
                    o.max_drawdown_30d,
                    o.max_favorable_excursion_90d,
                    o.max_drawdown_90d,
                    o.activity_collapse_30d,
                    o.liquidity_collapse_30d,
                    o.rug_proxy_30d,
                    o.evidence_json AS outcome_evidence_json
                FROM research_universe_snapshots u
                JOIN research_token_outcomes o
                  ON o.chain_id = u.chain_id
                 AND o.token_address = u.token_address
                 AND o.snapshot_at = u.snapshot_at
                WHERE o.methodology_version = ?
                  {chain_filter}
                ORDER BY u.snapshot_at, u.chain_id, u.token_address
                """,
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["quality_flags"] = json.loads(item.pop("quality_flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            item["outcome_evidence"] = json.loads(
                item.pop("outcome_evidence_json") or "{}"
            )
            output.append(item)
        return output

    def research_model_prediction_rows(
        self,
        run_id: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM research_model_predictions
                WHERE research_model_run_id = ?
                ORDER BY snapshot_at DESC, rank_at_snapshot, probability DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["feature_values"] = json.loads(item.pop("feature_values_json") or "{}")
            item["explanation"] = json.loads(item.pop("explanation_json") or "{}")
            output.append(item)
        return output

    def research_dashboard(self) -> dict[str, Any]:
        with self.connect() as connection:
            universe = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS snapshot_count,
                        COUNT(DISTINCT chain_id || ':' || token_address) AS token_count,
                        COUNT(DISTINCT snapshot_at) AS date_count,
                        SUM(is_tradeable) AS tradeable_snapshot_count,
                        MIN(snapshot_at) AS first_snapshot_at,
                        MAX(snapshot_at) AS last_snapshot_at
                    FROM research_universe_snapshots
                    """
                ).fetchone()
            )
            outcomes = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS outcome_count,
                        SUM(CASE WHEN listed_within_90d IS NOT NULL THEN 1 ELSE 0 END) AS matured_90d_snapshot_count,
                        SUM(CASE WHEN listed_within_90d = 1 THEN 1 ELSE 0 END) AS positive_90d_count,
                        SUM(CASE WHEN listed_within_90d = 0 THEN 1 ELSE 0 END) AS negative_90d_count,
                        SUM(CASE WHEN rug_proxy_30d = 1 THEN 1 ELSE 0 END) AS rug_proxy_count,
                        COUNT(DISTINCT CASE
                            WHEN listed_within_90d = 1
                            THEN chain_id || ':' || token_address || ':' || COALESCE(listing_at, '')
                        END) AS independent_positive_event_count
                    FROM research_token_outcomes
                    """
                ).fetchone()
            )
            event_coverage = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS event_count,
                        SUM(pre_announcement_flow_observed) AS events_with_flow,
                        SUM(CASE WHEN pre_announcement_flow_observed = 0 THEN 1 ELSE 0 END) AS zero_flow_events,
                        SUM(universe_snapshot_observed) AS events_in_universe
                    FROM research_event_coverage
                    """
                ).fetchone()
            )
            wallets = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS opportunity_count,
                        COUNT(DISTINCT wallet_address) AS wallet_count,
                        SUM(outcome_matured) AS matured_opportunity_count,
                        SUM(CASE WHEN listed_within_90d = 1 THEN 1 ELSE 0 END) AS wallet_hit_count
                    FROM wallet_token_opportunities
                    """
                ).fetchone()
            )
            attention = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS attention_snapshot_count,
                        COUNT(DISTINCT token_address) AS attention_token_count,
                        MAX(observed_at) AS latest_attention_at
                    FROM token_attention_snapshots
                    """
                ).fetchone()
            )
            universe_by_chain = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        chain_id,
                        COUNT(*) AS snapshot_count,
                        COUNT(DISTINCT token_address) AS token_count,
                        MIN(snapshot_at) AS first_snapshot_at,
                        MAX(snapshot_at) AS last_snapshot_at
                    FROM research_universe_snapshots
                    GROUP BY chain_id
                    ORDER BY snapshot_count DESC
                    """
                ).fetchall()
            ]
            wallet_labels = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT label, COUNT(*) AS wallet_count
                    FROM wallet_research_scores
                    WHERE model_version = 'wallet_research_v2'
                    GROUP BY label
                    ORDER BY wallet_count DESC
                    """
                ).fetchall()
            ]
            signal_types = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT signal_type, status, COUNT(*) AS signal_count
                    FROM signals
                    WHERE detected_at = (SELECT MAX(detected_at) FROM signals)
                    GROUP BY signal_type, status
                    ORDER BY signal_count DESC
                    """
                ).fetchall()
            ]
        model_runs = self.research_model_run_rows(limit=10)
        snapshot_count = int(universe.get("snapshot_count") or 0)
        positive_events = int(outcomes.get("independent_positive_event_count") or 0)
        return {
            "universe": universe,
            "universe_by_chain": universe_by_chain,
            "outcomes": outcomes,
            "event_coverage": event_coverage,
            "wallets": wallets,
            "wallet_labels": wallet_labels,
            "attention": attention,
            "signal_types": signal_types,
            "model_runs": model_runs,
            "wallet_scores": self.wallet_research_score_rows(limit=100),
            "readiness": {
                "minimum_snapshot_count": 100_000,
                "minimum_independent_positive_events": 100,
                "snapshot_progress": min(1.0, snapshot_count / 100_000),
                "event_progress": min(1.0, positive_events / 100),
                "dataset_gate_passed": snapshot_count >= 100_000
                and positive_events >= 100,
                "llm_used_for_prediction": False,
            },
        }

    def upsert_signal_shadow_marks(self, rows: list[dict[str, Any]]) -> int:
        created_at = utc_now_iso()
        records = [
            (
                int(row["signal_id"]),
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
                row["observed_at"],
                float(row.get("horizon_hours") or 0),
                optional_float(row.get("entry_price_usd")),
                optional_float(row.get("mark_price_usd")),
                optional_float(row.get("entry_liquidity_usd")),
                optional_float(row.get("mark_liquidity_usd")),
                optional_float(row.get("gross_return")),
                optional_float(row.get("estimated_round_trip_slippage_bps")),
                optional_float(row.get("net_return_after_slippage")),
                row.get("source", "market_snapshot_shadow_v1"),
                json.dumps(row.get("evidence", {}), sort_keys=True),
                created_at,
            )
            for row in rows
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO signal_shadow_marks (
                    signal_id, chain_id, token_address, observed_at,
                    horizon_hours, entry_price_usd, mark_price_usd,
                    entry_liquidity_usd, mark_liquidity_usd, gross_return,
                    estimated_round_trip_slippage_bps, net_return_after_slippage,
                    source, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id, observed_at, source)
                DO UPDATE SET
                    horizon_hours = excluded.horizon_hours,
                    mark_price_usd = excluded.mark_price_usd,
                    mark_liquidity_usd = excluded.mark_liquidity_usd,
                    gross_return = excluded.gross_return,
                    estimated_round_trip_slippage_bps = excluded.estimated_round_trip_slippage_bps,
                    net_return_after_slippage = excluded.net_return_after_slippage,
                    evidence_json = excluded.evidence_json
                """,
                records,
            )
        return len(records)

    def signal_shadow_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS mark_count,
                        COUNT(DISTINCT signal_id) AS signal_count,
                        MIN(observed_at) AS first_mark_at,
                        MAX(observed_at) AS last_mark_at,
                        AVG(net_return_after_slippage) AS average_net_return,
                        SUM(CASE WHEN net_return_after_slippage > 0 THEN 1 ELSE 0 END) AS positive_mark_count
                    FROM signal_shadow_marks
                    """
                ).fetchone()
            )
        first = normalize_optional_dune_timestamp(row.get("first_mark_at"))
        last = normalize_optional_dune_timestamp(row.get("last_mark_at"))
        history_days = 0.0
        if first and last:
            history_days = max(
                0.0,
                (
                    datetime.fromisoformat(last)
                    - datetime.fromisoformat(first)
                ).total_seconds()
                / 86_400,
            )
        row["history_days"] = history_days
        return row

    def start_funding_scan(self, config: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO funding_scans (status, started_at, config_json)
                VALUES ('running', ?, ?)
                """,
                (utc_now_iso(), json.dumps(config, sort_keys=True)),
            )
            return int(cursor.lastrowid)

    def finish_funding_scan(
        self,
        scan_id: int,
        status: str,
        *,
        instrument_count: int = 0,
        market_snapshot_count: int = 0,
        orderbook_count: int = 0,
        history_row_count: int = 0,
        route_count: int = 0,
        paper_candidate_count: int = 0,
        paper_execution_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_scans
                SET status = ?,
                    finished_at = ?,
                    instrument_count = ?,
                    market_snapshot_count = ?,
                    orderbook_count = ?,
                    history_row_count = ?,
                    route_count = ?,
                    paper_candidate_count = ?,
                    paper_execution_count = ?,
                    error = ?
                WHERE funding_scan_id = ?
                """,
                (
                    status,
                    utc_now_iso(),
                    instrument_count,
                    market_snapshot_count,
                    orderbook_count,
                    history_row_count,
                    route_count,
                    paper_candidate_count,
                    paper_execution_count,
                    error,
                    scan_id,
                ),
            )

    def insert_funding_scan_warnings(
        self,
        scan_id: int,
        warnings: list[str],
    ) -> int:
        unique = list(dict.fromkeys(str(warning) for warning in warnings if warning))
        if not unique:
            return 0
        now = utc_now_iso()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_scan_warnings (
                    funding_scan_id, warning, created_at
                ) VALUES (?, ?, ?)
                """,
                [(scan_id, warning, now) for warning in unique],
            )
        return len(unique)

    def upsert_funding_instruments(self, rows: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_instruments (
                    venue, symbol, canonical_asset, base_asset, quote_asset,
                    collateral_asset, contract_type, contract_multiplier,
                    status, source_url, observed_at, raw_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, symbol) DO UPDATE SET
                    canonical_asset = excluded.canonical_asset,
                    base_asset = excluded.base_asset,
                    quote_asset = excluded.quote_asset,
                    collateral_asset = excluded.collateral_asset,
                    contract_type = excluded.contract_type,
                    contract_multiplier = excluded.contract_multiplier,
                    status = excluded.status,
                    source_url = excluded.source_url,
                    observed_at = excluded.observed_at,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        row["venue"],
                        row["symbol"],
                        row["canonical_asset"],
                        row["base_asset"],
                        row["quote_asset"],
                        row["collateral_asset"],
                        row["contract_type"],
                        row.get("contract_multiplier", 1),
                        row["status"],
                        row.get("source_url"),
                        row["observed_at"],
                        json.dumps(row.get("raw", {}), sort_keys=True),
                        now,
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def insert_funding_market_snapshots(
        self,
        scan_id: int,
        rows: list[dict[str, Any]],
        *,
        include_raw_json: bool = True,
    ) -> int:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_market_snapshots (
                    funding_scan_id, venue, symbol, canonical_asset,
                    funding_rate, funding_interval_hours, hourly_funding_rate,
                    funding_rate_kind, next_funding_at, mark_price, index_price,
                    open_interest_usd, volume_24h_usd, observed_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_id,
                        row["venue"],
                        row["symbol"],
                        row["canonical_asset"],
                        row["funding_rate"],
                        row["funding_interval_hours"],
                        row["hourly_funding_rate"],
                        row["funding_rate_kind"],
                        row.get("next_funding_at"),
                        row.get("mark_price"),
                        row.get("index_price"),
                        row.get("open_interest_usd"),
                        row.get("volume_24h_usd"),
                        row["observed_at"],
                        json.dumps(
                            row.get("raw", {}) if include_raw_json else {},
                            sort_keys=True,
                        ),
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def latest_funding_catalog_by_venue(
        self,
        venues: list[str],
        min_observed_at: str,
    ) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]]:
        """Return recent normalized market snapshots without external requests."""
        cached: dict[
            str,
            tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]],
        ] = {}
        with self.connect() as connection:
            for venue in dict.fromkeys(venues):
                scan = connection.execute(
                    """
                    SELECT market.funding_scan_id,
                           MAX(market.observed_at) AS observed_at,
                           COUNT(*) AS market_count
                    FROM funding_market_snapshots market
                    JOIN funding_scans scan
                      ON scan.funding_scan_id = market.funding_scan_id
                     AND scan.status = 'success'
                    WHERE market.venue = ?
                      AND market.observed_at >= ?
                    GROUP BY market.funding_scan_id
                    ORDER BY observed_at DESC, market.funding_scan_id DESC
                    LIMIT 1
                    """,
                    (venue, min_observed_at),
                ).fetchone()
                if scan is None or int(scan["market_count"] or 0) <= 0:
                    continue
                scan_id = int(scan["funding_scan_id"])
                market_rows = connection.execute(
                    """
                    SELECT venue, symbol, canonical_asset, funding_rate,
                           funding_interval_hours, hourly_funding_rate,
                           funding_rate_kind, next_funding_at, mark_price,
                           index_price, open_interest_usd, volume_24h_usd,
                           observed_at, raw_json
                    FROM funding_market_snapshots
                    WHERE funding_scan_id = ? AND venue = ?
                    ORDER BY symbol
                    """,
                    (scan_id, venue),
                ).fetchall()
                instrument_rows = connection.execute(
                    """
                    SELECT instrument.venue, instrument.symbol,
                           instrument.canonical_asset, instrument.base_asset,
                           instrument.quote_asset, instrument.collateral_asset,
                           instrument.contract_type,
                           instrument.contract_multiplier, instrument.status,
                           instrument.source_url, instrument.observed_at,
                           instrument.raw_json
                    FROM funding_instruments instrument
                    JOIN funding_market_snapshots market
                      ON market.venue = instrument.venue
                     AND market.symbol = instrument.symbol
                    WHERE market.funding_scan_id = ?
                      AND market.venue = ?
                    ORDER BY instrument.symbol
                    """,
                    (scan_id, venue),
                ).fetchall()
                instruments: list[dict[str, Any]] = []
                unit_multipliers: dict[tuple[str, str], float] = {}
                for row in instrument_rows:
                    item = dict(row)
                    item["raw"] = json.loads(item.pop("raw_json") or "{}")
                    multiplier = funding_unit_multiplier(item)
                    item["canonical_unit_multiplier"] = multiplier
                    unit_multipliers[(item["venue"], item["symbol"])] = multiplier
                    instruments.append(item)
                markets: list[dict[str, Any]] = []
                for row in market_rows:
                    item = dict(row)
                    item["raw"] = json.loads(item.pop("raw_json") or "{}")
                    item["canonical_unit_multiplier"] = unit_multipliers.get(
                        (item["venue"], item["symbol"]),
                        1.0,
                    )
                    markets.append(item)
                cached[venue] = (instruments, markets, [])
        return cached

    def latest_funding_market_with_instrument(
        self,
        venue: str,
        symbol: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT market.venue, market.symbol, market.canonical_asset,
                       market.funding_rate, market.funding_interval_hours,
                       market.hourly_funding_rate, market.funding_rate_kind,
                       market.next_funding_at, market.mark_price,
                       market.index_price, market.open_interest_usd,
                       market.volume_24h_usd, market.observed_at,
                       market.raw_json,
                       instrument.contract_multiplier,
                       instrument.source_url,
                       instrument.raw_json AS instrument_raw_json
                FROM funding_market_snapshots market
                LEFT JOIN funding_instruments instrument
                  ON instrument.venue = market.venue
                 AND instrument.symbol = market.symbol
                WHERE market.venue = ? AND market.symbol = ?
                ORDER BY market.observed_at DESC,
                         market.funding_market_snapshot_id DESC
                LIMIT 1
                """,
                (str(venue), str(symbol)),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        instrument_raw = json.loads(item.pop("instrument_raw_json") or "{}")
        item["instrument_raw"] = instrument_raw
        item["canonical_unit_multiplier"] = funding_unit_multiplier(
            {
                "venue": item["venue"],
                "symbol": item["symbol"],
                "canonical_asset": item["canonical_asset"],
                "contract_multiplier": item.get("contract_multiplier") or 1.0,
                "raw": instrument_raw,
            }
        )
        return item

    def funding_market_snapshot_rows(
        self,
        venue: str,
        symbol: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT venue, symbol, canonical_asset, funding_rate,
                       funding_interval_hours, hourly_funding_rate,
                       funding_rate_kind, next_funding_at, mark_price,
                       index_price, open_interest_usd, volume_24h_usd,
                       observed_at, raw_json
                FROM funding_market_snapshots
                WHERE venue = ? AND symbol = ?
                ORDER BY observed_at DESC, funding_market_snapshot_id DESC
                LIMIT ?
                """,
                (str(venue), str(symbol), int(limit)),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            output.append(item)
        return output

    def insert_funding_orderbooks(
        self,
        scan_id: int,
        rows: list[dict[str, Any]],
        *,
        include_raw_json: bool = True,
    ) -> int:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_orderbook_snapshots (
                    funding_scan_id, venue, symbol, observed_at,
                    bids_json, asks_json, best_bid, best_ask, mid_price,
                    bid_depth_usd, ask_depth_usd, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_id,
                        row["venue"],
                        row["symbol"],
                        row["observed_at"],
                        json.dumps(row.get("bids", [])),
                        json.dumps(row.get("asks", [])),
                        row.get("best_bid"),
                        row.get("best_ask"),
                        row.get("mid_price"),
                        row.get("bid_depth_usd", 0),
                        row.get("ask_depth_usd", 0),
                        json.dumps(
                            row.get("raw", {}) if include_raw_json else {},
                            sort_keys=True,
                        ),
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def funding_orderbook_sequences(
        self,
        markets: list[tuple[str, str]],
        limit_per_market: int = 60,
        before_at: str | None = None,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Return recent L2 snapshots in chronological order for each market."""
        limit = max(1, min(int(limit_per_market), 1_000))
        sequences: dict[tuple[str, str], list[dict[str, Any]]] = {}
        with self.connect() as connection:
            for venue, symbol in dict.fromkeys(markets):
                if before_at:
                    rows = connection.execute(
                        """
                        SELECT venue, symbol, observed_at, bids_json, asks_json,
                               best_bid, best_ask, mid_price,
                               bid_depth_usd, ask_depth_usd
                        FROM funding_orderbook_snapshots
                        WHERE venue = ? AND symbol = ? AND observed_at < ?
                        ORDER BY observed_at DESC, funding_orderbook_snapshot_id DESC
                        LIMIT ?
                        """,
                        (venue, symbol, before_at, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT venue, symbol, observed_at, bids_json, asks_json,
                               best_bid, best_ask, mid_price,
                               bid_depth_usd, ask_depth_usd
                        FROM funding_orderbook_snapshots
                        WHERE venue = ? AND symbol = ?
                        ORDER BY observed_at DESC, funding_orderbook_snapshot_id DESC
                        LIMIT ?
                        """,
                        (venue, symbol, limit),
                    ).fetchall()
                decoded: list[dict[str, Any]] = []
                for row in reversed(rows):
                    item = dict(row)
                    item["bids"] = json.loads(item.pop("bids_json") or "[]")
                    item["asks"] = json.loads(item.pop("asks_json") or "[]")
                    decoded.append(item)
                sequences[(venue, symbol)] = decoded
        return sequences

    def latest_funding_orderbooks(
        self,
        markets: list[tuple[str, str]],
        min_observed_at: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Return one recent true L2 snapshot per market for live cache reuse."""
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        with self.connect() as connection:
            for venue, symbol in dict.fromkeys(markets):
                row = connection.execute(
                    """
                    SELECT venue, symbol, observed_at, bids_json, asks_json,
                           best_bid, best_ask, mid_price,
                           bid_depth_usd, ask_depth_usd, raw_json
                    FROM funding_orderbook_snapshots
                    WHERE venue = ? AND symbol = ? AND observed_at >= ?
                    ORDER BY observed_at DESC, funding_orderbook_snapshot_id DESC
                    LIMIT 1
                    """,
                    (venue, symbol, min_observed_at),
                ).fetchone()
                if row is None:
                    continue
                item = dict(row)
                item["bids"] = json.loads(item.pop("bids_json") or "[]")
                item["asks"] = json.loads(item.pop("asks_json") or "[]")
                item["raw"] = json.loads(item.pop("raw_json") or "{}")
                latest[(venue, symbol)] = item
        return latest

    def insert_funding_route_universe(
        self,
        scan_id: int,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_route_universe (
                    funding_scan_id, canonical_asset,
                    long_venue, long_symbol, short_venue, short_symbol,
                    current_hourly_spread, quick_gross_rate,
                    quick_taker_cost_rate, quick_maker_cost_rate,
                    quick_best_case_net_rate, quick_schedule_ready,
                    execution_eligible, execution_screen_reason, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_id,
                        row["canonical_asset"],
                        row["long_venue"],
                        row["long_symbol"],
                        row["short_venue"],
                        row["short_symbol"],
                        row["current_hourly_spread"],
                        row["quick_gross_rate"],
                        row["quick_taker_cost_rate"],
                        row["quick_maker_cost_rate"],
                        row["quick_best_case_net_rate"],
                        int(bool(row["quick_schedule_ready"])),
                        int(bool(row["execution_eligible"])),
                        row["execution_screen_reason"],
                        row["observed_at"],
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def funding_history_is_fresh(
        self,
        venue: str,
        symbol: str,
        maximum_age_hours: float,
        required_start_at: str | None = None,
    ) -> bool:
        with self.connect() as connection:
            if required_start_at:
                sync_row = connection.execute(
                    """
                    SELECT requested_start_at, fetched_at
                    FROM funding_history_sync_state
                    WHERE venue = ? AND symbol = ?
                    """,
                    (venue, symbol),
                ).fetchone()
                if not sync_row:
                    return False
                try:
                    requested = datetime.fromisoformat(
                        str(sync_row["requested_start_at"]).replace("Z", "+00:00")
                    )
                    required = datetime.fromisoformat(
                        str(required_start_at).replace("Z", "+00:00")
                    )
                    fetched = datetime.fromisoformat(
                        str(sync_row["fetched_at"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    return False
                requested = (
                    requested.replace(tzinfo=UTC)
                    if requested.tzinfo is None
                    else requested.astimezone(UTC)
                )
                required = (
                    required.replace(tzinfo=UTC)
                    if required.tzinfo is None
                    else required.astimezone(UTC)
                )
                fetched = (
                    fetched.replace(tzinfo=UTC)
                    if fetched.tzinfo is None
                    else fetched.astimezone(UTC)
                )
                return (
                    requested <= required
                    and (datetime.now(UTC) - fetched).total_seconds()
                    <= maximum_age_hours * 3600
                )
            row = connection.execute(
                """
                SELECT MAX(observed_at) AS observed_at
                FROM funding_rate_history
                WHERE venue = ? AND symbol = ?
                """,
                (venue, symbol),
            ).fetchone()
        if not row or not row["observed_at"]:
            return False
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        return (datetime.now(UTC) - observed).total_seconds() <= maximum_age_hours * 3600

    def funding_history_latest_at_map(
        self,
        markets: list[tuple[str, str]],
    ) -> dict[tuple[str, str], str]:
        output: dict[tuple[str, str], str] = {}
        with self.connect() as connection:
            for venue, symbol in dict.fromkeys(markets):
                row = connection.execute(
                    """
                    SELECT MAX(funding_at) AS funding_at
                    FROM funding_rate_history
                    WHERE venue = ? AND symbol = ?
                    """,
                    (venue, symbol),
                ).fetchone()
                if row and row["funding_at"]:
                    output[(venue, symbol)] = str(row["funding_at"])
        return output

    def record_funding_history_sync(
        self,
        venue: str,
        symbol: str,
        requested_start_at: str,
        fetched_at: str,
        rows: list[dict[str, Any]],
    ) -> None:
        funding_times = sorted(
            str(row["funding_at"])
            for row in rows
            if row.get("funding_at")
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_history_sync_state (
                    venue, symbol, requested_start_at, fetched_at,
                    row_count, earliest_funding_at, latest_funding_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, symbol) DO UPDATE SET
                    requested_start_at = CASE
                        WHEN excluded.requested_start_at < requested_start_at
                        THEN excluded.requested_start_at
                        ELSE requested_start_at
                    END,
                    fetched_at = excluded.fetched_at,
                    row_count = excluded.row_count,
                    earliest_funding_at = excluded.earliest_funding_at,
                    latest_funding_at = excluded.latest_funding_at
                """,
                (
                    venue,
                    symbol,
                    requested_start_at,
                    fetched_at,
                    len(rows),
                    funding_times[0] if funding_times else None,
                    funding_times[-1] if funding_times else None,
                ),
            )

    def funding_history_sync_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS market_count,
                    SUM(row_count) AS fetched_row_count,
                    MIN(requested_start_at) AS earliest_requested_start_at,
                    MAX(fetched_at) AS latest_fetched_at
                FROM funding_history_sync_state
                """
            ).fetchone()
        return dict(row) if row else {}

    def upsert_funding_history(self, rows: list[dict[str, Any]]) -> int:
        prepared_rows: list[tuple[Any, ...]] = []
        for row in rows:
            raw_payload = dict(row.get("raw", {}) or {})
            explicit_semantics = (
                row.get("rate_semantics")
                or row.get("funding_rate_semantics")
                or raw_payload.get("rate_semantics")
                or raw_payload.get("funding_rate_semantics")
            )
            raw_payload["rate_semantics"] = str(explicit_semantics or "unclear")
            explicit_source = (
                row.get("rate_semantics_source")
                or row.get("funding_rate_semantics_source")
                or raw_payload.get("rate_semantics_source")
                or raw_payload.get("funding_rate_semantics_source")
            )
            raw_payload["rate_semantics_source"] = str(explicit_source or "unknown")
            prepared_rows.append(
                (
                    row["venue"],
                    row["symbol"],
                    row["funding_at"],
                    row["funding_rate"],
                    row["funding_interval_hours"],
                    row["hourly_funding_rate"],
                    row.get("mark_price"),
                    row["observed_at"],
                    json.dumps(raw_payload, sort_keys=True),
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_rate_history (
                    venue, symbol, funding_at, funding_rate,
                    funding_interval_hours, hourly_funding_rate,
                    mark_price, observed_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, symbol, funding_at) DO UPDATE SET
                    funding_rate = excluded.funding_rate,
                    funding_interval_hours = excluded.funding_interval_hours,
                    hourly_funding_rate = excluded.hourly_funding_rate,
                    mark_price = excluded.mark_price,
                    observed_at = excluded.observed_at,
                    raw_json = excluded.raw_json
                """,
                prepared_rows,
            )
        return len(rows)

    def funding_history_rows(
        self,
        venue: str,
        symbol: str,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT venue, symbol, funding_at, funding_rate,
                   funding_interval_hours, hourly_funding_rate,
                   mark_price, observed_at, raw_json
            FROM funding_rate_history
            WHERE venue = ? AND symbol = ?
        """
        params: list[Any] = [venue, symbol]
        if since:
            query += " AND funding_at >= ?"
            params.append(since)
        query += " ORDER BY funding_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            output.append(item)
        return output

    def insert_funding_routes(
        self,
        scan_id: int,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        output = []
        with self.connect() as connection:
            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO funding_routes (
                        funding_scan_id, route_key, route_type, canonical_asset,
                        venue_scope, long_venue, long_symbol, short_venue,
                        short_symbol, status, confidence_score, target_notional,
                        market_capacity, capital_required, horizon_days,
                        current_hourly_spread, current_gross_apr,
                        projected_hourly_spread, projected_gross_apr,
                        historical_median_hourly_spread,
                        positive_spread_fraction, persistence_score,
                        history_point_count, expected_gross_funding,
                        expected_net_profit, net_roc_annualized, total_fees,
                        slippage_cost, basis_gap, basis_reserve,
                        operations_buffer, long_next_funding_at,
                        short_next_funding_at, observed_at, legs_json,
                        rationale_json, risk_flags_json, evidence_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        row["route_key"],
                        row["route_type"],
                        row["canonical_asset"],
                        row["venue_scope"],
                        row["long_venue"],
                        row["long_symbol"],
                        row["short_venue"],
                        row["short_symbol"],
                        row["status"],
                        row["confidence_score"],
                        row["target_notional"],
                        row["market_capacity"],
                        row["capital_required"],
                        row["horizon_days"],
                        row["current_hourly_spread"],
                        row["current_gross_apr"],
                        row["projected_hourly_spread"],
                        row["projected_gross_apr"],
                        row["historical_median_hourly_spread"],
                        row["positive_spread_fraction"],
                        row["persistence_score"],
                        row["history_point_count"],
                        row["expected_gross_funding"],
                        row["expected_net_profit"],
                        row["net_roc_annualized"],
                        row["total_fees"],
                        row["slippage_cost"],
                        row["basis_gap"],
                        row["basis_reserve"],
                        row["operations_buffer"],
                        row.get("long_next_funding_at"),
                        row.get("short_next_funding_at"),
                        row["observed_at"],
                        json.dumps(row.get("legs", [])),
                        json.dumps(row.get("rationale", [])),
                        json.dumps(row.get("risk_flags", [])),
                        json.dumps(row.get("evidence", {}), sort_keys=True),
                    ),
                )
                output.append({**row, "funding_route_id": int(cursor.lastrowid)})
        return output

    def previous_funding_routes(
        self,
        before_scan_id: int,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            previous = connection.execute(
                """
                SELECT funding_scan_id
                FROM funding_scans
                WHERE funding_scan_id < ? AND status = 'success'
                  AND COALESCE(
                      json_extract(config_json, '$.horizon_mode'),
                      'fixed'
                  ) <> 'research'
                ORDER BY funding_scan_id DESC
                LIMIT 1
                """,
                (before_scan_id,),
            ).fetchone()
            if not previous:
                return []
            rows = connection.execute(
                """
                SELECT *
                FROM funding_routes
                WHERE funding_scan_id = ? AND status = 'paper_candidate'
                ORDER BY CAST(json_extract(
                             evidence_json,
                             '$.current_nowcast_net'
                         ) AS REAL) DESC,
                         CAST(json_extract(
                             evidence_json,
                             '$.conservative_net_profit'
                         ) AS REAL) DESC,
                         expected_net_profit DESC
                """,
                (int(previous["funding_scan_id"]),),
            ).fetchall()
        return [decode_funding_route(dict(row)) for row in rows]

    def insert_funding_paper_executions(
        self,
        scan_id: int,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        now = utc_now_iso()
        with self.connect() as connection:
            route_ids = [
                int(row["funding_route_id"])
                for row in rows
                if row.get("funding_route_id") is not None
            ]
            existing_route_ids: set[int] = set()
            if route_ids:
                placeholders = ",".join("?" for _ in route_ids)
                existing_route_ids = {
                    int(row["funding_route_id"])
                    for row in connection.execute(
                        f"""
                        SELECT funding_route_id
                        FROM funding_routes
                        WHERE funding_route_id IN ({placeholders})
                        """,
                        route_ids,
                    )
                }
            insert_rows = [
                row
                for row in rows
                if row.get("funding_route_id") is not None
                and int(row["funding_route_id"]) in existing_route_ids
            ]
            if not insert_rows:
                return 0
            connection.executemany(
                """
                INSERT INTO funding_paper_executions (
                    funding_route_id, funding_scan_id, model_version, status,
                    created_at, requested_notional, filled_notional,
                    fill_ratio, latency_ms, expected_net_profit,
                    repriced_net_profit, result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(funding_route_id, funding_scan_id, model_version)
                DO UPDATE SET
                    status = excluded.status,
                    created_at = excluded.created_at,
                    requested_notional = excluded.requested_notional,
                    filled_notional = excluded.filled_notional,
                    fill_ratio = excluded.fill_ratio,
                    latency_ms = excluded.latency_ms,
                    expected_net_profit = excluded.expected_net_profit,
                    repriced_net_profit = excluded.repriced_net_profit,
                    result_json = excluded.result_json
                """,
                [
                    (
                        row["funding_route_id"],
                        scan_id,
                        row["model_version"],
                        row["status"],
                        now,
                        row["requested_notional"],
                        row["filled_notional"],
                        row["fill_ratio"],
                        row["latency_ms"],
                        row.get("expected_net_profit"),
                        row.get("repriced_net_profit"),
                        json.dumps(row.get("result", {}), sort_keys=True),
                    )
                    for row in insert_rows
                ],
            )
        return len(insert_rows)

    def funding_dashboard(
        self,
        horizon_mode: str | None = None,
        horizon_hours: float | None = None,
        include_watch_scans: bool = False,
    ) -> dict[str, Any]:
        normalized_mode = str(horizon_mode or "").strip().lower()
        requested_hours = float(horizon_hours or 0.0)
        scan_filters = [
            "status = 'success'",
            "COALESCE(json_extract(config_json, '$.horizon_mode'), 'fixed') <> 'research'",
        ]
        if not include_watch_scans:
            scan_filters.append(
                "COALESCE(json_extract(config_json, '$.scan_mode'), 'manual') <> 'watch'"
            )
        scan_params: list[Any] = []
        if normalized_mode == "next_settlement":
            scan_filters.append(
                "json_extract(config_json, '$.horizon_mode') = 'next_settlement'"
            )
        elif normalized_mode == "fixed":
            scan_filters.append(
                "json_extract(config_json, '$.horizon_mode') = 'fixed'"
            )
            scan_filters.append(
                "ABS(COALESCE(CAST(json_extract(config_json, '$.horizon_hours') AS REAL), 24.0) - ?) < 0.001"
            )
            scan_params.append(requested_hours)
        scan_where = " AND ".join(scan_filters)
        with self.connect() as connection:
            scan_row = connection.execute(
                f"""
                SELECT * FROM funding_scans
                WHERE {scan_where}
                ORDER BY
                    CASE
                        WHEN json_extract(config_json, '$.focused_route_key') IS NULL
                        THEN 0
                        ELSE 1
                    END,
                    funding_scan_id DESC
                LIMIT 1
                """,
                scan_params,
            ).fetchone()
            research_row = connection.execute(
                """
                SELECT * FROM funding_scans
                WHERE status = 'success'
                  AND json_extract(config_json, '$.horizon_mode') = 'research'
                ORDER BY funding_scan_id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_scan = dict(scan_row) if scan_row else {}
            research_scan = dict(research_row) if research_row else {}
            if latest_scan:
                latest_scan["config"] = json.loads(
                    latest_scan.pop("config_json") or "{}"
                )
                latest_scan["scan_mode"] = latest_scan["config"].get(
                    "scan_mode",
                    "manual",
                )
                latest_scan["duration_seconds"] = elapsed_seconds(
                    latest_scan.get("started_at"),
                    latest_scan.get("finished_at"),
                )
            if research_scan:
                research_scan["config"] = json.loads(
                    research_scan.pop("config_json") or "{}"
                )
                research_scan["scan_mode"] = research_scan["config"].get(
                    "scan_mode",
                    "manual",
                )
                research_scan["duration_seconds"] = elapsed_seconds(
                    research_scan.get("started_at"),
                    research_scan.get("finished_at"),
                )
            scan_id = latest_scan.get("funding_scan_id")
            minimum_visible_profit = max(
                1.0,
                float(
                    (latest_scan.get("config") or {}).get(
                        "minimum_net_profit",
                        1.0,
                    )
                    or 1.0
                ),
            )
            instrument_collapse_audit: dict[str, Any] = {}
            if scan_id:
                candidate_rows = connection.execute(
                    """
                    SELECT * FROM funding_routes
                    WHERE funding_scan_id = ? AND status IN ('paper_candidate', 'watch')
                      AND market_capacity >= ?
                      AND (
                          (
                              COALESCE(json_extract(
                                  evidence_json,
                                  '$.decision_mode'
                              ), '') = 'settlement_capture'
                              AND COALESCE(
                                  CAST(json_extract(
                                      evidence_json,
                                      '$.selected_strategy.expected_net_pnl'
                                  ) AS REAL),
                                  CAST(json_extract(
                                      evidence_json,
                                      '$.strategy_classification.expected_net_pnl'
                                  ) AS REAL),
                                  CAST(json_extract(
                                      evidence_json,
                                      '$.current_nowcast_net'
                                  ) AS REAL),
                                  0
                              ) >= ?
                          )
                          OR (
                              COALESCE(json_extract(
                                  evidence_json,
                                  '$.decision_mode'
                              ), '') != 'settlement_capture'
                              AND COALESCE(CAST(json_extract(
                                  evidence_json,
                                  '$.conservative_net_profit'
                              ) AS REAL), expected_net_profit, 0) >= ?
                          )
                      )
                    ORDER BY COALESCE(
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.selected_strategy.expected_net_pnl'
                                 ) AS REAL),
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.strategy_classification.expected_net_pnl'
                                 ) AS REAL),
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.current_nowcast_net'
                                 ) AS REAL)
                             ) DESC,
                             CAST(json_extract(
                                 evidence_json,
                                 '$.conservative_net_profit'
                             ) AS REAL) DESC,
                             expected_net_profit DESC
                    """,
                    (
                        scan_id,
                        FUNDING_MINIMUM_ACTIONABLE_NOTIONAL,
                        minimum_visible_profit,
                        minimum_visible_profit,
                    ),
                ).fetchall()
                watch_rows = connection.execute(
                    """
                    SELECT * FROM funding_routes
                    WHERE funding_scan_id = ? AND status = 'watch'
                      AND market_capacity >= ?
                      AND (
                          expected_net_profit > 0
                          OR CAST(json_extract(
                              evidence_json,
                              '$.conservative_net_profit'
                          ) AS REAL) > 0
                          OR CAST(json_extract(
                              evidence_json,
                              '$.current_nowcast_net'
                          ) AS REAL) > 0
                          OR CAST(json_extract(
                              evidence_json,
                              '$.selected_strategy.expected_net_pnl'
                          ) AS REAL) > 0
                          OR CAST(json_extract(
                              evidence_json,
                              '$.strategy_classification.expected_net_pnl'
                          ) AS REAL) > 0
                      )
                      AND NOT (
                          COALESCE(json_extract(
                              evidence_json,
                              '$.decision_mode'
                          ), '') = 'settlement_capture'
                          AND COALESCE(
                              CAST(json_extract(
                                  evidence_json,
                                  '$.selected_strategy.expected_net_pnl'
                              ) AS REAL),
                              CAST(json_extract(
                                  evidence_json,
                                  '$.strategy_classification.expected_net_pnl'
                              ) AS REAL),
                              CAST(json_extract(
                                  evidence_json,
                                  '$.current_nowcast_net'
                              ) AS REAL),
                              0
                          ) >= ?
                          AND json_extract(
                              evidence_json,
                              '$.synchronized_capability_passed'
                          )
                      )
                    ORDER BY COALESCE(
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.selected_strategy.expected_net_pnl'
                                 ) AS REAL),
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.strategy_classification.expected_net_pnl'
                                 ) AS REAL),
                                 CAST(json_extract(
                                     evidence_json,
                                     '$.current_nowcast_net'
                                 ) AS REAL)
                             ) DESC,
                             CAST(json_extract(
                                 evidence_json,
                                 '$.conservative_net_profit'
                             ) AS REAL) DESC,
                             expected_net_profit DESC,
                             market_capacity DESC
                    """,
                    (scan_id, FUNDING_MINIMUM_ACTIONABLE_NOTIONAL, minimum_visible_profit),
                ).fetchall()
                maker_rows = connection.execute(
                    """
                    SELECT * FROM funding_routes
                    WHERE funding_scan_id = ?
                      AND status = 'watch'
                      AND market_capacity >= ?
                      AND COALESCE(CAST(json_extract(
                          evidence_json,
                          '$.execution_scenarios.maker_entry_taker_exit.setup_visible'
                      ) AS INTEGER), 0) = 1
                    ORDER BY CAST(json_extract(
                                 evidence_json,
                                 '$.execution_scenarios.maker_entry_taker_exit.current_net_profit_if_filled'
                             ) AS REAL) DESC,
                             CAST(json_extract(
                                 evidence_json,
                                 '$.execution_scenarios.maker_entry_taker_exit.current_expected_attempt_pnl'
                             ) AS REAL) DESC,
                             market_capacity DESC
                    """,
                    (scan_id, FUNDING_MINIMUM_ACTIONABLE_NOTIONAL),
                ).fetchall()
                route_counts = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT status, COUNT(*) AS route_count
                        FROM funding_routes
                        WHERE funding_scan_id = ? AND market_capacity >= ?
                        GROUP BY status
                        ORDER BY status
                        """,
                        (scan_id, FUNDING_MINIMUM_ACTIONABLE_NOTIONAL),
                    ).fetchall()
                ]
                paper_summary = dict(
                    connection.execute(
                        """
                        SELECT
                            COUNT(*) AS execution_count,
                            SUM(CASE WHEN status IN ('revalidated_profitable', 'settlement_reauthorized') THEN 1 ELSE 0 END) AS profitable_count,
                            SUM(CASE WHEN status = 'settlement_exit_required' THEN 1 ELSE 0 END) AS exit_required_count,
                            AVG(fill_ratio) AS average_fill_ratio,
                            SUM(COALESCE(repriced_net_profit, 0)) AS repriced_net_profit
                        FROM funding_paper_executions
                        WHERE funding_scan_id = ?
                        """,
                        (scan_id,),
                    ).fetchone()
                )
                venues = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT
                            market.venue,
                            COUNT(*) AS instrument_count,
                            COUNT(book.funding_orderbook_snapshot_id) AS book_count,
                            MAX(market.observed_at) AS observed_at
                        FROM funding_market_snapshots market
                        LEFT JOIN funding_orderbook_snapshots book
                          ON book.funding_scan_id = market.funding_scan_id
                         AND book.venue = market.venue
                         AND book.symbol = market.symbol
                        WHERE market.funding_scan_id = ?
                        GROUP BY market.venue
                        ORDER BY market.venue
                        """,
                        (scan_id,),
                    ).fetchall()
                ]
                warnings = [
                    row["warning"]
                    for row in connection.execute(
                        """
                        SELECT warning
                        FROM funding_scan_warnings
                        WHERE funding_scan_id = ?
                        ORDER BY funding_scan_warning_id
                        """,
                        (scan_id,),
                    ).fetchall()
                ]
                diagnostic_rows = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT canonical_asset, long_venue, short_venue,
                               market_capacity, target_notional,
                               expected_gross_funding,
                               expected_net_profit, total_fees,
                               slippage_cost, basis_reserve, operations_buffer,
                               status,
                               legs_json, risk_flags_json, evidence_json
                        FROM funding_routes
                        WHERE funding_scan_id = ?
                        """,
                        (scan_id,),
                    ).fetchall()
                ]
                universe_summary = dict(
                    connection.execute(
                        """
                        SELECT
                            COUNT(*) AS route_count,
                            SUM(quick_schedule_ready) AS schedule_ready_count,
                            SUM(execution_eligible) AS execution_shortlist_count,
                            SUM(CASE WHEN execution_eligible = 0 THEN 1 ELSE 0 END)
                                AS pre_execution_reject_count
                        FROM funding_route_universe
                        WHERE funding_scan_id = ?
                        """,
                        (scan_id,),
                    ).fetchone()
                )
                universe_reasons = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT execution_screen_reason,
                               COUNT(*) AS route_count
                        FROM funding_route_universe
                        WHERE funding_scan_id = ?
                        GROUP BY execution_screen_reason
                        ORDER BY route_count DESC, execution_screen_reason
                        """,
                        (scan_id,),
                    ).fetchall()
                ]
                top_universe_routes = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT canonical_asset, long_venue, long_symbol,
                               short_venue, short_symbol,
                               quick_gross_rate, quick_taker_cost_rate,
                               quick_maker_cost_rate, quick_best_case_net_rate,
                               execution_eligible, execution_screen_reason
                        FROM funding_route_universe
                        WHERE funding_scan_id = ?
                        ORDER BY quick_best_case_net_rate DESC
                        LIMIT 10
                        """,
                        (scan_id,),
                    ).fetchall()
                ]
                instrument_collapse_audit = funding_instrument_collapse_audit(
                    connection,
                    int(scan_id),
                )
                horizon_comparisons = funding_horizon_comparisons(
                    connection,
                    [str(row["route_key"]) for row in candidate_rows],
                )
            else:
                candidate_rows = []
                watch_rows = []
                maker_rows = []
                route_counts = []
                paper_summary = {}
                venues = []
                warnings = []
                diagnostic_rows = []
                universe_summary = {}
                universe_reasons = []
                top_universe_routes = []
                horizon_comparisons = {}
        candidates = [
            row
            for row in (decode_funding_route(dict(row)) for row in candidate_rows)
            if funding_route_decision_profit(row) >= minimum_visible_profit
        ]
        horizon_comparisons = add_current_live_horizon_projections(
            candidates,
            horizon_comparisons,
        )
        watch = [decode_funding_route(dict(row)) for row in watch_rows]
        raw_maker_setups = [
            decode_funding_route(dict(row)) for row in maker_rows
        ]
        maker_setups = [
            row
            for row in raw_maker_setups
            if not set(
                row.get("evidence", {}).get("blocking_risk_flags") or []
            ).intersection({"unit_identity_mismatch", "basis_divergence"})
        ]
        capacity_eligible_route_count = sum(
            int(row.get("route_count") or 0) for row in route_counts
        )
        blocker_counts: Counter[str] = Counter()
        economics_funnel = {
            "calculated": len(diagnostic_rows),
            "capacity_eligible": 0,
            "current_raw_gross_covers_full_cost": 0,
            "current_raw_actionable": 0,
            "gross_covers_full_cost": 0,
            "median_net_positive": 0,
            "q25_net_positive": 0,
            "q25_actionable": 0,
            "paper_candidate": len(candidates),
        }
        constraint_counts: Counter[str] = Counter()
        component_samples: dict[str, list[float]] = {
            "current_raw_gross": [],
            "forecast_median_gross": [],
            "fees": [],
            "slippage": [],
            "basis_reserve": [],
            "basis_stress_loss": [],
            "operations_buffer": [],
            "execution_cost": [],
            "current_raw_net": [],
            "current_opportunity_net": [],
            "forecast_median_net": [],
            "q25_net": [],
            "break_even_gap": [],
        }
        near_misses: list[dict[str, Any]] = []
        liquidity_snapshot_counts: list[float] = []
        liquidity_persistence_scores: list[float] = []
        liquidity_executable_fractions: list[float] = []
        maker_profile_count = 0
        empirical_maker_profile_count = 0
        refill_event_count = 0
        minimum_probability = float(
            (latest_scan.get("config") or {}).get(
                "minimum_profit_probability",
                0.70,
            )
        )
        for row in diagnostic_rows:
            evidence = json.loads(row.get("evidence_json") or "{}")
            flags = evidence.get("blocking_risk_flags") or []
            liquidity = evidence.get("liquidity_sequence") or {}
            if liquidity:
                liquidity_snapshot_counts.append(
                    float(liquidity.get("minimum_snapshot_count") or 0)
                )
                liquidity_persistence_scores.append(
                    float(liquidity.get("route_persistence_score") or 0)
                )
                liquidity_executable_fractions.append(
                    float(liquidity.get("route_executable_fraction") or 0)
                )
                refill_event_count += int(liquidity.get("refill_event_count") or 0)
                maker_profile = liquidity.get("maker_entry") or {}
                maker_profile_count += 1
                empirical_maker_profile_count += int(
                    bool(maker_profile.get("data_ready"))
                )
            capacity_eligible = (
                float(row.get("market_capacity") or 0)
                >= FUNDING_MINIMUM_ACTIONABLE_NOTIONAL
            )
            if not capacity_eligible:
                constraint_counts["insufficient_capacity"] += 1
                continue
            economics_funnel["capacity_eligible"] += 1
            data_quality_flags = {
                "unit_identity_mismatch",
                "basis_divergence",
            }
            if set(flags).intersection(data_quality_flags):
                blocker_counts.update(str(flag) for flag in flags)
                constraint_counts["data_quality_issue"] += 1
                continue

            gross = float(row.get("expected_gross_funding") or 0)
            execution_cost = float(evidence.get("execution_cost") or 0)
            funding_notional = float(
                evidence.get("funding_notional")
                or row.get("target_notional")
                or 0
            )
            raw_gross = float(
                evidence.get("current_nowcast_gross")
                if evidence.get("current_nowcast_gross") is not None
                else funding_notional
                * float(
                    (evidence.get("forecast") or {}).get(
                        "raw_settlement_rate"
                    )
                    or 0
                )
            )
            opportunity_gross = float(
                evidence.get("current_opportunity_gross")
                if evidence.get("current_opportunity_gross") is not None
                else raw_gross
            )
            median_net = float(row.get("expected_net_profit") or 0)
            q25_net = float(evidence.get("conservative_net_profit") or 0)
            actionable_threshold = float(
                evidence.get("actionable_profit_threshold") or 0
            )
            forecast_median_gross = float(
                row.get("expected_gross_funding") or 0
            )
            fees = float(row.get("total_fees") or 0)
            slippage = float(row.get("slippage_cost") or 0)
            basis_reserve = float(row.get("basis_reserve") or 0)
            basis_stress_loss = float(evidence.get("basis_stress_loss") or 0)
            operations_buffer = float(row.get("operations_buffer") or 0)
            current_raw_net = raw_gross - execution_cost
            opportunity_net = float(
                evidence.get("current_opportunity_net")
                if evidence.get("current_opportunity_net") is not None
                else current_raw_net
            )
            break_even_gap = max(0.0, execution_cost - opportunity_gross)
            component_values = {
                "current_raw_gross": raw_gross,
                "forecast_median_gross": forecast_median_gross,
                "fees": fees,
                "slippage": slippage,
                "basis_reserve": basis_reserve,
                "basis_stress_loss": basis_stress_loss,
                "operations_buffer": operations_buffer,
                "execution_cost": execution_cost,
                "current_raw_net": current_raw_net,
                "current_opportunity_net": opportunity_net,
                "forecast_median_net": median_net,
                "q25_net": q25_net,
                "break_even_gap": break_even_gap,
            }
            for name, value in component_values.items():
                component_samples[name].append(value)
            near_misses.append(
                {
                    "canonical_asset": row.get("canonical_asset"),
                    "long_venue": row.get("long_venue"),
                    "short_venue": row.get("short_venue"),
                    "legs": json.loads(row.get("legs_json") or "[]"),
                    "target_notional": float(row.get("target_notional") or 0),
                    "funding_notional": funding_notional,
                    "current_raw_gross": raw_gross,
                    "current_opportunity_gross": opportunity_gross,
                    "execution_cost": execution_cost,
                    "current_raw_net": current_raw_net,
                    "current_opportunity_net": opportunity_net,
                    "break_even_gap": break_even_gap,
                    "expected_net_profit": median_net,
                    "q25_net_profit": q25_net,
                    "actionable_profit_threshold": actionable_threshold,
                    "current_basis_stress_net_profit": float(
                        evidence.get("current_basis_stress_net_profit") or 0
                    ),
                    "needed_improvement": evidence.get("needed_improvement") or {},
                }
            )
            settlement_lead_seconds = evidence.get("settlement_lead_seconds")
            minimum_lead_seconds = float(
                evidence.get("minimum_settlement_lead_seconds")
                or (latest_scan.get("config") or {}).get(
                    "minimum_settlement_lead_seconds",
                    0,
                )
            )
            if opportunity_gross > execution_cost:
                economics_funnel["current_raw_gross_covers_full_cost"] += 1
            if (
                opportunity_net > 0
                and settlement_lead_seconds is not None
                and float(settlement_lead_seconds) >= minimum_lead_seconds
            ):
                economics_funnel["current_raw_actionable"] += 1
            if gross > execution_cost:
                economics_funnel["gross_covers_full_cost"] += 1
            if median_net > 0:
                economics_funnel["median_net_positive"] += 1
            if q25_net > 0:
                economics_funnel["q25_net_positive"] += 1
            if q25_net >= actionable_threshold:
                economics_funnel["q25_actionable"] += 1
            probability = float(evidence.get("net_profit_probability") or 0)
            settlement_capture = (
                evidence.get("decision_mode") == "settlement_capture"
            )
            selected_expected_net = float(
                (evidence.get("selected_strategy") or {}).get("expected_net_pnl")
                or (evidence.get("strategy_classification") or {}).get("expected_net_pnl")
                or evidence.get("current_nowcast_net")
                or 0
            )
            actionable_synchronized_watch = (
                row.get("status") == "watch"
                and settlement_capture
                and bool(evidence.get("synchronized_capability_passed"))
                and selected_expected_net >= minimum_visible_profit
                and "live_net_pnl_not_positive" not in flags
            )
            if row.get("status") == "paper_candidate" or actionable_synchronized_watch:
                constraint_counts["paper_candidate"] += 1
                continue
            blocker_counts.update(str(flag) for flag in flags)
            economic_flags = {
                "conservative_net_after_costs_too_low",
                "actionable_profit_too_low",
                "profit_probability_too_low",
                "forecast_median_net_negative",
                "live_net_pnl_not_positive",
            }
            non_economic_blockers = [
                flag for flag in flags if flag not in economic_flags
            ]
            if opportunity_gross <= execution_cost:
                constraint_counts["current_spread_below_cost"] += 1
            elif settlement_capture and opportunity_net <= 0:
                constraint_counts["live_pnl_not_positive"] += 1
            elif settlement_capture and (
                "basis_not_covered_by_live_funding" in flags
            ):
                constraint_counts["basis_not_covered_by_live_funding"] += 1
            elif settlement_capture and non_economic_blockers:
                constraint_counts["model_or_execution_quality_gate"] += 1
            elif settlement_capture:
                constraint_counts["unclassified_gate"] += 1
            elif median_net <= 0:
                constraint_counts["forecast_median_below_cost"] += 1
            elif q25_net <= 0:
                constraint_counts["downside_q25_below_cost"] += 1
            elif q25_net < actionable_threshold:
                constraint_counts["q25_below_actionable_profit"] += 1
            elif probability < minimum_probability:
                constraint_counts["profit_probability_below_gate"] += 1
            elif "basis_not_covered_by_funding" in flags:
                constraint_counts["basis_not_covered_by_funding"] += 1
            elif non_economic_blockers:
                constraint_counts["model_or_execution_quality_gate"] += 1
            else:
                constraint_counts["unclassified_gate"] += 1
        stage_order = (
            "insufficient_capacity",
            "data_quality_issue",
            "current_spread_below_cost",
            "live_pnl_not_positive",
            "forecast_median_below_cost",
            "downside_q25_below_cost",
            "q25_below_actionable_profit",
            "profit_probability_below_gate",
            "basis_not_covered_by_funding",
            "basis_not_covered_by_live_funding",
            "model_or_execution_quality_gate",
            "unclassified_gate",
            "paper_candidate",
        )
        exclusive_stages = [
            {"stage": stage, "route_count": int(constraint_counts.get(stage, 0))}
            for stage in stage_order
            if constraint_counts.get(stage, 0)
        ]
        blocking_stages = [
            row for row in exclusive_stages if row["stage"] != "paper_candidate"
        ]
        primary_constraint = max(
            blocking_stages,
            key=lambda row: row["route_count"],
            default=None,
        )
        median_components = {
            name: median_float(values) for name, values in component_samples.items()
        }
        near_misses.sort(
            key=lambda row: (
                float(row["current_raw_net"]),
                float(row["expected_net_profit"]),
            ),
            reverse=True,
        )
        return {
            "latest_scan": latest_scan,
            "latest_research_scan": research_scan,
            "routes": candidates,
            "watch_routes": watch,
            "maker_routes": maker_setups,
            "route_counts": route_counts,
            "paper_summary": paper_summary,
            "venues": venues,
            "warnings": warnings,
            "minimum_visible_capacity": FUNDING_MINIMUM_ACTIONABLE_NOTIONAL,
            "visible_route_count": len(candidates),
            "internal_watch_route_count": len(watch),
            "internal_maker_setup_count": len(maker_setups),
            "capacity_eligible_route_count": capacity_eligible_route_count,
            "universe_summary": {
                "route_count": int(
                    universe_summary.get("route_count")
                    or latest_scan.get("route_count")
                    or 0
                ),
                "schedule_ready_count": int(
                    universe_summary.get("schedule_ready_count") or 0
                ),
                "execution_shortlist_count": int(
                    universe_summary.get("execution_shortlist_count")
                    or latest_scan.get("route_count")
                    or 0
                ),
                "pre_execution_reject_count": int(
                    universe_summary.get("pre_execution_reject_count") or 0
                ),
                "screen_reasons": universe_reasons,
                "top_routes": top_universe_routes,
                "selection_policy": "adaptive_score_with_orderbook_market_budget",
            },
            "instrument_collapse_audit": instrument_collapse_audit,
            "horizon_comparisons": horizon_comparisons,
            "economics_funnel": economics_funnel,
            "constraint_diagnostics": {
                "primary_constraint": primary_constraint,
                "exclusive_stages": exclusive_stages,
                "median_components": median_components,
                "near_misses": near_misses[:5],
                "classification": "first_failed_gate",
                "execution_quality": {
                    "route_count": len(liquidity_snapshot_counts),
                    "median_minimum_snapshots": median_float(
                        liquidity_snapshot_counts
                    ),
                    "median_persistence_score": median_float(
                        liquidity_persistence_scores
                    ),
                    "median_executable_fraction": median_float(
                        liquidity_executable_fractions
                    ),
                    "maker_profile_count": maker_profile_count,
                    "empirical_maker_profile_count": empirical_maker_profile_count,
                    "refill_event_count": refill_event_count,
                },
            },
            "blocker_summary": [
                {"risk_flag": flag, "route_count": count}
                for flag, count in blocker_counts.most_common(8)
            ],
            "minimum_visible_profit": minimum_visible_profit,
            "history_sync": self.funding_history_sync_summary(),
            "execution_mode": "paper_only_research",
            "llm_in_execution_loop": False,
        }

    def latest_funding_scan_config(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT config_json
                FROM funding_scans
                WHERE status = 'success'
                  AND COALESCE(
                      json_extract(config_json, '$.horizon_mode'),
                      'fixed'
                  ) <> 'research'
                  AND COALESCE(
                      json_extract(config_json, '$.scan_mode'),
                      'manual'
                  ) <> 'watch'
                ORDER BY funding_scan_id DESC
                LIMIT 1
                """
            ).fetchone()
        return json.loads(row["config_json"] or "{}") if row else {}

    def prune_funding_auto_scans(self, keep: int = 360) -> int:
        retained = max(1, int(keep))
        with self.connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            scan_rows = connection.execute(
                """
                SELECT funding_scan_id, config_json
                FROM funding_scans
                WHERE status = 'success'
                ORDER BY funding_scan_id DESC
                """
            ).fetchall()
            auto_scan_ids = []
            for row in scan_rows:
                try:
                    config = json.loads(row["config_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                if config.get("scan_mode") == "auto":
                    auto_scan_ids.append(int(row["funding_scan_id"]))
            expired = auto_scan_ids[retained:]
            for offset in range(0, len(expired), 200):
                scan_ids = expired[offset : offset + 200]
                placeholders = ",".join("?" for _ in scan_ids)
                route_ids = [
                    int(row["funding_route_id"])
                    for row in connection.execute(
                        f"""
                        SELECT funding_route_id
                        FROM funding_routes
                        WHERE funding_scan_id IN ({placeholders})
                        """,
                        scan_ids,
                    ).fetchall()
                ]
                # Detach paper events referencing deleted scans/routes.
                connection.execute(
                    f"UPDATE funding_paper_events SET funding_scan_id = NULL WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                if route_ids:
                    route_placeholders = ",".join("?" for _ in route_ids)
                    connection.execute(
                        f"UPDATE funding_paper_events SET funding_route_id = NULL WHERE funding_route_id IN ({route_placeholders})",
                        route_ids,
                    )
                    connection.execute(
                        f"""
                        DELETE FROM funding_paper_executions
                        WHERE funding_route_id IN ({route_placeholders})
                        """,
                        route_ids,
                    )
                connection.execute(
                    f"DELETE FROM funding_paper_executions WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_routes WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_route_universe WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_orderbook_snapshots WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_market_snapshots WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_scan_warnings WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
                connection.execute(
                    f"DELETE FROM funding_scans WHERE funding_scan_id IN ({placeholders})",
                    scan_ids,
                )
        return len(expired)

    def ensure_funding_paper_accounts(
        self,
        venues: list[str] | set[str] | tuple[str, ...],
        starting_balance: float = 1_000.0,
    ) -> int:
        now = utc_now_iso()
        clean_venues = sorted({str(venue) for venue in venues if str(venue)})
        if not clean_venues:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO funding_paper_accounts (
                    venue, starting_balance, cash_balance, reserved_margin,
                    realized_pnl, updated_at
                )
                VALUES (?, ?, ?, 0, 0, ?)
                ON CONFLICT(venue) DO NOTHING
                """,
                [
                    (venue, float(starting_balance), float(starting_balance), now)
                    for venue in clean_venues
                ],
            )
        return len(clean_venues)

    def funding_paper_account_rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *,
                       cash_balance - reserved_margin AS available_balance
                FROM funding_paper_accounts
                ORDER BY venue
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_funding_paper_account_reserved(
        self,
        venue: str,
        delta: float,
    ) -> None:
        """Adjust reserved_margin for a venue account. Idempotent via ledger."""
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_paper_accounts
                SET reserved_margin = MAX(0, reserved_margin + ?),
                    updated_at = ?
                WHERE venue = ?
                """,
                (float(delta), now, str(venue)),
            )

    def update_funding_paper_account_cash(
        self,
        venue: str,
        delta: float,
    ) -> None:
        """Adjust cash/realized PnL for v2 idempotent paper ledger effects."""
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_paper_accounts
                SET cash_balance = cash_balance + ?,
                    realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE venue = ?
                """,
                (float(delta), float(delta), now, str(venue)),
            )

    def paper_account_consistency_report(self) -> dict[str, Any]:
        """Compare venue accounts with idempotent ledger-derived effects."""
        with self.connect() as connection:
            accounts = [dict(row) for row in connection.execute(
                "SELECT * FROM funding_paper_accounts ORDER BY venue"
            ).fetchall()]
            ledger_rows = connection.execute(
                "SELECT * FROM paper_event_ledger ORDER BY created_at"
            ).fetchall()
            reconciliation_rows = connection.execute(
                """
                SELECT *
                FROM funding_settlement_reconciliations
                WHERE status IN (
                    'RATE_AND_MARK_RECONCILED',
                    'RECONCILIATION_EFFECT_MISMATCH'
                )
                ORDER BY scheduled_funding_at, venue
                """
            ).fetchall()
        cash_by_venue: dict[str, float] = {}
        reserve_by_venue: dict[str, float] = {}
        decoded_ledger_rows: list[dict[str, Any]] = []
        for row in ledger_rows:
            item = dict(row)
            venue = str(row["venue"] or "")
            payload: dict[str, Any]
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            item["payload"] = payload
            decoded_ledger_rows.append(item)
            if not venue:
                continue
            cash_by_venue[venue] = cash_by_venue.get(venue, 0.0) + float(row["cash_delta"] or 0.0)
            event_type = str(row["event_type"] or "")
            if event_type == "collateral_reserve":
                reserve_by_venue[venue] = reserve_by_venue.get(venue, 0.0) + float(payload.get("amount") or 0.0)
            elif event_type == "collateral_release":
                reserve_by_venue[venue] = reserve_by_venue.get(venue, 0.0) - float(payload.get("amount") or 0.0)
        mismatches: list[dict[str, Any]] = []
        venues = {str(row["venue"]) for row in accounts} | set(cash_by_venue) | set(reserve_by_venue)
        accounts_by_venue = {str(row["venue"]): row for row in accounts}
        for venue in sorted(venues):
            account = accounts_by_venue.get(venue)
            if account is None:
                mismatches.append({"venue": venue, "kind": "account_missing"})
                continue
            expected_cash = float(account["starting_balance"] or 0.0) + float(cash_by_venue.get(venue, 0.0))
            expected_realized = float(cash_by_venue.get(venue, 0.0))
            expected_reserved = max(0.0, float(reserve_by_venue.get(venue, 0.0)))
            actual_cash = float(account["cash_balance"] or 0.0)
            actual_realized = float(account["realized_pnl"] or 0.0)
            actual_reserved = float(account["reserved_margin"] or 0.0)
            if abs(actual_cash - expected_cash) > 1e-8:
                mismatches.append({
                    "venue": venue,
                    "kind": "cash_balance_mismatch",
                    "expected": expected_cash,
                    "actual": actual_cash,
                })
            if abs(actual_realized - expected_realized) > 1e-8:
                mismatches.append({
                    "venue": venue,
                    "kind": "realized_pnl_mismatch",
                    "expected": expected_realized,
                    "actual": actual_realized,
                })
            if abs(actual_reserved - expected_reserved) > 1e-8:
                mismatches.append({
                    "venue": venue,
                    "kind": "reserved_margin_mismatch",
                    "expected": expected_reserved,
                    "actual": actual_reserved,
                })
        ledger_by_key = {
            str(row.get("event_key") or ""): row
            for row in decoded_ledger_rows
            if str(row.get("event_key") or "")
        }
        for recon in reconciliation_rows:
            try:
                evidence = json.loads(recon["evidence_json"] or "{}")
            except (TypeError, ValueError):
                evidence = {}
            financial_effect = (
                evidence.get("financial_effect")
                if isinstance(evidence.get("financial_effect"), dict)
                else {}
            )
            position_id = str(recon["position_id"] or "")
            venue = str(recon["venue"] or "")
            cycle_id = str(recon["cycle_id"] or "")
            scheduled_at = str(recon["scheduled_funding_at"] or "")
            event_key = str(financial_effect.get("event_key") or "")
            ledger = ledger_by_key.get(event_key) if event_key else None
            if ledger is None:
                suffix = f":{venue}:{scheduled_at}"
                candidates = [
                    row
                    for row in decoded_ledger_rows
                    if str(row.get("event_type") or "") == "funding"
                    and str(row.get("position_id") or "") == position_id
                    and str(row.get("cycle_id") or "") == cycle_id
                    and str(row.get("venue") or "") == venue
                    and str(row.get("event_key") or "").endswith(suffix)
                ]
                ledger = candidates[0] if len(candidates) == 1 else None
                if ledger is not None:
                    event_key = str(ledger.get("event_key") or "")
            if ledger is None:
                mismatches.append(
                    {
                        "venue": venue,
                        "kind": "reconciliation_effect_missing",
                        "position_id": position_id,
                        "cycle_id": cycle_id,
                        "scheduled_funding_at": scheduled_at,
                        "event_key": event_key or None,
                    }
                )
                continue
            effect_mismatches: list[str] = []
            expected_pnl = float(recon["funding_pnl"] or 0.0)
            actual_delta = float(ledger.get("cash_delta") or 0.0)
            if str(ledger.get("event_type") or "") != "funding":
                effect_mismatches.append("event_type")
            if str(ledger.get("position_id") or "") != position_id:
                effect_mismatches.append("position_id")
            if str(ledger.get("cycle_id") or "") != cycle_id:
                effect_mismatches.append("cycle_id")
            if str(ledger.get("venue") or "") != venue:
                effect_mismatches.append("venue")
            if abs(actual_delta - expected_pnl) > 1e-8:
                effect_mismatches.append("cash_delta")
            payload = ledger.get("payload") if isinstance(ledger.get("payload"), dict) else {}
            if str(payload.get("side") or "") != str(recon["side"] or ""):
                effect_mismatches.append("side")
            if not str(event_key or ledger.get("event_key") or "").endswith(f":{venue}:{scheduled_at}"):
                effect_mismatches.append("event_key_scheduled_identity")
            financial_delta = financial_effect.get("cash_delta")
            if financial_delta is not None:
                try:
                    financial_delta_float = float(financial_delta)
                except (TypeError, ValueError):
                    financial_delta_float = math.nan
                if not math.isfinite(financial_delta_float) or abs(financial_delta_float - expected_pnl) > 1e-8:
                    effect_mismatches.append("financial_effect_cash_delta")
            if effect_mismatches:
                mismatches.append(
                    {
                        "venue": venue,
                        "kind": "reconciliation_effect_mismatch",
                        "position_id": position_id,
                        "cycle_id": cycle_id,
                        "scheduled_funding_at": scheduled_at,
                        "event_key": event_key or ledger.get("event_key"),
                        "mismatches": list(dict.fromkeys(effect_mismatches)),
                        "expected_cash_delta": expected_pnl,
                        "actual_cash_delta": actual_delta,
                    }
                )
        return {
            "ok": not mismatches,
            "mismatches": mismatches,
            "ledger_cash_delta_by_venue": cash_by_venue,
            "ledger_reserved_margin_by_venue": {
                venue: max(0.0, value) for venue, value in reserve_by_venue.items()
            },
        }

    def repair_paper_account_consistency(self) -> dict[str, Any]:
        """Reset paper account balances to the deterministic ledger-derived state."""
        report = self.paper_account_consistency_report()
        now = utc_now_iso()
        if report["ok"]:
            return {"repaired": False, **report}
        with self.connect() as connection:
            accounts = [dict(row) for row in connection.execute(
                "SELECT * FROM funding_paper_accounts ORDER BY venue"
            ).fetchall()]
            accounts_by_venue = {str(row["venue"]): row for row in accounts}
            cash_by_venue = {
                str(key): float(value)
                for key, value in (report.get("ledger_cash_delta_by_venue") or {}).items()
            }
            reserve_by_venue = {
                str(key): float(value)
                for key, value in (report.get("ledger_reserved_margin_by_venue") or {}).items()
            }
            venues = set(accounts_by_venue) | set(cash_by_venue) | set(reserve_by_venue)
            for venue in sorted(venues):
                account = accounts_by_venue.get(venue)
                if account is None:
                    connection.execute(
                        """
                        INSERT INTO funding_paper_accounts (
                            venue, starting_balance, cash_balance, reserved_margin,
                            realized_pnl, updated_at
                        )
                        VALUES (?, 0, 0, 0, 0, ?)
                        ON CONFLICT(venue) DO NOTHING
                        """,
                        (venue, now),
                    )
                    account = {
                        "venue": venue,
                        "starting_balance": 0.0,
                    }
                cash_delta = cash_by_venue.get(venue, 0.0)
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET cash_balance = ?,
                        realized_pnl = ?,
                        reserved_margin = ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (
                        float(account["starting_balance"] or 0.0) + cash_delta,
                        cash_delta,
                        max(0.0, reserve_by_venue.get(venue, 0.0)),
                        now,
                        venue,
                    ),
                )
        return {"repaired": True, **self.paper_account_consistency_report()}

    def funding_paper_open_positions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM funding_paper_positions
                WHERE status IN ('open', 'settlement_pending')
                ORDER BY max_settlement_at, opened_at
                """
            ).fetchall()
        return [decode_funding_paper_position(dict(row)) for row in rows]

    def funding_paper_position_by_entry_key(
        self,
        entry_key: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM funding_paper_positions
                WHERE entry_key = ?
                """,
                (entry_key,),
            ).fetchone()
        return decode_funding_paper_position(dict(row)) if row else None

    def open_funding_paper_position(
        self,
        position: dict[str, Any],
    ) -> int:
        now = utc_now_iso()
        long_venue = str(position["long_venue"])
        short_venue = str(position["short_venue"])
        long_reserved = float(position["long_reserved_margin"])
        short_reserved = float(position["short_reserved_margin"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO funding_paper_positions (
                    entry_key, route_key, status, opened_at,
                    open_funding_scan_id, open_funding_route_id,
                    canonical_asset, long_venue, long_symbol, short_venue,
                    short_symbol, base_quantity, target_notional,
                    long_notional, short_notional, long_reserved_margin,
                    short_reserved_margin, long_settlement_at,
                    short_settlement_at, max_settlement_at,
                    expected_live_gross, expected_live_net,
                    expected_execution_cost, entry_cross_spread,
                    entry_basis_bps, entry_legs_json,
                    entry_evidence_json, notes_json
                )
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position["entry_key"],
                    position["route_key"],
                    now,
                    position.get("open_funding_scan_id"),
                    position.get("open_funding_route_id"),
                    position["canonical_asset"],
                    long_venue,
                    position["long_symbol"],
                    short_venue,
                    position["short_symbol"],
                    float(position.get("base_quantity") or 0.0),
                    float(position.get("target_notional") or 0.0),
                    float(position.get("long_notional") or 0.0),
                    float(position.get("short_notional") or 0.0),
                    long_reserved,
                    short_reserved,
                    position.get("long_settlement_at"),
                    position.get("short_settlement_at"),
                    position.get("max_settlement_at"),
                    float(position.get("expected_live_gross") or 0.0),
                    float(position.get("expected_live_net") or 0.0),
                    float(position.get("expected_execution_cost") or 0.0),
                    position.get("entry_cross_spread"),
                    position.get("entry_basis_bps"),
                    json.dumps(position.get("entry_legs") or []),
                    json.dumps(position.get("entry_evidence") or {}, sort_keys=True),
                    json.dumps(position.get("notes") or {}, sort_keys=True),
                ),
            )
            position_id = int(cursor.lastrowid)
            for venue, reserved in (
                (long_venue, long_reserved),
                (short_venue, short_reserved),
            ):
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET reserved_margin = reserved_margin + ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (reserved, now, venue),
                )
                account = connection.execute(
                    """
                    SELECT cash_balance, reserved_margin
                    FROM funding_paper_accounts
                    WHERE venue = ?
                    """,
                    (venue,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO funding_paper_balance_ledger (
                        created_at, venue, funding_paper_position_id,
                        event_type, cash_delta, reserved_delta,
                        cash_balance_after, reserved_margin_after, payload_json
                    )
                    VALUES (?, ?, ?, 'reserve_margin', 0, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        venue,
                        position_id,
                        reserved,
                        float(account["cash_balance"]),
                        float(account["reserved_margin"]),
                        json.dumps({"entry_key": position["entry_key"]}),
                    ),
                )
        return position_id

    def close_funding_paper_position(
        self,
        position_id: int,
        close: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM funding_paper_positions
                WHERE funding_paper_position_id = ?
                """,
                (int(position_id),),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown funding paper position {position_id}")
            position = dict(row)
            if position["status"] == "closed":
                return
            actual_execution_cost = float(close.get("actual_execution_cost") or 0.0)
            long_cash_delta = float(close.get("long_cash_delta") or 0.0)
            short_cash_delta = float(close.get("short_cash_delta") or 0.0)
            actual_net_pnl = float(close.get("actual_net_pnl") or 0.0)
            connection.execute(
                """
                UPDATE funding_paper_positions
                SET status = 'closed',
                    closed_at = ?,
                    close_funding_scan_id = ?,
                    close_funding_route_id = ?,
                    actual_funding_pnl = ?,
                    actual_basis_pnl = ?,
                    actual_execution_cost = ?,
                    actual_net_pnl = ?,
                    close_reason = ?,
                    close_legs_json = ?,
                    close_evidence_json = ?,
                    settlement_json = ?,
                    notes_json = ?
                WHERE funding_paper_position_id = ?
                """,
                (
                    now,
                    close.get("close_funding_scan_id"),
                    close.get("close_funding_route_id"),
                    float(close.get("actual_funding_pnl") or 0.0),
                    float(close.get("actual_basis_pnl") or 0.0),
                    actual_execution_cost,
                    actual_net_pnl,
                    str(close.get("close_reason") or "settlement_capture_complete"),
                    json.dumps(close.get("close_legs") or []),
                    json.dumps(close.get("close_evidence") or {}, sort_keys=True),
                    json.dumps(close.get("settlement") or {}, sort_keys=True),
                    json.dumps(close.get("notes") or {}, sort_keys=True),
                    int(position_id),
                ),
            )
            for venue, reserved, cash_delta in (
                (
                    position["long_venue"],
                    float(position["long_reserved_margin"] or 0.0),
                    long_cash_delta,
                ),
                (
                    position["short_venue"],
                    float(position["short_reserved_margin"] or 0.0),
                    short_cash_delta,
                ),
            ):
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET cash_balance = cash_balance + ?,
                        reserved_margin = MAX(0, reserved_margin - ?),
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (cash_delta, reserved, cash_delta, now, venue),
                )
                account = connection.execute(
                    """
                    SELECT cash_balance, reserved_margin
                    FROM funding_paper_accounts
                    WHERE venue = ?
                    """,
                    (venue,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO funding_paper_balance_ledger (
                        created_at, venue, funding_paper_position_id,
                        event_type, cash_delta, reserved_delta,
                        cash_balance_after, reserved_margin_after, payload_json
                    )
                    VALUES (?, ?, ?, 'close_position', ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        venue,
                        int(position_id),
                        cash_delta,
                        -reserved,
                        float(account["cash_balance"]),
                        float(account["reserved_margin"]),
                        json.dumps(close.get("settlement") or {}, sort_keys=True),
                    ),
                )

    def accrue_funding_paper_settlement(
        self,
        position_id: int,
        accrual: dict[str, Any],
    ) -> bool:
        now = utc_now_iso()
        settlement_key = str(accrual.get("settlement_key") or "")
        if not settlement_key:
            raise ValueError("settlement_key is required")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM funding_paper_positions
                WHERE funding_paper_position_id = ?
                """,
                (int(position_id),),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown funding paper position {position_id}")
            position = decode_funding_paper_position(dict(row))
            if position["status"] == "closed":
                return False
            notes = dict(position.get("notes") or {})
            accrued_keys = [
                str(key) for key in notes.get("accrued_settlement_keys") or []
            ]
            if settlement_key in accrued_keys:
                return False
            long_cash_delta = float(accrual.get("long_cash_delta") or 0.0)
            short_cash_delta = float(accrual.get("short_cash_delta") or 0.0)
            funding_delta = long_cash_delta + short_cash_delta
            accrued_funding_pnl = (
                float(notes.get("accrued_funding_pnl") or 0.0) + funding_delta
            )
            accrued_settlements = list(notes.get("accrued_settlements") or [])
            accrued_settlements.append(accrual.get("settlement") or {})
            accrued_keys.append(settlement_key)
            notes.update(
                {
                    "accrued_funding_pnl": accrued_funding_pnl,
                    "accrued_settlement_count": len(accrued_keys),
                    "accrued_settlement_keys": accrued_keys[-100:],
                    "accrued_settlements": accrued_settlements[-100:],
                    "latest_hold_decision": accrual.get("hold_decision") or {},
                    "latest_accrual_at": now,
                }
            )
            expected_live_gross = safe_float(
                accrual.get("next_expected_live_gross"),
                position.get("expected_live_gross"),
            )
            expected_live_net = safe_float(
                accrual.get("next_expected_live_net"),
                position.get("expected_live_net"),
            )
            connection.execute(
                """
                UPDATE funding_paper_positions
                SET status = 'open',
                    long_settlement_at = ?,
                    short_settlement_at = ?,
                    max_settlement_at = ?,
                    expected_live_gross = ?,
                    expected_live_net = ?,
                    actual_funding_pnl = ?,
                    actual_net_pnl = ?,
                    entry_legs_json = ?,
                    entry_evidence_json = ?,
                    settlement_json = ?,
                    notes_json = ?
                WHERE funding_paper_position_id = ?
                """,
                (
                    accrual.get("next_long_settlement_at"),
                    accrual.get("next_short_settlement_at"),
                    accrual.get("next_max_settlement_at"),
                    expected_live_gross,
                    expected_live_net,
                    accrued_funding_pnl,
                    accrued_funding_pnl,
                    json.dumps(accrual.get("next_entry_legs") or []),
                    json.dumps(
                        accrual.get("next_entry_evidence") or {},
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "latest_accrual": accrual.get("settlement") or {},
                            "accrued_funding_pnl": accrued_funding_pnl,
                            "history_missing_fallback": bool(
                                accrual.get("history_missing_fallback")
                            ),
                        },
                        sort_keys=True,
                    ),
                    json.dumps(notes, sort_keys=True),
                    int(position_id),
                ),
            )
            for side, venue, cash_delta in (
                ("long", position["long_venue"], long_cash_delta),
                ("short", position["short_venue"], short_cash_delta),
            ):
                connection.execute(
                    """
                    UPDATE funding_paper_accounts
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE venue = ?
                    """,
                    (cash_delta, cash_delta, now, venue),
                )
                account = connection.execute(
                    """
                    SELECT cash_balance, reserved_margin
                    FROM funding_paper_accounts
                    WHERE venue = ?
                    """,
                    (venue,),
                ).fetchone()
                if account:
                    connection.execute(
                        """
                        INSERT INTO funding_paper_balance_ledger (
                            created_at, venue, funding_paper_position_id,
                            event_type, cash_delta, reserved_delta,
                            cash_balance_after, reserved_margin_after,
                            payload_json
                        )
                        VALUES (?, ?, ?, 'funding_accrual', ?, 0, ?, ?, ?)
                        """,
                        (
                            now,
                            venue,
                            int(position_id),
                            cash_delta,
                            float(account["cash_balance"]),
                            float(account["reserved_margin"]),
                            json.dumps(
                                {
                                    "side": side,
                                    "settlement_key": settlement_key,
                                    "settlement": accrual.get("settlement") or {},
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
        return True

    def set_funding_paper_position_status(
        self,
        position_id: int,
        status: str,
    ) -> None:
        normalized = str(status or "").strip() or "open"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_paper_positions
                SET status = ?
                WHERE funding_paper_position_id = ?
                  AND status <> 'closed'
                """,
                (normalized, int(position_id)),
            )

    def insert_funding_paper_event(
        self,
        event: dict[str, Any],
    ) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            funding_paper_position_id = existing_row_id(
                connection,
                "funding_paper_positions",
                "funding_paper_position_id",
                event.get("funding_paper_position_id"),
            )
            funding_scan_id = existing_row_id(
                connection,
                "funding_scans",
                "funding_scan_id",
                event.get("funding_scan_id"),
            )
            funding_route_id = existing_row_id(
                connection,
                "funding_routes",
                "funding_route_id",
                event.get("funding_route_id"),
            )
            params = (
                event.get("created_at") or now,
                event["event_type"],
                event.get("severity") or "info",
                funding_paper_position_id,
                funding_scan_id,
                funding_route_id,
                event.get("route_key"),
                event["message"],
                json.dumps(event.get("payload") or {}, sort_keys=True),
                event.get("telegram_status") or "not_configured",
                event.get("telegram_error"),
            )
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO funding_paper_events (
                        created_at, event_type, severity,
                        funding_paper_position_id, funding_scan_id,
                        funding_route_id, route_key, message, payload_json,
                        telegram_status, telegram_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
            except sqlite3.IntegrityError:
                # Retention may have deleted the referenced scan/route between
                # the existing_row_id check and this INSERT.  Retry with NULL
                # FK references so the event itself is never lost.
                cursor = connection.execute(
                    """
                    INSERT INTO funding_paper_events (
                        created_at, event_type, severity,
                        funding_paper_position_id, funding_scan_id,
                        funding_route_id, route_key, message, payload_json,
                        telegram_status, telegram_error
                    )
                    VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        params[0],  # created_at
                        params[1],  # event_type
                        params[2],  # severity
                        params[6],  # route_key
                        params[7],  # message
                        params[8],  # payload_json
                        params[9],  # telegram_status
                        params[10],  # telegram_error
                    ),
                )
        return int(cursor.lastrowid)

    def update_funding_paper_event_telegram_status(
        self,
        event_id: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_paper_events
                SET telegram_status = ?, telegram_error = ?
                WHERE funding_paper_event_id = ?
                """,
                (str(status), error, int(event_id)),
            )

    def upsert_funding_shadow_opportunity(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        key = str(row["opportunity_key"])
        observed_at = str(row.get("observed_at") or now)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_shadow_opportunities (
                    opportunity_key, environment, profile, canonical_asset,
                    status, long_venue, long_symbol, short_venue, short_symbol,
                    settlement_at, settlement_skew_seconds,
                    seconds_until_settlement, preliminary_gross_funding,
                    funding_net_excluding_points, stablecoin_risk_json,
                    capability_status_json, blockers_json,
                    points_metadata_json, payload_json, first_observed_at,
                    last_observed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_key) DO UPDATE SET
                    status = excluded.status,
                    settlement_at = excluded.settlement_at,
                    settlement_skew_seconds = excluded.settlement_skew_seconds,
                    seconds_until_settlement = excluded.seconds_until_settlement,
                    preliminary_gross_funding = excluded.preliminary_gross_funding,
                    funding_net_excluding_points = excluded.funding_net_excluding_points,
                    stablecoin_risk_json = excluded.stablecoin_risk_json,
                    capability_status_json = excluded.capability_status_json,
                    blockers_json = excluded.blockers_json,
                    points_metadata_json = excluded.points_metadata_json,
                    payload_json = excluded.payload_json,
                    last_observed_at = excluded.last_observed_at,
                    updated_at = excluded.updated_at
                WHERE excluded.last_observed_at >= funding_shadow_opportunities.last_observed_at
                """,
                (
                    key,
                    row.get("environment", "mainnet"),
                    row.get("profile", "dex_shadow"),
                    row["canonical_asset"],
                    row["status"],
                    row["long_venue"],
                    row.get("long_symbol"),
                    row["short_venue"],
                    row.get("short_symbol"),
                    row.get("settlement_at"),
                    row.get("settlement_skew_seconds"),
                    row.get("seconds_until_settlement"),
                    float(row.get("preliminary_gross_funding") or 0.0),
                    float(row.get("funding_net_excluding_points") or 0.0),
                    json.dumps(row.get("stablecoin_risk") or {}, sort_keys=True),
                    json.dumps(row.get("capability_status") or {}, sort_keys=True),
                    json.dumps(row.get("blockers") or [], sort_keys=True),
                    json.dumps(row.get("points_metadata") or {}, sort_keys=True),
                    json.dumps(row, sort_keys=True),
                    observed_at,
                    observed_at,
                    now,
                ),
            )
        return key

    def insert_funding_shadow_observation(self, row: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO funding_shadow_observations (
                    opportunity_key, environment, venue, symbol,
                    canonical_asset, side, observed_at, next_funding_at,
                    normalized_next_funding_rate, mark_price, index_price,
                    volume_24h_usd, open_interest_usd,
                    response_received_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["opportunity_key"],
                    row.get("environment", "mainnet"),
                    row["venue"],
                    row.get("symbol"),
                    row["canonical_asset"],
                    row["side"],
                    row["observed_at"],
                    row.get("next_funding_at"),
                    row.get("normalized_next_funding_rate"),
                    row.get("mark_price"),
                    row.get("index_price"),
                    row.get("volume_24h_usd"),
                    row.get("open_interest_usd"),
                    row.get("response_received_at"),
                    json.dumps(row.get("payload") or {}, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def claim_funding_shadow_alert(
        self,
        row: dict[str, Any],
        *,
        claim_ttl_seconds: float = 300.0,
        retry_delay_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> int | None:
        now_dt = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        now_iso = now_dt.isoformat()
        claim_expires_at = (
            now_dt + timedelta(seconds=max(1.0, float(claim_ttl_seconds)))
        ).isoformat()
        payload_json = json.dumps(row.get("payload") or {}, sort_keys=True)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT *
                  FROM funding_shadow_alerts
                 WHERE alert_key = ?
                """,
                (row["alert_key"],),
            ).fetchone()
            if existing is not None:
                state = _stored_alert_state(existing)
                existing_claim_expires = _parse_iso_datetime(existing["claim_expires_at"])
                existing_next_retry = _parse_iso_datetime(existing["next_retry_at"])
                if state in {"SENT", "FAILED_PERMANENT"}:
                    return None
                if (
                    state == "CLAIMED"
                    and existing_claim_expires is not None
                    and existing_claim_expires > now_dt
                ):
                    return None
                if (
                    state in {"FAILED_RETRYABLE", "RETRY_SCHEDULED"}
                    and existing_next_retry is not None
                    and existing_next_retry > now_dt
                ):
                    return None
                attempt_count = int(existing["attempt_count"] or 0) + 1
                connection.execute(
                    """
                    UPDATE funding_shadow_alerts
                       SET opportunity_key = ?,
                           environment = ?,
                           status = ?,
                           message = ?,
                           telegram_status = ?,
                           telegram_error = NULL,
                           state = 'CLAIMED',
                           attempt_count = ?,
                           claimed_at = ?,
                           claim_expires_at = ?,
                           next_retry_at = NULL,
                           payload_json = ?
                     WHERE alert_key = ?
                    """,
                    (
                        row["opportunity_key"],
                        row.get("environment", "mainnet"),
                        row["status"],
                        row["message"],
                        row.get("telegram_status") or "queued",
                        attempt_count,
                        now_iso,
                        claim_expires_at,
                        payload_json,
                        row["alert_key"],
                    ),
                )
                return int(existing["funding_shadow_alert_id"])

            cursor = connection.execute(
                """
                INSERT INTO funding_shadow_alerts (
                    alert_key, opportunity_key, environment, status,
                    message, telegram_status, telegram_error,
                    state, attempt_count, claimed_at, claim_expires_at,
                    payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'CLAIMED', 1, ?, ?, ?, ?)
                """,
                (
                    row["alert_key"],
                    row["opportunity_key"],
                    row.get("environment", "mainnet"),
                    row["status"],
                    row["message"],
                    row.get("telegram_status") or "not_configured",
                    row.get("telegram_error"),
                    now_iso,
                    claim_expires_at,
                    json.dumps(row.get("payload") or {}, sort_keys=True),
                    row.get("created_at") or now_iso,
                ),
            )
        return int(cursor.lastrowid) if cursor.lastrowid else None

    def insert_funding_shadow_alert(self, row: dict[str, Any]) -> int | None:
        return self.claim_funding_shadow_alert(row)

    def update_funding_shadow_alert_status(
        self,
        alert_key: str,
        telegram_status: str,
        telegram_error: str | None = None,
        *,
        retry_delay_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> None:
        now_dt = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        now_iso = now_dt.isoformat()
        normalized_status = str(telegram_status or "").strip().lower()
        redacted_error = redact_sensitive_text(telegram_error)
        state = "FAILED_RETRYABLE"
        sent_at = None
        failed_at = now_iso
        next_retry_at = (
            now_dt + timedelta(seconds=max(1.0, float(retry_delay_seconds)))
        ).isoformat()
        if normalized_status == "sent":
            state = "SENT"
            sent_at = now_iso
            failed_at = None
            next_retry_at = None
        elif normalized_status == "disabled":
            state = "FAILED_PERMANENT"
            next_retry_at = None
        elif normalized_status in {"failed_permanent", "permanent_failure"}:
            state = "FAILED_PERMANENT"
            next_retry_at = None
        elif normalized_status in {"retry_scheduled"}:
            state = "RETRY_SCHEDULED"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE funding_shadow_alerts
                   SET telegram_status = ?,
                       telegram_error = ?,
                       state = ?,
                       sent_at = COALESCE(?, sent_at),
                       failed_at = ?,
                       next_retry_at = ?,
                       last_error_redacted = ?
                 WHERE alert_key = ?
                """,
                (
                    telegram_status,
                    redacted_error,
                    state,
                    sent_at,
                    failed_at,
                    next_retry_at,
                    redacted_error,
                    alert_key,
                ),
            )

    def upsert_funding_shadow_settlement_event(
        self,
        opportunity_key: str,
        event: dict[str, Any],
        *,
        classification: str,
    ) -> int:
        now = utc_now_iso()
        event_id = str(event["event_id"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO funding_shadow_settlement_events (
                    opportunity_key, event_id, classification, venue,
                    environment, symbol, canonical_underlying, leg_id,
                    scheduled_at, earliest_possible_assessment_at,
                    latest_possible_assessment_at, settlement_interval_seconds,
                    displayed_rate_period_seconds, raw_api_rate, normalized_rate,
                    rate_per_next_settlement, receiver_side,
                    expected_cashflow_usd, conservative_cashflow_usd,
                    rate_status, source_event_at, response_received_at,
                    confirmation_source, settlement_semantics_status,
                    evidence_version, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_key, event_id, classification) DO UPDATE SET
                    venue = excluded.venue,
                    environment = excluded.environment,
                    symbol = excluded.symbol,
                    canonical_underlying = excluded.canonical_underlying,
                    leg_id = excluded.leg_id,
                    scheduled_at = excluded.scheduled_at,
                    earliest_possible_assessment_at = excluded.earliest_possible_assessment_at,
                    latest_possible_assessment_at = excluded.latest_possible_assessment_at,
                    settlement_interval_seconds = excluded.settlement_interval_seconds,
                    displayed_rate_period_seconds = excluded.displayed_rate_period_seconds,
                    raw_api_rate = excluded.raw_api_rate,
                    normalized_rate = excluded.normalized_rate,
                    rate_per_next_settlement = excluded.rate_per_next_settlement,
                    receiver_side = excluded.receiver_side,
                    expected_cashflow_usd = excluded.expected_cashflow_usd,
                    conservative_cashflow_usd = excluded.conservative_cashflow_usd,
                    rate_status = excluded.rate_status,
                    source_event_at = excluded.source_event_at,
                    response_received_at = excluded.response_received_at,
                    confirmation_source = excluded.confirmation_source,
                    settlement_semantics_status = excluded.settlement_semantics_status,
                    evidence_version = excluded.evidence_version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    opportunity_key,
                    event_id,
                    classification,
                    event.get("venue"),
                    event.get("environment"),
                    event.get("symbol"),
                    event.get("canonical_underlying"),
                    event.get("leg_id"),
                    event.get("scheduled_at"),
                    event.get("earliest_possible_assessment_at"),
                    event.get("latest_possible_assessment_at"),
                    event.get("settlement_interval_seconds"),
                    event.get("displayed_rate_period_seconds"),
                    event.get("raw_api_rate"),
                    event.get("normalized_rate"),
                    event.get("rate_per_next_settlement"),
                    event.get("receiver_side"),
                    event.get("expected_cashflow_usd"),
                    event.get("conservative_cashflow_usd"),
                    event.get("rate_status"),
                    event.get("source_event_at"),
                    event.get("response_received_at"),
                    event.get("confirmation_source"),
                    event.get("settlement_semantics_status"),
                    event.get("evidence_version"),
                    json.dumps(event, sort_keys=True),
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid or 0)

    def upsert_funding_shadow_venue_health(self, row: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_shadow_venue_health (
                    venue, environment, status, latency_ms,
                    request_started_at, response_received_at,
                    parsing_completed_at, network_latency_ms,
                    parsing_latency_ms, total_latency_ms, endpoint_class,
                    last_error, observed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, environment) DO UPDATE SET
                    status = excluded.status,
                    latency_ms = excluded.latency_ms,
                    request_started_at = excluded.request_started_at,
                    response_received_at = excluded.response_received_at,
                    parsing_completed_at = excluded.parsing_completed_at,
                    network_latency_ms = excluded.network_latency_ms,
                    parsing_latency_ms = excluded.parsing_latency_ms,
                    total_latency_ms = excluded.total_latency_ms,
                    endpoint_class = excluded.endpoint_class,
                    last_error = excluded.last_error,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                WHERE excluded.observed_at >= funding_shadow_venue_health.observed_at
                """,
                (
                    row["venue"],
                    row.get("environment", "mainnet"),
                    row.get("status", "unknown"),
                    row.get("latency_ms"),
                    row.get("request_started_at"),
                    row.get("response_received_at"),
                    row.get("parsing_completed_at"),
                    row.get("network_latency_ms"),
                    row.get("parsing_latency_ms"),
                    row.get("total_latency_ms"),
                    row.get("endpoint_class"),
                    row.get("last_error"),
                    row.get("observed_at") or now,
                    now,
                ),
            )

    def funding_shadow_paper_safety_snapshot(self) -> dict[str, float]:
        tables = {
            "funding_paper_positions": "funding_paper_positions",
            "funding_capture_positions": "funding_capture_positions",
            "funding_paper_orders": "funding_paper_orders",
            "funding_paper_accounts": "funding_paper_accounts",
            "paper_event_ledger": "paper_event_ledger",
        }
        with self.connect() as connection:
            snapshot = {
                name: float(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for name, table in tables.items()
            }
            balances = connection.execute(
                """
                SELECT COALESCE(SUM(cash_balance), 0),
                       COALESCE(SUM(reserved_margin), 0)
                  FROM funding_paper_accounts
                """
            ).fetchone()
            snapshot["funding_paper_accounts_cash_balance"] = float(balances[0] or 0.0)
            snapshot["funding_paper_accounts_reserved_margin"] = float(balances[1] or 0.0)
            ledger = connection.execute(
                "SELECT COALESCE(SUM(cash_delta), 0) FROM paper_event_ledger"
            ).fetchone()
            snapshot["paper_event_ledger_cash_delta"] = float(ledger[0] or 0.0)
            return snapshot

    def prune_funding_shadow_runtime_rows(
        self,
        *,
        keep_observations: int = 10_000,
        keep_events: int = 20_000,
        keep_alerts: int = 5_000,
    ) -> dict[str, int]:
        deleted: dict[str, int] = {}
        with self.connect() as connection:
            for key, table, order_column, keep in (
                (
                    "observations",
                    "funding_shadow_observations",
                    "funding_shadow_observation_id",
                    keep_observations,
                ),
                (
                    "settlement_events",
                    "funding_shadow_settlement_events",
                    "funding_shadow_settlement_event_id",
                    keep_events,
                ),
                (
                    "alerts",
                    "funding_shadow_alerts",
                    "funding_shadow_alert_id",
                    keep_alerts,
                ),
            ):
                cursor = connection.execute(
                    f"""
                    DELETE FROM {table}
                     WHERE {order_column} NOT IN (
                        SELECT {order_column}
                          FROM {table}
                         ORDER BY {order_column} DESC
                         LIMIT ?
                     )
                    """,
                    (max(0, int(keep)),),
                )
                deleted[key] = int(cursor.rowcount if cursor.rowcount is not None else 0)
        return deleted

    def funding_shadow_counts(self) -> dict[str, int]:
        tables = {
            "opportunities": "funding_shadow_opportunities",
            "observations": "funding_shadow_observations",
            "settlement_events": "funding_shadow_settlement_events",
            "alerts": "funding_shadow_alerts",
            "venue_health": "funding_shadow_venue_health",
            "probe_runs": "funding_semantics_probe_runs",
            "probe_observations": "funding_semantics_probe_observations",
            "paper_positions": "funding_capture_positions",
            "paper_orders": "funding_paper_orders",
            "paper_accounts": "funding_paper_accounts",
        }
        with self.connect() as connection:
            return {
                name: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for name, table in tables.items()
            }

    def insert_funding_semantics_probe_run(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        probe_run_id = str(row["probe_run_id"])
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO funding_semantics_probe_runs (
                    probe_run_id, venue, environment, mode, db_path,
                    started_at, completed_at, status, orders_enabled,
                    base_url, max_notional_usd, entry_lead_seconds,
                    max_wait_seconds, confirmation_timeout_seconds,
                    no_telegram, error, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(probe_run_id) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    status = excluded.status,
                    error = excluded.error,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    probe_run_id,
                    row.get("venue", "risex"),
                    row.get("environment", "testnet"),
                    row.get("mode", "public"),
                    row.get("db_path"),
                    row.get("started_at", now),
                    row.get("completed_at"),
                    row.get("status", "RUNNING"),
                    1 if row.get("orders_enabled") else 0,
                    row.get("base_url"),
                    row.get("max_notional_usd"),
                    row.get("entry_lead_seconds"),
                    row.get("max_wait_seconds"),
                    row.get("confirmation_timeout_seconds"),
                    1 if row.get("no_telegram", True) else 0,
                    row.get("error"),
                    json.dumps(row.get("payload") or {}, sort_keys=True),
                    row.get("created_at", now),
                    now,
                ),
            )
        return probe_run_id

    def insert_funding_semantics_probe_observation(self, row: dict[str, Any]) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO funding_semantics_probe_observations (
                    probe_run_id, venue, environment, symbol,
                    scheduled_settlement_at, actual_assessment_at,
                    actual_confirmation_at, entry_lead_seconds,
                    entry_request_at, acknowledgement_at, fill_at,
                    hold_duration_seconds, size, predicted_rate,
                    rate_period_seconds, expected_full_payment,
                    expected_prorata_payment, realized_payment,
                    balance_delta, classification, confidence, errors,
                    raw_evidence_metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["probe_run_id"],
                    row.get("venue", "risex"),
                    row.get("environment", "testnet"),
                    row.get("symbol"),
                    row.get("scheduled_settlement_at"),
                    row.get("actual_assessment_at"),
                    row.get("actual_confirmation_at"),
                    row.get("entry_lead_seconds"),
                    row.get("entry_request_at"),
                    row.get("acknowledgement_at"),
                    row.get("fill_at"),
                    row.get("hold_duration_seconds"),
                    row.get("size"),
                    row.get("predicted_rate"),
                    row.get("rate_period_seconds"),
                    row.get("expected_full_payment"),
                    row.get("expected_prorata_payment"),
                    row.get("realized_payment"),
                    row.get("balance_delta"),
                    row.get("classification"),
                    row.get("confidence"),
                    row.get("errors"),
                    json.dumps(row.get("raw_evidence_metadata") or {}, sort_keys=True),
                    row.get("created_at", now),
                ),
            )
        return int(cursor.lastrowid)

    def funding_paper_pending_reprice_events(
        self,
        limit: int = 20,
        min_created_at: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        age_filter = ""
        if min_created_at:
            age_filter = "AND event.created_at >= ?"
            params.append(str(min_created_at))
        params.append(max(1, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    event.funding_paper_event_id AS event_id,
                    event.created_at AS event_created_at,
                    event.payload_json AS event_payload_json,
                    position.*
                FROM funding_paper_events event
                JOIN funding_paper_positions position
                  ON position.funding_paper_position_id =
                     event.funding_paper_position_id
                WHERE event.event_type = 'pnl_repriced'
                  AND event.telegram_status IN ('not_configured', 'queued')
                  {age_filter}
                ORDER BY event.funding_paper_event_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        events = []
        for raw in rows:
            item = dict(raw)
            event_id = int(item.pop("event_id"))
            event_created_at = item.pop("event_created_at")
            event_payload = json.loads(item.pop("event_payload_json") or "{}")
            position = add_funding_paper_display_fields(
                decode_funding_paper_position(item)
            )
            events.append(
                {
                    "event_id": event_id,
                    "created_at": event_created_at,
                    "payload": event_payload,
                    "position": position,
                }
            )
        return events

    def record_funding_paper_equity_snapshot(self) -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as connection:
            account = dict(
                connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(cash_balance), 0) AS total_cash,
                        COALESCE(SUM(reserved_margin), 0) AS total_reserved_margin,
                        COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                    FROM funding_paper_accounts
                    """
                ).fetchone()
            )
            counts = dict(
                connection.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status IN ('open', 'settlement_pending') THEN 1 ELSE 0 END)
                            AS open_position_count,
                        SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)
                            AS closed_position_count
                    FROM funding_paper_positions
                    """
                ).fetchone()
            )
            v2_rows = connection.execute(
                """
                SELECT p.*, p.config_json
                FROM funding_capture_positions p
                WHERE p.state IN (
                    'OPEN', 'SETTLEMENT_CROSSED', 'POST_SETTLEMENT_EVALUATION',
                    'HOLDING_NEXT_CYCLE', 'EXIT_SCHEDULED', 'EXIT_SUBMITTED',
                    'PARTIALLY_CLOSED', 'EMERGENCY_UNWIND'
                )
                """
            ).fetchall()
            v2_unrealized_price_pnl = 0.0
            v2_estimated_close_fees = 0.0
            v2_net_liquidation_pnl = 0.0
            v2_confirmed_funding_pnl = 0.0
            v2_last_valid_net_liquidation_pnl = 0.0
            equity_quality = "FRESH"
            for raw_position in v2_rows:
                position = dict(raw_position)
                config = json.loads(position.get("config_json") or "{}")
                data_quality = config.get("data_quality") or {}
                snapshot = config.get("last_valid_executable_snapshot") or {}
                if data_quality.get("state") != "HEALTHY" or not snapshot:
                    equity_quality = "INVALID_STALE"
                    last_value = snapshot.get("paper_price_pnl")
                    close_fees = snapshot.get("paper_close_fees")
                    if last_value is not None and close_fees is not None:
                        v2_last_valid_net_liquidation_pnl += (
                            float(last_value) - float(close_fees)
                        )
                    continue
                observed = _parse_iso_datetime(snapshot.get("observed_at"))
                now_dt = _parse_iso_datetime(now) or datetime.now(UTC)
                if observed is None or (now_dt - observed).total_seconds() > 2.0:
                    equity_quality = "INVALID_STALE"
                    if snapshot.get("paper_price_pnl") is not None and snapshot.get("paper_close_fees") is not None:
                        v2_last_valid_net_liquidation_pnl += (
                            float(snapshot["paper_price_pnl"])
                            - float(snapshot["paper_close_fees"])
                        )
                    continue
                price_pnl = float(snapshot.get("paper_price_pnl") or 0.0)
                close_fees = float(snapshot.get("paper_close_fees") or 0.0)
                v2_unrealized_price_pnl += price_pnl
                v2_estimated_close_fees += close_fees
                v2_net_liquidation_pnl += price_pnl - close_fees
                v2_confirmed_funding_pnl += float(
                    snapshot.get("paper_confirmed_funding_pnl") or 0.0
                )
            legacy_open_count = int(counts["open_position_count"] or 0)
            v2_open_count = len(v2_rows)
            total_cash = float(account["total_cash"] or 0.0)
            total_reserved_margin = float(account["total_reserved_margin"] or 0.0)
            snapshot = {
                "observed_at": now,
                "legacy_open_position_count": legacy_open_count,
                "v2_open_position_count": v2_open_count,
                "total_open_position_count": legacy_open_count + v2_open_count,
                "total_cash": total_cash,
                "total_reserved_margin": total_reserved_margin,
                "available_cash": total_cash - total_reserved_margin,
                "v2_unrealized_price_pnl": v2_unrealized_price_pnl,
                "v2_estimated_close_fees": v2_estimated_close_fees,
                "v2_net_liquidation_pnl": v2_net_liquidation_pnl
                if equity_quality == "FRESH"
                else None,
                "v2_last_valid_net_liquidation_pnl": v2_last_valid_net_liquidation_pnl
                if equity_quality == "INVALID_STALE"
                else None,
                "v2_confirmed_funding_pnl": v2_confirmed_funding_pnl,
                "total_equity": total_cash
                + (v2_net_liquidation_pnl if equity_quality == "FRESH" else 0.0),
                "equity_quality": equity_quality,
                "realized_pnl": float(account["realized_pnl"] or 0.0),
                "open_position_count": legacy_open_count + v2_open_count,
                "closed_position_count": int(counts["closed_position_count"] or 0),
            }
            connection.execute(
                """
                INSERT INTO funding_paper_equity_snapshots (
                    observed_at, total_cash, total_reserved_margin,
                    total_equity, realized_pnl, open_position_count,
                    closed_position_count, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["observed_at"],
                    snapshot["total_cash"],
                    snapshot["total_reserved_margin"],
                    snapshot["total_equity"],
                    snapshot["realized_pnl"],
                    snapshot["open_position_count"],
                    snapshot["closed_position_count"],
                    json.dumps(snapshot, sort_keys=True),
                ),
            )
        return snapshot

    def funding_history_rate_near(
        self,
        venue: str,
        symbol: str,
        settlement_at: str,
        tolerance_seconds: int = 900,
    ) -> dict[str, Any] | None:
        center = parse_iso_datetime(settlement_at)
        if center is None:
            return None
        start = center - timedelta(seconds=max(60, int(tolerance_seconds)))
        end = center + timedelta(seconds=max(60, int(tolerance_seconds)))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM funding_rate_history
                WHERE venue = ? AND symbol = ?
                  AND funding_at BETWEEN ? AND ?
                ORDER BY funding_at
                """,
                (str(venue), str(symbol), start.isoformat(), end.isoformat()),
            ).fetchall()
        candidates = [dict(row) for row in rows]
        if not candidates:
            return None

        def distance_seconds(row: dict[str, Any]) -> float:
            funding_at = parse_iso_datetime(row.get("funding_at")) or center
            return abs((funding_at - center).total_seconds())

        return min(candidates, key=distance_seconds)

    def refresh_estimated_funding_paper_positions(self) -> int:
        now = utc_now_iso()
        updated = 0
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM funding_paper_positions
                WHERE status = 'closed'
                  AND COALESCE(CAST(json_extract(
                      settlement_json,
                      '$.history_missing_fallback'
                  ) AS INTEGER), 0) = 1
                ORDER BY closed_at DESC, funding_paper_position_id DESC
                """
            ).fetchall()
            for raw in rows:
                position = decode_funding_paper_position(dict(raw))
                settlement = dict(position.get("settlement") or {})
                new_settlement = dict(settlement)
                old_leg_pnl: dict[str, float] = {}
                new_leg_pnl: dict[str, float] = {}
                history_missing = False
                changed = False
                for side in ("long", "short"):
                    leg = dict((settlement.get(side) or {}))
                    venue = str(position.get(f"{side}_venue") or leg.get("venue") or "")
                    symbol = str(
                        position.get(f"{side}_symbol") or leg.get("symbol") or ""
                    )
                    settlement_at = str(
                        position.get(f"{side}_settlement_at")
                        or leg.get("settlement_at")
                        or ""
                    )
                    notional = float(position.get(f"{side}_notional") or 0.0)
                    old_rate = funding_paper_settlement_rate(leg)
                    old_leg_pnl[side] = funding_paper_leg_pnl(
                        side,
                        notional,
                        old_rate,
                    )
                    history_row = funding_history_rate_near_from_connection(
                        connection,
                        venue,
                        symbol,
                        settlement_at,
                    )
                    if history_row is None:
                        history_missing = True
                        new_rate = old_rate
                    else:
                        new_rate = float(history_row.get("funding_rate") or 0.0)
                        if (
                            leg.get("source") != "history"
                            or abs(new_rate - old_rate) > 1e-12
                        ):
                            changed = True
                        leg.update(
                            {
                                "venue": venue,
                                "symbol": symbol,
                                "settlement_at": settlement_at,
                                "funding_rate": new_rate,
                                "source": "history",
                                "history_row": history_row,
                            }
                        )
                    new_leg_pnl[side] = funding_paper_leg_pnl(
                        side,
                        notional,
                        new_rate,
                    )
                    new_settlement[side] = leg
                if not changed:
                    continue
                actual_funding_pnl = sum(new_leg_pnl.values())
                actual_execution_cost = float(
                    position.get("actual_execution_cost")
                    or position.get("expected_execution_cost")
                    or 0.0
                )
                actual_basis_pnl = float(position.get("actual_basis_pnl") or 0.0)
                actual_net_pnl = (
                    actual_funding_pnl + actual_basis_pnl - actual_execution_cost
                )
                new_settlement["history_missing_fallback"] = history_missing
                notes = dict(position.get("notes") or {})
                notes["pnl_repriced_at"] = now
                notes["pnl_reprice_source"] = "published_funding_history"
                connection.execute(
                    """
                    UPDATE funding_paper_positions
                    SET actual_funding_pnl = ?,
                        actual_basis_pnl = ?,
                        actual_net_pnl = ?,
                        settlement_json = ?,
                        notes_json = ?
                    WHERE funding_paper_position_id = ?
                    """,
                    (
                        actual_funding_pnl,
                        actual_basis_pnl,
                        actual_net_pnl,
                        json.dumps(new_settlement, sort_keys=True),
                        json.dumps(notes, sort_keys=True),
                        int(position["funding_paper_position_id"]),
                    ),
                )
                for side in ("long", "short"):
                    cash_delta = new_leg_pnl[side] - old_leg_pnl[side]
                    if abs(cash_delta) < 1e-12:
                        continue
                    venue = str(position.get(f"{side}_venue") or "")
                    connection.execute(
                        """
                        UPDATE funding_paper_accounts
                        SET cash_balance = cash_balance + ?,
                            realized_pnl = realized_pnl + ?,
                            updated_at = ?
                        WHERE venue = ?
                        """,
                        (cash_delta, cash_delta, now, venue),
                    )
                    account = connection.execute(
                        """
                        SELECT cash_balance, reserved_margin
                        FROM funding_paper_accounts
                        WHERE venue = ?
                        """,
                        (venue,),
                    ).fetchone()
                    if account:
                        connection.execute(
                            """
                            INSERT INTO funding_paper_balance_ledger (
                                created_at, venue, funding_paper_position_id,
                                event_type, cash_delta, reserved_delta,
                                cash_balance_after, reserved_margin_after,
                                payload_json
                            )
                            VALUES (?, ?, ?, 'history_reprice', ?, 0, ?, ?, ?)
                            """,
                            (
                                now,
                                venue,
                                int(position["funding_paper_position_id"]),
                                cash_delta,
                                float(account["cash_balance"]),
                                float(account["reserved_margin"]),
                                json.dumps(
                                    {
                                        "side": side,
                                        "old_leg_pnl": old_leg_pnl[side],
                                        "new_leg_pnl": new_leg_pnl[side],
                                    },
                                    sort_keys=True,
                                ),
                            ),
                        )
                connection.execute(
                    """
                    INSERT INTO funding_paper_events (
                        created_at, event_type, severity,
                        funding_paper_position_id, route_key, message,
                        payload_json, telegram_status
                    )
                    VALUES (?, 'pnl_repriced', 'info', ?, ?, ?, ?, 'not_configured')
                    """,
                    (
                        now,
                        int(position["funding_paper_position_id"]),
                        position.get("route_key"),
                        "Funding paper PnL repriced from published funding history",
                        json.dumps(
                            {
                                "actual_funding_pnl": actual_funding_pnl,
                                "actual_basis_pnl": actual_basis_pnl,
                                "actual_net_pnl": actual_net_pnl,
                                "history_missing_fallback": history_missing,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                updated += 1
        return updated

    def latest_funding_route_by_key(
        self,
        route_key: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT route.*
                FROM funding_routes route
                JOIN funding_scans scan
                  ON scan.funding_scan_id = route.funding_scan_id
                WHERE route.route_key = ?
                  AND scan.status = 'success'
                ORDER BY route.funding_scan_id DESC
                LIMIT 1
                """,
                (str(route_key),),
            ).fetchone()
        return decode_funding_route(dict(row)) if row else None

    def funding_route_by_scan_and_key(
        self,
        funding_scan_id: int,
        route_key: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT route.*
                FROM funding_routes route
                JOIN funding_scans scan
                  ON scan.funding_scan_id = route.funding_scan_id
                WHERE route.funding_scan_id = ?
                  AND route.route_key = ?
                  AND scan.status = 'success'
                LIMIT 1
                """,
                (int(funding_scan_id), str(route_key)),
            ).fetchone()
        return decode_funding_route(dict(row)) if row else None

    def funding_paper_dashboard(
        self,
        *,
        refresh_estimates: bool = True,
    ) -> dict[str, Any]:
        if refresh_estimates:
            self.refresh_estimated_funding_paper_positions()
        with self.connect() as connection:
            accounts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *,
                           cash_balance - reserved_margin AS available_balance
                    FROM funding_paper_accounts
                    ORDER BY venue
                    """
                ).fetchall()
            ]
            open_positions = [
                decode_funding_paper_position(dict(row))
                for row in connection.execute(
                    """
                    SELECT *
                    FROM funding_paper_positions
                    WHERE status IN ('open', 'settlement_pending')
                    ORDER BY max_settlement_at, opened_at
                    """
                ).fetchall()
            ]
            closed_positions = [
                decode_funding_paper_position(dict(row))
                for row in connection.execute(
                    """
                    SELECT *
                    FROM funding_paper_positions
                    WHERE status = 'closed'
                    ORDER BY closed_at DESC
                    LIMIT 100
                    """
                ).fetchall()
            ]
            for position in [*open_positions, *closed_positions]:
                add_funding_paper_display_fields(position)
            events = [
                decode_funding_paper_event(dict(row))
                for row in connection.execute(
                    """
                    SELECT *
                    FROM funding_paper_events
                    ORDER BY funding_paper_event_id DESC
                    LIMIT 100
                    """
                ).fetchall()
            ]
            trade_event_types = ("open", "close", "settlement_pending")
            trade_placeholders = ",".join("?" for _ in trade_event_types)
            trade_events = [
                decode_funding_paper_event(dict(row))
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM funding_paper_events
                    WHERE event_type IN ({trade_placeholders})
                    ORDER BY funding_paper_event_id DESC
                    LIMIT 100
                    """,
                    trade_event_types,
                ).fetchall()
            ]
            system_events = [
                decode_funding_paper_event(dict(row))
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM funding_paper_events
                    WHERE event_type NOT IN ({trade_placeholders})
                    ORDER BY funding_paper_event_id DESC
                    LIMIT 80
                    """,
                    trade_event_types,
                ).fetchall()
            ]
            equity = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM funding_paper_equity_snapshots
                    ORDER BY observed_at DESC
                    LIMIT 300
                    """
                ).fetchall()
            ]
            summary = dict(
                connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(starting_balance), 0) AS starting_capital,
                        COALESCE(SUM(cash_balance), 0) AS total_cash,
                        COALESCE(SUM(reserved_margin), 0) AS reserved_margin,
                        COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                    FROM funding_paper_accounts
                    """
                ).fetchone()
            )
            trade_summary = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS closed_trade_count,
                        SUM(CASE WHEN actual_net_pnl > 0 THEN 1 ELSE 0 END)
                            AS win_count,
                        AVG(actual_net_pnl) AS average_net_pnl,
                        MIN(actual_net_pnl) AS worst_net_pnl,
                        MAX(actual_net_pnl) AS best_net_pnl
                    FROM funding_paper_positions
                    WHERE status = 'closed'
                    """
                ).fetchone()
            )
        starting = float(summary.get("starting_capital") or 0.0)
        total_cash = float(summary.get("total_cash") or 0.0)
        closed_count = int(trade_summary.get("closed_trade_count") or 0)
        win_count = int(trade_summary.get("win_count") or 0)
        return {
            "summary": {
                "starting_capital": starting,
                "total_cash": total_cash,
                "reserved_margin": float(summary.get("reserved_margin") or 0.0),
                "available_cash": total_cash
                - float(summary.get("reserved_margin") or 0.0),
                "realized_pnl": float(summary.get("realized_pnl") or 0.0),
                "return_pct": (total_cash - starting) / starting
                if starting > 0
                else 0.0,
                "open_position_count": len(open_positions),
                "closed_trade_count": closed_count,
                "win_rate": win_count / closed_count if closed_count > 0 else None,
                "average_net_pnl": trade_summary.get("average_net_pnl"),
                "worst_net_pnl": trade_summary.get("worst_net_pnl"),
                "best_net_pnl": trade_summary.get("best_net_pnl"),
            },
            "accounts": accounts,
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "events": events,
            "trade_events": trade_events,
            "system_events": system_events,
            "equity": list(reversed(equity)),
            "execution_mode": "deterministic_paper_trader",
            "llm_in_execution_loop": False,
        }

    def funding_paper_trade_report_rows(
        self,
        *,
        refresh_estimates: bool = True,
    ) -> list[dict[str, Any]]:
        if refresh_estimates:
            self.refresh_estimated_funding_paper_positions()
        with self.connect() as connection:
            rows = [
                decode_funding_paper_position(dict(row))
                for row in connection.execute(
                    """
                    SELECT *
                    FROM funding_paper_positions
                    ORDER BY opened_at DESC, funding_paper_position_id DESC
                    """
                ).fetchall()
            ]
        return [funding_paper_trade_report_row(row) for row in rows]


def decode_funding_route(item: dict[str, Any]) -> dict[str, Any]:
    item["legs"] = json.loads(item.pop("legs_json") or "[]")
    item["rationale"] = json.loads(item.pop("rationale_json") or "[]")
    item["risk_flags"] = json.loads(item.pop("risk_flags_json") or "[]")
    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
    return item


def funding_route_decision_profit(row: dict[str, Any]) -> float:
    evidence = row.get("evidence") or {}
    selected = evidence.get("selected_strategy") or evidence.get(
        "strategy_classification"
    ) or {}
    if selected:
        return safe_float(
            selected.get("expected_net_pnl"),
            evidence.get("current_nowcast_net"),
        )
    if evidence.get("decision_mode") == "settlement_capture":
        return safe_float(evidence.get("current_nowcast_net"))
    return safe_float(
        evidence.get("conservative_net_profit"),
        row.get("expected_net_profit"),
    )


def safe_float(*values: Any) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def funding_history_rate_near_from_connection(
    connection: Any,
    venue: str,
    symbol: str,
    settlement_at: str,
    tolerance_seconds: int = 900,
) -> dict[str, Any] | None:
    center = parse_iso_datetime(settlement_at)
    if center is None:
        return None
    start = center - timedelta(seconds=max(60, int(tolerance_seconds)))
    end = center + timedelta(seconds=max(60, int(tolerance_seconds)))
    rows = connection.execute(
        """
        SELECT *
        FROM funding_rate_history
        WHERE venue = ? AND symbol = ?
          AND funding_at BETWEEN ? AND ?
        ORDER BY funding_at
        """,
        (str(venue), str(symbol), start.isoformat(), end.isoformat()),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    if not candidates:
        return None

    def distance_seconds(row: dict[str, Any]) -> float:
        funding_at = parse_iso_datetime(row.get("funding_at")) or center
        return abs((funding_at - center).total_seconds())

    return min(candidates, key=distance_seconds)


def funding_paper_settlement_rate(leg: dict[str, Any]) -> float:
    return safe_float(leg.get("funding_rate"))


def funding_paper_leg_pnl(side: str, notional: float, funding_rate: float) -> float:
    if str(side).lower() == "long":
        return -float(notional) * float(funding_rate)
    return float(notional) * float(funding_rate)


def decode_funding_paper_position(item: dict[str, Any]) -> dict[str, Any]:
    item["entry_legs"] = json.loads(item.pop("entry_legs_json") or "[]")
    item["entry_evidence"] = json.loads(item.pop("entry_evidence_json") or "{}")
    item["close_legs"] = json.loads(item.pop("close_legs_json") or "[]")
    item["close_evidence"] = json.loads(item.pop("close_evidence_json") or "{}")
    item["settlement"] = json.loads(item.pop("settlement_json") or "{}")
    item["notes"] = json.loads(item.pop("notes_json") or "{}")
    item["target_notional_per_leg"] = item.get("target_notional")
    return item


def add_funding_paper_display_fields(item: dict[str, Any]) -> dict[str, Any]:
    item["close_reason_label"] = funding_paper_close_reason_label(
        item.get("close_reason")
    )
    item["pnl_quality_label"] = funding_paper_pnl_quality_label(item)
    item["close_window_label"] = funding_paper_close_window_label(item)
    return item


def funding_paper_trade_report_row(row: dict[str, Any]) -> dict[str, Any]:
    strategy = (row.get("notes") or {}).get("strategy") or {}
    strategy_name = strategy.get("strategy_name") or (row.get("notes") or {}).get(
        "strategy_name"
    )
    return {
        "ID": row.get("funding_paper_position_id"),
        "Статус": funding_paper_status_label(row.get("status")),
        "Стратегия": strategy_name or "",
        "Открыто": report_datetime(row.get("opened_at")),
        "Закрыто": report_datetime(row.get("closed_at")),
        "Актив": row.get("canonical_asset"),
        "Маршрут": (
            f"LONG {row.get('long_venue')} / SHORT {row.get('short_venue')}"
        ),
        "Long": row.get("long_venue"),
        "Short": row.get("short_venue"),
        "Объем $": report_money(row.get("target_notional")),
        "Long notional $": report_money(row.get("long_notional")),
        "Short notional $": report_money(row.get("short_notional")),
        "Base qty": report_quantity(row.get("base_quantity")),
        "Expected net $": report_money(row.get("expected_live_net")),
        "Funding PnL $": report_money(row.get("actual_funding_pnl")),
        "Basis PnL $": report_money(row.get("actual_basis_pnl")),
        "Costs $": report_money(row.get("actual_execution_cost")),
        "Net PnL $": report_money(row.get("actual_net_pnl")),
        "Причина закрытия": funding_paper_close_reason_label(
            row.get("close_reason")
        ),
        "Состояние окна при закрытии": funding_paper_close_window_label(row),
        "Качество PnL": funding_paper_pnl_quality_label(row),
    }


def report_datetime(value: Any) -> str:
    timestamp = parse_iso_datetime(value)
    if timestamp is None:
        return ""
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def report_money(value: Any) -> str:
    number = optional_report_float(value)
    return "" if number is None else f"{number:.2f}"


def report_quantity(value: Any) -> str:
    number = optional_report_float(value)
    if number is None:
        return ""
    return f"{number:.2f}"


def optional_report_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def funding_paper_status_label(value: Any) -> str:
    return {
        "open": "Открыта",
        "settlement_pending": "Ждет settlement",
        "closed": "Закрыта",
    }.get(str(value or ""), str(value or ""))


def funding_paper_close_reason_label(value: Any) -> str:
    raw = str(value or "")
    if raw.startswith("price_stop_loss"):
        return (
            "Закрыто по price stop-loss: цена ушла от входа сильнее порога, "
            "обе ноги закрыты одновременно."
        )
    if raw.startswith("spread_stop_loss"):
        return (
            "Закрыто по basis/spread stop-loss: hedge spread ушел против позиции "
            "сильнее порога."
        )
    return {
        "arbitrage_window_closed_live_net_non_positive": (
            "Окно арбитража закрылось: live net стал неположительным."
        ),
        "arbitrage_window_data_quality_issue": (
            "Закрыто: свежие данные маршрута выглядят несовместимыми."
        ),
        "arbitrage_window_unverifiable_route_missing": (
            "Закрыто: свежий route snapshot не найден, продолжение окна не подтверждено."
        ),
        "arbitrage_window_unverifiable_route_stale": (
            "Закрыто: route snapshot устарел, продолжение окна не подтверждено."
        ),
        "arbitrage_window_unverifiable_next_settlement_missing": (
            "Закрыто: не удалось определить следующий funding settlement."
        ),
        "arbitrage_window_unverifiable_live_net_missing": (
            "Закрыто: не удалось проверить live net."
        ),
        "arbitrage_window_unverifiable": (
            "Закрыто: продолжение арбитражного окна не подтверждено."
        ),
        "settlement_capture_complete": (
            "Закрыто старой логикой paper-теста. Новая логика удерживает "
            "позицию, пока окно остается положительным."
        ),
        "settlement_capture_complete_estimated_missing_history": (
            "Закрыто старой логикой paper-теста. Новая логика удерживает "
            "позицию, пока окно остается положительным."
        ),
        "first_settlement_pair_complete": (
            "Закрыто старой логикой paper-теста. Новая логика удерживает "
            "позицию, пока окно остается положительным."
        ),
        "test": "Тестовое закрытие.",
        "": "",
    }.get(raw, raw)


def funding_paper_pnl_quality_label(row: dict[str, Any]) -> str:
    if row.get("status") != "closed":
        return ""
    settlement = row.get("settlement") or {}
    if settlement.get("history_missing_fallback"):
        return (
            "Предварительный PnL: одна или обе funding history не были "
            "опубликованы вовремя, поэтому расчет сделан по ставкам на входе."
        )
    return "Финальный PnL: рассчитан по опубликованной funding history."


def funding_paper_close_window_label(row: dict[str, Any]) -> str:
    if row.get("status") != "closed":
        return ""
    evidence = row.get("close_evidence") or {}
    live_net = optional_report_float(evidence.get("current_nowcast_net"))
    if live_net is not None:
        if live_net > 0:
            return (
                "Окно при закрытии еще выглядело положительным: live net "
                f"{report_money(live_net)}."
            )
        if live_net < 0:
            return (
                "Окно при закрытии уже не выглядело положительным: live net "
                f"{report_money(live_net)}."
            )
        return "Окно при закрытии было около нуля: live net $0.00."
    if row.get("close_funding_route_id"):
        return "Close-scan был, но live net в нем не рассчитан."
    return "Свежий route snapshot при закрытии не найден."


def decode_funding_paper_event(item: dict[str, Any]) -> dict[str, Any]:
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    return item


def funding_horizon_comparisons(
    connection: Any,
    route_keys: list[str],
    horizons: tuple[float, ...] = (4.0, 8.0),
) -> dict[str, Any]:
    clean_keys = sorted({str(key) for key in route_keys if key})
    if not clean_keys:
        return {}
    placeholders = ",".join("?" for _ in clean_keys)
    result: dict[str, Any] = {}
    for hours in horizons:
        scan_row = connection.execute(
            """
            SELECT *
            FROM funding_scans
            WHERE status = 'success'
              AND json_extract(config_json, '$.horizon_mode') = 'fixed'
              AND ABS(COALESCE(
                    CAST(json_extract(config_json, '$.horizon_hours') AS REAL),
                    24.0
                  ) - ?) < 0.001
            ORDER BY funding_scan_id DESC
            LIMIT 1
            """,
            (hours,),
        ).fetchone()
        if not scan_row:
            continue
        scan = dict(scan_row)
        scan["config"] = json.loads(scan.pop("config_json") or "{}")
        scan["scan_mode"] = scan["config"].get("scan_mode", "manual")
        scan["duration_seconds"] = elapsed_seconds(
            scan.get("started_at"),
            scan.get("finished_at"),
        )
        rows = connection.execute(
            f"""
            SELECT *
            FROM funding_routes
            WHERE funding_scan_id = ?
              AND route_key IN ({placeholders})
              AND market_capacity >= ?
            """,
            [scan["funding_scan_id"], *clean_keys, FUNDING_MINIMUM_ACTIONABLE_NOTIONAL],
        ).fetchall()
        route_map: dict[str, Any] = {}
        for raw in rows:
            route = decode_funding_route(dict(raw))
            evidence = route.get("evidence") or {}
            forecast = evidence.get("forecast") or {}
            schedule_nowcast_rate = first_finite_float(
                forecast.get("schedule_nowcast_rate"),
                evidence.get("current_nowcast_settlement_rate"),
            )
            funding_notional = first_finite_float(
                evidence.get("funding_notional"),
                route.get("target_notional"),
            )
            execution_cost = first_finite_float(evidence.get("execution_cost")) or 0.0
            live_if_persists_gross = None
            live_if_persists_net = None
            if schedule_nowcast_rate is not None and funding_notional is not None:
                live_if_persists_gross = funding_notional * schedule_nowcast_rate
                live_if_persists_net = live_if_persists_gross - execution_cost
            route_map[str(route["route_key"])] = {
                "route_key": route["route_key"],
                "status": route["status"],
                "canonical_asset": route["canonical_asset"],
                "long_venue": route["long_venue"],
                "short_venue": route["short_venue"],
                "expected_net_profit": route.get("expected_net_profit"),
                "market_capacity": route.get("market_capacity"),
                "risk_flags": route.get("risk_flags") or [],
                "evidence": {
                    "current_nowcast_net": evidence.get("current_nowcast_net"),
                    "conservative_net_profit": evidence.get(
                        "conservative_net_profit"
                    ),
                    "decision_mode": evidence.get("decision_mode"),
                    "net_profit_probability": evidence.get(
                        "net_profit_probability"
                    ),
                    "horizon": evidence.get("horizon"),
                    "schedule_nowcast_rate": schedule_nowcast_rate,
                    "funding_notional": funding_notional,
                    "execution_cost": execution_cost,
                    "live_if_persists_gross": live_if_persists_gross,
                    "live_if_persists_net": live_if_persists_net,
                },
            }
        result[f"{int(hours)}h"] = {
            "latest_scan": scan,
            "routes": route_map,
        }
    return result


def add_current_live_horizon_projections(
    current_routes: list[dict[str, Any]],
    comparisons: dict[str, Any],
    horizons: tuple[float, ...] = (4.0, 8.0),
) -> dict[str, Any]:
    # The dashboard columns "4h/8h if live spread holds" must be a projection
    # from the same current route snapshot as Next PnL. Reusing older fixed
    # horizon scans here makes a fresh positive candidate look negative just
    # because the fixed scan was taken at another time.
    result: dict[str, Any] = {}
    for hours in horizons:
        horizon_key = f"{int(hours)}h"
        block = result.setdefault(horizon_key, {"latest_scan": {}, "routes": {}})
        routes = block.setdefault("routes", {})
        for route in current_routes:
            route_key = str(route.get("route_key") or "")
            if not route_key:
                continue
            projection = current_live_hold_projection(route, hours)
            comparison = {
                "route_key": route_key,
                "status": route.get("status"),
                "canonical_asset": route.get("canonical_asset"),
                "long_venue": route.get("long_venue"),
                "short_venue": route.get("short_venue"),
                "expected_net_profit": None,
                "market_capacity": route.get("market_capacity"),
                "risk_flags": route.get("risk_flags") or [],
                "evidence": projection or {
                    "live_projection_source": "current_route_snapshot",
                    "live_projection_unavailable_reason": (
                        "missing_current_route_schedule"
                    ),
                    "live_if_persists_gross": None,
                    "live_if_persists_net": None,
                },
            }
            routes[route_key] = comparison
    return result


def current_live_hold_projection(
    route: dict[str, Any],
    horizon_hours: float,
) -> dict[str, Any] | None:
    legs = route.get("legs") or []
    long_leg = next((leg for leg in legs if leg.get("side") == "long"), None)
    short_leg = next((leg for leg in legs if leg.get("side") == "short"), None)
    if not long_leg or not short_leg:
        return None
    start = parse_iso_datetime(route.get("observed_at"))
    if start is None:
        return None
    schedule = live_hold_settlement_schedule(
        long_leg,
        short_leg,
        start,
        horizon_hours,
    )
    long_hourly = funding_leg_hourly_rate(long_leg)
    short_hourly = funding_leg_hourly_rate(short_leg)
    if long_hourly is None or short_hourly is None:
        return None
    base_projection = {
        "live_projection_source": "current_route_snapshot",
        "live_projection_observed_at": route.get("observed_at"),
        "live_projection_horizon": schedule,
        "schedule_nowcast_rate": None,
        "funding_notional": first_finite_float(
            (route.get("evidence") or {}).get("funding_notional"),
            route.get("target_notional"),
        ),
        "execution_cost": first_finite_float(
            (route.get("evidence") or {}).get("execution_cost")
        )
        or 0.0,
        "live_if_persists_gross": None,
        "live_if_persists_net": None,
    }
    if not schedule["settlements"]:
        return {
            **base_projection,
            "live_projection_unavailable_reason": "no_settlement_in_horizon",
        }
    if float(schedule.get("paired_coverage_hours") or 0.0) <= 0.0:
        return {
            **base_projection,
            "live_projection_unavailable_reason": (
                "no_paired_settlement_in_horizon"
            ),
        }
    schedule_nowcast_rate = sum(
        short_hourly * float(row.get("short_settlement_interval_hours") or 0.0)
        - long_hourly * float(row.get("long_settlement_interval_hours") or 0.0)
        for row in schedule["settlements"]
    )
    evidence = route.get("evidence") or {}
    funding_notional = first_finite_float(
        evidence.get("funding_notional"),
        route.get("target_notional"),
    )
    if funding_notional is None:
        return None
    execution_cost = first_finite_float(evidence.get("execution_cost")) or 0.0
    live_if_persists_gross = funding_notional * schedule_nowcast_rate
    live_if_persists_net = live_if_persists_gross - execution_cost
    return {
        **base_projection,
        "live_projection_unavailable_reason": None,
        "schedule_nowcast_rate": schedule_nowcast_rate,
        "funding_notional": funding_notional,
        "execution_cost": execution_cost,
        "live_if_persists_gross": live_if_persists_gross,
        "live_if_persists_net": live_if_persists_net,
    }


def live_hold_settlement_schedule(
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
    start: datetime,
    horizon_hours: float,
) -> dict[str, Any]:
    end = start + timedelta(hours=max(0.0, float(horizon_hours or 0.0)))
    events: list[tuple[datetime, str, float]] = []
    events.extend(leg_settlement_events(long_leg, start, end, "long"))
    events.extend(leg_settlement_events(short_leg, start, end, "short"))
    events.sort(key=lambda item: (item[0], item[1]))

    long_coverage = 0.0
    short_coverage = 0.0
    paired_coverage = 0.0
    rows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(events):
        event_at = events[cursor][0]
        settled_sides: list[str] = []
        long_interval = 0.0
        short_interval = 0.0
        while cursor < len(events) and events[cursor][0] == event_at:
            _, side, interval_hours = events[cursor]
            if side == "long":
                long_coverage += interval_hours
                long_interval += interval_hours
            else:
                short_coverage += interval_hours
                short_interval += interval_hours
            settled_sides.append(side)
            cursor += 1
        new_paired_coverage = min(long_coverage, short_coverage)
        added_coverage = max(0.0, new_paired_coverage - paired_coverage)
        paired_coverage = new_paired_coverage
        rows.append(
            {
                "settlement_at": event_at.isoformat(),
                "hours_from_start": max(
                    0.0,
                    (event_at - start).total_seconds() / 3600.0,
                ),
                "paired_coverage_hours": added_coverage,
                "cumulative_paired_coverage_hours": paired_coverage,
                "long_settlement_interval_hours": long_interval,
                "short_settlement_interval_hours": short_interval,
                "settled_sides": sorted(set(settled_sides)),
            }
        )
    return {
        "horizon_mode": "live_hold_projection",
        "horizon_hours": float(horizon_hours),
        "horizon_label": f"{float(horizon_hours):g}h if live spread holds",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "long_next_settlement_at": parse_iso_datetime(
            long_leg.get("next_funding_at")
        ).isoformat()
        if parse_iso_datetime(long_leg.get("next_funding_at"))
        else None,
        "short_next_settlement_at": parse_iso_datetime(
            short_leg.get("next_funding_at")
        ).isoformat()
        if parse_iso_datetime(short_leg.get("next_funding_at"))
        else None,
        "paired_coverage_hours": paired_coverage,
        "settlement_event_count": len(rows),
        "long_settlement_count": sum(
            float(row["long_settlement_interval_hours"]) > 0 for row in rows
        ),
        "short_settlement_count": sum(
            float(row["short_settlement_interval_hours"]) > 0 for row in rows
        ),
        "settlements": rows,
    }


def leg_settlement_events(
    leg: dict[str, Any],
    start: datetime,
    end: datetime,
    side: str,
) -> list[tuple[datetime, str, float]]:
    next_funding = normalized_leg_next_settlement(leg, start)
    interval_hours = funding_leg_interval_hours(leg)
    if next_funding is None or interval_hours is None:
        return []
    interval = timedelta(hours=interval_hours)
    output: list[tuple[datetime, str, float]] = []
    cursor = next_funding
    while cursor <= end and len(output) < 1_000:
        output.append((cursor, side, interval_hours))
        cursor += interval
    return output


def normalized_leg_next_settlement(
    leg: dict[str, Any],
    start: datetime,
) -> datetime | None:
    next_funding = parse_iso_datetime(leg.get("next_funding_at"))
    interval_hours = funding_leg_interval_hours(leg)
    if next_funding is None or interval_hours is None:
        return None
    interval = timedelta(hours=interval_hours)
    if next_funding <= start:
        lag = start - next_funding
        if lag > min(interval / 4, timedelta(minutes=5)):
            return None
        next_funding += interval
    return next_funding


def funding_leg_interval_hours(leg: dict[str, Any]) -> float | None:
    value = first_finite_float(leg.get("funding_interval_hours"))
    return max(1.0, value) if value is not None else None


def funding_leg_hourly_rate(leg: dict[str, Any]) -> float | None:
    hourly = first_finite_float(leg.get("hourly_funding_rate"))
    if hourly is not None:
        return hourly
    rate = first_finite_float(leg.get("funding_rate"))
    interval = funding_leg_interval_hours(leg)
    if rate is None or interval is None:
        return None
    return rate / interval


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def elapsed_seconds(started_at: Any, finished_at: Any) -> float | None:
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (finished - started).total_seconds())


def normalize_symbol(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if not value or len(value) > 30:
        return ""
    return value


def normalize_chain_address(chain_id: str, address: Any) -> str:
    value = str(address or "").strip()
    return value if chain_id == "solana" else value.lower()


def is_distribution_listing_title(title: str) -> bool:
    lowered = str(title).lower()
    return any(
        phrase in lowered
        for phrase in ("hodler airdrops", "launchpool", "megadrop")
    )


def is_initial_binance_listing_title(title: str) -> bool:
    lowered = str(title).lower()
    excluded_phrases = (
        "notice on new trading pairs",
        "new trading pairs",
        "trading bots services",
        "will add",
        "adds ",
        " on earn",
        "buy crypto",
        "convert & margin",
        "zero trading fee",
        "futures",
        "margin trading",
        "delist",
        "removes",
        "will support",
    )
    if any(phrase in lowered for phrase in excluded_phrases):
        return False
    if is_distribution_listing_title(title):
        return True
    return any(
        phrase in lowered
        for phrase in (
            "will list ",
            "will list(",
            "will list:",
            "binance lists ",
            "binance to list ",
            "will list and",
        )
    )


def registry_mapping_status(
    symbols: list[str],
    contract_links: list[dict[str, Any]],
) -> str:
    if len(symbols) == 1 and contract_links:
        return "single_symbol_contract_link"
    if len(symbols) == 1:
        return "single_symbol_no_contract"
    if contract_links:
        return "multi_symbol_contracts_manual_review"
    return "multi_symbol_no_contract_manual_review"


def registry_confidence(
    category: str,
    symbols: list[str],
    contract_links: list[dict[str, Any]],
) -> float:
    score = 0.45
    if category == "spot":
        score += 0.25
    elif category == "alpha_or_airdrop":
        score += 0.15
    if len(symbols) == 1:
        score += 0.15
    if contract_links:
        score += 0.1
    return min(score, 0.95)


def pairs_for_symbol(symbol: str, pairs: list[str]) -> list[str]:
    prefix = f"{symbol}/"
    return sorted(pair for pair in pairs if pair.startswith(prefix))


def normalize_dune_timestamp(value: Any) -> str:
    """Normalize Dune and Binance timestamps to one UTC storage identity."""
    text = str(value).strip().replace(" UTC", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def normalize_optional_dune_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return normalize_dune_timestamp(str(value))


def normalize_dune_array(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def first_finite_float(*values: Any) -> float | None:
    for value in values:
        parsed = optional_float(value)
        if parsed is not None:
            return parsed
    return None


def median_float(values: list[float]) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def funding_instrument_collapse_audit(
    connection: sqlite3.Connection,
    scan_id: int,
) -> dict[str, Any]:
    selected_symbols: dict[tuple[str, str], set[str]] = {}
    for row in connection.execute(
        """
        SELECT canonical_asset, long_venue AS venue, long_symbol AS symbol
        FROM funding_route_universe
        WHERE funding_scan_id = ?
        UNION
        SELECT canonical_asset, short_venue AS venue, short_symbol AS symbol
        FROM funding_route_universe
        WHERE funding_scan_id = ?
        """,
        (scan_id, scan_id),
    ).fetchall():
        key = (str(row["canonical_asset"]), str(row["venue"]))
        selected_symbols.setdefault(key, set()).add(str(row["symbol"]))

    market_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT venue, symbol, canonical_asset, funding_rate,
                   hourly_funding_rate, mark_price, index_price,
                   open_interest_usd, volume_24h_usd, observed_at
            FROM funding_market_snapshots
            WHERE funding_scan_id = ?
            ORDER BY canonical_asset, venue, symbol
            """,
            (scan_id,),
        ).fetchall()
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in market_rows:
        key = (str(row["canonical_asset"]), str(row["venue"]))
        groups.setdefault(key, []).append(row)
    asset_venues: dict[str, set[str]] = {}
    venue_coverage: Counter[str] = Counter()
    for asset, venue in groups:
        asset_venues.setdefault(asset, set()).add(venue)
        venue_coverage[venue] += 1

    duplicate_groups: list[dict[str, Any]] = []
    for (asset, venue), rows in groups.items():
        if len(rows) <= 1:
            continue
        open_interest_values = [
            float(row["open_interest_usd"])
            for row in rows
            if row.get("open_interest_usd") is not None
            and math.isfinite(float(row["open_interest_usd"]))
        ]
        volume_values = [
            float(row["volume_24h_usd"])
            for row in rows
            if row.get("volume_24h_usd") is not None
            and math.isfinite(float(row["volume_24h_usd"]))
        ]
        mark_prices = [
            float(row["mark_price"])
            for row in rows
            if row.get("mark_price") is not None
            and math.isfinite(float(row["mark_price"]))
        ]
        funding_rates = [
            float(row["hourly_funding_rate"])
            for row in rows
            if row.get("hourly_funding_rate") is not None
            and math.isfinite(float(row["hourly_funding_rate"]))
        ]
        duplicate_groups.append(
            {
                "canonical_asset": asset,
                "venue": venue,
                "instrument_count": len(rows),
                "symbols": [str(row["symbol"]) for row in rows],
                "selected_symbols": sorted(selected_symbols.get((asset, venue), set())),
                "max_open_interest_usd": max(open_interest_values)
                if open_interest_values
                else None,
                "max_volume_24h_usd": max(volume_values) if volume_values else None,
                "mark_price_min": min(mark_prices) if mark_prices else None,
                "mark_price_max": max(mark_prices) if mark_prices else None,
                "hourly_funding_rate_min": min(funding_rates)
                if funding_rates
                else None,
                "hourly_funding_rate_max": max(funding_rates)
                if funding_rates
                else None,
            }
        )
    duplicate_groups.sort(
        key=lambda row: (
            int(row["instrument_count"]),
            float(row.get("max_open_interest_usd") or 0),
            float(row.get("max_volume_24h_usd") or 0),
        ),
        reverse=True,
    )
    raw_market_count = len(market_rows)
    canonical_market_count = len(groups)
    asset_route_counts = [
        {
            "canonical_asset": asset,
            "venue_count": len(venues),
            "route_count": len(venues) * (len(venues) - 1) // 2,
            "venues": sorted(venues),
        }
        for asset, venues in asset_venues.items()
    ]
    asset_route_counts.sort(
        key=lambda row: (
            int(row["route_count"]),
            int(row["venue_count"]),
            str(row["canonical_asset"]),
        ),
        reverse=True,
    )
    theoretical_route_count = sum(
        int(row["route_count"]) for row in asset_route_counts
    )
    asset_count = len(asset_venues)
    return {
        "raw_market_count": raw_market_count,
        "canonical_venue_asset_count": canonical_market_count,
        "canonical_asset_count": asset_count,
        "average_venues_per_asset": safe_ratio(canonical_market_count, asset_count),
        "theoretical_route_count": theoretical_route_count,
        "top_assets_by_route_count": asset_route_counts[:10],
        "venue_coverage": [
            {"venue": venue, "asset_count": count}
            for venue, count in venue_coverage.most_common()
        ],
        "collapsed_market_count": max(0, raw_market_count - canonical_market_count),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups[:25],
        "selection_policy": "one_symbol_per_canonical_asset_venue_for_route_generation",
    }


def safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def existing_row_id(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    value: Any,
) -> int | None:
    if value is None:
        return None
    try:
        row_id = int(value)
    except (TypeError, ValueError):
        return None
    if row_id <= 0:
        return None
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
        (row_id,),
    ).fetchone()
    return row_id if row else None


def optional_bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def classify_holding_behavior(
    pre_buy_usd: float,
    pre_sell_usd: float,
    post_sell_usd: float,
    pre_sell_ratio: float,
    post_sell_ratio: float,
) -> str:
    if pre_buy_usd < 100:
        if pre_sell_usd > 0 or post_sell_usd > 0:
            return "dust_or_prior_holder"
        return "dust_accumulator"
    if pre_sell_ratio >= 0.8:
        return "pre_listing_flipper"
    if pre_sell_ratio >= 0.35:
        return "partial_pre_listing_seller"
    if post_sell_ratio >= 0.8:
        return "post_listing_seller"
    if post_sell_ratio >= 0.35:
        return "partial_post_listing_seller"
    if pre_sell_ratio <= 0.05 and post_sell_ratio <= 0.05:
        return "strong_accumulator"
    return "accumulator"
