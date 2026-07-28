# Model Instructions for Smart Money Radar

This document is the working contract for any AI model that edits or audits this
repository. It is intentionally more operational than the product docs: read it
before changing code, risk gates, venue adapters, paper bot behavior, Telegram
messages, or dashboard logic.

## 1. Current Product Scope

The active product is Funding Radar:

- deterministic public-data scanner for perpetual funding/spread opportunities;
- paper-only position lifecycle;
- local dashboard;
- Telegram notifications;
- CSV export for paper trade reports.

The older Prediction, Dune, wallet research, and on-chain radar work may still
have remnants in history or docs, but the current repo should stay funding-first
unless Daniil explicitly changes direction.

Do not enable live trading. Do not add exchange order placement. Do not treat
paper outcomes as proof of real executable alpha.

## 2. Agent Roles

Default workflow:

- Codex is architect, orchestrator, reviewer, and final risk owner.
- Qwen/QN is an implementation worker for scoped tasks.
- Other models should follow this same split unless Daniil explicitly says to
  work differently.

Implementation workers may edit files, run commands, add tests, and refactor
inside the requested scope. They must not independently change strategy
philosophy, risk gates, venue eligibility, funding-unit assumptions, live-trading
status, or architecture boundaries.

## 3. Repository Map

Important paths:

- `smart_money_radar/funding/` - venue adapters, scanner, forecasts, economics,
  route construction, liquidity and profiles.
- `smart_money_radar/paper_bot/` - paper entry, hold, settlement, close,
  spread monitoring, Telegram formatting.
- `smart_money_radar/storage.py` - SQLite schema and dashboard/report queries.
- `smart_money_radar/cli.py` - operational commands.
- `smart_money_radar/web/index.html` - local dashboard UI.
- `scripts/` - local start/stop/status/launchd helpers.
- `tests/test_funding_radar.py` - scanner/economics/adapters.
- `tests/test_funding_paper_trader.py` - bot lifecycle, entry/hold/close.
- `tests/test_funding_retention.py` - pruning and data retention.
- `QWEN.md` - short worker instructions.
- `docs/AGENT_HANDOFF.md` - handoff context and where raw transcripts live.

Before editing, read the relevant module and the matching tests. Do not patch by
memory.

## 4. Funding Data Contract

Every funding market row must obey this contract:

- `funding_rate`: cashflow rate for the venue's actual settlement interval.
- `funding_interval_hours`: actual settlement interval in hours.
- `hourly_funding_rate`: `funding_rate / funding_interval_hours`; this is for
  cross-venue comparison and projections, not necessarily what the UI displays.
- `next_funding_at`: next expected cashflow time.
- `funding_rate_kind`: audit label for the source and meaning of the rate.
- `published_funding_rate` and `published_funding_interval_hours`: optional UI
  fields when a venue displays a normalized rate that differs from the cashflow
  rate.

Never assume all venues publish the same unit. Some publish a per-settlement
rate, some publish hourly estimates, some publish an 8-hour equivalent, and some
update the estimate continuously inside a settlement interval. If units are
uncertain, fail closed with a visible risk flag rather than creating candidates.

Examples:

- RiseX currently uses `current_funding_rate` as the 1-hour cashflow rate and
  `funding_rate_8h` as display/audit metadata.
- Lighter publishes an 8-hour-equivalent display rate while the cashflow model
  stores hourly funding.
- OKX, Binance, Bybit, Aster, MEXC, and similar CEX venues may publish a current
  next-settlement estimate that can update differently from the cashflow
  settlement interval.

Any adapter change must add tests that prove:

- source units;
- settlement interval;
- `hourly_funding_rate`;
- `published_*` display behavior if relevant;
- depth unit conversion and contract multiplier handling.

## 5. Scanner and Candidate Contract

The scanner should screen the broad route universe without arbitrary top-N caps.
It may use cheap pre-depth arithmetic to avoid impossible full-depth work, but
only when a route cannot become profitable after adding real depth and costs.

Do not add hard route limits such as "top 50", "top 300", or "100 routes" as a
production gate. If a performance guard is necessary, it must be:

- explained as a temporary safety fuse;
- visible in diagnostics;
- tested;
- not able to silently hide otherwise positive routes.

Default candidate status is not "funding is positive" or "spread is positive"
by itself. The active paper strategy is:

```text
synchronized_funding_capture_v2
```

This is cross-venue delta-neutral funding capture:

- long perpetual on one venue;
- short perpetual on another venue;
- same canonical base quantity;
- entry reason is the nearest synchronized funding settlement;
- spread convergence is not expected profit;
- executable spread, book walking, fees, basis deterioration, stale data,
  liquidation/margin risk, and venue capability are costs/gates.

The initial economic model is:

```text
initial_expected_net_pnl =
  conservative_funding_gross
  - baseline_round_trip_book_cost
  - total_round_trip_fee_estimate
  - entry_basis_reserve_usd
  - entry_legging_reserve_usd
```

Entry requires:

```text
conservative_funding_gross >= max(2.50, reference_notional * 0.005)
initial_expected_net_pnl >= max(1.00, reference_notional * 0.002)
initial_cost_coverage_ratio >= 1.50
```

Entry history policy:

- `entry_history_required = False`.
- `entry_history_mode = "disabled"`.
- Long-term raw funding history, historical persistence, historical medians,
  historical win rate, and old forecast windows are not entry blockers.
- Focused pre-entry observations are mandatory, but they are current paired
  snapshots, not long-term history.
- Settlement reconciliation data is post-trade evidence and cannot create
  pre-entry funding cashflow.

Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any`
remain available for experimental/research profiles, but they are not the
default paper strategy and must not be shown as production-ready by default.

## 6. Paper Bot Runtime Modes

The default runtime is split between `smart_money_radar/funding/trader.py` and
`smart_money_radar/paper_bot/runtime_v2.py`.

Actual v2 call graph:

```text
PaperBot.run_*_iteration
  -> scanner / focused recheck
  -> PaperBot.process_synchronized_entry_candidates
  -> SynchronizedFundingRuntimeV2.consider_route
  -> strategy_synchronized_funding.entry_underwriting
  -> strategy_synchronized_funding.initial_entry_economics
  -> risk.entry_risk_gates
  -> execution.simulate_marketable_ioc / entry_fill_state / t20_deadline_passed
  -> accounting.order_fee_event_key / make_ledger_entry
  -> storage funding_capture_* + funding_paper_orders + paper_event_ledger

PaperBot.process_synchronized_open_positions
  -> risk hard/stale/dynamic-basis checks
  -> SynchronizedFundingRuntimeV2.mark_settlement_crossed
  -> settlement.build_settlement_crossing_rows
  -> SynchronizedFundingRuntimeV2.next_cycle_hold_or_close_decision
  -> cycle_manager.next_cycle_schedule_decision
  -> cycle_manager.next_cycle_observation_decision
  -> strategy_synchronized_funding.hold_economics
  -> SynchronizedFundingRuntimeV2.close_position when hold fails
```

Legacy `position.py` accounting and `funding_paper_positions` remain for old
profiles and old reports. They are not the source of truth for
`synchronized_funding_capture_v2`.

The intended runtime cadence is unchanged:

- P0 open v2/legacy positions: cancel background full scan, refresh involved
  venues, run risk/hold/close, reconciliation, and equity snapshot, then return;
- P1 critical hot routes at <=120 seconds to settlement: cancel background scan
  and run due hot rechecks before discovery;
- P2 due reconciliation;
- P3 normal focused watch-route rechecks;
- P4 lightweight discovery;
- P5 background full scan only when no open position and no hot route exists.

Cadence:

- no hot routes: background full market scan every `scan_interval_seconds` after
  completion;
- hot/watch route exists: focused recheck every `monitor_interval_seconds`;
- urgent route, pending settlement, or open position: focused recheck every
  `hot_interval_seconds`;
- status Telegram report: every `status_report_interval_seconds`, or disabled if
  the value is `0`.

Full market scan can run in the background while hot rechecks continue. It must
not call blocking `run_full_iteration()` from the scheduler, must not wait on an
unfinished future, must not clear hot routes, and must not overwrite a newer
focused snapshot.

## 7. Scanner, Watch, and Entry Lifecycle

For `synchronized_funding_capture_v2`, the full scanner is not authoritative for
entry. It can output only:

- `research_only` when capability/contract/collateral/funding semantics fail;
- `rejected` when a route is structurally impossible;
- `watch` when a route is worth focused observation.

The scanner must not create a final `paper_candidate` for synchronized funding.
The dashboard may display actionable `watch` routes in its main table, but the
row status must remain `watch` until focused underwriting opens a paper position.

Entry can happen only through `SynchronizedFundingRuntimeV2.consider_route`.
Required conditions:

1. Both venues pass the fail-closed capability contract.
2. Collateral and quote assets match and are one of `USDT`, `USDC`, or `USD`.
3. Both legs publish a normalized next-settlement funding rate with
   `positive_long_pays` sign convention.
4. Both `next_funding_at` timestamps align within 1 second.
5. Current lead is inside T-35 to T-25 seconds, target T-30.
6. Focused observations contain at least 10 valid paired snapshots over at least
   20 seconds.
7. Latest observation age is at most 2 seconds and response skew at most 1
   second.
8. All observed gross funding PnL values are positive and latest gross is at
   least 80% of the median.
9. Conservative funding is `0.90 * min(observed gross funding)`.
10. Spread convergence is exactly `0.0`; executable spread/basis is cost/risk.
11. Initial economics pass gross, net, and 1.50x coverage gates.
12. Two marketable IOC paper fills succeed with at least 99.9% fill on each leg
    and quantity mismatch at most 0.1%.
13. Both simulated fills complete no later than T-20.

`ENTRY_SUBMITTED -> OPEN` is forbidden without two fills. Partial fill produces
`PARTIALLY_HEDGED`/unwind evidence and does not create an open v2 position.

## 8. Settlement, Hold, and Close Lifecycle

Every settlement is a cycle. Crossing a scheduled funding timestamp creates two
`funding_settlement_reconciliations` rows with `PENDING` status. It does not add
funding PnL, account cashflow, equity, win rate, or reconciled profitability.

If public history is missing:

- funding rate remains `NULL`;
- funding PnL remains `NULL`;
- reconciliation stays `PENDING` or `UNRECONCILED`;
- paper balance and `paper_net_if_exit_now` do not include that funding.

Normal close is forbidden before T+20. Hold/close evaluation happens around
T+30 using the next exact timestamps, not `funding_interval_hours` equality.

Hold is allowed only if:

- next long and short funding timestamps align within 1 second;
- next settlement is 300-14,400 seconds away;
- at least 15 valid next-cycle observations cover at least 20 seconds;
- all next-cycle gross funding values are positive and stable;
- incremental hold economics pass gross, net, and 1.50x coverage gates;
- opening fees are treated as sunk costs;
- projected total after the next cycle is non-negative;
- captured settlements remain below 4 and projected age remains at most 14,700
  seconds;
- hard risk gates pass.

Before those economics are accepted, apply hold history reliability:

```text
history_adjusted_next_funding =
  next_conservative_funding_gross * history_multiplier
```

The history sample is local-only and scoped to the same canonical asset,
directed long venue, directed short venue, collateral asset, and next-settlement
wait bucket (`<=1h`, `>1h..<=2h`, `>2h..<=4h`). It uses only fully reconciled
prior hold cycles, never raw exchange funding history.

If valid cycles `< 8`, history is insufficient: multiplier `0.75`, no automatic
veto, and at most one additional settlement may be held. If valid cycles `>= 8`,
hold fails when positive realization rate `< 0.70` or p25 realization ratio
`< 0.50`; otherwise the multiplier is `clamp(0.50, 1.00, p25 ratio)`.

Good history cannot override negative current funding, negative current
executable PnL, stale data, liquidity failure, basis risk, schedule mismatch, or
hard risk.

Hold does not wait for the prior cycle's reconciliation. The position can be
`HOLDING_NEXT_CYCLE` while previous public funding rows are still pending.

Close uses two simulated reduce-only exit orders. Price PnL is:

```text
long price PnL  = q * (long_exit_fill - long_entry_fill)
short price PnL = q * (short_entry_fill - short_exit_fill)
paper_price_pnl = long price PnL + short price PnL
```

Do not add a separate basis PnL on top. Basis movement is already inside long
plus short price PnL.

Risk exits:

- stale data after retries;
- dynamic basis deterioration over the active funding-derived budget;
- executable PnL below the active risk budget after fresh snapshots;
- liquidation/margin/mark-index hard gates;
- venue/schedule/contract failure.

A common 10% move in both legs is telemetry and forces fresh risk calculation;
it is not by itself an automatic close.

## 9. PnL Accounting

Funding cashflow signs:

```text
long leg PnL  = -notional * funding_rate
short leg PnL =  notional * funding_rate
```

Therefore:

- positive funding: longs pay, shorts receive;
- negative funding: longs receive, shorts pay.

Current executable PnL for an open position must exclude pending future funding:

```text
paper_net_if_exit_now =
  current_long_price_pnl
  + current_short_price_pnl
  + confirmed_funding_pnl
  - paper_open_fees
  - current_close_fees
  - emergency_unwind_costs_already_incurred
```

Do not include expected future funding, provisional settlement estimates, or
spread convergence in `paper_net_if_exit_now`.

Closed/reconciled result:

```text
paper_net_pnl_reconciled =
  paper_long_price_pnl
  + paper_short_price_pnl
  + sum(reconciled_cycle_funding_pnl)
  - paper_open_fees
  - paper_close_fees
  - paper_emergency_unwind_cost
```

The older dashboard/storage layer still contains compatibility fields such as
`actual_net_pnl`. New accounting code should prefer the `paper_*` names and
should not treat pending funding as confirmed PnL.

## 10. Venue Rules

Risky or disabled venues live in `smart_money_radar/funding/venues.py`. Do not
resurrect a disabled venue by only changing the UI. A venue is active only when:

- its adapter is registered;
- it is not in `DEACTIVATED_FUNDING_VENUES`;
- funding units are tested;
- contract/base quantity units are tested;
- depth units are tested;
- failure mode is fail-closed;
- it has regression tests.

Default profile uses `venue_set=None`, meaning all active registered adapters are
scanned automatically. Do not replace this with a hardcoded production list.

For `synchronized_funding_capture_v2`, active is still not enough. A venue is
paper-eligible only if it supports perpetuals, linear contracts, USDT/USDC/USD
collateral, discrete next-settlement funding, next funding timestamp, mark/index
prices, executable orderbook depth, 24h quote volume, open interest, taker fee,
quantity step, and min notional. Otherwise it remains diagnostics/research-only
with an explicit reason.

Do not infer critical capability fields from indirect clues. `contract_type`,
matching collateral/quote assets, `funding_rate_kind`, or a present
`normalized_next_funding_rate` are not enough. Paper eligibility requires explicit
`contract_kind`, `funding_rate_semantics`, `funding_rate_unit`,
`funding_sign_convention`, and `supports_discrete_funding`. Unknown means
research-only until an adapter-level contract is declared and tested.

For CLOB venues, depth must be modeled by walking all returned levels needed for
the target base quantity. Do not use only top-of-book size. Do not use a fixed
BPS cutoff as a hard gate. Large book walks may receive risk flags, but if the
VWAP math still leaves positive net, the route can remain visible.

For pool-based DEX perps such as Jupiter Perps or GMX, do not pretend they are
CLOB venues. They need separate pool-perps economics: utilization, borrow fees,
position caps, price impact, keeper/network fees, collateral constraints, and
pool imbalance.

## 11. Data Retention and Runtime Data

The system should preserve paper trade history and compact evidence needed to
understand decisions. It should not accumulate unlimited scan diagnostics.

Keep:

- paper positions;
- paper events needed to explain opens, holds, closes, crashes, and Telegram;
- latest useful scan snapshots;
- compact funding history needed for forecasts.

Prune:

- stale route universes;
- stale orderbook snapshots;
- stale market snapshots;
- old routine scan/status events;
- old diagnostic raw JSON unless explicitly debugging.

Retention must never break paper-position foreign keys. If pruning fails with a
SQLite lock or FK issue, the trading loop should continue and record a warning.

## 12. Dashboard and Telegram Rules

The dashboard and Telegram should show decision-relevant information only:

- active candidates with positive actionable PnL;
- funding rate per leg and interval;
- published/display rate if it differs from cashflow rate;
- next settlement timing;
- selected edge label;
- funding component, spread component, fees/slippage/reserves when relevant;
- clear close reason with details.

Do not show negative-PnL routes as candidates. They can be diagnostics only.

Funding bot must use funding Telegram credentials:

- `FUNDING_TELEGRAM_BOT_TOKEN`;
- `FUNDING_TELEGRAM_CHAT_ID`.

Prediction or other bots must not send funding status messages.

## 13. Testing Requirements

Run focused tests for the area changed. For funding arithmetic, adapter units,
entry/hold/close behavior, or route filtering, run at least:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_radar.py tests/test_funding_paper_trader.py tests/test_synchronized_funding_v2.py -q
```

For retention or storage changes, also run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_retention.py -q
```

Before claiming the system is working, run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

If tests cannot run, state exactly why and what residual risk remains.

## 14. Common Failure Patterns

Be especially suspicious of:

- funding values that are 4x or 8x too large;
- UI multiplying an already per-interval rate by the interval again;
- `published_funding_rate` being used for cashflow PnL;
- route filters that separately demand "good funding" and "good spread" instead
  of evaluating total net;
- old history acting as a hard veto for a strong next-settlement live
  opportunity;
- full market scans blocking urgent focused checks;
- stale snapshots being used as fresh entry data;
- common 10% price moves being treated as automatic close instead of telemetry;
- spread-only routes ignoring funding drag;
- funding-only routes ignoring spread/basis drag;
- disabled venues appearing in candidates, paper accounts, or Telegram;
- contract-unit aliases such as `1000TOKEN`, `kTOKEN`, or venue-specific lot
  units being compared as if they were the same base quantity.

## 15. Change Reporting

Every model should report:

- files changed;
- behavior changed;
- tests run and results;
- risks that remain;
- whether the implementation matches the intended trading logic or changes it.

If a model is uncertain whether a trading/risk behavior should change, it should
list the uncertainty instead of silently choosing a more aggressive rule.
