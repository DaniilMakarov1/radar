#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from smart_money_radar.funding.fees import (  # noqa: E402
    FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
    FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
    FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    fee_evidence_status,
)
from smart_money_radar.funding.readiness_policy import (  # noqa: E402
    EvaluationMode,
    modeled_fee_rate,
)
from smart_money_radar.funding.route_identity import (  # noqa: E402
    ROUTE_IDENTITY_SCHEMA_VERSION,
    canonical_opportunity_key,
    route_identity_summary,
)
from smart_money_radar.funding.trader import (  # noqa: E402
    PaperBot,
    PaperBotConfig,
    serializable_config,
)
from smart_money_radar.paper_bot.clock import FakeClock  # noqa: E402
from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route  # noqa: E402
from smart_money_radar.storage import SQLiteStore  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"
TRUTHY = {"1", "true", "yes", "on", "enabled"}
ACTIVE_STATES = {
    "DISCOVERED",
    "ARMED",
    "ENTRY_SUBMITTED",
    "OPEN",
    "SETTLEMENT_CROSSED",
    "POST_SETTLEMENT_EVALUATION",
    "HOLDING_NEXT_CYCLE",
    "EXIT_SUBMITTED",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _record(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    *,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    item: dict[str, Any] = {"name": name, "status": status}
    if details:
        item["details"] = details
    if error:
        item["error"] = error
    checks.append(item)


def _check(
    checks: list[dict[str, Any]],
    name: str,
    fn: Callable[[], dict[str, Any] | None],
) -> None:
    try:
        details = fn() or {}
    except Exception as exc:  # pragma: no cover - gate should report, not raise.
        _record(checks, name, FAIL, error=f"{type(exc).__name__}: {exc}")
        return
    _record(checks, name, PASS, details=details)


def _fee_market(
    *,
    kind: str = FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    source_kind: str = "configured_trusted_fee",
    now: datetime,
    observed_at: str | None = None,
    reviewed_at: str | None = None,
    account_observed_at: str | None = None,
    rate: float = 0.0005,
) -> dict[str, Any]:
    evidence = {
        "fee_evidence_kind": kind,
        "source_kind": source_kind,
        "source_identifier": "launch-gate-fee-source",
        "trust_status": "CONFIGURED_TRUSTED"
        if kind == FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
        else "OFFICIAL",
        "venue": "binance",
        "liquidity_role": "taker",
        "market_observed_at": now.isoformat(),
        "reviewed_at": reviewed_at or now.isoformat(),
        "fee_schedule_reviewed_at": reviewed_at or now.isoformat(),
        "environment": "mainnet",
        "product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "applicability": "taker",
        "evidence_version": "launch-gate-fee-v1",
    }
    if observed_at:
        evidence["observed_at"] = observed_at
        evidence["fee_source_observed_at"] = observed_at
    if account_observed_at:
        evidence["account_fee_observed_at"] = account_observed_at
    return {
        "venue": "binance",
        "environment": "mainnet",
        "product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "taker_fee_rate": rate,
        "fee_source": source_kind,
        "fee_evidence": evidence,
        "response_received_at": now.isoformat(),
    }


def _identity_route(
    *,
    now: datetime,
    multiplier: Any = 1,
    timestamp: str | None = None,
    product_id: str | None = None,
    symbol: str = "BTCUSDT",
    collateral: str = "USDT",
    environment: str = "mainnet",
) -> dict[str, Any]:
    settlement = timestamp or (now + timedelta(minutes=1)).isoformat()
    return {
        "route_key": "BTC:binance:bybit",
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "short_venue": "bybit",
        "legs": [
            {
                "side": "long",
                "venue": "binance",
                "environment": environment,
                "symbol": symbol,
                "product_id": product_id,
                "canonical_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": collateral,
                "contract_type": "linear_perpetual",
                "product_type": "linear_perpetual",
                "contract_multiplier": multiplier,
                "canonical_unit_multiplier": "1.000000",
                "next_funding_at": settlement,
            },
            {
                "side": "short",
                "venue": "bybit",
                "environment": environment,
                "symbol": symbol,
                "canonical_asset": "BTC",
                "quote_asset": "USDT",
                "collateral_asset": collateral,
                "contract_type": "linear_perpetual",
                "product_type": "linear_perpetual",
                "contract_multiplier": "1.0",
                "canonical_unit_multiplier": 1,
                "next_funding_at": settlement,
            },
        ],
    }


def _check_live_disabled() -> dict[str, Any]:
    live_env = {
        name: "set"
        for name in (
            "RADAR_LIVE_ENABLED",
            "SMART_MONEY_RADAR_LIVE_ENABLED",
            "FUNDING_LIVE_ENABLED",
            "FUNDING_PAPER_LIVE_ENABLED",
            "LIVE_TRADING_ENABLED",
        )
        if str(os.getenv(name, "")).strip().lower() in TRUTHY
    }
    if live_env:
        raise AssertionError(f"live env toggles enabled: {sorted(live_env)}")
    config = PaperBotConfig().validated()
    if any(item != "synchronized_funding_capture" for item in config.strategy_set):
        raise AssertionError(f"unexpected default strategies: {config.strategy_set}")
    return {
        "live_enabled": False,
        "strategy_set": list(config.strategy_set),
    }


def _check_fee_freshness() -> dict[str, Any]:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    stale_reviewed = "2026-07-29T00:00:00+00:00"
    stale_market = _fee_market(
        now=now,
        observed_at=now.isoformat(),
        reviewed_at=stale_reviewed,
    )
    stale = fee_evidence_status(stale_market, now=now)
    modeled = modeled_fee_rate(stale_market, now=now)
    if stale["verified"] is not False:
        raise AssertionError("stale static fee schedule verified")
    if stale["observed_at"] != stale_reviewed:
        raise AssertionError("market snapshot updated static fee freshness")
    if modeled["verified"] is not False or "fee_fallback_used" not in modeled["risk_flags"]:
        raise AssertionError("stale fee did not apply fallback/reserve")
    if modeled["uncertainty_reserve_bps"] <= 0:
        raise AssertionError("stale fee did not apply uncertainty reserve")

    fresh_static = fee_evidence_status(
        _fee_market(now=now, reviewed_at=now.isoformat()),
        now=now,
    )
    public = fee_evidence_status(
        _fee_market(
            kind=FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
            source_kind="official_public_fee_endpoint",
            now=now,
            observed_at=now.isoformat(),
        ),
        now=now,
    )
    account = fee_evidence_status(
        _fee_market(
            kind=FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
            source_kind="official_account_fee_endpoint",
            now=now,
            account_observed_at=now.isoformat(),
        ),
        now=now,
    )
    for label, status in {
        "fresh_static": fresh_static,
        "account_endpoint": account,
    }.items():
        if status["verified"] is not True:
            raise AssertionError(f"{label} fee evidence not verified: {status}")
    if public["verified"] is not False:
        raise AssertionError(f"public_endpoint fee evidence unexpectedly verified: {public}")
    if public.get("blocker") != "taker_fee_account_applicability_unverified":
        raise AssertionError(f"public_endpoint fee blocker mismatch: {public}")
    return {
        "stale_static": {
            "fee_evidence_kind": stale["fee_evidence_kind"],
            "market_observed_at": stale["market_observed_at"],
            "observed_at": stale["observed_at"],
            "expires_at": stale["expires_at"],
            "blocker": stale["blocker"],
            "fallback_required": stale["fallback_required"],
            "uncertainty_reserve_required": stale["uncertainty_reserve_required"],
        },
        "fresh_kinds": {
            "static": fresh_static["fee_evidence_kind"],
            "public": public["fee_evidence_kind"],
            "account": account["fee_evidence_kind"],
        },
        "public_endpoint": {
            "verified": public["verified"],
            "blocker": public["blocker"],
            "account_applicable": public["account_applicable"],
        },
    }


def _check_identity() -> dict[str, Any]:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    z_time = "2026-07-30T13:00:00Z"
    offset_time = "2026-07-30T16:00:00+03:00"
    base = _identity_route(now=now, multiplier=1, timestamp=z_time)
    float_mult = _identity_route(now=now, multiplier=1.0, timestamp=offset_time)
    string_mult = _identity_route(now=now, multiplier="1.000000", timestamp=offset_time)
    enriched = _identity_route(
        now=now,
        multiplier="1.000000",
        timestamp=offset_time,
        product_id="BTC-USDT-PERP",
    )
    base_summary = route_identity_summary(base)
    if base_summary["identity_schema_version"] != ROUTE_IDENTITY_SCHEMA_VERSION:
        raise AssertionError("unexpected route identity schema")
    keys = {
        canonical_opportunity_key(base),
        canonical_opportunity_key(float_mult),
        canonical_opportunity_key(string_mult),
        canonical_opportunity_key(enriched),
    }
    variants = {
        route_identity_summary(base)["route_variant_key"],
        route_identity_summary(float_mult)["route_variant_key"],
        route_identity_summary(string_mult)["route_variant_key"],
        route_identity_summary(enriched)["route_variant_key"],
    }
    if len(keys) != 1 or len(variants) != 1:
        raise AssertionError("canonical identity changed for formatting/alias enrichment")
    usdc = route_identity_summary(_identity_route(now=now, collateral="USDC"))
    testnet = route_identity_summary(_identity_route(now=now, environment="testnet"))
    if usdc["route_variant_key"] == base_summary["route_variant_key"]:
        raise AssertionError("collateral did not separate route variants")
    if testnet["route_variant_key"] == base_summary["route_variant_key"]:
        raise AssertionError("environment did not separate route variants")
    return {
        "schema_version": ROUTE_IDENTITY_SCHEMA_VERSION,
        "route_family_key": base_summary["route_family_key"],
        "route_variant_key": base_summary["route_variant_key"],
        "canonical_opportunity_key": base_summary["canonical_opportunity_key"],
        "alias_count": len(base_summary["product_identity_aliases"]),
    }


def _check_experimental_guard() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="radar-launch-gate-runtime-") as tmp:
        store = SQLiteStore(Path(tmp) / "runtime.sqlite")
        store.init_db()
        bot = PaperBot(
            store,
            PaperBotConfig(telegram_enabled=False).validated(),
            clock=FakeClock(datetime(2026, 7, 30, 12, 0, tzinfo=UTC)),
        )
        guard = bot.synchronized_runtime._verified_readiness_guard(
            {
                "planner": {
                    "evaluation_mode": EvaluationMode.EXPERIMENTAL_SIMULATION.value,
                    "route_readiness": {
                        "verified_paper_ready": False,
                        "experimental_simulation_ready": True,
                        "verified_paper_blockers": ["simulated_execution_only"],
                    },
                }
            }
        )
    if guard["passed"] is not False:
        raise AssertionError("experimental simulation passed verified guard")
    return {
        "experimental_guard_passed": guard["passed"],
        "blockers": guard["blockers"],
    }


def _check_db(path: Path, *, allow_migration: bool) -> dict[str, Any]:
    store = SQLiteStore(path)
    if allow_migration:
        store.init_db()
        before = _db_digest(path)
        store.init_db()
        after = _db_digest(path)
        if before != after:
            raise AssertionError("second migration changed DB content")
    integrity = _sqlite_scalar(path, "PRAGMA integrity_check;")
    foreign_keys = _sqlite_fetchall(path, "PRAGMA foreign_key_check;")
    if integrity != "ok":
        raise AssertionError(f"integrity_check={integrity}")
    if foreign_keys:
        raise AssertionError(f"foreign_key_check returned {len(foreign_keys)} rows")
    identity = store.funding_capture_active_identity_issues()
    if not identity["ok"]:
        raise AssertionError("active identity issues present")
    account = store.paper_account_consistency_report()
    if not account["ok"]:
        raise AssertionError("paper account/ledger consistency mismatch")
    duplicates = _duplicate_ledger_events(path)
    if duplicates:
        raise AssertionError("duplicate ledger event keys present")
    user_version = _sqlite_scalar(path, "PRAGMA user_version;")
    return {
        "db_path": str(path),
        "allow_migration": allow_migration,
        "integrity_check": integrity,
        "foreign_key_check_rows": len(foreign_keys),
        "schema_user_version": user_version,
        "active_identity_issues": identity,
        "account_consistency_ok": account["ok"],
        "duplicate_ledger_event_keys": len(duplicates),
    }


def _sqlite_scalar(path: Path, sql: str) -> Any:
    with sqlite3.connect(path) as connection:
        row = connection.execute(sql).fetchone()
    return row[0] if row else None


def _sqlite_fetchall(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(sql).fetchall()


def _db_digest(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        version = connection.execute("PRAGMA user_version;").fetchone()[0]
    return _sha256_json({"user_version": version, "schema": [tuple(row) for row in rows]})


def _duplicate_ledger_events(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT event_key, COUNT(*) AS count
                FROM paper_event_ledger
                WHERE COALESCE(event_key, '') != ''
                GROUP BY event_key
                HAVING COUNT(*) > 1
                ORDER BY count DESC, event_key
                """
            ).fetchall()
        ]


def _check_runtime_scripts() -> dict[str, Any]:
    commands = [
        [sys.executable, "scripts/run_synchronized_funding_paper_mvp.py"],
        [sys.executable, "scripts/run_synchronized_funding_two_cycle_paper_mvp.py"],
    ]
    results = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"{' '.join(command)} failed rc={completed.returncode}: "
                f"{completed.stderr[-1000:]}"
            )
        last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
        try:
            parsed = json.loads(last_line)
        except json.JSONDecodeError:
            parsed = {"last_line": last_line}
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "last_event": parsed,
            }
        )
    return {"runtime_checks": results}


def _check_live_enabled_venues(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"db_checked": False, "live_enabled_route_legs": 0}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT funding_route_id, route_key, legs_json
            FROM funding_routes
            ORDER BY funding_scan_id DESC, funding_route_id DESC
            LIMIT 500
            """
        ).fetchall()
    live_enabled = []
    for row in rows:
        try:
            legs = json.loads(row["legs_json"] or "[]")
        except json.JSONDecodeError:
            legs = []
        for leg in legs if isinstance(legs, list) else []:
            if isinstance(leg, dict) and leg.get("live_enabled") is True:
                live_enabled.append(
                    {
                        "funding_route_id": row["funding_route_id"],
                        "route_key": row["route_key"],
                        "venue": leg.get("venue"),
                        "symbol": leg.get("symbol"),
                    }
                )
    if live_enabled:
        raise AssertionError("live-enabled route legs present")
    return {"db_checked": True, "live_enabled_route_legs": 0}


def _prepare_db(args: argparse.Namespace) -> tuple[Path, tempfile.TemporaryDirectory[str] | None, bool]:
    if args.db:
        path = Path(args.db).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"DB does not exist: {path}")
        return path, None, bool(args.allow_db_migration)
    tmp = tempfile.TemporaryDirectory(prefix="radar-launch-gate-db-")
    path = Path(tmp.name) / "launch-gate.sqlite"
    store = SQLiteStore(path)
    store.init_db()
    store.ensure_funding_paper_accounts(["binance", "bybit"], 10_000.0)
    return path, tmp, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Existing production SQLite DB to inspect.")
    parser.add_argument(
        "--allow-db-migration",
        action="store_true",
        help="Run application init/migration on --db before checking idempotency.",
    )
    parser.add_argument(
        "--skip-runtime-scripts",
        action="store_true",
        help="Report runtime MVP checks as NOT_RUN. Overall status will not be PASS.",
    )
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []
    config = PaperBotConfig().validated()
    commit = _git(["rev-parse", "HEAD"])
    config_hash = _sha256_json(serializable_config(config))
    tmp: tempfile.TemporaryDirectory[str] | None = None
    db_path: Path | None = None
    allow_migration = False

    _check(checks, "live_global_disabled", _check_live_disabled)
    _check(checks, "fee_freshness_semantics", _check_fee_freshness)
    _check(checks, "canonical_identity_semantics", _check_identity)
    _check(checks, "experimental_execution_guard", _check_experimental_guard)
    try:
        db_path, tmp, allow_migration = _prepare_db(args)
        _check(
            checks,
            "sqlite_migration_identity_account_ledger",
            lambda: _check_db(db_path, allow_migration=allow_migration),
        )
        _check(
            checks,
            "live_enabled_venues_absent",
            lambda: _check_live_enabled_venues(db_path),
        )
    except Exception as exc:
        _record(checks, "sqlite_migration_identity_account_ledger", FAIL, error=str(exc))
    finally:
        if tmp is not None:
            tmp.cleanup()
    if args.skip_runtime_scripts:
        _record(
            checks,
            "restart_reconciliation_mvp",
            NOT_RUN,
            details={"reason": "skip_runtime_scripts"},
        )
    else:
        _check(checks, "restart_reconciliation_mvp", _check_runtime_scripts)

    if any(item["status"] == FAIL for item in checks):
        status = FAIL
    elif any(item["status"] == NOT_RUN for item in checks):
        status = NOT_RUN
    else:
        status = PASS
    payload = {
        "status": status,
        "commit_sha": commit,
        "config_hash": config_hash,
        "db_path": str(db_path) if db_path else None,
        "db_mode": "existing" if args.db else "temporary",
        "db_migration_allowed": allow_migration,
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    print(json.dumps(payload, sort_keys=True, indent=2, default=_json_default))
    return 0 if status == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
