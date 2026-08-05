# Refactor Status

Last updated: 2026-08-05

## RF-001

Status: draft PR open.

Branch: `task/rf-001-no-hold-cutover`

Base: `origin/integration` at
`43e0f87ac296d614c405ba4b5e65d978f12e3fbe`.

PR: https://github.com/DaniilMakarov1/radar/pull/1

Initial PR head: `bbac33e8722eafc2bcc7d4628fd278c7bfeecd6b`.

Qwen used: no.

## Current Result

- Production funding capture has no planned continuation after the first captured
  settlement boundary.
- Boundary crossing preserves reconciliation rows and leaves pending funding out
  of cash/equity until reconciliation/close processing.
- Mandatory exit waits for T+20 in normal conditions and can exit earlier on
  hard risk.
- Default production states are `ENTRY_SUBMITTED`, `OPEN`, `EXITING`, `CLOSED`,
  and `FAILED`; review/integrity is stored in config fields.
- Two independent captures use distinct capture ids; a later planned boundary
  requires fresh discovery, underwriting, fills, and storage identity.
- Old product surface and legacy database compatibility remain documented
  deletion targets.

## Validation Log

- Baseline focused synchronized suite before RF-001 edits:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_synchronized_funding_v2.py`
  -> `242 passed`.
- Updated focused suites run during implementation:
  - `tests/test_synchronized_funding_v2.py` -> `219 passed`.
  - `tests/test_funding_shadow_monitor.py` -> `100 passed`.
  - `tests/test_reconciliation_worker.py` -> `25 passed`.
  - `tests/test_funding_paper_trader.py` -> `111 passed`.
  - `tests/test_rf001_no_hold_reachability.py` was run with the updated
    synchronized and funding trader suites -> combined `333 passed`.
  - `tests/test_funding_shadow_monitor.py`,
    `tests/test_reconciliation_worker.py`, and
    `tests/test_fee_identity_migration.py` -> combined `133 passed`.
  - `scripts/run_synchronized_funding_paper_mvp.py` -> passed.
  - `scripts/run_synchronized_funding_two_capture_paper_mvp.py` -> passed.
- Full suite:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
  -> `741 passed`.
- Compile check:
  `python3 -m compileall -q smart_money_radar` -> passed.
- Whitespace check:
  `git diff --check` -> passed.
- Exact old-symbol search found only the migration compatibility test fixture
  listed below.

PR metadata recorded above. Later status changes should update this file in the
follow-up PR that changes the relevant code.

## Exact Search Classification

Current expected exact-search residue:

- A migration compatibility test contains the old all-caps next-cycle state
  literal to prove historical fee identity remains distinct.

No production code path should contain old continuation planner functions,
planner exits beyond the first boundary, or removed hold configuration knobs.
