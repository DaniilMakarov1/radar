from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_money_radar.attention import enrich_attention_snapshots
from smart_money_radar.config import PROJECT_ROOT, x_social_gate_enabled
from smart_money_radar.decision import build_capital_readiness, build_signal_decision
from smart_money_radar.dune_queries import write_generated_live_radar_sql
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient, write_json
from smart_money_radar.ingestion.market import (
    DexScreenerClient,
    GoPlusClient,
    MarketDataError,
)
from smart_money_radar.ingestion.onchain import (
    BlockscoutClient,
    HyperSyncClient,
    MoralisClient,
    OnchainDataError,
)
from smart_money_radar.ingestion.social import GdeltSocialClient, build_social_snapshot
from smart_money_radar.scoring.wallets import MODEL_VERSION
from smart_money_radar.shadow import refresh_signal_shadow_marks
from smart_money_radar.storage import SQLiteStore, utc_now_iso


DEFAULT_LIVE_SQL_PATH = PROJECT_ROOT / "queries/generated/base_live_radar.generated.sql"
DEFAULT_LIVE_RESULT_PATH = PROJECT_ROOT / "exports/base_live_radar.json"
MAX_LIVE_FLOW_AGE_HOURS = 168.0
MIN_INDEPENDENT_FLOW_USD = 5_000.0
CONCENTRATION_WARNING_SHARE = 0.65
CONCENTRATION_BLOCK_SHARE = 0.80


def run_base_live_scan(
    store: SQLiteStore,
    window_hours: int = 336,
    min_trade_usd: float = 25.0,
    max_wallets: int = 200,
    max_tokens_to_enrich: int = 100,
    sql_path: Path = DEFAULT_LIVE_SQL_PATH,
    result_path: Path = DEFAULT_LIVE_RESULT_PATH,
    dry_run: bool = False,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    wallets = qualified_live_wallets(store, max_wallets=max_wallets)
    excluded_addresses = sorted(
        {
            row["contract_address"].lower()
            for row in store.chain_backtest_targets("base")
        }
    )
    write_generated_live_radar_sql(
        output_path=sql_path,
        wallets=wallets,
        excluded_token_addresses=excluded_addresses,
        window_hours=window_hours,
        min_trade_usd=min_trade_usd,
    )
    if dry_run:
        return {
            "dry_run": True,
            "tracked_wallet_count": len(wallets),
            "excluded_training_token_count": len(excluded_addresses),
            "sql_path": str(sql_path),
        }
    if not wallets:
        return {
            "dry_run": False,
            "tracked_wallet_count": 0,
            "observation_count": 0,
            "signal_count": 0,
            "status": "no_qualified_wallets",
        }

    client = DuneClient()
    execution = client.execute_sql(
        sql_path.read_text(encoding="utf-8"),
        performance="medium",
    )
    execution_id = execution["execution_id"]
    store.record_dune_execution(
        execution_id=execution_id,
        source="dune_base_live_radar",
        state=execution.get("state", "submitted"),
        sql_file=str(sql_path),
        output_file=str(result_path),
    )
    try:
        result = client.poll_results(
            execution_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=30,
        )
        write_json(result_path, result)
        rows = result.get("result", {}).get("rows", [])
        store.finish_dune_execution(
            execution_id=execution_id,
            state=result.get("state", "QUERY_STATE_COMPLETED"),
            row_count=len(rows),
            output_file=str(result_path),
        )
    except DuneAPIError as exc:
        store.finish_dune_execution(
            execution_id=execution_id,
            state="failed",
            error=str(exc),
            output_file=str(result_path),
        )
        raise

    observation_count = store.import_radar_observations(
        chain_id="base",
        execution_id=execution_id,
        rows=rows,
    )
    observations = store.latest_radar_observations(limit=max_tokens_to_enrich)
    token_addresses = [row["token_address"] for row in observations]
    enrichment_errors = []
    market_count = 0
    risk_count = 0
    social_count = 0
    attention_count = 0
    observed_at = utc_now_iso()
    if token_addresses:
        try:
            market_snapshots = DexScreenerClient().token_market_snapshots(
                chain_id="base",
                token_addresses=token_addresses,
                observed_at=observed_at,
            )
            market_count = store.upsert_token_market_snapshots(market_snapshots)
        except MarketDataError as exc:
            enrichment_errors.append(f"dexscreener: {exc}")
        try:
            risk_snapshots = GoPlusClient().token_risk_snapshots(
                chain_id="base",
                token_addresses=token_addresses,
                observed_at=observed_at,
            )
            risk_count = store.upsert_token_risk_snapshots(risk_snapshots)
        except MarketDataError as exc:
            enrichment_errors.append(f"goplus: {exc}")
        if x_social_gate_enabled():
            try:
                social_count = enrich_social_snapshots(
                    store,
                    max_tokens=min(max_tokens_to_enrich, 3),
                    observed_at=observed_at,
                )
            except Exception as exc:
                enrichment_errors.append(f"social: {exc}")
        try:
            attention_result = enrich_attention_snapshots(
                store,
                max_tokens=min(max_tokens_to_enrich, 3),
            )
            attention_count = attention_result["snapshot_count"]
            enrichment_errors.extend(
                f"attention: {error}" for error in attention_result["errors"]
            )
        except Exception as exc:
            enrichment_errors.append(f"attention: {exc}")

    signals = recompute_live_signals(store)
    onchain_result = enrich_onchain_snapshots(
        store,
        max_tokens=min(max_tokens_to_enrich, 10),
        observed_at=observed_at,
    )
    enrichment_errors.extend(onchain_result["errors"])
    if onchain_result["snapshot_count"]:
        signals = recompute_live_signals(store)
    return {
        "dry_run": False,
        "status": "completed",
        "execution_id": execution_id,
        "tracked_wallet_count": len(wallets),
        "observation_count": observation_count,
        "market_snapshot_count": market_count,
        "risk_snapshot_count": risk_count,
        "social_snapshot_count": social_count,
        "attention_snapshot_count": attention_count,
        "onchain_snapshot_count": onchain_result["snapshot_count"],
        "onchain_snapshots_by_source": onchain_result["by_source"],
        "signal_count": len(signals),
        "qualified_signal_count": sum(
            1 for row in signals if row["status"] == "candidate"
        ),
        "enrichment_errors": enrichment_errors,
        "result_path": str(result_path),
    }


def qualified_live_wallets(
    store: SQLiteStore,
    max_wallets: int = 200,
) -> list[dict[str, Any]]:
    rows = store.wallet_score_rows(
        model_version=MODEL_VERSION,
        limit=max_wallets * 2,
        labels=("strong_candidate", "watch_candidate"),
    )
    return [
        row
        for row in rows
        if row["target_count"] >= 2
        and row["confidence_score"] >= 35
        and row["noise_score"] < 65
    ][:max_wallets]


def recompute_live_signals(store: SQLiteStore) -> list[dict[str, Any]]:
    observations = store.latest_radar_observations(limit=500)
    dataset_target_count = store.observed_target_count()
    require_x_social = x_social_gate_enabled()
    capital_readiness = build_capital_readiness(
        store.research_dashboard(),
        identity=store.identity_coverage_summary(),
        shadow=store.signal_shadow_summary(),
    )
    signals = [
        build_signal(
            attach_wallet_context(store, row),
            dataset_target_count=dataset_target_count,
            require_x_social=require_x_social,
            capital_readiness=capital_readiness,
        )
        for row in observations
    ]
    store.upsert_signals(signals)
    refresh_signal_shadow_marks(store)
    return signals


def enrich_social_snapshots(
    store: SQLiteStore,
    max_tokens: int = 3,
    observed_at: str | None = None,
) -> int:
    observations = store.latest_radar_observations(limit=max_tokens)
    if not observations:
        return 0

    gdelt = GdeltSocialClient()
    snapshots = [
        build_social_snapshot(
            chain_id=row["chain_id"],
            token_address=row["token_address"],
            token_name=row.get("market_token_name"),
            token_symbol=row.get("market_token_symbol") or row.get("token_symbol"),
            observed_at=observed_at,
            gdelt=gdelt,
        )
        for row in observations
    ]
    return store.upsert_token_social_snapshots(snapshots)


def enrich_onchain_snapshots(
    store: SQLiteStore,
    max_tokens: int = 10,
    observed_at: str | None = None,
) -> dict[str, Any]:
    signals = store.dashboard_signals(limit=500)
    targets = [
        row
        for row in signals
        if row.get("status") in {"candidate", "needs_review"}
    ][: max(1, max_tokens)]
    if not targets:
        return {
            "target_count": 0,
            "snapshot_count": 0,
            "by_source": {},
            "errors": [],
        }

    timestamp = observed_at or utc_now_iso()
    blockscout = BlockscoutClient()
    moralis = MoralisClient()
    hypersync = HyperSyncClient()
    snapshots = []
    errors = []
    for target in targets:
        chain_id = target["chain_id"]
        token_address = target["contract_address"]
        wallets = [
            row["wallet_address"]
            for row in target.get("evidence", {}).get("wallets", [])
            if row.get("wallet_address")
        ]
        try:
            snapshots.append(
                blockscout.token_snapshot(
                    chain_id,
                    token_address,
                    observed_at=timestamp,
                )
            )
        except OnchainDataError as exc:
            errors.append(f"blockscout:{target['token_symbol']}: {exc}")
        if moralis.api_key:
            try:
                snapshots.append(
                    moralis.token_holder_snapshot(
                        chain_id,
                        token_address,
                        observed_at=timestamp,
                    )
                )
            except OnchainDataError as exc:
                errors.append(f"moralis:{target['token_symbol']}: {exc}")
        if hypersync.api_token and wallets:
            try:
                snapshots.append(
                    hypersync.recent_wallet_transfer_snapshot(
                        chain_id,
                        token_address,
                        wallets,
                        observed_at=timestamp,
                    )
                )
            except OnchainDataError as exc:
                errors.append(f"hypersync:{target['token_symbol']}: {exc}")

    stored_count = store.upsert_token_onchain_snapshots(snapshots)
    by_source: dict[str, int] = {}
    for snapshot in snapshots:
        source = str(snapshot["source"])
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "target_count": len(targets),
        "snapshot_count": stored_count,
        "by_source": by_source,
        "errors": errors,
    }


def attach_wallet_context(
    store: SQLiteStore,
    observation: dict[str, Any],
) -> dict[str, Any]:
    item = dict(observation)
    evidence = dict(item.get("evidence") or {})
    addresses = [
        str(address).lower()
        for address in evidence.get("accumulating_wallet_addresses", [])
        if address
    ]
    contexts = store.wallet_context_for_addresses(addresses, model_version=MODEL_VERSION)
    flow_by_address: dict[str, float] = {}
    for flow in evidence.get("accumulating_wallet_flows", []):
        address = str(flow.get("wallet_address") or "").lower()
        net_flow = optional_float(flow.get("net_buy_usd"))
        if address and net_flow is not None and net_flow > 0:
            flow_by_address[address] = flow_by_address.get(address, 0.0) + net_flow

    context_by_address = {
        str(row["wallet_address"]).lower(): row for row in contexts
    }
    cluster_flows: dict[str, float] = {}
    supported_flow_by_address: dict[str, float] = {}
    for address, net_flow in flow_by_address.items():
        context = context_by_address.get(address)
        if not context or not context.get("cluster_id"):
            continue
        context["live_net_buy_usd"] = net_flow
        independence_status = str(
            (context.get("cluster_evidence") or {}).get("independence_status")
            or "unknown"
        )
        context["independence_status"] = independence_status
        if independence_status != "supported":
            continue
        supported_flow_by_address[address] = net_flow
        cluster_id = str(context["cluster_id"])
        cluster_flows[cluster_id] = cluster_flows.get(cluster_id, 0.0) + net_flow

    detected_cluster_ids = {
        row["cluster_id"] for row in contexts if row.get("cluster_id")
    }
    supported_cluster_ids = {
        str(row["cluster_id"])
        for row in contexts
        if row.get("cluster_id")
        and str((row.get("cluster_evidence") or {}).get("independence_status"))
        == "supported"
    }
    wallet_count = int(item.get("tracked_wallet_count") or 0)
    context_complete = bool(
        addresses
        and len(context_by_address) == len(set(addresses))
        and len(detected_cluster_ids) > 0
        and all(row.get("cluster_id") for row in contexts)
        and all(row.get("entity_checked") for row in contexts)
    )
    observed_positive_flow_total = sum(flow_by_address.values())
    positive_flow_total = sum(supported_flow_by_address.values())
    identity_flow_coverage = (
        positive_flow_total / observed_positive_flow_total
        if observed_positive_flow_total > 0
        else 0.0
    )
    top_wallet_flow = max(supported_flow_by_address.values(), default=0.0)
    top_cluster_flow = max(cluster_flows.values(), default=0.0)
    flow_concentration_available = bool(
        context_complete
        and positive_flow_total > 0
        and identity_flow_coverage >= 0.95
        and len(cluster_flows) == len(supported_cluster_ids)
    )
    item["wallet_context"] = contexts
    item["detected_cluster_count"] = len(detected_cluster_ids)
    item["cluster_count"] = len(supported_cluster_ids)
    item["effective_wallet_count"] = (
        len(supported_cluster_ids) if flow_concentration_available else 0
    )
    item["cluster_independence_available"] = flow_concentration_available
    item["identity_flow_coverage"] = identity_flow_coverage
    item["unknown_identity_flow_usd"] = max(
        0.0, observed_positive_flow_total - positive_flow_total
    )
    item["cluster_independence_ratio"] = (
        len(supported_cluster_ids) / wallet_count
        if wallet_count and supported_cluster_ids
        else None
    )
    item["flow_concentration_available"] = flow_concentration_available
    item["positive_wallet_flow_usd"] = positive_flow_total
    item["top_wallet_net_buy_share"] = (
        top_wallet_flow / positive_flow_total if positive_flow_total else None
    )
    item["top_cluster_net_buy_share"] = (
        top_cluster_flow / positive_flow_total if positive_flow_total else None
    )
    item["residual_independent_net_buy_usd"] = (
        positive_flow_total - top_cluster_flow
        if flow_concentration_available
        else None
    )
    item["cluster_flows"] = [
        {"cluster_id": cluster_id, "net_buy_usd": net_flow}
        for cluster_id, net_flow in sorted(
            cluster_flows.items(),
            key=lambda pair: pair[1],
            reverse=True,
        )
    ]
    return item


def build_signal(
    observation: dict[str, Any],
    dataset_target_count: int,
    require_x_social: bool = False,
    capital_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gross_buy = float(observation.get("gross_buy_usd") or 0)
    gross_sell = float(observation.get("gross_sell_usd") or 0)
    net_buy = float(observation.get("net_buy_usd") or 0)
    wallet_count = int(observation.get("tracked_wallet_count") or 0)
    strong_wallet_count = int(observation.get("strong_wallet_count") or 0)
    weighted_wallet_score = float(observation.get("weighted_wallet_score") or 0)
    liquidity = optional_float(observation.get("liquidity_usd"))
    volume_24h = optional_float(observation.get("volume_24h_usd"))
    fdv = optional_float(observation.get("fdv_usd"))
    market_cap = optional_float(observation.get("market_cap_usd"))
    risk_score = optional_float(observation.get("risk_score"))
    website_url = observation.get("website_url")
    social_links = observation.get("social_links") or []
    is_honeypot = observation.get("is_honeypot")
    is_open_source = observation.get("is_open_source")
    buy_tax = optional_float(observation.get("buy_tax"))
    sell_tax = optional_float(observation.get("sell_tax"))
    holder_count = optional_int(observation.get("holder_count"))
    top_holder_ratio = optional_float(observation.get("top_holder_ratio"))
    blockscout_contract_verified = optional_bool(
        observation.get("blockscout_contract_verified")
    )
    blockscout_is_contract = optional_bool(observation.get("blockscout_is_contract"))
    blockscout_is_scam = optional_bool(observation.get("blockscout_is_scam"))
    blockscout_reputation = observation.get("blockscout_reputation")
    blockscout_proxy_type = observation.get("blockscout_proxy_type")
    blockscout_implementations = observation.get("blockscout_implementations") or []
    blockscout_holder_count = optional_int(observation.get("blockscout_holder_count"))
    blockscout_flags = observation.get("blockscout_flags") or []
    moralis_top10_ratio = optional_float(observation.get("moralis_top10_holder_ratio"))
    moralis_top10_eoa_ratio = optional_float(
        observation.get("moralis_top10_eoa_holder_ratio")
    )
    moralis_top10_contract_ratio = optional_float(
        observation.get("moralis_top10_contract_holder_ratio")
    )
    moralis_labeled_holder_ratio = optional_float(
        observation.get("moralis_labeled_holder_ratio")
    )
    moralis_flags = observation.get("moralis_flags") or []
    hypersync_inbound_wallet_count = optional_int(
        observation.get("hypersync_inbound_wallet_count")
    )
    hypersync_outbound_wallet_count = optional_int(
        observation.get("hypersync_outbound_wallet_count")
    )
    hypersync_transfer_count = optional_int(observation.get("hypersync_transfer_count"))
    hypersync_last_activity_at = observation.get("hypersync_last_activity_at")
    hypersync_flags = observation.get("hypersync_flags") or []
    boosts_active = int(observation.get("boosts_active") or 0)
    social_silence = optional_float(observation.get("social_silence_score"))
    social_coverage = optional_float(observation.get("social_coverage_score")) or 0.0
    social_x_available = bool(observation.get("social_x_available"))
    social_provider_counts = observation.get("social_provider_counts") or {}
    social_flags = observation.get("social_flags") or []
    public_attention_score = optional_float(observation.get("public_attention_score"))
    onchain_attention_score = optional_float(observation.get("onchain_attention_score"))
    attention_gap_score = optional_float(observation.get("attention_gap_score"))
    attention_coverage_score = optional_float(
        observation.get("attention_coverage_score")
    ) or 0.0
    attention_flags = observation.get("attention_flags") or []
    onchain_trader_growth = optional_float(
        observation.get("onchain_trader_growth_7d")
    )
    onchain_volume_growth = optional_float(
        observation.get("onchain_volume_growth_7d")
    )
    listing_probability_90d = optional_float(
        observation.get("listing_probability_90d")
    )
    cluster_count = int(observation.get("cluster_count") or 0)
    cluster_independence_available = bool(
        observation.get("cluster_independence_available")
    )
    cluster_independence_ratio = optional_float(
        observation.get("cluster_independence_ratio")
    )
    effective_wallet_count = int(observation.get("effective_wallet_count") or 0)
    detected_cluster_count = int(observation.get("detected_cluster_count") or 0)
    identity_flow_coverage = optional_float(
        observation.get("identity_flow_coverage")
    ) or 0.0
    unknown_identity_flow = optional_float(
        observation.get("unknown_identity_flow_usd")
    ) or 0.0
    flow_concentration_available = bool(
        observation.get("flow_concentration_available")
    )
    positive_wallet_flow = optional_float(observation.get("positive_wallet_flow_usd"))
    top_wallet_share = optional_float(observation.get("top_wallet_net_buy_share"))
    top_cluster_share = optional_float(observation.get("top_cluster_net_buy_share"))
    residual_independent_flow = optional_float(
        observation.get("residual_independent_net_buy_usd")
    )
    cluster_flows = observation.get("cluster_flows") or []
    wallet_context = observation.get("wallet_context") or []
    pair_age_hours = iso_age_hours(
        observation.get("pair_created_at"),
        observation.get("observed_at"),
    )
    last_trade_age_hours = iso_age_hours(
        observation.get("last_trade_at"),
        observation.get("observed_at"),
    )
    hypersync_activity_age_hours = iso_age_hours(
        hypersync_last_activity_at,
        observation.get("observed_at"),
    )
    signal_temperature = (
        "hot"
        if last_trade_age_hours is not None and last_trade_age_hours <= 48
        else "early"
        if last_trade_age_hours is not None
        and last_trade_age_hours <= MAX_LIVE_FLOW_AGE_HOURS
        else "stale"
    )
    signal_type = classify_signal_type(
        pair_age_hours=pair_age_hours,
        public_attention_score=public_attention_score,
        attention_gap_score=attention_gap_score,
        onchain_volume_growth=onchain_volume_growth,
        boosts_active=boosts_active,
        listing_probability_90d=listing_probability_90d,
    )
    sell_ratio = gross_sell / gross_buy if gross_buy > 0 else 0.0
    liquidity_to_fdv = liquidity / fdv if liquidity and fdv and fdv > 0 else None
    volume_to_liquidity = (
        volume_24h / liquidity if volume_24h is not None and liquidity else None
    )
    holder_count_gap_ratio = (
        abs(holder_count - blockscout_holder_count)
        / max(holder_count, blockscout_holder_count)
        if holder_count and blockscout_holder_count
        else None
    )
    concentration_share = max(
        top_wallet_share or 0.0,
        top_cluster_share or 0.0,
    )
    flow_concentrated = bool(
        flow_concentration_available
        and concentration_share >= CONCENTRATION_WARNING_SHARE
    )
    severe_flow_concentration = bool(
        flow_concentration_available
        and concentration_share >= CONCENTRATION_BLOCK_SHARE
    )
    fresh_wallet_flow = bool(
        last_trade_age_hours is not None
        and last_trade_age_hours <= MAX_LIVE_FLOW_AGE_HOURS
    )
    blockscout_security_ready = bool(
        blockscout_is_contract is True
        and blockscout_contract_verified is True
        and blockscout_is_scam is not True
        and str(blockscout_reputation or "").lower() != "scam"
    )
    moralis_holder_ready = moralis_top10_ratio is not None
    hypersync_confirmation_available = hypersync_transfer_count is not None
    holder_concentration_ratio = max(
        [
            ratio
            for ratio in (top_holder_ratio, moralis_top10_ratio)
            if ratio is not None
        ],
        default=None,
    )

    accumulation_score = min(
        35.0,
        effective_wallet_count * 6.0 + math.log10(max(net_buy, 1.0)) * 4.0,
    )
    wallet_quality_score = min(20.0, weighted_wallet_score * 0.2)
    liquidity_score = 0.0
    if liquidity is not None:
        if liquidity >= 1_000_000:
            liquidity_score = 14.0
        elif liquidity >= 250_000:
            liquidity_score = 11.0
        elif liquidity >= 100_000:
            liquidity_score = 8.0
        elif liquidity >= 50_000:
            liquidity_score = 5.0
    market_health_score = 0.0
    if volume_24h is not None:
        market_health_score += 5.0 if volume_24h >= 100_000 else 3.0 if volume_24h >= 10_000 else 0.0
    if liquidity_to_fdv is not None and liquidity_to_fdv >= 0.02:
        market_health_score += 3.0
    elif liquidity_to_fdv is not None and liquidity_to_fdv >= 0.005:
        market_health_score += 1.5
    project_score = (
        (5.0 if website_url else 0.0) + (3.0 if social_links else 0.0)
        if require_x_social
        else (8.0 if website_url else 0.0)
    )
    contract_score = 0.0 if risk_score is None else max(0.0, 10.0 - risk_score * 0.1)
    due_diligence_score = (
        (3.0 if blockscout_security_ready else 0.0)
        + (2.0 if moralis_holder_ready else 0.0)
        + (1.0 if hypersync_confirmation_available else 0.0)
    )
    social_score = (
        min(8.0, social_silence * 0.08)
        if require_x_social and social_silence is not None
        else 0.0
    )
    attention_edge_score = (
        min(8.0, max(0.0, attention_gap_score - 50.0) * 0.2)
        if attention_gap_score is not None and attention_coverage_score >= 0.35
        else 0.0
    )
    independence_score = min(
        10.0,
        min(4.0, effective_wallet_count * 0.8)
        + (max(0.0, 1.0 - (top_cluster_share or 1.0)) * 8.0),
    )
    freshness_score = 4.0 if signal_temperature == "hot" else 2.0 if signal_temperature == "early" else 0.0
    risk_penalty = min(30.0, (risk_score or 0.0) * 0.3)
    flow_penalty = 18.0 if sell_ratio >= 0.8 else 8.0 if sell_ratio >= 0.35 else 0.0
    promotion_penalty = min(6.0, boosts_active * 1.5)
    concentration_penalty = 15.0 if severe_flow_concentration else 5.0 if flow_concentrated else 0.0
    confidence = (
        accumulation_score
        + wallet_quality_score
        + liquidity_score
        + market_health_score
        + project_score
        + contract_score
        + due_diligence_score
        + social_score
        + attention_edge_score
        + independence_score
        + freshness_score
    )
    confidence -= (
        risk_penalty
        + flow_penalty
        + promotion_penalty
        + concentration_penalty
    )
    confidence = max(0.0, min(100.0, confidence))

    missing = []
    if liquidity is None:
        missing.append("liquidity")
        confidence = min(confidence, 45.0)
    if risk_score is None:
        missing.append("contract_risk")
        confidence = min(confidence, 42.0)
    if not blockscout_security_ready:
        missing.append("blockscout_contract_verification")
        confidence = min(confidence, 48.0)
    if not moralis_holder_ready:
        missing.append("moralis_holder_distribution")
        confidence = min(confidence, 48.0)
    if not hypersync_confirmation_available:
        missing.append("hypersync_wallet_transfers")
    if pair_age_hours is None:
        missing.append("pair_age")
    if require_x_social:
        if social_silence is None:
            missing.append("social_silence")
            confidence -= 4.0
        elif not social_x_available:
            missing.append("x_social_coverage")
    if attention_gap_score is None:
        missing.append("attention_gap")
    elif attention_coverage_score < 0.35:
        missing.append("attention_coverage")
    if not cluster_independence_available:
        missing.append("cluster_independence")
        confidence -= 4.0
    if identity_flow_coverage < 0.95:
        missing.append("identity_flow_coverage")
        confidence -= 6.0
    if not flow_concentration_available:
        missing.append("wallet_flow_distribution")
        confidence -= 4.0
    if last_trade_age_hours is None:
        missing.append("flow_freshness")
    confidence = max(0.0, confidence)
    if dataset_target_count < 10:
        missing.append("historical_sample")
        sample_factor = min(0.9, 0.55 + dataset_target_count * 0.03)
        confidence *= sample_factor
        confidence = min(confidence, 60.0)
    else:
        confidence = min(confidence, 65.0)

    security_verified = bool(
        risk_score is not None
        and blockscout_security_ready
        and moralis_holder_ready
    )
    x_social_coverage_ready = (
        not require_x_social
        or (social_x_available and social_silence is not None)
    )
    social_hype_detected = bool(
        require_x_social
        and social_x_available
        and social_silence is not None
        and social_silence < 35
    )
    hard_risk = (
        bool(is_honeypot)
        or is_open_source is False
        or (buy_tax is not None and buy_tax > 0.1)
        or (sell_tax is not None and sell_tax > 0.1)
        or (risk_score is not None and risk_score >= 60)
        or (holder_count is not None and holder_count < 100)
        or (
            holder_concentration_ratio is not None
            and holder_concentration_ratio >= 0.7
        )
        or blockscout_is_contract is False
        or blockscout_is_scam is True
        or str(blockscout_reputation or "").lower() == "scam"
    )
    base_market_qualified = (
        wallet_count >= 3
        and net_buy >= 5_000
        and sell_ratio < 0.35
        and liquidity is not None
        and liquidity >= 50_000
        and volume_24h is not None
        and volume_24h >= 10_000
        and bool(website_url)
        and (not require_x_social or bool(social_links))
        and pair_age_hours is not None
        and pair_age_hours >= 12
        and (liquidity_to_fdv is None or liquidity_to_fdv >= 0.005)
        and not hard_risk
    )
    onchain_independence_ready = bool(
        cluster_independence_available
        and flow_concentration_available
        and identity_flow_coverage >= 0.95
        and effective_wallet_count >= 3
        and residual_independent_flow is not None
        and residual_independent_flow >= MIN_INDEPENDENT_FLOW_USD
        and fresh_wallet_flow
        and strong_wallet_count >= 1
        and not severe_flow_concentration
    )
    qualifies = bool(
        base_market_qualified
        and onchain_independence_ready
        and security_verified
        and x_social_coverage_ready
        and not social_hype_detected
    )
    status = (
        "candidate"
        if qualifies and confidence >= 40
        else "needs_review"
        if base_market_qualified
        and onchain_independence_ready
        and (
            not security_verified
            or not x_social_coverage_ready
        )
        else "filtered"
    )
    decision = build_signal_decision(status, signal_type, capital_readiness)
    signal_level = "medium" if status == "candidate" and confidence >= 55 else "low"
    risk_flags = list(observation.get("risk_flags") or [])
    if not website_url:
        risk_flags.append("website_missing")
    if liquidity is None or liquidity < 50_000:
        risk_flags.append("insufficient_liquidity")
    if sell_ratio >= 0.35:
        risk_flags.append("tracked_wallet_selling")
    if risk_score is None:
        risk_flags.append("contract_risk_missing")
    if is_open_source is False:
        risk_flags.append("source_not_verified")
    if volume_24h is None or volume_24h < 10_000:
        risk_flags.append("low_24h_volume")
    if pair_age_hours is None:
        risk_flags.append("pair_age_missing")
    elif pair_age_hours < 12:
        risk_flags.append("pair_too_new")
    if liquidity_to_fdv is not None and liquidity_to_fdv < 0.005:
        risk_flags.append("thin_liquidity_to_fdv")
    if holder_count is not None and holder_count < 100:
        risk_flags.append("low_holder_count")
    if holder_concentration_ratio is not None and holder_concentration_ratio > 0.5:
        risk_flags.append("concentrated_ownership")
    if blockscout_is_contract is False:
        risk_flags.append("blockscout_not_contract")
    if blockscout_contract_verified is False:
        risk_flags.append("blockscout_unverified_contract")
    if blockscout_is_scam or str(blockscout_reputation or "").lower() == "scam":
        risk_flags.append("blockscout_scam_reputation")
    if moralis_top10_eoa_ratio is not None and moralis_top10_eoa_ratio >= 0.5:
        risk_flags.append("moralis_high_eoa_concentration")
    if holder_count_gap_ratio is not None and holder_count_gap_ratio > 0.25:
        risk_flags.append("holder_count_source_disagreement")
    if boosts_active:
        risk_flags.append("dexscreener_promotion_active")
    if public_attention_score is not None and public_attention_score >= 65:
        risk_flags.append("public_attention_hot")
    if attention_coverage_score < 0.35:
        risk_flags.append("attention_coverage_low")
    if require_x_social and social_hype_detected:
        risk_flags.append("x_hype_detected")
    if not cluster_independence_available:
        risk_flags.append("cluster_independence_missing")
    elif effective_wallet_count < 3:
        risk_flags.append("too_few_independent_clusters")
    if not flow_concentration_available:
        risk_flags.append("wallet_flow_distribution_missing")
    if identity_flow_coverage < 0.95:
        risk_flags.append("identity_coverage_incomplete")
    if top_wallet_share is not None and top_wallet_share >= CONCENTRATION_WARNING_SHARE:
        risk_flags.append("wallet_flow_concentration")
    if top_cluster_share is not None and top_cluster_share >= CONCENTRATION_WARNING_SHARE:
        risk_flags.append("cluster_flow_concentration")
    if severe_flow_concentration:
        risk_flags.append("severe_flow_concentration")
    if (
        residual_independent_flow is not None
        and residual_independent_flow < MIN_INDEPENDENT_FLOW_USD
    ):
        risk_flags.append("insufficient_independent_flow")
    if not fresh_wallet_flow:
        risk_flags.append("stale_wallet_flow")

    symbol = (
        observation.get("market_token_symbol")
        or observation.get("token_symbol")
        or "UNKNOWN"
    )
    signal_type_label = {
        "discovery": "раннее discovery",
        "pre_listing": "pre-listing hypothesis",
        "accumulation": "накопление зрелого актива",
        "hot_momentum": "горячий momentum",
    }[signal_type]
    thesis = (
        f"Тип сигнала: {signal_type_label}. "
        f"{wallet_count} отслеживаемых адресов объединены в "
        f"{effective_wallet_count} независимых кластеров и дали "
        f"${net_buy:,.0f} чистых покупок за "
        f"{int(observation.get('window_hours') or 0)} ч.; продажи составили "
        f"{sell_ratio:.1%} от покупок. "
        + (
            f"Крупнейший кластер дал {top_cluster_share:.1%} положительного "
            f"потока, ещё ${residual_independent_flow:,.0f} пришло от остальных. "
            if top_cluster_share is not None
            and residual_independent_flow is not None
            else "Распределение потока между кластерами ещё не измерено. "
        )
        + (
            f"Последняя активность была {last_trade_age_hours:.1f} ч. назад."
            if last_trade_age_hours is not None
            else "Свежесть последней активности не измерена."
        )
        + (
            f" Blockscout подтверждает верифицированный контракт; Moralis "
            f"оценивает долю top-10 в {moralis_top10_ratio:.1%}."
            if blockscout_security_ready and moralis_top10_ratio is not None
            else " Независимая contract/holder проверка ещё не завершена."
        )
    )
    why_signal = [
        f"{wallet_count} отслеживаемых адресов накопили ${net_buy:,.0f} чистыми за окно наблюдения.",
        f"Продажи составили только {sell_ratio:.1%} от покупок.",
    ]
    if cluster_independence_available and cluster_independence_ratio is not None:
        why_signal.append(
            f"После объединения связанных адресов осталось {effective_wallet_count} "
            f"независимых кластеров из {wallet_count} кошельков."
        )
    if top_cluster_share is not None and residual_independent_flow is not None:
        why_signal.append(
            f"Крупнейший кластер контролирует {top_cluster_share:.1%} положительного "
            f"потока; остальные кластеры внесли ${residual_independent_flow:,.0f}."
        )
    if last_trade_age_hours is not None:
        why_signal.append(
            f"Последняя отслеживаемая сделка была {last_trade_age_hours:.1f} ч. назад; "
            f"режим сигнала: {signal_temperature}."
        )
    if require_x_social and social_silence is not None:
        source_name = "X + news" if social_x_available else "GDELT news proxy"
        why_signal.append(
            f"Social-silence {social_silence:.0f}/100 по {source_name}."
        )
    if attention_gap_score is not None:
        why_signal.append(
            f"Attention Gap {attention_gap_score:.0f}/100 при покрытии "
            f"{attention_coverage_score:.0%}: on-chain активность сопоставлена с "
            "DexScreener, Farcaster, GitHub и news-прокси."
        )
    if listing_probability_90d is not None:
        why_signal.append(
            f"Point-in-time ML вероятность Binance listing <=90d: "
            f"{listing_probability_90d:.1%}."
        )
    if liquidity is not None:
        why_signal.append(f"Ликвидность пары: ${liquidity:,.0f}.")
    if blockscout_security_ready:
        proxy_note = f", proxy {blockscout_proxy_type}" if blockscout_proxy_type else ""
        why_signal.append(
            f"Blockscout подтверждает верифицированный контракт без scam-метки"
            f"{proxy_note}."
        )
    if moralis_top10_ratio is not None:
        why_signal.append(
            f"Moralis: top-10 держат {moralis_top10_ratio:.1%} supply, "
            f"из них EOA {moralis_top10_eoa_ratio or 0.0:.1%}."
        )
    if hypersync_confirmation_available and hypersync_transfer_count:
        why_signal.append(
            f"HyperSync независимо нашёл {hypersync_transfer_count or 0} transfer-событий: "
            f"входы у {hypersync_inbound_wallet_count or 0} и выходы у "
            f"{hypersync_outbound_wallet_count or 0} отслеживаемых кошельков."
        )
    counter_evidence = []
    if require_x_social and not social_x_available:
        counter_evidence.append(
            "Нет прямого X/Twitter покрытия: оценка соцтишины основана только на GDELT news proxy."
        )
    if attention_coverage_score < 0.5:
        counter_evidence.append(
            f"Attention Gap покрыт только на {attention_coverage_score:.0%}; "
            "это вспомогательный признак, а не hard gate."
        )
    if not cluster_independence_available:
        counter_evidence.append(
            "Кластеры кошельков ещё не рассчитаны для этого наблюдения."
        )
    if not flow_concentration_available:
        counter_evidence.append(
            "Нет полного распределения положительного потока по кошелькам и кластерам."
        )
    if flow_concentrated:
        counter_evidence.append(
            f"Поток заметно концентрирован: крупнейший кошелёк или кластер даёт "
            f"{concentration_share:.1%} положительных покупок."
        )
    if (
        residual_independent_flow is not None
        and residual_independent_flow < MIN_INDEPENDENT_FLOW_USD
    ):
        counter_evidence.append(
            f"После исключения крупнейшего кластера остаётся только "
            f"${residual_independent_flow:,.0f}; для сигнала нужно "
            f"${MIN_INDEPENDENT_FLOW_USD:,.0f}."
        )
    if last_trade_age_hours is not None and not fresh_wallet_flow:
        counter_evidence.append(
            f"Последняя активность была {last_trade_age_hours / 24:.1f} дн. назад; "
            "поток уже не считается свежим."
        )
    if not blockscout_security_ready:
        counter_evidence.append(
            "Blockscout ещё не подтвердил одновременно contract status, verification и отсутствие scam-метки."
        )
    if not moralis_holder_ready:
        counter_evidence.append(
            "Нет независимого распределения top holders от Moralis."
        )
    if holder_count_gap_ratio is not None and holder_count_gap_ratio > 0.25:
        counter_evidence.append(
            f"GoPlus и Blockscout расходятся по числу холдеров на "
            f"{holder_count_gap_ratio:.0%}; снимки могли быть сделаны в разное время."
        )
    if hypersync_confirmation_available and not hypersync_transfer_count:
        counter_evidence.append(
            "HyperSync не нашёл прямых Transfer-событий кошельков; DEX-маршрутизация может скрывать такую связь."
        )
    if dataset_target_count < 10:
        counter_evidence.append(
            f"Историческая выборка Base пока мала: {dataset_target_count} целей."
        )
    if require_x_social and social_hype_detected:
        counter_evidence.append("X уже показывает заметный всплеск обсуждений.")
    return {
        "token_symbol": symbol,
        "token_name": observation.get("market_token_name"),
        "chain_id": observation["chain_id"],
        "contract_address": observation["token_address"],
        "signal_type": signal_type,
        "signal_level": signal_level,
        "confidence_score": round(confidence, 2),
        "status": status,
        "detected_at": observation["observed_at"],
        "strong_wallet_count": strong_wallet_count,
        "cluster_count": cluster_count,
        "net_buy_usd": net_buy,
        "liquidity_usd": liquidity,
        "social_silence_score": social_silence,
        "risk_score": risk_score,
        "thesis": thesis,
        "risk_flags": sorted(set(risk_flags)),
        "evidence": {
            "tracked_wallet_count": wallet_count,
            "watch_wallet_count": int(observation.get("watch_wallet_count") or 0),
            "gross_buy_usd": gross_buy,
            "gross_sell_usd": gross_sell,
            "sell_to_buy_ratio": sell_ratio,
            "weighted_wallet_score": weighted_wallet_score,
            "cluster_independence_available": cluster_independence_available,
            "cluster_independence_ratio": cluster_independence_ratio,
            "independent_cluster_count": cluster_count,
            "detected_cluster_count": detected_cluster_count,
            "identity_flow_coverage": identity_flow_coverage,
            "unknown_identity_flow_usd": unknown_identity_flow,
            "effective_wallet_count": effective_wallet_count,
            "flow_concentration_available": flow_concentration_available,
            "positive_wallet_flow_usd": positive_wallet_flow,
            "top_wallet_net_buy_share": top_wallet_share,
            "top_cluster_net_buy_share": top_cluster_share,
            "residual_independent_net_buy_usd": residual_independent_flow,
            "cluster_flows": cluster_flows,
            "last_trade_at": observation.get("last_trade_at"),
            "last_trade_age_hours": last_trade_age_hours,
            "signal_temperature": signal_temperature,
            "wallets": wallet_context,
            "volume_24h_usd": volume_24h,
            "market_cap_usd": market_cap,
            "fdv_usd": fdv,
            "liquidity_to_fdv": liquidity_to_fdv,
            "volume_to_liquidity": volume_to_liquidity,
            "pair_age_hours": pair_age_hours,
            "boosts_active": boosts_active,
            "holder_count": holder_count,
            "top_holder_ratio": top_holder_ratio,
            "source_checks": {
                "blockscout": {
                    "contract_verified": blockscout_contract_verified,
                    "is_contract": blockscout_is_contract,
                    "is_scam": blockscout_is_scam,
                    "reputation": blockscout_reputation,
                    "proxy_type": blockscout_proxy_type,
                    "implementation_addresses": blockscout_implementations,
                    "holder_count": blockscout_holder_count,
                    "flags": blockscout_flags,
                    "security_ready": blockscout_security_ready,
                },
                "moralis": {
                    "top10_holder_ratio": moralis_top10_ratio,
                    "top10_eoa_holder_ratio": moralis_top10_eoa_ratio,
                    "top10_contract_holder_ratio": moralis_top10_contract_ratio,
                    "labeled_holder_ratio": moralis_labeled_holder_ratio,
                    "flags": moralis_flags,
                    "holder_ready": moralis_holder_ready,
                },
                "hypersync": {
                    "inbound_wallet_count": hypersync_inbound_wallet_count,
                    "outbound_wallet_count": hypersync_outbound_wallet_count,
                    "transfer_count": hypersync_transfer_count,
                    "last_activity_at": hypersync_last_activity_at,
                    "activity_age_hours": hypersync_activity_age_hours,
                    "flags": hypersync_flags,
                    "available": hypersync_confirmation_available,
                },
                "holder_count_gap_ratio": holder_count_gap_ratio,
            },
            "is_open_source": is_open_source,
            "buy_tax": buy_tax,
            "sell_tax": sell_tax,
            "website_url": website_url,
            "social_links": social_links,
            "social_silence_score": social_silence,
            "social_coverage_score": social_coverage,
            "social_x_available": social_x_available,
            "social_provider_counts": social_provider_counts,
            "social_flags": social_flags,
            "attention": {
                "public_attention_score": public_attention_score,
                "onchain_attention_score": onchain_attention_score,
                "attention_gap_score": attention_gap_score,
                "coverage_score": attention_coverage_score,
                "onchain_trader_growth_7d": onchain_trader_growth,
                "onchain_volume_growth_7d": onchain_volume_growth,
                "flags": attention_flags,
            },
            "listing_probability_90d": listing_probability_90d,
            "price_usd": optional_float(observation.get("price_usd")),
            "decision": decision,
            "x_social_gate_enabled": require_x_social,
            "gates": {
                "base_market_qualified": base_market_qualified,
                "onchain_independence_ready": onchain_independence_ready,
                "security_verified": security_verified,
                "blockscout_security_ready": blockscout_security_ready,
                "moralis_holder_ready": moralis_holder_ready,
                "hypersync_confirmation_available": hypersync_confirmation_available,
                "x_social_coverage_ready": x_social_coverage_ready,
                "fresh_wallet_flow": fresh_wallet_flow,
            },
            "why_signal": why_signal,
            "counter_evidence": counter_evidence,
            "missing_features": sorted(set(missing)),
            "data_mode": "near_live_dune",
            "model_version": MODEL_VERSION,
        },
    }


def classify_signal_type(
    pair_age_hours: float | None,
    public_attention_score: float | None,
    attention_gap_score: float | None,
    onchain_volume_growth: float | None,
    boosts_active: int,
    listing_probability_90d: float | None,
) -> str:
    pair_age_days = pair_age_hours / 24 if pair_age_hours is not None else None
    if (
        listing_probability_90d is not None
        and listing_probability_90d >= 0.1
        and (attention_gap_score is None or attention_gap_score >= 55)
    ):
        return "pre_listing"
    if (
        pair_age_days is not None
        and pair_age_days <= 90
        and boosts_active == 0
        and (public_attention_score is None or public_attention_score < 65)
    ):
        return "discovery"
    if (
        boosts_active > 0
        or (public_attention_score is not None and public_attention_score >= 65)
        or (onchain_volume_growth is not None and onchain_volume_growth >= 1.5)
    ):
        return "hot_momentum"
    return "accumulation"


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return None if number is None else int(number)


def optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def iso_age_hours(start: Any, end: Any) -> float | None:
    if not start:
        return None
    try:
        start_at = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(
            str(end or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00")
        )
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        return max(0.0, (end_at - start_at).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None
