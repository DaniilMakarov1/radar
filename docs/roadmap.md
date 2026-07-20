# Roadmap

## Current Decision

Start with Base as the first live MVP network while designing the data model for Base, Solana, BNB Chain, and Ethereum from day one.

Do not start with a constantly-running LLM agent. Start with a deterministic research and screening engine.

## Phase 0: Project Definition

Deliverables:

- Product spec.
- Architecture.
- Backtest methodology.
- Data source plan.
- Initial roadmap.

Status:

- Complete for initial project definition.

## Phase 1: Historical Binance Dataset

Goal:

Create a canonical Binance listing dataset.

Deliverables:

- Binance announcement collector.
- Normalized listing schema.
- Token-symbol-to-contract mapping process.
- Manual review workflow for ambiguous mappings.

Risks:

- Some announcements are not spot listings.
- Some symbols map to multiple contracts or chains.
- Some listings involve existing major tokens where DEX pre-listing behavior is less useful.

Current implementation:

- Local SQLite schema exists.
- Binance announcement catalog collector exists.
- Binance article detail collector exists.
- Raw announcement JSON is stored.
- Initial category classifier exists for spot, futures, tokenized stocks, margin, product bundles, and airdrops.
- Contract links are extracted from Binance announcement bodies when available.
- BaseScan, BscScan, Etherscan, and Solscan are represented in the chain/explorer config.
- Token/listing registry exists.
- Backtest target CSV export exists.
- Current local catalog has 699 Binance announcements, 586 normalized events,
  and 10 RPC-verified Base targets.

Operational note:

- Binance may rate-limit article detail backfills. Continue with `--start-page` after cooldown instead of restarting from page 1.

## Phase 2: Base Historical DEX Dataset

Goal:

Build the first usable historical dataset on Base.

Deliverables:

- Dune query templates for Base DEX trades.
- Token discovery from DexScreener.
- Liquidity snapshots.
- Wallet trade histories around listed tokens.
- Initial token quality filters.

## Phase 3: Point-in-Time Backtest

Goal:

Measure whether wallet and cluster behavior predicts future Binance listings.

Deliverables:

- Point-in-time dataset builder.
- Wallet scoring V0.
- Cluster heuristics V0.
- Token scoring V0.
- Backtest metrics.
- False positive review.

Exit criteria:

- Backtest can run without future leakage.
- Metrics are understandable.
- Top true positives and false positives can be inspected.

## Phase 4: Live Base Screener

Goal:

Detect new Base accumulation candidates.

Deliverables:

- Scheduled data refresh.
- Candidate signal generation.
- Risk filters.
- Simple local report output.
- Optional LLM-generated research memo.

Telegram can be added here or in the next phase.

## Phase 5: Alerts and Digest

Goal:

Deliver usable signals.

Deliverables:

- Telegram real-time alerts.
- Daily digest.
- Signal archive.
- Manual feedback tagging: useful, ignored, false positive, scam, late, too risky.

## Phase 6: BNB Chain Expansion

Goal:

Add BNB Chain because it is cheap, active, and close to Binance ecosystem.

Deliverables:

- BNB DEX ingestion.
- BNB-specific scam/risk filters.
- Cross-chain wallet/entity links.

## Phase 7: Ethereum High-Conviction Layer

Goal:

Use Ethereum for serious labels, fund-like wallets, and high-conviction signals.

Deliverables:

- Ethereum historical DEX ingestion.
- Better wallet labels.
- Fund/market-maker/team/CEX filtering.
- High-conviction watchlist generation.

## Phase 8: Solana Expansion

Goal:

Add Solana as a separate high-opportunity but high-noise pipeline after the EVM
research contract is stable.

Deliverables:

- Solana DEX/Jupiter historical ingestion.
- Solana token quality filters.
- Solana wallet/entity model.
- Helius/Birdeye live data integration if needed.

## Open Questions

- Minimum liquidity threshold for V1.
- Whether to include Binance Alpha/HODLer Airdrops in the initial target set.
- Whether to score futures listings separately from spot listings.
- How aggressive the system should be with memecoins.
- Which paid data source becomes worth it first if free-tier data is insufficient.

## Current Execution State

Implemented in code:

- a global `research_only` / `shadow_validation` / `trade_eligible` capital state;
- weekly EVM V4 universe generators with completed-week cutoffs and thirteen explicit zero-activity follow-up weeks;
- observation-safe 30/60/90-day labels, returns, drawdown, collapse/rug outcomes, and zero-flow event coverage;
- full wallet opportunity denominator with Bayesian shrinkage, PnL, turnover, relative sizing, holding, and exit quality;
- fail-closed three-hop identity coverage, CEX/service/team filtering, standard account owners, synchronized flows, and repeated rare route edges;
- Discovery, Pre-listing, Accumulation, and Hot/Momentum signal stages;
- X-independent Attention Gap using DexScreener, GDELT, GitHub, optional Farcaster, and on-chain acceleration;
- Base, BNB Chain, Ethereum, and separate Solana pipeline entry points;
- logistic and gradient-boosting models with a hard 100k/100-event gate, temporal embargo, bootstrap lift interval, and Brier baseline check;
- a research-only shadow ledger with liquidity-aware return marks;
- Prediction Radar with future-book paper execution, event-level wallet scores, fee fail-closed behavior, and seven-day retention;
- Research dashboard view and auditable SQLite model tables.

Current measured state on July 13, 2026:

- no Research Validity universe has been imported yet;
- identity coverage is 0%, so no live token is capital-eligible;
- the shadow ledger has begun collecting marks but has not reached 60 days;
- Prediction scan 6 covered 48 events, 728 markets, 594 order books, and found no executable route;
- Binance history contains 701 announcements, 586 normalized events, and 28 RPC-verified Base targets;
- wallet walk-forward run 10 remains diagnostic-only: 16 evaluated targets, 9.62% precision, and no valid wallet/token prediction claim;
- six ML runs correctly stopped at the empty dataset gate without fitting a model;
- automatic execution remains disabled even after future gates pass.

Latest Dune finding:

- the July 13 reset supplied 2,500 credits and API export attempts consumed 2,499.346;
- the completed V3 Base execution is retained for audit but is not imported because four follow-up weeks do not support honest 60/90-day labels;
- V4 is prepared with a `$200k / 20 / 10` default, 704,332 locally projected snapshots, 14 exported columns, resumable page cache, and a fail-closed cost preflight;
- follow `docs/dune_backfill_runbook.md` only after credits are added or reset;
- finish Base validity and identity first, then BNB Chain, Ethereum, and Solana;
- fit ML only when the dataset and validation gates pass;
- promote from research to shadow, then to capital eligibility only through measured gates.
