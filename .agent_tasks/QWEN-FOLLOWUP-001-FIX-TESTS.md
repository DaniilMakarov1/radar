You are the primary implementation engineer for the Radar repository.

This is a follow-up implementation task. You must edit repository files.

Do not commit, push, reset, checkout, clean, rebase, merge, deploy, run live
trading, read .env, read secrets, or modify real SQLite databases.

The previous qwen3.7-plus writing pass changed files, but focused tests failed:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_qwen_worker.py tests/test_synchronized_funding_v2.py tests/test_funding_retention.py -q --tb=short

4 failed, 51 passed

FAILED tests/test_qwen_worker.py::test_worker_script_build_prompt_includes_system_and_schema
AttributeError: <module 'run_qwen_worker' ...> does not have the attribute 'REPO_ROOT'

FAILED tests/test_synchronized_funding_v2.py::test_close_decision_enforces_max_settlements
assert 'wait' == 'close'

FAILED tests/test_synchronized_funding_v2.py::test_close_decision_enforces_max_position_age
assert 'wait' == 'close'

FAILED tests/test_funding_retention.py::FundingRetentionTest::test_prunes_funding_rate_history_to_latest_rows_per_market
TypeError: 'NoneType' object is not iterable
```

Fix the implementation and tests correctly.

Important product invariants:

1. Default strategy remains synchronized_funding_capture_v2.
2. Entry window remains T-35 through T-25.
3. Fill deadline remains T-20.
4. The next settlement timestamps must differ by no more than 1 second.
5. A one-hour aligned next settlement may be held.
6. A four-hour aligned next settlement may be held.
7. A one-hour versus four-hour mismatch must close the position.
8. Maximum captured settlements: 4.
9. Maximum position age: 14,700 seconds.
10. Opening costs are sunk and must not be charged again in hold economics.
11. Pending funding may not be included in confirmed PnL.
12. Reconciliation must be idempotent.
13. The 10% common price move is telemetry, not an automatic stop.
14. Retention must be time-based and must not delete active/unreconciled episode data.

Specific guidance:

- Do not weaken tests to hide a bug.
- If max settlement/age close checks should happen only after settlement is
  reached, adjust the tests to use the correct post-settlement timing and route
  setup, or adjust implementation if the lifecycle ordering is wrong.
- For retention, avoid breaking existing sync-state expectations. If you add
  time-based pruning, preserve active/unreconciled funding history and preserve
  expected sync-state refresh behavior.
- For the Qwen worker test, load the module correctly or improve the worker
  interface so the test validates real behavior.

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_qwen_worker.py tests/test_synchronized_funding_v2.py tests/test_funding_retention.py -q --tb=short
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m compileall smart_money_radar
```

Report files changed, tests run, and results.
