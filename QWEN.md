# Smart Money Radar — Agent Instructions

## Project Overview

Smart Money Radar is a research-first on-chain intelligence system. Three active modules:

1. **Funding Radar** — delta-neutral funding carry scanner across 17+ perp venues
2. **Prediction Radar** — prediction market arbitrage (Polymarket + Kalshi)
3. **Local Analytics (Dune analog)** — local SQLite analytics layer replacing Dune for research validity

## Goal

Ship production-quality modules. Do not stop on minor blockers — work around them, note them, and continue. Prioritize forward progress.

## Working Principles

### Autonomy & Tempo
- **Do not stop on minor issues.** If a lint warning, import ordering, or cosmetic issue blocks progress, note it and move on. Fix cosmetic issues only after functional work is done.
- **No artificial tool-call limits.** Work through problems thoroughly. If a task requires many file reads, edits, or searches, do them all — do not pause to ask "should I continue?"
- **Be autonomous.** Make reasonable decisions without asking. Only ask when the choice is genuinely ambiguous or has significant consequences.
- **No progress reports.** Do not narrate every step. Work silently, deliver results.
- **Batch work efficiently.** Read multiple files in parallel. Make independent edits concurrently. Minimize round-trips.
- **Fail-closed by default.** Every external integration must gracefully degrade when APIs are unavailable or keys are missing.

### Code Quality
- **Think before coding.** State assumptions explicitly. If multiple interpretations exist, present them — don't pick silently. If a simpler approach exists, say so.
- **Simplicity first.** Minimum code that solves the problem. No features beyond what was asked. No abstractions for single-use code. No speculative "flexibility." If 200 lines could be 50, rewrite it.
- **Surgical changes.** Touch only what you must. Don't "improve" adjacent code, comments, or formatting. Don't refactor things that aren't broken. Match existing style exactly. Every changed line should trace directly to the user's request.
- **Goal-driven execution.** Transform tasks into verifiable goals. For multi-step tasks, state a brief plan with verification checks. Strong success criteria let you loop independently.
- **Preserve existing architecture.** This is a mature codebase built with OpenAI Codex. Follow existing patterns, naming conventions, and module structure exactly.
- **Test after changes.** Run `python3 -m pytest tests/ -x -q` after modifications. (unittest discover misses pytest-style function tests — 31 tests silently skipped.)

### Git & Repo Hygiene
- **Commit coherent completed work.** Don't commit half-finished features.
- **Write clear commit messages.** Format: `module: brief description of what changed`.
- **Never force-push or rebase without explicit permission.**
- **Don't commit secrets.** All secrets go in `.env`, never in code or config files.
- **Check `git status` before committing.** Don't stage unrelated changes.

## Tech Stack

- Python 3.11+, no external web framework for core logic
- SQLite (warehouse/radar.sqlite) for all storage
- scikit-learn, scipy, joblib for ML
- websocket-client for real-time feeds
- Local dashboard on port 8787

## Key Commands

```bash
# Status
python3 -m smart_money_radar.cli status

# Funding
python3 -m smart_money_radar.cli funding-scan --target-notional 10000 --horizon-mode next_settlement
python3 -m smart_money_radar.cli funding-report
python3 -m smart_money_radar.cli funding-watch --target-notional 10000 --horizon-mode fixed --horizon-hours 24 --interval-seconds 60
python3 -m smart_money_radar.cli funding-history-backfill --days 90 --limit 200

# Prediction
python3 -m smart_money_radar.cli prediction-scan
python3 -m smart_money_radar.cli prediction-report
python3 -m smart_money_radar.cli prediction-watch --interval-seconds 60 --skip-wallets

# Analytics (Dune analog)
python3 -m smart_money_radar.cli analytics-init
python3 -m smart_money_radar.cli analytics-list
python3 -m smart_money_radar.cli analytics-run --slug <slug> --limit 20
python3 -m smart_money_radar.cli analytics-query --sql "..." --limit 20

# Research
python3 -m smart_money_radar.cli research-universe-backfill --chain base
python3 -m smart_money_radar.cli research-validity --chain base
python3 -m smart_money_radar.cli train-research-models --chain evm

# Tests
python3 -m pytest tests/ -x -q
```

## Module Locations

- Funding: `smart_money_radar/funding/`
- Prediction: `smart_money_radar/prediction/`
- Analytics/Dune analog: `smart_money_radar/analytics.py`, `smart_money_radar/dashboard_dune.py`, `smart_money_radar/dune_queries.py`, `smart_money_radar/research_validity.py`
- Core: `smart_money_radar/cli.py`, `smart_money_radar/config.py`, `smart_money_radar/storage.py`
- Dashboard: `smart_money_radar/dashboard.py`, `smart_money_radar/web/`
- Schema: `warehouse/schema.sql`
- Docs: `docs/`
- Scripts (launchd bots): `scripts/`

## Code Conventions

- Deterministic pipelines, not LLM agents
- Fail-closed: missing API keys or unavailable services must not crash
- All scoring is code-driven; LLM is advisory only
- Point-in-time correctness: never use future data for past signals
- SQLite for all persistence; no Postgres dependency in runtime
- Environment variables for all secrets; `.env.example` documents all keys
- Type hints in Python
- Business logic separated from CLI routes
- Clear names for models, services, providers
- Avoid large monolithic files; refactor only when the current feature needs a cleaner boundary

## Architecture Notes

- **Funding module** scans 17+ perp venues (Binance, Bitget, Bybit, OKX, Gate, KuCoin, MEXC, Kraken, Deribit, Backpack, Aster, Hyperliquid, dYdX, Lighter, Paradex, Vertex, Drift) for pairwise delta-neutral funding carry. Full orderbook depth, VWAP slippage, maker/taker fee modeling, settlement schedules, regime filters, persistence analysis.
- **Prediction module** scans Polymarket + Kalshi for arbitrage: binary complement, multi-outcome complete set, negative-risk conversion, threshold implication, cross-venue semantic match. Paper execution only.
- **Analytics module** is a local Dune replacement: SQLite-backed, saved queries, custom SQL, research validity pipeline with point-in-time snapshots and outcome labels.
- **Dashboard** serves on port 8787 with dedicated views for Funding, Prediction, and Analytics.
- **Launchd bots** in `scripts/` run funding-watch, prediction-watch, and dashboard as macOS services.

## Error Handling Patterns

- Wrap external API calls in try/except; log and continue
- Missing API keys → skip that venue/source, don't crash
- Network timeouts → retry with backoff, then skip
- Invalid data → validate at boundaries, reject early
- SQLite locking → use WAL mode, short transactions

## User Interaction Patterns

The user (Daniil) works in a specific style learned from extensive Codex sessions:

- **Directives, not questions.** "Сделай это", "Реализуй", "Продолжай" — implement, don't propose.
- **Challenge back.** User explicitly asked: "поспорь со мной, не пытайся мне понравиться." Give honest pushback when logic is wrong.
- **Think like a fund CEO.** User frames tasks as "ты генеральный директор крипто хедж фонда" — think strategically about capital, risk, and edge.
- **Explain simply when asked.** "Объясни как для чайника" — break down complex logic into plain language.
- **No artificial limits.** User repeatedly removed route caps, BPS filters, and top-N limits. Don't add arbitrary caps without justification.
- **Speed matters.** Dashboard refresh, bot scan cycles, and API calls should be fast. Flag and fix latency issues.
- **Visual results.** User prefers dashboard over terminal. Keep the web UI clean — remove clutter, keep only decision-relevant data.
- **Telegram notifications.** Funding bot sends status to Telegram. Keep messages formatted with line breaks and clear structure.
- **Data hygiene.** Don't accumulate scan history, logs, or raw API responses. Keep only what's needed for training and audit.

## Funding Module Specifics

- **Settlement timing is critical.** Each venue has different funding intervals (1h, 4h, 8h). The bot must account for actual interval, not assume uniform.
- **Funding rate normalization.** Always verify whether a venue reports rate per-interval or annualized. MEXC, Binance, OKX all differ.
- **Route filtering funnel.** User wants to see WHERE routes get filtered out. The first-failed-gate funnel explains zero-candidate scans.
- **No hard profit floor.** Show all routes with positive PnL, sorted descending. Don't hide small opportunities.
- **Dynamic BPS threshold.** Replace fixed BPS cutoffs with dynamic thresholds based on fee tier and venue.
- **Parallel route checking.** When multiple candidates exist, check them in parallel (worker pool), not sequentially.
- **Bot scan intervals:**
  - No routes → full scan every 5 min
  - Watch route exists → focused recheck every 120s
  - ≤3 min to settlement → focused recheck every 10s
  - Pending/open position → focused recheck every 10s
- **Arm window.** Bot must send final API request no later than 15 seconds before settlement window closes, not in the last 0-10 seconds.
- **Position closing.** Keep position open if arbitrage window continues after first settlement. Only close when the window genuinely ends.

## Dashboard / UI Preferences

- Keep only decision-relevant columns. Remove clutter.
- Show funding rate and interval for BOTH venues in candidates.
- Include "Needed Improvement" column: what fee tier, maker vs taker, or basis change would make a route profitable.
- Export to CSV/Excel button for trade reports.
- Date/time format: human-readable, no "T" separator.
- Auto-refresh: 20 seconds (not 10).
- Minimum capacity filter: $500 per leg (hide routes below).
- Separate candidates (pass all gates) from watchlist.

## Data Retention

- Keep only last 1 scan snapshot (dynamic: routes + 1); delete older ones.
- Keep trade/paper execution history for training.
- Delete raw API responses after normalization.
- funding_rate_history: keep only what's needed for forecast (72h + 14d + 30d windows), prune older.
- SQLite WAL mode for concurrent access.

## What NOT To Do

- Don't add LLM calls to deterministic scoring pipelines
- Don't introduce new dependencies without justification
- Don't change the SQLite schema without a migration path
- Don't store raw API responses — normalize and compact
- Don't use future data for past signals (point-in-time violation)
- Don't enable live execution — paper only
- Don't commit .env files or secrets
- Don't add arbitrary route caps (top-N, max routes) without justification
- Don't assume uniform funding intervals across venues
- Don't close positions after first settlement if the arbitrage window continues
- Don't use risky/unverified exchanges (BloFin, Bitunix, Phemex, BingX removed by user — see `funding/venues.py`)
- Don't show completed/expired scenarios or routes with negative PnL in the dashboard
- Don't let one module's bot send messages about another module (funding bot ≠ prediction bot)
- Don't show irrelevant data — if no candidates, say "no candidates", don't fill with noise

## Cross-Module Patterns

- Reuse patterns across modules: funding adapter pattern → prediction clients, paper execution → prediction paper, Telegram notifications → all bots.
- When building a new module, check existing modules for reusable patterns first.
- storage.py is monolithic (10k+ lines) — add new tables/methods following existing naming conventions.
- cli.py has 75 commands — add new subcommands following the existing argparse pattern.

## Current State (as of 2026-07-23)

- **Funding:** 29 venue adapters (4 deactivated: BingX, Bitunix, BloFin, Phemex — see `funding/venues.py`), paper trader, Telegram alerts. Launchd requires reinstall.
- **Prediction:** 3 venues (Polymarket, Kalshi, Hyperliquid HIP-4), 7 route strategies, 0 executable routes (all `not_profitable`; `execution_atomic=False` by design — paper-only).
- **Analytics/Dune:** 5 curated views, 5 built-in queries, read-only SQL. Research universe NOT imported (Dune credits exhausted).
- **Research Validity:** Code-complete but data-empty. ML models gated on 100k rows / 100 events.
- **Identity Graph:** 0% coverage — no live token is capital-eligible.
- **Wallet backtest:** Diagnostic only (9.62% precision, no valid prediction claim).
