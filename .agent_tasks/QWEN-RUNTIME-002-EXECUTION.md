IMPLEMENT NOW. EDIT PRODUCTION FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus acting as implementation engineer. Codex/GPT is the architect/reviewer.

Scope: non-atomic paper execution, v2 accounting, ledger, and settlement reconciliation.

Hard constraints:
- Edit production runtime code, not only tests/docs/helpers.
- Do not run live bots, dashboards, watchers, migrations against the real DB, or real account/API endpoints.
- Use only temporary SQLite/fake data for tests.
- Do not read .env, .env.*, secrets, real DB files, .git/**, or .agent_runs/**.
- Do not commit/push.
- New helpers count only if PaperBot/runtime actually calls them.

Required runtime changes:
1. Implement and integrate v2 execution states:
   DISCOVERED, REJECTED, ARMED, ENTRY_SUBMITTED, LEG_1_FILLED,
   PARTIALLY_HEDGED, OPEN, SETTLEMENT_CROSSED, POST_SETTLEMENT_EVALUATION,
   HOLDING_NEXT_CYCLE, EXIT_SCHEDULED, EXIT_SUBMITTED, PARTIALLY_CLOSED,
   EMERGENCY_UNWIND, CLOSED_PENDING_RECONCILIATION, RECONCILED,
   UNRECONCILED, FAILED.
2. ENTRY_SUBMITTED -> OPEN is forbidden unless both simulated fills exist.
3. EXIT_SUBMITTED -> CLOSED_PENDING_RECONCILIATION is forbidden unless both legs are closed or explicit residual unwind is recorded.
4. Add simulate_marketable_ioc(levels, side, target_quantity, depth_haircut_fraction):
   - execution haircut is 0.40;
   - executable quantity per level = visible quantity * haircut;
   - return filled_quantity, average_fill_price, notional, fee, unfilled_quantity.
5. Entry fully filled only if both legs fill_ratio >= 0.999 and quantity mismatch <= 0.001. Partial fills become PARTIALLY_HEDGED, immediately unwind any exposure, and never become OPEN.
6. Simulate latency from venue p95 RTT of last 20 requests:
   - fallback 750ms;
   - block venue if p95 > 3000ms;
   - simulated latency clamp 750..3000ms;
   - record decision/submitted/ack/fill times for orders.
7. Enforce T-20 deadline: both legs filled no later than settlement_at - 20 seconds. Otherwise fail/unwind/no funding captured.
8. Normal exit is reduce-only paper MARKETABLE_IOC with 0.40 haircut. If depth insufficient, fill available part and execute residual with 100 bps adverse penalty, recording residual quantity and emergency_unwind_cost through PARTIALLY_CLOSED -> EMERGENCY_UNWIND.
9. Use v2 paper accounting:
   - long price PnL = q * (long_exit_fill - long_entry_fill)
   - short price PnL = q * (short_entry_fill - short_exit_fill)
   - paper_price_pnl = long + short
   - paper_net_if_exit_now = paper_price_pnl + confirmed_funding_pnl - paper_open_fees - current/estimated close fees - emergency_unwind_cost
   - do not add separate basis_pnl on top of price PnL.
10. Add idempotent paper ledger with event_key. Minimum keys:
   - order_fee:<order_id>
   - price_pnl:<position_id>:close
   - funding:<position_id>:<venue>:<scheduled_funding_at>
11. Entry reserves isolated collateral but does not spend it. Expected funding is never a cashflow.
12. In synchronized runtime remove phantom funding:
   - no settlement_rate_or_entry;
   - no entry_estimate_fallback;
   - no use_entry_estimate_for_missing=True;
   - missing public history means funding_rate NULL, funding_pnl NULL, reconciliation PENDING, no balance/equity/win-rate impact.
13. On settlement crossing create one PENDING reconciliation row per leg. Reconciliation polls/matches venue/symbol/scheduled_funding_at with +/-120s tolerance:
   - rate+mark => RATE_AND_MARK_RECONCILED and funding_pnl;
   - rate without mark => PUBLIC_RATE_CONFIRMED, funding_pnl NULL, no balance change;
   - timeout => UNRECONCILED.
   Hold/close decision does not wait for reconciliation.

Tests required:
- Entry window boundaries T-36 no, T-35 yes, T-25 yes, T-24 no.
- Late fill after T-20 never opens and unwinds.
- One leg filled never OPEN.
- Partial fill long 100%, short 90% => PARTIALLY_HEDGED/unwind recorded.
- Quantity mismatch 0.100% allowed; 0.101% unwinds.
- Pre-settlement close has no funding PnL and no funding ledger entry.
- After settlement with missing history => PENDING, funding_pnl NULL, balance unchanged, paper_net_if_exit excludes funding.
- Reconciliation idempotency: duplicate public event changes balance once.
- Rate without mark: no balance change.
- New v2 storage lifecycle creates capture position, cycle, observations, two entry orders, two exit orders, reconciliation rows.
- Monkeypatch open_funding_paper_position to raise; v2 lifecycle must pass without calling it.

Acceptance:
- Production code diff is non-empty and includes runtime integration.
- Focused tests pass:
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_synchronized_funding_v2.py tests/test_funding_paper_trader.py -q
