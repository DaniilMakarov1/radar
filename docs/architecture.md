# Architecture

## Design Principles

- Build as modular data pipelines, not as a constantly-running LLM agent.
- Use deterministic code for collection, normalization, scoring, filtering, and backtesting.
- Use LLM calls only for signal explanation, daily summaries, and research memos.
- Keep every module network-aware so new chains can be added without rewriting the system.
- Store raw data and normalized data separately.
- Every scored feature should be reproducible from stored data.
- Every signal should be auditable.

## High-Level Flow

```text
Data Sources
  Binance announcements
  DEX trades
  token metadata
  liquidity
  wallet transactions
  token security
  social data

        ↓

Ingestion
  raw API pulls
  scheduled jobs
  webhook listeners

        ↓

Warehouse
  normalized tokens
  listings
  wallets
  trades
  liquidity snapshots
  labels
  social snapshots
  risk snapshots

        ↓

Research / Backtest
  point-in-time datasets
  feature generation
  wallet scoring
  token scoring
  historical simulations

        ↓

Live Scoring
  wallet cluster detection
  accumulation detection
  risk filters
  social silence filters
  signal gate

        ↓

Analyst Layer
  LLM summary only after candidate passes filters

        ↓

Alerts
  console/logs first
  Telegram later
  dashboard later
```

## Suggested Repository Structure

```text
smart-money-radar/
  docs/
  ingestion/
    binance/
    dex/
    market_data/
    social/
    security/
  warehouse/
    migrations/
    models/
  research/
    notebooks/
    experiments/
  scoring/
    wallets/
    clusters/
    tokens/
    risks/
    social/
  backtest/
    datasets/
    simulations/
    metrics/
  alerts/
    telegram/
    digest/
  agent/
    prompts/
    reports/
  app/
    api/
    dashboard/
```

## Core Modules

### Research Validity V2 Tables

- `research_universe_snapshots`: weekly point-in-time token universe by chain.
- `research_token_outcomes`: mature 30/60/90-day labels, returns, drawdowns, and collapse/rug outcomes.
- `research_event_coverage`: Binance events including explicit zero-flow observations.
- `wallet_token_opportunities`: complete buy/sell/miss denominator for selected wallets.
- `wallet_token_weekly_flows`: historical point-in-time wallet features.
- `wallet_research_scores`: Bayesian wallet posterior, PnL, risk, turnover, and exit quality.
- `wallet_identity_edges`: multi-hop funding, owner, synchronous, route, CEX/MM/team evidence.
- `token_attention_snapshots`: X-independent public/on-chain acceleration gap.
- `research_model_runs` and `research_model_predictions`: temporal model audit trail.

### Ingestion

Collect raw data from external sources.

Initial jobs:

- Binance listing announcements.
- Token metadata and contracts.
- DEX trades on Base.
- Historical DEX trades for Base, Solana, BNB Chain, Ethereum.
- Liquidity snapshots.
- Token security snapshots.
- Basic social snapshots.

### Warehouse

The warehouse stores both raw and normalized tables.

Initial table families:

- `raw_*`: raw API responses.
- `tokens`: canonical token registry.
- `listings`: Binance listing events.
- `chains`: chain metadata.
- `wallets`: canonical wallet registry.
- `trades`: normalized DEX trades.
- `liquidity_snapshots`: pair and token liquidity over time.
- `wallet_scores`: point-in-time wallet scores.
- `token_scores`: point-in-time token scores.
- `risk_snapshots`: token risk checks.
- `social_snapshots`: social attention metrics.
- `signals`: generated signal events.

### Scoring

Scoring should be decomposed into independent components:

- Wallet quality score.
- Cluster independence score.
- Token quality score.
- Accumulation score.
- Social silence score.
- Historical similarity score.
- Risk score.
- Final signal score.

### Backtest

The backtest engine must generate point-in-time datasets. At each historical date, the system can only use data available before or at that date.

Backtest must evaluate:

- Did the token get listed on Binance later?
- How far ahead was the signal?
- Was there enough liquidity to enter?
- What was the estimated slippage?
- Was social hype already present?
- Was the signal driven by independent clusters?

### Analyst Layer

The LLM should never be responsible for raw market scanning.

LLM responsibilities:

- Explain why a candidate passed filters.
- Compare the candidate with historical analogs.
- Summarize risks.
- Generate a Telegram-ready alert.
- Generate a daily digest.

LLM calls should be triggered only after deterministic scoring creates a candidate.

### Prediction Radar

Prediction-market collection and execution simulation is a separate deterministic
pipeline that shares the SQLite warehouse and local dashboard:

```text
Polymarket Gamma/CLOB + Data API    Kalshi public REST
                  -> contract normalization
                  -> orderbook snapshots
                  -> route constraints and cross-venue matching
                  -> full-depth VWAP and verified fee metadata
                  -> future-orderbook paper execution
                  -> paper-candidate dashboard
```

Core tables:

- `prediction_events` and `prediction_markets`: normalized resolution contracts;
- `prediction_orderbook_snapshots`: YES/NO depth for every scanned market;
- `prediction_constraints`: deterministic implication relationships;
- `prediction_contract_matches`: audited cross-venue semantic matches;
- `prediction_routes`: profitable and rejected route evaluations;
- `prediction_paper_executions`: latency, partial-fill, and queue audit trail;
- `prediction_wallet_positions` and `prediction_wallet_scores`: provisional
  Polymarket specialist history with Bayesian shrinkage;
- `prediction_event_token_links`: explicit links into the token registry.

The scanner never relies on an LLM. An LLM may later explain an already-qualified
route, but cannot modify prices, fees, contract equivalence, or execution status.
Unknown fee schedules, non-identical resolution terms, and non-atomic execution
remain blocked from capital even when the displayed spread is positive.

Signal stages are separate contracts:

- `discovery`: young token and early independent flow;
- `pre_listing`: research model probability after the dataset gate;
- `accumulation`: established token with smart-wallet accumulation;
- `hot_momentum`: flow already accompanied by public promotion or sharp momentum.

## First Technical Direction

For V1 implementation:

- Python for ingestion, scoring, and backtests.
- Postgres for normalized storage.
- DuckDB or local parquet for early research iterations.
- Dune for historical DEX datasets.
- DexScreener for pair/liquidity discovery.
- Etherscan-like APIs and Moralis where useful for EVM data.
- Helius/Birdeye later for Solana-specific live monitoring.
