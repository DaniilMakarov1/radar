You are the primary implementation engineer for the Radar repository.

This is an implementation task, not a review-only task.

You have permission to read, create, modify, refactor, and delete any
repository file required to make the implementation correct.

You may redesign local implementation details, split modules, consolidate
modules, add migrations, add dependencies, update lockfiles, add tests,
and update documentation.

You must actually edit repository files.

A response that only lists findings, recommendations, or an audit is a
failed task.

CURRENT CHECKPOINT

Original base SHA:
    0d4103f3c69b892a21d3fe08577fa80cb6318b2a

Checkpoint SHA:
    ee609a4c32c565af443112475af2a6319addd13f

Review the complete implementation diff and the resulting repository state.

PRIMARY OBJECTIVE

Bring the existing synchronized_funding_capture_v2 implementation to a
review-ready state.

Do not reimplement the feature blindly from scratch.

First inspect what Codex already implemented, then:

1. identify correctness problems;
2. implement fixes;
3. complete missing functionality;
4. remove contradictory or obsolete behavior;
5. improve architecture where necessary;
6. add or correct regression tests;
7. run the complete test suite;
8. fix failures caused by the implementation;
9. update documentation to match actual behavior.

NON-NEGOTIABLE PRODUCT INVARIANTS

1. The system is paper-only.

2. No real exchange order may be sent.

3. The default strategy is:

       synchronized_funding_capture_v2

4. The strategy may use all active connected venues, but every venue must
   pass explicit capability checks.

5. A connected venue is not automatically paper-eligible.

6. Spread convergence is never counted as expected profit for the main
   funding strategy.

7. The initial entry window is T-35 through T-25 seconds.

8. Both legs must be filled no later than T-20 seconds.

9. The two legs are non-atomic and require explicit partial-fill and unwind
   handling.

10. After each settlement, the system may hold for another settlement only
    after new underwriting.

11. The next settlement timestamps must differ by no more than 1 second.

12. The next settlement must be between 300 and 14,400 seconds away.

13. A one-hour aligned next settlement may be held.

14. A four-hour aligned next settlement may be held.

15. A one-hour versus four-hour mismatch must close the position.

16. The maximum number of captured settlements is 4.

17. The maximum position age is 14,700 seconds.

18. Opening costs are sunk costs and may not be charged again in incremental
    hold economics.

19. Pending or provisional funding may not be included in confirmed PnL.

20. Each settlement cycle is reconciled separately and idempotently.

21. Reconciliation may continue while the position remains open for another
    cycle.

22. The 10% common price move is telemetry and a critical warning, not an
    automatic stop by itself.

23. Hard exits are driven by basis risk, executable PnL risk, margin risk,
    liquidation distance, stale data, quantity mismatch, venue failure,
    contract status, or schedule mismatch.

24. Orderbook snapshots from different venues must retain their real request
    and response timestamps.

25. Stale snapshots may not be treated as fresh.

26. Actual/paper PnL must be decomposable into:
       long price PnL
       short price PnL
       funding PnL by cycle
       open fees
       close fees
       emergency unwind costs

27. A paper position cannot become OPEN after only one leg is filled.

28. A paper position cannot become CLOSED until both legs are closed or the
    residual exposure is explicitly unwound.

29. Retention must be time-based, not "keep the latest 24 funding rows".

30. Active and unreconciled episode data may not be deleted by retention.

31. Live trading must remain absent or technically disabled.

QWEN WORKER REQUIREMENT

In addition to reviewing the funding implementation, implement or repair the
reusable Qwen worker infrastructure:

    scripts/run_qwen_worker.py
    .qwen/worker_system.md
    .agent_tasks/README.md
    tests/test_qwen_worker.py

The worker must be able to perform future code-writing tasks, not only audits.

TESTING

Run:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
    python3 -m compileall smart_money_radar

Also run the official lint and type-check commands if configured.

Do not hide test failures.

Do not change expected test values merely to make an incorrect
implementation pass.

SECURITY

Do not read:

    .env
    private keys
    seed phrases
    SSH keys
    system keychains
    real exchange credentials

Do not:

    commit
    push
    reset
    checkout
    clean
    rebase
    merge
    deploy
    run the live bot
    modify a real SQLite database
    send exchange orders

You may use:

    git status
    git diff
    git log
    git show

FINAL RESPONSE

Report:

1. files changed;
2. architectural changes;
3. correctness bugs fixed;
4. tests added;
5. tests executed;
6. test results;
7. remaining limitations;
8. whether any dependency was added;
9. whether any migration was added;
10. confirmation that you edited code rather than only auditing it.
