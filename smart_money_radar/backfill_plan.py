from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.dune_queries import (
    write_base_identity_graph_sql,
    write_base_shared_routes_sql,
    write_wallet_opportunity_sql,
    write_wallet_weekly_flows_sql,
    write_weekly_evm_universe_sql,
)
from smart_money_radar.storage import SQLiteStore, utc_now_iso
from smart_money_radar.wallet_research import research_wallet_selection


def prepare_dune_backfill(
    store: SQLiteStore,
    output_root: Path | None = None,
) -> dict[str, Any]:
    root = output_root or PROJECT_ROOT / "queries" / "generated"
    root.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []

    for chain_id in ("base", "bsc", "ethereum"):
        path = root / f"{chain_id}_weekly_universe_v4.generated.sql"
        write_weekly_evm_universe_sql(path, chain_id=chain_id)
        artifacts.append(sql_artifact(path, "weekly_universe_v4", chain_id))

    wallets_200 = research_wallet_selection(store, "base", max_wallets=200)
    wallets_50 = wallets_200[:50]
    opportunity_50 = root / "base_wallet_opportunities_50_v3.generated.sql"
    opportunity_200 = root / "base_wallet_opportunities_200_v3.generated.sql"
    weekly_flows = root / "base_wallet_weekly_flows_200_v3.generated.sql"
    identity = root / "base_identity_graph_v3.generated.sql"
    routes = root / "base_identity_routes_v3.generated.sql"
    write_wallet_opportunity_sql(opportunity_50, "base", wallets_50)
    write_wallet_opportunity_sql(opportunity_200, "base", wallets_200)
    write_wallet_weekly_flows_sql(weekly_flows, "base", wallets_200)
    write_base_identity_graph_sql(identity, wallets_200)
    write_base_shared_routes_sql(routes, wallets_200)
    for path, kind in (
        (opportunity_50, "wallet_opportunities_50"),
        (opportunity_200, "wallet_opportunities_200"),
        (weekly_flows, "wallet_weekly_flows_200"),
        (identity, "identity_funding_hops"),
        (routes, "identity_shared_routes"),
    ):
        artifacts.append(sql_artifact(path, kind, "base"))

    commands = [
        "python3 -m smart_money_radar.cli dune-usage",
        "python3 -m smart_money_radar.cli research-universe-probe --chain base",
        "python3 -m smart_money_radar.cli research-universe-backfill --chain base",
        "python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 50",
        "python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 200",
        "python3 -m smart_money_radar.cli wallet-weekly-flows --chain base --max-wallets 200",
        "python3 -m smart_money_radar.cli identity-graph-v2 --max-wallets 200",
        "python3 -m smart_money_radar.cli train-research-models --chain base",
        "python3 -m smart_money_radar.cli research-status",
    ]
    manifest = {
        "prepared_at": utc_now_iso(),
        "executes_dune": False,
        "wallet_count": len(wallets_200),
        "universe_contract": {
            "version": "v4_dense_90d_followup",
            "minimum_weekly_volume_usd": 200_000,
            "minimum_weekly_trades": 20,
            "minimum_weekly_traders": 10,
            "followup_weeks": 13,
            "export_column_count": 14,
            "budget_preflight_required": True,
        },
        "artifacts": artifacts,
        "recommended_commands": commands,
        "stop_conditions": [
            "Stop if Dune usage has not reset.",
            "Stop if dune-export-plan is not affordable with a 10% reserve.",
            "Inspect the Base threshold probe before the large universe export.",
            "Do not train a model if the dataset gate remains blocked.",
            "Unknown identity coverage must remain ineligible for live capital.",
        ],
    }
    manifest_path = PROJECT_ROOT / "exports" / "dune_backfill_manifest_v4.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def sql_artifact(path: Path, kind: str, chain_id: str) -> dict[str, Any]:
    sql = path.read_text(encoding="utf-8")
    checks = {
        "nonempty": bool(sql.strip()),
        "point_in_time_cutoff": "block_time <" in sql or "announced_at" in sql,
        "no_dune_execution": True,
    }
    if kind == "weekly_universe_v4":
        checks.update(
            {
                "completed_weeks_only": "DATE_TRUNC('week', CURRENT_TIMESTAMP)" in sql,
                "explicit_zero_followup": "is_dense_zero" in sql,
                "ninety_day_followup": "SEQUENCE(0, 13)" in sql,
            }
        )
    return {
        "kind": kind,
        "chain_id": chain_id,
        "path": str(path),
        "bytes": len(sql.encode("utf-8")),
        "checks": checks,
        "valid": all(checks.values()),
    }
