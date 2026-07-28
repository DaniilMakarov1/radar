IMPLEMENT NOW. EDIT PRODUCTION RUNTIME FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus, implementation engineer for Radar.

This is a production runtime completion pass. Do not return review-only notes.
Change production runtime code and add end-to-end tests using FakeClock, fake
routes/adapters, and temporary SQLite only. Do not run real bot/dashboard/live
execution. Do not read .env or secrets. Do not touch real database files.

Current confirmed defects in HEAD 1a634897a055e702823f3c4275db305d26c9cc3b:

- `smart_money_radar/funding/trader.py::process_synchronized_open_positions`
  changes hard-risk positions to `EMERGENCY_UNWIND` and continues, leaving paper
  exposure stranded.
- `smart_money_radar/paper_bot/runtime_v2.py::_execute_entry` uses
  `_synthetic_levels` for missing entry books.
- `runtime_v2.close_position` also uses synthetic/fallback full fills by entry
  price for normal close and hardcodes `confirmed_funding_pnl=0.0`.
- `_target_quantity` ignores venue quantity steps/minimums.
- `consider_route(..., accounts)` receives accounts but does not enforce account
  limits/collateral availability or write reserve/release ledger events.
- partial entry failures can leave position/cycle state inconsistent and do not
  create explicit unwind orders.
- `_entry_risk_gate` uses fake values `liquidation_distance_fraction=1.0` and
  `margin_safety_ratio=99.0`.

Implement only PASS 1 scope:

1. HARD-RISK MUST ACTUALLY CLOSE

In synchronized PaperBot runtime, a hard-risk event must not only set state.

Required flow:

- state -> `EXIT_SUBMITTED`;
- call a runtime emergency close method;
- create one exit order per leg;
- record actual simulated fill per leg;
- record close fees;
- record price PnL;
- record emergency unwind cost;
- close any residual exposure;
- final state `CLOSED_PENDING_RECONCILIATION`;
- position no longer appears as open exposure.

`EMERGENCY_UNWIND` is an intermediate state, not final open exposure.

Pricing rules:

- For fresh executable books, use execution haircut 0.40.
- If current snapshot unavailable, use last valid executable close snapshot
  stored for the position and apply adverse penalty 100 bps to both legs.
- If no valid executable snapshot ever existed, use last known mark and apply
  adverse penalty 300 bps; set `pricing_quality = "fallback_mark_300bps"` and
  exclude this episode from normal reconciled profitability metrics.
- Never close by entry price without adverse penalty.

2. PARTIAL ENTRY MUST UNWIND

If any fill ratio < 0.999, quantity mismatch > 0.001, or any fill after T-20:

- position never becomes `OPEN`;
- state path:
  `ENTRY_SUBMITTED -> PARTIALLY_HEDGED -> EMERGENCY_UNWIND -> FAILED`;
- create unwind order for each actually filled leg quantity;
- apply 100 bps adverse penalty;
- record entry fee;
- record unwind fee;
- record price PnL;
- record emergency cost;
- release collateral/reserves;
- do not create captured funding cycle;
- do not leave position in `ARMED`.

3. REMOVE SYNTHETIC ENTRY BOOK

Remove `_synthetic_levels` from normal entry. Missing asks/bids must reject with
`executable_orderbook_missing`. Normal close also must not create full fill from
entry price. Synthetic/fallback pricing is allowed only in documented emergency
close fallback described above.

4. REAL QUANTITY ROUNDING

Use:

```
q_raw = min(target_notional / long_open_price, target_notional / short_open_price)
```

Read `quantity_step` and `min_quantity`/`min_notional` from both route legs.
Translate both quantity steps to integer units on a common decimal scale, use LCM,
then:

```
common_step = LCM(long_step_units, short_step_units) / decimal_scale
q = floor(q_raw / common_step) * common_step
```

Validate both legs:

- `q >= min_quantity`;
- notional >= `min_notional`;
- 450 <= notional <= 500.

Missing step/minimum => no entry, research-only/entry rejection reason.

5. PAPER ACCOUNT ENFORCEMENT

The `accounts` parameter must be enforced.

Before entry, enforce:

- `max_open_positions_total = 1`;
- `max_open_positions_per_venue = 1`;
- `max_gross_exposure_usd = 1000`.

For each venue:

```
isolated_collateral = leg_notional / leverage
collateral_reserve = leg_notional * 0.25
required_available_cash = isolated_collateral + collateral_reserve + estimated_open_fee
```

At leverage=1 and leg_notional=500, required cash must be at least 625 + fee.

On entry:

- idempotently reserve collateral;
- reserve is not a fee expense;
- order fee is expense.

On failed entry and close:

- release collateral/reserve idempotently.

Add ledger/reservation keys:

- `collateral_reserve:<position_id>:<venue>`
- `collateral_release:<position_id>:<venue>`

Retry must not reserve or release collateral twice.

6. TESTS

Add end-to-end tests that drive `PaperBot`/`SynchronizedFundingRuntimeV2`, not
only helper functions:

- hard risk executes two exit orders, collateral release, price PnL ledger, final
  `CLOSED_PENDING_RECONCILIATION`, not stranded `EMERGENCY_UNWIND`;
- hard stale with no fresh book uses conservative emergency fallback and closes;
- partial entry long 100% / short 90% never opens, creates unwind order, records
  fees/loss/emergency cost, releases collateral, final state `FAILED`;
- late T-20 fill unwinds;
- missing entry book rejects with `executable_orderbook_missing`;
- quantity step common LCM example: long step 0.01, short step 0.025, q_raw 4.999
  => common_step 0.05, q 4.95;
- insufficient balance rejects with no entry orders;
- max position limit rejects second synchronized entry.

Run targeted tests before returning.

Acceptance:

- production runtime diff is non-empty;
- PaperBot actually calls the emergency close path;
- no normal entry can use synthetic books;
- accounts are not ignored;
- tests pass.
