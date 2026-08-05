# Radar Architecture

Last updated: 2026-08-05

## Current Product

Radar is Funding-first and paper-only. The default paper runtime captures one
funding settlement boundary, then exits at the first normal-safe opportunity.
Production must not continue a position into another planned funding cycle.

Default strategy ownership:

- `single_settlement_hedged_capture_v1`: one favorable near settlement plus a
  hedge leg; hedge settlement alignment is not required.
- `synchronized_funding_capture_v2`: used only when both legs settle together
  within 1 second.
- Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any`
  remain research/experimental labels.

## Production Call Graph

```text
CLI funding-paper-trader
-> smart_money_radar.cli.funding_paper_trader_config
-> smart_money_radar.funding.trader.PaperBot
-> PaperBot.run_loop / run_iteration / run_hot_iteration / run_full_iteration
-> recovery, reconciliation, risk, and background scan scheduling
-> funding discovery or focused recheck
-> PaperBot.process_synchronized_entry_candidates
-> SynchronizedFundingRuntimeV2.consider_route
-> FundingSettlementPlanner.plan
-> entry_underwriting / initial_entry_economics
-> risk.entry_risk_gates
-> execution.simulate_marketable_ioc
-> funding_capture_positions, funding_capture_cycles, funding_paper_orders,
   paper_event_ledger
-> PaperBot.process_synchronized_open_positions
-> refresh_open_capture_route
-> mark_settlement_crossed / apply_settlement_boundary
-> process_pending_reconciliations
-> poll_synchronized_position_risk
-> mandatory_exit_after_boundary_decision
-> close_position
```

Full scans must not block open-position risk, focused hot checks, T-30 entry,
T-20 fill handling, settlement reconciliation, or mandatory exit.

## Lifecycle Contract

Production capture states are:

```text
ENTRY_SUBMITTED, OPEN, EXITING, CLOSED, FAILED
```

`DISCOVERED`, `REJECTED`, `ARMED`, `LEG_1_FILLED`, and `PARTIALLY_HEDGED` may
exist as pre-open evidence states. Review and accounting integrity must be stored
in structured config fields such as `integrity`, `reconciliation_state`,
`boundary_crossed_at`, and `normal_close_not_before`, not in extra terminal
states.

Boundary behavior:

- Crossing the captured settlement creates reconciliation obligations.
- Pending funding is excluded from cash, equity, win rate, and realized paper PnL.
- Normal close waits until T+20 after the captured boundary.
- Hard-risk close may happen earlier.
- A missing executable route after T+20 keeps the exposure actionable and
  retryable.
- A delayed or partial exit that crosses a later funding event is an incident and
  reconciliation case, not a planned continuation.
- A later planned boundary requires new discovery, underwriting, fills, and a new
  `capture_id`.

## Owner Map

| Area | Current owner | Notes |
| --- | --- | --- |
| Funding venue contracts | `smart_money_radar/funding/settlement_contracts.py`, adapter tests | Fail closed on unknown units, settlement timing, collateral, market type, or source identity. |
| Route planning | `smart_money_radar/funding/strategy_synchronized_funding.py` | Planner returns first-boundary capture plans only. |
| Paper runtime | `smart_money_radar/funding/trader.py`, `smart_money_radar/paper_bot/runtime_v2.py` | Default production loop, hot scheduling, entry, boundary, risk, mandatory exit, reconciliation. |
| Execution simulation | `smart_money_radar/paper_bot/execution.py` | Marketable IOC paper fills and reduce-only close simulation. |
| Accounting | `smart_money_radar/paper_bot/accounting.py`, `smart_money_radar/storage.py` | Ledger and paper account mutation; funding cash enters only through reconciliation/close rules. |
| Risk | `smart_money_radar/paper_bot/risk.py` | Hard-risk exits, stale data, basis deterioration, margin/liquidation checks. |
| UI/Telegram | `smart_money_radar/dashboard.py`, `smart_money_radar/web/index.html`, `smart_money_radar/paper_bot/telegram.py` | Display actionable watch/open/exit/reconciliation state; no hold action surface. |
| Operations | `scripts/start_funding_bot.sh`, `scripts/run_funding_bot_launchd.sh`, launchd helpers, smoke scripts | Paper-only process management. |

## Duplication Inventory

The repository still contains older product surface that should be preserved
until a separate deletion PR:

- Binance announcements, token registry, wallet buys, holding metrics, backtest,
  prediction, Dune/local analytics, and on-chain research paths in CLI, storage,
  dashboard, scripts, and docs.
- Two paper lifecycle families:
  `funding_capture_positions`/`funding_capture_cycles` for the production v2
  runtime and `funding_paper_positions` for legacy reports/profiles.
- Multiple discovery entrypoints:
  funding scan service, lightweight discovery, background full discovery,
  focused route refresh, dashboard scan endpoints, scanner paper revalidation,
  shadow monitor, and script smoke runs.
- Venue construction in profiles, venue registry helpers, CLI/script factories,
  adapter imports, and fallback client builders.
- Configuration in `PaperBotConfig`, profile files, CLI flags, environment
  variables, launchd scripts, start/status scripts, and smoke gates.
- Financial writers in ledger functions, direct account updates, close payloads,
  reconciliation effects, legacy paper-position accounting, account repair, and
  report exports.
- SQLite writers in schema init, capture upserts, cycle upserts, order writes,
  observations, event ledger, reserves, reconciled funding effects, route rows,
  legacy position open/close, dashboard jobs, and scan history.

## Safety Boundaries

- Paper-only: no live order placement and no live exchange trading mode.
- Active default funding adapters are RiseX and Hyperliquid unless explicitly
  promoted.
- Respect `DEACTIVATED_FUNDING_VENUES`.
- Scanner output is not a trade decision.
- Estimates are not cashflow.
- Route identity must include venue, canonical base, venue symbol,
  quote/collateral, market type, environment/profile, and settlement timestamps
  wherever it affects storage, dedupe, or risk.
