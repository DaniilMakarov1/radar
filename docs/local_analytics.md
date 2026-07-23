# Local Radar Analytics

Local Radar Analytics is the internal Dune-like layer for Smart Money Radar. It
lets the project run repeatable analytical SQL against local Radar datasets
without spending Dune credits.

## Current Scope

The first implementation is intentionally local and conservative:

- SQLite is the execution engine because it is already part of the project.
- Analytics SQL is read-only and must start with `SELECT` or `WITH`.
- Write/admin keywords are blocked before execution.
- Results are bounded by a configurable row limit.
- Every run is recorded in `analytics_executions`.
- Built-in query definitions are stored in `analytics_queries`.
- Curated datasets are exposed as `analytics_*` views.

This layer does not yet replace Dune as a raw historical `dex.trades` data
source. It replaces the repeated analytical pass over data that Radar already
stores locally. The next milestone is to feed these same curated views from
locally collected swap/transfer data.

## Curated Views

- `analytics_pre_listing_wallet_flows`: historical wallet flow, holding behavior,
  and wallet score context.
- `analytics_research_token_snapshots`: point-in-time token universe, outcomes,
  and latest attention gap context.
- `analytics_live_signals`: current live candidate signal facts.
- `analytics_funding_routes`: latest funding route candidates.
- `analytics_prediction_routes`: latest prediction-market route candidates.

## Commands

Initialize or refresh the local analytics catalog:

```bash
python3 -m smart_money_radar.cli analytics-init
```

List local views and saved queries:

```bash
python3 -m smart_money_radar.cli analytics-list
```

Run a built-in query:

```bash
python3 -m smart_money_radar.cli analytics-run \
  --slug pre-listing-wallet-leaders \
  --limit 20
```

Run custom read-only SQL:

```bash
python3 -m smart_money_radar.cli analytics-query \
  --sql "SELECT chain_id, COUNT(*) AS rows FROM analytics_research_token_snapshots GROUP BY chain_id" \
  --limit 20
```

Emit JSON for downstream tools:

```bash
python3 -m smart_money_radar.cli analytics-run \
  --slug research-universe-readiness \
  --json
```

## Built-in Queries

- `pre-listing-wallet-leaders`
- `research-universe-readiness`
- `live-signal-funnel`
- `funding-route-leaders`
- `prediction-route-leaders`

## Dune Replacement Roadmap

The remaining Dune dependency is raw historical DEX coverage. Replace it in
stages:

1. Store raw EVM logs/transfers locally for tracked tokens and wallets.
2. Decode DEX swap events for the protocols Radar actually uses on Base first.
3. Build a local `dex_trades`-equivalent fact table with time, wallet, token,
   side, amount, and USD notional.
4. Re-point pre-listing, live-radar, wallet-opportunity, and universe models from
   Dune SQL artifacts to local SQL models.
5. Move large local history to DuckDB/Parquet or ClickHouse when SQLite becomes
   the bottleneck.

The API boundary should remain the same: analytical code reads curated views,
while ingestion modules decide whether rows came from Dune imports, HyperSync,
Blockscout, RPC, or another provider.

## Local Base Live Ingestion

The first local Dune-replacement ingestion block is available as:

```bash
python3 -m smart_money_radar.cli local-live-scan
```

The scan works in two passes:

1. It scans Base ERC-20 `Transfer` events for the current qualified wallet set
   through HyperSync. This finds candidate tokens without Dune.
2. For candidates with a DexScreener pair, it scans that pair for V2/V3-style
   `Swap` events. The scanner now takes the top liquid pair candidates per token,
   not only the single best DexScreener pair.
3. It keeps a local trade when either the same transaction moved the candidate
   token into/out of a tracked wallet, or the swap transaction was sent by a
   tracked wallet and RPC `token0`/`token1` confirms which swap amount belongs to
   the candidate token.

The compact local facts are written to:

- `local_dex_trades`: one compact wallet/token trade fact per swap-backed tx;
- `local_dex_rollups`: token-level wallet-flow rollups for the current scan;
- `radar_observations`: latest local live observations, with source
  `local_hypersync_dex_trades` for swap-backed rows and
  `local_hypersync_transfer_proxy` only for fallback rows.

Signals are generated only from swap-backed observations and only when the
existing signal builder returns `candidate` or `needs_review`. Filtered local
signals are not stored.

Retention is deliberately strict:

- raw HyperSync logs are never written to disk;
- `local_dex_trades` and `local_dex_rollups` keep only the latest local snapshot;
- local `radar_observations` keep only the latest snapshot per local source;
- local DexScreener market snapshots use source `local_live_dexscreener` and keep
  only the latest local snapshot;
- local signals keep only the latest local detected timestamp, and are deleted
  when a new local scan produces no signal-passing token;
- `local_ingestion_cursors` stores only the latest block cursor and metadata;
- historical paper/training ledgers are not touched.

Useful controls:

```bash
python3 -m smart_money_radar.cli local-live-scan \
  --window-hours 24 \
  --max-wallets 200 \
  --max-tokens 100 \
  --max-pairs-per-token 5 \
  --min-pair-liquidity-usd 1000
python3 -m smart_money_radar.cli local-live-scan --no-transfer-fallback
python3 -m smart_money_radar.cli local-live-scan --use-cursor
python3 -m smart_money_radar.cli local-live-scan --no-signals
```

Current limitation: the local decoder covers common V2/V3-style pool `Swap`
events and infers the tracked wallet side from same-transaction ERC-20 transfers
or tracked `tx_from` swap senders. It is already stricter than transfer-only
flow, but it is not yet a full historical Dune `dex.trades` clone across every
Base DEX/router path or trace shape.
