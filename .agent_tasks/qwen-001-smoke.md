TASK_ID:
QWEN-001-SMOKE

OBJECTIVE:
Return a JSON result proving the worker can run under the project policy.

CURRENT_BEHAVIOR:
NOT_APPLICABLE

REQUIRED_BEHAVIOR:
Do not edit files. Do not run shell commands. Return status completed.

ALLOWED_FILES:
NOT_APPLICABLE

FORBIDDEN_FILES:
All repository files.

EXACT_FIELDS:
NOT_APPLICABLE

EXACT_FORMULAS:
NOT_APPLICABLE

EXACT_ENUMS:
NOT_APPLICABLE

EXACT_DEFAULTS:
NOT_APPLICABLE

ACCEPTANCE_TESTS:
No tests. This is a smoke task.

DO_NOT:
Do not edit files. Do not run shell commands. Do not use tools.

EXPECTED_OUTPUT:
JSON with status completed, summary QWEN_WORKER_OK, empty changed_files,
empty tests_run, empty test_results, and empty blockers.
