# Smart Money Radar

Smart Money Radar is a research-first on-chain intelligence system for finding early token accumulation patterns before major public hype and potential Binance listings.

The system is designed as a deterministic screener plus an optional LLM analyst layer. Market scanning, scoring, filtering, and backtesting should be handled by code. The LLM should only summarize already-computed evidence, risks, historical analogs, and confidence.

## Initial Scope

- Primary live network: Base.
- Research networks from day one: Base, Solana, BNB Chain, Ethereum.
- First exchange target: Binance listings only.
- First signal horizons: early signals and warm signals.
- Excluded for V1: intraday scalping, tokens with no website, obvious scams, honeypots, extreme tax tokens, and projects with unacceptably thin or suspicious liquidity.

## Core Question

Can we identify wallets and wallet clusters that historically accumulated tokens before Binance listings, then detect when similar independent clusters accumulate a new token while social hype is still low?

## Current Scoring Mode

The live radar currently runs in on-chain-only mode. X data is neither fetched nor used
as a gate unless `X_SOCIAL_GATE_ENABLED=1` is set explicitly. A live candidate must
instead pass contract and market checks, contain at least three independent wallet
clusters, leave at least $5,000 of positive flow after removing the largest cluster,
and have tracked activity within the last seven days. Blockscout must confirm a
verified non-scam contract, while Moralis independently checks top-holder
distribution. HyperSync direct-transfer evidence is recorded but is not yet a hard
gate because routed swaps may not transfer tokens directly to the signer. A wallet
cluster counts as one vote even when it contains several addresses. A cluster with
incomplete entity, funding, route, or owner coverage cannot vote as independent.

## Project Documents

- [Product Spec](docs/product_spec.md)
- [Architecture](docs/architecture.md)
- [Backtest Methodology](docs/backtest_methodology.md)
- [Data Sources](docs/data_sources.md)
- [Local Radar Analytics](docs/local_analytics.md)
- [Roadmap](docs/roadmap.md)
- [How It Works](docs/how_it_works.md)
- [Dune Backfill Runbook V4](docs/dune_backfill_runbook.md)
- [Funding Radar](docs/funding_radar.md)

## Current Local Commands

Initialize local storage:

```bash
python3 -m smart_money_radar.cli init-db
```

Collect recent Binance announcements:

```bash
python3 -m smart_money_radar.cli collect-binance --pages 1 --page-size 20 --with-details
```

Inspect collected announcements:

```bash
python3 -m smart_money_radar.cli show-binance --limit 20
```

Build the token/listing registry from collected announcements:

```bash
python3 -m smart_money_radar.cli sync-registry --rebuild
```

Verify Base ticker-to-contract mappings on-chain before generating research SQL:

```bash
python3 -m smart_money_radar.cli verify-base-contracts
```

BNB Chain and Ethereum mappings use the same fail-closed RPC verification:

```bash
python3 -m smart_money_radar.cli verify-evm-contracts --chain bsc
python3 -m smart_money_radar.cli verify-evm-contracts --chain ethereum
```

Inspect the registry:

```bash
python3 -m smart_money_radar.cli registry-report --limit 30
```

Export Binance spot targets for backtest research:

```bash
python3 -m smart_money_radar.cli export-backtest-targets --output exports/backtest_targets.csv
```

Show concise project status:

```bash
python3 -m smart_money_radar.cli status
```

Write a local Markdown/HTML report:

```bash
python3 -m smart_money_radar.cli report
```

Initialize and inspect the local Dune-like analytics layer:

```bash
python3 -m smart_money_radar.cli analytics-init
python3 -m smart_money_radar.cli analytics-list
```

Run a saved local analytics query without calling Dune:

```bash
python3 -m smart_money_radar.cli analytics-run \
  --slug pre-listing-wallet-leaders \
  --limit 20
```

Run a custom read-only SQL query over local Radar datasets:

```bash
python3 -m smart_money_radar.cli analytics-query \
  --sql "SELECT * FROM analytics_live_signals ORDER BY confidence_score DESC" \
  --limit 20
```

Generate Dune SQL for Base pre-listing buyers:

```bash
python3 -m smart_money_radar.cli render-dune-base-buyers
```

Generate Dune SQL for Base sell-side / holding behavior:

```bash
python3 -m smart_money_radar.cli render-dune-base-holding
```

Check Dune API configuration:

```bash
python3 -m smart_money_radar.cli dune-health
```

Dry-run the generated Dune SQL execution:

```bash
python3 -m smart_money_radar.cli dune-execute-sql \
  --sql-file queries/generated/base_pre_listing_buyers.generated.sql \
  --output exports/base_pre_listing_buyers.json \
  --dry-run
```

Execute and poll Dune SQL after adding `DUNE_API_KEY` to `.env`:

```bash
python3 -m smart_money_radar.cli dune-execute-sql \
  --sql-file queries/generated/base_pre_listing_buyers.generated.sql \
  --output exports/base_pre_listing_buyers.json \
  --poll
```

Fetch all pages for an existing Dune execution:

```bash
python3 -m smart_money_radar.cli dune-fetch-results \
  --execution-id EXECUTION_ID \
  --output exports/base_pre_listing_buyers.json
```

Import Base pre-listing buyers:

```bash
python3 -m smart_money_radar.cli import-base-buyers \
  --input exports/base_pre_listing_buyers.json
```

Import Base holding behavior:

```bash
python3 -m smart_money_radar.cli import-base-holding \
  --input exports/base_holding_behavior.json
```

Both imports replace the complete Base research snapshot, preventing stale rows
from an older announcement cutoff from leaking into a new model run.

Inspect imported pre-listing buyers:

```bash
python3 -m smart_money_radar.cli buyers-report --limit 20
```

Inspect sell-side / holding behavior:

```bash
python3 -m smart_money_radar.cli holding-report --limit 20
```

Compute wallet diagnostics and provisional V1 scores:

```bash
python3 -m smart_money_radar.cli score-wallets
```

Run the strict chronological wallet walk-forward:

```bash
python3 -m smart_money_radar.cli run-backtest
python3 -m smart_money_radar.cli backtest-report
```

Generate and execute point-in-time token negative controls:

```bash
python3 -m smart_money_radar.cli render-dune-negative-controls
python3 -m smart_money_radar.cli dune-execute-sql \
  --sql-file queries/generated/base_negative_controls.generated.sql \
  --output exports/base_negative_controls.json \
  --performance medium --poll
python3 -m smart_money_radar.cli import-negative-controls
```

Inspect wallet scores:

```bash
python3 -m smart_money_radar.cli wallet-report --limit 20
```

Inspect repeated wallets across multiple Binance backtest targets:

```bash
python3 -m smart_money_radar.cli repeatability-report --limit 20
```

Export wallet scores to CSV:

```bash
python3 -m smart_money_radar.cli export-wallet-scores --output exports/wallet_scores.csv
```

Run a 14-day near-live Base scan using qualified wallets, then enrich the top
tokens with DexScreener market data and GoPlus contract risk:

```bash
python3 -m smart_money_radar.cli live-scan
python3 -m smart_money_radar.cli live-report --limit 20
```

Run the local Base scan without Dune. It uses wallet `Transfer` events to find
candidate tokens, scans the top liquid DexScreener pairs for V2/V3 `Swap`
events, writes compact `local_dex_trades`/rollups, and keeps only the latest
local snapshot. Raw HyperSync logs are not stored:

```bash
python3 -m smart_money_radar.cli local-live-scan \
  --window-hours 24 \
  --max-wallets 200 \
  --max-tokens 100 \
  --max-pairs-per-token 5
python3 -m smart_money_radar.cli live-report --limit 20
```

Use `--no-transfer-fallback` to write only swap-backed observations, and
`--use-cursor` to start pool-swap ingestion after the stored local block cursor.

Run the local dashboard:

```bash
python3 -m smart_money_radar.cli dashboard
```

Then open:

```text
http://127.0.0.1:8787
```

## Prediction Radar

Collect free public Polymarket and Kalshi market data, normalize resolution
contracts, scan full orderbook depth, and create deterministic paper executions:

```bash
python3 -m smart_money_radar.cli prediction-scan
python3 -m smart_money_radar.cli prediction-report
```

Continuously refresh market books without repeatedly downloading wallet history:

```bash
python3 -m smart_money_radar.cli prediction-watch \
  --interval-seconds 60 \
  --skip-wallets
```

The dashboard has a dedicated `Prediction` view. It lists only paper candidates
that remain profitable after executable VWAP, verified venue fees, an operations
buffer, semantic contract checks, and minimum profit gates. Multi-leg execution is
treated as non-atomic. Paper fills use a genuinely later orderbook snapshot; all
live execution is disabled.

Current route families:

- binary YES/NO complement;
- exhaustive multi-outcome complete set;
- standard negative-risk conversion;
- deterministic threshold implication;
- semantically matched Polymarket/Kalshi complement.

Augmented negative-risk events are never assumed exhaustive. Cross-venue routes
with different cancellation or resolution terms remain in contract review even
when their displayed prices appear profitable.

## Funding Radar

Scan public Binance USD-M Futures, Bitget, Bybit, OKX, Gate, KuCoin Futures,
MEXC, Kraken, Deribit, Backpack, Aster, Hyperliquid, dYdX, Lighter, Paradex,
Vertex Base, and freshness-gated Drift perpetual markets for pairwise
delta-neutral funding carry routes:

```bash
python3 -m smart_money_radar.cli funding-scan \
  --target-notional 10000 \
  --horizon-mode next_settlement
python3 -m smart_money_radar.cli funding-report
```

The supported trading horizons are `next_settlement`, fixed `4`, `8`, or `24`
hours. The explicit `research` mode is a non-actionable 72-hour stress scenario.
Progressively collect the 90-day public funding dataset with:

```bash
python3 -m smart_money_radar.cli funding-history-backfill --days 90 --limit 200
python3 -m smart_money_radar.cli funding-history-backfill --venue mexc --limit 50
python3 -m smart_money_radar.cli funding-history-backfill --venue paradex --limit 10
```

Continuously refresh books and revalidate paper candidates on a later snapshot:

```bash
python3 -m smart_money_radar.cli funding-watch \
  --target-notional 10000 \
  --horizon-mode fixed \
  --horizon-hours 24 \
  --interval-seconds 60
```

The dashboard's `Funding` view separates routes that pass every gate from the
watchlist, displays the current funding rate and interval for both venues, and
currently refreshes only on a manual request. The scanner generates every
cross-venue route in the live universe with no production top-N cap. It fetches
full books for every route whose zero-slippage settlement carry can still cover
the cheaper of taker or maker-assisted fees, basis reserve, and operations
reserve; all other routes remain stored with an auditable exclusion reason.
Routes below $500 capacity on the smaller leg are hidden, and the model sizes each
remaining route with equal canonical base-asset quantity on both venues before
applying profitability gates. Manual and watch refreshes incrementally update
stale history; auto snapshots never wait for a backfill. The separate resumable
`funding-history-backfill` command remains responsible for broad 90-day coverage.
Capacity consumes every available orderbook level returned by the venue API,
without a fixed BPS prefilter. The resulting VWAP slippage is charged in full,
and reliance on levels farther than 100 bps is surfaced as a risk.
The latest 60 local L2 snapshots are also used to measure executable-depth
persistence, near-touch refill after liquidity shocks, and a conservative
120-second maker price-through probability. Maker outcomes split both-fill,
one-leg, and no-fill cases and reserve observed post-fill adverse selection; they
remain shadow-only because public L2 data cannot reconstruct queue position.
An on-screen first-failed-gate funnel explains zero-candidate scans without
double-counting blockers and shows the nearest routes plus realistic VIP/maker
improvement scenarios when they exist.
Refresh cycles do not overlap. Calculations use both entry and exit orderbook sides, round-trip taker
fees, signed executable basis stress and operations reserves, collateral on both venues, normalized
hourly funding history, discrete settlement events or Paradex continuous-accrual
hourly checkpoints, settlement-by-settlement decay, historical outcome
distributions, Q25 net PnL, regime age, settlement schedules, history coverage,
per-leg dynamic next-funding nowcast weighting applied only to each nearest settlement,
24-hour and 72-hour realized spread,
14-day persistence, a non-predictive 30-day regime filter, 90-day anomaly context, conditional
4/8/24-hour regime survival, full collateral on both legs, a $5/10-bps actionable
profit gate, a separate large-basis coverage stress, canonical normalization for
bundled contracts such as `1000PEPE`, and a depth haircut. In `next_settlement`
mode, current executable net PnL controls candidacy while historical Median, Q25,
persistence, and win probability are advisory warnings. Fixed 4/8/24-hour carry
continues to require the historical gates. A route is re-authorized after every
crossed settlement.
A failed venue is reported but does not abort valid pairs between the
remaining venues. Live execution is disabled. See
[Funding Radar](docs/funding_radar.md) for the model and current limitations.

## Research Validity V3 Outcomes / V4 Universe

Prepare and validate every generated SQL artifact without creating a Dune execution:

```bash
python3 -m smart_money_radar.cli prepare-dune-backfill
```

Measure the Base universe before downloading it:

```bash
python3 -m smart_money_radar.cli research-universe-probe --chain base
```

Build weekly point-in-time snapshots and outcome labels:

```bash
python3 -m smart_money_radar.cli research-universe-backfill \
  --chain base \
  --minimum-weekly-volume-usd 200000 \
  --minimum-weekly-trades 20 \
  --minimum-weekly-traders 10
python3 -m smart_money_radar.cli research-validity --chain base
```

V4 emits thirteen follow-up weeks and exports only fourteen model-relevant
columns. Before manually fetching an existing execution, inspect the projected
cost with `dune-export-plan`; the automated backfill applies the same budget gate.

Build the complete opportunity denominator for the first 50 wallets, then 200:

```bash
python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 50
python3 -m smart_money_radar.cli wallet-opportunity-backfill --chain base --max-wallets 200
python3 -m smart_money_radar.cli wallet-weekly-flows --chain base --max-wallets 200
python3 -m smart_money_radar.cli identity-graph-v2 --max-wallets 200
```

Collect Attention Gap without X and inspect research readiness:

```bash
python3 -m smart_money_radar.cli attention-gap --max-tokens 3
python3 -m smart_money_radar.cli research-status
```

Cross-chain history uses the same EVM pipeline; Solana remains separate:

```bash
python3 -m smart_money_radar.cli cross-chain-history --chain bsc
python3 -m smart_money_radar.cli cross-chain-history --chain ethereum
python3 -m smart_money_radar.cli solana-history
```

The model command enforces the dataset gate. It stores a blocked run without fitting
anything until there are at least 100,000 mature token-time rows and 100 independent
listing events:

```bash
python3 -m smart_money_radar.cli train-research-models --chain evm
```

Dune query execution and result export consume separate credits. Check the account's
current billing-period usage and enable extra credits in Dune subscription settings
before a large export. A successful broad execution can be reused with
`--execution-id` so thresholds are applied server-side without recomputing the SQL.

Run tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Current Research Artifacts

- Backtest target CSV: [exports/backtest_targets.csv](exports/backtest_targets.csv)
- Wallet score CSV: [exports/wallet_scores.csv](exports/wallet_scores.csv)
- Latest Markdown report: [reports/latest.md](reports/latest.md)
- Latest HTML report: [reports/latest.html](reports/latest.html)
- Generated Base buyers SQL: [queries/generated/base_pre_listing_buyers.generated.sql](queries/generated/base_pre_listing_buyers.generated.sql)
- Generated Base holding SQL: [queries/generated/base_holding_behavior.generated.sql](queries/generated/base_holding_behavior.generated.sql)
- Generated Base negative-control SQL: [queries/generated/base_negative_controls.generated.sql](queries/generated/base_negative_controls.generated.sql)
- Generated Base Live Radar SQL: [queries/generated/base_live_radar.generated.sql](queries/generated/base_live_radar.generated.sql)
- Base pre-listing buyers Dune template: [queries/dune/base_pre_listing_buyers.sql](queries/dune/base_pre_listing_buyers.sql)
- Base liquidity context Dune template: [queries/dune/base_target_liquidity_context.sql](queries/dune/base_target_liquidity_context.sql)

## Guiding Principle

No signal should be trusted until it survives point-in-time backtesting. The system must never use future data to explain past success.

The dashboard therefore separates current research candidates from acceptance
readiness. A high wallet-flow score is not presented as proven Binance-listing
alpha while token negative controls still fail.
