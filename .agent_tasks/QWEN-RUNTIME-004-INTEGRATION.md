IMPLEMENT NOW. EDIT PRODUCTION FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus acting as implementation engineer. Codex/GPT is the architect/reviewer.

Scope: end-to-end integration tests, call graph proof, documentation alignment.

Hard constraints:
- Edit production runtime code if call graph is incomplete. Do not only edit tests/docs.
- Do not run live bots, dashboards, watchers, migrations against the real DB, or real account/API endpoints.
- Use only temporary SQLite/fake adapters/fake clock.
- Do not read .env, .env.*, secrets, real DB files, .git/**, or .agent_runs/**.
- Do not commit/push.
- New helpers count only if PaperBot/runtime actually calls them.

Required runtime/test changes:
1. Add injectable Clock:
   - now()
   - monotonic()
   - sleep()
   Production uses SystemClock. Tests use FakeClock.
2. Add end-to-end PaperBot tests with fake venue adapters and temporary SQLite proving synchronized runtime calls:
   - strategy_synchronized_funding
   - execution
   - accounting
   - cycle_manager
   - risk
   - settlement
3. Required E2E tests:
   - monkeypatch legacy build_strategy_evaluation to raise; v2 lifecycle still works;
   - monkeypatch legacy open_funding_paper_position to raise; v2 lifecycle still works;
   - zero funding + positive spread does not enter;
   - no phantom funding before settlement;
   - no phantom funding after settlement when public history missing;
   - pending reconciliation excluded from reconciled profitability metrics;
   - paper PnL does not double count basis;
   - fees, price PnL, funding ledger keys are idempotent;
   - v2 lifecycle creates funding_capture_position, funding_capture_cycle, observations, two entry orders, two exit orders, reconciliation rows.
4. Static acceptance checks must pass or be isolated to explicit legacy modules only:
   - no use_entry_estimate_for_missing=True in synchronized runtime;
   - no settlement_rate_or_entry in trader.py or synchronized runtime;
   - actual_net_pnl/actual_funding_pnl/actual_execution_cost only in legacy/report compatibility;
   - spread_convergence only in experimental spread strategy;
   - basis_stop_loss_bps not used by synchronized runtime.
5. Documentation after runtime integration:
   - README and MODEL_INSTRUCTIONS must reflect actual call graph, not helper files;
   - scanner status before observations = watch;
   - paper_candidate only after focused underwriting;
   - spread convergence = 0;
   - entry requires two fills and T-20 deadline;
   - funding estimate is not cashflow;
   - hold independent from reconciliation;
   - exact next timestamps are used, not interval equality;
   - paper PnL reconciled only after rate + settlement mark;
   - all active venues scanned;
   - only fail-closed compatible venues paper-eligible;
   - live trading absent.

Acceptance:
- Production code diff is non-empty if previous passes did not fully integrate runtime.
- Focused tests pass:
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_synchronized_funding_v2.py tests/test_funding_paper_trader.py tests/test_funding_retention.py -q
- Full tests and compileall should pass if feasible:
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
  python3 -m compileall smart_money_radar scripts/run_qwen_worker.py
