from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from smart_money_radar.config import x_social_gate_enabled
from smart_money_radar.ingestion.evm import EvmRpcClient, EvmRpcError
from smart_money_radar.ingestion.market import (
    DexScreenerClient,
    MarketDataError,
)
from smart_money_radar.ingestion.onchain import HyperSyncClient, OnchainDataError
from smart_money_radar.live_radar import (
    attach_wallet_context,
    build_signal,
    qualified_live_wallets,
)
from smart_money_radar.storage import SQLiteStore, utc_now_iso


LOCAL_LIVE_SOURCE = "local_hypersync_transfer_proxy"
LOCAL_DEX_SOURCE = "local_hypersync_dex_trades"
LOCAL_MARKET_SOURCE = "local_live_dexscreener"


@dataclass
class TokenFlow:
    token_address: str
    inbound_transfer_count: int = 0
    outbound_transfer_count: int = 0
    first_trade_at: str | None = None
    last_trade_at: str | None = None
    wallet_raw_flows: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.wallet_raw_flows is None:
            self.wallet_raw_flows = {}


def run_base_local_live_scan(
    store: SQLiteStore,
    window_hours: int = 24,
    max_wallets: int = 200,
    max_tokens: int = 100,
    min_wallets: int = 1,
    max_hypersync_pages: int = 3,
    max_pairs_per_token: int = 5,
    min_pair_liquidity_usd: float = 1_000.0,
    use_cursor: bool = False,
    generate_signals: bool = True,
    allow_transfer_fallback: bool = True,
    observed_at: str | None = None,
    hypersync: HyperSyncClient | None = None,
    market: DexScreenerClient | None = None,
    rpc: EvmRpcClient | None = None,
) -> dict[str, Any]:
    store.init_db()
    timestamp = observed_at or utc_now_iso()
    wallets = qualified_live_wallets(store, max_wallets=max_wallets)
    wallet_addresses = [row["wallet_address"] for row in wallets]
    excluded_addresses = {
        row["contract_address"].lower()
        for row in store.chain_backtest_targets("base")
    }
    if not wallet_addresses:
        pruned_dex_observations = store.prune_radar_observations_by_source(
            LOCAL_DEX_SOURCE,
            keep_latest_snapshots=0,
        )
        pruned_proxy_observations = store.prune_radar_observations_by_source(
            LOCAL_LIVE_SOURCE,
            keep_latest_snapshots=0,
        )
        pruned_markets = store.prune_token_market_snapshots_by_source(
            LOCAL_MARKET_SOURCE,
            keep_latest_snapshots=0,
        )
        pruned_dex_trades = store.prune_local_dex_trades_by_source(
            LOCAL_DEX_SOURCE,
            keep_latest_snapshots=0,
        )
        pruned_dex_rollups = store.prune_local_dex_rollups_by_source(
            LOCAL_DEX_SOURCE,
            keep_latest_snapshots=0,
        )
        pruned_signals = store.prune_signals_by_evidence_source_mode(
            LOCAL_DEX_SOURCE,
            keep_latest_detected=0,
        )
        return {
            "status": "no_qualified_wallets",
            "tracked_wallet_count": 0,
            "transfer_event_count": 0,
            "swap_event_count": 0,
            "dex_trade_count": 0,
            "dex_rollup_count": 0,
            "pair_candidate_count": 0,
            "pair_token_count": 0,
            "observation_count": 0,
            "fallback_observation_count": 0,
            "signal_count": 0,
            "qualified_signal_count": 0,
            "pruned_observation_count": pruned_dex_observations
            + pruned_proxy_observations,
            "pruned_market_snapshot_count": pruned_markets,
            "pruned_dex_trade_count": pruned_dex_trades,
            "pruned_dex_rollup_count": pruned_dex_rollups,
            "pruned_signal_count": pruned_signals,
            "raw_logs_stored": False,
        }

    hypersync_client = hypersync or HyperSyncClient()
    scan_payload = hypersync_client.recent_wallet_transfer_events(
        chain_id="base",
        wallet_addresses=wallet_addresses,
        observed_at=timestamp,
        window_hours=window_hours,
        max_pages=max_hypersync_pages,
    )
    events = [
        event
        for event in scan_payload["events"]
        if str(event.get("token_address") or "").lower() not in excluded_addresses
    ]
    grouped = group_transfer_events(events)
    candidates = [
        flow
        for flow in grouped.values()
        if positive_wallet_count(flow) >= max(1, int(min_wallets))
    ]
    candidates.sort(
        key=lambda flow: (
            positive_wallet_count(flow),
            flow.inbound_transfer_count - flow.outbound_transfer_count,
        ),
        reverse=True,
    )
    candidates = candidates[: max(1, int(max_tokens))]
    token_addresses = [flow.token_address for flow in candidates]

    warnings = []
    market_snapshots = []
    try:
        market_snapshots = (market or DexScreenerClient()).token_market_snapshots(
            "base",
            token_addresses,
            observed_at=timestamp,
            max_pairs_per_token=max_pairs_per_token,
            minimum_pair_liquidity_usd=min_pair_liquidity_usd,
        )
        for snapshot in market_snapshots:
            snapshot["source"] = LOCAL_MARKET_SOURCE
        store.upsert_token_market_snapshots(market_snapshots)
    except MarketDataError as exc:
        warnings.append(f"dexscreener: {exc}")

    market_by_token = {
        snapshot["token_address"].lower(): snapshot
        for snapshot in market_snapshots
    }
    decimals_by_token = token_decimals(
        token_addresses,
        rpc=rpc,
        warnings=warnings,
    )
    wallet_context = wallet_context_by_address(wallets)
    market_contexts = market_contexts_by_token(
        market_snapshots,
        max_pairs_per_token=max_pairs_per_token,
        min_pair_liquidity_usd=min_pair_liquidity_usd,
    )
    swap_payload = {
        "chain_id": "base",
        "observed_at": timestamp,
        "from_block": scan_payload.get("from_block"),
        "to_block": scan_payload.get("to_block"),
        "next_block": scan_payload.get("to_block"),
        "events": [],
        "page_count": 0,
        "pair_addresses": [],
    }
    pair_addresses = market_pair_addresses(market_contexts)
    pair_tokens_by_pair = pair_tokens_from_market_contexts(market_contexts)
    pair_tokens_by_pair.update(
        pair_tokens(
            pair_addresses,
            rpc=rpc,
            warnings=warnings,
        )
    )
    if pair_addresses:
        cursor = store.local_ingestion_cursor(
            LOCAL_DEX_SOURCE,
            "base",
            "candidate_pair_swaps",
        )
        cursor_from_block = (
            int(cursor["block_number"]) + 1
            if use_cursor and cursor and cursor.get("block_number") is not None
            else scan_payload.get("from_block")
        )
        pool_reader = getattr(hypersync_client, "recent_pool_swap_events", None)
        if callable(pool_reader):
            try:
                swap_payload = pool_reader(
                    chain_id="base",
                    pair_addresses=pair_addresses,
                    observed_at=timestamp,
                    window_hours=window_hours,
                    max_pages=max_hypersync_pages,
                    from_block=cursor_from_block,
                    to_block=scan_payload.get("to_block"),
                )
            except OnchainDataError as exc:
                warnings.append(f"hypersync_swaps: {exc}")
        else:
            warnings.append("hypersync_swaps: recent_pool_swap_events unavailable")

    cursor_block = scan_payload.get("to_block")
    if swap_payload.get("to_block") is not None:
        cursor_block = swap_payload.get("to_block")
    if cursor_block is not None:
        store.upsert_local_ingestion_cursor(
            LOCAL_DEX_SOURCE,
            "base",
            "candidate_pair_swaps",
            int(cursor_block),
            metadata={
                "observed_at": timestamp,
                "window_hours": window_hours,
                "use_cursor": bool(use_cursor),
                "pair_count": len(pair_addresses),
                "pair_token_count": len(pair_tokens_by_pair),
                "raw_logs_stored": False,
            },
        )

    dex_trades = infer_local_dex_trades(
        transfer_events=events,
        swap_events=swap_payload.get("events", []),
        market_contexts=market_contexts,
        decimals_by_token=decimals_by_token,
        pair_tokens_by_pair=pair_tokens_by_pair,
        wallet_context=wallet_context,
        observed_at=timestamp,
        window_hours=window_hours,
    )
    dex_trade_count = store.import_local_dex_trades(dex_trades)
    dex_rollups = rollups_from_local_dex_trades(
        dex_trades,
        wallet_context=wallet_context,
        observed_at=timestamp,
        window_hours=window_hours,
        scan_payload=scan_payload,
        swap_payload=swap_payload,
    )
    dex_rollup_count = store.import_local_dex_rollups(dex_rollups)
    dex_observation_count = store.import_radar_observations(
        chain_id="base",
        execution_id=None,
        rows=dex_rollups,
        source=LOCAL_DEX_SOURCE,
    )

    dex_tokens = {row["token_address"].lower() for row in dex_rollups}
    fallback_rows = [
        radar_row_from_flow(
            flow=flow,
            wallet_context=wallet_context,
            market_snapshot=market_by_token.get(flow.token_address),
            decimals=decimals_by_token.get(flow.token_address),
            observed_at=timestamp,
            window_hours=window_hours,
            scan_payload=scan_payload,
        )
        for flow in candidates
        if allow_transfer_fallback and flow.token_address not in dex_tokens
    ]
    fallback_rows = [
        row
        for row in fallback_rows
        if row["tracked_wallet_count"] >= max(1, int(min_wallets))
    ]
    fallback_observation_count = store.import_radar_observations(
        chain_id="base",
        execution_id=None,
        rows=fallback_rows,
        source=LOCAL_LIVE_SOURCE,
    )
    pruned_dex_observations = store.prune_radar_observations_by_source(
        LOCAL_DEX_SOURCE,
        keep_latest_snapshots=1 if dex_observation_count else 0,
    )
    pruned_proxy_observations = store.prune_radar_observations_by_source(
        LOCAL_LIVE_SOURCE,
        keep_latest_snapshots=1 if fallback_observation_count else 0,
    )
    pruned_markets = store.prune_token_market_snapshots_by_source(
        LOCAL_MARKET_SOURCE,
        keep_latest_snapshots=1 if market_snapshots else 0,
    )
    pruned_dex_trades = store.prune_local_dex_trades_by_source(
        LOCAL_DEX_SOURCE,
        keep_latest_snapshots=1 if dex_trade_count else 0,
    )
    pruned_dex_rollups = store.prune_local_dex_rollups_by_source(
        LOCAL_DEX_SOURCE,
        keep_latest_snapshots=1 if dex_rollup_count else 0,
    )
    local_signals = (
        recompute_local_live_signals(store, source=LOCAL_DEX_SOURCE)
        if generate_signals
        else []
    )
    pruned_signals = store.prune_signals_by_evidence_source_mode(
        LOCAL_DEX_SOURCE,
        keep_latest_detected=1 if local_signals else 0,
    )
    return {
        "status": "completed",
        "tracked_wallet_count": len(wallet_addresses),
        "transfer_event_count": len(events),
        "swap_event_count": len(swap_payload.get("events", [])),
        "dex_trade_count": dex_trade_count,
        "dex_rollup_count": dex_rollup_count,
        "pair_candidate_count": len(pair_addresses),
        "pair_token_count": len(pair_tokens_by_pair),
        "token_candidate_count": len(candidates),
        "observation_count": dex_observation_count + fallback_observation_count,
        "dex_observation_count": dex_observation_count,
        "fallback_observation_count": fallback_observation_count,
        "market_snapshot_count": len(market_snapshots),
        "priced_token_count": sum(
            1 for token in token_addresses if token in decimals_by_token
        ),
        "hypersync_page_count": scan_payload.get("page_count", 0),
        "hypersync_swap_page_count": swap_payload.get("page_count", 0),
        "from_block": scan_payload.get("from_block"),
        "to_block": scan_payload.get("to_block"),
        "next_block": scan_payload.get("next_block"),
        "cursor_block": cursor_block,
        "signal_count": len(local_signals),
        "qualified_signal_count": sum(
            1 for signal in local_signals if signal.get("status") == "candidate"
        ),
        "pruned_observation_count": pruned_dex_observations
        + pruned_proxy_observations,
        "pruned_market_snapshot_count": pruned_markets,
        "pruned_dex_trade_count": pruned_dex_trades,
        "pruned_dex_rollup_count": pruned_dex_rollups,
        "pruned_signal_count": pruned_signals,
        "raw_logs_stored": False,
        "warnings": warnings,
    }


def market_contexts_by_token(
    market_snapshots: list[dict[str, Any]],
    max_pairs_per_token: int = 5,
    min_pair_liquidity_usd: float = 1_000.0,
) -> dict[str, dict[str, Any]]:
    contexts = {}
    for snapshot in market_snapshots:
        token_address = str(snapshot.get("token_address") or "").lower()
        if not token_address:
            continue
        raw = snapshot.get("raw") if isinstance(snapshot.get("raw"), dict) else {}
        pair_contexts = pair_contexts_from_snapshot(
            snapshot,
            token_address=token_address,
            max_pairs=max_pairs_per_token,
            min_liquidity_usd=min_pair_liquidity_usd,
        )
        contexts[token_address] = {
            "token_address": token_address,
            "token_symbol": snapshot.get("token_symbol"),
            "pair_address": str(snapshot.get("pair_address") or "").lower(),
            "dex_id": snapshot.get("dex_id"),
            "project": snapshot.get("dex_id"),
            "price_usd": snapshot.get("price_usd"),
            "raw_pair": raw,
            "base_token": raw.get("baseToken") if isinstance(raw, dict) else None,
            "quote_token": raw.get("quoteToken") if isinstance(raw, dict) else None,
            "pair_contexts": pair_contexts,
        }
    return contexts


def pair_contexts_from_snapshot(
    snapshot: dict[str, Any],
    token_address: str,
    max_pairs: int,
    min_liquidity_usd: float,
) -> list[dict[str, Any]]:
    raw = snapshot.get("raw") if isinstance(snapshot.get("raw"), dict) else {}
    candidates = raw.get("candidatePairs") or raw.get("candidate_pairs")
    pairs = candidates if isinstance(candidates, list) else []
    if not pairs and snapshot.get("pair_address"):
        pairs = [
            {
                "rank": 1,
                "pairAddress": snapshot.get("pair_address"),
                "dexId": snapshot.get("dex_id"),
                "liquidity_usd": snapshot.get("liquidity_usd"),
                "volume_24h_usd": snapshot.get("volume_24h_usd"),
                "priceUsd": snapshot.get("price_usd"),
                "baseToken": raw.get("baseToken"),
                "quoteToken": raw.get("quoteToken"),
            }
        ]
    contexts = []
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        pair_address = str(
            pair.get("pairAddress") or pair.get("pair_address") or ""
        ).lower()
        if not is_evm_address(pair_address) or pair_address in seen:
            continue
        liquidity = optional_float(pair.get("liquidity_usd"))
        if liquidity is None:
            liquidity = optional_float(
                (pair.get("liquidity") or {}).get("usd")
                if isinstance(pair.get("liquidity"), dict)
                else None
            )
        if liquidity is not None and liquidity < min_liquidity_usd and contexts:
            continue
        contexts.append(
            {
                "token_address": token_address,
                "pair_address": pair_address,
                "dex_id": pair.get("dexId") or pair.get("dex_id") or snapshot.get("dex_id"),
                "project": pair.get("dexId") or pair.get("dex_id") or snapshot.get("dex_id"),
                "rank": int(pair.get("rank") or len(contexts) + 1),
                "liquidity_usd": liquidity,
                "volume_24h_usd": optional_float(pair.get("volume_24h_usd")),
                "base_token": compact_token_reference(pair.get("baseToken")),
                "quote_token": compact_token_reference(pair.get("quoteToken")),
            }
        )
        seen.add(pair_address)
        if len(contexts) >= max(1, int(max_pairs)):
            break
    return contexts


def compact_token_reference(value: Any) -> dict[str, Any]:
    token = value if isinstance(value, dict) else {}
    return {
        "address": str(token.get("address") or "").lower() or None,
        "symbol": token.get("symbol"),
        "name": token.get("name"),
    }


def market_pair_addresses(market_contexts: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(pair.get("pair_address") or "").lower()
            for context in market_contexts.values()
            for pair in context.get("pair_contexts", [])
            if is_evm_address(pair.get("pair_address"))
        }
    )


def pair_tokens_from_market_contexts(
    market_contexts: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    tokens_by_pair = {}
    for context in market_contexts.values():
        for pair in context.get("pair_contexts", []):
            pair_address = str(pair.get("pair_address") or "").lower()
            base_token = pair.get("base_token") or {}
            quote_token = pair.get("quote_token") or {}
            token0 = str(base_token.get("address") or "").lower()
            token1 = str(quote_token.get("address") or "").lower()
            if (
                is_evm_address(pair_address)
                and is_evm_address(token0)
                and is_evm_address(token1)
            ):
                tokens_by_pair.setdefault(pair_address, (token0, token1))
    return tokens_by_pair


def infer_local_dex_trades(
    transfer_events: list[dict[str, Any]],
    swap_events: list[dict[str, Any]],
    market_contexts: dict[str, dict[str, Any]],
    decimals_by_token: dict[str, int],
    pair_tokens_by_pair: dict[str, tuple[str, str]],
    wallet_context: dict[str, dict[str, Any]],
    observed_at: str,
    window_hours: int,
) -> list[dict[str, Any]]:
    candidate_tokens = set(market_contexts)
    tokens_by_pair: dict[str, list[str]] = defaultdict(list)
    pair_context_by_token_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for token_address, context in market_contexts.items():
        pair_contexts = context.get("pair_contexts") or []
        if not pair_contexts and context.get("pair_address"):
            pair_contexts = [
                {
                    "pair_address": str(context["pair_address"]).lower(),
                    "dex_id": context.get("dex_id"),
                    "project": context.get("project"),
                    "rank": 1,
                }
            ]
        for pair_context in pair_contexts:
            pair_address = str(pair_context.get("pair_address") or "").lower()
            if not pair_address:
                continue
            tokens_by_pair[pair_address].append(token_address)
            pair_context_by_token_pair[(token_address, pair_address)] = pair_context

    swaps_by_tx_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for swap in swap_events:
        tx_hash = str(swap.get("tx_hash") or "").lower()
        pair_address = str(swap.get("pair_address") or "").lower()
        if not tx_hash or pair_address not in tokens_by_pair:
            continue
        for token_address in tokens_by_pair[pair_address]:
            swaps_by_tx_token[(tx_hash, token_address)].append(swap)

    flow_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in transfer_events:
        token_address = str(event.get("token_address") or "").lower()
        tx_hash = str(event.get("tx_hash") or "").lower()
        if token_address not in candidate_tokens or not tx_hash:
            continue
        if (tx_hash, token_address) not in swaps_by_tx_token:
            continue
        amount_raw = int(event.get("amount_raw") or 0)
        directions = []
        if event.get("to_tracked"):
            directions.append((str(event.get("to_address") or "").lower(), amount_raw))
        if event.get("from_tracked"):
            directions.append((str(event.get("from_address") or "").lower(), -amount_raw))
        for wallet_address, signed_amount in directions:
            if not wallet_address:
                continue
            key = (tx_hash, wallet_address, token_address)
            group = flow_groups.setdefault(
                key,
                {
                    "tx_hash": tx_hash,
                    "wallet_address": wallet_address,
                    "token_address": token_address,
                    "amount_raw": 0,
                    "transfer_count": 0,
                    "first_transfer_at": None,
                    "last_transfer_at": None,
                    "block_number": event.get("block_number"),
                    "trade_inference": "tracked_wallet_token_transfer_in_swap_tx",
                },
            )
            group["amount_raw"] += signed_amount
            group["transfer_count"] += 1
            block_time = event.get("block_time")
            if block_time:
                group["first_transfer_at"] = min(
                    [
                        value
                        for value in (group.get("first_transfer_at"), block_time)
                        if value
                    ]
                )
                group["last_transfer_at"] = max(
                    [
                        value
                        for value in (group.get("last_transfer_at"), block_time)
                        if value
                    ]
                )

    tracked_wallets = set(wallet_context)
    for swap in swap_events:
        tx_hash = str(swap.get("tx_hash") or "").lower()
        pair_address = str(swap.get("pair_address") or "").lower()
        if not tx_hash or pair_address not in tokens_by_pair:
            continue
        pair_tokens = pair_tokens_by_pair.get(pair_address)
        for token_address in tokens_by_pair[pair_address]:
            net_raw = swap_token_net_raw(
                swap=swap,
                token_address=token_address,
                pair_tokens=pair_tokens,
            )
            if net_raw is None or net_raw == 0:
                continue
            wallet_match = tracked_wallet_for_swap_delta(
                swap,
                net_raw=net_raw,
                tracked_wallets=tracked_wallets,
            )
            if wallet_match is None:
                continue
            wallet_address, trade_inference, wallet_field = wallet_match
            key = (tx_hash, wallet_address, token_address)
            if key in flow_groups:
                continue
            flow_groups[key] = {
                "tx_hash": tx_hash,
                "wallet_address": wallet_address,
                "token_address": token_address,
                "amount_raw": net_raw,
                "transfer_count": 0,
                "first_transfer_at": swap.get("block_time"),
                "last_transfer_at": swap.get("block_time"),
                "block_number": swap.get("block_number"),
                "pair_address": pair_address,
                "trade_inference": trade_inference,
                "swap_wallet_field": wallet_field,
            }

    trades = []
    for (tx_hash, wallet_address, token_address), group in sorted(flow_groups.items()):
        net_raw = int(group.get("amount_raw") or 0)
        if net_raw == 0:
            continue
        related_swaps = swaps_by_tx_token.get((tx_hash, token_address), [])
        group_pair_address = str(group.get("pair_address") or "").lower()
        if group_pair_address:
            related_swaps = [
                row
                for row in related_swaps
                if str(row.get("pair_address") or "").lower() == group_pair_address
            ]
        related_swaps = sorted(
            related_swaps,
            key=lambda row: int(row.get("log_index") or 0),
        )
        if not related_swaps:
            continue
        swap = related_swaps[0]
        context = market_contexts[token_address]
        pair_address = str(swap.get("pair_address") or "").lower()
        pair_context = pair_context_by_token_pair.get((token_address, pair_address), {})
        decimals = decimals_by_token.get(token_address)
        price = optional_float(context.get("price_usd")) or 0.0
        amount_token = None
        amount_usd = None
        if decimals is not None:
            amount_token = abs(net_raw) / 10**decimals
            amount_usd = amount_token * price if price > 0 else None
        side = "buy" if net_raw > 0 else "sell"
        wallet_row = wallet_context.get(wallet_address, {})
        trades.append(
            {
                "chain_id": "base",
                "project": pair_context.get("project") or context.get("project"),
                "dex_id": pair_context.get("dex_id") or context.get("dex_id"),
                "pair_address": swap.get("pair_address"),
                "token_address": token_address,
                "token_symbol": context.get("token_symbol"),
                "wallet_address": wallet_address,
                "tx_hash": tx_hash,
                "log_index": int(swap.get("log_index") or 0),
                "block_number": swap.get("block_number") or group.get("block_number"),
                "block_time": swap.get("block_time")
                or group.get("last_transfer_at")
                or group.get("first_transfer_at"),
                "side": side,
                "amount_raw": str(abs(net_raw)),
                "amount_token": amount_token,
                "amount_usd": amount_usd,
                "price_usd": price or None,
                "observed_at": observed_at,
                "window_hours": window_hours,
                "source": LOCAL_DEX_SOURCE,
                "evidence": {
                    "source_mode": LOCAL_DEX_SOURCE,
                    "raw_logs_stored": False,
                    "trade_inference": group.get("trade_inference"),
                    "transfer_count": group.get("transfer_count", 0),
                    "first_transfer_at": group.get("first_transfer_at"),
                    "last_transfer_at": group.get("last_transfer_at"),
                    "pair_rank": pair_context.get("rank"),
                    "pair_liquidity_usd": pair_context.get("liquidity_usd"),
                    "wallet_label": wallet_row.get("label"),
                    "wallet_interest_score": wallet_row.get("interest_score"),
                    "wallet_confidence_score": wallet_row.get("confidence_score"),
                    "swap_protocol_shape": swap.get("protocol_shape"),
                    "swap_tx_from": swap.get("tx_from"),
                    "swap_tx_to": swap.get("tx_to"),
                    "swap_sender": swap.get("sender"),
                    "swap_recipient": swap.get("recipient"),
                    "swap_wallet_field": group.get("swap_wallet_field"),
                    "swap_topic": swap.get("topic0"),
                    "swap_decoded": swap_decoded_evidence(swap),
                    "pair_tokens": pair_tokens_by_pair.get(pair_address),
                },
            }
        )
    return trades


def tracked_wallet_for_swap_delta(
    swap: dict[str, Any],
    net_raw: int,
    tracked_wallets: set[str],
) -> tuple[str, str, str] | None:
    tx_from = str(swap.get("tx_from") or "").lower()
    if tx_from in tracked_wallets:
        return tx_from, "tracked_tx_sender_swap_delta", "tx_from"

    recipient = str(swap.get("recipient") or "").lower()
    if net_raw > 0 and recipient in tracked_wallets:
        return recipient, "tracked_swap_recipient_output_delta", "recipient"

    sender = str(swap.get("sender") or "").lower()
    if net_raw < 0 and sender in tracked_wallets:
        return sender, "tracked_swap_sender_input_delta", "sender"

    return None


def swap_decoded_evidence(swap: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "amount0_in_raw",
        "amount1_in_raw",
        "amount0_out_raw",
        "amount1_out_raw",
        "amount0_delta_raw",
        "amount1_delta_raw",
    )
    return {key: swap[key] for key in keys if key in swap}


def swap_token_net_raw(
    swap: dict[str, Any],
    token_address: str,
    pair_tokens: tuple[str, str] | None,
) -> int | None:
    if not pair_tokens:
        return None
    token0, token1 = (pair_tokens[0].lower(), pair_tokens[1].lower())
    token = token_address.lower()
    if token not in {token0, token1}:
        return None
    token_index = 0 if token == token0 else 1
    if swap.get("protocol_shape") == "v2":
        amount_in = safe_int(
            swap.get("amount0_in_raw" if token_index == 0 else "amount1_in_raw")
        )
        amount_out = safe_int(
            swap.get("amount0_out_raw" if token_index == 0 else "amount1_out_raw")
        )
        return amount_out - amount_in
    if swap.get("protocol_shape") == "v3":
        delta = safe_int(
            swap.get("amount0_delta_raw" if token_index == 0 else "amount1_delta_raw")
        )
        return -delta
    return None


def rollups_from_local_dex_trades(
    trades: list[dict[str, Any]],
    wallet_context: dict[str, dict[str, Any]],
    observed_at: str,
    window_hours: int,
    scan_payload: dict[str, Any],
    swap_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    trades_by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        trades_by_token[str(trade.get("token_address") or "").lower()].append(trade)

    rows = []
    for token_address, token_trades in trades_by_token.items():
        wallet_net_usd: dict[str, float] = defaultdict(float)
        wallet_net_raw: dict[str, int] = defaultdict(int)
        gross_buy_usd = 0.0
        gross_sell_usd = 0.0
        buy_trade_count = 0
        sell_trade_count = 0
        first_trade_at = None
        last_trade_at = None
        dex_ids = set()
        pair_addresses = set()
        for trade in token_trades:
            side = trade.get("side")
            amount_usd = optional_float(trade.get("amount_usd")) or 0.0
            amount_raw = int(trade.get("amount_raw") or 0)
            wallet = str(trade.get("wallet_address") or "").lower()
            if side == "buy":
                gross_buy_usd += amount_usd
                wallet_net_usd[wallet] += amount_usd
                wallet_net_raw[wallet] += amount_raw
                buy_trade_count += 1
            elif side == "sell":
                gross_sell_usd += amount_usd
                wallet_net_usd[wallet] -= amount_usd
                wallet_net_raw[wallet] -= amount_raw
                sell_trade_count += 1
            block_time = trade.get("block_time")
            if block_time:
                first_trade_at = min(
                    [value for value in (first_trade_at, block_time) if value]
                )
                last_trade_at = max(
                    [value for value in (last_trade_at, block_time) if value]
                )
            if trade.get("dex_id"):
                dex_ids.add(str(trade["dex_id"]))
            if trade.get("pair_address"):
                pair_addresses.add(str(trade["pair_address"]).lower())

        positive_wallets = [
            wallet for wallet, net_usd in wallet_net_usd.items() if net_usd > 0
        ]
        positive_contexts = [
            wallet_context[wallet]
            for wallet in positive_wallets
            if wallet in wallet_context
        ]
        strong_count = sum(
            1 for row in positive_contexts if row.get("label") == "strong_candidate"
        )
        watch_count = sum(
            1 for row in positive_contexts if row.get("label") == "watch_candidate"
        )
        weighted_wallet_score = average(
            [float(row.get("interest_score") or 0) for row in positive_contexts]
        )
        average_confidence = average(
            [float(row.get("confidence_score") or 0) for row in positive_contexts]
        )
        wallet_flows = [
            {
                "wallet_address": wallet,
                "net_buy_usd": round(wallet_net_usd[wallet], 6),
                "raw_token_flow": str(wallet_net_raw[wallet]),
            }
            for wallet in sorted(wallet_net_usd)
        ]
        token_symbol = next(
            (trade.get("token_symbol") for trade in token_trades if trade.get("token_symbol")),
            None,
        )
        rows.append(
            {
                "chain_id": "base",
                "token_address": token_address,
                "token_symbol": token_symbol,
                "observed_at": observed_at,
                "window_hours": window_hours,
                "tracked_wallet_count": len(positive_wallets),
                "strong_wallet_count": strong_count,
                "watch_wallet_count": watch_count,
                "gross_buy_usd": gross_buy_usd,
                "gross_sell_usd": gross_sell_usd,
                "net_buy_usd": gross_buy_usd - gross_sell_usd,
                "buy_trade_count": buy_trade_count,
                "sell_trade_count": sell_trade_count,
                "first_trade_at": first_trade_at,
                "last_trade_at": last_trade_at,
                "weighted_wallet_score": weighted_wallet_score,
                "average_wallet_confidence": average_confidence,
                "accumulating_wallet_addresses": positive_wallets,
                "accumulating_wallet_net_buy_usd": [
                    round(wallet_net_usd[wallet], 6)
                    for wallet in positive_wallets
                ],
                "source": LOCAL_DEX_SOURCE,
                "evidence": {
                    "source_mode": LOCAL_DEX_SOURCE,
                    "raw_logs_stored": False,
                    "retention_policy": "keep_latest_local_snapshot_only",
                    "local_dex_trade_count": len(token_trades),
                    "swap_event_count": len(swap_payload.get("events", [])),
                    "hypersync_transfer_event_count": len(
                        scan_payload.get("events", [])
                    ),
                    "hypersync_from_block": scan_payload.get("from_block"),
                    "hypersync_to_block": scan_payload.get("to_block"),
                    "hypersync_swap_from_block": swap_payload.get("from_block"),
                    "hypersync_swap_to_block": swap_payload.get("to_block"),
                    "dex_ids": sorted(dex_ids),
                    "pair_addresses": sorted(pair_addresses),
                    "accumulating_wallet_flows": wallet_flows,
                    "trade_refs": [
                        {
                            "tx_hash": trade.get("tx_hash"),
                            "log_index": trade.get("log_index"),
                            "wallet_address": trade.get("wallet_address"),
                            "side": trade.get("side"),
                            "amount_usd": trade.get("amount_usd"),
                        }
                        for trade in token_trades[:20]
                    ],
                },
            }
        )
    rows.sort(key=lambda row: float(row.get("net_buy_usd") or 0), reverse=True)
    return rows


def recompute_local_live_signals(
    store: SQLiteStore,
    source: str = LOCAL_DEX_SOURCE,
) -> list[dict[str, Any]]:
    observations = store.latest_radar_observations(limit=500, source=source)
    if not observations:
        return []
    dataset_target_count = store.observed_target_count()
    require_x_social = x_social_gate_enabled()
    signals = []
    for row in observations:
        signal = build_signal(
            attach_wallet_context(store, row),
            dataset_target_count=dataset_target_count,
            require_x_social=require_x_social,
        )
        signal["evidence"]["source_mode"] = source
        signal["evidence"]["raw_logs_stored"] = False
        signal["evidence"]["retention_policy"] = "keep_latest_local_snapshot_only"
        if signal.get("status") != "filtered":
            signals.append(signal)
    if signals:
        store.upsert_signals(signals)
    return signals


def group_transfer_events(events: list[dict[str, Any]]) -> dict[str, TokenFlow]:
    grouped: dict[str, TokenFlow] = {}
    for event in events:
        token_address = str(event.get("token_address") or "").lower()
        if not token_address:
            continue
        flow = grouped.setdefault(
            token_address,
            TokenFlow(token_address=token_address),
        )
        amount_raw = int(event.get("amount_raw") or 0)
        if event.get("to_tracked"):
            wallet = str(event.get("to_address") or "").lower()
            flow.wallet_raw_flows[wallet] = flow.wallet_raw_flows.get(wallet, 0) + amount_raw
            flow.inbound_transfer_count += 1
        if event.get("from_tracked"):
            wallet = str(event.get("from_address") or "").lower()
            flow.wallet_raw_flows[wallet] = flow.wallet_raw_flows.get(wallet, 0) - amount_raw
            flow.outbound_transfer_count += 1
        block_time = event.get("block_time")
        if block_time:
            flow.first_trade_at = min(
                [time for time in (flow.first_trade_at, block_time) if time]
            )
            flow.last_trade_at = max(
                [time for time in (flow.last_trade_at, block_time) if time]
            )
    return grouped


def radar_row_from_flow(
    flow: TokenFlow,
    wallet_context: dict[str, dict[str, Any]],
    market_snapshot: dict[str, Any] | None,
    decimals: int | None,
    observed_at: str,
    window_hours: int,
    scan_payload: dict[str, Any],
) -> dict[str, Any]:
    price = float(market_snapshot.get("price_usd") or 0) if market_snapshot else 0.0
    divisor = 10 ** decimals if decimals is not None else None
    wallet_flows = []
    gross_buy_usd = 0.0
    gross_sell_usd = 0.0
    positive_wallets = []
    for wallet, raw_amount in sorted((flow.wallet_raw_flows or {}).items()):
        net_usd = 0.0
        if divisor and price > 0:
            net_usd = raw_amount / divisor * price
        if net_usd > 0:
            gross_buy_usd += net_usd
            positive_wallets.append(wallet)
        elif net_usd < 0:
            gross_sell_usd += abs(net_usd)
        wallet_flows.append(
            {
                "wallet_address": wallet,
                "net_buy_usd": round(net_usd, 6),
                "raw_token_flow": str(raw_amount),
            }
        )
    positive_contexts = [
        wallet_context[wallet]
        for wallet in positive_wallets
        if wallet in wallet_context
    ]
    strong_count = sum(
        1 for row in positive_contexts if row.get("label") == "strong_candidate"
    )
    watch_count = sum(
        1 for row in positive_contexts if row.get("label") == "watch_candidate"
    )
    weighted_wallet_score = average(
        [float(row.get("interest_score") or 0) for row in positive_contexts]
    )
    average_confidence = average(
        [float(row.get("confidence_score") or 0) for row in positive_contexts]
    )
    net_buy_usd = gross_buy_usd - gross_sell_usd
    return {
        "token_address": flow.token_address,
        "token_symbol": market_snapshot.get("token_symbol") if market_snapshot else None,
        "observed_at": observed_at,
        "window_hours": window_hours,
        "tracked_wallet_count": len(positive_wallets),
        "strong_wallet_count": strong_count,
        "watch_wallet_count": watch_count,
        "gross_buy_usd": gross_buy_usd,
        "gross_sell_usd": gross_sell_usd,
        "net_buy_usd": net_buy_usd,
        "buy_trade_count": flow.inbound_transfer_count,
        "sell_trade_count": flow.outbound_transfer_count,
        "first_trade_at": flow.first_trade_at,
        "last_trade_at": flow.last_trade_at,
        "weighted_wallet_score": weighted_wallet_score,
        "average_wallet_confidence": average_confidence,
        "accumulating_wallet_addresses": positive_wallets,
        "accumulating_wallet_net_buy_usd": [
            row["net_buy_usd"]
            for row in wallet_flows
            if row["wallet_address"] in positive_wallets
        ],
        "evidence": {
            "source_mode": "local_hypersync_transfer_proxy",
            "raw_logs_stored": False,
            "retention_policy": "keep_latest_local_snapshot_only",
            "transfer_proxy_warning": (
                "ERC-20 transfers are a local proxy for DEX trades; routed swaps, "
                "airdrops, bridge transfers, and wallet-to-wallet transfers need "
                "later swap decoding before this can equal Dune dex.trades."
            ),
            "price_source": LOCAL_MARKET_SOURCE if market_snapshot else None,
            "current_price_usd": price or None,
            "decimals": decimals,
            "amount_usd_method": (
                "current_price_times_transfer_amount"
                if decimals is not None and price > 0
                else "unpriced_transfer_count_only"
            ),
            "inbound_transfer_count": flow.inbound_transfer_count,
            "outbound_transfer_count": flow.outbound_transfer_count,
            "wallet_transfer_flows": wallet_flows,
            "hypersync_from_block": scan_payload.get("from_block"),
            "hypersync_to_block": scan_payload.get("to_block"),
            "hypersync_pages": scan_payload.get("page_count"),
        },
    }


def token_decimals(
    token_addresses: list[str],
    rpc: EvmRpcClient | None,
    warnings: list[str],
) -> dict[str, int]:
    client = rpc or EvmRpcClient(chain_id="base", min_delay_seconds=0.05)
    decimals = {}
    for address in token_addresses:
        try:
            decimals[address] = client.erc20_decimals(address)
        except (EvmRpcError, ValueError) as exc:
            warnings.append(f"decimals:{address}: {exc}")
    return decimals


def pair_tokens(
    pair_addresses: list[str],
    rpc: EvmRpcClient | None,
    warnings: list[str],
) -> dict[str, tuple[str, str]]:
    client = rpc or EvmRpcClient(chain_id="base", min_delay_seconds=0.05)
    tokens = {}
    for address in pair_addresses:
        try:
            token0, token1 = client.pair_tokens(address)
            tokens[address.lower()] = (token0.lower(), token1.lower())
        except (EvmRpcError, ValueError) as exc:
            warnings.append(f"pair_tokens:{address}: {exc}")
    return tokens


def wallet_context_by_address(wallets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row["wallet_address"]).lower(): row
        for row in wallets
        if row.get("wallet_address")
    }


def positive_wallet_count(flow: TokenFlow) -> int:
    return sum(1 for amount in (flow.wallet_raw_flows or {}).values() if amount > 0)


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_evm_address(value: Any) -> bool:
    text = str(value or "")
    if len(text) != 42 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
        return True
    except ValueError:
        return False
