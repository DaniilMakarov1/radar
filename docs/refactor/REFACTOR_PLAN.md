# Refactor Plan

Last updated: 2026-08-05

## Goal

Reduce Radar to a Funding-first, paper-only architecture without deleting
historical data or old product modules inside RF-001. This plan records what
owns current production behavior and what should be removed in later, smaller
PRs.

## RF-001 Cutover

Done in this task:

- Remove production continuation-cycle planning from the funding capture runtime.
- Remove hold-oriented config and CLI/profile knobs from the default paper path.
- Keep boundary crossing as `OPEN` while mandatory exit is pending.
- Use `EXITING` for in-progress or partial exits and `CLOSED` only after flat.
- Keep review and reconciliation integrity in config fields instead of state
  names.
- Replace the two-cycle smoke with a two-independent-capture smoke.
- Add reachability tests that fail if old continuation symbols or hold action
  surfaces return.
- Document the current call graph and duplicate owners.

## Deletion Targets

Phase 1: Lifecycle names and legacy helpers

- Rename legacy `position_hold_decision` and `hold_decision` payload fields to
  exit/revalidation terminology, or isolate them behind a legacy-only module.
- Remove `SQLiteStore.accrue_funding_paper_settlement` after legacy reports no
  longer rely on it.
- Remove old v2 compatibility state readers after migration windows are closed.

Phase 2: Paper lifecycle split

- Decide whether `funding_paper_positions` remains report-only.
- Move legacy paper open/close accounting behind a compatibility boundary.
- Make `funding_capture_positions` the only production open-exposure source.

Phase 3: Discovery and venue ownership

- Collapse discovery ownership into one scheduler contract.
- Centralize venue client construction and remove fallback factories that can
  silently change the active venue set.
- Move dashboard-triggered scans and smoke scripts onto the same discovery
  interface.

Phase 4: Old product surface

- Archive or delete prediction, token/wallet, Dune/local analytics, old backtest,
  and unrelated dashboard routes.
- Keep data migrations explicit; do not delete SQLite tables without a separate
  migration and backup plan.

Phase 5: Operations

- Consolidate launchd, screen/nohup, start, stop, status, smoke, and launch-gate
  scripts into one documented paper-only operations path.

## Acceptance Checks

Every refactor PR should include:

- focused tests for the edited owner;
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`;
- `python3 -m compileall -q smart_money_radar`;
- `git diff --check`;
- exact source search for old continuation symbols;
- confirmation that no live bot, live orders, DB migration, merge, or force push
  occurred.
