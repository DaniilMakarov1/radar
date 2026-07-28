TASK_ID:
QWEN-002-V2-READONLY-AUDIT

OBJECTIVE:
Review the current repository diff for contradictions with synchronized_funding_capture_v2.

CURRENT_BEHAVIOR:
Codex implemented the v2 contract, tests, docs, and storage helpers.

REQUIRED_BEHAVIOR:
Read the current diff and report only concrete blockers or high-risk inconsistencies.
Do not edit files.

ALLOWED_FILES:
NOT_APPLICABLE

FORBIDDEN_FILES:
All repository files.

EXACT_FIELDS:
NOT_APPLICABLE

EXACT_FORMULAS:
Check that these intended formulas are not contradicted:
- spread convergence is not expected profit for synchronized_funding_capture_v2;
- long funding PnL = -quantity * mark * funding_rate;
- short funding PnL = quantity * mark * funding_rate;
- initial expected net = conservative funding gross minus modeled costs/reserves;
- current executable PnL excludes pending future funding.

EXACT_ENUMS:
NOT_APPLICABLE

EXACT_DEFAULTS:
Check these defaults conceptually:
- default strategy_set is synchronized_funding_capture only;
- entry window is 25-35 seconds;
- target entry lead is 30 seconds;
- max entry snapshot age is 2 seconds;
- common 10% price move is telemetry, not automatic close;
- live trading remains disabled.

ACCEPTANCE_TESTS:
Do not run tests unless you can do so without modifying files.

DO_NOT:
Do not edit files.
Do not run git commit, push, reset, checkout, clean, rebase, merge, or stash.
Do not read secrets.
Do not enable live trading.
Do not suggest weakening risk gates without a specific reason.

EXPECTED_OUTPUT:
JSON matching .qwen/worker_result.schema.json. Put findings in blockers if any;
otherwise status completed and summary QWEN_AUDIT_OK.
