# Refactor Status

Last updated: 2026-08-05

Repository: `DaniilMakarov1/radar`

Latest accepted task: RF-000B

Accepted branch/SHA:
`review/synchronized-funding-v2-qwen-20260728-075432`
`43e0f87ac296d614c405ba4b5e65d978f12e3fbe`

Integration SHA: `43e0f87ac296d614c405ba4b5e65d978f12e3fbe`

Current task: RF-001

Architecture v1: FROZEN

Current branch: `task/rf-001-no-hold-cutover`

Exact final head: PENDING FINAL PUSH. A committed file cannot contain its own
commit hash; the final report records the true pushed HEAD after the status
commit.

PR: #1 https://github.com/DaniilMakarov1/radar/pull/1

CI run/status: LOCAL VALIDATION PASSED; GITHUB CI REQUIRED AFTER FINAL PUSH.

Qwen used: no.

## Current Result

- Production strategy is documented as `FUNDING_SETTLEMENT_CAPTURE`.
- `ONE_SETTLEMENT` and `MULTIPLE_SETTLEMENTS` are plan shapes of one
  `CapturePlan`.
- Production funding capture has no planned continuation after the first
  captured settlement boundary.
- Boundary crossing preserves reconciliation rows and leaves pending funding out
  of cash/equity until reconciliation/close processing.
- Mandatory exit waits for T+20 in normal conditions and can exit earlier on
  hard risk.
- Target production states are `ENTRY_SUBMITTED`, `OPEN`, `EXITING`, `CLOSED`,
  and `FAILED`; review/integrity is stored in config fields.
- Legacy non-flat states remain recovery-visible and normalize to `EXITING`.

## Blockers

- No known RF-001 code blocker after local validation.
- GitHub CI status is REQUIRED after final push.
- Strategy-alias code cutover is intentionally not part of RF-001.

## Local Evidence

- Active DB evidence: UNKNOWN; RF-001 did not inspect or mutate the active DB.
- Live service evidence: UNKNOWN; RF-001 did not start live services.
- Accepted checkout evidence: VERIFIED clean at
  `43e0f87ac296d614c405ba4b5e65d978f12e3fbe`; RF-001 did not edit
  `/Users/daniilmakarov/Desktop/radar`.
- CI evidence: REQUIRED after final push.
- Local tests:
  - Full suite:
    `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
    -> `746 passed`.
  - Targeted legacy/mismatch/mandatory/reconciliation/two-capture suite:
    `8 passed, 213 deselected`.
  - Targeted opportunity/legacy/mandatory/reconciliation/two-capture suite:
    `6 passed, 215 deselected`.
  - RF-001 reachability:
    `tests/test_rf001_no_hold_reachability.py` -> `6 passed`.
  - Compile:
    `python3 -m compileall -q smart_money_radar` -> passed.
  - Whitespace:
    `git diff --check` -> passed.
  - Two-capture smoke:
    `PYTHONPATH=. python3 scripts/run_synchronized_funding_two_capture_paper_mvp.py`
    -> two distinct captures opened, crossed, and closed mandatory.

## Scorecard Summary

| Area | Status |
| --- | --- |
| Exact NO HOLD production behavior | IMPLEMENTED |
| Legacy non-flat recovery visibility | IMPLEMENTED |
| Control docs | IMPLEMENTED |
| Strategy alias code cutover | DELETION TARGET |
| Dashboard read-only target | DOCUMENTED |
| Old product deletion | NOT STARTED |

## Deletion Progress

- Deleted old two-cycle smoke script.
- Added two-independent-capture smoke script.
- Removed production continuation helpers.
- Remaining deletion targets are listed in
  `docs/refactor/REFACTOR_PLAN.md`.

## Exact Search Classification

Expected remaining old literals after RF-001 follow-up:

- `HOLDING_NEXT_CYCLE`: LEGACY READ COMPATIBILITY in storage state sets and
  recovery tests; HISTORICAL FIXTURE in migration tests.
- `POST_SETTLEMENT_EVALUATION`: LEGACY READ COMPATIBILITY in storage state sets
  and recovery tests.
- `next_cycle`: DELETION TARGET where used only in historical docs or old helper
  names; FORBIDDEN BEHAVIOR if it creates a new production cycle.
- `hold_economics`: FORBIDDEN BEHAVIOR in production code; acceptable only as
  historical text if explicitly marked.
- `HoldHistoryReliability`: FORBIDDEN BEHAVIOR in production code; acceptable
  only as historical text if explicitly marked.

No remaining match may create Hold or a new cycle.

## Exact Next Task

RF-002: Strategy Alias Cutover to `FUNDING_SETTLEMENT_CAPTURE`.
