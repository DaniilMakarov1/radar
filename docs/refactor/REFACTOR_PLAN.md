# Refactor Plan

Last updated: 2026-08-05

## Goal

Move Radar toward the Architecture Freeze v1 target while preserving RF-001
safety: Funding-first, paper-only, exact NO HOLD, no destructive DB migration,
and no deletion of old product surface without proof.

## Evidence-Backed Refactor Scorecard

Counts are evidence-backed only. Unknown counts are marked `UNKNOWN`.

| Area | Current score | Evidence | Target |
| --- | --- | --- | --- |
| Strategy contract | PARTIAL | RF-001 code enforces no planned continuation; compatibility names remain. | One `FUNDING_SETTLEMENT_CAPTURE` name in production. |
| Lifecycle states | PARTIAL | Position writers use `ENTRY_SUBMITTED`, `OPEN`, `EXITING`, `CLOSED`, `FAILED`; legacy reads remain. | Legacy states deleted after migration rehearsal. |
| Discovery ownership | WEAK | Broad/focused/dashboard/script entrypoints still duplicate ownership. | `DiscoveryEngine(BROAD/FOCUSED)`. |
| Venue scope | WEAK | Profile helpers and direct fallback factories coexist. | `VenueRegistry` + `ResolvedVenueScope`. |
| Ledger ownership | PARTIAL | Paper event ledger is authoritative for cash checks, but legacy account writers remain. | Causal append-only `Ledger`. |
| Status/UI ownership | WEAK | Dashboard still owns old product routes and scan jobs. | Read-only `StatusSnapshot`. |
| Deletion proof | UNKNOWN | Full production call-site counts not audited for every old module. | Counts known before deletion. |

## Deletion Ledger

| Old owner/path | Production call sites | Target owner | Blocker | Target Task ID | Deletion proof |
| --- | --- | --- | --- | --- | --- |
| Compatibility strategy names in planner/runtime docs and payloads | UNKNOWN | `CapturePlanner` using `FUNDING_SETTLEMENT_CAPTURE` | Alias migration and downstream report compatibility | RF-002 | Source search plus report fixture update. |
| `position_hold_decision` / `hold_decision` legacy payload fields | Legacy close helper and tests | `CaptureLifecycle` exit revalidation | Legacy report compatibility | RF-003 | No production call site and tests renamed. |
| `SQLiteStore.accrue_funding_paper_settlement` | UNKNOWN legacy reports/tests | `SettlementLifecycle` + `Ledger` | Need report/export audit | RF-004 | No caller plus DB rehearsal. |
| `funding_paper_positions` open/close lifecycle | Legacy paper profiles/reports | `funding_capture_positions` | Decide report-only compatibility boundary | RF-005 | No production writer and report fixtures pass. |
| Dashboard scan endpoints/jobs | Dashboard routes and UI actions | Read-only `StatusSnapshot` | Observability replacement | RF-006 | Dashboard has no scan mutation endpoint. |
| Direct venue fallback factories | CLI/scripts/runtime helper paths | `VenueRegistry` | Runtime identity cutover | RF-007 | One composition root constructs clients. |
| Launchd/screen/nohup script spread | Multiple scripts | One paper operations command | Operator runbook | RF-008 | Script inventory and smoke proof. |
| Old prediction/token/wallet/Dune/backtest product surface | UNKNOWN | Archived docs or deleted modules | Data retention decision | RF-009 | Import graph proves Funding UI unaffected. |

## Fallback / Compatibility Inventory

- Legacy non-flat states are read-compatible and block entry:
  `HOLDING_NEXT_CYCLE`, `POST_SETTLEMENT_EVALUATION`,
  `SETTLEMENT_CROSSED`, `EXIT_SCHEDULED`, `EXIT_SUBMITTED`,
  `PARTIALLY_CLOSED`, `EMERGENCY_UNWIND`, `SETTLEMENT_PLAN_MISMATCH`.
- Compatibility/version metadata names remain until RF-002:
  `single_settlement_hedged_capture_v1`, `synchronized_funding_capture_v2`.
- Legacy paper-position tables and report paths remain until RF-005.
- Dashboard-triggered scans are deletion targets for RF-006, not candidates to
  move onto the shared discovery interface.
- Unknown counts must stay `UNKNOWN` until measured by source search and runtime
  call-graph tests.

## Safety Preservation Matrix

| Safety rule | Current preservation | Next proof |
| --- | --- | --- |
| Paper-only | No live order path changed. | CI source search for live execution writes. |
| Exact NO HOLD | Production continuation helpers removed; legacy states normalize to `EXITING`. | RF-001 reachability tests. |
| Pending funding excluded | Boundary creates obligations; funding ledger waits for reconciliation. | Reconciliation tests and MVP smoke. |
| Normal close T+20 | Mandatory exit records `normal_close_not_before`. | Targeted T+20 test. |
| Unknown exposure non-flat | Legacy and unknown non-flat states remain open-exposure. | Legacy restart tests. |
| One SQLite | No active DB mutation or migration in RF-001. | Backup/rehearsal before schema work. |

## DB Backup, Rehearsal, and Cutover Rules

- Never run destructive migration on the active DB during review work.
- Before schema deletion or data rewrite: copy the DB, run migration on the copy,
  run integrity checks, and compare row counts and ledger/account invariants.
- Cutover tasks must include rollback instructions and a read-only verification
  command.
- Compatibility reads may be added before deletion; compatibility writes require
  explicit task approval.

## CI Evolution

- Keep full pytest, compileall, diff check, single-capture smoke, two-capture
  smoke, and RF-001 reachability tests.
- Add source-search CI for forbidden production continuation symbols.
- Add dashboard read-only tests before RF-006.
- Add one composition-root smoke before RF-007.

## Deployment and Runtime Identity Rules

- Runtime identity must include branch/commit, environment, profile, venue scope,
  paper/live mode, SQLite path, strategy name, and plan shape.
- Launch scripts must print the resolved identity before starting.
- Multiple runtimes must not point at the same SQLite writer without an explicit
  lock/owner record.

## Ordered Small Task IDs

| Task ID | Scope |
| --- | --- |
| RF-002 | Strategy alias code cutover to `FUNDING_SETTLEMENT_CAPTURE`. |
| RF-003 | Rename/isolate legacy hold-named helper and payload fields. |
| RF-004 | Delete legacy settlement accrual writer after report audit. |
| RF-005 | Split or retire legacy `funding_paper_positions` lifecycle. |
| RF-006 | Observability cutover: dashboard read-only and delete scan jobs. |
| RF-007 | One composition root with `VenueRegistry` and `ResolvedVenueScope`. |
| RF-008 | Consolidate operations scripts and runtime identity printing. |
| RF-009 | Archive/delete old non-funding product surface. |

## Exact Recommended Next Task

RF-002: Strategy Alias Cutover. Replace compatibility strategy names in code
paths with `FUNDING_SETTLEMENT_CAPTURE`, keep old names only in migration/report
compatibility tests, and prove `ONE_SETTLEMENT` / `MULTIPLE_SETTLEMENTS` are
plan shapes rather than strategy owners.
