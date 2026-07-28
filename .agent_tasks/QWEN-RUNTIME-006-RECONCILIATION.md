IMPLEMENT NOW. EDIT PRODUCTION RUNTIME FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus, implementation engineer for Radar.

This is runtime-completion pass 006. Change production runtime code. Do not
return review-only notes. Do not run real bot/dashboard/live execution, do not
read secrets, and do not touch real SQLite databases. Tests must use FakeClock,
fake providers/adapters, and temporary SQLite.

Current confirmed defects after pass 005:

- `runtime_v2.close_position` still passes `confirmed_funding_pnl=0.0`.
- There is no `FundingSettlementDataProvider`.
- There is no `process_pending_reconciliations(now)` worker.
- `PaperBot` does not call a reconciliation worker on cadence.
- PENDING rows can be created at settlement crossing, but are not actually
  processed into `PUBLIC_RATE_CONFIRMED`, `RATE_AND_MARK_RECONCILED`, or
  `UNRECONCILED`.
- Closed v2 positions are not finalized to `RECONCILED`/`UNRECONCILED` from
  captured cycle statuses.
- `paper_net_if_exit_now` does not include already confirmed funding ledger
  entries.

Implement:

1. FundingSettlementDataProvider

Add an injectable interface/class, preferably in `smart_money_radar/paper_bot/settlement.py`
or `runtime_v2.py` if cleaner:

```
get_public_funding_event(venue, symbol, scheduled_funding_at, tolerance_seconds)
get_nearest_mark_snapshot(venue, symbol, scheduled_funding_at, max_distance_seconds)
```

Production provider must use existing stored/public funding-history/market
snapshot data only. Do not add real API calls. Tests use fake provider.

2. Reconciliation Worker

Add runtime method:

```
process_pending_reconciliations(now)
```

For each PENDING row:

- public event missing and age <= 600s: remain PENDING;
- public event missing and age > 600s: set UNRECONCILED;
- rate found but nearest mark missing: set PUBLIC_RATE_CONFIRMED, `funding_pnl = NULL`, no ledger cashflow;
- rate + mark found: set RATE_AND_MARK_RECONCILED, calculate funding:

LONG:

```
-quantity * settlement_mark * confirmed_rate
```

SHORT:

```
quantity * settlement_mark * confirmed_rate
```

Ledger key:

```
funding:<position_id>:<venue>:<scheduled_funding_at>
```

Duplicate processing must not change cash twice.

3. Cycle and Position Finalization

- Cycle becomes `RECONCILED` only when both legs are `RATE_AND_MARK_RECONCILED`.
- Cycle reconciled funding = sum both legs.
- Closed position becomes `RECONCILED` only when all captured cycles reconciled.
- Then calculate:

```
paper_net_pnl_reconciled =
    total price PnL
    + total reconciled funding
    - all order fees
    - all emergency costs
```

- If any captured cycle becomes `UNRECONCILED` and position is already closed:
  position state = `UNRECONCILED`, `paper_net_pnl_reconciled = NULL`.
- Position may remain OPEN for cycle 2 while cycle 1 reconciliation is pending.

4. Current/Close PnL Uses Confirmed Funding

Remove hardcoded `confirmed_funding_pnl=0.0` from v2 close/current PnL path.

At every current-PnL and close calculation use:

```
confirmed_funding_pnl = sum(idempotent reconciled funding ledger entries)
```

Do not include:

- PENDING;
- PUBLIC_RATE_CONFIRMED without mark;
- expected funding;
- provisional funding.

At every open-position poll calculate/store:

```
long_price_pnl =
    q * (current_long_close_vwap - long_entry_fill)

short_price_pnl =
    q * (short_entry_fill - current_short_close_vwap)

paper_net_if_exit_now =
    long_price_pnl
    + short_price_pnl
    + confirmed_funding_pnl
    - open_fees
    - estimated_current_close_fees
    - incurred_emergency_costs
```

Store current executable PnL in the latest observation and position runtime
state. Do not add separate `basis_pnl`.

5. PaperBot Integration

`PaperBot` must call `process_pending_reconciliations(now)` on a 10-second cadence
inside normal synchronized runtime loop. Use the injectable clock. Tests should
prove PaperBot calls the worker, not just helper functions.

6. Tests

Add end-to-end tests with FakeClock/fake provider/temp SQLite:

- PENDING reconciliation: no public event => no funding ledger and no balance
  change.
- Reconciliation success: public rate + mark => two leg funding values, one
  idempotent ledger event per leg, cycle reconciled.
- Duplicate reconciliation: same event twice => no second cash change.
- Reconciliation timeout after 600 seconds => cycle/closed position UNRECONCILED.
- Close includes confirmed funding.
- Current executable PnL includes confirmed funding, excludes pending/public-rate-only.
- PaperBot calls reconciliation worker on cadence.

Acceptance:

- production runtime diff is non-empty;
- `PaperBot` actually calls the reconciliation worker;
- no hardcoded `confirmed_funding_pnl=0.0` remains in `runtime_v2.py`;
- targeted tests pass.
