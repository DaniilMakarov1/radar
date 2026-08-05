# Radar Architecture

Last updated: 2026-08-05

## Architecture Freeze v1

Architecture v1 is FROZEN for RF-001. The production strategy is:

```text
FUNDING_SETTLEMENT_CAPTURE
```

Plan shapes are:

```text
ONE_SETTLEMENT
MULTIPLE_SETTLEMENTS
```

These are shapes of one `CapturePlan`, not separate production strategies.
`single_settlement_hedged_capture_v1` and `synchronized_funding_capture_v2` are
temporary compatibility/version metadata only; they are deletion targets after
the alias cutover is reviewed separately.

Evidence allowed to reopen this freeze:

- a failing recovery or accounting regression test;
- a reproducible paper-runtime incident with exact DB rows and timestamps;
- a venue contract proof that changes funding units, settlement timing, market
  type, collateral, or public/final rate source;
- a performance trace proving the hot path blocks T-30 entry, T-20 fill handling,
  open-position risk, mandatory exit, or reconciliation;
- a product decision from Daniil that explicitly changes the paper-only scope.

## Product and Safety Scope

- Funding-first and paper-only.
- No live trading, no live order placement, no private account mutation.
- One SQLite store is the local source of truth.
- Active default venue scope remains constrained by `DEACTIVATED_FUNDING_VENUES`
  and explicit profile readiness.
- Estimates are never cashflow.
- Scanner output is never a trade decision.

Exact NO HOLD:

- production must not plan continuation into another funding cycle;
- after the first captured settlement boundary, preserve reconciliation
  obligations and exit at the first normal-safe opportunity;
- normal close waits until T+20 after the captured boundary unless hard risk
  requires earlier exit;
- a delayed or partial exit that crosses another funding event is an
  incident/reconciliation case, not planned strategy behavior.

## Current Production Call Graph

```text
CLI funding-paper-trader
-> smart_money_radar.cli.funding_paper_trader_config
-> smart_money_radar.funding.trader.PaperBot
-> PaperBot.run_loop / run_iteration / run_hot_iteration / run_full_iteration
-> runtime recovery / reconciliation / risk / background scan scheduling
-> broad discovery or focused recheck
-> PaperBot.process_synchronized_entry_candidates
-> SynchronizedFundingRuntimeV2.consider_route
-> FundingSettlementPlanner.plan
-> entry_underwriting / initial_entry_economics
-> risk.entry_risk_gates
-> execution.simulate_marketable_ioc
-> funding_capture_positions / funding_capture_cycles /
   funding_paper_orders / paper_event_ledger
-> PaperBot.process_synchronized_open_positions
-> refresh_open_capture_route
-> mark_settlement_crossed / apply_settlement_boundary
-> process_pending_reconciliations
-> poll_synchronized_position_risk
-> mandatory_exit_after_boundary_decision
-> close_position
```

## Target Production Call Graph

```text
RuntimeSpec
-> CompositionRoot
-> VenueRegistry.resolve(scope) -> ResolvedVenueScope
-> PaperRuntime
-> DiscoveryEngine(BROAD)
-> DiscoveryEngine(FOCUSED)
-> CapturePlanner -> CapturePlan
-> CaptureLifecycle
-> fills/exposure
-> SettlementLifecycle -> SettlementAssessment
-> causal append-only Ledger
-> StatusSnapshot
```

Hierarchy:

```text
RuntimeSpec -> CapturePlan -> fills/exposure ->
SettlementAssessment -> Ledger -> StatusSnapshot
```

## RuntimeSpec

`RuntimeSpec` owns immutable runtime identity:

- environment/profile;
- venue allow/deactivate set;
- target notional and paper account scope;
- strategy name `FUNDING_SETTLEMENT_CAPTURE`;
- plan-shape allow set `ONE_SETTLEMENT` / `MULTIPLE_SETTLEMENTS`;
- paper-only safety mode;
- code/version metadata.

## PaperRuntime

`PaperRuntime` owns scheduling and hot-path priority:

- open exposure and recovery first;
- hard-risk checks before normal discovery;
- T-30 entry and T-20 fill handling;
- settlement boundary and reconciliation;
- mandatory exit after T+20;
- background discovery only when no open exposure and no hot route is blocked.

## One Composition Root

Target architecture has one composition root for production paper runtime. It
constructs `RuntimeSpec`, `VenueRegistry`, `DiscoveryEngine`, `CapturePlanner`,
`CaptureLifecycle`, `SettlementLifecycle`, `Ledger`, and `StatusSnapshot`.

CLI, launchd scripts, smoke scripts, and tests should call the composition root
instead of assembling partially different runtimes.

## VenueRegistry and ResolvedVenueScope

`VenueRegistry` owns venue capability and identity. `ResolvedVenueScope` is the
only runtime input after profile/deactivation resolution. It contains resolved
clients, environment, supported market types, collateral policy, contract unit
proof, fee model, and funding settlement semantics.

## DiscoveryEngine

`DiscoveryEngine` has two modes:

- `BROAD`: cheap funding and catalog sweep. It must not block open exposure,
  focused routes, or mandatory exit.
- `FOCUSED`: paired route refresh with executable books and timestamps for entry,
  risk, boundary handling, and close.

dashboard-triggered scans are deletion targets during the observability cutover;
they must not be moved onto the shared discovery interface.

## CapturePlanner

`CapturePlanner` creates one `CapturePlan` for
`FUNDING_SETTLEMENT_CAPTURE`. The plan shape is `ONE_SETTLEMENT` or
`MULTIPLE_SETTLEMENTS`. The plan captures exactly one settlement boundary and
does not own continuation cycles.

## CaptureLifecycle

Target capture states:

```text
ENTRY_SUBMITTED / OPEN / EXITING / CLOSED / FAILED
```

Pre-open evidence states such as `DISCOVERED`, `REJECTED`, `ARMED`,
`LEG_1_FILLED`, and `PARTIALLY_HEDGED` may exist as non-production or
pre-open evidence. Review state belongs in config fields.

## SettlementLifecycle

`SettlementLifecycle` owns boundary obligations and settlement assessment.

`SettlementAssessment` value:

```text
CONFIRMED_ZERO / CONFIRMED_NONZERO / UNKNOWN
```

`SettlementAssessment` cause:

```text
PLANNED / INCIDENT
```

Pending or unknown funding is excluded from cash, equity, win rate, and realized
PnL. Confirmed zero is a valid settlement outcome, not missing data.

## Ledger

The target ledger is causal and append-only. Every cash-affecting event has a
venue. Funding cashflow can enter only through reconciled settlement evidence.
Price PnL is long exit plus short exit. Do not add a separate basis PnL on top.

## StatusSnapshot

`StatusSnapshot` reads runtime state and ledger-derived balances. It is bounded,
actionable, and non-authoritative. It must not mutate runtime state.

Dashboard read-only: the dashboard may display state and request reports, but it
must not be a production scan or lifecycle writer in the target architecture.

## Current-Owner / Target-Owner Matrix

| Area | Current owner | Target owner |
| --- | --- | --- |
| Runtime config | `PaperBotConfig`, CLI flags, scripts, profiles | `RuntimeSpec` |
| Venue construction | profile helpers, direct adapter factories, CLI/script fallbacks | `VenueRegistry` + `ResolvedVenueScope` |
| Broad discovery | funding service, lightweight discovery, dashboard scan jobs, smoke scripts | `DiscoveryEngine(BROAD)` |
| Focused refresh | PaperBot targeted refresh and focused selection | `DiscoveryEngine(FOCUSED)` |
| Capture planning | `FundingSettlementPlanner` plus compatibility names | `CapturePlanner` |
| Entry/open/exit | `PaperBot` + `SynchronizedFundingRuntimeV2` | `CaptureLifecycle` |
| Boundary/reconciliation | storage methods + runtime reconciliation worker | `SettlementLifecycle` |
| Cash/accounting | `paper_event_ledger`, account mutation helpers, repair jobs | causal append-only `Ledger` |
| UI/Telegram/status | dashboard, Telegram helpers, report/export queries | read-only `StatusSnapshot` |
| SQLite writes | multiple methods in `SQLiteStore` | one writer boundary over one SQLite |

## Recovery and Legacy-State Rules

Current non-flat position states:

```text
OPEN / EXITING
```

Legacy non-flat position states remain read-compatible until deletion:

```text
HOLDING_NEXT_CYCLE / POST_SETTLEMENT_EVALUATION / SETTLEMENT_CROSSED /
EXIT_SCHEDULED / EXIT_SUBMITTED / PARTIALLY_CLOSED / EMERGENCY_UNWIND /
SETTLEMENT_PLAN_MISMATCH
```

Recovery rules:

- all current and legacy non-flat states block new entry;
- legacy non-flat rows are normalized to `EXITING` with
  `original_legacy_state` and recovery evidence in config;
- recovery must use the first already crossed boundary, not a future cycle
  timestamp;
- no recovery path may create a new cycle or call continuation underwriting;
- unknown exposure is non-flat until both legs are proven closed;
- `CLOSED` and `FAILED` require evidence that both legs are flat.

## One Writer / One SQLite

SQLite remains the single local durable store. The target design has one writer
boundary for lifecycle, ledger, and status snapshots. Until deletion, legacy
methods stay compatibility-only and must be documented as deletion targets.

## Dependency Direction

Allowed dependency direction:

```text
RuntimeSpec
-> VenueRegistry
-> DiscoveryEngine
-> CapturePlanner
-> CaptureLifecycle
-> SettlementLifecycle
-> Ledger
-> StatusSnapshot
```

Lower layers must not call UI, Telegram, dashboard scan jobs, or launch scripts.

## Cutover With Deletion

RF-001 freezes behavior and preserves historical rows. Later tasks should delete
old owners only after tests prove no production call site remains and a DB backup
or rehearsal plan exists.

## Non-Goals

- No live trading.
- No destructive schema migration.
- No deletion of old product modules inside RF-001.
- No strategy-alias code cutover inside this fix.
- No dashboard-triggered production scan rewrite.
- No expansion to new venues without an explicit venue contract task.
